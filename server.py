import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
import yfinance as yf
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "news_server",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
)

# Below this average daily volume a listing is treated as illiquid / possibly the wrong symbol
LOW_VOLUME_THRESHOLD = 50_000
# If the newest bar is older than this many days, the data is considered stale
STALE_DAYS = 7
# Google News tends to reject the default feedparser/urllib agent from cloud hosts
NEWS_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


############# Helpers ##############

def _fetch_news(query: str, days: int, max_items: int, label: str) -> str:
    """Shared Google News RSS fetcher used by both news tools."""
    try:
        days = max(1, min(days, 30))
        max_items = max(1, min(max_items, 10))

        search = quote(f"{query} when:{days}d")
        url = f"https://news.google.com/rss/search?q={search}&hl=en-IN&gl=IN&ceid=IN:en"
        # Fetch ourselves (User-Agent + timeout) instead of feedparser's default fetcher,
        # which Google often blocks, and so we can report the real failure reason.
        try:
            req = urllib.request.Request(url, headers={"User-Agent": NEWS_USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            return f"Error: news feed request failed with HTTP {e.code} ({e.reason})."
        except Exception as e:
            return f"Error: could not reach the news feed ({type(e).__name__}: {e})."

        feed = feedparser.parse(raw)

        if getattr(feed, "bozo", 0) and not feed.entries:
            reason = getattr(feed, "bozo_exception", "unknown parse error")
            return f"Error: could not parse the news feed ({reason})."

        if not feed.entries:
            return f"No news found for '{query}' in the last {days} days."

        now = datetime.now(timezone.utc)
        items = []
        for e in feed.entries:
            if not getattr(e, "published_parsed", None):
                continue
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            source = e.get("source", {}).get("title", "Unknown source")
            items.append((published, e.title, source))

        if not items:
            return f"No dated news found for '{query}' in the last {days} days."

        items.sort(key=lambda x: x[0], reverse=True)

        lines = [f"Latest {label} for '{query}' (last {days} days, newest first):"]
        for published, title, source in items[:max_items]:
            hours = int((now - published).total_seconds() // 3600)
            age = f"{hours}h ago" if hours < 48 else f"{hours // 24}d ago"
            lines.append(f"- [{published:%Y-%m-%d %H:%M} UTC, {age}] {title} ({source})")

        return "\n".join(lines)

    except Exception as e:
        return f"Error executing news tool: {str(e)}"


############# News and Weather Tools ##############

@mcp.tool()
def get_stock_news(query: str, days: int = 7, max_items: int = 6) -> str:
    """
    Fetch recent news headlines about a company or stock, newest first, each with its date and source.
    Pass the COMPANY NAME plus the word 'stock' (e.g. 'State Bank of India stock'),
    not the ticker symbol, because news search works better with names.
    'days' = how far back to look (1-30). 'max_items' = how many headlines to return (1-10).
    """
    return _fetch_news(query, days, max_items, label="news")


@mcp.tool()
def get_general_news(topic: str, days: int = 3, max_items: int = 6) -> str:
    """
    Fetch recent news headlines on a general, non-company topic (world events, economy,
    policy, sectors, commodities), newest first, each with its date and source.
    'days' = how far back to look (1-30). 'max_items' = how many headlines to return (1-10).
    """
    return _fetch_news(topic, days, max_items, label="headlines")


@mcp.tool()
def get_weather(location: str) -> str:
    """Get the current weather for a given location. (Demo stub, returns fixed values.)"""
    if "delhi" in location.lower():
        return "It's 15°C and rainy."
    return "It's 22°C and sunny."


############# Stock Tools ##############

@mcp.tool()
def search_ticker(query: str, max_results: int = 5) -> str:
    """
    Look up the correct ticker symbol for a company name (e.g. 'Suzuki Motor', 'Maruti Suzuki').
    Returns candidate symbols with company name, exchange and type.
    Use this BEFORE the price/fundamentals tools whenever you are not 100% sure of the exact
    symbol, and pick the listing on the primary exchange (NSE for Indian companies).
    """
    try:
        max_results = max(1, min(max_results, 10))
        quotes = yf.Search(query, max_results=max_results).quotes

        if not quotes:
            return f"No ticker matches found for '{query}'."

        equities = [q for q in quotes if q.get("quoteType") in ("EQUITY", "ETF")]
        quotes = equities or quotes

        lines = [f"Ticker candidates for '{query}':"]
        for q in quotes:
            name = q.get("longname") or q.get("shortname") or "N/A"
            lines.append(
                f"- {q.get('symbol', 'N/A')} | {name} | "
                f"Exchange: {q.get('exchDisp') or q.get('exchange', 'N/A')} | "
                f"Type: {q.get('quoteType', 'N/A')}"
            )
        lines.append("Verify exchange and currency match the company you want before using a symbol.")
        return "\n".join(lines)

    except Exception as e:
        return f"Error executing ticker search: {str(e)}"


@mcp.tool()
def getStock_price_info(stock: str, days: int = 5) -> str:
    """
    Fetch recent daily OHLCV rows plus risk metrics (annualized volatility, max drawdown,
    current drawdown, 52-week range) for technical analysis. Also reports exchange, currency
    and data-quality warnings (illiquid / stale data).
    'days' = how many recent trading days to list (1-30). Risk metrics always use the last 1 year.
    """
    try:
        days = max(1, min(days, 30))
        clean_stock = stock.strip().upper()
        ticker = yf.Ticker(clean_stock)
        df = ticker.history(period="1y")

        if df.empty:
            return (
                f"Error: No trading data available for ticker '{clean_stock}'. "
                f"Use search_ticker to find the correct symbol."
            )

        meta = getattr(ticker, "history_metadata", None) or {}
        currency = meta.get("currency", "N/A")
        exchange = meta.get("fullExchangeName") or meta.get("exchangeName") or "N/A"

        # Recent rows to display
        recent = df.tail(days)
        lines = [
            f"Recent OHLCV for {clean_stock} (Exchange: {exchange}, Currency: {currency}, "
            f"last {len(recent)} trading days):"
        ]
        for date, row in recent.iterrows():
            date_str = date.strftime('%Y-%m-%d')
            lines.append(
                f"- Date: {date_str} | Open: {row['Open']:.2f} | High: {row['High']:.2f} | "
                f"Low: {row['Low']:.2f} | Close: {row['Close']:.2f} | Volume: {int(row['Volume'])}"
            )

        # Risk metrics over the full 1 year
        returns = df['Close'].pct_change().dropna()
        volatility = returns.std() * (252 ** 0.5) * 100          # annualized, in %
        running_max = df['Close'].cummax()
        drawdown = (df['Close'] / running_max - 1) * 100         # % below the peak so far
        max_drawdown = drawdown.min()
        current_drawdown = drawdown.iloc[-1]

        last_close = df['Close'].iloc[-1]
        high_52 = df['High'].max()
        low_52 = df['Low'].min()
        avg_volume = df['Volume'].mean()

        lines.append("\nRisk & Range Metrics (last 1 year):")
        lines.append(f"- Latest Close: {last_close:.2f} {currency}")
        lines.append(f"- 52-Week High: {high_52:.2f}")
        lines.append(f"- 52-Week Low: {low_52:.2f}")
        lines.append(f"- Annualized Volatility: {volatility:.2f}%")
        lines.append(f"- Max Drawdown: {max_drawdown:.2f}%")
        lines.append(f"- Current Drawdown from Peak: {current_drawdown:.2f}%")
        lines.append(f"- Average Daily Volume: {int(avg_volume)}")

        # Data-quality warnings so the model doesn't present bad data as fact
        warnings = []
        if avg_volume < LOW_VOLUME_THRESHOLD:
            warnings.append(
                f"Average daily volume is very low ({int(avg_volume)} shares). This looks like an "
                f"illiquid listing or the wrong symbol; prices and risk metrics may not be meaningful."
            )
        last_dt = df.index[-1].to_pydatetime()
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - last_dt).days
        if age_days > STALE_DAYS:
            warnings.append(f"Latest data point is {age_days} days old; the stock may be delisted or stale.")

        if warnings:
            lines.append("\n⚠️ DATA QUALITY WARNING:")
            lines.extend(f"- {w}" for w in warnings)
            lines.append("- Verify the symbol with search_ticker and tell the user the data may be unreliable.")

        return "\n".join(lines)

    except Exception as e:
        return f"Error executing price tool: {str(e)}"


@mcp.tool()
def getStock_fundamentals(stock: str) -> str:
    """
    Fetch core fundamental metrics: valuation (P/E, P/B), profitability (ROE, margin),
    growth (revenue, earnings), financial health (debt-to-equity, FCF), dividend yield, beta and market cap.
    Use this when evaluating long-term intrinsic value or financial health.
    Reports a data-quality warning when most metrics are missing.
    """
    try:
        clean_stock = stock.strip().upper()
        ticker = yf.Ticker(clean_stock)
        info = ticker.info

        if not info or len(info) <= 5:  # yfinance returns a tiny fallback dict if invalid
            return (
                f"Error: Could not retrieve fundamental analysis metrics for ticker '{clean_stock}'. "
                f"Use search_ticker to find the correct symbol."
            )

        def pct(value):
            # for fields yfinance gives as a fraction (0.15 -> 15.00%)
            return f"{value * 100:.2f}%" if isinstance(value, (int, float)) else "N/A"

        def num(value, digits=2):
            return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "N/A"

        company_name = info.get('longName', clean_stock)
        sector = info.get('sector', 'N/A')
        exchange = info.get('fullExchangeName') or info.get('exchange', 'N/A')
        currency = info.get('currency', 'N/A')

        market_cap = info.get('marketCap')
        f_mcap = f"{market_cap:,} {currency}" if isinstance(market_cap, (int, float)) else "N/A"

        free_cash_flow = info.get('freeCashflow')
        f_fcf = f"{free_cash_flow:,} {currency}" if isinstance(free_cash_flow, (int, float)) else "N/A"

        # yfinance reports debtToEquity as a percentage (45 means 0.45)
        de_raw = info.get('debtToEquity')
        f_de = f"{de_raw / 100:.2f}" if isinstance(de_raw, (int, float)) else "N/A"

        summary = [
            f"Fundamental Profile for {company_name} ({clean_stock}):",
            f"- Exchange: {exchange} | Currency: {currency}",
            f"- Sector: {sector}",
            f"- Market Cap: {f_mcap}",
            f"- Beta (vs market): {num(info.get('beta'))}",
            "\n[Valuation]",
            f"  - Trailing P/E: {num(info.get('trailingPE'))}",
            f"  - Forward P/E: {num(info.get('forwardPE'))}",
            f"  - Price-to-Book: {num(info.get('priceToBook'))}",
            "\n[Profitability]",
            f"  - ROE: {pct(info.get('returnOnEquity'))}",
            f"  - Net Profit Margin: {pct(info.get('profitMargins'))}",
            "\n[Growth]",
            f"  - Revenue Growth: {pct(info.get('revenueGrowth'))}",
            f"  - Earnings Growth: {pct(info.get('earningsGrowth'))}",
            "\n[Financial Health]",
            f"  - Debt-to-Equity (ratio): {f_de}",
            f"  - Free Cash Flow: {f_fcf}",
            "\n[Dividend]",
            f"  - Dividend Yield (raw value as reported by yfinance, units vary by version): "
            f"{num(info.get('dividendYield'))}",
        ]

        # Data-quality check: how many core metrics are actually present?
        core_keys = [
            'marketCap', 'beta', 'trailingPE', 'forwardPE', 'priceToBook', 'returnOnEquity',
            'profitMargins', 'revenueGrowth', 'earningsGrowth', 'debtToEquity', 'freeCashflow',
        ]
        missing = sum(1 for k in core_keys if not isinstance(info.get(k), (int, float)))
        if missing >= len(core_keys) // 2:
            summary.append(
                f"\n⚠️ DATA QUALITY WARNING: {missing} of {len(core_keys)} core metrics are missing. "
                f"This is likely an obscure/illiquid listing or the wrong symbol. "
                f"Verify with search_ticker and tell the user the data is incomplete; do not infer valuation from it."
            )

        return "\n".join(summary)

    except Exception as e:
        return f"Error executing fundamental tool: {str(e)}"


if __name__ == "__main__":
    # Start the server using the streamable-http transport
    mcp.run(transport='streamable-http')