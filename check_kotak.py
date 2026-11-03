"""
Step-by-step Kotak Neo connection check. READ-ONLY: it never places orders.

Run it once after filling in your .env file:
    python check_kotak.py

Each step prints OK or FAILED, plus Kotak's raw reply so you (or I) can
spot any difference from what the app expects.
"""
import json
import sys
from datetime import date, timedelta

from trader.brokers.base import BrokerError
from trader.brokers.kotak_neo import KotakNeoBroker
from trader.config import INDEXES, Secrets
from trader.models import OptionContract

SHOW_RAW = "--quiet" not in sys.argv


def show(title, value):
    if not SHOW_RAW:
        return
    text = json.dumps(value, indent=2, default=str) if not isinstance(value, str) else value
    if len(text) > 2500:
        text = text[:2500] + "\n... (cut)"
    print(f"   raw {title}:\n" + "\n".join("     " + line for line in text.splitlines()))


def step(n, title, fn):
    print(f"\n[{n}] {title}")
    try:
        result = fn()
        print("   OK")
        return result
    except Exception as e:
        print(f"   FAILED: {e}")
        return None


def main():
    secrets = Secrets.from_env()
    missing = secrets.missing_kotak_fields()
    if missing:
        print("Fill these in your .env file first:", ", ".join(missing))
        sys.exit(1)

    broker = KotakNeoBroker(secrets)
    api = broker._client()
    info = INDEXES["NIFTY"]
    today = date.today()

    code = None
    if not secrets.kotak_totp_secret:
        code = input("\nEnter the 6-digit code from your authenticator app: ").strip()
    if step(1, "Login (TOTP + MPIN)", lambda: broker.login(code)) is None and not broker.is_logged_in():
        print("\nLogin failed. Check .env values, the TOTP code, and that your static IP is registered.")
        sys.exit(1)

    if hasattr(api, "whatsmyip"):
        step(2, "Your public IP (for live trading this must be the static IP registered with Kotak)",
             lambda: show("whatsmyip", api.whatsmyip()))

    def nifty_quote():
        raw = api.quotes(instrument_tokens=[{"instrument_token": info["quote_token"],
                                             "exchange_segment": info["quote_segment"]}], quote_type="ltp")
        show("quotes", raw)
        ltp = broker.get_index_ltp("NIFTY")
        print(f"   NIFTY LTP = {ltp:,.2f}")
        return ltp

    spot = step(3, "NIFTY live price", nifty_quote)

    def candle():
        raw = api.historical_data(neosymbol=f"{info['quote_segment']}|{info['quote_token']}", interval="D",
                                  from_date=(today - timedelta(days=10)).isoformat(), to_date=today.isoformat())
        show("historical_data", raw)
        c = broker.get_previous_day_candle("NIFTY", today)
        if c is None:
            raise BrokerError("No candle from Kotak or backup. You'll need to type it in the dashboard.")
        if c.source != "Kotak":
            print(f"   Note: Kotak candle failed ({broker.last_candle_error}); used {c.source}")
        print(f"   Yesterday {c.date}: O {c.open} H {c.high} L {c.low} C {c.close} -> {c.color}")
        print("   Compare these numbers with your chart!")
        return c

    step(4, "Yesterday's NIFTY daily candle", candle)

    if spot:
        def atm():
            exp = api.expiries(exchange=info["fo_segment"], underlying=info["underlying"])
            expiry_list = exp.get("expiries") if isinstance(exp, dict) else None
            print(f"   Nearest expiries: {expiry_list[:3] if expiry_list else exp}")
            step_size = info["strike_step"]
            print(f"   NIFTY {spot:,.2f} -> ATM strike should be {round(spot / step_size) * step_size}")
            chain = api.option_chain(exchange=info["fo_segment"], underlying=info["underlying"],
                                     expiry=expiry_list[0] if expiry_list else None,
                                     instrument_type="option", count=40)
            for opt in ("CE", "PE"):
                c = broker.find_atm_option("NIFTY", spot, opt, today, "nearest")
                raw_quote = api.quotes(instrument_tokens=[{"instrument_token": c.token,
                                                           "exchange_segment": c.segment}], quote_type="ltp")
                show(f"quote {opt}", raw_quote)
                ltp = broker.get_option_ltp(c)
                chain_ltp = None
                rows = chain.get("call" if opt == "CE" else "put", []) if isinstance(chain, dict) else []
                for r in rows:
                    if (r.get("inst") or {}).get("symbol") == c.symbol:
                        chain_ltp = (r.get("quote") or {}).get("ltp")
                print(f"   ATM {opt}: {c.symbol} strike {c.strike:.0f} expiry {c.expiry} lot {c.lot_size} "
                      f"token {c.token}")
                print(f"      price from quotes: {ltp}   price from option chain: {chain_ltp}")
        step(5, "Find ATM options (expiry, strike, lot size, price)", atm)

    def capital():
        raw = api.limits()
        show("limits", {k: raw.get(k) for k in ("Net", "MarginUsed", "CollateralValue", "stat")} if isinstance(raw, dict) else raw)
        cap = broker.get_available_capital()
        if cap is None:
            print("   Kotak shows no usable funds. For Paper mode, set 'Capital for the loss limit'")
            print("   to 'Amount I type in' in the dashboard Settings. Live trading needs funds added.")
        else:
            print(f"   Available funds (Net) = ₹{cap:,.2f}")
        return cap

    step(6, "Available capital", capital)
    def positions():
        show("positions", api.positions())
        c = OptionContract("check", "0", "nse_fo", 0, "CE", "", 1)
        broker.get_open_quantity(c)
        print("   Positions read correctly (no open positions is fine)")

    step(7, "Positions", positions)

    print("\nDone. No orders were placed. If any step FAILED, copy the output (hide personal details) "
          "and share it so the adapter can be adjusted.")


if __name__ == "__main__":
    main()
