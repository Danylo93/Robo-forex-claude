"""Gestão de risco do robô HFT: o que impede a conta de quebrar.

Em alta frequência o erro não custa uma operação, custa cem. Toda entrada passa
por estas travas, e o kill-switch derruba o robô pelo resto do dia quando o
prejuízo diário, o drawdown ou a sequência de perdas estoura o limite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .config import HftSettings
from .features import Features
from .models import AccountState, Intent, Tick, Trade


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    lots: float = 0.0


@dataclass
class RiskManager:
    settings: HftSettings
    account: AccountState
    news_filter: object | None = None  # robo_forex.news.NewsFilter (opcional)
    news_spec: object | None = None  # robo_forex.config.SymbolSpec
    last_trade_at: Optional[datetime] = None
    trade_stamps: list[datetime] = field(default_factory=list)
    current_day: Optional[int] = None

    # ------------------------------------------------------------- pré-trade
    def check(self, tick: Tick, features: Features, intent: Intent) -> RiskDecision:
        cfg = self.settings.risk
        self._roll_day(tick.ts)

        if self.account.halted:
            return RiskDecision(False, f"robô parado: {self.account.halt_reason}")
        if cfg.skip_weekend and tick.ts.weekday() >= 5:
            return RiskDecision(False, "fim de semana")
        if not (cfg.session_start_hour_utc <= tick.ts.hour < cfg.session_end_hour_utc):
            return RiskDecision(
                False,
                f"fora da sessão ({cfg.session_start_hour_utc}h-{cfg.session_end_hour_utc}h UTC)",
            )
        if features.spread_pips > cfg.max_spread_pips:
            return RiskDecision(
                False, f"spread {features.spread_pips:.2f} > {cfg.max_spread_pips:.2f} pips"
            )
        if self.last_trade_at is not None:
            elapsed = (tick.ts - self.last_trade_at).total_seconds()
            if elapsed < self.settings.strategy.cooldown_seconds:
                return RiskDecision(False, f"aguardando {elapsed:.1f}s de intervalo")
        hour_trades = len([t for t in self.trade_stamps if tick.ts - t <= timedelta(hours=1)])
        if hour_trades >= cfg.max_trades_per_hour:
            return RiskDecision(False, f"limite de {cfg.max_trades_per_hour} operações/hora")
        if len(self.trade_stamps) >= cfg.max_trades_per_day:
            return RiskDecision(False, f"limite de {cfg.max_trades_per_day} operações/dia")

        news = self._news_block(tick.ts)
        if news:
            return RiskDecision(False, news)

        edge = self._edge_check(features, intent)
        if edge:
            return RiskDecision(False, edge)

        lots = self.size(intent)
        if lots <= 0:
            return RiskDecision(False, "tamanho de posição calculado em zero")
        return RiskDecision(True, "ok", lots)

    def size(self, intent: Intent) -> float:
        """Lotes: por risco percentual do saldo ou fixo."""
        cfg = self.settings.risk
        instrument = self.settings.instrument
        if intent.lots:
            return instrument.round_lots(intent.lots)
        if cfg.fixed_lots > 0:
            return instrument.round_lots(cfg.fixed_lots)
        risk_amount = self.account.balance * (cfg.risk_percent_per_trade / 100.0)
        risk_per_lot = intent.stop_pips * instrument.pip_value_per_lot
        if risk_per_lot <= 0:
            return 0.0
        return instrument.round_lots(risk_amount / risk_per_lot)

    # ------------------------------------------------------------ pós-trade
    def register(self, trade: Trade) -> None:
        self.account.register(trade)
        self.last_trade_at = trade.closed_at
        self.trade_stamps.append(trade.closed_at)
        self._evaluate_halt()

    def _evaluate_halt(self) -> None:
        cfg = self.settings.risk
        account = self.account
        day_limit = account.day_start_balance * (cfg.max_daily_loss_percent / 100.0)
        dd_limit = account.peak_equity * (cfg.max_drawdown_percent / 100.0)
        if account.day_pnl <= -day_limit:
            self._halt(f"perda diária de {abs(account.day_pnl):.2f} atingiu o limite")
        elif account.drawdown >= dd_limit:
            self._halt(f"drawdown de {account.drawdown:.2f} atingiu o limite")
        elif account.consecutive_losses >= cfg.max_consecutive_losses:
            self._halt(f"{account.consecutive_losses} perdas seguidas")

    def _halt(self, reason: str) -> None:
        self.account.halted = True
        self.account.halt_reason = reason

    def resume(self) -> None:
        self.account.halted = False
        self.account.halt_reason = ""
        self.account.consecutive_losses = 0

    # ------------------------------------------------------------ auxiliares
    def _roll_day(self, now: datetime) -> None:
        day = now.toordinal()
        if self.current_day is None:
            self.current_day = day
            return
        if day != self.current_day:
            self.current_day = day
            self.account.roll_day()
            self.trade_stamps.clear()
            if self.account.halted:
                self.resume()  # novo dia, novo limite diário

    def _news_block(self, now: datetime) -> Optional[str]:
        if not self.settings.risk.news_blackout or not self.news_filter or not self.news_spec:
            return None
        verdict = self.news_filter.check(self.news_spec, now)  # type: ignore[attr-defined]
        if verdict.blocked:
            return verdict.reason
        return None

    def _edge_check(self, features: Features, intent: Intent) -> Optional[str]:
        """A vantagem estimada precisa superar o custo do round-turn."""
        cfg = self.settings.strategy
        if cfg.min_edge_multiple <= 0:
            return None
        cost_pips = self.settings.cost_per_trade_pips(features.spread_pips)
        price = features.mid or 1.0
        cost_bps = cost_pips * self.settings.instrument.pip / price * 10_000.0
        needed = cost_bps * cfg.min_edge_multiple
        if intent.edge_bps < needed:
            return (
                f"vantagem {intent.edge_bps:.2f}bps < custo exigido {needed:.2f}bps "
                f"({cost_pips:.2f} pips de custo)"
            )
        return None
