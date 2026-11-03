"""
Tests for the NSE holiday calendar.

The safety property is the one that matters: an unknown year must never cause a
trading day to be skipped. Getting a holiday wrong costs a wasted login; getting a
trading day wrong costs the day.

Two of the 2026 dates are checkable against reality rather than against a web page:
data/history/NIFTY_D.csv, downloaded from Kotak, has no candle for 2 Oct or 20 Oct.
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.holidays import (HOLIDAYS, have_calendar_for, holiday_name,
                             is_trading_day, next_trading_day, why_closed)


class TestKnownHolidays(unittest.TestCase):
    def test_dussehra_is_shut(self):
        """20 Oct 2026, a Tuesday. Confirmed by the gap in the Kotak daily file."""
        self.assertEqual(holiday_name(date(2026, 10, 20)), "Dussehra")
        self.assertFalse(is_trading_day(date(2026, 10, 20)))

    def test_gandhi_jayanti_is_shut(self):
        """2 Oct 2026, a Friday. Also missing from the Kotak daily file."""
        self.assertFalse(is_trading_day(date(2026, 10, 2)))

    def test_an_ordinary_monday_is_open(self):
        """19 Oct 2026 was a real trading day. The bot missed it because the Mac was
        asleep, and the status screen must keep saying so."""
        self.assertTrue(is_trading_day(date(2026, 10, 19)))
        self.assertIsNone(holiday_name(date(2026, 10, 19)))
        self.assertEqual(why_closed(date(2026, 10, 19)), "")

    def test_the_days_around_dussehra_are_open(self):
        self.assertTrue(is_trading_day(date(2026, 10, 21)))
        self.assertTrue(is_trading_day(date(2026, 10, 22)))

    def test_upcoming_holidays(self):
        for day, name in ((date(2026, 11, 10), "Diwali Balipratipada"),
                          (date(2026, 11, 24), "Prakash Gurpurb Sri Guru Nanak Dev"),
                          (date(2026, 12, 25), "Christmas")):
            self.assertEqual(holiday_name(day), name)

    def test_no_weekend_dates_are_listed(self):
        """A holiday on a Saturday removes no trading day, so listing it would only
        make the calendar look wrong when compared with NSE's own published table."""
        for day in HOLIDAYS:
            self.assertLess(date.fromisoformat(day).weekday(), 5, f"{day} is a weekend")

    def test_every_date_parses(self):
        for day in HOLIDAYS:
            date.fromisoformat(day)


class TestWeekends(unittest.TestCase):
    def test_saturday_and_sunday_are_shut(self):
        self.assertFalse(is_trading_day(date(2026, 10, 24)))
        self.assertFalse(is_trading_day(date(2026, 10, 25)))
        self.assertEqual(why_closed(date(2026, 10, 24)), "weekend")


class TestTheSafeDirection(unittest.TestCase):
    """The whole design rests on this: not knowing must never close the market."""

    def test_an_unknown_year_trades_on_every_weekday(self):
        self.assertFalse(have_calendar_for(date(2031, 6, 10)))
        self.assertTrue(is_trading_day(date(2031, 6, 10)))       # a Tuesday
        self.assertEqual(why_closed(date(2031, 6, 10)), "")

    def test_an_unknown_year_still_knows_weekends(self):
        self.assertFalse(is_trading_day(date(2031, 6, 14)))      # a Saturday

    def test_a_known_year_is_reported_as_known(self):
        self.assertTrue(have_calendar_for(date(2026, 3, 1)))

    def test_no_year_is_entirely_blanked_out(self):
        """A typo that closed a whole year would silently stop all trading."""
        for year in sorted({int(d[:4]) for d in HOLIDAYS}):
            weekdays = [date(year, m, d) for m in (1, 6, 12) for d in (7, 14, 21)]
            open_days = [d for d in weekdays if is_trading_day(d)]
            self.assertGreater(len(open_days), 5, f"{year} looks mostly shut")


class TestNextTradingDay(unittest.TestCase):
    def test_friday_rolls_to_monday(self):
        self.assertEqual(next_trading_day(date(2026, 10, 23)), date(2026, 10, 26))

    def test_it_steps_over_a_holiday(self):
        """19 Oct is a Monday, 20 Oct is Dussehra, so the next open day is the 21st."""
        self.assertEqual(next_trading_day(date(2026, 10, 19)), date(2026, 10, 21))

    def test_it_steps_over_a_holiday_against_a_weekend(self):
        """2 Oct 2026 is Gandhi Jayanti on a Friday, so the next open day is Monday."""
        self.assertEqual(next_trading_day(date(2026, 10, 1)), date(2026, 10, 5))

    def test_it_always_returns_something(self):
        self.assertIsInstance(next_trading_day(date(2026, 12, 24)), date)


if __name__ == "__main__":
    unittest.main(verbosity=2)
