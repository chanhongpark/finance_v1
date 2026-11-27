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
from core.ledger.models import AuditRecord, LotStatus, utc_now_iso
from core.ledger.reconciler import ReconciliationReport, reconcile_holdings
from core.notifier.telegram import TelegramNotifier
from core.risk.risk_manager import RiskManager
from core.sheets.user_sheet_manager import UserSheetManager

logger = logging.getLogger(__name__)


def _run_market_screening(
    market: Market,
    ctx: UserContext,
    sheet_manager: UserSheetManager,
    data_provider: HybridMarketDataProvider,
    universe_symbols: Sequence[str],
    include_live_quotes_for_candidates: bool = True,
    exclude_algo_ids: Sequence[str] = (),
) -> Dict[str, List[Signal]]:
    """
    Two-stage market screening (R13, R14, R15 + API minimization):
    - Stage 1: Fetch OHLCV for entire universe via FinanceDataReader + local cache (0 KIS API calls).
      Evaluate all enabled algorithms for `market` independently (duplicate recommendations allowed).
    - Stage 2: Only for candidate symbols & OPEN lots in `market`, fetch KIS live quote and
      re-evaluate for final buy/sell signals.
    """
    mkt_str = market.value
    excluded = set(exclude_algo_ids)
    norm_universe = [
        s.strip().upper() if market == Market.US else s.strip()
        for s in universe_symbols
        if s and s.strip()
    ]
    norm_universe = list(dict.fromkeys(norm_universe))

    algo_configs = sheet_manager.load_algorithm_configs()
    algos = [
        algo for algo in default_registry.list_all(market=market)
        if algo.id not in excluded and algo_configs.get(algo.id, {}).get("enabled", True)
    ]

    open_lots = [
        l for l in sheet_manager.load_lots()
        if l.status == LotStatus.OPEN
        and l.qty > 0
        and l.market.upper() == mkt_str
        and l.algo_id not in excluded
    ]
    lot_symbols = [
        l.symbol.strip().upper() if market == Market.US else l.symbol.strip()
        for l in open_lots
    ]
    all_symbols = list(dict.fromkeys(norm_universe + lot_symbols))

    # Stage 1: Batch historical OHLCV (0 KIS API calls)
    ohlcv_map = data_provider.get_batch_daily_ohlcv(all_symbols, market=mkt_str)

    stage1_candidate_symbols: List[str] = []
    stage1_buy_signals: List[Signal] = []

    def _find_open_lot(sym: str, algo_id: str):
        return next(
            (
                l for l in open_lots
                if (l.symbol.strip().upper() if market == Market.US else l.symbol.strip()) == sym
                and l.algo_id == algo_id
            ),
            None,
        )

    for sym in norm_universe:
        df = ohlcv_map.get(sym)
        if df is None or df.empty:
            continue
        for algo in algos:
            params = algo_configs.get(algo.id, {}).get("params", {})
            existing_lot = _find_open_lot(sym, algo.id)
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
            quote = data_provider.get_live_quote(sym, market=mkt_str)
            if quote:
                ohlcv_map[sym] = data_provider.stitch_live_quote(base_df, quote)

        # Re-evaluate buy candidates with stitched live candle
        for sym in stage1_candidate_symbols:
            df = ohlcv_map.get(sym)
            if df is None or df.empty:
                continue
            for algo in algos:
                params = algo_configs.get(algo.id, {}).get("params", {})
                existing_lot = _find_open_lot(sym, algo.id)
                sig = algo.evaluate_entry(sym, df, params=params, extra={"existing_lot": existing_lot})
                if sig is not None:
                    buy_signals.append(sig)
    else:
        buy_signals = stage1_buy_signals

    # Evaluate exits for all OPEN lots using latest registered version of lot.algo_id (R14)
    for lot in open_lots:
        sym_key = lot.symbol.strip().upper() if market == Market.US else lot.symbol.strip()
        df = ohlcv_map.get(sym_key)
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


