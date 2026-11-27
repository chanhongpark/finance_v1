"""Regression tests for Google Sheets persistence, ledger integrity, and order safety (#1-#12)."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

from core.algorithms import Market, PriceType, Signal, SignalAction
from core.context import ExecutionMode, UserContext, UserCredentials, UserSettings
from core.execution.order_service import OrderService
from core.execution.runner import run_daily_reconciliation
from core.kis.client import MockKISClient
from core.ledger.calculator import _parse_date, recalculate_lot_from_fills
from core.ledger.models import Fill, FillSide, Lot, LotStatus, PendingOrder, PendingOrderStatus
from core.notifier.telegram import TelegramNotifier
from core.sheets.schema import SCHEMA_VERSION, SHEET_SCHEMAS
from core.sheets.user_sheet_manager import GSpreadBackend, InMemorySheetBackend, UserSheetManager
from ui.auth import find_duplicate_sheet_ids


def _make_ctx(tmp_path: Path, user_id: str = "tester") -> UserContext:
    return UserContext(
        user_id=user_id,
        credentials=UserCredentials(kis_app_key="K", kis_app_secret="S", is_mock=True, telegram_chat_id="C"),
        sheet_id=f"sheet_{user_id}",
        settings=UserSettings(execution_mode=ExecutionMode.FULL_AUTO),
        base_data_dir=tmp_path / "data",
    )


def _make_mock_gspread_backend(ws: MagicMock, tab_name: str = "Lots", headers: List[str] | None = None) -> GSpreadBackend:
    backend = GSpreadBackend.__new__(GSpreadBackend)
    backend._ws_cache = {tab_name: ws}
    backend._headers_cache = {tab_name: list(headers)} if headers is not None else {}
    return backend


def test_gspread_uses_raw_and_numericise_ignore_and_no_clear_on_overwrite() -> None:
    """#1, #2, #4, #11: GSpreadBackend must use RAW, numericise_ignore=['all'], and avoid ws.clear()."""
    ws = MagicMock()
    ws.get_all_records.return_value = [{"symbol": "005930", "order_no": "00012345"}]
    ws.col_values.return_value = ["lot_id", "lot_1", "lot_2", "lot_3"]  # 4 existing rows
    backend = _make_mock_gspread_backend(ws, "Lots", SHEET_SCHEMAS["Lots"])

    records = backend.read_records("Lots")
    assert records[0]["symbol"] == "005930"
    ws.get_all_records.assert_called_once_with(numericise_ignore=["all"])

    backend.append_rows("Lots", [["lot_1", "005930"]])
    ws.append_rows.assert_called_once_with([["lot_1", "005930"]], value_input_option="RAW")

    backend.overwrite_table("Lots", ["lot_id", "symbol"], [["lot_1", "005930"]])
    ws.clear.assert_not_called()
    ws.update.assert_called_once_with(
        "A1",
        [["lot_id", "symbol"], ["lot_1", "005930"]],
        value_input_option="RAW",
    )
    # Only trailing rows 3..4 cleared after successful overwrite
    ws.batch_clear.assert_called_once_with(["A3:B4"])


def test_gspread_row_level_upsert_updates_only_matching_rows() -> None:
    """#5: upsert_rows_by_key updates existing lot rows in place and appends only new lots."""
    ws = MagicMock()
    headers = ["lot_id", "symbol", "qty"]
    ws.col_values.return_value = ["lot_id", "lot_a", "lot_b"]
    backend = _make_mock_gspread_backend(ws, "Lots", headers)

    backend.upsert_rows_by_key(
        "Lots",
        "lot_id",
        [
            {"lot_id": "lot_b", "symbol": "005930", "qty": 15},
            {"lot_id": "lot_c", "symbol": "000660", "qty": 5},
        ],
    )

    ws.clear.assert_not_called()
    ws.batch_update.assert_called_once_with(
        [{"range": "A3:C3", "values": [["lot_b", "005930", 15]]}],
        value_input_option="RAW",
    )
    ws.append_rows.assert_called_once_with([["lot_c", "000660", 5]], value_input_option="RAW")


