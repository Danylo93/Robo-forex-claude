"""Linhas de tendência (LTB/LTA) usadas como confluência extra.

No gráfico do GBP/NZD a venda acontece no encontro da linha de oferta com uma
LTB descendente. Aqui a reta é ajustada sobre pares de pivots e validada:
nenhuma vela pode ter fechado significativamente além dela.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from ..models import Pivot, Series, Trendline
from .swings import highs, lows


@dataclass
class TrendlineParams:
    min_touches: int = 2
    touch_atr: float = 0.35  # distância para contar um toque
    violation_atr: float = 0.5  # fechamento além da reta invalida
    max_violations: int = 1
    min_bars_apart: int = 4
    max_pivots: int = 8  # nº de pivots recentes considerados


def _validate(
    line: Trendline, candles: Series, kind: str, atr_value: float, p: TrendlineParams
) -> Trendline | None:
    start = min(pv.index for pv in line.pivots)
    touches = 0
    violations = 0
    for i in range(start, len(candles)):
        value = line.value_at(i)
        bar = candles[i]
        if kind == "resistance":
            if abs(bar.high - value) <= p.touch_atr * atr_value:
                touches += 1
            if bar.close > value + p.violation_atr * atr_value:
                violations += 1
        else:
            if abs(bar.low - value) <= p.touch_atr * atr_value:
                touches += 1
            if bar.close < value - p.violation_atr * atr_value:
                violations += 1
    if violations > p.max_violations or touches < p.min_touches:
        return None
    line.touches = touches
    return line


def find_trendlines(
    candles: Series,
    pivots: list[Pivot],
    atr_value: float,
    kind: str = "resistance",
    params: TrendlineParams | None = None,
) -> list[Trendline]:
    """Retas descendentes sobre topos (`resistance`) ou ascendentes sobre fundos."""
    p = params or TrendlineParams()
    if atr_value <= 0:
        return []
    selected = (highs(pivots) if kind == "resistance" else lows(pivots))[-p.max_pivots :]
    lines: list[Trendline] = []
    for a, b in combinations(selected, 2):
        if b.index - a.index < p.min_bars_apart:
            continue
        slope = (b.price - a.price) / (b.index - a.index)
        if kind == "resistance" and slope >= 0:
            continue  # só LTB (descendente) serve como oferta dinâmica
        if kind == "support" and slope <= 0:
            continue
        intercept = a.price - slope * a.index
        line = _validate(
            Trendline(kind=kind, slope=slope, intercept=intercept, pivots=[a, b]),
            candles,
            kind,
            atr_value,
            p,
        )
        if line:
            lines.append(line)
    lines.sort(key=lambda ln: (-ln.touches, -max(pv.index for pv in ln.pivots)))
    return lines


def trendline_confluence(
    lines: list[Trendline], index: int, top: float, bottom: float, tolerance: float
) -> Trendline | None:
    """Primeira reta cuja projeção em `index` cai dentro (ou perto) da faixa."""
    for line in lines:
        value = line.value_at(index)
        if bottom - tolerance <= value <= top + tolerance:
            return line
    return None
