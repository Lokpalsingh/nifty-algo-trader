"""
Alerts show on the dashboard and, if configured, go to your Telegram.

Levels:
  INFO      - dashboard only, unless the caller asks with push=True
  TRADE     - entries and exits (dashboard + Telegram)
  WARNING   - something needs attention (dashboard + Telegram)
  CRITICAL  - act now, e.g. connection lost with an open position (dashboard + Telegram)
"""
import logging
import threading
import time as _time
from collections import deque
from datetime import datetime

log = logging.getLogger("alerts")

INFO, TRADE, WARNING, CRITICAL = "INFO", "TRADE", "WARNING", "CRITICAL"
_ICONS = {INFO: "ℹ️", TRADE: "💹", WARNING: "⚠️", CRITICAL: "🚨"}


class AlertHub:
    def __init__(self, telegram_token: str = "", telegram_chat_id: str = "", clock=None, max_items: int = 300):
        self.token = telegram_token
        self.chat_id = telegram_chat_id
        self.clock = clock
        self._items = deque(maxlen=max_items)
        self._lock = threading.Lock()
        self._senders = []
        self.telegram_error = ""

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def add(self, level: str, message: str, push: bool = False) -> None:
        """push=True also sends an INFO message to Telegram. Use it for things you want
        on your phone even though nothing is wrong: the morning start, the day's report.
        Without it, INFO stays on the dashboard, which nobody is watching at 15:20."""
        now = self.clock.now() if self.clock else datetime.now()
        item = {"time": now.strftime("%H:%M:%S"), "date": now.strftime("%Y-%m-%d"),
                "level": level, "message": message}
        with self._lock:
            self._items.appendleft(item)
        getattr(log, "warning" if level in (WARNING, CRITICAL) else "info")("%s %s", level, message)
        if (push or level != INFO) and self.telegram_enabled:
            sender = threading.Thread(
                target=self._send_telegram,
                args=(f"{_ICONS.get(level, '')} {message}".strip(),), daemon=True)
            with self._lock:
                self._senders = [s for s in self._senders if s.is_alive()]
                self._senders.append(sender)
            sender.start()

    def recent(self, limit: int = 100):
        with self._lock:
            return list(self._items)[:limit]

    def flush(self, timeout: float = 20.0) -> bool:
        """Wait for Telegram sends to finish, and say whether they did.

        Call this before a short-lived process exits. The senders are daemon threads,
        so quitting kills them mid-request and the message is never delivered - which
        is exactly how a 15:20 report can vanish without a single error anywhere."""
        deadline = _time.monotonic() + timeout
        with self._lock:
            pending = list(self._senders)
        for sender in pending:
            sender.join(max(0.0, deadline - _time.monotonic()))
        left = [s for s in pending if s.is_alive()]
        if left:
            log.warning("%d Telegram message(s) still sending after %.0fs", len(left), timeout)
        return not left

    # Telegram refuses anything over 4096 characters, so long reports go in pieces.
    LIMIT = 3500

    def _split(self, text: str):
        while text:
            if len(text) <= self.LIMIT:
                yield text
                return
            cut = text.rfind("\n", 0, self.LIMIT)
            yield text[:cut if cut > 0 else self.LIMIT]
            text = text[cut if cut > 0 else self.LIMIT:].lstrip("\n")

    def _send_telegram(self, text: str) -> None:
        try:
            import requests
            for part in self._split(text):
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                  json={"chat_id": self.chat_id, "text": part}, timeout=15)
                if r.status_code != 200:
                    self.telegram_error = f"Telegram replied {r.status_code}: {r.text[:200]}"
                    log.warning("Telegram send failed: %s", self.telegram_error)
                    return
            self.telegram_error = ""
        except Exception as e:
            self.telegram_error = f"Telegram send failed: {e}"
            log.warning("%s", self.telegram_error)
