"""Share prices from the Alpaca market data API.

Set ALPACA_API_KEY and ALPACA_SECRET_KEY. Algo Trader Plus unlocks the SIP
feed (all US exchanges). IEX is only used if SIP rejects the request.
Lookups never fall back to simulated prices.
"""

import json
import os
import urllib.request
from dotenv import load_dotenv
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockLatestTradeRequest,
    StockSnapshotRequest,
)
from alpaca.trading.client import TradingClient

load_dotenv(override=True)

alpaca_api_key = os.getenv("ALPACA_API_KEY")
alpaca_secret_key = os.getenv("ALPACA_SECRET_KEY")
alpaca_configured = bool(alpaca_api_key and alpaca_secret_key)

_data_client: StockHistoricalDataClient | None = None
_trading_client: TradingClient | None = None


def data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(alpaca_api_key, alpaca_secret_key)
    return _data_client


def trading_client() -> TradingClient:
    """Paper client. Used only for the market clock, never to place orders."""
    global _trading_client
    if _trading_client is None:
        _trading_client = TradingClient(alpaca_api_key, alpaca_secret_key, paper=True)
    return _trading_client


def _lookup(mapping: dict, symbol: str):
    if symbol in mapping:
        return mapping[symbol]
    if len(mapping) == 1:
        return next(iter(mapping.values()))
    raise KeyError(symbol)


def _latest_trade(client: StockHistoricalDataClient, symbol: str, feed: DataFeed) -> float:
    trades = client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbol, feed=feed)
    )
    return float(_lookup(trades, symbol).price)


def _snapshot(client: StockHistoricalDataClient, symbol: str, feed: DataFeed) -> float:
    snapshots = client.get_stock_snapshot(
        StockSnapshotRequest(symbol_or_symbols=symbol, feed=feed)
    )
    snap = _lookup(snapshots, symbol)
    for bar in (snap.latest_trade, snap.minute_bar, snap.daily_bar, snap.previous_daily_bar):
        price = getattr(bar, "price", None) or getattr(bar, "close", None)
        if price:
            return float(price)
    raise RuntimeError(f"No Alpaca snapshot price for {symbol}")


# SIP first (Algo Trader Plus), IEX if that feed is rejected. Within a feed,
# latest trade first and snapshot last. Always start at SIP so a paid
# subscription is not stuck on IEX after one early failure.
price_methods = [_latest_trade, _snapshot]
feeds = [DataFeed.SIP, DataFeed.IEX]


# Bare tickers the crypto tools accept. get_share_price does not use this list,
# so a stock with the same letters is still priced as a stock.
KNOWN_CRYPTO = frozenset({
    "AAVE", "ADA", "APT", "ARB", "ATOM", "AVAX", "BCH", "BONK", "BTC", "CRV",
    "DOGE", "DOT", "ETC", "ETH", "FIL", "HBAR", "INJ", "LDO", "LINK", "LTC",
    "MKR", "NEAR", "OP", "PEPE", "POL", "RENDER", "SHIB", "SOL", "SUI", "TON",
    "TRX", "UNI", "WIF", "XLM", "XRP",
})


def crypto_pair(symbol: str) -> str | None:
    """BTC/USD, BTC-USD, and BTCUSD. A bare ticker such as AAPL is not a pair."""
    raw = symbol.strip().upper().replace(" ", "").replace("-", "/")
    if "/" in raw:
        base, quote = raw.split("/", 1)
        if quote in {"USD", "USDT", "USDC"} and base.isalpha() and base:
            return f"{base}/USD"
        return None
    for suffix in ("USDT", "USDC", "USD"):
        if raw.endswith(suffix) and len(raw) > len(suffix) + 1 and raw[:-len(suffix)].isalpha():
            return f"{raw[:-len(suffix)]}/USD"
    return None


def crypto_symbol(symbol: str) -> str:
    """Canonical pair for an order. Bare coins such as BTC are allowed here."""
    pair = crypto_pair(symbol)
    if pair:
        return pair
    raw = symbol.strip().upper()
    if raw in KNOWN_CRYPTO:
        return f"{raw}/USD"
    raise ValueError(
        f"{symbol} is not a crypto pair. Use BTC, ETH, SOL, or a pair like BTC/USD."
    )


def _public_crypto_price(pair: str) -> float:
    """Coinbase spot when Alpaca's crypto feed rejects the key."""
    base = pair.split("/")[0]
    url = f"https://api.coinbase.com/v2/prices/{base}-USD/spot"
    request = urllib.request.Request(url, headers={"User-Agent": "trading-floor"})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode())
    price = float(payload["data"]["amount"])
    if price <= 0:
        raise RuntimeError(f"No crypto price for {pair}")
    return price


def get_crypto_price(symbol: str) -> float:
    """Coinbase spot. Alpaca's crypto tape is wrong on some pairs (NEAR)."""
    pair = crypto_pair(symbol) or symbol
    return _public_crypto_price(pair)


def get_share_price(symbol: str) -> float:
    """Return the current Alpaca price. Never substitute a simulated price.

    Crypto pairs use Coinbase spot. Stocks use Alpaca SIP, then IEX.
    Missing keys or a failed lookup raise instead of filling at a fake price.
    """
    if not alpaca_configured:
        raise RuntimeError("Alpaca keys required")
    pair = crypto_pair(symbol)
    if pair:
        return get_crypto_price(pair)
    return get_share_price_alpaca(symbol)


def get_share_price_alpaca(symbol: str) -> float:
    """SIP first, then IEX. Each lookup starts at SIP."""
    client = data_client()
    symbol = symbol.upper()
    last_error: Exception | None = None
    for feed in feeds:
        for method in price_methods:
            try:
                return method(client, symbol, feed)
            except Exception as e:
                last_error = e
                continue
    raise RuntimeError(f"No Alpaca price available for {symbol}") from last_error


def is_market_open() -> bool:
    """Whether the US market is open; True on simulated data or if Alpaca is unreachable."""
    if not alpaca_configured:
        return True
    try:
        return bool(trading_client().get_clock().is_open)
    except Exception:
        return True
