from pydantic import BaseModel, Field
import json
from dotenv import load_dotenv
from datetime import datetime
from .market import KNOWN_CRYPTO, crypto_pair, crypto_symbol, get_share_price
from .database import write_account, read_account, write_log

load_dotenv(override=True)

INITIAL_BALANCE = 10_000.0
SPREAD = 0.002

# Santy and Cathe are the crypto desk. Every trader can short stocks.
# These caps apply to every short. The long-exposure cap still applies only on this desk.
SHORT_TRADERS = frozenset({"santy", "cathe"})
MAX_POSITION_FRACTION = 0.40
MAX_GROSS_SHORT_FRACTION = 1.0
MAINTENANCE_EQUITY_FRACTION = 0.25
DEFAULT_TRAIL_PERCENT = 8.0
MIN_TRAIL_PERCENT = 0.2
MAX_TRAIL_PERCENT = 25.0
EXIT_CHECK_SECONDS = 20


class ExitPlan(BaseModel):
    """A live stop, target, and trailing stop for one open position."""

    stop_loss: float | None = None
    take_profit: float | None = None
    trail_percent: float | None = None
    extreme: float | None = None
    trail_active: bool = False


class Transaction(BaseModel):
    symbol: str
    quantity: float
    price: float
    timestamp: str
    rationale: str
    side: str = ""

    def total(self) -> float:
        return self.quantity * self.price
    
    def __repr__(self):
        return f"{abs(self.quantity)} shares of {self.symbol} at {self.price} each."


