"""Linha de comando do robô HFT."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .brokers import PaperBroker, build_broker
from .config import HftSettings
from .engine import Engine
from .report import render, summarize
from .strategies import STRATEGIES, build_strategy
from .ticks import read_tick_csv, synthetic_ticks


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    s = argparse.SUPPRESS
    add = common.add_argument
    add("-c", "--config", default=s, help="arquivo .yaml/.json de configuração")
    add("--symbol", default=s, help="ativo (ex.: EURUSD)")
    add("--strategy", choices=sorted(STRATEGIES), default=s, help="estratégia")
    add("--balance", type=float, default=s, help="saldo inicial")
    add("--lots", type=float, default=s, help="lote fixo (0 = dimensionar por risco)")
    add("--spread", type=float, default=s, help="spread máximo aceito, em pips")
    add("--commission", type=float, default=s, help="comissão por lote por perna")
    add("--latency", type=float, default=s, help="latência simulada em ms")
    add("--no-news", action="store_true", default=s, help="desliga o bloqueio por notícia")
    add("--json", dest="json_out", default=s, help="grava o relatório em JSON")
    add("-v", "--verbose", action="store_true", default=s, help="mostra cada evento")

    parser = argparse.ArgumentParser(
        prog="robo-hft",
        parents=[common],
        description="Robô de alta frequência: backtest com custos reais, modo papel e execução "
        "em conta demo/real (OANDA ou MetaTrader 5).",
    )
    sub = parser.add_subparsers(dest="command")

    back = sub.add_parser("backtest", parents=[common], help="simula sobre ticks")
    back.add_argument("--ticks-csv", help="arquivo CSV de ticks (time,bid,ask)")
    back.add_argument("--count", type=int, default=40_000, help="ticks sintéticos")
    back.add_argument("--reversion", type=float, default=0.02, help="força da reversão sintética")
    back.add_argument("--tick-spread", type=float, default=0.6, help="spread dos ticks sintéticos")
    back.add_argument("--seed", type=int, default=7)

    val = sub.add_parser("validate", parents=[common], help="acha o ponto de equilíbrio de custos")
    val.add_argument("--count", type=int, default=40_000)
    val.add_argument("--seed", type=int, default=7)
    val.add_argument(
        "--spreads", default="0.2,0.4,0.6,0.8,1.0", help="spreads em pips a testar"
    )
    val.add_argument("--reversion", type=float, default=0.02)

    paper = sub.add_parser("paper", parents=[common], help="opera em papel com preços ao vivo")
    paper.add_argument("--seconds", type=float, default=300.0, help="duração da sessão")

    live = sub.add_parser("live", parents=[common], help="opera na corretora configurada")
    live.add_argument("--seconds", type=float, default=600.0, help="duração da sessão")
    live.add_argument(
        "--confirmo-conta-real",
        action="store_true",
        help="obrigatório para operar com dinheiro real (broker.mode=live)",
    )

    mm = sub.add_parser(
        "mm", parents=[common], help="market making: simula cotação de dois lados"
    )
    mm.add_argument("--count", type=int, default=40_000, help="ticks sintéticos")
    mm.add_argument("--ticks-csv", help="arquivo CSV de ticks")
    mm.add_argument("--tick-spread", type=float, default=0.6)
    mm.add_argument("--seed", type=int, default=9)
    mm.add_argument("--half-spread", type=float, default=0.4, help="meia-distância base em pips")
    mm.add_argument("--quote-size", type=float, default=0.1, help="lotes por cotação")
    mm.add_argument("--max-inventory", type=float, default=0.5, help="estoque máximo em lotes")
    mm.add_argument("--rebate", type=float, default=0.0, help="rebate de maker por lote")
    mm.add_argument("--fee", type=float, default=0.0, help="taxa de maker por lote")
    mm.add_argument("--max-loss", type=float, default=0.0, help="perda que interrompe a sessão")

    lat = sub.add_parser(
        "latency", parents=[common], help="mede a latência do caminho crítico"
    )
    lat.add_argument("--count", type=int, default=50_000, help="ticks processados")
    lat.add_argument("--no-gc-tuning", action="store_true", help="não desliga o coletor de lixo")
    lat.add_argument("--cpu", type=int, help="fixa o processo neste núcleo")

    init = sub.add_parser("init-config", parents=[common], help="grava configuração de exemplo")
    init.add_argument("--out", default="config/hft.yaml")
    return parser


def opt(args, name, default=None):
    return getattr(args, name, default)


def apply_overrides(settings: HftSettings, args) -> HftSettings:
    if opt(args, "symbol"):
        settings.instrument = type(settings.instrument)(symbol=args.symbol)
    if opt(args, "strategy"):
        settings.strategy.name = args.strategy
    if opt(args, "balance"):
        settings.risk.account_balance = args.balance
    if opt(args, "lots") is not None:
        settings.risk.fixed_lots = args.lots
    if opt(args, "spread"):
        settings.risk.max_spread_pips = args.spread
    if opt(args, "commission") is not None:
        settings.costs.commission_per_lot_per_side = args.commission
    if opt(args, "latency") is not None:
        settings.costs.latency_ms = args.latency
    if opt(args, "no_news"):
        settings.risk.news_blackout = False
    return settings


def build_news(settings: HftSettings):
    """Reaproveita o calendário econômico do robô de análise."""
    if not settings.risk.news_blackout:
        return None, None
    try:
        from robo_forex.config import NewsSettings, SymbolSpec
        from robo_forex.news import NewsFilter

        news_settings = NewsSettings(
            minutes_before=settings.risk.news_minutes_before,
            minutes_after=settings.risk.news_minutes_after,
        )
        return NewsFilter(news_settings), SymbolSpec(settings.instrument.symbol)
    except Exception:
        return None, None


def make_logger(verbose: bool):
    def log(event: str, data: dict) -> None:
        if not verbose:
            return
        detail = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"[{event}] {detail}")

    return log


# ---------------------------------------------------------------- comandos
def cmd_backtest(settings: HftSettings, args) -> int:
    if opt(args, "ticks_csv"):
        ticks = read_tick_csv(args.ticks_csv)
        spread = ticks[0].spread / settings.instrument.pip if ticks else 0.0
    else:
        spread = args.tick_spread
        ticks = synthetic_ticks(
            count=args.count,
            start_price=1.1000,
            pip=settings.instrument.pip,
            spread_pips=spread,
            reversion=args.reversion,
            seed=args.seed,
        )
        print(
            f"* ticks sintéticos ({len(ticks)}, reversão {args.reversion}) — servem para "
            "validar o motor e o custo, não para provar lucro real\n"
        )
    broker = PaperBroker(settings)
    engine = Engine(
        settings, broker, build_strategy(settings), on_event=make_logger(bool(opt(args, "verbose")))
    )
    engine.run(ticks)
    report = summarize(engine, settings)
    print(render(report, settings, spread))
    _write_json(report, args)
    return 0


def cmd_validate(settings: HftSettings, args) -> int:
    """Roda o mesmo fluxo variando o spread: onde a estratégia deixa de pagar."""
    spreads = [float(x) for x in args.spreads.split(",") if x.strip()]
    print(
        "Ponto de equilíbrio por spread (ticks sintéticos, reversão "
        f"{args.reversion}, {args.count} ticks)\n"
    )
    print(f"{'spread':>8} {'custo/op':>10} {'ops':>6} {'pips médios':>12} {'líquido':>10}  veredito")
    rows = []
    for spread in spreads:
        ticks = synthetic_ticks(
            count=args.count,
            pip=settings.instrument.pip,
            spread_pips=spread,
            reversion=args.reversion,
            seed=args.seed,
        )
        local = HftSettings.from_dict(settings.to_dict())
        local.risk.max_spread_pips = max(spread + 0.05, local.risk.max_spread_pips)
        engine = Engine(local, PaperBroker(local), build_strategy(local))
        engine.run(ticks)
        report = summarize(engine, local)
        cost = local.cost_per_trade_pips(spread)
        status = "paga" if report.net_pnl > 0 and report.count >= 30 else "não paga"
        print(
            f"{spread:>8.2f} {cost:>10.2f} {report.count:>6} {report.avg_pips:>+12.3f} "
            f"{report.net_pnl:>+10.2f}  {status}"
        )
        rows.append({"spread": spread, "cost_pips": round(cost, 3), **report.to_dict()})
    print(
        "\nLeitura: enquanto os 'pips médios' brutos não superarem o 'custo/op', "
        "não existe robô que salve — o dinheiro vai todo para spread e comissão."
    )
    if opt(args, "json_out"):
        Path(args.json_out).write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"relatório gravado: {args.json_out}")
    return 0


def cmd_paper(settings: HftSettings, args) -> int:
    """Preços reais da corretora, ordens apenas simuladas."""
    live_broker = build_broker(settings)
    paper = PaperBroker(settings)
    news, spec = build_news(settings)
    engine = Engine(
        settings, paper, build_strategy(settings), news, spec,
        on_event=make_logger(bool(opt(args, "verbose"))),
    )
    try:
        live_broker.connect()
    except Exception as exc:
        print(f"não foi possível conectar para ler preços: {exc}", file=sys.stderr)
        return 1
    import time

    print(f"modo papel: lendo preços de {live_broker.name}, ordens simuladas. Ctrl+C encerra.")
    started = time.monotonic()
    interval = max(settings.broker.poll_ms, 1.0) / 1000.0
    try:
        while time.monotonic() - started < args.seconds:
            tick = live_broker.tick()
            if tick:
                engine.on_tick(tick)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nencerrado pelo usuário")
    finally:
        live_broker.shutdown()
    report = summarize(engine, settings)
    print(render(report, settings))
    _write_json(report, args)
    return 0


def cmd_live(settings: HftSettings, args) -> int:
    if settings.broker.provider == "paper":
        print(
            "broker.provider=paper não envia ordens. Configure 'oanda' ou 'mt5' para operar.",
            file=sys.stderr,
        )
        return 2
    if settings.broker.mode == "live" and not opt(args, "confirmo_conta_real"):
        print(
            "RECUSADO: broker.mode=live opera dinheiro real.\n"
            "Rode antes em demo (broker.mode=demo) e, quando tiver estatística própria,\n"
            "repita o comando com --confirmo-conta-real.",
            file=sys.stderr,
        )
        return 2
    news, spec = build_news(settings)
    broker = build_broker(settings)
    engine = Engine(
        settings, broker, build_strategy(settings), news, spec,
        on_event=make_logger(True),
    )
    print(
        f"iniciando em {broker.name} ({settings.broker.mode}) — "
        f"{settings.instrument.symbol}, {settings.strategy.name}"
    )
    try:
        engine.run_live(max_seconds=args.seconds)
    except KeyboardInterrupt:
        print("\nencerrado pelo usuário")
    report = summarize(engine, settings)
    print(render(report, settings))
    _write_json(report, args)
    return 0


def cmd_market_making(settings: HftSettings, args) -> int:
    """Simula o market maker: cotação de dois lados, estoque e seleção adversa."""
    from .marketmaker import MarketMaker, MarketMakerParams, QuotingEngine, render_maker_report

    if opt(args, "ticks_csv"):
        ticks = read_tick_csv(args.ticks_csv)
    else:
        ticks = synthetic_ticks(
            count=args.count,
            pip=settings.instrument.pip,
            spread_pips=args.tick_spread,
            seed=args.seed,
        )
        print(
            f"* ticks sintéticos ({len(ticks)}) — o simulador executa a cotação apenas quando "
            "o preço a atravessa, que é o pior caso realista\n"
        )
    params = MarketMakerParams(
        base_half_spread_pips=args.half_spread,
        quote_size_lots=args.quote_size,
        max_inventory_lots=args.max_inventory,
        rebate_per_lot=args.rebate,
        fee_per_lot=args.fee,
    )
    engine = QuotingEngine(
        settings,
        MarketMaker(settings, params),
        params,
        max_loss=args.max_loss or None,
    )
    report = engine.run(ticks)
    print(render_maker_report(report, settings))
    _write_json(report, args)
    return 0


def cmd_latency(settings: HftSettings, args) -> int:
    """Mede o caminho crítico do motor com o runtime de baixa latência ligado."""
    from .brokers.paper import PaperBroker
    from .latency import LatencyTracker
    from .lowlat import TradingRuntime

    tracker = LatencyTracker()
    engine = Engine(settings, PaperBroker(settings), build_strategy(settings))
    ticks = synthetic_ticks(count=args.count, pip=settings.instrument.pip, seed=5)

    runtime = TradingRuntime(
        disable_gc=not opt(args, "no_gc_tuning", False), cpu=opt(args, "cpu")
    )
    with runtime:
        for note in runtime.notes:
            print(f"* {note}")
        for tick in ticks:
            started = tracker.start("tick_to_trade")
            engine.on_tick(tick)
            tracker.stop("tick_to_trade", started)

    print(f"\n{len(ticks)} ticks processados\n")
    print(tracker.report())
    print(
        "\nA cauda (p99.9 e máximo) é o que importa: picos vêm do coletor de lixo e do "
        "\nagendador do sistema. Em Python o piso realista é dezenas de µs — para competir "
        "\nem µs o caminho crítico precisa ser C++/Rust (docs/HFT_INSTITUCIONAL.md)."
    )
    if opt(args, "json_out"):
        Path(args.json_out).write_text(
            json.dumps(tracker.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"relatório gravado: {args.json_out}")
    return 0


def cmd_init_config(settings: HftSettings, args) -> int:
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = settings.to_dict()
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore

            path.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
        except ImportError:
            path = path.with_suffix(".json")
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"configuração gravada em {path}")
    return 0


def _write_json(report, args) -> None:
    if opt(args, "json_out"):
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"relatório gravado: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = apply_overrides(HftSettings.load(opt(args, "config")), args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"erro de configuração: {exc}", file=sys.stderr)
        return 2
    handlers = {
        "backtest": cmd_backtest,
        "validate": cmd_validate,
        "mm": cmd_market_making,
        "latency": cmd_latency,
        "paper": cmd_paper,
        "live": cmd_live,
        "init-config": cmd_init_config,
    }
    command = args.command or "backtest"
    if command == "backtest" and not hasattr(args, "count"):
        args = parser.parse_args((argv or []) + ["backtest"])
    return handlers[command](settings, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
