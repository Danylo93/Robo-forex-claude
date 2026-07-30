"""Confluência: cruzamento entre linha de oferta/demanda, order block e LTB/LTA.

É o coração do setup. Só interessa a região onde a zona horizontal (respeitada
várias vezes) se sobrepõe a um order block do mesmo lado — opcionalmente com uma
linha de tendência passando pelo mesmo ponto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import OrderBlock, Series, Side, Trendline, Zone, ZoneKind
from .order_blocks import order_block_strength
from .trendlines import trendline_confluence
from .zones import zone_strength


@dataclass
class ConfluenceParams:
    require_order_block: bool = True
    min_overlap_atr: float = 0.05  # sobreposição mínima entre zona e order block
    trendline_tolerance_atr: float = 0.4
    weight_zone: float = 0.45
    weight_order_block: float = 0.35
    weight_trendline: float = 0.10
    weight_bias: float = 0.10
    zone_only_penalty: float = 0.75  # aplicado quando não há order block


@dataclass
class Confluence:
    side: Side
    zone: Zone
    order_block: OrderBlock | None
    trendline: Trendline | None
    entry_top: float
    entry_bottom: float
    risk_level: float  # borda distal usada para o stop (antes do buffer)
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def proximal(self) -> float:
        """Preço de acionamento: borda por onde o preço entra na região."""
        return self.entry_bottom if self.side is Side.SELL else self.entry_top


def _bias_bonus(side: Side, bias: str) -> float:
    if bias == "down":
        return 100.0 if side is Side.SELL else 0.0
    if bias == "up":
        return 100.0 if side is Side.BUY else 0.0
    return 50.0


def build_confluences(
    candles: Series,
    zones: list[Zone],
    order_blocks: list[OrderBlock],
    trendlines: list[Trendline],
    atr_value: float,
    kind: ZoneKind,
    bias: str = "neutral",
    params: ConfluenceParams | None = None,
) -> list[Confluence]:
    """Gera as regiões de confluência candidatas, da melhor para a pior nota."""
    p = params or ConfluenceParams()
    side = kind.side
    index = len(candles) - 1
    out: list[Confluence] = []

    for zone in zones:
        if zone.broken or zone.kind is not kind:
            continue
        blocks = [
            ob
            for ob in order_blocks
            if ob.side is side and zone.overlap(ob) >= p.min_overlap_atr * atr_value
        ]
        best_block = max(blocks, key=order_block_strength) if blocks else None
        if best_block is None and p.require_order_block:
            continue

        if best_block is not None:
            entry_top = min(zone.top, best_block.top)
            entry_bottom = max(zone.bottom, best_block.bottom)
            if entry_top <= entry_bottom:  # sobreposição degenerada: usa o bloco
                entry_top, entry_bottom = best_block.top, best_block.bottom
            risk_level = (
                max(zone.top, best_block.top)
                if side is Side.SELL
                else min(zone.bottom, best_block.bottom)
            )
        else:
            entry_top, entry_bottom = zone.top, zone.bottom
            risk_level = zone.top if side is Side.SELL else zone.bottom

        line = trendline_confluence(
            trendlines, index, entry_top, entry_bottom, p.trendline_tolerance_atr * atr_value
        )

        z_score = zone_strength(zone, atr_value)
        ob_score = order_block_strength(best_block) if best_block else 0.0
        tl_score = min(100.0, 60.0 + 20.0 * (line.touches - 1)) if line else 0.0
        score = (
            p.weight_zone * z_score
            + p.weight_order_block * ob_score
            + p.weight_trendline * tl_score
            + p.weight_bias * _bias_bonus(side, bias)
        )
        if best_block is None:
            score *= p.zone_only_penalty

        reasons = [
            f"Linha de {'oferta' if kind is ZoneKind.SUPPLY else 'demanda'} com "
            f"{zone.touches} toques ({zone.rejections} rejeições) em "
            f"{zone.bottom:.5f}-{zone.top:.5f}"
        ]
        if best_block:
            reasons.append(
                f"Order block de {best_block.side.label_ptbr.lower()} em "
                f"{best_block.bottom:.5f}-{best_block.top:.5f} com deslocamento de "
                f"{best_block.displacement_atr:.1f}x ATR"
                + (" e rompimento de estrutura (BOS)" if best_block.bos else "")
                + (" — já mitigado" if best_block.mitigated else " — ainda não testado")
            )
        else:
            reasons.append("Sem order block na região (setup apenas de zona)")
        if line:
            reasons.append(
                f"Linha de tendência {'de baixa' if line.kind == 'resistance' else 'de alta'} "
                f"com {line.touches} toques passando pela zona"
            )
        reasons.append(f"Viés do tempo gráfico maior: {bias}")

        out.append(
            Confluence(
                side=side,
                zone=zone,
                order_block=best_block,
                trendline=line,
                entry_top=entry_top,
                entry_bottom=entry_bottom,
                risk_level=risk_level,
                score=score,
                reasons=reasons,
            )
        )

    out.sort(key=lambda c: -c.score)
    return out
