"""
Unified Algorithm interface, Signal, Condition, and Parameter schema (R4.1, R4.2, R14).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import pandas as pd

from core.ledger.models import Lot, utc_now_iso


class Market(str, Enum):
    KR = "KR"
    US = "US"


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PriceType(str, Enum):
    LIMIT = "지정가"
    MARKET = "시장가"
    LOC = "LOC"


@dataclass(frozen=True)
class Condition:
    """Single auditable rule evaluation shown in tooltips and Signals sheet (R4.2)."""
    name: str
    value: Any
    threshold: Any
    op: str
    passed: bool
    desc: str

    def format_badge(self) -> str:
        icon = "✅" if self.passed else "❌"
        return f"{self.name}={self.value} {self.op} {self.threshold} {icon}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "threshold": self.threshold,
            "op": self.op,
            "passed": bool(self.passed),
            "desc": self.desc,
        }


@dataclass(frozen=True)
class ParamSpec:
    """Self-describing parameter schema used to auto-generate UI inputs & tooltips (R3.2, R4.1)."""
    key: str
    label: str
    param_type: str                 # "int" | "float" | "bool" | "select"
    default: Any
    description: str
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    step: Optional[float] = None
    options: Optional[List[Any]] = None


@dataclass
class Signal:
    """Standardized entry/exit trading signal emitted by any Algorithm (R4.1)."""
    action: SignalAction
    algo_id: str
    algo_version: str
    symbol: str
    market: Market
    price_type: PriceType
    price: float
    qty: Optional[int] = None
    qty_ratio: Optional[float] = None
    conditions: List[Condition] = field(default_factory=list)
    score: float = 0.0
    reason_text: str = ""
    ts: str = field(default_factory=utc_now_iso)
    lot_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def conditions_as_dicts(self) -> List[Dict[str, Any]]:
        return [c.to_dict() for c in self.conditions]

    def format_conditions_tooltip(self) -> str:
        if not self.conditions:
            return self.reason_text
        lines = [c.format_badge() for c in self.conditions]
        if self.reason_text:
            lines.append(f"— {self.reason_text}")
        return "\n".join(lines)


class Algorithm(ABC):
    """
    Base class for all KR & US trading algorithms (R4.1).

    Every algorithm is responsible for BOTH entry (`evaluate_entry`) and
    exit (`evaluate_exit`) of its own lots. Under R14, `evaluate_exit` always
    runs using the latest registered version of the algorithm.
    """
    id: str = ""
    version: str = "1.0.0"
    name: str = ""
    description: str = ""
    market: Market = Market.KR
    params_schema: List[ParamSpec] = []

    def get_default_params(self) -> Dict[str, Any]:
        return {spec.key: spec.default for spec in self.params_schema}

    def merge_params(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        merged = self.get_default_params()
        if overrides:
            for k, v in overrides.items():
                if v is not None:
                    merged[k] = v
        return merged

    def build_help_text(self) -> str:
        """Auto-generate tooltip help text directly from algorithm metadata (R3.2)."""
        lines = [f"**{self.name}** (v{self.version} / {self.market.value})", self.description]
        if self.params_schema:
            lines.append("")
            lines.append("주요 파라미터:")
            for spec in self.params_schema[:5]:
                lines.append(f"- {spec.label}: {spec.description} (기본값: {spec.default})")
        return "\n".join(lines)

    @abstractmethod
    def evaluate_entry(
        self,
        symbol: str,
        data: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        """Evaluate whether to enter (or add to) a position in `symbol`."""

    @abstractmethod
    def evaluate_exit(
        self,
        lot: Lot,
        data: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        """Evaluate whether to partially or fully exit an existing `lot`."""
