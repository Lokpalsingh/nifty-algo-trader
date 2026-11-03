"""
Trading costs for one buy+sell round trip of an index option.

Rates as of September 2026 (Budget 2026 raised options STT to 0.15% on the
sell side of premium from 1 April 2026). Charges change, and brokers differ,
so check your own contract note and adjust these if needed.
"""
from dataclasses import dataclass


@dataclass
class CostModel:
    brokerage_per_order: float = 20.0    # flat, per executed order
    stt_sell_pct: float = 0.15           # % of sell premium value
    exchange_pct: float = 0.03503        # % of premium value, both sides (NSE options)
    sebi_pct: float = 0.0001             # ₹10 per crore
    stamp_buy_pct: float = 0.003         # % of buy premium value
    gst_pct: float = 18.0                # on brokerage + exchange + sebi

    def round_trip(self, buy_price: float, sell_price: float, qty: int) -> float:
        buy_value = buy_price * qty
        sell_value = sell_price * qty
        turnover = buy_value + sell_value

        brokerage = 2 * self.brokerage_per_order
        stt = sell_value * self.stt_sell_pct / 100
        exchange = turnover * self.exchange_pct / 100
        sebi = turnover * self.sebi_pct / 100
        stamp = buy_value * self.stamp_buy_pct / 100
        gst = (brokerage + exchange + sebi) * self.gst_pct / 100
        return round(brokerage + stt + exchange + sebi + stamp + gst, 2)
