"""Tests for HybridMarketDataProvider: FDR incremental cache + 0 KIS calls in Stage 1."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from core.context import UserContext, UserCredentials
from core.data.cache_store import SharedMarketCache
from core.data.market_data_provider import HybridMarketDataProvider
from core.kis.client import MockKISClient


def test_stage1_batch_uses_fdr_and_cache_with_zero_kis_calls(tmp_path: Path) -> None:
    ctx = UserContext("u1", UserCredentials(is_mock=True), "sheet_1", base_data_dir=tmp_path / "data")
    mock_kis = MockKISClient(ctx)
    mock_kis.quotes["005930"] = {"symbol": "005930", "price": 75_500.0, "open": 74_000.0, "high": 76_000.0, "low": 73_800.0, "volume": 500_000}

    fdr_calls = []

    def fake_fdr(symbol: str, start: str, end: str) -> pd.DataFrame:
        fdr_calls.append((symbol, start, end))
        base = date(2026, 11, 10)
        rows = []
        for i in range(5):
            d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append({"Date": d, "Open": 70000 + i * 500, "High": 71000 + i * 500, "Low": 69500 + i * 500, "Close": 70500 + i * 500, "Volume": 100000})
        return pd.DataFrame(rows)

    cache = SharedMarketCache(tmp_path / "data" / "shared")
    provider = HybridMarketDataProvider(cache=cache, kis_client=mock_kis, fdr_reader=fake_fdr)

    # Stage 1 batch screening on 3 symbols -> 0 KIS API calls!
    as_of_day = date(2026, 11, 14)
    batch = provider.get_batch_daily_ohlcv(["005930", "000660", "035420"], market="KR", as_of=as_of_day)
    assert len(batch) == 3
    assert mock_kis.quote_call_count == 0
    assert len(fdr_calls) == 3

    # Second call on the same day hits local cache -> 0 additional FDR calls, 0 KIS calls!
    batch2 = provider.get_batch_daily_ohlcv(["005930", "000660", "035420"], market="KR", as_of=as_of_day)
    assert len(batch2) == 3
    assert len(fdr_calls) == 3
    assert mock_kis.quote_call_count == 0

    # Stage 2 live quote stitching for 1 candidate -> exactly 1 KIS API call!
    live_df = provider.get_daily_ohlcv("005930", market="KR", as_of=date(2026, 11, 16), include_live_quote=True)
    assert mock_kis.quote_call_count == 1
    assert float(live_df["close"].iloc[-1]) == 75_500.0
    assert str(live_df["date"].iloc[-1]) == "2026-11-16"
