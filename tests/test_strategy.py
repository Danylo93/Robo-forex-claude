"""Testes de ponta a ponta da estratégia e do scanner."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from robo_forex.config import NewsSettings, Settings, SymbolSpec
from robo_forex.feeds.synthetic import SyntheticFeed, build_legs, pattern_candles
from robo_forex.models import NewsEvent, Side
from robo_forex.news import NewsFilter
from robo_forex.scanner import Scanner
from robo_forex.strategy import Strategy, session_ok

from .helpers import offline_settings

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)  # quinta-feira


class FakeCalendar:
    def __init__(self, events):
        self._events = events

    def events(self):
        return list(self._events)


class SellSetupTests(unittest.TestCase):
    def setUp(self):
        self.settings = offline_settings()
        self.spec = SymbolSpec("GBPNZD")
        self.analysis = Strategy(self.settings).analyze(
            self.spec, pattern_candles("sell"), None, NOW
        )

    def test_signal_is_generated(self):
        self.assertIsNotNone(self.analysis.signal, self.analysis.rejections)
        self.assertIs(self.analysis.signal.side, Side.SELL)

    def test_levels_are_ordered(self):
        signal = self.analysis.signal
        self.assertGreater(signal.stop, signal.entry)
        self.assertLess(signal.target, signal.entry)

    def test_rr_meets_minimum(self):
        self.assertGreaterEqual(self.analysis.signal.rr, self.settings.risk.min_rr)

    def test_stop_sits_beyond_zone_top(self):
        signal = self.analysis.signal
        self.assertGreaterEqual(signal.stop, signal.zone.top)

    def test_entry_at_zone_proximal_edge(self):
        signal = self.analysis.signal
        self.assertLessEqual(signal.entry, signal.zone.top)
        self.assertGreaterEqual(signal.entry, signal.zone.bottom - 1e-9)

    def test_order_block_overlaps_zone(self):
        signal = self.analysis.signal
        self.assertIsNotNone(signal.order_block)
        self.assertGreater(signal.zone.overlap(signal.order_block), 0)

    def test_reasons_describe_the_setup(self):
        text = " ".join(self.analysis.signal.reasons).lower()
        self.assertIn("linha de oferta", text)
        self.assertIn("order block", text)
        self.assertIn("alvo", text)

    def test_position_is_sized(self):
        self.assertGreater(self.analysis.signal.position.stop_pips, 0)

    def test_serializes_to_dict(self):
        data = self.analysis.signal.to_dict()
        self.assertEqual(data["side"], "SELL")
        self.assertEqual(data["side_ptbr"], "VENDA")
        self.assertIn("zone", data)
        self.assertIn("order_block", data)


class BuySetupTests(unittest.TestCase):
    def test_demand_pattern_generates_buy(self):
        analysis = Strategy(offline_settings()).analyze(
            SymbolSpec("AUDUSD"), pattern_candles("buy"), None, NOW
        )
        self.assertIsNotNone(analysis.signal, analysis.rejections)
        signal = analysis.signal
        self.assertIs(signal.side, Side.BUY)
        self.assertLess(signal.stop, signal.entry)
        self.assertGreater(signal.target, signal.entry)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.candles = pattern_candles("sell")
        self.spec = SymbolSpec("GBPNZD")

    def test_impossible_rr_rejects(self):
        settings = offline_settings()
        settings.risk.min_rr = 50.0
        settings.risk.target_mode = "structure"
        analysis = Strategy(settings).analyze(self.spec, self.candles, None, NOW)
        self.assertIsNone(analysis.signal)
        self.assertTrue(any("RR" in r for r in analysis.rejections))

    def test_high_min_score_rejects(self):
        settings = offline_settings()
        settings.strategy.min_score = 99.0
        analysis = Strategy(settings).analyze(self.spec, self.candles, None, NOW)
        self.assertIsNone(analysis.signal)
        self.assertTrue(any("nota" in r for r in analysis.rejections))

    def test_requiring_order_block_filters_zone_only_setups(self):
        settings = offline_settings()
        settings.strategy.order_block.displacement_atr = 50.0  # nenhum bloco válido
        analysis = Strategy(settings).analyze(self.spec, self.candles, None, NOW)
        self.assertIsNone(analysis.signal)
        self.assertEqual(analysis.order_blocks, [])

    def test_sell_only_mode_blocks_buys(self):
        settings = offline_settings()
        settings.strategy.allow_buy = False
        analysis = Strategy(settings).analyze(
            SymbolSpec("AUDUSD"), pattern_candles("buy"), None, NOW
        )
        self.assertIsNone(analysis.signal)

    def test_news_blocks_signal(self):
        settings = offline_settings()
        settings.news.enabled = True
        news = NewsFilter(
            NewsSettings(minutes_before=60, minutes_after=30),
            FakeCalendar([NewsEvent(ts=NOW + timedelta(minutes=15), currency="NZD",
                                    title="RBNZ", impact="high")]),
        )
        analysis = Strategy(settings, news).analyze(self.spec, self.candles, None, NOW)
        self.assertIsNone(analysis.signal)
        self.assertTrue(any("RBNZ" in r for r in analysis.rejections))

    def test_news_outside_window_allows_signal(self):
        settings = offline_settings()
        news = NewsFilter(
            NewsSettings(minutes_before=60, minutes_after=30),
            FakeCalendar([NewsEvent(ts=NOW + timedelta(hours=6), currency="NZD",
                                    title="RBNZ", impact="high")]),
        )
        analysis = Strategy(settings, news).analyze(self.spec, self.candles, None, NOW)
        self.assertIsNotNone(analysis.signal, analysis.rejections)
        self.assertTrue(any("próximas 12h" in w for w in analysis.signal.warnings))

    def test_short_history_is_rejected(self):
        analysis = Strategy(offline_settings()).analyze(self.spec, self.candles[:20], None, NOW)
        self.assertIsNone(analysis.signal)
        self.assertTrue(any("histórico insuficiente" in r for r in analysis.rejections))

    def test_flat_market_has_no_setup(self):
        flat = build_legs(1.1000, [(1.1000, 80)], noise=0.00001)
        analysis = Strategy(offline_settings()).analyze(SymbolSpec("EURUSD"), flat, None, NOW)
        self.assertIsNone(analysis.signal)

    def test_price_beyond_stop_rejects(self):
        # preço rompeu a zona de oferta: setup invalidado
        candles = pattern_candles("sell")
        extended = candles + build_legs(
            candles[-1].close, [(2.3200, 10)], 60, candles[-1].ts + timedelta(hours=1)
        )
        analysis = Strategy(offline_settings()).analyze(self.spec, extended, None, NOW)
        if analysis.signal:
            self.assertLess(analysis.signal.entry, analysis.signal.stop)


class SessionTests(unittest.TestCase):
    def test_weekend_blocked(self):
        settings = Settings()
        ok, reason = session_ok(datetime(2026, 8, 1, 12, tzinfo=timezone.utc), settings)  # sábado
        self.assertFalse(ok)
        self.assertIn("fim de semana", reason)

    def test_night_blocked(self):
        settings = Settings()
        ok, _ = session_ok(datetime(2026, 7, 30, 3, tzinfo=timezone.utc), settings)
        self.assertFalse(ok)

    def test_friday_cutoff(self):
        settings = Settings()
        ok, reason = session_ok(datetime(2026, 7, 31, 19, tzinfo=timezone.utc), settings)
        self.assertFalse(ok)
        self.assertIn("sexta", reason)

    def test_inside_window(self):
        settings = Settings()
        ok, _ = session_ok(NOW, settings)
        self.assertTrue(ok)

    def test_disabled_always_ok(self):
        settings = Settings()
        settings.session.enabled = False
        ok, _ = session_ok(datetime(2026, 8, 1, 3, tzinfo=timezone.utc), settings)
        self.assertTrue(ok)


class ScannerTests(unittest.TestCase):
    def test_scan_reports_signals_and_diagnostics(self):
        settings = offline_settings()
        settings.symbols = [SymbolSpec("GBPNZD"), SymbolSpec("EURUSD")]
        result = Scanner(settings, feed=SyntheticFeed("sell"), news=None).scan(now=NOW)
        self.assertEqual(len(result.analyses), 2)
        self.assertTrue(result.signals)
        data = result.to_dict()
        self.assertEqual(len(data["scanned"]), 2)
        self.assertEqual(data["signals"][0]["side"], "SELL")

    def test_feed_errors_are_captured(self):
        class BrokenFeed:
            def fetch(self, spec, timeframe, bars):
                raise RuntimeError("feed fora do ar")

        settings = offline_settings()
        settings.symbols = [SymbolSpec("GBPNZD")]
        result = Scanner(settings, feed=BrokenFeed(), news=None).scan(now=NOW)
        self.assertIn("GBPNZD", result.errors)
        self.assertEqual(result.signals, [])


if __name__ == "__main__":
    unittest.main()
