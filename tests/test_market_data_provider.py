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


def _make_bars(start_day: date, end_day: date, close_price: float = 100.0) -> pd.DataFrame:
    rows = []
    d = start_day
    while d <= end_day:
        if d.weekday() < 5:
            rows.append(
                {
                    "Date": d.strftime("%Y-%m-%d"),
                    "Open": close_price,
                    "High": close_price * 1.01,
                    "Low": close_price * 0.99,
                    "Close": close_price,
                    "Volume": 10000,
                }
            )
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def test_lookback_expansion_backfills_head_and_slices_return(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_fdr(symbol: str, start: str, end: str) -> pd.DataFrame:
        calls.append((symbol, start, end))
        return _make_bars(date.fromisoformat(start), date.fromisoformat(end), close_price=70_000.0)

    cache = SharedMarketCache(tmp_path / "shared")
    provider = HybridMarketDataProvider(cache=cache, fdr_reader=fake_fdr)
    as_of = date(2026, 11, 20)

    # First call creates cache with 60-day lookback
    df_60 = provider.get_daily_ohlcv("005930", market="KR", lookback_days=60, as_of=as_of)
    assert len(calls) == 1
    assert df_60["date"].iloc[0] >= (as_of - timedelta(days=60)).strftime("%Y-%m-%d")

    # Second call asks for 300-day lookback -> must backfill older history!
    df_300 = provider.get_daily_ohlcv("005930", market="KR", lookback_days=300, as_of=as_of)
    assert len(calls) == 2
    assert len(df_300) > len(df_60) * 3
    assert df_300["date"].iloc[0] <= (as_of - timedelta(days=295)).strftime("%Y-%m-%d")

    # Third call asks for 60-day lookback again -> 0 FDR calls, returned DF is sliced to 60 days
    df_60_again = provider.get_daily_ohlcv("005930", market="KR", lookback_days=60, as_of=as_of)
    assert len(calls) == 2
    assert len(df_60_again) == len(df_60)
    # Full 300-day history remains intact on disk
    assert len(cache.load_ohlcv("KR", "005930")) == len(df_300)


def test_stitch_live_quote_respects_market_session_and_us_timezone(tmp_path: Path) -> None:
    from datetime import datetime
    from core.market_calendar import KST, US_EASTERN

    ctx = UserContext("u_tz", UserCredentials(is_mock=True), "sheet_tz", base_data_dir=tmp_path / "data")
    mock_kis = MockKISClient(ctx)
    mock_kis.quotes["005930"] = {"symbol": "005930", "price": 80_000.0, "open": 79_000.0, "high": 81_000.0, "low": 78_500.0, "volume": 200_000}
    mock_kis.quotes["TQQQ"] = {"symbol": "TQQQ", "price": 62.5, "open": 61.0, "high": 63.0, "low": 60.5, "volume": 500_000}

    fdr_calls: list[tuple[str, str, str]] = []

    def fake_fdr(symbol: str, start: str, end: str) -> pd.DataFrame:
        fdr_calls.append((symbol, start, end))
        # Simulate FDR returning even an extra intraday bar if requested, to verify capping
        end_d = date.fromisoformat(end)
        return _make_bars(date.fromisoformat(start), end_d, close_price=60.0 if symbol == "TQQQ" else 75_000.0)

    cache = SharedMarketCache(tmp_path / "shared")
    provider = HybridMarketDataProvider(cache=cache, kis_client=mock_kis, fdr_reader=fake_fdr)

    # 1. KR pre-market (Tue 2026-11-17 08:30 KST): last closed = Mon 2026-11-16, NO live stitch
    kr_pre = datetime(2026, 11, 17, 8, 30, tzinfo=KST)
    df_pre = provider.get_daily_ohlcv("005930", market="KR", lookback_days=30, include_live_quote=True, now=kr_pre)
    assert df_pre["date"].iloc[-1] == "2026-11-16"
    assert mock_kis.quote_call_count == 0

    # 2. KR regular session (Tue 2026-11-17 10:30 KST): cache hits Mon, stitches Tue in memory only
    kr_open = datetime(2026, 11, 17, 10, 30, tzinfo=KST)
    df_open = provider.get_daily_ohlcv("005930", market="KR", lookback_days=30, include_live_quote=True, now=kr_open)
    assert df_open["date"].iloc[-1] == "2026-11-17"
    assert float(df_open["close"].iloc[-1]) == 80_000.0
    assert mock_kis.quote_call_count == 1
    assert len(fdr_calls) == 1  # cache hit!
    # Disk cache must NOT contain incomplete Tue candle
    assert cache.load_ohlcv("KR", "005930")["date"].iloc[-1] == "2026-11-16"

    # 3. US market during KST Wed 01:00 (= Tue 2026-11-17 11:00 ET, US open):
    #    last closed US = Mon 2026-11-16, stitched date must be US Tue 2026-11-17 (NOT KST Wed 11-18!)
    us_open_kst = datetime(2026, 11, 18, 1, 0, tzinfo=KST)
    df_us = provider.get_daily_ohlcv("TQQQ", market="US", lookback_days=30, include_live_quote=True, now=us_open_kst)
    assert df_us["date"].iloc[-1] == "2026-11-17"
    assert float(df_us["close"].iloc[-1]) == 62.5
    assert cache.load_ohlcv("US", "TQQQ")["date"].iloc[-1] == "2026-11-16"

    # 4. US daytime in KST (Tue 2026-11-17 14:00 KST = Tue 00:00 ET, US closed):
    #    last closed US = Mon 2026-11-16 -> cache hit (0 extra FDR calls), NO fake stitch
    fdr_before = len(fdr_calls)
    us_closed_kst = datetime(2026, 11, 17, 14, 0, tzinfo=KST)
    df_us_closed = provider.get_daily_ohlcv("TQQQ", market="US", lookback_days=30, include_live_quote=True, now=us_closed_kst)
    assert len(fdr_calls) == fdr_before
    assert df_us_closed["date"].iloc[-1] == "2026-11-16"
    assert datetime(2026, 11, 17, 14, 0, tzinfo=KST).astimezone(US_EASTERN).date() == date(2026, 11, 17)


def test_split_divergence_triggers_full_refetch(tmp_path: Path) -> None:
    split_happened = False
    calls: list[tuple[str, str, str]] = []

    def fake_fdr(symbol: str, start: str, end: str) -> pd.DataFrame:
        calls.append((symbol, start, end))
        price = 50.0 if split_happened else 100.0  # 2-for-1 stock split
        return _make_bars(date.fromisoformat(start), date.fromisoformat(end), close_price=price)

    cache = SharedMarketCache(tmp_path / "shared")
    provider = HybridMarketDataProvider(cache=cache, fdr_reader=fake_fdr)

    # Initial cache at pre-split price 100.0
    df1 = provider.get_daily_ohlcv("SOXL", market="US", lookback_days=30, as_of=date(2026, 11, 13))
    assert float(df1["close"].iloc[0]) == 100.0
    assert len(calls) == 1

    # Next trading day: 2-for-1 split happened -> incremental overlap detects 50% diff & refetches full range
    split_happened = True
    df2 = provider.get_daily_ohlcv("SOXL", market="US", lookback_days=30, as_of=date(2026, 11, 16))
    assert len(calls) == 3  # 1 initial + 1 incremental + 1 full refetch
    assert all(float(c) == 50.0 for c in df2["close"])


def test_holiday_cooldown_and_kr_symbol_normalization_and_post_close_gap(tmp_path: Path) -> None:
    from datetime import datetime
    from core.market_calendar import KST, US_EASTERN, is_market_regular_session_open, is_us_market_holiday, last_closed_session

    # 1. Real holiday calendar checks (Thanksgiving, Chuseok, Good Friday, Observed Jul 4)
    us_thanksgiving = datetime(2026, 11, 26, 11, 0, tzinfo=US_EASTERN)
    assert is_market_regular_session_open("US", dt=us_thanksgiving) is False
    assert last_closed_session("US", dt=us_thanksgiving) == date(2026, 11, 25)
    assert is_us_market_holiday(date(2026, 4, 3)) is True   # Good Friday
    assert is_us_market_holiday(date(2026, 7, 3)) is True   # Independence Day (observed)

    kr_chuseok = datetime(2026, 9, 25, 10, 0, tzinfo=KST)
    assert is_market_regular_session_open("KR", dt=kr_chuseok) is False
    assert last_closed_session("KR", dt=kr_chuseok) == date(2026, 9, 23)

    # 2. KR 6-digit normalization + post-close 15-min cooldown + KIS post-close gap fill
    fdr_calls = 0

    def fake_fdr_lagging(symbol: str, start: str, end: str) -> pd.DataFrame:
        nonlocal fdr_calls
        fdr_calls += 1
        # Simulate FDR lagging right after close on 2026-11-18: only has up to 2026-11-17
        end_d = min(date.fromisoformat(end), date(2026, 11, 17))
        return _make_bars(date.fromisoformat(start), end_d, close_price=70_000.0)

    ctx = UserContext("u_pc", UserCredentials(is_mock=True), "sheet_pc", base_data_dir=tmp_path / "data")
    mock_kis = MockKISClient(ctx)
    mock_kis.quotes["005930"] = {"symbol": "005930", "price": 71_200.0, "open": 70_500.0, "high": 71_500.0, "low": 70_100.0, "volume": 300_000}

    cache = SharedMarketCache(tmp_path / "shared")
    provider = HybridMarketDataProvider(
        cache=cache,
        kis_client=mock_kis,
        fdr_reader=fake_fdr_lagging,
        post_close_recheck_seconds=900.0,
        holiday_recheck_seconds=14400.0,
    )

    # 16:00 KST (just after close): unpadded "5930" -> normalized "005930", fills today's close from KIS
    t1 = datetime(2026, 11, 18, 16, 0, tzinfo=KST)
    df1 = provider.get_daily_ohlcv("5930", market="KR", lookback_days=30, include_live_quote=True, now=t1)
    assert fdr_calls == 1
    assert (tmp_path / "shared" / "ohlcv" / "KR" / "005930.csv").exists()
    assert not list((tmp_path / "shared" / "ohlcv" / "KR").glob("*.tmp"))
    assert df1["date"].iloc[-1] == "2026-11-18"
    assert float(df1["close"].iloc[-1]) == 71_200.0

    # 10 min later (within 15-min post-close cooldown): 0 extra FDR calls
    t2 = datetime(2026, 11, 18, 16, 10, tzinfo=KST)
    provider.get_daily_ohlcv("005930", market="KR", lookback_days=30, now=t2)
    assert fdr_calls == 1

    # 20 min later (past 15-min post-close cooldown): retries FDR!
    t3 = datetime(2026, 11, 18, 16, 20, tzinfo=KST)
    provider.get_daily_ohlcv("005930", market="KR", lookback_days=30, now=t3)
    assert fdr_calls == 2

    # 3. Stage 2 helper `stitch_live_quote_if_applicable` must NOT add a fake bar on weekend or holiday
    base_df = cache.load_ohlcv("KR", "005930")
    sat_kst = datetime(2026, 11, 21, 12, 0, tzinfo=KST)
    stitched_sat = provider.stitch_live_quote_if_applicable(base_df, mock_kis.quotes["005930"], market="KR", now=sat_kst)
    assert len(stitched_sat) == len(base_df)

    # 4. `as_of` upper-bound slicing: requesting earlier `as_of` never leaks future cached bars
    df_past = provider.get_daily_ohlcv("005930", market="KR", lookback_days=30, as_of=date(2026, 11, 13))
    assert df_past["date"].iloc[-1] == "2026-11-13"

