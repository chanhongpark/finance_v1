"""
Order confirmation modal before placing any real/mock broker order (R3.3).
Displays symbol, qty, price, price_type, and algorithm name.
"""
from __future__ import annotations

import streamlit as st

from core.algorithms.base import Signal
from core.execution.order_service import OrderService
from ui.auth import AppServices


@st.dialog("주문 실행 확인")
def open_order_confirmation_dialog(services: AppServices, signal: Signal) -> None:
    mode_text = "🟢 모의투자" if services.ctx.is_mock else "🔴 실전투자 (실제 자금 체결)"
    st.markdown(f"**계좌 모드:** {mode_text}")
    st.divider()

    symbol_display = services.data_provider.format_symbol_display(signal.symbol, signal.market.value)
    col1, col2 = st.columns(2)
    col1.metric("종목", f"{symbol_display} ({signal.market.value})")
    col2.metric("매매 구분", signal.action.value)

    col3, col4 = st.columns(2)
    col3.metric("주문 수량", f"{signal.qty or 0:,}주")
    col4.metric("주문 가격", f"{signal.price:,.2f} ({signal.price_type.value})")

    st.markdown(f"**담당 알고리즘:** `{signal.algo_id}` (v{signal.algo_version})")
    st.markdown(f"**판단 사유:** {signal.reason_text}")

    if signal.conditions:
        with st.expander("판단 조건 상세 ⓘ", expanded=False):
            for c in signal.conditions:
                st.write(f"- {c.format_badge()} — {c.desc}")

    st.divider()
    c_cancel, c_ok = st.columns(2)
    if c_cancel.button("취소", use_container_width=True):
        st.rerun()

    if c_ok.button("확인 후 주문 실행", type="primary", use_container_width=True):
        svc = OrderService(services.ctx, services.sheet, services.kis, notifier=services.notifier)
        outcome = svc.execute_signal(signal, user_confirmed=True)
        if outcome.executed:
            st.success(f"✅ {outcome.message} (주문번호: {outcome.order_no})")
        else:
            st.warning(f"⚠️ [{outcome.decision}] {outcome.message}")
        st.rerun()
