"""
Mobile-friendly card view renderer for Lots and Signals (R3.5).
"""
from __future__ import annotations

from typing import Callable, Optional

import streamlit as st

from core.algorithms.base import Signal
from core.ledger.models import Lot, LotMetrics


def _format_symbol_title(symbol: str, market: str, symbol_name: Optional[str]) -> str:
    sym = symbol.strip().upper()
    if market.upper() == "KR" and sym.isdigit() and len(sym) < 6:
        sym = sym.zfill(6)
    name = (symbol_name or "").strip()
    if not name or name.upper() == sym:
        return sym
    if market.upper() == "KR":
        return f"{name} ({sym})"
    return f"{sym} ({name})"


def render_lot_card(
    lot: Lot,
    metrics: LotMetrics,
    current_price: Optional[float] = None,
    on_map_click: Optional[Callable[[Lot], None]] = None,
    symbol_name: Optional[str] = None,
) -> None:
    eval_price = current_price if (current_price and current_price > 0) else metrics.avg_price
    unrealized_pnl = (eval_price - metrics.avg_price) * metrics.remaining_qty
    unrealized_pct = ((eval_price - metrics.avg_price) / metrics.avg_price * 100.0) if metrics.avg_price > 0 else 0.0

    with st.container(border=True):
        top_l, top_r = st.columns([3, 1])
        algo_badge = "⚠️ 미할당(manual)" if lot.algo_id == "manual" else f"`{lot.algo_id}` v{lot.algo_version}"
        symbol_title = _format_symbol_title(lot.symbol, lot.market, symbol_name)
        top_l.markdown(f"**{symbol_title}** ({lot.market}) · {algo_badge}")
        if lot.algo_id == "manual" and on_map_click is not None:
            if top_r.button("알고리즘 연결", key=f"map_{lot.lot_id}", use_container_width=True):
                on_map_click(lot)
        else:
            top_r.caption(f"보유 {metrics.holding_days}일")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("잔량", f"{metrics.remaining_qty:,}주")
        m2.metric("평단가", f"{metrics.avg_price:,.2f}")
        m3.metric("평가손익", f"{unrealized_pnl:+,.0f}", f"{unrealized_pct:+.2f}%")
        m4.metric("실현손익", f"{metrics.realized_pnl:+,.0f}")


def render_signal_card(
    signal: Signal,
    idx: int,
    on_order_click: Callable[[Signal], None],
    symbol_name: Optional[str] = None,
) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        action_icon = "🟢 매수" if signal.action.value == "BUY" else "🔴 매도"
        symbol_title = _format_symbol_title(signal.symbol, signal.market.value, symbol_name)
        c1.markdown(
            f"**{action_icon} {symbol_title}** ({signal.market.value}) · `{signal.algo_id}` (점수 {signal.score:.0f})"
        )
        if c2.button("주문 확인", key=f"ord_{signal.algo_id}_{signal.symbol}_{idx}", type="primary", use_container_width=True):
            on_order_click(signal)

        st.caption(
            f"가격: **{signal.price:,.2f}** ({signal.price_type.value}) | 수량: **{signal.qty or 0:,}주** | {signal.reason_text}"
        )
        if signal.conditions:
            st.markdown(
                " ".join(f"`{c.format_badge()}`" for c in signal.conditions),
                help=signal.format_conditions_tooltip(),
            )
