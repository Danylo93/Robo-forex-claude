"""Backtest simples e conservador do setup (validação de expectativa).

Para cada barra o robô é reexecutado apenas com o histórico disponível até ali
(sem look-ahead). O sinal vira ordem pendente na borda da zona; se for acionada,
a saída é no stop ou no alvo — quando a mesma barra toca os dois, assume-se o
stop (pior caso).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import Settings, SymbolSpec
from .feeds.base import resample, timeframe_minutes
from .models import Candle, Side, Signal
from .strategy import Strategy


@dataclass
class Trade:
    symbol: str
    side: Side
    signal_ts: datetime
    entry: float
    stop: float
    target: float
    rr_planned: float
    score: float
    filled_ts: datetime | None = None
    exit_ts: datetime | None = None
    exit_price: float = 0.0
    result_r: float = 0.0
    outcome: str = "pendente"  # alvo | stop | expirada | aberta

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "signal_ts": self.signal_ts.isoformat(),
            "filled_ts": self.filled_ts.isoformat() if self.filled_ts else None,
            "exit_ts": self.exit_ts.isoformat() if self.exit_ts else None,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "rr_planned": round(self.rr_planned, 2),
            "outcome": self.outcome,
            "result_r": round(self.result_r, 2),
            "score": round(self.score, 1),
        }


@dataclass
class BacktestReport:
    symbol: str
    timeframe: str
    trades: list[Trade] = field(default_factory=list)
    signals: int = 0
    bars: int = 0

    @property
    def closed(self) -> list[Trade]:
        return [t for t in self.trades if t.outcome in ("alvo", "stop")]

    @property
    def wins(self) -> list[Trade]:
        return [t for t in self.closed if t.result_r > 0]

    @property
    def win_rate(self) -> float:
        return 100.0 * len(self.wins) / len(self.closed) if self.closed else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.result_r for t in self.closed)

    @property
    def expectancy_r(self) -> float:
        return self.total_r / len(self.closed) if self.closed else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.result_r for t in self.wins)
        losses = -sum(t.result_r for t in self.closed if t.result_r < 0)
        return gains / losses if losses > 0 else float("inf") if gains > 0 else 0.0

    @property
    def max_drawdown_r(self) -> float:
        peak = 0.0
        equity = 0.0
        worst = 0.0
        for trade in self.closed:
            equity += trade.result_r
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return worst

    def summary(self) -> str:
        return (
            f"{self.symbol} {self.timeframe} | barras {self.bars} | sinais {self.signals} | "
            f"operações {len(self.closed)} | acerto {self.win_rate:.0f}% | "
            f"resultado {self.total_r:+.1f}R | expectativa {self.expectancy_r:+.2f}R/op | "
            f"fator de lucro {self.profit_factor:.2f} | pior sequência {self.max_drawdown_r:.1f}R"
        )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "signals": self.signals,
            "trades": [t.to_dict() for t in self.trades],
            "stats": {
                "closed": len(self.closed),
                "win_rate": round(self.win_rate, 1),
                "total_r": round(self.total_r, 2),
                "expectancy_r": round(self.expectancy_r, 3),
                "profit_factor": (
                    round(self.profit_factor, 2) if self.profit_factor != float("inf") else None
                ),
                "max_drawdown_r": round(self.max_drawdown_r, 2),
            },
        }


def run_backtest(
    settings: Settings,
    spec: SymbolSpec,
    candles: list[Candle],
    warmup: int = 200,
    step: int = 1,
    max_hold_bars: int = 200,
) -> BacktestReport:
    """Roda a estratégia barra a barra e simula as ordens resultantes."""
    cfg = settings.strategy
    strategy = Strategy(settings, news=None)  # notícias não se aplicam ao histórico
    report = BacktestReport(symbol=spec.symbol, timeframe=cfg.timeframe, bars=len(candles))
    if len(candles) <= warmup + 10:
        return report

    ltf_minutes = timeframe_minutes(cfg.timeframe)
    htf_all: list[Candle] = []
    if cfg.htf_timeframe:
        htf_minutes = timeframe_minutes(cfg.htf_timeframe)
        if htf_minutes % ltf_minutes == 0:
            htf_all = resample(candles, ltf_minutes, htf_minutes)

    open_trade: Trade | None = None
    pending: tuple[Trade, int] | None = None  # (trade, barra de expiração)

    for i in range(warmup, len(candles)):
        bar = candles[i]

        # 1) gestão do que já está em andamento
        if open_trade:
            _update_open(open_trade, bar, max_hold_bars, i)
            if open_trade.outcome in ("alvo", "stop"):
                open_trade = None
        elif pending:
            trade, expires = pending
            if bar.low <= trade.entry <= bar.high:
                trade.filled_ts = bar.ts
                trade.outcome = "aberta"
                trade._entry_bar = i  # type: ignore[attr-defined]
                open_trade = trade
                pending = None
                _update_open(open_trade, bar, max_hold_bars, i, entry_bar=True)
                if open_trade.outcome in ("alvo", "stop"):
                    open_trade = None
            elif i >= expires:
                trade.outcome = "expirada"
                pending = None

        # 2) nova análise apenas quando não há posição/ordem
        if open_trade or pending or (i - warmup) % step != 0:
            continue
        window = candles[max(0, i - cfg.lookback + 1) : i + 1]
        htf_window = [c for c in htf_all if c.ts <= bar.ts][-cfg.htf_lookback :]
        analysis = strategy.analyze(spec, window, htf_window, now=bar.ts)
        signal = analysis.signal
        if not signal:
            continue
        report.signals += 1
        trade = _trade_from_signal(signal, spec)
        report.trades.append(trade)
        pending = (trade, i + cfg.signal_ttl_bars)

    if open_trade and open_trade.outcome == "aberta":
        open_trade.result_r = 0.0
    return report


def _trade_from_signal(signal: Signal, spec: SymbolSpec) -> Trade:
    return Trade(
        symbol=spec.symbol,
        side=signal.side,
        signal_ts=signal.ts,
        entry=signal.entry,
        stop=signal.stop,
        target=signal.target,
        rr_planned=signal.rr,
        score=signal.score,
    )


def _update_open(
    trade: Trade, bar: Candle, max_hold_bars: int, index: int, entry_bar: bool = False
) -> None:
    """Verifica stop/alvo na barra atual (stop tem prioridade no empate)."""
    risk = abs(trade.entry - trade.stop)
    if risk <= 0:
        trade.outcome = "stop"
        trade.result_r = -1.0
        return
    hit_stop = bar.high >= trade.stop if trade.side is Side.SELL else bar.low <= trade.stop
    hit_target = bar.low <= trade.target if trade.side is Side.SELL else bar.high >= trade.target
    if hit_stop:
        trade.outcome = "stop"
        trade.exit_price = trade.stop
        trade.exit_ts = bar.ts
        trade.result_r = -1.0
        return
    if hit_target:
        trade.outcome = "alvo"
        trade.exit_price = trade.target
        trade.exit_ts = bar.ts
        trade.result_r = abs(trade.target - trade.entry) / risk
        return
    entry_index = getattr(trade, "_entry_bar", index)
    if not entry_bar and index - entry_index >= max_hold_bars:
        trade.outcome = "stop" if _adverse(trade, bar) else "alvo"
        trade.exit_price = bar.close
        trade.exit_ts = bar.ts
        signed = (
            (trade.entry - bar.close) if trade.side is Side.SELL else (bar.close - trade.entry)
        )
        trade.result_r = signed / risk
        trade.outcome = "alvo" if trade.result_r > 0 else "stop"


def _adverse(trade: Trade, bar: Candle) -> bool:
    return bar.close > trade.entry if trade.side is Side.SELL else bar.close < trade.entry