def test_legacy_corrupted_kr_symbol_and_korean_locale_date_recovery() -> None:
    """#1 & #2 defensive parsing for already-corrupted sheet rows."""
    lot = Lot.from_sheet_row({"lot_id": "lot_1", "symbol": 5930, "market": "KR", "qty": 3, "avg_price": 70000})
    assert lot.symbol == "005930"
    assert _parse_date("2026. 11. 27") == date(2026, 11, 27)
    assert _parse_date("2026. 11. 27.") == date(2026, 11, 27)
    assert _parse_date("2026-11-27T09:00:00Z") == date(2026, 11, 27)


def test_order_service_blocks_non_mock_kis_on_in_memory_sheet(tmp_path: Path) -> None:
    """#3: Live KIS client must be blocked when Sheet backend is InMemorySheetBackend."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)

    class FakeLiveKIS:
        placed = False

        def place_order(self, **kwargs: Any) -> Dict[str, Any]:
            self.placed = True
            return {"success": True, "order_no": "0001"}

    fake_kis = FakeLiveKIS()
    svc = OrderService(ctx, sheet, fake_kis, notifier=TelegramNotifier(ctx, dry_run=True))
    sig = Signal(SignalAction.BUY, "kr_fujimoto_126", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 70_000, qty=2)
    out = svc.execute_signal(sig, user_confirmed=True)

    assert not out.executed
    assert out.decision == "BLOCKED_NO_SHEET"
    assert not fake_kis.placed


def test_pending_queue_keeps_lots_behind_fills_and_flushes_both(tmp_path: Path) -> None:
    """#6: When append_fills fails, Lots upsert is also queued and both flush cleanly on retry."""
    ctx = _make_ctx(tmp_path)
    backend = InMemorySheetBackend(ctx.sheet_id)
    sheet = UserSheetManager(ctx, backend=backend)
    sheet.save_user_settings(ctx.settings)
    kis = MockKISClient(ctx)
    svc = OrderService(ctx, sheet, kis, notifier=TelegramNotifier(ctx, dry_run=True))

    orig_append = backend.append_rows

    def fail_on_fills(tab_name: str, rows: List[List[Any]]) -> None:
        if tab_name == "Fills":
            raise RuntimeError("429 quota exceeded")
        orig_append(tab_name, rows)

    backend.append_rows = fail_on_fills  # type: ignore[method-assign]

    sig = Signal(SignalAction.BUY, "kr_fujimoto_126", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 70_000, qty=2)
    out = svc.execute_signal(sig, user_confirmed=True)
    assert out.executed
    # Neither Fills nor Lots should be written prematurely while Fills is queued
    assert len(sheet.load_fills(use_cache=False)) == 0
    assert len(sheet.load_lots(use_cache=False)) == 0

    # Restore backend and flush pending writes
    backend.append_rows = orig_append  # type: ignore[method-assign]
    flushed = sheet.flush_pending_writes()
    assert flushed == 2
    assert len(sheet.load_fills(use_cache=False)) == 1
    assert len(sheet.load_lots(use_cache=False)) == 1


def test_sell_allocation_mismatch_logs_audit_and_notifies(tmp_path: Path) -> None:
    """#7: Selling with 0 matching OPEN lots logs SELL_ALLOCATION_MISMATCH and marks UNALLOCATED."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)
    kis = MockKISClient(ctx)
    notif = TelegramNotifier(ctx, dry_run=True)
    svc = OrderService(ctx, sheet, kis, notifier=notif)

    sell_sig = Signal(SignalAction.SELL, "kr_fujimoto_126", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 75_000, qty=5)
    out = svc.execute_signal(sell_sig, user_confirmed=True)

    assert out.decision == "UNALLOCATED"
    audits = sheet.load_audits(use_cache=False)
    assert any(a["action"] == "SELL_ALLOCATION_MISMATCH" for a in audits)
    assert any("배분 불일치" in m for m in notif.sent_messages)


def test_append_maps_values_by_actual_worksheet_header_order(tmp_path: Path) -> None:
    """#8: _safe_append aligns values to actual worksheet header order, not hardcoded schema order."""
    ctx = _make_ctx(tmp_path)
    backend = InMemorySheetBackend(ctx.sheet_id)
    sheet = UserSheetManager(ctx, backend=backend)

    # Simulate user/migration reordering Fills columns
    reordered = list(reversed(SHEET_SCHEMAS["Fills"]))
    backend.headers["Fills"] = reordered

    fill = Fill("fill_1", "lot_1", FillSide.BUY, "2026-11-27", qty=3, price=70_000, fee=31.5, tax=0.0, order_no="00123", note="test")
    sheet.append_fills([fill])

    loaded = sheet.load_fills(use_cache=False)
    assert len(loaded) == 1
    assert loaded[0].fill_id == "fill_1"
    assert loaded[0].lot_id == "lot_1"
    assert loaded[0].qty == 3
    assert loaded[0].order_no == "00123"


