"""Page 3: Trade History — Lots, Append-only Fills, Signals, and Audit Log (R3.1)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.auth import AppServices


def render_history(services: AppServices) -> None:
    st.title("📒 거래 기록 (원장)")

    t_lots, t_fills, t_signals, t_audit = st.tabs(["운영 건 (Lots)", "Fills (체결 원장)", "Signals (시그널 근거)", "Audit (감사 로그)"])

    with t_lots:
        lots = services.sheet.load_lots()
        if lots:
            st.dataframe(pd.DataFrame([l.to_sheet_row() for l in lots]), use_container_width=True, hide_index=True)
        else:
            st.info("기록된 운영 건이 없습니다.")

    with t_fills:
        fills = services.sheet.load_fills()
        if fills:
            st.dataframe(pd.DataFrame([f.to_sheet_row() for f in fills]), use_container_width=True, hide_index=True)
        else:
            st.info("기록된 체결(Fill)이 없습니다.")

    with t_signals:
        signals = services.sheet.load_signals()
        if signals:
            st.dataframe(pd.DataFrame(signals), use_container_width=True, hide_index=True)
        else:
            st.info("기록된 시그널 이력이 없습니다.")

    with t_audit:
        audits = services.sheet.load_audits()
        if audits:
            st.dataframe(pd.DataFrame(audits), use_container_width=True, hide_index=True)
        else:
            st.info("감사 로그가 없습니다.")
