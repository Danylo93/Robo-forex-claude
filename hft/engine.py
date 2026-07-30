"""Motor do robô HFT: tick -> features -> estratégia -> risco -> corretora.

O mesmo motor roda no backtest, no modo papel e ao vivo. A diferença está no
adaptador de corretora e na origem dos ticks.

Latência é modelada de verdade: a decisão tomada no tick T só é executada no
primeiro tick com carimbo >= T + `costs.latency_ms`. Alvo e stop, por ficarem
registrados na corretora, não sofrem esse atraso.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

from .config import HftSettings
from .features import FeatureWindow, Features
from .models import AccountState, Fill, Intent, Position, Side, Tick, Trade
from .risk import RiskManager
from .strategies import Strategy, build_strategy

EventHook = Callable[[str, dict], None]


@dataclass
class PendingOrder:
    """Decisão aguardando a latência passar."""

    intent: Intent
    lots: float
    decided_at: datetime
    execute_at: datetime


@dataclass
class EngineStats:
    ticks: int = 0
    entries: int = 0
    blocked: dict[str, int] = field(default_factory=dict)

    def block(self, reason: str) -> None:
        # agrupa por motivo, ignorando os números (senão cada valor vira uma linha)
        head = reason.split(":")[0].split("(")[0]
        words = []
        for word in head.split():
            if any(ch.isdigit() for ch in word):
                break
            words.append(word)
        key = " ".join(words) or head.strip()
        self.blocked[key] = self.blocked.get(key, 0) + 1


class Engine:
    def __init__(
        self,
        settings: HftSettings,
        broker,
        strategy: Optional[Strategy] = None,
        news_filter=None,
        news_spec=None,
        account: Optional[AccountState] = None,
        on_event: Optional[EventHook] = None,
    ):
        self.settings = settings
        self.broker = broker
        self.strategy = strategy or build_strategy(settings)
        self.account = account or AccountState(balance=settings.risk.account_balance)
        self.risk = RiskManager(settings, self.account, news_filter, news_spec)
        self.window = FeatureWindow(
            settings.strategy.window_ticks,
            settings.strategy.warmup_ticks,
            settings.instrument.pip,
        )
        self.position: Optional[Position] = None
        self.pending: Optional[PendingOrder] = None
        self.stats = EngineStats()
        self.on_event = on_event

    # ------------------------------------------------------------------ loop
    def on_tick(self, tick: Tick) -> Optional[Trade]:
        """Processa um tick. Devolve a operação quando ela é encerrada."""
        self.stats.ticks += 1
        if hasattr(self.broker, "feed"):
            self.broker.feed(tick)
        features = self.window.update(tick)

        if self.position is not None:
            return self._manage(tick, features)
        if self.pending is not None:
            self._try_execute(tick)
            return None
        self._maybe_enter(tick, features)
        return None

    def run(self, ticks: Iterable[Tick]) -> list[Trade]:
        """Consome um fluxo finito de ticks (backtest/replay)."""
        for tick in ticks:
            self.on_tick(tick)
        if self.position is not None and self.window.last is not None:
            self._close(self.window.last, "fim dos dados")
        return self.account.trades

    def run_live(
        self, max_seconds: Optional[float] = None, sleeper: Callable[[float], None] = time.sleep
    ) -> list[Trade]:
        """Consulta a corretora em intervalos fixos até o limite de tempo."""
        self.broker.connect()
        started = time.monotonic()
        interval = max(self.settings.broker.poll_ms, 1.0) / 1000.0
        while max_seconds is None or (time.monotonic() - started) < max_seconds:
            try:
                tick = self.broker.tick()
            except Exception as exc:
                self._emit("erro", {"mensagem": str(exc)})
                sleeper(interval * 5)
                continue
            if tick is not None:
                self.on_tick(tick)
            if self.account.halted and self.position is None:
                self._emit("parado", {"motivo": self.account.halt_reason})
                break
            sleeper(interval)
        if self.position is not None:
            last = self.window.last
            if last is not None:
                self._close(last, "encerramento da sessão")
        self.broker.shutdown()
        return self.account.trades

    # -------------------------------------------------------------- entrada
    def _maybe_enter(self, tick: Tick, features: Features) -> None:
        intent = self.strategy.entry(tick, features)
        if intent is None:
            return
        decision = self.risk.check(tick, features, intent)
        if not decision.allowed:
            self.stats.block(decision.reason)
            self._emit("bloqueado", {"motivo": decision.reason, "lado": intent.side.value})
            return
        latency = timedelta(milliseconds=self.settings.costs.latency_ms)
        self.pending = PendingOrder(
            intent=intent, lots=decision.lots, decided_at=tick.ts, execute_at=tick.ts + latency
        )
        if latency.total_seconds() <= 0:
            self._try_execute(tick)

    def _try_execute(self, tick: Tick) -> None:
        pending = self.pending
        if pending is None or tick.ts < pending.execute_at:
            return
        self.pending = None
        intent = pending.intent
        pip = self.settings.instrument.pip
        try:
            fill = self.broker.open(intent.side, pending.lots, tick, 0.0, 0.0)
        except Exception as exc:
            self._emit("erro", {"mensagem": f"falha ao abrir: {exc}"})
            return
        sign = intent.side.sign
        take_profit = fill.price + intent.take_profit_pips * pip * sign
        stop_loss = fill.price - intent.stop_pips * pip * sign
        self.position = Position(
            side=intent.side,
            entry=fill.price,
            lots=fill.lots,
            opened_at=fill.ts,
            take_profit=take_profit,
            stop_loss=stop_loss,
            max_hold_seconds=intent.max_hold_seconds,
            reason=intent.reason,
            commission_paid=fill.commission,
            entry_slippage_pips=fill.slippage_pips,
        )
        self.stats.entries += 1
        self._emit(
            "entrada",
            {
                "lado": intent.side.value,
                "preco": fill.price,
                "lotes": fill.lots,
                "motivo": intent.reason,
                "atraso_ms": (tick.ts - pending.decided_at).total_seconds() * 1000.0,
            },
        )

    # ---------------------------------------------------------------- saída
    def _manage(self, tick: Tick, features: Features) -> Optional[Trade]:
        position = self.position
        assert position is not None
        exit_price = tick.bid if position.side is Side.BUY else tick.ask

        if position.side is Side.BUY:
            if exit_price <= position.stop_loss:
                return self._close(tick, "stop")
            if exit_price >= position.take_profit:
                return self._close(tick, "alvo")
        else:
            if exit_price >= position.stop_loss:
                return self._close(tick, "stop")
            if exit_price <= position.take_profit:
                return self._close(tick, "alvo")

        if position.age_seconds(tick.ts) >= position.max_hold_seconds:
            return self._close(tick, "tempo máximo")
        reason = self.strategy.exit(tick, features, position)
        if reason:
            return self._close(tick, reason)
        return None

    def _close(self, tick: Tick, reason: str) -> Optional[Trade]:
        position = self.position
        if position is None:
            return None
        try:
            fill: Fill = self.broker.close(position, tick, reason)
        except Exception as exc:
            self._emit("erro", {"mensagem": f"falha ao encerrar: {exc}"})
            return None
        self.position = None
        instrument = self.settings.instrument
        pips = (fill.price - position.entry) / instrument.pip * position.side.sign
        gross = pips * instrument.pip_value_per_lot * position.lots
        commission = position.commission_paid + fill.commission
        trade = Trade(
            symbol=instrument.symbol,
            side=position.side,
            lots=position.lots,
            opened_at=position.opened_at,
            closed_at=fill.ts,
            entry=position.entry,
            exit=fill.price,
            gross_pnl=gross,
            commission=commission,
            net_pnl=gross - commission,
            pips=pips,
            exit_reason=reason,
            reason=position.reason,
        )
        self.risk.register(trade)
        if hasattr(self.broker, "apply_pnl"):
            self.broker.apply_pnl(trade.net_pnl)
        self._emit(
            "saida",
            {
                "motivo": reason,
                "pips": round(pips, 2),
                "liquido": round(trade.net_pnl, 4),
                "saldo": round(self.account.balance, 2),
            },
        )
        if self.account.halted:
            self._emit("parado", {"motivo": self.account.halt_reason})
        return trade

    def _emit(self, event: str, data: dict) -> None:
        if self.on_event:
            self.on_event(event, data)
