import json
import logging
import math
import os
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
LIVE_MARKET_STATES = {"REGULAR", "PRE", "PREPRE"}
MAX_PLAUSIBLE_YIELD_PCT = 30.0
NEWS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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


def _drop_live_bar(df: pd.DataFrame, ticker: "yf.Ticker") -> pd.DataFrame:
    """Remove today's bar while the exchange is still open (or not yet open)."""
    if not EXCLUDE_LIVE_BAR or df.empty:
        return df
    try:
        state = str((ticker.info or {}).get("marketState", "")).upper()
    except Exception:
        return df
    if state not in LIVE_MARKET_STATES:
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
    """
    clean_stock = stock.strip().upper()
    try:
        days = max(1, min(days, 30))
        ticker = yf.Ticker(clean_stock)
        df = ticker.history(period="1y")

        # Yahoo can return a row with NaN prices (e.g. an unfinished session); drop it so nothing is computed from it.
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = _drop_live_bar(df, ticker)

        if len(df) < 2:
            log.warning("%s: no usable price history", clean_stock)
            return PriceData(ticker=clean_stock).model_dump_json()

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

    except Exception:
        log.exception("getStock_price_info failed for %s", clean_stock)
        return PriceData(ticker=clean_stock).model_dump_json()


@mcp.tool()
def getStock_fundamentals(stock: str) -> str:
    """
    Fetch valuation, profitability, growth, dividend, and debt metrics as structured FundamentalsData JSON.
    """
    clean_stock = stock.strip().upper()
    try:
        ticker = yf.Ticker(clean_stock)
        info = ticker.info or {}

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
        return data.model_dump_json()

    except Exception:
        log.exception("getStock_fundamentals failed for %s", clean_stock)
        return FundamentalsData(ticker=clean_stock).model_dump_json()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")