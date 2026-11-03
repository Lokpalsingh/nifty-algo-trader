"""
Dashboard. Start it with:

    streamlit run dashboard.py

Then open the link it prints (usually http://localhost:8501).
"""
import inspect
import logging
from datetime import date

import pandas as pd
import streamlit as st

from trader.config import SETTINGS_FILE, Settings
from trader.engine import Engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

st.set_page_config(page_title="NIFTY algo trader", page_icon="📈", layout="wide")

# Full-width buttons on both newer and older Streamlit versions
WIDE = ({"width": "stretch"} if "width" in inspect.signature(st.button).parameters
        else {"use_container_width": True})

st.markdown("""
<style>
.mode-banner {padding: 0.7rem 1rem; border-radius: 8px; font-weight: 600; margin-bottom: 0.8rem;}
.mode-demo  {background: #e7f0fb; color: #164a7a; border-left: 6px solid #2f78c4;}
.mode-paper {background: #fdf3dc; color: #6b4a00; border-left: 6px solid #d99a00;}
.mode-live  {background: #fde4e1; color: #8a1c10; border-left: 6px solid #d93a26;}
.plan-box {padding: 0.9rem 1rem; border-radius: 8px; border: 1px solid rgba(128,128,128,0.35);}
.candle-red {color: #d93a26; font-weight: 700;}
.candle-green {color: #1c8a4a; font-weight: 700;}
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_engine() -> Engine:
    return Engine()


engine = get_engine()

MODE_LABELS = {
    "demo": "Demo: fake prices, fake orders",
    "paper": "Paper: real Kotak prices, simulated orders",
    "live": "Live: real orders with real money",
}


def rupees(value, sign=False):
    if value is None:
        return "–"
    return f"₹{value:+,.0f}" if sign else f"₹{value:,.0f}"


# ====================================================================== #
# Sidebar: mode, login, start/stop, kill switch
# ====================================================================== #
with st.sidebar:
    snap = engine.snapshot()
    st.header("Controls")

    st.subheader("1. Mode")
    modes = ["demo", "paper", "live"] if snap["live_allowed"] else ["demo", "paper"]
    chosen = st.radio("Mode", modes, index=modes.index(engine.mode) if engine.mode in modes else 0,
                      format_func=lambda m: MODE_LABELS[m], disabled=snap["running"],
                      label_visibility="collapsed")
    if chosen != engine.mode and not snap["running"]:
        try:
            engine.set_mode(chosen)
            st.rerun()
        except Exception as e:
            st.error(str(e))
    if not snap["live_allowed"]:
        st.caption("Live mode is locked. It unlocks only when LIVE_TRADING=true is in your .env file.")
    if snap["running"]:
        st.caption("Stop the engine to change mode.")

    if engine.mode != "demo":
        st.subheader("2. Log in")
        if snap["needs_login"]:
            code = ""
            if not engine.secrets.kotak_totp_secret:
                code = st.text_input("6-digit code from your authenticator app", max_chars=6)
            if st.button("Log in to Kotak Neo", **WIDE):
                try:
                    engine.login(code or None)
                    st.success("Logged in")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        else:
            st.success("Logged in to Kotak Neo")

    st.subheader("3. Run" if engine.mode != "demo" else "2. Run")
    c1, c2 = st.columns(2)
    if c1.button("Start", type="primary", disabled=snap["running"] or snap["needs_login"],
                 **WIDE):
        try:
            engine.start()
            st.rerun()
        except Exception as e:
            st.error(str(e))
    if c2.button("Stop", disabled=not snap["running"], **WIDE):
        engine.stop()
        st.rerun()
    if engine.mode == "demo":
        st.caption("Demo runs a fast clock starting at 09:15, so a full day plays out in about 12 minutes.")

    st.divider()
    st.subheader("Emergency")
    if st.button("Exit everything and stop for today", type="primary", **WIDE,
                 help="Closes every open position now and blocks new trades until tomorrow."):
        try:
            engine.kill_switch()
            st.warning("Kill switch activated")
        except Exception as e:
            st.error(f"Kill switch error: {e}. Exit manually in the Kotak app.")
    if snap["risk"]["kill_switch"] or (snap["done_reason"] and "Kill switch" in snap["done_reason"]):
        st.caption("Trading is stopped for today by the kill switch.")
        if st.checkbox("I want to trade again today"):
            if st.button("Clear kill switch", **WIDE):
                problem = engine.reset_today()
                if problem:
                    st.error(problem)
                else:
                    st.success("Kill switch cleared")
                    st.rerun()

    if snap["paused_reason"]:
        st.error(snap["paused_reason"])
        if st.button("Resume new entries", **WIDE):
            engine.resume()
            st.rerun()


# ====================================================================== #
# Header
# ====================================================================== #
st.title("NIFTY algo trader")
st.markdown(f'<div class="mode-banner mode-{engine.mode}">{MODE_LABELS[engine.mode]}</div>',
            unsafe_allow_html=True)

tab_today, tab_settings, tab_trades, tab_alerts = st.tabs(["Today", "Settings", "Trades", "Alerts"])


# ====================================================================== #
# Today (refreshes every 2 seconds)
# ====================================================================== #
with tab_today:
    @st.fragment(run_every=2)
    def today_panel():
        s = engine.snapshot()
        running = "running" if s["running"] else "stopped"
        names = {"prev_day": "Yesterday's high/low breakout",
                 "narrow_orb": "Narrow day + opening range"}
        st.caption(f"Engine {running}  |  Market time {s['clock']}  |  Strategy: "
                   f"{names.get(engine.settings.strategy, engine.settings.strategy)}")

        if s["connection_lost"]:
            st.error(f"Connection problem: {s['last_error']}")
        elif s["setup_error"]:
            st.warning(s["setup_error"])
        elif s["done_reason"]:
            st.info(f"Done for today: {s['done_reason']}")
        elif s["running"]:
            st.info(s["status"])
        else:
            st.info("Press Start in the sidebar to begin.")

        plan = s["plan"]
        if plan:
            c = plan["candle"]
            color_class = "candle-red" if plan["color"] == "RED" else "candle-green"
            st.markdown(
                f'<div class="plan-box">Yesterday ({c["date"]}) was '
                f'<span class="{color_class}">{plan["color"].lower()}</span>: '
                f'open {c["open"]:,.2f}, high {c["high"]:,.2f}, low {c["low"]:,.2f}, close {c["close"]:,.2f}'
                f'<br><small>Source: {c["source"]}. Check these match your chart.</small><br><br>'
                f'<b>{plan["text"]}</b></div>',
                unsafe_allow_html=True)
            st.write("")

        orb = s.get("orb")
        if orb and orb.get("high"):
            st.caption(f"Opening range {orb['low']:,.2f} to {orb['high']:,.2f}  |  stop "
                       f"{orb['stop_points']:.0f} index points, target {orb['target_points']:.0f}"
                       + (f"  |  entered with NIFTY at {orb['entry_spot']:,.2f}" if orb.get("entry_spot") else ""))

        risk = s["risk"]
        total = (risk["realized"] or 0) + (risk["unrealized"] or 0)
        m1, m2, m3, m4 = st.columns(4)
        if s["spot"] and plan and plan.get("level"):
            m1.metric("NIFTY now", f"{s['spot']:,.2f}", f"{s['spot'] - plan['level']:+,.2f} from level",
                      delta_color="off")
        else:
            m1.metric("NIFTY now", f"{s['spot']:,.2f}" if s["spot"] else "–")
        m2.metric("Trades used", f"{s['trades_today']} of {s['max_trades']}")
        m3.metric("P&L today", rupees(total, sign=True))
        m4.metric("Daily loss limit", rupees(risk["loss_limit"]),
                  help=f"{engine.settings.daily_loss_pct}% of capital {rupees(risk['capital'])}")
        if risk["loss_limit"]:
            used = min(1.0, max(0.0, -total / risk["loss_limit"]))
            st.progress(used, text=f"Loss limit used: {used * 100:.0f}%")
        if risk.get("profit_target"):
            st.caption(f"Profit target: a trade is sold when it is up {rupees(risk['profit_target'])} "
                       f"({engine.settings.target_capital_pct:g}% of capital). No more trades after that today.")

        pos = s["position"]
        if pos:
            st.subheader(f"Open trade {pos['trade_no']}")
            p1, p2, p3, p4, p5 = st.columns(5)
            p1.metric("Option", pos["symbol"])
            p2.metric("Quantity", pos["qty"])
            p3.metric("Bought at", f"₹{pos['entry_price']:.2f}", pos["entry_time"], delta_color="off")
            p4.metric("Stop-loss", f"₹{pos['sl_trigger']:.2f}")
            if pos["ltp"]:
                pnl = (pos["ltp"] - pos["entry_price"]) * pos["qty"]
                p5.metric("Price now", f"₹{pos['ltp']:.2f}", rupees(pnl, sign=True))
            if pos.get("target_price"):
                st.caption(f"Profit target: sells if the option reaches ₹{pos['target_price']:.2f}")
            if pos.get("lock_price"):
                if pos["sl_trigger"] >= pos["lock_price"]:
                    st.caption(f"Minimum profit ₹{pos['min_profit']:,.0f} is locked.")
                else:
                    st.caption(f"Minimum profit: when the option reaches ₹{pos['lock_price']:.2f}, the stop-loss "
                               f"jumps there to lock ₹{pos['min_profit']:,.0f}.")
            if pos.get("trailing"):
                how = ("last red candle low" if pos.get("trail_mode") == "candle_low"
                       else f"high ₹{pos['high_price']:.2f}")
                extra = (f" Last red candle low: ₹{pos['last_red_low']:.2f}."
                         if pos.get("last_red_low") else " No red candle yet.")
                st.caption(f"Trailing stop-loss on ({how}).{extra} "
                           f"Locked in if the stop-loss hits now: {rupees(pos['locked_in'], sign=True)}")
            if not pos["sl_at_broker"]:
                st.warning("No stop-loss order at the broker. The app is watching the stop-loss itself, "
                           "keep it running.")
            if pos["exiting"]:
                st.error("Trying to exit this trade. If it doesn't close soon, exit it in the Kotak app.")

    today_panel()


# ====================================================================== #
# Settings
# ====================================================================== #
with tab_settings:
    cur = engine.settings
    st.caption(f"Saved in {SETTINGS_FILE.name}. Changes apply to new trades, not to a trade already open.")

    with st.form("settings"):
        st.subheader("Strategy")
        strat_labels = {"prev_day": "Yesterday's high/low breakout (your original)",
                        "narrow_orb": "Narrow day + 15-minute opening range (research, paper only)"}
        strategy = st.selectbox("Strategy", list(strat_labels), format_func=strat_labels.get,
                                index=list(strat_labels).index(cur.strategy),
                                help="Stop the engine before switching. The rules below that don't apply "
                                     "to the chosen strategy are ignored.")
        if strategy != cur.strategy and engine.is_running:
            st.warning("Switching strategy needs the engine stopped: press Stop, then Save settings, "
                       "then Start.")
        st.subheader("Trade")
        a1, a2, a3 = st.columns(3)
        lots = a1.number_input("Lots per trade", min_value=1, max_value=50, value=cur.lots, step=1)
        product = a2.selectbox("Product", ["MIS", "NRML"], index=["MIS", "NRML"].index(cur.product),
                               help="MIS = intraday. Your trades exit at the exit time anyway.")
        expiry_labels = {"nearest": "Nearest expiry", "next_on_expiry_day": "Next expiry on expiry day"}
        expiry = a3.selectbox("Expiry", list(expiry_labels), format_func=expiry_labels.get,
                              index=list(expiry_labels).index(cur.expiry_choice))

        st.subheader("Rules")
        b1, b2, b3, b4 = st.columns(4)
        setup_time = b1.text_input("Read yesterday's candle at", cur.setup_time)
        entry_start = b2.text_input("Entries start", cur.entry_start)
        entry_end = b3.text_input("Entries end", cur.entry_end)
        exit_time = b4.text_input("Exit all at", cur.exit_time)
        sig_labels = {"breakout": "Follow the break (normal)", "reverse": "Fade the break (reverse)"}
        q1, q2 = st.columns(2)
        signal_mode = q1.selectbox("Signal", list(sig_labels), format_func=sig_labels.get,
                                   index=list(sig_labels).index(cur.signal_mode))
        strike_offset = q2.number_input("Strike offset (0 = ATM, + = OTM, − = ITM)", min_value=-5,
                                        max_value=5, value=int(cur.strike_offset_steps), step=1)
        c0, c1, c2, c3 = st.columns(4)
        sl_labels = {"points": "Points (₹)", "percent": "Percent of buy price"}
        sl_mode = c0.selectbox("Stop-loss type", list(sl_labels), format_func=sl_labels.get,
                               index=list(sl_labels).index(cur.stop_loss_mode))
        sl_points = c1.number_input("Stop-loss (points on option price)", min_value=1.0,
                                    value=float(cur.stop_loss_points), step=1.0)
        sl_percent = c2.number_input("Stop-loss (%)", min_value=1.0, max_value=90.0,
                                     value=float(cur.stop_loss_percent), step=5.0)
        target_points = c3.number_input("Profit target (points, 0 = off)", min_value=0.0,
                                        value=float(cur.target_points), step=10.0)
        d0, d1, d2 = st.columns(3)
        target_pct = d0.number_input("Profit target per trade (% of capital, 0 = off)", min_value=0.0,
                                     max_value=100.0, value=float(cur.target_capital_pct), step=1.0,
                                     help="The trade is sold once its profit reaches this % of your capital. "
                                          "Example: 6% of ₹3,00,000 = ₹18,000.")
        max_trades = d1.number_input("Max trades per day", min_value=1, max_value=200,
                                     value=cur.max_trades_per_day, step=1,
                                     help="Only one trade is ever open at a time.")
        buffer_pts = d2.number_input("Break must cross level by (NIFTY points)", min_value=0.0,
                                     value=float(cur.breakout_buffer_points), step=1.0)
        re_labels = {"level": "NIFTY comes back and breaks yesterday's level again",
                     "green_candle": "Same option: breaks the high of a green candle on its own chart"}
        reentry_mode = st.selectbox("Buy again after a stop-loss when", list(re_labels),
                                    format_func=re_labels.get, index=list(re_labels).index(cur.reentry_mode),
                                    help="Green candle mode: after a stop-loss or trailing stop-loss exit, it "
                                         "watches the same option. Once a green candle forms, it buys when the "
                                         "price goes above that candle's high. Candle length is set below.")
        st.markdown("**Trailing stop-loss** — moves the stop-loss up as the price rises, never down.")
        t1, t2, t3, t4 = st.columns(4)
        trail_on = t1.checkbox("Use trailing stop-loss", value=cur.trail_enabled)
        trail_labels = {"candle_low": "Red candle low (option's own chart)", "points": "Points (₹)",
                        "percent": "Percent of price"}
        trail_mode = t2.selectbox("Trail by", list(trail_labels), format_func=trail_labels.get,
                                  index=list(trail_labels).index(cur.trail_mode))
        trail_distance = t3.number_input("Trail distance (points/percent modes)", min_value=0.5,
                                         value=float(cur.trail_distance), step=1.0,
                                         help="Stop-loss stays this far below the highest price reached.")
        trail_start = t4.number_input("Start trailing after profit of (points)", min_value=0.0,
                                      value=float(cur.trail_start_points), step=5.0,
                                      help="0 means start trailing straight away.")
        u1, u2, u3 = st.columns(3)
        min_profit = u3.number_input("Minimum profit to lock (₹, 0 = off)", min_value=0.0,
                                     value=float(cur.min_profit_rupees), step=100.0,
                                     help="Until a trade is this much up, the first stop-loss stays where it is. "
                                          "Once it is, the stop-loss jumps to lock this profit and trailing "
                                          "carries on above it.")
        trail_minutes = u1.number_input("Candle length (minutes)", min_value=1, max_value=60,
                                        value=int(cur.trail_candle_minutes), step=1,
                                        help="Candles on the option's own price chart. Used by 'Red candle "
                                             "low' trailing and by green-candle re-entry.")
        trail_buffer = u2.number_input("Extra gap below the red candle low (₹)", min_value=0.0,
                                       value=float(cur.trail_candle_buffer), step=0.5)
        e1, e2, e3 = st.columns(3)
        already_broken = e1.checkbox("Enter at 09:31 if NIFTY is already past the level",
                                     value=cur.enter_if_already_broken)
        skip_doji = e2.checkbox("No trade after a doji day (open = close)", value=cur.skip_doji_days)
        cooldown = e3.number_input("Wait after a stop-loss (minutes)", min_value=0, max_value=120,
                                   value=cur.cooldown_minutes_after_sl, step=1)
        s1_fields = dict(
            setup_time=setup_time.strip(), entry_start=entry_start.strip(),
            entry_end=entry_end.strip(), exit_time=exit_time.strip(),
            stop_loss_points=float(sl_points), stop_loss_mode=sl_mode,
            stop_loss_percent=float(sl_percent), target_points=float(target_points),
            target_capital_pct=float(target_pct),
            signal_mode=signal_mode, strike_offset_steps=int(strike_offset), reentry_mode=reentry_mode,
            max_trades_per_day=int(max_trades),
            trail_enabled=trail_on, trail_mode=trail_mode, trail_distance=float(trail_distance),
            trail_start_points=float(trail_start), trail_candle_minutes=int(trail_minutes),
            trail_candle_buffer=float(trail_buffer), min_profit_rupees=float(min_profit),
            breakout_buffer_points=float(buffer_pts), enter_if_already_broken=already_broken,
            skip_doji_days=skip_doji, cooldown_minutes_after_sl=int(cooldown),
        )
        st.subheader("Safety")
        h1, h2, h3 = st.columns(3)
        loss_pct = h1.number_input("Daily loss limit (% of capital)", min_value=0.1, max_value=20.0,
                                   value=float(cur.daily_loss_pct), step=0.5)
        cap_labels = {"broker": "Available funds from Kotak", "manual": "Amount I type in"}
        cap_source = h2.selectbox("Capital (for the loss limit and profit target)", list(cap_labels),
                                  format_func=cap_labels.get,
                                  index=list(cap_labels).index(cur.capital_source))
        manual_capital = h3.number_input("My capital (₹), if typed in", min_value=1000.0,
                                         value=float(cur.manual_capital), step=10000.0)
        i1, i2 = st.columns(2)
        max_lots = i1.number_input("Max lots in one order", min_value=1, max_value=50,
                                   value=cur.max_lots_per_order, step=1)
        max_orders = i2.number_input("Max orders per day", min_value=5, max_value=500,
                                     value=cur.max_orders_per_day, step=5)

        with st.expander("Narrow day + opening range (only for that strategy)"):
            st.caption("Yesterday must be a quiet day. Then the first minutes' range decides the "
                       "direction, and the stop and target are counted in NIFTY index points.")
            o1, o2, o3, o4 = st.columns(4)
            orb_fields = dict(
                orb_minutes=int(o1.number_input("Opening range (minutes)", min_value=1, max_value=60,
                                                value=int(cur.orb_minutes), step=5)),
                orb_narrow_pct=float(o2.number_input("Narrow day = quietest x% of recent days",
                                                     min_value=1.0, max_value=100.0,
                                                     value=float(cur.orb_narrow_pct), step=5.0)),
                orb_lookback_days=int(o3.number_input("Days to compare with", min_value=5, max_value=60,
                                                      value=int(cur.orb_lookback_days), step=5)),
                orb_entry_end=o4.text_input("Last entry time", cur.orb_entry_end).strip(),
            )
            o5, o6, o7, o8 = st.columns(4)
            orb_fields.update(
                orb_stop_points=float(o5.number_input("Stop (index points)", min_value=1.0,
                                                      value=float(cur.orb_stop_points), step=5.0)),
                orb_target_r=float(o6.number_input("Target (x the stop)", min_value=0.5,
                                                   value=float(cur.orb_target_r), step=0.5)),
                orb_itm_steps=int(o7.number_input("Strikes in the money", min_value=0, max_value=10,
                                                  value=int(cur.orb_itm_steps), step=1,
                                                  help="Deep in-the-money options keep more of a big move "
                                                       "and lose less time value.")),
                orb_delta_guess=float(o8.number_input("Backup stop, x the index stop", min_value=0.1,
                                                      value=float(cur.orb_delta_guess), step=0.1,
                                                      help="Only a safety net at the broker in case the app "
                                                           "stops. Keep it wider than the index stop.")),
            )

        adv = {}
        with st.expander("Order handling (advanced)"):
            j1, j2, j3, j4 = st.columns(4)
            adv = dict(
                order_price_buffer=float(j1.number_input("Limit price buffer (₹)", min_value=0.05,
                                                         value=float(cur.order_price_buffer), step=0.5)),
                sl_limit_buffer=float(j2.number_input("Stop-loss limit gap (₹)", min_value=0.05,
                                                      value=float(cur.sl_limit_buffer), step=0.5)),
                sl_grace_seconds=int(j3.number_input("Backup stop-loss after (sec)", min_value=1,
                                                     max_value=60, value=cur.sl_grace_seconds, step=1)),
                fill_timeout_seconds=int(j4.number_input("Wait for fill (sec)", min_value=2, max_value=60,
                                                         value=cur.fill_timeout_seconds, step=1)),
            )
        with st.expander("Yesterday's candle (type in by hand)"):
            st.caption("Only needed if the app can't fetch it, or it doesn't match your chart.")
            use_manual = st.checkbox("Use the candle below instead of fetching it", value=cur.use_manual_candle)
            m0, m1, m2, m3, m4 = st.columns(5)
            mc = cur.manual_candle
            adv.update(use_manual_candle=use_manual, manual_candle=dict(
                date=m0.text_input("Date (YYYY-MM-DD)", mc.date).strip(),
                open=m1.number_input("Open", min_value=0.0, value=float(mc.open), step=0.05, format="%.2f"),
                high=m2.number_input("High", min_value=0.0, value=float(mc.high), step=0.05, format="%.2f"),
                low=m3.number_input("Low", min_value=0.0, value=float(mc.low), step=0.05, format="%.2f"),
                close=m4.number_input("Close", min_value=0.0, value=float(mc.close), step=0.05,
                                      format="%.2f")))

        if st.form_submit_button("Save settings", type="primary"):
            try:
                data = cur.to_dict()
                data.update(strategy=strategy, lots=int(lots), product=product, expiry_choice=expiry,
                            daily_loss_pct=float(loss_pct), capital_source=cap_source,
                            manual_capital=float(manual_capital), max_lots_per_order=int(max_lots),
                            max_orders_per_day=int(max_orders))
                data.update(s1_fields)
                data.update(orb_fields)
                data.update(adv)
                engine.update_settings(Settings.from_dict(data))
                st.success("Settings saved")
            except RuntimeError as e:
                st.error(f"NOT SAVED: {e}. Press Stop in the sidebar, save again, then press Start.")
            except ValueError as e:
                st.error(f"Not saved: {e}")


# ====================================================================== #
# Trades
# ====================================================================== #
with tab_trades:
    all_rows = engine.journal.trades()
    mode_rows = [r for r in all_rows if r["mode"] == engine.mode]
    today_str = engine.snapshot()["day"] or date.today().isoformat()
    todays = [r for r in mode_rows if r["date"] == today_str]

    st.subheader(f"Today ({engine.mode})")
    if todays:
        st.dataframe(pd.DataFrame(todays), hide_index=True, **WIDE)
    else:
        st.caption("No closed trades yet today.")

    st.subheader(f"All {engine.mode} trades")
    if mode_rows:
        df = pd.DataFrame(mode_rows)
        df["pnl"] = df["pnl"].astype(float)
        wins = int((df["pnl"] > 0).sum())
        t1, t2, t3 = st.columns(3)
        t1.metric("Total P&L", rupees(df["pnl"].sum(), sign=True))
        t2.metric("Trades", len(df))
        t3.metric("Winning trades", f"{wins} ({wins / len(df) * 100:.0f}%)")
        daily = df.groupby("date")["pnl"].sum().reset_index()
        st.bar_chart(daily, x="date", y="pnl")
        st.dataframe(df, hide_index=True, **WIDE)
        st.download_button("Download trades CSV", df.to_csv(index=False), file_name=f"trades_{engine.mode}.csv")
    else:
        st.caption("Nothing recorded yet. P&L shown here is before brokerage, taxes and charges.")


# ====================================================================== #
# Alerts
# ====================================================================== #
with tab_alerts:
    s = engine.snapshot()
    if s["telegram"]:
        st.success("Telegram alerts are on")
        if s["telegram_error"]:
            st.error(s["telegram_error"])
        if st.button("Send a test alert"):
            engine.alerts.add("WARNING", "Test alert from NIFTY algo trader")
            st.info("Sent. Check Telegram.")
    else:
        st.info("Telegram alerts are off. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env to turn them on.")
    items = engine.alerts.recent(200)
    if items:
        st.dataframe(pd.DataFrame(items)[["date", "time", "level", "message"]], hide_index=True,
                     **WIDE)
    else:
        st.caption("No alerts yet.")
