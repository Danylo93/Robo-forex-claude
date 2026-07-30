"""Robô de alta frequência para conta real: motor, risco e adaptadores.

O que este pacote é: um motor de scalping automatizado, com custos modelados,
kill-switch e adaptadores para OANDA (REST) e MetaTrader 5, além de corretora
simulada para backtest e modo papel.

O que ele não é: HFT institucional. Sem colocation, sem feed direto e sem FIX,
a latência de uma conta retail fica entre 20 e 200 ms — cedo demais para
arbitragem de latência ou market making competitivo. A vantagem tem que vir do
padrão, não da velocidade. Detalhes em docs/HFT.md.
"""

from .config import HftSettings
from .engine import Engine
from .models import AccountState, Intent, Position, Side, Tick, Trade
from .report import HftReport, render, summarize
from .risk import RiskManager
from .strategies import STRATEGIES, build_strategy

__version__ = "1.0.0"

__all__ = [
    "AccountState",
    "Engine",
    "HftReport",
    "HftSettings",
    "Intent",
    "Position",
    "RiskManager",
    "STRATEGIES",
    "Side",
    "Tick",
    "Trade",
    "build_strategy",
    "render",
    "summarize",
    "__version__",
]
