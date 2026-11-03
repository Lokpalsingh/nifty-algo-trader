"""
The engine ties everything together and runs the strategy about once a second
in the background, while the dashboard shows what's happening.

Modes:
  demo   - fake prices, fake orders. No Kotak login. Runs a fast clock so a
           whole day plays out in minutes. For learning how it behaves.
  paper  - REAL Kotak prices, SIMULATED orders. Needs Kotak login.
           Use this for many days before going live.
  live   - REAL prices, REAL orders. Only unlocked when LIVE_TRADING=true
           is set in your .env file.
"""
import logging
import threading
from typing import Optional

from trader import alerts as A
from trader.alerts import AlertHub
from trader.brokers.base import BrokerError
from trader.brokers.demo_market import DemoMarket
from trader.brokers.kotak_neo import KotakNeoBroker
from trader.brokers.paper import PaperBroker
from trader.clock import FastClock, RealClock
from trader.config import Secrets, Settings
from trader.journal import Journal
from trader.risk import RiskManager
from trader.strategy import PrevDayBreakout
from trader.strategy_orb import NarrowOrb

STRATEGIES = {"prev_day": PrevDayBreakout, "narrow_orb": NarrowOrb}

# Every broker the app can talk to, by the name you put in settings.json.
#
# Only Kotak Neo is written so far. To add yours: write an adapter in
# trader/brokers/ that implements trader/brokers/base.py, import it above, and add
# one line here. Nothing else in the app changes - the strategies, risk manager,
# journal and dashboard only ever speak to the Broker interface. BROKERS.md walks
# through it method by method.
BROKERS = {"kotak": KotakNeoBroker}

log = logging.getLogger("engine")

MODES = ("demo", "paper", "live")


