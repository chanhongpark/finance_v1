"""
Ledger domain models (R2.1, R2.2, R5).

Fills is the append-only source of truth; Lots represent logical positions
grouped by (symbol, algo_id) with derived qty/avg_price and algorithm state.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LotStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class FillSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Lot:
    """Represents a logical holding unit tracked by a specific algorithm."""
    lot_id: str
    symbol: str
    market: str                         # "KR" | "US"
    algo_id: str                        # e.g. "kr_monthly_trend", "us_infinite_buying", "manual"
    algo_version: str                   # Version at initial buy (audit history)
    buy_date: str                       # YYYY-MM-DD
    qty: int = 0                        # Derived from Fills
    avg_price: float = 0.0              # Derived from Fills
    status: LotStatus = LotStatus.OPEN
    params_snapshot: Dict[str, Any] = field(default_factory=dict)
    algo_state: Dict[str, Any] = field(default_factory=dict)
    entry_conditions: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def to_sheet_row(self) -> Dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "symbol": self.symbol,
            "market": self.market,
            "algo_id": self.algo_id,
            "algo_version": self.algo_version,
            "buy_date": self.buy_date,
            "qty": int(self.qty),
            "avg_price": round(float(self.avg_price), 4),
            "status": self.status.value if isinstance(self.status, LotStatus) else str(self.status),
            "params_snapshot": json.dumps(self.params_snapshot, ensure_ascii=False),
            "algo_state_json": json.dumps(self.algo_state, ensure_ascii=False),
            "entry_conditions_json": json.dumps(self.entry_conditions, ensure_ascii=False),
            "note": self.note,
        }

    @classmethod
    def from_sheet_row(cls, row: Dict[str, Any]) -> "Lot":
        def _load_json(raw: Any, default: Any) -> Any:
            if isinstance(raw, (dict, list)):
                return raw
            if not raw:
                return default
            try:
                return json.loads(str(raw))
            except json.JSONDecodeError:
                return default

        status_raw = str(row.get("status", LotStatus.OPEN.value)).upper()
        status = LotStatus(status_raw) if status_raw in {"OPEN", "CLOSED"} else LotStatus.OPEN
        return cls(
            lot_id=str(row.get("lot_id", "")),
            symbol=str(row.get("symbol", "")),
            market=str(row.get("market", "KR")).upper(),
            algo_id=str(row.get("algo_id", "manual")),
            algo_version=str(row.get("algo_version", "1.0.0")),
            buy_date=str(row.get("buy_date", "")),
            qty=int(float(row.get("qty", 0) or 0)),
            avg_price=float(row.get("avg_price", 0.0) or 0.0),
            status=status,
            params_snapshot=_load_json(row.get("params_snapshot"), {}),
            algo_state=_load_json(row.get("algo_state_json"), {}),
            entry_conditions=_load_json(row.get("entry_conditions_json"), []),
            note=str(row.get("note", "")),
        )


@dataclass(frozen=True)
class Fill:
    """Append-only execution ledger row (R2.2, R5.4)."""
    fill_id: str
    lot_id: str
    side: FillSide                      # BUY | SELL
    date: str                           # YYYY-MM-DD or ISO timestamp
    qty: int                            # Positive integer (or negative for adjustment)
    price: float
    fee: float = 0.0
    tax: float = 0.0
    order_no: str = ""
    note: str = ""

    def to_sheet_row(self) -> Dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "lot_id": self.lot_id,
            "side": self.side.value if isinstance(self.side, FillSide) else str(self.side),
            "date": self.date,
            "qty": int(self.qty),
            "price": round(float(self.price), 4),
            "fee": round(float(self.fee), 4),
            "tax": round(float(self.tax), 4),
            "order_no": self.order_no,
            "note": self.note,
        }

    @classmethod
    def from_sheet_row(cls, row: Dict[str, Any]) -> "Fill":
        side_raw = str(row.get("side", FillSide.BUY.value)).upper()
        side = FillSide(side_raw) if side_raw in {"BUY", "SELL"} else FillSide.BUY
        return cls(
            fill_id=str(row.get("fill_id", "")),
            lot_id=str(row.get("lot_id", "")),
            side=side,
            date=str(row.get("date", "")),
            qty=int(float(row.get("qty", 0) or 0)),
            price=float(row.get("price", 0.0) or 0.0),
            fee=float(row.get("fee", 0.0) or 0.0),
            tax=float(row.get("tax", 0.0) or 0.0),
            order_no=str(row.get("order_no", "")),
            note=str(row.get("note", "")),
        )


@dataclass(frozen=True)
class SignalRecord:
    """Recorded signal & conditions in the Signals sheet (R2.1, R4.2)."""
    ts: str
    symbol: str
    algo_id: str
    algo_version: str
    action: str                         # BUY | SELL
    conditions: List[Dict[str, Any]]
    decision: str                       # PROPOSED | EXECUTED | REJECTED_RISK | SKIPPED | RISK_OVERRIDE
    score: float = 0.0
    reason_text: str = ""

    def to_sheet_row(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "algo_id": self.algo_id,
            "algo_version": self.algo_version,
            "action": self.action,
            "conditions_json": json.dumps(self.conditions, ensure_ascii=False),
            "decision": self.decision,
            "score": round(float(self.score), 2),
            "reason_text": self.reason_text,
        }


@dataclass(frozen=True)
class AuditRecord:
    """Immutable audit log row (R2.1, R10)."""
    ts: str
    actor: str
    action: str
    detail: str

    def to_sheet_row(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LotMetrics:
    """Derived accounting metrics calculated purely from append-only Fills."""
    lot_id: str
    remaining_qty: int
    avg_price: float
    total_buy_qty: int
    total_sell_qty: int
    total_buy_cost: float
    total_sell_proceeds: float
    total_fees_and_taxes: float
    realized_pnl: float
    realized_return_pct: float
    holding_days: int
    status: LotStatus


@dataclass(frozen=True)
class AlgorithmPerformance:
    """Per-algorithm aggregated performance metrics (R5.7)."""
    algo_id: str
    total_lots: int
    open_lots: int
    closed_lots: int
    win_count: int
    loss_count: int
    win_rate_pct: float
    avg_return_pct: float
    total_realized_pnl: float
    avg_holding_days: float
    max_drawdown_pct: float
