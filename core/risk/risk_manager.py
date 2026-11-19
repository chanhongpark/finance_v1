"""
Per-user RiskManager (R5.2, R7).

Enforces:
- Daily maximum buy amount (KRW / USD)
- Max weight per symbol
- Max invested budget per algorithm
- Max concurrent distinct symbols
- Daily circuit breaker
- Global hard stop-loss (`risk_override`) across ALL open lots regardless of algorithm
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from core.algorithms.base import Condition, Market, PriceType, Signal, SignalAction
from core.context import UserContext, UserSettings
from core.ledger.models import Lot, LotStatus


@dataclass(frozen=True)
class RiskCheckResult:
    allowed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, ctx: UserContext, settings: Optional[UserSettings] = None) -> None:
        self.ctx = ctx
        self.settings = settings or ctx.settings

    def check_buy_signal(
        self,
        signal: Signal,
        open_lots: Sequence[Lot],
        today_bought_amount: float = 0.0,
        total_portfolio_value: float = 0.0,
        daily_portfolio_pnl_pct: float = 0.0,
    ) -> RiskCheckResult:
        """Validate a BUY signal against all per-user risk limits (R7)."""
        if signal.action != SignalAction.BUY:
            return RiskCheckResult(True)

        # 1. Circuit breaker
        if daily_portfolio_pnl_pct <= self.settings.circuit_breaker_daily_loss_pct:
            return RiskCheckResult(
                False,
                f"서킷브레이커 발동: 일일 손실률 {daily_portfolio_pnl_pct*100:.1f}% <= {self.settings.circuit_breaker_daily_loss_pct*100:.1f}%",
            )

        order_amount = float(signal.qty or 0) * float(signal.price or 0.0)
        is_us = signal.market == Market.US

        # 2. Daily max buy amount
        max_daily = self.settings.max_daily_buy_amount_usd if is_us else self.settings.max_daily_buy_amount_krw
        if (today_bought_amount + order_amount) > max_daily:
            return RiskCheckResult(
                False,
                f"일일 최대 매수금액 초과 ({today_bought_amount + order_amount:,.0f} > {max_daily:,.0f})",
            )

        active_lots = [l for l in open_lots if l.status == LotStatus.OPEN and l.qty > 0]

        # 3. Max concurrent symbols
        held_symbols = {l.symbol for l in active_lots}
        if signal.symbol not in held_symbols and len(held_symbols) >= self.settings.max_concurrent_symbols:
            return RiskCheckResult(
                False,
                f"최대 동시 보유 종목 수 초과 ({len(held_symbols)} >= {self.settings.max_concurrent_symbols})",
            )

        # 4. Max budget per algorithm
        algo_invested = sum(l.qty * l.avg_price for l in active_lots if l.algo_id == signal.algo_id and l.market == signal.market.value)
        max_algo = self.settings.max_algo_budget_usd if is_us else self.settings.max_algo_budget_krw
        if (algo_invested + order_amount) > max_algo:
            return RiskCheckResult(
                False,
                f"알고리즘({signal.algo_id}) 최대 투입금 초과 ({algo_invested + order_amount:,.0f} > {max_algo:,.0f})",
            )

        # 5. Max symbol weight
        if total_portfolio_value > 0:
            sym_invested = sum(l.qty * l.avg_price for l in active_lots if l.symbol == signal.symbol)
            projected_weight = (sym_invested + order_amount) / total_portfolio_value
            if projected_weight > self.settings.max_symbol_weight_pct:
                return RiskCheckResult(
                    False,
                    f"종목당 최대 비중 초과 ({projected_weight*100:.1f}% > {self.settings.max_symbol_weight_pct*100:.1f}%)",
                )

        return RiskCheckResult(True)

    def check_global_stop_loss_override(
        self,
        lot: Lot,
        current_price: float,
    ) -> Optional[Signal]:
        """
        Common hard stop-loss applied to ALL lots (including manual lots).
        Marked explicitly with `metadata={"risk_override": True}` (R5.2).
        """
        if lot.status != LotStatus.OPEN or lot.qty <= 0 or lot.avg_price <= 0 or current_price <= 0:
            return None

        pnl_rate = (current_price - lot.avg_price) / lot.avg_price
        if pnl_rate <= self.settings.global_hard_stop_loss_pct:
            market = Market.US if lot.market.upper() == "US" else Market.KR
            return Signal(
                action=SignalAction.SELL,
                algo_id=lot.algo_id,
                algo_version="risk_override",
                symbol=lot.symbol,
                market=market,
                price_type=PriceType.MARKET if market == Market.KR else PriceType.LIMIT,
                price=round(current_price, 2 if market == Market.US else 0),
                qty=lot.qty,
                qty_ratio=1.0,
                conditions=[
                    Condition(
                        name="공통 강제 손절(risk_override)",
                        value=round(pnl_rate * 100.0, 2),
                        threshold=round(self.settings.global_hard_stop_loss_pct * 100.0, 2),
                        op="<=",
                        passed=True,
                        desc="알고리즘 판단보다 우선하는 계좌 공통 최대 손절 규칙",
                    )
                ],
                score=100.0,
                reason_text=f"[risk_override] 공통 강제 손절 도달 ({pnl_rate*100:+.1f}%)",
                lot_id=lot.lot_id,
                metadata={"risk_override": True},
            )
        return None
