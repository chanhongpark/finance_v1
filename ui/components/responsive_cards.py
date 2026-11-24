"""
Mobile-friendly card view renderer for Lots and Signals (R3.5).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import streamlit as st

from core.algorithms import default_registry
from core.algorithms.base import Signal
from core.indicators import calculate_technical_indicators, resample_to_monthly
from core.ledger.models import Lot, LotMetrics


def _fmt_mkt_price(price: float, is_kr: bool) -> str:
    return f"{price:,.0f}원" if is_kr else f"${price:,.2f}"


def compute_lot_guidance(
    lot: Lot,
    avg_price: float,
    current_price: Optional[float],
    algo_configs: Dict[str, Dict[str, Any]],
    global_hard_stop_loss_pct: float = -0.15,
    cached_df: Optional[pd.DataFrame] = None,
) -> List[str]:
    """
    Compute compact, algorithm-specific sell / add-buy price guidance badges for an OPEN lot.
    Uses only lot state, algorithm params, live price, and optional locally cached OHLCV (0 API calls).
    """
    if avg_price <= 0:
        return []

    is_kr = lot.market.upper() == "KR"
    algo_id = lot.algo_id
    algo = default_registry.get(algo_id)
    raw_params = algo_configs.get(algo_id, {}).get("params", {})
    p = algo.merge_params(raw_params) if algo is not None else {}
    state = lot.algo_state or {}
    items: List[str] = []

    if algo_id in {"kr_monthly_trend", "us_monthly_trend"}:
        tp_rate = float(p.get("target_profit_rate", 0.20))
        sl_rate = float(p.get("stop_loss_rate", -0.07))
        sma_period = int(p.get("monthly_sma_period", 10))
        tp_price = avg_price * (1.0 + tp_rate)
        sl_price = avg_price * (1.0 + sl_rate)
        items.append(f"🔴 익절: **{_fmt_mkt_price(tp_price, is_kr)}** ({tp_rate*100:+.0f}%)")
        items.append(f"🛑 손절: **{_fmt_mkt_price(sl_price, is_kr)}** ({sl_rate*100:.1f}%)")
        sma_val = None
        if cached_df is not None and not cached_df.empty:
            monthly = resample_to_monthly(cached_df)
            if len(monthly) >= sma_period:
                val = monthly["close"].rolling(window=sma_period).mean().iloc[-1]
                if pd.notna(val) and float(val) > 0:
                    sma_val = float(val)
        if sma_val is not None:
            items.append(f"📉 추세이탈: 월봉SMA{sma_period} **{_fmt_mkt_price(sma_val, is_kr)}**")
        else:
            items.append(f"📉 추세이탈: 월봉SMA{sma_period} 하향")

    elif algo_id in {"kr_cup_and_handle", "us_cup_and_handle"}:
        tp_rate = float(p.get("target_profit_rate", 0.10))
        sl_rate = float(p.get("stop_loss_rate", -0.05))
        custom_tp = float(state.get("target_price", 0.0) or 0.0)
        custom_sl = float(state.get("stop_loss_price", 0.0) or 0.0)
        tp_price = custom_tp if custom_tp > 0 else avg_price * (1.0 + tp_rate)
        sl_price = custom_sl if custom_sl > 0 else avg_price * (1.0 + sl_rate)
        tp_pct = (tp_price - avg_price) / avg_price * 100.0
        sl_pct = (sl_price - avg_price) / avg_price * 100.0
        items.append(f"🔴 익절: **{_fmt_mkt_price(tp_price, is_kr)}** ({tp_pct:+.1f}%)")
        items.append(f"🛑 손절: **{_fmt_mkt_price(sl_price, is_kr)}** ({sl_pct:+.1f}%)")

    elif algo_id in {"kr_fujimoto_126", "us_fujimoto_126"}:
        sl_rate = float(p.get("stop_loss_rate", -0.05))
        sl_price = avg_price * (1.0 + sl_rate)
        entry_stage = int(state.get("entry_stage", 0) or 0)
        exit_stage = int(state.get("exit_stage", 0) or 0)

        cloud_top = cloud_bottom = None
        if cached_df is not None and not cached_df.empty:
            ind = calculate_technical_indicators(cached_df, min_bars=5)
            if ind is not None and not ind.empty:
                ct = float(ind["cloud_top"].iloc[-1])
                cb = float(ind["cloud_bottom"].iloc[-1])
                if ct > 0:
                    cloud_top = ct
                if cb > 0:
                    cloud_bottom = cb

        if entry_stage == 0:
            items.append("🟢 추가매수: 1단계 RSI≤30 반등 (1/9)")
        elif entry_stage == 1:
            items.append("🟢 추가매수: 2단계 MACD 골든크로스 (2/9)")
        elif entry_stage == 2:
            if cloud_top is not None:
                items.append(f"🟢 추가매수: 3단계 구름상단 **{_fmt_mkt_price(cloud_top, is_kr)}** 돌파 (6/9)")
            else:
                items.append("🟢 추가매수: 3단계 구름상단 돌파 (6/9)")
        else:
            items.append("🟢 매수완료 (3/3)")

        if exit_stage == 0:
            sell_txt = "1차 RSI≥70 이탈(1/9)"
        elif exit_stage == 1:
            sell_txt = "2차 MACD 데드크로스(2/9)"
        else:
            sell_txt = "3차 전량청산"
        if cloud_bottom is not None:
            items.append(f"🔴 매도: {sell_txt} · 구름하단 **{_fmt_mkt_price(cloud_bottom, is_kr)}**")
        else:
            items.append(f"🔴 매도: {sell_txt} · 구름하단 이탈")
        items.append(f"🛑 손절: **{_fmt_mkt_price(sl_price, is_kr)}** ({sl_rate*100:.1f}%)")

    elif algo_id == "us_infinite_buying":
        division = int(state.get("division_count", p.get("division_count", 40)))
        total_budget = float(state.get("total_budget_usd", p.get("total_budget_usd", 10000.0)))
        target_roi = float(state.get("target_roi", p.get("target_roi", 0.10)))
        exhaust_action = str(state.get("exhaust_action", p.get("exhaust_action", "AUTO"))).upper()
        exhaust_roi = float(state.get("exhaust_target_roi", p.get("exhaust_target_roi", 0.05)))
        max_loc_prem = float(state.get("max_loc_premium_ratio", p.get("max_loc_premium_ratio", 0.15)))
        one_round = total_budget / max(1, division)
        if "current_round" in state:
            curr_round = float(state["current_round"])
        else:
            curr_round = round((avg_price * lot.qty) / one_round, 2) if one_round > 0 else 0.0

        is_exhausted = curr_round >= division
        eff_roi = exhaust_roi if (is_exhausted and exhaust_action == "LOWER_TARGET") else target_roi
        sell_price = round(avg_price * (1.0 + eff_roi), 2)
        items.append(f"🔴 익절(지정가): **${sell_price:,.2f}** (+{eff_roi*100:.1f}%)")

        if is_exhausted:
            items.append(f"⏸️ 회차 소진 (T={curr_round:.1f}/{division})")
        else:
            half_div = division / 2.0
            star_pct = 10.0 - (curr_round * (10.0 / half_div))
            ref_curr = current_price if (current_price and current_price > 0) else avg_price
            cap = round(ref_curr * (1.0 + max_loc_prem), 2)
            if curr_round < half_div:
                loc1 = round(min(avg_price, cap), 2)
                loc2 = round(min(max(avg_price * (1.0 + star_pct / 100.0), avg_price + 0.01), cap), 2)
                items.append(f"🟢 추가매수 LOC(T={curr_round:.1f}): 평단 **${loc1:,.2f}** · 큰수(☆{star_pct:+.1f}%) **${loc2:,.2f}**")
            else:
                star_loc = round(min(avg_price * (1.0 + star_pct / 100.0), cap), 2)
                items.append(f"🟢 추가매수 LOC(T={curr_round:.1f}): 별지점(☆{star_pct:+.1f}%) **${star_loc:,.2f}**")

    else:
        # manual or unregistered algorithm -> global hard stop-loss only
        hard_sl_price = avg_price * (1.0 + float(global_hard_stop_loss_pct))
        items.append(f"🛑 공통 강제손절: **{_fmt_mkt_price(hard_sl_price, is_kr)}** ({global_hard_stop_loss_pct*100:.1f}%)")

    return items


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
    guidance_items: Optional[List[str]] = None,
) -> None:
    eval_price = current_price if (current_price and current_price > 0) else metrics.avg_price
    unrealized_pnl = (eval_price - metrics.avg_price) * metrics.remaining_qty
    unrealized_pct = ((eval_price - metrics.avg_price) / metrics.avg_price * 100.0) if metrics.avg_price > 0 else 0.0

    with st.container(border=True):
        top_l, top_r = st.columns([4, 1])
        algo_badge = "⚠️ 미할당(manual)" if lot.algo_id == "manual" else f"`{lot.algo_id}` v{lot.algo_version}"
        symbol_title = _format_symbol_title(lot.symbol, lot.market, symbol_name)
        top_l.markdown(f"**{symbol_title}** ({lot.market}) · {algo_badge} · 보유 {metrics.holding_days}일")
        if lot.algo_id == "manual" and on_map_click is not None:
            if top_r.button("알고리즘 연결", key=f"map_{lot.lot_id}", use_container_width=True):
                on_map_click(lot)

        is_kr = lot.market.upper() == "KR"
        price_fmt = "{:,.0f}" if is_kr else "{:,.2f}"
        pnl_fmt = "{:+,.0f}" if is_kr else "{:+,.2f}"
        has_live = current_price is not None and current_price > 0
        pnl_color = "#d6334c" if unrealized_pnl > 0 else ("#2f6fdb" if unrealized_pnl < 0 else "#6b7280")
        curr_txt = price_fmt.format(eval_price) if has_live else "-"
        pnl_txt = f"{pnl_fmt.format(unrealized_pnl)} ({unrealized_pct:+.2f}%)" if has_live else "-"

        st.markdown(
            f"""
            <div style="display:flex;flex-wrap:wrap;gap:14px;align-items:baseline;font-size:0.86rem;line-height:1.4;margin:2px 0 4px 0;">
                <span><span style="color:#6b7280;">잔량</span> <b>{metrics.remaining_qty:,}주</b></span>
                <span><span style="color:#6b7280;">평단가</span> <b>{price_fmt.format(metrics.avg_price)}</b></span>
                <span><span style="color:#6b7280;">현재가</span> <b>{curr_txt}</b></span>
                <span><span style="color:#6b7280;">평가손익</span> <b style="color:{pnl_color};">{pnl_txt}</b></span>
                <span><span style="color:#6b7280;">실현손익</span> <b>{pnl_fmt.format(metrics.realized_pnl)}</b></span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if guidance_items:
            st.caption("  |  ".join(guidance_items))


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
