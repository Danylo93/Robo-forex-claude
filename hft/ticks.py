"""Origens de tick: gerador sintético, CSV e conversão a partir de candles.

O gerador sintético existe para testar o motor e para *medir quanta vantagem*
uma estratégia precisa ter para vencer os custos. Ele não prova que a estratégia
dá lucro no mercado real — dados sintéticos provam apenas que o código funciona.
"""

from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import Tick


def synthetic_ticks(
    count: int = 20_000,
    start_price: float = 1.1000,
    pip: float = 0.0001,
    spread_pips: float = 0.6,
    volatility_pips: float = 0.35,
    reversion: float = 0.02,
    drift_pips: float = 0.0,
    ticks_per_second: float = 4.0,
    book_informativeness: float = 0.0,
    seed: int = 7,
    start: Optional[datetime] = None,
) -> list[Tick]:
    """Série de ticks com reversão à média (Ornstein-Uhlenbeck) e spread fixo.

    `reversion` é a força com que o preço volta à âncora — é literalmente a
    vantagem que a estratégia de reversão tenta capturar. `book_informativeness`
    (0 a 1) define quanto o desequilíbrio do livro antecipa o próximo movimento;
    o padrão 0 gera um livro sem poder preditivo, que é o caso honesto.
    """
    rng = random.Random(seed)
    ts = start or datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    step = timedelta(seconds=1.0 / max(ticks_per_second, 0.001))
    sigma = volatility_pips * pip
    half_spread = spread_pips * pip / 2.0
    drift = drift_pips * pip
    mid = start_price
    anchor = start_price

    # gera os incrementos antes para poder informar o livro quando pedido
    increments: list[float] = []
    values: list[float] = []
    for _ in range(count):
        anchor += rng.gauss(0.0, sigma * 0.05) + drift
        pull = reversion * (anchor - mid)
        move = pull + rng.gauss(0.0, sigma)
        increments.append(move)
        mid += move
        values.append(mid)

    ticks: list[Tick] = []
    for i, price in enumerate(values):
        future = increments[i + 1] if i + 1 < len(increments) else 0.0
        signal = max(-1.0, min(1.0, future / (sigma * 3.0))) * book_informativeness
        noise = rng.uniform(-0.35, 0.35)
        imbalance = max(-0.95, min(0.95, signal + noise))
        total = rng.uniform(500_000, 2_000_000)
        bid_size = total * (1 + imbalance) / 2
        ask_size = total * (1 - imbalance) / 2
        ticks.append(
            Tick(
                ts=ts,
                bid=round(price - half_spread, 7),
                ask=round(price + half_spread, 7),
                bid_size=round(bid_size, 1),
                ask_size=round(ask_size, 1),
            )
        )
        ts += step
    return ticks


def read_tick_csv(path: str | Path) -> list[Tick]:
    """CSV de ticks: colunas time/date, bid, ask e (opcional) tamanhos."""
    from robo_forex.news import parse_ts  # reaproveita o parser tolerante

    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        delimiter = csv.Sniffer().sniff(text[:2048], delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError(f"CSV de ticks sem cabeçalho: {path}")
    columns = {c.strip().lower().lstrip("<").rstrip(">"): c for c in reader.fieldnames}

    def column(*names: str) -> Optional[str]:
        for name in names:
            if name in columns:
                return columns[name]
        return None

    ts_col = column("time", "date", "datetime", "timestamp")
    bid_col = column("bid")
    ask_col = column("ask")
    if not (ts_col and bid_col and ask_col):
        raise ValueError(f"CSV de ticks precisa de time, bid e ask: {path}")
    bid_size_col = column("bid_size", "bidvolume", "bidsize")
    ask_size_col = column("ask_size", "askvolume", "asksize")

    ticks: list[Tick] = []
    for row in reader:
        ts = parse_ts(row.get(ts_col, ""))
        if ts is None:
            continue
        try:
            bid, ask = float(row[bid_col]), float(row[ask_col])
        except (KeyError, TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0:
            continue
        ticks.append(
            Tick(
                ts=ts,
                bid=bid,
                ask=ask,
                bid_size=_float(row.get(bid_size_col or "", 0)),
                ask_size=_float(row.get(ask_size_col or "", 0)),
            )
        )
    ticks.sort(key=lambda t: t.ts)
    if not ticks:
        raise ValueError(f"nenhum tick válido em {path}")
    return ticks


def ticks_from_candles(
    candles: Iterable, pip: float = 0.0001, spread_pips: float = 0.6, per_candle: int = 8
) -> Iterator[Tick]:
    """Aproxima ticks a partir de candles M1 (open -> high/low -> close).

    Serve para testar o motor quando não há histórico de ticks. O caminho dentro
    da barra é uma aproximação: use dados de tick de verdade antes de confiar em
    qualquer número.
    """
    half_spread = spread_pips * pip / 2.0
    for candle in candles:
        path = _intrabar_path(candle, per_candle)
        span = timedelta(minutes=1) / max(len(path), 1)
        for i, price in enumerate(path):
            yield Tick(
                ts=candle.ts + span * i,
                bid=price - half_spread,
                ask=price + half_spread,
                bid_size=1_000_000.0,
                ask_size=1_000_000.0,
            )


def _intrabar_path(candle, points: int) -> list[float]:
    """Caminho plausível dentro da barra, respeitando OHLC."""
    first, second = (candle.low, candle.high) if candle.close >= candle.open else (
        candle.high,
        candle.low,
    )
    anchors = [candle.open, first, second, candle.close]
    if points <= len(anchors):
        return anchors
    out: list[float] = []
    legs = len(anchors) - 1
    per_leg = max(1, points // legs)
    for i in range(legs):
        a, b = anchors[i], anchors[i + 1]
        for j in range(per_leg):
            out.append(a + (b - a) * j / per_leg)
    out.append(candle.close)
    return out


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
