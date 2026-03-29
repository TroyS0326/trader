from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict


@dataclass
class SymbolMarketStats:
    symbol: str
    price: float
    daily_dollar_volume: float
    spread_pct: float


@dataclass
class ScoreTriplet:
    opportunity: int
    tradability: int
    entry_quality: int

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)
