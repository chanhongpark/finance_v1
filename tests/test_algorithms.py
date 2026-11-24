"""Tests for all 4 registered algorithms (KR 3 + US 1), conditions, and R14 latest-version exit."""
from datetime import date, timedelta

import pandas as pd

from core.algorithms import (
    AlgorithmRegistry,
    KRMonthlyTrendAlgorithm,
    Market,
    default_registry,
)
from core.ledger.models import Lot


def test_registry_has_kr_3_and_us_4_algorithms() -> None:
    all_ids = {a.id for a in default_registry.list_all()}
    assert all_ids == {
        "kr_monthly_trend",
        "kr_cup_and_handle",
        "kr_fujimoto_126",
        "us_monthly_trend",
        "us_cup_and_handle",
        "us_fujimoto_126",
        "us_infinite_buying",
    }
    kr_ids = {a.id for a in default_registry.list_all(market=Market.KR)}
    us_ids = {a.id for a in default_registry.list_all(market=Market.US)}
    assert kr_ids == {"kr_monthly_trend", "kr_cup_and_handle", "kr_fujimoto_126"}
    assert us_ids == {
        "us_monthly_trend",
        "us_cup_and_handle",
        "us_fujimoto_126",
        "us_infinite_buying",
    }


def test_kr_monthly_trend_golden_cross_and_latest_version_exit_r14() -> None:
    # 12 monthly bars: 10 bars around 10,000, month 11 at 9,800 (below SMA), month 12 breaks out to 10,600
    closes = [10_000] * 10 + [9_800, 10_600]
    dates = [f"2025-{m:02d}-28" for m in range(1, 13)]
    monthly_df = pd.DataFrame(
        {"date": dates, "open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * 12}
    )

    algo = default_registry.require("kr_monthly_trend")
    sig = algo.evaluate_entry("005930", monthly_df, extra={"monthly_df": monthly_df})
    assert sig is not None
    assert sig.action.value == "BUY"
    assert len(sig.conditions) >= 3
    assert all(c.passed for c in sig.conditions)

    # R14: Lot bought under v1.0.0 is evaluated by latest v2.0.0 registered instance
    class UpgradedMonthlyTrend(KRMonthlyTrendAlgorithm):
        version = "2.0.0"

    local_reg = AlgorithmRegistry()
    local_reg.register(UpgradedMonthlyTrend)

    old_lot = Lot(
        lot_id="lot_old",
        symbol="005930",
        market="KR",
        algo_id="kr_monthly_trend",
        algo_version="1.0.0",
        buy_date="2025-12-28",
        qty=10,
        avg_price=10_600.0,
    )
    # Price drops -8% -> triggers emergency stop loss under latest v2.0.0
    drop_df = pd.DataFrame(
        [{"date": "2026-01-10", "open": 9_800, "high": 9_800, "low": 9_700, "close": 9_700, "volume": 1000}]
    )
    latest_algo = local_reg.require(old_lot.algo_id)
    exit_sig = latest_algo.evaluate_exit(old_lot, drop_df)
    assert exit_sig is not None
    assert exit_sig.action.value == "SELL"
    assert exit_sig.algo_version == "2.0.0"


def test_kr_fujimoto_126_split_entry_and_exit_stages() -> None:
    algo = default_registry.require("kr_fujimoto_126")
    # Build 30-bar series where RSI drops below 30 then rebounds above 30
    base = date(2026, 9, 1)
    prices = [10000 - i * 250 for i in range(28)] + [3000, 3400]
    rows = [
        {"date": (base + timedelta(days=i)).strftime("%Y-%m-%d"), "open": p, "high": p + 50, "low": p - 50, "close": p, "volume": 1000}
        for i, p in enumerate(prices)
    ]
    df = pd.DataFrame(rows)

    sig1 = algo.evaluate_entry("005930", df)
    assert sig1 is not None
    assert sig1.action.value == "BUY"
    assert sig1.metadata["next_entry_stage"] == 1

    # Emergency stop-loss exit on a held lot
    lot = Lot("l_fuj", "005930", "KR", "kr_fujimoto_126", "1.0.0", "2026-09-30", qty=9, avg_price=4000.0, algo_state={"entry_stage": 1, "exit_stage": 0})
    exit_sig = algo.evaluate_exit(lot, df)
    assert exit_sig is not None
    assert exit_sig.action.value == "SELL"
    assert exit_sig.qty == 9
    assert exit_sig.metadata["next_exit_stage"] == 3


def test_us_monthly_trend_and_fujimoto_use_usd_pricing() -> None:
    # US Monthly Trend: 12 monthly bars in USD with decimal prices
    closes = [100.0] * 10 + [98.5, 106.25]
    dates = [f"2025-{m:02d}-28" for m in range(1, 13)]
    monthly_df = pd.DataFrame(
        {"date": dates, "open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * 12}
    )

    us_trend = default_registry.require("us_monthly_trend")
    buy_sig = us_trend.evaluate_entry("AAPL", monthly_df, extra={"monthly_df": monthly_df})
    assert buy_sig is not None
    assert buy_sig.market == Market.US
    assert buy_sig.algo_id == "us_monthly_trend"
    assert buy_sig.price == 106.25
    assert buy_sig.qty == 9  # $1,000 default budget // $106.25

    us_lot = Lot("l_us_tr", "AAPL", "US", "us_monthly_trend", "1.0.0", "2025-12-28", qty=5, avg_price=106.25)
    drop_df = pd.DataFrame(
        [{"date": "2026-01-10", "open": 98.0, "high": 98.0, "low": 97.25, "close": 97.25, "volume": 1000}]
    )
    sell_sig = us_trend.evaluate_exit(us_lot, drop_df)
    assert sell_sig is not None
    assert sell_sig.market == Market.US
    assert sell_sig.price == 97.25

    # US Fujimoto 1:2:6: RSI oversold rebound in USD
    us_fuj = default_registry.require("us_fujimoto_126")
    base = date(2026, 9, 1)
    prices = [100.0 - i * 2.5 for i in range(28)] + [30.0, 34.5]
    rows = [
        {"date": (base + timedelta(days=i)).strftime("%Y-%m-%d"), "open": p, "high": p + 0.5, "low": p - 0.5, "close": p, "volume": 1000}
        for i, p in enumerate(prices)
    ]
    df = pd.DataFrame(rows)
    fuj_buy = us_fuj.evaluate_entry("NVDA", df)
    assert fuj_buy is not None
    assert fuj_buy.market == Market.US
    assert fuj_buy.algo_id == "us_fujimoto_126"
    assert fuj_buy.price == 34.5
    assert fuj_buy.metadata["next_entry_stage"] == 1


def test_run_us_screening_generates_buy_and_sell_excluding_infinite_buying(tmp_path) -> None:
    from core.context import UserContext, UserCredentials
    from core.data.cache_store import SharedMarketCache
    from core.data.market_data_provider import HybridMarketDataProvider
    from core.execution.runner import run_us_screening
    from core.sheets.user_sheet_manager import InMemorySheetBackend, UserSheetManager

    ctx = UserContext(
        user_id="tester_us",
        sheet_id="sheet_us",
        credentials=UserCredentials(kis_app_key="k", kis_app_secret="s", kis_account_no="12345678-01"),
    )
    sheet = UserSheetManager(ctx, InMemorySheetBackend("sheet_us"))
    sheet.ensure_schema()

    # Open US lot on us_fujimoto_126 (should trigger stop-loss sell) and one on us_infinite_buying (should be excluded)
    sheet.upsert_lots([
        Lot("l_fuj_us", "NVDA", "US", "us_fujimoto_126", "1.0.0", "2026-09-01", qty=9, avg_price=50.0, algo_state={"entry_stage": 1, "exit_stage": 0}),
        Lot("l_inf_us", "TQQQ", "US", "us_infinite_buying", "1.0.0", "2026-09-01", qty=10, avg_price=100.0, algo_state={"current_round": 2.0}),
    ])

    base = date(2026, 9, 1)
    prices = [100.0 - i * 2.5 for i in range(28)] + [30.0, 34.5]
    df_nvda = pd.DataFrame([
        {"date": (base + timedelta(days=i)).strftime("%Y-%m-%d"), "open": p, "high": p + 0.5, "low": p - 0.5, "close": p, "volume": 1000}
        for i, p in enumerate(prices)
    ])
    df_tqqq = pd.DataFrame([
        {"date": (base + timedelta(days=i)).strftime("%Y-%m-%d"), "open": 50.0, "high": 51.0, "low": 49.0, "close": 50.0, "volume": 1000}
        for i in range(30)
    ])

    def fake_fdr(symbol: str, start: str, end: str) -> pd.DataFrame:
        return df_nvda if symbol == "NVDA" else df_tqqq

    provider = HybridMarketDataProvider(
        cache=SharedMarketCache(shared_dir=tmp_path),
        kis_client=None,
        fdr_reader=fake_fdr,
    )

    result = run_us_screening(
        ctx=ctx,
        sheet_manager=sheet,
        data_provider=provider,
        universe_symbols=["nvda", "aapl"],
        include_live_quotes_for_candidates=False,
    )

    # NVDA has an existing open us_fujimoto_126 lot at avg 50.0 vs curr 34.5 -> sell signal
    assert any(s.symbol == "NVDA" and s.market == Market.US for s in result["sell"])
    # TQQQ lot belongs to us_infinite_buying -> excluded from screening sells
    assert all(s.algo_id != "us_infinite_buying" for s in result["buy"] + result["sell"])

    result2 = run_us_screening(
        ctx=ctx,
        sheet_manager=sheet,
        data_provider=HybridMarketDataProvider(
            cache=SharedMarketCache(shared_dir=tmp_path / "c2"),
            kis_client=None,
            fdr_reader=lambda sym, s, e: df_nvda,
        ),
        universe_symbols=["msft"],
        include_live_quotes_for_candidates=False,
    )
    assert any(s.symbol == "MSFT" and s.algo_id == "us_fujimoto_126" and s.market == Market.US for s in result2["buy"])
