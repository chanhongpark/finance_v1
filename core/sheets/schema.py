"""
Fixed personal Google Sheet schema and version verification/migration (R2.1).
"""
from __future__ import annotations

from typing import Dict, List

SCHEMA_VERSION = "1.0.0"

SHEET_SCHEMAS: Dict[str, List[str]] = {
    "Settings": [
        "key",
        "value",
        "updated_at",
    ],
    "Algorithms": [
        "algo_id",
        "enabled",
        "params_json",
        "updated_at",
    ],
    "Lots": [
        "lot_id",
        "symbol",
        "market",
        "algo_id",
        "algo_version",
        "buy_date",
        "qty",
        "avg_price",
        "status",
        "params_snapshot",
        "algo_state_json",
        "entry_conditions_json",
        "note",
    ],
    "Fills": [
        "fill_id",
        "lot_id",
        "side",
        "date",
        "qty",
        "price",
        "fee",
        "tax",
        "order_no",
        "note",
    ],
    "Signals": [
        "ts",
        "symbol",
        "algo_id",
        "algo_version",
        "action",
        "conditions_json",
        "decision",
        "score",
        "reason_text",
    ],
    "Audit": [
        "ts",
        "actor",
        "action",
        "detail",
    ],
}


def compute_missing_columns(tab_name: str, existing_headers: List[str]) -> List[str]:
    """Return any required columns missing from an existing worksheet header row."""
    expected = SHEET_SCHEMAS.get(tab_name, [])
    existing_set = {h.strip() for h in existing_headers if h}
    return [col for col in expected if col not in existing_set]
