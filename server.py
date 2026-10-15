import json
import logging
import math
import os
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
import pandas as pd
import yfinance as yf
from mcp.server.fastmcp import FastMCP

# Import Pydantic models
from models.models import (
    DailyBar,
    PriceData,
    FundamentalsData,
    NewsData,
    NewsItem,
)

mcp = FastMCP(
    "news_server",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stock-mcp")

# Threshold constants
LOW_VOLUME_THRESHOLD = 50_000
STALE_DAYS = 7

# A daily bar for a session that is still trading is partial (tiny volume, moving close). Leave it out so
# latest_close, volatility and drawdown are computed on finished sessions only.
EXCLUDE_LIVE_BAR = True
MAX_PLAUSIBLE_YIELD_PCT = 30.0
NEWS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---- Yahoo resilience settings ----
INFO_TTL_SECONDS = 600          # ticker.info is cached per symbol and shared by every tool
INFO_ATTEMPTS = 3               # total tries when info is empty or Yahoo answers 429
HISTORY_ATTEMPTS = 3
BACKOFF_SECONDS = 1.5           # waits ~1.5s, then ~3s (plus jitter) between tries
# If none of these are present, the info dict is a blocked/thin response, not real data.
INFO_PROOF_KEYS = ("marketCap", "sector", "longName", "beta", "trailingPE", "forwardPE",
                   "regularMarketPrice", "currentPrice")


############# Helpers ##############

def _fetch_news_data(query: str, days: int, max_items: int) -> NewsData:
    """Shared Google News RSS fetcher returning a NewsData model."""
    try:
        days = max(1, min(days, 30))
        max_items = max(1, min(max_items, 10))

        search = quote(f"{query} when:{days}d")
        url = f"https://news.google.com/rss/search?q={search}&hl=en-IN&gl=IN&ceid=IN:en"

        req = urllib.request.Request(url, headers={"User-Agent": NEWS_USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()

        feed = feedparser.parse(raw)

        if not feed.entries:
            return NewsData(query=query, items=[])

        items = []
        for e in feed.entries:
            if not getattr(e, "published_parsed", None):
                continue
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            source = e.get("source", {}).get("title", "Unknown source")
            items.append(
                NewsItem(
                    published=published.isoformat(),
                    title=e.title,
                    source=source,
                )
            )

        items.sort(key=lambda x: x.published, reverse=True)
        return NewsData(query=query, items=items[:max_items])

    except Exception:
        return NewsData(query=query, items=[])


def _num(val, digits: int = 2) -> float | None:
    """A rounded finite float, or None for None / NaN / inf / non-numbers."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


def _to_pct(val: float | None) -> float | None:
    """Converts a fractional value (0.15) to a plain percentage number (15.0)."""
    n = _num(val, 6)
    return None if n is None else round(n * 100.0, 2)


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:150]}"


def _sleep_backoff(attempt: int) -> None:
    time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1) + random.uniform(0, 0.5))


# ---- ticker.info: one cached, retried fetch per symbol, shared by both stock tools ----

_info_cache: dict[str, tuple[float, dict]] = {}
_info_locks: dict[str, threading.Lock] = {}
_info_guard = threading.Lock()


def _info_usable(info) -> bool:
    return isinstance(info, dict) and any(info.get(k) is not None for k in INFO_PROOF_KEYS)


def _cached_info(symbol: str) -> dict | None:
    hit = _info_cache.get(symbol)
    if hit and time.monotonic() - hit[0] < INFO_TTL_SECONDS:
        return hit[1]
    return None


def _get_info(symbol: str) -> tuple[dict, str | None]:
    """Returns (info, error). Cached ~10 min per symbol. Retries with backoff when Yahoo returns an empty/thin
    dict or raises (429 etc.). Failures are never cached, so the next call tries Yahoo again."""
    cached = _cached_info(symbol)
    if cached is not None:
        return cached, None

    with _info_guard:
        lock = _info_locks.setdefault(symbol, threading.Lock())

    with lock:                                   # concurrent callers for one symbol wait and share the result
        cached = _cached_info(symbol)
        if cached is not None:
            return cached, None

        err = "unknown error"
        for attempt in range(1, INFO_ATTEMPTS + 1):
            try:
                info = yf.Ticker(symbol).info
                if _info_usable(info):
                    if len(_info_cache) > 500:
                        _info_cache.clear()
                    _info_cache[symbol] = (time.monotonic(), info)
                    if attempt > 1:
                        log.info("%s: info OK on attempt %d/%d", symbol, attempt, INFO_ATTEMPTS)
                    return info, None
                err = "Yahoo returned an empty response (likely rate limited)"
                log.warning("%s: info empty on attempt %d/%d (%d keys)", symbol, attempt, INFO_ATTEMPTS,
                            len(info or {}))
            except Exception as e:
                err = _err(e)
                log.warning("%s: info raised on attempt %d/%d: %s", symbol, attempt, INFO_ATTEMPTS, err)
            if attempt < INFO_ATTEMPTS:
                _sleep_backoff(attempt)
        return {}, err


# ---- price history with retry ----

def _fetch_history(symbol: str):
    """Returns (ticker, df, error). df has NaN-price rows removed and at least 2 rows when error is None."""
    err = "unknown error"
    for attempt in range(1, HISTORY_ATTEMPTS + 1):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="1y")
            if df is not None and not df.empty:
                # Yahoo can return a row with NaN prices (e.g. an unfinished session); drop it.
                df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if df is not None and len(df) >= 2:
                return ticker, df, None
            err = "Yahoo returned no usable price history"
            log.warning("%s: history empty on attempt %d/%d", symbol, attempt, HISTORY_ATTEMPTS)
        except Exception as e:
            err = _err(e)
            log.warning("%s: history raised on attempt %d/%d: %s", symbol, attempt, HISTORY_ATTEMPTS, err)
        if attempt < HISTORY_ATTEMPTS:
            _sleep_backoff(attempt)
    return None, None, err


def _session_is_live(ticker: "yf.Ticker") -> bool:
    """True while the exchange is in its pre-market or regular session. Read from the chart metadata that
    history() already fetched, so it costs no extra Yahoo request (no ticker.info call)."""
    try:
        period = (ticker.history_metadata or {}).get("currentTradingPeriod") or {}
        start = (period.get("pre") or period.get("regular") or {}).get("start")
        end = (period.get("regular") or {}).get("end")
        return bool(start and end and start <= time.time() < end)
    except Exception:
        return False


def _drop_live_bar(df: pd.DataFrame, ticker: "yf.Ticker") -> pd.DataFrame:
    """Remove today's bar while the exchange is still open (or not yet open)."""
    if not EXCLUDE_LIVE_BAR or df.empty or not _session_is_live(ticker):
        return df
    tz = getattr(df.index, "tz", None)
    today = datetime.now(tz).date() if tz is not None else datetime.now().date()
    if df.index[-1].date() == today:
        return df.iloc[:-1]
    return df


def _last_price(ticker: "yf.Ticker", info: dict) -> float | None:
    for key in ("currentPrice", "regularMarketPrice", "previousClose"):
        px = _num(info.get(key), 6)
        if px:
            return px
    try:
        return _num(ticker.fast_info.get("last_price"), 6) or None
    except Exception:
        return None


def _dividend_yield_pct(ticker: "yf.Ticker", info: dict, price: float | None) -> float | None:
    """Trailing dividend yield in percent. Uses the quoted rate, else the dividends actually paid in 12 months."""
    if not price:
        return None
    pct = None
    rate = _num(info.get("dividendRate"), 6) or _num(info.get("trailingAnnualDividendRate"), 6)
    if rate and rate > 0:
        pct = rate / price * 100
    else:
        try:
            divs = ticker.dividends
            if divs is not None and len(divs):
                cutoff = pd.Timestamp.now(tz=divs.index.tz) - pd.Timedelta(days=365)
                paid = float(divs[divs.index >= cutoff].sum())
                if paid > 0:
                    pct = paid / price * 100
        except Exception:
            log.exception("dividend history lookup failed")
    if pct is None or not 0 < pct <= MAX_PLAUSIBLE_YIELD_PCT:
        return None
    return round(pct, 2)


############# News and Weather Tools ##############

@mcp.tool()
def get_stock_news(query: str, days: int = 7, max_items: int = 6) -> str:
    """
    Fetch recent news headlines about a company or stock as structured JSON.
    Pass the COMPANY NAME plus the word 'stock' (e.g. 'State Bank of India stock'),
    not the ticker symbol.
    """
    data = _fetch_news_data(query, days, max_items)
    return data.model_dump_json()


@mcp.tool()
def get_general_news(topic: str, days: int = 3, max_items: int = 6) -> str:
    """
    Fetch recent news headlines on a general, non-company topic as structured JSON.
    """
    data = _fetch_news_data(topic, days, max_items)
    return data.model_dump_json()


@mcp.tool()
def get_weather(location: str) -> str:
    """Get current weather for a location as structured JSON."""
    result = {
        "location": location,
        "weather": "15°C and rainy" if "delhi" in location.lower() else "22°C and sunny"
    }
    return json.dumps(result)


############# Stock Tools ##############

@mcp.tool()
def search_ticker(query: str, max_results: int = 5) -> str:
    """
    Look up correct ticker symbols for a company name as structured JSON.
    Use this BEFORE price/fundamentals tools whenever unsure of exact ticker symbol.
    """
    try:
        max_results = max(1, min(max_results, 10))
        quotes = yf.Search(query, max_results=max_results).quotes or []

        candidates = []
        for q in quotes:
            if q.get("quoteType") in ("EQUITY", "ETF"):
                candidates.append({
                    "symbol": q.get("symbol"),
                    "name": q.get("longname") or q.get("shortname"),
                    "exchange": q.get("exchDisp") or q.get("exchange"),
                    "type": q.get("quoteType"),
                })
        return json.dumps({"query": query, "candidates": candidates})
    except Exception as e:
        return json.dumps({"query": query, "candidates": [], "error": str(e)})


@mcp.tool()
def getStock_price_info(stock: str, days: int = 5) -> str:
    """
    Fetch recent daily OHLCV rows and annualized risk metrics as structured PriceData JSON.
    'days' = recent trading days to list (1-30). Risk metrics use 1-year window of finished sessions.
    If the fetch failed, the JSON has a non-null "error" field.
    """
    clean_stock = stock.strip().upper()
    try:
        days = max(1, min(days, 30))
        ticker, df, err = _fetch_history(clean_stock)
        if err:
            log.error("%s: price fetch FAILED: %s", clean_stock, err)
            return PriceData(ticker=clean_stock, error=err).model_dump_json()

        df = _drop_live_bar(df, ticker)
        if len(df) < 2:
            log.warning("%s: fewer than 2 finished sessions", clean_stock)
            return PriceData(ticker=clean_stock, error="fewer than 2 finished trading sessions").model_dump_json()

        volume = df["Volume"].fillna(0)
        bars = [
            DailyBar(
                date=idx.strftime("%Y-%m-%d"),
                open=round(float(row["Open"]), 2),
                high=round(float(row["High"]), 2),
                low=round(float(row["Low"]), 2),
                close=round(float(row["Close"]), 2),
                volume=int(vol),
            )
            for (idx, row), vol in zip(df.tail(days).iterrows(), volume.tail(days))
        ]

        returns = df["Close"].pct_change().dropna()
        volatility = returns.std() * (252 ** 0.5) * 100
        running_max = df["Close"].cummax()
        drawdown = (df["Close"] / running_max - 1) * 100

        data = PriceData(
            ticker=clean_stock,
            latest_close=_num(df["Close"].iloc[-1]),
            high_52w=_num(df["High"].max()),
            low_52w=_num(df["Low"].min()),
            volatility_pct=_num(volatility) if not returns.empty else None,
            max_drawdown_pct=_num(drawdown.min()),
            current_drawdown_pct=_num(drawdown.iloc[-1]),
            avg_volume=int(volume.mean()),
            recent_bars=bars,
        )
        return data.model_dump_json()

    except Exception as e:
        log.exception("getStock_price_info failed for %s", clean_stock)
        return PriceData(ticker=clean_stock, error=_err(e)).model_dump_json()


@mcp.tool()
def getStock_fundamentals(stock: str) -> str:
    """
    Fetch valuation, profitability, growth, dividend, and debt metrics as structured FundamentalsData JSON.
    If the fetch failed, the JSON has a non-null "error" field and all metrics are null.
    """
    clean_stock = stock.strip().upper()

    info, err = _get_info(clean_stock)
    if err:
        log.error("%s: fundamentals fetch FAILED: %s", clean_stock, err)
        return FundamentalsData(ticker=clean_stock, error=f"Yahoo info unavailable: {err}").model_dump_json()

    try:
        ticker = yf.Ticker(clean_stock)       # no network call yet; only used by the fallbacks below

        de_raw = _num(info.get("debtToEquity"), 6)
        debt_to_equity = round(de_raw / 100.0, 2) if de_raw is not None else None

        market_cap = _num(info.get("marketCap"), 0)
        if market_cap is None:
            try:
                market_cap = _num(ticker.fast_info.get("market_cap"), 0)
            except Exception:
                market_cap = None

        data = FundamentalsData(
            ticker=clean_stock,
            company_name=info.get("longName"),
            sector=info.get("sector"),
            market_cap=market_cap,
            beta=_num(info.get("beta")),
            trailing_pe=_num(info.get("trailingPE")),
            forward_pe=_num(info.get("forwardPE")),
            price_to_book=_num(info.get("priceToBook")),
            roe_pct=_to_pct(info.get("returnOnEquity")),
            profit_margin_pct=_to_pct(info.get("profitMargins")),
            revenue_growth_pct=_to_pct(info.get("revenueGrowth")),
            earnings_growth_pct=_to_pct(info.get("earningsGrowth")),
            debt_to_equity=debt_to_equity,
            free_cash_flow=_num(info.get("freeCashflow"), 0),
            dividend_yield_pct=_dividend_yield_pct(ticker, info, _last_price(ticker, info)),
        )
        filled = sum(1 for k, v in data.model_dump().items() if k not in ("ticker", "error") and v is not None)
        log.info("%s: fundamentals OK, %d fields populated", clean_stock, filled)
        return data.model_dump_json()

    except Exception as e:
        log.exception("getStock_fundamentals failed for %s", clean_stock)
        return FundamentalsData(ticker=clean_stock, error=_err(e)).model_dump_json()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
