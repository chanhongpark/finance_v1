"""
Order confirmation modal before placing any real/mock broker order (R3.3).

- Shows a price chart (candles + MA20/MA60 + volume) with the order price and
  algorithm reference levels (target / stop-loss / avg price) as evidence.
- BUY: quantity and order amount can be edited manually (linked to each other).
- Order type selectable per market (KR: 지정가/시장가/조건부지정가/최유리/최우선, US: 지정가/LOC/LOO/MOC/MOO).
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from core.algorithms.base import PRICE_TYPE_HELP, SUPPORTED_PRICE_TYPES, PriceType, Signal, SignalAction
from core.execution.order_service import OrderService
from core.indicators import calculate_technical_indicators
from ui.auth import AppServices

CHART_LOOKBACK_DAYS = 200
CHART_BARS = 120
FUJIMOTO_ALGO_IDS = {"kr_fujimoto_126", "us_fujimoto_126"}


def kr_tick_size(price: float) -> int:
    """KRX tick size table (KOSPI/KOSDAQ unified since 2023)."""
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def round_to_tick(price: float, market: str) -> float:
    if market.upper() != "KR":
        return round(price, 2)
    tick = kr_tick_size(price)
    return float(round(price / tick) * tick)


def _signal_key(signal: Signal) -> str:
    raw = f"{signal.action.value}|{signal.algo_id}|{signal.symbol}|{signal.ts}|{signal.lot_id}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


def _build_price_chart(
    df: pd.DataFrame,
    signal: Signal,
    order_price: Optional[float],
    avg_price: Optional[float],
) -> go.Figure:
    show_fujimoto = signal.algo_id in FUJIMOTO_ALGO_IDS
    ind_df = calculate_technical_indicators(df, min_bars=5) if show_fujimoto else None
    if ind_df is not None and not ind_df.empty:
        data = ind_df.copy()
        data["date"] = pd.to_datetime(data["date"].astype(str).str[:10], errors="coerce")
        data = data.dropna(subset=["date"]).sort_values("date")
    else:
        show_fujimoto = False
        data = df.copy()
        data["date"] = pd.to_datetime(data["date"].astype(str).str[:10], errors="coerce")
        data = data.dropna(subset=["date"]).sort_values("date")
        data["ma20"] = data["close"].rolling(20).mean()
        data["ma60"] = data["close"].rolling(60).mean()

    data = data.tail(CHART_BARS)

    if show_fujimoto:
        fig = make_subplots(
            rows=3,
            cols=1,
            shared_xaxes=True,
            row_heights=[0.56, 0.16, 0.28],
            vertical_spacing=0.03,
        )
    else:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03)

    if show_fujimoto:
        # Shaded Ichimoku Cloud behind candlesticks
        fig.add_trace(
            go.Scatter(
                x=data["date"],
                y=data["cloud_top"],
                name="구름상단",
                line=dict(color="rgba(59, 130, 246, 0.55)", width=1.0, dash="dot"),
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=data["date"],
                y=data["cloud_bottom"],
                name="구름하단",
                line=dict(color="rgba(239, 68, 68, 0.55)", width=1.0, dash="dot"),
                fill="tonexty",
                fillcolor="rgba(148, 163, 184, 0.22)",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=data["date"],
                y=data["tenkan_sen"],
                name="전환선",
                line=dict(color="#14b8a6", width=1.1, dash="dash"),
            ),
            row=1,
            col=1,
        )

    fig.add_trace(
        go.Candlestick(
            x=data["date"],
            open=data["open"],
            high=data["high"],
            low=data["low"],
            close=data["close"],
            name="일봉",
            # Korean convention: up = red, down = blue
            increasing_line_color="#d6334c",
            increasing_fillcolor="#d6334c",
            decreasing_line_color="#2f6fdb",
            decreasing_fillcolor="#2f6fdb",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(go.Scatter(x=data["date"], y=data["ma20"], name="MA20", line=dict(color="#e8a33d", width=1.4)), row=1, col=1)
    fig.add_trace(go.Scatter(x=data["date"], y=data["ma60"], name="MA60", line=dict(color="#8a63d2", width=1.4)), row=1, col=1)

    vol_colors = ["#d6334c" if c >= o else "#2f6fdb" for o, c in zip(data["open"], data["close"])]
    fig.add_trace(go.Bar(x=data["date"], y=data["volume"], name="거래량", marker_color=vol_colors, opacity=0.6, showlegend=False), row=2, col=1)

    if show_fujimoto:
        hist_colors = ["#d6334c" if v >= 0 else "#2f6fdb" for v in data["macd_hist"]]
        fig.add_trace(
            go.Bar(x=data["date"], y=data["macd_hist"], name="MACD Hist", marker_color=hist_colors, opacity=0.65),
            row=3,
            col=1,
        )
        fig.add_trace(
            go.Scatter(x=data["date"], y=data["macd_line"], name="MACD", line=dict(color="#e8a33d", width=1.5)),
            row=3,
            col=1,
        )
        fig.add_trace(
            go.Scatter(x=data["date"], y=data["macd_signal"], name="Signal", line=dict(color="#8a63d2", width=1.4)),
            row=3,
            col=1,
        )
        fig.add_hline(y=0.0, line=dict(color="#9ca3af", width=1.0, dash="dot"), row=3, col=1)
        fig.update_yaxes(title_text="MACD", row=3, col=1)

    def _hline(y: Optional[float], label: str, color: str, dash: str) -> None:
        if y is None or not (isinstance(y, (int, float)) and y > 0 and math.isfinite(y)):
            return
        fig.add_hline(
            y=float(y),
            line=dict(color=color, width=1.3, dash=dash),
            annotation_text=f"{label} {y:,.2f}" if signal.market.value == "US" else f"{label} {y:,.0f}",
            annotation_position="top left",
            annotation_font_color=color,
            row=1,
            col=1,
        )

    action_color = "#1a9e5c" if signal.action == SignalAction.BUY else "#d6334c"
    _hline(order_price, "주문가", action_color, "solid")
    _hline(avg_price, "평단가", "#6b7280", "dot")
    meta = signal.metadata or {}
    _hline(meta.get("target_price"), "목표가", "#1a9e5c", "dash")
    _hline(meta.get("stop_loss_price"), "손절가", "#d6334c", "dash")

    fig.update_layout(
        height=560 if show_fujimoto else 420,
        margin=dict(l=8, r=8, t=24, b=8),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1),
        hovermode="x unified",
    )
    # Hide weekends to avoid gaps
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    return fig


def _render_evidence_chart(services: AppServices, signal: Signal, order_price: Optional[float]) -> None:
    try:
        df = services.data_provider.get_daily_ohlcv(
            signal.symbol,
            market=signal.market.value,
            lookback_days=CHART_LOOKBACK_DAYS,
            include_live_quote=True,
        )
    except Exception as exc:  # chart is evidence only; never block the order flow
        st.caption(f"차트 데이터를 불러오지 못했습니다: {exc}")
        return
    if df is None or df.empty or not {"open", "high", "low", "close"}.issubset(df.columns):
        st.caption("차트 데이터가 없습니다.")
        return

    avg_price = None
    if signal.lot_id:
        lot = next((l for l in services.sheet.load_lots() if l.lot_id == signal.lot_id), None)
        if lot is not None:
            avg_price = float(lot.avg_price or 0) or None

    st.plotly_chart(_build_price_chart(df, signal, order_price, avg_price), config={"displayModeBar": False})


@st.dialog("주문 실행 확인", width="large")
def open_order_confirmation_dialog(services: AppServices, signal: Signal) -> None:
    is_buy = signal.action == SignalAction.BUY
    market = signal.market.value
    is_kr = market == "KR"
    k = _signal_key(signal)
    price_fmt = "{:,.0f}" if is_kr else "{:,.2f}"

    mode_text = "🟢 모의투자" if services.ctx.is_mock else "🔴 실전투자 (실제 자금 체결)"
    symbol_display = services.data_provider.format_symbol_display(signal.symbol, market)
    action_text = "🟢 매수" if is_buy else "🔴 매도"
    st.markdown(f"**{action_text} · {symbol_display}** ({market}) · 계좌 모드: {mode_text}")
    st.caption(f"담당 알고리즘 `{signal.algo_id}` (v{signal.algo_version}) · 판단 사유: {signal.reason_text}")

    # --- Order type & price ---
    supported = SUPPORTED_PRICE_TYPES[(market, signal.action.value)]
    default_type = signal.price_type if signal.price_type in supported else supported[0]
    base_price = float(signal.price or 0.0)
    price_key, qty_key, amt_key = f"price_{k}", f"qty_{k}", f"amt_{k}"

    def _ref_price() -> float:
        """Current unit price from widget state (callbacks run before the script, so never use closures)."""
        ptype = st.session_state.get(f"ptype_{k}", default_type)
        if ptype.requires_price:
            p = round_to_tick(float(st.session_state.get(price_key, base_price) or 0.0), market)
            return p if p > 0 else base_price
        return base_price

    def _on_qty_change() -> None:
        if qty_key not in st.session_state:
            return
        st.session_state[amt_key] = float(st.session_state[qty_key] * _ref_price())

    def _on_amt_change() -> None:
        p = _ref_price()
        st.session_state[qty_key] = int(st.session_state[amt_key] // p) if p > 0 else 0

    c_type, c_price = st.columns(2)
    price_type: PriceType = c_type.selectbox(
        "주문 유형",
        options=supported,
        index=supported.index(default_type),
        format_func=lambda p: p.value,
        key=f"ptype_{k}",
        on_change=_on_qty_change if is_buy else None,
        help="\n".join(f"- **{p.value}**: {PRICE_TYPE_HELP[p]}" for p in supported),
    )

    if price_type.requires_price:
        unit_price = c_price.number_input(
            "주문 단가",
            min_value=0.0,
            value=base_price,
            step=float(kr_tick_size(base_price)) if is_kr else 0.01,
            format="%.0f" if is_kr else "%.2f",
            key=price_key,
            on_change=_on_qty_change if is_buy else None,
        )
        sent_price = round_to_tick(unit_price, market)
        if is_kr and sent_price != unit_price:
            c_price.caption(f"호가단위 적용 → {sent_price:,.0f}원")
    else:
        sent_price = base_price
        c_price.metric("참고 가격 (체결가는 시장 결정)", price_fmt.format(base_price))
    st.caption(PRICE_TYPE_HELP[price_type])

    # --- Quantity / amount ---
    ref_price = sent_price if sent_price > 0 else base_price
    if is_buy:
        if qty_key not in st.session_state:
            st.session_state[qty_key] = int(signal.qty or 0)
            st.session_state[amt_key] = float(st.session_state[qty_key] * ref_price)

        c_qty, c_amt = st.columns(2)
        c_qty.number_input("주문 수량 (주)", min_value=0, step=1, key=qty_key, on_change=_on_qty_change)
        c_amt.number_input(
            f"주문 금액 ({'원' if is_kr else 'USD'})",
            min_value=0.0,
            step=10_000.0 if is_kr else 10.0,
            format="%.0f" if is_kr else "%.2f",
            key=amt_key,
            on_change=_on_amt_change,
            help="금액을 입력하면 단가 기준으로 매수 가능한 최대 수량이 자동 계산됩니다.",
        )
        qty = int(st.session_state[qty_key])
        if int(signal.qty or 0) != qty:
            st.caption(f"알고리즘 제안 수량: {int(signal.qty or 0):,}주 → 변경: {qty:,}주")
    else:
        qty = int(signal.qty or 0)
        st.metric("주문 수량", f"{qty:,}주")

    est_amount = qty * ref_price
    st.markdown(f"**예상 주문 금액:** {price_fmt.format(est_amount)} {'원' if is_kr else 'USD'}")

    # --- Evidence chart ---
    st.markdown("##### 📈 주문 근거 차트")
    _render_evidence_chart(services, signal, sent_price if price_type.requires_price else None)
    if signal.conditions:
        with st.expander("판단 조건 상세 ⓘ", expanded=False):
            for c in signal.conditions:
                st.write(f"- {c.format_badge()} — {c.desc}")

    st.divider()
    c_cancel, c_ok = st.columns(2)
    if c_cancel.button("취소", width="stretch"):
        st.rerun()

    if c_ok.button("확인 후 주문 실행", type="primary", width="stretch", disabled=qty <= 0):
        final_signal = dataclasses.replace(
            signal,
            qty=qty,
            price=float(sent_price),
            price_type=price_type,
        )
        svc = OrderService(services.ctx, services.sheet, services.kis, notifier=services.notifier)
        outcome = svc.execute_signal(final_signal, user_confirmed=True)
        if outcome.executed:
            st.toast(f"✅ {outcome.message} (주문번호: {outcome.order_no})")
        else:
            st.toast(f"⚠️ [{outcome.decision}] {outcome.message}")
        for key in (qty_key, amt_key):
            st.session_state.pop(key, None)
        st.rerun()
