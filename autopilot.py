"""
Autopilot: runs the trading day for you, start to finish, without the dashboard.

    python3 autopilot.py              # run it; leave the Terminal window open
    python3 autopilot.py --dry-run    # print the plan for today and exit
    python3 autopilot.py --once       # do today only, then stop

What it does, every weekday, by itself:

  08:50  logs in to Kotak with the TOTP secret from .env
         refreshes the daily candle history
         runs the morning health check (price, candle, capital, expiry, data)
  09:15  starts the engine, which trades the strategy in settings.json
  15:20  stops the engine
         downloads the day's 1-minute candles into data/history
         writes the day's report and sends it to Telegram
  Friday also sends the week's report

It never switches strategy by itself, and it never trades live unless
LIVE_TRADING=true is in your .env. Both of those are deliberate; see the notes
at the bottom of this file.
"""
import argparse
import logging
import time
from datetime import date, datetime, time as time_of_day, timedelta

from trader import alerts as A
from trader.alerts import AlertHub
from trader.clock import IST
from trader.config import Secrets, Settings
from trader.engine import Engine

log = logging.getLogger("autopilot")

WAKE_AT = time_of_day(8, 45)          # log in and health check; matches the LaunchAgent,
                                      # so a slow login has the full 30 minutes to finish
START_AT = time_of_day(9, 15)         # engine on
STOP_AT = time_of_day(15, 20)         # engine off, after the 15:14 exit
CHECK_EVERY = 30                      # seconds between checks


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_day(d: date) -> bool:
    """Weekday, and not an NSE holiday we know about.

    A year the holiday file does not cover falls back to the weekday check, so the
    worst case is a wasted login on a holiday - never a skipped trading day."""
    from trader.holidays import is_trading_day as market_open
    return market_open(d)


