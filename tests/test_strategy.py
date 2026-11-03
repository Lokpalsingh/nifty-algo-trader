"""
Tests for the strategy rules, using a scripted fake market and a fake clock.
No internet or Kotak account needed.

Run from the project folder:
    python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.alerts import AlertHub
from trader.brokers.base import Broker
from trader.brokers.paper import PaperBroker
from trader.clock import IST, FakeClock
from trader.config import Settings
from trader.journal import Journal
from trader.models import Candle, OptionContract
from trader.risk import RiskManager
from trader.strategy import PrevDayBreakout

# A Wednesday, so it's a normal weekday
DAY = date(2026, 9, 16)


def at(hhmm: str, sec: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, sec, tzinfo=IST)


class ScriptedMarket(Broker):
    """Prices are set by the test. Option premium follows NIFTY with delta 0.5,
    plus an adjustable 'premium_shift' to force stop-losses."""
    name = "scripted"
    is_simulated = True

    def __init__(self, candle: Candle, spot: float):
        self.candle = candle
        self.spot = spot
        self.premium_shift = 0.0
        self.lot_size = 65

    def login(self, totp_code=None): pass
    def is_logged_in(self): return True
    def get_index_ltp(self, index): return self.spot

    def get_option_ltp(self, c):
        d = 1 if c.option_type == "CE" else -1
        return round(max(0.05, 120 + 0.5 * d * (self.spot - c.strike) + self.premium_shift), 2)

    def get_previous_day_candle(self, index, today): return self.candle

    def find_atm_option(self, index, spot, option_type, today, expiry_choice):
        k = round(spot / 50) * 50
        return OptionContract(f"NIFTY{k}{option_type}", f"{k}{option_type}", "nse_fo", k, option_type,
                              "2026-09-22", self.lot_size)

    def get_available_capital(self): return 1_000_000.0
    def place_order(self, *a, **k): raise NotImplementedError
    def cancel_order(self, order_id): raise NotImplementedError
    def get_order_status(self, order_id): raise NotImplementedError
    def get_open_quantity(self, contract): return 0


RED = Candle("2026-09-15", open=24550, high=24600, low=24400, close=24450)
GREEN = Candle("2026-09-15", open=24450, high=24600, low=24400, close=24550)
DOJI = Candle("2026-09-15", open=24500, high=24600, low=24400, close=24500)


class StrategyTestCase(unittest.TestCase):
    def make(self, candle=RED, spot=24450.0, persist=False, data_dir=None, **settings_overrides):
        settings = Settings(capital_source="manual", manual_capital=1_000_000.0, sl_grace_seconds=3,
                            fill_timeout_seconds=2)
        for k, v in settings_overrides.items():
            setattr(settings, k, v)
        settings.validate()
        self.clock = FakeClock(at("09:15"))
        self.market = ScriptedMarket(candle, spot)
        self.broker = PaperBroker(self.market)
        self.tmp = data_dir or tempfile.mkdtemp()
        self.journal = Journal(Path(self.tmp), mode="paper")
        self.alerts = AlertHub(clock=self.clock)
        self.risk = RiskManager(settings)
        self.strategy = PrevDayBreakout(self.broker, self.risk, self.journal, self.alerts, settings,
                                        self.clock, mode="paper", persist=persist)
        return self.strategy

    def tick_at(self, hhmm, spot=None, sec=0):
        self.clock.set(at(hhmm, sec))
        if spot is not None:
            self.market.spot = spot
        self.strategy.tick()

    def force_stop_loss(self, drop=25.0):
        """Make the option premium fall so the stop-loss triggers."""
        self.market.premium_shift = -drop


class TestPlan(StrategyTestCase):
    def test_red_day_plans_pe_below_low(self):
        s = self.make(RED)
        self.tick_at("09:16")
        self.assertEqual(s.plan["option_type"], "PE")
        self.assertEqual(s.plan["level"], 24400)
        self.assertEqual(s.plan["direction"], "DOWN")

    def test_green_day_plans_ce_above_high(self):
        s = self.make(GREEN)
        self.tick_at("09:16")
        self.assertEqual(s.plan["option_type"], "CE")
        self.assertEqual(s.plan["level"], 24600)

    def test_doji_day_no_trade(self):
        s = self.make(DOJI, spot=24300)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24500)
        self.tick_at("09:32", spot=24300)
        self.assertIsNone(s.position)
        self.assertIn("doji", s.done_reason)

    def test_loss_limit_is_1_percent_of_capital(self):
        self.make(RED, manual_capital=300000.0)
        self.tick_at("09:16")
        self.assertAlmostEqual(self.risk.loss_limit, 3000.0)

    def test_profit_target_is_6_percent_of_capital(self):
        self.make(RED, manual_capital=300000.0)
        self.tick_at("09:16")
        self.assertAlmostEqual(self.risk.trade_profit_target, 18000.0)
        plan_alert = next(a for a in self.alerts.recent() if "Today's plan" in a["message"])
        self.assertIn("Profit target ₹18,000 per trade", plan_alert["message"])

    def test_one_stop_loss_bigger_than_limit_means_no_trade(self):
        # capital 1,00,000 -> 1% limit ₹1,000. One 20-pt stop-loss on 65 qty = ₹1,300.
        s = self.make(RED, manual_capital=100000.0)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)
        self.assertIsNone(s.position)
        self.assertIn("above the limit of ₹1,000", s.done_reason)


class TestEntries(StrategyTestCase):
    def test_red_day_buys_atm_pe_when_low_breaks(self):
        s = self.make(RED, spot=24450)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)      # inside the level -> armed
        self.assertIsNone(s.position)
        self.tick_at("09:40", spot=24395)      # breaks below 24400
        self.assertIsNotNone(s.position)
        self.assertEqual(s.position.contract.option_type, "PE")
        self.assertEqual(s.position.contract.strike, 24400)   # ATM
        self.assertEqual(s.position.qty, 65)                  # 1 lot
        self.assertEqual(s.trades_today, 1)
        self.assertAlmostEqual(s.position.sl_trigger, s.position.entry_price - 20, places=2)
        self.assertIsNotNone(s.position.sl_order_id)          # stop-loss order placed

    def test_green_day_ignores_low_break_and_buys_ce_on_high_break(self):
        s = self.make(GREEN, spot=24500)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24500)
        self.tick_at("09:35", spot=24350)      # below low: must be ignored on a green day
        self.assertIsNone(s.position)
        self.tick_at("09:50", spot=24610)      # above high
        self.assertEqual(s.position.contract.option_type, "CE")

    def test_no_entry_before_931(self):
        s = self.make(RED, spot=24450)
        self.tick_at("09:16")
        self.tick_at("09:20", spot=24420)
        self.tick_at("09:25", spot=24380)      # break before window
        self.assertIsNone(s.position)

    def test_already_broken_at_931_waits_for_fresh_break(self):
        s = self.make(RED, spot=24350)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24350)      # already below low (gap down)
        self.tick_at("09:32", spot=24340)
        self.assertIsNone(s.position)
        self.tick_at("09:40", spot=24410)      # comes back above
        self.tick_at("09:45", spot=24390)      # fresh break
        self.assertIsNotNone(s.position)

    def test_already_broken_setting_enters_immediately(self):
        s = self.make(RED, spot=24350, enter_if_already_broken=True)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24350)
        self.assertIsNotNone(s.position)

    def test_no_entry_after_1502(self):
        s = self.make(RED, spot=24450)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("15:02", spot=24380)
        self.assertIsNone(s.position)


class TestExits(StrategyTestCase):
    def enter_pe(self, hhmm="09:40"):
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at(hhmm, spot=24395)
        self.assertIsNotNone(self.strategy.position)

    def test_stop_loss_at_broker_closes_trade(self):
        s = self.make(RED)
        self.enter_pe()
        entry = s.position.entry_price
        self.force_stop_loss(25)
        self.tick_at("09:41", spot=24395)
        self.assertIsNone(s.position)
        rows = self.journal.trades()
        self.assertEqual(rows[-1]["exit_reason"], "Stop-loss hit")
        self.assertLess(float(rows[-1]["exit_price"]), entry - 19.9)
        self.assertEqual(s.done_reason, "")    # can still re-enter

    def test_gap_through_stop_loss_software_exits_after_grace(self):
        s = self.make(RED)
        self.enter_pe()
        self.force_stop_loss(40)               # below the SL limit price, broker SL can't fill
        self.tick_at("09:41", spot=24395, sec=0)
        self.assertIsNotNone(s.position)       # waiting grace period
        self.tick_at("09:41", spot=24395, sec=5)
        self.assertIsNone(s.position)          # software exited
        self.assertEqual(self.broker.get_open_quantity(OptionContract("", "24400PE", "", 0, "PE", "", 65)), 0)

    def test_time_exit_at_1514(self):
        s = self.make(RED)
        self.enter_pe()
        self.tick_at("15:13", spot=24390)
        self.assertIsNotNone(s.position)
        self.tick_at("15:14", spot=24390)
        self.assertIsNone(s.position)
        self.assertEqual(self.journal.trades()[-1]["exit_reason"], "Time exit (15:14)")
        self.assertTrue(s.done_reason)

    def test_reentry_after_stop_loss_and_max_5_trades(self):
        s = self.make(RED)
        self.tick_at("09:16")
        minute = 31
        for n in range(1, 7):
            self.market.premium_shift = 0
            self.tick_at(f"10:{minute:02d}", spot=24420)   # back inside -> re-arm
            self.tick_at(f"10:{minute + 1:02d}", spot=24395)  # break
            if n <= 5:
                self.assertIsNotNone(s.position, f"trade {n} should open")
                self.force_stop_loss(25)
                self.tick_at(f"10:{minute + 2:02d}", spot=24395)
                self.assertIsNone(s.position)
            else:
                self.assertIsNone(s.position, "6th trade must not open")
            minute += 4
        self.assertEqual(s.trades_today, 5)
        self.assertIn("5 trades", s.done_reason)

    def test_no_reentry_without_coming_back_inside(self):
        s = self.make(RED)
        self.enter_pe()
        self.force_stop_loss(25)
        self.tick_at("09:41", spot=24395)
        self.market.premium_shift = 0
        self.tick_at("09:42", spot=24390)      # still below, never came back above
        self.assertIsNone(s.position)

    def test_daily_loss_limit_blocks_next_trade(self):
        # capital 2,00,000 -> 1% limit ₹2,000. One SL at 25 pts x 65 = ₹1,625.
        # Next trade's possible loss (20 x 65 = ₹1,300) would cross ₹2,000 -> blocked.
        s = self.make(RED, manual_capital=200000.0)
        self.enter_pe()
        self.force_stop_loss(25)
        self.tick_at("09:41", spot=24395)
        self.market.premium_shift = 0
        self.tick_at("09:45", spot=24420)
        self.tick_at("09:46", spot=24395)
        self.assertIsNone(s.position)
        self.assertIn("limit", s.done_reason)

    def test_kill_switch_exits_and_stops(self):
        s = self.make(RED)
        self.enter_pe()
        s.kill_switch()
        self.assertIsNone(s.position)
        self.tick_at("09:50", spot=24420)
        self.tick_at("09:51", spot=24390)
        self.assertIsNone(s.position)
        self.assertEqual(s.trades_today, 1)


class TestTrailingStop(StrategyTestCase):
    """PE premium = 120 + 0.5 x (strike - NIFTY), so NIFTY falling = PE rising."""

    def enter(self, **settings):
        s = self.make(RED, **settings)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)      # buys 24400 PE at 122.50
        self.assertAlmostEqual(s.position.entry_price, 122.5)
        self.assertAlmostEqual(s.position.sl_trigger, 102.5)
        return s

    def test_stop_loss_follows_price_up(self):
        s = self.enter(trail_enabled=True, trail_mode="points", trail_distance=20.0)
        self.tick_at("09:45", spot=24315)      # PE now 162.50
        self.assertAlmostEqual(s.position.high_price, 162.5)
        self.assertAlmostEqual(s.position.sl_trigger, 142.5)   # 20 below the high

    def test_stop_loss_never_moves_down(self):
        s = self.enter(trail_enabled=True, trail_mode="points", trail_distance=20.0)
        self.tick_at("09:45", spot=24315)      # PE 162.50 -> stop 142.50
        self.tick_at("09:46", spot=24375)      # PE back to 132.50
        self.assertAlmostEqual(s.position.sl_trigger, 142.5)   # unchanged

    def test_trailing_stop_books_profit(self):
        s = self.enter(trail_enabled=True, trail_mode="points", trail_distance=20.0)
        self.tick_at("09:45", spot=24315)      # PE 162.50, stop 142.50
        self.tick_at("09:50", spot=24355)      # PE falls to 142.50 -> stop hits
        self.assertIsNone(s.position)
        row = self.journal.trades()[-1]
        self.assertEqual(row["exit_reason"], "Stop-loss hit")
        self.assertAlmostEqual(float(row["pnl"]), (142.5 - 122.5) * 65)   # +1300 profit
        self.assertGreater(self.risk.state.realized_pnl, 0)

    def test_trailing_waits_for_start_profit(self):
        s = self.enter(trail_enabled=True, trail_mode="points", trail_distance=20.0, trail_start_points=30.0)
        self.tick_at("09:45", spot=24375)      # PE 132.50, only +10 -> no trailing yet
        self.assertAlmostEqual(s.position.sl_trigger, 102.5)
        self.tick_at("09:46", spot=24325)      # PE 157.50, +35 -> trailing starts
        self.assertAlmostEqual(s.position.sl_trigger, 137.5)

    def test_percent_trailing(self):
        s = self.enter(trail_enabled=True, trail_mode="percent", trail_distance=10.0)
        self.tick_at("09:45", spot=24315)      # PE 162.50 -> stop 10% below = 146.25
        self.assertAlmostEqual(s.position.sl_trigger, 146.25)

    def test_trailing_off_by_default(self):
        s = self.enter()
        self.tick_at("09:45", spot=24315)
        self.assertAlmostEqual(s.position.sl_trigger, 102.5)   # fixed stop-loss


class TestCandleTrailing(StrategyTestCase):
    """Trailing on the option's own 1-minute chart: after a red candle completes,
    the stop-loss moves up to that candle's low. PE price = 120 + 0.5 x (24400 - NIFTY)."""

    def enter(self, **overrides):
        settings = dict(trail_enabled=True, trail_mode="candle_low", trail_candle_minutes=1)
        settings.update(overrides)
        s = self.make(RED, **settings)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)          # buys 24400 PE at 122.50
        self.assertAlmostEqual(s.position.sl_trigger, 102.5)
        return s

    def minute(self, hhmm, prices):
        """Feed several prices inside one minute."""
        for i, spot in enumerate(prices):
            self.tick_at(hhmm, spot=spot, sec=i * 5)

    def test_stop_moves_to_last_red_candle_low(self):
        s = self.enter()
        self.minute("09:41", [24345, 24335, 24340])   # 147.5 -> 152.5 -> 150.0 (green candle)
        self.minute("09:42", [24340, 24330, 24345])   # opens 150.0, low 147.5... closes 147.5 = RED
        self.minute("09:43", [24330, 24325, 24320])   # red candle above is now complete
        self.assertAlmostEqual(s.position.sl_trigger, 147.5)   # low of the red candle
        self.assertIsNotNone(s.position)

    def test_stop_keeps_rising_with_each_new_red_candle(self):
        s = self.enter()
        self.minute("09:41", [24345, 24335, 24340])   # green
        self.minute("09:42", [24340, 24330, 24345])   # red, low 147.5
        self.minute("09:43", [24330, 24325, 24320])   # red candle completes
        self.assertAlmostEqual(s.position.sl_trigger, 147.5)
        self.minute("09:44", [24320, 24310, 24315])   # green
        self.minute("09:45", [24315, 24305, 24320])   # red, low 160.0 (higher low)
        self.minute("09:46", [24310, 24305, 24300])
        self.assertAlmostEqual(s.position.sl_trigger, 160.0)

    def test_no_red_candle_means_original_stop_stays(self):
        s = self.enter()
        self.minute("09:41", [24395, 24390, 24385])   # green only
        self.minute("09:42", [24385, 24380, 24375])   # green only
        self.minute("09:43", [24375, 24370, 24365])
        self.assertAlmostEqual(s.position.sl_trigger, 102.5)

    def test_price_falling_below_red_low_exits(self):
        s = self.enter()
        self.minute("09:41", [24345, 24335, 24340])   # green
        self.minute("09:42", [24340, 24330, 24345])   # red candle, low 147.5
        self.tick_at("09:43", spot=24400, sec=0)      # price collapses to 120, below that low
        self.assertIsNone(s.position)
        row = self.journal.trades()[-1]
        self.assertIn("Trailing stop-loss", row["exit_reason"])

    def test_buffer_below_the_low(self):
        s = self.enter(trail_candle_buffer=2.0)
        self.minute("09:41", [24345, 24335, 24340])   # green
        self.minute("09:42", [24340, 24330, 24345])   # red, low 147.5
        self.minute("09:43", [24330, 24325, 24320])
        self.assertAlmostEqual(s.position.sl_trigger, 145.5)   # 2 rupees below the low


