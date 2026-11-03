"""
The safety layer. The strategy must ask here before opening any trade.
It can always say no, whatever the strategy wants.
"""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class RiskState:
    day_capital: Optional[float] = None
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    orders_today: int = 0
    kill_switch: bool = False
    stop_reason: str = ""


class RiskManager:
    def __init__(self, settings):
        self.settings = settings
        self.state = RiskState()

    # ---- daily setup ----
    def new_day(self, capital: Optional[float]) -> None:
        self.state = RiskState(day_capital=capital)

    def restore(self, saved: dict) -> None:
        for key, value in saved.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)

    @property
    def loss_limit(self) -> Optional[float]:
        if not self.state.day_capital:
            return None
        return self.state.day_capital * self.settings.daily_loss_pct / 100.0

    @property
    def trade_profit_target(self) -> Optional[float]:
        """Rupees one trade must make before it is sold (target_capital_pct of capital)."""
        pct = getattr(self.settings, "target_capital_pct", 0.0) or 0.0
        if not self.state.day_capital or pct <= 0:
            return None
        return self.state.day_capital * pct / 100.0

    @property
    def total_pnl(self) -> float:
        return self.state.realized_pnl + self.state.unrealized_pnl

    # ---- checks ----
    def can_open_trade(self, qty: int, lots: int, sl_points: float) -> Tuple[bool, str]:
        s = self.state
        if s.kill_switch:
            return False, "Kill switch is on"
        if s.stop_reason:
            return False, s.stop_reason
        if self.loss_limit is None:
            return False, "Capital unknown, cannot work out the daily loss limit"
        if lots > self.settings.max_lots_per_order:
            return False, f"{lots} lots is above max lots per order ({self.settings.max_lots_per_order})"
        if s.orders_today + 3 > self.settings.max_orders_per_day:
            return False, f"Daily order cap ({self.settings.max_orders_per_day}) reached"
        worst_case = s.realized_pnl - sl_points * qty
        if worst_case < -self.loss_limit:
            return False, (f"If this trade hits its stop-loss, today's loss would be "
                           f"₹{-worst_case:,.0f}, above the limit of ₹{self.loss_limit:,.0f}")
        return True, ""

    def loss_limit_breached(self) -> bool:
        limit = self.loss_limit
        return limit is not None and self.total_pnl <= -limit

    # ---- updates ----
    def count_order(self) -> None:
        self.state.orders_today += 1

    def add_realized(self, pnl: float) -> None:
        self.state.realized_pnl += pnl
        self.state.unrealized_pnl = 0.0

    def set_unrealized(self, pnl: float) -> None:
        self.state.unrealized_pnl = pnl

    def stop_for_day(self, reason: str) -> None:
        if not self.state.stop_reason:
            self.state.stop_reason = reason

    def activate_kill_switch(self) -> None:
        self.state.kill_switch = True
        self.stop_for_day("Kill switch pressed")
