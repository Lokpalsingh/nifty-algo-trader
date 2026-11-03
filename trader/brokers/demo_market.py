"""
Fake market for Demo mode. Lets you watch the whole system work (entries,
stop-losses, re-entries, time exit, loss limit) without a Kotak account and
outside market hours. NIFTY moves randomly; option prices come from the
Black-Scholes formula at a random demo VIX. Nothing here is real.
"""
import random
import threading
from datetime import date, timedelta
from typing import Optional

from trader.brokers.base import Broker
from trader.models import Candle, OptionContract


class DemoMarket(Broker):
    name = "Demo market"
    is_simulated = True
    DEMO_LOT_SIZE = 65
    DEMO_CAPITAL = 200000.0

    def __init__(self, clock, seed: Optional[int] = None):
        self.clock = clock
        self.rng = random.Random(seed)
        self._lock = threading.Lock()
        prev_close = round(self.rng.uniform(24000, 25000), 2)
        prev_open = prev_close + self.rng.choice([-1, 1]) * self.rng.uniform(40, 160)
        body_hi, body_lo = max(prev_open, prev_close), min(prev_open, prev_close)
        self.prev = Candle(
            date="", open=round(prev_open, 2),
            high=round(body_hi + self.rng.uniform(10, 60), 2),
            low=round(body_lo - self.rng.uniform(10, 60), 2),
            close=prev_close, source="Demo (random)",
        )
        self.level = self.prev.high if self.prev.color == "GREEN" else self.prev.low
        # start a little on the "inside" of the level so a real cross can happen
        side = -1 if self.prev.color == "GREEN" else 1
        self._spot = self.level + side * self.rng.uniform(15, 40)
        self._last_time = None
        self._minutes = []          # 1-minute candles built as the demo day runs
        # some demo days trend away from the level (winning trades), some chop around it (stop-losses)
        self._trend_per_hour = self.rng.choice([-1, 1]) * self.rng.uniform(0, 70)
        self._day_start = None
        self._day_open = self._spot
        self.iv = round(self.rng.uniform(0.11, 0.17), 3)     # demo India VIX between 11 and 17

    def login(self, totp_code=None):
        pass

    def is_logged_in(self):
        return True

    def _advance(self):
        now = self.clock.now()
        if self._last_time is None:
            self._last_time = now
            self._day_start = now
            return
        seconds = int((now - self._last_time).total_seconds())
        if seconds <= 0:
            return
        for i in range(min(seconds, 3600)):
            self._last_time += timedelta(seconds=1)
            # wanders around the breakout level so the demo shows plenty of action
            hours = (self._last_time - self._day_start).total_seconds() / 3600
            target = self.level + self._trend_per_hour * hours
            pull = 0.002 * (target - self._spot)
            self._spot += pull + self.rng.gauss(0, 1.1)
            self._record_minute(self._last_time, self._spot)
        self._last_time = now

    def _record_minute(self, when, price):
        from trader.backtest.history import Bar
        stamp = when.replace(second=0, microsecond=0)
        if self._minutes and self._minutes[-1].time == stamp:
            c = self._minutes[-1]
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.volume += self.rng.uniform(800, 1500)
        else:
            self._minutes.append(Bar(stamp, price, price, price, price, self.rng.uniform(800, 1500)))
            self._minutes = self._minutes[-500:]

    def get_index_ltp(self, index: str) -> float:
        with self._lock:
            self._advance()
            return round(self._spot, 2)

    def get_option_ltp(self, contract: OptionContract) -> float:
        from trader.backtest.option_model import option_price
        with self._lock:
            self._advance()
            return option_price(self._spot, contract.strike, contract.option_type, self.clock.now(),
                                date.fromisoformat(contract.expiry), self.iv)

    def get_minute_candles(self, index: str, since_hhmm: str = "09:15"):
        from trader.backtest.history import Bar
        with self._lock:
            self._advance()
        out = []
        for c in self._minutes:
            if c.time.strftime("%H:%M") >= since_hhmm:
                out.append(c)
        return out

    def get_daily_candles(self, index: str, days: int = 25):
        """Made-up daily history, deliberately wider than the demo's own previous day,
        so the narrow-day strategy has something to trade in Demo mode."""
        prev_range = self.prev.high - self.prev.low
        out = []
        base = self._spot
        for i in range(days, 0, -1):
            rng = prev_range * self.rng.uniform(1.1, 2.5)
            open_price = base - i * self.rng.uniform(-30, 30)
            high = open_price + rng * 0.6
            low = high - rng
            close = low + rng * self.rng.uniform(0.2, 0.8)
            out.append(Candle((self.clock.now().date() - timedelta(days=i)).isoformat(),
                              round(open_price, 2), round(high, 2), round(low, 2), round(close, 2),
                              "Demo (random)"))
        return out

    def get_previous_day_candle(self, index: str, today: date):
        prev_day = today - timedelta(days=1)
        while prev_day.weekday() >= 5:
            prev_day -= timedelta(days=1)
        self.prev.date = prev_day.isoformat()
        return self.prev

    def get_expiry(self, index, today, expiry_choice):
        from trader.backtest.option_model import next_weekly_expiry
        return next_weekly_expiry(today, use_next_on_expiry_day=(expiry_choice == "next_on_expiry_day")).isoformat()

    def get_option(self, index, strike, option_type, today, expiry_choice):
        strike = float(strike)
        return OptionContract(symbol=f"DEMO-NIFTY-{int(strike)}{option_type}",
                              token=f"demo-{int(strike)}{option_type}", segment="demo", strike=strike,
                              option_type=option_type, expiry=self.get_expiry(index, today, expiry_choice),
                              lot_size=self.DEMO_LOT_SIZE)

    def find_atm_option(self, index, spot, option_type, today, expiry_choice):
        return self.get_option(index, round(spot / 50) * 50, option_type, today, expiry_choice)

    def get_index_open(self, index):
        with self._lock:
            self._advance()
            return round(self._day_open, 2)

    def get_vix(self):
        return round(self.iv * 100, 2)

    def get_available_capital(self):
        return self.DEMO_CAPITAL

    # Demo market never receives orders directly; PaperBroker wraps it.
    def place_order(self, *a, **k):
        raise NotImplementedError

    def cancel_order(self, order_id):
        raise NotImplementedError

    def get_order_status(self, order_id):
        raise NotImplementedError

    def get_open_quantity(self, contract):
        return 0
