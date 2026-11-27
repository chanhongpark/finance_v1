"""
Shared OHLCV & KRX listing local incremental cache (data/shared/).

Market data is non-sensitive public data, so caching it in data/shared/
eliminates redundant FinanceDataReader / KIS API calls and dramatically
reduces screening latency.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from core.indicators import normalize_ohlcv


def normalize_cache_symbol(symbol: str, market: str) -> str:
    sym = str(symbol).strip().upper().replace("/", "_")
    if market.upper() == "KR" and sym.isdigit() and len(sym) < 6:
        return sym.zfill(6)
    return sym


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write text to a temp file in the same directory and atomically replace `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp_path, "w", encoding=encoding, newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        # Retry briefly on Windows where a concurrent reader can raise PermissionError
        for attempt, delay in enumerate((0.02, 0.05, 0.1, 0.0)):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(delay)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def has_overlap_price_divergence(
    existing: Optional[pd.DataFrame],
    incoming: Optional[pd.DataFrame],
    threshold: float = 0.01,
) -> bool:
    """
    Detect stock splits / reverse splits / adjusted-price revisions by comparing
    closing prices on overlapping dates between cached and incoming DataFrames.
    """
    if existing is None or existing.empty or incoming is None or incoming.empty:
        return False
    ex_norm = normalize_ohlcv(existing)
    inc_norm = normalize_ohlcv(incoming)
    if ex_norm.empty or inc_norm.empty:
        return False

    merged = pd.merge(
        ex_norm[["date", "close"]],
        inc_norm[["date", "close"]],
        on="date",
        suffixes=("_old", "_new"),
    )
    if merged.empty:
        return False

    for _, row in merged.iterrows():
        old_c = float(row["close_old"])
        new_c = float(row["close_new"])
        if old_c > 0 and abs(new_c - old_c) / old_c >= threshold:
            return True
    return False


class SharedMarketCache:
    """File-backed incremental cache for OHLCV bars and stock master listings."""

    def __init__(self, shared_dir: Path | str = Path("data") / "shared") -> None:
        self.shared_dir = Path(shared_dir)
        self.ohlcv_dir = self.shared_dir / "ohlcv"
        self.ohlcv_dir.mkdir(parents=True, exist_ok=True)

    def _ohlcv_path(self, market: str, symbol: str) -> Path:
        safe_symbol = normalize_cache_symbol(symbol, market)
        path = self.ohlcv_dir / market.upper() / f"{safe_symbol}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _ohlcv_meta_path(self, market: str, symbol: str) -> Path:
        safe_symbol = normalize_cache_symbol(symbol, market)
        path = self.ohlcv_dir / market.upper() / f"{safe_symbol}.meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def load_ohlcv(self, market: str, symbol: str) -> Optional[pd.DataFrame]:
        path = self._ohlcv_path(market, symbol)
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path)
            norm = normalize_ohlcv(df)
            return norm if not norm.empty else None
        except Exception:
            return None

    def save_ohlcv(self, market: str, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        norm = normalize_ohlcv(df)
        path = self._ohlcv_path(market, symbol)
        _atomic_write_text(path, norm.to_csv(index=False))
        return norm

    def merge_and_save_ohlcv(
        self,
        market: str,
        symbol: str,
        existing: Optional[pd.DataFrame],
        incoming: pd.DataFrame,
    ) -> pd.DataFrame:
        inc_norm = normalize_ohlcv(incoming)
        if existing is None or existing.empty:
            return self.save_ohlcv(market, symbol, inc_norm)
        if inc_norm.empty:
            return existing
        combined = pd.concat([existing, inc_norm], ignore_index=True)
        return self.save_ohlcv(market, symbol, combined)

    def load_ohlcv_meta(self, market: str, symbol: str) -> Dict[str, Any]:
        path = self._ohlcv_meta_path(market, symbol)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_ohlcv_meta(self, market: str, symbol: str, meta: Dict[str, Any]) -> None:
        path = self._ohlcv_meta_path(market, symbol)
        _atomic_write_text(path, json.dumps(meta, ensure_ascii=False))

    def load_listing(self, market: str = "KRX", max_age_seconds: float = 86400.0) -> Optional[List[Dict[str, Any]]]:
        path = self.shared_dir / f"listing_{market.upper()}.json"
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > max_age_seconds:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_listing(self, records: List[Dict[str, Any]], market: str = "KRX") -> None:
        path = self.shared_dir / f"listing_{market.upper()}.json"
        _atomic_write_text(path, json.dumps(records, ensure_ascii=False))
