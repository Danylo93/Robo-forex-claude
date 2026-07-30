"""Estratégias de alta frequência.

Duas famílias que fazem sentido em conta retail (latência de dezenas de ms):

- `mean_reversion`: o preço se afasta demais da média da janela sem que o livro
  confirme a direção -> aposta na volta. É a que melhor sobrevive à latência,
  porque não disputa velocidade: espera o exagero.
- `imbalance_momentum`: desequilíbrio persistente do livro + movimento na mesma
  direção -> segue o fluxo por poucos segundos.

Toda entrada só é liberada se a vantagem estimada superar o custo do
round-turn (`min_edge_multiple`). Sem isso, alta frequência vira uma máquina de
pagar spread.
"""

from __future__ import annotations

from typing import Optional, Protocol

from .config import HftSettings
from .features import Features
from .models import Intent, Position, Side, Tick


class Strategy(Protocol):
    name: str

    def entry(self, tick: Tick, features: Features) -> Optional[Intent]: ...

    def exit(self, tick: Tick, features: Features, position: Position) -> Optional[str]: ...


class MeanReversionStrategy:
    """Compra o exagero para baixo e vende o exagero para cima."""

    name = "mean_reversion"

    def __init__(self, settings: HftSettings):
        self.settings = settings

    def entry(self, tick: Tick, features: Features) -> Optional[Intent]:
        cfg = self.settings.strategy
        if not features.ready:
            return None
        side: Optional[Side] = None
        if features.z_score <= -cfg.entry_z and features.imbalance >= -cfg.imbalance_threshold:
            side = Side.BUY  # caiu demais e o livro não está vendendo com força
        elif features.z_score >= cfg.entry_z and features.imbalance <= cfg.imbalance_threshold:
            side = Side.SELL
        if side is None:
            return None
        return Intent(
            side=side,
            take_profit_pips=cfg.take_profit_pips,
            stop_pips=cfg.stop_pips,
            max_hold_seconds=cfg.max_hold_seconds,
            edge_bps=features.reversion_edge_bps,
            reason=(
                f"z={features.z_score:+.2f} vol={features.volatility_bps:.2f}bps "
                f"livro={features.imbalance:+.2f}"
            ),
        )

    def exit(self, tick: Tick, features: Features, position: Position) -> Optional[str]:
        cfg = self.settings.strategy
        if position.side is Side.BUY and features.z_score >= -cfg.exit_z:
            return "voltou à média"
        if position.side is Side.SELL and features.z_score <= cfg.exit_z:
            return "voltou à média"
        return None


class ImbalanceMomentumStrategy:
    """Segue desequilíbrio persistente do livro confirmado pelo movimento."""

    name = "imbalance_momentum"

    def __init__(self, settings: HftSettings):
        self.settings = settings

    def entry(self, tick: Tick, features: Features) -> Optional[Intent]:
        cfg = self.settings.strategy
        if not features.ready:
            return None
        side: Optional[Side] = None
        if (
            features.imbalance >= cfg.imbalance_threshold
            and features.momentum_bps >= cfg.momentum_bps
        ):
            side = Side.BUY
        elif (
            features.imbalance <= -cfg.imbalance_threshold
            and features.momentum_bps <= -cfg.momentum_bps
        ):
            side = Side.SELL
        if side is None:
            return None
        return Intent(
            side=side,
            take_profit_pips=cfg.take_profit_pips,
            stop_pips=cfg.stop_pips,
            max_hold_seconds=cfg.max_hold_seconds,
            edge_bps=abs(features.momentum_bps),
            reason=(
                f"fluxo={features.imbalance:+.2f} momento={features.momentum_bps:+.2f}bps"
            ),
        )

    def exit(self, tick: Tick, features: Features, position: Position) -> Optional[str]:
        cfg = self.settings.strategy
        # o fluxo virou contra a posição: sai antes do stop
        if position.side is Side.BUY and features.imbalance <= -cfg.imbalance_threshold:
            return "fluxo inverteu"
        if position.side is Side.SELL and features.imbalance >= cfg.imbalance_threshold:
            return "fluxo inverteu"
        return None


STRATEGIES = {
    MeanReversionStrategy.name: MeanReversionStrategy,
    ImbalanceMomentumStrategy.name: ImbalanceMomentumStrategy,
}


def build_strategy(settings: HftSettings) -> Strategy:
    name = settings.strategy.name
    if name not in STRATEGIES:
        raise ValueError(
            f"estratégia desconhecida: {name} (disponíveis: {', '.join(STRATEGIES)})"
        )
    return STRATEGIES[name](settings)
