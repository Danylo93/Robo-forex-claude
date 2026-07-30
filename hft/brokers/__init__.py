"""Adaptadores de corretora do robô HFT."""

from __future__ import annotations

from ..config import HftSettings
from .base import Broker, BrokerError
from .mt5 import Mt5Broker
from .oanda import OandaBroker
from .paper import PaperBroker

__all__ = ["Broker", "BrokerError", "Mt5Broker", "OandaBroker", "PaperBroker", "build_broker"]


def build_broker(settings: HftSettings) -> Broker:
    provider = settings.broker.provider.lower()
    if provider == "paper":
        return PaperBroker(settings)
    if provider == "oanda":
        return OandaBroker(settings)
    if provider == "mt5":
        return Mt5Broker(settings)
    raise ValueError(f"corretora desconhecida: {settings.broker.provider}")
