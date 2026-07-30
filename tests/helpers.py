"""Fixtures compartilhadas pelos testes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from robo_forex.config import Settings
from robo_forex.feeds.synthetic import build_legs, pattern_candles
from robo_forex.models import Candle


def offline_settings(**overrides) -> Settings:
    """Configuração determinística: sem rede, sem filtro de sessão/notícias."""
    settings = Settings()
    settings.feed.provider = "synthetic"
    settings.news.enabled = False
    settings.session.enabled = False
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        target = getattr(settings, section)
        setattr(target, field, value)
    return settings


def candle(ts_offset: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=ts_offset),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=100.0,
    )


def long_series(repeats: int = 6) -> list[Candle]:
    """Série longa: o padrão repetido, para testes de backtest."""
    candles = pattern_candles("sell")
    for _ in range(repeats - 1):
        last = candles[-1]
        block = build_legs(
            last.close,
            [
                (last.close * 0.995, 6),
                (last.close * 1.006, 10),
                (last.close * 0.994, 8),
                (last.close * 1.0055, 9),
                (last.close * 0.988, 6),
                (last.close * 0.999, 7),
            ],
            60,
            last.ts + timedelta(hours=1),
        )
        candles = candles + block
    return candles
