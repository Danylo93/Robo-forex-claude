"""Market making com controle de inventário (Avellaneda–Stoikov) e simulador.

Esta é a estratégia que justifica infraestrutura institucional: o lucro vem de
capturar o spread e o rebate de maker, não de acertar direção. Ela só fecha a
conta com prioridade de fila e custo de transação próximo de zero — por isso
depende de colocation e de acordo de maker, não de código melhor.

O modelo, em uma linha: cota-se em torno de um **preço de reserva** deslocado
pelo inventário, com meia-distância proporcional à volatilidade.

    reserva      = meio − (estoque/estoque_máx) · max_skew_pips · τ
    meia-distância = base_half_spread_pips + vol_widen · σ_pips · τ

Quando o estoque cresce, a reserva se afasta do meio e as cotações "empurram" a
posição de volta a zero — é o mecanismo que impede o market maker de acumular
direcional até quebrar.

Isto é a estrutura do Avellaneda–Stoikov reparametrizada em pips. A fórmula
literal do paper está em `avellaneda_stoikov()`, com a explicação de por que γ e
κ nas unidades originais são impossíveis de calibrar num par de câmbio.

O simulador aqui embutido é deliberadamente pessimista: assume que a fila anda
contra você e que todo preenchimento acontece quando o preço atravessa a
cotação, que é exatamente o caso em que o mercado sabe algo que você não sabe
(seleção adversa). É assim que se descobre se a estratégia vive sem rebate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import pstdev
from typing import Callable, Iterable, Optional

from .config import HftSettings
from .latency import LatencyTracker
from .models import Side, Tick


def avellaneda_stoikov(
    mid: float, sigma: float, inventory: float, gamma: float, kappa: float, tau: float
) -> tuple[float, float]:
    """Fórmula original do paper — cuidado com as unidades.

        r = s − q·γ·σ²·τ
        δ = γ·σ²·τ + (2/γ)·ln(1 + γ/κ)

    `sigma` é a volatilidade **no horizonte restante**, em unidades de preço, e
    `kappa` é a intensidade de chegada de ordens **por unidade de preço**. Num
    par de câmbio, κ na casa de 1,0 produz spread de milhares de pips: para o
    termo logarítmico fazer sentido, κ precisa estar na ordem de 1/pip (10.000).
    É por isso que a classe abaixo usa uma reparametrização em pips — mantém a
    estrutura (preço de reserva + skew por inventário + spread proporcional à
    volatilidade) com números que dá para calibrar.

    Devolve (preço_de_reserva, meia_distância).
    """
    variance = sigma * sigma * max(tau, 0.0)
    reservation = mid - inventory * gamma * variance
    spread = variance * gamma + (2.0 / gamma) * math.log(1.0 + gamma / kappa)
    return reservation, spread / 2.0


@dataclass
class MarketMakerParams:
    base_half_spread_pips: float = 0.4  # meia-distância mínima em regime calmo
    vol_widen: float = 0.8  # quanto a volatilidade alarga a cotação
    max_skew_pips: float = 1.2  # deslocamento do preço de reserva no estoque cheio
    horizon_seconds: float = 600.0  # janela de "fim do jogo" do modelo
    volatility_window: int = 200  # ticks usados na estimativa de σ
    quote_size_lots: float = 0.1
    max_inventory_lots: float = 0.5
    min_half_spread_pips: float = 0.2  # nunca cotar mais apertado que isso
    max_half_spread_pips: float = 5.0
    requote_threshold_pips: float = 0.1  # só recota se a cotação mudou o bastante
    adverse_imbalance: float = 0.7  # deixa de cotar o lado contra o fluxo
    rebate_per_lot: float = 0.0  # rebate de maker por lote executado
    fee_per_lot: float = 0.0  # taxa de maker por lote executado
    flatten_at_end: bool = True
    queue_fill_ratio: float = 0.0  # 0 = só executa quando o preço atravessa (pessimista)


@dataclass
class Quote:
    bid: float
    ask: float
    size: float
    reservation: float
    half_spread: float
    skew_pips: float = 0.0
    quote_bid: bool = True
    quote_ask: bool = True

    def price(self, side: Side) -> Optional[float]:
        if side is Side.BUY:
            return self.bid if self.quote_bid else None
        return self.ask if self.quote_ask else None


@dataclass
class MakerFill:
    ts: datetime
    side: Side
    price: float
    lots: float
    mid_at_fill: float
    rebate: float = 0.0
    fee: float = 0.0

    @property
    def adverse_bps(self) -> float:
        """Quanto o meio já estava contra a execução (seleção adversa)."""
        if not self.price:
            return 0.0
        edge = (self.mid_at_fill - self.price) * self.side.sign
        return edge / self.price * 10_000.0


class MarketMaker:
    """Gera as duas pontas a partir de meio, volatilidade e inventário."""

    name = "market_maker"

    def __init__(self, settings: HftSettings, params: Optional[MarketMakerParams] = None):
        self.settings = settings
        self.params = params or MarketMakerParams()

    def quote(
        self,
        mid: float,
        sigma: float,
        inventory_lots: float,
        remaining_fraction: float = 1.0,
        imbalance: float = 0.0,
    ) -> Optional[Quote]:
        """Cotação de dois lados; None quando não há preço válido.

        `sigma` chega em unidades de preço (desvio padrão dos incrementos do
        meio na janela) e vira pips internamente.
        """
        if mid <= 0:
            return None
        p = self.params
        pip = self.settings.instrument.pip
        tau = max(0.0, min(1.0, remaining_fraction))
        sigma_pips = sigma / pip if pip else 0.0

        # inventário normalizado: 1.0 = limite máximo de estoque.
        # O preço de reserva se afasta do meio na direção que desova o estoque:
        # comprado demais -> cota mais baixo -> tende a vender.
        q = inventory_lots / p.max_inventory_lots if p.max_inventory_lots else 0.0
        skew_pips = -q * p.max_skew_pips * max(tau, 0.25)
        reservation = mid + skew_pips * pip

        half_pips = p.base_half_spread_pips + p.vol_widen * sigma_pips * tau
        half = max(
            p.min_half_spread_pips * pip, min(p.max_half_spread_pips * pip, half_pips * pip)
        )

        quote = Quote(
            bid=reservation - half,
            ask=reservation + half,
            size=p.quote_size_lots,
            reservation=reservation,
            half_spread=half,
            skew_pips=skew_pips,
        )

        # estoque no limite: só cota o lado que reduz posição
        if inventory_lots >= p.max_inventory_lots:
            quote.quote_bid = False
        if inventory_lots <= -p.max_inventory_lots:
            quote.quote_ask = False
        # fluxo forte de um lado: recua a ponta que seria atropelada
        if imbalance >= p.adverse_imbalance:
            quote.quote_ask = False
        elif imbalance <= -p.adverse_imbalance:
            quote.quote_bid = False
        return quote


@dataclass
class MakerReport:
    symbol: str
    ticks: int = 0
    quotes: int = 0
    requotes: int = 0
    fills: list[MakerFill] = field(default_factory=list)
    realized_pnl: float = 0.0
    inventory_lots: float = 0.0
    final_mid: float = 0.0
    rebates: float = 0.0
    fees: float = 0.0
    max_inventory: float = 0.0
    halted_reason: str = ""
    latency: Optional[LatencyTracker] = None

    @property
    def fill_count(self) -> int:
        return len(self.fills)

    @property
    def unrealized_pnl(self) -> float:
        return 0.0  # o simulador zera o estoque no fim quando `flatten_at_end`

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl + self.rebates - self.fees

    @property
    def avg_adverse_bps(self) -> float:
        if not self.fills:
            return 0.0
        return sum(f.adverse_bps for f in self.fills) / len(self.fills)

    @property
    def fill_ratio(self) -> float:
        return 100.0 * self.fill_count / self.quotes if self.quotes else 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "ticks": self.ticks,
            "quotes": self.quotes,
            "requotes": self.requotes,
            "fills": self.fill_count,
            "fill_ratio": round(self.fill_ratio, 2),
            "realized_pnl": round(self.realized_pnl, 4),
            "rebates": round(self.rebates, 4),
            "fees": round(self.fees, 4),
            "net_pnl": round(self.net_pnl, 4),
            "avg_adverse_bps": round(self.avg_adverse_bps, 3),
            "max_inventory_lots": round(self.max_inventory, 3),
            "final_inventory_lots": round(self.inventory_lots, 3),
            "halted_reason": self.halted_reason,
            "latency": self.latency.to_dict() if self.latency else {},
        }


class QuotingEngine:
    """Roda o market maker sobre um fluxo de ticks, com fila pessimista.

    Regra de execução: a compra em `bid` só é preenchida quando o mercado
    **atravessa** o preço (o melhor ask do mercado desce até ele). É o pior caso
    realista — o preenchimento acontece justamente quando o mercado está indo
    contra a posição.
    """

    def __init__(
        self,
        settings: HftSettings,
        maker: Optional[MarketMaker] = None,
        params: Optional[MarketMakerParams] = None,
        track_latency: bool = True,
        max_loss: Optional[float] = None,
    ):
        self.settings = settings
        self.params = params or (maker.params if maker else MarketMakerParams())
        self.maker = maker or MarketMaker(settings, self.params)
        self.latency = LatencyTracker() if track_latency else None
        self.max_loss = max_loss

        self.inventory = 0.0  # em lotes
        self.cash = 0.0  # caixa em moeda da conta (contabilidade do estoque)
        self.quote: Optional[Quote] = None
        self.report = MakerReport(symbol=settings.instrument.symbol, latency=self.latency)
        self._mids: list[float] = []
        self._start: Optional[datetime] = None

    # ---------------------------------------------------------------- laço
    def run(self, ticks: Iterable[Tick]) -> MakerReport:
        for tick in ticks:
            self.on_tick(tick)
        last = self.report.final_mid
        if self.params.flatten_at_end and self.inventory and last:
            self._flatten(last)
        return self.report

    def on_tick(self, tick: Tick) -> None:
        if self._start is None:
            self._start = tick.ts
        self.report.ticks += 1
        self.report.final_mid = tick.mid

        if self.latency:
            started = self.latency.start("tick_to_trade")
        self._mids.append(tick.mid)
        if len(self._mids) > self.params.volatility_window:
            del self._mids[0]

        # 1) executa o que a cotação anterior teria pego neste tick
        self._match(tick)

        if self.report.halted_reason:
            self.quote = None
            return

        # 2) recalcula a cotação
        if self.latency:
            with self.latency.stage("strategy"):
                new_quote = self._compute(tick)
        else:
            new_quote = self._compute(tick)

        if new_quote is not None and self._should_requote(new_quote):
            if self.quote is not None:
                self.report.requotes += 1
            self.quote = new_quote
            self.report.quotes += 1
        if self.latency:
            self.latency.stop("tick_to_trade", started)

    # ------------------------------------------------------------ cotação
    def _compute(self, tick: Tick) -> Optional[Quote]:
        sigma = self._sigma()
        elapsed = (tick.ts - self._start).total_seconds() if self._start else 0.0
        remaining = 1.0 - min(1.0, elapsed / max(self.params.horizon_seconds, 1e-9))
        remaining = max(remaining, 0.05)  # nunca zera: senão o spread colapsa no fim
        return self.maker.quote(tick.mid, sigma, self.inventory, remaining, tick.imbalance)

    def _sigma(self) -> float:
        """Volatilidade por tick, em unidades de preço.

        Usa o desvio dos **incrementos** do meio, não dos níveis: desvio de
        nível mistura tendência com volatilidade e inflaria a cotação.
        """
        if len(self._mids) < 10:
            return self.settings.instrument.pip
        diffs = [b - a for a, b in zip(self._mids, self._mids[1:])]
        return pstdev(diffs) or self.settings.instrument.pip

    def _should_requote(self, new: Quote) -> bool:
        if self.quote is None:
            return True
        threshold = self.params.requote_threshold_pips * self.settings.instrument.pip
        return (
            abs(new.bid - self.quote.bid) >= threshold
            or abs(new.ask - self.quote.ask) >= threshold
            or new.quote_bid != self.quote.quote_bid
            or new.quote_ask != self.quote.quote_ask
        )

    # ------------------------------------------------------------ execução
    def _match(self, tick: Tick) -> None:
        quote = self.quote
        if quote is None:
            return
        # compra: o mercado desceu até nossa oferta (ask do mercado <= nosso bid)
        if quote.quote_bid and tick.ask <= quote.bid:
            self._fill(tick, Side.BUY, quote.bid, quote.size)
        # venda: o mercado subiu até nossa oferta (bid do mercado >= nosso ask)
        elif quote.quote_ask and tick.bid >= quote.ask:
            self._fill(tick, Side.SELL, quote.ask, quote.size)

    def _fill(self, tick: Tick, side: Side, price: float, lots: float) -> None:
        params = self.params
        pip_value = self.settings.instrument.pip_value_per_lot
        pip = self.settings.instrument.pip
        rebate = params.rebate_per_lot * lots
        fee = params.fee_per_lot * lots

        # contabilidade em pips: caixa recebe o preço vendido, paga o comprado
        self.cash += (price / pip) * pip_value * lots * (-side.sign)
        self.inventory += lots * side.sign
        self.report.max_inventory = max(self.report.max_inventory, abs(self.inventory))
        self.report.rebates += rebate
        self.report.fees += fee
        self.report.fills.append(
            MakerFill(
                ts=tick.ts, side=side, price=price, lots=lots, mid_at_fill=tick.mid,
                rebate=rebate, fee=fee,
            )
        )
        self._mark(tick.mid)
        self.quote = None  # cotação consumida: será recolocada no próximo tick
        if self.max_loss is not None and self.report.net_pnl <= -abs(self.max_loss):
            self.report.halted_reason = (
                f"perda de {abs(self.report.net_pnl):.2f} atingiu o limite de {self.max_loss:.2f}"
            )

    def _mark(self, mid: float) -> None:
        """Marca a posição a mercado: caixa + estoque avaliado no meio."""
        pip_value = self.settings.instrument.pip_value_per_lot
        pip = self.settings.instrument.pip
        inventory_value = (mid / pip) * pip_value * self.inventory
        self.report.realized_pnl = self.cash + inventory_value
        self.report.inventory_lots = self.inventory

    def _flatten(self, mid: float) -> None:
        """Zera o estoque no meio do mercado ao fim da sessão."""
        pip_value = self.settings.instrument.pip_value_per_lot
        pip = self.settings.instrument.pip
        self.cash += (mid / pip) * pip_value * self.inventory
        self.inventory = 0.0
        self.report.realized_pnl = self.cash
        self.report.inventory_lots = 0.0


def render_maker_report(report: MakerReport, settings: HftSettings) -> str:
    """Relatório com a decomposição que revela se o market making se paga."""
    bar = "─" * 72
    spread_capture = report.realized_pnl
    lines = [
        bar,
        f"MARKET MAKING — {report.symbol} | {report.ticks} ticks",
        bar,
        f"cotações enviadas   {report.quotes} ({report.requotes} recotações)",
        f"execuções           {report.fill_count}  (taxa de execução {report.fill_ratio:.1f}%)",
        f"estoque máximo      {report.max_inventory:.2f} lotes | final "
        f"{report.inventory_lots:.2f}",
        "",
        "Decomposição do resultado:",
        f"  spread capturado  {spread_capture:+.2f}",
        f"  rebates           {report.rebates:+.2f}",
        f"  taxas             {-report.fees:+.2f}",
        f"  LÍQUIDO           {report.net_pnl:+.2f}",
        "",
        f"seleção adversa     {report.avg_adverse_bps:+.3f} bps por execução",
    ]
    if report.halted_reason:
        lines += ["", f"PARADO: {report.halted_reason}"]
    if report.latency and report.latency.histograms["tick_to_trade"].count:
        lines += ["", "Latência do caminho crítico:", report.latency.report()]
    lines += [bar, _maker_verdict(report), bar]
    return "\n".join(lines)


def _maker_verdict(report: MakerReport) -> str:
    if report.fill_count < 30:
        return (
            f"AMOSTRA PEQUENA ({report.fill_count} execuções): a cotação quase não foi "
            "atingida. Aperte o spread ou use dados com mais movimento."
        )
    if report.avg_adverse_bps < 0 and report.net_pnl <= 0:
        return (
            f"SELEÇÃO ADVERSA DOMINA: {report.avg_adverse_bps:+.3f} bps por execução. "
            "Sem prioridade de fila e sem rebate, market making perde — é exatamente "
            "para isso que serve a infraestrutura institucional."
        )
    if report.net_pnl <= 0:
        return "NEGATIVO nesta amostra: o spread capturado não cobriu o estoque carregado."
    if report.rebates and report.net_pnl - report.rebates <= 0:
        return (
            "SÓ VIVE DE REBATE: sem o acordo de maker o resultado é negativo. "
            "Confirme que a sua conta tem esse rebate por escrito antes de contar com ele."
        )
    return (
        f"POSITIVO nesta amostra: {report.net_pnl:+.2f} com {report.fill_count} execuções e "
        f"seleção adversa de {report.avg_adverse_bps:+.3f} bps. Valide em dados reais de livro."
    )
