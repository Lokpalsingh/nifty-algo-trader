"""
Replays the strategy over historical data, day by day, minute by minute.

It uses the SAME strategy file that trades live (trader/strategy.py), so the
results reflect your real rules, including the breakout logic, stop-loss,
trailing stop, re-entries, the 5-trade cap, the 2% daily loss limit and the
15:14 exit.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from trader.alerts import AlertHub
from trader.backtest.broker import BacktestBroker
from trader.backtest.costs import CostModel
from trader.backtest.history import Bar, group_by_day
from trader.clock import IST, FakeClock, parse_hhmm
from trader.config import Settings
from trader.models import Candle
from trader.risk import RiskManager
from trader.strategy import PrevDayBreakout
from trader.strategy_orb import NarrowOrb

log = logging.getLogger("backtest")


class MemoryJournal:
    """Keeps trades in memory instead of writing files."""

    def __init__(self):
        self.rows: List[dict] = []

    def load_state(self, day):
        return None

    def save_state(self, day, state):
        pass

    def record_trade(self, row: dict):
        self.rows.append(dict(row))


class QuietAlerts(AlertHub):
    def add(self, level, message):
        pass


@dataclass
class DayResult:
    day: str
    trades: List[dict] = field(default_factory=list)
    gross: float = 0.0
    costs: float = 0.0
    plan: str = ""
    net: float = 0.0


@dataclass
class BacktestResult:
    days: List[DayResult] = field(default_factory=list)
    settings: Optional[Settings] = None
    label: str = ""

    @property
    def trades(self) -> List[dict]:
        return [t for d in self.days for t in d.trades]

    def summary(self) -> dict:
        trades = self.trades
        wins = [t for t in trades if t["net"] > 0]
        losses = [t for t in trades if t["net"] <= 0]
        traded_days = [d for d in self.days if d.trades]
        win_days = [d for d in traded_days if d.net > 0]
        equity, peak, drawdown = 0.0, 0.0, 0.0
        for d in self.days:
            equity += d.net
            peak = max(peak, equity)
            drawdown = min(drawdown, equity - peak)
        return {
            "days_tested": len(self.days),
            "days_with_trades": len(traded_days),
            "win_days": len(win_days),
            "avg_traded_day": (sum(d.net for d in traded_days) / len(traded_days)) if traded_days else 0.0,
            "day_profit_factor": (sum(d.net for d in self.days if d.net > 0)
                                  / abs(sum(d.net for d in self.days if d.net < 0))
                                  if any(d.net < 0 for d in self.days) else 0.0),
            "trades": len(trades),
            "wins": len(wins),
            "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
            "gross": sum(t["gross"] for t in trades),
            "costs": sum(t["costs"] for t in trades),
            "net": sum(t["net"] for t in trades),
            "profit_factor": (sum(t["net"] for t in wins) / abs(sum(t["net"] for t in losses))
                              if losses and sum(t["net"] for t in losses) else 0.0),
            "avg_trade": (sum(t["net"] for t in trades) / len(trades)) if trades else 0.0,
            "avg_win": (sum(t["net"] for t in wins) / len(wins)) if wins else 0.0,
            "avg_loss": (sum(t["net"] for t in losses) / len(losses)) if losses else 0.0,
            "best_day": max((d.net for d in self.days), default=0.0),
            "worst_day": min((d.net for d in self.days), default=0.0),
            "max_drawdown": drawdown,
            "exit_reasons": _count(t["exit_reason"] for t in trades),
        }


def _count(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def _day_candle(day: str, bars: List[Bar]) -> Candle:
    return Candle(date=day, open=bars[0].open, high=max(b.high for b in bars),
                  low=min(b.low for b in bars), close=bars[-1].close, source="History")


def _sub_ticks(bar: Bar):
    """Approximate the path inside one minute: open, then the extremes in a
    sensible order, then close."""
    if bar.close >= bar.open:
        path = [bar.open, bar.low, bar.high, bar.close]
    else:
        path = [bar.open, bar.high, bar.low, bar.close]
    return [(bar.time + timedelta(seconds=i * 12), price) for i, price in enumerate(path)]


def run_backtest(bars: List[Bar], settings: Settings, costs: CostModel,
                 iv: float = 0.14, slippage: float = 1.0, lot_size: int = 65,
                 capital: float = 300000.0, label: str = "",
                 vix_by_day: Optional[Dict[str, float]] = None,
                 time_model: str = "calendar") -> BacktestResult:
    """vix_by_day: India VIX at each day's open. When given, option prices for that
    day use it as their volatility instead of the fixed `iv`."""
    by_day = group_by_day(bars)
    days = sorted(by_day)
    result = BacktestResult(settings=settings, label=label)
    broker = BacktestBroker(lot_size=lot_size, iv=iv, slippage=slippage, capital=capital,
                            time_model=time_model)
    stop_after = parse_hhmm(settings.exit_time)
    vix_by_day = vix_by_day or {}

    for i in range(1, len(days)):
        day, prev_day = days[i], days[i - 1]
        day_bars = by_day[day]
        if len(day_bars) < 30:                 # half-day or bad data
            continue
        broker.set_day(_day_candle(prev_day, by_day[prev_day]), day_bars, vix=vix_by_day.get(day))

        clock = FakeClock(datetime.combine(day_bars[0].time.date(), parse_hhmm("09:15"), tzinfo=IST))
        journal = MemoryJournal()
        risk = RiskManager(settings)
        alerts = QuietAlerts(clock=clock)
        strategy_class = NarrowOrb if settings.strategy == "narrow_orb" else PrevDayBreakout
        strategy = strategy_class(broker, risk, journal, alerts, settings, clock,
                                  mode="backtest", persist=False)

        for bar in day_bars:
            if bar.time.time() > stop_after and not strategy.position:
                break
            for when, price in _sub_ticks(bar):
                clock.set(when)
                broker.set_time(when, price)
                try:
                    strategy.tick()
                except Exception as e:      # never let one bad day stop the run
                    log.warning("%s %s: %s", day, when.time(), e)
                    break

        day_result = DayResult(day=day, plan=(strategy.plan or {}).get("text", ""))
        for row in journal.rows:
            qty = int(row["qty"])
            entry, exit_ = float(row["entry_price"]), float(row["exit_price"])
            short = row.get("direction") == "short"
            gross = (entry - exit_) * qty if short else (exit_ - entry) * qty
            # charges depend on which side is the buy: a short leg sells first and buys back
            charge = costs.round_trip(exit_, entry, qty) if short else costs.round_trip(entry, exit_, qty)
            row.update(gross=round(gross, 2), costs=charge, net=round(gross - charge, 2))
            day_result.trades.append(row)
        day_result.gross = sum(t["gross"] for t in day_result.trades)
        day_result.costs = sum(t["costs"] for t in day_result.trades)
        day_result.net = round(day_result.gross - day_result.costs, 2)
        result.days.append(day_result)
    return result
