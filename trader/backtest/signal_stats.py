"""
Does the signal itself have an edge?

This looks ONLY at the NIFTY index, with no options, no premiums, no Black-Scholes
and no charges. It answers two questions from real data:

  1. After a break of yesterday's high/low, how far does NIFTY travel in each
     direction before it goes against you? (MFE = best move in your favour,
     MAE = worst move against you.)
  2. For every combination of stop-loss and target in index points, what would
     the average trade have earned, going WITH the break and AGAINST it?

If the index itself shows no edge, no option setting can create one. If it does,
these numbers tell you what stop and target sizes the data actually supports.
"""
from dataclasses import dataclass
from statistics import median
from typing import Dict, List, Optional, Tuple

from trader.backtest.history import Bar, group_by_day
from trader.clock import parse_hhmm


@dataclass
class BreakEvent:
    day: str
    time: str
    price: float          # index level when the break happened
    direction: str        # "UP" = broke above yesterday's high, "DOWN" = below the low
    mfe_up: float = 0.0   # best rise after the break
    mfe_down: float = 0.0 # best fall after the break


def find_breaks(bars: List[Bar], prev_high: float, prev_low: float, prev_color: str,
                buffer_points: float, entry_start: str, entry_end: str,
                max_events: int = 5) -> List[BreakEvent]:
    """Same break logic as the live strategy: be inside the level first, then cross it."""
    if prev_color == "DOJI":
        return []
    up = prev_color == "GREEN"
    level = prev_high if up else prev_low
    start, end = parse_hhmm(entry_start), parse_hhmm(entry_end)
    events: List[BreakEvent] = []
    armed: Optional[bool] = None

    for bar in bars:
        t = bar.time.time()
        if t < start or t >= end or len(events) >= max_events:
            continue
        inside = bar.low <= level if up else bar.high >= level
        broken = bar.high > level + buffer_points if up else bar.low < level - buffer_points
        if armed is None:
            armed = inside
        elif inside and not armed:
            armed = True
        if armed and broken:
            price = level + buffer_points if up else level - buffer_points
            events.append(BreakEvent(bar.time.date().isoformat(), bar.time.strftime("%H:%M"),
                                     price, "UP" if up else "DOWN"))
            armed = False
    return events


def measure_excursions(bars: List[Bar], event: BreakEvent, exit_time: str) -> BreakEvent:
    """How far the index moved up and down after the break, until the exit time."""
    end = parse_hhmm(exit_time)
    best_up = best_down = 0.0
    started = False
    for bar in bars:
        if bar.time.strftime("%H:%M") <= event.time:
            continue
        if bar.time.time() >= end:
            break
        started = True
        best_up = max(best_up, bar.high - event.price)
        best_down = max(best_down, event.price - bar.low)
    if started:
        event.mfe_up, event.mfe_down = round(best_up, 2), round(best_down, 2)
    return event


def simulate(bars: List[Bar], event: BreakEvent, side: str, stop: float, target: float,
             exit_time: str) -> Tuple[float, str]:
    """Trade the index in points. side "follow" goes with the break, "fade" against it.
    If a bar touches both stop and target, the stop is assumed first (pessimistic)."""
    long_side = (event.direction == "UP") if side == "follow" else (event.direction == "DOWN")
    end = parse_hhmm(exit_time)
    last = event.price
    for bar in bars:
        if bar.time.strftime("%H:%M") <= event.time:
            continue
        if bar.time.time() >= end:
            break
        last = bar.close
        if long_side:
            if bar.low <= event.price - stop:
                return -stop, "stop"
            if target and bar.high >= event.price + target:
                return target, "target"
        else:
            if bar.high >= event.price + stop:
                return -stop, "stop"
            if target and bar.low <= event.price - target:
                return target, "target"
    points = (last - event.price) if long_side else (event.price - last)
    return round(points, 2), "time"


def collect_events(bars: List[Bar], buffer_points: float, entry_start: str,
                   entry_end: str, exit_time: str, max_events: int = 5):
    by_day = group_by_day(bars)
    days = sorted(by_day)
    events: List[Tuple[BreakEvent, List[Bar]]] = []
    for i in range(1, len(days)):
        prev_bars, day_bars = by_day[days[i - 1]], by_day[days[i]]
        if len(day_bars) < 30 or len(prev_bars) < 30:
            continue
        prev_open, prev_close = prev_bars[0].open, prev_bars[-1].close
        color = "GREEN" if prev_close > prev_open else ("RED" if prev_close < prev_open else "DOJI")
        found = find_breaks(day_bars, max(b.high for b in prev_bars), min(b.low for b in prev_bars),
                            color, buffer_points, entry_start, entry_end, max_events)
        for event in found:
            events.append((measure_excursions(day_bars, event, exit_time), day_bars))
    return events, len(days) - 1


def excursion_stats(events) -> Dict[str, float]:
    if not events:
        return {}
    follow = [e.mfe_up if e.direction == "UP" else e.mfe_down for e, _ in events]
    against = [e.mfe_down if e.direction == "UP" else e.mfe_up for e, _ in events]
    return {
        "events": len(events),
        "follow_median": median(follow),
        "follow_mean": sum(follow) / len(follow),
        "against_median": median(against),
        "against_mean": sum(against) / len(against),
        "follow_bigger_pct": sum(1 for f, a in zip(follow, against) if f > a) / len(follow) * 100,
    }


def grid(events, side: str, stops: List[float], targets: List[float], exit_time: str):
    """Average index points per trade for each stop/target pair."""
    out = {}
    for stop in stops:
        for target in targets:
            results = [simulate(bars, e, side, stop, target, exit_time) for e, bars in events]
            points = [p for p, _ in results]
            wins = sum(1 for p in points if p > 0)
            out[(stop, target)] = {
                "avg": sum(points) / len(points) if points else 0.0,
                "total": sum(points),
                "win_rate": wins / len(points) * 100 if points else 0.0,
                "trades": len(points),
            }
    return out


def split_halves(events, side: str, stop: float, target: float, exit_time: str):
    mid = len(events) // 2
    halves = []
    for part in (events[:mid], events[mid:]):
        points = [simulate(bars, e, side, stop, target, exit_time)[0] for e, bars in part]
        halves.append(sum(points))
    return halves
