"""Medição de latência tick-to-trade em nanossegundos.

Num sistema institucional a latência é métrica de produção, não curiosidade: se
você não mede p99 e p99.9, não sabe o que está perdendo. Cada estágio do caminho
crítico é cronometrado separadamente para saber *onde* está o tempo.

Aviso técnico honesto: em Python o próprio ato de medir custa ~100-200 ns, e o
coletor de lixo produz pausas de milissegundos. Estes números servem para
comparar mudanças no seu código, não para prometer competitividade em µs — para
isso o caminho crítico precisa ser reescrito em C++/Rust (ver docs/HFT_INSTITUCIONAL.md).
"""

from __future__ import annotations

import time
from bisect import insort
from dataclasses import dataclass, field
from typing import Optional

NS_PER_US = 1_000
NS_PER_MS = 1_000_000


@dataclass
class Histogram:
    """Amostras com percentis — o que importa em latência é a cauda."""

    name: str
    samples: list[int] = field(default_factory=list)
    capacity: int = 100_000

    def record(self, nanos: int) -> None:
        if len(self.samples) < self.capacity:
            insort(self.samples, nanos)
        elif nanos > self.samples[0]:  # cheio: mantém as piores, que é o que importa
            self.samples.pop(0)
            insort(self.samples, nanos)

    def percentile(self, pct: float) -> int:
        if not self.samples:
            return 0
        index = min(len(self.samples) - 1, max(0, round(pct / 100.0 * len(self.samples)) - 1))
        return self.samples[index]

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def mean(self) -> float:
        return sum(self.samples) / len(self.samples) if self.samples else 0.0

    @property
    def max(self) -> int:
        return self.samples[-1] if self.samples else 0

    @property
    def min(self) -> int:
        return self.samples[0] if self.samples else 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "mean_us": round(self.mean / NS_PER_US, 2),
            "p50_us": round(self.percentile(50) / NS_PER_US, 2),
            "p90_us": round(self.percentile(90) / NS_PER_US, 2),
            "p99_us": round(self.percentile(99) / NS_PER_US, 2),
            "p999_us": round(self.percentile(99.9) / NS_PER_US, 2),
            "max_us": round(self.max / NS_PER_US, 2),
        }


class LatencyTracker:
    """Cronômetros por estágio do caminho crítico."""

    STAGES = ("feed_decode", "book_update", "strategy", "risk", "wire", "tick_to_trade")

    def __init__(self, capacity: int = 100_000):
        self.histograms: dict[str, Histogram] = {
            stage: Histogram(stage, capacity=capacity) for stage in self.STAGES
        }
        self._starts: dict[str, int] = {}

    def start(self, stage: str) -> int:
        now = time.perf_counter_ns()
        self._starts[stage] = now
        return now

    def stop(self, stage: str, started_at: Optional[int] = None) -> int:
        start = started_at if started_at is not None else self._starts.pop(stage, 0)
        if not start:
            return 0
        elapsed = time.perf_counter_ns() - start
        self.record(stage, elapsed)
        return elapsed

    def record(self, stage: str, nanos: int) -> None:
        histogram = self.histograms.get(stage)
        if histogram is None:
            histogram = self.histograms[stage] = Histogram(stage)
        histogram.record(nanos)

    def stage(self, name: str) -> "StageTimer":
        return StageTimer(self, name)

    def report(self) -> str:
        lines = [
            f"{'estágio':<16}{'n':>8}{'p50':>10}{'p99':>10}{'p99.9':>10}{'máx':>10}  (µs)",
            "-" * 66,
        ]
        for stage in self.STAGES:
            histogram = self.histograms[stage]
            if not histogram.count:
                continue
            data = histogram.to_dict()
            lines.append(
                f"{stage:<16}{data['count']:>8}{data['p50_us']:>10.2f}{data['p99_us']:>10.2f}"
                f"{data['p999_us']:>10.2f}{data['max_us']:>10.2f}"
            )
        if len(lines) == 2:
            lines.append("(sem amostras)")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            stage: histogram.to_dict()
            for stage, histogram in self.histograms.items()
            if histogram.count
        }


class StageTimer:
    """Context manager: `with tracker.stage("strategy"): ...`"""

    __slots__ = ("tracker", "name", "_start")

    def __init__(self, tracker: LatencyTracker, name: str):
        self.tracker = tracker
        self.name = name
        self._start = 0

    def __enter__(self) -> "StageTimer":
        self._start = time.perf_counter_ns()
        return self

    def __exit__(self, *exc) -> None:
        self.tracker.record(self.name, time.perf_counter_ns() - self._start)
        return None
