"""
Top header with permanent REAL / VTS mode color badge & mobile-responsive CSS (R3.4, R3.5).
"""
from __future__ import annotations

import streamlit as st

from core.context import UserContext


def render_app_header(ctx: UserContext, is_offline_demo: bool = False) -> None:
    st.markdown(
        """
        <style>
        .mode-banner {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            padding: 10px 14px;
            border-radius: 10px;
            margin-bottom: 14px;
            font-size: 0.92rem;
        }
        .mode-real {
            background-color: #fde8e8;
            border: 1px solid #f87171;
            color: #991b1b;
        }
        .mode-mock {
            background-color: #dcfce7;
            border: 1px solid #4ade80;
            color: #166534;
        }
        .lot-card {
            border: 1px solid rgba(128, 128, 128, 0.25);
            border-radius: 10px;
            padding: 12px 14px;
            margin-bottom: 10px;
        }
        @media (max-width: 768px) {
            .block-container { padding-left: 0.75rem !important; padding-right: 0.75rem !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    css_class = "mode-mock" if ctx.is_mock else "mode-real"
    badge_icon = "🟢 모의투자 (VTS)" if ctx.is_mock else "🔴 실전투자 (REAL)"
    demo_tag = " · ⚙️ 로컬/데모 백엔드" if is_offline_demo else ""

    st.markdown(
        f"""
        <div class="mode-banner {css_class}">
            <strong>{badge_icon}</strong>
            <span>사용자: <code>{ctx.user_id}</code>{demo_tag}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