class Engine:
    def __init__(self, settings: Optional[Settings] = None, secrets: Optional[Secrets] = None,
                 demo_speed: float = 30.0):
        self.settings = settings or Settings.load()
        self.secrets = secrets or Secrets.from_env()
        self.demo_speed = demo_speed
        self.mode = "demo"
        self._health_check_day = ""
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._consecutive_errors = 0
        self._first_error_at = None
        self.last_error = ""
        self._build("demo")

    # ------------------------------------------------------------------ #
    def _build(self, mode: str):
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode}")
        if mode == "live" and not self.secrets.live_trading_allowed:
            raise ValueError("Live mode is locked. Set LIVE_TRADING=true in .env and restart the app.")
        self.mode = mode
        if mode == "demo":
            self.clock = FastClock(speed=self.demo_speed)
            self.broker = PaperBroker(DemoMarket(self.clock), name="Demo")
        else:
            self.clock = RealClock()
            name = (self.settings.broker or "kotak").lower()
            if name not in BROKERS:
                raise ValueError(f"Unknown broker '{name}'. Known: {', '.join(sorted(BROKERS))}. "
                                 f"See BROKERS.md to add another one.")
            real = BROKERS[name](self.secrets, product=self.settings.product)
            self.broker = (PaperBroker(real, name=f"Paper ({real.name} prices)")
                           if mode == "paper" else real)
        self.alerts = AlertHub(self.secrets.telegram_bot_token, self.secrets.telegram_chat_id, clock=self.clock)
        self.risk = RiskManager(self.settings)
        self.journal = Journal(mode=mode)
        strategy_class = STRATEGIES.get(self.settings.strategy, PrevDayBreakout)
        self.strategy = strategy_class(self.broker, self.risk, self.journal, self.alerts, self.settings,
                                       self.clock, mode=mode, persist=(mode != "demo"))

    def set_mode(self, mode: str):
        with self._lock:
            if self.is_running:
                raise RuntimeError("Stop the engine before changing mode")
            self._build(mode)
            self.alerts.add(A.INFO, f"Mode set to {mode.upper()}")

    # ------------------------------------------------------------------ #
    @property
    def needs_login(self) -> bool:
        return self.mode != "demo" and not self.broker.is_logged_in()

    def login(self, totp_code: Optional[str] = None):
        with self._lock:
            self.broker.login(totp_code)
            self.alerts.add(A.INFO, f"Logged in to Kotak Neo ({self.mode} mode)")

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        with self._lock:
            if self.is_running:
                return
            if self.needs_login:
                raise BrokerError("Log in to Kotak Neo first")
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="engine", daemon=True)
            self._thread.start()
            label = {"demo": "DEMO (fake prices)", "paper": "PAPER (real prices, simulated orders)",
                     "live": "LIVE (real orders)"}[self.mode]
            self.alerts.add(A.WARNING if self.mode == "live" else A.INFO, f"Engine started in {label}")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        open_text = self.strategy.open_position_text()
        if open_text and self.mode != "demo":
            self.alerts.add(A.CRITICAL, f"Engine stopped with an OPEN position: {open_text}. "
                                        f"Nothing is watching it until you press Start again or "
                                        f"exit it in the Kotak app.")
        else:
            self.alerts.add(A.INFO, "Engine stopped")

    def kill_switch(self):
        with self._lock:
            self.strategy.kill_switch()

    def resume(self):
        with self._lock:
            self.strategy.resume()

    def reset_today(self) -> str:
        with self._lock:
            return self.strategy.reset_today()

    def update_settings(self, new: Settings):
        with self._lock:
            new.validate()
            switching = new.strategy != self.settings.strategy
            if switching and self.is_running:
                raise RuntimeError("Stop the engine before switching strategy")
            new.save()
            self.settings = new
            if switching:
                if self.strategy.position:
                    raise RuntimeError("Close the open trade before switching strategy")
                self._clear_todays_plan()
                strategy_class = STRATEGIES.get(new.strategy, PrevDayBreakout)
                self.strategy = strategy_class(self.broker, self.risk, self.journal, self.alerts, new,
                                               self.clock, mode=self.mode, persist=(self.mode != "demo"))
                self.alerts.add(A.INFO, f"Strategy switched to {new.strategy}. Today's plan will be "
                                        f"worked out again from scratch.")
            self.strategy.settings = new
            self.risk.settings = new
            if hasattr(self.broker, "product"):
                self.broker.product = new.product
            self.alerts.add(A.INFO, "Settings saved. They apply to new trades.")

    def _clear_todays_plan(self):
        """A new strategy must make its own plan. Wipe today's saved plan and the
        'done for today' flag, but keep the trade count and P&L, which still count
        against today's limits."""
        day = self.clock.now().date().isoformat()
        saved = self.journal.load_state(day)
        if not saved:
            return
        saved.update(plan=None, done_reason="", armed=None, paused_reason="",
                     or_high=None, or_low=None, entry_spot=None,
                     reentry_contract=None, reentry_after="")
        self.journal.save_state(day, saved)

    # ------------------------------------------------------------------ #
    def health_check(self) -> dict:
        """Ask the broker for everything the day needs, once, and say what is
        missing instead of silently skipping the day."""
        s = self.settings
        today = self.clock.now().date()
        results = {}

        def check(name, fn):
            try:
                value = fn()
                results[name] = (True, value)
            except Exception as e:
                results[name] = (False, str(e))

        check("NIFTY price", lambda: f"{self.broker.get_index_ltp(s.index):,.2f}")
        def candle_info():
            c = self.broker.get_previous_day_candle(s.index, today)
            return f"{c.date} {c.color.lower()}" if c else "none"

        check("Yesterday's candle", candle_info)
        check("Capital", lambda: (f"₹{self.broker.get_available_capital():,.0f}"
                                  if s.capital_source == "broker" else f"₹{s.manual_capital:,.0f} (typed in)"))
        check("Expiry", lambda: self.broker.get_expiry(s.index, today, s.expiry_choice))
        if s.strategy == "narrow_orb":
            check("Daily history", lambda: f"{len(self.broker.get_daily_candles(s.index, s.orb_lookback_days))} days")
            check("Minute candles", lambda: f"{len(self.broker.get_minute_candles(s.index, '09:15'))} candles")

        bad = [name for name, (ok, _) in results.items() if not ok]
        lines = ", ".join(f"{name}: {value}" if ok else f"{name}: FAILED ({value})"
                          for name, (ok, value) in results.items())
        self.alerts.add(A.WARNING if bad else A.INFO,
                        ("Morning check found problems - " if bad else "Morning check OK - ") + lines)
        return results

    def _run(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    if self.mode != "demo" and self._health_check_day != self.clock.now().date().isoformat():
                        self._health_check_day = self.clock.now().date().isoformat()
                        self.health_check()
                    self.strategy.tick()
                self._on_success()
            except Exception as e:
                self._on_error(e)
            self.clock.sleep(self.settings.poll_seconds, self._stop)

    def _on_success(self):
        if self.strategy.connection_lost:
            self.strategy.connection_lost = False
            self.alerts.add(A.WARNING, "Connection restored")
        self._consecutive_errors = 0
        self._first_error_at = None
        self.last_error = ""

    def _on_error(self, e: Exception):
        self._consecutive_errors += 1
        self.last_error = str(e)
        now = self.clock.now()
        if self._first_error_at is None:
            self._first_error_at = now
        if not isinstance(e, BrokerError):
            log.exception("Unexpected error in strategy tick")
        down_for = (now - self._first_error_at).total_seconds()
        if down_for >= self.settings.connection_lost_seconds and not self.strategy.connection_lost:
            self.strategy.connection_lost = True
            open_text = self.strategy.open_position_text()
            extra = f" OPEN position: {open_text}." if open_text else ""
            self.alerts.add(A.CRITICAL, f"Connection problem for {down_for:.0f}s, new entries paused. "
                                        f"Last error: {e}.{extra}")

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        # Don't freeze the dashboard while the strategy waits for an order fill
        got_lock = self._lock.acquire(timeout=0.5)
        try:
            snap = self.strategy.snapshot()
            snap.update(mode=self.mode, running=self.is_running, needs_login=self.needs_login,
                        clock=self.clock.now().strftime("%a %d %b %H:%M:%S"), last_error=self.last_error,
                        telegram=self.alerts.telegram_enabled, telegram_error=self.alerts.telegram_error,
                        live_allowed=self.secrets.live_trading_allowed)
            return snap
        finally:
            if got_lock:
                self._lock.release()
