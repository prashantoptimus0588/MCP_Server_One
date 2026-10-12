import os
from mcp.server.fastmcp import FastMCP
import yfinance as yf

mcp = FastMCP(
    "news_server",
    host="0.0.0.0",
    port=int(os.environ.get("PORT",8000)),
)

#############  News and Weather Tools ##############

@mcp.tool()
def get_news(location:str)->str:
    """Get the current news for a given location."""
    
    if "delhi" in location.lower():
        return "Massive Protest regarding vote chori are going on"
    
    return "Stock market of India is falling very bad"

@mcp.tool()
def get_weather(location: str) -> str:
    """Get the current weather for a given location."""
    if "delhi" in location.lower():
        return "It's 15°C and rainy."
    return "It's 22°C and sunny."


@mcp.tool()
def getStock_price_info(stock: str, days: int = 5) -> str:
    """
    Fetch a structured text summary of recent historical OHLCV data for technical analysis.
    """
    try:
        clean_stock = stock.strip().upper()
        ticker = yf.Ticker(clean_stock)
        df = ticker.history(period=f"{days}d")
        
        if df.empty:
            return f"Error: No trading data available for ticker '{clean_stock}'."
        
        summary_lines = [f"Historical OHLCV Analysis for {clean_stock} over last {days} days:"]
        
        for date, row in df.iterrows():
            date_str = date.strftime('%Y-%m-%d')
            line = (f"- Date: {date_str} | Open: {row['Open']:.2f} | High: {row['High']:.2f} | "
                    f"Low: {row['Low']:.2f} | Close: {row['Close']:.2f} | Volume: {int(row['Volume'])}")
            summary_lines.append(line)
            
        highest_high = df['High'].max()
        lowest_low = df['Low'].min()
        avg_volume = df['Volume'].mean()
        
        summary_lines.append("\nSummary Insights:")
        summary_lines.append(f"- Highest Peak in Period: {highest_high:.2f}")
        summary_lines.append(f"- Lowest Floor in Period: {lowest_low:.2f}")
        summary_lines.append(f"- Average Daily Trading Volume: {int(avg_volume)}")
        
        return "\n".join(summary_lines)
    
    except Exception as e:
        return f"Error executing price tool: {str(e)}"


@mcp.tool()
def getStock_fundamentals(stock: str) -> str:
    """
    Fetch core corporate fundamental valuation metrics, financial indicators, and company health metadata.
    Use this tool when evaluating long-term intrinsic value or checking financial health metrics (P/E ratio, ROE, Debt, FCF).
    """
    try:
        clean_stock = stock.strip().upper()
        ticker = yf.Ticker(clean_stock)
        info = ticker.info
        
        if not info or len(info) <= 5:  # yfinance returns a tiny fallback dict if invalid
            return f"Error: Could not retrieve fundamental analysis metrics for ticker '{clean_stock}'."
        
        # Pull key information fields safely using .get() to prevent KeyErrors
        company_name = info.get('longName', clean_stock)
        sector = info.get('sector', 'N/A')
        currency = info.get('currency', 'INR')
        
        # 1. Valuation Metrics
        pe_ratio = info.get('trailingPE', 'N/A')
        forward_pe = info.get('forwardPE', 'N/A')
        pb_ratio = info.get('priceToBook', 'N/A')
        
        # 2. Profitability & Efficiency Metrics
        roe = info.get('returnOnEquity', 'N/A')
        profit_margin = info.get('profitMargins', 'N/A')
        
        # 3. Financial Health & Liquidity
        debt_to_equity = info.get('debtToEquity', 'N/A')
        free_cash_flow = info.get('freeCashflow', 'N/A')
        
        # Format strings neatly
        f_roe = f"{roe * 100:.2f}%" if isinstance(roe, (int, float)) else "N/A"
        f_margin = f"{profit_margin * 100:.2f}%" if isinstance(profit_margin, (int, float)) else "N/A"
        f_fcf = f"{free_cash_flow:,} {currency}" if isinstance(free_cash_flow, (int, float)) else "N/A"
        
        summary = [
            f"Fundamental Profile for {company_name} ({clean_stock}):",
            f"- Sector: {sector}",
            f"\n[Valuation Indicators]",
            f"  - Trailing P/E Ratio: {pe_ratio}",
            f"  - Forward P/E Ratio: {forward_pe}",
            f"  - Price-to-Book (P/B) Ratio: {pb_ratio}",
            f"\n[Profitability & Management Efficiency]",
            f"  - Return on Equity (ROE): {f_roe}",
            f"  - Net Profit Margin: {f_margin}",
            f"\n[Financial Solvency & Capital Health]",
            f"  - Debt-to-Equity Ratio: {debt_to_equity}",
            f"  - Free Cash Flow (FCF): {f_fcf}"
        ]
        
        return "\n".join(summary)
        
    except Exception as e:
        return f"Error executing fundamental tool: {str(e)}"


if __name__ == "__main__":
    # Start the server using Stdio transport layer
    mcp.run(transport='streamable-http')
    
    