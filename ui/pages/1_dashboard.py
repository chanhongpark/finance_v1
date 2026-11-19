"""Page 1: Dashboard — Open Lots, Manual Holding Onboarding/Reconciliation, Per-Algo Performance (R3.1, R5.7)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core.execution.runner import run_daily_reconciliation
from core.ledger.calculator import calculate_algorithm_performance, recalculate_lot_from_fills
from core.ledger.models import LotStatus
from ui.auth import AppServices
from ui.components.lot_editor_dialog import open_lot_editor_dialog
from ui.components.responsive_cards import render_lot_card


def render_dashboard(services: AppServices) -> None:
    st.title("📊 대시보드")

    # Action bar: Reconcile KIS holdings + Manual holding registration
    b1, b2, _ = st.columns([2, 2, 3])
    if b1.button("🔄 KIS 보유잔고 동기화 (일치 확인)", use_container_width=True, help="KIS 실제 잔고와 Sheet 원장을 대조하여 미등록 보유종목을 자동 감지합니다."):
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

    if b2.button("➕ 기존 보유종목 수동 등록", use_container_width=True, help="매수 히스토리가 없는 기존 보유 종목을 원장에 직접 등록합니다."):
        open_lot_editor_dialog(services, manual_lot=None)

    lots = services.sheet.load_lots()
    fills = services.sheet.load_fills()
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

    # Top KPI summary
    lot_metrics_list = [(lot, recalculate_lot_from_fills(lot, fills)) for lot in open_lots]
    total_invested = sum(m.remaining_qty * m.avg_price for _, m in lot_metrics_list)
    total_unrealized = sum(
        ((live_prices.get((lot.market.upper(), lot.symbol), 0.0) or m.avg_price) - m.avg_price) * m.remaining_qty
        for lot, m in lot_metrics_list
    )
    perf_map = calculate_algorithm_performance(lots, fills)
    total_realized = sum(p.total_realized_pnl for p in perf_map.values())

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("보유 건수", f"{len(open_lots)}건")
    k2.metric("총 매입원금 (추정)", f"{total_invested:,.0f}")
    k3.metric("총 평가손익", f"{total_unrealized:+,.0f}")
    k4.metric("누적 실현손익", f"{total_realized:+,.0f}")

    st.subheader("보유 건 현황")
    if not lot_metrics_list:
        st.info("보유 중인 건이 없습니다. 추천 페이지에서 매수하거나 상단 **[➕ 기존 보유종목 수동 등록]** 으로 기존 잔고를 추가해 보세요.")
    else:
        for lot, metrics in lot_metrics_list:
            curr_price = live_prices.get((lot.market.upper(), lot.symbol), 0.0)
            render_lot_card(
                lot=lot,
                metrics=metrics,
                current_price=curr_price if curr_price > 0 else None,
                on_map_click=lambda target_lot: open_lot_editor_dialog(services, manual_lot=target_lot),
                symbol_name=services.data_provider.get_symbol_name(lot.symbol, lot.market),
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
                "실현손익": p.total_realized_pnl,
                "평균 보유일": p.avg_holding_days,
                "MDD(%)": p.max_drawdown_pct,
            }
            for p in perf_map.values()
        ]
        st.dataframe(pd.DataFrame(perf_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("아직 집계된 알고리즘 거래 이력이 없습니다.")
