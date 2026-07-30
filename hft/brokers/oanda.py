"""Adaptador OANDA v20 (REST) — conta demo (practice) ou real (trade).

O token nunca fica no arquivo de configuração: vem de variável de ambiente
(`broker.token_env`, padrão `OANDA_TOKEN`).

Endpoints usados:
  GET  /v3/accounts/{id}/pricing?instruments=EUR_USD
  POST /v3/accounts/{id}/orders                     (ordem a mercado com TP/SL)
  PUT  /v3/accounts/{id}/positions/{inst}/close     (encerra a posição)
  GET  /v3/accounts/{id}/summary                    (saldo)
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import HftSettings
from ..models import Fill, Position, Side, Tick
from .base import BrokerError

PRACTICE_HOST = "https://api-fxpractice.oanda.com"
LIVE_HOST = "https://api-fxtrade.oanda.com"


def to_instrument(symbol: str) -> str:
    """EURUSD -> EUR_USD (formato da OANDA).

    Só divide pares de moedas (6 letras). Índices e commodities (SPX500, XAUUSD
    já com underscore, US30_USD) passam intactos — para esses, informe o nome
    exato da OANDA em `instrument.broker_symbol`.
    """
    if "_" in symbol or not symbol.isalpha() or len(symbol) != 6:
        return symbol
    return f"{symbol[:3]}_{symbol[3:]}"


def market_order_payload(
    instrument: str, units: int, take_profit: float, stop_loss: float, digits: int
) -> dict[str, Any]:
    """Monta o corpo da ordem a mercado com TP e SL anexados."""
    order: dict[str, Any] = {
        "type": "MARKET",
        "instrument": instrument,
        "units": str(units),
        "timeInForce": "FOK",
        "positionFill": "DEFAULT",
    }
    if take_profit > 0:
        order["takeProfitOnFill"] = {"price": f"{take_profit:.{digits}f}", "timeInForce": "GTC"}
    if stop_loss > 0:
        order["stopLossOnFill"] = {"price": f"{stop_loss:.{digits}f}", "timeInForce": "GTC"}
    return {"order": order}


def parse_price(payload: dict[str, Any]) -> Optional[Tick]:
    """Converte a resposta de /pricing no tick do topo do livro."""
    prices = payload.get("prices") or []
    if not prices:
        return None
    price = prices[0]
    bids, asks = price.get("bids") or [], price.get("asks") or []
    if not bids or not asks:
        return None
    ts = price.get("time", "")
    try:
        stamp = datetime.fromisoformat(ts.replace("Z", "+00:00")[:26] + "+00:00")
    except ValueError:
        stamp = datetime.now(timezone.utc)
    return Tick(
        ts=stamp.astimezone(timezone.utc),
        bid=float(bids[0]["price"]),
        ask=float(asks[0]["price"]),
        bid_size=float(bids[0].get("liquidity", 0) or 0),
        ask_size=float(asks[0].get("liquidity", 0) or 0),
    )


class OandaBroker:
    name = "oanda"

    def __init__(self, settings: HftSettings, timeout: float = 10.0):
        self.settings = settings
        self.timeout = timeout
        cfg = settings.broker
        self.account_id = cfg.account_id
        self.instrument = to_instrument(settings.instrument.broker_symbol)
        self.host = cfg.oanda_host or (LIVE_HOST if cfg.mode == "live" else PRACTICE_HOST)
        self._token = os.environ.get(cfg.token_env, "")
        self._last: Optional[Tick] = None

    # -------------------------------------------------------------- conexão
    def connect(self) -> None:
        if not self._token:
            raise BrokerError(
                f"token ausente: exporte {self.settings.broker.token_env} com a chave da OANDA"
            )
        if not self.account_id:
            raise BrokerError("broker.account_id não configurado")
        self._request("GET", f"/v3/accounts/{self.account_id}/summary")

    def shutdown(self) -> None:
        return None

    # --------------------------------------------------------------- preços
    def tick(self) -> Optional[Tick]:
        query = urllib.parse.urlencode({"instruments": self.instrument})
        payload = self._request("GET", f"/v3/accounts/{self.account_id}/pricing?{query}")
        tick = parse_price(payload)
        if tick:
            self._last = tick
        return tick

    # --------------------------------------------------------------- ordens
    def open(
        self, side: Side, lots: float, tick: Tick, take_profit: float, stop_loss: float
    ) -> Fill:
        units = int(round(lots * self.settings.instrument.contract_size)) * side.sign
        body = market_order_payload(
            self.instrument, units, take_profit, stop_loss, self.settings.instrument.digits
        )
        response = self._request("POST", f"/v3/accounts/{self.account_id}/orders", body)
        transaction = response.get("orderFillTransaction") or {}
        if not transaction:
            reason = response.get("orderCancelTransaction", {}).get("reason", "sem preenchimento")
            raise BrokerError(f"ordem não executada: {reason}")
        price = float(transaction.get("price", tick.price_for(side)))
        commission = abs(float(transaction.get("commission", 0.0) or 0.0))
        return Fill(ts=tick.ts, side=side, price=price, lots=lots, commission=commission)

    def close(self, position: Position, tick: Tick, reason: str) -> Fill:
        field = "longUnits" if position.side is Side.BUY else "shortUnits"
        response = self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/positions/{self.instrument}/close",
            {field: "ALL"},
        )
        transaction = (
            response.get("longOrderFillTransaction")
            or response.get("shortOrderFillTransaction")
            or {}
        )
        price = float(transaction.get("price", tick.price_for(position.side.opposite)))
        commission = abs(float(transaction.get("commission", 0.0) or 0.0))
        return Fill(
            ts=tick.ts,
            side=position.side.opposite,
            price=price,
            lots=position.lots,
            commission=commission,
        )

    def balance(self) -> float:
        payload = self._request("GET", f"/v3/accounts/{self.account_id}/summary")
        return float((payload.get("account") or {}).get("balance", 0.0))

    # ------------------------------------------------------------ transporte
    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict[str, Any]:
        url = self.host + path
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=ssl.create_default_context()
            ) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise BrokerError(f"OANDA {exc.code} em {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BrokerError(f"falha de comunicação com a OANDA: {exc}") from exc
