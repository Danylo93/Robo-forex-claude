"""Corretora simulada com custos realistas.

Usada no backtest e no modo papel. Aplica spread, comissão e derrapagem — os
mesmos custos que decidem se a estratégia sobrevive na conta real.
"""

from __future__ import annotations

from typing import Optional

from ..config import HftSettings
from ..models import Fill, Position, Side, Tick


class PaperBroker:
    name = "paper"

    def __init__(self, settings: HftSettings, balance: Optional[float] = None):
        self.settings = settings
        self._balance = balance if balance is not None else settings.risk.account_balance
        self._tick: Optional[Tick] = None
        self.commission_paid = 0.0
        self.slippage_paid_pips = 0.0

    # ------------------------------------------------------------------ feed
    def connect(self) -> None:
        return None

    def feed(self, tick: Tick) -> None:
        """O motor injeta o tick atual (backtest e modo papel)."""
        self._tick = tick

    def tick(self) -> Optional[Tick]:
        return self._tick

    def shutdown(self) -> None:
        return None

    # --------------------------------------------------------------- ordens
    def open(
        self, side: Side, lots: float, tick: Tick, take_profit: float, stop_loss: float
    ) -> Fill:
        price = self._fill_price(side, tick)
        commission = self.settings.costs.commission_per_lot_per_side * lots
        self.commission_paid += commission
        self.slippage_paid_pips += self.settings.costs.extra_slippage_pips
        return Fill(
            ts=tick.ts,
            side=side,
            price=price,
            lots=lots,
            commission=commission,
            slippage_pips=self.settings.costs.extra_slippage_pips,
        )

    def close(self, position: Position, tick: Tick, reason: str) -> Fill:
        side = position.side.opposite
        price = self._fill_price(side, tick)
        commission = self.settings.costs.commission_per_lot_per_side * position.lots
        self.commission_paid += commission
        self.slippage_paid_pips += self.settings.costs.extra_slippage_pips
        return Fill(
            ts=tick.ts,
            side=side,
            price=price,
            lots=position.lots,
            commission=commission,
            slippage_pips=self.settings.costs.extra_slippage_pips,
        )

    def balance(self) -> float:
        return self._balance

    def apply_pnl(self, amount: float) -> None:
        self._balance += amount

    # ------------------------------------------------------------ auxiliares
    def _fill_price(self, side: Side, tick: Tick) -> float:
        """Agressor paga o spread, mais markup e derrapagem contra a posição."""
        costs = self.settings.costs
        pip = self.settings.instrument.pip
        penalty = (costs.extra_slippage_pips + costs.spread_markup_pips / 2.0) * pip
        return tick.price_for(side) + penalty * side.sign
