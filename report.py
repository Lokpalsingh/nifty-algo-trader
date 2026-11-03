"""
What the bot actually did. Run it whenever, or every Friday evening.

    python3 report.py                # the last 7 days
    python3 report.py --days 30      # a month
    python3 report.py --telegram     # also send the summary to your phone

It reads data/trades.csv, adds the charges the dashboard's P&L leaves out, and
compares what happened with what the backtest said to expect. Paper and live
trades are counted separately; demo trades are ignored.
"""
import argparse
import csv
from collections import defaultdict
from datetime import date, datetime, timedelta

from trader.backtest.costs import CostModel
from trader.config import DATA_DIR, Secrets, Settings

# Rupees per trade your own backtest predicts, by strategy name. Fill these in from
# `python3 backtest.py` and the report will tell you how far live results have drifted
# from the backtest - which is the number that matters, not the P&L on its own.
# Leave a strategy out and the report simply omits the comparison for it.
EXPECTED: dict = {}
EXPECTED_NOTE: dict = {}


def load_rows(days: int):
    path = DATA_DIR / "trades.csv"
    if not path.exists():
        return []
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("mode") == "demo" or row["date"] < cutoff:
                continue
            try:
                row["entry"] = float(row["entry_price"])
                row["exit"] = float(row["exit_price"])
                row["qty"] = int(row["qty"])
                row["gross"] = float(row["pnl"])
            except (ValueError, KeyError):
                continue
            rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--telegram", action="store_true", help="send the summary to Telegram as well")
    args = ap.parse_args()

    settings = Settings.load()
    costs = CostModel()
    rows = load_rows(args.days)
    title = f"Trading report, last {args.days} days ({settings.strategy})"
    lines = [title, "=" * len(title)]

    if not rows:
        lines.append("No paper or live trades in this period.")
        lines.append("That is normal for the narrow-day strategy: it trades about one day in three,")
        lines.append("and only after a quiet day.")
    else:
        by_day = defaultdict(list)
        for r in rows:
            r["charges"] = costs.round_trip(r["entry"], r["exit"], r["qty"])
            r["net"] = r["gross"] - r["charges"]
            by_day[r["date"]].append(r)

        lines.append(f"{'Date':<12}{'Trades':>7}{'Gross':>11}{'Charges':>10}{'Net':>11}")
        for day in sorted(by_day):
            day_rows = by_day[day]
            lines.append(f"{day:<12}{len(day_rows):>7}"
                         f"{sum(r['gross'] for r in day_rows):>+11,.0f}"
                         f"{sum(r['charges'] for r in day_rows):>10,.0f}"
                         f"{sum(r['net'] for r in day_rows):>+11,.0f}")

        gross = sum(r["gross"] for r in rows)
        charges = sum(r["charges"] for r in rows)
        net = gross - charges
        wins = [r for r in rows if r["net"] > 0]
        avg = net / len(rows)
        lines += ["-" * 51,
                  f"{'TOTAL':<12}{len(rows):>7}{gross:>+11,.0f}{charges:>10,.0f}{net:>+11,.0f}",
                  "",
                  f"Win rate      : {len(wins) / len(rows) * 100:.0f}%  ({len(wins)} of {len(rows)})",
                  f"Average trade : Rs {avg:+,.0f} after charges",
                  f"Best / worst  : Rs {max(r['net'] for r in rows):+,.0f} / "
                  f"Rs {min(r['net'] for r in rows):+,.0f}"]

        reasons = defaultdict(int)
        for r in rows:
            reasons[r["exit_reason"]] += 1
        lines.append("How trades ended:")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"   {count:>3}  {reason}")

        expected = EXPECTED.get(settings.strategy)
        if expected is not None:
            lines += ["",
                      f"Expected   : Rs {expected:+,.0f} a trade  ({EXPECTED_NOTE[settings.strategy]})",
                      f"Actual     : Rs {avg:+,.0f} a trade",
                      f"Difference : Rs {avg - expected:+,.0f} a trade"]
            if len(rows) < 12:
                lines.append("With fewer than a dozen trades this difference means very little.")

    lines += ["", "Reminder: paper trades, no real money. LIVE_TRADING stays false until months of "
                  "evidence say otherwise."]
    text = "\n".join(lines)
    print(text)

    if args.telegram:
        from trader.alerts import AlertHub
        secrets = Secrets.from_env()
        hub = AlertHub(secrets.telegram_bot_token, secrets.telegram_chat_id)
        if not hub.telegram_enabled:
            print("\nTelegram isn't set up in .env, so nothing was sent.")
        else:
            hub.add("INFO", text, push=True)
            # The sender is a daemon thread: exiting now would kill it mid-request.
            print("\nSent to Telegram." if hub.flush() else
                  f"\nTelegram did not confirm: {hub.telegram_error or 'timed out'}")


if __name__ == "__main__":
    main()
