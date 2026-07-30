"""Tipos do robô de alta frequência: tick, ordem, execução e posição."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


@dataclass(frozen=True)
class Tick:
    """Melhor oferta de compra e venda (topo do livro)."""

    ts: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def microprice(self) -> float:
        """Mid ponderado pelo tamanho: prevê melhor o próximo movimento."""
        total = self.bid_size + self.ask_size
        if total <= 0:
            return self.mid
        # mais volume na compra empurra o preço para o ask
        return (self.bid * self.ask_size + self.ask * self.bid_size) / total

    @property
    def imbalance(self) -> float:
        """Desequilíbrio do livro em [-1, 1]; positivo = pressão compradora."""
        total = self.bid_size + self.ask_size
        if total <= 0:
            return 0.0
        return (self.bid_size - self.ask_size) / total

    def price_for(self, side: Side) -> float:
        """Preço agressor: compra no ask, vende no bid."""
        return self.ask if side is Side.BUY else self.bid


@dataclass
class Intent:
    """Decisão da estratégia: abrir posição com alvo e stop em pips."""

    side: Side
    take_profit_pips: float
    stop_pips: float
    max_hold_seconds: float
    reason: str
    lots: Optional[float] = None
    edge_bps: float = 0.0  # vantagem estimada, em pontos-base do preço


@dataclass
class Fill:
    ts: datetime
    side: Side
    price: float
    lots: float
    commission: float = 0.0
    slippage_pips: float = 0.0


@dataclass
class Position:
    """Posição aberta e sua contabilidade."""

    side: Side
    entry: float
    lots: float
    opened_at: datetime
    take_profit: float
    stop_loss: float
    max_hold_seconds: float
    reason: str = ""
    commission_paid: float = 0.0
    entry_slippage_pips: float = 0.0

    def unrealized(self, tick: Tick, pip: float, pip_value: float) -> float:
        exit_price = tick.bid if self.side is Side.BUY else tick.ask
        pips = (exit_price - self.entry) / pip * self.side.sign
        return pips * pip_value * self.lots

    def age_seconds(self, now: datetime) -> float:
        return (now - self.opened_at).total_seconds()


@dataclass
class Trade:
    """Operação encerrada, com a decomposição bruto/custos/líquido."""

    symbol: str
    side: Side
    lots: float
    opened_at: datetime
    closed_at: datetime
    entry: float
    exit: float
    gross_pnl: float
    commission: float
    net_pnl: float
    pips: float
    exit_reason: str
    reason: str = ""
    edge_bps: float = 0.0

    @property
    def duration_seconds(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds()

    @property
    def won(self) -> bool:
        return self.net_pnl > 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "lots": round(self.lots, 3),
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "seconds": round(self.duration_seconds, 2),
            "entry": self.entry,
            "exit": self.exit,
            "pips": round(self.pips, 2),
            "gross_pnl": round(self.gross_pnl, 4),
            "commission": round(self.commission, 4),
            "net_pnl": round(self.net_pnl, 4),
            "exit_reason": self.exit_reason,
            "reason": self.reason,
        }


@dataclass
class AccountState:
    """Estado da conta durante a sessão de operação."""

    balance: float
    equity: float = 0.0
    starting_balance: float = 0.0
    day_start_balance: float = 0.0
    peak_equity: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""

    def __post_init__(self) -> None:
        self.equity = self.equity or self.balance
        self.starting_balance = self.starting_balance or self.balance
        self.day_start_balance = self.day_start_balance or self.balance
        self.peak_equity = self.peak_equity or self.balance

    def register(self, trade: Trade) -> None:
        self.trades.append(trade)
        self.balance += trade.net_pnl
        self.equity = self.balance
        self.peak_equity = max(self.peak_equity, self.equity)
        if trade.net_pnl > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

    @property
    def net_pnl(self) -> float:
        return self.balance - self.starting_balance

    @property
    def day_pnl(self) -> float:
        return self.balance - self.day_start_balance

    @property
    def drawdown(self) -> float:
        return self.peak_equity - self.equity

    def roll_day(self) -> None:
        self.day_start_balance = self.balance


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
