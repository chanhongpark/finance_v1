"""
GitHub Actions CLI entry point for US Infinite Buying automated execution (R1.5, R11, R15).

- Reads 1-user-1-set credentials strictly from GitHub Repository Secrets (env vars).
- Reads/writes all state exclusively to the user's personal Google Sheet.
- Performs ZERO git commits.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import load_user_context_from_env
from core.data.market_data_provider import HybridMarketDataProvider
from core.execution.runner import run_daily_reconciliation, run_us_infinite_buying
from core.kis.client import KISClient
from core.market_calendar import is_us_market_holiday
from core.notifier.telegram import TelegramNotifier
from core.sheets.user_sheet_manager import UserSheetManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("us_infinite_actions")


def main() -> int:
    if is_us_market_holiday() and os.getenv("FORCE_RUN", "").lower() != "true":
        logger.info("US market holiday/weekend detected. Skipping execution.")
        return 0

    ctx = load_user_context_from_env()
    if not ctx.sheet_id:
        logger.error("SHEET_ID secret is not set. Aborting.")
        return 1

    sheet = UserSheetManager(ctx)
    sheet.flush_pending_writes()

    kis = KISClient(ctx)
    notifier = TelegramNotifier(ctx)
    provider = HybridMarketDataProvider(kis_client=kis)

    # 1. Reconcile US holdings before placing today's orders
    run_daily_reconciliation(ctx, sheet, kis, markets=("US",), notifier=notifier)

    # 2. Target US symbols from personal Sheet Settings or env fallback
    raw_settings = sheet.load_raw_settings()
    symbols_csv = raw_settings.get("us_infinite_symbols") or os.getenv("US_INFINITE_SYMBOLS", "TQQQ,SOXL")
    symbols = [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]

    logger.info("Running US Infinite Buying for user=%s, symbols=%s", ctx.user_id, symbols)
    outcomes = run_us_infinite_buying(ctx, sheet, kis, provider, symbols=symbols, notifier=notifier)
    logger.info("Completed %d order evaluations.", len(outcomes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
