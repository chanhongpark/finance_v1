"""
finance_v1 — Streamlit 4-page entry point (R3.1).
Pages: ① 대시보드, ② 추천, ③ 거래 기록, ④ 설정
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import streamlit as st

from ui.auth import get_app_services
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
