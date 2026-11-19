"""
Shared OHLCV & KRX listing local incremental cache (data/shared/).

Market data is non-sensitive public data, so caching it in data/shared/
eliminates redundant FinanceDataReader / KIS API calls and dramatically
reduces screening latency.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from core.indicators import normalize_ohlcv


class SharedMarketCache:
    """File-backed incremental cache for OHLCV bars and stock master listings."""

    def __init__(self, shared_dir: Path | str = Path("data") / "shared") -> None:
        self.shared_dir = Path(shared_dir)
        self.ohlcv_dir = self.shared_dir / "ohlcv"
        self.ohlcv_dir.mkdir(parents=True, exist_ok=True)

    def _ohlcv_path(self, market: str, symbol: str) -> Path:
        safe_symbol = symbol.strip().upper().replace("/", "_")
        path = self.ohlcv_dir / market.upper() / f"{safe_symbol}.csv"
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
        norm.to_csv(path, index=False)
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
