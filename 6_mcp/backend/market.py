"""Share prices from the Alpaca market data API, or a simulator when no keys are set.

Set ALPACA_API_KEY and ALPACA_SECRET_KEY to use live data. Algo Trader Plus
unlocks the SIP feed (all US exchanges). Without both keys, prices come from
market_simulator so the whole trading floor still runs out of the box.
"""

import json
import os
import urllib.request
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
from .market_simulator import simulated_price

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
# latest trade first and snapshot last. Remember the first feed that works.
price_methods = [_latest_trade, _snapshot]
feeds = [DataFeed.SIP, DataFeed.IEX]
feed_index = 0


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
    """Latest crypto trade, then the quote midpoint. This feed does not close."""
    pair = crypto_pair(symbol) or symbol
    client = crypto_client()
    try:
        trades = client.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=pair))
        price = float(_lookup(trades, pair).price)
        if price > 0:
            return price
    except Exception:
        pass
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
        print(f"Alpaca crypto unavailable for {pair} ({type(error).__name__}); using Coinbase spot")
    return _public_crypto_price(pair)


def get_share_price(symbol: str) -> float:
    """Return the current price for a symbol, from Alpaca or the simulator.

    Crypto pairs use the 24/7 crypto feed. A failed crypto lookup is not replaced
    with a simulated stock price, because that price would be wildly wrong.
    """
    pair = crypto_pair(symbol)
    if pair:
        if alpaca_configured:
            return get_crypto_price(pair)
        return simulated_price(pair)
    if alpaca_configured:
        try:
            return get_share_price_alpaca(symbol)
        except Exception as e:
            print(f"Alpaca API unavailable ({e}); using a simulated price")
    return simulated_price(symbol)


def get_share_price_alpaca(symbol: str) -> float:
    """Best price the subscription allows, remembering the working feed."""
    global feed_index
    client = data_client()
    symbol = symbol.upper()
    last_error: Exception | None = None
    for index in range(feed_index, len(feeds)):
        for method in price_methods:
            try:
                price = method(client, symbol, feeds[index])
                feed_index = index
                return price
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
