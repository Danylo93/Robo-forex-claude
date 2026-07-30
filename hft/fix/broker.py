"""Camada de aplicação FIX: market data e ordens, no formato do motor.

Implementa o protocolo `Broker` do robô, então a mesma estratégia que roda em
papel roda contra um ECN por FIX — trocando só a configuração.

Mensagens: `V` (assina market data), `W` (snapshot do livro), `X` (incremental),
`D` (nova ordem), `8` (execution report).
"""

from __future__ import annotations

import itertools
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from ..config import HftSettings
from ..latency import LatencyTracker
from ..models import Fill, Position, Side, Tick
from ..orderbook import OrderBook
from . import message as fix
from .message import FixMessage
from .session import FixConfig, FixSession
from .transport import SocketTransport, Transport

# MDEntryType: 0 = oferta de compra, 1 = oferta de venda
BID_ENTRY = "0"
ASK_ENTRY = "1"


class FixBrokerError(RuntimeError):
    pass


class FixBroker:
    """Corretora via FIX 4.4 com livro L2 mantido localmente."""

    name = "fix"

    def __init__(
        self,
        settings: HftSettings,
        fix_config: FixConfig,
        transport: Optional[Transport] = None,
        depth: int = 10,
        clock: Callable[[], float] = time.monotonic,
        latency: Optional[LatencyTracker] = None,
        on_event: Optional[Callable[[str, dict], None]] = None,
    ):
        self.settings = settings
        self.fix_config = fix_config
        self.symbol = settings.instrument.broker_symbol
        self.transport = transport or SocketTransport(
            fix_config.host, fix_config.port, fix_config.use_ssl
        )
        self.session = FixSession(fix_config, self.transport, clock, on_event)
        self.book = OrderBook(depth=depth)
        self.latency = latency
        self.on_event = on_event
        self._ids = itertools.count(1)
        self._executions: list[FixMessage] = []
        self._balance = settings.risk.account_balance
        self._clock = clock

    # -------------------------------------------------------------- conexão
    def connect(self, timeout: float = 10.0) -> None:
        self.session.connect()
        deadline = self._clock() + timeout
        while self._clock() < deadline and not self.session.state.logged_on:
            self.session.poll()
        if not self.session.state.logged_on:
            raise FixBrokerError("logon FIX não confirmado dentro do tempo limite")
        self.subscribe()

    def shutdown(self) -> None:
        try:
            self.session.logout()
            self.session.poll()
        finally:
            self.session.close()

    def subscribe(self) -> None:
        """MarketDataRequest: snapshot + atualizações incrementais."""
        request = FixMessage(fix.MARKET_DATA_REQUEST)
        request.set(fix.MD_REQ_ID, f"MD{next(self._ids)}")
        request.set(fix.SUBSCRIPTION_REQUEST_TYPE, "1")  # snapshot + updates
        request.set(fix.MARKET_DEPTH, self.book.depth)
        request.set(fix.MD_UPDATE_TYPE, "1")  # incremental
        request.append(fix.NO_MD_ENTRY_TYPES, 2)
        request.append(fix.MD_ENTRY_TYPE, BID_ENTRY)
        request.append(fix.MD_ENTRY_TYPE, ASK_ENTRY)
        request.append(fix.NO_RELATED_SYM, 1)
        request.append(fix.SYMBOL, self.symbol)
        self.session.send(request)

    # --------------------------------------------------------------- preços
    def tick(self) -> Optional[Tick]:
        """Processa o que chegou e devolve o topo do livro."""
        started = self.latency.start("feed_decode") if self.latency else None
        messages = self.session.poll()
        if self.latency and started:
            self.latency.stop("feed_decode", started)
        if messages and self.latency:
            with self.latency.stage("book_update"):
                self._consume(messages)
        else:
            self._consume(messages)
        return self.book.to_tick() if self.book.valid else None

    def _consume(self, messages: list[FixMessage]) -> None:
        for message in messages:
            msg_type = message.msg_type
            if msg_type == fix.MARKET_DATA_SNAPSHOT:
                self._apply_snapshot(message)
            elif msg_type == fix.MARKET_DATA_INCREMENTAL:
                self._apply_incremental(message)
            elif msg_type == fix.EXECUTION_REPORT:
                self._executions.append(message)
            elif msg_type in (fix.MARKET_DATA_REJECT, fix.BUSINESS_MESSAGE_REJECT):
                self._emit("rejeitada", {"texto": message.get(fix.TEXT, ""), "tipo": msg_type})

    def _apply_snapshot(self, message: FixMessage) -> None:
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        for entry in message.groups(fix.NO_MD_ENTRIES, fix.MD_ENTRY_TYPE):
            try:
                price = float(entry[fix.MD_ENTRY_PX])
                size = float(entry.get(fix.MD_ENTRY_SIZE, 0) or 0)
            except (KeyError, ValueError):
                continue
            (bids if entry[fix.MD_ENTRY_TYPE] == BID_ENTRY else asks).append((price, size))
        self.book.snapshot(bids, asks, _entry_time(message))

    def _apply_incremental(self, message: FixMessage) -> None:
        ts = _entry_time(message)
        for entry in message.groups(fix.NO_MD_ENTRIES, fix.MD_UPDATE_ACTION):
            entry_type = entry.get(fix.MD_ENTRY_TYPE)
            if entry_type not in (BID_ENTRY, ASK_ENTRY):
                continue
            try:
                price = float(entry[fix.MD_ENTRY_PX])
            except (KeyError, ValueError):
                continue
            size = float(entry.get(fix.MD_ENTRY_SIZE, 0) or 0)
            action = entry.get(fix.MD_UPDATE_ACTION, "0")
            side = Side.BUY if entry_type == BID_ENTRY else Side.SELL
            self.book.apply(side, price, size, action, ts)

    # --------------------------------------------------------------- ordens
    def open(
        self, side: Side, lots: float, tick: Tick, take_profit: float, stop_loss: float
    ) -> Fill:
        return self._send_order(side, lots, tick)

    def close(self, position: Position, tick: Tick, reason: str) -> Fill:
        return self._send_order(position.side.opposite, position.lots, tick)

    def _send_order(self, side: Side, lots: float, tick: Tick, timeout: float = 5.0) -> Fill:
        order_id = f"O{int(time.time() * 1000)}{next(self._ids)}"
        order = FixMessage(fix.NEW_ORDER_SINGLE)
        order.set(fix.CL_ORD_ID, order_id)
        order.set(fix.SYMBOL, self.symbol)
        order.set(fix.SIDE, "1" if side is Side.BUY else "2")
        order.set(fix.ORDER_QTY, _quantity(lots, self.settings))
        order.set(fix.ORD_TYPE, "1")  # a mercado
        order.set(fix.TIME_IN_FORCE, "3")  # immediate or cancel
        order.set(fix.TRANSACT_TIME, fix.utc_timestamp())
        if self.latency:
            with self.latency.stage("wire"):
                self.session.send(order)
        else:
            self.session.send(order)
        self._emit("ordem", {"id": order_id, "lado": side.value, "lotes": lots})
        return self._await_fill(order_id, side, lots, tick, timeout)

    def _await_fill(
        self, order_id: str, side: Side, lots: float, tick: Tick, timeout: float
    ) -> Fill:
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            self._consume(self.session.poll())
            for report in list(self._executions):
                if report.get(fix.CL_ORD_ID) != order_id:
                    continue
                self._executions.remove(report)
                status = report.get(fix.ORD_STATUS, "")
                exec_type = report.get(fix.EXEC_TYPE, "")
                if status in ("8", "4") or exec_type in ("8", "4"):  # rejeitada/cancelada
                    raise FixBrokerError(
                        f"ordem {order_id} rejeitada: {report.get(fix.TEXT, 'sem motivo')}"
                    )
                if status in ("1", "2") or exec_type in ("1", "2", "F"):  # parcial/total
                    price = report.get_float(fix.LAST_PX) or tick.price_for(side)
                    filled = report.get_float(fix.LAST_QTY)
                    filled_lots = (
                        filled / self.settings.instrument.contract_size if filled else lots
                    )
                    return Fill(
                        ts=datetime.now(timezone.utc),
                        side=side,
                        price=price,
                        lots=filled_lots or lots,
                    )
        raise FixBrokerError(f"sem execution report para {order_id} em {timeout:.0f}s")

    def balance(self) -> float:
        """FIX não padroniza saldo: use o valor configurado ou a API da corretora."""
        return self._balance

    def _emit(self, event: str, data: dict) -> None:
        if self.on_event:
            self.on_event(event, data)


def _quantity(lots: float, settings: HftSettings) -> str:
    units = lots * settings.instrument.contract_size
    return str(int(round(units))) if units >= 1 else f"{units:.4f}"


def _entry_time(message: FixMessage) -> Optional[datetime]:
    raw = message.get(fix.SENDING_TIME, "")
    if not raw:
        return None
    for pattern in ("%Y%m%d-%H:%M:%S.%f", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
