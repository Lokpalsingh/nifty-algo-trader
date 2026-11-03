"""
Option prices for the backtest.

Kotak's history covers the NIFTY index, not the price of every expired option
contract, so option premiums here are CALCULATED with the Black-Scholes model
from the index price, time to expiry and an assumed volatility (IV).

This means entry and exit TIMING is realistic (it comes from real NIFTY data),
but the rupee amounts are an estimate. Real premiums move with changing IV,
demand and the bid-ask spread. Treat the P&L as indicative, not exact.
"""
import math
from datetime import date, datetime, time, timedelta

from trader.clock import IST

RISK_FREE = 0.065
MARKET_CLOSE = time(15, 30)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# NSE moved NIFTY weekly expiry from Thursday to Tuesday from 1 September 2025.
# Exchange holidays that move an expiry a day earlier are not modelled.
EXPIRY_DAY_SWITCH = date(2025, 9, 1)


def _is_expiry(d: date, weekday) -> bool:
    if weekday is not None:
        return d.weekday() == weekday
    return d.weekday() == (1 if d >= EXPIRY_DAY_SWITCH else 3)


def next_weekly_expiry(day: date, weekday: int = None, use_next_on_expiry_day: bool = False) -> date:
    """Next NIFTY weekly expiry on or after `day` (Thursday before Sep 2025, Tuesday after).
    Pass weekday (0=Monday ... 6=Sunday) to force a specific day."""
    expiry = day
    while not _is_expiry(expiry, weekday):
        expiry += timedelta(days=1)
    if expiry == day and use_next_on_expiry_day:
        expiry += timedelta(days=1)
        while not _is_expiry(expiry, weekday):
            expiry += timedelta(days=1)
    return expiry


def trading_days_to(today: date, expiry: date) -> int:
    """Weekdays after today up to and including expiry. 0 = expiry is today."""
    days, d = 0, today
    while d < expiry:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


MARKET_OPEN = time(9, 15)
TRADING_MINUTES_PER_DAY = 375
TIME_MODELS = ("calendar", "trading")


def years_to_expiry(now: datetime, expiry: date, time_model: str = "calendar") -> float:
    """Time left until expiry, as a fraction of a year.

    calendar: nights and weekends count like trading hours, so an option loses
              only about a quarter of a day's value during market hours.
    trading:  value is lost only while the market is open, so intraday decay is
              about four times faster. Real weekly options sit between the two.
    """
    if time_model == "trading":
        minutes = 0.0
        day = now.date()
        if day.weekday() < 5 and day <= expiry:
            start = max(now, datetime.combine(day, MARKET_OPEN, tzinfo=IST))
            end = datetime.combine(day, MARKET_CLOSE, tzinfo=IST)
            minutes += max(0.0, (end - start).total_seconds() / 60)
        day += timedelta(days=1)
        while day <= expiry:
            if day.weekday() < 5:
                minutes += TRADING_MINUTES_PER_DAY
            day += timedelta(days=1)
        return max(1.0, minutes) / (252 * TRADING_MINUTES_PER_DAY)
    end = datetime.combine(expiry, MARKET_CLOSE, tzinfo=IST)
    seconds = max(60.0, (end - now).total_seconds())
    return seconds / (365 * 24 * 3600)


def option_price(spot: float, strike: float, option_type: str, now: datetime,
                 expiry: date, iv: float, time_model: str = "calendar") -> float:
    """Black-Scholes price of a European option. iv is e.g. 0.14 for 14%."""
    return option_price_t(spot, strike, option_type, years_to_expiry(now, expiry, time_model), iv)


def option_price_t(spot: float, strike: float, option_type: str, t: float, iv: float) -> float:
    """Same as option_price, with the time to expiry (in years) already worked out."""
    if iv <= 0 or t <= 0:
        intrinsic = spot - strike if option_type == "CE" else strike - spot
        return max(0.05, round(intrinsic, 2))
    d1 = (math.log(spot / strike) + (RISK_FREE + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    discount = math.exp(-RISK_FREE * t)
    if option_type == "CE":
        price = spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    else:
        price = strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(0.05, round(price, 2))