def test_unmatched_inquire_ccnl_saves_pending_order_and_settles_before_reconcile(tmp_path: Path) -> None:
    """A & #9: Unfilled US LOC order is saved as SUBMITTED PendingOrder and settled before reconciliation."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)

    class DeferredFillMockKIS(MockKISClient):
        def __init__(self, c: UserContext) -> None:
            super().__init__(c)
            self.fill_released = False

        def inquire_ccnl(self, order_no: str = "", market: str = "KR", lookback_days: int = 5) -> List[Dict[str, Any]]:
            if not self.fill_released:
                return []
            return super().inquire_ccnl(order_no=order_no, market=market, lookback_days=lookback_days)

    kis = DeferredFillMockKIS(ctx)
    svc = OrderService(ctx, sheet, kis, notifier=TelegramNotifier(ctx, dry_run=True))
    sig = Signal(
        SignalAction.BUY,
        "us_infinite_buying",
        "1.0.0",
        "TQQQ",
        Market.US,
        PriceType.LOC,
        50.0,
        qty=4,
        metadata={"round_inc": 0.5},
    )
    out = svc.execute_signal(sig, user_confirmed=True)

    assert not out.executed
    assert out.decision == "SUBMITTED"
    assert len(sheet.load_fills(use_cache=False)) == 0
    assert len(sheet.load_lots(use_cache=False)) == 0
    pending = sheet.load_pending_orders(use_cache=False)
    assert len(pending) == 1
    assert pending[0].status == PendingOrderStatus.SUBMITTED

    # Next day: KIS actually filled the LOC order and holds 4 shares of TQQQ
    kis.fill_released = True
    kis.holdings = [{"symbol": "TQQQ", "market": "US", "qty": 4, "avg_price": 50.0}]

    report = run_daily_reconciliation(ctx, sheet, kis, markets=("US",))
    assert report.is_matched
    assert len(report.created_manual_lots) == 0

    lots = sheet.load_lots(use_cache=False)
    assert len(lots) == 1
    assert lots[0].algo_id == "us_infinite_buying"
    assert lots[0].qty == 4
    assert lots[0].algo_state["current_round"] == 0.5
    assert sheet.load_pending_orders(use_cache=False)[0].status == PendingOrderStatus.FILLED


def test_partial_fill_settles_remainder_without_double_state_increment(tmp_path: Path) -> None:
    """A: Partially filled order settles delta later without incrementing algo_state twice."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)

    class PartialMockKIS(MockKISClient):
        def __init__(self, c: UserContext) -> None:
            super().__init__(c)
            self.stage = 1

        def inquire_ccnl(self, order_no: str = "", market: str = "KR", lookback_days: int = 5) -> List[Dict[str, Any]]:
            if self.stage == 1:
                return [{"order_no": order_no, "symbol": "005930", "filled_qty": 3, "unfilled_qty": 7, "avg_price": 70_000}]
            return [{"order_no": order_no, "symbol": "005930", "filled_qty": 10, "unfilled_qty": 0, "avg_price": 70_500}]

    kis = PartialMockKIS(ctx)
    svc = OrderService(ctx, sheet, kis, notifier=TelegramNotifier(ctx, dry_run=True))
    sig = Signal(
        SignalAction.BUY,
        "kr_fujimoto_126",
        "1.0.0",
        "005930",
        Market.KR,
        PriceType.LIMIT,
        70_000,
        qty=10,
        metadata={"next_entry_stage": 1},
    )
    out = svc.execute_signal(sig, user_confirmed=True)
    assert out.executed and out.filled_qty == 3
    assert sheet.load_lots(use_cache=False)[0].qty == 3
    assert sheet.load_pending_orders(use_cache=False)[0].status == PendingOrderStatus.PARTIAL

    # Remaining 7 shares fill later
    kis.stage = 2
    settled = svc.sync_pending_orders(markets=("KR",))
    assert len(settled) == 1
    assert settled[0].filled_qty == 7

    lots = sheet.load_lots(use_cache=False)
    assert len(lots) == 1
    assert lots[0].qty == 10
    assert lots[0].algo_state["entry_stage"] == 1
    assert sheet.load_pending_orders(use_cache=False)[0].status == PendingOrderStatus.FILLED


