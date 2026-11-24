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


def test_kis_us_quote_falls_back_across_nas_nys_ams_and_caches(tmp_path: Path, monkeypatch) -> None:
    from core.kis.client import KISClient

    ctx = UserContext(
        "u_us",
        UserCredentials(kis_app_key="k", kis_app_secret="s", kis_account_no="12345678-01", is_mock=True),
        "sheet_us",
        base_data_dir=tmp_path / "data",
    )
    client = KISClient(ctx, calls_per_second=100.0)
    monkeypatch.setattr(client, "get_access_token", lambda: "fake_token")

    tried_exchanges: list[str] = []

    class FakeResp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    def fake_get(url, headers=None, params=None, timeout=None):
        excd = params["EXCD"]
        tried_exchanges.append(excd)
        if excd == "AMS":
            return FakeResp({"output": {"last": "32.45", "open": "31.90", "high": "33.00", "low": "31.50", "tvol": "12345"}})
        return FakeResp({"output": {"last": "", "base": ""}})

    monkeypatch.setattr("core.kis.client.requests.get", fake_get)

    # First lookup tries NAS -> NYS -> AMS and succeeds on AMS
    quote = client.get_current_price("SOXL", market="US")
    assert quote is not None
    assert quote["price"] == 32.45
    assert quote["exchange"] == "AMS"
    assert tried_exchanges == ["NAS", "NYS", "AMS"]

    # Second lookup hits cached AMS immediately (1 call)
    tried_exchanges.clear()
    quote2 = client.get_current_price("SOXL", market="US")
    assert quote2 is not None
    assert quote2["price"] == 32.45
    assert tried_exchanges == ["AMS"]
