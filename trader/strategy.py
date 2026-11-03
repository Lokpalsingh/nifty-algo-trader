"""
NIFTY previous-day candle breakout
==================================

Your rules, exactly as agreed:

  1. Look at YESTERDAY's NIFTY daily candle.
       RED   (close < open)  -> today, only BUY the ATM PE,
                                when NIFTY breaks BELOW yesterday's LOW.
       GREEN (close > open)  -> today, only BUY the ATM CE,
                                when NIFTY breaks ABOVE yesterday's HIGH.
       DOJI  (close = open)  -> no trade today (can be changed in Settings).
  2. New entries only between 09:31 and 15:02.
  3. Stop-loss: 20 points below the option's buy price. Optional trailing, and an
     optional minimum profit: once a trade is that many rupees up, the stop-loss
     jumps to lock it (before that, the first stop-loss stays).
  4. Re-entry after a stop-loss (Settings, "reentry_mode"):
       level        -> NIFTY must come back inside the level and break it again.
       green_candle -> the SAME option all day: once a green candle forms on its
                       own chart after the exit, buy when the price goes above
                       that candle's high. A trailing stop-loss exit also counts.
     Only one trade is open at a time. Maximum trades a day is a setting.
  5. Any open trade is closed at 15:14.
  6. The risk manager can block any trade (1% daily loss limit, kill switch).
  7. Profit target: a trade is sold once it is up 6% of capital (Settings,
     0 = off). After that, no more trades that day.

How a "break" works: NIFTY must first be on the inside of the level, then
cross it. If NIFTY is already past the level when entries open at 09:31 (for
example a gap), the app waits for it to come back and break again. You can
change that with "enter_if_already_broken" in Settings.
"""
import logging
import math
from datetime import datetime, timedelta
from typing import Optional

from trader import alerts as A
from trader.brokers.base import BrokerError
from trader.candles import CandleBuilder
from trader.clock import parse_hhmm
from trader.config import INDEXES, manual_candle_or_none
from trader.models import (
    BUY, CANCELLED, COMPLETE, LIMIT, OPEN, REJECTED, SELL, STOP_LIMIT,
    Candle, OptionContract, OrderStatus, Position, round_to_tick,
)


def ceil_to_tick(price: float, tick: float = 0.05) -> float:
    """Round UP to the tick size, so a locked profit is never below the minimum."""
    return round(math.ceil(price / tick - 1e-9) * tick, 2)


log = logging.getLogger("strategy")


