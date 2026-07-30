"""Feeds de dados: Yahoo Finance, CSV local e sintético (offline)."""

from __future__ import annotations

from ..config import FeedSettings
from .base import DataFeed, drop_unclosed, resample, timeframe_minutes
from .csv_feed import CsvFeed, read_csv
from .synthetic import SyntheticFeed, pattern_candles
from .yahoo import FeedError, YahooFeed

__all__ = [
    "DataFeed",
    "CsvFeed",
    "FeedError",
    "SyntheticFeed",
    "YahooFeed",
    "build_feed",
    "drop_unclosed",
    "pattern_candles",
    "read_csv",
    "resample",
    "timeframe_minutes",
]


def build_feed(settings: FeedSettings) -> DataFeed:
    provider = settings.provider.lower()
    if provider == "yahoo":
        return YahooFeed(settings.cache_dir, settings.cache_minutes)
    if provider == "csv":
        return CsvFeed(settings.csv_dir)
    if provider == "synthetic":
        return SyntheticFeed()
    raise ValueError(f"feed desconhecido: {settings.provider}")
