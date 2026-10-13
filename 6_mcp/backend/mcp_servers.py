import os
from pathlib import Path
from dotenv import load_dotenv
from agents.mcp import MCPServerStdio, create_static_tool_filter
from .accounts import SHORT_TRADERS
from .market import alpaca_api_key, alpaca_configured, alpaca_secret_key

load_dotenv(override=True)

PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
tavily_env = {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY")}
TIMEOUT = 120

# Snapshot, bars, and movers are enough to price a name and see the move.
# The rest of stock-data (quotes, prints, most-active) stays off the prompt.
ALPACA_PRICE_TOOLS = [
    "get_stock_snapshot",
    "get_stock_bars",
    "get_market_movers",
]

# The market data server for the trader.
# With keys, hand the agent Alpaca's own market data server, run locally over stdio.
# Trading and account toolsets stay off: this floor has its own paper accounts.
# stock-data alone: news, assets, and corporate actions never reach the model.
# Without keys, use our market server, which serves simulated prices.
if alpaca_configured:
    market_params = {
        "command": "uvx",
        "args": ["alpaca-mcp-server"],
        "env": {
            "ALPACA_API_KEY": alpaca_api_key,
            "ALPACA_SECRET_KEY": alpaca_secret_key,
            "ALPACA_PAPER_TRADE": "true",
            "ALPACA_TOOLSETS": "stock-data",
        },
    }
else:
    market_params = {
        "command": "uv",
        "args": ["run", "-m", "backend.market_server"],
        "cwd": PROJECT_DIR
    }


def _market_server() -> MCPServerStdio:
    """Alpaca is limited to three price tools. The simulator has one tool, so it stays unfiltered."""
    if alpaca_configured:
        return MCPServerStdio(
            market_params,
            client_session_timeout_seconds=TIMEOUT,
            tool_filter=create_static_tool_filter(allowed_tool_names=ALPACA_PRICE_TOOLS),
        )
    return MCPServerStdio(market_params, client_session_timeout_seconds=TIMEOUT)


# Stock shorts for every trader. Crypto tools stay on Santy and Cathe.
STOCK_SHORT_TOOLS = ["short_shares", "cover_shares", "set_exit", "get_exits"]


def _short_server(name: str | None) -> MCPServerStdio:
    params = {"command": "uv", "args": ["run", "-m", "backend.short_server"], "cwd": PROJECT_DIR}
    if name and name.lower() in SHORT_TRADERS:
        return MCPServerStdio(params, client_session_timeout_seconds=TIMEOUT)
    return MCPServerStdio(
        params,
        client_session_timeout_seconds=TIMEOUT,
        tool_filter=create_static_tool_filter(allowed_tool_names=STOCK_SHORT_TOOLS),
    )


def trader_mcp_servers(name: str | None = None) -> list[MCPServerStdio]:
    """The trader's MCP servers: Accounts, Push, Market data, and stock shorts.

    Santy and Cathe also see the crypto tools. The other four do not.
    """
    return [
        MCPServerStdio(
            {"command": "uv", "args": ["run", "-m", "backend.accounts_server"], "cwd": PROJECT_DIR},
            client_session_timeout_seconds=TIMEOUT,
        ),
        MCPServerStdio(
            {"command": "uv", "args": ["run", "-m", "backend.push_server"], "cwd": PROJECT_DIR},
            client_session_timeout_seconds=TIMEOUT,
        ),
        _market_server(),
        _short_server(name),
    ]


def researcher_mcp_servers(name: str) -> list[MCPServerStdio]:
    """The researcher's MCP servers: Fetch, Tavily web search and Memory.

    Tavily's server offers several tools; we restrict it to web search so the
    researcher reaches for plain search rather than its heavier crawl or deep-research tools.
    """
    servers = [
        MCPServerStdio(
            {"command": "uvx", "args": ["--with", "mcp<2", "mcp-server-fetch"]},
            client_session_timeout_seconds=TIMEOUT,
        )
    ]
    if tavily_env.get("TAVILY_API_KEY"):
        servers.append(
            MCPServerStdio(
                {"command": "npx", "args": ["-y", "tavily-mcp@latest"], "env": tavily_env},
                client_session_timeout_seconds=TIMEOUT,
                tool_filter=create_static_tool_filter(allowed_tool_names=["tavily_search"]),
            )
        )
    servers.append(
        MCPServerStdio(
            {
                "command": "npx",
                "args": ["-y", "mcp-memory-libsql"],
                "env": {"LIBSQL_URL": f"file:./memory/{name}.db"},
            },
            client_session_timeout_seconds=TIMEOUT,
        )
    )
    return servers
    
