"""
UserContext-injected KIS REST API Client (R1.2, R1.6, R5.5, R8).

Reserved strictly for real-time quotes (Stage 2), account balances,
executions (inquire_ccnl), and live orders. Historical OHLCV is served
by HybridMarketDataProvider (FinanceDataReader + local cache).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from core.context import UserContext
from core.kis.rate_limiter import AccountRateLimiter

logger = logging.getLogger(__name__)

KIS_REAL_BASE = "https://openapi.koreainvestment.com:9443"
KIS_MOCK_BASE = "https://openapivts.koreainvestment.com:29443"

# KIS domestic ORD_DVSN codes keyed by PriceType value.
KR_ORD_DVSN: Dict[str, str] = {
    "지정가": "00",
    "시장가": "01",
    "조건부지정가": "02",
    "최유리지정가": "03",
    "최우선지정가": "04",
}
KR_PRICED_ORD_DVSN = {"00", "02"}

# KIS overseas (US) ORD_DVSN codes keyed by (side, PriceType value).
US_ORD_DVSN: Dict[tuple, str] = {
    ("BUY", "지정가"): "00",
    ("BUY", "LOO"): "32",
    ("BUY", "LOC"): "34",
    ("SELL", "지정가"): "00",
    ("SELL", "MOO"): "31",
    ("SELL", "LOO"): "32",
    ("SELL", "MOC"): "33",
    ("SELL", "LOC"): "34",
}

US_QUOTE_EXCHANGES = ("NAS", "NYS", "AMS")
_US_EXCD_TO_ORDER_EXCG = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}
_US_ORDER_EXCG_TO_EXCD = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}


class KISClient:
    """Per-user KIS API client with isolated token cache in data/<user_id>/kis_token.json."""

    def __init__(
        self,
        ctx: UserContext,
        calls_per_second: float = 5.0,
        timeout: float = 10.0,
    ) -> None:
        self.ctx = ctx
        self.base_url = KIS_MOCK_BASE if ctx.is_mock else KIS_REAL_BASE
        self.timeout = timeout
        self.rate_limiter = AccountRateLimiter(calls_per_second=calls_per_second)
        self._token_path: Path = ctx.resolve_user_path("kis_token.json")
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._us_exchange_cache: Dict[str, str] = {}

    def _load_cached_token(self) -> Optional[str]:
        if not self._token_path.exists():
            return None
        try:
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
            if data.get("is_mock") != self.ctx.is_mock:
                return None
            exp = datetime.fromisoformat(data["expires_at"])
            if exp > datetime.now(timezone.utc) + timedelta(minutes=5):
                self._access_token = data["access_token"]
                self._token_expires_at = exp
                return self._access_token
        except Exception:
            return None
        return None

    def _save_cached_token(self, token: str, expires_at: datetime) -> None:
        payload = {
            "user_id": self.ctx.user_id,
            "is_mock": self.ctx.is_mock,
            "access_token": token,
            "expires_at": expires_at.isoformat(),
        }
        self._token_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get_access_token(self) -> str:
        if self._access_token and self._token_expires_at and self._token_expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            return self._access_token

        cached = self._load_cached_token()
        if cached:
            return cached

        if not self.ctx.credentials.kis_app_key or not self.ctx.credentials.kis_app_secret:
            raise RuntimeError(f"KIS credentials missing for user '{self.ctx.user_id}'")

        self.rate_limiter.acquire()
        url = f"{self.base_url}/oauth2/tokenP"
        resp = requests.post(
            url,
            json={
                "grant_type": "client_credentials",
                "appkey": self.ctx.credentials.kis_app_key,
                "appsecret": self.ctx.credentials.kis_app_secret,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body["access_token"]
        expires_in = int(body.get("expires_in", 86400))
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        self._access_token = token
        self._token_expires_at = expires_at
        self._save_cached_token(token, expires_at)
        return token

    def _headers(self, tr_id: str) -> Dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.get_access_token()}",
            "appkey": self.ctx.credentials.kis_app_key,
            "appsecret": self.ctx.credentials.kis_app_secret,
            "tr_id": tr_id,
        }

    def get_current_price(self, symbol: str, market: str = "KR", exchange: str = "NAS") -> Optional[Dict[str, Any]]:
        """Fetch real-time quote for a single symbol (Stage 2 only)."""
        if market.upper() == "KR":
            self.rate_limiter.acquire()
            url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
            headers = self._headers("FHKST01010100")
            params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
            resp = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            resp.raise_for_status()
            out = resp.json().get("output", {})
            price = float(out.get("stck_prpr", 0) or 0)
            if price <= 0:
                return None
            return {
                "symbol": symbol,
                "price": price,
                "open": float(out.get("stck_oprc", price) or price),
                "high": float(out.get("stck_hgpr", price) or price),
                "low": float(out.get("stck_lwpr", price) or price),
                "volume": float(out.get("acml_vol", 0) or 0),
            }

        sym = symbol.upper()
        url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        headers = self._headers("HHDFS00000300")

        candidates: List[str] = []
        cached_excd = self._us_exchange_cache.get(sym)
        for excd in (cached_excd, exchange, *US_QUOTE_EXCHANGES):
            if excd and excd not in candidates:
                candidates.append(excd)

        for excd in candidates:
            self.rate_limiter.acquire()
            params = {"AUTH": "", "EXCD": excd, "SYMB": sym}
            resp = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            resp.raise_for_status()
            out = resp.json().get("output", {})
            price = float(out.get("last", 0) or out.get("base", 0) or 0)
            if price > 0:
                self._us_exchange_cache[sym] = excd
                return {
                    "symbol": sym,
                    "exchange": excd,
                    "price": price,
                    "open": float(out.get("open", price) or price),
                    "high": float(out.get("high", price) or price),
                    "low": float(out.get("low", price) or price),
                    "volume": float(out.get("tvol", 0) or 0),
                }
        return None

    def inquire_balance(self, market: str = "KR", exchange: str = "NASD") -> List[Dict[str, Any]]:
        """Return actual broker holdings: [{symbol, market, qty, avg_price, current_price}]."""
        self.rate_limiter.acquire()
        if market.upper() == "KR":
            tr_id = "VTTC8434R" if self.ctx.is_mock else "TTTC8434R"
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
            params = {
                "CANO": self.ctx.credentials.account_prefix,
                "ACNT_PRDT_CD": self.ctx.credentials.account_suffix,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            }
            resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=self.timeout)
            resp.raise_for_status()
            items = resp.json().get("output1", [])
            holdings: List[Dict[str, Any]] = []
            for it in items:
                qty = int(float(it.get("hldg_qty", 0) or 0))
                if qty <= 0:
                    continue
                holdings.append(
                    {
                        "symbol": str(it.get("pdno", "")).strip(),
                        "name": str(it.get("prdt_name", "")).strip(),
                        "market": "KR",
                        "qty": qty,
                        "avg_price": float(it.get("pchs_avg_pric", 0) or 0),
                        "current_price": float(it.get("prpr", 0) or 0),
                    }
                )
            return holdings

        tr_id = "VTTS3012R" if self.ctx.is_mock else "TTTS3012R"
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {
            "CANO": self.ctx.credentials.account_prefix,
            "ACNT_PRDT_CD": self.ctx.credentials.account_suffix,
            "OVRS_EXCG_CD": exchange,
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=self.timeout)
        resp.raise_for_status()
        items = resp.json().get("output1", [])
        holdings = []
        for it in items:
            qty = int(float(it.get("ovrs_cblc_qty", 0) or 0))
            if qty <= 0:
                continue
            sym = str(it.get("ovrs_pdno", "")).strip().upper()
            excg = str(it.get("ovrs_excg_cd", "")).strip().upper()
            if sym and excg in _US_ORDER_EXCG_TO_EXCD:
                self._us_exchange_cache[sym] = _US_ORDER_EXCG_TO_EXCD[excg]
            holdings.append(
                {
                    "symbol": sym,
                    "name": str(it.get("ovrs_item_name", "")).strip(),
                    "market": "US",
                    "qty": qty,
                    "avg_price": float(it.get("pchs_avg_pric", 0) or 0),
                    "current_price": float(it.get("now_pric2", 0) or 0),
                }
            )
        return holdings

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        market: str = "KR",
        price_type: str = "지정가",
        exchange: str = "NASD",
    ) -> Dict[str, Any]:
        """Submit KR or US order to KIS API."""
        self.rate_limiter.acquire()
        side_u = side.upper()
        if market.upper() == "KR":
            if side_u == "BUY":
                tr_id = "VTTC0802U" if self.ctx.is_mock else "TTTC0802U"
            else:
                tr_id = "VTTC0801U" if self.ctx.is_mock else "TTTC0801U"
            ord_dvsn = KR_ORD_DVSN.get(price_type)
            if ord_dvsn is None:
                return {"success": False, "order_no": "", "message": f"국내 주식에서 지원하지 않는 주문유형: {price_type}"}
            send_price = ord_dvsn in KR_PRICED_ORD_DVSN
            body = {
                "CANO": self.ctx.credentials.account_prefix,
                "ACNT_PRDT_CD": self.ctx.credentials.account_suffix,
                "PDNO": symbol,
                "ORD_DVSN": ord_dvsn,
                "ORD_QTY": str(int(qty)),
                "ORD_UNPR": str(int(round(price))) if send_price else "0",
            }
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
            resp = requests.post(url, headers=self._headers(tr_id), json=body, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            ok = data.get("rt_cd") == "0"
            return {
                "success": ok,
                "order_no": data.get("output", {}).get("ODNO", ""),
                "message": data.get("msg1", ""),
            }

        if side_u == "BUY":
            tr_id = "VTTT1002U" if self.ctx.is_mock else "TTTT1002U"
        else:
            tr_id = "VTTT1006U" if self.ctx.is_mock else "TTTT1006U"
        ord_dvsn = US_ORD_DVSN.get((side_u, price_type))
        if ord_dvsn is None:
            return {"success": False, "order_no": "", "message": f"미국 주식 {side_u}에서 지원하지 않는 주문유형: {price_type}"}
        if ord_dvsn in {"31", "33"}:  # MOO / MOC: price is broker-determined
            price = 0.0
        sym = symbol.upper()
        resolved_exchange = exchange
        if exchange == "NASD" and sym in self._us_exchange_cache:
            resolved_exchange = _US_EXCD_TO_ORDER_EXCG.get(self._us_exchange_cache[sym], exchange)
        body = {
            "CANO": self.ctx.credentials.account_prefix,
            "ACNT_PRDT_CD": self.ctx.credentials.account_suffix,
            "OVRS_EXCG_CD": resolved_exchange,
            "PDNO": sym,
            "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_dvsn,
        }
        url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        resp = requests.post(url, headers=self._headers(tr_id), json=body, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        ok = data.get("rt_cd") == "0"
        return {
            "success": ok,
            "order_no": data.get("output", {}).get("ODNO", ""),
            "message": data.get("msg1", ""),
        }

    def inquire_ccnl(self, order_no: str = "", market: str = "KR") -> List[Dict[str, Any]]:
        """Query confirmed executions so only filled quantities are recorded as Fills (R5.5)."""
        self.rate_limiter.acquire()
        today_str = datetime.now().strftime("%Y%m%d")
        if market.upper() == "KR":
            tr_id = "VTTC8001R" if self.ctx.is_mock else "TTTC8001R"
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
            params = {
                "CANO": self.ctx.credentials.account_prefix,
                "ACNT_PRDT_CD": self.ctx.credentials.account_suffix,
                "INQR_STRT_DT": today_str,
                "INQR_END_DT": today_str,
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "01",
                "ORD_GNO_BRNO": "",
                "ODNO": order_no,
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            }
            resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=self.timeout)
            resp.raise_for_status()
            rows = resp.json().get("output1", [])
            return [
                {
                    "order_no": str(r.get("odno", "")),
                    "symbol": str(r.get("pdno", "")),
                    "filled_qty": int(float(r.get("tot_ccld_qty", 0) or 0)),
                    "unfilled_qty": int(float(r.get("rmn_qty", 0) or 0)),
                    "avg_price": float(r.get("avg_prvs", 0) or 0),
                }
                for r in rows
            ]
        return []


class MockKISClient:
    """In-memory mock KIS client for offline/unit/E2E testing without live broker calls."""

    def __init__(self, ctx: UserContext) -> None:
        self.ctx = ctx
        self.quotes: Dict[str, Dict[str, Any]] = {}
        self.holdings: List[Dict[str, Any]] = []
        self.placed_orders: List[Dict[str, Any]] = []
        self.confirmed_fills: Dict[str, Dict[str, Any]] = {}
        self.quote_call_count: int = 0
        self._order_seq = 1

    def get_current_price(self, symbol: str, market: str = "KR", exchange: str = "NAS") -> Optional[Dict[str, Any]]:
        self.quote_call_count += 1
        return self.quotes.get(symbol.upper()) or self.quotes.get(symbol)

    def inquire_balance(self, market: str = "KR", exchange: str = "NASD") -> List[Dict[str, Any]]:
        return [dict(h) for h in self.holdings if h.get("market", "KR").upper() == market.upper()]

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        market: str = "KR",
        price_type: str = "지정가",
        exchange: str = "NASD",
    ) -> Dict[str, Any]:
        order_no = f"MOCK_{self.ctx.safe_user_id}_{self._order_seq:04d}"
        self._order_seq += 1
        record = {
            "order_no": order_no,
            "symbol": symbol,
            "side": side.upper(),
            "qty": int(qty),
            "price": float(price),
            "market": market.upper(),
            "price_type": price_type,
        }
        self.placed_orders.append(record)
        self.confirmed_fills[order_no] = {
            "order_no": order_no,
            "symbol": symbol,
            "filled_qty": int(qty),
            "unfilled_qty": 0,
            "avg_price": float(price),
        }
        return {"success": True, "order_no": order_no, "message": "MOCK_OK"}

    def inquire_ccnl(self, order_no: str = "", market: str = "KR") -> List[Dict[str, Any]]:
        if order_no and order_no in self.confirmed_fills:
            return [dict(self.confirmed_fills[order_no])]
        return [dict(v) for v in self.confirmed_fills.values()]
