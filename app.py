"""
finance_v1 — Streamlit 4-page entry point (R3.1).
Pages: ① 대시보드, ② 추천, ③ 거래 기록, ④ 설정
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import streamlit as st

_APP_ROOT = Path(__file__).resolve().parent


def _evict_stale_app_modules_if_updated() -> None:
    """
    Streamlit Cloud hot-reloads `app.py` and dynamically loaded pages in the same
    Python process without reloading transitive `core.*` / `ui.*` modules already
    cached in `sys.modules`. Detect source mtime changes and clear stale modules
    so newly added symbols (e.g. `run_us_screening`) never fail with ImportError.
    """
    sig = max(
        (
            p.stat().st_mtime_ns
            for folder in ("core", "ui")
            for p in (_APP_ROOT / folder).rglob("*.py")
        ),
        default=0,
    )
    prev_sig = getattr(sys, "_finance_v1_code_sig", None)
    has_loaded = any(
        name == "core" or name.startswith("core.") or name == "ui" or name.startswith("ui.")
        for name in sys.modules
    )
    if (prev_sig is None and has_loaded) or (prev_sig is not None and prev_sig != sig):
        for name in list(sys.modules):
            if name == "core" or name.startswith("core.") or name == "ui" or name.startswith("ui."):
                sys.modules.pop(name, None)
    sys._finance_v1_code_sig = sig  # type: ignore[attr-defined]


_evict_stale_app_modules_if_updated()

from ui.auth import get_app_services, render_logout_button
from ui.components.header import render_app_header

st.set_page_config(
    page_title="KIS Auto Trader v2 (finance_v1)",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _load_page_fn(filename: str, fn_name: str):
    path = Path(__file__).resolve().parent / "ui" / "pages" / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, fn_name)


def main() -> None:
    services = get_app_services()
    render_app_header(services.ctx, is_offline_demo=services.is_offline_demo)

    page = st.sidebar.radio(
        "메뉴",
        options=["① 대시보드", "② 추천", "③ 거래 기록", "④ 설정"],
        label_visibility="collapsed",
    )
    render_logout_button()

    if page == "① 대시보드":
        _load_page_fn("1_dashboard.py", "render_dashboard")(services)
    elif page == "② 추천":
        _load_page_fn("2_recommendations.py", "render_recommendations")(services)
    elif page == "③ 거래 기록":
        _load_page_fn("3_history.py", "render_history")(services)
    elif page == "④ 설정":
        _load_page_fn("4_settings.py", "render_settings")(services)


if __name__ == "__main__":
    main()
