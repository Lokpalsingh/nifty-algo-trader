"""
Paper (dry-run) broker: real or demo market prices, but orders are only
simulated on your computer. Nothing is ever sent to an exchange.

- Paper mode:  prices come from Kotak (live), orders are simulated here.
- Demo mode:   prices come from demo_market.py (fake), orders simulated here.
"""
import itertools
import threading
from datetime import date
from typing import Dict, Optional

from trader.brokers.base import Broker, BrokerError
from trader.models import (
    BUY, CANCELLED, COMPLETE, LIMIT, MARKET, OPEN, SELL, STOP_LIMIT,
    OptionContract, OrderStatus,
)


class PaperBroker(Broker):
    is_simulated = True

    def __init__(self, market: Broker, name: str = "Paper"):
        self.market = market          # where prices come from
        self.name = name
        self._orders: Dict[str, dict] = {}
        self._net_qty: Dict[str, int] = {}
        self._contracts: Dict[str, OptionContract] = {}
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

    # ---- session & data: passed straight through to the market source ----
    def login(self, totp_code: Optional[str] = None) -> None:
        self.market.login(totp_code)

    def is_logged_in(self) -> bool:
        return self.market.is_logged_in()

    def get_index_ltp(self, index: str) -> float:
        return self.market.get_index_ltp(index)

    def get_option_ltp(self, contract: OptionContract) -> float:
        return self.market.get_option_ltp(contract)

    def get_previous_day_candle(self, index: str, today: date):
        return self.market.get_previous_day_candle(index, today)

    def get_minute_candles(self, index: str, since_hhmm: str = "09:15"):
        return self.market.get_minute_candles(index, since_hhmm)

    def get_daily_candles(self, index: str, days: int = 25):
        return self.market.get_daily_candles(index, days)

    def find_atm_option(self, index, spot, option_type, today, expiry_choice):
        return self.market.find_atm_option(index, spot, option_type, today, expiry_choice)

    def get_expiry(self, index, today, expiry_choice):
        return self.market.get_expiry(index, today, expiry_choice)

    def get_option(self, index, strike, option_type, today, expiry_choice):
        return self.market.get_option(index, strike, option_type, today, expiry_choice)

    def get_option_ltps(self, contracts):
        return self.market.get_option_ltps(contracts)

    def get_index_open(self, index):
        return self.market.get_index_open(index)

    def get_vix(self):
        return self.market.get_vix()

    def get_open_option_positions(self, index):
        """Simulated positions only live in memory, so this is empty after a restart."""
        with self._lock:
            return [{"symbol": self._contracts[t].symbol, "token": t, "net_qty": q}
                    for t, q in self._net_qty.items() if q and t in self._contracts]

    def get_available_capital(self):
        return self.market.get_available_capital()

    # ---- simulated orders ----
    def place_order(self, contract, side, qty, order_type, price, trigger_price=0.0, tag=""):
        if qty <= 0:
            raise BrokerError("Quantity must be positive")
        with self._lock:
            order_id = f"PAPER-{next(self._ids)}"
            self._contracts[contract.token] = contract
            self._orders[order_id] = dict(contract=contract, side=side, qty=qty, type=order_type,
                                          price=price, trigger=trigger_price, status=OPEN,
                                          filled=0, avg=0.0)
            self._try_fill(order_id)
            return order_id

    def _try_fill(self, order_id: str) -> None:
        o = self._orders[order_id]
        if o["status"] != OPEN:
            return
        ltp = self.market.get_option_ltp(o["contract"])
        fill = False
        if o["type"] == MARKET:
            fill = True
        elif o["type"] == LIMIT:
            fill = ltp <= o["price"] if o["side"] == BUY else ltp >= o["price"]
        elif o["type"] == STOP_LIMIT:
            if o["side"] == SELL:
                # triggers when price falls to trigger; fills only if still above limit
                fill = ltp <= o["trigger"] and ltp >= o["price"]
            else:
                fill = ltp >= o["trigger"] and ltp <= o["price"]
        if fill:
            o.update(status=COMPLETE, filled=o["qty"], avg=ltp)
            token = o["contract"].token
            delta = o["qty"] if o["side"] == BUY else -o["qty"]
            self._net_qty[token] = self._net_qty.get(token, 0) + delta

    def modify_order(self, order_id, contract, qty, order_type, price, trigger_price=0.0):
        with self._lock:
            o = self._orders.get(order_id)
            if not o or o["status"] != OPEN:
                raise BrokerError(f"Order {order_id} can no longer be modified")
            o.update(qty=qty, type=order_type, price=price, trigger=trigger_price)
            self._try_fill(order_id)
            return order_id

    def cancel_order(self, order_id: str) -> None:
        with self._lock:
            o = self._orders.get(order_id)
            if o and o["status"] == OPEN:
                o["status"] = CANCELLED

    def get_order_status(self, order_id: str) -> OrderStatus:
        with self._lock:
            if order_id not in self._orders:
                raise BrokerError(f"Unknown order {order_id}")
            self._try_fill(order_id)
            o = self._orders[order_id]
            return OrderStatus(order_id, o["status"], o["filled"], o["avg"])

    def get_open_quantity(self, contract: OptionContract) -> int:
        with self._lock:
            return self._net_qty.get(contract.token, 0)
