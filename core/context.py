"""
UserContext and per-user data/path isolation (R1.2, R1.4, R1.6).

No module-level global user config or singleton is allowed.
All services (KIS client, Sheet manager, algorithms, risk manager, notifier)
must receive an explicit UserContext instance.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


class UserIsolationError(PermissionError):
    """Raised when a service attempts to access another user's sheet or local path."""


class ExecutionMode(str, Enum):
    PROPOSE_ONLY = "PROPOSE_ONLY"     # 제안만 (수동 승인/주문)
    SEMI_AUTO = "SEMI_AUTO"           # 반자동 (UI 확인 후 주문)
    FULL_AUTO = "FULL_AUTO"           # 완전자동 (스케줄러/액션 자동 주문)


@dataclass(frozen=True)
class UserCredentials:
    """User-scoped sensitive credentials loaded strictly from Secrets/ENV (never from Sheet)."""
    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_account_no: str = ""
    is_mock: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    gcp_service_account: Optional[Dict[str, Any]] = None

    @property
    def account_prefix(self) -> str:
        """8-digit KIS CANO."""
        cleaned = self.kis_account_no.replace("-", "").strip()
        return cleaned[:8] if len(cleaned) >= 8 else cleaned

    @property
    def account_suffix(self) -> str:
        """2-digit KIS ACNT_PRDT_CD."""
        cleaned = self.kis_account_no.replace("-", "").strip()
        return cleaned[8:10] if len(cleaned) >= 10 else "01"


@dataclass
class UserSettings:
    """User-scoped runtime & risk settings (defaults in code, overrides in personal Sheet)."""
    execution_mode: ExecutionMode = ExecutionMode.SEMI_AUTO
    max_daily_buy_amount_krw: float = 3_000_000.0
    max_daily_buy_amount_usd: float = 2_000.0
    max_symbol_weight_pct: float = 0.25          # 종목당 최대 비중 (25%)
    max_algo_budget_krw: float = 10_000_000.0    # 알고리즘당 최대 투입금 (KRW)
    max_algo_budget_usd: float = 20_000.0        # 알고리즘당 최대 투입금 (USD)
    max_concurrent_symbols: int = 10             # 동시 보유 종목 수 상한
    circuit_breaker_daily_loss_pct: float = -0.05  # 일일 계좌 손실 서킷브레이커 (-5%)
    global_hard_stop_loss_pct: float = -0.10     # 공통 강제 손절 (risk_override, -10%)
    allowed_email: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UserSettings":
        mode_raw = str(data.get("execution_mode", ExecutionMode.SEMI_AUTO.value)).upper()
        try:
            mode = ExecutionMode(mode_raw)
        except ValueError:
            mode = ExecutionMode.SEMI_AUTO
        known = {
            "execution_mode",
            "max_daily_buy_amount_krw",
            "max_daily_buy_amount_usd",
            "max_symbol_weight_pct",
            "max_algo_budget_krw",
            "max_algo_budget_usd",
            "max_concurrent_symbols",
            "circuit_breaker_daily_loss_pct",
            "global_hard_stop_loss_pct",
            "allowed_email",
        }
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            execution_mode=mode,
            max_daily_buy_amount_krw=float(data.get("max_daily_buy_amount_krw", 3_000_000.0)),
            max_daily_buy_amount_usd=float(data.get("max_daily_buy_amount_usd", 2_000.0)),
            max_symbol_weight_pct=float(data.get("max_symbol_weight_pct", 0.25)),
            max_algo_budget_krw=float(data.get("max_algo_budget_krw", 10_000_000.0)),
            max_algo_budget_usd=float(data.get("max_algo_budget_usd", 20_000.0)),
            max_concurrent_symbols=int(data.get("max_concurrent_symbols", 10)),
            circuit_breaker_daily_loss_pct=float(data.get("circuit_breaker_daily_loss_pct", -0.05)),
            global_hard_stop_loss_pct=float(data.get("global_hard_stop_loss_pct", -0.10)),
            allowed_email=str(data.get("allowed_email", "")),
            extra=extra,
        )

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "execution_mode": self.execution_mode.value,
            "max_daily_buy_amount_krw": self.max_daily_buy_amount_krw,
            "max_daily_buy_amount_usd": self.max_daily_buy_amount_usd,
            "max_symbol_weight_pct": self.max_symbol_weight_pct,
            "max_algo_budget_krw": self.max_algo_budget_krw,
            "max_algo_budget_usd": self.max_algo_budget_usd,
            "max_concurrent_symbols": self.max_concurrent_symbols,
            "circuit_breaker_daily_loss_pct": self.circuit_breaker_daily_loss_pct,
            "global_hard_stop_loss_pct": self.global_hard_stop_loss_pct,
            "allowed_email": self.allowed_email,
        }
        result.update(self.extra)
        return result


