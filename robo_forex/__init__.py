"""Robô de análise: linha de oferta/demanda + order block em confluência.

Estratégia identificada nos gráficos de referência (GBP/NZD H1, HK50 H1 e
AUD/USD H4): o preço volta a uma linha horizontal de oferta (ou demanda) já
respeitada várias vezes, onde existe um order block do mesmo lado; a entrada fica
na borda proximal da região, o stop atrás da borda distal e o alvo na liquidez
oposta, buscando 3:1.
"""

from .config import Settings, SymbolSpec
from .models import Candle, OrderBlock, Side, Signal, Zone, ZoneKind
from .news import NewsFilter
from .scanner import ScanResult, Scanner
from .strategy import Analysis, Strategy

__version__ = "1.0.0"

__all__ = [
    "Analysis",
    "Candle",
    "NewsFilter",
    "OrderBlock",
    "ScanResult",
    "Scanner",
    "Settings",
    "Side",
    "Signal",
    "Strategy",
    "SymbolSpec",
    "Zone",
    "ZoneKind",
    "__version__",
]
