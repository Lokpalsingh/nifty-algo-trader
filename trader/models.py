"""Plain data objects shared by every broker and the strategy."""
from dataclasses import asdict, dataclass, fields
from typing import Optional


def round_to_tick(price: float, tick: float = 0.05) -> float:
    """Option prices on NSE move in steps of 0.05."""
    return max(tick, round(round(price / tick) * tick, 2))


@dataclass
class Candle:
    date: str
    open: float
    high: float
    low: float
    close: float
    source: str = ""

    @property
    def color(self) -> str:
        if self.close > self.open:
            return "GREEN"
        if self.close < self.open:
            return "RED"
        return "DOJI"


@dataclass
class OptionContract:
    symbol: str          # trading symbol, e.g. NIFTY26SEP24500PE
    token: str           # broker instrument token
    segment: str         # e.g. nse_fo
    strike: float
    option_type: str     # CE / PE
    expiry: str          # YYYY-MM-DD
    lot_size: int

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


# Broker-neutral order vocabulary. Each broker adapter translates these.
BUY, SELL = "BUY", "SELL"
LIMIT, STOP_LIMIT, MARKET = "LIMIT", "SL", "MARKET"

OPEN, COMPLETE, REJECTED, CANCELLED = "OPEN", "COMPLETE", "REJECTED", "CANCELLED"


@dataclass
class OrderStatus:
    order_id: str
    status: str             # OPEN / COMPLETE / REJECTED / CANCELLED
    filled_qty: int = 0
    avg_price: float = 0.0
    message: str = ""

    @property
    def is_final(self) -> bool:
        return self.status in (COMPLETE, REJECTED, CANCELLED)


@dataclass
class Position:
    contract: OptionContract
    qty: int
    entry_price: float
    sl_trigger: float
    entry_time: str
    sl_order_id: Optional[str] = None
    trade_no: int = 0
    high_price: float = 0.0        # highest option price seen since entry (for trailing)

    def to_dict(self):
        d = asdict(self)
        d["contract"] = self.contract.to_dict()
        return d

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in dict(d).items() if k in known}   # ignore fields from older versions
        d["contract"] = OptionContract.from_dict(d["contract"])
        return cls(**d)
