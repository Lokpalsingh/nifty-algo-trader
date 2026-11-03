"""Switching strategy must clear the old strategy's plan for today."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.isolation import use_temp_settings
from trader.config import Secrets, Settings
from trader.engine import Engine
from trader.journal import Journal


class TestStrategySwitch(unittest.TestCase):
    def setUp(self):
        # These tests call Engine.update_settings, which saves settings.json. Without
        # this line they save the REAL one, which is how orb_narrow_pct kept going
        # back to 30 between 15 and 26 Oct 2026.
        use_temp_settings(self)

    def build(self, strategy="narrow_orb"):
        settings = Settings(strategy=strategy, capital_source="manual", manual_capital=100000.0,
                            daily_loss_pct=3.5)
        engine = Engine(settings, Secrets())
        engine.set_mode("demo")
        engine.journal = Journal(Path(tempfile.mkdtemp()), mode="paper")
        engine.strategy.journal = engine.journal
        return engine

    def test_switch_clears_the_old_plan_but_keeps_todays_numbers(self):
        engine = self.build("narrow_orb")
        day = engine.clock.now().date().isoformat()
        engine.journal.save_state(day, {"plan": {"text": "Yesterday was not a narrow day"},
                                        "done_reason": "Yesterday was not a narrow day",
                                        "trades_today": 1, "risk": {"realized_pnl": -900.0}})

        engine.update_settings(Settings.from_dict({**engine.settings.to_dict(), "strategy": "prev_day"}))

        saved = engine.journal.load_state(day)
        self.assertIsNone(saved["plan"])
        self.assertEqual(saved["done_reason"], "")
        self.assertEqual(saved["trades_today"], 1)          # today's trades still count
        self.assertEqual(saved["risk"]["realized_pnl"], -900.0)
        self.assertEqual(type(engine.strategy).__name__, "PrevDayBreakout")

    def test_switch_is_refused_while_a_trade_is_open(self):
        engine = self.build("prev_day")
        engine.strategy.position = object()              # pretend a trade is open
        with self.assertRaises(RuntimeError):
            engine.update_settings(Settings.from_dict({**engine.settings.to_dict(),
                                                       "strategy": "narrow_orb"}))

    def test_saving_without_changing_strategy_leaves_the_plan_alone(self):
        engine = self.build("narrow_orb")
        day = engine.clock.now().date().isoformat()
        engine.journal.save_state(day, {"plan": {"text": "today's plan"}, "done_reason": "done",
                                        "trades_today": 0, "risk": {}})
        engine.update_settings(Settings.from_dict({**engine.settings.to_dict(), "lots": 1}))
        saved = engine.journal.load_state(day)
        self.assertEqual(saved["plan"]["text"], "today's plan")
        self.assertEqual(saved["done_reason"], "done")


if __name__ == "__main__":
    unittest.main()


class TestBrokerRegistry(unittest.TestCase):
    """Adding a broker should be one adapter file plus one line in BROKERS, and a
    name nobody implemented should fail at startup rather than at 09:15."""

    def setUp(self):
        use_temp_settings(self)

    def test_kotak_is_registered(self):
        from trader.engine import BROKERS
        self.assertIn("kotak", BROKERS)

    def test_the_default_is_kotak(self):
        self.assertEqual(Settings().broker, "kotak")

    def test_an_unknown_broker_fails_with_a_useful_message(self):
        engine = Engine(Settings(broker="kotak"), Secrets())
        engine.settings.broker = "not_a_broker"
        with self.assertRaises(ValueError) as caught:
            engine.set_mode("paper")
        message = str(caught.exception)
        self.assertIn("not_a_broker", message)
        self.assertIn("kotak", message, "it should list what it does know")
        self.assertIn("BROKERS.md", message, "and say where to look")

    def test_a_registered_broker_is_the_one_built(self):
        """A fake adapter, registered the way BROKERS.md describes, must be what the
        engine actually constructs - including underneath the paper wrapper."""
        from trader.brokers.base import Broker
        import trader.engine as engine_module

        built = {}

        class Fake(Broker):
            name = "Fake Broker"

            def __init__(self, secrets, product="MIS"):
                built["made"] = True

            def login(self, totp_code=None): pass
            def is_logged_in(self): return True
            def get_index_ltp(self, index): return 24000.0
            def get_option_ltp(self, c): return 100.0
            def get_previous_day_candle(self, index, today): return None
            def find_atm_option(self, *a, **k): raise NotImplementedError
            def place_order(self, *a, **k): raise NotImplementedError
            def cancel_order(self, order_id): pass
            def get_order_status(self, order_id): raise NotImplementedError
            def get_available_capital(self): return 100000.0
            def get_open_quantity(self, contract): return 0

        original = dict(engine_module.BROKERS)
        engine_module.BROKERS["fake"] = Fake
        self.addCleanup(lambda: engine_module.BROKERS.clear() or
                        engine_module.BROKERS.update(original))

        engine = Engine(Settings(broker="fake"), Secrets())
        engine.set_mode("paper")
        self.assertTrue(built.get("made"), "the registered adapter was never built")
        self.assertIn("Fake Broker", engine.broker.name)

    def test_demo_mode_needs_no_broker_at_all(self):
        """Someone evaluating this should be able to run it with no account."""
        engine = Engine(Settings(broker="not_a_broker"), Secrets())
        engine.set_mode("demo")
        self.assertTrue(engine.broker.is_simulated)
