"""Linhas de oferta e demanda (zonas horizontais respeitadas várias vezes).

A "linha de oferta" do setup é construída agrupando topos (ou fundos) que
ocorreram em preços próximos. Quanto mais vezes o preço bateu e foi rejeitado
naquela faixa, mais forte é o interesse vendedor (ou comprador) na região.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from ..models import Candle, Pivot, Series, Zone, ZoneKind
from .swings import highs, lows


@dataclass
class ZoneParams:
    cluster_atr: float = 0.6  # distância máxima entre pivots do mesmo agrupamento
    min_pivots: int = 2  # nº mínimo de topos/fundos formando a linha
    min_touches: int = 2  # nº mínimo de toques (inclui os pivots)
    min_height_atr: float = 0.15
    max_height_atr: float = 2.0
    touch_cooldown: int = 3  # barras mínimas entre dois toques distintos
    rejection_atr: float = 0.7  # afastamento necessário para considerar "respeitada"
    rejection_bars: int = 8
    break_close_atr: float = 0.25  # fechamento além da borda distal invalida a zona


def _cluster(prices_idx: list[Pivot], tolerance: float) -> list[list[Pivot]]:
    """Agrupa pivots cujos preços estão dentro da tolerância."""
    groups: list[list[Pivot]] = []
    for pivot in sorted(prices_idx, key=lambda p: p.price):
        if groups and abs(pivot.price - mean(p.price for p in groups[-1])) <= tolerance:
            groups[-1].append(pivot)
        else:
            groups.append([pivot])
    return groups


def _band(group: list[Pivot], candles: Series, kind: ZoneKind) -> tuple[float, float]:
    """Faixa da zona: da mecha extrema até o corpo médio das velas de pivot."""
    bars: list[Candle] = [candles[p.index] for p in group]
    if kind is ZoneKind.SUPPLY:
        top = max(bar.high for bar in bars)
        bottom = min(bar.body_low for bar in bars)
    else:
        bottom = min(bar.low for bar in bars)
        top = max(bar.body_high for bar in bars)
    return top, bottom


def _normalize_height(top: float, bottom: float, kind: ZoneKind, atr_value: float, p: ZoneParams):
    height = top - bottom
    min_h = p.min_height_atr * atr_value
    max_h = p.max_height_atr * atr_value
    if height < min_h:
        if kind is ZoneKind.SUPPLY:
            bottom = top - min_h
        else:
            top = bottom + min_h
    elif max_h > 0 and height > max_h:
        if kind is ZoneKind.SUPPLY:
            bottom = top - max_h
        else:
            top = bottom + max_h
    return top, bottom


def _scan_touches(zone: Zone, candles: Series, atr_value: float, p: ZoneParams) -> None:
    """Conta toques/rejeições e marca a zona como rompida, se for o caso."""
    supply = zone.kind is ZoneKind.SUPPLY
    was_inside = False
    last_touch = -10**9
    break_level = (
        zone.top + p.break_close_atr * atr_value
        if supply
        else zone.bottom - p.break_close_atr * atr_value
    )
    for i in range(zone.created_index, len(candles)):
        bar = candles[i]
        if (supply and bar.close > break_level) or (not supply and bar.close < break_level):
            zone.broken = True
            zone.broken_index = i
            break
        inside = bar.high >= zone.bottom if supply else bar.low <= zone.top
        if supply:
            inside = inside and bar.low <= zone.top
        else:
            inside = inside and bar.high >= zone.bottom
        if inside and not was_inside and (i - last_touch) >= p.touch_cooldown:
            zone.touches += 1
            last_touch = i
            zone.last_touch_index = i
            window = candles[i : min(i + p.rejection_bars + 1, len(candles))]
            if supply:
                moved = zone.bottom - min(b.low for b in window)
            else:
                moved = max(b.high for b in window) - zone.top
            if moved >= p.rejection_atr * atr_value:
                zone.rejections += 1
        was_inside = inside


def build_zones(
    candles: Series,
    pivots: list[Pivot],
    kind: ZoneKind,
    atr_value: float,
    params: ZoneParams | None = None,
) -> list[Zone]:
    """Constrói as zonas de oferta (topos) ou demanda (fundos) do gráfico."""
    p = params or ZoneParams()
    if not candles or atr_value <= 0:
        return []
    selected = highs(pivots) if kind is ZoneKind.SUPPLY else lows(pivots)
    zones: list[Zone] = []
    for group in _cluster(selected, p.cluster_atr * atr_value):
        if len(group) < p.min_pivots:
            continue
        top, bottom = _band(group, candles, kind)
        top, bottom = _normalize_height(top, bottom, kind, atr_value, p)
        zone = Zone(
            kind=kind,
            top=top,
            bottom=bottom,
            pivots=sorted(group, key=lambda pv: pv.index),
            created_index=min(pv.index for pv in group),
        )
        _scan_touches(zone, candles, atr_value, p)
        if zone.touches >= p.min_touches:
            zones.append(zone)
    zones.sort(key=lambda z: (-z.touches, -z.last_touch_index))
    return zones


def active_zones(zones: list[Zone], kind: ZoneKind, price: float) -> list[Zone]:
    """Zonas ainda válidas e posicionadas do lado correto do preço atual."""
    out = []
    for z in zones:
        if z.broken or z.kind is not kind:
            continue
        if kind is ZoneKind.SUPPLY and z.top <= price:
            continue  # oferta já foi superada pelo preço
        if kind is ZoneKind.DEMAND and z.bottom >= price:
            continue
        out.append(z)
    return out


def zone_strength(zone: Zone, atr_value: float) -> float:
    """Nota 0-100 da zona: toques, rejeições e compactação da faixa."""
    touch_score = min(zone.touches, 5) / 5.0 * 55.0
    rejection_score = min(zone.rejections, 4) / 4.0 * 30.0
    tightness = 0.0
    if atr_value > 0:
        tightness = max(0.0, 1.0 - (zone.height / (2.0 * atr_value))) * 15.0
    return touch_score + rejection_score + tightness