class PrevDayBreakout:
    def __init__(self, broker, risk, journal, alerts, settings, clock, mode: str, persist: bool = True):
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.alerts = alerts
        self.settings = settings
        self.clock = clock
        self.mode = mode
        self.persist = persist
        self.connection_lost = False
        self.candles = CandleBuilder(minutes=settings.trail_candle_minutes)
        self.day: Optional[str] = None
        self._reset_day_fields()

    # ================================================================== #
    # Day state
    # ================================================================== #
    def _reset_day_fields(self):
        self.plan: Optional[dict] = None
        self.setup_done = False
        self.setup_error = ""
        self._setup_error_alerted = False
        self.armed: Optional[bool] = None
        self.trades_today = 0
        self.position: Optional[Position] = None
        self.cooldown_until: Optional[str] = None
        self.done_reason = ""
        self.paused_reason = ""
        self.exit_retry_reason: Optional[str] = None
        self.exit_progress = {"qty": 0, "value": 0.0}
        self.sl_breach_since: Optional[datetime] = None
        self.needs_reconcile = False
        self.last_spot: Optional[float] = None
        self.last_option_ltp: Optional[float] = None
        self._idle_spot_at: Optional[datetime] = None
        self.status_text = "Starting"
        # green-candle re-entry: the option to watch after a stop-loss, and the
        # minute of that exit (only candles that start after it count)
        self.reentry_contract: Optional[OptionContract] = None
        self.reentry_after = ""
        self.reentry_candles = CandleBuilder(minutes=self.settings.trail_candle_minutes)

    def _start_day(self, day: str):
        self.day = day
        self._reset_day_fields()
        saved = self.journal.load_state(day) if self.persist else None
        if saved:
            self.plan = saved.get("plan")
            self.setup_done = bool(self.plan)
            self.armed = saved.get("armed")
            self.trades_today = saved.get("trades_today", 0)
            self.cooldown_until = saved.get("cooldown_until")
            self.done_reason = saved.get("done_reason", "")
            self.paused_reason = saved.get("paused_reason", "")
            self.exit_retry_reason = saved.get("exit_retry_reason")
            self.exit_progress = saved.get("exit_progress") or {"qty": 0, "value": 0.0}
            if saved.get("position"):
                self.position = Position.from_dict(saved["position"])
            if saved.get("reentry_contract"):
                self.reentry_contract = OptionContract.from_dict(saved["reentry_contract"])
                # candles from before the restart are lost, so start counting from now
                self.reentry_after = max(saved.get("reentry_after", ""),
                                         self.reentry_candles.bucket_of(self.clock.now()))
            self._restore_extra(saved)
            self.risk.new_day(None)
            self.risk.restore(saved.get("risk", {}))
            self.needs_reconcile = True
            self.alerts.add(A.INFO, f"Restored today's progress: {self.trades_today} trade(s) taken, "
                                    f"P&L ₹{self.risk.state.realized_pnl:+,.0f}")
        else:
            self.risk.new_day(None)

    def save(self):
        if not self.persist or not self.day:
            return
        self.journal.save_state(self.day, {
            "plan": self.plan,
            "armed": self.armed,
            "trades_today": self.trades_today,
            "cooldown_until": self.cooldown_until,
            "done_reason": self.done_reason,
            "paused_reason": self.paused_reason,
            "exit_retry_reason": self.exit_retry_reason,
            "exit_progress": self.exit_progress,
            "position": self.position.to_dict() if self.position else None,
            "reentry_contract": self.reentry_contract.to_dict() if self.reentry_contract else None,
            "reentry_after": self.reentry_after,
            "risk": vars(self.risk.state),
            **self._extra_state(),
        })

    # ================================================================== #
    # Main loop step (called about once a second by the engine)
    # ================================================================== #
    def tick(self):
        now = self.clock.now()
        today = now.date().isoformat()
        if self.day != today:
            self._start_day(today)

        if now.weekday() >= 5:
            self.status_text = "Market closed (weekend)"
            return

        s = self.settings
        t = now.time()
        if t < parse_hhmm(s.setup_time):
            self.status_text = f"Waiting for {s.setup_time} to read yesterday's candle"
            return

        if not self.setup_done:
            self._setup(now)
            if not self.setup_done:
                return

        if self.needs_reconcile:
            self._reconcile()

        if self.position:
            self._manage_position(now)
            return

        if t >= parse_hhmm(s.exit_time):
            self.done_reason = self.done_reason or "Trading day finished"
        if self.done_reason:
            self._refresh_spot_quietly(now)      # keep showing the live price on the dashboard
            self.status_text = f"Done for today: {self.done_reason}"
            return
        if self.risk.state.kill_switch or self.risk.state.stop_reason:
            self.done_reason = self.risk.state.stop_reason or "Kill switch pressed"
            self.save()
            return
        if self.paused_reason:
            self._refresh_spot_quietly(now)
            self.status_text = f"Paused: {self.paused_reason}"
            return

        spot = self.broker.get_index_ltp(s.index)
        self.last_spot = spot

        start, end = self._entry_window()
        if t < parse_hhmm(start):
            self.status_text = f"Plan ready. Entries open at {start}"
            return
        if t >= parse_hhmm(end):
            self.status_text = f"Entry window closed at {end}. No new trades today"
            return

        self._look_for_entry(now, spot)

    # ---- the entry signal (a different strategy overrides this) ----
    def _look_for_entry(self, now: datetime, spot: float):
        s = self.settings
        if self._watching_for_reentry():
            self._look_for_green_candle_reentry(now, spot)
            return
        level = self.plan["level"]
        up = self.plan["direction"] == "UP"
        buf = s.breakout_buffer_points
        broken = spot > level + buf if up else spot < level - buf
        inside = spot <= level if up else spot >= level

        if self.armed is None:
            self.armed = inside or s.enter_if_already_broken
            if not self.armed:
                self.alerts.add(A.INFO, f"NIFTY {spot:,.2f} is already past the level {level:,.2f} at "
                                        f"{s.entry_start}. Waiting for it to come back and break again.")
            self.save()
        elif inside and not self.armed:
            self.armed = True
            self.save()

        if self._entry_blocked(now):
            return

        word = "above high" if up else "below low"
        if not (self.armed and broken):
            gap = spot - level
            self.status_text = (f"Watching for NIFTY to break {word} {level:,.2f} "
                                f"(now {spot:,.2f}, {gap:+,.2f} pts)")
            return

        self._enter(now, spot)

    def _refresh_spot_quietly(self, now: datetime) -> None:
        """Keep NIFTY's price on the dashboard even when no trade can happen today.
        Asks at most once every few seconds, and never breaks the loop if it fails."""
        last = getattr(self, "_idle_spot_at", None)
        if last is not None and (now - last).total_seconds() < 5:
            return
        self._idle_spot_at = now
        try:
            self.last_spot = self.broker.get_index_ltp(self.settings.index)
        except BrokerError as e:
            log.debug("Idle price check failed: %s", e)

    def _entry_blocked(self, now: datetime) -> bool:
        s = self.settings
        if self.connection_lost:
            self.status_text = "Connection unstable, new entries paused"
            return True
        if self.cooldown_until and now.isoformat() < self.cooldown_until:
            self.status_text = "Cooling down after stop-loss"
            return True
        if self.trades_today >= s.max_trades_per_day:
            self.done_reason = f"All {s.max_trades_per_day} trades used for today"
            self.save()
            return True
        return False

    # ---- green-candle re-entry on the option's own chart ----
    def _watching_for_reentry(self) -> bool:
        return self.settings.reentry_mode == "green_candle" and self.reentry_contract is not None

    def _reentry_trigger(self):
        """The green candle whose high must break, or None if there isn't one yet."""
        return self.reentry_candles.last_green(self.reentry_after)

    def _look_for_green_candle_reentry(self, now: datetime, spot: float):
        """After a stop-loss: keep building the same option's candles. Once a green
        candle has formed after the exit, buy when the price goes above its high."""
        s = self.settings
        c = self.reentry_contract
        ltp = self.broker.get_option_ltp(c)
        self.last_option_ltp = ltp
        self.reentry_candles.add(now, ltp)
        if self._entry_blocked(now):
            return
        green = self._reentry_trigger()
        minutes = s.trail_candle_minutes
        if green is None:
            self.status_text = (f"Waiting to buy {c.symbol} again: needs a green {minutes}-min candle "
                                f"first (price now ₹{ltp:.2f})")
            return
        if ltp <= green.high:
            self.status_text = (f"Buy {c.symbol} again when it goes above ₹{green.high:.2f} "
                                f"(high of the {green.start} green candle). Price now ₹{ltp:.2f}")
            return
        self._enter(now, spot, contract=c)

    # ================================================================== #
    # Morning setup
    # ================================================================== #
    def _setup(self, now: datetime):
        s = self.settings
        manual = manual_candle_or_none(s)
        if manual:
            candle = Candle(manual.date, manual.open, manual.high, manual.low, manual.close, "Typed in manually")
        else:
            candle = self.broker.get_previous_day_candle(s.index, now.date())

        if candle is None:
            self._setup_failed("Could not get yesterday's candle. Type it in under Settings > Yesterday's candle.")
            return

        if s.capital_source == "manual":
            capital = s.manual_capital
        else:
            capital = self.broker.get_available_capital()
        if not capital or capital <= 0:
            self._setup_failed("Could not read your capital from the broker. "
                               "Set Capital source to manual in Settings.")
            return

        if self.risk.state.day_capital is None:
            self.risk.state.day_capital = capital

        color = candle.color
        plan = {"candle": vars(candle).copy(), "color": color, "capital": capital,
                "loss_limit": self.risk.loss_limit}
        if color == "DOJI" and s.skip_doji_days:
            plan.update(direction=None, level=None, option_type=None,
                        text="Yesterday was a doji (open = close). No trade today.")
            self.done_reason = "Yesterday was a doji"
        else:
            if color == "DOJI":
                color = "GREEN"
            up = color == "GREEN"
            follow = s.signal_mode == "breakout"
            option_type = ("CE" if up else "PE") if follow else ("PE" if up else "CE")
            plan.update(direction="UP" if up else "DOWN",
                        level=candle.high if up else candle.low,
                        option_type=option_type)
            offset = ""
            if s.strike_offset_steps:
                offset = f" ({abs(s.strike_offset_steps)} strike "
                offset += "OTM)" if s.strike_offset_steps > 0 else "ITM)"
            plan["text"] = (f"Yesterday ({candle.date}) was {color}. Buy {'ATM ' if not offset else ''}"
                            f"{option_type}{offset} when NIFTY breaks "
                            f"{'above' if up else 'below'} {plan['level']:,.2f}"
                            f"{'' if follow else '  [reverse mode]'}")
        self.plan = plan
        self.setup_done = True
        self.setup_error = ""
        target = self.risk.trade_profit_target
        target_text = f" Profit target ₹{target:,.0f} per trade." if target else ""
        self.alerts.add(A.TRADE, f"Today's plan: {plan['text']}. Daily loss limit ₹{self.risk.loss_limit:,.0f}."
                                 f"{target_text}")
        self.save()

    def _setup_failed(self, message: str):
        self.setup_error = message
        self.status_text = message
        if not self._setup_error_alerted:
            self.alerts.add(A.WARNING, message)
            self._setup_error_alerted = True

    def _reconcile(self):
        """After a restart, check the broker agrees with what we remember."""
        self.needs_reconcile = False
        if not self.position:
            return
        try:
            open_qty = self.broker.get_open_quantity(self.position.contract)
        except BrokerError as e:
            self.needs_reconcile = True
            raise e
        if open_qty <= 0:
            self.alerts.add(A.WARNING, f"Remembered position {self.position.contract.symbol} is no longer open at "
                                       f"the broker (closed while the app was off). Check P&L in your broker app.")
            self.position = None
            self.exit_retry_reason = None
            self.exit_progress = {"qty": 0, "value": 0.0}
            self.armed = False
            self.save()

    # ================================================================== #
    # Entry
    # ================================================================== #
    def _enter(self, now: datetime, spot: float, contract: Optional[OptionContract] = None):
        """Buy. `contract` is given for a green-candle re-entry (same option all day);
        otherwise the ATM (or offset) option is picked from NIFTY's price now."""
        s = self.settings
        if contract is None:
            option_type = self.plan["option_type"]
            # a positive offset picks a strike further out of the money
            contract = self.broker.find_atm_option(s.index, spot + self._strike_shift(option_type),
                                                   option_type, now.date(), s.expiry_choice)
        qty = s.lots * contract.lot_size

        ltp = self.broker.get_option_ltp(contract)
        sl_distance = self._stop_distance(ltp)

        ok, reason = self.risk.can_open_trade(qty, s.lots, sl_distance)
        if not ok:
            self.done_reason = reason
            self.alerts.add(A.WARNING, f"No more trades today: {reason}")
            self.save()
            return

        limit_price = round_to_tick(ltp + s.order_price_buffer)
        trade_no = self.trades_today + 1
        try:
            order_id = self.broker.place_order(contract, BUY, qty, LIMIT, limit_price, tag=f"pdb-entry-{trade_no}")
        except BrokerError as e:
            self.paused_reason = (f"Entry order failed ({e}). Check your broker app for any order or position, "
                                  f"then press Resume on the dashboard.")
            self.alerts.add(A.CRITICAL, self.paused_reason)
            self.save()
            return
        self.risk.count_order()

        status = self._wait_for_fill(order_id)
        if status.status == OPEN:
            self.broker.cancel_order(order_id)
            status = self.broker.get_order_status(order_id)
        if status.filled_qty <= 0:
            msg = status.message if status.status == REJECTED else f"not filled at limit ₹{limit_price:.2f}"
            if self._watching_for_reentry():
                # needs a NEW green candle, so it doesn't fire the same order every second
                self.reentry_after = self.reentry_candles.bucket_of(now)
                self.alerts.add(A.WARNING, f"Entry {contract.symbol} {msg}. Skipped, waiting for a new "
                                           f"green candle.")
            else:
                self.alerts.add(A.WARNING, f"Entry {contract.symbol} {msg}. Skipped, waiting for a fresh "
                                           f"breakout.")
            self.armed = False
            self.save()
            return

        entry_price = status.avg_price or ltp
        self.trades_today = trade_no
        sl_trigger = round(entry_price - self._stop_distance(entry_price), 2)
        pos = Position(contract=contract, qty=status.filled_qty, entry_price=entry_price,
                       sl_trigger=max(0.05, round_to_tick(sl_trigger)), entry_time=now.isoformat(),
                       trade_no=trade_no, high_price=entry_price)
        self.position = pos
        self._on_entered(pos, spot)
        self.armed = False
        self.candles = CandleBuilder(minutes=s.trail_candle_minutes)
        self.exit_progress = {"qty": 0, "value": 0.0}
        self.save()

        if sl_trigger > 0.05:
            sl_limit = round_to_tick(max(0.05, pos.sl_trigger - s.sl_limit_buffer))
            try:
                pos.sl_order_id = self.broker.place_order(contract, SELL, pos.qty, STOP_LIMIT, sl_limit,
                                                          trigger_price=pos.sl_trigger, tag=f"pdb-sl-{trade_no}")
                self.risk.count_order()
            except BrokerError as e:
                self.alerts.add(A.WARNING, f"Could not place the stop-loss order at the broker ({e}). "
                                           f"The app is watching the stop-loss itself, keep it running.")

        partial = "" if pos.qty == qty else f" (partial fill {pos.qty}/{qty})"
        self.alerts.add(A.TRADE, f"Trade {trade_no}/{s.max_trades_per_day}: BOUGHT {contract.symbol} x{pos.qty}"
                                 f"{partial} at ₹{entry_price:.2f}. Stop-loss ₹{pos.sl_trigger:.2f}. "
                                 f"NIFTY {spot:,.2f}")
        self.save()

    def _wait_for_fill(self, order_id: str) -> OrderStatus:
        deadline = self.clock.now() + timedelta(seconds=self.settings.fill_timeout_seconds)
        status = self.broker.get_order_status(order_id)
        while not status.is_final and self.clock.now() < deadline:
            self.clock.sleep(0.5)
            status = self.broker.get_order_status(order_id)
        return status

    # ================================================================== #
    # Managing an open trade
    # ================================================================== #
    def _manage_position(self, now: datetime):
        s = self.settings
        pos = self.position

        if self.exit_retry_reason:
            self._exit(now, self.exit_retry_reason)
            return

        ltp = self.broker.get_option_ltp(pos.contract)
        self.last_option_ltp = ltp
        try:
            self.last_spot = self.broker.get_index_ltp(s.index)
        except BrokerError:
            pass
        self.risk.set_unrealized((ltp - pos.entry_price) * pos.qty)

        if pos.sl_order_id:
            st = self.broker.get_order_status(pos.sl_order_id)
            if st.status == COMPLETE:
                self._close_position(now, st.avg_price or pos.sl_trigger, "Stop-loss hit")
                return
            if st.status in (REJECTED, CANCELLED):
                self.alerts.add(A.WARNING, f"Stop-loss order at broker was {st.status.lower()} "
                                           f"{st.message}. The app is watching the stop-loss itself.")
                pos.sl_order_id = None
                self.save()

        if ltp > pos.high_price:
            pos.high_price = ltp
        self.candles.add(now, ltp)
        trail_exit = self._update_trailing_stop(ltp)
        if trail_exit:
            self._exit(now, trail_exit)
            return

        extra = self._extra_exit_reason(now, ltp)
        if extra:
            self._exit(now, extra)
            return

        if s.target_points > 0 and ltp >= pos.entry_price + s.target_points:
            self._exit(now, f"Target hit (+{s.target_points:.0f} points)")
            return

        target_rupees = self.risk.trade_profit_target
        if target_rupees and (ltp - pos.entry_price) * pos.qty >= target_rupees:
            self._exit(now, f"Profit target hit ({s.target_capital_pct:g}% of capital, "
                            f"₹{target_rupees:,.0f})")
            return

        if now.time() >= parse_hhmm(s.exit_time):
            self._exit(now, f"Time exit ({s.exit_time})")
            return

        if self.risk.state.kill_switch:
            self._exit(now, "Kill switch")
            return

        if self.risk.loss_limit_breached():
            self.risk.stop_for_day("Daily loss limit reached")
            self._exit(now, "Daily loss limit reached")
            return

        if ltp <= pos.sl_trigger:
            if self.sl_breach_since is None:
                self.sl_breach_since = now
            waited = (now - self.sl_breach_since).total_seconds()
            if pos.sl_order_id is None or waited >= s.sl_grace_seconds:
                self._exit(now, "Stop-loss hit")
                return
        else:
            self.sl_breach_since = None

        pnl = (ltp - pos.entry_price) * pos.qty
        self.status_text = (f"In trade {pos.trade_no}: {pos.contract.symbol} LTP ₹{ltp:.2f}, "
                            f"stop-loss ₹{pos.sl_trigger:.2f}, P&L ₹{pnl:+,.0f}")

    def min_profit_lock_price(self) -> Optional[float]:
        """Option price at which the minimum profit (min_profit_rupees) is locked."""
        s = self.settings
        pos = self.position
        if pos is None or s.min_profit_rupees <= 0 or not pos.qty:
            return None
        return ceil_to_tick(pos.entry_price + s.min_profit_rupees / pos.qty)

    def _update_trailing_stop(self, ltp: float) -> Optional[str]:
        """Move the stop-loss up (never down). Returns an exit reason when the
        price is already below the new stop level.

        Minimum profit (min_profit_rupees): until the trade has been that much up,
        the first stop-loss stays where it is. Once it has, the stop-loss jumps to
        lock that profit and trailing carries on from there, so a trade ends as a
        loss at the first stop-loss or with at least that profit (15:14 aside).
        candle_low: to the low of the last red candle on the option's own chart.
        points/percent: to a fixed distance below the highest price reached.
        """
        s = self.settings
        pos = self.position
        if pos is None:
            return None
        lock_price = self.min_profit_lock_price()
        if lock_price is not None and pos.high_price < lock_price:
            return None                               # not yet up the minimum: first SL stays
        if not s.trail_enabled and lock_price is None:
            return None

        new_trigger = None
        how = ""
        reason = "Trailing stop-loss (red candle low)" if s.trail_mode == "candle_low" else "Trailing stop-loss"
        if s.trail_enabled and pos.high_price - pos.entry_price >= s.trail_start_points:
            if s.trail_mode == "candle_low":
                red = self.candles.last_red()
                if red is not None:
                    new_trigger = red.low - s.trail_candle_buffer
                    how = f"last red {s.trail_candle_minutes}-min candle low"
            elif s.trail_mode == "percent":
                new_trigger = pos.high_price * (1 - s.trail_distance / 100.0)
                how = f"high ₹{pos.high_price:.2f}"
            else:
                new_trigger = pos.high_price - s.trail_distance
                how = f"high ₹{pos.high_price:.2f}"
        if lock_price is not None and (new_trigger is None or new_trigger < lock_price):
            new_trigger = lock_price
            how = f"₹{s.min_profit_rupees:,.0f} minimum profit locked"
            reason = f"Profit lock (₹{s.min_profit_rupees:,.0f} minimum)"
        if new_trigger is None:
            return None
        new_trigger = round_to_tick(max(0.05, new_trigger))
        if new_trigger <= pos.sl_trigger + 0.04:      # must rise by at least one tick
            return None
        if new_trigger >= ltp:
            # price is already below the new stop level: the rule says get out now
            return reason

        old_trigger = pos.sl_trigger
        pos.sl_trigger = new_trigger
        locked = (new_trigger - pos.entry_price) * pos.qty
        if pos.sl_order_id:
            new_limit = round_to_tick(max(0.05, new_trigger - s.sl_limit_buffer))
            try:
                pos.sl_order_id = self.broker.modify_order(pos.sl_order_id, pos.contract, pos.qty,
                                                           STOP_LIMIT, new_limit, new_trigger)
                self.risk.count_order()
            except BrokerError as e:
                log.warning("Could not modify stop-loss order: %s", e)
                self._replace_stop_loss_order(new_trigger, str(e))
        self.alerts.add(A.INFO, f"Stop-loss moved up ₹{old_trigger:.2f} -> ₹{new_trigger:.2f} "
                                f"({how}, locked ₹{locked:+,.0f})")
        self.save()
        return None

    def _replace_stop_loss_order(self, new_trigger: float, why: str):
        """If the broker won't modify the order, cancel it and place a fresh one."""
        s = self.settings
        pos = self.position
        try:
            self.broker.cancel_order(pos.sl_order_id)
            st = self.broker.get_order_status(pos.sl_order_id)
            if st.status == COMPLETE:
                self._close_position(self.clock.now(), st.avg_price or pos.sl_trigger, "Stop-loss hit")
                return
            pos.sl_order_id = self.broker.place_order(pos.contract, SELL, pos.qty, STOP_LIMIT,
                                                      round_to_tick(max(0.05, new_trigger - s.sl_limit_buffer)),
                                                      trigger_price=new_trigger, tag=f"pdb-sl-{pos.trade_no}")
            self.risk.count_order()
        except BrokerError as e:
            pos.sl_order_id = None
            self.alerts.add(A.WARNING, f"Stop-loss order at the broker is gone ({why}; {e}). "
                                       f"The app is watching the stop-loss itself, keep it running.")

    def _extra_exit_reason(self, now: datetime, ltp: float) -> Optional[str]:
        """Strategies can add their own exit rules here."""
        return None

    # ---- small hooks so another strategy can reuse all of the above ----
    def _stop_distance(self, price: float) -> float:
        """Rupees below the option's price where the stop-loss sits."""
        s = self.settings
        return (price * s.stop_loss_percent / 100.0) if s.stop_loss_mode == "percent" else s.stop_loss_points

    def _entry_window(self):
        """When new entries are allowed. Another strategy can use its own times."""
        return self.settings.entry_start, self.settings.entry_end

    def _strike_shift(self, option_type: str) -> float:
        """How far from the money to pick the strike (+ = OTM, - = ITM)."""
        s = self.settings
        step = INDEXES[s.index]["strike_step"]
        return s.strike_offset_steps * step * (1 if option_type == "CE" else -1)

    def _on_entered(self, pos, spot: float) -> None:
        """Called right after a fill, with the index price at that moment."""

    def _extra_state(self) -> dict:
        """Extra day state to save (and restore) across restarts."""
        return {}

    def _restore_extra(self, saved: dict) -> None:
        pass

    def _exit(self, now: datetime, reason: str):
        """Close the open trade with limit orders. Never sells more than the
        broker shows as open, so a retry can't accidentally create a short."""
        s = self.settings
        pos = self.position
        prog = self.exit_progress
        try:
            if pos.sl_order_id:
                self.broker.cancel_order(pos.sl_order_id)
                st = self.broker.get_order_status(pos.sl_order_id)
                if st.status == OPEN:
                    self.exit_retry_reason = reason
                    self.save()
                    return
                if st.filled_qty > 0:
                    prog["qty"] += st.filled_qty
                    prog["value"] += st.filled_qty * (st.avg_price or pos.sl_trigger)
                pos.sl_order_id = None
                self.save()

            for attempt in range(3):
                remaining = pos.qty - prog["qty"]
                if remaining <= 0:
                    break
                broker_open = self.broker.get_open_quantity(pos.contract)
                remaining = min(remaining, max(0, broker_open))
                if remaining <= 0:
                    # broker shows nothing left; treat remainder as closed at last price
                    est = self.last_option_ltp or pos.sl_trigger
                    prog["value"] += (pos.qty - prog["qty"]) * est
                    prog["qty"] = pos.qty
                    self.alerts.add(A.WARNING, f"Broker shows {pos.contract.symbol} already closed. "
                                               f"Exit price estimated, check your broker app.")
                    break
                ltp = self.broker.get_option_ltp(pos.contract)
                price = round_to_tick(max(0.05, ltp - s.order_price_buffer * (attempt + 1)))
                order_id = self.broker.place_order(pos.contract, SELL, remaining, LIMIT, price,
                                                   tag=f"pdb-exit-{pos.trade_no}")
                self.risk.count_order()
                st = self._wait_for_fill(order_id)
                if st.status == OPEN:
                    self.broker.cancel_order(order_id)
                    st = self.broker.get_order_status(order_id)
                if st.filled_qty > 0:
                    prog["qty"] += st.filled_qty
                    prog["value"] += st.filled_qty * (st.avg_price or price)
                self.save()
        except BrokerError:
            self.exit_retry_reason = reason
            self.save()
            raise

        left = pos.qty - prog["qty"]
        if left > 0:
            if self.exit_retry_reason != reason:
                self.alerts.add(A.CRITICAL, f"Could not fully exit {pos.contract.symbol} ({left} qty left). "
                                            f"The app keeps trying. You can also exit it in your broker app.")
            self.exit_retry_reason = reason
            self.save()
            return

        self._close_position(now, prog["value"] / prog["qty"], reason)

    def _close_position(self, now: datetime, exit_price: float, reason: str):
        s = self.settings
        pos = self.position
        pnl = (exit_price - pos.entry_price) * pos.qty
        self.risk.add_realized(pnl)
        self.journal.record_trade({
            "date": self.day, "trade_no": pos.trade_no, "mode": self.mode, "symbol": pos.contract.symbol,
            "option_type": pos.contract.option_type, "strike": pos.contract.strike, "qty": pos.qty,
            "entry_time": pos.entry_time[11:19], "entry_price": f"{pos.entry_price:.2f}",
            "exit_time": now.isoformat()[11:19], "exit_price": f"{exit_price:.2f}",
            "pnl": f"{pnl:.2f}", "exit_reason": reason,
        })
        self.position = None
        self.exit_retry_reason = None
        self.exit_progress = {"qty": 0, "value": 0.0}
        self.sl_breach_since = None
        self.last_option_ltp = None
        self.armed = False

        self.alerts.add(A.TRADE, f"Trade {pos.trade_no} closed ({reason}): SOLD {pos.contract.symbol} at "
                                 f"₹{exit_price:.2f}. P&L ₹{pnl:+,.0f}. Today ₹{self.risk.state.realized_pnl:+,.0f}")

        green_mode = s.reentry_mode == "green_candle"
        stop_exit = reason == "Stop-loss hit" or (green_mode and reason.startswith(("Trailing stop-loss",
                                                                                    "Profit lock")))
        if stop_exit and s.cooldown_minutes_after_sl > 0:
            self.cooldown_until = (now + timedelta(minutes=s.cooldown_minutes_after_sl)).isoformat()
        if self.risk.loss_limit_breached():
            self.risk.stop_for_day("Daily loss limit reached")
        if self.risk.state.stop_reason:
            self.done_reason = self.risk.state.stop_reason
        elif not stop_exit:
            self.done_reason = reason
        elif self.trades_today >= s.max_trades_per_day:
            self.done_reason = f"All {s.max_trades_per_day} trades used for today"
        if green_mode and stop_exit and not self.done_reason:
            self.reentry_contract = pos.contract
            self.reentry_candles = CandleBuilder(minutes=s.trail_candle_minutes)
            self.reentry_after = self.reentry_candles.bucket_of(now)
            self.alerts.add(A.INFO, f"Watching {pos.contract.symbol} to buy again: once a green "
                                    f"{s.trail_candle_minutes}-min candle forms, it buys when the price "
                                    f"goes above that candle's high.")
        if self.done_reason:
            self.alerts.add(A.WARNING if self.risk.state.stop_reason else A.INFO,
                            f"No more trades today: {self.done_reason}")
        self.save()

    # ================================================================== #
    # Controls from the dashboard
    # ================================================================== #
    def kill_switch(self):
        self.risk.activate_kill_switch()
        self.alerts.add(A.CRITICAL, "Kill switch pressed. Exiting any open trade and stopping for today.")
        if self.position:
            self._exit(self.clock.now(), "Kill switch")
        self.done_reason = "Kill switch pressed"
        self.save()

    def reset_today(self) -> str:
        """Clear a kill switch so trading can continue today. Refuses when it
        would be unsafe: an open trade, or the daily loss limit already hit."""
        if self.position:
            return "There is an open trade. Close it first."
        if self.risk.loss_limit_breached():
            return "Today's loss limit is reached. Trading stays stopped until tomorrow."
        if self.risk.state.stop_reason and "loss limit" in self.risk.state.stop_reason.lower():
            return "Today's loss limit is reached. Trading stays stopped until tomorrow."
        if self.trades_today >= self.settings.max_trades_per_day:
            return f"All {self.settings.max_trades_per_day} trades for today are used."
        self.risk.state.kill_switch = False
        self.risk.state.stop_reason = ""
        self.done_reason = ""
        self.paused_reason = ""
        self.armed = False
        self.alerts.add(A.WARNING, "Kill switch cleared. Watching for a fresh breakout again. "
                                   f"Trades used so far: {self.trades_today}/{self.settings.max_trades_per_day}")
        self.save()
        return ""

    def resume(self):
        if self.paused_reason:
            self.paused_reason = ""
            self.armed = False
            self.alerts.add(A.INFO, "Resumed. Waiting for a fresh breakout.")
            self.save()

    def open_position_text(self) -> str:
        if not self.position:
            return ""
        at_broker = "its stop-loss order is at the broker" if self.position.sl_order_id else "no stop-loss order at the broker"
        return f"{self.position.contract.symbol} x{self.position.qty} ({at_broker})"

    def snapshot(self) -> dict:
        pos = self.position
        return {
            "day": self.day,
            "status": self.status_text,
            "setup_error": self.setup_error,
            "plan": self.plan,
            "armed": self.armed,
            "spot": self.last_spot,
            "trades_today": self.trades_today,
            "max_trades": self.settings.max_trades_per_day,
            "done_reason": self.done_reason,
            "paused_reason": self.paused_reason,
            "connection_lost": self.connection_lost,
            "reentry": None if not self._watching_for_reentry() or pos else {
                "symbol": self.reentry_contract.symbol,
                "trigger": (self._reentry_trigger().high if self._reentry_trigger() else None),
                "candle": (self._reentry_trigger().start if self._reentry_trigger() else None),
                "ltp": self.last_option_ltp,
            },
            "position": None if not pos else {
                "symbol": pos.contract.symbol, "qty": pos.qty, "entry_price": pos.entry_price,
                "sl_trigger": pos.sl_trigger, "ltp": self.last_option_ltp, "trade_no": pos.trade_no,
                "high_price": pos.high_price, "trailing": self.settings.trail_enabled,
                "trail_mode": self.settings.trail_mode,
                "last_red_low": (self.candles.last_red().low if self.candles.last_red() else None),
                "locked_in": (pos.sl_trigger - pos.entry_price) * pos.qty,
                "entry_time": pos.entry_time[11:19], "sl_at_broker": bool(pos.sl_order_id),
                "exiting": bool(self.exit_retry_reason),
                "lock_price": self.min_profit_lock_price(),
                "min_profit": self.settings.min_profit_rupees,
                "target_price": (pos.entry_price + self.risk.trade_profit_target / pos.qty
                                 if self.risk.trade_profit_target and pos.qty else None),
            },
            "risk": {
                "capital": self.risk.state.day_capital,
                "loss_limit": self.risk.loss_limit,
                "profit_target": self.risk.trade_profit_target,
                "realized": self.risk.state.realized_pnl,
                "unrealized": self.risk.state.unrealized_pnl,
                "kill_switch": self.risk.state.kill_switch,
                "stop_reason": self.risk.state.stop_reason,
            },
        }
