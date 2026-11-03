"""
Tests for the narrow-day opening range strategy (strategy = "narrow_orb").
Scripted prices, no internet and no Kotak account.
"""
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trader.backtest.history as history_module
from trader.alerts import AlertHub
from trader.backtest.history import Bar
from trader.brokers.base import Broker
from trader.brokers.paper import PaperBroker
from trader.clock import IST, FakeClock
from trader.config import Settings
from trader.journal import Journal
from trader.models import Candle, OptionContract
from trader.risk import RiskManager
from trader.strategy_orb import NarrowOrb

DAY = date(2026, 9, 16)          # a Wednesday


def at(hhmm: str, sec: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, sec, tzinfo=IST)


NARROW = Candle("2026-09-15", open=24500, high=24525, low=24475, close=24510)   # range 50
WIDE = Candle("2026-09-15", open=24500, high=24700, low=24300, close=24610)     # range 400


class OrbMarket(Broker):
    """Deep in-the-money options are priced as intrinsic value plus 20."""
    name = "scripted-orb"
    is_simulated = True

    def __init__(self, prev: Candle, spot: float, minute_bars=None):
        self.prev = prev
        self.spot = spot
        self.minute_bars = minute_bars or []
        self.lot_size = 65

    def login(self, totp_code=None): pass
    def is_logged_in(self): return True
    def get_index_ltp(self, index): return self.spot
    def get_previous_day_candle(self, index, today): return self.prev

    def get_daily_candles(self, index, days=25):
        # twenty days with a 200-point range each: a 50-point day is clearly narrow
        return [Candle(f"2026-08-{i + 1:02d}", 24400, 24500, 24300, 24450) for i in range(20)]

    def get_minute_candles(self, index, since_hhmm="09:15"):
        return self.minute_bars

    def find_atm_option(self, index, spot, option_type, today, expiry_choice):
        k = round(spot / 50) * 50
        return OptionContract(f"NIFTY{k}{option_type}", f"{k}{option_type}", "nse_fo", k, option_type,
                              "2026-09-22", self.lot_size)

    def get_option_ltp(self, c):
        intrinsic = (self.spot - c.strike) if c.option_type == "CE" else (c.strike - self.spot)
        return round(max(0.05, intrinsic + 20), 2)

    def get_available_capital(self): return 1_000_000.0
    def place_order(self, *a, **k): raise NotImplementedError
    def cancel_order(self, order_id): raise NotImplementedError
    def get_order_status(self, order_id): raise NotImplementedError
    def get_open_quantity(self, contract): return 0


class OrbTestCase(unittest.TestCase):
    """Every ORB test starts with NO history files.

    This is not tidiness. The suite passed on a machine with an empty data/history
    and failed on one that held 1,236 real daily candles: the strategy
    answered from his live file and the scripted case under test never ran. A test
    that reads whatever happens to be on the disk is not a test. Any case that wants
    a file puts one there itself, as TestSavedDailyFile does."""

    def setUp(self):
        self._real_loaders = (history_module.load_daily, history_module.load_bars)
        history_module.load_daily = lambda index="NIFTY": []
        history_module.load_bars = lambda index="NIFTY", interval="1min": []

    def tearDown(self):
        history_module.load_daily, history_module.load_bars = self._real_loaders

    def make(self, prev=NARROW, spot=24500.0, minute_bars=None, **overrides):
        settings = Settings(strategy="narrow_orb", capital_source="manual", manual_capital=1_000_000.0,
                            sl_grace_seconds=3, fill_timeout_seconds=2, max_trades_per_day=1)
        for k, v in overrides.items():
            setattr(settings, k, v)
        settings.validate()
        self.clock = FakeClock(at("09:15"))
        self.market = OrbMarket(prev, spot, minute_bars)
        self.broker = PaperBroker(self.market)
        self.journal = Journal(Path(tempfile.mkdtemp()), mode="paper")
        self.alerts = AlertHub(clock=self.clock)
        self.risk = RiskManager(settings)
        self.strategy = NarrowOrb(self.broker, self.risk, self.journal, self.alerts, settings,
                                  self.clock, mode="paper", persist=False)
        return self.strategy

    def tick(self, hhmm, spot=None, sec=0):
        self.clock.set(at(hhmm, sec))
        if spot is not None:
            self.market.spot = spot
        self.strategy.tick()

    def build_range(self):
        """09:16 to 09:29: NIFTY moves between 24,480 and 24,520."""
        self.tick("09:16", spot=24500)
        for minute, spot in (("09:18", 24520), ("09:22", 24480), ("09:27", 24505)):
            self.tick(minute, spot=spot)


