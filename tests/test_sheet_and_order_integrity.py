"""Regression tests for Google Sheets persistence, ledger integrity, and order safety (#1-#12)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

from core.algorithms import Market, PriceType, Signal, SignalAction
from core.context import ExecutionMode, UserContext, UserCredentials, UserSettings
from core.execution.order_service import OrderService
from core.kis.client import MockKISClient
from core.ledger.calculator import _parse_date
from core.ledger.models import Fill, FillSide, Lot, LotStatus
from core.notifier.telegram import TelegramNotifier
from core.sheets.schema import SHEET_SCHEMAS
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


def test_unmatched_inquire_ccnl_does_not_mark_order_filled(tmp_path: Path) -> None:
    """#9: If inquire_ccnl returns no matching execution, OrderService must return UNFILLED."""
    ctx = _make_ctx(tmp_path)
    sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
    sheet.save_user_settings(ctx.settings)

    class UnfilledMockKIS(MockKISClient):
        def inquire_ccnl(self, order_no: str = "", market: str = "KR") -> List[Dict[str, Any]]:
            return []

    kis = UnfilledMockKIS(ctx)
    svc = OrderService(ctx, sheet, kis, notifier=TelegramNotifier(ctx, dry_run=True))
    sig = Signal(SignalAction.BUY, "kr_fujimoto_126", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 70_000, qty=2)
    out = svc.execute_signal(sig, user_confirmed=True)

    assert not out.executed
    assert out.decision == "UNFILLED"
    assert len(sheet.load_fills(use_cache=False)) == 0
    assert len(sheet.load_lots(use_cache=False)) == 0


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
