"""
Backtest your strategy on past NIFTY data.

First time (downloads and saves history, needs Kotak login):
    python backtest.py --download 180

After that it runs offline from the saved file:
    python backtest.py
    python backtest.py --compare          # tests several rule variants
    python backtest.py --iv 16 --slippage 2

Honest limits, please read:
  * NIFTY prices are real (Kotak 1-minute history).
  * Option premiums are CALCULATED with Black-Scholes, not real option
    history, because Kotak keeps no price history for expired contracts.
    So entry/exit timing is realistic, rupee amounts are an estimate.
    If India VIX history was downloaded, each day uses that day's opening VIX
    as the volatility; otherwise the fixed --iv. VIX stays fixed during the day
    and all strikes share it (no skew), so crash days are UNDER-stated.
  * Costs are included (brokerage, STT, exchange, SEBI, stamp duty, GST) and
    so is slippage, but real fills can still be worse.
  * A good backtest is not a promise. It's the minimum evidence before risking
    money, not proof of future profits.
"""
import argparse
import csv
import sys
from dataclasses import replace

from trader.backtest.costs import CostModel
from trader.backtest.history import csv_path, download, download_vix, load_bars, load_vix
from trader.backtest.runner import run_backtest
from trader.config import DATA_DIR, Secrets, Settings


def rupees(value: float) -> str:
    return f"₹{value:,.0f}"


def print_report(result, summary: dict, title: str):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)
    print(f"Days tested            : {summary['days_tested']}")
    print(f"Days with a trade      : {summary['days_with_trades']} "
          f"({summary['days_with_trades'] / max(1, summary['days_tested']) * 100:.0f}% of days)")
    print(f"Total trades           : {summary['trades']}")
    if not summary["trades"]:
        print("\nNo trades at all. The breakout level was never crossed in the entry window.")
        return
    print(f"Winning trades         : {summary['wins']} ({summary['win_rate']:.0f}%)")
    print(f"Average win / loss     : {rupees(summary['avg_win'])} / {rupees(summary['avg_loss'])}")
    print(f"Average trade          : {rupees(summary['avg_trade'])}")
    print(f"Profit factor          : {summary['profit_factor']:.2f}   (above 1.0 = profitable; "
          f"1.3+ is usually needed to survive live)")
    print("-" * 62)
    print(f"Gross P&L              : {rupees(summary['gross'])}")
    print(f"Charges + slippage     : {rupees(-summary['costs'])}")
    print(f"NET P&L                : {rupees(summary['net'])}")
    print(f"Net per trading day    : {rupees(summary['net'] / max(1, summary['days_tested']))}")
    print("-" * 62)
    print(f"Best day / worst day   : {rupees(summary['best_day'])} / {rupees(summary['worst_day'])}")
    print(f"Worst drawdown         : {rupees(summary['max_drawdown'])}")
    print("How trades ended       :")
    for reason, count in sorted(summary["exit_reasons"].items(), key=lambda kv: -kv[1]):
        print(f"   {count:>4}  {reason}")


def save_trades(result, name: str):
    path = DATA_DIR / f"backtest_{name}.csv"
    rows = result.trades
    if not rows:
        return None
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def split_halves(result):
    """Net P&L in the first half and second half of the tested period.
    A setting that only works in one half is probably luck."""
    days = result.days
    mid = len(days) // 2
    return (round(sum(d.net for d in days[:mid])), round(sum(d.net for d in days[mid:])))


