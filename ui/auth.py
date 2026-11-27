"""
Streamlit authentication & UserContext session bootstrap (R1.1, R1.2).
Supports both:
- Multi-user ID/Password login via `[users.<id>]` sections in st.secrets (no Google Cloud Console OAuth needed)
- Single-user 1-set secrets with optional `APP_PASSWORD` / `ALLOWED_EMAIL` login gate
"""
from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Optional

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


def _to_plain_obj(obj: Any) -> Any:
    if isinstance(obj, Mapping) or hasattr(obj, "items"):
        return {k: _to_plain_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain_obj(v) for v in obj]
    return obj


def _read_secrets_dict() -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(os.environ)
    try:
        for k, v in st.secrets.items():
            merged[k] = _to_plain_obj(v)
    except Exception:
        pass
    return merged


def _verify_password(input_pw: str, expected_pw: str) -> bool:
    """Verify plaintext or `sha256:<hex>` password in constant time."""
    if not expected_pw:
        return True
    if expected_pw.startswith("sha256:"):
        expected_hash = expected_pw[7:].strip().lower()
        actual_hash = hashlib.sha256(input_pw.encode("utf-8")).hexdigest().lower()
        return hmac.compare_digest(actual_hash, expected_hash)
    return hmac.compare_digest(input_pw, expected_pw)


