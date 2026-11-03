# NIFTY algo trader

Automatic intraday trading software for NIFTY index options: two strategies, a
dashboard, a backtester, safety limits, Telegram alerts, and an autopilot that runs
the trading day unattended. 233 tests.

**Kotak Neo is the only broker implemented.** If you use another one you write a
single adapter file — see **BROKERS.md**. Nothing else in the app changes.

## Read this before anything else

**Neither strategy in here is a money-maker.** Both were tested hard on years of
NIFTY data. One has no edge at all and is kept only as a worked example; the other
has a small measurable edge that is roughly the same size as the cost of collecting
it — in other words, about break-even, with most of the profit coming from a handful
of big days.

Nothing in this repository should be funded on the strength of the rules it ships
with. What it is good for is running *your* idea and finding out honestly whether it
works: `backtest.py` replays any strategy against real history with real charges,
and the backtest runs the same strategy files that trade live, so the two cannot
drift apart.

Paper trade everything. `LIVE_TRADING` stays `false` until months of evidence say
otherwise.

One more thing that decides whether this is usable for you: **one NIFTY lot is 65
units**, and the `narrow_orb` stop risks about **₹2,925 per trade**. Normal position
sizing is 1% of capital per trade, so sizing this properly needs roughly **₹3 lakh**.
Below that, every risk rule in here collides with the lot size — the app will refuse
trades, correctly, and tell you why.

