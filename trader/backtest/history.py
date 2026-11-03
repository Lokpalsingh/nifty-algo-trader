"""
Historical 1-minute NIFTY data.

Kotak allows 30 days of 1-minute data per request, so this fetches in chunks
and saves them to data/history/NIFTY_1min.csv. After the first download it
reads from that file, so backtests run offline and instantly.

You can also supply your own CSV with columns: datetime,open,high,low,close
(datetime like 2026-09-17 09:15:00).
"""
import csv
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List

from trader.clock import IST
from trader.config import DATA_DIR, INDEXES, VIX

log = logging.getLogger("history")
HISTORY_DIR = DATA_DIR / "history"
CHUNK_DAYS = 28          # Kotak's limit for 1-minute data is 30 days


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def csv_path(index: str = "NIFTY", interval: str = "1min") -> Path:
    return HISTORY_DIR / f"{index}_{interval}.csv"


def load_bars(index: str = "NIFTY", interval: str = "1min") -> List[Bar]:
    path = csv_path(index, interval)
    if not path.exists():
        return []
    bars = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            when = datetime.fromisoformat(row["datetime"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=IST)
            bars.append(Bar(when, float(row["open"]), float(row["high"]),
                            float(row["low"]), float(row["close"]),
                            float(row.get("volume") or 0)))
    bars.sort(key=lambda b: b.time)
    return bars


def save_bars(bars: List[Bar], index: str = "NIFTY", interval: str = "1min") -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = csv_path(index, interval)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([b.time.isoformat(), b.open, b.high, b.low, b.close, b.volume])
    return path


def download(broker, days: int, index: str = "NIFTY", interval: str = "1min") -> List[Bar]:
    """Fetch history from the broker in chunks and merge it with what's cached."""
    info = INDEXES[index]
    neosymbol = f"{info['quote_segment']}|{info['quote_token']}"
    api = broker._client()
    known: Dict[str, Bar] = {b.time.isoformat(): b for b in load_bars(index, interval)}
    today = date.today()
    start = today - timedelta(days=days)
    got_any = False

    chunk_start = start
    while chunk_start < today:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), today)
        try:
            resp = api.historical_data(neosymbol=neosymbol, interval=interval,
                                       from_date=chunk_start.isoformat(), to_date=chunk_end.isoformat())
        except Exception as e:
            log.warning("Download failed for %s..%s: %s", chunk_start, chunk_end, e)
            chunk_start = chunk_end
            continue
        candles = ((resp or {}).get("data") or {}).get("candles") or []
        for row in candles:
            when = datetime.fromisoformat(str(row[0]))
            if when.tzinfo is None:
                when = when.replace(tzinfo=IST)
            volume = float(row[5]) if len(row) > 5 else 0.0
            known[when.isoformat()] = Bar(when, float(row[1]), float(row[2]), float(row[3]),
                                          float(row[4]), volume)
        got_any = got_any or bool(candles)
        print(f"   {chunk_start} to {chunk_end}: {len(candles)} candles")
        chunk_start = chunk_end

    bars = sorted(known.values(), key=lambda b: b.time)
    if got_any:
        save_bars(bars, index, interval)
    return bars


def daily_csv_path(index: str = "NIFTY") -> Path:
    return HISTORY_DIR / f"{index}_D.csv"


