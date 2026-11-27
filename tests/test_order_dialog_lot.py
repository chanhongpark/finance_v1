"""Tests for Dashboard holding '주문 확인' signal builder and chart rendering."""
from __future__ import annotations

import dataclasses

import pandas as pd

from core.algorithms.base import Market, PriceType, SignalAction
from core.context import ExecutionMode, UserContext, UserCredentials, UserSettings
from core.execution.order_service import OrderService
from core.kis.client import MockKISClient
from core.ledger.models import Fill, FillSide, Lot, LotMetrics, LotStatus
from core.sheets.user_sheet_manager import InMemorySheetBackend, UserSheetManager
from ui.components.order_dialog import _build_price_chart, build_lot_order_signal


def _sample_ohlcv(days: int = 120, base: float = 70_000.0) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=days)
    closes = [base + i * 100.0 for i in range(days)]
    return pd.DataFrame(
        {
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "open": [c - 50.0 for c in closes],
            "high": [c + 200.0 for c in closes],
            "low": [c - 200.0 for c in closes],
            "close": closes,
            "volume": [100_000 + i * 1000 for i in range(days)],
        }
    )


def _metrics_for(lot: Lot) -> LotMetrics:
    return LotMetrics(
        lot_id=lot.lot_id,
        remaining_qty=lot.qty,
        avg_price=lot.avg_price,
        total_buy_qty=lot.qty,
        total_sell_qty=0,
        total_buy_cost=lot.qty * lot.avg_price,
        total_sell_proceeds=0.0,
        total_fees_and_taxes=0.0,
        realized_pnl=0.0,
        realized_return_pct=0.0,
        holding_days=10,
        status=LotStatus.OPEN,
    )


def test_build_lot_order_signal_kr_monthly_and_chart():
    lot = Lot(
        lot_id="lot_kr_m1",
        symbol="005930",
        market="KR",
        algo_id="kr_monthly_trend",
        algo_version="1.0.0",
        buy_date="2026-06-01",
        qty=15,
        avg_price=70_000.0,
        status=LotStatus.OPEN,
        entry_conditions=[
            {
                "name": "월봉추세",
                "value": 70000,
                "threshold": 65000,
                "op": ">",
                "passed": True,
                "desc": "종가가 월봉 SMA10 상회",
            }
        ],
    )
    sig = build_lot_order_signal(
        lot=lot,
        metrics=_metrics_for(lot),
        current_price=73_420.0,
        algo_configs={},
        global_hard_stop_loss_pct=-0.15,
    )
    assert sig.action == SignalAction.SELL
    assert sig.market == Market.KR
    assert sig.price_type == PriceType.LIMIT
    assert sig.price == 73_400.0  # KR tick size 100
    assert sig.qty == 15
    assert sig.lot_id == "lot_kr_m1"
    assert sig.metadata["target_price"] == 84_000.0
    assert sig.metadata["stop_loss_price"] == 65_100.0
    assert sig.metadata["monthly_sma_period"] == 10
    assert len(sig.conditions) == 1

    fig = _build_price_chart(_sample_ohlcv(260), sig, sig.price, lot.avg_price)
    assert len(fig.data) >= 3


def test_build_lot_order_signal_us_infinite_and_manual_fallback():
    us_lot = Lot(
        lot_id="lot_us_inf",
        symbol="TQQQ",
        market="US",
        algo_id="us_infinite_buying",
        algo_version="2.2.0",
        buy_date="2026-08-01",
        qty=20,
        avg_price=50.0,
        status=LotStatus.OPEN,
        algo_state={"current_round": 5.0, "division_count": 40, "target_roi": 0.10},
    )
    us_sig = build_lot_order_signal(
        lot=us_lot,
        metrics=_metrics_for(us_lot),
        current_price=52.346,
        algo_configs={},
        global_hard_stop_loss_pct=-0.15,
    )
    assert us_sig.market == Market.US
    assert us_sig.price == 52.35
    assert us_sig.metadata["target_price"] == 55.0
    assert us_sig.metadata["stop_loss_price"] == 42.5

    manual_lot = Lot(
        lot_id="lot_manual",
        symbol="000660",
        market="KR",
        algo_id="manual",
        algo_version="1.0.0",
        buy_date="2026-09-01",
        qty=5,
        avg_price=150_000.0,
        status=LotStatus.OPEN,
    )
    manual_sig = build_lot_order_signal(
        lot=manual_lot,
        metrics=_metrics_for(manual_lot),
        current_price=None,  # fallback to avg_price
        algo_configs={},
        global_hard_stop_loss_pct=-0.15,
    )
    assert manual_sig.price == 150_000.0
    assert manual_sig.metadata["stop_loss_price"] == 127_500.0

    fig = _build_price_chart(_sample_ohlcv(120, base=150_000.0), manual_sig, manual_sig.price, manual_lot.avg_price)
    assert len(fig.data) >= 3


def test_dashboard_lot_order_executes_partial_sell_and_additional_buy():
    ctx = UserContext(
        user_id="tester_dash",
        sheet_id="sheet_dash_test",
        credentials=UserCredentials(),
        settings=UserSettings(execution_mode=ExecutionMode.SEMI_AUTO),
    )
    sheet = UserSheetManager(ctx, InMemorySheetBackend(ctx.sheet_id))
    lot = Lot(
        lot_id="lot_dash_1",
        symbol="005930",
        market="KR",
        algo_id="kr_fujimoto_126",
        algo_version="1.0.0",
        buy_date="2026-10-01",
        qty=10,
        avg_price=70_000.0,
        status=LotStatus.OPEN,
    )
    sheet.append_fills(
        [Fill(fill_id="fill_init_1", lot_id="lot_dash_1", side=FillSide.BUY, date="2026-10-01", qty=10, price=70_000.0)]
    )
    sheet.upsert_lots([lot])

    kis = MockKISClient(ctx)
    svc = OrderService(ctx, sheet, kis)

    base_sig = build_lot_order_signal(lot, _metrics_for(lot), current_price=75_000.0, algo_configs={})

    # Partial sell of 4 shares from the holding
    sell_sig = dataclasses.replace(base_sig, qty=4)
    sell_out = svc.execute_signal(sell_sig, user_confirmed=True)
    assert sell_out.executed is True
    updated_lot = next(l for l in sheet.load_lots(use_cache=False) if l.lot_id == "lot_dash_1")
    assert updated_lot.qty == 6
    assert updated_lot.status == LotStatus.OPEN

    # Additional buy of 2 shares into the same holding
    buy_sig = dataclasses.replace(base_sig, action=SignalAction.BUY, qty=2, price=74_000.0)
    buy_out = svc.execute_signal(buy_sig, user_confirmed=True)
    assert buy_out.executed is True
    updated_lot2 = next(l for l in sheet.load_lots(use_cache=False) if l.lot_id == "lot_dash_1")
    assert updated_lot2.qty == 8
    assert updated_lot2.lot_id == "lot_dash_1"
