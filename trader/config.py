"""
Two kinds of configuration live here:

1. SECRETS (Kotak login, Telegram token) come only from your local .env file.
   They are never saved anywhere else and never shown on the dashboard.

2. STRATEGY SETTINGS (lots, stop-loss, times, limits) live in settings.json.
   You can change them from the dashboard's Settings tab, no coding needed.
"""
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

log = logging.getLogger("config")

PROJECT_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    # An explicit path, not "wherever we happen to be running": launchd starts
    # processes in whatever folder it likes, and a .env it couldn't find looks
    # exactly like a missing password.
    load_dotenv(PROJECT_DIR / ".env")
except ImportError:
    pass

DATA_DIR = PROJECT_DIR / "data"
SETTINGS_FILE = PROJECT_DIR / "settings.json"
LIVE_SETTINGS_FILE = PROJECT_DIR / "settings.json"   # never reassigned; the real one

# Set when a test run was caught writing the live settings file (see Settings.save).
LAST_TEST_DIVERSION: Optional[Path] = None


def _test_runner_active() -> bool:
    """True when a test runner is loaded in this process. Nothing in the running
    app imports these, so this can only be true under `unittest` or `pytest`."""
    return any(name in sys.modules for name in ("unittest", "pytest"))


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Secrets (.env)
# --------------------------------------------------------------------------- #
@dataclass
class Secrets:
    kotak_consumer_key: str = ""
    kotak_mobile_number: str = ""
    kotak_ucc: str = ""
    kotak_mpin: str = ""
    kotak_totp_secret: str = ""        # optional: lets the app make the 6-digit code itself
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    live_trading_allowed: bool = False  # master switch, must be true to unlock Live mode

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            kotak_consumer_key=os.getenv("KOTAK_CONSUMER_KEY", "").strip(),
            kotak_mobile_number=os.getenv("KOTAK_MOBILE_NUMBER", "").strip(),
            kotak_ucc=os.getenv("KOTAK_UCC", "").strip(),
            kotak_mpin=os.getenv("KOTAK_MPIN", "").strip(),
            kotak_totp_secret=os.getenv("KOTAK_TOTP_SECRET", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            live_trading_allowed=_env_bool("LIVE_TRADING", False),
        )

    def missing_kotak_fields(self):
        required = {
            "KOTAK_CONSUMER_KEY": self.kotak_consumer_key,
            "KOTAK_MOBILE_NUMBER": self.kotak_mobile_number,
            "KOTAK_UCC": self.kotak_ucc,
            "KOTAK_MPIN": self.kotak_mpin,
        }
        return [name for name, value in required.items() if not value]


# --------------------------------------------------------------------------- #
# Index details. Only NIFTY is used right now; others can be added later.
# --------------------------------------------------------------------------- #
INDEXES = {
    "NIFTY": {
        "quote_token": "Nifty 50",   # Kotak quotes/feeds use the index name as token
        "quote_segment": "nse_cm",
        "fo_segment": "nse_fo",
        "underlying": "NIFTY",       # name used by Kotak's option chain / expiries
        "strike_step": 50,
        "yahoo_symbol": "^NSEI",     # backup source for yesterday's candle
    },
}

# India VIX, used by the backtest as each day's option volatility
VIX = {"quote_token": "INDIA VIX", "quote_segment": "nse_cm", "yahoo_symbol": "^INDIAVIX"}


# --------------------------------------------------------------------------- #
# Strategy settings (settings.json)
# --------------------------------------------------------------------------- #
@dataclass
class ManualCandle:
    """Yesterday's candle typed in by hand, used only if automatic fetch fails
    or you want to override it. date must be the previous trading day."""
    date: str = ""          # "YYYY-MM-DD"
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0


