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
from typing import Any, Dict, List, Optional, Set, Tuple

from core.context import UserContext, UserSettings
from core.ledger.models import AuditRecord, Fill, Lot, PendingOrder, SignalRecord, normalize_date_str, utc_now_iso
from core.sheets.schema import SCHEMA_VERSION, SHEET_SCHEMAS, compute_missing_columns

logger = logging.getLogger(__name__)


class SheetQuotaError(RuntimeError):
    """Raised when Google Sheets API returns 429 / quota exceeded."""


def _col_letter(col_idx: int) -> str:
    """Convert 1-based column index to A1 column letter (1 -> 'A', 27 -> 'AA')."""
    result = []
    n = max(1, int(col_idx))
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result.append(chr(ord("A") + rem))
    return "".join(reversed(result))


class SheetBackend(ABC):
    """Abstract worksheet backend for GSpread or in-memory testing."""

    @abstractmethod
    def ensure_worksheet(self, tab_name: str, headers: List[str]) -> None:
        pass

    @abstractmethod
    def get_headers(self, tab_name: str) -> List[str]:
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

    @abstractmethod
    def upsert_rows_by_key(self, tab_name: str, key_col: str, records: List[Dict[str, Any]]) -> None:
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

    def get_headers(self, tab_name: str) -> List[str]:
        return list(self.headers.get(tab_name, []))

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

    def upsert_rows_by_key(self, tab_name: str, key_col: str, records: List[Dict[str, Any]]) -> None:
        if not records:
            return
        headers = self.headers[tab_name]
        table = self.tables.setdefault(tab_name, [])
        index_by_key = {str(r.get(key_col, "")): idx for idx, r in enumerate(table)}
        for rec in records:
            key = str(rec.get(key_col, ""))
            if not key:
                continue
            if key in index_by_key:
                # Preserve user-added custom columns not present in rec
                existing_row = dict(table[index_by_key[key]])
                for col in headers:
                    if col in rec:
                        existing_row[col] = rec[col]
                table[index_by_key[key]] = existing_row
            else:
                row_dict = {col: rec.get(col, "") for col in headers}
                index_by_key[key] = len(table)
                table.append(row_dict)


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
        self._headers_cache: Dict[str, List[str]] = {}

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
                self._call_with_retry(ws.update, "A1", [headers], value_input_option="RAW")
                self._headers_cache[tab_name] = list(headers)
            else:
                missing = compute_missing_columns(tab_name, existing)
                if missing:
                    new_headers = existing + missing
                    self._call_with_retry(ws.update, "A1", [new_headers], value_input_option="RAW")
                    self._headers_cache[tab_name] = list(new_headers)
                else:
                    self._headers_cache[tab_name] = list(existing)
        except gspread.WorksheetNotFound:
            ws = self._call_with_retry(
                self._spreadsheet.add_worksheet,
                title=tab_name,
                rows=1000,
                cols=max(len(headers) + 2, 10),
            )
            self._ws_cache[tab_name] = ws
            self._call_with_retry(ws.update, "A1", [headers], value_input_option="RAW")
            self._headers_cache[tab_name] = list(headers)

    def get_headers(self, tab_name: str) -> List[str]:
        if tab_name not in self._headers_cache or not self._headers_cache[tab_name]:
            ws = self._get_ws(tab_name)
            self._headers_cache[tab_name] = list(self._call_with_retry(ws.row_values, 1))
        return list(self._headers_cache[tab_name])

    def read_records(self, tab_name: str) -> List[Dict[str, Any]]:
        ws = self._get_ws(tab_name)
        # Preserve leading zeros (KR symbols like "005930", order_no) and ISO date strings
        return self._call_with_retry(ws.get_all_records, numericise_ignore=["all"])

    def append_rows(self, tab_name: str, rows: List[List[Any]]) -> None:
        if not rows:
            return
        ws = self._get_ws(tab_name)
        # Use RAW so "005930", "2026-11-27", and notes starting with =/+/- are stored verbatim
        self._call_with_retry(ws.append_rows, rows, value_input_option="RAW")

    def overwrite_table(self, tab_name: str, headers: List[str], rows: List[List[Any]]) -> None:
        ws = self._get_ws(tab_name)
        prev_row_count = len(self._call_with_retry(ws.col_values, 1))
        payload = [headers] + rows
        # Overwrite in place first without clearing, so a mid-write failure never wipes the tab
        self._call_with_retry(ws.update, "A1", payload, value_input_option="RAW")
        self._headers_cache[tab_name] = list(headers)
        if prev_row_count > len(payload):
            end_col = _col_letter(max(len(headers), 1))
            trailing_range = f"A{len(payload) + 1}:{end_col}{prev_row_count}"
            self._call_with_retry(ws.batch_clear, [trailing_range])

    def upsert_rows_by_key(self, tab_name: str, key_col: str, records: List[Dict[str, Any]]) -> None:
        if not records:
            return
        ws = self._get_ws(tab_name)
        headers = self.get_headers(tab_name)
        if key_col not in headers:
            raise ValueError(f"Key column '{key_col}' not found in worksheet '{tab_name}' headers: {headers}")
        key_col_idx = headers.index(key_col) + 1
        existing_keys = self._call_with_retry(ws.col_values, key_col_idx)
        # Row 1 is header; data rows start at row 2
        row_num_by_key: Dict[str, int] = {}
        for row_idx, val in enumerate(existing_keys[1:], start=2):
            val_str = str(val).strip()
            if val_str:
                row_num_by_key[val_str] = row_idx

        batch_updates: List[Dict[str, Any]] = []
        to_append: List[List[Any]] = []

        for rec in records:
            key = str(rec.get(key_col, "")).strip()
            if not key:
                continue
            if key in row_num_by_key:
                row_num = row_num_by_key[key]
                # Only update contiguous spans of columns present in rec so user-added custom columns stay intact
                managed_indices = [i for i, col in enumerate(headers) if col in rec]
                if not managed_indices:
                    continue
                span_start = managed_indices[0]
                span_prev = managed_indices[0]
                for idx in managed_indices[1:] + [None]:
                    if idx is not None and idx == span_prev + 1:
                        span_prev = idx
                        continue
                    start_col = _col_letter(span_start + 1)
                    end_col = _col_letter(span_prev + 1)
                    vals = [rec[headers[c]] for c in range(span_start, span_prev + 1)]
                    batch_updates.append({"range": f"{start_col}{row_num}:{end_col}{row_num}", "values": [vals]})
                    if idx is not None:
                        span_start = idx
                        span_prev = idx
            else:
                to_append.append([rec.get(col, "") for col in headers])

        if batch_updates:
            self._call_with_retry(ws.batch_update, batch_updates, value_input_option="RAW")
        if to_append:
            self._call_with_retry(ws.append_rows, to_append, value_input_option="RAW")


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

    @property
    def is_in_memory(self) -> bool:
        return isinstance(self._backend, InMemorySheetBackend)

    def ensure_schema(self) -> None:
        """Verify all required tabs, backfill legacy PendingOrders keys, and update schema_version (R2.1)."""
        for tab_name, headers in SHEET_SCHEMAS.items():
            self._backend.ensure_worksheet(tab_name, headers)

        self._backfill_pending_order_keys()

        settings = self.load_raw_settings()
        if settings.get("schema_version") != SCHEMA_VERSION:
            self.save_setting("schema_version", SCHEMA_VERSION)

    def _backfill_pending_order_keys(self) -> None:
        """One-time self-healing migration: populate blank order_key on legacy PendingOrders rows."""
        records = self._backend.read_records("PendingOrders")
        if not records:
            return
        needs_migration = any(
            str(r.get("order_no", "")).strip() and not str(r.get("order_key", "")).strip()
            for r in records
        )
        if not needs_migration:
            return
        headers = self._backend.get_headers("PendingOrders") or SHEET_SCHEMAS["PendingOrders"]
        migrated_rows: List[List[Any]] = []
        for r in records:
            row_copy = dict(r)
            if str(row_copy.get("order_no", "")).strip() and not str(row_copy.get("order_key", "")).strip():
                ord_dt = normalize_date_str(str(row_copy.get("order_date", "")))
                ord_no = str(row_copy.get("order_no", "")).strip()
                row_copy["order_key"] = f"{ord_dt}_{ord_no}"
            migrated_rows.append([row_copy.get(col, "") for col in headers])
        self._backend.overwrite_table("PendingOrders", headers, migrated_rows)
        self.invalidate_cache("PendingOrders")

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

    def _enqueue_failed_write(self, op: str, tab_name: str, payload: Any, **extra: Any) -> None:
        queue: List[Dict[str, Any]] = []
        if self._pending_queue_path.exists():
            try:
                queue = json.loads(self._pending_queue_path.read_text(encoding="utf-8"))
            except Exception:
                queue = []
        entry: Dict[str, Any] = {"ts": utc_now_iso(), "op": op, "tab": tab_name, "payload": payload}
        entry.update(extra)
        queue.append(entry)
        self._pending_queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")

    def has_pending_writes(self) -> bool:
        if not self._pending_queue_path.exists():
            return False
        try:
            queue = json.loads(self._pending_queue_path.read_text(encoding="utf-8"))
            return bool(queue)
        except Exception as exc:
            # Fail-closed: treat corrupted queue file as pending so reconciliation does not double-count
            logger.error("Corrupted pending_writes queue at %s (%s); treating as pending", self._pending_queue_path, exc)
            return True

    def flush_pending_writes(self) -> int:
        """Retry queued writes in strict FIFO order (stop on first failure) with Fill deduplication."""
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
        for idx, item in enumerate(queue):
            try:
                op = item.get("op")
                tab = item["tab"]
                if op == "append_rows":
                    payload = item["payload"]
                    headers = self._backend.get_headers(tab) or SHEET_SCHEMAS[tab]
                    if tab == "Fills":
                        # Deduplicate by fill_id so timeout retries / concurrent tabs never double-record
                        existing_records = self._backend.read_records("Fills")
                        existing_ids = {
                            str(r.get("fill_id", "")).strip()
                            for r in existing_records
                            if r.get("fill_id")
                        }
                        if payload and isinstance(payload[0], dict):
                            payload = [
                                d for d in payload
                                if str(d.get("fill_id", "")).strip() not in existing_ids
                            ]
                        elif payload and "fill_id" in headers:
                            fid_idx = headers.index("fill_id")
                            payload = [
                                r for r in payload
                                if fid_idx >= len(r) or str(r[fid_idx]).strip() not in existing_ids
                            ]
                    if payload:
                        if isinstance(payload[0], dict):
                            rows = [[d.get(col, "") for col in headers] for d in payload]
                        else:
                            rows = payload
                        self._backend.append_rows(tab, rows)
                        self.invalidate_cache(tab)
                    flushed += 1
                elif op == "upsert_rows":
                    key_col = str(item.get("key_col", "lot_id"))
                    self._backend.upsert_rows_by_key(tab, key_col, item["payload"])
                    self.invalidate_cache(tab)
                    flushed += 1
            except Exception as exc:
                logger.warning("Failed to flush pending write for %s (preserving FIFO order): %s", item.get("tab"), exc)
                # Preserve strict FIFO order: keep this item and all subsequent items
                remaining.extend(queue[idx:])
                break

        self._pending_queue_path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
        return flushed

    def _safe_append(self, tab_name: str, dicts: List[Dict[str, Any]]) -> bool:
        if not dicts:
            return True
        headers = self._backend.get_headers(tab_name) or SHEET_SCHEMAS[tab_name]
        rows = [[d.get(col, "") for col in headers] for d in dicts]
        try:
            self._backend.append_rows(tab_name, rows)
            self.invalidate_cache(tab_name)
            return True
        except Exception as exc:
            logger.error("Sheet append failed for %s (%s); queuing locally", tab_name, exc)
            self._enqueue_failed_write("append_rows", tab_name, dicts)
            return False

    def _safe_upsert(self, tab_name: str, key_col: str, dicts: List[Dict[str, Any]]) -> bool:
        if not dicts:
            return True
        try:
            self._backend.upsert_rows_by_key(tab_name, key_col, dicts)
            self.invalidate_cache(tab_name)
            return True
        except Exception as exc:
            logger.error("Sheet upsert failed for %s (%s); queuing locally", tab_name, exc)
            self._enqueue_failed_write("upsert_rows", tab_name, dicts, key_col=key_col)
            return False

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
        headers = self._backend.get_headers("Lots") or SHEET_SCHEMAS["Lots"]
        rows = [[lot.to_sheet_row().get(col, "") for col in headers] for lot in lots]
        self._backend.overwrite_table("Lots", headers, rows)
        self.invalidate_cache("Lots")

    def upsert_lots(self, updated_lots: List[Lot], queue_only: bool = False) -> bool:
        """Row-level upsert by lot_id without unnecessary full-table reload."""
        dicts = [lot.to_sheet_row() for lot in updated_lots]
        if not dicts:
            return True
        if queue_only:
            self._enqueue_failed_write("upsert_rows", "Lots", dicts, key_col="lot_id")
            return False
        return self._safe_upsert("Lots", "lot_id", dicts)

    def load_fills(self, use_cache: bool = True) -> List[Fill]:
        rows = self._read_tab("Fills", use_cache=use_cache)
        seen: Set[str] = set()
        result: List[Fill] = []
        for r in rows:
            fid = str(r.get("fill_id", "")).strip()
            if not fid or fid in seen:
                continue
            seen.add(fid)
            result.append(Fill.from_sheet_row(r))
        return result

    def append_fills(self, fills: List[Fill], known_fill_ids: Optional[Set[str]] = None) -> bool:
        """Append-only ledger write with fill_id deduplication (R2.2). Reuses known_fill_ids to save reads."""
        if not fills:
            return True
        deduped = list(fills)
        if known_fill_ids is not None:
            existing_ids = {str(fid).strip() for fid in known_fill_ids if fid}
            deduped = [f for f in fills if str(f.fill_id).strip() not in existing_ids]
        else:
            try:
                existing_ids = {
                    str(r.get("fill_id", "")).strip()
                    for r in self._backend.read_records("Fills")
                    if r.get("fill_id")
                }
                deduped = [f for f in fills if str(f.fill_id).strip() not in existing_ids]
            except Exception as exc:
                logger.warning("Could not pre-check existing fill_ids before append (%s); proceeding safely", exc)
        if not deduped:
            return True
        return self._safe_append("Fills", [f.to_sheet_row() for f in deduped])

    # ------------------------------------------------------------------
    # PendingOrders (Upsert by composite order_key for deferred/partial fill settlement)
    # ------------------------------------------------------------------
    def load_pending_orders(self, use_cache: bool = True) -> List[PendingOrder]:
        rows = self._read_tab("PendingOrders", use_cache=use_cache)
        return [PendingOrder.from_sheet_row(r) for r in rows if r.get("order_no")]

    def upsert_pending_orders(self, orders: List[PendingOrder], queue_only: bool = False) -> bool:
        dicts = [o.to_sheet_row() for o in orders]
        if not dicts:
            return True
        if queue_only:
            self._enqueue_failed_write("upsert_rows", "PendingOrders", dicts, key_col="order_key")
            return False
        return self._safe_upsert("PendingOrders", "order_key", dicts)

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
