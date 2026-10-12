import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "news_server",
    host="0.0.0.0",
    port=int(os.environ.get("PORT",8000)),
)

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


if __name__ == "__main__":
    # Start the server using Stdio transport layer
    mcp.run(transport='streamable-http')
    
    