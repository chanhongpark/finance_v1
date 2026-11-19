"""Page 2: Today's Buy/Sell Recommendations (KR 3 algos + US Infinite Buying preview) (R3.1, R4.3, R13, R15)."""
from __future__ import annotations

import streamlit as st

from core.algorithms import Market, USInfiniteBuyingAlgorithm, default_registry
from core.execution.runner import run_kr_screening
from core.ledger.models import LotStatus
from ui.auth import AppServices
from ui.components.order_dialog import open_order_confirmation_dialog
from ui.components.responsive_cards import render_signal_card
from ui.components.tooltips import build_algorithm_tooltip

DEFAULT_KR_WATCHLIST = "005930,000660,035420,005380,051910,035720,068270,105560,055550,012330"


def render_recommendations(services: AppServices) -> None:
    st.title("🎯 오늘의 추천 (매수 / 매도)")

    tab_kr, tab_us = st.tabs(["🇰🇷 국내 주식 추천 (월간추세 · 컵앤핸들 · 후지모토 1:2:6)", "🇺🇸 미국 무한매수 현황"])

    with tab_kr:
        st.caption(
            "1단계에서 FinanceDataReader + 로컬 증분 캐시로 고속 스크리닝(KIS API 0회) 후, "
            "2단계에서 최종 후보 및 보유 종목만 KIS 실시간 현재가를 결합합니다. "
            "여러 알고리즘이 같은 종목을 추천하면 각각 별도 행으로 표시되며 독립 매수가 가능합니다."
        )

        # Show registered KR algorithms with auto-generated tooltips (R3.2)
        kr_algos = default_registry.list_all(market=Market.KR)
        cols = st.columns(len(kr_algos))
        for col, algo in zip(cols, kr_algos):
            col.markdown(f"**{algo.name}** ⓘ", help=build_algorithm_tooltip(algo))

        raw_settings = services.sheet.load_raw_settings()
        default_syms = raw_settings.get("kr_watchlist", DEFAULT_KR_WATCHLIST)
        watchlist_input = st.text_input(
            "스크리닝 대상 국내 종목코드 (쉼표 구분)",
            value=default_syms,
            help="KOSPI/KOSDAQ 6자리 종목코드를 쉼표로 구분하여 입력합니다.",
        )

        if st.button("🔍 국내 추천 스크리닝 실행", type="primary"):
            symbols = [s.strip() for s in watchlist_input.split(",") if s.strip()]
            with st.spinner(f"{len(symbols)}개 종목 1단계(FDR 캐시) → 2단계(KIS 실시간) 평가 중..."):
                result = run_kr_screening(
                    ctx=services.ctx,
                    sheet_manager=services.sheet,
                    data_provider=services.data_provider,
                    universe_symbols=symbols,
                    include_live_quotes_for_candidates=True,
                )
            st.session_state["kr_screening_result"] = result

        result = st.session_state.get("kr_screening_result")
        if result:
            st.subheader(f"🔴 매도 / 청산 제안 ({len(result['sell'])}건)")
            if not result["sell"]:
                st.caption("현재 청산 조건에 도달한 보유 건이 없습니다.")
            for idx, sig in enumerate(result["sell"]):
                render_signal_card(sig, idx, on_order_click=lambda s: open_order_confirmation_dialog(services, s))

            st.subheader(f"🟢 신규 / 추가 매수 제안 ({len(result['buy'])}건)")
            if not result["buy"]:
                st.caption("현재 진입 조건을 충족한 종목이 없습니다.")
            for idx, sig in enumerate(result["buy"]):
                render_signal_card(sig, idx + 1000, on_order_click=lambda s: open_order_confirmation_dialog(services, s))

    with tab_us:
        us_algo = default_registry.require("us_infinite_buying")
        assert isinstance(us_algo, USInfiniteBuyingAlgorithm)
        st.markdown(f"**{us_algo.name}** ⓘ", help=build_algorithm_tooltip(us_algo))
        st.caption("미국 무한매수 자동매매는 GitHub Actions에서 스케줄 실행되며, 아래에서 당일 예상 LOC/지정가 주문을 미리 확인하거나 수동 실행할 수 있습니다.")

        raw_settings = services.sheet.load_raw_settings()
        us_syms_str = st.text_input("미국 무한매수 대상 티커 (쉼표 구분)", value=raw_settings.get("us_infinite_symbols", "TQQQ,SOXL"))

        if st.button("🔎 미국 당일 LOC/익절 주문 미리보기", type="primary"):
            us_syms = [s.strip().upper() for s in us_syms_str.split(",") if s.strip()]
            open_us_lots = {
                l.symbol.upper(): l
                for l in services.sheet.load_lots()
                if l.status == LotStatus.OPEN and l.qty > 0 and l.market == "US" and l.algo_id == us_algo.id
            }
            algo_cfgs = services.sheet.load_algorithm_configs()
            params = algo_cfgs.get(us_algo.id, {}).get("params", {})

            us_signals = []
            for sym in us_syms:
                df = services.data_provider.get_daily_ohlcv(sym, market="US", lookback_days=60, include_live_quote=True)
                if not df.empty:
                    us_signals.extend(us_algo.generate_daily_signals(sym, df, lot=open_us_lots.get(sym), params=params))
            st.session_state["us_preview_signals"] = us_signals

        for idx, sig in enumerate(st.session_state.get("us_preview_signals", [])):
            render_signal_card(sig, idx + 2000, on_order_click=lambda s: open_order_confirmation_dialog(services, s))