class TestPlan(OrbTestCase):
    def test_narrow_day_qualifies(self):
        s = self.make(NARROW)
        self.tick("09:16")
        self.assertTrue(s.plan["narrow"])
        self.assertEqual(s.done_reason, "")

    def test_wide_day_is_skipped(self):
        s = self.make(WIDE)
        self.tick("09:16")
        self.assertFalse(s.plan["narrow"])
        self.tick("09:31", spot=24600)
        self.assertIsNone(s.position)
        self.assertIn("not a narrow day", s.done_reason)


class TestDataProblems(OrbTestCase):
    def test_short_daily_history_skips_the_day_and_says_why(self):
        s = self.make()
        self.market.get_daily_candles = lambda index, days=25: []
        s._ranges_from_saved_history = lambda days: []
        self.tick("09:16")
        self.assertIn("Not enough daily history", s.done_reason)
        self.assertIn("0 days of daily history", s.plan["text"])

    def test_saved_history_is_used_when_the_broker_is_short(self):
        s = self.make()
        self.market.get_daily_candles = lambda index, days=25: []
        s._ranges_from_saved_history = lambda days: [200.0] * 20
        self.tick("09:16")
        self.assertTrue(s.plan["narrow"])          # yesterday's 50-point range is narrow vs 200
        self.assertEqual(s.done_reason, "")


class TestOpeningRange(OrbTestCase):
    def test_range_from_live_prices(self):
        s = self.make()
        self.build_range()
        self.assertIsNone(s.or_high)          # not finished before 09:30
        self.tick("09:31", spot=24510)
        self.assertAlmostEqual(s.or_high, 24520)
        self.assertAlmostEqual(s.or_low, 24480)
        self.assertIsNone(s.position)         # inside the range, no trade yet

    def test_range_from_broker_candles(self):
        from trader.backtest.history import Bar
        bars = [Bar(at("09:15") + timedelta(minutes=i), 24500, 24540, 24460, 24500) for i in range(15)]
        s = self.make(minute_bars=bars)
        self.tick("09:16")
        self.tick("09:31", spot=24500)
        self.assertAlmostEqual(s.or_high, 24540)
        self.assertAlmostEqual(s.or_low, 24460)


class TestEntries(OrbTestCase):
    def enter_long(self):
        s = self.make()
        self.build_range()
        self.tick("09:31", spot=24510)        # inside the range
        self.tick("09:35", spot=24530)        # breaks above 24,520
        return s

    def test_break_above_buys_deep_itm_ce(self):
        s = self.enter_long()
        self.assertIsNotNone(s.position)
        self.assertEqual(s.position.contract.option_type, "CE")
        self.assertEqual(s.position.contract.strike, 24250)     # 6 strikes in the money
        self.assertAlmostEqual(s.entry_spot, 24530)

    def test_break_below_buys_deep_itm_pe(self):
        s = self.make()
        self.build_range()
        self.tick("09:35", spot=24470)        # breaks below 24,480
        self.assertEqual(s.position.contract.option_type, "PE")
        self.assertEqual(s.position.contract.strike, 24750)     # 6 strikes in the money

    def test_no_break_by_the_cutoff(self):
        s = self.make()
        self.build_range()
        self.tick("09:31", spot=24500)
        self.tick("14:00", spot=24500)
        self.assertIsNone(s.position)
        self.assertIn("No break by 14:00", s.done_reason)

    def test_only_one_trade_a_day(self):
        s = self.enter_long()
        self.tick("09:40", spot=24500)        # index stop hit
        self.assertIsNone(s.position)
        self.tick("09:50", spot=24560)        # breaks again, but the day is over
        self.assertIsNone(s.position)
        self.assertEqual(s.trades_today, 1)


