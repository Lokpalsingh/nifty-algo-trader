"""
Saves the day's progress to disk so that if the app or laptop restarts,
it remembers how many trades were taken, today's P&L and any open position.

  data/state_YYYY-MM-DD.json  - today's live state
  data/trades.csv             - every closed trade, forever
"""
import csv
import json
from pathlib import Path
from typing import Optional

from trader.config import DATA_DIR

TRADE_COLUMNS = ["date", "trade_no", "mode", "symbol", "option_type", "strike", "qty",
                 "entry_time", "entry_price", "exit_time", "exit_price", "pnl", "exit_reason",
                 "direction"]          # "long" = bought first ("short" kept for older files)


class Journal:
    def __init__(self, data_dir: Path = DATA_DIR, mode: str = "demo"):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode

    def _state_path(self, day: str) -> Path:
        return self.dir / f"state_{self.mode}_{day}.json"

    def load_state(self, day: str) -> Optional[dict]:
        path = self._state_path(day)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def save_state(self, day: str, state: dict) -> None:
        path = self._state_path(day)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _upgrade_file(self, path: Path) -> None:
        """Older versions wrote fewer columns. Rewrite the file once with the new header,
        so new rows line up; old rows are all strategy 1 trades, i.e. "long"."""
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames == TRADE_COLUMNS:
                return
            rows = list(reader)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
            writer.writeheader()
            for r in rows:
                r = {k: r.get(k, "") for k in TRADE_COLUMNS}
                r["direction"] = r["direction"] or "long"
                writer.writerow(r)
        tmp.replace(path)

    def record_trade(self, row: dict) -> None:
        path = self.dir / "trades.csv"
        new_file = not path.exists()
        if not new_file:
            self._upgrade_file(path)
        row = dict(row)
        row.setdefault("direction", "long")
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in TRADE_COLUMNS})

    def trades(self, day: Optional[str] = None, mode: Optional[str] = None):
        path = self.dir / "trades.csv"
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if day:
            rows = [r for r in rows if r["date"] == day]
        if mode:
            rows = [r for r in rows if r["mode"] == mode]
        return rows
