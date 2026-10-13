import os
from mcp.server.fastmcp import FastMCP
import yfinance as yf
import feedparser
from urllib.parse import quote
from datetime import datetime, timezone

mcp = FastMCP(
    "news_server",
    host="0.0.0.0",
    port=int(os.environ.get("PORT",8000)),
)

#############  News and Weather Tools ##############

@mcp.tool()
def get_stock_news(query: str, days: int = 7, max_items: int = 6) -> str:
    """
    Fetch recent news headlines about a company or stock, newest first, each with its date and source.
    Pass the COMPANY NAME plus the word 'stock' (e.g. 'State Bank of India stock'),
    not the ticker symbol, because news search works better with names.
    'days' = how far back to look (1-30). 'max_items' = how many headlines to return (1-10).
    """
    try:
        days = max(1, min(days, 30))
        max_items = max(1, min(max_items, 10))

        search = quote(f"{query} when:{days}d")
        url = f"https://news.google.com/rss/search?q={search}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(url)

        if getattr(feed, "bozo", 0) and not feed.entries:
            return "Error: could not fetch the news feed right now."

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

        lines = [f"Latest news for '{query}' (last {days} days, newest first):"]
        for published, title, source in items[:max_items]:
            hours = int((now - published).total_seconds() // 3600)
            age = f"{hours}h ago" if hours < 48 else f"{hours // 24}d ago"
            lines.append(f"- [{published:%Y-%m-%d %H:%M} UTC, {age}] {title} ({source})")

        return "\n".join(lines)

    except Exception as e:
        return f"Error executing news tool: {str(e)}"
    
@mcp.tool()
def get_weather(location: str) -> str:
    """Get the current weather for a given location."""
    if "delhi" in location.lower():
        return "It's 15°C and rainy."
    return "It's 22°C and sunny."

@mcp.tool()
def getStock_price_info(stock: str, days: int = 5) -> str:
    """
    Fetch recent daily OHLCV rows plus risk metrics (annualized volatility, max drawdown,
    current drawdown, 52-week range) for technical analysis.
    'days' = how many recent trading days to list. Risk metrics always use the last 1 year.
    """
    try:
        clean_stock = stock.strip().upper()
        ticker = yf.Ticker(clean_stock)
        df = ticker.history(period="1y")

        if df.empty:
            return f"Error: No trading data available for ticker '{clean_stock}'."

        # Recent rows to display
        recent = df.tail(days)
        lines = [f"Recent OHLCV for {clean_stock} (last {len(recent)} trading days):"]
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
        lines.append(f"- Latest Close: {last_close:.2f}")
        lines.append(f"- 52-Week High: {high_52:.2f}")
        lines.append(f"- 52-Week Low: {low_52:.2f}")
        lines.append(f"- Annualized Volatility: {volatility:.2f}%")
        lines.append(f"- Max Drawdown: {max_drawdown:.2f}%")
        lines.append(f"- Current Drawdown from Peak: {current_drawdown:.2f}%")
        lines.append(f"- Average Daily Volume: {int(avg_volume)}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error executing price tool: {str(e)}"

@mcp.tool()
def getStock_fundamentals(stock: str) -> str:
    """
    Fetch core fundamental metrics: valuation (P/E, P/B), profitability (ROE, margin),
    growth (revenue, earnings), financial health (debt-to-equity, FCF), dividend yield, beta and market cap.
    Use this when evaluating long-term intrinsic value or financial health.
    """
    try:
        clean_stock = stock.strip().upper()
        ticker = yf.Ticker(clean_stock)
        info = ticker.info

        if not info or len(info) <= 5:  # yfinance returns a tiny fallback dict if invalid
            return f"Error: Could not retrieve fundamental analysis metrics for ticker '{clean_stock}'."

        def pct(value):
            # for fields yfinance gives as a fraction (0.15 -> 15.00%)
            return f"{value * 100:.2f}%" if isinstance(value, (int, float)) else "N/A"

        def num(value, digits=2):
            return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "N/A"

        company_name = info.get('longName', clean_stock)
        sector = info.get('sector', 'N/A')
        currency = info.get('currency', 'INR')

        market_cap = info.get('marketCap')
        f_mcap = f"{market_cap:,} {currency}" if isinstance(market_cap, (int, float)) else "N/A"

        free_cash_flow = info.get('freeCashflow')
        f_fcf = f"{free_cash_flow:,} {currency}" if isinstance(free_cash_flow, (int, float)) else "N/A"

        # yfinance reports debtToEquity as a percentage (45 means 0.45)
        de_raw = info.get('debtToEquity')
        f_de = f"{de_raw / 100:.2f}" if isinstance(de_raw, (int, float)) else "N/A"

        summary = [
            f"Fundamental Profile for {company_name} ({clean_stock}):",
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
            f"  - Dividend Yield (as reported by yfinance): {num(info.get('dividendYield'))}",
        ]

        return "\n".join(summary)

    except Exception as e:
        return f"Error executing fundamental tool: {str(e)}"




if __name__ == "__main__":
    # Start the server using Stdio transport layer
    mcp.run(transport='streamable-http')
    
    