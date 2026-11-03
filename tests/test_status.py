"""
Tests for status.py - the "is it working?" command.

This one matters more than it looks. status.py is what the owner trusts instead of
reading the log himself, so a status screen that says WORKING when the bot did not
run is worse than having no status screen at all. Every scenario below is one that
has actually happened or nearly happened: a Mac asleep at 08:45, an 18-minute login,
an engine that woke but never started.

No network, no Kotak, no trader package - status.py is pure standard library on
purpose, so it still works when everything else is broken.
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import status

SETTINGS = '{"strategy": "narrow_orb", "manual_capital": 100000.0, "daily_loss_pct": 3.5}'
MONDAY = datetime(2026, 10, 26, 9, 12)          # a weekday, before the engine is due
SATURDAY = datetime(2026, 10, 24, 11, 0)


def ran_at(day: str, hhmmss: str = "08:50:50", login: int = 3) -> str:
    """The lines a healthy morning leaves behind."""
    return (f"{day} {hhmmss},088 autopilot TIMING process alive\n"
            f"{day} {hhmmss},093 autopilot TIMING startup took 0s\n"
            f"{day} {hhmmss},168 autopilot TIMING login took {login}s\n"
            f"{day} {hhmmss},933 autopilot TIMING health check took 3s\n")


class StatusTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "data" / "history").mkdir(parents=True)
        status.PROJECT = self.dir
        status.DATA = self.dir / "data"
        status.LOG = self.dir / "data" / "autopilot.log"
        self._real_datetime = status.datetime
        self._real_agent = status.launch_agent_loaded
        status.launch_agent_loaded = lambda: True
        self.settings(SETTINGS)
        self.env("KOTAK_MPIN=xxx\nLIVE_TRADING=false\n")

    def tearDown(self):
        status.datetime = self._real_datetime
        status.launch_agent_loaded = self._real_agent

    # -- fixtures ---------------------------------------------------------- #
    def clock(self, when: datetime):
        real = self._real_datetime

        class Frozen(real):
            @classmethod
            def now(cls, tz=None):
                return when
        status.datetime = Frozen

    def log(self, text):
        status.LOG.write_text(text, encoding="utf-8")

    def settings(self, text):
        (self.dir / "settings.json").write_text(text, encoding="utf-8")

    def env(self, text):
        (self.dir / ".env").write_text(text, encoding="utf-8")

    def daily_file(self, newest: date, rows: int = 1236):
        lines = ["date,open,high,low,close"]
        for i in range(rows):
            lines.append(f"{(newest - timedelta(days=i)).isoformat()},100,200,50,150")
        (self.dir / "data" / "history" / "NIFTY_D.csv").write_text("\n".join(lines), encoding="utf-8")

    def out(self, *args, when: datetime = MONDAY):
        self.clock(when)
        buffer, old = io.StringIO(), sys.argv
        sys.argv = ["status.py", *args]
        try:
            with redirect_stdout(buffer):
                status.main()
        finally:
            sys.argv = old
        return buffer.getvalue()


class TestTheVerdict(StatusTestCase):
    def test_a_healthy_morning_before_the_open(self):
        self.log(ran_at("2026-10-26"))
        text = self.out()
        self.assertIn("WORKING", text)
        self.assertNotIn("NEEDS ATTENTION", text)
        self.assertIn("08:50:50", text)
        self.assertIn("login          3s", text)

    def test_engine_running_is_said_plainly(self):
        self.log(ran_at("2026-10-26")
                 + "2026-10-26 09:16:02,001 alerts INFO Engine started for 26 Oct.\n")
        text = self.out(when=datetime(2026, 10, 26, 11, 0))
        self.assertIn("engine on since 09:16:02", text)

    def test_it_did_not_run_today(self):
        """The silent failure: Mac asleep at 08:45, nothing in the log, no complaint
        from anywhere. This is the single most important thing status.py catches."""
        self.log(ran_at("2026-10-23"))
        text = self.out(when=datetime(2026, 10, 26, 10, 30))
        self.assertIn("NEEDS ATTENTION", text)
        self.assertIn("has not run today", text)

    def test_before_the_schedule_fires_is_not_a_problem(self):
        self.log(ran_at("2026-10-23"))
        text = self.out(when=datetime(2026, 10, 26, 8, 20))
        self.assertNotIn("NEEDS ATTENTION", text)
        self.assertIn("WAITING", text)

    def test_woke_but_the_engine_never_started(self):
        self.log(ran_at("2026-10-26"))
        text = self.out(when=datetime(2026, 10, 26, 9, 45))
        self.assertIn("NEEDS ATTENTION", text)
        self.assertIn("engine never started", text)

    def test_login_failure_is_reported(self):
        self.log("2026-10-26 08:50:50,088 autopilot TIMING process alive\n"
                 "2026-10-26 08:51:20,000 alerts INFO Login failed after 30s: bad MPIN. "
                 "No trading today unless this clears.\n")
        text = self.out(when=datetime(2026, 10, 26, 10, 0))
        self.assertIn("NEEDS ATTENTION", text)
        self.assertIn("login failed", text)

    def test_a_slow_login_is_reported(self):
        """16 Oct 2026: login took 1,073 seconds and the engine missed the open."""
        self.log(ran_at("2026-10-26", login=1073))
        text = self.out()
        self.assertIn("NEEDS ATTENTION", text)
        self.assertIn("1073s", text)

    def test_a_four_second_login_is_not_reported(self):
        self.log(ran_at("2026-10-26", login=4))
        self.assertNotIn("NEEDS ATTENTION", self.out())

    def test_the_schedule_being_gone_is_caught(self):
        status.launch_agent_loaded = lambda: False
        self.log(ran_at("2026-10-26"))
        text = self.out()
        self.assertIn("NEEDS ATTENTION", text)
        self.assertIn("install_autopilot.command", text)

    def test_a_machine_without_launchctl_does_not_cry_wolf(self):
        status.launch_agent_loaded = lambda: None
        self.log(ran_at("2026-10-26"))
        self.assertNotIn("schedule is not installed", self.out())

    def test_weekend_expects_nothing(self):
        self.log(ran_at("2026-10-23"))
        text = self.out(when=SATURDAY)
        self.assertIn("RESTING", text)
        self.assertNotIn("NEEDS ATTENTION", text)


class TestWhatItReports(StatusTestCase):
    def test_paper_mode_is_stated(self):
        self.log(ran_at("2026-10-26"))
        self.assertIn("live trading locked", self.out())

    def test_live_trading_is_shouted_about(self):
        """If real money is ever armed, that must be the loudest line on the screen."""
        self.env("LIVE_TRADING=true\n")
        self.log(ran_at("2026-10-26"))
        self.assertIn("REAL MONEY IS ARMED", self.out())

    def test_live_trading_false_is_not_mistaken_for_true(self):
        self.env("LIVE_TRADING=false\n")
        self.log(ran_at("2026-10-26"))
        text = self.out()
        self.assertIn("live trading locked", text)
        self.assertNotIn("REAL MONEY", text)

    def test_telegram_off_is_called_out(self):
        self.log(ran_at("2026-10-26"))
        self.assertIn("Telegram is off", self.out())

    def test_telegram_on_is_quiet(self):
        self.env("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=123\nLIVE_TRADING=false\n")
        self.log(ran_at("2026-10-26"))
        text = self.out()
        self.assertNotIn("Telegram is off", text)
        self.assertIn("telegram       on", text)

    def test_missed_days_are_counted_and_named(self):
        """The log starts on 19 Oct and has nothing for 21, 22 or 23, so those three
        really were missed. 20 Oct is Dussehra and is not counted either way."""
        self.log(ran_at("2026-10-19") + ran_at("2026-10-26"))
        text = self.out("--days", "6", when=datetime(2026, 10, 26, 10, 0))
        self.assertIn("RAN ON 2 OF THE LAST 5 TRADING DAYS", text)
        self.assertIn("did NOT run", text)
        for day in ("21 Oct", "22 Oct", "23 Oct"):
            self.assertIn(day, text)
        self.assertNotIn("20 Oct", text)
        self.assertIn("missed 3 of the last 5", text)

    def test_days_before_the_log_begins_are_not_blamed(self):
        """The log starts on 23 Oct, so 19-22 Oct are unknown, not missed."""
        self.log(ran_at("2026-10-23") + ran_at("2026-10-26"))
        text = self.out("--days", "14", when=datetime(2026, 10, 26, 10, 0))
        self.assertNotIn("16 Oct", text)
        self.assertIn("log covers     2026-10-23", text)

    def test_the_plan_is_shown(self):
        self.log(ran_at("2026-10-26") + "2026-10-26 09:16:25,663 alerts TRADE Today's plan: "
                 "Yesterday's range was 419 points, above the narrow-day line of 184. "
                 "No trade today.\n")
        text = self.out(when=datetime(2026, 10, 26, 11, 0))
        self.assertIn("Yesterday's range was 419 points", text)
        self.assertNotIn("Today's plan: Today", text)

    def test_a_current_daily_file_is_current(self):
        self.log(ran_at("2026-10-26"))
        self.daily_file(date(2026, 10, 23))
        text = self.out()
        self.assertIn("1,236 days", text)
        self.assertIn("current", text)

    def test_a_stale_daily_file_is_flagged(self):
        self.log(ran_at("2026-10-26"))
        self.daily_file(date(2026, 9, 1))
        self.assertIn("STALE", self.out())

    def test_a_missing_daily_file_says_how_to_fix_it(self):
        self.log(ran_at("2026-10-26"))
        self.assertIn("--download-daily", self.out())

    def test_trades_are_counted(self):
        (self.dir / "data" / "trades.csv").write_text(
            "date,mode,entry_price,exit_price,qty,pnl\n"
            "2026-10-22,paper,100,130,65,1950\n"
            "2026-10-23,paper,120,110,65,-650\n"
            "2026-10-23,demo,120,900,65,50000\n", encoding="utf-8")
        self.log(ran_at("2026-10-26"))
        text = self.out()
        self.assertIn("narrow_orb     2 trades, 1 green, gross Rs +1,300", text)
        self.assertIn("10 more before", text)          # the demo row is ignored


class TestItNeverCrashes(StatusTestCase):
    """status.py is the thing you run WHEN things are broken, so it has to survive
    every kind of missing or mangled input."""

    def test_no_log_at_all(self):
        self.assertIn("ALGO TRADER STATUS", self.out(when=datetime(2026, 10, 26, 8, 20)))

    def test_empty_log(self):
        self.log("")
        self.assertIn("ALGO TRADER STATUS", self.out())

    def test_log_of_pure_noise(self):
        self.log('{"request_id": "x", "status_code": 429}\n09:16:24  undated print line\n')
        self.assertIn("ALGO TRADER STATUS", self.out())

    def test_broken_settings_file(self):
        self.settings("{not json at all")
        self.log(ran_at("2026-10-26"))
        self.assertIn("strategy       ?", self.out())

    def test_no_env_file(self):
        (self.dir / ".env").unlink()
        self.log(ran_at("2026-10-26"))
        self.assertIn("ALGO TRADER STATUS", self.out())

    def test_mangled_trades_file(self):
        (self.dir / "data" / "trades.csv").write_text("date,pnl\nnot-a-date,not-a-number\n",
                                                      encoding="utf-8")
        self.log(ran_at("2026-10-26"))
        self.assertIn("no trades yet", self.out())

    def test_capital_from_the_broker_instead_of_typed_in(self):
        self.settings('{"strategy": "narrow_orb", "capital_source": "broker"}')
        self.log(ran_at("2026-10-26"))
        self.assertIn("from the broker", self.out())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestItDoesNotMisattribute(StatusTestCase):
    """26 Oct 2026: status.py reported "3 trades, 1 green" under narrow_orb. All three
    were from 25 Sep, taken by the prev_day breakout we abandoned for losing money.
    Mixing a dead strategy's losses into the live one's record is the worst thing a
    status screen can do, because it looks like evidence."""

    def trades_csv(self, *rows):
        body = "date,mode,entry_price,exit_price,qty,pnl\n" + "".join(
            f"{d},paper,100,110,65,{p}\n" for d, p in rows)
        (self.dir / "data" / "trades.csv").write_text(body, encoding="utf-8")

    def test_trades_from_before_the_log_are_kept_separate(self):
        self.log(ran_at("2026-10-26"))
        self.trades_csv(("2026-09-22", 1404), ("2026-09-24", -982), ("2026-09-25", -975))
        text = self.out()
        self.assertIn("narrow_orb     no trades yet", text)
        self.assertIn("earlier        3 trades up to 2026-09-25", text)
        self.assertIn("Rs -553", text)
        self.assertIn("not evidence about narrow_orb", text)

    def test_trades_since_the_log_began_count_normally(self):
        self.log(ran_at("2026-10-19") + ran_at("2026-10-26"))
        self.trades_csv(("2026-09-25", -975), ("2026-10-20", 900), ("2026-10-21", -300))
        text = self.out()
        self.assertIn("narrow_orb     2 trades, 1 green, gross Rs +600", text)
        self.assertIn("earlier        1 trades up to 2026-09-25", text)
        self.assertIn("10 more before", text)

    def test_the_install_day_is_not_counted_as_missed(self):
        """15 Oct 2026 was the evening the schedule was installed, so there was no
        08:45 run and nothing went wrong. Blaming it was a false alarm."""
        self.log("2026-10-15 21:36:03,581 alerts INFO Nothing left to do today. Exiting.\n"
                 + ran_at("2026-10-16") + ran_at("2026-10-26"))
        text = self.out("--days", "10", when=datetime(2026, 10, 26, 10, 0))
        self.assertNotIn("15 Oct", text)
        self.assertIn("did NOT run", text)              # 19-23 Oct genuinely missing

    def test_one_missed_day_is_information_not_an_alarm(self):
        """Six judged days, one missed, engine up: worth listing, not worth shouting.
        A status screen that cries wolf over one sleepy morning gets ignored."""
        self.log(ran_at("2026-10-19") + ran_at("2026-10-22") + ran_at("2026-10-23")
                 + ran_at("2026-10-26")
                 + "2026-10-26 09:15:05,000 alerts INFO Engine started for 26 Oct.\n")
        text = self.out("--days", "7", when=datetime(2026, 10, 26, 10, 0))
        self.assertIn("RAN ON 4 OF THE LAST 5 TRADING DAYS", text)
        self.assertIn("did NOT run    21 Oct", text)
        self.assertNotIn("NEEDS ATTENTION", text)


class TestTheSettingThatDrifted(StatusTestCase):
    """26 Oct 2026: orb_narrow_pct had gone back to 30 after being set to 50 on the
    15th. Nobody noticed for eleven days because it was on no screen anywhere - it
    had to be reverse-engineered from the percentile of the daily ranges. One of the
    six no-trade days was a no-trade only because of it."""

    def test_the_narrow_day_percent_is_on_the_screen(self):
        self.settings('{"strategy": "narrow_orb", "orb_narrow_pct": 50.0, '
                      '"orb_lookback_days": 20, "manual_capital": 100000.0}')
        self.log(ran_at("2026-10-26"))
        self.assertIn("narrow day     quietest 50.0% of the last 20 days", self.out())

    def test_a_drifted_value_is_visible(self):
        self.settings('{"strategy": "narrow_orb", "orb_narrow_pct": 30.0, '
                      '"orb_lookback_days": 20}')
        self.log(ran_at("2026-10-26"))
        self.assertIn("quietest 30.0%", self.out())

    def test_an_unsaved_setting_is_called_out(self):
        """Absent from settings.json means it is whatever the code says today, and it
        will silently go back there after any change."""
        self.settings('{"strategy": "narrow_orb", "manual_capital": 100000.0}')
        self.log(ran_at("2026-10-26"))
        text = self.out()
        self.assertIn("not in settings.json", text)
        self.assertIn("check tomorrow that it stayed set", text)

    def test_other_strategies_do_not_get_the_line(self):
        self.settings('{"strategy": "prev_day", "manual_capital": 100000.0}')
        self.log(ran_at("2026-10-26"))
        self.assertNotIn("narrow day", self.out())


class TestHolidaysAreNotFailures(StatusTestCase):
    """20 Oct 2026 was Dussehra. The status screen counted it as a day the bot ran,
    and would have counted it as a day the bot failed had the Mac been off - either
    way, a market holiday has no business in the reliability count."""

    def test_a_holiday_is_not_counted_at_all(self):
        self.log(ran_at("2026-10-19") + ran_at("2026-10-21") + ran_at("2026-10-22")
                 + ran_at("2026-10-23") + ran_at("2026-10-26")
                 + "2026-10-26 09:15:05,000 alerts INFO Engine started for 26 Oct.\n")
        text = self.out("--days", "6", when=datetime(2026, 10, 26, 10, 0))
        self.assertIn("RAN ON 5 OF THE LAST 5 TRADING DAYS", text)
        self.assertNotIn("20 Oct", text)
        self.assertNotIn("NEEDS ATTENTION", text)

    def test_a_holiday_today_says_why(self):
        self.log(ran_at("2026-11-09"))
        text = self.out(when=datetime(2026, 11, 10, 11, 0))      # Diwali Balipratipada
        self.assertIn("RESTING", text)
        self.assertIn("Diwali Balipratipada", text)
        self.assertNotIn("NEEDS ATTENTION", text)

    def test_a_holiday_with_no_run_is_not_a_problem(self):
        """Nothing in the log for a holiday is correct, not a missed day."""
        self.log(ran_at("2026-10-19"))
        text = self.out(when=datetime(2026, 10, 20, 14, 0))      # Dussehra
        self.assertNotIn("has not run today", text)
        self.assertNotIn("NEEDS ATTENTION", text)

    def test_a_real_missed_weekday_is_still_reported(self):
        """The calendar must not become an excuse. 19 Oct was open and was missed."""
        self.log(ran_at("2026-10-16") + ran_at("2026-10-21") + ran_at("2026-10-22")
                 + ran_at("2026-10-23"))
        text = self.out("--days", "6", when=datetime(2026, 10, 26, 10, 0))
        self.assertIn("did NOT run", text)
        self.assertIn("19 Oct", text)

    def test_the_weekend_still_rests(self):
        self.log(ran_at("2026-10-23"))
        text = self.out(when=SATURDAY)
        self.assertIn("RESTING", text)
        self.assertIn("weekend", text)