class TestNewOptions(StrategyTestCase):
    """Reverse mode, strike offset, percent stop-loss and profit target."""

    def test_reverse_mode_buys_the_opposite_option(self):
        s = self.make(RED, signal_mode="reverse")
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)        # red day, low breaks
        self.assertEqual(s.position.contract.option_type, "CE")   # fades the break

    def test_strike_offset_picks_otm_and_itm(self):
        s = self.make(RED, strike_offset_steps=2)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)
        self.assertEqual(s.position.contract.strike, 24300)       # PE, 2 strikes OTM = lower

        s = self.make(RED, strike_offset_steps=-1)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)
        self.assertEqual(s.position.contract.strike, 24450)       # PE, 1 strike ITM = higher

    def test_percent_stop_loss(self):
        s = self.make(RED, stop_loss_mode="percent", stop_loss_percent=25.0)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)        # entry 122.50
        self.assertAlmostEqual(s.position.sl_trigger, round(122.5 * 0.75, 2), places=1)

    def test_profit_target_books_the_trade(self):
        s = self.make(RED, target_points=30.0)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)        # entry 122.50
        self.tick_at("09:45", spot=24345)        # PE now 147.50, +25 -> not yet
        self.assertIsNotNone(s.position)
        self.tick_at("09:46", spot=24330)        # PE 155.00, +32.5 -> target
        self.assertIsNone(s.position)
        self.assertIn("Target hit", self.journal.trades()[-1]["exit_reason"])
        self.assertGreater(self.risk.state.realized_pnl, 0)

    def test_profit_target_as_percent_of_capital(self):
        # capital 30,000 x 6% = ₹1,800 target. 65 qty -> needs +27.7 points on the option.
        s = self.make(RED, manual_capital=30000.0, daily_loss_pct=5.0, target_capital_pct=6.0)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)        # entry 122.50
        self.assertAlmostEqual(s.snapshot()["position"]["target_price"], 122.5 + 1800 / 65, places=2)
        self.tick_at("09:45", spot=24345)        # PE 147.50, +25 x 65 = ₹1,625 -> not yet
        self.assertIsNotNone(s.position)
        self.tick_at("09:46", spot=24330)        # PE 155.00, +32.5 x 65 = ₹2,112 -> target
        self.assertIsNone(s.position)
        self.assertIn("Profit target hit (6% of capital", self.journal.trades()[-1]["exit_reason"])
        self.assertIn("Profit target hit", s.done_reason)   # no more trades today
        self.tick_at("09:50", spot=24420)
        self.tick_at("09:51", spot=24395)
        self.assertIsNone(s.position)

    def test_profit_target_off_when_zero(self):
        s = self.make(RED, manual_capital=30000.0, daily_loss_pct=5.0, target_capital_pct=0.0)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)
        self.tick_at("09:46", spot=24330)
        self.assertIsNotNone(s.position)
        self.assertIsNone(s.snapshot()["position"]["target_price"])


