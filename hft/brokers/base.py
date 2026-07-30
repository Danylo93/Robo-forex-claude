"""Contrato dos adaptadores de corretora."""

from __future__ import annotations

from typing import Optional, Protocol

from ..models import Fill, Position, Side, Tick


class BrokerError(RuntimeError):
    pass


class Broker(Protocol):
    """Interface mínima que o motor precisa para operar."""

    name: str

    def connect(self) -> None: ...

    def tick(self) -> Optional[Tick]:
        """Último preço disponível (None quando o feed ainda não respondeu)."""

    def open(
        self, side: Side, lots: float, tick: Tick, take_profit: float, stop_loss: float
    ) -> Fill: ...

    def close(self, position: Position, tick: Tick, reason: str) -> Fill: ...

    def balance(self) -> float: ...

    def shutdown(self) -> None: ...
