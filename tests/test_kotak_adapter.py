"""
Checks the Kotak adapter reads Kotak's replies correctly, using the sample
responses published in Kotak's SDK docs. Uses a fake SDK, so no login needed.
"""
import sys
import types
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.brokers.base import BrokerError
from trader.brokers.kotak_neo import KotakNeoBroker
from trader.config import Secrets
from trader.models import BUY, COMPLETE, OPEN, SELL, STOP_LIMIT, OptionContract


class FakeNeoAPI:
    last = None

    def __init__(self, **kwargs):
        FakeNeoAPI.last = self
        self.calls = []
        self.order_rows = [
            {"ordSt": "open", "fldQty": 0, "avgPrc": "0.00", "updRecvTm": 1},
            {"ordSt": "complete", "fldQty": 65, "avgPrc": "123.45", "updRecvTm": 3},
            {"ordSt": "open pending", "fldQty": 0, "avgPrc": "0.00", "updRecvTm": 2},
        ]

    def totp_login(self, **k):
        self.calls.append(("totp_login", k))
        return {"data": {"token": "x"}}

    def totp_validate(self, **k):
        self.calls.append(("totp_validate", k))
        return {"data": {"token": "y"}}

    def quotes(self, instrument_tokens, quote_type):
        self.calls.append(("quotes", instrument_tokens, quote_type))
        token = instrument_tokens[0]["instrument_token"]
        return [{"exchange_token": token, "ltp": "24512.3500" if token == "Nifty 50" else "166.7500"}]

    def historical_data(self, neosymbol, interval, from_date, to_date):
        self.calls.append(("historical_data", neosymbol, interval))
        return {"status": "success", "interval": "D", "data": {"candles": [
            ["2026-09-14T09:15:00+0530", 24300, 24420, 24280, 24400, 0, 0],
            ["2026-09-15T09:15:00+0530", 24550, 24600, 24400, 24450, 0, 0],
            ["2026-09-16T09:15:00+0530", 24460, 24480, 24410, 24470, 0, 0],  # today's partial candle
        ]}}

    def expiries(self, exchange, underlying, instrument_type=None):
        return {"exchange": exchange, "underlying": underlying,
                "expiries": ["2026-09-16", "2026-09-22", "2026-09-29"]}

    def option_chain(self, exchange, underlying, expiry=None, instrument_type=None, count=None):
        self.calls.append(("option_chain", expiry))
        def row(strike, t):
            return {"instrument": {"neoSymbol": f"nse_fo|{strike}{t}", "symbol": f"NIFTY26SEP{strike}{t}",
                                   "optionType": t, "strikePrice": str(strike)},
                    "quote": {"ltp": "100.0"}}
        strikes = [24400, 24450, 24500, 24550, 24600]
        return {"data": {"common_data": {"mktLot": "65", "exSeg": "nse_fo", "expiryDt": expiry},
                         "call": [row(s, "CE") for s in strikes], "put": [row(s, "PE") for s in strikes]}}

    def place_order(self, **k):
        self.calls.append(("place_order", k))
        return {"stat": "Ok", "nOrdNo": "250922000001", "stCode": 200}

    def order_history(self, order_id):
        return {"data": {"stat": "Ok", "stCode": 200, "data": self.order_rows}}

    def cancel_order(self, order_id):
        return {"stCode": 1021, "errMsg": "order is completed", "status_code": 400}

    def limits(self):
        return {"Net": "250000.50", "stCode": 200, "stat": "Ok"}

    def positions(self):
        return {"stat": "ok", "stCode": 200, "data": [
            {"tok": "71472", "cfBuyQty": "0", "flBuyQty": "130", "cfSellQty": "0", "flSellQty": "65", "lotSz": "65"}
        ]}


