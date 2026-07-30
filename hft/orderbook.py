"""Livro de ofertas L2: níveis de preço agregados, com métricas de fluxo.

O topo do livro (L1) basta para scalping; market making precisa de profundidade
para medir pressão, estimar custo de varrer o livro e posicionar a fila.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import Side, Tick

NEW = "0"
CHANGE = "1"
DELETE = "2"


class OrderBook:
    """Níveis agregados por preço, ordenados sob demanda."""

    __slots__ = ("_bids", "_asks", "_sorted_bids", "_sorted_asks", "_dirty", "depth", "last_update")

    def __init__(self, depth: int = 10):
        self.depth = depth
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._sorted_bids: list[tuple[float, float]] = []
        self._sorted_asks: list[tuple[float, float]] = []
        self._dirty = True
        self.last_update: Optional[datetime] = None

    # ----------------------------------------------------------- atualização
    def apply(self, side: Side, price: float, size: float, action: str = NEW,
              ts: Optional[datetime] = None) -> None:
        """Aplica uma entrada incremental (MDUpdateAction 0/1/2)."""
        levels = self._bids if side is Side.BUY else self._asks
        if action == DELETE or size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self._dirty = True
        self.last_update = ts or datetime.now(timezone.utc)

    def snapshot(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]],
                 ts: Optional[datetime] = None) -> None:
        """Substitui o livro inteiro (mensagem W)."""
        self._bids = {price: size for price, size in bids if size > 0}
        self._asks = {price: size for price, size in asks if size > 0}
        self._dirty = True
        self.last_update = ts or datetime.now(timezone.utc)

    def clear(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self._dirty = True

    def _rebuild(self) -> None:
        if not self._dirty:
            return
        self._sorted_bids = sorted(self._bids.items(), key=lambda kv: -kv[0])[: self.depth]
        self._sorted_asks = sorted(self._asks.items(), key=lambda kv: kv[0])[: self.depth]
        self._dirty = False

    # -------------------------------------------------------------- consulta
    @property
    def bids(self) -> list[tuple[float, float]]:
        self._rebuild()
        return self._sorted_bids

    @property
    def asks(self) -> list[tuple[float, float]]:
        self._rebuild()
        return self._sorted_asks

    @property
    def best_bid(self) -> Optional[tuple[float, float]]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[tuple[float, float]]:
        return self.asks[0] if self.asks else None

    @property
    def valid(self) -> bool:
        bid, ask = self.best_bid, self.best_ask
        return bool(bid and ask and ask[0] > bid[0])

    @property
    def mid(self) -> float:
        bid, ask = self.best_bid, self.best_ask
        if not bid or not ask:
            return 0.0
        return (bid[0] + ask[0]) / 2.0

    @property
    def spread(self) -> float:
        bid, ask = self.best_bid, self.best_ask
        return ask[0] - bid[0] if bid and ask else 0.0

    @property
    def microprice(self) -> float:
        bid, ask = self.best_bid, self.best_ask
        if not bid or not ask:
            return 0.0
        total = bid[1] + ask[1]
        if total <= 0:
            return self.mid
        return (bid[0] * ask[1] + ask[0] * bid[1]) / total

    def imbalance(self, levels: int = 3) -> float:
        """Desequilíbrio de volume nos N melhores níveis, em [-1, 1]."""
        bid_volume = sum(size for _, size in self.bids[:levels])
        ask_volume = sum(size for _, size in self.asks[:levels])
        total = bid_volume + ask_volume
        if total <= 0:
            return 0.0
        return (bid_volume - ask_volume) / total

    def sweep(self, side: Side, quantity: float) -> tuple[float, float]:
        """Preço médio e quantidade atendida ao varrer o livro agressivamente.

        Compra consome o lado do ask. Devolve (preço_médio, quantidade_atendida).
        """
        levels = self.asks if side is Side.BUY else self.bids
        remaining = quantity
        cost = 0.0
        for price, size in levels:
            take = min(remaining, size)
            cost += take * price
            remaining -= take
            if remaining <= 0:
                break
        filled = quantity - remaining
        return (cost / filled if filled > 0 else 0.0), filled

    def slippage(self, side: Side, quantity: float) -> float:
        """Quanto varrer `quantity` custa acima do topo do livro."""
        average, filled = self.sweep(side, quantity)
        top = self.best_ask if side is Side.BUY else self.best_bid
        if not filled or not top:
            return 0.0
        return (average - top[0]) * side.sign

    def queue_ahead(self, side: Side, price: float) -> float:
        """Volume à frente na fila daquele nível (aproxima a posição na fila)."""
        levels = self._bids if side is Side.BUY else self._asks
        return levels.get(price, 0.0)

    def to_tick(self, ts: Optional[datetime] = None) -> Optional[Tick]:
        """Converte o topo do livro no tick usado pelo motor."""
        bid, ask = self.best_bid, self.best_ask
        if not bid or not ask:
            return None
        return Tick(
            ts=ts or self.last_update or datetime.now(timezone.utc),
            bid=bid[0],
            ask=ask[0],
            bid_size=bid[1],
            ask_size=ask[1],
        )
