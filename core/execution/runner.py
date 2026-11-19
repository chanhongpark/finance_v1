"""
Runners for:
1. Two-stage KR screening (FDR batch first -> KIS live quote only for final candidates & open lots)
2. US Infinite Buying automated runner (GitHub Actions - R11, R15)
3. Daily KIS <-> Sheet reconciliation (R2.3)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from core.algorithms import Market, Signal, USInfiniteBuyingAlgorithm, default_registry
from core.context import UserContext
from core.data.market_data_provider import HybridMarketDataProvider
from core.execution.order_service import ExecutionOutcome, OrderService
from core.ledger.models import LotStatus
from core.ledger.reconciler import ReconciliationReport, reconcile_holdings
from core.notifier.telegram import TelegramNotifier
from core.risk.risk_manager import RiskManager
from core.sheets.user_sheet_manager import UserSheetManager

logger = logging.getLogger(__name__)


def run_kr_screening(
    ctx: UserContext,
    sheet_manager: UserSheetManager,
    data_provider: HybridMarketDataProvider,
    universe_symbols: Sequence[str],
    include_live_quotes_for_candidates: bool = True,
) -> Dict[str, List[Signal]]:
    """
    Two-stage KR screening (R13, R14, R15 + API minimization):
    - Stage 1: Fetch OHLCV for entire universe via FinanceDataReader + local cache (0 KIS API calls).
      Evaluate all enabled KR algorithms independently (duplicate recommendations on same symbol allowed).
    - Stage 2: Only for candidate symbols & OPEN KR lots, fetch KIS live quote and re-evaluate
      for final buy/sell signals.
    """
    algo_configs = sheet_manager.load_algorithm_configs()
    kr_algos = [
        algo for algo in default_registry.list_all(market=Market.KR)
        if algo_configs.get(algo.id, {}).get("enabled", True)
    ]

    open_kr_lots = [
        l for l in sheet_manager.load_lots()
        if l.status == LotStatus.OPEN and l.qty > 0 and l.market == "KR"
    ]
    lot_symbols = [l.symbol for l in open_kr_lots]
    all_symbols = list(dict.fromkeys(list(universe_symbols) + lot_symbols))

    # Stage 1: Batch historical OHLCV (0 KIS API calls)
    ohlcv_map = data_provider.get_batch_daily_ohlcv(all_symbols, market="KR")

    stage1_candidate_symbols: List[str] = []
    stage1_buy_signals: List[Signal] = []

    for sym in universe_symbols:
        df = ohlcv_map.get(sym)
        if df is None or df.empty:
            continue
        for algo in kr_algos:
            params = algo_configs.get(algo.id, {}).get("params", {})
            existing_lot = next(
                (l for l in open_kr_lots if l.symbol == sym and l.algo_id == algo.id),
                None,
            )
            sig = algo.evaluate_entry(sym, df, params=params, extra={"existing_lot": existing_lot})
            if sig is not None:
                stage1_buy_signals.append(sig)
                if sym not in stage1_candidate_symbols:
                    stage1_candidate_symbols.append(sym)

    # Stage 2: Refresh live quotes via KIS ONLY for final candidates & open lots
    buy_signals: List[Signal] = []
    sell_signals: List[Signal] = []
    risk_mgr = RiskManager(ctx, sheet_manager.load_user_settings())

    if include_live_quotes_for_candidates and data_provider.kis_client is not None:
        live_targets = list(dict.fromkeys(stage1_candidate_symbols + lot_symbols))
        for sym in live_targets:
            base_df = ohlcv_map.get(sym)
            if base_df is None:
                continue
            quote = data_provider.get_live_quote(sym, market="KR")
            if quote:
                ohlcv_map[sym] = data_provider.stitch_live_quote(base_df, quote)

        # Re-evaluate buy candidates with stitched live candle
        for sym in stage1_candidate_symbols:
            df = ohlcv_map.get(sym)
            if df is None or df.empty:
                continue
            for algo in kr_algos:
                params = algo_configs.get(algo.id, {}).get("params", {})
                existing_lot = next(
                    (l for l in open_kr_lots if l.symbol == sym and l.algo_id == algo.id),
                    None,
                )
                sig = algo.evaluate_entry(sym, df, params=params, extra={"existing_lot": existing_lot})
                if sig is not None:
                    buy_signals.append(sig)
    else:
        buy_signals = stage1_buy_signals

    # Evaluate exits for all OPEN KR lots using latest registered version of lot.algo_id (R14)
    for lot in open_kr_lots:
        df = ohlcv_map.get(lot.symbol)
        if df is None or df.empty:
            continue
        curr_price = float(df["close"].iloc[-1])

        # Global hard stop-loss override takes precedence (R5.2)
        override_sig = risk_mgr.check_global_stop_loss_override(lot, curr_price)
        if override_sig is not None:
            sell_signals.append(override_sig)
            continue

        algo = default_registry.get(lot.algo_id)
        if algo is None:
            continue
        params = algo_configs.get(algo.id, {}).get("params", {})
        exit_sig = algo.evaluate_exit(lot, df, params=params)
        if exit_sig is not None:
            sell_signals.append(exit_sig)

    return {"buy": buy_signals, "sell": sell_signals}


def run_us_infinite_buying(
    ctx: UserContext,
    sheet_manager: UserSheetManager,
    kis_client: Any,
    data_provider: HybridMarketDataProvider,
    symbols: Sequence[str],
    notifier: Optional[TelegramNotifier] = None,
) -> List[ExecutionOutcome]:
    """
    US Infinite Buying automated runner for GitHub Actions (R11, R15).
    Zero Git state commits — reads/writes exclusively to personal Google Sheet.
    """
    algo = default_registry.require("us_infinite_buying")
    assert isinstance(algo, USInfiniteBuyingAlgorithm)

    algo_configs = sheet_manager.load_algorithm_configs()
    cfg = algo_configs.get(algo.id, {"enabled": True, "params": {}})
    if not cfg.get("enabled", True):
        logger.info("US Infinite Buying is disabled in personal Sheet for '%s'", ctx.user_id)
        return []

    params = cfg.get("params", {})
    open_us_lots = [
        l for l in sheet_manager.load_lots()
        if l.status == LotStatus.OPEN and l.qty > 0 and l.market == "US" and l.algo_id == algo.id
    ]
    lot_by_sym = {l.symbol.upper(): l for l in open_us_lots}
    order_svc = OrderService(ctx, sheet_manager, kis_client, notifier=notifier)
    outcomes: List[ExecutionOutcome] = []

    for raw_sym in symbols:
        sym = raw_sym.strip().upper()
        if not sym:
            continue
        try:
            df = data_provider.get_daily_ohlcv(sym, market="US", lookback_days=90, include_live_quote=True)
            lot = lot_by_sym.get(sym)
            signals = algo.generate_daily_signals(sym, df, lot=lot, params=params)
            for sig in signals:
                outcome = order_svc.execute_signal(sig, user_confirmed=False)
                outcomes.append(outcome)
        except Exception as exc:
            logger.exception("US Infinite Buying error for %s: %s", sym, exc)
            if notifier:
                notifier.notify_error(f"미국 무한매수({sym})", str(exc))

    return outcomes


def run_daily_reconciliation(
    ctx: UserContext,
    sheet_manager: UserSheetManager,
    kis_client: Any,
    markets: Sequence[str] = ("KR", "US"),
    notifier: Optional[TelegramNotifier] = None,
) -> ReconciliationReport:
    """
    Compare KIS actual holdings against Sheet OPEN lots across requested markets (R2.3).
    """
    kis_holdings: List[Dict[str, Any]] = []
    for mkt in markets:
        try:
            kis_holdings.extend(kis_client.inquire_balance(market=mkt))
        except Exception as exc:
            logger.warning("Failed to inquire %s balance for reconciliation: %s", mkt, exc)

    sheet_lots = sheet_manager.load_lots(use_cache=False)
    report = reconcile_holdings(kis_holdings, sheet_lots, actor=f"reconciler:{ctx.user_id}")

    if report.created_fills:
        sheet_manager.append_fills(report.created_fills)
    if report.created_manual_lots:
        sheet_manager.upsert_lots(report.created_manual_lots)
    for audit in report.audits:
        sheet_manager.append_audit(audit)

    if not report.is_matched and notifier is not None:
        lines = ["🔎 [잔고 대사 불일치 감지]"]
        for d in report.discrepancies:
            lines.append(f"• {d.symbol}({d.market}): KIS {d.kis_qty}주 vs Sheet {d.sheet_qty}주 -> {d.action_taken}")
        notifier.send_text("\n".join(lines))

    return report