class TestKillSwitchReset(StrategyTestCase):
    def test_clearing_kill_switch_allows_new_trades(self):
        s = self.make(RED)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        s.kill_switch()
        self.tick_at("10:00", spot=24395)
        self.assertIsNone(s.position)                 # blocked
        self.assertEqual(s.reset_today(), "")
        self.tick_at("10:05", spot=24420)             # back inside, re-arm
        self.tick_at("10:06", spot=24395)             # fresh break
        self.assertIsNotNone(s.position)

    def test_cannot_clear_with_open_trade(self):
        s = self.make(RED)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)
        self.assertIn("open trade", s.reset_today())

    def test_cannot_clear_after_loss_limit(self):
        s = self.make(RED, manual_capital=200000.0)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)
        self.force_stop_loss(40)
        self.tick_at("09:41", spot=24395, sec=5)
        self.assertIsNone(s.position)
        self.assertIn("loss limit", s.reset_today().lower())


class TestGreenCandleReentry(StrategyTestCase):
    """Re-entry on the option's own 1-minute chart, same option all day.
    PE price = 120 + 0.5 x (24400 - NIFTY) + premium_shift."""

    def minute(self, hhmm, prices, start_sec=0):
        for i, spot in enumerate(prices):
            self.tick_at(hhmm, spot=spot, sec=start_sec + i * 5)

    def stopped_out(self, **overrides):
        settings = dict(reentry_mode="green_candle", max_trades_per_day=50)
        settings.update(overrides)
        s = self.make(RED, **settings)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)          # buys 24400 PE at 122.50
        self.force_stop_loss(25)
        self.tick_at("09:41", spot=24395)          # stop-loss hit
        self.assertIsNone(s.position)
        self.assertEqual(s.done_reason, "")
        if settings["reentry_mode"] == "green_candle":
            self.assertEqual(s.reentry_contract.strike, 24400)
        else:
            self.assertIsNone(s.reentry_contract)
        self.market.premium_shift = 0
        return s

    def test_buys_same_option_when_green_candle_high_breaks(self):
        s = self.stopped_out()
        self.minute("09:42", [24395, 24385, 24390])   # 122.5 -> 127.5 -> 125.0: green, high 127.5
        self.assertIsNone(s.position)                  # candle not finished yet
        self.tick_at("09:43", spot=24392)              # 124.0: green candle done, not above 127.5
        self.assertIsNone(s.position)
        self.assertIn("127.50", s.status_text)
        self.tick_at("09:43", spot=24340, sec=10)      # 150.0 > 127.5 -> buy again
        self.assertIsNotNone(s.position)
        self.assertEqual(s.trades_today, 2)
        # same option as the first trade, although ATM is now 24350
        self.assertEqual(s.position.contract.strike, 24400)
        self.assertEqual(s.position.contract.option_type, "PE")
        # NIFTY never went back above yesterday's low: the level doesn't matter any more

    def test_candle_of_the_exit_minute_does_not_count(self):
        s = self.stopped_out()
        self.minute("09:41", [24390, 24380, 24385], start_sec=10)   # green, but same minute as the exit
        self.tick_at("09:42", spot=24370)              # 135.0, above that candle's high
        self.assertIsNone(s.position)
        self.assertIn("needs a green", s.status_text)

    def test_red_candles_alone_never_trigger(self):
        s = self.stopped_out()
        self.minute("09:42", [24390, 24380, 24400])    # 125 -> 130 -> 120: red
        self.minute("09:43", [24400, 24395, 24405])    # 120 -> 122.5 -> 117.5: red
        self.tick_at("09:44", spot=24340)              # 150, but no green candle yet
        self.assertIsNone(s.position)

    def test_keeps_trading_after_each_stop_one_at_a_time(self):
        s = self.stopped_out()
        self.minute("09:42", [24395, 24385, 24390])    # green, high 127.5
        self.tick_at("09:43", spot=24380)              # 130 -> trade 2
        self.assertEqual(s.trades_today, 2)
        self.tick_at("09:43", spot=24370, sec=10)      # still in trade 2, no second position
        self.assertEqual(s.trades_today, 2)
        self.force_stop_loss(25)
        self.tick_at("09:44", spot=24380)              # trade 2 stopped out
        self.assertIsNone(s.position)
        self.market.premium_shift = 0
        self.minute("09:45", [24380, 24370, 24375])    # green, high 135
        self.tick_at("09:46", spot=24360)              # 140 -> trade 3
        self.assertEqual(s.trades_today, 3)
        self.assertEqual(s.position.contract.strike, 24400)

    def test_trailing_stop_exit_also_watches_for_reentry(self):
        s = self.make(RED, reentry_mode="green_candle", trail_enabled=True, trail_mode="candle_low",
                      trail_candle_minutes=1)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)              # PE at 122.50, stop 102.50
        self.minute("09:41", [24345, 24335, 24340])    # green
        self.minute("09:42", [24340, 24330, 24345])    # red, low 147.5
        self.minute("09:43", [24330, 24325, 24320])    # stop moves to 147.5
        self.minute("09:44", [24350, 24360])           # 145, 140: below the red low -> exit
        self.assertIsNone(s.position)
        self.assertEqual(s.done_reason, "")
        self.assertIsNotNone(s.reentry_contract)

    def test_time_exit_still_ends_the_day(self):
        s = self.make(RED, reentry_mode="green_candle")
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)
        self.tick_at("15:14", spot=24390)
        self.assertIsNone(s.position)
        self.assertIn("Time exit", s.done_reason)
        self.assertIsNone(s.reentry_contract)

    def test_level_mode_still_needs_a_fresh_cross(self):
        s = self.stopped_out(reentry_mode="level")
        self.minute("09:42", [24395, 24385, 24390])
        self.tick_at("09:43", spot=24340)
        self.assertIsNone(s.position)                  # NIFTY never went back above 24400

    def test_restart_keeps_watching_the_same_option(self):
        tmp = tempfile.mkdtemp()
        self.stopped_out(persist=True, data_dir=tmp)
        s2 = self.make(RED, persist=True, data_dir=tmp, reentry_mode="green_candle", max_trades_per_day=50)
        self.tick_at("09:42", spot=24395)
        self.assertEqual(s2.reentry_contract.strike, 24400)
        self.minute("09:43", [24395, 24385, 24390])    # green after the restart
        self.tick_at("09:44", spot=24380)
        self.assertIsNotNone(s2.position)
        self.assertEqual(s2.trades_today, 2)


