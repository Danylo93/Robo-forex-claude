"""Tipos base do robô: candles, pivots, zonas, order blocks e sinais."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Sequence


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def label_ptbr(self) -> str:
        return "COMPRA" if self is Side.BUY else "VENDA"

    @property
    def sign(self) -> int:
        """+1 para compra, -1 para venda (usado nos cálculos de risco)."""
        return 1 if self is Side.BUY else -1


class ZoneKind(str, Enum):
    SUPPLY = "supply"
    DEMAND = "demand"

    @property
    def side(self) -> Side:
        return Side.SELL if self is ZoneKind.SUPPLY else Side.BUY


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_bull(self) -> bool:
        return self.close > self.open

    @property
    def is_bear(self) -> bool:
        return self.close < self.open

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass(frozen=True)
class Pivot:
    """Topo ou fundo fractal."""

    index: int
    price: float
    kind: str  # "high" | "low"
    ts: Optional[datetime] = None

    @property
    def is_high(self) -> bool:
        return self.kind == "high"


@dataclass
class Zone:
    """Linha de oferta/demanda: faixa horizontal respeitada várias vezes."""

    kind: ZoneKind
    top: float
    bottom: float
    pivots: list[Pivot] = field(default_factory=list)
    touches: int = 0
    rejections: int = 0
    created_index: int = 0
    last_touch_index: int = 0
    broken: bool = False
    broken_index: Optional[int] = None

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def proximal(self) -> float:
        """Borda por onde o preço entra na zona (onde fica a ordem)."""
        return self.bottom if self.kind is ZoneKind.SUPPLY else self.top

    @property
    def distal(self) -> float:
        """Borda oposta, atrás da qual fica o stop."""
        return self.top if self.kind is ZoneKind.SUPPLY else self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def overlap(self, other: "Zone | OrderBlock") -> float:
        """Altura da interseção com outra faixa (0.0 se não há sobreposição)."""
        return max(0.0, min(self.top, other.top) - max(self.bottom, other.bottom))


@dataclass
class OrderBlock:
    """Última(s) vela(s) contrárias antes de um deslocamento que rompe estrutura."""

    side: Side  # SELL = order block de venda (vela de alta antes da queda)
    top: float
    bottom: float
    index: int
    ts: Optional[datetime] = None
    candles: int = 1
    displacement_atr: float = 0.0
    bos: bool = False
    bos_index: Optional[int] = None
    mitigated: bool = False
    mitigated_index: Optional[int] = None

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def proximal(self) -> float:
        return self.bottom if self.side is Side.SELL else self.top

    @property
    def distal(self) -> float:
        return self.top if self.side is Side.SELL else self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def overlap(self, other: "Zone | OrderBlock") -> float:
        return max(0.0, min(self.top, other.top) - max(self.bottom, other.bottom))


@dataclass
class Trendline:
    """Reta ajustada em pivots (LTA/LTB) usada como confluência extra."""

    kind: str  # "resistance" (topos) | "support" (fundos)
    slope: float
    intercept: float
    pivots: list[Pivot] = field(default_factory=list)
    touches: int = 0

    def value_at(self, index: int) -> float:
        return self.intercept + self.slope * index


@dataclass
class NewsEvent:
    ts: datetime
    currency: str
    title: str
    impact: str  # "high" | "medium" | "low"

    @property
    def is_high(self) -> bool:
        return self.impact.lower().startswith("h")


@dataclass
class NewsVerdict:
    blocked: bool
    reason: str = ""
    events: list[NewsEvent] = field(default_factory=list)


@dataclass
class Position:
    """Dimensionamento sugerido da operação."""

    risk_amount: float
    stop_pips: float
    lots: float
    units: float
    pip_value: float


@dataclass
class Signal:
    """Setup pronto: entrada, stop, alvo e justificativa."""

    symbol: str
    timeframe: str
    side: Side
    entry: float
    stop: float
    target: float
    rr: float
    score: float
    ts: datetime
    digits: int = 5
    zone: Optional[Zone] = None
    order_block: Optional[OrderBlock] = None
    trendline: Optional[Trendline] = None
    htf_bias: str = "neutral"
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    position: Optional[Position] = None
    target_basis: str = ""
    last_price: float = 0.0

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward(self) -> float:
        return abs(self.target - self.entry)

    @property
    def distance_to_entry(self) -> float:
        return abs(self.last_price - self.entry)

    def fmt(self, price: float) -> str:
        return f"{price:.{self.digits}f}"

    def to_dict(self) -> dict:
        data = {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "side_ptbr": self.side.label_ptbr,
            "entry": round(self.entry, self.digits),
            "stop": round(self.stop, self.digits),
            "target": round(self.target, self.digits),
            "rr": round(self.rr, 2),
            "score": round(self.score, 1),
            "ts": self.ts.isoformat(),
            "htf_bias": self.htf_bias,
            "target_basis": self.target_basis,
            "last_price": round(self.last_price, self.digits),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }
        if self.zone:
            data["zone"] = {
                "kind": self.zone.kind.value,
                "top": round(self.zone.top, self.digits),
                "bottom": round(self.zone.bottom, self.digits),
                "touches": self.zone.touches,
                "rejections": self.zone.rejections,
            }
        if self.order_block:
            data["order_block"] = {
                "side": self.order_block.side.value,
                "top": round(self.order_block.top, self.digits),
                "bottom": round(self.order_block.bottom, self.digits),
                "displacement_atr": round(self.order_block.displacement_atr, 2),
                "bos": self.order_block.bos,
                "mitigated": self.order_block.mitigated,
            }
        if self.trendline:
            data["trendline"] = {
                "kind": self.trendline.kind,
                "touches": self.trendline.touches,
            }
        if self.position:
            data["position"] = {
                "risk_amount": round(self.position.risk_amount, 2),
                "stop_pips": round(self.position.stop_pips, 1),
                "lots": round(self.position.lots, 2),
                "units": round(self.position.units, 0),
            }
        return data


Series = Sequence[Candle]
