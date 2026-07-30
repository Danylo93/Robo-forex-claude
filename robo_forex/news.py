"""Filtro de notícias: calendário econômico e janela de bloqueio.

O robô não opera com evento de alto impacto próximo para qualquer uma das moedas
do par — é justamente quando zonas e order blocks são varridos por volatilidade.
"""

from __future__ import annotations

import csv
import json
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Protocol

from .config import NewsSettings, SymbolSpec
from .models import NewsEvent, NewsVerdict

IMPACT_ALIASES = {
    "high": "high",
    "holiday": "high",
    "medium": "medium",
    "moderate": "medium",
    "low": "low",
    "non-economic": "low",
}


def normalize_impact(value: str) -> str:
    return IMPACT_ALIASES.get((value or "").strip().lower(), "low")


def parse_ts(value: str) -> Optional[datetime]:
    """Aceita ISO8601 (com ou sem offset) e epoch em segundos."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    raw = raw.replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                ts = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


class Calendar(Protocol):
    def events(self) -> list[NewsEvent]: ...


class NullCalendar:
    """Calendário vazio (filtro de notícias desligado)."""

    def events(self) -> list[NewsEvent]:
        return []


class FileCalendar:
    """Calendário lido de arquivo local (.json ou .csv)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def events(self) -> list[NewsEvent]:
        if not self.path.exists():
            raise FileNotFoundError(f"calendário não encontrado: {self.path}")
        text = self.path.read_text(encoding="utf-8")
        if self.path.suffix.lower() == ".csv":
            rows: Iterable[dict] = list(csv.DictReader(text.splitlines()))
        else:
            rows = json.loads(text)
        return _rows_to_events(rows)


class FairEconomyCalendar:
    """Calendário semanal do Forex Factory (espelho JSON público)."""

    def __init__(self, settings: NewsSettings, cache_dir: str | Path = ".cache"):
        self.settings = settings
        self.cache_path = Path(cache_dir) / "calendar.json"

    def events(self) -> list[NewsEvent]:
        rows = self._cached_rows()
        if rows is None:
            rows = self._download()
        return _rows_to_events(rows)

    def _cached_rows(self) -> Optional[list[dict]]:
        if not self.cache_path.exists():
            return None
        age = datetime.now(timezone.utc).timestamp() - self.cache_path.stat().st_mtime
        if age > self.settings.cache_minutes * 60:
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _download(self) -> list[dict]:
        request = urllib.request.Request(
            self.settings.url, headers={"User-Agent": "robo-forex/1.0"}
        )
        context = ssl.create_default_context()
        with urllib.request.urlopen(
            request, timeout=self.settings.timeout, context=context
        ) as response:
            payload = response.read().decode("utf-8", errors="replace")
        rows = json.loads(payload)
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(rows), encoding="utf-8")
        except OSError:
            pass  # cache é best-effort
        return rows


def _rows_to_events(rows: Iterable[dict]) -> list[NewsEvent]:
    events: list[NewsEvent] = []
    for row in rows:
        ts = parse_ts(row.get("date") or row.get("ts") or row.get("datetime") or "")
        if ts is None:
            continue
        currency = (row.get("country") or row.get("currency") or "").upper()
        events.append(
            NewsEvent(
                ts=ts,
                currency=currency,
                title=row.get("title") or row.get("event") or "evento",
                impact=normalize_impact(row.get("impact") or row.get("importance") or ""),
            )
        )
    events.sort(key=lambda e: e.ts)
    return events


def build_calendar(settings: NewsSettings, cache_dir: str | Path = ".cache") -> Calendar:
    if not settings.enabled or settings.provider == "none":
        return NullCalendar()
    if settings.provider == "file":
        if not settings.file:
            raise ValueError("news.provider='file' exige news.file preenchido")
        return FileCalendar(settings.file)
    if settings.provider == "faireconomy":
        return FairEconomyCalendar(settings, cache_dir)
    raise ValueError(f"provider de notícias desconhecido: {settings.provider}")


class NewsFilter:
    """Aplica a janela de bloqueio de notícias sobre um ativo."""

    def __init__(self, settings: NewsSettings, calendar: Optional[Calendar] = None,
                 cache_dir: str | Path = ".cache"):
        self.settings = settings
        self._calendar = calendar if calendar is not None else build_calendar(settings, cache_dir)
        self._events: Optional[list[NewsEvent]] = None
        self._error: Optional[str] = None

    def load(self) -> list[NewsEvent]:
        if self._events is None:
            try:
                self._events = self._calendar.events()
            except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
                self._events = []
                self._error = f"calendário indisponível ({type(exc).__name__}: {exc})"
        return self._events

    def relevant(self, spec: SymbolSpec) -> list[NewsEvent]:
        impacts = {i.lower() for i in self.settings.impacts}
        currencies = {c.upper() for c in spec.currencies}
        return [
            e
            for e in self.load()
            if e.impact in impacts and (not currencies or e.currency in currencies)
        ]

    def check(self, spec: SymbolSpec, when: datetime) -> NewsVerdict:
        """Bloqueia se `when` cai na janela de um evento relevante."""
        if not self.settings.enabled:
            return NewsVerdict(blocked=False, reason="filtro de notícias desativado")
        events = self.load()
        if self._error:
            if self.settings.fail_open:
                return NewsVerdict(blocked=False, reason=f"aviso: {self._error}")
            return NewsVerdict(blocked=True, reason=self._error)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        before = timedelta(minutes=self.settings.minutes_before)
        after = timedelta(minutes=self.settings.minutes_after)
        hits = [
            e
            for e in self.relevant(spec)
            if (e.ts - before) <= when <= (e.ts + after)
        ]
        if hits:
            names = ", ".join(f"{e.currency} {e.title} ({e.ts:%d/%m %H:%M} UTC)" for e in hits[:3])
            return NewsVerdict(blocked=True, reason=f"notícia de alto impacto: {names}", events=hits)
        del events
        return NewsVerdict(blocked=False, reason="sem notícia relevante na janela")

    def upcoming(self, spec: SymbolSpec, when: datetime, hours: int = 24) -> list[NewsEvent]:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        horizon = when + timedelta(hours=hours)
        return [e for e in self.relevant(spec) if when <= e.ts <= horizon]