def sanitize_user_id(user_id: str) -> str:
    """Convert user_id (e.g., email or alias) into a safe single directory name."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", user_id.strip())
    if not cleaned or cleaned in {".", ".."} or ".." in cleaned:
        raise ValueError(f"Invalid user_id for directory isolation: {user_id!r}")
    return cleaned


@dataclass
class UserContext:
    """
    Core per-user execution context injected into all services (R1.2).
    """
    user_id: str
    credentials: UserCredentials
    sheet_id: str
    settings: UserSettings = field(default_factory=UserSettings)
    base_data_dir: Path = field(default_factory=lambda: Path("data"))

    def __post_init__(self) -> None:
        if not self.user_id or not self.user_id.strip():
            raise ValueError("user_id must not be empty")
        self.base_data_dir = Path(self.base_data_dir)

    @property
    def safe_user_id(self) -> str:
        return sanitize_user_id(self.user_id)

    @property
    def is_mock(self) -> bool:
        return self.credentials.is_mock

    @property
    def mode_label(self) -> str:
        return "모의투자 (VTS)" if self.is_mock else "실전투자 (REAL)"

    def get_user_data_dir(self, create: bool = True) -> Path:
        """Return isolated local directory: data/<user_id>/ (R1.6)."""
        user_dir = (self.base_data_dir / self.safe_user_id).resolve()
        if create:
            user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def resolve_user_path(self, relative_path: str | Path, create_parent: bool = True) -> Path:
        """
        Resolve a file path strictly inside this user's data/<user_id>/ directory.
        Raises UserIsolationError on path traversal or cross-user path access.
        """
        user_dir = self.get_user_data_dir(create=create_parent)
        target = (user_dir / relative_path).resolve()
        try:
            target.relative_to(user_dir)
        except ValueError as exc:
            raise UserIsolationError(
                f"User '{self.user_id}' attempted to access path outside own directory: {relative_path}"
            ) from exc
        if create_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def assert_sheet_access(self, target_sheet_id: str) -> None:
        """Enforce that this UserContext can only access its own sheet_id (R1.3, R10)."""
        if not target_sheet_id or target_sheet_id != self.sheet_id:
            raise UserIsolationError(
                f"User '{self.user_id}' is not authorized to access sheet '{target_sheet_id}'"
            )


def _parse_bool(val: Any, default: bool = True) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_gcp_sa(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def load_user_context_from_mapping(
    source: Mapping[str, Any],
    base_data_dir: Path | str = Path("data"),
) -> UserContext:
    """
    Build UserContext from a 1-user-1-set secrets mapping (Streamlit st.secrets or env dict).
    """
    user_id = str(source.get("USER_ID", "default_user")).strip() or "default_user"
    sheet_id = str(source.get("SHEET_ID", "")).strip()

    gcp_sa = _parse_gcp_sa(
        source.get("gcp_service_account") or source.get("GCP_SERVICE_ACCOUNT_JSON")
    )

    creds = UserCredentials(
        kis_app_key=str(source.get("KIS_APP_KEY", "")).strip(),
        kis_app_secret=str(source.get("KIS_APP_SECRET", "")).strip(),
        kis_account_no=str(source.get("KIS_ACCOUNT_NO", "")).strip(),
        is_mock=_parse_bool(source.get("IS_MOCK"), default=True),
        telegram_bot_token=str(source.get("TELEGRAM_BOT_TOKEN", "")).strip(),
        telegram_chat_id=str(source.get("TELEGRAM_CHAT_ID", "")).strip(),
        gcp_service_account=gcp_sa,
    )

    settings = UserSettings(
        allowed_email=str(source.get("ALLOWED_EMAIL", "")).strip(),
    )

    return UserContext(
        user_id=user_id,
        credentials=creds,
        sheet_id=sheet_id,
        settings=settings,
        base_data_dir=Path(base_data_dir),
    )


def load_user_context_from_env(base_data_dir: Path | str = Path("data")) -> UserContext:
    """Load 1-user-1-set UserContext from OS environment variables (GitHub Actions / CLI)."""
    return load_user_context_from_mapping(os.environ, base_data_dir=base_data_dir)
