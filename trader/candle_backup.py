"""
Backup source for yesterday's daily candle, used only if the broker's own
historical data call fails. Uses Yahoo Finance (free, no login needed).
If this also fails, the dashboard asks you to type the candle in by hand.
"""
import logging
from datetime import date
from typing import List, Optional

from trader.models import Candle

log = logging.getLogger("candle_backup")


def yahoo_previous_day_candle(symbol: str, today: date) -> Optional[Candle]:
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed, no backup candle source")
        return None
    try:
        hist = yf.Ticker(symbol).history(period="15d", interval="1d")
    except Exception as e:
        log.warning("Yahoo daily candle failed: %s", e)
        return None
    best = None
    for ts, row in hist.iterrows():
        day = ts.date().isoformat()
        if day < today.isoformat():
            best = Candle(day, float(row["Open"]), float(row["High"]),
                          float(row["Low"]), float(row["Close"]), "Yahoo Finance (backup)")
    return best


def yahoo_daily_candles(symbol: str, today: date, days: int = 25) -> List[Candle]:
    """Recent completed daily candles, oldest first. Backup for the narrow-day filter."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed, no backup daily history")
        return []
    try:
        hist = yf.Ticker(symbol).history(period=f"{max(days * 2, 40)}d", interval="1d")
    except Exception as e:
        log.warning("Yahoo daily history failed: %s", e)
        return []
    out = []
    for ts, row in hist.iterrows():
        day = ts.date().isoformat()
        if day >= today.isoformat():
            continue
        out.append(Candle(day, float(row["Open"]), float(row["High"]),
                          float(row["Low"]), float(row["Close"]), "Yahoo Finance (backup)"))
    return out[-days:]


def yahoo_last_close(symbol: str) -> Optional[float]:
    """Latest close for a Yahoo symbol, e.g. ^INDIAVIX. Backup only."""
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="5d", interval="1d")
        if len(hist):
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.warning("Yahoo last close for %s failed: %s", symbol, e)
    return None
