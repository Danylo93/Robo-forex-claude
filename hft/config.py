"""Configuração do robô HFT."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class InstrumentSettings:
    """Ativo e sua microestrutura."""

    symbol: str = "EURUSD"
    broker_symbol: str = ""  # nome no broker (EUR_USD na OANDA, EURUSD no MT5)
    pip: float = 0.0001
    digits: int = 5
    contract_size: float = 100_000.0
    pip_value_per_lot: float = 10.0  # na moeda da conta; confira na corretora
    min_lots: float = 0.01
    lot_step: float = 0.01
    max_lots: float = 5.0

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper().replace("/", "")
        if not self.broker_symbol:
            self.broker_symbol = self.symbol
        if "JPY" in self.symbol and self.pip == 0.0001:
            self.pip, self.digits = 0.01, 3

    def round_lots(self, lots: float) -> float:
        steps = max(1, round(lots / self.lot_step))
        return min(max(steps * self.lot_step, self.min_lots), self.max_lots)


@dataclass
class CostSettings:
    """Custos de transação — o que decide se um robô de alta frequência dá lucro."""

    commission_per_lot_per_side: float = 3.5  # conta ECN típica: US$ 7 o round-turn
    extra_slippage_pips: float = 0.1  # derrapagem média por perna
    latency_ms: float = 60.0  # atraso entre decidir e executar
    swap_per_lot_per_day: float = 0.0
    spread_markup_pips: float = 0.0  # corretora sem comissão: spread aumentado


@dataclass
class StrategySettings:
    name: str = "mean_reversion"  # mean_reversion | imbalance_momentum
    window_ticks: int = 120  # janela das features
    warmup_ticks: int = 200
    entry_z: float = 2.2  # desvios para acionar a reversão
    exit_z: float = 0.3
    imbalance_threshold: float = 0.35
    momentum_bps: float = 0.8
    take_profit_pips: float = 1.2
    stop_pips: float = 2.4
    max_hold_seconds: float = 90.0
    min_edge_multiple: float = 1.5  # vantagem estimada / custo por operação
    cooldown_seconds: float = 5.0


@dataclass
class RiskSettings:
    account_balance: float = 1_000.0
    risk_percent_per_trade: float = 0.25
    fixed_lots: float = 0.0  # >0 ignora o dimensionamento por risco
    max_spread_pips: float = 1.0  # spread acima disso: não opera
    max_daily_loss_percent: float = 2.0
    max_drawdown_percent: float = 5.0
    max_consecutive_losses: int = 5
    max_trades_per_hour: int = 60
    max_trades_per_day: int = 300
    max_open_positions: int = 1
    session_start_hour_utc: int = 7
    session_end_hour_utc: int = 20
    skip_weekend: bool = True
    news_blackout: bool = True
    news_minutes_before: int = 15
    news_minutes_after: int = 15


@dataclass
class BrokerSettings:
    provider: str = "paper"  # paper | oanda | mt5
    mode: str = "demo"  # demo | live  (live exige confirmação explícita)
    account_id: str = ""
    token_env: str = "OANDA_TOKEN"  # o segredo vem de variável de ambiente
    oanda_host: str = ""  # vazio: escolhido pelo modo
    poll_ms: float = 200.0  # intervalo de leitura de preço
    mt5_login: int = 0
    mt5_server: str = ""
    mt5_password_env: str = "MT5_PASSWORD"
    magic: int = 987001


@dataclass
class HftSettings:
    instrument: InstrumentSettings = field(default_factory=InstrumentSettings)
    costs: CostSettings = field(default_factory=CostSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    broker: BrokerSettings = field(default_factory=BrokerSettings)
    log_file: str = ""

    @classmethod
    def load(cls, path: str | Path | None) -> "HftSettings":
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"configuração não encontrada: {p}")
        raw = p.read_text(encoding="utf-8")
        if p.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise RuntimeError("PyYAML não instalado: use .json") from exc
            data = yaml.safe_load(raw) or {}
        else:
            data = json.loads(raw)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HftSettings":
        settings = cls()
        for name in ("instrument", "costs", "strategy", "risk", "broker"):
            if data.get(name):
                _apply(getattr(settings, name), data[name])
        if "log_file" in data:
            settings.log_file = data["log_file"]
        return settings

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": asdict(self.instrument),
            "costs": asdict(self.costs),
            "strategy": asdict(self.strategy),
            "risk": asdict(self.risk),
            "broker": asdict(self.broker),
            "log_file": self.log_file,
        }

    def cost_per_trade(self) -> float:
        """Custo estimado de um round-turn, na moeda da conta, por lote."""
        commission = 2 * self.costs.commission_per_lot_per_side
        slippage = 2 * self.costs.extra_slippage_pips * self.instrument.pip_value_per_lot
        return commission + slippage

    def cost_per_trade_pips(self, spread_pips: float) -> float:
        """Custo de um round-turn em pips: spread + comissão + derrapagem."""
        pip_value = self.instrument.pip_value_per_lot or 1.0
        commission_pips = 2 * self.costs.commission_per_lot_per_side / pip_value
        return (
            spread_pips
            + self.costs.spread_markup_pips
            + commission_pips
            + 2 * self.costs.extra_slippage_pips
        )


def _apply(target: Any, data: dict[str, Any]) -> None:
    valid = {f.name for f in fields(target)}
    for key, value in data.items():
        if key not in valid:
            raise ValueError(f"opção desconhecida em '{type(target).__name__}': {key}")
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(target, key, value)
    if hasattr(target, "__post_init__"):
        target.__post_init__()
