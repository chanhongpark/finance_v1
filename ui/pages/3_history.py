"""Page 3: Trade History — Lots, Append-only Fills, Signals, and Audit Log (R3.1)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.auth import AppServices


def render_history(services: AppServices) -> None:
    st.title("📒 거래 기록 (원장)")

    t_lots, t_fills, t_signals, t_audit = st.tabs(["운영 건 (Lots)", "Fills (체결 원장)", "Signals (시그널 근거)", "Audit (감사 로그)"])

    with t_lots:
        st.info(
            "**운영 건 (Lots)** — 알고리즘이 관리하는 포지션 단위 원장입니다. "
            "한 번의 진입(분할매수 포함)부터 청산까지를 하나의 건(lot)으로 묶어 "
            "종목·담당 알고리즘·평단가·잔량·상태(OPEN/CLOSED)·알고리즘 진행 단계를 보여줍니다. "
            "같은 종목이라도 알고리즘이 다르면 별도의 건으로 관리됩니다.",
            icon=":material/inventory_2:",
        )
        lots = services.sheet.load_lots()
        if lots:
            lot_rows = []
            for l in lots:
                row = l.to_sheet_row()
                sym_name = services.data_provider.get_symbol_name(l.symbol, l.market)
                ordered = {"lot_id": row["lot_id"], "symbol": row["symbol"], "종목명": sym_name}
                ordered.update({k: v for k, v in row.items() if k not in {"lot_id", "symbol"}})
                lot_rows.append(ordered)
            st.dataframe(pd.DataFrame(lot_rows), hide_index=True)
        else:
            st.info("기록된 운영 건이 없습니다.")

    with t_fills:
        st.info(
            "**Fills (체결 원장)** — 증권사(KIS)에서 실제로 체결이 확인된 매수/매도 내역입니다. "
            "수정·삭제 없이 추가만 되는(append-only) 기록이며, 각 체결은 소속 운영 건(lot_id)과 주문번호로 연결됩니다. "
            "운영 건의 평단가·잔량·실현손익은 이 체결 내역으로부터 재계산됩니다.",
            icon=":material/receipt_long:",
        )
        fills = services.sheet.load_fills()
        if fills:
            st.dataframe(pd.DataFrame([f.to_sheet_row() for f in fills]), hide_index=True)
        else:
            st.info("기록된 체결(Fill)이 없습니다.")

    with t_signals:
        st.info(
            "**Signals (시그널 근거)** — 알고리즘이 생성한 매수/매도 신호와 그 판단 근거(조건별 통과 여부·점수·사유)입니다. "
            "실제 주문 여부와 관계없이 기록되며, decision 컬럼으로 처리 결과를 확인할 수 있습니다 "
            "(EXECUTED: 체결, PROPOSED: 제안/승인대기, REJECTED_RISK: 리스크 한도로 거부, ORDER_FAILED: 주문 실패, UNFILLED: 미체결).",
            icon=":material/insights:",
        )
        signals = services.sheet.load_signals()
        if signals:
            sig_df = pd.DataFrame(signals)
            if "symbol" in sig_df.columns:
                def _normalize_sig_symbol(row: pd.Series) -> str:
                    sym = str(row.get("symbol", "")).strip()
                    algo_id = str(row.get("algo_id", "")).lower()
                    is_kr = not algo_id.startswith("us_") and sym.isdigit()
                    return sym.zfill(6) if (is_kr and len(sym) < 6) else sym

                sig_df["symbol"] = sig_df.apply(_normalize_sig_symbol, axis=1)

                def _resolve_sig_name(row: pd.Series) -> str:
                    sym = str(row.get("symbol", "")).strip()
                    algo_id = str(row.get("algo_id", "")).lower()
                    mkt = "US" if algo_id.startswith("us_") or (sym and not sym.isdigit()) else "KR"
                    return services.data_provider.get_symbol_name(sym, mkt)

                sig_df.insert(
                    sig_df.columns.get_loc("symbol") + 1,
                    "종목명",
                    sig_df.apply(_resolve_sig_name, axis=1),
                )
            st.dataframe(sig_df, hide_index=True)
        else:
            st.info("기록된 시그널 이력이 없습니다.")

    with t_audit:
        st.info(
            "**Audit (감사 로그)** — 주문 실행·리스크 거부·잔고 동기화·수동 등록/수정 등 "
            "원장에 영향을 준 모든 시스템/사용자 행위를 시간순으로 남긴 이력입니다. 문제 발생 시 추적용으로 사용합니다.",
            icon=":material/history:",
        )
        audits = services.sheet.load_audits()
        if audits:
            st.dataframe(pd.DataFrame(audits), hide_index=True)
        else:
            st.info("감사 로그가 없습니다.")
