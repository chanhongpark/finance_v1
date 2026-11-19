"""
Manual onboarding & algorithm mapping for pre-existing holdings (no prior history).

Supports:
1. Direct manual registration of existing holdings (Lot + initial MANUAL_INIT Fill + Audit).
2. Assigning an algorithm (and optional quantity split) to a 'manual' lot created by reconciliation.
3. Initial algo_state builders for stateful algorithms (Fujimoto 1:2:6 stages, US Infinite Buying T-round).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from core.ledger.models import (
    AuditRecord,
    Fill,
    FillSide,
    Lot,
    LotStatus,
    generate_id,
    utc_now_iso,
)


def build_fujimoto_initial_state(
    entry_stage: int = 1,
    exit_stage: int = 0,
    base_entry_price: float = 0.0,
) -> Dict[str, Any]:
    """
    Build initial algo_state for Fujimoto 1:2:6 existing holdings.
    entry_stage: 1 (1/9), 2 (1+2/9), 3 (1+2+6 full)
    exit_stage: 0 (none), 1 (1st exit done), 2 (2nd exit done)
    """
    return {
        "entry_stage": max(1, min(3, int(entry_stage))),
        "exit_stage": max(0, min(2, int(exit_stage))),
        "base_entry_price": round(float(base_entry_price), 2),
    }


def build_infinite_buying_initial_state(
    qty: int,
    avg_price: float,
    total_budget_usd: float = 10_000.0,
    division_count: int = 40,
    current_round_override: Optional[float] = None,
    status: str = "RUNNING",
) -> Dict[str, Any]:
    """
    Build initial algo_state for US Infinite Buying existing holdings.
    Automatically calculates current_round (T) from total invested / one_round_budget
    unless explicitly overridden by the user.
    """
    div = max(1, int(division_count))
    one_round_budget = float(total_budget_usd) / div if total_budget_usd > 0 else 250.0
    invested = float(qty) * float(avg_price)
    calc_round = round(invested / one_round_budget, 2) if one_round_budget > 0 else 0.0
    curr_round = float(current_round_override) if current_round_override is not None else calc_round

    return {
        "current_round": max(0.0, round(curr_round, 2)),
        "division_count": div,
        "total_budget_usd": round(float(total_budget_usd), 2),
        "status": status.upper(),
    }


def register_existing_holding(
    symbol: str,
    market: str,
    qty: int,
    avg_price: float,
    algo_id: str,
    algo_version: str = "1.0.0",
    buy_date: Optional[str] = None,
    algo_state: Optional[Dict[str, Any]] = None,
    params_snapshot: Optional[Dict[str, Any]] = None,
    note: str = "기존 보유 종목 수동 등록",
    actor: str = "user",
) -> Tuple[Lot, Fill, AuditRecord]:
    """
    Create a new Lot, an initial append-only BUY Fill (order_no='MANUAL_INIT'),
    and an AuditRecord for a pre-existing account holding.
    """
    if qty <= 0:
        raise ValueError("qty must be > 0")
    if avg_price <= 0:
        raise ValueError("avg_price must be > 0")

    effective_buy_date = buy_date or date.today().strftime("%Y-%m-%d")
    lot_id = generate_id("lot")
    fill_id = generate_id("fill")

    lot = Lot(
        lot_id=lot_id,
        symbol=symbol.strip().upper(),
        market=market.strip().upper(),
        algo_id=algo_id.strip(),
        algo_version=algo_version,
        buy_date=effective_buy_date,
        qty=int(qty),
        avg_price=round(float(avg_price), 4),
        status=LotStatus.OPEN,
        params_snapshot=dict(params_snapshot or {}),
        algo_state=dict(algo_state or {}),
        entry_conditions=[
            {
                "name": "manual_onboarding",
                "value": f"{qty}주 @ {avg_price}",
                "threshold": "existing_holding",
                "op": "==",
                "passed": True,
                "desc": note,
            }
        ],
        note=note,
    )

    initial_fill = Fill(
        fill_id=fill_id,
        lot_id=lot_id,
        side=FillSide.BUY,
        date=effective_buy_date,
        qty=int(qty),
        price=round(float(avg_price), 4),
        fee=0.0,
        tax=0.0,
        order_no="MANUAL_INIT",
        note=note,
    )

    audit = AuditRecord(
        ts=utc_now_iso(),
        actor=actor,
        action="MANUAL_REGISTER_HOLDING",
        detail=f"symbol={lot.symbol}, qty={qty}, avg_price={avg_price}, algo_id={algo_id}, lot_id={lot_id}",
    )

    return lot, initial_fill, audit


def assign_algorithm_to_manual_lot(
    manual_lot: Lot,
    target_algo_id: str,
    target_algo_version: str = "1.0.0",
    assign_qty: Optional[int] = None,
    buy_date: Optional[str] = None,
    algo_state: Optional[Dict[str, Any]] = None,
    params_snapshot: Optional[Dict[str, Any]] = None,
    note: str = "기존 미할당 보유분 알고리즘 매핑",
    actor: str = "user",
) -> Tuple[List[Lot], List[Fill], AuditRecord]:
    """
    Assign an algorithm (and optional buy_date/state) to an existing 'manual' lot.

    - If `assign_qty` is None or equals `manual_lot.qty`, the manual lot itself is updated in place.
    - If `0 < assign_qty < manual_lot.qty`, the lot is split:
      * A correcting SELL-side adjustment Fill (-assign_qty transfer) reduces the manual lot,
      * A new Lot + initial BUY Fill is created for `target_algo_id` with `assign_qty`.

    Returns:
        (updated_or_created_lots, new_fills_to_append, audit_record)
    """
    if manual_lot.status != LotStatus.OPEN or manual_lot.qty <= 0:
        raise ValueError("Only OPEN manual lots with qty > 0 can be assigned")

    effective_qty = manual_lot.qty if assign_qty is None else int(assign_qty)
    if effective_qty <= 0 or effective_qty > manual_lot.qty:
        raise ValueError(f"assign_qty ({effective_qty}) must be between 1 and {manual_lot.qty}")

    effective_buy_date = buy_date or manual_lot.buy_date or date.today().strftime("%Y-%m-%d")

    # Case 1: Full assignment (re-tag the existing manual lot)
    if effective_qty == manual_lot.qty:
        updated = Lot(
            lot_id=manual_lot.lot_id,
            symbol=manual_lot.symbol,
            market=manual_lot.market,
            algo_id=target_algo_id,
            algo_version=target_algo_version,
            buy_date=effective_buy_date,
            qty=manual_lot.qty,
            avg_price=manual_lot.avg_price,
            status=LotStatus.OPEN,
            params_snapshot=dict(params_snapshot or manual_lot.params_snapshot),
            algo_state=dict(algo_state or manual_lot.algo_state),
            entry_conditions=list(manual_lot.entry_conditions),
            note=note,
        )
        audit = AuditRecord(
            ts=utc_now_iso(),
            actor=actor,
            action="ASSIGN_MANUAL_LOT",
            detail=f"lot_id={manual_lot.lot_id}, symbol={manual_lot.symbol}, algo_id={target_algo_id}, qty={effective_qty}",
        )
        return [updated], [], audit

    # Case 2: Partial split (keep remainder in manual_lot, create new Lot for target_algo_id)
    remainder_qty = manual_lot.qty - effective_qty
    reduced_manual_lot = Lot(
        lot_id=manual_lot.lot_id,
        symbol=manual_lot.symbol,
        market=manual_lot.market,
        algo_id=manual_lot.algo_id,
        algo_version=manual_lot.algo_version,
        buy_date=manual_lot.buy_date,
        qty=remainder_qty,
        avg_price=manual_lot.avg_price,
        status=LotStatus.OPEN,
        params_snapshot=dict(manual_lot.params_snapshot),
        algo_state=dict(manual_lot.algo_state),
        entry_conditions=list(manual_lot.entry_conditions),
        note=f"{manual_lot.note} (분할 이관: -{effective_qty}주 -> {target_algo_id})".strip(),
    )

    new_lot, new_buy_fill, _ = register_existing_holding(
        symbol=manual_lot.symbol,
        market=manual_lot.market,
        qty=effective_qty,
        avg_price=manual_lot.avg_price,
        algo_id=target_algo_id,
        algo_version=target_algo_version,
        buy_date=effective_buy_date,
        algo_state=algo_state,
        params_snapshot=params_snapshot,
        note=f"{note} (from {manual_lot.lot_id})",
        actor=actor,
    )

    # Append-only transfer-out fill on manual lot (dated on/after manual_lot.buy_date so ledger order is preserved)
    transfer_date = max(date.today().strftime("%Y-%m-%d"), manual_lot.buy_date or "")
    transfer_out_fill = Fill(
        fill_id=generate_id("fill"),
        lot_id=manual_lot.lot_id,
        side=FillSide.SELL,
        date=transfer_date,
        qty=effective_qty,
        price=manual_lot.avg_price,
        fee=0.0,
        tax=0.0,
        order_no=f"SPLIT_TO_{new_lot.lot_id}",
        note=f"Split {effective_qty} shares to {target_algo_id} ({new_lot.lot_id})",
    )

    audit = AuditRecord(
        ts=utc_now_iso(),
        actor=actor,
        action="SPLIT_MANUAL_LOT",
        detail=(
            f"source_lot={manual_lot.lot_id} (rem={remainder_qty}), "
            f"new_lot={new_lot.lot_id} (algo={target_algo_id}, qty={effective_qty})"
        ),
    )

    return [reduced_manual_lot, new_lot], [transfer_out_fill, new_buy_fill], audit
