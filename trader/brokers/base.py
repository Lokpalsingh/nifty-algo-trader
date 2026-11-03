"""
The common "language" every broker adapter must speak.

To add a new broker later (Zerodha, Angel One, ...), create a new file in this
folder with a class that implements these methods. The strategy, risk manager
and dashboard never change, because they only ever call these methods.
"""
from abc import ABC, abstractmethod
from datetime import date
from typing import Dict, List, Optional

from trader.models import Candle, OptionContract, OrderStatus


class BrokerError(Exception):
    """Something went wrong talking to the broker (network, login, rejected call)."""


class Broker(ABC):
    name: str = "base"
    is_simulated: bool = False   # True = orders never reach a real exchange

    # ---- session ----
    @abstractmethod
    def login(self, totp_code: Optional[str] = None) -> None: ...

    @abstractmethod
    def is_logged_in(self) -> bool: ...

    # ---- market data ----
    @abstractmethod
    def get_index_ltp(self, index: str) -> float: ...

    @abstractmethod
    def get_option_ltp(self, contract: OptionContract) -> float: ...

    @abstractmethod
    def get_previous_day_candle(self, index: str, today: date) -> Optional[Candle]: ...

    def get_daily_candles(self, index: str, days: int = 25) -> list:
        """Recent completed daily candles, oldest first. Used by the narrow-day filter."""
        return []

    def get_minute_candles(self, index: str, since_hhmm: str = "09:15"):
        """Today's 1-minute candles (with volume where the feed provides it).
        Used by indicator-based strategies. Brokers that can't supply them raise."""
        raise BrokerError(f"{self.name} cannot supply minute candles")

    @abstractmethod
    def find_atm_option(self, index: str, spot: float, option_type: str,
                        today: date, expiry_choice: str) -> OptionContract: ...

    # ---- extra market data (exact strikes, batch prices, VIX, positions) ----
    def get_expiry(self, index: str, today: date, expiry_choice: str) -> str:
        """The weekly expiry being traded, as YYYY-MM-DD."""
        raise BrokerError(f"{self.name} cannot list expiries")

    def get_option(self, index: str, strike: float, option_type: str,
                   today: date, expiry_choice: str) -> OptionContract:
        """The option at exactly this strike (not the nearest one)."""
        raise BrokerError(f"{self.name} cannot look up options by strike")

    def get_option_ltps(self, contracts: List[OptionContract]) -> Dict[str, float]:
        """Prices for several options at once, keyed by token. Brokers that can
        fetch in one call should override this; it matters when 12 legs are open."""
        return {c.token: self.get_option_ltp(c) for c in contracts}

    def get_index_open(self, index: str) -> Optional[float]:
        """Today's opening value of the index, or None if unknown."""
        return None

    def get_vix(self) -> Optional[float]:
        """India VIX, or None if it can't be read."""
        return None

    def get_open_option_positions(self, index: str) -> List[dict]:
        """Open option positions on this index at the broker:
        [{"symbol": ..., "token": ..., "net_qty": ...}]. Used after a restart."""
        return []

    # ---- orders & account ----
    @abstractmethod
    def place_order(self, contract: OptionContract, side: str, qty: int, order_type: str,
                    price: float, trigger_price: float = 0.0, tag: str = "") -> str:
        """Returns the order id. side: BUY/SELL, order_type: LIMIT/SL/MARKET."""

    def modify_order(self, order_id: str, contract: OptionContract, qty: int, order_type: str,
                     price: float, trigger_price: float = 0.0) -> str:
        """Change an existing order (used to move a trailing stop-loss up).
        Brokers that can't modify raise BrokerError; the strategy then cancels
        and places a fresh order instead."""
        raise BrokerError(f"{self.name} cannot modify orders")

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderStatus: ...

    @abstractmethod
    def get_available_capital(self) -> Optional[float]: ...

    @abstractmethod
    def get_open_quantity(self, contract: OptionContract) -> int:
        """Net open quantity (in units, not lots) for this contract."""
