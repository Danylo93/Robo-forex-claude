"""Features de microestrutura calculadas em janela deslizante de ticks."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import fmean, pstdev
from typing import Optional

from .models import Tick


@dataclass
class Features:
    """Fotografia do estado do mercado no tick atual."""

    mid: float
    microprice: float
    spread_pips: float
    z_score: float  # desvio do mid em relação à média da janela
    volatility_bps: float  # desvio padrão dos retornos, em pontos-base
    momentum_bps: float  # variação do mid na janela, em pontos-base
    imbalance: float  # desequilíbrio médio do livro em [-1, 1]
    ticks_per_second: float
    ready: bool

    @property
    def reversion_edge_bps(self) -> float:
        """Vantagem esperada ao apostar na volta à média."""
        return abs(self.z_score) * self.volatility_bps


class FeatureWindow:
    """Mantém a janela de ticks e devolve as features do momento."""

    def __init__(self, size: int = 120, warmup: int = 200, pip: float = 0.0001):
        self.size = max(size, 10)
        self.warmup = max(warmup, self.size)
        self.pip = pip
        self.mids: deque[float] = deque(maxlen=self.size)
        self.imbalances: deque[float] = deque(maxlen=self.size)
        self.stamps: deque[float] = deque(maxlen=self.size)
        self.seen = 0
        self.last: Optional[Tick] = None

    def update(self, tick: Tick) -> Features:
        self.last = tick
        self.seen += 1
        self.mids.append(tick.mid)
        self.imbalances.append(tick.imbalance)
        self.stamps.append(tick.ts.timestamp())
        return self.compute()

    def compute(self) -> Features:
        tick = self.last
        if tick is None:
            return Features(0, 0, 0, 0, 0, 0, 0, 0, False)
        mids = list(self.mids)
        mean = fmean(mids)
        std = pstdev(mids) if len(mids) > 1 else 0.0
        z = (tick.mid - mean) / std if std > 0 else 0.0

        returns = [
            (b - a) / a * 10_000.0 for a, b in zip(mids, mids[1:]) if a  # em bps
        ]
        volatility = pstdev(returns) if len(returns) > 1 else 0.0
        momentum = (mids[-1] - mids[0]) / mids[0] * 10_000.0 if mids[0] else 0.0
        elapsed = (self.stamps[-1] - self.stamps[0]) if len(self.stamps) > 1 else 0.0
        rate = (len(self.stamps) - 1) / elapsed if elapsed > 0 else 0.0

        return Features(
            mid=tick.mid,
            microprice=tick.microprice,
            spread_pips=tick.spread / self.pip if self.pip else 0.0,
            z_score=z,
            volatility_bps=volatility,
            momentum_bps=momentum,
            imbalance=fmean(self.imbalances) if self.imbalances else 0.0,
            ticks_per_second=rate,
            ready=self.seen >= self.warmup and std > 0,
        )
