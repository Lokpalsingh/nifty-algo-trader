"""
A broker that replays history. It implements the same interface as the Kotak
adapter, so the backtest runs the REAL strategy code with no changes.
"""
from datetime import date
from typing import Dict, List, Optional

from trader.backtest.option_model import next_weekly_expiry, option_price_t, years_to_expiry
from trader.brokers.base import Broker, BrokerError
from trader.models import (
    BUY, CANCELLED, COMPLETE, LIMIT, MARKET, OPEN, SELL, STOP_LIMIT,
    Candle, OptionContract, OrderStatus,
)


class BacktestBroker(Broker):
    name = "Backtest"
    is_simulated = True

    def __init__(self, lot_size: int = 65, iv: float = 0.14, slippage: float = 1.0,
                 capital: float = 300000.0, expiry_weekday: Optional[int] = None, strike_step: int = 50,
                 time_model: str = "calendar"):
        self.lot_size = lot_size
        self.time_model = time_model
        self.iv = iv                      # used when the day's India VIX isn't known
        self.day_iv = iv
        self.slippage = slippage          # rupees lost on each entry and exit
        self.capital = capital
        self.expiry_weekday = expiry_weekday
        self.strike_step = strike_step
        self.now = None                   # set by the runner for every tick
        self.spot = 0.0
        self.prev_candle: Optional[Candle] = None
        self._orders: Dict[str, dict] = {}
        self._net: Dict[str, int] = {}
        self._next_id = 1

    # ---- the runner drives these ----
    def set_time(self, now, spot: float):
        self.now = now
        self.spot = spot

    def set_day(self, prev_candle: Candle, day_bars=None, vix: Optional[float] = None):
        history = getattr(self, "daily_history", [])
        if prev_candle is not None and (not history or history[-1].date != prev_candle.date):
            history.append(prev_candle)
        self.daily_history = history[-60:]
        self.prev_candle = prev_candle
        self.day_bars = day_bars or []
        self.day_vix = vix
        self.day_iv = vix / 100.0 if vix else self.iv
        self._bar_pos = 0
        self._served = []
        self._orders.clear()
        self._net.clear()

    # ---- session ----
    def login(self, totp_code=None):
        pass

    def is_logged_in(self):
        return True

    # ---- market data ----
    def get_index_ltp(self, index: str) -> float:
        return self.spot

    def get_option_ltp(self, contract: OptionContract) -> float:
        key = (self.now, contract.expiry)
        if key != getattr(self, "_t_key", None):          # same moment -> same time to expiry
            self._t_key = key
            self._t = years_to_expiry(self.now, date.fromisoformat(contract.expiry), self.time_model)
        return option_price_t(self.spot, contract.strike, contract.option_type, self._t, self.day_iv)

    def get_previous_day_candle(self, index: str, today: date):
        return self.prev_candle

    def get_daily_candles(self, index: str, days: int = 25):
        return list(getattr(self, "daily_history", []))[-days:]

    def get_minute_candles(self, index: str, since_hhmm: str = "09:15"):
        """Everything up to the current simulated time, built once as the day advances."""
        now_hhmm = self.now.strftime("%H:%M")
        bars = getattr(self, "day_bars", [])
        while self._bar_pos < len(bars) and bars[self._bar_pos].time.strftime("%H:%M") <= now_hhmm:
            bar = bars[self._bar_pos]
            if bar.time.strftime("%H:%M") >= since_hhmm:
                self._served.append(bar)
            self._bar_pos += 1
        return self._served

    def get_expiry(self, index, today, expiry_choice):
        return next_weekly_expiry(today, self.expiry_weekday,
                                  use_next_on_expiry_day=(expiry_choice == "next_on_expiry_day")).isoformat()

    def get_option(self, index, strike, option_type, today, expiry_choice):
        expiry = date.fromisoformat(self.get_expiry(index, today, expiry_choice))
        return OptionContract(symbol=f"NIFTY{expiry:%d%b%y}{int(strike)}{option_type}".upper(),
                              token=f"{int(strike)}{option_type}{expiry}", segment="backtest",
                              strike=float(strike), option_type=option_type,
                              expiry=expiry.isoformat(), lot_size=self.lot_size)

    def find_atm_option(self, index, spot, option_type, today, expiry_choice):
        strike = round(spot / self.strike_step) * self.strike_step
        return self.get_option(index, strike, option_type, today, expiry_choice)

    def get_index_open(self, index):
        bars = getattr(self, "day_bars", [])
        return bars[0].open if bars else None

    def get_vix(self):
        return self.day_vix if self.day_vix else self.iv * 100

    def get_available_capital(self):
        return self.capital

    # ---- orders ----
    def place_order(self, contract, side, qty, order_type, price, trigger_price=0.0, tag=""):
        order_id = f"BT-{self._next_id}"
        self._next_id += 1
        self._orders[order_id] = dict(contract=contract, side=side, qty=qty, type=order_type,
                                      price=price, trigger=trigger_price, status=OPEN, filled=0, avg=0.0)
        self._try_fill(order_id)
        return order_id

    def modify_order(self, order_id, contract, qty, order_type, price, trigger_price=0.0):
        o = self._orders.get(order_id)
        if not o or o["status"] != OPEN:
            raise BrokerError("Order can no longer be modified")
        o.update(qty=qty, type=order_type, price=price, trigger=trigger_price)
        self._try_fill(order_id)
        return order_id

    def _try_fill(self, order_id: str):
        o = self._orders[order_id]
        if o["status"] != OPEN:
            return
        ltp = self.get_option_ltp(o["contract"])
        fill_at = None
        if o["type"] == MARKET:
            fill_at = ltp
        elif o["type"] == LIMIT:
            if o["side"] == BUY and ltp <= o["price"]:
                fill_at = ltp
            elif o["side"] == SELL and ltp >= o["price"]:
                fill_at = ltp
        elif o["type"] == STOP_LIMIT and o["side"] == SELL:
            if ltp <= o["trigger"] and ltp >= o["price"]:
                fill_at = min(ltp, o["trigger"])
        if fill_at is None:
            return
        # slippage: you buy a little higher and sell a little lower than you see
        fill_at = fill_at + self.slippage if o["side"] == BUY else max(0.05, fill_at - self.slippage)
        o.update(status=COMPLETE, filled=o["qty"], avg=round(fill_at, 2))
        token = o["contract"].token
        self._net[token] = self._net.get(token, 0) + (o["qty"] if o["side"] == BUY else -o["qty"])

    def cancel_order(self, order_id: str) -> None:
        o = self._orders.get(order_id)
        if o and o["status"] == OPEN:
            o["status"] = CANCELLED

    def get_order_status(self, order_id: str) -> OrderStatus:
        if order_id not in self._orders:
            raise BrokerError(f"Unknown order {order_id}")
        self._try_fill(order_id)
        o = self._orders[order_id]
        return OrderStatus(order_id, o["status"], o["filled"], o["avg"])

    def get_open_quantity(self, contract: OptionContract) -> int:
        return self._net.get(contract.token, 0)
