"""
KR Cup & Handle Breakout Algorithm (R4, R15).
Migrated from dalpan_kis/core/strategy/cup_and_handle.py into unified Algorithm interface.
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
from core.indicators import calculate_technical_indicators, detect_cup_and_handle
from core.ledger.models import Lot


@register_algorithm
class KRCupAndHandleAlgorithm(Algorithm):
    id = "kr_cup_and_handle"
    version = "1.0.0"
    name = "컵앤핸들 돌파 (Cup & Handle)"
    description = (
        "U자형 바닥(컵)과 얕은 조정(핸들) 이후 거래량 증가와 함께 피봇 저항선을 돌파할 때 매수하고, "
        "목표 익절·손절·핸들 저점 이탈 시 청산합니다."
    )
    market = Market.KR
    _budget_param_key: str = "budget_per_stock_krw"
    _price_decimals: int = 0
    _currency_symbol: str = "원"
    params_schema = [
        ParamSpec("min_cup_bars", "컵 최소 봉수", "int", 20, "컵 패턴 최소 거래일수", 10, 60, 1),
        ParamSpec("max_cup_bars", "컵 최대 봉수", "int", 80, "컵 패턴 최대 거래일수", 30, 150, 5),
        ParamSpec("min_handle_bars", "핸들 최소 봉수", "int", 3, "핸들 최소 거래일수", 2, 10, 1),
        ParamSpec("max_handle_bars", "핸들 최대 봉수", "int", 20, "핸들 최대 거래일수", 5, 30, 1),
        ParamSpec("min_cup_depth", "컵 최소 깊이", "float", 0.08, "좌측 고점 대비 최소 하락률", 0.03, 0.20, 0.01),
        ParamSpec("max_cup_depth", "컵 최대 깊이", "float", 0.38, "좌측 고점 대비 최대 하락률", 0.15, 0.60, 0.01),
        ParamSpec("vol_breakout_mult", "돌파 거래량 배수", "float", 1.3, "20일 평균 거래량 대비 돌파 최소 배수", 1.0, 4.0, 0.1),
        ParamSpec("allow_near_pivot", "피봇 근접 선제 진입", "bool", False, "피봇선 -2% 이내 근접 시 조기 진입 허용"),
        ParamSpec("target_profit_rate", "기본 목표 익절률", "float", 0.10, "기본 목표 익절 수익률", 0.03, 0.50, 0.01),
        ParamSpec("stop_loss_rate", "손절 기준 수익률", "float", -0.05, "손절 기준 수익률", -0.20, -0.01, 0.005),
        ParamSpec("buy_score_threshold", "최소 매수 점수", "int", 65, "매수 추천 최소 종합 점수", 0, 100, 5),
        ParamSpec("budget_per_stock_krw", "종목당 기본 예산(원)", "float", 500_000.0, "1회 진입 시 배정 예산", 100_000.0, 20_000_000.0, 100_000.0),
    ]

    def _round_price(self, price: float) -> float:
        return round(price, self._price_decimals)

    def _fmt_price(self, price: float) -> str:
        if self._price_decimals == 0:
            return f"{price:,.0f}{self._currency_symbol}"
        return f"{self._currency_symbol}{price:,.{self._price_decimals}f}"

    def evaluate_entry(
        self,
        symbol: str,
        data: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        p = self.merge_params(params)
        df = calculate_technical_indicators(data, min_bars=35)
        if df is None or len(df) < 35:
            return None

        pattern = detect_cup_and_handle(
            df=df,
            min_cup_bars=int(p["min_cup_bars"]),
            max_cup_bars=int(p["max_cup_bars"]),
            min_handle_bars=int(p["min_handle_bars"]),
            max_handle_bars=int(p["max_handle_bars"]),
            min_cup_depth=float(p["min_cup_depth"]),
            max_cup_depth=float(p["max_cup_depth"]),
            vol_breakout_mult=float(p["vol_breakout_mult"]),
        )
        if not pattern or not pattern.get("is_pattern"):
            return None

        is_breakout = bool(pattern["is_breakout"])
        status = str(pattern["status"])
        allow_near = bool(p["allow_near_pivot"])
        vol_ratio = float(pattern["vol_ratio"])
        vol_mult = float(p["vol_breakout_mult"])

        curr_price = float(df["close"].iloc[-1])
        pivot_price = float(pattern["pivot_price"])

        cond_pivot = Condition(
            name="피봇 돌파",
            value=self._round_price(curr_price),
            threshold=self._round_price(pivot_price),
            op=">=",
            passed=is_breakout or (allow_near and status == "near_pivot"),
            desc="피봇 저항선 돌파 또는 허용된 근접 구간",
        )
        cond_vol = Condition(
            name="거래량 배수",
            value=round(vol_ratio, 2),
            threshold=round(vol_mult, 2),
            op=">=",
            passed=(vol_ratio >= 1.0) if not is_breakout else (vol_ratio >= vol_mult),
            desc="20일 평균 대비 돌파 거래량 확인",
        )
        conditions: List[Condition] = [cond_pivot, cond_vol]
        if not all(c.passed for c in conditions):
            return None

        score = float(pattern["score"])
        if vol_ratio >= 2.0:
            score += 10.0
        elif vol_ratio >= 1.5:
            score += 5.0
        ma20 = float(df["ma20"].iloc[-1])
        ma60 = float(df["ma60"].iloc[-1])
        if ma20 > ma60 > 0:
            score += 5.0
        score = min(100.0, score)

        threshold_score = float(p["buy_score_threshold"])
        cond_score = Condition("종합점수", round(score, 1), threshold_score, ">=", score >= threshold_score, "최소 점수 충족")
        conditions.append(cond_score)
        if not cond_score.passed:
            return None

        budget = float(p[self._budget_param_key])
        qty = max(1, int(budget // curr_price)) if budget >= curr_price else 0
        if qty <= 0:
            return None

        return Signal(
            action=SignalAction.BUY,
            algo_id=self.id,
            algo_version=self.version,
            symbol=symbol,
            market=self.market,
            price_type=PriceType.LIMIT,
            price=self._round_price(curr_price),
            qty=qty,
            conditions=conditions,
            score=score,
            reason_text=(
                f"컵앤핸들 피봇({self._fmt_price(pivot_price)}) {'돌파' if is_breakout else '근접'} "
                f"(컵 깊이 -{pattern['cup_depth_pct']}%, 거래량 {vol_ratio:.1f}x)"
            ),
            metadata={
                "target_price": self._round_price(float(pattern["target_price"])),
                "stop_loss_price": self._round_price(float(pattern["stop_loss_price"])),
                "pivot_price": self._round_price(pivot_price),
                "cup_pattern": {
                    "p1_date": pattern.get("p1_date"),
                    "p1_price": self._round_price(float(pattern["p1_price"])),
                    "cup_low_date": pattern.get("cup_low_date"),
                    "cup_low_price": self._round_price(float(pattern["cup_low_price"])),
                    "p2_date": pattern.get("p2_date"),
                    "p2_price": self._round_price(float(pattern["p2_price"])),
                    "handle_low_date": pattern.get("handle_low_date"),
                    "handle_low_price": self._round_price(float(pattern["handle_low_price"])),
                    "breakout_date": pattern.get("breakout_date"),
                    "pivot_price": self._round_price(pivot_price),
                    "cup_depth_pct": pattern.get("cup_depth_pct"),
                    "handle_depth_pct": pattern.get("handle_depth_pct"),
                },
            },
        )

    def evaluate_exit(
        self,
        lot: Lot,
        data: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        if lot.qty <= 0 or lot.avg_price <= 0:
            return None
        p = self.merge_params(params)
        df = calculate_technical_indicators(data, min_bars=5)
        if df is None or df.empty:
            return None

        curr_price = float(df["close"].iloc[-1])
        pnl_rate = (curr_price - lot.avg_price) / lot.avg_price
        target_profit = float(p["target_profit_rate"])
        stop_loss = float(p["stop_loss_rate"])

        custom_stop = float(lot.algo_state.get("stop_loss_price", 0.0) or 0.0)
        custom_target = float(lot.algo_state.get("target_price", 0.0) or 0.0)

        conditions: List[Condition] = []
        reason = ""

        if pnl_rate <= stop_loss or (custom_stop > 0 and curr_price <= custom_stop):
            conditions.append(
                Condition("수익률(%)", round(pnl_rate * 100, 2), round(stop_loss * 100, 2), "<=", True, "손절 기준 도달")
            )
            reason = f"컵앤핸들 손절 도달 ({pnl_rate*100:+.1f}%)"
        elif pnl_rate >= target_profit or (custom_target > 0 and curr_price >= custom_target):
            conditions.append(
                Condition("수익률(%)", round(pnl_rate * 100, 2), round(target_profit * 100, 2), ">=", True, "목표 익절 도달")
            )
            reason = f"컵앤핸들 목표 익절 도달 ({pnl_rate*100:+.1f}%)"

        if not reason:
            return None

        return Signal(
            action=SignalAction.SELL,
            algo_id=self.id,
            algo_version=self.version,
            symbol=lot.symbol,
            market=self.market,
            price_type=PriceType.LIMIT,
            price=self._round_price(curr_price),
            qty=lot.qty,
            qty_ratio=1.0,
            conditions=conditions,
            score=100.0,
            reason_text=reason,
            lot_id=lot.lot_id,
        )


@register_algorithm
class USCupAndHandleAlgorithm(KRCupAndHandleAlgorithm):
    id = "us_cup_and_handle"
    version = "1.0.0"
    name = "컵앤핸들 돌파 (Cup & Handle)"
    description = (
        "미국 주식/ETF 대상 U자형 바닥(컵)과 얕은 조정(핸들) 이후 거래량 증가와 함께 피봇 저항선을 돌파할 때 매수하고, "
        "목표 익절·손절·핸들 저점 이탈 시 청산합니다."
    )
    market = Market.US
    _budget_param_key = "budget_per_stock_usd"
    _price_decimals = 2
    _currency_symbol = "$"
    params_schema = [
        ParamSpec("min_cup_bars", "컵 최소 봉수", "int", 20, "컵 패턴 최소 거래일수", 10, 60, 1),
        ParamSpec("max_cup_bars", "컵 최대 봉수", "int", 80, "컵 패턴 최대 거래일수", 30, 150, 5),
        ParamSpec("min_handle_bars", "핸들 최소 봉수", "int", 3, "핸들 최소 거래일수", 2, 10, 1),
        ParamSpec("max_handle_bars", "핸들 최대 봉수", "int", 20, "핸들 최대 거래일수", 5, 30, 1),
        ParamSpec("min_cup_depth", "컵 최소 깊이", "float", 0.08, "좌측 고점 대비 최소 하락률", 0.03, 0.20, 0.01),
        ParamSpec("max_cup_depth", "컵 최대 깊이", "float", 0.38, "좌측 고점 대비 최대 하락률", 0.15, 0.60, 0.01),
        ParamSpec("vol_breakout_mult", "돌파 거래량 배수", "float", 1.3, "20일 평균 거래량 대비 돌파 최소 배수", 1.0, 4.0, 0.1),
        ParamSpec("allow_near_pivot", "피봇 근접 선제 진입", "bool", False, "피봇선 -2% 이내 근접 시 조기 진입 허용"),
        ParamSpec("target_profit_rate", "기본 목표 익절률", "float", 0.10, "기본 목표 익절 수익률", 0.03, 0.50, 0.01),
        ParamSpec("stop_loss_rate", "손절 기준 수익률", "float", -0.05, "손절 기준 수익률", -0.20, -0.01, 0.005),
        ParamSpec("buy_score_threshold", "최소 매수 점수", "int", 65, "매수 추천 최소 종합 점수", 0, 100, 5),
        ParamSpec("budget_per_stock_usd", "종목당 기본 예산(USD)", "float", 500.0, "1회 진입 시 배정 예산 ($)", 100.0, 50_000.0, 100.0),
    ]
