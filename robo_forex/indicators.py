"""Indicadores auxiliares em Python puro (sem numpy/pandas)."""

from __future__ import annotations

from statistics import mean
from typing import Optional

from .models import Series


def true_range(prev_close: float, high: float, low: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(candles: Series, period: int = 14, end: Optional[int] = None) -> float:
    """ATR (média simples do True Range) até o índice `end` inclusive."""
    end = len(candles) - 1 if end is None else end
    if end <= 0:
        return candles[end].range if candles else 0.0
    start = max(1, end - period + 1)
    trs = [
        true_range(candles[i - 1].close, candles[i].high, candles[i].low)
        for i in range(start, end + 1)
    ]
    return mean(trs) if trs else candles[end].range


def atr_series(candles: Series, period: int = 14) -> list[float]:
    """ATR calculado para cada barra (útil no backtest)."""
    out: list[float] = []
    trs: list[float] = []
    for i, candle in enumerate(candles):
        tr = candle.range if i == 0 else true_range(candles[i - 1].close, candle.high, candle.low)
        trs.append(tr)
        window = trs[-period:]
        out.append(mean(window))
    return out


def sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return mean(values[-period:])


def ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    k = 2.0 / (period + 1)
    value = mean(values[:period])
    for price in values[period:]:
        value = price * k + value * (1 - k)
    return value


def closes(candles: Series) -> list[float]:
    return [c.close for c in candles]


def linreg(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Regressão linear simples -> (slope, intercept)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    mx, my = mean(xs), mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return slope, my - slope * mx