def run_sweep(bars, base: Settings, common: dict, top: int):
    """Test many rule combinations and rank them by net P&L."""
    from dataclasses import replace

    capital = common.get("capital") or 0.0
    lot = common.get("lot_size") or 65
    limit = capital * base.daily_loss_pct / 100.0
    print(f"\nCapital {rupees(capital)}, daily loss limit {rupees(limit)} "
          f"({base.daily_loss_pct:g}%), {base.lots} lot(s) of {lot}.")
    print("A trade is refused when one stop-loss would break the daily limit:")
    for points in (20, 30, 40):
        cost = points * lot * base.lots
        print(f"   {points}-point stop = {rupees(cost)}  "
              f"{'OK' if cost <= limit else 'REFUSED, every trade blocked'}")

    stops = [("SL 20 pts", dict(stop_loss_mode="points", stop_loss_points=20.0)),
             ("SL 30 pts", dict(stop_loss_mode="points", stop_loss_points=30.0)),
             ("SL 40 pts", dict(stop_loss_mode="points", stop_loss_points=40.0)),
             ("SL 30%", dict(stop_loss_mode="percent", stop_loss_percent=30.0))]
    entries = [("from 09:31", dict(entry_start="09:31")),
               ("from 10:15", dict(entry_start="10:15"))]
    buffers = [("buffer 0", dict(breakout_buffer_points=0.0)),
               ("buffer 20", dict(breakout_buffer_points=20.0))]
    trails = [("no trail", dict(trail_enabled=False)),
              ("trail 20pts", dict(trail_enabled=True, trail_mode="points", trail_distance=20.0))]
    targets = [("no target", dict(target_points=0.0)),
               ("target 50", dict(target_points=50.0))]
    signals = [("breakout", dict(signal_mode="breakout")),
               ("reverse", dict(signal_mode="reverse"))]

    combos = []
    for a in stops:
        for b in entries:
            for c in buffers:
                for d in trails:
                    for e in targets:
                        for f in signals:
                            parts = [a, b, c, d, e, f]
                            name = " | ".join(p[0] for p in parts)
                            changes = {}
                            for p in parts:
                                changes.update(p[1])
                            combos.append((name, replace(base, **changes)))

    print(f"\nTesting {len(combos)} combinations. This takes a few minutes...\n")
    rows = []
    for i, (name, variant) in enumerate(combos, 1):
        result = run_backtest(bars, variant, label=name, **common)
        s = result.summary()
        first, second = split_halves(result)
        rows.append({"name": name, "trades": s["trades"], "win": s["win_rate"], "net": s["net"],
                     "dd": s["max_drawdown"], "first": first, "second": second})
        if i % 10 == 0 or i == len(combos):
            print(f"   {i}/{len(combos)} done")

    rows.sort(key=lambda r: -r["net"])
    path = DATA_DIR / "backtest_sweep.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    def table(title, subset):
        print("\n" + "=" * 100)
        print(title)
        print("=" * 100)
        print(f"{'Combination':<58}{'Trades':>7}{'Win%':>6}{'Net':>11}{'1st half':>10}{'2nd half':>10}")
        for r in subset:
            print(f"{r['name']:<58}{r['trades']:>7}{r['win']:>5.0f}%{rupees(r['net']):>11}"
                  f"{rupees(r['first']):>10}{rupees(r['second']):>10}")

    table(f"BEST {top}", rows[:top])
    table("WORST 5", rows[-5:])

    profitable = [r for r in rows if r["net"] > 0]
    both_halves = [r for r in profitable if r["first"] > 0 and r["second"] > 0]
    empty = [r for r in rows if not r["trades"]]
    print("\n" + "-" * 100)
    print(f"Combinations tested            : {len(rows)}")
    if empty:
        print(f"Combinations with NO trades    : {len(empty)}  "
              f"(the daily loss limit refused every trade -- nothing was tested)")
        print(f"   Re-run with a bigger --capital or --loss-pct so the stop-loss fits "
              f"under the limit,\n   then scale the position size back to your real money.")
    print(f"Profitable overall             : {len(profitable)}")
    print(f"Profitable in BOTH halves      : {len(both_halves)}")
    print(f"Full table saved to {path}")
    print("\nHow to read this:")
    print(" * If almost none are profitable, the signal itself has no edge and no setting will fix it.")
    print(" * A combination that wins overall but loses in one half is probably luck, not edge.")
    print(" * Testing 128 combinations means some will look good by chance alone. Only trust a")
    print("   result that wins in both halves, has plenty of trades, and beats the others clearly.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", type=int, metavar="DAYS",
                    help="download this many days of 1-minute NIFTY history from Kotak first")
    ap.add_argument("--download-daily", type=int, metavar="YEARS",
                    help="download this many YEARS of daily NIFTY candles. "
                         "Kotak only serves 5 years and rate-limits, so this goes in chunks")
    ap.add_argument("--pause", type=float, default=2.0,
                    help="seconds between download chunks (raise it if Kotak rate-limits)")
    ap.add_argument("--iv", type=float, default=14.0, help="assumed option volatility %% (default 14)")
    ap.add_argument("--slippage", type=float, default=1.0, help="rupees lost per entry and per exit (default 1)")
    ap.add_argument("--lot-size", type=int, default=65)
    ap.add_argument("--capital", type=float, default=None,
                    help="overrides the capital in settings.json (used for the daily loss limit)")
    ap.add_argument("--loss-pct", type=float, default=None,
                    help="overrides the daily loss limit %% in settings.json")
    ap.add_argument("--compare", action="store_true", help="run several rule variants and compare them")
    ap.add_argument("--sweep", action="store_true",
                    help="test many combinations of stop-loss, entry time, buffer, target, "
                         "trailing and reverse mode, then rank them")
    ap.add_argument("--top", type=int, default=15, help="how many sweep results to show (default 15)")
    ap.add_argument("--time-model", choices=("calendar", "trading"), default="calendar",
                    help="how option prices decay: calendar = also overnight/weekends (default), "
                         "trading = only during market hours")
    args = ap.parse_args()

    settings = Settings.load()
    if args.capital:
        # the strategy asks settings for the capital, so override it there too
        settings = replace(settings, capital_source="manual", manual_capital=args.capital)
    if args.loss_pct:
        settings = replace(settings, daily_loss_pct=args.loss_pct)
    capital = args.capital or (settings.manual_capital if settings.capital_source == "manual" else 300000.0)
    print(f"Capital for the limits: ₹{capital:,.0f}, daily loss limit "
          f"{settings.daily_loss_pct:g}% = ₹{capital * settings.daily_loss_pct / 100:,.0f}")

    if args.download_daily:
        from trader.backtest.history import download_daily
        from trader.brokers.kotak_neo import KotakNeoBroker
        secrets = Secrets.from_env()
        broker = KotakNeoBroker(secrets)
        broker.login(None if secrets.kotak_totp_secret else input("6-digit code: ").strip())
        print(f"Downloading {args.download_daily} years of daily NIFTY candles...")
        path = download_daily(broker, args.download_daily, pause=args.pause)
        print(f"Saved to {path}")
        if not args.download:
            return

    if args.download:
        from trader.brokers.kotak_neo import KotakNeoBroker
        secrets = Secrets.from_env()
        broker = KotakNeoBroker(secrets)
        code = None
        if not secrets.kotak_totp_secret:
            code = input("6-digit code from your authenticator app: ").strip()
        broker.login(code)
        print(f"Downloading {args.download} days of 1-minute NIFTY history...")
        download(broker, args.download)
        print("Downloading India VIX history...")
        download_vix(broker, args.download)

    bars = load_bars()
    if not bars:
        print(f"No history found at {csv_path()}.\nRun:  python backtest.py --download 180")
        sys.exit(1)
    print(f"History: {len(bars):,} minutes, {bars[0].time.date()} to {bars[-1].time.date()}")

    vix = load_vix()
    if vix:
        print(f"India VIX: {len(vix)} days (each day's option prices use that day's VIX)")
    else:
        print(f"India VIX: none saved, option prices use a fixed {args.iv:.0f}% volatility")

    costs = CostModel()
    common = dict(costs=costs, iv=args.iv / 100.0, slippage=args.slippage,
                  lot_size=args.lot_size, capital=capital, vix_by_day=vix, time_model=args.time_model)

    if args.sweep:
        run_sweep(bars, settings, common, args.top)
        return

    if not args.compare:
        result = run_backtest(bars, settings, **common)
        summary = result.summary()
        print_report(result, summary, "YOUR CURRENT SETTINGS")
        path = save_trades(result, "current")
        if path:
            print(f"\nEvery trade saved to {path}")
        print("\nRemember: option premiums are estimated, so treat the rupee amounts as indicative.")
        return

    variants = [
        ("Fixed 20-point stop-loss, no trailing",
         replace(settings, trail_enabled=False)),
        ("Trailing: red 1-min candle low",
         replace(settings, trail_enabled=True, trail_mode="candle_low", trail_candle_minutes=1)),
        ("Trailing: red 5-min candle low",
         replace(settings, trail_enabled=True, trail_mode="candle_low", trail_candle_minutes=5)),
        ("Trailing: 20 points below the high",
         replace(settings, trail_enabled=True, trail_mode="points", trail_distance=20.0)),
        ("Fixed stop + break must cross by 15 pts",
         replace(settings, trail_enabled=False, breakout_buffer_points=15.0)),
        ("Fixed stop + only 2 trades a day",
         replace(settings, trail_enabled=False, max_trades_per_day=2)),
    ]
    rows = []
    for name, variant in variants:
        result = run_backtest(bars, variant, label=name, **common)
        s = result.summary()
        rows.append((name, s))
        print_report(result, s, name)

    print("\n" + "=" * 78)
    print("COMPARISON (net P&L after all charges)")
    print("=" * 78)
    print(f"{'Variant':<42}{'Trades':>8}{'Win%':>7}{'Net':>12}{'Worst DD':>11}")
    for name, s in rows:
        print(f"{name:<42}{s['trades']:>8}{s['win_rate']:>6.0f}%{rupees(s['net']):>12}"
              f"{rupees(s['max_drawdown']):>11}")
    print("\nPicking the best line here is called curve fitting: with few trades, the winner is\n"
          "often luck. Prefer a variant that wins across different periods and by a clear margin.")


if __name__ == "__main__":
    main()
