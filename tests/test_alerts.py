"""
Does an alert actually reach the phone?

This matters more than it looks. The autopilot runs with nobody watching, so the
Telegram message IS the product: if it silently doesn't go out, a losing week
looks identical to a quiet one. Every test here failed before the fix.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.alerts import CRITICAL, INFO, TRADE, AlertHub


class FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class RecordingHub(AlertHub):
    """Same hub, but the Telegram call is recorded instead of made. Sends still run
    on their own threads, so flush() is exercised exactly as it is in production."""

    def __init__(self, *a, status_code=200, **kw):
        super().__init__(*a, **kw)
        self.sent = []
        self._status = status_code

    def _send_telegram(self, text):
        for part in self._split(text):
            self.sent.append(part)
            if self._status != 200:
                self.telegram_error = f"Telegram replied {self._status}"
                return
        self.telegram_error = ""


def hub(**kw):
    return RecordingHub("token", "chat-id", **kw)


class TelegramDelivery(unittest.TestCase):
    def test_info_stays_on_the_dashboard(self):
        h = hub()
        h.add(INFO, "engine polled the price")
        h.flush()
        self.assertEqual(h.sent, [], "routine INFO should not buzz the phone")
        self.assertEqual(len(h.recent()), 1, "but it should still be on the dashboard")

    def test_info_with_push_is_sent(self):
        """The autopilot's morning line and daily report are INFO, and must arrive."""
        h = hub()
        h.add(INFO, "Engine started for 15 Oct.", push=True)
        self.assertTrue(h.flush())
        self.assertEqual(len(h.sent), 1)
        self.assertIn("Engine started", h.sent[0])

    def test_trades_and_problems_are_sent_without_asking(self):
        h = hub()
        h.add(TRADE, "bought NIFTY 25000 CE")
        h.add(CRITICAL, "login failed")
        h.flush()
        self.assertEqual(len(h.sent), 2)

    def test_nothing_is_sent_without_credentials(self):
        h = RecordingHub("", "")
        h.add(CRITICAL, "login failed")
        h.flush()
        self.assertEqual(h.sent, [])

    def test_long_report_is_split_not_rejected(self):
        """Telegram refuses anything over 4096 characters. A week's report can
        exceed that, and the refusal used to be invisible."""
        report = "\n".join(f"line {i:04d} " + "x" * 60 for i in range(200))
        self.assertGreater(len(report), 4096)
        h = hub()
        h.add(INFO, report, push=True)
        h.flush()
        self.assertGreater(len(h.sent), 1, "should have gone out in parts")
        for part in h.sent:
            self.assertLessEqual(len(part), AlertHub.LIMIT)
        # Nothing lost: every line survives somewhere in the parts.
        joined = "\n".join(h.sent)
        for i in (0, 99, 199):
            self.assertIn(f"line {i:04d}", joined)

    def test_split_handles_a_wall_of_text_with_no_newlines(self):
        body = "y" * (AlertHub.LIMIT * 2 + 7)
        h = hub()
        h.add(INFO, body, push=True)
        h.flush()
        self.assertEqual(sum(p.count("y") for p in h.sent), len(body), "no characters dropped")
        for part in h.sent:
            self.assertLessEqual(len(part), AlertHub.LIMIT)

    def test_flush_waits_for_the_sender(self):
        """Without this, a short-lived process exits and kills the daemon thread
        mid-request: the message is never delivered and nothing reports a problem."""
        import threading
        started, release = threading.Event(), threading.Event()

        class SlowHub(RecordingHub):
            def _send_telegram(self, text):
                started.set()
                release.wait(5)
                super()._send_telegram(text)

        h = SlowHub("token", "chat")
        h.add(INFO, "the day's report", push=True)
        self.assertTrue(started.wait(2))
        self.assertEqual(h.sent, [], "send is still in flight")
        release.set()
        self.assertTrue(h.flush(), "flush should wait for it")
        self.assertEqual(len(h.sent), 1)

    def test_flush_gives_up_and_says_so(self):
        import threading
        release = threading.Event()

        class StuckHub(RecordingHub):
            def _send_telegram(self, text):
                release.wait(10)

        h = StuckHub("token", "chat")
        h.add(INFO, "stuck", push=True)
        self.assertFalse(h.flush(timeout=0.3), "a hung send must be reported, not assumed sent")
        release.set()

    def test_failure_is_recorded(self):
        h = hub(status_code=429)
        h.add(INFO, "report", push=True)
        h.flush()
        self.assertIn("429", h.telegram_error)

    def test_flush_is_safe_with_nothing_pending(self):
        h = hub()
        self.assertTrue(h.flush())


if __name__ == "__main__":
    unittest.main(verbosity=2)