class TestExits(OrbTestCase):
    def enter_long(self, **overrides):
        s = self.make(**overrides)
        self.build_range()
        self.tick("09:35", spot=24530)
        self.assertIsNotNone(s.position)
        return s

    def test_index_stop_loss(self):
        s = self.enter_long()
        self.tick("10:00", spot=24499)        # 31 points against the entry
        self.assertIsNone(s.position)
        row = self.journal.trades()[-1]
        self.assertIn("Index stop-loss", row["exit_reason"])
        self.assertLess(float(row["pnl"]), 0)

    def test_index_target(self):
        s = self.enter_long()
        self.tick("11:00", spot=24681)        # 151 points in favour
        self.assertIsNone(s.position)
        row = self.journal.trades()[-1]
        self.assertIn("Index target hit", row["exit_reason"])
        self.assertGreater(float(row["pnl"]), 0)

    def test_small_moves_keep_the_trade_open(self):
        s = self.enter_long()
        self.tick("10:00", spot=24510)        # 20 points against: inside the stop
        self.assertIsNotNone(s.position)
        self.tick("10:30", spot=24600)        # 70 points ahead: short of the target
        self.assertIsNotNone(s.position)

    def test_time_exit_at_1514(self):
        s = self.enter_long()
        self.tick("15:14", spot=24540)
        self.assertIsNone(s.position)
        self.assertIn("Time exit", self.journal.trades()[-1]["exit_reason"])

    def test_stop_and_target_follow_the_settings(self):
        s = self.enter_long(orb_stop_points=50.0, orb_target_r=3.0)
        self.tick("10:00", spot=24490)        # 40 against: inside a 50-point stop
        self.assertIsNotNone(s.position)
        self.tick("10:05", spot=24479)        # 51 against
        self.assertIsNone(s.position)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# The saved daily file (data/history/NIFTY_D.csv)
#
# On 16 Oct 2026 that file held 1,236 days while Kotak was rate-limiting the live
# call down to 20, and the strategy used the 20. The file is preferred now, but
# only while it is current: a stale file would set the narrow-day line from last
# month's volatility, which is worse than a throttled live call.
# --------------------------------------------------------------------------- #
def saved_days(count: int, day_range: float, newest: date):
    """`count` weekday bars of exactly `day_range` points, newest last."""
    out, d = [], newest
    while len(out) < count:
        if d.weekday() < 5:
            out.append(Bar(datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST),
                           24000.0, 24000.0 + day_range, 24000.0, 24000.0 + day_range / 2))
        d -= timedelta(days=1)
    return list(reversed(out))


