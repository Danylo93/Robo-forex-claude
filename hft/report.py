"""Estatísticas do robô HFT, com a decomposição honesta de custos."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean

from .config import HftSettings
from .engine import Engine
from .models import Trade

BAR = "─" * 72


@dataclass
class HftReport:
    symbol: str
    strategy: str
    ticks: int
    trades: list[Trade] = field(default_factory=list)
    blocked: dict[str, int] = field(default_factory=dict)
    starting_balance: float = 0.0
    final_balance: float = 0.0
    halted_reason: str = ""

    # ------------------------------------------------------------ agregados
    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def commission(self) -> float:
        return sum(t.commission for t in self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def win_rate(self) -> float:
        return 100.0 * len([t for t in self.trades if t.won]) / self.count if self.count else 0.0

    @property
    def avg_pips(self) -> float:
        return fmean([t.pips for t in self.trades]) if self.trades else 0.0

    @property
    def avg_net(self) -> float:
        return self.net_pnl / self.count if self.count else 0.0

    @property
    def avg_seconds(self) -> float:
        return fmean([t.duration_seconds for t in self.trades]) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.net_pnl for t in self.trades if t.net_pnl > 0)
        losses = -sum(t.net_pnl for t in self.trades if t.net_pnl < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def max_drawdown(self) -> float:
        equity = 0.0
        peak = 0.0
        worst = 0.0
        for trade in self.trades:
            equity += trade.net_pnl
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return abs(worst)

    @property
    def return_percent(self) -> float:
        if self.starting_balance <= 0:
            return 0.0
        return 100.0 * self.net_pnl / self.starting_balance

    def exits(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for trade in self.trades:
            out[trade.exit_reason] = out.get(trade.exit_reason, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "ticks": self.ticks,
            "trades": self.count,
            "win_rate": round(self.win_rate, 1),
            "gross_pnl": round(self.gross_pnl, 2),
            "commission": round(self.commission, 2),
            "net_pnl": round(self.net_pnl, 2),
            "return_percent": round(self.return_percent, 2),
            "avg_pips": round(self.avg_pips, 3),
            "avg_net_per_trade": round(self.avg_net, 4),
            "avg_seconds": round(self.avg_seconds, 1),
            "profit_factor": (
                round(self.profit_factor, 2) if self.profit_factor != float("inf") else None
            ),
            "max_drawdown": round(self.max_drawdown, 2),
            "exits": self.exits(),
            "blocked": self.blocked,
            "halted_reason": self.halted_reason,
        }


def summarize(engine: Engine, settings: HftSettings) -> HftReport:
    return HftReport(
        symbol=settings.instrument.symbol,
        strategy=settings.strategy.name,
        ticks=engine.stats.ticks,
        trades=list(engine.account.trades),
        blocked=dict(engine.stats.blocked),
        starting_balance=engine.account.starting_balance,
        final_balance=engine.account.balance,
        halted_reason=engine.account.halt_reason,
    )


def render(report: HftReport, settings: HftSettings, spread_pips: float | None = None) -> str:
    """Relatório de console com a conta de padaria explícita."""
    instrument = settings.instrument
    spread = spread_pips if spread_pips is not None else settings.risk.max_spread_pips
    cost_pips = settings.cost_per_trade_pips(spread)
    lines = [
        BAR,
        f"ROBÔ HFT — {report.symbol} | estratégia {report.strategy} | {report.ticks} ticks",
        BAR,
        f"operações        {report.count}",
        f"acerto           {report.win_rate:.1f}%",
        f"média por trade  {report.avg_pips:+.3f} pips brutos | "
        f"{report.avg_net:+.4f} líquido | {report.avg_seconds:.1f}s",
        "",
        "Decomposição do resultado:",
        f"  bruto          {report.gross_pnl:+.2f}",
        f"  comissões      {-report.commission:+.2f}",
        f"  LÍQUIDO        {report.net_pnl:+.2f}  ({report.return_percent:+.2f}% do saldo)",
        "",
        f"fator de lucro   {report.profit_factor:.2f}",
        f"pior drawdown    {report.max_drawdown:.2f}",
        f"saldo            {report.starting_balance:.2f} -> {report.final_balance:.2f}",
        "",
        "Custo por operação (round-turn):",
        f"  {cost_pips:.2f} pips = spread {spread:.2f} + comissão "
        f"{2 * settings.costs.commission_per_lot_per_side / max(instrument.pip_value_per_lot, 1e-9):.2f}"
        f" + derrapagem {2 * settings.costs.extra_slippage_pips:.2f}",
        f"  a estratégia precisa capturar mais que isso por operação para dar lucro",
    ]
    if report.exits():
        lines += ["", "Saídas:"]
        lines += [f"  {reason}: {count}" for reason, count in sorted(report.exits().items())]
    if report.blocked:
        lines += ["", "Entradas bloqueadas pelo risco:"]
        lines += [
            f"  {reason}: {count}"
            for reason, count in sorted(report.blocked.items(), key=lambda kv: -kv[1])[:8]
        ]
    if report.halted_reason:
        lines += ["", f"KILL-SWITCH ACIONADO: {report.halted_reason}"]
    lines += [BAR, verdict(report, cost_pips)]
    lines.append(BAR)
    return "\n".join(lines)


def verdict(report: HftReport, cost_pips: float) -> str:
    """Leitura direta do resultado, sem maquiagem."""
    if report.count < 30:
        return (
            f"AMOSTRA PEQUENA ({report.count} operações): não dá para concluir nada. "
            "Rode mais dados antes de qualquer decisão."
        )
    if report.net_pnl <= 0:
        return (
            f"NÃO PAGA OS CUSTOS: {report.avg_pips:+.3f} pips brutos por operação contra "
            f"{cost_pips:.2f} pips de custo. Não opere isso com dinheiro real."
        )
    if report.profit_factor < 1.15 or report.avg_net <= 0:
        return (
            "MARGEM FINA: o lucro existe mas não cobre imprevisto (alargamento de spread, "
            "requote, latência pior). Trate como não validado."
        )
    return (
        f"POSITIVO NESTA AMOSTRA: {report.avg_pips:+.3f} pips brutos por operação contra "
        f"{cost_pips:.2f} de custo, fator de lucro {report.profit_factor:.2f}. "
        "Confirme em dados de tick reais e depois em conta demo antes de ir para a real."
    )