def load_daily(index: str = "NIFTY") -> List[Bar]:
    """Daily candles saved by download_daily(), oldest first."""
    path = daily_csv_path(index)
    if not path.exists():
        return []
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            when = datetime.fromisoformat(row["date"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=IST)
            out.append(Bar(when, float(row["open"]), float(row["high"]),
                           float(row["low"]), float(row["close"])))
    out.sort(key=lambda b: b.time)
    return out


def download_daily(broker, years: int = 5, index: str = "NIFTY", pause: float = 2.0) -> Path:
    """Years of DAILY candles from the broker, in chunks, saved for the swing lab.

    Kotak refuses dates older than 5 years and rate-limits fast requests, so this
    clamps the start date, pauses between chunks and retries when throttled. Any
    candles already saved are kept, so you can run it again to fill gaps."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    end = date.today()
    oldest_allowed = end - timedelta(days=5 * 365 - 20)        # stay inside the broker's limit
    start = max(end - timedelta(days=int(years * 365.25)), oldest_allowed)
    found: Dict[str, Bar] = {b.time.date().isoformat(): b for b in load_daily(index)}
    if found:
        log.info("Keeping %d daily candles already saved", len(found))

    windows = []
    window_start = start
    while window_start < end:
        window_end = min(end, window_start + timedelta(days=170))
        windows.append((window_start, window_end))
        window_start = window_end + timedelta(days=1)

    for n, (w_start, w_end) in enumerate(windows, 1):
        # already have this stretch? don't spend a request on it
        weekdays = sum(1 for i in range((w_end - w_start).days + 1)
                       if (w_start + timedelta(days=i)).weekday() < 5)
        have = sum(1 for d in found if w_start.isoformat() <= d <= w_end.isoformat())
        if have >= weekdays * 0.9:
            print(f"   {n}/{len(windows)}  {w_start} to {w_end}: already have {have} candles, skipping")
            continue

        for attempt in range(4):
            try:
                rows = broker.get_daily_candles_between(index, w_start, w_end)
                for c in rows:
                    when = datetime.fromisoformat(c.date).replace(tzinfo=IST)
                    found[c.date] = Bar(when, c.open, c.high, c.low, c.close)
                print(f"   {n}/{len(windows)}  {w_start} to {w_end}: {len(rows)} candles")
                break
            except Exception as e:
                text = str(e).lower()
                busy = ("429" in text or "rate limit" in text or "503" in text
                        or "circuit breaker" in text or "temporarily unavailable" in text)
                if busy and attempt < 3:
                    wait = max(pause, 30.0) * (attempt + 1)      # the breaker needs real time
                    print(f"   {n}/{len(windows)}  broker is throttling, waiting {wait:.0f}s "
                          f"(try {attempt + 2} of 4)")
                    time.sleep(wait)
                    continue
                print(f"   {n}/{len(windows)}  {w_start} to {w_end}: FAILED ({e})")
                break
        time.sleep(pause)

    # save as we go, so a later failure never loses what was fetched
    path = daily_csv_path(index)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close"])
        for day in sorted(found):
            b = found[day]
            writer.writerow([day, b.open, b.high, b.low, b.close])
    log.info("Saved %d daily candles to %s", len(found), path)
    return path


def group_by_day(bars: List[Bar]) -> Dict[str, List[Bar]]:
    days: Dict[str, List[Bar]] = {}
    for b in bars:
        days.setdefault(b.time.date().isoformat(), []).append(b)
    return days


# --------------------------------------------------------------------------- #
# India VIX (daily). Used by the backtest as each day's option volatility.
# --------------------------------------------------------------------------- #
VIX_CHUNK_DAYS = 170     # Kotak allows 180 days of daily candles per request


def vix_csv_path() -> Path:
    return HISTORY_DIR / "INDIAVIX_D.csv"


def load_vix() -> Dict[str, float]:
    """{"2026-09-17": 13.42, ...} using each day's opening VIX (known at 09:15)."""
    path = vix_csv_path()
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {row["date"]: float(row["open"]) for row in csv.DictReader(f) if float(row["open"]) > 0}


def download_vix(broker, days: int) -> Dict[str, float]:
    """Daily India VIX from Kotak, or from Yahoo Finance if Kotak has none."""
    today = date.today()
    start = today - timedelta(days=days)
    found: Dict[str, float] = {}
    try:
        api = broker._client()
        chunk_start = start
        while chunk_start < today:
            chunk_end = min(chunk_start + timedelta(days=VIX_CHUNK_DAYS), today)
            resp = api.historical_data(neosymbol=f"{VIX['quote_segment']}|{VIX['quote_token']}", interval="D",
                                       from_date=chunk_start.isoformat(), to_date=chunk_end.isoformat())
            for row in ((resp or {}).get("data") or {}).get("candles") or []:
                if float(row[1]) > 0:
                    found[str(row[0])[:10]] = float(row[1])
            chunk_start = chunk_end
    except Exception as e:
        log.warning("India VIX history from Kotak failed: %s", e)

    if not found:
        try:
            import yfinance as yf
            hist = yf.Ticker(VIX["yahoo_symbol"]).history(start=start.isoformat(), interval="1d")
            for ts, row in hist.iterrows():
                if float(row["Open"]) > 0:
                    found[ts.date().isoformat()] = float(row["Open"])
            source = "Yahoo Finance"
        except Exception as e:
            log.warning("India VIX history from Yahoo failed: %s", e)
            source = ""
    else:
        source = "Kotak"

    if found:
        merged = {**load_vix(), **found}
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        with vix_csv_path().open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "open"])
            for day in sorted(merged):
                writer.writerow([day, merged[day]])
        print(f"   India VIX: {len(found)} days from {source}")
    else:
        print("   India VIX: not available, the backtest will use the fixed --iv instead")
    return load_vix()