def test_mixed_legacy_and_iso_dates_sort_chronologically() -> None:
    """B: Legacy Korean locale date ('2026. 11. 20') and ISO ('2026-11-27') must sort chronologically."""
    lot = Lot.from_sheet_row(
        {"lot_id": "lot_1", "symbol": "005930", "market": "KR", "algo_id": "kr_fujimoto_126", "buy_date": "2026. 11. 20"}
    )
    assert lot.buy_date == "2026-11-20"
    f_buy = Fill.from_sheet_row(
        {"fill_id": "f1", "lot_id": "lot_1", "side": "BUY", "date": "2026. 11. 20", "qty": 10, "price": 100}
    )
    assert f_buy.date == "2026-11-20"

    # Even if raw unnormalized Fill objects are passed directly to calculator, sorting uses parsed dates
    raw_buy = Fill("f1", "lot_1", FillSide.BUY, "2026. 11. 20", qty=10, price=100)
    raw_sell = Fill("f2", "lot_1", FillSide.SELL, "2026-11-27", qty=4, price=110)
    metrics = recalculate_lot_from_fills(lot, [raw_buy, raw_sell])
    assert metrics.remaining_qty == 6
    assert metrics.realized_pnl == 40.0


def test_pending_queue_strict_fifo_and_fill_dedup_and_reconcile_skip(tmp_path: Path) -> None:
    """C, D, E: Queue stops on first failure, deduplicates Fills by fill_id, and skips reconcile while queued."""
    ctx = _make_ctx(tmp_path)
    backend = InMemorySheetBackend(ctx.sheet_id)
    sheet = UserSheetManager(ctx, backend=backend)
    sheet.save_user_settings(ctx.settings)
    kis = MockKISClient(ctx)
    svc = OrderService(ctx, sheet, kis, notifier=TelegramNotifier(ctx, dry_run=True))

    orig_append = backend.append_rows

    def always_fail_fills(tab_name: str, rows: List[List[Any]]) -> None:
        if tab_name == "Fills":
            raise RuntimeError("transient error")
        orig_append(tab_name, rows)

    backend.append_rows = always_fail_fills  # type: ignore[method-assign]
    sig = Signal(SignalAction.BUY, "kr_fujimoto_126", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 70_000, qty=2)
    svc.execute_signal(sig, user_confirmed=True)

    # C: Flushing while Fills still fails must NOT flush Lots ahead of Fills
    assert sheet.flush_pending_writes() == 0
    assert sheet.has_pending_writes()
    assert len(sheet.load_lots(use_cache=False)) == 0

    # E: Reconciliation must skip when pending writes remain
    kis.holdings = [{"symbol": "005930", "market": "KR", "qty": 2, "avg_price": 70_000}]
    report = run_daily_reconciliation(ctx, sheet, kis, markets=("KR",))
    assert report.skipped_reason != ""
    assert len(sheet.load_lots(use_cache=False)) == 0

    # D: Simulate Fill already written on server before timeout, then flush -> must NOT duplicate fill_id
    backend.append_rows = orig_append  # type: ignore[method-assign]
    import json

    queued = json.loads(sheet._pending_queue_path.read_text(encoding="utf-8"))
    queued_fill_dict = queued[0]["payload"][0]
    sheet.append_fills([Fill.from_sheet_row(queued_fill_dict)])
    assert len(sheet.load_fills(use_cache=False)) == 1

    flushed = sheet.flush_pending_writes()
    assert flushed == 2
    assert len(sheet.load_fills(use_cache=False)) == 1  # deduplicated!
    assert len(sheet.load_lots(use_cache=False)) == 1


