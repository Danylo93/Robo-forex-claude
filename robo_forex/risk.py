"""Cálculo de stop, alvo, relação risco-retorno e tamanho de posição."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import RiskSettings, SymbolSpec
from .models import Position, Side


@dataclass
class TargetChoice:
    price: float
    rr: float
    basis: str  # descrição da referência usada


def stop_from_zone(side: Side, risk_level: float, atr_value: float, cfg: RiskSettings) -> float:
    """Stop atrás da borda distal da zona, com respiro em ATR."""
    buffer = cfg.stop_buffer_atr * atr_value
    return risk_level + buffer if side is Side.SELL else risk_level - buffer


def entry_from_zone(side: Side, proximal: float, atr_value: float, cfg: RiskSettings) -> float:
    """Entrada na borda proximal; `entry_offset_atr` desloca a ordem para dentro."""
    offset = cfg.entry_offset_atr * atr_value
    return proximal + offset if side is Side.SELL else proximal - offset


def rr_of(side: Side, entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    return abs(target - entry) / risk


def pick_target(
    side: Side,
    entry: float,
    stop: float,
    candidates: list[tuple[float, str]],
    atr_value: float,
    cfg: RiskSettings,
) -> Optional[TargetChoice]:
    """Escolhe o alvo.

    `structure`: primeira liquidez oposta (fundo/topo, zona contrária) que já
    entregue o RR mínimo. `rr`: alvo puramente derivado do risco.
    `structure_then_rr` (padrão): tenta a estrutura e, se nenhuma referência
    servir, usa o alvo por RR.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    padding = cfg.target_padding_atr * atr_value

    if cfg.target_mode in ("structure", "structure_then_rr"):
        ordered = sorted(
            candidates,
            key=lambda item: abs(entry - item[0]),
        )
        for price, basis in ordered:
            adjusted = price + padding if side is Side.SELL else price - padding
            if side is Side.SELL and adjusted >= entry:
                continue
            if side is Side.BUY and adjusted <= entry:
                continue
            rr = rr_of(side, entry, stop, adjusted)
            if rr >= cfg.min_rr:
                return TargetChoice(price=adjusted, rr=rr, basis=basis)

    if cfg.target_mode in ("rr", "structure_then_rr"):
        target = entry - cfg.min_rr * risk if side is Side.SELL else entry + cfg.min_rr * risk
        return TargetChoice(
            price=target, rr=cfg.min_rr, basis=f"alvo por RR fixo {cfg.min_rr:.1f}:1"
        )
    return None


def pip_value_per_lot(
    spec: SymbolSpec,
    price: float,
    account_currency: str = "USD",
    conversion_rate: Optional[float] = None,
) -> tuple[float, Optional[str]]:
    """Valor financeiro de 1 pip por lote padrão, na moeda da conta.

    Retorna (valor, aviso). Quando nenhuma das moedas do par é a moeda da conta
    e não há taxa de conversão informada, o cálculo assume 1:1 e devolve aviso —
    o tamanho de posição deve ser conferido na corretora.
    """
    notional_pip = spec.pip * spec.contract_size
    if spec.kind != "forex":
        return notional_pip, None
    account = account_currency.upper()
    if spec.quote == account:
        return notional_pip, None
    if spec.base == account and price > 0:
        return notional_pip / price, None
    if conversion_rate and conversion_rate > 0:
        return notional_pip * conversion_rate, None
    return notional_pip, (
        f"valor do pip aproximado: {spec.quote or 'moeda de cotação'} != {account} e "
        "nenhuma taxa de conversão informada"
    )


def size_position(
    spec: SymbolSpec,
    entry: float,
    stop: float,
    cfg: RiskSettings,
    conversion_rate: Optional[float] = None,
) -> tuple[Position, list[str]]:
    """Dimensiona a operação a partir do risco percentual da conta."""
    warnings: list[str] = []
    stop_distance = abs(entry - stop)
    stop_pips = stop_distance / spec.pip if spec.pip else 0.0
    risk_amount = cfg.account_balance * (cfg.risk_percent / 100.0)
    pip_value, warning = pip_value_per_lot(spec, entry, cfg.account_currency, conversion_rate)
    if warning:
        warnings.append(warning)
    lots = 0.0
    if stop_pips > 0 and pip_value > 0:
        lots = risk_amount / (stop_pips * pip_value)
    return (
        Position(
            risk_amount=risk_amount,
            stop_pips=stop_pips,
            lots=round(lots, 2),
            units=round(lots * spec.contract_size, 0),
            pip_value=pip_value,
        ),
        warnings,
    )