class TestMinimumProfit(StrategyTestCase):
    """Minimum profit ₹1,000 on 65 qty = 15.38 points -> lock price 122.50 + 15.40 = 137.90.
    PE price = 120 + 0.5 x (24400 - NIFTY)."""

    def minute(self, hhmm, prices, start_sec=0):
        for i, spot in enumerate(prices):
            self.tick_at(hhmm, spot=spot, sec=start_sec + i * 5)

    def enter(self, **overrides):
        settings = dict(trail_enabled=True, trail_mode="candle_low", trail_candle_minutes=1,
                        min_profit_rupees=1000.0)
        settings.update(overrides)
        s = self.make(RED, **settings)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)          # buys 24400 PE at 122.50, stop 102.50
        self.assertAlmostEqual(s.min_profit_lock_price(), 137.90)
        return s

    def test_red_candles_do_not_move_the_stop_before_1000(self):
        s = self.enter()
        self.minute("09:41", [24390, 24385, 24392])    # 125 -> 127.5 -> 124: red, low 124
        self.tick_at("09:42", spot=24390)              # red candle complete
        self.assertAlmostEqual(s.position.sl_trigger, 102.5)   # first stop-loss stays

    def test_stop_jumps_to_lock_1000_then_keeps_trailing(self):
        s = self.enter()
        self.minute("09:41", [24390, 24385, 24392])    # red, low 124 (ignored)
        self.tick_at("09:42", spot=24390)
        self.tick_at("09:42", spot=24360, sec=5)       # 140: trade is ₹1,137 up
        self.assertAlmostEqual(s.position.sl_trigger, 137.90)
        locked = (s.position.sl_trigger - s.position.entry_price) * s.position.qty
        self.assertGreaterEqual(locked, 1000)
        self.minute("09:43", [24350, 24340, 24345])    # green
        self.minute("09:44", [24330, 24325, 24336])    # 155 -> 157.5 -> 152: red, low 152
        self.tick_at("09:45", spot=24335)              # red candle complete
        self.assertAlmostEqual(s.position.sl_trigger, 152.0)   # trailing above the lock

    def test_falls_back_after_lock_exits_in_profit(self):
        s = self.enter()
        self.tick_at("09:41", spot=24360)              # 140 -> lock at 137.90
        self.tick_at("09:41", spot=24365, sec=10)      # 137.50: stop-loss order fills
        self.assertIsNone(s.position)
        pnl = float(self.journal.trades()[-1]["pnl"])
        self.assertGreater(pnl, 950)                   # ~₹1,000 (fills a tick or two below)

    def test_works_without_trailing(self):
        s = self.enter(trail_enabled=False)
        self.tick_at("09:41", spot=24360)              # 140
        self.assertAlmostEqual(s.position.sl_trigger, 137.90)

    def test_off_when_zero(self):
        s = self.make(RED, trail_enabled=False)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)
        self.tick_at("09:41", spot=24360)
        self.assertAlmostEqual(s.position.sl_trigger, 102.5)
        self.assertIsNone(s.min_profit_lock_price())

    def test_green_candle_mode_buys_again_after_a_locked_exit(self):
        s = self.enter(reentry_mode="green_candle", max_trades_per_day=50)
        self.tick_at("09:41", spot=24360)              # lock
        self.tick_at("09:41", spot=24365, sec=10)      # exit with ~₹1,000
        self.assertIsNone(s.position)
        self.assertEqual(s.done_reason, "")
        self.assertIsNotNone(s.reentry_contract)


class TestRestart(StrategyTestCase):
    def test_restart_remembers_trade_count(self):
        tmp = tempfile.mkdtemp()
        s = self.make(RED, persist=True, data_dir=tmp)
        self.tick_at("09:16")
        self.tick_at("09:31", spot=24420)
        self.tick_at("09:40", spot=24395)
        self.force_stop_loss(25)
        self.tick_at("09:41", spot=24395)
        self.assertEqual(s.trades_today, 1)
        realized = self.risk.state.realized_pnl

        # "restart the app"
        s2 = self.make(RED, persist=True, data_dir=tmp)
        self.tick_at("09:50", spot=24420)
        self.assertEqual(s2.trades_today, 1)
        self.assertAlmostEqual(self.risk.state.realized_pnl, realized)


if __name__ == "__main__":
    unittest.main()