class Account(BaseModel):
    name: str
    balance: float
    strategy: str
    holdings: dict[str, float]
    transactions: list[Transaction]
    portfolio_value_time_series: list[tuple[str, float]]
    exits: dict[str, ExitPlan] = Field(default_factory=dict)

    @classmethod
    def get(cls, name: str):
        fields = read_account(name.lower())
        if not fields:
            fields = {
                "name": name.lower(),
                "balance": INITIAL_BALANCE,
                "strategy": "",
                "holdings": {},
                "transactions": [],
                "portfolio_value_time_series": []
            }
            write_account(name, fields)
        return cls(**fields)
    
    
    def save(self):
        write_account(self.name.lower(), self.model_dump())

    def reset(self, strategy: str=""):
        self.balance = INITIAL_BALANCE
        self.strategy = strategy
        self.holdings = {}
        self.transactions = []
        self.portfolio_value_time_series = []
        self.exits = {}
        self.save()

    def deposit(self, amount: float):
        """ Deposit funds into the account. """
        if amount <= 0:
            raise ValueError("Deposit amount must be positive.")
        self.balance += amount
        print(f"Deposited ${amount}. New balance: ${self.balance}")
        self.save()

    def withdraw(self, amount: float):
        """ Withdraw funds from the account, ensuring it doesn't go negative. """
        if amount > self.balance:
            raise ValueError("Insufficient funds for withdrawal.")
        self.balance -= amount
        print(f"Withdrew ${amount}. New balance: ${self.balance}")
        self.save()

    def buy_shares(self, symbol: str, quantity: int, rationale: str) -> str:
        """ Buy shares of a stock if sufficient funds are available. """
        return self._buy(symbol, int(quantity), rationale)

    def buy_crypto(self, symbol: str, quantity: float, rationale: str) -> str:
        """Buy a coin. Quantity is in coins and can be fractional. Crypto trades 24/7."""
        self._require_short_desk()
        symbol, quantity = self._crypto_order(symbol, quantity)
        return self._buy(symbol, quantity, rationale)

    def _buy(self, symbol: str, quantity: float, rationale: str) -> str:
        price = get_share_price(symbol)
        buy_price = price * (1 + SPREAD)
        total_cost = buy_price * quantity

        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
        if total_cost > self.balance:
            raise ValueError("Insufficient funds to buy shares.")
        elif price==0:
            raise ValueError(f"Unrecognized symbol {symbol}")

        current = self.holdings.get(symbol, 0)
        resulting = current + quantity
        if self._exposure_grows(symbol, resulting):
            self._assert_position_size(symbol, resulting)

        self._set_holding(symbol, resulting)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # A buy against a short is a cover. Long buys stay unlabeled, as before.
        side = "cover" if current < 0 else ""
        transaction = Transaction(
            symbol=symbol, quantity=quantity, price=buy_price, timestamp=timestamp, rationale=rationale, side=side
        )
        self.transactions.append(transaction)
        self._sync_exit(symbol)

        self.balance -= total_cost
        self.save()
        write_log(self.name, "account", f"Bought {quantity} of {symbol}")
        return "Completed. Latest details:\n" + self.report()

    def sell_shares(self, symbol: str, quantity: int, rationale: str) -> str:
        """ Sell shares of a stock if the user has enough shares. """
        return self._sell(symbol, int(quantity), rationale)

    def sell_crypto(self, symbol: str, quantity: float, rationale: str) -> str:
        """Sell coins you hold. Quantity is in coins and can be fractional."""
        self._require_short_desk()
        symbol, quantity = self._crypto_order(symbol, quantity)
        return self._sell(symbol, quantity, rationale)

    def _sell(self, symbol: str, quantity: float, rationale: str) -> str:
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
        if self.holdings.get(symbol, 0) + 1e-8 < quantity:
            raise ValueError(f"Cannot sell {quantity} shares of {symbol}. Not enough shares held.")

        price = get_share_price(symbol)
        sell_price = price * (1 - SPREAD)
        total_proceeds = sell_price * quantity

        self._set_holding(symbol, self.holdings.get(symbol, 0) - quantity)
        self._sync_exit(symbol)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        transaction = Transaction(symbol=symbol, quantity=-quantity, price=sell_price, timestamp=timestamp, rationale=rationale)
        self.transactions.append(transaction)

        self.balance += total_proceeds
        self.save()
        write_log(self.name, "account", f"Sold {quantity} of {symbol}")
        return "Completed. Latest details:\n" + self.report()

    def calculate_portfolio_value(self):
        """ Calculate the total value of the user's portfolio. """
        total_value = self.balance
        for symbol, quantity in self.holdings.items():
            total_value += get_share_price(symbol) * quantity
        return total_value

    def calculate_profit_loss(self, portfolio_value: float):
        """ Calculate profit or loss from the initial spend. """
        initial_spend = sum(transaction.total() for transaction in self.transactions)
        return portfolio_value - initial_spend - self.balance

    def get_holdings(self):
        """ Report the current holdings of the user. """
        return self.holdings

    def get_profit_loss(self):
        """ Report the user's profit or loss at any point in time. """
        return self.calculate_profit_loss()

    def list_transactions(self):
        """ List all transactions made by the user. """
        return [transaction.model_dump() for transaction in self.transactions]
    
    def report(self) -> str:
        """ Return a json string representing the account.  """
        portfolio_value = self.calculate_portfolio_value()
        self.portfolio_value_time_series.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), portfolio_value))
        self.save()
        pnl = self.calculate_profit_loss(portfolio_value)
        data = self.model_dump()
        data["total_portfolio_value"] = portfolio_value
        data["total_profit_loss"] = pnl
        write_log(self.name, "account", "Retrieved account details")
        return json.dumps(data)
    
    def get_strategy(self) -> str:
        """ Return the strategy of the account """
        write_log(self.name, "account", "Retrieved strategy")
        return self.strategy
    
    def change_strategy(self, strategy: str) -> str:
        """ At your discretion, if you choose to, call this to change your investment strategy for the future """
        self.strategy = strategy
        self.save()
        write_log(self.name, "account", "Changed strategy")
        return "Changed strategy"

    def short_shares(self, symbol: str, quantity: float, rationale: str) -> str:
        """Open or add to a short. The holding goes negative. Profit if the price falls."""
        if crypto_pair(symbol) or symbol.strip().upper() in KNOWN_CRYPTO:
            self._require_short_desk()
            symbol, quantity = self._crypto_order(symbol, quantity)
        else:
            number = float(quantity)
            if number <= 0 or abs(number - round(number)) > 1e-6:
                raise ValueError("Quantity must be a positive whole number of shares.")
            quantity = int(round(number))
            symbol = self._canon(symbol)
        return self._open_short(symbol, quantity, rationale)

    def short_crypto(self, symbol: str, quantity: float, rationale: str) -> str:
        """Short a coin. Quantity is in coins and can be fractional. The market is open 24/7."""
        self._require_short_desk()
        symbol, quantity = self._crypto_order(symbol, quantity)
        return self._open_short(symbol, quantity, rationale)

    def _open_short(self, symbol: str, quantity: float, rationale: str) -> str:
        if self.holdings.get(symbol, 0) > 0:
            raise ValueError(f"Already long {symbol}. Sell that long before shorting it.")

        price = get_share_price(symbol)
        if price == 0:
            raise ValueError(f"Unrecognized symbol {symbol}")
        sell_price = price * (1 - SPREAD)
        resulting = self.holdings.get(symbol, 0) - quantity
        self._assert_position_size(symbol, resulting)
        self._assert_short_book(symbol, resulting)

        self._set_holding(symbol, resulting)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.transactions.append(
            Transaction(
                symbol=symbol,
                quantity=-quantity,
                price=sell_price,
                timestamp=timestamp,
                rationale=rationale,
                side="short",
            )
        )
        self.balance += sell_price * quantity
        self._sync_exit(symbol)
        self.save()
        write_log(self.name, "account", f"Shorted {quantity} of {symbol}")
        return "Short opened.\n" + self._exit_summary(symbol) + "\n" + self.report()

    def cover_shares(self, symbol: str, quantity: float, rationale: str) -> str:
        """Buy back shares to reduce or close a short."""
        if crypto_pair(symbol) or symbol.strip().upper() in KNOWN_CRYPTO:
            self._require_short_desk()
            symbol, quantity = self._crypto_order(symbol, quantity)
        else:
            number = float(quantity)
            if number <= 0 or abs(number - round(number)) > 1e-6:
                raise ValueError("Quantity must be a positive whole number of shares.")
            quantity = int(round(number))
            symbol = self._canon(symbol)
        return self._cover(symbol, quantity, rationale)

    def cover_crypto(self, symbol: str, quantity: float, rationale: str) -> str:
        """Buy coins to shrink or close a crypto short."""
        self._require_short_desk()
        symbol, quantity = self._crypto_order(symbol, quantity)
        return self._cover(symbol, quantity, rationale)

    def _cover(self, symbol: str, quantity: float, rationale: str) -> str:
        held = self.holdings.get(symbol, 0)
        if held >= 0:
            raise ValueError(f"No short in {symbol} to cover.")
        if quantity > abs(held) + 1e-8:
            raise ValueError(
                f"Cover of {quantity} is larger than the short of {abs(held)}. Cover the short, then buy to go long."
            )

        price = get_share_price(symbol)
        if price == 0:
            raise ValueError(f"Unrecognized symbol {symbol}")
        buy_price = price * (1 + SPREAD)
        total_cost = buy_price * quantity
        if total_cost > self.balance:
            raise ValueError("Insufficient funds to cover the short.")

        self._set_holding(symbol, held + quantity)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.transactions.append(
            Transaction(
                symbol=symbol,
                quantity=quantity,
                price=buy_price,
                timestamp=timestamp,
                rationale=rationale,
                side="cover",
            )
        )
        self.balance -= total_cost
        self._sync_exit(symbol)
        self.save()
        write_log(self.name, "account", f"Covered {quantity} of {symbol}")
        return "Cover completed. Latest details:\n" + self.report()

    def set_exit(
        self,
        symbol: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        trail_percent: float | None = None,
    ) -> str:
        """Replace the stop and trail. A wider stop is allowed. A take-profit mark never closes the trade."""
        symbol = self._canon(symbol)
        qty = self.holdings.get(symbol, 0)
        if qty == 0:
            raise ValueError(f"No open position in {symbol}.")
        price = get_share_price(symbol)
        if price <= 0:
            raise ValueError(f"Unrecognized symbol {symbol}")
        if stop_loss is not None and stop_loss <= 0:
            stop_loss = None
        if take_profit is not None and take_profit <= 0:
            take_profit = None
        if trail_percent is not None and trail_percent <= 0:
            trail_percent = None
        if stop_loss is None and take_profit is None and trail_percent is None:
            raise ValueError("Set a stop_loss, a take_profit, or a trail_percent.")
        if trail_percent is not None and not (MIN_TRAIL_PERCENT <= trail_percent <= MAX_TRAIL_PERCENT):
            raise ValueError(f"trail_percent must be between {MIN_TRAIL_PERCENT} and {MAX_TRAIL_PERCENT}.")

        side = "short" if qty < 0 else "long"
        self._validate_levels(stop_loss, take_profit, price, side)
        plan = self._ratchet(
            ExitPlan(stop_loss=stop_loss, take_profit=take_profit, trail_percent=trail_percent, extreme=price),
            price,
            side,
        )
        self._validate_levels(plan.stop_loss, plan.take_profit, price, side)
        self.exits[symbol] = plan
        self.save()
        write_log(self.name, "account", f"Set exit on {symbol}")
        return self._exit_summary(symbol)

    def enforce_exits(self) -> list[str]:
        """Move trails and close anything that hit a stop or a margin call.

        A take-profit price does not flatten the position and does not pull the stop in.

        Each symbol is re-read from the database before it is written, so a trader
        filling an order in the same second is not overwritten by a stale snapshot.
        """
        notes: list[str] = []
        notes.extend(Account.get(self.name)._enforce_margin())
        snapshot = Account.get(self.name)
        naked = [
            symbol
            for symbol, qty in snapshot.holdings.items()
            if qty != 0 and symbol not in snapshot.exits and (self.name.lower() in SHORT_TRADERS or qty < 0)
        ]
        for symbol in naked:
            account = Account.get(self.name)
            if account.holdings.get(symbol, 0) != 0 and symbol not in account.exits:
                account._arm_default_exit(symbol)
                account.save()
                notes.append(f"{account.name} armed a default exit on {symbol}")
        for symbol in list(snapshot.exits):
            note = Account.get(self.name)._enforce_symbol(symbol)
            if note:
                notes.append(note)
        return notes

    def _crypto_order(self, symbol: str, quantity: float) -> tuple[str, float]:
        pair = crypto_symbol(symbol)
        quantity = round(float(quantity), 8)
        if quantity <= 0:
            raise ValueError("Quantity must be positive. Crypto size is in coins and can be fractional.")
        return self._canon(pair), quantity

    def _set_holding(self, symbol: str, quantity: float) -> None:
        if abs(quantity) < 1e-8:
            self.holdings.pop(symbol, None)
        else:
            self.holdings[symbol] = quantity

    def _require_short_desk(self) -> None:
        if self.name.lower() not in SHORT_TRADERS:
            raise ValueError(f"{self.name} cannot trade crypto. That desk is Santy and Cathe only.")

    def _canon(self, symbol: str) -> str:
        wanted = symbol.strip().upper()
        for key in list(self.holdings) + list(self.exits):
            if key.upper() == wanted:
                return key
        return wanted

    def _exposure_grows(self, symbol: str, resulting: int) -> bool:
        if self.name.lower() not in SHORT_TRADERS:
            return False
        return abs(resulting) > abs(self.holdings.get(symbol, 0))

    def _assert_position_size(self, symbol: str, resulting_qty: int) -> None:
        price = get_share_price(symbol)
        if price <= 0:
            raise ValueError(f"Unrecognized symbol {symbol}")
        equity = self.calculate_portfolio_value()
        if equity <= 0:
            raise ValueError("No equity left to trade.")
        notional = abs(resulting_qty) * price
        limit = equity * MAX_POSITION_FRACTION
        if notional > limit + 0.01:
            raise ValueError(
                f"{symbol} would be ${notional:.0f}, above the {MAX_POSITION_FRACTION:.0%} equity cap (${limit:.0f})."
            )

    def _assert_short_book(self, symbol: str, resulting_qty: int) -> None:
        equity = self.calculate_portfolio_value()
        total = 0.0
        for held, qty in self.holdings.items():
            if qty >= 0 or held == symbol:
                continue
            total += get_share_price(held) * abs(qty)
        total += get_share_price(symbol) * abs(resulting_qty)
        limit = max(equity, 0) * MAX_GROSS_SHORT_FRACTION
        if total > limit + 0.01:
            raise ValueError(
                f"Short book would be ${total:.0f}, above the {MAX_GROSS_SHORT_FRACTION:.0%} equity cap (${limit:.0f})."
            )

    def _sync_exit(self, symbol: str) -> None:
        qty = self.holdings.get(symbol, 0)
        if qty == 0:
            self.exits.pop(symbol, None)
            return
        if symbol not in self.exits and (self.name.lower() in SHORT_TRADERS or qty < 0):
            self._arm_default_exit(symbol)

    def _arm_default_exit(self, symbol: str) -> None:
        price = get_share_price(symbol)
        qty = self.holdings.get(symbol, 0)
        if qty == 0 or price <= 0:
            return
        distance = DEFAULT_TRAIL_PERCENT / 100
        if qty > 0:
            stop = price * (1 - distance)
        else:
            stop = price * (1 + distance)
        self.exits[symbol] = ExitPlan(
            stop_loss=round(stop, 4),
            take_profit=None,
            trail_percent=DEFAULT_TRAIL_PERCENT,
            extreme=price,
        )

    def _validate_levels(self, stop: float | None, target: float | None, price: float, side: str) -> None:
        if side == "long":
            if stop is not None and stop >= price:
                raise ValueError(f"Long stop must be below the price {price:.2f}.")
            if target is not None and target <= price:
                raise ValueError(f"Long target must be above the price {price:.2f}.")
        else:
            if stop is not None and stop <= price:
                raise ValueError(f"Short stop must be above the price {price:.2f}.")
            if target is not None and target >= price:
                raise ValueError(f"Short target must be below the price {price:.2f}.")

    def _ratchet(self, plan: ExitPlan, price: float, side: str) -> ExitPlan:
        """Keep the stop they chose until the trade has run by the trail width.

        After that, the trail follows the best price and only tightens. A small
        pullback does not close a spike, and a wider stop is not replaced early.
        """
        if not plan.trail_percent or plan.trail_percent <= 0 or price <= 0:
            return plan
        percent = plan.trail_percent / 100
        anchor = price if plan.extreme is None else plan.extreme
        if side == "long":
            if plan.stop_loss is None:
                plan = plan.model_copy(
                    update={"stop_loss": round(price * (1 - percent), 4), "extreme": anchor}
                )
            if not plan.trail_active and price < anchor * (1 + percent):
                return plan
            extreme = max(anchor, price)
            stop = max(plan.stop_loss, extreme * (1 - percent))
        else:
            if plan.stop_loss is None:
                plan = plan.model_copy(
                    update={"stop_loss": round(price * (1 + percent), 4), "extreme": anchor}
                )
            if not plan.trail_active and price > anchor * (1 - percent):
                return plan
            extreme = min(anchor, price)
            stop = min(plan.stop_loss, extreme * (1 + percent))
        if plan.trail_active and stop == plan.stop_loss and extreme == plan.extreme:
            return plan
        return plan.model_copy(
            update={"extreme": extreme, "stop_loss": round(stop, 4), "trail_active": True}
        )

    def _trigger(self, plan: ExitPlan, price: float, side: str) -> str | None:
        """Only a stop closes the trade. A take-profit mark never does."""
        if side == "long":
            if plan.stop_loss is not None and price <= plan.stop_loss:
                return f"Stop loss hit at {price:.2f} (stop {plan.stop_loss:.2f})"
        elif plan.stop_loss is not None and price >= plan.stop_loss:
            return f"Stop loss hit at {price:.2f} (stop {plan.stop_loss:.2f})"
        return None

    def _release_target(self, plan: ExitPlan, price: float, side: str, symbol: str) -> tuple[ExitPlan, str | None]:
        """A profit mark never closes the trade and never pulls the stop in."""
        if plan.take_profit is None:
            return plan, None
        hit = price >= plan.take_profit if side == "long" else price <= plan.take_profit
        if not hit:
            return plan, None
        return plan.model_copy(update={"take_profit": None}), (
            f"{self.name} price reached {plan.take_profit:.2f} on {symbol}. "
            "Trail left unchanged. Position still open."
        )

    def _exit_summary(self, symbol: str) -> str:
        plan = self.exits.get(symbol)
        if plan is None:
            return f"{symbol}: no exit."
        stop = f"{plan.stop_loss:.2f}" if plan.stop_loss is not None else "none"
        trail = f"{plan.trail_percent:g}%" if plan.trail_percent is not None else "none"
        mark = ""
        if plan.take_profit is not None:
            mark = f" A mark at {plan.take_profit:.2f} does not close the trade and does not tighten the stop."
        return (
            f"{symbol} exit: stop {stop}, trail {trail}.{mark} "
            "The stop stays where you set it until price has moved your way by the trail width. "
            "After that it only tightens. A spike is left open. Only the stop, or you, can close it."
        )

    def _affordable_cover_qty(self, symbol: str, wanted: float) -> float:
        price = get_share_price(symbol)
        buy_price = price * (1 + SPREAD)
        if buy_price <= 0:
            return 0
        affordable = self.balance / buy_price
        if crypto_pair(symbol):
            return max(0.0, min(float(wanted), round(affordable, 8)))
        return max(0, min(int(wanted), int(affordable)))

    def _enforce_margin(self) -> list[str]:
        shorts: list[tuple[float, str, int]] = []
        short_notional = 0.0
        for symbol, qty in self.holdings.items():
            if qty < 0:
                notional = get_share_price(symbol) * abs(qty)
                short_notional += notional
                shorts.append((notional, symbol, abs(qty)))
        if short_notional <= 0:
            return []
        equity = self.calculate_portfolio_value()
        if equity >= short_notional * MAINTENANCE_EQUITY_FRACTION:
            return []
        shorts.sort(reverse=True)
        _, symbol, qty = shorts[0]
        fresh = Account.get(self.name)
        if fresh.holdings.get(symbol, 0) >= 0:
            return []
        cover_qty = fresh._affordable_cover_qty(symbol, min(qty, abs(fresh.holdings[symbol])))
        if cover_qty <= 0:
            write_log(fresh.name, "account", f"Margin call on {symbol} but not enough cash to cover")
            return [f"{fresh.name} margin call on {symbol} but not enough cash to cover"]
        fresh.cover_shares(symbol, cover_qty, "Margin call: equity too thin versus the short book")
        return [f"{fresh.name} margin-covered {cover_qty} {symbol}"]

    def _enforce_symbol(self, symbol: str) -> str | None:
        qty = self.holdings.get(symbol, 0)
        plan = self.exits.get(symbol)
        if plan is None or qty == 0:
            if qty == 0 and symbol in self.exits:
                self.exits.pop(symbol, None)
                self.save()
            return None
        try:
            price = get_share_price(symbol)
        except Exception as error:
            return f"{self.name} price check failed for {symbol}: {error}"
        if price <= 0:
            return None
        fresh = Account.get(self.name)
        if fresh.holdings.get(symbol, 0) != qty:
            return None
        plan = fresh.exits.get(symbol, plan)
        side = "short" if qty < 0 else "long"
        updated = fresh._ratchet(plan, price, side)
        reason = fresh._trigger(updated, price, side)
        if reason is None:
            updated, note = fresh._release_target(updated, price, side, symbol)
            fresh.exits[symbol] = updated
            if updated != plan:
                fresh.save()
            return note
        fresh.exits[symbol] = updated
        close_qty = abs(fresh.holdings.get(symbol, 0))
        try:
            if side == "long":
                fresh.sell_shares(symbol, close_qty, reason)
            else:
                cover_qty = fresh._affordable_cover_qty(symbol, close_qty)
                if cover_qty <= 0:
                    fresh.save()
                    return f"{fresh.name} {reason} on {symbol} but not enough cash to cover"
                fresh.cover_shares(symbol, cover_qty, reason)
        except Exception as error:
            return f"{fresh.name} could not close {symbol}: {error}"
        return f"{fresh.name} {reason} on {close_qty} {symbol}"
