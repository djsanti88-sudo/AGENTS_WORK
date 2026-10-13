"""Share prices from the Alpaca market data API.

Set ALPACA_API_KEY and ALPACA_SECRET_KEY. Algo Trader Plus unlocks the SIP
feed (all US exchanges). IEX is only used if SIP rejects the request.
Lookups never fall back to simulated prices.
"""

import os
from dotenv import load_dotenv
from alpaca.data.enums import DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoLatestQuoteRequest,
    CryptoLatestTradeRequest,
    StockLatestTradeRequest,
    StockSnapshotRequest,
)
from alpaca.trading.client import TradingClient

load_dotenv(override=True)

alpaca_api_key = os.getenv("ALPACA_API_KEY")
alpaca_secret_key = os.getenv("ALPACA_SECRET_KEY")
alpaca_configured = bool(alpaca_api_key and alpaca_secret_key)

_data_client: StockHistoricalDataClient | None = None
_crypto_client: CryptoHistoricalDataClient | None = None
_trading_client: TradingClient | None = None


def data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        _data_client = StockHistoricalDataClient(alpaca_api_key, alpaca_secret_key)
    return _data_client


def crypto_client() -> CryptoHistoricalDataClient:
    global _crypto_client
    if _crypto_client is None:
        _crypto_client = CryptoHistoricalDataClient(alpaca_api_key, alpaca_secret_key)
    return _crypto_client


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


def get_crypto_price(symbol: str) -> float:
    """Alpaca crypto latest trade, then quote midpoint. No Coinbase fallback."""
    pair = crypto_pair(symbol) or symbol
    client = crypto_client()
    last_error: Exception | None = None
    try:
        trades = client.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=pair))
        price = float(_lookup(trades, pair).price)
        if price > 0:
            return price
    except Exception as error:
        last_error = error
    try:
        quotes = client.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=pair))
        quote = _lookup(quotes, pair)
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        if bid > 0 or ask > 0:
            return bid or ask
    except Exception as error:
        last_error = error
    raise RuntimeError(f"No Alpaca crypto price for {pair}") from last_error


def get_share_price(symbol: str) -> float:
    """Return the current Alpaca price. Never use Coinbase or simulated prices.

    Crypto pairs use Alpaca crypto. Stocks use SIP, then IEX.
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
