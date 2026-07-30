"""Estratégia: linha de oferta/demanda + order block em confluência.

Fluxo por ativo:
 1. ATR e pivots do tempo gráfico operacional (padrão H1);
 2. linhas de oferta/demanda (topos/fundos agrupados, com toques e rejeições);
 3. order blocks com deslocamento relevante e rompimento de estrutura;
 4. linhas de tendência como confluência adicional;
 5. viés estrutural do tempo gráfico maior (padrão H4);
 6. entrada na borda proximal da confluência, stop atrás da borda distal e alvo
    na liquidez oposta respeitando o RR mínimo (padrão 3:1);
 7. filtros de notícia, sessão, distância e nota mínima.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .analysis.confluence import Confluence, build_confluences
from .analysis.order_blocks import find_order_blocks
from .analysis.structure import htf_bias
from .analysis.swings import find_pivots, swing_highs_above, swing_lows_below
from .analysis.trendlines import find_trendlines
from .analysis.zones import active_zones, build_zones
from .config import Settings, SymbolSpec
from .indicators import atr as atr_of
from .models import (
    Candle,
    NewsVerdict,
    OrderBlock,
    Pivot,
    Series,
    Side,
    Signal,
    Trendline,
    Zone,
    ZoneKind,
)
from .news import NewsFilter
from .risk import entry_from_zone, pick_target, rr_of, size_position, stop_from_zone


@dataclass
class Analysis:
    """Resultado completo da análise de um ativo (com ou sem sinal)."""

    symbol: str
    timeframe: str
    price: float
    atr: float
    ts: datetime
    bias: str = "neutral"
    candles: list[Candle] = field(default_factory=list)
    pivots: list[Pivot] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    trendlines: list[Trendline] = field(default_factory=list)
    confluences: list[Confluence] = field(default_factory=list)
    signal: Optional[Signal] = None
    rejections: list[str] = field(default_factory=list)
    news: Optional[NewsVerdict] = None


def session_ok(now: datetime, settings: Settings) -> tuple[bool, str]:
    """Janela de operação: evita madrugada, fim de semana e sexta no fim do dia."""
    cfg = settings.session
    if not cfg.enabled:
        return True, "filtro de sessão desativado"
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    weekday = now.weekday()  # 0 = segunda
    if cfg.skip_weekend and weekday >= 5:
        return False, "fim de semana: mercado fechado"
    if weekday == 4 and now.hour >= cfg.friday_cutoff_hour_utc:
        return False, f"sexta após {cfg.friday_cutoff_hour_utc}h UTC: evitando gap de fim de semana"
    if not (cfg.start_hour_utc <= now.hour < cfg.end_hour_utc):
        return False, (
            f"fora da janela de operação ({cfg.start_hour_utc}h-{cfg.end_hour_utc}h UTC)"
        )
    return True, "dentro da janela de operação"


class Strategy:
    def __init__(self, settings: Settings, news: Optional[NewsFilter] = None):
        self.settings = settings
        self.news = news

    # --------------------------------------------------------------- análise
    def analyze(
        self,
        spec: SymbolSpec,
        candles: Series,
        htf_candles: Optional[Series] = None,
        now: Optional[datetime] = None,
        conversion_rate: Optional[float] = None,
    ) -> Analysis:
        cfg = self.settings.strategy
        risk_cfg = self.settings.risk
        min_bars = max(60, cfg.atr_period + 20)
        last_ts = candles[-1].ts if candles else datetime.now(timezone.utc)
        analysis = Analysis(
            symbol=spec.symbol,
            timeframe=cfg.timeframe,
            price=candles[-1].close if candles else 0.0,
            atr=0.0,
            ts=last_ts,
            candles=list(candles),
        )
        if len(candles) < min_bars:
            analysis.rejections.append(
                f"histórico insuficiente: {len(candles)} barras (mínimo {min_bars})"
            )
            return analysis

        atr_value = atr_of(candles, cfg.atr_period)
        analysis.atr = atr_value
        if atr_value <= 0:
            analysis.rejections.append("ATR zerado: dados sem variação")
            return analysis

        analysis.pivots = find_pivots(candles, cfg.pivot_left, cfg.pivot_right)
        supply = build_zones(candles, analysis.pivots, ZoneKind.SUPPLY, atr_value, cfg.zone)
        demand = build_zones(candles, analysis.pivots, ZoneKind.DEMAND, atr_value, cfg.zone)
        analysis.zones = supply + demand
        analysis.order_blocks = find_order_blocks(
            candles, analysis.pivots, atr_value, cfg.order_block
        )
        if cfg.use_trendlines:
            analysis.trendlines = find_trendlines(
                candles, analysis.pivots, atr_value, "resistance", cfg.trendline
            ) + find_trendlines(candles, analysis.pivots, atr_value, "support", cfg.trendline)
        analysis.bias = (
            htf_bias(htf_candles, cfg.htf_pivot_left, cfg.htf_pivot_right)
            if htf_candles
            else "neutral"
        )

        price = analysis.price
        candidates: list[Confluence] = []
        if cfg.allow_sell:
            candidates += build_confluences(
                candles,
                active_zones(supply, ZoneKind.SUPPLY, price),
                analysis.order_blocks,
                [t for t in analysis.trendlines if t.kind == "resistance"],
                atr_value,
                ZoneKind.SUPPLY,
                analysis.bias,
                cfg.confluence,
            )
        if cfg.allow_buy:
            candidates += build_confluences(
                candles,
                active_zones(demand, ZoneKind.DEMAND, price),
                analysis.order_blocks,
                [t for t in analysis.trendlines if t.kind == "support"],
                atr_value,
                ZoneKind.DEMAND,
                analysis.bias,
                cfg.confluence,
            )
        candidates.sort(key=lambda c: -c.score)
        analysis.confluences = candidates
        if not candidates:
            analysis.rejections.append(
                "nenhuma confluência entre linha de oferta/demanda e order block"
            )
            return analysis

        now = now or datetime.now(timezone.utc)
        for confluence in candidates:
            signal, reason = self._to_signal(
                spec, analysis, confluence, atr_value, now, conversion_rate
            )
            if signal:
                analysis.signal = signal
                return analysis
            analysis.rejections.append(reason)
        return analysis

    # ------------------------------------------------------------- montagem
    def _to_signal(
        self,
        spec: SymbolSpec,
        analysis: Analysis,
        confluence: Confluence,
        atr_value: float,
        now: datetime,
        conversion_rate: Optional[float] = None,
    ) -> tuple[Optional[Signal], str]:
        cfg = self.settings.strategy
        risk_cfg = self.settings.risk
        side = confluence.side
        price = analysis.price
        tag = f"{side.label_ptbr} {confluence.entry_bottom:.5f}-{confluence.entry_top:.5f}"

        if confluence.score < cfg.min_score:
            return None, f"{tag}: nota {confluence.score:.0f} abaixo do mínimo {cfg.min_score:.0f}"
        if cfg.require_htf_alignment and analysis.bias != ("down" if side is Side.SELL else "up"):
            return None, f"{tag}: viés do tempo gráfico maior ({analysis.bias}) não confirma"
        block = confluence.order_block
        if cfg.require_untested_block and block and block.mitigated:
            return None, f"{tag}: order block já mitigado"

        entry = entry_from_zone(side, confluence.proximal, atr_value, risk_cfg)
        stop = stop_from_zone(side, confluence.risk_level, atr_value, risk_cfg)

        # O preço precisa estar antes da entrada (ordem pendente) e não além do stop.
        tolerance = 0.1 * atr_value
        if side is Side.SELL and price > entry + tolerance:
            if price >= stop:
                return None, f"{tag}: preço já rompeu a zona (stop virtual atingido)"
            entry = price  # já dentro da zona: entrada a mercado na região
        if side is Side.BUY and price < entry - tolerance:
            if price <= stop:
                return None, f"{tag}: preço já rompeu a zona (stop virtual atingido)"
            entry = price
        distance = abs(price - entry)
        if distance > cfg.max_distance_atr * atr_value:
            return None, (
                f"{tag}: entrada distante {distance / atr_value:.1f}x ATR "
                f"(máximo {cfg.max_distance_atr:.1f})"
            )
        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            return None, f"{tag}: stop inválido"
        if stop_distance > risk_cfg.max_stop_atr * atr_value:
            return None, (
                f"{tag}: stop de {stop_distance / atr_value:.1f}x ATR acima do limite "
                f"({risk_cfg.max_stop_atr:.1f})"
            )
        if cfg.require_rejection_candle and not self._rejection_candle(analysis, confluence):
            return None, f"{tag}: aguardando vela de rejeição no reteste"

        target = pick_target(
            side, entry, stop, self._target_candidates(analysis, side, entry), atr_value, risk_cfg
        )
        if target is None or target.rr < risk_cfg.min_rr:
            got = f"{target.rr:.1f}" if target else "n/d"
            return None, f"{tag}: RR {got} abaixo do mínimo {risk_cfg.min_rr:.1f}:1"

        warnings: list[str] = []
        news_verdict = None
        if self.news:
            news_verdict = self.news.check(spec, now)
            analysis.news = news_verdict
            if news_verdict.blocked:
                return None, f"{tag}: {news_verdict.reason}"
            if news_verdict.reason.startswith("aviso"):
                warnings.append(news_verdict.reason)
            upcoming = self.news.upcoming(spec, now, hours=12)
            if upcoming:
                warnings.append(
                    "eventos nas próximas 12h: "
                    + ", ".join(f"{e.currency} {e.title} {e.ts:%d/%m %H:%M}Z" for e in upcoming[:3])
                )
        ok, reason = session_ok(now, self.settings)
        if not ok:
            return None, f"{tag}: {reason}"

        position, position_warnings = size_position(spec, entry, stop, risk_cfg, conversion_rate)
        warnings.extend(position_warnings)

        reasons = list(confluence.reasons)
        reasons.append(f"Alvo: {target.basis}")
        return (
            Signal(
                symbol=spec.symbol,
                timeframe=cfg.timeframe,
                side=side,
                entry=entry,
                stop=stop,
                target=target.price,
                rr=rr_of(side, entry, stop, target.price),
                score=confluence.score,
                ts=analysis.ts,
                digits=spec.digits,
                zone=confluence.zone,
                order_block=block,
                trendline=confluence.trendline,
                htf_bias=analysis.bias,
                reasons=reasons,
                warnings=warnings,
                position=position,
                target_basis=target.basis,
                last_price=price,
            ),
            "",
        )

    # ------------------------------------------------------------- auxiliares
    def _target_candidates(
        self, analysis: Analysis, side: Side, entry: float
    ) -> list[tuple[float, str]]:
        """Liquidez oposta: fundos/topos anteriores e zonas contrárias."""
        last = len(analysis.candles) - 1
        out: list[tuple[float, str]] = []
        if side is Side.SELL:
            for pivot in swing_lows_below(analysis.pivots, entry, last)[:6]:
                out.append((pivot.price, f"fundo anterior em {pivot.price:.5f}"))
            for zone in analysis.zones:
                if zone.kind is ZoneKind.DEMAND and zone.top < entry and not zone.broken:
                    out.append((zone.top, f"linha de demanda em {zone.top:.5f}"))
        else:
            for pivot in swing_highs_above(analysis.pivots, entry, last)[:6]:
                out.append((pivot.price, f"topo anterior em {pivot.price:.5f}"))
            for zone in analysis.zones:
                if zone.kind is ZoneKind.SUPPLY and zone.bottom > entry and not zone.broken:
                    out.append((zone.bottom, f"linha de oferta em {zone.bottom:.5f}"))
        return out

    @staticmethod
    def _rejection_candle(analysis: Analysis, confluence: Confluence) -> bool:
        """Confirmação opcional: última vela fechada rejeitando a região."""
        candle = analysis.candles[-1]
        if confluence.side is Side.SELL:
            touched = candle.high >= confluence.entry_bottom
            return touched and candle.is_bear and candle.close < confluence.entry_bottom
        touched = candle.low <= confluence.entry_top
        return touched and candle.is_bull and candle.close > confluence.entry_top
