"""Page 1: Dashboard — Open Lots, Manual Holding Onboarding/Reconciliation, Per-Algo Performance (R3.1, R5.7)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core.execution.runner import run_daily_reconciliation
from core.ledger.calculator import calculate_algorithm_performance, recalculate_lot_from_fills
from core.ledger.models import LotStatus
from ui.auth import AppServices
from ui.components.lot_editor_dialog import open_lot_editor_dialog
from ui.components.responsive_cards import compute_lot_guidance, render_lot_card


_DASHBOARD_COMPACT_CSS = """
<style>
[data-testid="stMetric"] {
    padding: 0.1rem 0 !important;
}
[data-testid="stMetricLabel"] p {
    font-size: 0.76rem !important;
    margin-bottom: 0.05rem !important;
}
[data-testid="stMetricValue"] {
    font-size: 1.15rem !important;
    line-height: 1.25 !important;
}
[data-testid="stMetricDelta"] {
    font-size: 0.74rem !important;
}
</style>
"""


def render_dashboard(services: AppServices) -> None:
    st.markdown(_DASHBOARD_COMPACT_CSS, unsafe_allow_html=True)
    st.title("📊 대시보드")

    # Action bar: Reconcile KIS holdings + Manual holding registration
    b1, b2, _ = st.columns([2, 2, 3])
    if b1.button("🔄 KIS 보유잔고 동기화 (일치 확인)", width="stretch", help="KIS 실제 잔고와 Sheet 원장을 대조하여 미등록 보유종목을 자동 감지합니다."):
        report = run_daily_reconciliation(
            services.ctx,
            services.sheet,
            services.kis,
            markets=("KR", "US"),
            notifier=services.notifier,
        )
        if report.is_matched:
            st.success("✅ KIS 잔고와 Google Sheet 보유 원장이 100% 일치합니다.")
        else:
            st.warning(f"⚠️ {len(report.discrepancies)}건의 불일치가 감지되어 처리되었습니다.")
            for d in report.discrepancies:
                sym_disp = services.data_provider.format_symbol_display(d.symbol, d.market)
                st.write(f"- **{sym_disp}** ({d.market}): KIS {d.kis_qty}주 vs Sheet {d.sheet_qty}주 → {d.action_taken}")

    if b2.button("➕ 기존 보유종목 수동 등록", width="stretch", help="매수 히스토리가 없는 기존 보유 종목을 원장에 직접 등록합니다."):
        open_lot_editor_dialog(services, manual_lot=None)

    lots = services.sheet.load_lots()
    fills = services.sheet.load_fills()
    algo_configs = services.sheet.load_algorithm_configs()
    user_settings = services.sheet.load_user_settings()
    open_lots = [l for l in lots if l.status == LotStatus.OPEN and l.qty > 0]
    manual_lots = [l for l in open_lots if l.algo_id == "manual"]

    if manual_lots:
        st.warning(
            f"⚠️ 알고리즘이 지정되지 않은 기존 보유 종목(manual)이 **{len(manual_lots)}건** 있습니다. "
            "아래 카드의 **[알고리즘 연결]** 버튼을 눌러 담당 알고리즘과 초기 단계를 설정해 주세요."
        )

    # Fetch live prices for open lots (cached via data_provider TTL)
    live_prices: dict[tuple[str, str], float] = {}
    for lot in open_lots:
        key = (lot.market.upper(), lot.symbol)
        if key not in live_prices:
            quote = services.data_provider.get_live_quote(lot.symbol, market=lot.market)
            price = float(quote.get("price", 0.0) or 0.0) if quote else 0.0
            live_prices[key] = price

    # Overall summary (counts only — KRW/USD amounts are never summed together)
    kr_open = [l for l in open_lots if l.market.upper() == "KR"]
    us_open = [l for l in open_lots if l.market.upper() == "US"]
    s1, s2, s3 = st.columns(3)
    s1.metric("전체 보유 건수", f"{len(open_lots)}건")
    s2.metric("🇰🇷 한국 주식", f"{len(kr_open)}건")
    s3.metric("🇺🇸 미국 주식", f"{len(us_open)}건")

    tab_kr, tab_us = st.tabs([f"🇰🇷 한국 주식 ({len(kr_open)})", f"🇺🇸 미국 주식 ({len(us_open)})"])
    with tab_kr:
        _render_market_section(services, "KR", lots, fills, kr_open, live_prices, algo_configs, user_settings.global_hard_stop_loss_pct)
    with tab_us:
        _render_market_section(services, "US", lots, fills, us_open, live_prices, algo_configs, user_settings.global_hard_stop_loss_pct)


def _render_market_section(
    services: AppServices,
    market: str,
    all_lots: list,
    fills: list,
    open_lots: list,
    live_prices: dict[tuple[str, str], float],
    algo_configs: dict,
    global_hard_stop_loss_pct: float,
) -> None:
    is_kr = market == "KR"
    unit = "원" if is_kr else "USD"
    amt_fmt = "{:,.0f}" if is_kr else "{:,.2f}"
    pnl_fmt = "{:+,.0f}" if is_kr else "{:+,.2f}"

    lot_metrics_list = [(lot, recalculate_lot_from_fills(lot, fills)) for lot in open_lots]
    total_invested = sum(m.remaining_qty * m.avg_price for _, m in lot_metrics_list)
    total_eval = sum(
        (live_prices.get((lot.market.upper(), lot.symbol), 0.0) or m.avg_price) * m.remaining_qty
        for lot, m in lot_metrics_list
    )
    total_unrealized = total_eval - total_invested
    unrealized_pct = (total_unrealized / total_invested * 100.0) if total_invested > 0 else 0.0

    market_lots = [l for l in all_lots if l.market.upper() == market]
    perf_map = calculate_algorithm_performance(market_lots, fills)
    total_realized = sum(p.total_realized_pnl for p in perf_map.values())

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("보유 건수", f"{len(open_lots)}건")
    k2.metric(f"총 매입원금 ({unit})", amt_fmt.format(total_invested))
    k3.metric(f"총 평가손익 ({unit})", pnl_fmt.format(total_unrealized), f"{unrealized_pct:+.2f}%")
    k4.metric(f"누적 실현손익 ({unit})", pnl_fmt.format(total_realized))

    st.subheader("보유 건 현황")
    if not lot_metrics_list:
        st.info(
            f"보유 중인 {'한국' if is_kr else '미국'} 주식이 없습니다. "
            "추천 페이지에서 매수하거나 상단 **[➕ 기존 보유종목 수동 등록]** 으로 기존 잔고를 추가해 보세요."
        )
    else:
        for lot, metrics in lot_metrics_list:
            curr_price = live_prices.get((lot.market.upper(), lot.symbol), 0.0)
            cached_df = services.data_provider.cache.load_ohlcv(lot.market, lot.symbol)
            guidance = compute_lot_guidance(
                lot=lot,
                avg_price=metrics.avg_price,
                current_price=curr_price if curr_price > 0 else None,
                algo_configs=algo_configs,
                global_hard_stop_loss_pct=global_hard_stop_loss_pct,
                cached_df=cached_df,
            )
            render_lot_card(
                lot=lot,
                metrics=metrics,
                current_price=curr_price if curr_price > 0 else None,
                on_map_click=lambda target_lot: open_lot_editor_dialog(services, manual_lot=target_lot),
                symbol_name=services.data_provider.get_symbol_name(lot.symbol, lot.market),
                guidance_items=guidance,
            )

    st.subheader("알고리즘별 성과 비교")
    if perf_map:
        perf_rows = [
            {
                "알고리즘": p.algo_id,
                "전체 운영 건": p.total_lots,
                "보유 중": p.open_lots,
                "청산 완료": p.closed_lots,
                "승률(%)": p.win_rate_pct,
                "평균 수익률(%)": p.avg_return_pct,
                f"실현손익({unit})": p.total_realized_pnl,
                "평균 보유일": p.avg_holding_days,
                "MDD(%)": p.max_drawdown_pct,
            }
            for p in perf_map.values()
        ]
        st.dataframe(pd.DataFrame(perf_rows), hide_index=True)
    else:
        st.caption("아직 집계된 알고리즘 거래 이력이 없습니다.")
