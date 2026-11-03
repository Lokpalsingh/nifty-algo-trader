"""
Builds candles of the option's own price (the "strike price chart") from the
live prices the app already polls every second.

Used by the trailing stop-loss: after each red candle completes (close below
open), the stop-loss moves up to that candle's low.

Prices come from the same source as the trades, so what you see on the
dashboard matches what the stop-loss uses. Your broker's chart may differ by a
tick or two, because it is built from every trade on the exchange while this is
built from the prices the app polls.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class PriceCandle:
    start: str          # "HH:MM"
    open: float
    high: float
    low: float
    close: float

    @property
    def is_red(self) -> bool:
        return self.close < self.open

    @property
    def is_green(self) -> bool:
        return self.close > self.open


class CandleBuilder:
    """Turns a stream of prices into completed candles of the chosen length."""

    def __init__(self, minutes: int = 1, keep: int = 60):
        self.minutes = max(1, minutes)
        self.keep = keep
        self.completed: List[PriceCandle] = []
        self._current: Optional[PriceCandle] = None
        self._bucket: Optional[str] = None

    def _bucket_of(self, when: datetime) -> str:
        minute = (when.minute // self.minutes) * self.minutes
        return f"{when.hour:02d}:{minute:02d}"

    def add(self, when: datetime, price: float) -> Optional[PriceCandle]:
        """Add a price. Returns the candle that just completed, if any."""
        bucket = self._bucket_of(when)
        finished = None
        if self._bucket != bucket:
            if self._current is not None:
                self.completed.append(self._current)
                self.completed = self.completed[-self.keep:]
                finished = self._current
            self._current = PriceCandle(bucket, price, price, price, price)
            self._bucket = bucket
        else:
            c = self._current
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
        return finished

    @property
    def current(self) -> Optional[PriceCandle]:
        return self._current

    def last_red(self) -> Optional[PriceCandle]:
        for candle in reversed(self.completed):
            if candle.is_red:
                return candle
        return None

    def last_green(self, after_bucket: str = "") -> Optional[PriceCandle]:
        """Most recent completed green candle that started after `after_bucket` ("HH:MM")."""
        for candle in reversed(self.completed):
            if candle.start <= after_bucket:
                return None
            if candle.is_green:
                return candle
        return None

    def bucket_of(self, when: datetime) -> str:
        return self._bucket_of(when)

    def reset(self) -> None:
        self.completed.clear()
        self._current = None
        self._bucket = None
