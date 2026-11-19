"""Tests for per-user sheet & local filesystem path isolation (R1.2, R1.3, R1.6, R10)."""
from pathlib import Path

import pytest

from core.context import UserContext, UserCredentials, UserIsolationError
from core.sheets.user_sheet_manager import InMemorySheetBackend, UserSheetManager


def _make_ctx(tmp_path: Path, user_id: str, sheet_id: str) -> UserContext:
    return UserContext(
        user_id=user_id,
        credentials=UserCredentials(kis_app_key=f"KEY_{user_id}", kis_app_secret="SEC", is_mock=True),
        sheet_id=sheet_id,
        base_data_dir=tmp_path / "data",
    )


def test_cross_user_sheet_access_raises(tmp_path: Path) -> None:
    ctx_a = _make_ctx(tmp_path, "user_a@example.com", "sheet_A")
    ctx_b = _make_ctx(tmp_path, "user_b@example.com", "sheet_B")

    # User A opening User A's sheet succeeds
    mgr_a = UserSheetManager(ctx_a, backend=InMemorySheetBackend("sheet_A"))
    assert mgr_a.sheet_id == "sheet_A"

    # User A attempting to open User B's sheet MUST fail
    with pytest.raises(UserIsolationError):
        UserSheetManager(ctx_a, backend=InMemorySheetBackend("sheet_B"), target_sheet_id=ctx_b.sheet_id)


def test_local_data_dir_path_traversal_blocked(tmp_path: Path) -> None:
    ctx_a = _make_ctx(tmp_path, "user_a", "sheet_A")
    ctx_b = _make_ctx(tmp_path, "user_b", "sheet_B")

    path_a = ctx_a.resolve_user_path("kis_token.json")
    path_b = ctx_b.resolve_user_path("kis_token.json")
    assert path_a != path_b
    assert "user_a" in str(path_a)
    assert "user_b" in str(path_b)

    # Attempting to traverse into user_b's directory MUST fail
    with pytest.raises(UserIsolationError):
        ctx_a.resolve_user_path("../user_b/kis_token.json")