## Getting started (no broker account needed)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill it in later; Demo mode needs nothing
streamlit run dashboard.py
```

Pick **Demo** mode. It generates synthetic prices with a fast clock, so you can
watch a whole trading day in a couple of minutes without an account or any risk.

When you want real prices, fill in `.env`, then use **Paper** mode: real market data,
simulated orders, nothing reaching the exchange. `LIVE_TRADING` stays `false` in
`.env` until months of paper evidence say otherwise — and while it is false, no order
can reach the exchange however the dashboard is set.

```bash
python3 -m unittest discover -s tests   # 233 tests, ~0.5s
python3 status.py                        # is it working? one command
```

## The strategy: yesterday's candle breakout (buys options)

1. At 09:16 it reads **yesterday's NIFTY daily candle**.
   - **Red day** (close below open): today it only **buys the ATM PE**, when NIFTY breaks **below yesterday's low**.
   - **Green day** (close above open): today it only **buys the ATM CE**, when NIFTY breaks **above yesterday's high**.
   - **Doji day** (open equals close): no trade.
2. New trades only between **09:31 and 15:02**.
3. **Stop-loss: 20 points** below the price it bought the option at. A stop-loss order is placed at Kotak right after buying.
   Optional **trailing stop-loss** (off by default, switch it on in Settings). It only ever moves up. Three modes:
   - **Red candle low** (default mode): on the option's own 1-minute chart, each time a red candle completes,
     the stop-loss moves up to that candle's low. If the price is already below that low, the trade exits at once.
   - **Points / Percent**: the stop-loss stays a fixed distance below the highest price reached.
   This is how the software books profit before 15:14 — the trade closes when the price falls back to the trailing stop.
   Optional **minimum profit** (₹, off by default): until a trade is that much up, the first stop-loss stays
   where it is. Once it is, the stop-loss jumps to lock that profit, and trailing carries on above it. So a trade
   ends as a loss at the first stop-loss or with at least that profit (15:14 and big price jumps aside).
4. **Re-entry after a stop-loss**, two choices in Settings:
   - **NIFTY breaks the level again** (default): NIFTY must come back inside yesterday's level and break it again.
   - **Green candle on the same option**: after a stop-loss or trailing stop-loss exit, it keeps watching the
     *same* option (same strike all day). Once a green candle forms on that option's own chart, it buys when the
     price goes above that candle's high. Yesterday's candle then only decides CE or PE.

   Only one trade is open at a time. **Maximum trades a day** is a setting (default 5).
5. Any open trade is **sold at 15:14**.
6. **Daily loss limit: 1% of capital.** Before every trade it checks whether a stop-loss on that trade would take today's loss past the limit. If so, it doesn't take the trade.
7. **Profit target: 6% of capital per trade.** When one trade's profit reaches 6% of your capital, it is sold and there are no more trades that day. You can set anything from 0 (off) upwards in Settings, for example 10%.

All of these can be changed in the dashboard's Settings tab, along with a few extras:
**reverse (fade) mode** (buys the opposite option when the level breaks), a **strike offset**
(OTM/ITM instead of ATM), a **% stop-loss** instead of points, a **profit target**, and a
**breakout buffer** (NIFTY must cross the level by this many points).

> **"Break" means a fresh cross.** NIFTY must be on the inside of the level
> and then cross it. If NIFTY has already gapped past the level when entries
> open at 09:31, the app waits for it to come back and break again. You can
> change this in Settings.

## Strategy 2: narrow day + opening range (paper only)

Pick it in **Settings → Strategy** with the engine stopped.

1. **Only after a narrow day**: yesterday's range must be in the quietest X% of the last 20 days
   (`orb_narrow_pct`, default 30).
2. **Opening range**: the high and low of the first 15 minutes (09:15 to 09:30).
3. **Entry**: the first break of that range, until 14:00. Above it buys a CE, below it buys a PE.
   One trade a day.
4. **Stop and target are in INDEX points**, not premium: stop 30 points, target 150 points (5R).
   Everything is closed at 15:14. A wider backup stop-loss order still sits at the broker in case
   the app stops.
5. **Deep in-the-money option** (6 strikes by default). It keeps far more of a big move and loses
   much less time value than an ATM option.

**What to expect.** Tested over two years of option trades with charges and spread, this is
**around break-even** — a small real edge, about the same size as the cost of collecting it, with
most of the profit coming from a handful of big days. It is also very sensitive to how deep in the
money you buy: a few strikes either way flips it between modestly positive and clearly negative.
Run `backtest.py` yourself before trusting any of it. Paper trade it. Don't fund it.

## Read this first

- **This code has not been run against a real Kotak account yet.** It follows
  Kotak's official SDK documentation (version 3), and all the strategy rules
  are covered by automated tests. Still, run `check_kotak.py` and use **Paper
  mode for at least 2 to 3 weeks** before going live.
- **Know your risk per trade.** 1 lot × lot size × 20 points is what one
  stop-loss costs, before charges and slippage. For example, with a lot size
  of 65, one stop-loss is about ₹1,300, and 5 stop-losses in a day is about
  ₹6,500. If your 1% limit is smaller than one stop-loss, the app will never
  trade, and it tells you why on the dashboard. With a 1% limit you need at
  least ₹1,30,000 of capital for one 20-point stop-loss on 1 lot.
- **Know how big the profit target is.** 6% of ₹3,00,000 is ₹18,000. On 1 lot
  of 65 that needs the option to rise about 277 points in one trade, which only
  happens on big trend days. Most trades will end at the stop-loss, the trailing
  stop or 15:14 before they reach it.
- P&L in the app is **before brokerage, STT, taxes and charges**. Those add up
  quickly with many trades.
- Your laptop must stay **on, awake and connected** from 09:15 to 15:15. Turn
  off sleep mode on trading days.

---

## Step 1: Install (one time)

1. Install **Python 3.11 or newer** from https://www.python.org/downloads/
   (on Windows, tick **"Add Python to PATH"** during install).
2. Unzip this folder somewhere easy, e.g. `Documents\algo_trader`.
3. Open **Command Prompt** (Windows) or **Terminal** (Mac) and run:

```bash
cd Documents\algo_trader          # Mac: cd ~/Documents/algo_trader
python -m venv venv
venv\Scripts\activate             # Mac: source venv/bin/activate
pip install -r requirements.txt
```

You'll know it worked when you see `(venv)` at the start of the line.

## Step 2: Try Demo mode (no Kotak account needed)

```bash
streamlit run dashboard.py
```

A browser tab opens. Leave the mode on **Demo** and press **Start**. You'll
see a fake trading day play out fast: the plan, entries, stop-losses,
re-entries, the loss limit and the time exit. Prices are random. This is just
to learn how the dashboard behaves.

To stop the app, press `Ctrl + C` in the Command Prompt window.

## Step 3: Set up Kotak Neo

1. In Kotak Neo (app or web): **More → Trade API → Generate application**. Copy the consumer key.
2. On the same Trade API page, set up **TOTP Registration** with Google Authenticator or a similar app.
3. **Static IP.** Under SEBI's rules, API orders are only accepted from a static IP registered with your broker.
   - Ask your Wi-Fi provider for a **static IP add-on**.
   - Register that IP on Kotak's Trade API page.
   - Mobile data won't work for placing orders.
4. Copy `.env.example` to a new file named `.env` and fill in your details.
   Open it with Notepad. **Never share this file.**

## Step 4: Check the connection (read-only)

```bash
python check_kotak.py
```

This logs in and fetches data without placing any orders:

- your public IP
- NIFTY's price
- yesterday's candle
- the ATM CE and PE with lot size and expiry
- your funds and positions

Compare yesterday's candle with your chart. If any step says FAILED, share
that output with personal details hidden, and the Kotak adapter can be
adjusted.

## Step 5: Paper mode (real prices, simulated orders)

Every trading day:

1. Before 09:15, start the dashboard. On Windows you can just double-click `start_dashboard.bat`; otherwise run `streamlit run dashboard.py`.
2. Choose **Paper**, enter your authenticator code, and press **Log in to Kotak Neo**.
3. Press **Start**.
4. Check the plan box at 09:16 matches your chart.
5. Keep the laptop on until after 15:15. Review the **Trades** tab in the evening.

Kotak ends API sessions every day, so you log in every morning.

## Step 6: Going live (only after weeks of paper trading)

1. In `.env`, set `LIVE_TRADING=true`, then restart the app.
2. Choose **Live**, log in, and press **Start**. Start with **1 lot**.
3. Also check Kotak's intraday (MIS) auto square-off time is after your 15:14 exit.

The red **Exit everything and stop for today** button closes any open trade and
blocks new trades until the next day.

## Telegram alerts (optional)

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the `chat` → `id` number.
4. Put both in `.env` and restart the app. Use **Alerts → Send a test alert** to check.

You'll get alerts for the daily plan, every buy and sell, stop-losses, the
loss limit, connection problems, and exits that didn't complete. When the
autopilot runs the day, you also get its morning line and the 15:20 report,
because nobody is looking at the dashboard then.

To set it up without editing `.env` by hand:

```bash
python3 setup_telegram.py
```

It asks for the token, finds your chat id for you, keeps your Kotak settings exactly
as they are (with a backup at `.env.backup`), and sends a test message. The token is
typed invisibly and never printed, so the output is safe to share.

To check it is wired up before you rely on it:

```bash
python3 autopilot.py --dry-run     # last line says Telegram: on or off
```

If it says `off`, the token or chat id is missing from `.env` and the autopilot
will trade all day in silence.

---

## What happens when things go wrong

| Situation | What the app does |
|---|---|
| Internet drops | After 15 seconds it pauses new trades and alerts you. The stop-loss order already at Kotak keeps protecting an open trade. |
| Premium gaps through the stop-loss | If Kotak's stop-loss order doesn't fill within 3 seconds, the app exits with a limit order itself. |
| Entry order doesn't fill in 10 seconds | Cancels it, skips, and waits for a fresh breakout. |
| Entry order fails with an error | Pauses and asks you to check the Kotak app, then press **Resume**. |
| App or laptop restarts mid-day | Remembers today's trade count, P&L and open trade, then checks with Kotak that the trade is still open. |
| Exit can't complete | Keeps retrying and sends a CRITICAL alert. It never sells more than Kotak shows as open. |
| Yesterday's candle can't be fetched | Tries Yahoo Finance as a backup. If that fails too, type the candle in under **Settings → Yesterday's candle**. |

## Project files

```
dashboard.py              the screen you use
start_dashboard.bat       double-click to open the dashboard (Windows)
check_kotak.py            read-only connection test
settings.json             your strategy settings (edited from the dashboard)
.env                      your secrets (you create this, never share it)
trader/
  strategy.py             strategy 1: yesterday's candle breakout
  strategy_orb.py         strategy 2: narrow day + opening range (index-point stop and target)
  risk.py                 loss limit, trade limits, kill switch
  engine.py               runs everything, detects connection problems
  alerts.py               dashboard + Telegram alerts
  journal.py              saves progress and trade history in data/
  config.py               settings and .env loading
  brokers/
    base.py               the common interface every broker follows
    kotak_neo.py          Kotak Neo adapter (the only Kotak-specific file)
    paper.py              simulated orders for Demo and Paper modes
    demo_market.py        fake prices for Demo mode
