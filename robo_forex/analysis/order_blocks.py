"""Order blocks: última vela contrária antes de um deslocamento que rompe estrutura.

Order block de venda (bearish) = última vela de alta antes de uma queda impulsiva.
Order block de compra (bullish) = última vela de baixa antes de uma alta impulsiva.
Só é considerado válido quando o deslocamento tem tamanho relevante em ATR e
rompe o pivot anterior (BOS), sinal de que houve entrada institucional ali.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import OrderBlock, Pivot, Series, Side
from .structure import broke_high, broke_low


@dataclass
class OrderBlockParams:
    leg_bars: int = 6  # janela do deslocamento após o order block
    displacement_atr: float = 1.4  # tamanho mínimo do deslocamento em ATR
    max_candles: int = 3  # velas contrárias agrupadas no mesmo bloco
    require_bos: bool = True  # exige rompimento de estrutura
    extent: str = "body_to_wick"  # "body_to_wick" | "full" | "body"
    max_age: int = 400  # idade máxima do bloco em barras


def _extent(candles: Series, first: int, last: int, side: Side, mode: str) -> tuple[float, float]:
    bars = candles[first : last + 1]
    if mode == "full":
        return max(b.high for b in bars), min(b.low for b in bars)
    if mode == "body":
        return max(b.body_high for b in bars), min(b.body_low for b in bars)
    # body_to_wick: mecha do lado do risco, corpo do lado da entrada
    if side is Side.SELL:
        return max(b.high for b in bars), min(b.body_low for b in bars)
    return max(b.body_high for b in bars), min(b.low for b in bars)


def _group_start(candles: Series, index: int, bullish_block: bool, max_candles: int) -> int:
    """Estende o bloco para trás enquanto as velas mantêm a mesma direção."""
    start = index
    for _ in range(max_candles - 1):
        prev = start - 1
        if prev < 0:
            break
        candle = candles[prev]
        if bullish_block and candle.is_bull:
            start = prev
        elif not bullish_block and candle.is_bear:
            start = prev
        else:
            break
    return start


def find_order_blocks(
    candles: Series,
    pivots: list[Pivot],
    atr_value: float,
    params: OrderBlockParams | None = None,
) -> list[OrderBlock]:
    """Varre o gráfico e devolve os order blocks (mais recentes primeiro)."""
    p = params or OrderBlockParams()
    blocks: list[OrderBlock] = []
    n = len(candles)
    if n < 5 or atr_value <= 0:
        return blocks
    first_index = max(0, n - 1 - p.max_age)

    for i in range(first_index, n - 2):
        candle = candles[i]
        end = min(i + p.leg_bars, n - 1)
        if end <= i:
            continue
        window = candles[i + 1 : end + 1]
        if not window:
            continue

        # Order block de venda: vela de alta seguida por deslocamento de baixa.
        if candle.is_bull:
            drop = candle.high - min(b.low for b in window)
            if drop >= p.displacement_atr * atr_value:
                bos, bos_index = broke_low(candles, pivots, i + 1, end)
                if bos or not p.require_bos:
                    start = _group_start(candles, i, True, p.max_candles)
                    top, bottom = _extent(candles, start, i, Side.SELL, p.extent)
                    blocks.append(
                        OrderBlock(
                            side=Side.SELL,
                            top=top,
                            bottom=bottom,
                            index=i,
                            ts=candle.ts,
                            candles=i - start + 1,
                            displacement_atr=drop / atr_value,
                            bos=bos,
                            bos_index=bos_index,
                        )
                    )

        # Order block de compra: vela de baixa seguida por deslocamento de alta.
        if candle.is_bear:
            rally = max(b.high for b in window) - candle.low
            if rally >= p.displacement_atr * atr_value:
                bos, bos_index = broke_high(candles, pivots, i + 1, end)
                if bos or not p.require_bos:
                    start = _group_start(candles, i, False, p.max_candles)
                    top, bottom = _extent(candles, start, i, Side.BUY, p.extent)
                    blocks.append(
                        OrderBlock(
                            side=Side.BUY,
                            top=top,
                            bottom=bottom,
                            index=i,
                            ts=candle.ts,
                            candles=i - start + 1,
                            displacement_atr=rally / atr_value,
                            bos=bos,
                            bos_index=bos_index,
                        )
                    )

    _mark_mitigation(blocks, candles, p.leg_bars)
    blocks.sort(key=lambda b: -b.index)
    return _dedupe(blocks)


def _mark_mitigation(blocks: list[OrderBlock], candles: Series, leg_bars: int) -> None:
    """Marca blocos que já foram retestados (mitigados) após o deslocamento."""
    for block in blocks:
        for i in range(block.index + leg_bars + 1, len(candles)):
            bar = candles[i]
            touched = (
                bar.high >= block.bottom if block.side is Side.SELL else bar.low <= block.top
            )
            if touched:
                block.mitigated = True
                block.mitigated_index = i
                break


def _dedupe(blocks: list[OrderBlock]) -> list[OrderBlock]:
    """Remove blocos sobrepostos do mesmo lado, mantendo o mais recente/forte."""
    kept: list[OrderBlock] = []
    for block in blocks:
        duplicate = False
        for other in kept:
            if other.side is block.side and block.overlap(other) > 0:
                if block.displacement_atr > other.displacement_atr and block.index >= other.index:
                    kept.remove(other)
                    break
                duplicate = True
                break
        if not duplicate:
            kept.append(block)
    return kept


def order_block_strength(block: OrderBlock) -> float:
    """Nota 0-100: força do deslocamento, BOS e frescor do bloco."""
    displacement = min(block.displacement_atr / 3.0, 1.0) * 55.0
    bos = 25.0 if block.bos else 0.0
    fresh = 20.0 if not block.mitigated else 5.0
    return displacement + bos + fresh
