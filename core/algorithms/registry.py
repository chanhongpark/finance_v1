"""
Central Algorithm Registry (R4.5, R14).

Registering an algorithm makes it automatically available to:
- Streamlit UI (Recommendations, Settings, Tooltips)
- Exit evaluation (always resolves latest version for R14)
- GitHub Actions US Infinite Buying runner
"""
from __future__ import annotations

from typing import Dict, List, Optional, Type

from core.algorithms.base import Algorithm, Market


class AlgorithmRegistry:
    def __init__(self) -> None:
        self._algorithms: Dict[str, Algorithm] = {}

    def register(self, cls: Type[Algorithm]) -> Type[Algorithm]:
        instance = cls()
        if not instance.id:
            raise ValueError(f"Algorithm class {cls.__name__} must define a non-empty 'id'")
        self._algorithms[instance.id] = instance
        return cls

    def register_instance(self, instance: Algorithm) -> None:
        if not instance.id:
            raise ValueError("Algorithm instance must define a non-empty 'id'")
        self._algorithms[instance.id] = instance

    def get(self, algo_id: str) -> Optional[Algorithm]:
        """Always returns the latest registered version of `algo_id` (R14)."""
        return self._algorithms.get(algo_id)

    def require(self, algo_id: str) -> Algorithm:
        algo = self.get(algo_id)
        if algo is None:
            raise KeyError(f"Algorithm '{algo_id}' is not registered")
        return algo

    def list_all(self, market: Optional[Market] = None) -> List[Algorithm]:
        algos = list(self._algorithms.values())
        if market is not None:
            algos = [a for a in algos if a.market == market]
        return sorted(algos, key=lambda a: (a.market.value, a.id))


# Application-level registry (holds stateless code definitions only — zero user data!)
default_registry = AlgorithmRegistry()


def register_algorithm(cls: Type[Algorithm]) -> Type[Algorithm]:
    return default_registry.register(cls)
