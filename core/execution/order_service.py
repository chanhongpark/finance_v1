"""
OrderService: bridges Signals, RiskManager, KIS Client, and Personal Sheet Ledger
(R5.1, R5.2, R5.3, R5.4, R5.5, R5.6, R6).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from core.algorithms.base import Market, PriceType, Signal, SignalAction
from core.context import ExecutionMode, UserContext
from core.kis.client import MockKISClient
from core.ledger.calculator import allocate_sell_across_lots, apply_fills_to_lot
from core.ledger.models import (
    AuditRecord,
    Fill,
    FillSide,
    Lot,
    LotStatus,
    PendingOrder,
    PendingOrderStatus,
    SignalRecord,
    generate_id,
    parse_date_str,
    utc_now_iso,
)
from core.notifier.telegram import TelegramNotifier
from core.risk.risk_manager import RiskManager
from core.sheets.user_sheet_manager import UserSheetManager

# Default estimated brokerage commission & securities transaction tax rates
DEFAULT_KR_FEE_RATE = 0.00015      # 0.015% KIS online KR commission
DEFAULT_KR_SELL_TAX_RATE = 0.0018  # 0.18% KR securities transaction tax (sell only)
DEFAULT_US_FEE_RATE = 0.0025       # 0.25% KIS standard US commission
DEFAULT_US_SELL_TAX_RATE = 0.0
PENDING_ORDER_EXPIRE_DAYS = 5


def _order_no_matches(row_order_no: Any, target_order_no: str) -> bool:
    a = str(row_order_no or "").strip()
    b = str(target_order_no or "").strip()
    if not a or not b:
        return False
    return a == b or (a.lstrip("0") == b.lstrip("0") and a.lstrip("0") != "")


def _estimate_fee_and_tax(
    market: str,
    side: FillSide,
    qty: int,
    price: float,
    matched_row: Optional[Dict[str, Any]] = None,
) -> tuple[float, float]:
    gross = max(0, int(qty)) * float(price)
    if matched_row is not None:
        raw_fee = matched_row.get("fee")
        raw_tax = matched_row.get("tax")
        if raw_fee is not None or raw_tax is not None:
            return round(float(raw_fee or 0.0), 4), round(float(raw_tax or 0.0), 4)

    mkt = market.upper()
    if mkt == "KR":
        fee = round(gross * DEFAULT_KR_FEE_RATE, 2)
        tax = round(gross * DEFAULT_KR_SELL_TAX_RATE, 2) if side == FillSide.SELL else 0.0
        return fee, tax
    fee = round(gross * DEFAULT_US_FEE_RATE, 4)
    tax = round(gross * DEFAULT_US_SELL_TAX_RATE, 4) if side == FillSide.SELL else 0.0
    return fee, tax


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

        # 3. Block live broker orders when sheet backend is non-persistent (InMemorySheetBackend)
        if getattr(self.sheet, "is_in_memory", False) and not isinstance(self.kis, MockKISClient):
            msg = "영구 Google Sheet(GCP 서비스 계정)가 연결되지 않아 실/모의 KIS 주문을 차단했습니다."
            self._record_signal(signal, "BLOCKED_NO_SHEET")
            self.sheet.append_audit(
                AuditRecord(utc_now_iso(), self.ctx.user_id, "ORDER_BLOCKED_NO_SHEET", msg)
            )
            return ExecutionOutcome(False, "BLOCKED_NO_SHEET", msg)

        # 4. Place broker order
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

        order_no = str(order_res.get("order_no", "")).strip()

        # 5. Confirm filled quantity via inquire_ccnl (R5.5) — never assume filled when unmatched
        today_str = date.today().strftime("%Y-%m-%d")
        now_iso = utc_now_iso()
        matched_row: Optional[Dict[str, Any]] = None
        filled_price = float(signal.price)
        if hasattr(self.kis, "inquire_ccnl"):
            filled_qty = 0
            try:
                ccnl_rows = self.kis.inquire_ccnl(order_no=order_no, market=signal.market.value)
            except Exception as exc:
                # Record as PendingOrder so next sync settles it once KIS responds
                self._save_pending_order(
                    signal=signal,
                    order_no=order_no,
                    req_qty=qty,
                    recorded_filled_qty=0,
                    lot_id=signal.lot_id or "",
                    status=PendingOrderStatus.SUBMITTED,
                    order_date=today_str,
                    now_iso=now_iso,
                    state_applied=False,
                )
                msg = f"주문 접수됨(체결 조회 일시 오류로 후속 정산 대기, order_no={order_no}): {exc}"
                self._record_signal(signal, "SUBMITTED")
                self.sheet.append_audit(AuditRecord(now_iso, self.ctx.user_id, "ORDER_SUBMITTED_CCNL_ERROR", msg))
                return ExecutionOutcome(False, "SUBMITTED", msg, order_no=order_no)
            matched = [r for r in ccnl_rows if _order_no_matches(r.get("order_no"), order_no)]
            if matched:
                matched_row = matched[0]
                filled_qty = int(matched_row.get("filled_qty", 0) or 0)
                if float(matched_row.get("avg_price", 0) or 0) > 0:
                    filled_price = float(matched_row["avg_price"])
        else:
            filled_qty = qty

        if filled_qty <= 0:
            # Deferred fill (e.g. US LOC / KR limit order): persist PendingOrder for next-run settlement
            self._save_pending_order(
                signal=signal,
                order_no=order_no,
                req_qty=qty,
                recorded_filled_qty=0,
                lot_id=signal.lot_id or "",
                status=PendingOrderStatus.SUBMITTED,
                order_date=today_str,
                now_iso=now_iso,
                state_applied=False,
            )
            self._record_signal(signal, "SUBMITTED")
            self.sheet.append_audit(
                AuditRecord(
                    now_iso,
                    self.ctx.user_id,
                    "ORDER_SUBMITTED",
                    f"order_no={order_no}, {signal.action.value} {signal.symbol} req_qty={qty} (체결 대기 등록)",
                )
            )
            return ExecutionOutcome(
                False,
                "SUBMITTED",
                f"주문 접수 완료 · 체결 대기 (order_no={order_no})",
                order_no=order_no,
            )

        # 6. Apply immediately confirmed fill quantity to Fills & Lots
        outcome = self._apply_confirmed_fill(
            signal=signal,
            order_no=order_no,
            filled_qty=filled_qty,
            filled_price=filled_price,
            matched_row=matched_row,
            apply_algo_state=True,
            fill_date=today_str,
            record_signal=True,
        )

        # If only partially filled right away, track remainder in PendingOrders
        if filled_qty < qty:
            self._save_pending_order(
                signal=signal,
                order_no=order_no,
                req_qty=qty,
                recorded_filled_qty=filled_qty,
                lot_id=outcome.lot_ids[0] if outcome.lot_ids else (signal.lot_id or ""),
                status=PendingOrderStatus.PARTIAL,
                order_date=today_str,
                now_iso=now_iso,
                state_applied=True,
            )
        return outcome

    def sync_pending_orders(
        self,
        markets: Optional[Sequence[str]] = None,
    ) -> List[ExecutionOutcome]:
        """
        Re-query KIS inquire_ccnl for SUBMITTED/PARTIAL orders and settle any newly filled
        quantity into Fills & Lots BEFORE daily reconciliation runs.
        """
        if not hasattr(self.kis, "inquire_ccnl"):
            return []
        if getattr(self.sheet, "is_in_memory", False) and not isinstance(self.kis, MockKISClient):
            return []

        allowed_markets = {m.upper() for m in markets} if markets else None
        pending = [
            po for po in self.sheet.load_pending_orders(use_cache=False)
            if po.status in {PendingOrderStatus.SUBMITTED, PendingOrderStatus.PARTIAL}
            and (allowed_markets is None or po.market.upper() in allowed_markets)
        ]
        if not pending:
            return []

        today = date.today()
        today_str = today.strftime("%Y-%m-%d")
        now_iso = utc_now_iso()
        outcomes: List[ExecutionOutcome] = []
        updated_orders: List[PendingOrder] = []

        for po in pending:
            try:
                ccnl_rows = self.kis.inquire_ccnl(order_no=po.order_no, market=po.market)
            except Exception as exc:
                self.sheet.append_audit(
                    AuditRecord(now_iso, self.ctx.user_id, "PENDING_SYNC_ERROR", f"order_no={po.order_no}: {exc}")
                )
                continue

            matched = [r for r in ccnl_rows if _order_no_matches(r.get("order_no"), po.order_no)]
            order_dt = parse_date_str(po.order_date)
            is_past_order_day = order_dt is not None and order_dt < today
            is_expired_window = order_dt is not None and (today - order_dt) >= timedelta(days=PENDING_ORDER_EXPIRE_DAYS)

            if matched:
                row = matched[0]
                total_filled = int(row.get("filled_qty", 0) or 0)
                unfilled_qty = int(row.get("unfilled_qty", 0) or 0)
                avg_price = float(row.get("avg_price", 0) or 0) or float(po.limit_price)
                delta_qty = total_filled - int(po.recorded_filled_qty)

                if delta_qty > 0:
                    reconstructed_sig = Signal(
                        action=SignalAction(po.side),
                        algo_id=po.algo_id,
                        algo_version=po.algo_version,
                        symbol=po.symbol,
                        market=Market(po.market),
                        price_type=PriceType.LIMIT,
                        price=avg_price,
                        qty=delta_qty,
                        reason_text=po.reason_text or f"후속 체결 정산 ({po.order_no})",
                        lot_id=po.lot_id or None,
                        metadata=dict(po.metadata),
                    )
                    state_applied = bool(po.metadata.get("state_applied", False))
                    outcome = self._apply_confirmed_fill(
                        signal=reconstructed_sig,
                        order_no=po.order_no,
                        filled_qty=delta_qty,
                        filled_price=avg_price,
                        matched_row=row,
                        apply_algo_state=not state_applied,
                        fill_date=po.order_date or today_str,
                        record_signal=True,
                        entry_conditions_override=po.conditions,
                    )
                    outcomes.append(outcome)
                    po.recorded_filled_qty = total_filled
                    if outcome.lot_ids and not po.lot_id:
                        po.lot_id = outcome.lot_ids[0]
                    po.metadata["state_applied"] = True

                if po.recorded_filled_qty >= po.req_qty or (unfilled_qty == 0 and po.recorded_filled_qty > 0):
                    po.status = PendingOrderStatus.FILLED
                elif unfilled_qty == 0 and po.recorded_filled_qty == 0 and is_past_order_day:
                    po.status = PendingOrderStatus.EXPIRED
                elif po.recorded_filled_qty > 0:
                    po.status = PendingOrderStatus.PARTIAL
                po.updated_at = now_iso
                updated_orders.append(po)
            elif is_expired_window:
                po.status = PendingOrderStatus.FILLED if po.recorded_filled_qty > 0 else PendingOrderStatus.EXPIRED
                po.updated_at = now_iso
                updated_orders.append(po)
                self.sheet.append_audit(
                    AuditRecord(
                        now_iso,
                        self.ctx.user_id,
                        "PENDING_ORDER_EXPIRED",
                        f"order_no={po.order_no}, {po.side} {po.symbol} filled={po.recorded_filled_qty}/{po.req_qty}",
                    )
                )

        if updated_orders:
            self.sheet.upsert_pending_orders(updated_orders)
        return outcomes

    def _save_pending_order(
        self,
        signal: Signal,
        order_no: str,
        req_qty: int,
        recorded_filled_qty: int,
        lot_id: str,
        status: PendingOrderStatus,
        order_date: str,
        now_iso: str,
        state_applied: bool,
    ) -> None:
        meta = dict(signal.metadata)
        meta["state_applied"] = state_applied
        po = PendingOrder(
            order_no=order_no,
            symbol=signal.symbol,
            market=signal.market.value,
            algo_id=signal.algo_id,
            algo_version=signal.algo_version,
            side=signal.action.value,
            req_qty=req_qty,
            recorded_filled_qty=recorded_filled_qty,
            limit_price=float(signal.price),
            lot_id=lot_id,
            status=status,
            order_date=order_date,
            created_at=now_iso,
            updated_at=now_iso,
            metadata=meta,
            conditions=signal.conditions_as_dicts(),
            reason_text=signal.reason_text,
        )
        self.sheet.upsert_pending_orders([po])

    def _apply_confirmed_fill(
        self,
        signal: Signal,
        order_no: str,
        filled_qty: int,
        filled_price: float,
        matched_row: Optional[Dict[str, Any]],
        apply_algo_state: bool,
        fill_date: str,
        record_signal: bool = True,
        entry_conditions_override: Optional[List[Dict[str, Any]]] = None,
    ) -> ExecutionOutcome:
        is_risk_override = bool(signal.metadata.get("risk_override"))
        all_lots = self.sheet.load_lots(use_cache=False)
        all_fills = self.sheet.load_fills(use_cache=False)
        new_fills: List[Fill] = []
        touched_lots: List[Lot] = []
        unallocated_qty = 0

        if signal.action == SignalAction.BUY:
            target_lot = self._find_target_buy_lot(signal, all_lots)
            if target_lot is None:
                target_lot = Lot(
                    lot_id=generate_id("lot"),
                    symbol=signal.symbol,
                    market=signal.market.value,
                    algo_id=signal.algo_id,
                    algo_version=signal.algo_version,
                    buy_date=fill_date,
                    qty=0,
                    avg_price=0.0,
                    status=LotStatus.OPEN,
                    params_snapshot=dict(signal.metadata.get("params_snapshot", {})),
                    algo_state=self._next_algo_state({}, signal) if apply_algo_state else {},
                    entry_conditions=(
                        list(entry_conditions_override)
                        if entry_conditions_override is not None
                        else signal.conditions_as_dicts()
                    ),
                    note=signal.reason_text,
                )
            elif apply_algo_state:
                target_lot.algo_state = self._next_algo_state(target_lot.algo_state, signal)

            fee, tax = _estimate_fee_and_tax(signal.market.value, FillSide.BUY, filled_qty, filled_price, matched_row)
            fill = Fill(
                fill_id=generate_id("fill"),
                lot_id=target_lot.lot_id,
                side=FillSide.BUY,
                date=fill_date,
                qty=filled_qty,
                price=filled_price,
                fee=fee,
                tax=tax,
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
            allocated_qty = sum(q for _, q in allocations)
            unallocated_qty = max(0, filled_qty - allocated_qty)
            if unallocated_qty > 0:
                mismatch_detail = (
                    f"order_no={order_no}, symbol={signal.symbol}, filled_qty={filled_qty}, "
                    f"allocated_qty={allocated_qty}, unallocated_qty={unallocated_qty}"
                )
                self.sheet.append_audit(
                    AuditRecord(utc_now_iso(), self.ctx.user_id, "SELL_ALLOCATION_MISMATCH", mismatch_detail)
                )
                self.notifier.notify_error(
                    f"매도 Lot 배분 불일치 ({signal.symbol})",
                    f"체결 {filled_qty}주 중 {allocated_qty}주만 원장에 반영됨 (미배분 {unallocated_qty}주, order_no={order_no})",
                )

            lot_by_id = {l.lot_id: l for l in all_lots}
            for alloc_lot_id, alloc_qty in allocations:
                lot_obj = lot_by_id[alloc_lot_id]
                if apply_algo_state and "next_exit_stage" in signal.metadata:
                    lot_obj.algo_state["exit_stage"] = int(signal.metadata["next_exit_stage"])
                fee, tax = _estimate_fee_and_tax(signal.market.value, FillSide.SELL, alloc_qty, filled_price, matched_row)
                fill = Fill(
                    fill_id=generate_id("fill"),
                    lot_id=alloc_lot_id,
                    side=FillSide.SELL,
                    date=fill_date,
                    qty=alloc_qty,
                    price=filled_price,
                    fee=fee,
                    tax=tax,
                    order_no=order_no,
                    note="risk_override" if is_risk_override else signal.reason_text,
                )
                new_fills.append(fill)
                synced_lot = apply_fills_to_lot(lot_obj, all_fills + new_fills)
                touched_lots.append(synced_lot)

        if new_fills:
            fills_written = self.sheet.append_fills(new_fills)
            if touched_lots:
                # Keep Lots strictly ordered behind Fills if append_fills fell back to local queue
                self.sheet.upsert_lots(touched_lots, queue_only=not fills_written)

        if signal.action == SignalAction.SELL and unallocated_qty == filled_qty:
            decision_tag = "UNALLOCATED"
            msg = f"체결({filled_qty}주 @ {filled_price:,.2f})되었으나 배분할 OPEN Lot이 없어 원장 미반영"
        elif signal.action == SignalAction.SELL and unallocated_qty > 0:
            decision_tag = "PARTIAL_ALLOC"
            msg = f"체결 완료 ({filled_qty}주 @ {filled_price:,.2f}, 미배분 {unallocated_qty}주 확인 필요)"
        else:
            decision_tag = "RISK_OVERRIDE" if is_risk_override else "EXECUTED"
            msg = f"체결 완료 ({filled_qty}주 @ {filled_price:,.2f})"

        if record_signal:
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
        self.notifier.notify_signal(signal, status_label="체결완료" if unallocated_qty == 0 else f"체결(미배분 {unallocated_qty}주)")
        return ExecutionOutcome(
            executed=True,
            decision=decision_tag,
            message=msg,
            order_no=order_no,
            filled_qty=filled_qty,
            lot_ids=[l.lot_id for l in touched_lots],
        )

    @staticmethod
    def _find_target_buy_lot(signal: Signal, all_lots: List[Lot]) -> Optional[Lot]:
        """
        For stateful split-buy algorithms (us_infinite_buying, kr_fujimoto_126, us_fujimoto_126) or explicit lot_id,
        append new BUY Fill to the existing OPEN lot of the same (symbol, algo_id) (R5.3).
        """
        if signal.lot_id:
            for lot in all_lots:
                if lot.lot_id == signal.lot_id and lot.status == LotStatus.OPEN:
                    return lot
        if signal.algo_id in {"us_infinite_buying", "kr_fujimoto_126", "us_fujimoto_126"}:
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
