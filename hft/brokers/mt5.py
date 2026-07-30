"""Adaptador MetaTrader 5 (Windows, pacote `MetaTrader5`).

Instalação: `pip install MetaTrader5` — só funciona no Windows com o terminal
MT5 aberto e "Algo Trading" habilitado. O import é preguiçoso para o resto do
projeto continuar rodando em Linux/macOS.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import HftSettings
from ..models import Fill, Position, Side, Tick
from .base import BrokerError


def _import_mt5() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:  # pragma: no cover - depende do sistema
        raise BrokerError(
            "pacote MetaTrader5 não instalado (pip install MetaTrader5, apenas Windows)"
        ) from exc
    return mt5


class Mt5Broker:
    name = "mt5"

    def __init__(self, settings: HftSettings):
        self.settings = settings
        self.symbol = settings.instrument.broker_symbol
        self._mt5: Any = None

    def connect(self) -> None:
        mt5 = _import_mt5()
        cfg = self.settings.broker
        password = os.environ.get(cfg.mt5_password_env, "")
        kwargs: dict[str, Any] = {}
        if cfg.mt5_login and password and cfg.mt5_server:
            kwargs = {"login": cfg.mt5_login, "password": password, "server": cfg.mt5_server}
        if not mt5.initialize(**kwargs):
            raise BrokerError(f"falha ao conectar no MT5: {mt5.last_error()}")
        if not mt5.symbol_select(self.symbol, True):
            raise BrokerError(f"símbolo {self.symbol} indisponível no MT5")
        self._mt5 = mt5

    def shutdown(self) -> None:
        if self._mt5:
            self._mt5.shutdown()
            self._mt5 = None

    def tick(self) -> Optional[Tick]:
        mt5 = self._require()
        raw = mt5.symbol_info_tick(self.symbol)
        if raw is None or not raw.bid or not raw.ask:
            return None
        return Tick(
            ts=datetime.fromtimestamp(raw.time, tz=timezone.utc),
            bid=float(raw.bid),
            ask=float(raw.ask),
            bid_size=float(getattr(raw, "volume_real", 0) or 0),
            ask_size=float(getattr(raw, "volume_real", 0) or 0),
        )

    def open(
        self, side: Side, lots: float, tick: Tick, take_profit: float, stop_loss: float
    ) -> Fill:
        mt5 = self._require()
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY if side is Side.BUY else mt5.ORDER_TYPE_SELL,
            "price": tick.price_for(side),
            "sl": float(stop_loss) if stop_loss > 0 else 0.0,
            "tp": float(take_profit) if take_profit > 0 else 0.0,
            "deviation": 20,
            "magic": self.settings.broker.magic,
            "comment": "robo-hft",
            "type_filling": mt5.ORDER_FILLING_IOC,
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise BrokerError(f"ordem rejeitada pelo MT5: {getattr(result, 'comment', result)}")
        return Fill(ts=tick.ts, side=side, price=float(result.price), lots=float(result.volume))

    def close(self, position: Position, tick: Tick, reason: str) -> Fill:
        mt5 = self._require()
        side = position.side.opposite
        open_positions = mt5.positions_get(symbol=self.symbol) or []
        ticket = next(
            (p.ticket for p in open_positions if p.magic == self.settings.broker.magic), None
        )
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(position.lots),
            "type": mt5.ORDER_TYPE_BUY if side is Side.BUY else mt5.ORDER_TYPE_SELL,
            "price": tick.price_for(side),
            "deviation": 20,
            "magic": self.settings.broker.magic,
            "comment": f"robo-hft {reason}"[:31],
            "type_filling": mt5.ORDER_FILLING_IOC,
            "type_time": mt5.ORDER_TIME_GTC,
        }
        if ticket:
            request["position"] = ticket
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise BrokerError(f"encerramento rejeitado pelo MT5: {getattr(result, 'comment', result)}")
        return Fill(ts=tick.ts, side=side, price=float(result.price), lots=float(result.volume))

    def balance(self) -> float:
        mt5 = self._require()
        info = mt5.account_info()
        if info is None:
            raise BrokerError("não foi possível ler a conta no MT5")
        return float(info.balance)

    def _require(self) -> Any:
        if self._mt5 is None:
            raise BrokerError("MT5 não conectado: chame connect() primeiro")
        return self._mt5