def test_gspread_upsert_preserves_user_custom_columns() -> None:
    """Custom user-added columns in Lots tab are never overwritten by upsert_rows_by_key."""
    ws = MagicMock()
    headers = ["lot_id", "symbol", "user_memo", "qty"]
    ws.col_values.return_value = ["lot_id", "lot_a"]
    backend = _make_mock_gspread_backend(ws, "Lots", headers)

    backend.upsert_rows_by_key("Lots", "lot_id", [{"lot_id": "lot_a", "symbol": "005930", "qty": 10}])

    # Should update A2:B2 and D2:D2, leaving C2 (user_memo) completely untouched
    ws.batch_update.assert_called_once_with(
        [
            {"range": "A2:B2", "values": [["lot_a", "005930"]]},
            {"range": "D2:D2", "values": [[10]]},
        ],
        value_input_option="RAW",
    )


def test_order_service_records_estimated_kr_fee_and_sell_tax(tmp_path: Path) -> None:
    """#10: System order fills include estimated commission and KR sell tax."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)
    kis = MockKISClient(ctx)
    svc = OrderService(ctx, sheet, kis, notifier=TelegramNotifier(ctx, dry_run=True))

    buy_sig = Signal(SignalAction.BUY, "kr_fujimoto_126", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 100_000, qty=10)
    assert svc.execute_signal(buy_sig, user_confirmed=True).executed
    sell_sig = Signal(SignalAction.SELL, "kr_fujimoto_126", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 100_000, qty=10)
    assert svc.execute_signal(sell_sig, user_confirmed=True).executed

    fills = sheet.load_fills(use_cache=False)
    assert len(fills) == 2
    assert fills[0].fee == 150.0  # 1,000,000 * 0.00015
    assert fills[0].tax == 0.0
    assert fills[1].fee == 150.0
    assert fills[1].tax == 1800.0  # 1,000,000 * 0.0018


def test_find_duplicate_sheet_ids() -> None:
    """#12: Detect duplicate SHEET_ID across configured multi-user entries."""
    users = {
        "alice": {"SHEET_ID": "sheet_shared"},
        "bob": {"SHEET_ID": "sheet_shared"},
        "charlie": {"SHEET_ID": "sheet_unique"},
        "demo": {"SHEET_ID": ""},
    }
    dups = find_duplicate_sheet_ids(users)
    assert dups == {"sheet_shared": ["alice", "bob"]}


def test_sync_pending_orders_mid_loop_exception_never_duplicates_fills(tmp_path: Path) -> None:
    """Review #1: Exception on A2 after A1 settles must not duplicate A1's Fill on retry."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)

    po1 = PendingOrder("00001", "TQQQ", "US", "us_infinite_buying", "1.0.0", "BUY", req_qty=4, limit_price=50.0, order_date="2026-11-26")
    po2 = PendingOrder("00002", "SOXL", "US", "us_infinite_buying", "1.0.0", "BUY", req_qty=5, limit_price=30.0, order_date="2026-11-26")
    sheet.upsert_pending_orders([po1, po2])

    class FlakySecondOrderKIS(MockKISClient):
        def __init__(self, c: UserContext) -> None:
            super().__init__(c)
            self.fail_second = True

        def inquire_ccnl(self, order_no: str = "", market: str = "KR", lookback_days: int = 5) -> List[Dict[str, Any]]:
            if order_no == "00002" and self.fail_second:
                raise RuntimeError("500 KIS error on A2")
            return [
                {"order_no": "00001", "ord_dt": "2026-11-26", "symbol": "TQQQ", "side": "BUY", "filled_qty": 4, "unfilled_qty": 0, "avg_price": 50.0},
                {"order_no": "00002", "ord_dt": "2026-11-26", "symbol": "SOXL", "side": "BUY", "filled_qty": 5, "unfilled_qty": 0, "avg_price": 30.0},
            ]

    kis = FlakySecondOrderKIS(ctx)
    svc = OrderService(ctx, sheet, kis, notifier=TelegramNotifier(ctx, dry_run=True))

    # First run: A1 succeeds and is persisted immediately; A2 fails
    svc.sync_pending_orders(markets=("US",))
    assert svc.last_sync_had_errors is True
    assert len([f for f in sheet.load_fills(use_cache=False) if f.order_no == "00001"]) == 1

    # Second run: A2 succeeds; A1 must NOT be duplicated
    kis.fail_second = False
    svc.sync_pending_orders(markets=("US",))
    assert svc.last_sync_had_errors is False
    fills = sheet.load_fills(use_cache=False)
    assert len([f for f in fills if f.order_no == "00001"]) == 1
    assert len([f for f in fills if f.order_no == "00002"]) == 1


def test_composite_order_key_prevents_daily_order_no_collision(tmp_path: Path) -> None:
    """Review #2: Same order_no on different dates must not overwrite or cross-match."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)

    po_old = PendingOrder("000100", "005930", "KR", "kr_fujimoto_126", "1.0.0", "BUY", req_qty=2, limit_price=70_000, order_date="2026-11-25")
    po_new = PendingOrder("000100", "000660", "KR", "kr_fujimoto_126", "1.0.0", "BUY", req_qty=3, limit_price=180_000, order_date="2026-11-27")
    sheet.upsert_pending_orders([po_old, po_new])

    loaded = sheet.load_pending_orders(use_cache=False)
    assert len(loaded) == 2
    assert {p.order_key for p in loaded} == {"2026-11-25_000100", "2026-11-27_000100"}

    class SameOrderNoKIS(MockKISClient):
        def inquire_ccnl(self, order_no: str = "", market: str = "KR", lookback_days: int = 5) -> List[Dict[str, Any]]:
            # Only 2026-11-27 000660 filled; 2026-11-25 005930 has NOT filled
            return [
                {"order_no": "000100", "ord_dt": "2026-11-27", "symbol": "000660", "side": "BUY", "filled_qty": 3, "unfilled_qty": 0, "avg_price": 180_000}
            ]

    svc = OrderService(ctx, sheet, SameOrderNoKIS(ctx), notifier=TelegramNotifier(ctx, dry_run=True))
    svc.sync_pending_orders(markets=("KR",))

    lots = sheet.load_lots(use_cache=False)
    assert len(lots) == 1
    assert lots[0].symbol == "000660"
    assert lots[0].qty == 3