class KotakAdapterTest(unittest.TestCase):
    def setUp(self):
        fake = types.ModuleType("neo_api_client")
        fake.NeoAPI = FakeNeoAPI
        sys.modules["neo_api_client"] = fake
        secrets = Secrets(kotak_consumer_key="ck", kotak_mobile_number="+919999999999",
                          kotak_ucc="U1", kotak_mpin="123456")
        self.b = KotakNeoBroker(secrets, product="MIS")
        self.b.login("123456")
        self.api = FakeNeoAPI.last

    def test_index_ltp_uses_index_name(self):
        self.assertAlmostEqual(self.b.get_index_ltp("NIFTY"), 24512.35)
        self.assertEqual(self.api.calls[-1][1][0], {"instrument_token": "Nifty 50", "exchange_segment": "nse_cm"})

    def test_previous_day_candle_skips_today(self):
        c = self.b.get_previous_day_candle("NIFTY", date(2026, 9, 16))
        self.assertEqual(c.date, "2026-09-15")
        self.assertEqual(c.color, "RED")
        self.assertEqual((c.high, c.low), (24600, 24400))

    def test_atm_option_and_lot_size(self):
        c = self.b.find_atm_option("NIFTY", 24512.35, "PE", date(2026, 9, 17), "nearest")
        self.assertEqual(c.strike, 24500)
        self.assertEqual(c.token, "24500PE")
        self.assertEqual(c.symbol, "NIFTY26SEP24500PE")
        self.assertEqual(c.lot_size, 65)
        self.assertEqual(c.expiry, "2026-09-22")

    def test_expiry_day_switch(self):
        c = self.b.find_atm_option("NIFTY", 24500, "CE", date(2026, 9, 16), "next_on_expiry_day")
        self.assertEqual(c.expiry, "2026-09-22")
        c = self.b.find_atm_option("NIFTY", 24500, "CE", date(2026, 9, 16), "nearest")
        self.assertEqual(c.expiry, "2026-09-16")

    def test_place_stop_loss_order_fields(self):
        c = OptionContract("NIFTY26SEP24500PE", "71472", "nse_fo", 24500, "PE", "2026-09-22", 65)
        oid = self.b.place_order(c, SELL, 65, STOP_LIMIT, 98.40, trigger_price=103.40, tag="t")
        self.assertEqual(oid, "250922000001")
        k = self.api.calls[-1][1]
        self.assertEqual(k["transaction_type"], "S")
        self.assertEqual(k["order_type"], "SL")
        self.assertEqual(k["trigger_price"], "103.40")
        self.assertEqual(k["price"], "98.40")
        self.assertEqual(k["quantity"], "65")
        self.assertEqual(k["product"], "MIS")
        self.assertEqual(k["exchange_segment"], "nse_fo")

    def test_buy_maps_to_B(self):
        c = OptionContract("S", "1", "nse_fo", 1, "CE", "", 65)
        self.b.place_order(c, BUY, 65, "LIMIT", 100)
        self.assertEqual(self.api.calls[-1][1]["transaction_type"], "B")
        self.assertEqual(self.api.calls[-1][1]["order_type"], "L")

    def test_order_status_uses_latest_update(self):
        st = self.b.get_order_status("1")
        self.assertEqual(st.status, COMPLETE)
        self.assertEqual(st.filled_qty, 65)
        self.assertAlmostEqual(st.avg_price, 123.45)

    def test_order_status_open(self):
        self.api.order_rows = [{"ordSt": "trigger pending", "fldQty": 0, "avgPrc": "0", "updRecvTm": 5}]
        self.assertEqual(self.b.get_order_status("1").status, OPEN)

    def test_cancel_already_completed_is_ok(self):
        self.b.cancel_order("1")   # must not raise

    def test_capital_and_positions(self):
        self.assertAlmostEqual(self.b.get_available_capital(), 250000.50)
        c = OptionContract("S", "71472", "nse_fo", 1, "CE", "", 65)
        self.assertEqual(self.b.get_open_quantity(c), 65)

    def test_option_chain_other_shapes_and_lot_size_fallbacks(self):
        # lot size inside each row, chain wrapped one level deeper
        def chain_nested(**k):
            rows = [{"instrument": {"neoSymbol": "nse_fo|555", "symbol": "NIFTY26SEP23300CE",
                                    "strikePrice": "23300", "lotSize": "65"}}]
            return {"data": {"data": {"call": rows, "put": []}}}
        self.api.option_chain = chain_nested
        c = self.b.find_atm_option("NIFTY", 23287.9, "CE", date(2026, 9, 17), "nearest")
        self.assertEqual((c.token, c.lot_size, c.strike), ("555", 65, 23300))

        # no lot size anywhere in the chain -> scrip search backup
        def chain_no_lot(**k):
            return {"data": {"call": [{"instrument": {"neoSymbol": "nse_fo|777", "symbol": "NIFTY26SEP23300CE",
                                                       "strikePrice": "23300"}}]}}
        self.api.option_chain = chain_no_lot
        self.api.search_scrip = lambda **k: [{"pTrdSymbol": "NIFTY26SEP23300CE", "lLotSize": 65}]
        c = self.b.find_atm_option("NIFTY", 23287.9, "CE", date(2026, 9, 17), "nearest")
        self.assertEqual(c.lot_size, 65)

    def test_real_kotak_reply_shape_picks_true_atm(self):
        """Shape copied from a real Kotak reply: top-level common_data, 'inst', 'strkPrc'."""
        def real_chain(**k):
            def row(strike, t, token):
                return {"inst": {"neoSymbol": f"nse_fo|{token}", "symbol": f"NIFTY26922{strike}{t}",
                                 "optType": t, "strkPrc": str(strike), "exp": "2026-09-22", "moneyness": "itm"},
                        "quote": {"ltp": "100.0"}, "oi": {"cur": "1"}}
            strikes = list(range(22300, 24300, 50))
            return {"common_data": {"mktLot": "65", "multiplier": "1", "expiryDt": "2026-09-22",
                                    "unlSymbol": "NIFTY", "exSeg": "nse_fo"},
                    "call": [row(x, "CE", 50000 + n) for n, x in enumerate(strikes)],
                    "put": [row(x, "PE", 60000 + n) for n, x in enumerate(strikes)]}
        self.api.option_chain = real_chain
        ce = self.b.find_atm_option("NIFTY", 23288.2, "CE", date(2026, 9, 17), "nearest")
        pe = self.b.find_atm_option("NIFTY", 23288.2, "PE", date(2026, 9, 17), "nearest")
        self.assertEqual((ce.strike, ce.symbol, ce.lot_size), (23300, "NIFTY2692223300CE", 65))
        self.assertEqual((pe.strike, pe.symbol), (23300, "NIFTY2692223300PE"))

    def test_refuses_when_atm_strike_missing(self):
        def far_chain(**k):
            return {"common_data": {"mktLot": "65"},
                    "call": [{"inst": {"neoSymbol": "nse_fo|1", "symbol": "NIFTY2692222300CE",
                                       "optType": "CE", "strkPrc": "22300"}}]}
        self.api.option_chain = far_chain
        with self.assertRaises(BrokerError):
            self.b.find_atm_option("NIFTY", 23288.2, "CE", date(2026, 9, 17), "nearest")

    def test_option_chain_error_shows_reply(self):
        self.api.option_chain = lambda **k: {"Error": "Unexpected response format", "StatusCode": 500}
        with self.assertRaises(BrokerError) as ctx:
            self.b.find_atm_option("NIFTY", 23287.9, "CE", date(2026, 9, 17), "nearest")
        self.assertIn("Unexpected response format", str(ctx.exception))

    def test_positions_no_data_means_zero(self):
        self.api.positions = lambda: {"stCode": 5203, "errMsg": "No Data", "desc": "data not found",
                                      "stat": "Not_Ok"}
        c = OptionContract("S", "71472", "nse_fo", 1, "CE", "", 65)
        self.assertEqual(self.b.get_open_quantity(c), 0)

    def test_negative_funds_means_unknown_capital(self):
        self.api.limits = lambda: {"Net": "-23.67", "stat": "Ok"}
        self.assertIsNone(self.b.get_available_capital())

    def test_rejects_quote_for_wrong_instrument(self):
        self.api.quotes = lambda **k: [{"exchange_token": "99999", "ltp": "116.65"}]
        c = OptionContract("NIFTY2692223300PE", "56994", "nse_fo", 23300, "PE", "2026-09-22", 65)
        with self.assertRaises(BrokerError):
            self.b.get_option_ltp(c)

    def test_exact_strike_lookup_for_wings(self):
        c = self.b.get_option("NIFTY", 24600, "CE", date(2026, 9, 17), "nearest")
        self.assertEqual((c.symbol, c.token, c.strike, c.lot_size), ("NIFTY26SEP24600CE", "24600CE", 24600, 65))
        with self.assertRaises(BrokerError):
            self.b.get_option("NIFTY", 25500, "CE", date(2026, 9, 17), "nearest")   # not in the chain

    def test_option_chain_is_cached_but_refreshed_on_a_miss(self):
        calls = []
        original = self.api.option_chain
        self.api.option_chain = lambda **k: calls.append(1) or original(**k)
        self.b.get_option("NIFTY", 24500, "CE", date(2026, 9, 17), "nearest")
        self.b.get_option("NIFTY", 24550, "PE", date(2026, 9, 17), "nearest")
        self.assertEqual(len(calls), 1)                       # same expiry -> one chain call
        with self.assertRaises(BrokerError):
            self.b.get_option("NIFTY", 30000, "PE", date(2026, 9, 17), "nearest")
        self.assertEqual(len(calls), 2)                       # a miss refreshes once

    def test_batch_prices_in_one_call(self):
        calls = []

        def quotes(instrument_tokens, quote_type):
            calls.append(len(instrument_tokens))
            return [{"exchange_token": t["instrument_token"], "ltp": str(100 + i)}
                    for i, t in enumerate(instrument_tokens)]
        self.api.quotes = quotes
        legs = [OptionContract(f"S{i}", str(i), "nse_fo", 0, "CE", "", 65) for i in range(12)]
        prices = self.b.get_option_ltps(legs)
        self.assertEqual(calls, [12])
        self.assertEqual(prices["0"], 100.0)
        self.assertEqual(prices["11"], 111.0)

    def test_batch_prices_refuse_missing_instrument(self):
        self.api.quotes = lambda **k: [{"exchange_token": "1", "ltp": "10"}]
        legs = [OptionContract("A", "1", "nse_fo", 0, "CE", "", 65), OptionContract("B", "2", "nse_fo", 0, "PE", "", 65)]
        with self.assertRaises(BrokerError):
            self.b.get_option_ltps(legs)

    def test_index_open_from_ohlc_quote(self):
        self.api.quotes = lambda **k: [{"exchange_token": "Nifty 50", "ohlc": {"open": "23195.25", "close": "23217.6"}}]
        self.assertAlmostEqual(self.b.get_index_open("NIFTY"), 23195.25)

    def test_vix_uses_india_vix_token(self):
        seen = []

        def quotes(instrument_tokens, quote_type):
            seen.append(instrument_tokens[0]["instrument_token"])
            return [{"exchange_token": "India VIX", "ltp": "13.42"}]
        self.api.quotes = quotes
        self.assertAlmostEqual(self.b.get_vix(), 13.42)
        self.assertEqual(seen, ["INDIA VIX"])

    def test_open_option_positions(self):
        self.api.positions = lambda: {"stat": "ok", "stCode": 200, "data": [
            {"exSeg": "nse_fo", "sym": "NIFTY", "optTp": "CE", "trdSym": "NIFTY26SEP23000CE", "tok": "1",
             "cfBuyQty": "0", "flBuyQty": "0", "cfSellQty": "0", "flSellQty": "65"},
            {"exSeg": "nse_fo", "sym": "NIFTY", "optTp": "PE", "trdSym": "NIFTY26SEP23000PE", "tok": "2",
             "cfBuyQty": "0", "flBuyQty": "65", "cfSellQty": "0", "flSellQty": "65"},      # closed
            {"exSeg": "nse_fo", "sym": "TCS", "optTp": "XX", "trdSym": "TCS26SEPFUT", "tok": "3",
             "cfBuyQty": "0", "flBuyQty": "175", "cfSellQty": "0", "flSellQty": "0"},
        ]}
        self.assertEqual(self.b.get_open_option_positions("NIFTY"),
                         [{"symbol": "NIFTY26SEP23000CE", "token": "1", "net_qty": -65}])

    def test_error_reply_raises(self):
        self.api.quotes = lambda **k: {"error": [{"message": "bad"}]}
        with self.assertRaises(BrokerError):
            self.b.get_index_ltp("NIFTY")

    # ----------------------------------------------------------------- #
    # Daily candles are asked for several times a morning. On 16 Oct 2026 the
    # health check, the narrow-day filter and the engine each fetched them inside
    # the same second and Kotak answered 429. Completed days cannot change, so
    # the answer is remembered for the day.
    # ----------------------------------------------------------------- #
    def _fake_daily_rows(self, count=15):
        rows, day = [], date.today() - timedelta(days=60)
        while len(rows) < count:
            if day.weekday() < 5:
                rows.append([f"{day.isoformat()}T09:15:00+0530", 100.0, 110.0, 90.0, 105.0, 0, 0])
            day += timedelta(days=1)
        return rows

    def _daily_requests(self):
        return sum(1 for c in self.api.calls if c[0] == "historical_data")

    def test_daily_candles_are_fetched_once_a_day(self):
        rows = self._fake_daily_rows()

        def historical_data(neosymbol, interval, from_date, to_date):
            self.api.calls.append(("historical_data", neosymbol, interval))
            return {"status": "success", "data": {"candles": rows}}
        self.api.historical_data = historical_data

        first = self.b.get_daily_candles("NIFTY", 20)
        self.assertEqual(len(first), 15)
        once = self._daily_requests()
        self.assertEqual(self.b.get_daily_candles("NIFTY", 20), first)
        self.b.get_daily_candles("NIFTY", 20)
        self.assertEqual(self._daily_requests(), once,
                         "repeat calls in the same day must not reach Kotak")

    def test_a_different_window_is_fetched_separately(self):
        rows = self._fake_daily_rows()
        self.api.historical_data = lambda **k: (
            self.api.calls.append(("historical_data", k["neosymbol"], k["interval"])),
            {"status": "success", "data": {"candles": rows}})[1]
        self.b.get_daily_candles("NIFTY", 20)
        once = self._daily_requests()
        self.b.get_daily_candles("NIFTY", 50)
        self.assertEqual(self._daily_requests(), once + 1, "a longer lookback is a real question")

    def test_a_bad_answer_is_not_remembered(self):
        """Caching a failure would freeze the day's data trouble in place until
        tomorrow. A short answer must be asked again."""
        import trader.candle_backup as backup
        original = backup.yahoo_daily_candles
        backup.yahoo_daily_candles = lambda *a, **k: []
        try:
            self.api.historical_data = lambda **k: (
                self.api.calls.append(("historical_data", k["neosymbol"], k["interval"])),
                {"status": "success", "data": {"candles": []}})[1]
            self.b.get_daily_candles("NIFTY", 20)
            before = self._daily_requests()
            self.b.get_daily_candles("NIFTY", 20)
            self.assertEqual(self._daily_requests(), before + 1,
                             "a failed answer must be retried, not cached")
        finally:
            backup.yahoo_daily_candles = original


if __name__ == "__main__":
    unittest.main()
