"""Interface de linha de comando do robô."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .feeds import build_feed, drop_unclosed
from .news import NewsFilter
from .plot import save_svg
from .report import render_console, render_markdown, send_telegram, write_outputs
from .scanner import Scanner


def build_parser() -> argparse.ArgumentParser:
    # Opções comuns, aceitas antes ou depois do subcomando.
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)
    parser = argparse.ArgumentParser(
        prog="robo-forex",
        parents=[common],
        description="Robô de oferta/demanda + order block: analisa cada ativo e desenha "
        "entrada, stop e alvo.",
    )

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("scan", parents=[common], help="varre os ativos uma vez (padrão)")

    watch = sub.add_parser("watch", parents=[common], help="varre em loop")
    watch.add_argument("--interval", type=float, default=15.0, help="minutos entre varreduras")
    watch.add_argument("--rounds", type=int, default=0, help="nº de varreduras (0 = infinito)")

    back = sub.add_parser("backtest", parents=[common], help="valida a estratégia no histórico")
    back.add_argument("--symbol", help="ativo (padrão: todos os configurados)")
    back.add_argument("--bars", type=int, default=1500, help="barras de histórico")
    back.add_argument("--warmup", type=int, default=250, help="barras de aquecimento")
    back.add_argument("--step", type=int, default=1, help="reavaliar a cada N barras")
    back.add_argument("--out", help="salva o relatório em JSON")

    cal = sub.add_parser("calendar", parents=[common], help="mostra os eventos econômicos")
    cal.add_argument("--hours", type=int, default=48, help="horizonte em horas")

    init = sub.add_parser("init-config", parents=[common], help="grava configuração de exemplo")
    init.add_argument("--out", default="config/config.yaml")
    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Opções aceitas antes ou depois do subcomando.

    `default=SUPPRESS` é essencial: sem ele, o subparser reescreveria com None
    tudo que o parser principal já tinha lido (ex.: `--no-news scan`).
    """
    add = parser.add_argument
    s = argparse.SUPPRESS
    add("-c", "--config", default=s, help="arquivo .yaml ou .json de configuração")
    add("-s", "--symbols", default=s, help="lista de ativos separados por vírgula")
    add("--provider", choices=["yahoo", "csv", "synthetic"], default=s, help="feed de dados")
    add("--csv-dir", default=s, help="diretório dos CSVs (provider=csv)")
    add("--timeframe", default=s, help="tempo gráfico operacional (ex.: 1h)")
    add("--htf", default=s, help="tempo gráfico maior para o viés (ex.: 4h)")
    add("--balance", type=float, default=s, help="saldo da conta")
    add("--risk", type=float, default=s, help="risco por operação em %% do saldo")
    add("--min-rr", type=float, default=s, help="relação risco-retorno mínima")
    add("--min-score", type=float, default=s, help="nota mínima do setup (0-100)")
    add("--no-news", action="store_true", default=s, help="desliga o filtro de notícias")
    add("--no-session", action="store_true", default=s, help="desliga o filtro de sessão")
    add("--json", dest="json_out", default=s, help="salva o resultado em JSON")
    add("--markdown", dest="markdown_out", default=s, help="salva o resultado em Markdown")
    add("--charts", action="store_true", default=s, help="gera SVG de cada setup")
    add("--charts-dir", default=s, help="diretório dos gráficos")
    add("-v", "--verbose", action="store_true", default=s, help="diagnóstico por ativo")


def opt(args: argparse.Namespace, name: str, default=None):
    """Lê uma opção comum (ausente quando não informada, por causa do SUPPRESS)."""
    return getattr(args, name, default)


def apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    """Aplica os overrides de linha de comando sobre a configuração carregada."""
    if opt(args, "provider"):
        settings.feed.provider = args.provider
    if opt(args, "csv_dir"):
        settings.feed.csv_dir = args.csv_dir
    if opt(args, "timeframe"):
        settings.strategy.timeframe = args.timeframe
    if opt(args, "htf") is not None:
        settings.strategy.htf_timeframe = args.htf
    if opt(args, "balance"):
        settings.risk.account_balance = args.balance
    if opt(args, "risk"):
        settings.risk.risk_percent = args.risk
    if opt(args, "min_rr"):
        settings.risk.min_rr = args.min_rr
    if opt(args, "min_score") is not None:
        settings.strategy.min_score = args.min_score
    if opt(args, "no_news"):
        settings.news.enabled = False
    if opt(args, "no_session"):
        settings.session.enabled = False
    if opt(args, "json_out"):
        settings.output.json_out = args.json_out
    if opt(args, "markdown_out"):
        settings.output.markdown_out = args.markdown_out
    if opt(args, "charts"):
        settings.output.draw_charts = True
    if opt(args, "charts_dir"):
        settings.output.charts_dir = args.charts_dir
    return settings