def test_reconcile_skips_on_sync_error_and_excludes_active_pending_symbols(tmp_path: Path) -> None:
    """Review #3: Reconciliation must skip on sync errors and exclude symbols with active pending orders."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)

    po = PendingOrder("00099", "TQQQ", "US", "us_infinite_buying", "1.0.0", "BUY", req_qty=4, limit_price=50.0, order_date=date.today().strftime("%Y-%m-%d"))
    sheet.upsert_pending_orders([po])

    class ErrorCcnlKIS(MockKISClient):
        def inquire_ccnl(self, order_no: str = "", market: str = "KR", lookback_days: int = 5) -> List[Dict[str, Any]]:
            raise RuntimeError("KIS timeout")

    kis_err = ErrorCcnlKIS(ctx)
    kis_err.holdings = [{"symbol": "TQQQ", "market": "US", "qty": 4, "avg_price": 50.0}]
    report_err = run_daily_reconciliation(ctx, sheet, kis_err, markets=("US",))
    assert report_err.skipped_reason != ""
    assert len(sheet.load_lots(use_cache=False)) == 0

    # Even when inquire_ccnl succeeds with empty result (still SUBMITTED), TQQQ is excluded from manual lot creation
    class EmptyCcnlKIS(MockKISClient):
        def inquire_ccnl(self, order_no: str = "", market: str = "KR", lookback_days: int = 5) -> List[Dict[str, Any]]:
            return []

    kis_empty = EmptyCcnlKIS(ctx)
    kis_empty.holdings = [{"symbol": "TQQQ", "market": "US", "qty": 4, "avg_price": 50.0}]
    report_excl = run_daily_reconciliation(ctx, sheet, kis_empty, markets=("US",))
    assert report_excl.is_matched
    assert len(report_excl.created_manual_lots) == 0


def test_sync_queues_pending_order_update_when_fill_write_fails(tmp_path: Path) -> None:
    """Review #4: If append_fills fails during sync, PendingOrders update is also queued (stays SUBMITTED in sheet)."""
    ctx = _make_ctx(tmp_path)
    backend = InMemorySheetBackend(ctx.sheet_id)
    sheet = UserSheetManager(ctx, backend=backend)
    sheet.save_user_settings(ctx.settings)

    po = PendingOrder("00077", "TQQQ", "US", "us_infinite_buying", "1.0.0", "BUY", req_qty=4, limit_price=50.0, order_date="2026-11-26")
    sheet.upsert_pending_orders([po])

    class FilledKIS(MockKISClient):
        def inquire_ccnl(self, order_no: str = "", market: str = "KR", lookback_days: int = 5) -> List[Dict[str, Any]]:
            return [{"order_no": "00077", "ord_dt": "2026-11-26", "symbol": "TQQQ", "side": "BUY", "filled_qty": 4, "unfilled_qty": 0, "avg_price": 50.0}]

    orig_append = backend.append_rows

    def fail_fills(tab_name: str, rows: List[List[Any]]) -> None:
        if tab_name == "Fills":
            raise RuntimeError("429 quota")
        orig_append(tab_name, rows)

    backend.append_rows = fail_fills  # type: ignore[method-assign]
    svc = OrderService(ctx, sheet, FilledKIS(ctx), notifier=TelegramNotifier(ctx, dry_run=True))
    svc.sync_pending_orders(markets=("US",))

    # PendingOrders in sheet must still be SUBMITTED while Fill is in local queue
    assert sheet.load_pending_orders(use_cache=False)[0].status == PendingOrderStatus.SUBMITTED
    assert sheet.has_pending_writes()

    # Once backend recovers and flushes, Fills + Lots + PendingOrders(FILLED) all apply in order
    backend.append_rows = orig_append  # type: ignore[method-assign]
    assert sheet.flush_pending_writes() == 3
    assert len(sheet.load_fills(use_cache=False)) == 1
    assert len(sheet.load_lots(use_cache=False)) == 1
    assert sheet.load_pending_orders(use_cache=False)[0].status == PendingOrderStatus.FILLED


