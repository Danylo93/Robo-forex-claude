"""Gera SVG do setup (candles, zona, order block, entrada/stop/alvo).

SVG escrito à mão para não depender de matplotlib — abre em qualquer navegador
e reproduz o visual dos prints: caixa cinza na zona, linhas de VENDA/COMPRA,
STOP e ALVO.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from .models import Side, Signal
from .strategy import Analysis

BG = "#0f0f10"
GRID = "#232326"
BULL = "#e8e8e8"
BEAR = "#e03131"
TEXT = "#e8e8e8"
ZONE_FILL = "rgba(255,255,255,0.07)"
OB_FILL = "rgba(224,49,49,0.16)"
OB_FILL_BUY = "rgba(64,192,120,0.18)"
LINE = "#ffffff"


class _Scale:
    def __init__(self, width: int, height: int, bars: int, low: float, high: float, pad: int = 60):
        self.width, self.height, self.pad = width, height, pad
        self.bars = max(bars, 1)
        span = (high - low) or 1.0
        self.low = low - span * 0.05
        self.high = high + span * 0.05
        self.right_pad = 96

    def x(self, i: int) -> float:
        usable = self.width - self.pad - self.right_pad
        return self.pad + usable * (i / self.bars)

    def y(self, price: float) -> float:
        usable = self.height - 2 * self.pad
        ratio = (price - self.low) / (self.high - self.low)
        return self.height - self.pad - usable * ratio

    @property
    def bar_width(self) -> float:
        usable = self.width - self.pad - self.right_pad
        return max(1.6, usable / self.bars * 0.62)


def render_svg(
    analysis: Analysis, signal: Signal | None = None, bars: int = 140, width: int = 1200,
    height: int = 700,
) -> str:
    candles = analysis.candles[-bars:]
    if not candles:
        return "<svg xmlns='http://www.w3.org/2000/svg'/>"
    offset = len(analysis.candles) - len(candles)
    prices = [c.high for c in candles] + [c.low for c in candles]
    if signal:
        prices += [signal.entry, signal.stop, signal.target]
    scale = _Scale(width, height, len(candles), min(prices), max(prices))

    parts = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' font-family='Menlo,Consolas,monospace'>",
        f"<rect width='{width}' height='{height}' fill='{BG}'/>",
    ]

    # grade horizontal
    for i in range(5):
        price = scale.low + (scale.high - scale.low) * i / 4
        y = scale.y(price)
        parts.append(
            f"<line x1='{scale.pad}' y1='{y:.1f}' x2='{width - scale.right_pad}' y2='{y:.1f}' "
            f"stroke='{GRID}' stroke-width='1'/>"
        )
        parts.append(
            f"<text x='{width - scale.right_pad + 8}' y='{y + 4:.1f}' fill='#8a8a8f' "
            f"font-size='11'>{price:.5f}</text>"
        )

    digits = signal.digits if signal else 5

    # zona e order block
    if signal and signal.zone:
        top, bottom = scale.y(signal.zone.top), scale.y(signal.zone.bottom)
        parts.append(
            f"<rect x='{scale.pad}' y='{top:.1f}' width='{width - scale.pad - scale.right_pad}' "
            f"height='{max(bottom - top, 2):.1f}' fill='{ZONE_FILL}'/>"
        )
    if signal and signal.order_block:
        block = signal.order_block
        x = scale.x(max(block.index - offset, 0))
        top, bottom = scale.y(block.top), scale.y(block.bottom)
        fill = OB_FILL if block.side is Side.SELL else OB_FILL_BUY
        parts.append(
            f"<rect x='{x:.1f}' y='{top:.1f}' width='{width - scale.right_pad - x:.1f}' "
            f"height='{max(bottom - top, 2):.1f}' fill='{fill}'/>"
        )

    # linha de tendência
    if signal and signal.trendline:
        first = max(min(p.index for p in signal.trendline.pivots) - offset, 0)
        x1, x2 = scale.x(first), scale.x(len(candles) - 1)
        y1 = scale.y(signal.trendline.value_at(first + offset))
        y2 = scale.y(signal.trendline.value_at(len(candles) - 1 + offset))
        parts.append(
            f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' stroke='{BEAR}' "
            f"stroke-width='1.4' stroke-dasharray='3 4'/>"
        )

    # candles
    bw = scale.bar_width
    for i, candle in enumerate(candles):
        x = scale.x(i)
        color = BULL if candle.close >= candle.open else BEAR
        parts.append(
            f"<line x1='{x:.1f}' y1='{scale.y(candle.high):.1f}' x2='{x:.1f}' "
            f"y2='{scale.y(candle.low):.1f}' stroke='{color}' stroke-width='1'/>"
        )
        body_top = scale.y(max(candle.open, candle.close))
        body_bottom = scale.y(min(candle.open, candle.close))
        parts.append(
            f"<rect x='{x - bw / 2:.1f}' y='{body_top:.1f}' width='{bw:.1f}' "
            f"height='{max(body_bottom - body_top, 1):.1f}' fill='{color}'/>"
        )

    # níveis operacionais
    if signal:
        for price, label in (
            (signal.stop, "STOP"),
            (signal.entry, signal.side.label_ptbr),
            (signal.target, "ALVO"),
        ):
            y = scale.y(price)
            parts.append(
                f"<line x1='{scale.pad}' y1='{y:.1f}' x2='{width - scale.right_pad}' "
                f"y2='{y:.1f}' stroke='{LINE}' stroke-width='1.6'/>"
            )
            parts.append(
                f"<text x='{width - scale.right_pad - 6}' y='{y - 6:.1f}' fill='{TEXT}' "
                f"font-size='13' text-anchor='end'>{escape(label)} {price:.{digits}f}</text>"
            )

    title = f"{analysis.symbol}, {analysis.timeframe}"
    subtitle = (
        f"{signal.side.label_ptbr} {signal.fmt(signal.entry)} | STOP {signal.fmt(signal.stop)} | "
        f"ALVO {signal.fmt(signal.target)} | RR {signal.rr:.1f}:1 | nota {signal.score:.0f}"
        if signal
        else "sem setup válido"
    )
    parts.append(
        f"<text x='{scale.pad}' y='34' fill='#5a5a60' font-size='30' font-weight='bold'>"
        f"{escape(title)}</text>"
    )
    parts.append(
        f"<text x='{scale.pad}' y='56' fill='{TEXT}' font-size='13'>{escape(subtitle)}</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts)


def save_svg(analysis: Analysis, signal: Signal | None, directory: str | Path, bars: int = 140) -> Path:
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "sinal" if signal else "contexto"
    path = out_dir / f"{analysis.symbol}_{analysis.timeframe}_{suffix}.svg"
    path.write_text(render_svg(analysis, signal, bars), encoding="utf-8")
    return path