@dataclass
class Settings:
    # --- Which strategy ---
    #   prev_day   = yesterday's high/low breakout (your original)
    #   narrow_orb = after a narrow day, break of the first 15 minutes' range,
    #                stop and target in INDEX points, deep in-the-money option
    strategy: str = "prev_day"

    # --- Who to trade through ---
    # The name of a broker adapter listed in trader/engine.py's BROKERS registry.
    # Only "kotak" exists so far; BROKERS.md explains how to add another.
    broker: str = "kotak"

    # --- What to trade ---
    index: str = "NIFTY"
    lots: int = 1
    product: str = "MIS"                 # MIS = intraday
    expiry_choice: str = "nearest"       # "nearest" or "next_on_expiry_day"

    # --- Strategy rules ---
    setup_time: str = "09:16"            # when to read yesterday's candle
    entry_start: str = "09:31"
    entry_end: str = "15:02"
    exit_time: str = "15:14"
    signal_mode: str = "breakout"        # "breakout" (follow) or "reverse" (fade the break)
    strike_offset_steps: int = 0         # 0 = ATM, +1 = one strike OTM, -1 = one strike ITM
    stop_loss_mode: str = "points"       # "points" or "percent" of the buy price
    stop_loss_points: float = 20.0       # on option premium
    stop_loss_percent: float = 20.0      # used when stop_loss_mode is "percent"
    target_points: float = 0.0           # 0 = no profit target
    target_capital_pct: float = 6.0      # sell when ONE trade is up this % of capital (0 = off)
    trail_enabled: bool = False          # move the stop-loss up as the price rises
    trail_mode: str = "candle_low"       # "candle_low", "points" or "percent"
    trail_distance: float = 20.0         # points/percent modes: stays this far below the highest price
    trail_start_points: float = 0.0      # only start trailing once this much in profit
    trail_candle_minutes: int = 1        # candle_low mode: candle length on the option's own chart
    trail_candle_buffer: float = 0.0     # candle_low mode: extra rupees below the red candle's low
    min_profit_rupees: float = 0.0       # once a trade is this much up, the SL jumps to lock it;
                                         # before that the first SL stays put (0 = off)
    max_trades_per_day: int = 5
    # After a stop-loss (or trailing stop-loss) exit, how to buy again:
    #   "level"        = NIFTY must come back inside yesterday's level and break it again
    #   "green_candle" = same option all day; buy again when its own chart breaks
    #                    the high of a green candle that formed after the exit
    reentry_mode: str = "level"
    breakout_buffer_points: float = 0.0  # NIFTY must cross the level by this much
    enter_if_already_broken: bool = False  # if NIFTY is already past the level at 9:31
    cooldown_minutes_after_sl: int = 0
    skip_doji_days: bool = True

    # --- Narrow-day opening range strategy (strategy "narrow_orb") ---
    orb_minutes: int = 15                # length of the opening range
    orb_narrow_pct: float = 30.0         # yesterday's range must be in the bottom X% of recent days
    orb_lookback_days: int = 20          # how many days "recent" means
    orb_entry_end: str = "14:00"         # no new entries after this
    orb_stop_points: float = 30.0        # stop, in NIFTY index points
    orb_target_r: float = 5.0            # target = stop x this, also in index points
    orb_itm_steps: int = 6               # strikes in the money (keeps more of a big move and
                                         # loses less time value; 4 and 6 both tested)
    orb_delta_guess: float = 1.5         # backup stop-loss at the broker, as a multiple of the index
                                         # stop: wider on purpose, so the index rule exits first

    # --- Safety ---
    daily_loss_pct: float = 1.0
    capital_source: str = "broker"       # "broker" (Kotak available funds) or "manual"
    manual_capital: float = 100000.0
    max_lots_per_order: int = 2
    max_orders_per_day: int = 60

    # --- Order handling ---
    order_price_buffer: float = 2.0      # limit price = LTP +/- this many rupees
    sl_limit_buffer: float = 5.0         # stop-loss limit price sits this far below trigger
    sl_grace_seconds: int = 3            # if SL order hasn't filled by then, software exits
    fill_timeout_seconds: int = 10

    # --- Engine ---
    poll_seconds: float = 1.0
    connection_lost_seconds: int = 15

    # --- Manual backup for yesterday's candle ---
    use_manual_candle: bool = False
    manual_candle: ManualCandle = field(default_factory=ManualCandle)

    @property
    def index_info(self):
        return INDEXES[self.index]

    # ---- load / save ----
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        """Read settings.json, creating it with defaults if it is not there.

        The path is resolved HERE, not as a default argument. A default argument is
        bound once when this module is imported, which made the file impossible to
        redirect - see the note in save()."""
        target = Path(path) if path is not None else SETTINGS_FILE
        if not target.exists():
            settings = cls()
            settings.save(target)
            return settings
        raw = json.loads(target.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in raw.items() if k in known}
        if clean.get("strategy") not in ("prev_day", "narrow_orb"):
            clean["strategy"] = "prev_day"
        candle = clean.pop("manual_candle", None) or {}
        settings = cls(**clean)
        settings.manual_candle = ManualCandle(**{
            k: v for k, v in candle.items() if k in {f.name for f in fields(ManualCandle)}
        })
        settings.validate()
        return settings

    def save(self, path: Optional[Path] = None) -> None:
        """Write settings.json.

        Two things happened here that are worth not repeating.

        The path used to be a default argument, `path: Path = SETTINGS_FILE`. Python
        evaluates that once, at import, so the real file was welded into the function
        signature and no test could point it anywhere else. It is resolved at call
        time now, so `trader.config.SETTINGS_FILE` can be redirected.

        And on 26 Oct 2026 we found that tests/test_engine_switch.py had been writing
        the live settings file on every test run since 15 Oct: it switches strategy,
        Engine.update_settings calls save(), and orb_narrow_pct went back to its
        default of 30 each time. Eleven days of running the wrong configuration, with
        nothing on any screen to show it. So a test caught doing that now gets its
        write diverted to a scratch file and a loud warning. Diverting beats
        corrupting the file, and beats failing the test, which would bury the
        complaint under a stack trace."""
        self.validate()
        target = Path(path) if path is not None else SETTINGS_FILE
        if path is None and _test_runner_active() and target == LIVE_SETTINGS_FILE:
            global LAST_TEST_DIVERSION
            LAST_TEST_DIVERSION = target
            target = Path(tempfile.gettempdir()) / "algo_trader_settings_under_test.json"
            log.warning("A test tried to write the live %s - diverted to %s. Call "
                        "tests.isolation.use_temp_settings(self) in that test's setUp.",
                        LIVE_SETTINGS_FILE.name, target)
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        errors = []
        if self.index not in INDEXES:
            errors.append(f"Unknown index {self.index}")
        if self.lots < 1:
            errors.append("Lots must be at least 1")
        if self.lots > self.max_lots_per_order:
            errors.append("Lots is higher than the max lots per order limit")
        if self.stop_loss_points <= 0:
            errors.append("Stop-loss must be more than 0")
        if not (0 < self.daily_loss_pct <= 20):
            errors.append("Daily loss % must be between 0 and 20")
        if self.min_profit_rupees < 0:
            errors.append("Minimum profit can't be negative")
        if self.reentry_mode not in ("level", "green_candle"):
            errors.append("reentry_mode must be 'level' or 'green_candle'")
        if not (0 <= self.target_capital_pct <= 100):
            errors.append("Profit target % of capital must be between 0 (off) and 100")
        if self.max_trades_per_day < 1:
            errors.append("Max trades per day must be at least 1")
        for name in ("setup_time", "entry_start", "entry_end", "exit_time"):
            value = getattr(self, name)
            try:
                hh, mm = value.split(":")
                assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
            except Exception:
                errors.append(f"{name} must look like 09:31")
        if not errors and not (self.setup_time < self.entry_start < self.entry_end < self.exit_time):
            errors.append("Times must be in order: setup < entry start < entry end < exit")
        if self.strategy not in ("prev_day", "narrow_orb"):
            errors.append("strategy must be 'prev_day' or 'narrow_orb'")
        if not (1 <= self.orb_minutes <= 60):
            errors.append("Opening range must be 1 to 60 minutes")
        if not (1 <= self.orb_narrow_pct <= 100):
            errors.append("Narrow-day % must be between 1 and 100")
        if self.orb_stop_points <= 0 or self.orb_target_r <= 0:
            errors.append("Opening range stop and target must be more than 0")
        if not (-10 <= self.orb_itm_steps <= 10):
            errors.append("In-the-money steps must be between -10 and 10")
        if self.signal_mode not in ("breakout", "reverse"):
            errors.append("signal_mode must be 'breakout' or 'reverse'")
        if self.stop_loss_mode not in ("points", "percent"):
            errors.append("stop_loss_mode must be 'points' or 'percent'")
        if not (0 < self.stop_loss_percent <= 90):
            errors.append("Stop-loss percent must be between 0 and 90")
        if not (-5 <= self.strike_offset_steps <= 5):
            errors.append("Strike offset must be between -5 and +5")
        if self.target_points < 0:
            errors.append("Target cannot be negative")
        if self.trail_mode not in ("candle_low", "points", "percent"):
            errors.append("trail_mode must be 'candle_low', 'points' or 'percent'")
        if self.trail_enabled and self.trail_mode != "candle_low" and self.trail_distance <= 0:
            errors.append("Trail distance must be more than 0")
        if not (1 <= self.trail_candle_minutes <= 60):
            errors.append("Trail candle minutes must be between 1 and 60")
        if self.capital_source not in ("broker", "manual"):
            errors.append("capital_source must be 'broker' or 'manual'")
        if self.expiry_choice not in ("nearest", "next_on_expiry_day"):
            errors.append("expiry_choice must be 'nearest' or 'next_on_expiry_day'")
        if errors:
            raise ValueError("; ".join(errors))


def manual_candle_or_none(settings: Settings) -> Optional[ManualCandle]:
    if not settings.use_manual_candle:
        return None
    c = settings.manual_candle
    if not c.date or c.high <= 0 or c.low <= 0 or c.high < c.low:
        return None
    return c
