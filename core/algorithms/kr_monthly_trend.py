"""
KR Monthly Trend (GTAA) Algorithm (R4, R15).
Migrated from dalpan_kis/core/strategy/monthly_trend.py into unified Algorithm interface.
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
from core.indicators import normalize_ohlcv, resample_to_monthly
from core.ledger.models import Lot


@register_algorithm
class KRMonthlyTrendAlgorithm(Algorithm):
    id = "kr_monthly_trend"
    version = "1.0.0"
    name = "월봉 10이평 추세추종 (GTAA)"
    description = (
        "국내 대형주 대상 월봉 종가가 10개월 단순이동평균(SMA10)을 상향 돌파하거나 "
        "안정적으로 상회할 때 매수하고, 10개월선 하향 이탈 또는 비상 손절/목표 익절 시 청산합니다."
    )
    market = Market.KR
    _budget_param_key: str = "budget_per_stock_krw"
    _price_decimals: int = 0
    _currency_symbol: str = "원"
    params_schema = [
        ParamSpec("monthly_sma_period", "월봉 이평 기간", "int", 10, "추세 판별 월봉 SMA 기간 (개월)", 3, 24, 1),
        ParamSpec("allow_trend_continuation", "추세 지속 진입 허용", "bool", False, "신규 골든크로스 외 10이평 상회 지속 종목도 진입 허용"),
        ParamSpec("max_high_chase_pct", "이격도 과열 상한", "float", 0.20, "10개월선 대비 과열 추격 매수 차단 비율 (0.20 = +20%)", 0.05, 0.50, 0.01),
        ParamSpec("target_profit_rate", "목표 익절률", "float", 0.20, "목표 익절 수익률 (0.20 = +20%)", 0.05, 1.00, 0.05),
        ParamSpec("stop_loss_rate", "비상 손절률", "float", -0.07, "일봉 기준 비상 손절 수익률 (-0.07 = -7%)", -0.25, -0.02, 0.005),
        ParamSpec("buy_score_threshold", "최소 매수 점수", "int", 60, "매수 추천 최소 종합 점수 (0~100)", 0, 100, 5),
        ParamSpec("budget_per_stock_krw", "종목당 기본 예산(원)", "float", 1_000_000.0, "1회 진입 시 기본 배정 예산", 100_000.0, 20_000_000.0, 100_000.0),
        ParamSpec("high_price_limit_enabled", "100만원 초과 고가주 1주 제한", "bool", True, "주가 100만원 초과 시 최대 1주 제한"),
    ]

    def _round_price(self, price: float) -> float:
        return round(price, self._price_decimals)

    def _fmt_price(self, price: float) -> str:
        if self._price_decimals == 0:
            return f"{price:,.0f}{self._currency_symbol}"
        return f"{self._currency_symbol}{price:,.{self._price_decimals}f}"

    @staticmethod
    def _get_monthly_df(data: pd.DataFrame, extra: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
        if extra and isinstance(extra.get("monthly_df"), pd.DataFrame):
            return normalize_ohlcv(extra["monthly_df"])
        norm = normalize_ohlcv(data)
        if len(norm) >= 40:
            return resample_to_monthly(norm)
        return norm

    def evaluate_entry(
        self,
        symbol: str,
        data: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        p = self.merge_params(params)
        sma_period = int(p["monthly_sma_period"])
        monthly = self._get_monthly_df(data, extra)
        if monthly.empty or len(monthly) < sma_period + 1:
            return None

        monthly = monthly.copy()
        monthly["sma"] = monthly["close"].rolling(window=sma_period).mean()

        curr_close = float(monthly["close"].iloc[-1])
        curr_sma = float(monthly["sma"].iloc[-1])
        prev_close = float(monthly["close"].iloc[-2])
        prev_sma = float(monthly["sma"].iloc[-2])
        if pd.isna(curr_sma) or pd.isna(prev_sma) or curr_sma <= 0:
            return None

        is_golden_cross = (prev_close <= prev_sma) and (curr_close > curr_sma)
        is_continuation = (prev_close > prev_sma) and (curr_close > curr_sma)
        allow_cont = bool(p["allow_trend_continuation"])
        distance_pct = (curr_close - curr_sma) / curr_sma
        max_chase = float(p["max_high_chase_pct"])

        cond_above_sma = Condition(
            name=f"월봉종가 vs SMA({sma_period})",
            value=self._round_price(curr_close),
            threshold=self._round_price(curr_sma),
            op=">",
            passed=curr_close > curr_sma,
            desc="월봉 종가가 이동평균선 위에 위치",
        )
        cond_entry_type = Condition(
            name="골든크로스/지속추세",
            value="골든크로스" if is_golden_cross else ("추세지속" if is_continuation else "하락"),
            threshold="골든크로스" if not allow_cont else "상승추세",
            op="==",
            passed=is_golden_cross or (allow_cont and is_continuation),
            desc="신규 상향 돌파 또는 허용된 추세 지속",
        )
        cond_distance = Condition(
            name="SMA 이격도(%)",
            value=round(distance_pct * 100.0, 2),
            threshold=round(max_chase * 100.0, 2),
            op="<=",
            passed=distance_pct <= max_chase,
            desc="이평선 대비 과열 추격 매수 방지",
        )

        conditions: List[Condition] = [cond_above_sma, cond_entry_type, cond_distance]
        if not all(c.passed for c in conditions):
            return None

        score = 50.0 if is_golden_cross else 40.0
        if 0.01 <= distance_pct <= 0.08:
            score += 25.0
        elif 0.08 < distance_pct <= 0.15:
            score += 20.0
        else:
            score += 10.0

        if len(monthly) >= 3:
            p2_close = float(monthly["close"].iloc[-3])
            if curr_close > prev_close > p2_close:
                score += 15.0
            elif curr_close > prev_close:
                score += 10.0

        score = min(100.0, score)
        threshold_score = float(p["buy_score_threshold"])
        cond_score = Condition(
            name="종합점수",
            value=round(score, 1),
            threshold=threshold_score,
            op=">=",
            passed=score >= threshold_score,
            desc="최소 추천 점수 충족 여부",
        )
        conditions.append(cond_score)
        if not cond_score.passed:
            return None

        budget = float(p[self._budget_param_key])
        if bool(p.get("high_price_limit_enabled", False)) and curr_close > 1_000_000:
            qty = 1 if budget >= curr_close else 0
        else:
            qty = max(1, int(budget // curr_close)) if budget >= curr_close else 0

        if qty <= 0:
            return None

        tag = "월봉 골든크로스" if is_golden_cross else "월봉 추세 지속"
        return Signal(
            action=SignalAction.BUY,
            algo_id=self.id,
            algo_version=self.version,
            symbol=symbol,
            market=self.market,
            price_type=PriceType.LIMIT,
            price=self._round_price(curr_close),
            qty=qty,
            conditions=conditions,
            score=score,
            reason_text=f"{tag} (SMA{sma_period} 이격 +{distance_pct*100:.1f}%)",
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
        norm = normalize_ohlcv(data)
        if norm.empty:
            return None

        curr_price = float(norm["close"].iloc[-1])
        pnl_rate = (curr_price - lot.avg_price) / lot.avg_price
        stop_loss = float(p["stop_loss_rate"])
        target_profit = float(p["target_profit_rate"])
        sma_period = int(p["monthly_sma_period"])

        conditions: List[Condition] = []
        reason = ""

        # 1. Emergency stop loss
        if pnl_rate <= stop_loss:
            conditions.append(
                Condition("수익률(%)", round(pnl_rate * 100, 2), round(stop_loss * 100, 2), "<=", True, "비상 손절 도달")
            )
            reason = f"비상 손절 도달 ({pnl_rate*100:+.1f}% <= {stop_loss*100:.1f}%)"
        # 2. Target profit
        elif pnl_rate >= target_profit:
            conditions.append(
                Condition("수익률(%)", round(pnl_rate * 100, 2), round(target_profit * 100, 2), ">=", True, "목표 익절 도달")
            )
            reason = f"목표 익절 도달 ({pnl_rate*100:+.1f}% >= +{target_profit*100:.1f}%)"
        else:
            # 3. Monthly SMA dead cross
            monthly = self._get_monthly_df(norm, extra)
            if len(monthly) >= sma_period:
                curr_sma = float(monthly["close"].rolling(window=sma_period).mean().iloc[-1])
                if not pd.isna(curr_sma) and curr_price < curr_sma:
                    conditions.append(
                        Condition(
                            f"현재가 vs 월봉SMA({sma_period})",
                            self._round_price(curr_price),
                            self._round_price(curr_sma),
                            "<",
                            True,
                            "월봉 추세선 하향 이탈",
                        )
                    )
                    reason = f"월봉 {sma_period}이평({self._fmt_price(curr_sma)}) 하향 이탈"

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
class USMonthlyTrendAlgorithm(KRMonthlyTrendAlgorithm):
    id = "us_monthly_trend"
    version = "1.0.0"
    name = "월봉 10이평 추세추종 (GTAA)"
    description = (
        "미국 주식/ETF 대상 월봉 종가가 10개월 단순이동평균(SMA10)을 상향 돌파하거나 "
        "안정적으로 상회할 때 매수하고, 10개월선 하향 이탈 또는 비상 손절/목표 익절 시 청산합니다."
    )
    market = Market.US
    _budget_param_key = "budget_per_stock_usd"
    _price_decimals = 2
    _currency_symbol = "$"
    params_schema = [
        ParamSpec("monthly_sma_period", "월봉 이평 기간", "int", 10, "추세 판별 월봉 SMA 기간 (개월)", 3, 24, 1),
        ParamSpec("allow_trend_continuation", "추세 지속 진입 허용", "bool", False, "신규 골든크로스 외 10이평 상회 지속 종목도 진입 허용"),
        ParamSpec("max_high_chase_pct", "이격도 과열 상한", "float", 0.20, "10개월선 대비 과열 추격 매수 차단 비율 (0.20 = +20%)", 0.05, 0.50, 0.01),
        ParamSpec("target_profit_rate", "목표 익절률", "float", 0.20, "목표 익절 수익률 (0.20 = +20%)", 0.05, 1.00, 0.05),
        ParamSpec("stop_loss_rate", "비상 손절률", "float", -0.07, "일봉 기준 비상 손절 수익률 (-0.07 = -7%)", -0.25, -0.02, 0.005),
        ParamSpec("buy_score_threshold", "최소 매수 점수", "int", 60, "매수 추천 최소 종합 점수 (0~100)", 0, 100, 5),
        ParamSpec("budget_per_stock_usd", "종목당 기본 예산(USD)", "float", 1_000.0, "1회 진입 시 기본 배정 예산 ($)", 100.0, 50_000.0, 100.0),
    ]
