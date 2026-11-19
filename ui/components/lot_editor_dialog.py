"""
Manual onboarding & existing holding algorithm mapping modal.

Allows users to:
1. Register pre-existing holdings with no prior history (symbol, buy_date, qty, avg_price, algo_id, algo_state).
2. Map an unassigned 'manual' lot (from KIS reconciliation) to a specific algorithm,
   with optional quantity splitting across multiple algorithms.
3. Configure initial state for stateful algorithms:
   - Fujimoto 1:2:6: entry_stage (1/2/3), exit_stage (0/1/2)
   - US Infinite Buying: total_budget_usd, division_count, auto-calculated or manual T-round
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import streamlit as st

from core.algorithms import Market, default_registry
from core.ledger.models import Lot
from core.ledger.onboarding import (
    assign_algorithm_to_manual_lot,
    build_fujimoto_initial_state,
    build_infinite_buying_initial_state,
    register_existing_holding,
)
from ui.auth import AppServices


@st.dialog("기존 보유 종목 등록 / 알고리즘 연결")
def open_lot_editor_dialog(services: AppServices, manual_lot: Optional[Lot] = None) -> None:
    is_mapping = manual_lot is not None
    st.caption(
        "기존 계좌에 보유 중인 종목에 매수일·담당 알고리즘·진행 단계를 지정하면 "
        "앞으로 해당 알고리즘의 청산 및 추가매수 규칙이 자동 적용됩니다."
    )

    if is_mapping and manual_lot is not None:
        market_val = manual_lot.market
        symbol_val = manual_lot.symbol
        max_qty = manual_lot.qty
        avg_price_val = manual_lot.avg_price
        symbol_display = services.data_provider.format_symbol_display(symbol_val, market_val)
        st.info(f"대상 보유 건: `{manual_lot.lot_id}` | **{symbol_display}** ({market_val}) | 현재 잔량 {max_qty:,}주 @ {avg_price_val:,.2f}")
    else:
        c_m, c_s = st.columns([1, 2])
        market_val = c_m.selectbox("시장", ["KR", "US"], index=0)
        symbol_val = c_s.text_input("종목코드 (예: 005930 또는 TQQQ)").strip().upper()
        max_qty = 1_000_000
        avg_price_val = 0.0

    # Available algorithms for the chosen market (+ manual option)
    mkt_enum = Market.US if market_val == "US" else Market.KR
    algos = default_registry.list_all(market=mkt_enum)
    algo_options = {a.id: f"{a.name} ({a.id})" for a in algos}
    algo_options["manual"] = "수동 관리 유지 (manual — 공통 강제 손절만 적용)"

    selected_algo_id = st.selectbox(
        "담당 알고리즘",
        options=list(algo_options.keys()),
        format_func=lambda k: algo_options[k],
        help="선택한 알고리즘의 evaluate_exit 로직으로 향후 청산을 관리합니다.",
    )

    c1, c2, c3 = st.columns(3)
    default_date = date.today()
    if is_mapping and manual_lot and manual_lot.buy_date:
        try:
            default_date = datetime.strptime(manual_lot.buy_date[:10], "%Y-%m-%d").date()
        except ValueError:
            pass

    buy_dt = c1.date_input("최초 매수일", value=default_date, help="보유일수 계산에 사용됩니다.")
    qty_val = int(
        c2.number_input(
            "할당 수량" if is_mapping else "보유 수량",
            min_value=1,
            max_value=max(1, max_qty),
            value=max_qty if is_mapping else 1,
            step=1,
            help="기존 보유 수량 일부만 입력하면 나머지 수량은 별도 보유 건으로 분할 유지됩니다." if is_mapping else "보유 주식 수량",
        )
    )
    price_val = float(
        c3.number_input(
            "평균 매입단가",
            min_value=0.01,
            value=float(avg_price_val) if avg_price_val > 0 else (50000.0 if market_val == "KR" else 50.0),
            step=100.0 if market_val == "KR" else 0.5,
            disabled=is_mapping,
        )
    )

    algo_state = {}
    if selected_algo_id == "kr_fujimoto_126":
        st.markdown("##### 후지모토 1:2:6 진행 단계 설정")
        fc1, fc2 = st.columns(2)
        entry_stage = fc1.selectbox(
            "현재 매수 완료 단계",
            options=[1, 2, 3],
            format_func=lambda x: {1: "1단계 (1/9 탐색 진입 완료)", 2: "2단계 (1+2/9 추가 매수 완료)", 3: "3단계 (1+2+6 풀매수 완료)"}[x],
        )
        exit_stage = fc2.selectbox(
            "기완료 청산 단계",
            options=[0, 1, 2],
            format_func=lambda x: {0: "미청산 (0차)", 1: "1차 분할청산 완료", 2: "2차 분할청산 완료"}[x],
        )
        algo_state = build_fujimoto_initial_state(entry_stage=entry_stage, exit_stage=exit_stage, base_entry_price=price_val)

    elif selected_algo_id == "us_infinite_buying":
        st.markdown("##### 미국 무한매수법 초기 상태 설정")
        ic1, ic2 = st.columns(2)
        total_budget = ic1.number_input("종목 총 예산 ($)", min_value=100.0, value=10000.0, step=500.0)
        division_cnt = int(ic2.number_input("총 분할 횟수", min_value=10, max_value=100, value=40, step=5))
        auto_state = build_infinite_buying_initial_state(
            qty=qty_val,
            avg_price=price_val,
            total_budget_usd=total_budget,
            division_count=division_cnt,
        )
        override_t = st.number_input(
            "현재 진행 회차 (T값 — 자동 계산됨, 필요 시 조정)",
            min_value=0.0,
            max_value=float(division_cnt * 2),
            value=float(auto_state["current_round"]),
            step=0.5,
        )
        algo_state = build_infinite_buying_initial_state(
            qty=qty_val,
            avg_price=price_val,
            total_budget_usd=total_budget,
            division_count=division_cnt,
            current_round_override=override_t,
        )

    note = st.text_input("메모 / 매수 사유", value="기존 보유 종목 이관")

    if st.button("저장하기", type="primary", use_container_width=True):
        if not symbol_val:
            st.error("종목코드를 입력해 주세요.")
            return

        algo_obj = default_registry.get(selected_algo_id)
        algo_ver = algo_obj.version if algo_obj else "0.0.0"

        if is_mapping and manual_lot is not None:
            updated_lots, new_fills, audit = assign_algorithm_to_manual_lot(
                manual_lot=manual_lot,
                target_algo_id=selected_algo_id,
                target_algo_version=algo_ver,
                assign_qty=qty_val,
                buy_date=buy_dt.strftime("%Y-%m-%d"),
                algo_state=algo_state,
                note=note,
                actor=services.ctx.user_id,
            )
            if new_fills:
                services.sheet.append_fills(new_fills)
            services.sheet.upsert_lots(updated_lots)
            services.sheet.append_audit(audit)
            st.success("✅ 기존 보유 건에 알고리즘이 연결되었습니다.")
        else:
            lot, fill, audit = register_existing_holding(
                symbol=symbol_val,
                market=market_val,
                qty=qty_val,
                avg_price=price_val,
                algo_id=selected_algo_id,
                algo_version=algo_ver,
                buy_date=buy_dt.strftime("%Y-%m-%d"),
                algo_state=algo_state,
                note=note,
                actor=services.ctx.user_id,
            )
            services.sheet.append_fills([fill])
            services.sheet.upsert_lots([lot])
            services.sheet.append_audit(audit)
            symbol_display = services.data_provider.format_symbol_display(symbol_val, market_val)
            st.success(f"✅ {symbol_display} ({qty_val}주) 보유 건이 등록되었습니다.")
        st.rerun()
