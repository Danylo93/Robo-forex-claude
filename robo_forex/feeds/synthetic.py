"""Feed sintético determinístico: demonstração e testes sem rede.

`pattern_candles()` reproduz o padrão do setup (linha de oferta com três toques,
order block de venda na terceira reação, deslocamento com rompimento de estrutura
e reteste da região), o que também serve de fixture para os testes.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from ..config import SymbolSpec
from ..models import Candle
from .base import resample, timeframe_minutes


def _wobble(i: int, scale: float) -> float:
    """Ruído determinístico (sem random) para gerar mechas plausíveis."""
    return scale * (0.5 * math.sin(i * 1.7) + 0.35 * math.sin(i * 0.53 + 1.1))


def build_legs(
    start_price: float,
    legs: list[tuple[float, int]],
    bar_minutes: int = 60,
    start_ts: datetime | None = None,
    noise: float = 0.0004,
) -> list[Candle]:
    """Gera candles interpolando pernas (preço-alvo, nº de barras)."""
    ts = start_ts or datetime(2026, 7, 1, tzinfo=timezone.utc)
    price = start_price
    candles: list[Candle] = []
    i = 0
    for target, bars in legs:
        step = (target - price) / max(bars, 1)
        for _ in range(bars):
            open_ = price
            close = price + step
            drift = _wobble(i, noise)
            close += drift * 0.4
            high = max(open_, close) + abs(drift) + noise * 0.6
            low = min(open_, close) - abs(drift) - noise * 0.6
            candles.append(
                Candle(
                    ts=ts,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=1000 + 100 * (i % 7),
                )
            )
            price = close
            ts += timedelta(minutes=bar_minutes)
            i += 1
    return candles


def pattern_candles(
    side: str = "sell", bar_minutes: int = 60, start_ts: datetime | None = None
) -> list[Candle]:
    """Padrão completo do setup, pronto para gerar sinal na última barra."""
    if side == "sell":
        legs = [
            (2.2650, 8),  # contexto inicial
            (2.2560, 6),
            (2.2900, 10),  # 1º toque na linha de oferta
            (2.2740, 7),  # rejeição
            (2.2893, 9),  # 2º toque
            (2.2690, 8),  # rejeição
            (2.2896, 8),  # 3º toque -> order block de venda no topo
            (2.2600, 5),  # deslocamento com rompimento de estrutura (BOS)
            (2.2560, 4),
            (2.2845, 7),  # reteste da confluência: gatilho do sinal
        ]
        return build_legs(2.2600, legs, bar_minutes, start_ts)
    legs = [
        (0.6560, 8),
        (0.6620, 6),
        (0.6480, 10),  # 1º toque na linha de demanda
        (0.6580, 7),
        (0.6484, 9),  # 2º toque
        (0.6600, 8),
        (0.6482, 8),  # 3º toque -> order block de compra no fundo
        (0.6700, 5),  # deslocamento de alta com BOS
        (0.6740, 4),
        (0.6520, 7),  # reteste
    ]
    return build_legs(0.6600, legs, bar_minutes, start_ts, noise=0.0003)


class SyntheticFeed:
    """Feed offline: contexto lateral seguido do padrão do setup na ponta direita."""

    BASE_MINUTES = 60

    def __init__(self, side: str = "sell", now: datetime | None = None):
        self.side = side
        self.now = now or datetime.now(timezone.utc)

    def fetch(self, spec: SymbolSpec, timeframe: str, bars: int) -> list[Candle]:
        minutes = timeframe_minutes(timeframe)
        base = self.BASE_MINUTES
        factor = max(1, minutes // base)
        wanted = max(bars * factor, 1)
        end = self.now.replace(minute=0, second=0, microsecond=0)

        pattern = pattern_candles(self.side, base, end - timedelta(minutes=base * 80))
        pattern = pattern[-80:]
        filler_bars = max(0, wanted + 4 - len(pattern))
        candles = _context(pattern[0].open, filler_bars, base, pattern[0].ts) + list(pattern)
        if minutes != base:
            candles = resample(candles, base, minutes)
        return candles[-bars:] if bars else candles


def _context(price: float, bars: int, minutes: int, end_ts: datetime) -> list[Candle]:
    """Lateralidade determinística antes do padrão, para encher o histórico."""
    if bars <= 0:
        return []
    start = end_ts - timedelta(minutes=minutes * bars)
    legs: list[tuple[float, int]] = []
    remaining = bars
    up = True
    while remaining > 0:
        size = min(9, remaining)
        legs.append((price * (1.004 if up else 0.996), size))
        remaining -= size
        up = not up
    return build_legs(price, legs, minutes, start)
