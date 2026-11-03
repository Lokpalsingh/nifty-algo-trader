# Using a broker other than Kotak Neo

Only **Kotak Neo** is implemented. If you trade through someone else, you write one
file. Nothing else in the app changes — the strategies, risk manager, journal,
dashboard and backtester only ever talk to the `Broker` interface, never to a
specific broker.

Before you start: the whole app runs in **Demo mode** with no broker at all. Try it
first (`streamlit run dashboard.py`, mode Demo) to see whether the thing is worth
wiring to your account.

---

## The three steps

### 1. Write the adapter

Copy `trader/brokers/kotak_neo.py` as a starting point and make
`trader/brokers/<yours>.py`:

```python
from trader.brokers.base import Broker, BrokerError

class MyBroker(Broker):
    name = "My Broker"
    is_simulated = False

    def __init__(self, secrets, product: str = "MIS"):
        self.secrets = secrets
        self.product = product
```

Your class must accept `(secrets, product=...)`, because that is how the engine
builds it.

### 2. Register it

In `trader/engine.py`:

```python
from trader.brokers.mybroker import MyBroker

BROKERS = {"kotak": KotakNeoBroker, "mybroker": MyBroker}
```

### 3. Point settings at it

In `settings.json`:

```json
"broker": "mybroker"
```

That's the whole wiring. An unknown name fails at startup with a message listing
the ones it knows, rather than at 09:15 with money on the line.

---

## What you must implement

These are abstract — the app will not start without them.

| Method | Returns | Notes |
|---|---|---|
| `login(totp_code=None)` | – | Raise `BrokerError` on failure |
| `is_logged_in()` | `bool` | |
| `get_index_ltp(index)` | `float` | Last traded price of NIFTY itself |
| `get_option_ltp(contract)` | `float` | |
| `get_previous_day_candle(index, today)` | `Candle` or `None` | Must **exclude** today |
| `find_atm_option(index, spot, option_type, today, expiry_choice)` | `OptionContract` | |
| `place_order(contract, side, qty, order_type, price, trigger_price, tag)` | order id `str` | `side` BUY/SELL, `order_type` LIMIT/SL/MARKET |
| `cancel_order(order_id)` | – | |
| `get_order_status(order_id)` | `OrderStatus` | |
| `get_available_capital()` | `float` or `None` | |
| `get_open_quantity(contract)` | `int` | **Units, not lots** |

## What is optional

Each has a working default, so skip any your broker can't do. The app degrades
rather than breaking.

| Method | Default if you skip it | What you lose |
|---|---|---|
| `get_daily_candles(index, days)` | returns `[]` | The `narrow_orb` strategy falls back to saved files, then skips the day |
| `get_minute_candles(index, since_hhmm)` | raises | The opening range is built from live ticks instead — less exact |
| `get_expiry(index, today, expiry_choice)` | raises | Expiry shown as unknown on the dashboard |
| `get_option(index, strike, option_type, ...)` | raises | Deep in-the-money strikes unavailable, so `narrow_orb` can't pick its strike |
| `get_option_ltps(contracts)` | one call per contract | Just slower |
| `get_index_open(index)` | `None` | A minor dashboard field |
| `get_vix()` | `None` | Backtests use a fixed volatility instead |
| `get_open_option_positions(index)` | `[]` | The app can't notice a position it left open before a restart |
| `modify_order(...)` | raises | Trailing stops cancel and re-place instead of modifying |

For `narrow_orb` specifically you want `get_daily_candles` and `get_option`. Without
them it will run, but it will skip most days.

---

## Getting it right

**Read `trader/models.py` first.** `Candle`, `OptionContract` and `OrderStatus` are
small dataclasses and they are the whole contract between your adapter and the app.
Two details that bite:

- `get_open_quantity` is in **units** (one NIFTY lot is 65), not lots.
- `get_previous_day_candle` must never return today's partial candle. The Kotak
  adapter filters on `day < today.isoformat()`; do the same.

**Test it without touching the market.** `tests/test_kotak_adapter.py` fakes the
entire broker SDK with a dictionary — no network, no account, no orders. Copy it as
`tests/test_mybroker.py` and change the fake replies to your broker's shapes. There
are 30-odd cases in there covering the ugly parts: malformed option chains, errors,
partial fills, prices for the wrong instrument.

**Then Paper mode.** It uses your broker's real prices and simulates the orders, so
you find out whether your adapter reads the market correctly before it can place
anything. `LIVE_TRADING` stays `false` in `.env` until you are sure.

---

## A warning worth more than the code

The software here is reliable and well tested. **The strategies it ships with are
not a money-maker** — both were tested hard and came out somewhere between "no edge"
and "about break-even after costs". Do not point this at real money on the strength
of the rules in the box.

What it is actually good for is running *your* idea honestly: `backtest.py` replays a
strategy over real history with real brokerage, STT, exchange fees and slippage, and
it runs the same strategy files that trade live, so the backtest and the live rules
cannot drift apart. Build your own expectation with it first, then paper trade, then
decide.