backtest.py               test the strategy on past data
report.py                 weekly results, with charges, against what was expected
autopilot.py              runs the whole trading day by itself
start_autopilot.command   double-click launcher that also keeps the Mac awake
install_autopilot.command double-click once: runs every weekday by itself
uninstall_autopilot.command  stops the automatic daily run
BROKERS.md                how to add a broker other than Kotak
trader/backtest/          history download, option pricing, charges, replay engine
tests/                    automated tests of the strategy, adapter and backtester
data/trades.csv           every closed trade
```

## Adding another broker

Kotak Neo is the only adapter written. Adding yours is one file plus one line:

1. Write `trader/brokers/<yours>.py` implementing `trader/brokers/base.py`
2. Add it to the `BROKERS` registry in `trader/engine.py`
3. Set `"broker": "<yours>"` in `settings.json`

The strategies, risk limits, journal, autopilot and dashboard stay exactly the same —
they only ever talk to the `Broker` interface. **`BROKERS.md`** lists every method,
which are required and which are optional, what you lose by skipping each, and how to
test an adapter with no account and no network.

## Is it working? (`status.py`)

One command, plain English. Run it whenever you want to know, and nothing else:

```bash
python3 status.py
```

It tells you whether the schedule is installed, whether it ran today, how long
login took, whether the engine started, what the strategy decided, how many of the
last trading days it actually ran on, whether real money is armed, and whether your
daily history is current. It reads files only — it never logs in or places an order,
so it is safe to run mid-trade.

The line that matters most is the first one: `WORKING`, `WAITING`, `RESTING`, or
`NEEDS ATTENTION` with the reason. The most common real problem it catches is the
quiet one: the Mac was asleep at 08:45, so the bot never ran and nothing complained.

---

## Autopilot (running the day without the dashboard)

```bash
python3 autopilot.py --dry-run     # show today's plan
python3 autopilot.py               # run it; leave the window open
```

Or double-click **start_autopilot.command** in Finder, which also keeps the Mac
awake while it runs.

Every weekday it logs in at 08:50 with the TOTP secret from `.env`, refreshes the
daily candle history, runs the morning check, starts the engine at 09:15, stops it
at 15:20, downloads the day's 1-minute candles, and sends the day's report to
Telegram. On Fridays it sends the week's report too.

It does **not** choose the strategy for you, and it cannot trade live unless
`LIVE_TRADING=true` is in `.env`.

### Hands-off: double-click once, never type again

Double-click **install_autopilot.command**. It schedules the autopilot with macOS
(a LaunchAgent) to start by itself at **08:45 every weekday**, run the day in Paper
mode and report at 15:20. No Terminal, no commands, nothing to remember. The log
lands in `data/autopilot.log`.

The Mac has to be awake. To wake it automatically on weekdays, run this once:

```bash
sudo pmset repeat wakeorpoweron MTWRF 08:40:00
```

To stop everything, double-click **uninstall_autopilot.command**.

## Weekly report

```bash
python3 report.py              # the last 7 days
python3 report.py --days 30
python3 report.py --telegram   # send it to your phone as well
```

It reads `data/trades.csv`, adds the charges the dashboard leaves out, shows the
day-by-day result, how trades ended, and compares the average trade with what the
backtest said to expect. Demo trades are ignored.

The app also runs a **morning check** the first time it ticks each day in Paper or
Live mode: NIFTY's price, yesterday's candle, your capital, the expiry, and (for
the narrow-day strategy) the daily history and minute candles. It posts one alert
listing what came back, so a missing piece of data can't quietly cancel your day.

## Backtesting

Test the exact same rules on past NIFTY data before risking money.

First time (downloads history, needs a Kotak login):

```bash
python backtest.py --download 180
```

It also downloads daily **India VIX**, so each day's option prices use that
day's VIX instead of one fixed volatility.

Then, offline:

```bash
python backtest.py               # your current settings from settings.json
python backtest.py --compare     # rule variants side by side
python backtest.py --sweep       # 128 combinations, with a both-halves check
python backtest.py --iv 16 --slippage 2
python backtest.py --time-model trading
```

`--time-model trading` makes option prices lose value only during market hours
(calendar, the default, spreads it over nights and weekends too). Real weekly
options behave somewhere in between. Intraday option BUYING looks better under
the calendar model, so always re-run a result you like with `--time-model trading`.

`--sweep` tests stop-loss size and type, entry time, breakout buffer, profit target,
trailing and reverse (fade) mode against each other. It splits the period in half and
shows the result for each half, because a setting that only works in one half is
usually luck. The full table is saved to `data/backtest_sweep.csv`.

The report shows how often a breakout happens, win rate, average win and loss,
gross and net P&L after charges, the worst day and the worst drawdown. Every
trade is written to `data/backtest_current.csv`.

**What it is honest about:**

- NIFTY prices are real, from Kotak's 1-minute history (Kotak allows 30 days per
  request, so it downloads in chunks and caches them in `data/history/`).
- Option premiums are **calculated** with Black-Scholes, because no broker keeps
  price history for expired option contracts. Entry and exit timing is realistic;
  the rupee amounts are an estimate. Volatility is the day's opening India VIX when
  downloaded, otherwise `--iv`. It stays fixed during the day and is the same for
  every strike, so sudden crashes (when real volatility jumps) look milder than they
  were.
- NIFTY weekly expiry is Thursday before September 2025 and Tuesday after, as on NSE.
  Holiday shifts are not modelled.
- Charges (brokerage, STT, exchange, SEBI, stamp duty, GST) and slippage are
  included. Rates are those of September 2026 and can be edited in
  `trader/backtest/costs.py`.
- The backtest runs the same strategy files that trade live, so the rules cannot
  drift apart from what you paper trade.

## Running the tests

```bash
python -m unittest discover -s tests -v
```

These check every rule on scripted prices, including:

- a red day buys PE and a green day buys CE
- no trades before 09:31 or after 15:02
- the stop-loss, including when the premium gaps through it
- re-entry and the 5-trade maximum
- the 1% loss limit and the profit target (% of capital)
- green-candle re-entry on the same option, one trade at a time
- the minimum profit lock
- the 15:14 exit
- the kill switch
- a restart in the middle of the day
