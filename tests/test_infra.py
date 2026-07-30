"""Testes de configuração, feeds, indicadores, relatório, gráfico, backtest e CLI."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from robo_forex.backtest import run_backtest
from robo_forex.cli import main
from robo_forex.config import Settings, SymbolSpec
from robo_forex.feeds.base import drop_unclosed, resample, timeframe_minutes
from robo_forex.feeds.csv_feed import CsvFeed, read_csv
from robo_forex.feeds.synthetic import SyntheticFeed, pattern_candles
from robo_forex.feeds.yahoo import YahooFeed, _parse
from robo_forex.indicators import atr, ema, linreg, sma, true_range
from robo_forex.plot import render_svg
from robo_forex.report import render_console, render_markdown, telegram_text, write_outputs
from robo_forex.scanner import Scanner
from robo_forex.strategy import Strategy

from .helpers import candle, long_series, offline_settings

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


class IndicatorTests(unittest.TestCase):
    def test_true_range_uses_gaps(self):
        self.assertAlmostEqual(true_range(1.0, 1.2, 1.1), 0.2)

    def test_atr_positive(self):
        candles = pattern_candles("sell")
        self.assertGreater(atr(candles, 14), 0)

    def test_atr_on_single_candle(self):
        self.assertAlmostEqual(atr([candle(0, 1.0, 1.1, 0.9, 1.05)], 14), 0.2)

    def test_sma_and_ema(self):
        self.assertAlmostEqual(sma([1, 2, 3, 4], 4), 2.5)
        self.assertIsNone(sma([1, 2], 5))
        self.assertIsNotNone(ema([1, 2, 3, 4, 5], 3))

    def test_linreg_slope(self):
        slope, intercept = linreg([0, 1, 2], [0, 2, 4])
        self.assertAlmostEqual(slope, 2.0)
        self.assertAlmostEqual(intercept, 0.0)


class TimeframeTests(unittest.TestCase):
    def test_minutes_lookup(self):
        self.assertEqual(timeframe_minutes("4h"), 240)
        with self.assertRaises(ValueError):
            timeframe_minutes("7h")

    def test_resample_h1_to_h4(self):
        candles = [candle(i, 1.0 + i, 1.5 + i, 0.5 + i, 1.2 + i) for i in range(8)]
        h4 = resample(candles, 60, 240)
        self.assertEqual(len(h4), 2)
        self.assertAlmostEqual(h4[0].open, candles[0].open)
        self.assertAlmostEqual(h4[0].close, candles[3].close)
        self.assertAlmostEqual(h4[0].high, max(c.high for c in candles[:4]))

    def test_resample_rejects_incompatible(self):
        with self.assertRaises(ValueError):
            resample([candle(0, 1, 1, 1, 1)], 60, 90)

    def test_drop_unclosed_removes_forming_bar(self):
        candles = [candle(i, 1, 1, 1, 1) for i in range(3)]
        now = candles[-1].ts.replace(minute=30)
        self.assertEqual(len(drop_unclosed(candles, "1h", now)), 2)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        settings = Settings()
        self.assertEqual(settings.strategy.timeframe, "1h")
        self.assertAlmostEqual(settings.risk.min_rr, 3.0)
        self.assertTrue(any(s.symbol == "GBPNZD" for s in settings.symbols))

    def test_load_json_overrides(self):
        data = {
            "symbols": ["gbp/nzd", {"symbol": "HK50", "kind": "index", "currencies": ["HKD"]}],
            "strategy": {"timeframe": "4h", "zone": {"min_touches": 4}},
            "risk": {"min_rr": 2.5},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            settings = Settings.load(path)
        self.assertEqual([s.symbol for s in settings.symbols], ["GBPNZD", "HK50"])
        self.assertEqual(settings.strategy.timeframe, "4h")
        self.assertEqual(settings.strategy.zone.min_touches, 4)
        self.assertAlmostEqual(settings.risk.min_rr, 2.5)
        self.assertEqual(settings.spec("HK50").currencies, ["HKD"])

    def test_unknown_option_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"risk": {"nao_existe": 1}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                Settings.load(path)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            Settings.load("/tmp/nao-existe-config.json")

    def test_round_trip_dict(self):
        self.assertIn("strategy", Settings().to_dict())


class CsvFeedTests(unittest.TestCase):
    HEADER = "time,open,high,low,close,volume\n"

    def rows(self, n=5):
        out = ""
        for i in range(n):
            out += f"2026-07-0{i + 1} 10:00,1.10,1.11,1.09,1.105,{100 + i}\n"
        return out

    def test_reads_and_sorts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "EURUSD_1h.csv"
            path.write_text(self.HEADER + self.rows(), encoding="utf-8")
            candles = read_csv(path)
            feed = CsvFeed(tmp)
            fetched = feed.fetch(SymbolSpec("EURUSD"), "1h", 3)
        self.assertEqual(len(candles), 5)
        self.assertEqual(len(fetched), 3)
        self.assertTrue(all(a.ts <= b.ts for a, b in zip(candles, candles[1:])))

    def test_semicolon_and_mt5_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "GBPUSD_1h.csv"
            path.write_text(
                "<DATE>;<OPEN>;<HIGH>;<LOW>;<CLOSE>;<TICKVOL>\n"
                "2026-07-01 10:00;1.30;1.31;1.29;1.305;10\n"
                "2026-07-01 11:00;1.305;1.32;1.30;1.315;12\n",
                encoding="utf-8",
            )
            candles = read_csv(path)
        self.assertEqual(len(candles), 2)
        self.assertAlmostEqual(candles[0].close, 1.305)

    def test_aggregates_from_smaller_timeframe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "EURUSD_1h.csv"
            body = "".join(
                f"2026-07-01 {h:02d}:00,1.10,1.11,1.09,1.105,10\n" for h in range(0, 16)
            )
            path.write_text(self.HEADER + body, encoding="utf-8")
            h4 = CsvFeed(tmp).fetch(SymbolSpec("EURUSD"), "4h", 4)
        self.assertEqual(len(h4), 4)

    def test_missing_columns_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "X_1h.csv"
            path.write_text("time,close\n2026-07-01,1.1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_csv(path)

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                CsvFeed(tmp).fetch(SymbolSpec("EURUSD"), "1h", 10)


class YahooParseTests(unittest.TestCase):
    def test_parses_payload_and_skips_holes(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [0, 3600, 7200],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [1.0, None, 1.2],
                                    "high": [1.1, None, 1.3],
                                    "low": [0.9, None, 1.1],
                                    "close": [1.05, None, 1.25],
                                    "volume": [10, None, 12],
                                }
                            ]
                        },
                    }
                ]
            }
        }
        candles = _parse(payload)
        self.assertEqual(len(candles), 2)
        self.assertAlmostEqual(candles[1].close, 1.25)

    def test_source_interval_for_4h_is_60m(self):
        self.assertEqual(YahooFeed._source_minutes(240), 60)

    def test_range_days_respects_cap(self):
        self.assertLessEqual(YahooFeed._range_days(60, 100_000), 730)


class ReportTests(unittest.TestCase):
    def setUp(self):
        settings = offline_settings()
        settings.symbols = [SymbolSpec("GBPNZD")]
        self.settings = settings
        self.result = Scanner(settings, feed=SyntheticFeed("sell"), news=None).scan(now=NOW)

    def test_console_contains_levels(self):
        text = render_console(self.result, verbose=True)
        self.assertIn("VENDA", text)
        self.assertIn("STOP", text)
        self.assertIn("ALVO", text)
        self.assertIn("Diagnóstico", text)

    def test_markdown_table(self):
        text = render_markdown(self.result)
        self.assertIn("| Ativo | TF |", text)
        self.assertIn("GBPNZD", text)

    def test_telegram_text(self):
        text = telegram_text(self.result.signals[0])
        self.assertIn("GBPNZD", text)
        self.assertIn("ALVO", text)

    def test_write_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.settings.output.json_out = str(Path(tmp) / "out.json")
            self.settings.output.markdown_out = str(Path(tmp) / "out.md")
            paths = write_outputs(self.result, self.settings)
            self.assertEqual(len(paths), 2)
            data = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        self.assertTrue(data["signals"])

    def test_svg_has_labels_and_candles(self):
        analysis = self.result.analyses[0]
        svg = render_svg(analysis, analysis.signal)
        self.assertIn("<svg", svg)
        self.assertIn("STOP", svg)
        self.assertIn("ALVO", svg)
        self.assertIn("VENDA", svg)
        self.assertGreater(svg.count("<rect"), 20)

    def test_svg_without_signal(self):
        analysis = self.result.analyses[0]
        self.assertIn("sem setup", render_svg(analysis, None))


class BacktestTests(unittest.TestCase):
    def test_runs_and_produces_trades(self):
        settings = offline_settings()
        candles = long_series(8)
        report = run_backtest(
            settings, SymbolSpec("GBPNZD"), candles, warmup=120, step=2, max_hold_bars=80
        )
        self.assertGreater(report.bars, 300)
        self.assertGreaterEqual(report.signals, 1)
        self.assertTrue(report.trades)
        for trade in report.closed:
            self.assertIn(trade.outcome, ("alvo", "stop"))
            if trade.outcome == "stop":
                self.assertLessEqual(trade.result_r, 0.0)
        self.assertIn("acerto", report.summary())
        self.assertIn("stats", report.to_dict())

    def test_short_history_returns_empty(self):
        report = run_backtest(offline_settings(), SymbolSpec("GBPNZD"), pattern_candles("sell"))
        self.assertEqual(report.trades, [])

    def test_stats_on_empty_report(self):
        report = run_backtest(offline_settings(), SymbolSpec("GBPNZD"), [])
        self.assertEqual(report.win_rate, 0.0)
        self.assertEqual(report.expectancy_r, 0.0)
        self.assertEqual(report.max_drawdown_r, 0.0)


class BacktestCostTests(unittest.TestCase):
    """O custo de transação precisa aparecer no resultado, senão o backtest mente."""

    def signal(self, entry=1.1000, stop=1.1040, symbol="EURUSD"):
        from robo_forex.models import Side, Signal

        return Signal(
            symbol=symbol, timeframe="1h", side=Side.SELL, entry=entry, stop=stop,
            target=entry - 3 * abs(entry - stop), rr=3.0, score=70.0, ts=NOW,
        )

    def test_cost_is_measured_in_r(self):
        from robo_forex.backtest import cost_in_r
        from robo_forex.config import CostSettings, SymbolSpec

        costs = CostSettings(slippage_pips=0.2, commission_per_lot_round_turn=0.0)
        # stop de 40 pips, spread 0,8 + derrapagem 0,2 = 1,0 pip -> 0,025R
        cost = cost_in_r(self.signal(), SymbolSpec("EURUSD"), costs)
        self.assertAlmostEqual(cost, 1.0 / 40.0)

    def test_tighter_stop_pays_proportionally_more(self):
        from robo_forex.backtest import cost_in_r
        from robo_forex.config import CostSettings, SymbolSpec

        costs = CostSettings()
        wide = cost_in_r(self.signal(stop=1.1040), SymbolSpec("EURUSD"), costs)
        tight = cost_in_r(self.signal(stop=1.1010), SymbolSpec("EURUSD"), costs)
        self.assertAlmostEqual(tight, wide * 4, places=6)

    def test_commission_increases_cost(self):
        from robo_forex.backtest import cost_in_r
        from robo_forex.config import CostSettings, SymbolSpec

        spec = SymbolSpec("EURUSD")
        without = cost_in_r(self.signal(), spec, CostSettings())
        with_commission = cost_in_r(
            self.signal(), spec, CostSettings(commission_per_lot_round_turn=7.0)
        )
        self.assertGreater(with_commission, without)
        self.assertAlmostEqual(with_commission - without, 0.7 / 40.0, places=6)

    def test_disabled_costs_are_zero(self):
        from robo_forex.backtest import cost_in_r
        from robo_forex.config import CostSettings, SymbolSpec

        self.assertEqual(
            cost_in_r(self.signal(), SymbolSpec("EURUSD"), CostSettings(enabled=False)), 0.0
        )

    def test_spread_by_symbol_is_used(self):
        from robo_forex.backtest import cost_in_r
        from robo_forex.config import CostSettings, SymbolSpec

        costs = CostSettings()
        eur = cost_in_r(self.signal(symbol="EURUSD"), SymbolSpec("EURUSD"), costs)
        gbpnzd = cost_in_r(
            self.signal(entry=2.2900, stop=2.2940, symbol="GBPNZD"), SymbolSpec("GBPNZD"), costs
        )
        self.assertGreater(gbpnzd, eur)  # GBPNZD tem spread muito maior

    def test_net_result_is_gross_minus_cost(self):
        settings = offline_settings()
        report = run_backtest(
            settings, SymbolSpec("GBPNZD"), long_series(8), warmup=120, step=2, max_hold_bars=80
        )
        self.assertTrue(report.closed)
        for trade in report.closed:
            self.assertAlmostEqual(trade.result_r, trade.gross_r - trade.cost_r, places=9)
        self.assertGreater(report.cost_r, 0)
        self.assertAlmostEqual(report.total_r, report.gross_r - report.cost_r, places=6)

    def test_summary_shows_cost_breakdown(self):
        settings = offline_settings()
        report = run_backtest(
            settings, SymbolSpec("GBPNZD"), long_series(8), warmup=120, step=2, max_hold_bars=80
        )
        self.assertIn("custos", report.summary())
        self.assertIn("cost_r", report.to_dict()["stats"])


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_scan_with_synthetic_provider(self):
        code, out = self.run_cli(
            ["scan", "--provider", "synthetic", "--symbols", "GBPNZD", "--no-news",
             "--no-session", "-v"]
        )
        self.assertEqual(code, 0)
        self.assertIn("GBPNZD", out)

    def test_global_flags_before_subcommand(self):
        code, out = self.run_cli(
            ["--provider", "synthetic", "--no-news", "--no-session", "-s", "GBPNZD", "scan"]
        )
        self.assertEqual(code, 0)
        self.assertIn("VENDA", out)

    def test_json_and_chart_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli(
                ["scan", "--provider", "synthetic", "-s", "GBPNZD", "--no-news", "--no-session",
                 "--json", str(Path(tmp) / "r.json"), "--charts", "--charts-dir", tmp]
            )
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "r.json").exists())
            self.assertTrue(list(Path(tmp).glob("*.svg")))

    def test_init_config_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            code, _ = self.run_cli(["init-config", "--out", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("strategy", json.loads(path.read_text(encoding="utf-8")))

    def test_backtest_command(self):
        code, out = self.run_cli(
            ["backtest", "--provider", "synthetic", "-s", "GBPNZD", "--bars", "400",
             "--warmup", "150", "--step", "5", "--no-news", "--no-session"]
        )
        self.assertEqual(code, 0)
        self.assertIn("GBPNZD", out)

    def test_invalid_config_returns_error_code(self):
        code, _ = self.run_cli(["-c", "/tmp/nao-existe.json", "scan"])
        self.assertEqual(code, 2)

    def test_watch_with_limited_rounds(self):
        code, out = self.run_cli(
            ["watch", "--provider", "synthetic", "-s", "GBPNZD", "--no-news", "--no-session",
             "--rounds", "1", "--interval", "0.001"]
        )
        self.assertEqual(code, 0)
        self.assertIn("GBPNZD", out)


class StrategyEdgeTests(unittest.TestCase):
    def test_empty_candles(self):
        analysis = Strategy(offline_settings()).analyze(SymbolSpec("EURUSD"), [], None, NOW)
        self.assertIsNone(analysis.signal)
        self.assertTrue(analysis.rejections)


if __name__ == "__main__":
    unittest.main()