def run_kr_screening(
    ctx: UserContext,
    sheet_manager: UserSheetManager,
    data_provider: HybridMarketDataProvider,
    universe_symbols: Sequence[str],
    include_live_quotes_for_candidates: bool = True,
) -> Dict[str, List[Signal]]:
    """Two-stage KR screening (월간추세 · 컵앤핸들 · 후지모토 1:2:6)."""
    return _run_market_screening(
        market=Market.KR,
        ctx=ctx,
        sheet_manager=sheet_manager,
        data_provider=data_provider,
        universe_symbols=universe_symbols,
        include_live_quotes_for_candidates=include_live_quotes_for_candidates,
    )


def run_us_screening(
    ctx: UserContext,
    sheet_manager: UserSheetManager,
    data_provider: HybridMarketDataProvider,
    universe_symbols: Sequence[str],
    include_live_quotes_for_candidates: bool = True,
) -> Dict[str, List[Signal]]:
    """Two-stage US screening (월간추세 · 컵앤핸들 · 후지모토 1:2:6, excluding infinite buying)."""
    return _run_market_screening(
        market=Market.US,
        ctx=ctx,
        sheet_manager=sheet_manager,
        data_provider=data_provider,
        universe_symbols=universe_symbols,
        include_live_quotes_for_candidates=include_live_quotes_for_candidates,
        exclude_algo_ids=("us_infinite_buying",),
    )


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
    Flush queued writes, settle any pending/partial broker orders first,
    then compare KIS actual holdings against Sheet OPEN lots across requested markets (R2.3).
    """
    # 1. Flush any local pending writes first; abort reconciliation if queue still remains
    sheet_manager.flush_pending_writes()
    if sheet_manager.has_pending_writes():
        reason = "미반영 로컬 대기열(pending_writes)이 남아 있어 이중 계산 방지를 위해 잔고 대사를 건너뛰었습니다."
        logger.warning("%s (user=%s)", reason, ctx.user_id)
        if notifier is not None:
            notifier.send_text(f"⚠️ [잔고 대사 보류]\n{reason}")
        return ReconciliationReport(ts=utc_now_iso(), is_matched=False, skipped_reason=reason)

    # 2. Settle any SUBMITTED/PARTIAL orders (e.g. US LOC or KR limit fills) BEFORE balance comparison
    order_svc = OrderService(ctx, sheet_manager, kis_client, notifier=notifier)
    order_svc.sync_pending_orders(markets=markets)

    # Re-check queue in case settling pending orders queued writes due to quota error
    if sheet_manager.has_pending_writes():
        reason = "체결 정산 중 시트 기록이 대기열에 남아 잔고 대사를 보류했습니다."
        logger.warning("%s (user=%s)", reason, ctx.user_id)
        return ReconciliationReport(ts=utc_now_iso(), is_matched=False, skipped_reason=reason)

    kis_holdings: List[Dict[str, Any]] = []
    for mkt in markets:
        try:
            kis_holdings.extend(kis_client.inquire_balance(market=mkt))
        except Exception as exc:
            logger.warning("Failed to inquire %s balance for reconciliation: %s", mkt, exc)

    sheet_lots = sheet_manager.load_lots(use_cache=False)
    report = reconcile_holdings(kis_holdings, sheet_lots, actor=f"reconciler:{ctx.user_id}")

    if report.created_fills:
        fills_written = sheet_manager.append_fills(report.created_fills)
        if report.created_manual_lots:
            sheet_manager.upsert_lots(report.created_manual_lots, queue_only=not fills_written)
    elif report.created_manual_lots:
        sheet_manager.upsert_lots(report.created_manual_lots)
    for audit in report.audits:
        sheet_manager.append_audit(audit)

    if not report.is_matched and notifier is not None:
        lines = ["🔎 [잔고 대사 불일치 감지]"]
        for d in report.discrepancies:
            lines.append(f"• {d.symbol}({d.market}): KIS {d.kis_qty}주 vs Sheet {d.sheet_qty}주 -> {d.action_taken}")
        notifier.send_text("\n".join(lines))

    return report
