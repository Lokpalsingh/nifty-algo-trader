"""
Kotak Neo adapter, written for the official SDK `kotakneoapi` v3
(imported as `neo_api_client`). Method names and response fields follow
Kotak's docs: https://github.com/Kotak-Neo/kotak-neo-python/tree/main/docs

This is the ONLY file that knows Kotak's field names ("ltp", "nOrdNo",
"ordSt", "mktLot", ...). If Kotak changes something, fix it here.

Before trusting it, run `python check_kotak.py` once. It calls each read-only
method below and prints Kotak's raw replies, so you can see they match.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from trader.brokers.base import Broker, BrokerError
from trader.config import INDEXES, VIX, Secrets
from trader.models import (
    BUY, CANCELLED, COMPLETE, LIMIT, MARKET, OPEN, REJECTED, SELL, STOP_LIMIT,
    Candle, OptionContract, OrderStatus,
)

log = logging.getLogger("kotak")

_SIDE = {BUY: "B", SELL: "S"}
_ORDER_TYPE = {LIMIT: "L", STOP_LIMIT: "SL", MARKET: "MKT"}


NO_DATA_CODE = 5203   # Kotak's "No Data" reply, e.g. positions when you have none


def _short(resp: Any, limit: int = 600) -> str:
    text = str(resp)
    return text if len(text) <= limit else text[:limit] + " ..."


def _is_no_data(resp: Any) -> bool:
    return isinstance(resp, dict) and (
        str(resp.get("stCode")) == str(NO_DATA_CODE) or str(resp.get("errMsg", "")).lower() == "no data")


def _check(resp: Any, what: str, allow_no_data: bool = False) -> Any:
    """Kotak's SDK sometimes returns an error dict instead of raising."""
    if resp is None:
        raise BrokerError(f"{what}: empty reply from Kotak")
    if isinstance(resp, dict):
        if allow_no_data and _is_no_data(resp):
            return {"data": []}
        for key in ("error", "Error", "Error Message", "fault"):
            if resp.get(key):
                raise BrokerError(f"{what}: {_short(resp)}")
        if str(resp.get("status", "")).upper() == "ERROR":
            raise BrokerError(f"{what}: {_short(resp)}")
        code = resp.get("stCode")
        if code is not None and str(code) != "200":
            raise BrokerError(f"{what}: {resp.get('errMsg') or _short(resp)}")
    return resp


def _find_value(obj: Any, keys) -> Any:
    """Search a nested reply for the first non-empty value under any of these keys."""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, "", "0", 0):
                return obj[k]
        for v in obj.values():
            found = _find_value(v, keys)
            if found not in (None, "", "0", 0):
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_value(v, keys)
            if found not in (None, "", "0", 0):
                return found
    return None