def test_corrupted_pending_queue_is_fail_closed_and_schema_version_bumped(tmp_path: Path) -> None:
    """Review #6 & #7: Corrupted pending_writes.json returns True (fail-closed) and SCHEMA_VERSION is 1.1.0."""
    assert SCHEMA_VERSION == "1.1.0"
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet._pending_queue_path.write_text("{corrupted json", encoding="utf-8")
    assert sheet.has_pending_writes() is True


def test_legacy_pending_orders_without_order_key_backfilled_and_settled(tmp_path: Path) -> None:
    """Backfill legacy PendingOrders rows missing order_key so they never remain stuck in SUBMITTED."""
    ctx = _make_ctx(tmp_path)
    backend = InMemorySheetBackend(ctx.sheet_id)
    # Simulate sheet created before order_key column existed
    legacy_headers = [c for c in SHEET_SCHEMAS["PendingOrders"] if c != "order_key"]
    backend.headers["PendingOrders"] = legacy_headers
    backend.tables["PendingOrders"] = [
        {
            "order_no": "A1",
            "symbol": "TQQQ",
            "market": "US",
            "algo_id": "us_infinite_buying",
            "algo_version": "1.0.0",
            "side": "BUY",
            "req_qty": 4,
            "recorded_filled_qty": 0,
            "limit_price": 50.0,
            "lot_id": "",
            "status": "SUBMITTED",
            "order_date": "2026-11-27",
            "created_at": "2026-11-27T00:00:00Z",
            "updated_at": "2026-11-27T00:00:00Z",
            "metadata_json": "{}",
            "conditions_json": "[]",
            "reason_text": "legacy",
        }
    ]

    sheet = UserSheetManager(ctx, backend=backend)
    sheet.save_user_settings(ctx.settings)

    # ensure_schema must have backfilled order_key on the existing row
    raw_rows = backend.read_records("PendingOrders")
    assert len(raw_rows) == 1
    assert raw_rows[0]["order_key"] == "2026-11-27_A1"

    class FilledLegacyKIS(MockKISClient):
        def inquire_ccnl(self, order_no: str = "", market: str = "KR", lookback_days: int = 5) -> List[Dict[str, Any]]:
            return [{"order_no": "A1", "ord_dt": "2026-11-27", "symbol": "TQQQ", "side": "BUY", "filled_qty": 4, "unfilled_qty": 0, "avg_price": 50.0}]

    svc = OrderService(ctx, sheet, FilledLegacyKIS(ctx), notifier=TelegramNotifier(ctx, dry_run=True))
    svc.sync_pending_orders(markets=("US",))

    final_rows = backend.read_records("PendingOrders")
    assert len(final_rows) == 1
    assert final_rows[0]["order_key"] == "2026-11-27_A1"
    assert final_rows[0]["status"] == "FILLED"