def _extract_users_map(raw_secrets: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw_users = raw_secrets.get("users")
    if not isinstance(raw_users, Mapping):
        return {}
    return {
        str(k).strip(): dict(v)
        for k, v in raw_users.items()
        if str(k).strip() and isinstance(v, Mapping)
    }


def find_duplicate_sheet_ids(users: Dict[str, Dict[str, Any]]) -> Dict[str, list[str]]:
    """Return mapping of sheet_id -> list of user keys when multiple users share the same SHEET_ID."""
    by_sheet: Dict[str, list[str]] = {}
    for user_key, cfg in users.items():
        sid = str(cfg.get("SHEET_ID", "")).strip()
        if sid:
            by_sheet.setdefault(sid, []).append(user_key)
    return {sid: uids for sid, uids in by_sheet.items() if len(uids) > 1}


def _find_matching_user_key(users: Dict[str, Dict[str, Any]], login_id: str) -> Optional[str]:
    cleaned = login_id.strip()
    if not cleaned:
        return None
    if cleaned in users:
        return cleaned
    lowered = cleaned.lower()
    for key, cfg in users.items():
        if key.lower() == lowered:
            return key
        allowed_email = str(cfg.get("ALLOWED_EMAIL", cfg.get("email", ""))).strip().lower()
        if allowed_email and allowed_email == lowered:
            return key
    return None


def _authenticate_multi_user(raw_secrets: Dict[str, Any], users: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    auth_key = st.session_state.get("authenticated_user")
    if not auth_key or auth_key not in users:
        st.title("🔐 로그인")
        st.caption("등록된 사용자 아이디(또는 이메일)와 비밀번호로 접속해 주세요.")
        with st.form("multi_user_login_form", clear_on_submit=False):
            login_id = st.text_input("아이디 또는 이메일")
            password = st.text_input("비밀번호", type="password")
            submitted = st.form_submit_button("로그인", type="primary", use_container_width=True)

        if submitted:
            matched_key = _find_matching_user_key(users, login_id)
            if matched_key is not None:
                user_cfg = users[matched_key]
                expected_pw = str(user_cfg.get("password", user_cfg.get("APP_PASSWORD", ""))).strip()
                if _verify_password(password, expected_pw):
                    st.session_state["authenticated_user"] = matched_key
                    st.session_state.pop("app_services", None)
                    st.rerun()
            st.error("아이디 또는 비밀번호가 올바르지 않습니다.")
        st.stop()

    selected = dict(users[auth_key])
    selected.setdefault("USER_ID", auth_key)
    # Inherit shared service account if not overridden per user
    if "gcp_service_account" not in selected and "gcp_service_account" in raw_secrets:
        selected["gcp_service_account"] = raw_secrets["gcp_service_account"]
    if "GCP_SERVICE_ACCOUNT_JSON" not in selected and "GCP_SERVICE_ACCOUNT_JSON" in raw_secrets:
        selected["GCP_SERVICE_ACCOUNT_JSON"] = raw_secrets["GCP_SERVICE_ACCOUNT_JSON"]
    return selected


def _authenticate_single_user(raw_secrets: Dict[str, Any]) -> None:
    app_password = str(raw_secrets.get("APP_PASSWORD", "")).strip()
    allowed_email = str(raw_secrets.get("ALLOWED_EMAIL", "")).strip()
    user_id = str(raw_secrets.get("USER_ID", "default_user")).strip() or "default_user"

    if not app_password and not allowed_email:
        return

    if st.session_state.get("authenticated_user") == user_id:
        return

    st.title("🔐 로그인")
    st.caption("허용된 사용자만 접근할 수 있습니다.")
    with st.form("single_user_login_form", clear_on_submit=False):
        login_id = ""
        if allowed_email:
            login_id = st.text_input("이메일 또는 아이디")
        password = ""
        if app_password:
            password = st.text_input("비밀번호", type="password")
        else:
            st.caption("💡 보안을 위해 Secrets에 `APP_PASSWORD`를 함께 설정하는 것을 권장합니다.")
        submitted = st.form_submit_button("로그인", type="primary", use_container_width=True)

    if submitted:
        id_ok = True
        if allowed_email:
            entered = login_id.strip().lower()
            id_ok = entered in {allowed_email.lower(), user_id.lower()}
        pw_ok = _verify_password(password, app_password)

        if id_ok and pw_ok:
            st.session_state["authenticated_user"] = user_id
            st.session_state.pop("app_services", None)
            st.rerun()
        st.error("로그인 정보가 올바르지 않습니다.")
    st.stop()


def render_logout_button() -> None:
    """Render a sidebar logout button when session authentication is active."""
    if st.session_state.get("authenticated_user"):
        st.sidebar.divider()
        if st.sidebar.button("🚪 로그아웃", use_container_width=True):
            st.session_state.pop("authenticated_user", None)
            st.session_state.pop("app_services", None)
            st.rerun()


def get_app_services() -> AppServices:
    """Initialize and cache per-session AppServices strictly bound to UserContext."""
    raw_secrets = _read_secrets_dict()
    users = _extract_users_map(raw_secrets)

    if users:
        dups = find_duplicate_sheet_ids(users)
        if dups:
            details = ", ".join(f"`{sid}` ({', '.join(uids)})" for sid, uids in dups.items())
            st.error(
                f"🚨 멀티유저 설정 오류: 서로 다른 사용자에게 동일한 `SHEET_ID`가 중복 설정되어 있습니다: {details}. "
                "사용자별 독립 Google Sheet ID를 설정한 뒤 다시 시도해 주세요."
            )
            st.stop()
        effective_secrets = _authenticate_multi_user(raw_secrets, users)
    else:
        _authenticate_single_user(raw_secrets)
        effective_secrets = raw_secrets

    cached = st.session_state.get("app_services")
    if (
        cached is not None
        and type(cached.data_provider) is HybridMarketDataProvider
        and hasattr(cached.data_provider, "get_symbol_name")
        and hasattr(cached.data_provider, "format_symbol_display")
    ):
        return cached

    ctx = load_user_context_from_mapping(effective_secrets)

    offline_demo = False
    if ctx.sheet_id and not ctx.credentials.gcp_service_account:
        st.error(
            "🚨 설정 오류: `SHEET_ID`는 설정되어 있으나 GCP 서비스 계정 키(`gcp_service_account` 또는 "
            "`GCP_SERVICE_ACCOUNT_JSON`)가 없습니다. 메모리 백엔드로 주문 이력이 유실되는 것을 방지하기 위해 실행을 중단합니다."
        )
        st.stop()
    elif ctx.sheet_id and ctx.credentials.gcp_service_account:
        sheet = UserSheetManager(ctx)
        sheet.flush_pending_writes()
    else:
        # Local/demo fallback when SHEET_ID is intentionally omitted
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
