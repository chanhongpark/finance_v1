"""
US Laor Infinite Buying v2.1 Algorithm (R4.4, R11, R15).
Migrated from dalpan_kis/strategy.py into unified Algorithm interface.
State (current_round / T, status, budget) is stored in Lot.algo_state (Google Sheet),
eliminating all Git-committed JSON state files (R1.5).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from core.algorithms.base import (
    Algorithm,
    Condition,
    Market,
    ParamSpec,
    PriceType,
    Signal,
    SignalAction,
)
from core.algorithms.registry import register_algorithm
from core.indicators import normalize_ohlcv
from core.ledger.models import Lot


@register_algorithm
class USInfiniteBuyingAlgorithm(Algorithm):
    id = "us_infinite_buying"
    version = "2.1.0"
    name = "라오어 무한매수법 v2.1 (미국)"
    description = (
        "미국 레버리지 ETF(TQQQ, SOXL 등) 대상 40분할 자동 LOC 매수 + 목표 수익률(+10%) 지정가 매도 전략. "
        "전반전(T < 20)은 평단 LOC 0.5회차 + 별% LOC 0.5회차 분할 매수, 후반전(T >= 20)은 별% LOC 1.0회차 매수를 수행합니다."
    )
    market = Market.US
    params_schema = [
        ParamSpec("division_count", "총 분할 횟수", "int", 40, "원금 분할 횟수 (기본 40분할)", 10, 100, 5),
        ParamSpec("total_budget_usd", "종목당 총 예산($)", "float", 10_000.0, "종목당 배정 총 USD 예산", 500.0, 200_000.0, 500.0),
        ParamSpec("target_roi", "목표 익절률", "float", 0.10, "기본 목표 익절 수익률 (0.10 = +10%)", 0.03, 0.30, 0.01),
        ParamSpec("exhaust_action", "40회차 소진 시 대응", "select", "AUTO", "원금 소진 시 대응 전략", options=["AUTO", "HOLD", "LOWER_TARGET", "REBALANCE_QUARTER"]),
        ParamSpec("exhaust_target_roi", "소진 시 하향 목표률", "float", 0.05, "LOWER_TARGET 선택 시 하향 목표 수익률", 0.01, 0.15, 0.01),
        ParamSpec("max_loc_premium_ratio", "LOC 상한 캡 비율", "float", 0.10, "현재가 대비 LOC 과도 괴리 방지 캡", 0.03, 0.25, 0.01),
    ]

    @staticmethod
    def _latest_price(data: pd.DataFrame, extra: Optional[Dict[str, Any]] = None) -> float:
        if extra and extra.get("current_price"):
            return float(extra["current_price"])
        norm = normalize_ohlcv(data)
        if not norm.empty:
            return float(norm["close"].iloc[-1])
        return 0.0

    @staticmethod
    def determine_exhaust_action_auto(
        current_price: float,
        avg_price: float,
        data: Optional[pd.DataFrame] = None,
    ) -> tuple[str, str]:
        if avg_price <= 0:
            return "HOLD", "평단가 미확인 -> HOLD"
        pnl_rate = (current_price - avg_price) / avg_price
        ma5 = ma20 = None
        if data is not None and not data.empty:
            norm = normalize_ohlcv(data)
            if len(norm) >= 5:
                ma5 = float(norm["close"].tail(5).mean())
            if len(norm) >= 20:
                ma20 = float(norm["close"].tail(20).mean())

        if pnl_rate >= -0.05:
            return "HOLD", f"손실률 경미({pnl_rate*100:+.1f}%) -> HOLD"
        if ma5 is not None and ma20 is not None and ma5 > ma20 and current_price >= ma5:
            return "HOLD", f"단기 골든크로스(MA5 ${ma5:.2f} > MA20 ${ma20:.2f}) -> HOLD"
        if pnl_rate >= -0.15:
            return "LOWER_TARGET", f"박스권 조정({pnl_rate*100:+.1f}%) -> LOWER_TARGET"
        return "REBALANCE_QUARTER", f"깊은 하락({pnl_rate*100:+.1f}%) -> REBALANCE_QUARTER"

    def generate_daily_signals(
        self,
        symbol: str,
        data: pd.DataFrame,
        lot: Optional[Lot] = None,
        params: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> List[Signal]:
        """
        Generate the full set of daily US Infinite Buying orders (0..2 LOC BUYs + 0..1 Limit SELL).
        """
        p = self.merge_params(params)
        current_price = self._latest_price(data, extra)
        if current_price <= 0:
            return []

        state = dict(lot.algo_state) if lot else {}
        division = int(state.get("division_count", p["division_count"]))
        total_budget = float(state.get("total_budget_usd", p["total_budget_usd"]))
        target_roi = float(state.get("target_roi", p["target_roi"]))
        exhaust_action = str(state.get("exhaust_action", p["exhaust_action"])).upper()
        exhaust_target_roi = float(state.get("exhaust_target_roi", p["exhaust_target_roi"]))
        max_loc_premium = float(state.get("max_loc_premium_ratio", p["max_loc_premium_ratio"]))
        status = str(state.get("status", "RUNNING")).upper()

        avg_price = float(lot.avg_price) if (lot and lot.qty > 0) else 0.0
        total_qty = int(lot.qty) if lot else 0

        one_round_budget = total_budget / max(1, division)
        if "current_round" in state:
            curr_round = float(state["current_round"])
        else:
            curr_round = round((avg_price * total_qty) / one_round_budget, 2) if (one_round_budget > 0 and total_qty > 0) else 0.0

        is_exhausted = curr_round >= division
        effective_exhaust = exhaust_action
        auto_reason = ""
        if is_exhausted and exhaust_action == "AUTO":
            effective_exhaust, auto_reason = self.determine_exhaust_action_auto(current_price, avg_price, data)

        signals: List[Signal] = []

        # 1. Limit SELL order (if holding)
        if total_qty > 0 and avg_price > 0:
            if is_exhausted and effective_exhaust == "LOWER_TARGET":
                sell_price = round(avg_price * (1.0 + exhaust_target_roi), 2)
                sell_qty = total_qty
                sell_desc = f"전량 익절 매도 (소진 하향목표 +{exhaust_target_roi*100:.1f}%, ${sell_price:.2f})"
            elif is_exhausted and effective_exhaust == "REBALANCE_QUARTER":
                rebalance_qty = max(1, int(total_qty / division))
                sell_qty = max(0, total_qty - rebalance_qty)
                sell_price = round(avg_price * (1.0 + target_roi), 2)
                sell_desc = f"잔여 익절 매도 ({sell_qty}주, 목표가 ${sell_price:.2f})"
                # Also emit quarter rebalance sell
                signals.append(
                    Signal(
                        action=SignalAction.SELL,
                        algo_id=self.id,
                        algo_version=self.version,
                        symbol=symbol,
                        market=self.market,
                        price_type=PriceType.LIMIT,
                        price=round(current_price, 2),
                        qty=rebalance_qty,
                        conditions=[
                            Condition("회차(T)", curr_round, division, ">=", True, f"40회차 소진 쿼터순환 ({auto_reason})")
                        ],
                        score=90.0,
                        reason_text=f"쿼터 순환 매도 ({rebalance_qty}주 @ ${current_price:.2f})",
                        lot_id=lot.lot_id if lot else None,
                        metadata={"order_role": "QUARTER_REBALANCE_SELL"},
                    )
                )
            else:
                sell_price = round(avg_price * (1.0 + target_roi), 2)
                sell_qty = total_qty
                sell_desc = f"전량 익절 지정가 매도 (+{target_roi*100:.1f}%, ${sell_price:.2f})"

            if sell_qty > 0:
                signals.append(
                    Signal(
                        action=SignalAction.SELL,
                        algo_id=self.id,
                        algo_version=self.version,
                        symbol=symbol,
                        market=self.market,
                        price_type=PriceType.LIMIT,
                        price=sell_price,
                        qty=sell_qty,
                        qty_ratio=round(sell_qty / total_qty, 4),
                        conditions=[
                            Condition("목표 익절가($)", sell_price, round(avg_price, 2), ">", True, sell_desc)
                        ],
                        score=100.0,
                        reason_text=sell_desc,
                        lot_id=lot.lot_id if lot else None,
                        metadata={"order_role": "TAKE_PROFIT_SELL"},
                    )
                )

        if status in {"STOPPED", "PAUSED"}:
            return signals

        # 2. Daily LOC BUY order(s)
        half_budget = one_round_budget / 2.0
        half_division = division / 2.0
        base_price = avg_price if avg_price > 0 else current_price
        star_percent = 10.0 - (curr_round * (10.0 / half_division))
        max_allowed_loc = round(current_price * (1.0 + max_loc_premium), 2)

        if not is_exhausted:
            if curr_round < half_division:
                raw_loc1 = round(base_price, 2)
                loc1 = round(min(raw_loc1, max_allowed_loc), 2)
                raw_loc2 = round(base_price * 1.05, 2) if avg_price <= 0 else round(base_price * (1.0 + star_percent / 100.0), 2)
                if raw_loc2 <= raw_loc1:
                    raw_loc2 = round(raw_loc1 + 0.01, 2)
                loc2 = round(min(raw_loc2, max_allowed_loc), 2)
                if loc2 <= loc1 and loc1 > 0.01:
                    loc1 = round(max(0.01, loc2 - 0.01), 2)

                qty1 = max(1, int(half_budget / loc1))
                qty2 = max(1, int(half_budget / loc2))

                signals.append(
                    Signal(
                        action=SignalAction.BUY,
                        algo_id=self.id,
                        algo_version=self.version,
                        symbol=symbol,
                        market=self.market,
                        price_type=PriceType.LOC,
                        price=loc1,
                        qty=qty1,
                        conditions=[
                            Condition("회차(T)", curr_round, half_division, "<", True, "전반전 0.5회차 평단가 LOC"),
                            Condition("평단 LOC가($)", loc1, max_allowed_loc, "<=", True, "상한 캡 이내"),
                        ],
                        score=85.0,
                        reason_text=f"전반전 0.5회차 평단가 LOC (${loc1:.2f}, {qty1}주)",
                        lot_id=lot.lot_id if lot else None,
                        metadata={"round_inc": 0.5, "order_role": "FIRST_HALF_AVG_LOC"},
                    )
                )
                signals.append(
                    Signal(
                        action=SignalAction.BUY,
                        algo_id=self.id,
                        algo_version=self.version,
                        symbol=symbol,
                        market=self.market,
                        price_type=PriceType.LOC,
                        price=loc2,
                        qty=qty2,
                        conditions=[
                            Condition("회차(T)", curr_round, half_division, "<", True, f"전반전 0.5회차 큰수(☆{star_percent:+.1f}%) LOC"),
                            Condition("큰수 LOC가($)", loc2, max_allowed_loc, "<=", True, "상한 캡 이내"),
                        ],
                        score=85.0,
                        reason_text=f"전반전 0.5회차 큰수(☆{star_percent:+.1f}%) LOC (${loc2:.2f}, {qty2}주)",
                        lot_id=lot.lot_id if lot else None,
                        metadata={"round_inc": 0.5, "order_role": "FIRST_HALF_STAR_LOC"},
                    )
                )
            else:
                raw_loc = round(base_price * (1.0 + star_percent / 100.0), 2)
                loc_price = round(min(raw_loc, max_allowed_loc), 2)
                qty = max(1, int(one_round_budget / loc_price))
                signals.append(
                    Signal(
                        action=SignalAction.BUY,
                        algo_id=self.id,
                        algo_version=self.version,
                        symbol=symbol,
                        market=self.market,
                        price_type=PriceType.LOC,
                        price=loc_price,
                        qty=qty,
                        conditions=[
                            Condition("회차(T)", curr_round, division, "<", True, f"후반전 1.0회차(☆{star_percent:+.1f}%) LOC"),
                            Condition("후반 LOC가($)", loc_price, max_allowed_loc, "<=", True, "상한 캡 이내"),
                        ],
                        score=80.0,
                        reason_text=f"후반전 1.0회차(☆{star_percent:+.1f}%) LOC (${loc_price:.2f}, {qty}주)",
                        lot_id=lot.lot_id if lot else None,
                        metadata={"round_inc": 1.0, "order_role": "SECOND_HALF_STAR_LOC"},
                    )
                )
        elif effective_exhaust == "REBALANCE_QUARTER":
            loc_price = round(current_price * 1.02, 2)
            buy_qty = max(1, int(one_round_budget / loc_price))
            signals.append(
                Signal(
                    action=SignalAction.BUY,
                    algo_id=self.id,
                    algo_version=self.version,
                    symbol=symbol,
                    market=self.market,
                    price_type=PriceType.LOC,
                    price=loc_price,
                    qty=buy_qty,
                    conditions=[
                        Condition("소진 대응", "REBALANCE_QUARTER", "40회차 소진", "==", True, "쿼터 순환 LOC 재매수")
                    ],
                    score=75.0,
                    reason_text=f"쿼터 순환 LOC 재매수 (${loc_price:.2f}, {buy_qty}주)",
                    lot_id=lot.lot_id if lot else None,
                    metadata={"round_inc": 0.0, "order_role": "QUARTER_REBALANCE_BUY"},
                )
            )

        return signals

    def evaluate_entry(
        self,
        symbol: str,
        data: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        lot = (extra or {}).get("existing_lot")
        signals = self.generate_daily_signals(symbol, data, lot=lot, params=params, extra=extra)
        buys = [s for s in signals if s.action == SignalAction.BUY]
        return buys[0] if buys else None

    def evaluate_exit(
        self,
        lot: Lot,
        data: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        signals = self.generate_daily_signals(lot.symbol, data, lot=lot, params=params, extra=extra)
        sells = [s for s in signals if s.action == SignalAction.SELL]
        return sells[0] if sells else None
