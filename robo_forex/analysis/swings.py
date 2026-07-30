"""Detecção de pivots (topos/fundos fractais)."""

from __future__ import annotations

from ..models import Pivot, Series


def find_pivots(candles: Series, left: int = 2, right: int = 2) -> list[Pivot]:
    """Pivots fractais: extremo em relação a `left` barras à esquerda e `right` à direita.

    À esquerda a comparação é estrita (evita pivots duplicados em platôs);
    à direita aceita empate, o que preserva topos duplos como dois toques.
    """
    pivots: list[Pivot] = []
    n = len(candles)
    for i in range(left, n - right):
        high = candles[i].high
        low = candles[i].low
        if all(candles[j].high < high for j in range(i - left, i)) and all(
            candles[j].high <= high for j in range(i + 1, i + right + 1)
        ):
            pivots.append(Pivot(index=i, price=high, kind="high", ts=candles[i].ts))
        if all(candles[j].low > low for j in range(i - left, i)) and all(
            candles[j].low >= low for j in range(i + 1, i + right + 1)
        ):
            pivots.append(Pivot(index=i, price=low, kind="low", ts=candles[i].ts))
    pivots.sort(key=lambda p: p.index)
    return pivots


def highs(pivots: list[Pivot]) -> list[Pivot]:
    return [p for p in pivots if p.kind == "high"]


def lows(pivots: list[Pivot]) -> list[Pivot]:
    return [p for p in pivots if p.kind == "low"]


def last_pivot_before(pivots: list[Pivot], index: int, kind: str) -> Pivot | None:
    found = None
    for p in pivots:
        if p.index >= index:
            break
        if p.kind == kind:
            found = p
    return found


def swing_lows_below(pivots: list[Pivot], price: float, before_index: int) -> list[Pivot]:
    """Fundos abaixo de `price`, do mais próximo para o mais distante."""
    out = [p for p in pivots if p.kind == "low" and p.price < price and p.index <= before_index]
    out.sort(key=lambda p: -p.price)
    return out


def swing_highs_above(pivots: list[Pivot], price: float, before_index: int) -> list[Pivot]:
    """Topos acima de `price`, do mais próximo para o mais distante."""
    out = [p for p in pivots if p.kind == "high" and p.price > price and p.index <= before_index]
    out.sort(key=lambda p: p.price)
    return out
