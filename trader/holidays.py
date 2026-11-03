"""
NSE trading holidays.

Why this exists: the autopilot only checked the weekday, so on Dussehra it woke the
Mac, logged in to Kotak, found no data and reported a problem that was not one. The
status screen had the mirror-image fault - 20 Oct 2026 was a market holiday and it
counted as a day the bot ran, while a genuinely missed Monday sat next to it looking
the same.

The safe direction is deliberate. A day this file does not know about is treated as a
NORMAL TRADING DAY, so a missing or out-of-date list can only ever cost a wasted login
- never a skipped trading day. Add new years as NSE publishes them; nothing else needs
to change.

Full-day equity holidays only. Muhurat trading (the one-hour Diwali evening session) is
not here: it is not a 09:15 session and this strategy has no business in it.
"""
from datetime import date
from typing import Dict, Optional

# Published by NSE; 2026 cross-checked against two sources and against the gaps in
# data/history/NIFTY_D.csv, which is missing 2 Oct and 20 Oct exactly as listed.
# Holidays that fall on a Saturday or Sunday are left out - they remove no trading day.
HOLIDAYS: Dict[str, str] = {
    # 2026
    "2026-01-15": "Municipal Corporation Election, Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}

# Years this file actually covers. A date in any other year gets no opinion.
KNOWN_YEARS = {int(day[:4]) for day in HOLIDAYS}


def holiday_name(day: date) -> Optional[str]:
    """The holiday's name, or None if the market was open (or we do not know)."""
    return HOLIDAYS.get(day.isoformat())


def have_calendar_for(day: date) -> bool:
    """False means this file has no list for that year, so treat every weekday as open."""
    return day.year in KNOWN_YEARS


def is_trading_day(day: date) -> bool:
    """Monday to Friday, and not a holiday we know about."""
    if day.weekday() >= 5:
        return False
    return holiday_name(day) is None


def why_closed(day: date) -> str:
    """A short phrase for a log line or a status screen. Empty when the market is open."""
    if day.weekday() >= 5:
        return "weekend"
    name = holiday_name(day)
    if name:
        return name
    return ""


def next_trading_day(day: date) -> date:
    """The next day the market is open, starting the day after `day`."""
    from datetime import timedelta
    nxt = day + timedelta(days=1)
    for _ in range(30):                      # a month is far more than any NSE break
        if is_trading_day(nxt):
            return nxt
        nxt += timedelta(days=1)
    return nxt
