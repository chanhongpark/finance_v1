"""
UserContext-scoped Telegram notifier (R9).
Includes algorithm name and condition summary in every trade/proposal alert.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import requests

from core.algorithms.base import Signal
from core.context import UserContext

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends notifications exclusively to `ctx.credentials.telegram_chat_id`."""

    def __init__(self, ctx: UserContext, dry_run: bool = False) -> None:
        self.ctx = ctx
        self.dry_run = dry_run
        self.sent_messages: List[str] = []

    @property
    def is_configured(self) -> bool:
        return bool(self.ctx.credentials.telegram_bot_token and self.ctx.credentials.telegram_chat_id)

    def send_text(self, text: str) -> bool:
        prefix = f"[{self.ctx.user_id} | {'모의' if self.ctx.is_mock else '실전'}]\n"
        full_msg = prefix + text
        self.sent_messages.append(full_msg)

        if self.dry_run or not self.is_configured:
            logger.info("Telegram(dry_run=%s): %s", self.dry_run, full_msg)
            return True

        url = f"https://api.telegram.org/bot{self.ctx.credentials.telegram_bot_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": self.ctx.credentials.telegram_chat_id, "text": full_msg},
                timeout=8.0,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("Telegram send failed for '%s': %s", self.ctx.user_id, exc)
            return False

    def notify_signal(self, signal: Signal, status_label: str = "제안") -> bool:
        cond_summary = "\n".join(f"• {c.format_badge()}" for c in signal.conditions)
        msg = (
            f"📌 [{status_label}] {signal.action.value} {signal.symbol} ({signal.market.value})\n"
            f"알고리즘: {signal.algo_id} (v{signal.algo_version})\n"
            f"가격/수량: {signal.price:,.2f} ({signal.price_type.value}) × {signal.qty or 0}주\n"
            f"사유: {signal.reason_text}\n"
            f"{cond_summary}"
        )
        return self.send_text(msg)

    def notify_error(self, context_label: str, error_msg: str) -> bool:
        return self.send_text(f"⚠️ [오류] {context_label}\n{error_msg}")
