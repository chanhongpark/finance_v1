"""Tests for append-only Fill ledger, multi-lot sell allocation, and algo performance (R2.2, R5, R13)."""
from datetime import date

from core.ledger.calculator import (
    allocate_sell_across_lots,
    apply_fills_to_lot,
    calculate_algorithm_performance,
    recalculate_lot_from_fills,
)
from core.ledger.models import Fill, FillSide, Lot, LotStatus


def test_append_only_fills_recalculate_qty_avg_and_realized_pnl() -> None:
    lot = Lot(
        lot_id="lot_1",
        symbol="005930",
        market="KR",
        algo_id="kr_fujimoto_126",
        algo_version="1.0.0",
        buy_date="2026-10-01",
    )
    fills = [
        Fill("f1", "lot_1", FillSide.BUY, "2026-10-01", qty=10, price=70_000, fee=100, tax=0, order_no="O1"),
        Fill("f2", "lot_1", FillSide.BUY, "2026-10-05", qty=20, price=73_000, fee=200, tax=0, order_no="O2"),
        Fill("f3", "lot_1", FillSide.SELL, "2026-10-15", qty=10, price=80_000, fee=150, tax=1_600, order_no="O3"),
    ]

    metrics = recalculate_lot_from_fills(lot, fills, as_of_date=date(2026, 10, 20))
    # Weighted avg of 10@70,000 + 20@73,000 = 2,160,000 / 30 = 72,000
    assert metrics.remaining_qty == 20
    assert metrics.avg_price == 72_000.0
    assert metrics.status == LotStatus.OPEN
    # Realized PnL = (80,000 - 72,000)*10 - (100+200+150+1600) = 80,000 - 2,050 = 77,950
    assert metrics.realized_pnl == 77_950.0
    assert metrics.holding_days == 19

    # Final sell closes lot
    fills.append(Fill("f4", "lot_1", FillSide.SELL, "2026-10-20", qty=20, price=82_000, fee=0, tax=0, order_no="O4"))
    synced = apply_fills_to_lot(lot, fills, as_of_date=date(2026, 10, 25))
    assert synced.qty == 0
    assert synced.status == LotStatus.CLOSED


def test_multi_lot_sell_allocation_algo_priority_then_fifo() -> None:
    lot_a1 = Lot("lot_a1", "005930", "KR", "kr_monthly_trend", "1.0.0", "2026-09-01", qty=10, avg_price=70_000)
    lot_b1 = Lot("lot_b1", "005930", "KR", "kr_cup_and_handle", "1.0.0", "2026-09-05", qty=15, avg_price=71_000)
    lot_a2 = Lot("lot_a2", "005930", "KR", "kr_monthly_trend", "1.0.0", "2026-09-10", qty=20, avg_price=72_000)

    # Selling 25 shares for kr_monthly_trend should exhaust lot_a1 (10), then lot_a2 (15), untouched lot_b1
    allocs = allocate_sell_across_lots(
        symbol="005930",
        sell_qty=25,
        open_lots=[lot_a1, lot_b1, lot_a2],
        preferred_algo_id="kr_monthly_trend",
    )
    assert allocs == [("lot_a1", 10), ("lot_a2", 15)]

    # Selling 35 shares spills over to lot_b1 via FIFO
    allocs2 = allocate_sell_across_lots(
        symbol="005930",
        sell_qty=35,
        open_lots=[lot_a1, lot_b1, lot_a2],
        preferred_algo_id="kr_monthly_trend",
    )
    assert allocs2 == [("lot_a1", 10), ("lot_a2", 20), ("lot_b1", 5)]


def test_algorithm_performance_aggregation() -> None:
    l1 = Lot("l1", "005930", "KR", "kr_monthly_trend", "1.0.0", "2026-08-01")
    l2 = Lot("l2", "000660", "KR", "kr_monthly_trend", "1.0.0", "2026-09-01")
    fills = [
        Fill("f1", "l1", FillSide.BUY, "2026-08-01", 10, 10_000),
        Fill("f2", "l1", FillSide.SELL, "2026-08-11", 10, 12_000),  # +20%
        Fill("f3", "l2", FillSide.BUY, "2026-09-01", 10, 10_000),
        Fill("f4", "l2", FillSide.SELL, "2026-09-11", 10, 9_000),   # -10%
    ]
    perf = calculate_algorithm_performance([l1, l2], fills)
    m = perf["kr_monthly_trend"]
    assert m.closed_lots == 2
    assert m.win_rate_pct == 50.0
    assert m.avg_return_pct == 5.0
    assert m.total_realized_pnl == 10_000.0
    assert m.max_drawdown_pct == -10.0
