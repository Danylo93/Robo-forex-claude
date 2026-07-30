"""Estrutura de mercado: tendência por pivots, BOS e viés do tempo gráfico maior."""

from __future__ import annotations

from ..models import Pivot, Series
from .swings import find_pivots, highs, lows

UP = "up"
DOWN = "down"
NEUTRAL = "neutral"


def trend_from_pivots(pivots: list[Pivot]) -> str:
    """Topos e fundos ascendentes = alta; descendentes = baixa; misto = neutro."""
    hs = highs(pivots)[-3:]
    ls = lows(pivots)[-3:]
    if len(hs) < 2 or len(ls) < 2:
        return NEUTRAL
    higher_highs = hs[-1].price > hs[-2].price
    higher_lows = ls[-1].price > ls[-2].price
    if higher_highs and higher_lows:
        return UP
    if not higher_highs and not higher_lows:
        return DOWN
    return NEUTRAL


def htf_bias(candles: Series, left: int = 2, right: int = 2) -> str:
    """Viés estrutural do tempo gráfico maior (H4/D1)."""
    if len(candles) < (left + right + 6):
        return NEUTRAL
    return trend_from_pivots(find_pivots(candles, left, right))


def broke_low(candles: Series, pivots: list[Pivot], start: int, end: int) -> tuple[bool, int | None]:
    """True se algum fechamento entre `start` e `end` rompeu o último fundo anterior."""
    reference = None
    for p in pivots:
        if p.index >= start:
            break
        if p.kind == "low":
            reference = p
    if reference is None:
        return False, None
    for i in range(max(start, 0), min(end, len(candles) - 1) + 1):
        if candles[i].close < reference.price:
            return True, i
    return False, None


def broke_high(candles: Series, pivots: list[Pivot], start: int, end: int) -> tuple[bool, int | None]:
    """True se algum fechamento entre `start` e `end` rompeu o último topo anterior."""
    reference = None
    for p in pivots:
        if p.index >= start:
            break
        if p.kind == "high":
            reference = p
    if reference is None:
        return False, None
    for i in range(max(start, 0), min(end, len(candles) - 1) + 1):
        if candles[i].close > reference.price:
            return True, i
    return False, None
