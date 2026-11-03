"""
Narrow-day opening range breakout  (settings: strategy = "narrow_orb")
=====================================================================

The idea: a quiet day is often followed by a bigger one, so wait for a narrow day
and then trade the first break of the opening range. Every number below is a
setting in settings.json, so you can test your own values with backtest.py.

  1. Only trade after a NARROW day: yesterday's range must be in the bottom
     30% of the last 20 days. Quiet days are usually followed by bigger ones.
  2. Build the opening range from the first 15 minutes (09:15 to 09:30).
  3. From 09:31 until 14:00, buy when NIFTY closes a minute ABOVE that range
     (buy a CE) or BELOW it (buy a PE). First break only, one trade a day.
  4. The stop-loss and target are in INDEX points, not premium: stop 30 points
     against you, target 150 points (5R) in your favour. Everything closes at
     15:14.
  5. The option bought is deep IN the money (4 strikes by default). A 30-point
     index move then costs about 25 points of premium instead of 15, but the
     option keeps far more of a big move and bleeds much less time value. The
     strike depth matters a lot to the result - test it rather than assume it.

A backup stop-loss order still goes to the broker, placed at roughly the same
level using a delta estimate, in case the app or laptop dies mid-trade. The
app's own exit is the index rule above, whichever comes first.

Honest note: in testing this came out around break-even after option costs and
spread, and it is very sensitive to how deep in the money you buy. Back-test it
yourself before trusting it, and paper trade it before funding it.
"""
import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

from trader import alerts as A
from trader.clock import parse_hhmm
from trader.config import INDEXES, manual_candle_or_none
from trader.models import Candle
from trader.strategy import PrevDayBreakout

log = logging.getLogger("strategy")


