"""E2E mock test: Two independent users running concurrently with zero data cross-contamination."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from core.algorithms import Market, PriceType, Signal, SignalAction
from core.context import ExecutionMode, UserContext, UserCredentials, UserSettings
from core.data.cache_store import SharedMarketCache
from core.data.market_data_provider import HybridMarketDataProvider
from core.execution.order_service import OrderService
from core.execution.runner import run_us_infinite_buying
from core.kis.client import MockKISClient
from core.notifier.telegram import TelegramNotifier
from core.sheets.user_sheet_manager import InMemorySheetBackend, UserSheetManager


def test_two_mock_users_e2e_full_isolation(tmp_path: Path) -> None:
    ctx_a = UserContext(
        user_id="alice",
        credentials=UserCredentials(kis_app_key="KEY_A", kis_app_secret="SEC_A", is_mock=True, telegram_chat_id="CHAT_A"),
        sheet_id="sheet_alice",
        settings=UserSettings(execution_mode=ExecutionMode.FULL_AUTO),
        base_data_dir=tmp_path / "data",
    )
    ctx_b = UserContext(
        user_id="bob",
        credentials=UserCredentials(kis_app_key="KEY_B", kis_app_secret="SEC_B", is_mock=True, telegram_chat_id="CHAT_B"),
        sheet_id="sheet_bob",
        settings=UserSettings(execution_mode=ExecutionMode.FULL_AUTO),
        base_data_dir=tmp_path / "data",
    )

    sheet_a = UserSheetManager(ctx_a, backend=InMemorySheetBackend("sheet_alice"))
    sheet_b = UserSheetManager(ctx_b, backend=InMemorySheetBackend("sheet_bob"))
    sheet_a.save_user_settings(ctx_a.settings)
    sheet_b.save_user_settings(ctx_b.settings)

    kis_a = MockKISClient(ctx_a)
    kis_b = MockKISClient(ctx_b)
    notif_a = TelegramNotifier(ctx_a, dry_run=True)
    notif_b = TelegramNotifier(ctx_b, dry_run=True)

    # Alice buys 005930 under TWO different algorithms (R13 duplicate recommendations allowed)
    svc_a = OrderService(ctx_a, sheet_a, kis_a, notifier=notif_a)
    sig_a1 = Signal(SignalAction.BUY, "kr_monthly_trend", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 70_000, qty=5, reason_text="월간추세")
    sig_a2 = Signal(SignalAction.BUY, "kr_cup_and_handle", "1.0.0", "005930", Market.KR, PriceType.LIMIT, 70_000, qty=3, reason_text="컵앤핸들")
    out1 = svc_a.execute_signal(sig_a1)
    out2 = svc_a.execute_signal(sig_a2)
    assert out1.executed and out2.executed

    # Bob runs US Infinite Buying on TQQQ
    def fake_us_fdr(sym: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame(
            [{"date": end, "open": 50, "high": 52, "low": 49, "close": 50.0, "volume": 1000}]
        )

    provider_b = HybridMarketDataProvider(
        cache=SharedMarketCache(tmp_path / "data" / "shared"),
        kis_client=kis_b,
        fdr_reader=fake_us_fdr,
    )
    kis_b.quotes["TQQQ"] = {"symbol": "TQQQ", "price": 50.0, "open": 49.5, "high": 51.0, "low": 49.0, "volume": 5000}
    bob_outcomes = run_us_infinite_buying(ctx_b, sheet_b, kis_b, provider_b, symbols=["TQQQ"], notifier=notif_b)
    assert any(o.executed for o in bob_outcomes)

    # Verify strict isolation between Alice and Bob
    alice_lots = sheet_a.load_lots(use_cache=False)
    bob_lots = sheet_b.load_lots(use_cache=False)

    assert len(alice_lots) == 2
    assert {l.algo_id for l in alice_lots} == {"kr_monthly_trend", "kr_cup_and_handle"}
    assert all(l.symbol == "005930" for l in alice_lots)

    assert len(bob_lots) == 1
    assert bob_lots[0].symbol == "TQQQ"
    assert bob_lots[0].algo_id == "us_infinite_buying"

    # Notifications did not mix
    assert all("[alice |" in m for m in notif_a.sent_messages)
    assert all("[bob |" in m for m in notif_b.sent_messages)
