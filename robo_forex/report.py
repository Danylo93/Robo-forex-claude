"""Saída do robô: console, markdown, JSON e envio opcional para o Telegram."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Settings
from .models import Signal
from .scanner import ScanResult

BAR = "─" * 72


def format_signal(signal: Signal) -> str:
    """Bloco de texto no mesmo formato do gráfico: VENDA/COMPRA, STOP e ALVO."""
    lines = [
        f"{signal.symbol} | {signal.timeframe} | {signal.side.label_ptbr} "
        f"{'📉' if signal.side.value == 'SELL' else '📈'}  (nota {signal.score:.0f})",
        f"  {signal.side.label_ptbr:<6} {signal.fmt(signal.entry)}",
        f"  STOP   {signal.fmt(signal.stop)}   (risco 1R = {signal.fmt(signal.risk)})",
        f"  ALVO   {signal.fmt(signal.target)}   (RR {signal.rr:.1f}:1)",
        f"  preço atual {signal.fmt(signal.last_price)} | viés HTF: {signal.htf_bias}",
    ]
    if signal.position:
        lines.append(
            f"  risco {signal.position.risk_amount:.2f} | stop {signal.position.stop_pips:.1f} pips "
            f"| {signal.position.lots:.2f} lote(s)"
        )
    lines.append("  Racional:")
    lines.extend(f"    • {reason}" for reason in signal.reasons)
    if signal.warnings:
        lines.append("  Atenção:")
        lines.extend(f"    ! {warning}" for warning in signal.warnings)
    return "\n".join(lines)


def render_console(result: ScanResult, verbose: bool = False) -> str:
    out = [
        BAR,
        f"ROBÔ OFERTA/DEMANDA + ORDER BLOCK — {result.started_at:%d/%m/%Y %H:%M} UTC",
        BAR,
    ]
    for note in result.notes:
        out.append(f"* {note}")
    signals = result.signals
    if signals:
        out.append(f"\n{len(signals)} setup(s) encontrado(s):\n")
        for signal in signals:
            out.append(format_signal(signal))
            out.append("")
    else:
        out.append("\nNenhum setup válido no momento.\n")
    if verbose:
        out.append(BAR)
        out.append("Diagnóstico por ativo:")
        for analysis in result.analyses:
            status = "SINAL" if analysis.signal else "-"
            out.append(
                f"  {analysis.symbol:<8} {status:<6} preço {analysis.price:.5f} "
                f"ATR {analysis.atr:.5f} viés {analysis.bias:<7} "
                f"zonas {len(analysis.zones)} OBs {len(analysis.order_blocks)} "
                f"confluências {len(analysis.confluences)}"
            )
            for rejection in analysis.rejections[:4]:
                out.append(f"      · {rejection}")
    if result.errors:
        out.append(BAR)
        out.append("Erros de dados:")
        for symbol, error in result.errors.items():
            out.append(f"  {symbol}: {error}")
    out.append(BAR)
    return "\n".join(out)


def render_markdown(result: ScanResult) -> str:
    lines = [
        f"# Setups — {result.started_at:%d/%m/%Y %H:%M} UTC",
        "",
        "| Ativo | TF | Lado | Entrada | Stop | Alvo | RR | Nota | Viés |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in result.signals:
        lines.append(
            f"| {s.symbol} | {s.timeframe} | {s.side.label_ptbr} | {s.fmt(s.entry)} | "
            f"{s.fmt(s.stop)} | {s.fmt(s.target)} | {s.rr:.1f}:1 | {s.score:.0f} | {s.htf_bias} |"
        )
    if not result.signals:
        lines.append("| — | — | — | — | — | — | — | — | — |")
    for s in result.signals:
        lines += ["", f"## {s.symbol} {s.timeframe} — {s.side.label_ptbr}", ""]
        lines += [f"- {reason}" for reason in s.reasons]
        if s.warnings:
            lines += [f"- ⚠️ {warning}" for warning in s.warnings]
    if result.errors:
        lines += ["", "## Erros de dados", ""]
        lines += [f"- **{symbol}**: {error}" for symbol, error in result.errors.items()]
    return "\n".join(lines) + "\n"


def write_outputs(result: ScanResult, settings: Settings) -> list[Path]:
    written: list[Path] = []
    if settings.output.json_out:
        path = Path(settings.output.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        written.append(path)
    if settings.output.markdown_out:
        path = Path(settings.output.markdown_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(result), encoding="utf-8")
        written.append(path)
    return written


def telegram_text(signal: Signal) -> str:
    arrow = "📉" if signal.side.value == "SELL" else "📈"
    lines = [
        f"{arrow} *{signal.symbol}* | {signal.timeframe} | {signal.side.label_ptbr}",
        f"{signal.side.label_ptbr}: `{signal.fmt(signal.entry)}`",
        f"STOP: `{signal.fmt(signal.stop)}`",
        f"ALVO: `{signal.fmt(signal.target)}`  (RR {signal.rr:.1f}:1)",
        "",
    ]
    lines += [f"• {reason}" for reason in signal.reasons]
    if signal.warnings:
        lines += [f"⚠️ {warning}" for warning in signal.warnings]
    return "\n".join(lines)


def send_telegram(signal: Signal, token: str, chat_id: str, timeout: float = 10.0) -> bool:
    """Envia o sinal para um chat do Telegram. Retorna False em caso de falha."""
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": telegram_text(signal), "parse_mode": "Markdown"}
    ).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
