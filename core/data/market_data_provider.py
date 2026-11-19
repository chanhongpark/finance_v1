"""
Hybrid Market Data Provider: FinanceDataReader-first + KIS real-time stitching.

Design goals:
1. Stage 1 (Universe Screening): 0 KIS API calls! Uses FinanceDataReader in parallel
   with incremental local cache (`data/shared/ohlcv/`).
2. Stage 2 (Final Candidates & Open Lots): Fetches live quote from KIS API only for
   the small set of candidate/held symbols (with short TTL cache) and stitches today's
   candle onto the cached OHLCV DataFrame.
3. Fallback: Only falls back to KIS historical OHLCV if FinanceDataReader fails for a symbol.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from core.data.cache_store import SharedMarketCache
from core.indicators import normalize_ohlcv

logger = logging.getLogger(__name__)


def _default_fdr_reader(symbol: str, start: str, end: str) -> pd.DataFrame:
    import FinanceDataReader as fdr
    return fdr.DataReader(symbol, start, end)


def _default_fdr_listing(market: str) -> pd.DataFrame:
    import FinanceDataReader as fdr
    return fdr.StockListing(market)


_COMMON_US_NAMES: Dict[str, str] = {
    "TQQQ": "ProShares UltraPro QQQ",
    "SOXL": "Direxion Daily Semiconductor Bull 3X",
    "SQQQ": "ProShares UltraPro Short QQQ",
    "SOXS": "Direxion Daily Semiconductor Bear 3X",
    "UPRO": "ProShares UltraPro S&P500",
    "TMF": "Direxion Daily 20+ Yr Treasury Bull 3X",
    "QLD": "ProShares Ultra QQQ",
    "SSO": "ProShares Ultra S&P500",
    "QQQ": "Invesco QQQ Trust",
    "SPY": "SPDR S&P 500 ETF",
    "VOO": "Vanguard S&P 500 ETF",
    "VTI": "Vanguard Total Stock Market ETF",
    "SCHD": "Schwab US Dividend Equity ETF",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet",
    "META": "Meta Platforms",
}


class HybridMarketDataProvider:
    """
    Minimizes KIS API usage by serving historical OHLCV & listings from
    FinanceDataReader + incremental cache, reserving KIS for live quotes.
    """

    def __init__(
        self,
        cache: Optional[SharedMarketCache] = None,
        kis_client: Optional[Any] = None,
        fdr_reader: Optional[Callable[[str, str, str], pd.DataFrame]] = None,
        fdr_listing_fn: Optional[Callable[[str], pd.DataFrame]] = None,
        quote_ttl_seconds: float = 30.0,
        max_workers: int = 8,
    ) -> None:
        self.cache = cache or SharedMarketCache()
        self.kis_client = kis_client
        self._fdr_reader = fdr_reader or _default_fdr_reader
        self._fdr_listing_fn = fdr_listing_fn or _default_fdr_listing
        self.quote_ttl_seconds = quote_ttl_seconds
        self.max_workers = max_workers
        self._quote_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
        self._symbol_names: Dict[Tuple[str, str], str] = {
            ("US", sym): name for sym, name in _COMMON_US_NAMES.items()
        }
        self._missing_names: set[Tuple[str, str]] = set()
        self._kr_names_loaded: bool = False

    def get_krx_listing(self) -> List[Dict[str, Any]]:
        """Return cached KRX symbol listing (0 KIS API calls)."""
        cached = self.cache.load_listing("KRX")
        if cached is not None:
            return cached
        try:
            df = self._fdr_listing_fn("KRX")
            records: List[Dict[str, Any]] = []
            for _, row in df.iterrows():
                code = str(row.get("Code", row.get("Symbol", ""))).strip()
                name = str(row.get("Name", "")).strip()
                mkt = str(row.get("Market", "")).strip()
                if code and name:
                    records.append({"symbol": code, "name": name, "market_group": mkt})
            if records:
                self.cache.save_listing(records, "KRX")
            return records
        except Exception as exc:
            logger.warning("Failed to fetch KRX listing via FinanceDataReader: %s", exc)
            return []

    def get_krx_etf_listing(self) -> List[Dict[str, Any]]:
        """Return cached KRX ETF listing (0 KIS API calls)."""
        cached = self.cache.load_listing("KRX_ETF")
        if cached is not None:
            return cached
        try:
            df = self._fdr_listing_fn("ETF/KR")
            records: List[Dict[str, Any]] = []
            for _, row in df.iterrows():
                code = str(row.get("Symbol", row.get("Code", ""))).strip()
                name = str(row.get("Name", "")).strip()
                if code and name:
                    records.append({"symbol": code, "name": name, "market_group": "ETF"})
            if records:
                self.cache.save_listing(records, "KRX_ETF")
            return records
        except Exception as exc:
            logger.warning("Failed to fetch KRX ETF listing via FinanceDataReader: %s", exc)
            return []

    @staticmethod
    def _normalize_symbol(symbol: str, market: str) -> str:
        sym = str(symbol).strip().upper()
        if market.upper() == "KR" and sym.isdigit() and len(sym) < 6:
            return sym.zfill(6)
        return sym

    def _fetch_kr_name_from_naver(self, norm_sym: str) -> str:
        """Fallback lookup via Naver mobile stock API (works reliably from overseas IPs / Streamlit Cloud)."""
        try:
            import requests

            resp = requests.get(
                f"https://m.stock.naver.com/api/stock/{norm_sym}/basic",
                timeout=3.0,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                name = str(data.get("stockName", "")).strip()
                if name:
                    return name
        except Exception as exc:
            logger.debug("Naver stock name lookup failed for %s: %s", norm_sym, exc)
        return ""

    def register_symbol_names(self, market: str, names: Dict[str, str]) -> None:
        """Seed or update in-memory symbol -> name mappings (e.g. from KIS holdings)."""
        mkt = market.upper()
        for sym, name in names.items():
            s = self._normalize_symbol(sym, mkt)
            n = str(name).strip()
            if s and n:
                self._symbol_names[(mkt, s)] = n
                self._missing_names.discard((mkt, s))

    def _ensure_kr_names_loaded(self) -> None:
        if self._kr_names_loaded:
            return
        self._kr_names_loaded = True
        for rec in self.get_krx_listing():
            code = self._normalize_symbol(rec["symbol"], "KR")
            self._symbol_names[("KR", code)] = str(rec["name"]).strip()
        for rec in self.get_krx_etf_listing():
            code = self._normalize_symbol(rec["symbol"], "KR")
            self._symbol_names.setdefault(("KR", code), str(rec["name"]).strip())

    def get_symbol_name(self, symbol: str, market: str = "KR") -> str:
        """Resolve display name for a KR/US symbol using cached listings + Naver fallback."""
        mkt = market.upper()
        sym = self._normalize_symbol(symbol, mkt)
        if not sym:
            return ""
        if mkt == "KR":
            self._ensure_kr_names_loaded()
        cached = self._symbol_names.get((mkt, sym))
        if cached:
            return cached
        if mkt == "KR" and (mkt, sym) not in self._missing_names and self._fdr_listing_fn is _default_fdr_listing:
            naver_name = self._fetch_kr_name_from_naver(sym)
            if naver_name:
                self._symbol_names[(mkt, sym)] = naver_name
                return naver_name
            self._missing_names.add((mkt, sym))
        return ""

    def format_symbol_display(self, symbol: str, market: str = "KR") -> str:
        """Format symbol with its human-readable name (e.g. '삼성전자 (005930)')."""
        mkt = market.upper()
        sym = self._normalize_symbol(symbol, mkt)
        name = self.get_symbol_name(sym, mkt)
        if not name or name.upper() == sym:
            return sym
        if mkt == "KR":
            return f"{name} ({sym})"
        return f"{sym} ({name})"

    def get_daily_ohlcv(
        self,
        symbol: str,
        market: str = "KR",
        lookback_days: int = 420,
        as_of: Optional[date] = None,
        include_live_quote: bool = False,
    ) -> pd.DataFrame:
        """
        Fetch daily OHLCV using incremental local cache + FinanceDataReader.
        Only queries KIS API if `include_live_quote=True` or on FDR failure fallback.
        """
        today = as_of or date.today()
        start_dt = today - timedelta(days=lookback_days)
        cached_df = self.cache.load_ohlcv(market, symbol)

        need_fetch = True
        fetch_start = start_dt

        if cached_df is not None and not cached_df.empty:
            last_cached_str = str(cached_df["date"].iloc[-1])
            try:
                last_cached_dt = datetime.strptime(last_cached_str[:10], "%Y-%m-%d").date()
                # If already cached up to today (or Friday when today is weekend), skip FDR call
                if last_cached_dt >= today or (today.weekday() >= 5 and (today - last_cached_dt).days <= 2):
                    need_fetch = False
                else:
                    # Incremental fetch: overlap by 2 days to cover revisions
                    fetch_start = max(start_dt, last_cached_dt - timedelta(days=2))
            except ValueError:
                pass

        df = cached_df
        if need_fetch:
            try:
                incoming = self._fdr_reader(
                    symbol,
                    fetch_start.strftime("%Y-%m-%d"),
                    today.strftime("%Y-%m-%d"),
                )
                if incoming is not None and not incoming.empty:
                    df = self.cache.merge_and_save_ohlcv(market, symbol, cached_df, incoming)
            except Exception as exc:
                logger.warning("FinanceDataReader failed for %s (%s): %s", symbol, market, exc)
                if df is None and self.kis_client is not None and hasattr(self.kis_client, "get_daily_ohlcv_fallback"):
                    try:
                        fallback_df = self.kis_client.get_daily_ohlcv_fallback(symbol, market=market)
                        if fallback_df is not None and not fallback_df.empty:
                            df = self.cache.save_ohlcv(market, symbol, fallback_df)
                    except Exception as fb_exc:
                        logger.error("KIS OHLCV fallback also failed for %s: %s", symbol, fb_exc)

        if df is None:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        if include_live_quote:
            live_quote = self.get_live_quote(symbol, market=market)
            if live_quote and float(live_quote.get("price", 0.0)) > 0:
                df = self.stitch_live_quote(df, live_quote, today_str=today.strftime("%Y-%m-%d"))

        return df

    def get_batch_daily_ohlcv(
        self,
        symbols: Sequence[str],
        market: str = "KR",
        lookback_days: int = 420,
        as_of: Optional[date] = None,
    ) -> Dict[str, pd.DataFrame]:
        """
        Stage 1 high-speed parallel OHLCV loader using FinanceDataReader + incremental cache.
        Makes 0 KIS API calls.
        """
        results: Dict[str, pd.DataFrame] = {}
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols:
            return results

        workers = min(self.max_workers, len(unique_symbols))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    self.get_daily_ohlcv,
                    sym,
                    market,
                    lookback_days,
                    as_of,
                    False,
                ): sym
                for sym in unique_symbols
            }
            for fut in as_completed(future_map):
                sym = future_map[fut]
                try:
                    df = fut.result()
                    if not df.empty:
                        results[sym] = df
                except Exception as exc:
                    logger.warning("Batch OHLCV failed for %s: %s", sym, exc)
        return results

    def get_live_quote(self, symbol: str, market: str = "KR") -> Optional[Dict[str, Any]]:
        """
        Stage 2 live quote lookup via KIS API with short TTL cache.
        """
        if self.kis_client is None:
            return None
        key = (market.upper(), symbol.upper())
        now = time.monotonic()
        if key in self._quote_cache:
            ts, cached_quote = self._quote_cache[key]
            if (now - ts) < self.quote_ttl_seconds:
                return dict(cached_quote)

        quote = self.kis_client.get_current_price(symbol, market=market)
        if quote:
            self._quote_cache[key] = (now, dict(quote))
        return quote

    @staticmethod
    def stitch_live_quote(
        df: pd.DataFrame,
        live_quote: Dict[str, Any],
        today_str: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Merge today's real-time KIS quote onto the end of a historical OHLCV DataFrame.
        """
        work = normalize_ohlcv(df)
        price = float(live_quote.get("price", 0.0) or 0.0)
        if price <= 0:
            return work

        day = today_str or date.today().strftime("%Y-%m-%d")
        open_p = float(live_quote.get("open", price) or price)
        high_p = max(float(live_quote.get("high", price) or price), price)
        low_p = min(float(live_quote.get("low", price) or price), price)
        vol = float(live_quote.get("volume", 0.0) or 0.0)

        if not work.empty and str(work["date"].iloc[-1]) == day:
            idx = work.index[-1]
            work.loc[idx, "close"] = price
            work.loc[idx, "high"] = max(float(work.loc[idx, "high"]), high_p)
            work.loc[idx, "low"] = min(float(work.loc[idx, "low"]), low_p)
            if vol > 0:
                work.loc[idx, "volume"] = max(float(work.loc[idx, "volume"]), vol)
        else:
            new_row = pd.DataFrame(
                [{"date": day, "open": open_p, "high": high_p, "low": low_p, "close": price, "volume": vol}]
            )
            work = pd.concat([work, new_row], ignore_index=True)

        return work.reset_index(drop=True)
