"""Tests for the backtester and the cost model."""
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.backtest.costs import CostModel
from trader.backtest.history import Bar
from trader.backtest.option_model import next_weekly_expiry, option_price
from trader.backtest.runner import run_backtest
from trader.clock import IST
from trader.config import Settings


def make_day(day: date, path):
    """One trading day of 1-minute bars from a list of prices."""
    start = datetime.combine(day, datetime.min.time().replace(hour=9, minute=15), tzinfo=IST)
    bars = []
    for i, price in enumerate(path):
        prev = path[i - 1] if i else price
        bars.append(Bar(start + timedelta(minutes=i), prev, max(prev, price), min(prev, price), price))
    return bars


class TestCosts(unittest.TestCase):
    def test_round_trip_charges(self):
        # 1 lot (65), bought at 120, sold at 140
        charge = CostModel().round_trip(120, 140, 65)
        self.assertGreater(charge, 50)      # brokerage alone is 40
        self.assertLess(charge, 120)

    def test_charges_grow_with_size(self):
        one = CostModel().round_trip(120, 140, 65)
        ten = CostModel().round_trip(120, 140, 650)
        self.assertGreater(ten, one * 2)


class TestOptionModel(unittest.TestCase):
    def test_atm_call_and_put_are_close(self):
        now = datetime(2026, 9, 17, 10, 0, tzinfo=IST)
        expiry = date(2026, 9, 22)
        ce = option_price(23300, 23300, "CE", now, expiry, 0.14)
        pe = option_price(23300, 23300, "PE", now, expiry, 0.14)
        self.assertLess(abs(ce - pe), 25)
        self.assertGreater(ce, 50)

    def test_call_rises_with_the_index(self):
        now = datetime(2026, 9, 17, 10, 0, tzinfo=IST)
        expiry = date(2026, 9, 22)
        low = option_price(23300, 23300, "CE", now, expiry, 0.14)
        high = option_price(23400, 23300, "CE", now, expiry, 0.14)
        self.assertGreater(high, low)

    def test_expiry_is_next_tuesday(self):
        self.assertEqual(next_weekly_expiry(date(2026, 9, 17)), date(2026, 9, 22))   # Thu -> Tue
        self.assertEqual(next_weekly_expiry(date(2026, 9, 22)), date(2026, 9, 22))   # Tue itself
        self.assertEqual(next_weekly_expiry(date(2026, 9, 22), use_next_on_expiry_day=True),
                         date(2026, 9, 29))


class TestRunner(unittest.TestCase):
    def build(self):
        """Day 1 green (close above open), day 2 breaks above day 1's high."""
        day1 = make_day(date(2026, 9, 16), [23000 + i * 0.4 for i in range(375)])       # 23000 -> 23150
        flat = [23100] * 30
        rally = [23100 + i * 2 for i in range(60)]        # crosses 23149.6 quickly
        after = [23220 - i * 0.2 for i in range(285)]
        day2 = make_day(date(2026, 9, 17), flat + rally + after)
        return day1 + day2

    def test_runs_and_produces_a_trade(self):
        settings = Settings(capital_source="manual", manual_capital=500000.0, trail_enabled=False)
        result = run_backtest(self.build(), settings, CostModel())
        summary = result.summary()
        self.assertEqual(summary["days_tested"], 1)       # first day only provides the candle
        self.assertGreaterEqual(summary["trades"], 1)
        trade = result.trades[0]
        self.assertEqual(trade["option_type"], "CE")      # green day -> CE
        self.assertAlmostEqual(trade["net"], round(trade["gross"] - trade["costs"], 2), places=2)
        self.assertGreater(trade["costs"], 0)

    def test_respects_max_trades_setting(self):
        settings = Settings(capital_source="manual", manual_capital=500000.0,
                            trail_enabled=True, trail_mode="candle_low", max_trades_per_day=2)
        result = run_backtest(self.build(), settings, CostModel())
        for day in result.days:
            self.assertLessEqual(len(day.trades), 2)


