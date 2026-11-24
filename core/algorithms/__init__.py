"""
Unified Algorithm interface, registry, and auto-registration of all 7 active algorithms:
- KR (3): kr_monthly_trend, kr_cup_and_handle, kr_fujimoto_126
- US (4): us_monthly_trend, us_cup_and_handle, us_fujimoto_126, us_infinite_buying
"""
from core.algorithms.base import (
    Algorithm,
    Condition,
    Market,
    ParamSpec,
    PriceType,
    Signal,
    SignalAction,
)
from core.algorithms.registry import AlgorithmRegistry, default_registry, register_algorithm

# Auto-register all algorithms on package import (R4.5, R15)
from core.algorithms.kr_cup_and_handle import KRCupAndHandleAlgorithm, USCupAndHandleAlgorithm
from core.algorithms.kr_fujimoto_126 import KRFujimoto126Algorithm, USFujimoto126Algorithm
from core.algorithms.kr_monthly_trend import KRMonthlyTrendAlgorithm, USMonthlyTrendAlgorithm
from core.algorithms.us_infinite_buying import USInfiniteBuyingAlgorithm

__all__ = [
    "Algorithm",
    "AlgorithmRegistry",
    "Condition",
    "Market",
    "ParamSpec",
    "PriceType",
    "Signal",
    "SignalAction",
    "default_registry",
    "register_algorithm",
    "KRMonthlyTrendAlgorithm",
    "KRCupAndHandleAlgorithm",
    "KRFujimoto126Algorithm",
    "USMonthlyTrendAlgorithm",
    "USCupAndHandleAlgorithm",
    "USFujimoto126Algorithm",
    "USInfiniteBuyingAlgorithm",
]
