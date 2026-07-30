"""Varredura: roda a estratégia em todos os ativos configurados."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import Settings, SymbolSpec
from .feeds import DataFeed, build_feed, drop_unclosed
from .models import Signal
from .news import NewsFilter
from .strategy import Analysis, Strategy, session_ok


@dataclass
class ScanResult:
    started_at: datetime
    analyses: list[Analysis] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def signals(self) -> list[Signal]:
        found = [a.signal for a in self.analyses if a.signal]
        found.sort(key=lambda s: -s.score)
        return found

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "signals": [s.to_dict() for s in self.signals],
            "scanned": [
                {
                    "symbol": a.symbol,
                    "timeframe": a.timeframe,
                    "price": round(a.price, 6),
                    "atr": round(a.atr, 6),
                    "bias": a.bias,
                    "zones": len(a.zones),
                    "order_blocks": len(a.order_blocks),
                    "confluences": len(a.confluences),
                    "has_signal": a.signal is not None,
                    "rejections": a.rejections,
                }
                for a in self.analyses
            ],
            "errors": self.errors,
            "notes": self.notes,
        }


class Scanner:
    def __init__(
        self,
        settings: Settings,
        feed: Optional[DataFeed] = None,
        news: Optional[NewsFilter] = None,
    ):
        self.settings = settings
        self.feed = feed or build_feed(settings.feed)
        if news is None and settings.news.enabled:
            news = NewsFilter(settings.news, cache_dir=settings.feed.cache_dir)
        self.news = news
        self.strategy = Strategy(settings, news)
        self._rates: dict[str, Optional[float]] = {}

    def scan(
        self, symbols: Optional[list[str]] = None, now: Optional[datetime] = None
    ) -> ScanResult:
        now = now or datetime.now(timezone.utc)
        result = ScanResult(started_at=now)
        ok, reason = session_ok(now, self.settings)
        if not ok:
            result.notes.append(f"Aviso de sessão: {reason}")
        specs = (
            [self.settings.spec(s) for s in symbols] if symbols else list(self.settings.symbols)
        )
        for spec in specs:
            try:
                result.analyses.append(self.scan_symbol(spec, now))
            except Exception as exc:  # feeds de rede falham de várias formas
                result.errors[spec.symbol] = f"{type(exc).__name__}: {exc}"
        return result

    def scan_symbol(self, spec: SymbolSpec, now: Optional[datetime] = None) -> Analysis:
        cfg = self.settings.strategy
        now = now or datetime.now(timezone.utc)
        candles = drop_unclosed(
            list(self.feed.fetch(spec, cfg.timeframe, cfg.lookback)), cfg.timeframe, now
        )
        htf: list = []
        if cfg.htf_timeframe:
            try:
                htf = drop_unclosed(
                    list(self.feed.fetch(spec, cfg.htf_timeframe, cfg.htf_lookback)),
                    cfg.htf_timeframe,
                    now,
                )
            except Exception:
                htf = []  # viés é opcional: segue sem o tempo gráfico maior
        return self.strategy.analyze(spec, candles, htf, now, self.conversion_rate(spec))

    def conversion_rate(self, spec: SymbolSpec) -> Optional[float]:
        """Taxa moeda de cotação -> moeda da conta, para o valor do pip.

        Ex.: GBPNZD com conta em USD precisa de NZD/USD. Tenta o par direto e,
        se não existir no feed, usa o inverso. Falhas são silenciosas: o cálculo
        cai no modo aproximado, que já emite aviso no sinal.
        """
        account = self.settings.risk.account_currency.upper()
        quote = spec.quote
        if spec.kind != "forex" or not quote or quote == account:
            return None
        if quote in self._rates:
            return self._rates[quote]
        rate: Optional[float] = None
        for pair, invert in ((f"{quote}{account}", False), (f"{account}{quote}", True)):
            try:
                candles = self.feed.fetch(SymbolSpec(pair), self.settings.strategy.timeframe, 5)
            except Exception:
                continue
            if candles and candles[-1].close > 0:
                rate = 1.0 / candles[-1].close if invert else candles[-1].close
                break
        self._rates[quote] = rate
        return rate
