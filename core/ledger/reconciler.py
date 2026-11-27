"""
Daily Reconciliation between KIS real account holdings and personal Sheet OPEN Lots (R2.3).

If KIS holds more shares than the sum of Sheet OPEN lots (e.g., pre-existing holdings
or manual HTS trades), creates an unassigned `algo_id="manual"` Lot + initial Fill
and returns a discrepancy report so the user can notify via Telegram and map it in UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Sequence

from core.ledger.models import AuditRecord, Fill, FillSide, Lot, LotStatus, generate_id, utc_now_iso
from core.ledger.onboarding import register_existing_holding


def _norm_reconcile_symbol(symbol: Any, market: str) -> str:
    s = str(symbol or "").strip().upper()
    if market.upper() == "KR" and s.isdigit() and len(s) < 6:
        return s.zfill(6)
    return s


@dataclass
class DiscrepancyItem:
    symbol: str
    market: str
    kis_qty: int
    sheet_qty: int
    diff_qty: int           # kis_qty - sheet_qty
    kis_avg_price: float
    action_taken: str


@dataclass
class ReconciliationReport:
    ts: str
    is_matched: bool
    discrepancies: List[DiscrepancyItem] = field(default_factory=list)
    created_manual_lots: List[Lot] = field(default_factory=list)
    closed_duplicate_lots: List[Lot] = field(default_factory=list)
    created_fills: List[Fill] = field(default_factory=list)
    audits: List[AuditRecord] = field(default_factory=list)
    skipped_reason: str = ""


def reconcile_holdings(
    kis_holdings: Sequence[Dict[str, Any]],
    sheet_lots: Sequence[Lot],
    actor: str = "reconciler",
    exclude_keys: Sequence[tuple[str, str]] = (),
) -> ReconciliationReport:
    """
    Compare KIS actual holdings against Sheet OPEN lots grouped by (market, symbol).
    Symbols in `exclude_keys` (e.g. with unsettled SUBMITTED/PARTIAL pending orders) are skipped.
    """
    excluded = {(str(m).upper(), _norm_reconcile_symbol(s, str(m))) for m, s in exclude_keys}
    kis_map: Dict[tuple[str, str], Dict[str, Any]] = {}
    for h in kis_holdings:
        qty = int(h.get("qty", 0) or 0)
        if qty <= 0:
            continue
        mkt = str(h.get("market", "KR")).upper()
        key = (mkt, _norm_reconcile_symbol(h.get("symbol", ""), mkt))
        if key in excluded:
            continue
        kis_map[key] = {
            "qty": qty,
            "avg_price": float(h.get("avg_price", 0.0) or 0.0),
        }

    sheet_qty_map: Dict[tuple[str, str], int] = {}
    open_lots_by_key: Dict[tuple[str, str], List[Lot]] = {}
    for lot in sheet_lots:
        if lot.status == LotStatus.OPEN and lot.qty > 0:
            mkt = lot.market.upper()
            key = (mkt, _norm_reconcile_symbol(lot.symbol, mkt))
            if key in excluded:
                continue
            sheet_qty_map[key] = sheet_qty_map.get(key, 0) + int(lot.qty)
            open_lots_by_key.setdefault(key, []).append(lot)

    all_keys = sorted(set(kis_map.keys()) | set(sheet_qty_map.keys()))
    discrepancies: List[DiscrepancyItem] = []
    created_lots: List[Lot] = []
    closed_dup_lots: List[Lot] = []
    created_fills: List[Fill] = []
    audits: List[AuditRecord] = []

    for market, symbol in all_keys:
        kis_info = kis_map.get((market, symbol), {"qty": 0, "avg_price": 0.0})
        kis_qty = int(kis_info["qty"])
        kis_avg = float(kis_info["avg_price"])
        sheet_qty = int(sheet_qty_map.get((market, symbol), 0))
        diff = kis_qty - sheet_qty

        if diff == 0:
            continue

        if diff > 0:
            # Untracked shares exist in KIS -> create manual lot so user can map algorithm
            eff_price = kis_avg if kis_avg > 0 else 1.0
            lot, fill, audit = register_existing_holding(
                symbol=symbol,
                market=market,
                qty=diff,
                avg_price=eff_price,
                algo_id="manual",
                algo_version="0.0.0",
                buy_date=date.today().strftime("%Y-%m-%d"),
                note=f"대사 불일치 자동 생성 (KIS={kis_qty}, Sheet={sheet_qty})",
                actor=actor,
            )
            created_lots.append(lot)
            created_fills.append(fill)
            audits.append(audit)
            action_taken = f"manual Lot 생성 (+{diff}주, lot_id={lot.lot_id})"
        else:
            # Sheet has more shares than KIS: first auto-close exact duplicate `manual` lots if present
            excess = abs(diff)
            manual_candidates = [
                l for l in open_lots_by_key.get((market, symbol), [])
                if l.algo_id == "manual" and l.qty > 0
            ]
            seen_sig: set[tuple[str, int, float]] = set()
            dup_closed_ids: List[str] = []
            today_str = date.today().strftime("%Y-%m-%d")
            for m_lot in manual_candidates:
                sig = (str(m_lot.buy_date or ""), int(m_lot.qty), round(float(m_lot.avg_price or 0.0), 2))
                if sig in seen_sig and int(m_lot.qty) <= excess:
                    closed_lot = Lot(
                        lot_id=m_lot.lot_id,
                        symbol=m_lot.symbol,
                        market=m_lot.market,
                        algo_id=m_lot.algo_id,
                        algo_version=m_lot.algo_version,
                        buy_date=m_lot.buy_date,
                        qty=0,
                        avg_price=m_lot.avg_price,
                        status=LotStatus.CLOSED,
                        params_snapshot=dict(m_lot.params_snapshot),
                        algo_state=dict(m_lot.algo_state),
                        entry_conditions=list(m_lot.entry_conditions),
                        note=f"{m_lot.note} (중복 manual Lot 자동 정리)".strip(),
                    )
                    adj_fill = Fill(
                        fill_id=generate_id("fill"),
                        lot_id=m_lot.lot_id,
                        side=FillSide.SELL,
                        date=max(today_str, m_lot.buy_date or ""),
                        qty=int(m_lot.qty),
                        price=float(m_lot.avg_price),
                        fee=0.0,
                        tax=0.0,
                        order_no="DEDUP_MANUAL_LOT",
                        note="대사 시 중복 manual Lot 자동 정리 (손익 0)",
                    )
                    closed_dup_lots.append(closed_lot)
                    created_fills.append(adj_fill)
                    dup_closed_ids.append(m_lot.lot_id)
                    excess -= int(m_lot.qty)
                    audits.append(
                        AuditRecord(
                            ts=utc_now_iso(),
                            actor=actor,
                            action="RECONCILE_DEDUP_MANUAL_LOT",
                            detail=f"lot_id={m_lot.lot_id}, symbol={symbol}, market={market}, closed_qty={m_lot.qty}",
                        )
                    )
                else:
                    seen_sig.add(sig)

            if dup_closed_ids and excess == 0:
                action_taken = f"중복 manual Lot {len(dup_closed_ids)}건 자동 정리 ({', '.join(dup_closed_ids)})"
            elif dup_closed_ids and excess > 0:
                action_taken = (
                    f"중복 manual Lot {len(dup_closed_ids)}건 정리 후 잔여 초과 {excess}주 "
                    "(외부 매도 추정, 수동 확인 필요)"
                )
                audits.append(
                    AuditRecord(
                        ts=utc_now_iso(),
                        actor=actor,
                        action="RECONCILE_SHORTFALL",
                        detail=f"symbol={symbol}, market={market}, kis_qty={kis_qty}, sheet_qty={sheet_qty}, remaining_excess={excess}",
                    )
                )
            else:
                # Sheet has more shares than KIS (likely sold outside system in HTS)
                action_taken = f"경고: Sheet 잔량 초과 ({abs(diff)}주 외부 매도 추정, 수동 확인 필요)"
                audits.append(
                    AuditRecord(
                        ts=utc_now_iso(),
                        actor=actor,
                        action="RECONCILE_SHORTFALL",
                        detail=f"symbol={symbol}, market={market}, kis_qty={kis_qty}, sheet_qty={sheet_qty}",
                    )
                )

        discrepancies.append(
            DiscrepancyItem(
                symbol=symbol,
                market=market,
                kis_qty=kis_qty,
                sheet_qty=sheet_qty,
                diff_qty=diff,
                kis_avg_price=kis_avg,
                action_taken=action_taken,
            )
        )

    return ReconciliationReport(
        ts=utc_now_iso(),
        is_matched=(len(discrepancies) == 0),
        discrepancies=discrepancies,
        created_manual_lots=created_lots,
        closed_duplicate_lots=closed_dup_lots,
        created_fills=created_fills,
        audits=audits,
    )
