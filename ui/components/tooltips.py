"""
Automatic tooltip builders generated from Algorithm metadata & Signal conditions (R3.2, R4.2).
"""
from __future__ import annotations

from typing import Iterable

from core.algorithms.base import Algorithm, Condition, Signal


def build_algorithm_tooltip(algo: Algorithm) -> str:
    return algo.build_help_text()


def build_signal_tooltip(signal: Signal) -> str:
    return signal.format_conditions_tooltip()


def format_conditions_inline(conditions: Iterable[Condition]) -> str:
    return " | ".join(c.format_badge() for c in conditions)
