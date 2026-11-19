"""
KR Fujimoto Shigeru 1:2:6 Split-Entry & Split-Exit Algorithm (R4.4, R15).

Entry stages (1 : 2 : 6 = total 9 units):
- Stage 1 (1/9): RSI(14) oversold rebound (prev <= 30 and curr > 30, or curr <= 32 turning up)
- Stage 2 (2/9): MACD Golden Cross (macd_hist turns positive)
- Stage 3 (6/9): Ichimoku Cloud breakout (close > cloud_top)

Exit stages (reverse 1 : 2 : 6, migrated from dalpan_kis/core/indicators.py):
- Exit 1 (1/9): RSI(14) overbought >= 70 then drops below 70 (or profit >= 3% & RSI turning down below tenkan)
- Exit 2 (2/9): MACD Dead Cross (macd_hist < 0)
- Exit 3 (6/9, all remaining): Close breaks below Ichimoku cloud_bottom or stop loss
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
from core.indicators import calculate_technical_indicators
from core.ledger.models import Lot


@register_algorithm
class KRFujimoto126Algorithm(Algorithm):
    id = "kr_fujimoto_126"
    version = "1.0.0"
    name = "후지모토 1:2:6 분할매매"
    description = (
        "1단계(RSI 과매도 반등, 1/9) → 2단계(MACD 골든크로스, 2/9) → 3단계(일목 구름대 돌파, 6/9)로 "
        "분할 매수하고, 역방향 1:2:6(RSI 과열이탈 → MACD 데드크로스 → 구름대 하단이탈)으로 분할 청산합니다."
    )
    market = Market.KR
    params_schema = [
        ParamSpec("total_budget_krw", "종목당 총 9유닛 예산(원)", "float", 1_800_000.0, "1:2:6 전체(9유닛) 합산 예산", 90_000.0, 50_000_000.0, 90_000.0),
        ParamSpec("rsi_oversold", "1단계 RSI 과매도 기준", "float", 30.0, "1단계 매수 RSI 기준선", 15.0, 45.0, 1.0),
        ParamSpec("rsi_overbought", "1차 청산 RSI 과열 기준", "float", 70.0, "1차 청산 RSI 과열 기준선", 60.0, 85.0, 1.0),
        ParamSpec("stop_loss_rate", "비상 손절률", "float", -0.05, "비상 손절 기준 수익률 (-0.05 = -5%)", -0.20, -0.01, 0.005),
    ]

    def evaluate_entry(
        self,
        symbol: str,
        data: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Signal]:
        """
        Evaluate Stage 1 (initial) or Stage 2 / Stage 3 add-on entry.
        Pass `extra={"existing_lot": lot}` to check next split-buy stage on an existing OPEN lot.
        """
        p = self.merge_params(params)
        df = calculate_technical_indicators(data, min_bars=26)
        if df is None or len(df) < 26:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        curr_price = float(last["close"])
        curr_rsi = float(last["rsi14"])
        prev_rsi = float(prev["rsi14"])
        macd_hist = float(last["macd_hist"])
        prev_macd_hist = float(prev["macd_hist"])
        cloud_top = float(last["cloud_top"])

        existing_lot: Optional[Lot] = (extra or {}).get("existing_lot")
        curr_entry_stage = int(existing_lot.algo_state.get("entry_stage", 0)) if existing_lot else 0

        rsi_th = float(p["rsi_oversold"])
        unit_budget = float(p["total_budget_krw"]) / 9.0

        # Stage 1 (1/9): RSI oversold bounce
        if curr_entry_stage == 0:
            rsi_bounce = (prev_rsi <= rsi_th and curr_rsi > rsi_th) or (curr_rsi <= rsi_th + 2.0 and curr_rsi > prev_rsi)
            cond = Condition("RSI(14) 과매도 반등", round(curr_rsi, 1), rsi_th, "<=", rsi_bounce, "RSI 30 부근 반등 탐색 진입")
            if not rsi_bounce:
                return None
            qty = max(1, int((unit_budget * 1.0) // curr_price)) if (unit_budget * 1.0) >= curr_price else 1
            return Signal(
                action=SignalAction.BUY,
                algo_id=self.id,
                algo_version=self.version,
                symbol=symbol,
                market=self.market,
                price_type=PriceType.LIMIT,
                price=round(curr_price, 0),
                qty=qty,
                qty_ratio=round(1.0 / 9.0, 4),
                conditions=[cond],
                score=70.0,
                reason_text=f"후지모토 1단계 탐색 매수(1/9) - RSI={curr_rsi:.1f}",
                metadata={"next_entry_stage": 1},
            )

        # Stage 2 (2/9): MACD Golden Cross
        if curr_entry_stage == 1:
            macd_golden = (prev_macd_hist <= 0.0) and (macd_hist > 0.0)
            cond = Condition("MACD Hist 골든크로스", round(macd_hist, 2), 0.0, ">", macd_golden, "MACD 시그널 상향 돌파")
            if not macd_golden:
                return None
            qty = max(1, int((unit_budget * 2.0) // curr_price)) if (unit_budget * 2.0) >= curr_price else 1
            return Signal(
                action=SignalAction.BUY,
                algo_id=self.id,
                algo_version=self.version,
                symbol=symbol,
                market=self.market,
                price_type=PriceType.LIMIT,
                price=round(curr_price, 0),
                qty=qty,
                qty_ratio=round(2.0 / 9.0, 4),
                conditions=[cond],
                score=80.0,
                reason_text=f"후지모토 2단계 추가 매수(2/9) - MACD 골든크로스(Hist={macd_hist:.2f})",
                lot_id=existing_lot.lot_id if existing_lot else None,
                metadata={"next_entry_stage": 2},
            )

        # Stage 3 (6/9): Ichimoku Cloud breakout
        if curr_entry_stage == 2:
            cloud_break = curr_price > cloud_top
            cond = Condition("일목 구름대 돌파", round(curr_price, 0), round(cloud_top, 0), ">", cloud_break, "구름대 상단 돌파")
            if not cloud_break:
                return None
            qty = max(1, int((unit_budget * 6.0) // curr_price)) if (unit_budget * 6.0) >= curr_price else 1
            return Signal(
                action=SignalAction.BUY,
                algo_id=self.id,
                algo_version=self.version,
                symbol=symbol,
                market=self.market,
                price_type=PriceType.LIMIT,
                price=round(curr_price, 0),
                qty=qty,
                qty_ratio=round(6.0 / 9.0, 4),
                conditions=[cond],
                score=90.0,
                reason_text=f"후지모토 3단계 본격 매수(6/9) - 구름대 상단({cloud_top:,.0f}원) 돌파",
                lot_id=existing_lot.lot_id if existing_lot else None,
                metadata={"next_entry_stage": 3},
            )

        return None

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
        if df is None or len(df) < 2:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        curr_price = float(last["close"])
        curr_rsi = float(last["rsi14"])
        prev_rsi = float(prev["rsi14"])
        macd_hist = float(last["macd_hist"])
        prev_macd_hist = float(prev["macd_hist"])
        tenkan = float(last["tenkan_sen"])
        cloud_bottom = float(last["cloud_bottom"])

        pnl_rate = (curr_price - lot.avg_price) / lot.avg_price
        stop_loss = float(p["stop_loss_rate"])
        rsi_ob = float(p["rsi_overbought"])
        current_exit_stage = int(lot.algo_state.get("exit_stage", 0))

        recent_max_rsi = float(df["rsi14"].tail(6).iloc[:-1].max())

        stage3_cloud = curr_price < cloud_bottom
        stage3_stop = pnl_rate <= stop_loss
        stage2_macd = (prev_macd_hist >= 0.0 and macd_hist < 0.0) or (macd_hist < 0.0 and current_exit_stage < 2 and float(df["macd_hist"].tail(10).max()) > 0.0)
        stage1_rsi = (recent_max_rsi >= rsi_ob and curr_rsi < rsi_ob) or (
            pnl_rate >= 0.03 and recent_max_rsi >= (rsi_ob - 5.0) and curr_rsi < prev_rsi and curr_price < tenkan
        )

        target_stage = 0
        conditions: List[Condition] = []
        reason = ""

        if stage3_stop or stage3_cloud:
            target_stage = 3
            if stage3_stop:
                conditions.append(Condition("수익률(%)", round(pnl_rate * 100, 2), round(stop_loss * 100, 2), "<=", True, "비상 손절 도달"))
                reason = f"3차 전량 청산 (비상 손절 {pnl_rate*100:+.1f}%)"
            else:
                conditions.append(Condition("일목 구름대 하단 이탈", round(curr_price, 0), round(cloud_bottom, 0), "<", True, "추세 종료"))
                reason = f"3차 전량 청산 (구름대 하단 {cloud_bottom:,.0f}원 이탈)"
        elif stage2_macd:
            target_stage = 2
            conditions.append(Condition("MACD Hist 데드크로스", round(macd_hist, 2), 0.0, "<", True, "모멘텀 음전환"))
            reason = f"2차 분할 청산 (MACD 데드크로스 Hist={macd_hist:.2f})"
        elif stage1_rsi:
            target_stage = 1
            conditions.append(Condition("RSI(14) 과열 이탈", round(curr_rsi, 1), rsi_ob, "<", True, f"최근 고점 RSI {recent_max_rsi:.1f} 후 이탈"))
            reason = f"1차 분할 청산 (RSI {recent_max_rsi:.1f} -> {curr_rsi:.1f})"

        if target_stage <= current_exit_stage:
            return None

        if target_stage == 3:
            sell_qty = lot.qty
            ratio = 1.0
        elif target_stage == 2:
            ratio = (3.0 / 9.0) if current_exit_stage == 0 else (2.0 / 8.0)
            sell_qty = min(lot.qty, max(1, int(round(lot.qty * ratio))))
        else:
            ratio = 1.0 / 9.0
            sell_qty = min(lot.qty, max(1, int(round(lot.qty * ratio))))

        return Signal(
            action=SignalAction.SELL,
            algo_id=self.id,
            algo_version=self.version,
            symbol=lot.symbol,
            market=self.market,
            price_type=PriceType.LIMIT,
            price=round(curr_price, 0),
            qty=sell_qty,
            qty_ratio=round(ratio, 4),
            conditions=conditions,
            score=100.0,
            reason_text=reason,
            lot_id=lot.lot_id,
            metadata={"next_exit_stage": target_stage},
        )