def _find_list(obj: Any, keys) -> List[dict]:
    """Search a nested reply for the first non-empty list under any of these keys."""
    if isinstance(obj, dict):
        for k in keys:
            if isinstance(obj.get(k), list) and obj[k]:
                return obj[k]
        for v in obj.values():
            found = _find_list(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_list(v, keys)
            if found:
                return found
    return []


LOT_KEYS = ("mktLot", "lotSize", "lotSz", "lLotSize", "lot_size", "brdLtQty")


def _side_keys(option_type: str):
    return ("call", "calls", "CE") if option_type == "CE" else ("put", "puts", "PE")


def _inst(row: dict) -> dict:
    return row.get("instrument") or row.get("inst") or row


def _strike_of(row: dict) -> float:
    i = _inst(row)
    for key in ("strkPrc", "strikePrice", "strike_price", "stkPrc", "strike"):
        if i.get(key) not in (None, ""):
            return _f(i.get(key))
    return 0.0


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class KotakNeoBroker(Broker):
    name = "Kotak Neo"
    is_simulated = False

    def __init__(self, secrets: Secrets, product: str = "MIS"):
        self.secrets = secrets
        self.product = product
        self._api = None
        self._logged_in = False
        self.last_candle_error = ""
        self._chain_cache: Dict[tuple, Any] = {}      # strikes/tokens don't change during the day
        self._expiry_cache: Dict[tuple, List[str]] = {}
        self._daily_cache: Dict[tuple, List[Candle]] = {}   # completed days don't change either
        self.last_daily_error = ""

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    def _client(self):
        if self._api is None:
            try:
                from neo_api_client import NeoAPI
            except ImportError as e:
                raise BrokerError("Kotak SDK not installed. Run: pip install -r requirements.txt") from e
            if not self.secrets.kotak_consumer_key:
                raise BrokerError("KOTAK_CONSUMER_KEY is missing in your .env file")
            self._api = NeoAPI(consumer_key=self.secrets.kotak_consumer_key, environment="prod",
                               access_token=None, neo_fin_key=None)
        return self._api

    def login(self, totp_code: Optional[str] = None) -> None:
        missing = self.secrets.missing_kotak_fields()
        if missing:
            raise BrokerError(f"Missing in .env: {', '.join(missing)}")
        code = (totp_code or "").strip()
        if not code:
            if not self.secrets.kotak_totp_secret:
                raise BrokerError("Enter the 6-digit code from your authenticator app")
            import pyotp
            code = pyotp.TOTP(self.secrets.kotak_totp_secret).now()
        api = self._client()
        try:
            _check(api.totp_login(mobile_number=self.secrets.kotak_mobile_number,
                                  ucc=self.secrets.kotak_ucc, totp=code), "TOTP login")
            _check(api.totp_validate(mpin=self.secrets.kotak_mpin), "MPIN validate")
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"Kotak login failed: {e}") from e
        self._logged_in = True
        log.info("Logged in to Kotak Neo")

    def is_logged_in(self) -> bool:
        return self._logged_in

    def _require_login(self):
        if not self._logged_in:
            raise BrokerError("Not logged in to Kotak Neo")

    def _call(self, what: str, fn, *args, allow_no_data: bool = False, **kwargs):
        try:
            return _check(fn(*args, **kwargs), what, allow_no_data=allow_no_data)
        except BrokerError:
            raise
        except Exception as e:
            raise BrokerError(f"{what} failed: {e}") from e

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    @staticmethod
    def _quote_for(resp, token: str) -> Dict[str, Any]:
        """Return the quote row for exactly this instrument. Refuses a price that
        belongs to a different instrument."""
        rows = resp.get("data", resp) if isinstance(resp, dict) else resp
        if not isinstance(rows, list) or not rows:
            raise BrokerError(f"Unexpected quote reply: {_short(resp)}")
        for row in rows:
            if str(row.get("exchange_token", "")).strip().lower() == str(token).strip().lower():
                return row
        if len(rows) == 1 and "exchange_token" not in rows[0]:
            return rows[0]
        raise BrokerError(f"Quote reply was for a different instrument than {token}: {_short(resp)}")

    def _ltp(self, token: str, segment: str) -> float:
        api = self._client()
        resp = self._call("Quote", api.quotes,
                          instrument_tokens=[{"instrument_token": token, "exchange_segment": segment}],
                          quote_type="ltp")
        ltp = _f(self._quote_for(resp, token).get("ltp"))
        if ltp <= 0:
            raise BrokerError(f"No price for {segment}|{token}")
        return ltp

    def get_index_ltp(self, index: str) -> float:
        info = INDEXES[index]
        return self._ltp(info["quote_token"], info["quote_segment"])

    def get_option_ltp(self, contract: OptionContract) -> float:
        return self._ltp(contract.token, contract.segment)

    def get_previous_day_candle(self, index: str, today: date) -> Optional[Candle]:
        """Last completed daily candle before today. Tries Kotak first,
        then Yahoo Finance as a backup."""
        info = INDEXES[index]
        self.last_candle_error = ""
        try:
            api = self._client()
            resp = self._call("Historical data", api.historical_data,
                              neosymbol=f"{info['quote_segment']}|{info['quote_token']}",
                              interval="D",
                              from_date=(today - timedelta(days=15)).isoformat(),
                              to_date=today.isoformat())
            candles = (resp.get("data") or {}).get("candles") or []
            best = None
            for row in candles:
                day = str(row[0])[:10]
                if day < today.isoformat():
                    best = Candle(day, _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]), "Kotak")
            if best:
                return best
            self.last_candle_error = "Kotak returned no daily candles"
        except BrokerError as e:
            self.last_candle_error = str(e)
            log.warning("Kotak daily candle failed, trying backup: %s", e)

        from trader.candle_backup import yahoo_previous_day_candle
        return yahoo_previous_day_candle(info["yahoo_symbol"], today)

    def get_daily_candles(self, index: str, days: int = 25) -> List[Candle]:
        """Recent completed daily candles, oldest first (today is left out).
        Tries Kotak, then Yahoo Finance, so one flaky call can't stop the day.

        Answered from memory for the rest of the day once it works. Today is excluded,
        so these candles cannot change, and the health check, the narrow-day filter and
        the engine were each fetching them separately - three requests in the same
        second, which is how we earned a 429 at the open."""
        info = INDEXES[index]
        today = date.today()
        key = (index, days, today.isoformat())
        if self._daily_cache.get(key):
            return self._daily_cache[key]
        self.last_daily_error = ""
        out: List[Candle] = []
        try:
            api = self._client()
            resp = self._call("Daily candles", api.historical_data,
                              neosymbol=f"{info['quote_segment']}|{info['quote_token']}", interval="D",
                              from_date=(today - timedelta(days=days * 2 + 25)).isoformat(),
                              to_date=today.isoformat())
            rows = (resp.get("data") or {}).get("candles") or []
            for row in rows:
                day = str(row[0])[:10]
                if day >= today.isoformat() or len(row) < 5:
                    continue
                out.append(Candle(day, _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]), "Kotak"))
            if not out:
                self.last_daily_error = f"Kotak returned {len(rows)} daily rows and none usable"
        except BrokerError as e:
            self.last_daily_error = str(e)
            log.warning("Kotak daily history failed, trying Yahoo: %s", e)

        if len(out) < 10:
            from trader.candle_backup import yahoo_daily_candles
            backup = yahoo_daily_candles(info["yahoo_symbol"], today, days)
            if len(backup) > len(out):
                log.warning("Using Yahoo daily history (%d days); Kotak gave %d. %s",
                            len(backup), len(out), self.last_daily_error)
                out = backup
        out = out[-days:]
        if len(out) >= 10:            # only remember a usable answer, never a failed one
            self._daily_cache[key] = out
            self._daily_cache = {k: v for k, v in self._daily_cache.items()
                                 if k[2] == today.isoformat()}
        return out

    def get_daily_candles_between(self, index: str, from_date: date, to_date: date) -> List[Candle]:
        """Daily candles in a date range (Kotak serves about 180 days per request)."""
        info = INDEXES[index]
        resp = self._call("Daily candles", self._client().historical_data,
                          neosymbol=f"{info['quote_segment']}|{info['quote_token']}", interval="D",
                          from_date=from_date.isoformat(), to_date=to_date.isoformat())
        rows = (resp.get("data") or {}).get("candles") or []
        out = []
        for row in rows:
            if len(row) < 5:
                continue
            out.append(Candle(str(row[0])[:10], _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]), "Kotak"))
        return out

    def get_minute_candles(self, index: str, since_hhmm: str = "09:15"):
        from trader.backtest.history import Bar
        from trader.clock import IST
        info = INDEXES[index]
        today = date.today()
        api = self._client()
        resp = self._call("Minute candles", api.historical_data,
                          neosymbol=f"{info['quote_segment']}|{info['quote_token']}", interval="1min",
                          from_date=today.isoformat(), to_date=today.isoformat())
        rows = (resp.get("data") or {}).get("candles") or []
        out = []
        for row in rows:
            when = datetime.fromisoformat(str(row[0]))
            if when.tzinfo is None:
                when = when.replace(tzinfo=IST)
            if when.strftime("%H:%M") < since_hhmm:
                continue
            out.append(Bar(when, _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]),
                           _f(row[5]) if len(row) > 5 else 0.0))
        return out

    def _pick_expiry(self, underlying: str, segment: str, today: date, expiry_choice: str) -> str:
        key = (underlying, today.isoformat())
        if key not in self._expiry_cache:
            api = self._client()
            resp = self._call("Expiries", api.expiries, exchange=segment, underlying=underlying)
            self._expiry_cache[key] = sorted(e for e in resp.get("expiries", []) if e >= today.isoformat())
        expiries = self._expiry_cache[key]
        if not expiries:
            raise BrokerError(f"No expiries found for {underlying}")
        if expiry_choice == "next_on_expiry_day" and expiries[0] == today.isoformat() and len(expiries) > 1:
            return expiries[1]
        return expiries[0]

    def get_expiry(self, index: str, today: date, expiry_choice: str) -> str:
        info = INDEXES[index]
        return self._pick_expiry(info["underlying"], info["fo_segment"], today, expiry_choice)

    def _chain(self, info: dict, expiry: str, refresh: bool = False):
        key = (info["underlying"], expiry)
        if refresh or key not in self._chain_cache:
            api = self._client()
            self._chain_cache[key] = self._call(
                "Option chain", api.option_chain, exchange=info["fo_segment"],
                underlying=info["underlying"], expiry=expiry, instrument_type="option", count=40)
        return self._chain_cache[key]

    def _contract_from_row(self, info: dict, resp, row: dict, expiry: str, option_type: str) -> OptionContract:
        i = _inst(row)
        strike = _strike_of(row)
        neo_symbol = str(i.get("neoSymbol") or "")
        segment, _, token = neo_symbol.partition("|")
        token = token or str(i.get("token") or i.get("instrument_token") or "")
        symbol = i.get("symbol") or i.get("trading_symbol") or i.get("trdSym")
        if not token or not symbol:
            raise BrokerError(f"Could not read option symbol/token. Row: {_short(row)}")
        row_type = str(i.get("optType") or i.get("optionType") or "").upper()
        if (row_type and row_type != option_type) or not str(symbol).upper().endswith(option_type):
            raise BrokerError(f"Option type mismatch: wanted {option_type}, got {symbol}. Trade skipped for safety.")
        lot_size = int(_f(_find_value(row, LOT_KEYS) or _find_value(resp, LOT_KEYS), 0))
        if lot_size <= 0:
            lot_size = self._lot_size_from_scrip_search(info, expiry, option_type, strike, symbol)
        if lot_size <= 0:
            raise BrokerError(f"Could not read lot size. Option chain reply: {_short(resp)}")
        return OptionContract(symbol=str(symbol), token=token, segment=segment or info["fo_segment"],
                              strike=strike, option_type=option_type, expiry=expiry, lot_size=lot_size)

    def find_atm_option(self, index: str, spot: float, option_type: str,
                        today: date, expiry_choice: str) -> OptionContract:
        info = INDEXES[index]
        step = info["strike_step"]
        atm_strike = round(spot / step) * step
        expiry = self._pick_expiry(info["underlying"], info["fo_segment"], today, expiry_choice)

        strike, row, resp = None, None, None
        for refresh in (False, True):       # a cached chain may be centred on an old price
            resp = self._chain(info, expiry, refresh)
            rows = _find_list(resp, _side_keys(option_type))
            if not rows:
                if refresh:
                    raise BrokerError(f"Option chain has no {option_type} rows. Kotak replied: {_short(resp)}")
                continue
            row = min(rows, key=lambda r: abs(_strike_of(r) - atm_strike))
            strike = _strike_of(row)
            if abs(strike - atm_strike) <= step / 2:
                break
        # Safety: never trade anything except the real ATM strike
        if strike is None or abs(strike - atm_strike) > step / 2:
            raise BrokerError(f"ATM strike {atm_strike} not found in option chain (closest was {strike}). "
                              f"Trade skipped for safety.")
        return self._contract_from_row(info, resp, row, expiry, option_type)

    def get_option(self, index: str, strike: float, option_type: str,
                   today: date, expiry_choice: str) -> OptionContract:
        info = INDEXES[index]
        expiry = self._pick_expiry(info["underlying"], info["fo_segment"], today, expiry_choice)
        for refresh in (False, True):
            resp = self._chain(info, expiry, refresh)
            for row in _find_list(resp, _side_keys(option_type)):
                if abs(_strike_of(row) - strike) < 0.01:
                    return self._contract_from_row(info, resp, row, expiry, option_type)
        raise BrokerError(f"Strike {strike:.0f} {option_type} is not in Kotak's option chain for {expiry}. "
                          f"Trade skipped for safety.")

    def get_option_ltps(self, contracts: List[OptionContract]) -> Dict[str, float]:
        """One quotes call for up to 50 options (Kotak's limit), instead of one call each."""
        api = self._client()
        unique = list({(c.segment, c.token): c for c in contracts}.values())
        out: Dict[str, float] = {}
        for i in range(0, len(unique), 50):
            batch = unique[i:i + 50]
            resp = self._call("Quotes", api.quotes,
                              instrument_tokens=[{"instrument_token": c.token, "exchange_segment": c.segment}
                                                 for c in batch],
                              quote_type="ltp")
            rows = resp.get("data", resp) if isinstance(resp, dict) else resp
            by_token = {str(r.get("exchange_token", "")).strip().lower(): r
                        for r in (rows or []) if isinstance(r, dict)}
            for c in batch:
                row = by_token.get(str(c.token).strip().lower())
                ltp = _f(row.get("ltp")) if row else 0.0
                if ltp <= 0:
                    raise BrokerError(f"No price for {c.symbol} in Kotak's reply")
                out[c.token] = ltp
        return out

    def get_index_open(self, index: str) -> Optional[float]:
        info = INDEXES[index]
        try:
            api = self._client()
            resp = self._call("Index OHLC", api.quotes,
                              instrument_tokens=[{"instrument_token": info["quote_token"],
                                                  "exchange_segment": info["quote_segment"]}],
                              quote_type="ohlc")
            opened = _f((self._quote_for(resp, info["quote_token"]).get("ohlc") or {}).get("open"))
            if opened > 0:
                return opened
        except BrokerError as e:
            log.warning("Index open from quotes failed, trying today's first candle: %s", e)
        try:
            candles = self.get_minute_candles(index, "09:15")
            if candles:
                return candles[0].open
        except BrokerError as e:
            log.warning("Index open from candles failed: %s", e)
        return None

    def get_vix(self) -> Optional[float]:
        try:
            return self._ltp(VIX["quote_token"], VIX["quote_segment"])
        except BrokerError as e:
            log.warning("India VIX from Kotak failed, trying Yahoo Finance: %s", e)
        from trader.candle_backup import yahoo_last_close
        return yahoo_last_close(VIX["yahoo_symbol"])

    def get_open_option_positions(self, index: str) -> List[dict]:
        self._require_login()
        info = INDEXES[index]
        api = self._client()
        resp = self._call("Positions", api.positions, allow_no_data=True)
        out = []
        for row in resp.get("data", []) or []:
            if str(row.get("exSeg", "")) != info["fo_segment"]:
                continue
            if str(row.get("sym", "")).upper() != info["underlying"]:
                continue
            if str(row.get("optTp", "")).upper() not in ("CE", "PE"):
                continue
            net = (_f(row.get("cfBuyQty")) + _f(row.get("flBuyQty"))
                   - _f(row.get("cfSellQty")) - _f(row.get("flSellQty")))
            if net:
                out.append({"symbol": row.get("trdSym"), "token": str(row.get("tok")), "net_qty": int(net)})
        return out

    def _lot_size_from_scrip_search(self, info, expiry: str, option_type: str, strike: float, symbol: str) -> int:
        """Backup: read lot size from Kotak's scrip search."""
        api = self._client()
        try:
            resp = api.search_scrip(exchange_segment=info["fo_segment"], symbol=info["underlying"],
                                    expiry=expiry.replace("-", "")[:6], option_type=option_type,
                                    strike_price=str(int(strike)))
        except Exception as e:
            log.warning("search_scrip for lot size failed: %s", e)
            return 0
        rows = resp if isinstance(resp, list) else _find_list(resp, ("data",))
        for r in rows or []:
            if isinstance(r, dict) and str(r.get("pTrdSymbol", "")) == symbol:
                return int(_f(r.get("lLotSize"), 0))
        return int(_f(_find_value(rows, ("lLotSize",)), 0))

    # ------------------------------------------------------------------ #
    # Orders & account
    # ------------------------------------------------------------------ #
    def place_order(self, contract: OptionContract, side: str, qty: int, order_type: str,
                    price: float, trigger_price: float = 0.0, tag: str = "") -> str:
        self._require_login()
        api = self._client()
        resp = self._call(
            "Place order", api.place_order,
            exchange_segment=contract.segment,
            product=self.product,
            price=f"{price:.2f}",
            order_type=_ORDER_TYPE[order_type],
            quantity=str(qty),
            validity="DAY",
            trading_symbol=contract.symbol,
            transaction_type=_SIDE[side],
            amo="NO",
            disclosed_quantity="0",
            trigger_price=f"{trigger_price:.2f}",
            tag=tag or "nifty-pdb",
        )
        order_id = resp.get("nOrdNo")
        if not order_id:
            raise BrokerError(f"Order not accepted: {resp}")
        return str(order_id)

    def modify_order(self, order_id: str, contract: OptionContract, qty: int, order_type: str,
                     price: float, trigger_price: float = 0.0) -> str:
        self._require_login()
        api = self._client()
        resp = self._call("Modify order", api.modify_order, order_id=str(order_id), price=f"{price:.2f}",
                          order_type=_ORDER_TYPE[order_type], quantity=str(qty), validity="DAY",
                          trigger_price=f"{trigger_price:.2f}", disclosed_quantity="0", amo="NO")
        return str(resp.get("nOrdNo") or order_id)

    def cancel_order(self, order_id: str) -> None:
        self._require_login()
        api = self._client()
        try:
            resp = api.cancel_order(order_id=order_id)
        except Exception as e:
            raise BrokerError(f"Cancel failed: {e}") from e
        # "order is completed" is fine: it simply already filled
        if isinstance(resp, dict) and str(resp.get("stCode")) == "1021":
            return
        _check(resp, "Cancel order")

    def get_order_status(self, order_id: str) -> OrderStatus:
        self._require_login()
        api = self._client()
        resp = self._call("Order history", api.order_history, order_id=order_id, allow_no_data=True)
        inner = resp.get("data", resp)
        rows = inner.get("data", []) if isinstance(inner, dict) else inner
        if not rows:
            return OrderStatus(order_id, OPEN, message="No history yet")
        latest = max(rows, key=lambda r: int(_f(r.get("updRecvTm"), 0)))
        raw = str(latest.get("ordSt", "")).lower()
        if raw == "complete":
            status = COMPLETE
        elif raw == "rejected":
            status = REJECTED
        elif raw in ("cancelled", "canceled"):
            status = CANCELLED
        else:
            status = OPEN
        return OrderStatus(order_id=order_id, status=status,
                           filled_qty=int(_f(latest.get("fldQty"), 0)),
                           avg_price=_f(latest.get("avgPrc")),
                           message=str(latest.get("rejRsn", "")) if status == REJECTED else raw)

    def get_available_capital(self) -> Optional[float]:
        self._require_login()
        api = self._client()
        resp = self._call("Limits", api.limits)
        net = _f(resp.get("Net"), -1)
        return net if net >= 0 else None

    def get_open_quantity(self, contract: OptionContract) -> int:
        self._require_login()
        api = self._client()
        resp = self._call("Positions", api.positions, allow_no_data=True)
        for row in resp.get("data", []) or []:
            if str(row.get("tok")) == str(contract.token):
                bought = _f(row.get("cfBuyQty")) + _f(row.get("flBuyQty"))
                sold = _f(row.get("cfSellQty")) + _f(row.get("flSellQty"))
                return int(bought - sold)
        return 0
