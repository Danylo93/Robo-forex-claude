"""Configuração do robô (YAML ou JSON, com defaults sensatos)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .analysis.confluence import ConfluenceParams
from .analysis.order_blocks import OrderBlockParams
from .analysis.trendlines import TrendlineParams
from .analysis.zones import ZoneParams

JPY_PIP = 0.01
FX_PIP = 0.0001


@dataclass
class SymbolSpec:
    """Especificação do ativo: como buscar e como converter preço em pips."""

    symbol: str
    feed_symbol: str = ""
    kind: str = "forex"  # forex | index | commodity | crypto
    pip: float = 0.0
    digits: int = 0
    contract_size: float = 100_000.0
    currencies: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper().replace("/", "")
        if not self.feed_symbol:
            self.feed_symbol = f"{self.symbol}=X" if self.kind == "forex" else self.symbol
        if not self.pip:
            if self.kind == "forex":
                self.pip = JPY_PIP if "JPY" in self.symbol else FX_PIP
            else:
                self.pip = 1.0
        if not self.digits:
            if self.kind == "forex":
                self.digits = 3 if "JPY" in self.symbol else 5
            else:
                self.digits = 2
        if not self.currencies and self.kind == "forex" and len(self.symbol) == 6:
            self.currencies = [self.symbol[:3], self.symbol[3:]]

    @property
    def base(self) -> str:
        return self.currencies[0] if self.currencies else ""

    @property
    def quote(self) -> str:
        return self.currencies[1] if len(self.currencies) > 1 else ""


@dataclass
class StrategySettings:
    timeframe: str = "1h"
    htf_timeframe: str = "4h"
    lookback: int = 600
    htf_lookback: int = 400
    atr_period: int = 14
    pivot_left: int = 2
    pivot_right: int = 2
    htf_pivot_left: int = 2
    htf_pivot_right: int = 2
    use_trendlines: bool = True
    allow_buy: bool = True
    allow_sell: bool = True
    min_score: float = 55.0
    max_distance_atr: float = 3.0  # distância máxima do preço até a entrada
    require_untested_block: bool = False
    require_htf_alignment: bool = False
    require_rejection_candle: bool = False  # confirmação de rejeição no reteste
    signal_ttl_bars: int = 12
    zone: ZoneParams = field(default_factory=ZoneParams)
    order_block: OrderBlockParams = field(default_factory=OrderBlockParams)
    trendline: TrendlineParams = field(default_factory=TrendlineParams)
    confluence: ConfluenceParams = field(default_factory=ConfluenceParams)


@dataclass
class RiskSettings:
    account_balance: float = 10_000.0
    risk_percent: float = 0.5
    min_rr: float = 3.0
    target_mode: str = "structure_then_rr"  # structure_then_rr | structure | rr
    stop_buffer_atr: float = 0.25
    entry_offset_atr: float = 0.0  # >0 posiciona a ordem dentro da zona
    max_stop_atr: float = 3.5
    target_padding_atr: float = 0.15  # alvo um pouco antes da liquidez oposta
    account_currency: str = "USD"


@dataclass
class CostSettings:
    """Custos de transação — sem eles o backtest mente por omissão."""

    spread_pips: float = 1.0  # padrão quando o par não está no mapa abaixo
    spread_by_symbol: dict[str, float] = field(
        default_factory=lambda: {
            "EURUSD": 0.8, "GBPUSD": 1.0, "AUDUSD": 0.9, "NZDUSD": 1.4, "USDCAD": 1.3,
            "USDJPY": 0.9, "EURGBP": 1.2, "GBPJPY": 2.0, "GBPNZD": 3.5, "AUDJPY": 1.6,
        }
    )
    slippage_pips: float = 0.2  # derrapagem por operação (entrada + saída)
    commission_per_lot_round_turn: float = 0.0  # conta ECN típica: 7.0
    enabled: bool = True

    def spread_for(self, symbol: str) -> float:
        return self.spread_by_symbol.get(symbol.upper(), self.spread_pips)


@dataclass
class NewsSettings:
    enabled: bool = True
    provider: str = "faireconomy"  # faireconomy | file | none
    url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    file: str = ""
    impacts: list[str] = field(default_factory=lambda: ["high"])
    minutes_before: int = 60
    minutes_after: int = 30
    fail_open: bool = True  # se o calendário falhar, segue operando (com aviso)
    cache_minutes: int = 30
    timeout: float = 10.0


@dataclass
class SessionSettings:
    enabled: bool = True
    start_hour_utc: int = 6
    end_hour_utc: int = 20
    skip_weekend: bool = True
    friday_cutoff_hour_utc: int = 19


@dataclass
class FeedSettings:
    provider: str = "yahoo"  # yahoo | csv | synthetic
    csv_dir: str = "data"
    cache_dir: str = ".cache"
    cache_minutes: int = 5


@dataclass
class OutputSettings:
    charts_dir: str = "charts"
    json_out: str = ""
    markdown_out: str = ""
    draw_charts: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class Settings:
    symbols: list[SymbolSpec] = field(
        default_factory=lambda: [
            SymbolSpec("GBPNZD"),
            SymbolSpec("AUDUSD"),
            SymbolSpec("EURUSD"),
            SymbolSpec("GBPUSD"),
            SymbolSpec("USDJPY"),
            SymbolSpec("GBPJPY"),
            SymbolSpec("AUDJPY"),
            SymbolSpec("EURGBP"),
            SymbolSpec("NZDUSD"),
            SymbolSpec("USDCAD"),
        ]
    )
    feed: FeedSettings = field(default_factory=FeedSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    costs: CostSettings = field(default_factory=CostSettings)
    news: NewsSettings = field(default_factory=NewsSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    output: OutputSettings = field(default_factory=OutputSettings)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | Path | None) -> "Settings":
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"arquivo de configuração não encontrado: {p}")
        raw = p.read_text(encoding="utf-8")
        if p.suffix.lower() in (".yaml", ".yml"):
            data = _load_yaml(raw)
        else:
            data = json.loads(raw)
        return cls.from_dict(data or {})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        settings = cls()
        symbols = data.get("symbols")
        if symbols:
            specs = []
            for item in symbols:
                if isinstance(item, str):
                    specs.append(SymbolSpec(item))
                else:
                    specs.append(SymbolSpec(**item))
            settings.symbols = specs
        for name in ("feed", "strategy", "risk", "costs", "news", "session", "output"):
            if name in data and data[name]:
                _apply(getattr(settings, name), data[name])
        return settings

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": [asdict(s) for s in self.symbols],
            "feed": asdict(self.feed),
            "strategy": asdict(self.strategy),
            "risk": asdict(self.risk),
            "costs": asdict(self.costs),
            "news": asdict(self.news),
            "session": asdict(self.session),
            "output": asdict(self.output),
        }

    def spec(self, symbol: str) -> SymbolSpec:
        key = symbol.upper().replace("/", "")
        for s in self.symbols:
            if s.symbol == key:
                return s
        return SymbolSpec(key)


def _apply(target: Any, data: dict[str, Any]) -> None:
    """Aplica um dicionário sobre um dataclass, recursivamente."""
    valid = {f.name: f for f in fields(target)}
    for key, value in data.items():
        if key not in valid:
            raise ValueError(f"opção desconhecida em '{type(target).__name__}': {key}")
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(target, key, value)


def _load_yaml(raw: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise RuntimeError(
            "PyYAML não instalado: use um arquivo .json ou instale 'pyyaml'"
        ) from exc
    return yaml.safe_load(raw)