class TestVixInBacktest(unittest.TestCase):
    def test_vix_per_day_is_used(self):
        from trader.backtest.broker import BacktestBroker
        from trader.models import Candle
        b = BacktestBroker(iv=0.14)
        b.set_day(Candle("d", 1, 1, 1, 1), [], vix=22.0)
        self.assertAlmostEqual(b.day_iv, 0.22)
        self.assertEqual(b.get_vix(), 22.0)
        b.set_day(Candle("d", 1, 1, 1, 1), [], vix=None)
        self.assertAlmostEqual(b.day_iv, 0.14)


class TestExpiryAndDecay(unittest.TestCase):
    def test_expiry_moved_from_thursday_to_tuesday(self):
        self.assertEqual(next_weekly_expiry(date(2025, 8, 25)), date(2025, 8, 28))   # Thursday before
        self.assertEqual(next_weekly_expiry(date(2025, 8, 29)), date(2025, 9, 2))    # first Tuesday
        self.assertEqual(next_weekly_expiry(date(2026, 9, 17)), date(2026, 9, 22))

    def test_trading_hours_decay_faster_during_the_day(self):
        expiry = date(2026, 9, 22)
        morning = datetime(2026, 9, 21, 9, 22, tzinfo=IST)
        afternoon = datetime(2026, 9, 21, 15, 15, tzinfo=IST)
        cal = [option_price(23300, 23300, "CE", t, expiry, 0.14, "calendar") for t in (morning, afternoon)]
        trd = [option_price(23300, 23300, "CE", t, expiry, 0.14, "trading") for t in (morning, afternoon)]
        self.assertGreater((trd[0] - trd[1]) / trd[0], 2 * (cal[0] - cal[1]) / cal[0])


class TestJournalUpgrade(unittest.TestCase):
    def test_old_trades_file_gets_direction_column(self):
        import csv
        import tempfile
        from trader.journal import TRADE_COLUMNS, Journal
        tmp = Path(tempfile.mkdtemp())
        old_cols = [c for c in TRADE_COLUMNS if c != "direction"]
        with (tmp / "trades.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=old_cols)
            w.writeheader()
            w.writerow({c: "x" for c in old_cols})
        j = Journal(tmp, mode="paper")
        j.record_trade({"date": "2026-09-16", "symbol": "S", "pnl": "10", "direction": "short"})
        rows = j.trades()
        self.assertEqual(rows[0]["direction"], "long")
        self.assertEqual(rows[1]["direction"], "short")
        self.assertEqual(rows[1]["symbol"], "S")


class TestSignalStats(unittest.TestCase):
    """Index-only edge analysis."""

    def day_bars(self, prices):
        return make_day(date(2026, 9, 17), prices)

    def test_break_needs_inside_first_then_cross(self):
        from trader.backtest.signal_stats import find_breaks
        # level 23150 (green day). Price starts below, crosses at bar 40.
        prices = [23100] * 40 + [23200] * 335
        events = find_breaks(self.day_bars(prices), prev_high=23150, prev_low=23000,
                             prev_color="GREEN", buffer_points=0, entry_start="09:31", entry_end="15:02")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].direction, "UP")

    def test_buffer_blocks_small_pokes(self):
        from trader.backtest.signal_stats import find_breaks
        prices = [23100] * 40 + [23155] * 335          # only 5 points above the level
        events = find_breaks(self.day_bars(prices), 23150, 23000, "GREEN", 20,
                             "09:31", "15:02")
        self.assertEqual(events, [])

    def test_doji_day_has_no_breaks(self):
        from trader.backtest.signal_stats import find_breaks
        events = find_breaks(self.day_bars([23200] * 375), 23150, 23000, "DOJI", 0, "09:31", "15:02")
        self.assertEqual(events, [])

    def test_simulate_hits_target_and_stop(self):
        from trader.backtest.signal_stats import BreakEvent, simulate
        bars = self.day_bars([23100] * 40 + [23160 + i for i in range(335)])
        event = BreakEvent("2026-09-17", bars[40].time.strftime("%H:%M"), 23150.0, "UP")
        points, how = simulate(bars, event, "follow", stop=30, target=50, exit_time="15:02")
        self.assertEqual((points, how), (50, "target"))
        # fading that same rise must hit its stop
        points, how = simulate(bars, event, "fade", stop=30, target=50, exit_time="15:02")
        self.assertEqual((points, how), (-30, "stop"))


if __name__ == "__main__":
    unittest.main()
