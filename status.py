"""
Is it working? One command, plain English.

    python3 status.py              # today, plus the last two weeks of run history
    python3 status.py --days 30    # look further back

This replaces the four commands you used to need (launchctl list, tail the log,
grep TIMING, report.py). It reads files only - it never logs in, never places an
order, and never touches the running autopilot. Safe to run any time, including
while the engine is mid-trade.

Deliberately uses nothing but the Python standard library: if the venv or the
trader package is broken, this still has to be able to tell you so.
"""
import argparse
import csv
import json
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
DATA = PROJECT / "data"
LOG = DATA / "autopilot.log"
LABEL = "com.algotrader.autopilot"

PLAN_PREFIX = "Today's plan:"
MARKET_OPEN = "09:15"
ENGINE_DUE = "09:18"          # the engine starts on the first 30-second tick after 09:15,
                              # so give it a couple of minutes before calling it a problem
MORNING_DUE = "09:00"         # by now a weekday run should have left a line in the log

# A line the logger wrote: "2026-10-26 08:50:50,088 autopilot TIMING login took 3s".
# The plain print lines have no date, and the logger duplicates every one of them,
# so parsing only the dated form loses nothing and avoids counting things twice.
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ (\S+) (.*)$")


# --------------------------------------------------------------------------- #
# Reading what is on disk
# --------------------------------------------------------------------------- #
def read_log():
    """{day: [(time, logger, message), ...]} plus the first and last day seen."""
    if not LOG.exists():
        return {}, None, None
    days = {}
    with LOG.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            m = LINE.match(raw.rstrip("\n"))
            if m:
                days.setdefault(m.group(1), []).append((m.group(2), m.group(3), m.group(4)))
    if not days:
        return {}, None, None
    order = sorted(days)
    return days, order[0], order[-1]


def find(entries, needle):
    """First (time, message) whose message contains needle."""
    for when, _logger, message in entries:
        if needle in message:
            return when, message
    return None, None


def read_settings():
    path = PROJECT / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_env():
    """What .env defines. Secrets are reported as present-or-not and never read
    beyond that; LIVE_TRADING is the one value we look at, because "is real money
    switched on" is the most important line on this whole report."""
    path = PROJECT / ".env"
    filled, live = {}, False
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                name, value = name.strip(), value.strip().strip('"').strip("'")
                filled[name] = bool(value)
                if name == "LIVE_TRADING":
                    live = value.lower() in ("true", "1", "yes", "on")
    except Exception:
        pass
    return filled, live


def read_trades():
    path = DATA / "trades.csv"
    rows = []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("mode") == "demo":
                    continue
                try:
                    row["gross"] = float(row["pnl"])
                except (KeyError, ValueError):
                    continue
                rows.append(row)
    except Exception:
        pass
    return rows


def newest_day_in(path):
    """Last date in a candle CSV, and how many rows it has."""
    try:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return None, 0
        key = "date" if "date" in rows[0] else next(iter(rows[0]))
        return max(r[key][:10] for r in rows), len(rows)
    except Exception:
        return None, 0


