"""Feed gratuito do Yahoo Finance via API pública de gráficos (stdlib apenas)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ..config import SymbolSpec
from ..models import Candle
from .base import resample, timeframe_minutes

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Intervalos nativos suportados pelo Yahoo; o resto é agregado localmente.
NATIVE_INTERVALS = {1: "1m", 2: "2m", 5: "5m", 15: "15m", 30: "30m", 60: "60m", 1440: "1d"}
# Limite de histórico por intervalo (dias)
MAX_RANGE_DAYS = {1: 7, 2: 60, 5: 60, 15: 60, 30: 60, 60: 730, 1440: 10_000}


class FeedError(RuntimeError):
    pass


class YahooFeed:
    """Busca candles OHLC do Yahoo Finance, com cache em disco opcional."""

    def __init__(self, cache_dir: str | Path = ".cache", cache_minutes: int = 5, timeout: float = 20.0):
        self.cache_dir = Path(cache_dir)
        self.cache_minutes = cache_minutes
        self.timeout = timeout

    # ---------------------------------------------------------------- público
    def fetch(self, spec: SymbolSpec, timeframe: str, bars: int) -> list[Candle]:
        target = timeframe_minutes(timeframe)
        source = self._source_minutes(target)
        needed = bars * (target // source) + source
        days = self._range_days(source, needed)
        payload = self._request(spec.feed_symbol, NATIVE_INTERVALS[source], days)
        candles = _parse(payload)
        if source != target:
            candles = resample(candles, source, target)
        return candles[-bars:] if bars else candles

    # ----------------------------------------------------------------- ajuda
    @staticmethod
    def _source_minutes(target: int) -> int:
        if target in NATIVE_INTERVALS:
            return target
        for minutes in sorted(NATIVE_INTERVALS, reverse=True):
            if minutes < target and target % minutes == 0:
                return minutes
        raise FeedError(f"tempo gráfico incompatível com o Yahoo: {target}min")

    @staticmethod
    def _range_days(source_minutes: int, bars_needed: int) -> int:
        # ~5 dias úteis por semana e mercado 24h no forex
        days = max(5, int(bars_needed * source_minutes / (60 * 24) * 1.6) + 2)
        return min(days, MAX_RANGE_DAYS[source_minutes])

    def _cache_file(self, symbol: str, interval: str, days: int) -> Path:
        safe = urllib.parse.quote(symbol, safe="")
        return self.cache_dir / f"{safe}_{interval}_{days}d.json"

    def _request(self, symbol: str, interval: str, days: int) -> dict:
        cache = self._cache_file(symbol, interval, days)
        if cache.exists():
            age = datetime.now(timezone.utc).timestamp() - cache.stat().st_mtime
            if age <= self.cache_minutes * 60:
                try:
                    return json.loads(cache.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
        url = CHART_URL.format(symbol=urllib.parse.quote(symbol, safe="=^.-")) + "?" + (
            urllib.parse.urlencode({"interval": interval, "range": f"{days}d"})
        )
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 robo-forex/1.0"})
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=ssl.create_default_context()
            ) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise FeedError(f"falha ao buscar {symbol} no Yahoo: {exc}") from exc
        error = (payload.get("chart") or {}).get("error")
        if error:
            raise FeedError(f"Yahoo retornou erro para {symbol}: {error}")
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass
        return payload


def _parse(payload: dict) -> list[Candle]:
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        raise FeedError("resposta do Yahoo sem dados")
    result = results[0]
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    volumes = quote.get("volume") or [0] * len(stamps)
    candles: list[Candle] = []
    for i, ts in enumerate(stamps):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            continue
        if None in (o, h, l, c):
            continue  # buracos de liquidez / feriados
        candles.append(
            Candle(
                ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(volumes[i] or 0) if i < len(volumes) else 0.0,
            )
        )
    if not candles:
        raise FeedError("resposta do Yahoo sem candles válidos")
    return candles
