"""
OrderService: bridges Signals, RiskManager, KIS Client, and Personal Sheet Ledger
(R5.1, R5.2, R5.3, R5.4, R5.5, R5.6, R6).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from core.algorithms.base import Signal, SignalAction
from core.context import ExecutionMode, UserContext
from core.ledger.calculator import allocate_sell_across_lots, apply_fills_to_lot
from core.ledger.models import (
    AuditRecord,
    Fill,
    FillSide,
    Lot,
    LotStatus,
    SignalRecord,
    generate_id,
    utc_now_iso,
)
from core.notifier.telegram import TelegramNotifier
from core.risk.risk_manager import RiskManager
from core.sheets.user_sheet_manager import UserSheetManager


@dataclass
class ExecutionOutcome:
    executed: bool
    decision: str               # EXECUTED | PROPOSED | REJECTED_RISK | ORDER_FAILED | UNFILLED
    message: str
    order_no: str = ""
    filled_qty: int = 0
    lot_ids: List[str] = None

    def __post_init__(self) -> None:
        if self.lot_ids is None:
            self.lot_ids = []


class OrderService:
    """
    Executes or records a Signal strictly within a single UserContext.
    """

    def __init__(
        self,
        ctx: UserContext,
        sheet_manager: UserSheetManager,
        kis_client: Any,
        risk_manager: Optional[RiskManager] = None,
        notifier: Optional[TelegramNotifier] = None,
    ) -> None:
        self.ctx = ctx
        self.sheet = sheet_manager
        self.kis = kis_client
        self.risk = risk_manager or RiskManager(ctx, sheet_manager.load_user_settings())
        self.notifier = notifier or TelegramNotifier(ctx, dry_run=not ctx.credentials.telegram_bot_token)

    def execute_signal(
        self,
        signal: Signal,
        user_confirmed: bool = False,
        today_bought_amount: float = 0.0,
        total_portfolio_value: float = 0.0,
        daily_portfolio_pnl_pct: float = 0.0,
    ) -> ExecutionOutcome:
        """
        Validate risk, enforce ExecutionMode, place order via KIS, confirm fills via inquire_ccnl,
        update append-only Fills & Lots in personal Sheet, and notify via Telegram.
        """
        settings = self.sheet.load_user_settings()
        self.risk.settings = settings
        open_lots = [l for l in self.sheet.load_lots() if l.status == LotStatus.OPEN and l.qty > 0]
        is_risk_override = bool(signal.metadata.get("risk_override"))

        # 1. Risk check on BUY
        if signal.action == SignalAction.BUY:
            risk_res = self.risk.check_buy_signal(
                signal=signal,
                open_lots=open_lots,
                today_bought_amount=today_bought_amount,
                total_portfolio_value=total_portfolio_value,
                daily_portfolio_pnl_pct=daily_portfolio_pnl_pct,
            )
            if not risk_res.allowed:
                self._record_signal(signal, "REJECTED_RISK")
                self.sheet.append_audit(
                    AuditRecord(utc_now_iso(), self.ctx.user_id, "ORDER_REJECTED_RISK", risk_res.reason)
                )
                return ExecutionOutcome(False, "REJECTED_RISK", risk_res.reason)

        # 2. ExecutionMode gate (R6)
        mode = settings.execution_mode
        if mode == ExecutionMode.PROPOSE_ONLY:
            self._record_signal(signal, "PROPOSED")
            self.notifier.notify_signal(signal, status_label="제안(수동)")
            return ExecutionOutcome(False, "PROPOSED", "제안만 모드: 실주문 미실행")

        if mode == ExecutionMode.SEMI_AUTO and not user_confirmed:
            self._record_signal(signal, "PROPOSED")
            self.notifier.notify_signal(signal, status_label="승인대기(반자동)")
            return ExecutionOutcome(False, "PROPOSED", "반자동 모드: 사용자 확인 대기")

        # 3. Place broker order
        qty = int(signal.qty or 0)
        if qty <= 0:
            return ExecutionOutcome(False, "ORDER_FAILED", "주문 수량이 0 이하입니다.")

        order_res = self.kis.place_order(
            symbol=signal.symbol,
            side=signal.action.value,
            qty=qty,
            price=signal.price,
            market=signal.market.value,
            price_type=signal.price_type.value,
        )
        if not order_res.get("success"):
            msg = str(order_res.get("message", "KIS 주문 실패"))
            self._record_signal(signal, "ORDER_FAILED")
            self.notifier.notify_error(f"{signal.action.value} {signal.symbol}", msg)
            return ExecutionOutcome(False, "ORDER_FAILED", msg)

        order_no = str(order_res.get("order_no", ""))

        # 4. Confirm filled quantity via inquire_ccnl (R5.5)
        filled_qty = qty
        filled_price = float(signal.price)
        if hasattr(self.kis, "inquire_ccnl"):
            ccnl_rows = self.kis.inquire_ccnl(order_no=order_no, market=signal.market.value)
            matched = [r for r in ccnl_rows if r.get("order_no") == order_no]
            if matched:
                filled_qty = int(matched[0].get("filled_qty", 0) or 0)
                if float(matched[0].get("avg_price", 0) or 0) > 0:
                    filled_price = float(matched[0]["avg_price"])

        if filled_qty <= 0:
            self._record_signal(signal, "UNFILLED")
            return ExecutionOutcome(False, "UNFILLED", f"미체결 (order_no={order_no})", order_no=order_no)

        # 5. Update append-only Fills and Lots in personal Sheet
        today_str = date.today().strftime("%Y-%m-%d")
        all_lots = self.sheet.load_lots(use_cache=False)
        all_fills = self.sheet.load_fills(use_cache=False)
        new_fills: List[Fill] = []
        touched_lots: List[Lot] = []

        if signal.action == SignalAction.BUY:
            target_lot = self._find_target_buy_lot(signal, all_lots)
            if target_lot is None:
                target_lot = Lot(
                    lot_id=generate_id("lot"),
                    symbol=signal.symbol,
                    market=signal.market.value,
                    algo_id=signal.algo_id,
                    algo_version=signal.algo_version,
                    buy_date=today_str,
                    qty=0,
                    avg_price=0.0,
                    status=LotStatus.OPEN,
                    params_snapshot=dict(signal.metadata.get("params_snapshot", {})),
                    algo_state=self._next_algo_state({}, signal),
                    entry_conditions=signal.conditions_as_dicts(),
                    note=signal.reason_text,
                )
            else:
                target_lot.algo_state = self._next_algo_state(target_lot.algo_state, signal)

            fill = Fill(
                fill_id=generate_id("fill"),
                lot_id=target_lot.lot_id,
                side=FillSide.BUY,
                date=today_str,
                qty=filled_qty,
                price=filled_price,
                order_no=order_no,
                note=signal.reason_text,
            )
            new_fills.append(fill)
            synced_lot = apply_fills_to_lot(target_lot, all_fills + new_fills)
            touched_lots.append(synced_lot)
        else:
            allocations = allocate_sell_across_lots(
                symbol=signal.symbol,
                sell_qty=filled_qty,
                open_lots=all_lots,
                preferred_algo_id=signal.algo_id,
                preferred_lot_id=signal.lot_id,
            )
            lot_by_id = {l.lot_id: l for l in all_lots}
            for alloc_lot_id, alloc_qty in allocations:
                lot_obj = lot_by_id[alloc_lot_id]
                if "next_exit_stage" in signal.metadata:
                    lot_obj.algo_state["exit_stage"] = int(signal.metadata["next_exit_stage"])
                fill = Fill(
                    fill_id=generate_id("fill"),
                    lot_id=alloc_lot_id,
                    side=FillSide.SELL,
                    date=today_str,
                    qty=alloc_qty,
                    price=filled_price,
                    order_no=order_no,
                    note="risk_override" if is_risk_override else signal.reason_text,
                )
                new_fills.append(fill)
                synced_lot = apply_fills_to_lot(lot_obj, all_fills + new_fills)
                touched_lots.append(synced_lot)

        self.sheet.append_fills(new_fills)
        if touched_lots:
            self.sheet.upsert_lots(touched_lots)

        decision_tag = "RISK_OVERRIDE" if is_risk_override else "EXECUTED"
        self._record_signal(signal, decision_tag)
        self.sheet.append_audit(
            AuditRecord(
                ts=utc_now_iso(),
                actor=self.ctx.user_id,
                action=f"ORDER_{decision_tag}",
                detail=(
                    f"order_no={order_no}, {signal.action.value} {signal.symbol} "
                    f"qty={filled_qty}@{filled_price}, algo={signal.algo_id}"
                ),
            )
        )
        self.notifier.notify_signal(signal, status_label="체결완료")
        return ExecutionOutcome(
            executed=True,
            decision=decision_tag,
            message=f"체결 완료 ({filled_qty}주 @ {filled_price:,.2f})",
            order_no=order_no,
            filled_qty=filled_qty,
            lot_ids=[l.lot_id for l in touched_lots],
        )

    @staticmethod
    def _find_target_buy_lot(signal: Signal, all_lots: List[Lot]) -> Optional[Lot]:
        """
        For stateful split-buy algorithms (us_infinite_buying, kr_fujimoto_126) or explicit lot_id,
        append new BUY Fill to the existing OPEN lot of the same (symbol, algo_id) (R5.3).
        """
        if signal.lot_id:
            for lot in all_lots:
                if lot.lot_id == signal.lot_id and lot.status == LotStatus.OPEN:
                    return lot
        if signal.algo_id in {"us_infinite_buying", "kr_fujimoto_126"}:
            for lot in all_lots:
                if lot.symbol == signal.symbol and lot.algo_id == signal.algo_id and lot.status == LotStatus.OPEN:
                    return lot
        return None

    @staticmethod
    def _next_algo_state(current: Dict[str, Any], signal: Signal) -> Dict[str, Any]:
        updated = dict(current)
        if "next_entry_stage" in signal.metadata:
            updated["entry_stage"] = int(signal.metadata["next_entry_stage"])
        if "round_inc" in signal.metadata:
            curr_r = float(updated.get("current_round", 0.0) or 0.0)
            updated["current_round"] = round(curr_r + float(signal.metadata["round_inc"]), 2)
        if "target_price" in signal.metadata:
            updated["target_price"] = signal.metadata["target_price"]
        if "stop_loss_price" in signal.metadata:
            updated["stop_loss_price"] = signal.metadata["stop_loss_price"]
        return updated

    def _record_signal(self, signal: Signal, decision: str) -> None:
        self.sheet.append_signals(
            [
                SignalRecord(
                    ts=signal.ts,
                    symbol=signal.symbol,
                    algo_id=signal.algo_id,
                    algo_version=signal.algo_version,
                    action=signal.action.value,
                    conditions=signal.conditions_as_dicts(),
                    decision=decision,
                    score=signal.score,
                    reason_text=signal.reason_text,
                )
            ]
        )
