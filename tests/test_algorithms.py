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


def test_registry_has_exact_4_algorithms_excluding_momentum_and_rebound() -> None:
    all_ids = {a.id for a in default_registry.list_all()}
    assert all_ids == {
        "kr_monthly_trend",
        "kr_cup_and_handle",
        "kr_fujimoto_126",
        "us_infinite_buying",
    }
    kr_ids = {a.id for a in default_registry.list_all(market=Market.KR)}
    us_ids = {a.id for a in default_registry.list_all(market=Market.US)}
    assert kr_ids == {"kr_monthly_trend", "kr_cup_and_handle", "kr_fujimoto_126"}
    assert us_ids == {"us_infinite_buying"}


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
