"""
Unified Algorithm interface, registry, and auto-registration of all 4 active algorithms:
- KR (3): kr_monthly_trend, kr_cup_and_handle, kr_fujimoto_126
- US (1): us_infinite_buying
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

# Auto-register all 4 algorithms on package import (R4.5, R15)
from core.algorithms.kr_cup_and_handle import KRCupAndHandleAlgorithm
from core.algorithms.kr_fujimoto_126 import KRFujimoto126Algorithm
from core.algorithms.kr_monthly_trend import KRMonthlyTrendAlgorithm
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
    "USInfiniteBuyingAlgorithm",
]
