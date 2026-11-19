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
            lot_rows = []
            for l in lots:
                row = l.to_sheet_row()
                sym_name = services.data_provider.get_symbol_name(l.symbol, l.market)
                ordered = {"lot_id": row["lot_id"], "symbol": row["symbol"], "종목명": sym_name}
                ordered.update({k: v for k, v in row.items() if k not in {"lot_id", "symbol"}})
                lot_rows.append(ordered)
            st.dataframe(pd.DataFrame(lot_rows), use_container_width=True, hide_index=True)
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
            sig_df = pd.DataFrame(signals)
            if "symbol" in sig_df.columns:
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
            st.dataframe(sig_df, use_container_width=True, hide_index=True)
        else:
            st.info("기록된 시그널 이력이 없습니다.")

    with t_audit:
        audits = services.sheet.load_audits()
        if audits:
            st.dataframe(pd.DataFrame(audits), use_container_width=True, hide_index=True)
        else:
            st.info("감사 로그가 없습니다.")