def launch_agent_loaded():
    """True / False / None when we cannot tell (not a Mac, or launchctl missing)."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return None
        return any(LABEL in line for line in out.stdout.splitlines())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Working it out
# --------------------------------------------------------------------------- #
def market_open_on(day):
    """Weekday and not an NSE holiday. Falls back to the weekday test if the holiday
    file is missing, so status.py still works on its own."""
    if day.weekday() >= 5:
        return False
    try:
        from trader.holidays import is_trading_day
        return is_trading_day(day)
    except Exception:
        return True


def closed_because(day):
    try:
        from trader.holidays import why_closed
        return why_closed(day)
    except Exception:
        return "weekend" if day.weekday() >= 5 else ""


def trading_days_back(count, today):
    """The last `count` days the market was actually open, today first.

    Holidays used to be counted as ordinary trading days, which cut both ways:
    20 Oct 2026 was Dussehra and showed up as a day the bot "ran", while a genuinely
    missed Monday beside it looked identical."""
    out, day = [], today
    while len(out) < count:
        if market_open_on(day):
            out.append(day)
        day -= timedelta(days=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="how many weekdays of history to show")
    args = ap.parse_args()

    now = datetime.now()
    today = now.date()
    clock = now.strftime("%H:%M")
    days, first_day, last_day = read_log()
    today_key = today.isoformat()
    entries = days.get(today_key, [])
    settings = read_settings()
    env, live_trading = read_env()

    problems, notes = [], []

    # ---- did the scheduler survive? ----
    loaded = launch_agent_loaded()
    if loaded is False:
        problems.append("the weekday schedule is not installed - "
                        "run: bash install_autopilot.command")

    # ---- did it run today? ----
    alive_at, _ = find(entries, "TIMING process alive")
    if not alive_at:                                   # older versions had no TIMING line
        alive_at, _ = find(entries, "Autopilot waking up")
    shut = not market_open_on(today)
    shut_reason = closed_because(today)
    if shut:
        notes.append(f"{shut_reason or 'market shut'} - nothing is expected to run")
    elif not alive_at and clock >= MORNING_DUE:
        problems.append(f"it has not run today. Nothing in the log for {today_key}, and it is "
                        f"{clock}. The Mac was probably asleep or off at 08:45.")
    elif not alive_at:
        notes.append(f"it is {clock}; the schedule fires at 08:45")

    # ---- login, health, engine, plan ----
    login_at, login_msg = find(entries, "TIMING login took")
    failed_at, failed_msg = find(entries, "Login failed")
    health_at, health_msg = find(entries, "TIMING health check took")
    engine_at, _ = find(entries, "Engine started for")
    plan_at, plan_msg = find(entries, "Today's plan:")

    if failed_msg:
        problems.append(f"login failed at {failed_at}: {failed_msg[:110]}")
    def seconds_in(message):
        found = re.search(r"(\d+)s", message or "")
        return int(found.group(1)) if found else None

    login_seconds = seconds_in(login_msg)
    health_seconds = seconds_in(health_msg)
    if login_seconds is not None and login_seconds > 120:
        problems.append(f"login took {login_seconds}s - slow enough to risk the open")

    if alive_at and not engine_at and not shut and clock >= ENGINE_DUE and not failed_msg:
        problems.append(f"it woke at {alive_at} but the engine never started. "
                        f"Look at: tail -40 data/autopilot.log")

    # ---- anything shouting in today's log ----
    shouting = [(w, m) for w, lg, m in entries
                if lg == "alerts" and ("CRITICAL" in m or "WARNING" in m)]

    # ---- how often has it actually run? ----
    # Three outcomes, not two. A day we cannot judge is not a day it failed:
    # the log's own first day began part-way through (that is the day it was
    # installed), and today before 09:00 has not happened yet.
    ran, missed = [], []
    for day in trading_days_back(args.days, today):
        key = day.isoformat()
        if first_day and key < first_day:
            continue                                   # before the log existed
        rows = days.get(key, [])
        if find(rows, "TIMING process alive")[0] or find(rows, "Autopilot waking up")[0]:
            ran.append(day)
        elif key == first_day:
            continue                                   # log starts mid-day; nothing to blame
        elif day == today and clock < MORNING_DUE:
            continue                                   # too early to say
        else:
            missed.append(day)
    judged = len(ran) + len(missed)
    if missed and len(missed) >= max(2, judged // 4):
        problems.append(f"it missed {len(missed)} of the last {judged} trading days. "
                        f"The Mac has to be awake at 08:45.")

    # ---- a setting that is not saved is a setting that will drift back ----
    if settings.get("strategy") == "narrow_orb" and settings.get("orb_narrow_pct") is None:
        notes.append("orb_narrow_pct is not in settings.json, so it is running on the code's "
                     "default - set it, then check tomorrow that it stayed set")

    # ---- the quiet failure: no alerts configured ----
    if not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        notes.append("Telegram is off, so nothing above would ever reach your phone")

    trades = read_trades()
    daily_newest, daily_rows = newest_day_in(DATA / "history" / "NIFTY_D.csv")

    # ----------------------------------------------------------------- #
    # Print it
    # ----------------------------------------------------------------- #
    print()
    print(f"ALGO TRADER STATUS            {now:%a %d %b %Y}, {clock}")
    print("=" * 72)
    if problems:
        print("  NEEDS ATTENTION")
        for p in problems:
            print(f"    * {p}")
    elif shut:
        print(f"  RESTING - {shut_reason or 'market shut'}, no trading today. "
              f"Last run {last_day or 'never'}.")
    elif engine_at:
        print(f"  WORKING - woke {alive_at}, logged in, engine on since {engine_at}.")
    elif alive_at:
        print(f"  WORKING - woke {alive_at}, logged in. Engine starts at {MARKET_OPEN}.")
    else:
        print(f"  WAITING - the schedule fires at 08:45, it is {clock}.")
    for n in notes:
        print(f"    - {n}")

    print()
    print("TODAY")
    if not entries:
        print("   nothing in the log for today")
    else:
        print(f"   woke           {alive_at or '-'}")
        print(f"   login          {login_seconds}s" if login_seconds is not None
              else "   login          -")
        if health_seconds is not None:
            print(f"   health check   {health_seconds}s")
        print(f"   engine         {engine_at or 'not started yet'}")
        if plan_msg:
            plan_text = plan_msg.split(PLAN_PREFIX, 1)[-1].strip()
            print(f"   plan           {plan_text[:300]}")
        for when, message in shouting[:4]:
            print(f"   {when}       {message[:140]}")

    print()
    print(f"RAN ON {len(ran)} OF THE LAST {judged} TRADING DAYS")
    if ran:
        print("   ran            " + ", ".join(f"{d:%d %b}" for d in ran[:12]))
    if missed:
        print("   did NOT run    " + ", ".join(f"{d:%d %b}" for d in missed[:12]))
        print("                  (Mac asleep or off - those days were not traded)")
    if first_day:
        print(f"   log covers     {first_day} to {last_day}")

    print()
    print("SETTINGS")
    print(f"   strategy       {settings.get('strategy', '?')}")
    # 26 Oct 2026: this had quietly gone back to 30 after being set to 50 eleven days
    # earlier, and the only way we noticed was by reverse-engineering the percentile
    # out of the daily ranges. It is on the screen now.
    if settings.get("strategy") == "narrow_orb":
        pct, look = settings.get("orb_narrow_pct"), settings.get("orb_lookback_days", 20)
        if pct is None:
            print(f"   {'narrow day':<14} not in settings.json - running on the built-in default")
        else:
            print(f"   {'narrow day':<14} quietest {pct}% of the last {look} days")
    print(f"   mode           {'LIVE - REAL MONEY IS ARMED' if live_trading else 'PAPER  (live trading locked)'}")
    cap = settings.get("manual_capital")
    print(f"   capital        Rs {cap:,.0f}" if isinstance(cap, (int, float)) else "   capital        from the broker")
    loss = settings.get("daily_loss_pct")
    if isinstance(loss, (int, float)) and isinstance(cap, (int, float)):
        print(f"   daily loss cap {loss}% = Rs {cap * loss / 100:,.0f}")
    print(f"   telegram       {'on' if env.get('TELEGRAM_BOT_TOKEN') and env.get('TELEGRAM_CHAT_ID') else 'OFF'}")

    print()
    print("TRADES AND DATA")
    # trades.csv keeps no strategy column, so every row looks alike. It is not:
    # the three trades dated 25 Sep 2026 belong to the abandoned prev_day breakout,
    # and counting them as evidence about narrow_orb would be simply wrong. The
    # log's first day is the honest dividing line we actually have.
    strategy = settings.get("strategy", "current")
    # Whichever is EARLIER: the log's first day, or a fortnight ago. Taking the log
    # alone would orphan last week's trades the moment the log is rotated or deleted.
    fortnight = (today - timedelta(days=14)).isoformat()
    cutoff = min(first_day, fortnight) if first_day else fortnight
    now_trades = [r for r in trades if r.get("date", "") >= cutoff]
    old_trades = [r for r in trades if r.get("date", "") < cutoff]

    if now_trades:
        total = sum(r["gross"] for r in now_trades)
        wins = sum(1 for r in now_trades if r["gross"] > 0)
        print(f"   {strategy:<14} {len(now_trades)} trades, {wins} green, gross Rs {total:+,.0f}")
        print(f"   {'':<14} (before charges - report.py has the real number)")
        if len(now_trades) < 12:
            print(f"   {'':<14} {12 - len(now_trades)} more before the numbers mean anything")
    else:
        print(f"   {strategy:<14} no trades yet - it trades about one day in three")
    if old_trades:
        total = sum(r["gross"] for r in old_trades)
        print(f"   earlier        {len(old_trades)} trades up to "
              f"{max(r.get('date', '') for r in old_trades)}, gross Rs {total:+,.0f}")
        print(f"   {'':<14} (an older strategy - not evidence about {strategy})")
    if daily_newest:
        age = (today - date.fromisoformat(daily_newest)).days
        state = "current" if age <= 4 else f"STALE - {age} days old"
        print(f"   daily history  {daily_rows:,} days, newest {daily_newest} ({state})")
    else:
        print("   daily history  missing - run: python3 backtest.py --download-daily 5")
    print()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass            # piped into head or less and you quit early; nothing is wrong
