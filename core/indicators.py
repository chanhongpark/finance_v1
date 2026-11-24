"""
Technical indicators migrated from dalpan_kis/core/indicators.py.
Pure DataFrame transformations without global state.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize column names to lowercase: date, open, high, low, close, volume."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    out = df.copy()
    if "date" not in out.columns and "Date" not in out.columns:
        if isinstance(out.index, pd.DatetimeIndex):
            out = out.reset_index()
            out.rename(columns={out.columns[0]: "date"}, inplace=True)

    rename_map = {}
    for col in out.columns:
        low = str(col).strip().lower()
        if low in {"date", "open", "high", "low", "close", "volume"}:
            rename_map[col] = low
    out = out.rename(columns=rename_map)

    for col in ["open", "high", "low", "close", "volume"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    if "close" not in out.columns:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0].copy()

    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        out = out.dropna(subset=["date"]).sort_values("date").drop_duplicates(subset=["date"], keep="last")

    for col in ["open", "high", "low"]:
        if col not in out.columns or out[col].isna().all():
            out[col] = out["close"]
        else:
            out[col] = out[col].fillna(out["close"])

    if "volume" not in out.columns or out["volume"].isna().all():
        out["volume"] = 0.0
    else:
        out["volume"] = out["volume"].fillna(0.0)

    return out.reset_index(drop=True)


def calculate_technical_indicators(
    df: pd.DataFrame,
    atr_period: int = 14,
    min_bars: int = 20,
) -> Optional[pd.DataFrame]:
    """Compute MA5/20/60, vol_ma20, RSI14, Bollinger Bands, ATR, MACD, and Ichimoku Cloud."""
    work = normalize_ohlcv(df)
    if len(work) < max(1, int(min_bars)):
        return None

    work["ma5"] = work["close"].rolling(window=5, min_periods=1).mean()
    work["ma20"] = work["close"].rolling(window=20, min_periods=1).mean()
    work["ma60"] = work["close"].rolling(window=min(60, len(work)), min_periods=1).mean()
    work["vol_ma20"] = work["volume"].shift(1).rolling(window=20, min_periods=1).mean().fillna(work["volume"])

    # RSI(14)
    delta = work["close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=14, min_periods=1).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=14, min_periods=1).mean()
    rs = gain / (loss + 1e-9)
    work["rsi14"] = (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)

    # Bollinger Bands(20, 2)
    work["bb_mid"] = work["ma20"]
    work["bb_std"] = work["close"].rolling(window=20, min_periods=1).std().fillna(0.0)
    work["bb_upper"] = work["bb_mid"] + (work["bb_std"] * 2.0)
    work["bb_lower"] = work["bb_mid"] - (work["bb_std"] * 2.0)

    # ATR(14)
    prev_close = work["close"].shift(1).fillna(work["open"])
    tr = pd.concat(
        [
            work["high"] - work["low"],
            (work["high"] - prev_close).abs(),
            (work["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    work["atr"] = tr.rolling(window=atr_period, min_periods=1).mean().fillna(0.0)

    # MACD (12, 26, 9)
    ema12 = work["close"].ewm(span=12, adjust=False).mean()
    ema26 = work["close"].ewm(span=26, adjust=False).mean()
    work["macd_line"] = ema12 - ema26
    work["macd_signal"] = work["macd_line"].ewm(span=9, adjust=False).mean()
    work["macd_hist"] = work["macd_line"] - work["macd_signal"]

    # Ichimoku Cloud (9, 26, 52)
    high_9 = work["high"].rolling(window=9, min_periods=1).max()
    low_9 = work["low"].rolling(window=9, min_periods=1).min()
    work["tenkan_sen"] = (high_9 + low_9) / 2.0

    high_26 = work["high"].rolling(window=26, min_periods=1).max()
    low_26 = work["low"].rolling(window=26, min_periods=1).min()
    work["kijun_sen"] = (high_26 + low_26) / 2.0

    span_a_raw = (work["tenkan_sen"] + work["kijun_sen"]) / 2.0
    high_52 = work["high"].rolling(window=52, min_periods=1).max()
    low_52 = work["low"].rolling(window=52, min_periods=1).min()
    span_b_raw = (high_52 + low_52) / 2.0

    work["senkou_span_a"] = span_a_raw.shift(26).fillna(span_a_raw)
    work["senkou_span_b"] = span_b_raw.shift(26).fillna(span_b_raw)
    work["cloud_top"] = work[["senkou_span_a", "senkou_span_b"]].max(axis=1)
    work["cloud_bottom"] = work[["senkou_span_a", "senkou_span_b"]].min(axis=1)

    return work


def resample_to_monthly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV DataFrame into monthly bars."""
    work = normalize_ohlcv(daily_df)
    if work.empty or "date" not in work.columns:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    work["dt"] = pd.to_datetime(work["date"])
    work = work.set_index("dt")
    monthly = work.resample("ME").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna(subset=["close"])
    monthly = monthly.reset_index()
    monthly["date"] = monthly["dt"].dt.strftime("%Y-%m-%d")
    return monthly[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def detect_cup_and_handle(
    df: pd.DataFrame,
    min_cup_bars: int = 20,
    max_cup_bars: int = 80,
    min_handle_bars: int = 3,
    max_handle_bars: int = 25,
    min_cup_depth: float = 0.08,
    max_cup_depth: float = 0.40,
    max_rim_diff: float = 0.08,
    max_handle_depth_ratio: float = 0.50,
    vol_breakout_mult: float = 1.3,
) -> Optional[Dict[str, Any]]:
    """Detect Cup & Handle chart pattern (migrated from dalpan_kis/core/indicators.py)."""
    min_required_len = min_cup_bars + min_handle_bars + 15
    if df is None or len(df) < min_required_len:
        return None

    for col in ["high", "low", "close", "volume"]:
        if col not in df.columns:
            return None

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    vol = df["volume"].values
    n = len(df)
    curr_idx = n - 1
    curr_price = float(close[curr_idx])
    curr_vol = float(vol[curr_idx])
    vol_ma20 = (
        float(df["vol_ma20"].values[curr_idx])
        if "vol_ma20" in df.columns
        else float(np.mean(vol[max(0, curr_idx - 20):curr_idx]))
    )
    if vol_ma20 <= 0:
        vol_ma20 = 1.0

    best_pattern: Optional[Dict[str, Any]] = None
    best_score = -1

    for h_len in range(min_handle_bars, min_handle_bars + min(15, max_handle_bars - min_handle_bars + 1)):
        p2_idx = curr_idx - h_len
        if p2_idx < min_cup_bars + 10:
            continue

        p2_start = max(0, p2_idx - 3)
        p2_end = min(curr_idx - 1, p2_idx + 3)
        actual_p2_idx = p2_start + int(np.argmax(high[p2_start:p2_end + 1]))
        p2_price = float(high[actual_p2_idx])
        if p2_price <= 0:
            continue

        handle_highs = high[actual_p2_idx + 1:curr_idx]
        handle_lows = low[actual_p2_idx + 1:curr_idx]
        handle_vols = vol[actual_p2_idx + 1:curr_idx]
        if len(handle_lows) < min_handle_bars:
            continue

        handle_low = float(np.min(handle_lows))
        handle_high = float(np.max(handle_highs))
        handle_depth = (p2_price - handle_low) / p2_price

        for cup_len in range(min_cup_bars, min(max_cup_bars, actual_p2_idx - 5), 3):
            p1_start = max(5, actual_p2_idx - cup_len)
            p1_end = max(5, actual_p2_idx - min_cup_bars)
            if p1_start >= p1_end:
                continue

            p1_idx = p1_start + int(np.argmax(high[p1_start:p1_end]))
            p1_price = float(high[p1_idx])
            if p1_price <= 0:
                continue

            cup_inner_lows = low[p1_idx + 1:actual_p2_idx]
            if len(cup_inner_lows) < 8:
                continue

            cup_low_rel = int(np.argmin(cup_inner_lows))
            cup_low_idx = p1_idx + 1 + cup_low_rel
            cup_low = float(cup_inner_lows[cup_low_rel])

            cup_depth = (p1_price - cup_low) / p1_price
            if not (min_cup_depth <= cup_depth <= max_cup_depth):
                continue

            rim_diff = abs(p2_price - p1_price) / p1_price
            if rim_diff > max_rim_diff:
                continue

            cup_total_bars = actual_p2_idx - p1_idx
            low_pos_ratio = (cup_low_idx - p1_idx) / max(1, cup_total_bars)
            if not (0.25 <= low_pos_ratio <= 0.75):
                continue

            if handle_depth > cup_depth * max_handle_depth_ratio or handle_depth > 0.18:
                continue

            cup_midpoint = cup_low + (p1_price - cup_low) * (1.0 - max_handle_depth_ratio)
            if handle_low < cup_midpoint:
                continue

            pre_start = max(0, p1_idx - 25)
            if p1_idx <= pre_start:
                continue
            pre_low = float(np.min(low[pre_start:p1_idx]))
            pre_gain = (p1_price - pre_low) / max(1e-5, pre_low)
            if pre_gain < 0.12:
                continue

            pivot_price = max(p2_price, handle_high)
            is_breakout = curr_price >= pivot_price
            is_near_pivot = (curr_price >= pivot_price * 0.98) and not is_breakout
            vol_ratio = curr_vol / max(1.0, vol_ma20)
            vol_confirmed = vol_ratio >= vol_breakout_mult

            score = 60
            if rim_diff <= 0.03:
                score += 10
            elif rim_diff <= 0.05:
                score += 5

            cup_vol_mean = float(np.mean(vol[cup_low_idx:actual_p2_idx])) if cup_low_idx < actual_p2_idx else 1.0
            handle_vol_mean = float(np.mean(handle_vols)) if len(handle_vols) > 0 else 1.0
            if handle_vol_mean < cup_vol_mean * 0.75:
                score += 10

            if is_breakout and vol_confirmed:
                score += 15
            elif is_breakout or is_near_pivot:
                score += 5

            near_bottom = cup_low + (p1_price - cup_low) * 0.20
            if int(np.sum(cup_inner_lows <= near_bottom)) >= 4:
                score += 5

            if score > best_score:
                best_score = score
                target_price = pivot_price + (p2_price - cup_low)
                stop_loss_price = max(handle_low, pivot_price * 0.93)
                handle_low_idx = actual_p2_idx + 1 + int(np.argmin(handle_lows))
                dates_arr = df["date"].astype(str).values if "date" in df.columns else None
                best_pattern = {
                    "is_pattern": True,
                    "status": "breakout" if is_breakout else ("near_pivot" if is_near_pivot else "forming_handle"),
                    "p1_price": round(p1_price, 2),
                    "cup_low_price": round(cup_low, 2),
                    "cup_depth_pct": round(cup_depth * 100.0, 2),
                    "p2_price": round(p2_price, 2),
                    "handle_low_price": round(handle_low, 2),
                    "handle_depth_pct": round(handle_depth * 100.0, 2),
                    "pivot_price": round(pivot_price, 2),
                    "target_price": round(target_price, 2),
                    "stop_loss_price": round(stop_loss_price, 2),
                    "is_breakout": bool(is_breakout),
                    "vol_ratio": round(vol_ratio, 2),
                    "vol_confirmed": bool(vol_confirmed),
                    "score": min(100, score),
                    "p1_date": str(dates_arr[p1_idx])[:10] if dates_arr is not None else None,
                    "cup_low_date": str(dates_arr[cup_low_idx])[:10] if dates_arr is not None else None,
                    "p2_date": str(dates_arr[actual_p2_idx])[:10] if dates_arr is not None else None,
                    "handle_low_date": str(dates_arr[handle_low_idx])[:10] if dates_arr is not None else None,
                    "breakout_date": str(dates_arr[curr_idx])[:10] if dates_arr is not None else None,
                }

    return best_pattern