class TestSavedDailyFile(OrbTestCase):
    def setUp(self):
        super().setUp()
        self.broker_calls = []

    def use_file(self, day_range, newest, count=20):
        history_module.load_daily = lambda index="NIFTY": saved_days(count, day_range, newest)

    def watch_broker(self):
        real = self.market.get_daily_candles

        def counted(index, days=25):
            self.broker_calls.append(days)
            return real(index, days)
        self.market.get_daily_candles = counted

    def test_a_current_file_decides_and_the_broker_is_left_alone(self):
        """The file says 30-point days, so yesterday's 50 is NOT narrow - the opposite
        of what the broker's 200-point days would say. Whichever verdict appears tells
        us which source was used."""
        s = self.make(NARROW)
        self.watch_broker()
        self.use_file(30.0, newest=DAY - timedelta(days=1))
        self.tick("09:16")
        self.assertFalse(s.plan["narrow"], "the file's 30-point line should have decided this")
        self.assertAlmostEqual(s.plan["narrow_cutoff"], 30.0)
        self.assertEqual(self.broker_calls, [], "no live call is needed when the file is current")

    def test_a_stale_file_is_ignored(self):
        s = self.make(NARROW)
        self.watch_broker()
        self.use_file(30.0, newest=DAY - timedelta(days=40))
        self.tick("09:16")
        self.assertTrue(s.plan["narrow"], "a 40-day-old file must not set today's line")
        self.assertTrue(self.broker_calls, "a stale file means the broker gets asked")

    def test_a_short_file_is_ignored(self):
        s = self.make(NARROW)
        self.watch_broker()
        self.use_file(30.0, newest=DAY - timedelta(days=1), count=5)
        self.tick("09:16")
        self.assertTrue(s.plan["narrow"], "five days cannot set a percentile line")
        self.assertTrue(self.broker_calls)

    def test_a_missing_file_falls_through_to_the_broker(self):
        s = self.make(NARROW)
        self.watch_broker()
        history_module.load_daily = lambda index="NIFTY": []
        self.tick("09:16")
        self.assertTrue(s.plan["narrow"])
        self.assertTrue(self.broker_calls)

    def test_an_unreadable_file_does_not_cancel_the_day(self):
        def boom(index="NIFTY"):
            raise OSError("disk is sulking")
        s = self.make(NARROW)
        self.watch_broker()
        history_module.load_daily = boom
        self.tick("09:16")
        self.assertTrue(s.plan["narrow"], "a broken file must fall through, not stop trading")
        self.assertEqual(s.done_reason, "")


# --------------------------------------------------------------------------- #
# Which day is "yesterday"?
#
# 22 Oct 2026: Kotak was rate-limited, the Yahoo fallback returned 19 Oct, and the
# plan announced "yesterday's range was 330 points". 330 was 19 Oct's range; 21 Oct
# had traded and its range was 471. The saved daily file had 21 Oct the whole time.
# Nobody noticed for four days because the message never said which day it meant.
# --------------------------------------------------------------------------- #
def saved_file_ending(newest: date, count=20, day_range=200.0):
    out, d = [], newest
    while len(out) < count:
        if d.weekday() < 5:
            out.append(Bar(datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST),
                           24000.0, 24000.0 + day_range, 24000.0, 24000.0 + day_range / 2))
        d -= timedelta(days=1)
    return list(reversed(out))


def candle_on(day: str, day_range: float = 50.0, source: str = "Kotak"):
    return Candle(day, open=24500, high=24500 + day_range * 0.5,
                  low=24500 - day_range * 0.5, close=24510, source=source)