def symbols_from(args: argparse.Namespace) -> list[str] | None:
    raw = opt(args, "symbols") or opt(args, "symbol")
    if not raw:
        return None
    return [s.strip().upper().replace("/", "") for s in raw.split(",") if s.strip()]


# ---------------------------------------------------------------- comandos
def cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    scanner = Scanner(settings)
    result = scanner.scan(symbols_from(args))
    print(render_console(result, verbose=bool(opt(args, "verbose"))))
    for path in write_outputs(result, settings):
        print(f"arquivo gravado: {path}")
    if settings.output.draw_charts:
        by_symbol = {a.symbol: a for a in result.analyses}
        for signal in result.signals:
            analysis = by_symbol.get(signal.symbol)
            if analysis:
                print(f"gráfico: {save_svg(analysis, signal, settings.output.charts_dir)}")
    token, chat = settings.output.telegram_token, settings.output.telegram_chat_id
    if token and chat:
        for signal in result.signals:
            ok = send_telegram(signal, token, chat)
            print(f"telegram {signal.symbol}: {'enviado' if ok else 'falhou'}")
    return 0 if not result.errors or result.analyses else 1


def cmd_watch(settings: Settings, args: argparse.Namespace) -> int:
    rounds = 0
    while True:
        cmd_scan(settings, args)
        rounds += 1
        if args.rounds and rounds >= args.rounds:
            return 0
        try:
            time.sleep(max(args.interval, 0.1) * 60)
        except KeyboardInterrupt:
            print("\nencerrado pelo usuário")
            return 0


def cmd_backtest(settings: Settings, args: argparse.Namespace) -> int:
    from .backtest import run_backtest

    feed = build_feed(settings.feed)
    symbols = symbols_from(args)
    specs = [settings.spec(s) for s in symbols] if symbols else list(settings.symbols)
    reports = []
    for spec in specs:
        try:
            candles = drop_unclosed(
                list(feed.fetch(spec, settings.strategy.timeframe, args.bars)),
                settings.strategy.timeframe,
            )
        except Exception as exc:
            print(f"{spec.symbol}: falha ao obter dados ({exc})")
            continue
        report = run_backtest(settings, spec, candles, warmup=args.warmup, step=args.step)
        reports.append(report)
        print(report.summary())
    if reports:
        closed = [t for r in reports for t in r.closed]
        total = sum(t.result_r for t in closed)
        wins = len([t for t in closed if t.result_r > 0])
        print(
            f"\nTOTAL: {len(closed)} operações | acerto "
            f"{(100.0 * wins / len(closed) if closed else 0):.0f}% | resultado {total:+.1f}R"
        )
    if args.out and reports:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([r.to_dict() for r in reports], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"relatório gravado: {path}")
    return 0


def cmd_calendar(settings: Settings, args: argparse.Namespace) -> int:
    news = NewsFilter(settings.news, cache_dir=settings.feed.cache_dir)
    now = datetime.now(timezone.utc)
    events = news.load()
    if not events:
        print("nenhum evento carregado (verifique a conexão ou news.provider)")
        return 1
    symbols = symbols_from(args)
    specs = [settings.spec(s) for s in symbols] if symbols else list(settings.symbols)
    print(f"Eventos relevantes nas próximas {args.hours}h (UTC):\n")
    for spec in specs:
        upcoming = news.upcoming(spec, now, args.hours)
        verdict = news.check(spec, now)
        status = "BLOQUEADO" if verdict.blocked else "liberado"
        print(f"{spec.symbol} [{status}] {verdict.reason}")
        for event in upcoming[:6]:
            print(f"    {event.ts:%d/%m %H:%M} {event.currency:<4} {event.impact:<6} {event.title}")
    return 0


def cmd_init_config(settings: Settings, args: argparse.Namespace) -> int:
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = apply_overrides(Settings.load(opt(args, "config")), args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"erro de configuração: {exc}", file=sys.stderr)
        return 2
    command = args.command or "scan"
    handlers = {
        "scan": cmd_scan,
        "watch": cmd_watch,
        "backtest": cmd_backtest,
        "calendar": cmd_calendar,
        "init-config": cmd_init_config,
    }
    return handlers[command](settings, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
