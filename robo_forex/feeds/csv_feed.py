"""Feed de arquivos CSV locais (histórico da corretora, MT5, backtest)."""

from __future__ import annotations

import csv
from pathlib import Path

from ..config import SymbolSpec
from ..models import Candle
from ..news import parse_ts
from .base import resample, timeframe_minutes

HEADER_ALIASES = {
    "time": "ts",
    "date": "ts",
    "datetime": "ts",
    "timestamp": "ts",
    "ts": "ts",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "tickvol": "volume",
    "vol": "volume",
}


class CsvFeed:
    """Espera arquivos `<DIR>/<SYMBOL>_<TIMEFRAME>.csv` com colunas OHLC.

    Se o arquivo do tempo gráfico pedido não existir, tenta agregar a partir de
    um tempo gráfico menor disponível no mesmo diretório.
    """

    def __init__(self, directory: str | Path = "data"):
        self.directory = Path(directory)

    def fetch(self, spec: SymbolSpec, timeframe: str, bars: int) -> list[Candle]:
        target = timeframe_minutes(timeframe)
        path = self._find(spec.symbol, timeframe)
        source = target
        if path is None:
            path, source = self._find_smaller(spec.symbol, target)
        if path is None:
            raise FileNotFoundError(
                f"nenhum CSV para {spec.symbol} {timeframe} em {self.directory}"
            )
        candles = read_csv(path)
        if source != target:
            candles = resample(candles, source, target)
        return candles[-bars:] if bars else candles

    def _find(self, symbol: str, timeframe: str) -> Path | None:
        for name in (
            f"{symbol}_{timeframe}.csv",
            f"{symbol}-{timeframe}.csv",
            f"{symbol}{timeframe}.csv",
            f"{symbol.lower()}_{timeframe}.csv",
        ):
            candidate = self.directory / name
            if candidate.exists():
                return candidate
        return None

    def _find_smaller(self, symbol: str, target: int) -> tuple[Path | None, int]:
        from .base import TIMEFRAME_MINUTES

        options = sorted(
            {m for m in TIMEFRAME_MINUTES.values() if m < target and target % m == 0},
            reverse=True,
        )
        for minutes in options:
            for label, value in TIMEFRAME_MINUTES.items():
                if value != minutes:
                    continue
                path = self._find(symbol, label)
                if path:
                    return path, minutes
        return None, target


def read_csv(path: str | Path) -> list[Candle]:
    """Lê um CSV OHLC tolerando variações de cabeçalho e separador."""
    text = Path(path).read_text(encoding="utf-8-sig")
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError(f"CSV sem cabeçalho: {path}")
    mapping = {}
    for column in reader.fieldnames:
        key = HEADER_ALIASES.get(column.strip().lower().lstrip("<").rstrip(">"))
        if key:
            mapping[column] = key
    missing = {"ts", "open", "high", "low", "close"} - set(mapping.values())
    if missing:
        raise ValueError(f"colunas ausentes em {path}: {', '.join(sorted(missing))}")

    candles: list[Candle] = []
    for row in reader:
        values = {mapping[c]: row[c] for c in mapping if row.get(c) not in (None, "")}
        ts = parse_ts(values.get("ts", ""))
        if ts is None:
            continue
        try:
            candles.append(
                Candle(
                    ts=ts,
                    open=float(values["open"]),
                    high=float(values["high"]),
                    low=float(values["low"]),
                    close=float(values["close"]),
                    volume=float(values.get("volume", 0) or 0),
                )
            )
        except (KeyError, ValueError):
            continue
    candles.sort(key=lambda c: c.ts)
    if not candles:
        raise ValueError(f"nenhum candle válido em {path}")
    return candles