class TestWhichDayIsYesterday(OrbTestCase):
    def tick_on(self, day: date, hhmm: str = "09:16"):
        h, m = map(int, hhmm.split(":"))
        self.clock.set(datetime(day.year, day.month, day.day, h, m, tzinfo=IST))
        self.strategy.tick()

    def warnings(self):
        return [a["message"] for a in self.alerts.recent() if a["level"] == "WARNING"]

    def test_the_plan_names_the_day_it_used(self):
        """The whole bug in one line: say which day, not "yesterday"."""
        s = self.make(candle_on("2026-09-15"))
        self.tick_on(date(2026, 9, 16))
        self.assertIn("Tue 15 Sep", s.plan["text"])
        self.assertEqual(s.plan["candle_date"], "2026-09-15")
        self.assertEqual(s.plan["candle_days_behind"], 0)
        self.assertEqual(self.warnings(), [])

    def test_the_fresher_source_wins(self):
        """The broker offers 11 Sep, the saved file has 15 Sep. Use 15 Sep."""
        s = self.make(candle_on("2026-09-11", day_range=400.0))
        history_module.load_daily = lambda index="NIFTY": saved_file_ending(date(2026, 9, 15))
        self.tick_on(date(2026, 9, 16))
        self.assertEqual(s.plan["candle_date"], "2026-09-15")
        self.assertIn("saved daily file", s.plan["candle_source"])
        self.assertIn("Tue 15 Sep", s.plan["text"])
        self.assertEqual(self.warnings(), [], "15 Sep is not stale, so no warning")

    def test_a_candle_a_few_days_behind_warns_but_still_trades(self):
        """Fri 11 Sep when Tue 15 Sep was the last trading day: four days behind,
        usable, but it must say so out loud - that alert goes to Telegram."""
        s = self.make(candle_on("2026-09-11"))
        self.tick_on(date(2026, 9, 16))
        self.assertEqual(s.plan["candle_days_behind"], 4)
        self.assertTrue(s.plan["narrow"], "a 50-point range is still narrow")
        self.assertEqual(s.done_reason, "")
        warned = " ".join(self.warnings())
        self.assertIn("Fri 11 Sep", warned)
        self.assertIn("Tue 15 Sep", warned)
        self.assertIn("4 days behind", warned)

    def test_a_badly_stale_candle_cancels_the_day(self):
        """Past four days the data is not worth trading on."""
        s = self.make(candle_on("2026-09-07"))
        self.tick_on(date(2026, 9, 16))
        self.assertEqual(s.done_reason, "Yesterday's candle is too stale")
        self.assertIn("too stale", s.plan["text"])
        self.assertIn("Mon 07 Sep", s.plan["text"])
        self.assertIsNone(s.plan.get("narrow"))

    def test_no_trade_is_taken_after_a_stale_cancel(self):
        s = self.make(candle_on("2026-09-07"))
        self.tick_on(date(2026, 9, 16))
        self.tick_on(date(2026, 9, 16), "09:31")
        self.tick_on(date(2026, 9, 16), "09:45")
        self.assertIsNone(s.position)

    def test_a_holiday_does_not_make_a_candle_look_stale(self):
        """Mon 14 Sep 2026 is Ganesh Chaturthi. On Tue 15 Sep the last trading day
        was Fri 11 Sep, so an 11 Sep candle is current, not four days behind."""
        s = self.make(candle_on("2026-09-11"))
        self.tick_on(date(2026, 9, 15))
        self.assertEqual(s.plan["candle_days_behind"], 0)
        self.assertEqual(self.warnings(), [])
        self.assertIn("Fri 11 Sep", s.plan["text"])

    def test_a_typed_in_candle_is_never_called_stale(self):
        """If you typed it in by hand, you meant it."""
        s = self.make(candle_on("2026-09-07"), use_manual_candle=True)
        s.settings.manual_candle.date = "2026-09-07"
        s.settings.manual_candle.open = 24500
        s.settings.manual_candle.high = 24525
        s.settings.manual_candle.low = 24475
        s.settings.manual_candle.close = 24510
        self.tick_on(date(2026, 9, 16))
        self.assertEqual(s.plan["candle_days_behind"], 0)
        self.assertNotIn("too stale", s.plan["text"])

    def test_no_candle_anywhere_still_fails_the_way_it_did(self):
        s = self.make()
        self.market.get_previous_day_candle = lambda index, today: None
        self.tick_on(date(2026, 9, 16))
        self.assertIn("Could not get yesterday's candle", s.setup_error or s.status_text or "")

    def test_a_broker_that_raises_falls_back_to_the_file(self):
        def boom(index, today):
            raise RuntimeError("Kotak is throttling")
        s = self.make()
        self.market.get_previous_day_candle = boom
        history_module.load_daily = lambda index="NIFTY": saved_file_ending(date(2026, 9, 15))
        self.tick_on(date(2026, 9, 16))
        self.assertEqual(s.plan["candle_date"], "2026-09-15")
        self.assertEqual(s.done_reason, "")

    def test_a_rubbish_date_does_not_crash_the_morning(self):
        s = self.make(candle_on("not-a-date"))
        self.tick_on(date(2026, 9, 16))
        self.assertEqual(s.plan["candle_days_behind"], 0)
        self.assertIn("not-a-date", s.plan["text"])
