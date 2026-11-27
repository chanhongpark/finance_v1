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

from core.data.cache_store import SharedMarketCache, has_overlap_price_divergence
from core.indicators import normalize_ohlcv
from core.market_calendar import is_market_regular_session_open, last_closed_session, market_now

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
        holiday_recheck_seconds: float = 4 * 3600.0,
        post_close_recheck_seconds: float = 15 * 60.0,
        split_divergence_threshold: float = 0.01,
    ) -> None:
        self.cache = cache or SharedMarketCache()
        self.kis_client = kis_client
        self._fdr_reader = fdr_reader or _default_fdr_reader
        self._fdr_listing_fn = fdr_listing_fn or _default_fdr_listing
        self.quote_ttl_seconds = quote_ttl_seconds
        self.max_workers = max_workers
        self.holiday_recheck_seconds = holiday_recheck_seconds
        self.post_close_recheck_seconds = post_close_recheck_seconds
        self.split_divergence_threshold = split_divergence_threshold
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

    @staticmethod
    def _parse_ymd(val: Any) -> Optional[date]:
        if not val:
            return None
        try:
            return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    def get_daily_ohlcv(
        self,
        symbol: str,
        market: str = "KR",
        lookback_days: int = 420,
        as_of: Optional[date] = None,
        include_live_quote: bool = False,
        now: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """
        Fetch daily OHLCV using incremental local cache + FinanceDataReader.

        - Normalizes KR symbols to 6 digits so cache keys never fragment.
        - Fetches only completed sessions up to `last_closed_session(market)` (or explicit `as_of`),
          preventing incomplete intraday bars from being frozen in disk cache.
        - Backfills older history when a caller requests a wider `lookback_days` than cached.
        - Detects stock splits / adjusted-price revisions on overlapping bars (>= 1% close diff)
          and re-fetches full history to keep series consistent.
        - Records sidecar `checked_at` metadata to avoid repeated FDR calls on unscheduled holidays.
        - Slices returned DataFrame to the requested `lookback_days` window.
        - Stitches KIS live quote only during the market's regular trading session (using market local date),
          unless `as_of` is explicitly supplied.
        """
        mkt = market.upper()
        fetch_sym = self._normalize_symbol(symbol, mkt)
        local_now = market_now(mkt, dt=now)
        target_end = as_of if as_of is not None else last_closed_session(mkt, dt=now)
        start_dt = target_end - timedelta(days=lookback_days)
        target_end_str = target_end.strftime("%Y-%m-%d")
        start_dt_str = start_dt.strftime("%Y-%m-%d")
        now_epoch = now.timestamp() if now is not None else time.time()

        cached_df = self.cache.load_ohlcv(mkt, fetch_sym)
        meta = self.cache.load_ohlcv_meta(mkt, fetch_sym)

        need_head = False
        need_tail = False
        first_cached_dt: Optional[date] = None
        last_cached_dt: Optional[date] = None
        fetched_from_dt = self._parse_ymd(meta.get("fetched_from"))

        # Shorter recheck cooldown right after close on a trading day (for FDR closing-bar publication lag)
        is_post_close_window = (
            as_of is None
            and target_end == local_now.date()
            and (local_now.hour < 19 if mkt == "KR" else local_now.hour < 19)
        )
        effective_recheck_seconds = (
            self.post_close_recheck_seconds if is_post_close_window else self.holiday_recheck_seconds
        )

        if cached_df is None or cached_df.empty:
            need_head = True
            need_tail = True
        else:
            first_cached_dt = self._parse_ymd(cached_df["date"].iloc[0])
            last_cached_dt = self._parse_ymd(cached_df["date"].iloc[-1])

            # Check if caller asks for earlier history than what is cached (with 7-day holiday buffer)
            if first_cached_dt is None or (
                first_cached_dt > start_dt + timedelta(days=7)
                and (fetched_from_dt is None or fetched_from_dt > start_dt)
            ):
                need_head = True

            # Check if newer completed sessions are missing
            if last_cached_dt is None or last_cached_dt < target_end:
                if (
                    as_of is None
                    and meta.get("checked_target_date") == target_end_str
                    and (now_epoch - float(meta.get("checked_at", 0.0) or 0.0)) < effective_recheck_seconds
                ):
                    need_tail = False
                else:
                    need_tail = True

        def _fetch_fdr_capped(f_start: date, f_end: date) -> pd.DataFrame:
            raw = self._fdr_reader(
                fetch_sym,
                f_start.strftime("%Y-%m-%d"),
                f_end.strftime("%Y-%m-%d"),
            )
            if raw is None or raw.empty:
                return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
            norm = normalize_ohlcv(raw)
            if norm.empty:
                return norm
            # Never persist incomplete intraday bars beyond target_end
            end_cap = f_end.strftime("%Y-%m-%d")
            return norm[norm["date"] <= end_cap].reset_index(drop=True)

        df = cached_df
        if need_head or need_tail:
            fdr_succeeded = False
            incoming: Optional[pd.DataFrame] = None
            full_refetch_df: Optional[pd.DataFrame] = None
            refetch_start: Optional[date] = None
            is_full_fetch = cached_df is None or cached_df.empty or need_head

            try:
                if is_full_fetch:
                    incoming = _fetch_fdr_capped(start_dt, target_end)
                    fdr_succeeded = True
                else:
                    assert last_cached_dt is not None
                    inc_start = max(start_dt, last_cached_dt - timedelta(days=2))
                    incoming = _fetch_fdr_capped(inc_start, target_end)
                    fdr_succeeded = True
                    if not incoming.empty and has_overlap_price_divergence(
                        cached_df, incoming, threshold=self.split_divergence_threshold
                    ):
                        logger.warning(
                            "Price divergence (possible split/revision) detected for %s (%s); refetching full history",
                            fetch_sym,
                            mkt,
                        )
                        refetch_start = min(start_dt, first_cached_dt) if first_cached_dt else start_dt
                        full_refetch_df = _fetch_fdr_capped(refetch_start, target_end)
            except Exception as exc:
                logger.warning("FinanceDataReader failed for %s (%s): %s", fetch_sym, mkt, exc)

            if fdr_succeeded:
                try:
                    if is_full_fetch:
                        if incoming is not None and not incoming.empty:
                            if (
                                cached_df is not None
                                and not cached_df.empty
                                and not has_overlap_price_divergence(
                                    cached_df, incoming, threshold=self.split_divergence_threshold
                                )
                            ):
                                df = self.cache.merge_and_save_ohlcv(mkt, fetch_sym, cached_df, incoming)
                            else:
                                df = self.cache.save_ohlcv(mkt, fetch_sym, incoming)
                        effective_from = min(start_dt, fetched_from_dt) if fetched_from_dt else start_dt
                        meta["fetched_from"] = effective_from.strftime("%Y-%m-%d")
                    elif full_refetch_df is not None:
                        if not full_refetch_df.empty:
                            df = self.cache.save_ohlcv(mkt, fetch_sym, full_refetch_df)
                            if refetch_start is not None:
                                meta["fetched_from"] = refetch_start.strftime("%Y-%m-%d")
                        elif incoming is not None and not incoming.empty:
                            df = self.cache.save_ohlcv(mkt, fetch_sym, incoming)
                    elif incoming is not None and not incoming.empty:
                        df = self.cache.merge_and_save_ohlcv(mkt, fetch_sym, cached_df, incoming)

                    if df is not None and not df.empty:
                        meta["checked_at"] = now_epoch
                        meta["checked_target_date"] = target_end_str
                        self.cache.save_ohlcv_meta(mkt, fetch_sym, meta)
                except OSError as exc:
                    logger.warning("OHLCV cache write failed for %s (%s): %s", fetch_sym, mkt, exc)
                    if full_refetch_df is not None and not full_refetch_df.empty:
                        df = full_refetch_df
                    elif incoming is not None and not incoming.empty:
                        df = normalize_ohlcv(
                            pd.concat([cached_df, incoming], ignore_index=True)
                            if (cached_df is not None and not cached_df.empty and not is_full_fetch)
                            else incoming
                        )

        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        # Slice strictly to [start_dt, target_end] while keeping full history on disk
        df = df[(df["date"] >= start_dt_str) & (df["date"] <= target_end_str)].reset_index(drop=True)

        if include_live_quote:
            stitch_day = self._resolve_live_stitch_date(df, mkt, as_of=as_of, now=now)
            if stitch_day is not None:
                live_quote = self.get_live_quote(fetch_sym, market=mkt)
                if live_quote:
                    df = self.stitch_live_quote(df, live_quote, today_str=stitch_day)

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
        mkt = market.upper()
        norm_sym = self._normalize_symbol(symbol, mkt)
        key = (mkt, norm_sym)
        now = time.monotonic()
        if key in self._quote_cache:
            ts, cached_quote = self._quote_cache[key]
            if (now - ts) < self.quote_ttl_seconds:
                return dict(cached_quote)

        try:
            quote = self.kis_client.get_current_price(norm_sym, market=mkt)
        except Exception as exc:
            logger.warning("KIS live quote failed for %s (%s): %s", norm_sym, mkt, exc)
            quote = None
        if quote:
            self._quote_cache[key] = (now, dict(quote))
        return quote

    @staticmethod
    def _resolve_live_stitch_date(
        df: Optional[pd.DataFrame],
        market: str = "KR",
        *,
        as_of: Optional[date] = None,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Return YYYY-MM-DD session date to stitch, or None if live stitching should be skipped."""
        if as_of is not None:
            return as_of.strftime("%Y-%m-%d")

        mkt = market.upper()
        local_today = market_now(mkt, dt=now).date()
        local_today_str = local_today.strftime("%Y-%m-%d")

        if is_market_regular_session_open(mkt, dt=now):
            return local_today_str

        # Post-close gap fill: today was a real trading day and regular session has closed,
        # but cached FDR data doesn't have today's bar yet -> KIS quote has today's closing price.
        if last_closed_session(mkt, dt=now) == local_today:
            last_bar_date = str(df["date"].iloc[-1]) if (df is not None and not df.empty) else ""
            if last_bar_date < local_today_str:
                return local_today_str

        return None

    def stitch_live_quote_if_applicable(
        self,
        df: pd.DataFrame,
        live_quote: Dict[str, Any],
        market: str = "KR",
        *,
        as_of: Optional[date] = None,
        now: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """
        Stitch a KIS live quote onto `df` ONLY when it represents a valid session candle.
        Never adds a fake candle on pre-market, weekends, or market holidays.
        """
        if not live_quote or float(live_quote.get("price", 0.0) or 0.0) <= 0:
            return df
        stitch_day = self._resolve_live_stitch_date(df, market=market, as_of=as_of, now=now)
        if stitch_day is None:
            return df
        return self.stitch_live_quote(df, live_quote, today_str=stitch_day)

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
