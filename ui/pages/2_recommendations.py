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
DEFAULT_US_SYMBOLS = "TQQQ,SOXL"


def _parse_symbols(raw: str, *, upper: bool = False) -> list[str]:
    seen: set[str] = set()
    symbols: list[str] = []
    for token in raw.split(","):
        sym = token.strip()
        if upper:
            sym = sym.upper()
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


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
        saved_kr_syms = raw_settings.get("kr_watchlist", DEFAULT_KR_WATCHLIST)
        watchlist_input = st.text_input(
            "스크리닝 대상 국내 종목코드 (쉼표 구분)",
            value=saved_kr_syms,
            help="KOSPI/KOSDAQ 6자리 종목코드를 쉼표로 구분하여 입력합니다. 저장하거나 스크리닝 실행 시 개인 Sheet에 저장됩니다.",
        )

        btn_kr_run, btn_kr_save = st.columns([2, 1])
        run_kr_clicked = btn_kr_run.button("🔍 국내 추천 스크리닝 실행", type="primary", use_container_width=True)
        save_kr_clicked = btn_kr_save.button("💾 종목 목록 저장", use_container_width=True)

        if save_kr_clicked:
            symbols = _parse_symbols(watchlist_input)
            if not symbols:
                st.warning("종목코드를 1개 이상 입력해 주세요.")
            else:
                services.sheet.save_setting("kr_watchlist", ",".join(symbols))
                st.success(f"✅ 국내 스크리닝 종목({len(symbols)}개)이 개인 Sheet에 저장되었습니다.")

        if run_kr_clicked:
            symbols = _parse_symbols(watchlist_input)
            if not symbols:
                st.warning("종목코드를 1개 이상 입력해 주세요.")
            else:
                normalized_kr = ",".join(symbols)
                if normalized_kr != saved_kr_syms:
                    services.sheet.save_setting("kr_watchlist", normalized_kr)
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
                render_signal_card(
                    sig,
                    idx,
                    on_order_click=lambda s: open_order_confirmation_dialog(services, s),
                    symbol_name=services.data_provider.get_symbol_name(sig.symbol, sig.market.value),
                )

            st.subheader(f"🟢 신규 / 추가 매수 제안 ({len(result['buy'])}건)")
            if not result["buy"]:
                st.caption("현재 진입 조건을 충족한 종목이 없습니다.")
            for idx, sig in enumerate(result["buy"]):
                render_signal_card(
                    sig,
                    idx + 1000,
                    on_order_click=lambda s: open_order_confirmation_dialog(services, s),
                    symbol_name=services.data_provider.get_symbol_name(sig.symbol, sig.market.value),
                )

    with tab_us:
        us_algo = default_registry.require("us_infinite_buying")
        assert isinstance(us_algo, USInfiniteBuyingAlgorithm)
        st.markdown(f"**{us_algo.name}** ⓘ", help=build_algorithm_tooltip(us_algo))
        st.caption("미국 무한매수 자동매매는 GitHub Actions에서 스케줄 실행되며, 아래에서 당일 예상 LOC/지정가 주문을 미리 확인하거나 수동 실행할 수 있습니다.")

        raw_settings = services.sheet.load_raw_settings()
        saved_us_syms = raw_settings.get("us_infinite_symbols", DEFAULT_US_SYMBOLS)
        us_syms_str = st.text_input(
            "미국 무한매수 대상 티커 (쉼표 구분)",
            value=saved_us_syms,
            help="미국 티커를 쉼표로 구분하여 입력합니다. 저장하거나 미리보기 실행 시 개인 Sheet에 저장됩니다.",
        )

        btn_us_run, btn_us_save = st.columns([2, 1])
        run_us_clicked = btn_us_run.button("🔎 미국 당일 LOC/익절 주문 미리보기", type="primary", use_container_width=True)
        save_us_clicked = btn_us_save.button("💾 티커 목록 저장", use_container_width=True)

        if save_us_clicked:
            us_syms = _parse_symbols(us_syms_str, upper=True)
            if not us_syms:
                st.warning("티커를 1개 이상 입력해 주세요.")
            else:
                services.sheet.save_setting("us_infinite_symbols", ",".join(us_syms))
                st.success(f"✅ 미국 무한매수 티커({len(us_syms)}개)가 개인 Sheet에 저장되었습니다.")

        if run_us_clicked:
            us_syms = _parse_symbols(us_syms_str, upper=True)
            if not us_syms:
                st.warning("티커를 1개 이상 입력해 주세요.")
            else:
                normalized_us = ",".join(us_syms)
                if normalized_us != saved_us_syms:
                    services.sheet.save_setting("us_infinite_symbols", normalized_us)
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
            render_signal_card(
                sig,
                idx + 2000,
                on_order_click=lambda s: open_order_confirmation_dialog(services, s),
                symbol_name=services.data_provider.get_symbol_name(sig.symbol, sig.market.value),
            )
