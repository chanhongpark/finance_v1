"""Page 4: Personal Settings — Algorithm on/off & parameter overrides, ExecutionMode, Risk Limits (R3.1, R6, R7)."""
from __future__ import annotations

import streamlit as st

from core.algorithms import default_registry
from core.context import ExecutionMode
from ui.auth import AppServices
from ui.components.tooltips import build_algorithm_tooltip


def render_settings(services: AppServices) -> None:
    st.title("⚙️ 설정")
    st.caption(f"모든 설정은 개인 Google Sheet(`{services.sheet.sheet_id}`)에 저장되며 타 사용자와 완전히 분리됩니다.")

    user_settings = services.sheet.load_user_settings()

    st.subheader("1. 실행 모드 및 계좌 리스크 한도")
    with st.form("risk_settings_form"):
        mode_options = [ExecutionMode.PROPOSE_ONLY, ExecutionMode.SEMI_AUTO, ExecutionMode.FULL_AUTO]
        mode_labels = {
            ExecutionMode.PROPOSE_ONLY: "제안만 (수동 승인 · 실주문 안 함)",
            ExecutionMode.SEMI_AUTO: "반자동 (UI 확인 모달 승인 후 주문)",
            ExecutionMode.FULL_AUTO: "완전자동 (스케줄러/액션 자동 주문)",
        }
        selected_mode = st.selectbox(
            "실행 모드",
            options=mode_options,
            index=mode_options.index(user_settings.execution_mode),
            format_func=lambda m: mode_labels[m],
        )

        c1, c2 = st.columns(2)
        max_daily_krw = c1.number_input("일일 최대 매수금액 (KRW)", min_value=100_000.0, value=float(user_settings.max_daily_buy_amount_krw), step=500_000.0)
        max_daily_usd = c2.number_input("일일 최대 매수금액 (USD)", min_value=100.0, value=float(user_settings.max_daily_buy_amount_usd), step=250.0)

        c3, c4 = st.columns(2)
        max_weight = c3.slider("종목당 최대 비중 (%)", 5, 100, int(user_settings.max_symbol_weight_pct * 100)) / 100.0
        max_symbols = int(c4.number_input("최대 동시 보유 종목 수", min_value=1, max_value=50, value=int(user_settings.max_concurrent_symbols)))

        c5, c6 = st.columns(2)
        cb_loss = c5.number_input("일일 서킷브레이커 손실률 (%)", min_value=-30.0, max_value=-1.0, value=float(user_settings.circuit_breaker_daily_loss_pct * 100), step=1.0) / 100.0
        hard_sl = c6.number_input("공통 강제 손절률 risk_override (%)", min_value=-30.0, max_value=-2.0, value=float(user_settings.global_hard_stop_loss_pct * 100), step=1.0) / 100.0

        if st.form_submit_button("리스크/실행 설정 저장", type="primary"):
            user_settings.execution_mode = selected_mode
            user_settings.max_daily_buy_amount_krw = max_daily_krw
            user_settings.max_daily_buy_amount_usd = max_daily_usd
            user_settings.max_symbol_weight_pct = max_weight
            user_settings.max_concurrent_symbols = max_symbols
            user_settings.circuit_breaker_daily_loss_pct = cb_loss
            user_settings.global_hard_stop_loss_pct = hard_sl
            services.sheet.save_user_settings(user_settings)
            st.success("✅ 개인 Sheet에 저장되었습니다.")

    st.subheader("2. 알고리즘 활성화 및 파라미터 Override")
    algo_cfgs = services.sheet.load_algorithm_configs()

    for algo in default_registry.list_all():
        cfg = algo_cfgs.get(algo.id, {"enabled": True, "params": {}})
        merged = algo.merge_params(cfg.get("params", {}))

        with st.expander(f"{algo.name} ({algo.market.value} · v{algo.version})", expanded=False):
            st.caption(algo.description, help=build_algorithm_tooltip(algo))
            with st.form(f"algo_form_{algo.id}"):
                enabled = st.toggle("알고리즘 활성화", value=bool(cfg.get("enabled", True)))
                updated_params = {}
                for spec in algo.params_schema:
                    cur = merged.get(spec.key, spec.default)
                    if spec.param_type == "bool":
                        updated_params[spec.key] = st.checkbox(spec.label, value=bool(cur), help=spec.description)
                    elif spec.param_type == "select" and spec.options:
                        idx = spec.options.index(cur) if cur in spec.options else 0
                        updated_params[spec.key] = st.selectbox(spec.label, options=spec.options, index=idx, help=spec.description)
                    elif spec.param_type == "int":
                        updated_params[spec.key] = int(
                            st.number_input(
                                spec.label,
                                min_value=int(spec.min_value) if spec.min_value is not None else None,
                                max_value=int(spec.max_value) if spec.max_value is not None else None,
                                value=int(cur),
                                step=int(spec.step or 1),
                                help=spec.description,
                            )
                        )
                    else:
                        updated_params[spec.key] = float(
                            st.number_input(
                                spec.label,
                                min_value=float(spec.min_value) if spec.min_value is not None else None,
                                max_value=float(spec.max_value) if spec.max_value is not None else None,
                                value=float(cur),
                                step=float(spec.step or 0.01),
                                help=spec.description,
                            )
                        )

                if st.form_submit_button(f"{algo.name} 설정 저장"):
                    services.sheet.save_algorithm_config(algo.id, enabled=enabled, params=updated_params)
                    st.success(f"✅ `{algo.id}` 설정이 저장되었습니다.")
