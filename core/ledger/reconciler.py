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

from core.ledger.models import AuditRecord, Fill, Lot, LotStatus, utc_now_iso
from core.ledger.onboarding import register_existing_holding


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
    created_fills: List[Fill] = field(default_factory=list)
    audits: List[AuditRecord] = field(default_factory=list)


def reconcile_holdings(
    kis_holdings: Sequence[Dict[str, Any]],
    sheet_lots: Sequence[Lot],
    actor: str = "reconciler",
) -> ReconciliationReport:
    """
    Compare KIS actual holdings against Sheet OPEN lots grouped by (market, symbol).
    """
    kis_map: Dict[tuple[str, str], Dict[str, Any]] = {}
    for h in kis_holdings:
        qty = int(h.get("qty", 0) or 0)
        if qty <= 0:
            continue
        key = (str(h.get("market", "KR")).upper(), str(h.get("symbol", "")).strip().upper())
        kis_map[key] = {
            "qty": qty,
            "avg_price": float(h.get("avg_price", 0.0) or 0.0),
        }

    sheet_qty_map: Dict[tuple[str, str], int] = {}
    for lot in sheet_lots:
        if lot.status == LotStatus.OPEN and lot.qty > 0:
            key = (lot.market.upper(), lot.symbol.upper())
            sheet_qty_map[key] = sheet_qty_map.get(key, 0) + int(lot.qty)

    all_keys = sorted(set(kis_map.keys()) | set(sheet_qty_map.keys()))
    discrepancies: List[DiscrepancyItem] = []
    created_lots: List[Lot] = []
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
        created_fills=created_fills,
        audits=audits,
    )
