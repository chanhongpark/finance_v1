"""
UserContext-injected personal Google Sheet manager (R1.2, R1.3, R2.1, R2.4).

Features:
- Strict UserContext sheet_id isolation (no global singletons).
- Automatic schema verification & non-destructive column migration on startup.
- TTL read cache to stay well under Google Sheets 60 reads/min quota.
- Batch append/update with quota retry backoff & local pending write queue
  saved under data/<user_id>/pending_writes.json.
- Pluggable backend protocol (GSpreadBackend for production, InMemorySheetBackend for tests/local).
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.context import UserContext, UserSettings
from core.ledger.models import AuditRecord, Fill, Lot, SignalRecord, utc_now_iso
from core.sheets.schema import SCHEMA_VERSION, SHEET_SCHEMAS, compute_missing_columns

logger = logging.getLogger(__name__)


class SheetQuotaError(RuntimeError):
    """Raised when Google Sheets API returns 429 / quota exceeded."""


class SheetBackend(ABC):
    """Abstract worksheet backend for GSpread or in-memory testing."""

    @abstractmethod
    def ensure_worksheet(self, tab_name: str, headers: List[str]) -> None:
        pass

    @abstractmethod
    def read_records(self, tab_name: str) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def append_rows(self, tab_name: str, rows: List[List[Any]]) -> None:
        pass

    @abstractmethod
    def overwrite_table(self, tab_name: str, headers: List[str], rows: List[List[Any]]) -> None:
        pass


class InMemorySheetBackend(SheetBackend):
    """In-memory backend for unit/E2E tests and offline local simulation."""

    def __init__(self, sheet_id: str) -> None:
        self.sheet_id = sheet_id
        self.headers: Dict[str, List[str]] = {}
        self.tables: Dict[str, List[Dict[str, Any]]] = {}

    def ensure_worksheet(self, tab_name: str, headers: List[str]) -> None:
        if tab_name not in self.headers:
            self.headers[tab_name] = list(headers)
            self.tables[tab_name] = []
        else:
            missing = compute_missing_columns(tab_name, self.headers[tab_name])
            if missing:
                self.headers[tab_name].extend(missing)
                for row in self.tables[tab_name]:
                    for col in missing:
                        row.setdefault(col, "")

    def read_records(self, tab_name: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.tables.get(tab_name, [])]

    def append_rows(self, tab_name: str, rows: List[List[Any]]) -> None:
        headers = self.headers[tab_name]
        for row_vals in rows:
            record = {
                headers[i]: (row_vals[i] if i < len(row_vals) else "")
                for i in range(len(headers))
            }
            self.tables[tab_name].append(record)

    def overwrite_table(self, tab_name: str, headers: List[str], rows: List[List[Any]]) -> None:
        self.headers[tab_name] = list(headers)
        self.tables[tab_name] = [
            {headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers))}
            for r in rows
        ]


class GSpreadBackend(SheetBackend):
    """Production Google Sheets backend via gspread + google-auth."""

    def __init__(self, sheet_id: str, service_account_info: Dict[str, Any]) -> None:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(sheet_id)
        self._ws_cache: Dict[str, Any] = {}

    def _call_with_retry(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        delays = [1.0, 2.0, 4.0]
        last_exc: Optional[Exception] = None
        for attempt, delay in enumerate(delays, start=1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "429" in msg or "quota" in msg or "rate limit" in msg:
                    logger.warning("Sheets quota hit (attempt %d/%d), retrying in %.1fs", attempt, len(delays), delay)
                    time.sleep(delay)
                    continue
                raise
        raise SheetQuotaError(f"Google Sheets API quota exceeded after retries: {last_exc}") from last_exc

    def _get_ws(self, tab_name: str) -> Any:
        if tab_name not in self._ws_cache:
            self._ws_cache[tab_name] = self._call_with_retry(self._spreadsheet.worksheet, tab_name)
        return self._ws_cache[tab_name]

    def ensure_worksheet(self, tab_name: str, headers: List[str]) -> None:
        import gspread

        try:
            ws = self._get_ws(tab_name)
            existing = self._call_with_retry(ws.row_values, 1)
            if not existing:
                self._call_with_retry(ws.update, "A1", [headers])
            else:
                missing = compute_missing_columns(tab_name, existing)
                if missing:
                    new_headers = existing + missing
                    self._call_with_retry(ws.update, "A1", [new_headers])
        except gspread.WorksheetNotFound:
            ws = self._call_with_retry(
                self._spreadsheet.add_worksheet,
                title=tab_name,
                rows=1000,
                cols=max(len(headers) + 2, 10),
            )
            self._ws_cache[tab_name] = ws
            self._call_with_retry(ws.update, "A1", [headers])

    def read_records(self, tab_name: str) -> List[Dict[str, Any]]:
        ws = self._get_ws(tab_name)
        return self._call_with_retry(ws.get_all_records)

    def append_rows(self, tab_name: str, rows: List[List[Any]]) -> None:
        if not rows:
            return
        ws = self._get_ws(tab_name)
        self._call_with_retry(ws.append_rows, rows, value_input_option="USER_ENTERED")

    def overwrite_table(self, tab_name: str, headers: List[str], rows: List[List[Any]]) -> None:
        ws = self._get_ws(tab_name)
        self._call_with_retry(ws.clear)
        payload = [headers] + rows
        self._call_with_retry(ws.update, "A1", payload, value_input_option="USER_ENTERED")


class UserSheetManager:
    """
    Personal Google Sheet manager bound strictly to one UserContext.
    """

    def __init__(
        self,
        ctx: UserContext,
        backend: Optional[SheetBackend] = None,
        cache_ttl_seconds: float = 30.0,
        target_sheet_id: Optional[str] = None,
    ) -> None:
        effective_sheet_id = target_sheet_id if target_sheet_id is not None else ctx.sheet_id
        ctx.assert_sheet_access(effective_sheet_id)

        self.ctx = ctx
        self.sheet_id = effective_sheet_id
        self.cache_ttl_seconds = cache_ttl_seconds
        self._read_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._pending_queue_path: Path = ctx.resolve_user_path("pending_writes.json")

        if backend is not None:
            self._backend = backend
        elif ctx.credentials.gcp_service_account:
            self._backend = GSpreadBackend(self.sheet_id, ctx.credentials.gcp_service_account)
        else:
            logger.info("No GCP service account configured for '%s'; using InMemorySheetBackend", ctx.user_id)
            self._backend = InMemorySheetBackend(self.sheet_id)

        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Verify all 6 required tabs and schema_version on startup (R2.1)."""
        for tab_name, headers in SHEET_SCHEMAS.items():
            self._backend.ensure_worksheet(tab_name, headers)

        settings = self.load_raw_settings()
        if settings.get("schema_version") != SCHEMA_VERSION:
            self.save_setting("schema_version", SCHEMA_VERSION)

    def invalidate_cache(self, tab_name: Optional[str] = None) -> None:
        if tab_name:
            self._read_cache.pop(tab_name, None)
        else:
            self._read_cache.clear()

    def _read_tab(self, tab_name: str, use_cache: bool = True) -> List[Dict[str, Any]]:
        now = time.monotonic()
        if use_cache and tab_name in self._read_cache:
            ts, cached = self._read_cache[tab_name]
            if (now - ts) < self.cache_ttl_seconds:
                return [dict(r) for r in cached]
        records = self._backend.read_records(tab_name)
        self._read_cache[tab_name] = (now, [dict(r) for r in records])
        return records

    def _enqueue_failed_write(self, op: str, tab_name: str, payload: Any) -> None:
        queue: List[Dict[str, Any]] = []
        if self._pending_queue_path.exists():
            try:
                queue = json.loads(self._pending_queue_path.read_text(encoding="utf-8"))
            except Exception:
                queue = []
        queue.append({"ts": utc_now_iso(), "op": op, "tab": tab_name, "payload": payload})
        self._pending_queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")

    def flush_pending_writes(self) -> int:
        """Retry any writes previously queued due to quota/network errors."""
        if not self._pending_queue_path.exists():
            return 0
        try:
            queue = json.loads(self._pending_queue_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        if not queue:
            return 0

        remaining: List[Dict[str, Any]] = []
        flushed = 0
        for item in queue:
            try:
                if item["op"] == "append_rows":
                    self._backend.append_rows(item["tab"], item["payload"])
                    self.invalidate_cache(item["tab"])
                    flushed += 1
            except Exception as exc:
                logger.warning("Failed to flush pending write for %s: %s", item["tab"], exc)
                remaining.append(item)

        self._pending_queue_path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
        return flushed

    def _safe_append(self, tab_name: str, dicts: List[Dict[str, Any]]) -> None:
        if not dicts:
            return
        headers = SHEET_SCHEMAS[tab_name]
        rows = [[d.get(col, "") for col in headers] for d in dicts]
        try:
            self._backend.append_rows(tab_name, rows)
            self.invalidate_cache(tab_name)
        except Exception as exc:
            logger.error("Sheet append failed for %s (%s); queuing locally", tab_name, exc)
            self._enqueue_failed_write("append_rows", tab_name, rows)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def load_raw_settings(self, use_cache: bool = True) -> Dict[str, str]:
        rows = self._read_tab("Settings", use_cache=use_cache)
        return {str(r.get("key", "")): str(r.get("value", "")) for r in rows if r.get("key")}

    def load_user_settings(self) -> UserSettings:
        raw = self.load_raw_settings()
        merged = self.ctx.settings.to_dict()
        merged.update(raw)
        return UserSettings.from_dict(merged)

    def save_setting(self, key: str, value: Any) -> None:
        settings = self.load_raw_settings(use_cache=False)
        settings[str(key)] = str(value)
        ts = utc_now_iso()
        headers = SHEET_SCHEMAS["Settings"]
        rows = [[k, v, ts] for k, v in sorted(settings.items())]
        self._backend.overwrite_table("Settings", headers, rows)
        self.invalidate_cache("Settings")

    def save_user_settings(self, settings: UserSettings) -> None:
        raw = self.load_raw_settings(use_cache=False)
        for k, v in settings.to_dict().items():
            raw[str(k)] = str(v)
        ts = utc_now_iso()
        headers = SHEET_SCHEMAS["Settings"]
        rows = [[k, v, ts] for k, v in sorted(raw.items())]
        self._backend.overwrite_table("Settings", headers, rows)
        self.invalidate_cache("Settings")

    # ------------------------------------------------------------------
    # Algorithms
    # ------------------------------------------------------------------
    def load_algorithm_configs(self, use_cache: bool = True) -> Dict[str, Dict[str, Any]]:
        rows = self._read_tab("Algorithms", use_cache=use_cache)
        result: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            algo_id = str(r.get("algo_id", "")).strip()
            if not algo_id:
                continue
            enabled_raw = str(r.get("enabled", "true")).strip().lower()
            enabled = enabled_raw in {"true", "1", "yes", "y", "on"}
            params_raw = r.get("params_json", "{}")
            try:
                params = json.loads(params_raw) if isinstance(params_raw, str) and params_raw else {}
            except json.JSONDecodeError:
                params = {}
            result[algo_id] = {"enabled": enabled, "params": params}
        return result

    def save_algorithm_config(self, algo_id: str, enabled: bool, params: Dict[str, Any]) -> None:
        configs = self.load_algorithm_configs(use_cache=False)
        configs[algo_id] = {"enabled": bool(enabled), "params": dict(params)}
        ts = utc_now_iso()
        headers = SHEET_SCHEMAS["Algorithms"]
        rows = [
            [aid, str(cfg["enabled"]).lower(), json.dumps(cfg["params"], ensure_ascii=False), ts]
            for aid, cfg in sorted(configs.items())
        ]
        self._backend.overwrite_table("Algorithms", headers, rows)
        self.invalidate_cache("Algorithms")

    # ------------------------------------------------------------------
    # Lots & Fills (Append-only Fills, Upsert Lots)
    # ------------------------------------------------------------------
    def load_lots(self, use_cache: bool = True) -> List[Lot]:
        rows = self._read_tab("Lots", use_cache=use_cache)
        return [Lot.from_sheet_row(r) for r in rows if r.get("lot_id")]

    def save_lots(self, lots: List[Lot]) -> None:
        headers = SHEET_SCHEMAS["Lots"]
        rows = [[lot.to_sheet_row().get(col, "") for col in headers] for lot in lots]
        self._backend.overwrite_table("Lots", headers, rows)
        self.invalidate_cache("Lots")

    def upsert_lots(self, updated_lots: List[Lot]) -> List[Lot]:
        existing = {lot.lot_id: lot for lot in self.load_lots(use_cache=False)}
        for lot in updated_lots:
            existing[lot.lot_id] = lot
        merged = sorted(existing.values(), key=lambda x: (x.buy_date, x.lot_id))
        self.save_lots(merged)
        return merged

    def load_fills(self, use_cache: bool = True) -> List[Fill]:
        rows = self._read_tab("Fills", use_cache=use_cache)
        return [Fill.from_sheet_row(r) for r in rows if r.get("fill_id")]

    def append_fills(self, fills: List[Fill]) -> None:
        """Append-only ledger write (R2.2)."""
        self._safe_append("Fills", [f.to_sheet_row() for f in fills])

    # ------------------------------------------------------------------
    # Signals & Audit (Append-only)
    # ------------------------------------------------------------------
    def load_signals(self, use_cache: bool = True) -> List[Dict[str, Any]]:
        return self._read_tab("Signals", use_cache=use_cache)

    def append_signals(self, signals: List[SignalRecord]) -> None:
        self._safe_append("Signals", [s.to_sheet_row() for s in signals])

    def load_audits(self, use_cache: bool = True) -> List[Dict[str, Any]]:
        return self._read_tab("Audit", use_cache=use_cache)

    def append_audit(self, record: AuditRecord) -> None:
        self._safe_append("Audit", [record.to_sheet_row()])