class NarrowOrb(PrevDayBreakout):
    """Everything about orders, risk, journal and restarts comes from
    PrevDayBreakout. Only the plan, the entry and the exit rules change."""

    # How many days out of date "yesterday's" candle may be before the day is
    # abandoned. Four covers a weekend plus a holiday.
    MAX_STALE_DAYS = 4

    # ---------------------------------------------------------------- #
    # Day state
    # ---------------------------------------------------------------- #
    def _reset_day_fields(self):
        super()._reset_day_fields()
        self.or_high: Optional[float] = None
        self.or_low: Optional[float] = None
        self.entry_spot: Optional[float] = None
        self.live_high: Optional[float] = None      # fallback range, built from live prices
        self.live_low: Optional[float] = None

    def _extra_state(self) -> dict:
        return {"or_high": self.or_high, "or_low": self.or_low, "entry_spot": self.entry_spot}

    def _restore_extra(self, saved: dict) -> None:
        self.or_high = saved.get("or_high")
        self.or_low = saved.get("or_low")
        self.entry_spot = saved.get("entry_spot")

    # ---------------------------------------------------------------- #
    # Morning plan: is today a "narrow day" follow-up?
    # ---------------------------------------------------------------- #
    def _setup(self, now: datetime):
        s = self.settings
        manual = manual_candle_or_none(s)
        if manual:
            yesterday = Candle(manual.date, manual.open, manual.high, manual.low, manual.close,
                               "Typed in manually")
        else:
            yesterday = self._yesterday_candle(now)
        if yesterday is None:
            self._setup_failed("Could not get yesterday's candle. Type it in under "
                               "Settings > Yesterday's candle.")
            return

        expected = self._previous_trading_day(now.date())
        label = self._day_label(yesterday.date)
        stale_days = 0 if manual else self._days_behind(yesterday.date, expected)

        capital = s.manual_capital if s.capital_source == "manual" else self.broker.get_available_capital()
        if not capital or capital <= 0:
            self._setup_failed("Could not read your capital from the broker. "
                               "Set Capital source to manual in Settings.")
            return
        if self.risk.state.day_capital is None:
            self.risk.state.day_capital = capital

        history = self._daily_ranges(s.index, s.orb_lookback_days)
        plan = {"candle": vars(yesterday).copy(), "color": yesterday.color, "capital": capital,
                "loss_limit": self.risk.loss_limit, "direction": None, "level": None,
                "option_type": None, "candle_date": yesterday.date,
                "candle_source": yesterday.source, "candle_days_behind": stale_days}
        y_range = yesterday.high - yesterday.low

        if stale_days > self.MAX_STALE_DAYS:
            plan["text"] = (f"The newest daily candle anywhere is {label}, but the last trading "
                            f"day was {expected:%a %d %b}. That is {stale_days} days out of date, "
                            f"too stale to judge yesterday. No trade today.")
            self.done_reason = "Yesterday's candle is too stale"
        elif len(history) < 10:
            why = getattr(self.broker, "last_daily_error", "") or getattr(
                getattr(self.broker, "market", None), "last_daily_error", "")
            plan["text"] = (f"Only {len(history)} days of daily history came back, so I can't tell whether "
                            f"yesterday was a narrow day. No trade today."
                            + (f" ({why})" if why else ""))
            self.done_reason = "Not enough daily history"
        else:
            cutoff = self._percentile(history, s.orb_narrow_pct / 100.0)
            plan["narrow"] = y_range <= cutoff
            plan["yesterday_range"] = y_range
            plan["narrow_cutoff"] = cutoff
            if not plan["narrow"]:
                plan["text"] = (f"{label}'s range was {y_range:,.0f} points, above the narrow-day "
                                f"line of {cutoff:,.0f}. No trade today.")
                self.done_reason = "Yesterday was not a narrow day"
            else:
                plan["text"] = (f"{label}'s range was {y_range:,.0f} points, inside the quietest "
                                f"{s.orb_narrow_pct:.0f}% (line {cutoff:,.0f}). Waiting for the first "
                                f"{s.orb_minutes}-minute range, then its break. Stop {s.orb_stop_points:.0f} "
                                f"index points, target {s.orb_stop_points * s.orb_target_r:.0f}.")

        # Say so when the candle is behind but still usable. On 22 Oct 2026 this was
        # silent: the plan said "yesterday's range was 330" when 330 was 19 Oct's
        # range and 21 Oct had traded. Naming the date is most of the fix.
        if 0 < stale_days <= self.MAX_STALE_DAYS:
            days = "1 day" if stale_days == 1 else f"{stale_days} days"
            self.alerts.add(A.WARNING,
                            f"The newest daily candle is {label}, but the last trading day was "
                            f"{expected:%a %d %b} - {days} behind. Judging today on "
                            f"{label} from {yesterday.source or 'an unnamed source'}.")

        self.plan = plan
        self.setup_done = True
        self.setup_error = ""
        self.alerts.add(A.TRADE, f"Today's plan: {plan['text']} Daily loss limit "
                                 f"₹{self.risk.loss_limit:,.0f}.")
        self.save()

    # ---------------------------------------------------------------- #
    # Which day is "yesterday", and is it actually yesterday?
    # ---------------------------------------------------------------- #
    def _yesterday_candle(self, now: datetime) -> Optional[Candle]:
        """The freshest completed daily candle available, from either source.

        On 22 Oct 2026 Kotak was rate-limited, the Yahoo fallback answered with
        19 Oct, and the plan announced "yesterday's range was 330 points" - 19 Oct's
        range, two trading days stale, with no date in the message to give it away.
        The saved daily file had 21 Oct the whole time. So both are asked and the
        newer one wins; dates are ISO strings, so comparing them as text is the same
        as comparing them as dates."""
        today = now.date()
        candidates = []
        try:
            from_broker = self.broker.get_previous_day_candle(self.settings.index, today)
            if from_broker and from_broker.date:
                candidates.append(from_broker)
        except Exception as e:                      # data trouble must not crash the day
            log.warning("Previous day candle from the broker failed: %s", e)
        saved = self._saved_previous_candle(today)
        if saved:
            candidates.append(saved)
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.date)

    def _saved_previous_candle(self, today: date) -> Optional[Candle]:
        """The newest completed day in data/history/NIFTY_D.csv, as a Candle."""
        try:
            from trader.backtest.history import load_daily
            bars = load_daily()
        except Exception as e:
            log.warning("Saved daily history unavailable: %s", e)
            return None
        usable = [b for b in bars if b.time.date() < today and b.high and b.low]
        if not usable:
            return None
        b = usable[-1]
        return Candle(b.time.date().isoformat(), b.open, b.high, b.low, b.close,
                      "the saved daily file")

    def _previous_trading_day(self, today: date) -> date:
        """The last day the market was open before today, holidays included."""
        try:
            from trader.holidays import is_trading_day
        except Exception:                            # no calendar: weekdays only
            def is_trading_day(d):
                return d.weekday() < 5
        day = today - timedelta(days=1)
        for _ in range(15):
            if is_trading_day(day):
                return day
            day -= timedelta(days=1)
        return day

    @staticmethod
    def _day_label(iso_date: str) -> str:
        try:
            return f"{date.fromisoformat(iso_date):%a %d %b}"
        except (ValueError, TypeError):
            return iso_date or "an unknown day"

    @staticmethod
    def _days_behind(iso_date: str, expected: date) -> int:
        """How many days older than `expected` the candle is. Never negative: a
        candle fresher than expected is not a problem."""
        try:
            return max(0, (expected - date.fromisoformat(iso_date)).days)
        except (ValueError, TypeError):
            return 0

    def _daily_ranges(self, index: str, days: int) -> List[float]:
        """Daily ranges for the narrow-day line, oldest first.

        The saved daily file comes first, but only while it is current - a stale file
        would set the line from last month's volatility. "Current" means its newest day
        is within four calendar days, which covers a weekend and a holiday. Otherwise
        the broker is asked, and the 1-minute file is the last resort, so one flaky
        data call can never cancel the day."""
        saved, newest = self._saved_daily_ranges(days)
        if len(saved) >= 10 and newest:
            age = (self.clock.now().date() - date.fromisoformat(newest)).days
            if age <= 4:
                return saved
            log.info("Saved daily file stops at %s (%d days ago), asking the broker",
                     newest, age)

        candles = []
        try:
            candles = self.broker.get_daily_candles(index, days) or []
        except Exception as e:                       # never let data trouble crash the day
            log.warning("Daily candles failed: %s", e)
        ranges = [c.high - c.low for c in candles if c.high and c.low][-days:]
        if len(ranges) >= 10:
            return ranges
        saved = self._ranges_from_saved_history(days)
        if len(saved) > len(ranges):
            self.alerts.add(A.INFO, f"Daily history from the broker was short ({len(ranges)} days), "
                                    f"so the saved 1-minute history was used instead ({len(saved)} days).")
            return saved
        return ranges

    def _saved_daily_ranges(self, days: int) -> tuple:
        """Day ranges from data/history/NIFTY_D.csv, plus the newest day in it.

        This file is the best source we have and it was going unused: on 16 Oct 2026
        it held 1,236 days while the live call was being throttled down to 20. It also
        needs no network at 09:16, which is the one minute of the day when the network
        matters. Returns ([], "") if the file is missing or unreadable."""
        try:
            from trader.backtest.history import load_daily
            bars = load_daily()
        except Exception as e:
            log.warning("Saved daily history unavailable: %s", e)
            return [], ""
        today = self.clock.now().date().isoformat()
        usable = [b for b in bars
                  if b.time.date().isoformat() < today and b.high and b.low]
        if not usable:
            return [], ""
        ranges = [b.high - b.low for b in usable][-days:]
        return ranges, usable[-1].time.date().isoformat()

    def _ranges_from_saved_history(self, days: int) -> List[float]:
        """Fallback: day ranges from data/history/NIFTY_1min.csv, if it is there."""
        try:
            from trader.backtest.history import group_by_day, load_bars
            by_day = group_by_day(load_bars())
        except Exception as e:
            log.warning("Saved history unavailable: %s", e)
            return []
        today = self.clock.now().date().isoformat()
        out = []
        for day in sorted(by_day):
            if day >= today:
                continue
            bars = by_day[day]
            if len(bars) > 100:
                out.append(max(b.high for b in bars) - min(b.low for b in bars))
        return out[-days:]

    @staticmethod
    def _percentile(values: List[float], pct: float) -> float:
        ordered = sorted(values)
        k = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * pct))))
        return ordered[k]

    # ---------------------------------------------------------------- #
    # The opening range
    # ---------------------------------------------------------------- #
    def _range_end(self) -> str:
        start = parse_hhmm("09:15")
        minutes = start.hour * 60 + start.minute + self.settings.orb_minutes
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def _build_opening_range(self, now: datetime, spot: float) -> bool:
        """True once the range is known. Uses the broker's 1-minute candles, and
        falls back to the prices this app has seen if the broker has none."""
        end = self._range_end()
        if now.time() < parse_hhmm(end):
            if self.live_high is None or spot > self.live_high:
                self.live_high = spot
            if self.live_low is None or spot < self.live_low:
                self.live_low = spot
            self.status_text = (f"Building the first {self.settings.orb_minutes} minutes' range "
                                f"(until {end}). NIFTY {spot:,.2f}")
            return False
        if self.or_high is not None:
            return True

        bars = []
        try:
            bars = [b for b in (self.broker.get_minute_candles(self.settings.index, "09:15") or [])
                    if b.time.strftime("%H:%M") < end]
        except Exception as e:
            log.warning("Minute candles failed, using live prices for the range: %s", e)
        if bars:
            self.or_high = max(b.high for b in bars)
            self.or_low = min(b.low for b in bars)
            source = f"{len(bars)} one-minute candles"
        elif self.live_high is not None and self.live_low is not None:
            self.or_high, self.or_low = self.live_high, self.live_low
            source = "prices seen by this app"
        else:
            self._setup_failed(f"No prices for 09:15 to {end}, so the opening range is unknown. "
                               f"No trade today.")
            self.done_reason = "Opening range unknown"
            return False

        self.plan["or_high"] = self.or_high
        self.plan["or_low"] = self.or_low
        self.alerts.add(A.INFO, f"Opening range {self.or_low:,.2f} to {self.or_high:,.2f} "
                                f"({self.or_high - self.or_low:,.0f} points, from {source}). "
                                f"Buying CE above it, PE below it.")
        self.save()
        return True

    # ---------------------------------------------------------------- #
    # Entry
    # ---------------------------------------------------------------- #
    def _look_for_entry(self, now: datetime, spot: float):
        s = self.settings
        if not self.plan.get("narrow"):
            self.done_reason = self.done_reason or "Yesterday was not a narrow day"
            return
        if not self._build_opening_range(now, spot):
            return
        if now.time() >= parse_hhmm(s.orb_entry_end):
            self.done_reason = f"No break by {s.orb_entry_end}"
            self.status_text = self.done_reason
            self.save()
            return
        if self._entry_blocked(now):
            return

        if spot > self.or_high:
            self.plan.update(direction="UP", level=self.or_high, option_type="CE")
            self._enter(now, spot)
            return
        if spot < self.or_low:
            self.plan.update(direction="DOWN", level=self.or_low, option_type="PE")
            self._enter(now, spot)
            return
        self.status_text = (f"Waiting for NIFTY to leave {self.or_low:,.2f} - {self.or_high:,.2f} "
                            f"(now {spot:,.2f})")

    def _entry_window(self):
        """Watch from the open, because the opening range has to be built first."""
        return "09:15", self.settings.exit_time

    def _strike_shift(self, option_type: str) -> float:
        """Deep in the money: a CE below the spot, a PE above it."""
        step = INDEXES[self.settings.index]["strike_step"]
        return -self.settings.orb_itm_steps * step * (1 if option_type == "CE" else -1)

    def _stop_distance(self, price: float) -> float:
        """Backup stop at the broker, in premium: the index stop times a delta guess.
        The real exit is the index rule in _extra_exit_reason."""
        s = self.settings
        return max(1.0, s.orb_stop_points * s.orb_delta_guess)

    def _on_entered(self, pos, spot: float) -> None:
        self.entry_spot = spot
        long_side = pos.contract.option_type == "CE"
        stop = self.settings.orb_stop_points
        target = stop * self.settings.orb_target_r
        self.alerts.add(A.INFO,
                        f"Index stop {spot - stop if long_side else spot + stop:,.2f}, "
                        f"index target {spot + target if long_side else spot - target:,.2f} "
                        f"(NIFTY now {spot:,.2f}).")

    # ---------------------------------------------------------------- #
    # Exit: the stop and target are on the INDEX, not the premium
    # ---------------------------------------------------------------- #
    def _extra_exit_reason(self, now: datetime, ltp: float) -> Optional[str]:
        s = self.settings
        pos = self.position
        if pos is None or self.entry_spot is None or self.last_spot is None:
            return None
        move = self.last_spot - self.entry_spot
        if pos.contract.option_type == "PE":
            move = -move
        if move <= -s.orb_stop_points:
            return f"Index stop-loss ({s.orb_stop_points:.0f} points)"
        target = s.orb_stop_points * s.orb_target_r
        if move >= target:
            return f"Index target hit ({target:.0f} points)"
        return None

    def open_position_text(self) -> str:
        text = super().open_position_text()
        if text and self.entry_spot:
            text += f", NIFTY was {self.entry_spot:,.2f} at entry"
        return text

    def snapshot(self) -> dict:
        snap = super().snapshot()
        s = self.settings
        snap["orb"] = {"high": self.or_high, "low": self.or_low, "entry_spot": self.entry_spot,
                       "stop_points": s.orb_stop_points,
                       "target_points": s.orb_stop_points * s.orb_target_r,
                       "narrow": bool((self.plan or {}).get("narrow"))}
        return snap
