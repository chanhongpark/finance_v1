"""
KR / US market calendar, timezone, and US Daylight Saving Time utilities (R10).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import FrozenSet, Optional, Set
from zoneinfo import ZoneInfo

try:
    import holidays as _holidays_lib
except ImportError:  # pragma: no cover - fallback when holidays is not installed
    _holidays_lib = None

KST = ZoneInfo("Asia/Seoul")
US_EASTERN = ZoneInfo("America/New_York")

# Fixed fallback holidays (used when `holidays` package is unavailable)
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


@lru_cache(maxsize=32)
def _krx_holidays_for_year(year: int) -> FrozenSet[date]:
    days: Set[date] = {date(year, 12, 31)}  # KRX year-end closing day
    if _holidays_lib is not None:
        kr = _holidays_lib.country_holidays("KR", years=year)
        for d in kr.keys():
            # Constitution Day (Jul 17) is not a KRX trading holiday
            if (d.month, d.day) == (7, 17):
                continue
            days.add(d)
    else:
        for m, day in KR_FIXED_MONTH_DAY:
            days.add(date(year, m, day))
    return frozenset(days)


@lru_cache(maxsize=32)
def _nyse_holidays_for_year(year: int) -> FrozenSet[date]:
    days: Set[date] = set()
    if _holidays_lib is not None:
        nyse = _holidays_lib.financial_holidays("NYSE", years=year)
        days.update(nyse.keys())
    else:
        for m, day in US_FIXED_MONTH_DAY:
            days.add(date(year, m, day))
    return frozenset(days)


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
    if target in _krx_holidays_for_year(target.year):
        return True
    if extra_holidays and target.strftime("%Y-%m-%d") in extra_holidays:
        return True
    return False


def is_us_market_holiday(d: Optional[date] = None, extra_holidays: Optional[Set[str]] = None) -> bool:
    target = d or now_us_eastern().date()
    if target.weekday() >= 5:
        return True
    if target in _nyse_holidays_for_year(target.year):
        return True
    if extra_holidays and target.strftime("%Y-%m-%d") in extra_holidays:
        return True
    return False


def is_kr_regular_session_open(dt: Optional[datetime] = None) -> bool:
    kst_dt = market_now("KR", dt)
    if is_kr_market_holiday(kst_dt.date()):
        return False
    return time(9, 0) <= kst_dt.time() <= time(15, 30)


def is_us_regular_session_open(dt: Optional[datetime] = None) -> bool:
    et_dt = market_now("US", dt)
    if is_us_market_holiday(et_dt.date()):
        return False
    return time(9, 30) <= et_dt.time() <= time(16, 0)


def market_now(market: str, dt: Optional[datetime] = None) -> datetime:
    """Return `dt` (or current time) converted to the market's local timezone.
    Naive datetimes are interpreted in the host's local timezone before conversion.
    """
    tz = US_EASTERN if market.upper() == "US" else KST
    if dt is None:
        return datetime.now(tz)
    if dt.tzinfo is None:
        return dt.astimezone().astimezone(tz)
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
