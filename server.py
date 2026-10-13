import os
from mcp.server.fastmcp import FastMCP
import yfinance as yf
import feedparser
from urllib.parse import quote
from datetime import datetime, timezone
import math
from models.models import PriceData, DailyBar,FundamentalsData

mcp = FastMCP(
    "news_server",
    host="0.0.0.0",
    port=int(os.environ.get("PORT",8000)),
)

def _clean(x, digits=2):
    """Round a number, or return None if it is missing or NaN."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, digits)

def _pct(x):
    """Turn a fraction (0.15) into a percent number (15.0), or None if missing."""
    try:
        return _clean(float(x) * 100)
    except (TypeError, ValueError):
        return None

#############  News and Weather Tools ##############

@mcp.tool()
def get_stock_news(query: str, days: int = 7, max_items: int = 6) -> str:
    """
    Fetch recent news headlines about a company or stock, newest first, each with its date and source.
    Pass the COMPANY NAME plus the word 'stock' (e.g. 'State Bank of India stock'),
    not the ticker symbol, because news search works better with names.
    'days' = how far back to look (1-30). 'max_items' = how many headlines to return (1-10).
    Returns a Pydantic NewsData object serialized to a JSON string.
    """
    try:
        days = max(1, min(days, 30))
        max_items = max(1, min(max_items, 10))

        search = quote(f"{query} when:{days}d")
        url = f"https://google.com{search}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(url)

        # Initialize the target parent schema
        news_data = NewsData(query=query, items=[])

        if getattr(feed, "bozo", 0) and not feed.entries:
            # Return valid JSON containing the error text to prevent client breakdown
            return f'{{"error": "Could not fetch the news feed right now."}}'

        if not feed.entries:
            return news_data.model_dump_json()

        raw_items = []
        for e in feed.entries:
            if not getattr(e, "published_parsed", None):
                continue
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            source = e.get("source", {}).get("title", "Unknown source")
            raw_items.append((published, e.title, source))

        if not raw_items:
            return news_data.model_dump_json()

        # Sort newest first
        raw_items.sort(key=lambda x: x[0], reverse=True)

        # Build individual Pydantic NewsItem objects and append to our main model
        for published, title, source in raw_items[:max_items]:
            news_data.items.append(
                NewsItem(
                    published=published.strftime("%Y-%m-%d %H:%M UTC"),
                    title=title,
                    source=source
                )
            )

        # Return the complete structural payload as a clean JSON wire string
        return news_data.model_dump_json()

    except Exception as e:
        return f'{{"error": "Error executing news tool: {str(e)}"}}'
 
@mcp.tool()
def get_weather(location: str) -> str:
    """Get the current weather for a given location."""
    if "delhi" in location.lower():
        return "It's 15°C and rainy."
    return "It's 22°C and sunny."

@mcp.tool()
def getStock_price_info(stock: str, days: int = 5) -> str:
    """
    Fetch price data for a stock as JSON: recent daily OHLCV bars plus risk metrics
    (annualized volatility, max drawdown, current drawdown, 52-week range, average volume).
    'days' = how many recent trading days to include (1-30). Risk metrics always use the last 1 year.
    Use the exchange suffix for Indian stocks, e.g. SBIN.NS.
    """
    try:
        clean_stock = stock.strip().upper()
        df = yf.Ticker(clean_stock).history(period="1y")

        if df.empty:
            return f"Error: No trading data available for ticker '{clean_stock}'."

        days = max(1, min(days, 30))
        bars = [
            DailyBar(
                date=d.strftime('%Y-%m-%d'),
                open=round(r['Open'], 2),
                high=round(r['High'], 2),
                low=round(r['Low'], 2),
                close=round(r['Close'], 2),
                volume=int(r['Volume']),
            )
            for d, r in df.tail(days).iterrows()
        ]

        returns = df['Close'].pct_change().dropna()
        volatility = returns.std() * (252 ** 0.5) * 100
        drawdown = (df['Close'] / df['Close'].cummax() - 1) * 100

        data = PriceData(
            ticker=clean_stock,
            latest_close=_clean(df['Close'].iloc[-1]),
            high_52w=_clean(df['High'].max()),
            low_52w=_clean(df['Low'].min()),
            volatility_pct=_clean(volatility),
            max_drawdown_pct=_clean(drawdown.min()),
            current_drawdown_pct=_clean(drawdown.iloc[-1]),
            avg_volume=int(df['Volume'].mean()),
            recent_bars=bars,
        )
        return data.model_dump_json()

    except Exception as e:
        return f"Error executing price tool: {str(e)}"
    
@mcp.tool()
def getStock_fundamentals(stock: str) -> str:
    """
    Fetch fundamental metrics for a stock as JSON: valuation (P/E, P/B), profitability (ROE, margin),
    growth (revenue, earnings), debt-to-equity, free cash flow, beta, market cap and dividend yield.
    Missing values are null. Banks usually have no debt-to-equity or free cash flow.
    Use the exchange suffix for Indian stocks, e.g. SBIN.NS.
    """
    try:
        clean_stock = stock.strip().upper()
        info = yf.Ticker(clean_stock).info

        if not info or len(info) <= 5:
            return f"Error: Could not retrieve fundamental analysis metrics for ticker '{clean_stock}'."

        # yfinance gives debtToEquity as a percentage (45 means 0.45), so convert to a ratio
        de_raw = _clean(info.get('debtToEquity'))
        debt_to_equity = round(de_raw / 100, 2) if de_raw is not None else None

        data = FundamentalsData(
            ticker=clean_stock,
            company_name=info.get('longName'),
            sector=info.get('sector'),
            market_cap=_clean(info.get('marketCap'), 0),
            beta=_clean(info.get('beta')),
            trailing_pe=_clean(info.get('trailingPE')),
            forward_pe=_clean(info.get('forwardPE')),
            price_to_book=_clean(info.get('priceToBook')),
            roe_pct=_pct(info.get('returnOnEquity')),
            profit_margin_pct=_pct(info.get('profitMargins')),
            revenue_growth_pct=_pct(info.get('revenueGrowth')),
            earnings_growth_pct=_pct(info.get('earningsGrowth')),
            debt_to_equity=debt_to_equity,
            free_cash_flow=_clean(info.get('freeCashflow'), 0),
            dividend_yield_pct=_clean(info.get('dividendYield')),
        )
        return data.model_dump_json()

    except Exception as e:
        return f"Error executing fundamental tool: {str(e)}"


if __name__ == "__main__":
    # Start the server using Stdio transport layer
    mcp.run(transport='streamable-http')
    
    