def test_day_order_expires_after_session_close_even_if_kis_reports_unfilled_qty(tmp_path: Path) -> None:
    """US (order_date + 1 < today) and KR (order_date < today) day orders expire even if unfilled_qty > 0."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)

    today = date.today()
    us_old_date = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    kr_old_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    po_us = PendingOrder("U1", "TQQQ", "US", "us_infinite_buying", "1.0.0", "BUY", req_qty=4, limit_price=50.0, order_date=us_old_date)
    po_kr = PendingOrder("K1", "005930", "KR", "kr_fujimoto_126", "1.0.0", "BUY", req_qty=10, recorded_filled_qty=3, limit_price=70_000, order_date=kr_old_date)
    sheet.upsert_pending_orders([po_us, po_kr])

    class ResidualUnfilledKIS(MockKISClient):
        def inquire_ccnl(self, order_no: str = "", market: str = "KR", lookback_days: int = 5) -> List[Dict[str, Any]]:
            if market == "US":
                return [{"order_no": "U1", "ord_dt": us_old_date, "symbol": "TQQQ", "side": "BUY", "filled_qty": 0, "unfilled_qty": 4, "avg_price": 0.0}]
            return [{"order_no": "K1", "ord_dt": kr_old_date, "symbol": "005930", "side": "BUY", "filled_qty": 3, "unfilled_qty": 7, "avg_price": 70_000}]

    svc = OrderService(ctx, sheet, ResidualUnfilledKIS(ctx), notifier=TelegramNotifier(ctx, dry_run=True))
    svc.sync_pending_orders(markets=("US", "KR"))

    by_no = {p.order_no: p for p in sheet.load_pending_orders(use_cache=False)}
    assert by_no["U1"].status == PendingOrderStatus.EXPIRED
    assert by_no["K1"].status == PendingOrderStatus.FILLED


def test_duplicate_sell_settlement_returns_early_without_extra_notifications(tmp_path: Path) -> None:
    """Re-applying an already-recorded SELL fill must not emit duplicate Signals/Audits/Telegram alerts."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)
    kis = MockKISClient(ctx)
    notif = TelegramNotifier(ctx, dry_run=True)
    svc = OrderService(ctx, sheet, kis, notifier=notif)

    buy_sig = Signal(SignalAction.BUY, "kr_fujimoto_126", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 70_000, qty=5)
    assert svc.execute_signal(buy_sig, user_confirmed=True).executed

    sell_sig = Signal(SignalAction.SELL, "kr_fujimoto_126", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 75_000, qty=2)
    out1 = svc._apply_confirmed_fill(sell_sig, "S001", 2, 75_000, None, True, "2026-11-27", True, None, 2)
    assert out1.executed
    msg_count = len(notif.sent_messages)
    sig_count = len(sheet.load_signals(use_cache=False))

    # Re-applying exact same SELL order_no + cumulative qty must return early
    out2 = svc._apply_confirmed_fill(sell_sig, "S001", 2, 75_000, None, True, "2026-11-27", True, None, 2)
    assert out2.executed
    assert len(notif.sent_messages) == msg_count
    assert len(sheet.load_signals(use_cache=False)) == sig_count


def test_load_fills_deduplicates_and_append_fills_reuses_known_ids(tmp_path: Path) -> None:
    """load_fills deduplicates duplicate fill_ids; append_fills skips extra sheet read when known_fill_ids passed."""
    ctx = _make_ctx(tmp_path)
    backend = InMemorySheetBackend(ctx.sheet_id)
    sheet = UserSheetManager(ctx, backend=backend)

    # Inject duplicate raw rows simulating concurrent append race
    row_dict = Fill("fill_dup", "lot_1", FillSide.BUY, "2026-11-27", 5, 100.0).to_sheet_row()
    headers = backend.get_headers("Fills")
    row_vals = [row_dict.get(c, "") for c in headers]
    backend.append_rows("Fills", [row_vals, row_vals])

    loaded = sheet.load_fills(use_cache=False)
    assert len(loaded) == 1
    assert loaded[0].fill_id == "fill_dup"

    # Passing known_fill_ids avoids calling read_records("Fills")
    read_spy = MagicMock(side_effect=backend.read_records)
    backend.read_records = read_spy  # type: ignore[method-assign]
    sheet.append_fills([Fill("fill_new", "lot_1", FillSide.BUY, "2026-11-27", 2, 105.0)], known_fill_ids={"fill_dup"})
    read_spy.assert_not_called()
