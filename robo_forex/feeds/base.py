"""Contrato dos feeds de dados e utilitários de tempo gráfico."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..config import SymbolSpec
from ..models import Candle

TIMEFRAME_MINUTES = {
    "1m": 1,
    "2m": 2,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "60m": 60,
    "90m": 90,
    "2h": 120,
    "3h": 180,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
    "1w": 10080,
}


def timeframe_minutes(timeframe: str) -> int:
    key = timeframe.strip().lower()
    if key not in TIMEFRAME_MINUTES:
        raise ValueError(f"tempo gráfico não suportado: {timeframe}")
    return TIMEFRAME_MINUTES[key]


class DataFeed(Protocol):
    def fetch(self, spec: SymbolSpec, timeframe: str, bars: int) -> list[Candle]: ...


def resample(candles: list[Candle], source_minutes: int, target_minutes: int) -> list[Candle]:
    """Agrega candles para um tempo gráfico maior (ex.: 1h -> 4h)."""
    if target_minutes == source_minutes:
        return list(candles)
    if target_minutes % source_minutes != 0:
        raise ValueError(
            f"não é possível agregar {source_minutes}min em {target_minutes}min"
        )
    factor = target_minutes // source_minutes
    out: list[Candle] = []
    bucket: list[Candle] = []

    def flush() -> None:
        if not bucket:
            return
        out.append(
            Candle(
                ts=bucket[0].ts,
                open=bucket[0].open,
                high=max(c.high for c in bucket),
                low=min(c.low for c in bucket),
                close=bucket[-1].close,
                volume=sum(c.volume for c in bucket),
            )
        )
        bucket.clear()

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for candle in candles:
        ts = candle.ts if candle.ts.tzinfo else candle.ts.replace(tzinfo=timezone.utc)
        slot = int((ts - epoch).total_seconds() // 60) // target_minutes
        if bucket:
            bucket_slot = int((bucket[0].ts - epoch).total_seconds() // 60) // target_minutes
            if slot != bucket_slot or len(bucket) >= factor:
                flush()
        bucket.append(candle)
    flush()
    return out


def drop_unclosed(candles: list[Candle], timeframe: str, now: datetime | None = None) -> list[Candle]:
    """Remove a última barra se ela ainda estiver em formação."""
    if not candles:
        return candles
    now = now or datetime.now(timezone.utc)
    minutes = timeframe_minutes(timeframe)
    last = candles[-1]
    ts = last.ts if last.ts.tzinfo else last.ts.replace(tzinfo=timezone.utc)
    if ts + timedelta(minutes=minutes) > now:
        return candles[:-1]
    return candles
