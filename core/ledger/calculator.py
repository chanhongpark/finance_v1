"""
Append-only Fill ledger calculator and multi-lot sell allocator (R2.2, R5.4, R5.6, R5.7).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from core.ledger.models import (
    AlgorithmPerformance,
    Fill,
    FillSide,
    Lot,
    LotMetrics,
    LotStatus,
)


def _parse_date(date_str: str) -> Optional[date]:
    if not date_str:
        return None
    cleaned = str(date_str).strip()[:10].replace(".", "-").replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def recalculate_lot_from_fills(
    lot: Lot,
    fills: Sequence[Fill],
    as_of_date: Optional[date] = None,
) -> LotMetrics:
    """
    Compute remaining qty, weighted average buy price, realized PnL,
    holding days, and OPEN/CLOSED status purely from append-only Fills (R2.2).
    """
    lot_fills = sorted(
        [f for f in fills if f.lot_id == lot.lot_id],
        key=lambda f: (f.date, 0 if f.side == FillSide.BUY else 1, f.fill_id),
    )

    running_qty = 0
    running_cost = 0.0
    total_buy_qty = 0
    total_sell_qty = 0
    total_buy_cost = 0.0
    total_sell_proceeds = 0.0
    total_fees_and_taxes = 0.0
    realized_pnl = 0.0
    realized_cost_basis = 0.0
    last_sell_date: Optional[date] = None

    for fill in lot_fills:
        fee_tax = float(fill.fee) + float(fill.tax)
        total_fees_and_taxes += fee_tax

        if fill.side == FillSide.BUY:
            buy_qty = int(fill.qty)
            buy_amount = buy_qty * float(fill.price)
            running_qty += buy_qty
            running_cost += buy_amount
            total_buy_qty += buy_qty
            total_buy_cost += buy_amount
            # Fees on buy reduce realized PnL when tracked net
            realized_pnl -= fee_tax
        elif fill.side == FillSide.SELL:
            sell_qty = int(fill.qty)
            avg_before_sell = (running_cost / running_qty) if running_qty > 0 else 0.0
            matched_qty = min(sell_qty, max(0, running_qty))
            cost_removed = avg_before_sell * matched_qty
            proceeds = sell_qty * float(fill.price)

            running_qty = max(0, running_qty - sell_qty)
            running_cost = max(0.0, running_cost - cost_removed)

            total_sell_qty += sell_qty
            total_sell_proceeds += proceeds
            realized_cost_basis += cost_removed
            realized_pnl += (proceeds - cost_removed - fee_tax)
            last_sell_date = _parse_date(fill.date) or last_sell_date

    avg_price = (running_cost / running_qty) if running_qty > 0 else (
        (total_buy_cost / total_buy_qty) if total_buy_qty > 0 else 0.0
    )

    status = LotStatus.CLOSED if (total_buy_qty > 0 and running_qty == 0) else LotStatus.OPEN

    buy_dt = _parse_date(lot.buy_date)
    if buy_dt is None and lot_fills:
        buy_dt = _parse_date(lot_fills[0].date)

    ref_dt = last_sell_date if (status == LotStatus.CLOSED and last_sell_date) else (as_of_date or date.today())
    holding_days = max(0, (ref_dt - buy_dt).days) if buy_dt else 0

    realized_return_pct = (
        (realized_pnl / realized_cost_basis) * 100.0 if realized_cost_basis > 0 else 0.0
    )

    return LotMetrics(
        lot_id=lot.lot_id,
        remaining_qty=running_qty,
        avg_price=round(avg_price, 4),
        total_buy_qty=total_buy_qty,
        total_sell_qty=total_sell_qty,
        total_buy_cost=round(total_buy_cost, 4),
        total_sell_proceeds=round(total_sell_proceeds, 4),
        total_fees_and_taxes=round(total_fees_and_taxes, 4),
        realized_pnl=round(realized_pnl, 4),
        realized_return_pct=round(realized_return_pct, 2),
        holding_days=holding_days,
        status=status,
    )


def apply_fills_to_lot(
    lot: Lot,
    fills: Sequence[Fill],
    as_of_date: Optional[date] = None,
) -> Lot:
    """Return a copy of Lot with qty, avg_price, and status synced from Fills."""
    metrics = recalculate_lot_from_fills(lot, fills, as_of_date=as_of_date)
    return Lot(
        lot_id=lot.lot_id,
        symbol=lot.symbol,
        market=lot.market,
        algo_id=lot.algo_id,
        algo_version=lot.algo_version,
        buy_date=lot.buy_date,
        qty=metrics.remaining_qty,
        avg_price=metrics.avg_price,
        status=metrics.status,
        params_snapshot=dict(lot.params_snapshot),
        algo_state=dict(lot.algo_state),
        entry_conditions=list(lot.entry_conditions),
        note=lot.note,
    )


def allocate_sell_across_lots(
    symbol: str,
    sell_qty: int,
    open_lots: Sequence[Lot],
    preferred_algo_id: Optional[str] = None,
    preferred_lot_id: Optional[str] = None,
) -> List[Tuple[str, int]]:
    """
    Allocate a sell quantity across multiple OPEN lots of the same symbol (R5.6).

    Priority order:
    1. Explicit `preferred_lot_id` (if provided and matches symbol)
    2. Lots matching `preferred_algo_id` in FIFO order (by buy_date, lot_id)
    3. Remaining OPEN lots of the same symbol in FIFO order (by buy_date, lot_id)

    Returns:
        List of (lot_id, allocated_qty).
    """
    if sell_qty <= 0:
        return []

    candidates = [
        lot for lot in open_lots
        if lot.symbol == symbol and lot.status == LotStatus.OPEN and lot.qty > 0
    ]

    def _sort_key(lot: Lot) -> Tuple[int, int, str, str]:
        p_lot = 0 if (preferred_lot_id and lot.lot_id == preferred_lot_id) else 1
        p_algo = 0 if (preferred_algo_id and lot.algo_id == preferred_algo_id) else 1
        return (p_lot, p_algo, lot.buy_date or "9999-12-31", lot.lot_id)

    ordered = sorted(candidates, key=_sort_key)
    remaining = int(sell_qty)
    allocations: List[Tuple[str, int]] = []

    for lot in ordered:
        if remaining <= 0:
            break
        take = min(lot.qty, remaining)
        if take > 0:
            allocations.append((lot.lot_id, take))
            remaining -= take

    return allocations


def calculate_algorithm_performance(
    lots: Iterable[Lot],
    fills: Sequence[Fill],
    as_of_date: Optional[date] = None,
) -> Dict[str, AlgorithmPerformance]:
    """
    Aggregate per-algorithm performance metrics: win rate, avg return,
    holding period, total realized PnL, and MDD (R5.7).
    """
    grouped: Dict[str, List[Tuple[Lot, LotMetrics]]] = {}
    for lot in lots:
        metrics = recalculate_lot_from_fills(lot, fills, as_of_date=as_of_date)
        grouped.setdefault(lot.algo_id, []).append((lot, metrics))

    results: Dict[str, AlgorithmPerformance] = {}
    for algo_id, items in grouped.items():
        total_lots = len(items)
        open_lots = sum(1 for _, m in items if m.status == LotStatus.OPEN)
        closed_items = [(lot, m) for lot, m in items if m.status == LotStatus.CLOSED]
        closed_lots = len(closed_items)

        win_count = sum(1 for _, m in closed_items if m.realized_pnl > 0)
        loss_count = sum(1 for _, m in closed_items if m.realized_pnl <= 0)
        win_rate = (win_count / closed_lots * 100.0) if closed_lots > 0 else 0.0

        avg_return = (
            sum(m.realized_return_pct for _, m in closed_items) / closed_lots
            if closed_lots > 0 else 0.0
        )
        total_realized_pnl = sum(m.realized_pnl for _, m in items)
        avg_holding_days = (
            sum(m.holding_days for _, m in items) / total_lots
            if total_lots > 0 else 0.0
        )

        # Equity-curve MDD across closed lots sorted by buy_date
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for _, m in sorted(closed_items, key=lambda x: (x[0].buy_date, x[0].lot_id)):
            equity *= (1.0 + m.realized_return_pct / 100.0)
            if equity > peak:
                peak = equity
            dd = (equity - peak) / peak * 100.0 if peak > 0 else 0.0
            if dd < max_dd:
                max_dd = dd

        results[algo_id] = AlgorithmPerformance(
            algo_id=algo_id,
            total_lots=total_lots,
            open_lots=open_lots,
            closed_lots=closed_lots,
            win_count=win_count,
            loss_count=loss_count,
            win_rate_pct=round(win_rate, 2),
            avg_return_pct=round(avg_return, 2),
            total_realized_pnl=round(total_realized_pnl, 2),
            avg_holding_days=round(avg_holding_days, 1),
            max_drawdown_pct=round(max_dd, 2),
        )

    return results
