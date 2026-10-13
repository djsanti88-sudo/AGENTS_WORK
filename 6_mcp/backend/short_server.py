"""Short sales, covers, and live stops.

Every trader can short and cover stocks, and can set a stop. Crypto stays on
Santy and Cathe: the account methods reject any other name.
"""

from mcp.server.fastmcp import FastMCP

from .accounts import Account

mcp = FastMCP("short_server")


@mcp.tool()
async def buy_crypto(name: str, symbol: str, quantity: float, rationale: str) -> str:
    """Buy crypto. The market is open 24/7. Quantity is coins and can be fractional.

    One bitcoin is far larger than the account, so size BTC as a fraction of a coin.
    Use BTC, ETH, SOL, or a pair like BTC/USD.

    Args:
        name: The account name. Must be Santy or Cathe.
        symbol: Coin or pair, for example BTC or ETH/USD.
        quantity: Number of coins. Fractions are allowed.
        rationale: Why this long fits the strategy.
    """
    return Account.get(name).buy_crypto(symbol, quantity, rationale)


@mcp.tool()
async def sell_crypto(name: str, symbol: str, quantity: float, rationale: str) -> str:
    """Sell crypto you already hold. Quantity is coins and can be fractional.

    Args:
        name: The account name. Must be Santy or Cathe.
        symbol: Coin or pair.
        quantity: Number of coins to sell.
        rationale: Why you are selling.
    """
    return Account.get(name).sell_crypto(symbol, quantity, rationale)


@mcp.tool()
async def short_crypto(name: str, symbol: str, quantity: float, rationale: str) -> str:
    """Short crypto. The position goes negative and you profit if the price falls.

    Crypto does not close. Quantity is coins and can be fractional. A stop and an
    8% trail are armed. The trail does not pull that stop in until the trade has
    already moved 8% your way. There is no take profit, so a spike can run.

    Args:
        name: The account name. Must be Santy or Cathe.
        symbol: Coin or pair, for example BTC or SOL/USD.
        quantity: Number of coins to short.
        rationale: Why this short fits the strategy.
    """
    return Account.get(name).short_crypto(symbol, quantity, rationale)


@mcp.tool()
async def cover_crypto(name: str, symbol: str, quantity: float, rationale: str) -> str:
    """Buy coins to shrink or close a crypto short.

    Args:
        name: The account name. Must be Santy or Cathe.
        symbol: Coin or pair of the open short.
        quantity: Number of coins to buy back.
        rationale: Why you are covering.
    """
    return Account.get(name).cover_crypto(symbol, quantity, rationale)


@mcp.tool()
async def short_shares(name: str, symbol: str, quantity: int, rationale: str) -> str:
    """Sell shares you do not own. The position quantity goes negative.

    You profit if the price falls and lose if it rises. A default stop and an
    8% trailing stop are armed. The trail waits until the trade has moved 8%
    your way before it tightens. There is no take profit. Pass your account name.

    Args:
        name: The account name.
        symbol: Ticker, for example AAPL.
        quantity: Number of shares to short.
        rationale: Why this short fits the strategy.
    """
    return Account.get(name).short_shares(symbol, quantity, rationale)


@mcp.tool()
async def cover_shares(name: str, symbol: str, quantity: int, rationale: str) -> str:
    """Buy back shares to shrink or close a short.

    Args:
        name: The account name.
        symbol: Ticker of the open short.
        quantity: Number of shares to buy back. Cannot exceed the short.
        rationale: Why you are covering.
    """
    return Account.get(name).cover_shares(symbol, quantity, rationale)


@mcp.tool()
async def set_exit(
    name: str,
    symbol: str,
    stop_loss: float = 0,
    take_profit: float = 0,
    trail_percent: float = 0,
) -> str:
    """Set a dynamic stop and trail on an open long or short.

    You choose how much room the trade gets. A wide trail lets a spike run.
    A tight trail is only for a scalp you actually want. Pass 0 to leave a level unset.
    The default trail is 8%. You may set anything from 0.2 to 25.
    A short stop must be above the price. A long stop must be below the price.
    take_profit never closes the trade and never tightens the stop.
    Checked about every 20 seconds, including while you are not in a session.

    Args:
        name: The account name.
        symbol: Ticker of the open position.
        stop_loss: Price that closes the trade if it moves against you. 0 to skip.
        take_profit: Ignored as an exit. It does not close or tighten the trade. 0 to skip.
        trail_percent: Trailing distance in percent, from 0.2 to 25. Use 8 to 15 to ride a spike. 0 to skip.
    """
    return Account.get(name).set_exit(
        symbol,
        stop_loss=stop_loss or None,
        take_profit=take_profit or None,
        trail_percent=trail_percent or None,
    )


@mcp.tool()
async def get_exits(name: str) -> dict:
    """Return the live stop, target, and trail on every open position.

    Args:
        name: The account name.
    """
    account = Account.get(name)
    return {symbol: plan.model_dump() for symbol, plan in account.exits.items()}


if __name__ == "__main__":
    mcp.run(transport="stdio")
