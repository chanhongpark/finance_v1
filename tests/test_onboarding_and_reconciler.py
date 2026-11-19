"""Tests for daily reconciliation & existing holding manual registration/mapping/splitting."""
from datetime import date, timedelta

import pandas as pd

from core.algorithms import USInfiniteBuyingAlgorithm
from core.ledger.calculator import recalculate_lot_from_fills
from core.ledger.onboarding import (
    assign_algorithm_to_manual_lot,
    build_fujimoto_initial_state,
    build_infinite_buying_initial_state,
    register_existing_holding,
)
from core.ledger.reconciler import reconcile_holdings


def test_reconcile_creates_manual_lot_then_split_assigns_two_algorithms() -> None:
    kis_holdings = [
        {"symbol": "005930", "market": "KR", "qty": 100, "avg_price": 72_000.0},
    ]
    report = reconcile_holdings(kis_holdings, sheet_lots=[])
    assert not report.is_matched
    assert len(report.created_manual_lots) == 1
    manual_lot = report.created_manual_lots[0]
    assert manual_lot.algo_id == "manual"
    assert manual_lot.qty == 100

    all_fills = list(report.created_fills)

    # Split 60 shares to kr_monthly_trend, leaving 40 in manual_lot
    updated_lots, split_fills, audit = assign_algorithm_to_manual_lot(
        manual_lot=manual_lot,
        target_algo_id="kr_monthly_trend",
        target_algo_version="1.0.0",
        assign_qty=60,
        buy_date="2026-06-15",
    )
    all_fills.extend(split_fills)
    assert len(updated_lots) == 2
    rem_manual, monthly_lot = updated_lots[0], updated_lots[1]

    assert rem_manual.qty == 40
    assert monthly_lot.qty == 60
    assert monthly_lot.algo_id == "kr_monthly_trend"
    assert monthly_lot.buy_date == "2026-06-15"

    # Verify append-only Fills balance matches remaining qty on both lots
    assert recalculate_lot_from_fills(rem_manual, all_fills).remaining_qty == 40
    assert recalculate_lot_from_fills(monthly_lot, all_fills).remaining_qty == 60

    # Assign remaining 40 shares to Fujimoto 1:2:6 at Stage 2
    fujimoto_state = build_fujimoto_initial_state(entry_stage=2, exit_stage=0, base_entry_price=72_000.0)
    final_lots, _, _ = assign_algorithm_to_manual_lot(
        manual_lot=rem_manual,
        target_algo_id="kr_fujimoto_126",
        target_algo_version="1.0.0",
        assign_qty=40,
        algo_state=fujimoto_state,
    )
    assert final_lots[0].algo_id == "kr_fujimoto_126"
    assert final_lots[0].algo_state["entry_stage"] == 2

    # Re-reconcile: KIS(100) == Sheet(60 + 40) -> matched!
    report2 = reconcile_holdings(kis_holdings, sheet_lots=[monthly_lot, final_lots[0]])
    assert report2.is_matched


def test_manual_us_infinite_holding_auto_calculates_t_round_and_generates_orders() -> None:
    # Existing holding: 50 shares of TQQQ @ $50 = $2,500 invested out of $10,000 / 40 = $250/round -> T=10.0
    state = build_infinite_buying_initial_state(qty=50, avg_price=50.0, total_budget_usd=10_000.0, division_count=40)
    assert state["current_round"] == 10.0

    lot, _, _ = register_existing_holding(
        symbol="TQQQ",
        market="US",
        qty=50,
        avg_price=50.0,
        algo_id="us_infinite_buying",
        algo_version="2.1.0",
        algo_state=state,
    )

    today = date.today()
    df = pd.DataFrame(
        [
            {"date": (today - timedelta(days=2)).strftime("%Y-%m-%d"), "open": 49, "high": 51, "low": 48, "close": 50.0, "volume": 1000},
            {"date": (today - timedelta(days=1)).strftime("%Y-%m-%d"), "open": 50, "high": 52, "low": 49, "close": 51.0, "volume": 1200},
        ]
    )
    algo = USInfiniteBuyingAlgorithm()
    signals = algo.generate_daily_signals("TQQQ", df, lot=lot)
    # First half (T=10 < 20): 1 Limit SELL (+10% = $55.00) + 2 LOC BUYs
    sells = [s for s in signals if s.action.value == "SELL"]
    buys = [s for s in signals if s.action.value == "BUY"]
    assert len(sells) == 1
    assert sells[0].price == 55.0
    assert len(buys) == 2
