"""
KR / US market calendar, timezone, and US Daylight Saving Time utilities (R10).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional, Set
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
US_EASTERN = ZoneInfo("America/New_York")

# Fixed & major recurring holidays (YYYY-MM-DD can be extended per year)
KR_FIXED_MONTH_DAY: Set[tuple[int, int]] = {
    (1, 1),   # 신정
    (3, 1),   # 삼일절
    (5, 1),   # 근로자의 날 (증시 휴장)
    (5, 5),   # 어린이날
    (6, 6),   # 현충일
    (8, 15),  # 광복절
    (10, 3),  # 개천절
    (10, 9),  # 한글날
    (12, 25), # 성탄절
    (12, 31), # 연말 휴장
}

US_FIXED_MONTH_DAY: Set[tuple[int, int]] = {
    (1, 1),   # New Year's Day
    (6, 19),  # Juneteenth
    (7, 4),   # Independence Day
    (12, 25), # Christmas Day
}


def now_kst() -> datetime:
    return datetime.now(KST)


def now_us_eastern() -> datetime:
    return datetime.now(US_EASTERN)


def is_us_dst(dt: Optional[datetime] = None) -> bool:
    """Return True if US Eastern Time is currently observing Daylight Saving Time (EDT)."""
    target = (dt or now_us_eastern()).astimezone(US_EASTERN)
    dst_offset = target.dst()
    return bool(dst_offset and dst_offset.total_seconds() > 0)


def is_kr_market_holiday(d: Optional[date] = None, extra_holidays: Optional[Set[str]] = None) -> bool:
    target = d or now_kst().date()
    if target.weekday() >= 5:
        return True
    if (target.month, target.day) in KR_FIXED_MONTH_DAY:
        return True
    if extra_holidays and target.strftime("%Y-%m-%d") in extra_holidays:
        return True
    return False


def is_us_market_holiday(d: Optional[date] = None, extra_holidays: Optional[Set[str]] = None) -> bool:
    target = d or now_us_eastern().date()
    if target.weekday() >= 5:
        return True
    if (target.month, target.day) in US_FIXED_MONTH_DAY:
        return True
    if extra_holidays and target.strftime("%Y-%m-%d") in extra_holidays:
        return True
    return False


def is_kr_regular_session_open(dt: Optional[datetime] = None) -> bool:
    kst_dt = (dt or now_kst()).astimezone(KST)
    if is_kr_market_holiday(kst_dt.date()):
        return False
    return time(9, 0) <= kst_dt.time() <= time(15, 30)


def is_us_regular_session_open(dt: Optional[datetime] = None) -> bool:
    et_dt = (dt or now_us_eastern()).astimezone(US_EASTERN)
    if is_us_market_holiday(et_dt.date()):
        return False
    return time(9, 30) <= et_dt.time() <= time(16, 0)


def market_now(market: str, dt: Optional[datetime] = None) -> datetime:
    """Return `dt` (or current time) converted to the market's local timezone."""
    tz = US_EASTERN if market.upper() == "US" else KST
    if dt is None:
        return datetime.now(tz)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def is_market_regular_session_open(
    market: str,
    dt: Optional[datetime] = None,
    extra_holidays: Optional[Set[str]] = None,
) -> bool:
    """Return True if the given market's regular trading session is currently open."""
    mkt = market.upper()
    local_dt = market_now(mkt, dt)
    if mkt == "US":
        if is_us_market_holiday(local_dt.date(), extra_holidays=extra_holidays):
            return False
        return time(9, 30) <= local_dt.time() <= time(16, 0)
    if is_kr_market_holiday(local_dt.date(), extra_holidays=extra_holidays):
        return False
    return time(9, 0) <= local_dt.time() <= time(15, 30)


def last_closed_session(
    market: str,
    dt: Optional[datetime] = None,
    extra_holidays: Optional[Set[str]] = None,
) -> date:
    """
    Return the date (in the market's own timezone) of the most recently completed regular session.
    Before/during a trading session, returns the previous trading day so incomplete intraday bars
    are never cached to disk as final daily candles.
    """
    mkt = market.upper()
    local_dt = market_now(mkt, dt)
    close_time = time(16, 0) if mkt == "US" else time(15, 30)
    is_holiday_fn = is_us_market_holiday if mkt == "US" else is_kr_market_holiday

    d = local_dt.date()
    if not is_holiday_fn(d, extra_holidays=extra_holidays) and local_dt.time() > close_time:
        return d

    d -= timedelta(days=1)
    while is_holiday_fn(d, extra_holidays=extra_holidays):
        d -= timedelta(days=1)
    return d
