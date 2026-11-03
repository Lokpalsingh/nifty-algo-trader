"""
Time helpers. Everything runs on Indian time (IST), whatever your laptop's
timezone is set to. Tests and demo mode swap in a fake clock.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


class RealClock:
    def now(self) -> datetime:
        return datetime.now(IST)

    def sleep(self, seconds: float, stop_event=None) -> None:
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            import time as _t
            _t.sleep(seconds)


class FakeClock:
    """Manually controlled clock for tests."""

    def __init__(self, start: datetime):
        self._now = start if start.tzinfo else start.replace(tzinfo=IST)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value if value.tzinfo else value.replace(tzinfo=IST)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def sleep(self, seconds: float, stop_event=None) -> None:
        self.advance(seconds)


class FastClock:
    """Demo mode clock: starts at 9:15 today and runs faster than real time,
    so you can watch a whole trading day in a few minutes."""

    def __init__(self, speed: float = 30.0, start_hhmm: str = "09:15"):
        import time as _t
        self._t = _t
        self.speed = speed
        today = datetime.now(IST).date()
        # Demo always uses a weekday so the strategy doesn't skip the "day"
        while today.weekday() >= 5:
            today -= timedelta(days=1)
        self._start_sim = datetime.combine(today, parse_hhmm(start_hhmm), tzinfo=IST)
        self._start_real = _t.monotonic()

    def now(self) -> datetime:
        elapsed = (self._t.monotonic() - self._start_real) * self.speed
        return self._start_sim + timedelta(seconds=elapsed)

    def sleep(self, seconds: float, stop_event=None) -> None:
        real = seconds / self.speed
        if stop_event is not None:
            stop_event.wait(real)
        else:
            self._t.sleep(real)