class Autopilot:
    def __init__(self, mode: str = "paper", once: bool = False):
        self.settings = Settings.load()
        self.secrets = Secrets.from_env()
        self.engine = Engine(self.settings, self.secrets)
        self.engine.set_mode(mode)
        self.mode = mode
        self.once = once
        self.day_state = {}            # what has already been done today
        self.hub = AlertHub(self.secrets.telegram_bot_token, self.secrets.telegram_chat_id)

    # ---------------------------------------------------------------- #
    def tell(self, text: str, level: str = A.INFO):
        print(f"{now_ist():%H:%M:%S}  {text}", flush=True)
        self.hub.add(level, text, push=True)      # nobody is at the dashboard; send it

    def done(self, key: str) -> bool:
        return self.day_state.get("day") == date.today().isoformat() and key in self.day_state

    def mark(self, key: str):
        if self.day_state.get("day") != date.today().isoformat():
            self.day_state = {"day": date.today().isoformat()}
        self.day_state[key] = True

    # ---------------------------------------------------------------- #
    def market(self):
        """The thing that actually talks to Kotak, whichever broker is wrapping it."""
        return getattr(self.engine.broker, "market", self.engine.broker)

    def morning(self):
        """Log in and check everything is readable. Nothing slow belongs in here.

        16 Oct 2026 is the day to remember. The process reached its first line at
        08:57:21, logged in at 09:15:14 and started the engine at 09:16:24 - 84 seconds
        after the open. Two separate problems, and the big one is still open:

          08:45 -> 08:57  12 minutes before our first log line. Wake-from-sleep or
                          startup; the timings below now tell us which.
          08:57 -> 09:15  18 minutes inside login. Cause unknown. This is the one
                          that cost us the open.
          09:15 -> 09:15  35 seconds on the daily download (moved to the evening).

        So the download was never the main delay. Every phase is timed into the log
        now, because guessing at this cost a day."""
        t0 = time.monotonic()
        self.tell(f"Autopilot waking up. Strategy: {self.settings.strategy}, mode: {self.mode}.")
        try:
            self.engine.login(None if self.secrets.kotak_totp_secret else "")
        except Exception as e:
            self.tell(f"Login failed after {time.monotonic() - t0:.0f}s: {e}. "
                      f"No trading today unless this clears.", A.CRITICAL)
            return False

        login_took = time.monotonic() - t0
        log.info("TIMING login took %.0fs", login_took)
        if login_took > 120:
            self.tell(f"Login took {login_took / 60:.0f} minutes. Anything near 30 "
                      f"minutes means the engine misses the open.", A.WARNING)

        results = self.engine.health_check()
        log.info("TIMING health check took %.0fs", time.monotonic() - t0 - login_took)
        bad = [name for name, (ok, _) in results.items() if not ok]
        if bad:
            self.tell(f"Morning check has problems: {', '.join(bad)}. Watch the first trade closely.",
                      A.WARNING)
        return True

    def start_trading(self):
        self.engine.start()
        self.tell(f"Engine started for {date.today():%d %b}.")

    def stop_trading(self):
        self.engine.stop()
        self.tell("Engine stopped for the day.")

    def evening(self):
        """Fetch today's minutes, report, then the slow stuff."""
        try:
            from trader.backtest.history import download
            download(self.market(), days=5)
            self.tell("Today's 1-minute candles saved to data/history.")
        except Exception as e:
            log.warning("Minute history download failed: %s", e)

        self.send_report(days=1)
        if date.today().weekday() == 4:              # Friday
            self.send_report(days=7)

        # Last, with the market shut and nobody waiting on it. Kotak throttles this
        # hard and the retries can take twenty minutes; that is fine at 15:20 and
        # was not fine at 08:50.
        try:
            from trader.backtest.history import download_daily
            download_daily(self.market(), years=1, pause=3.0)
        except Exception as e:
            log.warning("Daily history refresh failed: %s", e)

    def send_report(self, days: int):
        import subprocess
        import sys
        try:
            out = subprocess.run([sys.executable, "report.py", "--days", str(days)],
                                 capture_output=True, text=True, timeout=120)
            text = out.stdout.strip() or "No report produced."
        except Exception as e:
            text = f"Report failed: {e}"
        print(text, flush=True)
        self.hub.add(A.INFO, text, push=True)     # split into parts by AlertHub if long

    # ---------------------------------------------------------------- #
    def plan_for_today(self) -> str:
        d = date.today()
        if not is_trading_day(d):
            from trader.holidays import why_closed
            return f"{d:%A %d %b}: {why_closed(d) or 'market shut'}, nothing to do."
        return (f"{d:%A %d %b}: wake {WAKE_AT:%H:%M}, start {START_AT:%H:%M}, stop {STOP_AT:%H:%M}. "
                f"Strategy {self.settings.strategy}, mode {self.mode}, "
                f"live trading {'ALLOWED' if self.secrets.live_trading_allowed else 'locked'}.")

    def step(self):
        now = now_ist()
        today = now.date()
        if not is_trading_day(today):
            return
        t = now.time()

        if WAKE_AT <= t < STOP_AT and not self.done("morning"):
            self.mark("morning")
            if not self.morning():
                self.mark("start")        # don't try to trade without a login
                self.mark("stop")

        if START_AT <= t < STOP_AT and self.done("morning") and not self.done("start"):
            self.mark("start")
            self.start_trading()

        if t >= STOP_AT and self.done("start") and not self.done("stop"):
            self.mark("stop")
            self.stop_trading()
            self.evening()

    def run(self):
        try:
            self._run()
        except KeyboardInterrupt:
            self.tell("Autopilot stopped by hand.")
        finally:
            if self.engine.is_running:
                self.engine.stop()
            # Last thing before the process dies: make sure the messages left.
            if self.hub.telegram_enabled and not self.hub.flush():
                print(f"Telegram may not have received everything: "
                      f"{self.hub.telegram_error or 'timed out'}", flush=True)

    def _run(self):
        self.tell(self.plan_for_today())
        if self.once and (now_ist().time() >= STOP_AT or not is_trading_day(date.today())):
            self.tell("Nothing left to do today. Exiting.")
            return
        while True:
            try:
                self.step()
            except Exception as e:
                log.exception("Autopilot step failed")
                self.tell(f"Autopilot hit an error: {e}", A.WARNING)
            if self.once and self.done("stop"):
                self.tell("Done for today, stopping as asked.")
                return
            time.sleep(CHECK_EVERY)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # First line out, before anything that could be slow. If launchd fires at 08:45
    # and this timestamp says 08:57, the Mac was asleep and the 12 minutes were not ours.
    log.info("TIMING process alive")
    started = time.monotonic()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("paper", "live"), default="paper")
    ap.add_argument("--dry-run", action="store_true", help="print today's plan and exit")
    ap.add_argument("--once", action="store_true", help="run one day, then exit")
    args = ap.parse_args()

    pilot = Autopilot(mode=args.mode, once=args.once)
    log.info("TIMING startup took %.0fs", time.monotonic() - started)
    if args.dry_run:
        print(pilot.plan_for_today())
        print("Telegram:", "on" if pilot.hub.telegram_enabled else "off (fill .env to get messages)")
        return
    if args.mode == "live" and not pilot.secrets.live_trading_allowed:
        print("Live mode needs LIVE_TRADING=true in .env. Refusing to start.")
        return
    pilot.run()


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Why this does NOT pick the strategy by itself
#
# Switching to "whatever worked last month" is the oldest way to lose money
# slowly: you buy each strategy after its good run and sell it after its bad one.
# It is only worth automating a choice that was measured in advance, and that means
# back-tested against the alternative, not inferred from last month's P&L. When a
# rule earns its place, the switch belongs inside the strategy (like the narrow-day
# filter does), not in a scheduler guessing from recent results.
# ---------------------------------------------------------------------------
