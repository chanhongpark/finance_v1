"""
Streamlit authentication & UserContext session bootstrap (R1.1, R1.2).
Reads 1-user-1-set config from st.secrets (no shared Registry sheet, no Admin mode).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict

import streamlit as st

from core.context import UserContext, load_user_context_from_mapping
from core.data.market_data_provider import HybridMarketDataProvider
from core.kis.client import KISClient, MockKISClient
from core.notifier.telegram import TelegramNotifier
from core.sheets.user_sheet_manager import InMemorySheetBackend, UserSheetManager


@dataclass
class AppServices:
    ctx: UserContext
    sheet: UserSheetManager
    kis: Any
    data_provider: HybridMarketDataProvider
    notifier: TelegramNotifier
    is_offline_demo: bool = False


def _read_secrets_dict() -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(os.environ)
    try:
        for k, v in st.secrets.items():
            merged[k] = dict(v) if hasattr(v, "items") else v
    except Exception:
        pass
    return merged


def _check_email_allowlist(ctx: UserContext) -> None:
    """If ALLOWED_EMAIL is configured in Secrets, verify via st.user / st.login (R1.1)."""
    allowed = ctx.settings.allowed_email.strip().lower()
    if not allowed:
        return

    user_obj = getattr(st, "user", None)
    is_logged_in = bool(getattr(user_obj, "is_logged_in", False)) if user_obj else False
    if not is_logged_in:
        st.warning("🔒 허용된 사용자만 접근할 수 있습니다. Google 계정으로 로그인해 주세요.")
        if hasattr(st, "login"):
            if st.button("Google 로그인", type="primary"):
                st.login()
        st.stop()

    logged_email = str(getattr(user_obj, "email", "")).strip().lower()
    if logged_email != allowed:
        st.error(f"접근 권한이 없는 계정입니다: `{logged_email}`")
        if hasattr(st, "logout"):
            if st.button("로그아웃"):
                st.logout()
        st.stop()


def get_app_services() -> AppServices:
    """Initialize and cache per-session AppServices strictly bound to UserContext."""
    if "app_services" in st.session_state:
        return st.session_state["app_services"]

    raw_secrets = _read_secrets_dict()
    ctx = load_user_context_from_mapping(raw_secrets)
    _check_email_allowlist(ctx)

    offline_demo = False
    if ctx.sheet_id and ctx.credentials.gcp_service_account:
        sheet = UserSheetManager(ctx)
    else:
        # Local/demo fallback without GCP service account
        if not ctx.sheet_id:
            ctx.sheet_id = f"local_sheet_{ctx.safe_user_id}"
        sheet = UserSheetManager(ctx, backend=InMemorySheetBackend(ctx.sheet_id))
        offline_demo = True

    if ctx.credentials.kis_app_key and ctx.credentials.kis_app_secret:
        kis: Any = KISClient(ctx)
    else:
        kis = MockKISClient(ctx)
        offline_demo = True

    data_provider = HybridMarketDataProvider(kis_client=kis)
    notifier = TelegramNotifier(ctx, dry_run=not ctx.credentials.telegram_bot_token)

    services = AppServices(
        ctx=ctx,
        sheet=sheet,
        kis=kis,
        data_provider=data_provider,
        notifier=notifier,
        is_offline_demo=offline_demo,
    )
    st.session_state["app_services"] = services
    return services
