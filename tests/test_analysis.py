"""Testes dos blocos de análise: pivots, zonas, order blocks e linhas."""

from __future__ import annotations

import unittest

from robo_forex.analysis.order_blocks import OrderBlockParams, find_order_blocks
from robo_forex.analysis.structure import htf_bias, trend_from_pivots
from robo_forex.analysis.swings import find_pivots, highs, lows
from robo_forex.analysis.trendlines import find_trendlines
from robo_forex.analysis.zones import ZoneParams, active_zones, build_zones, zone_strength
from robo_forex.feeds.synthetic import build_legs, pattern_candles
from robo_forex.indicators import atr
from robo_forex.models import Side, ZoneKind

from .helpers import candle


class PivotTests(unittest.TestCase):
    def test_detects_single_high_and_low(self):
        prices = [1.0, 1.1, 1.3, 1.1, 1.0, 0.8, 0.6, 0.8, 1.0]
        candles = [candle(i, p, p + 0.02, p - 0.02, p) for i, p in enumerate(prices)]
        pivots = find_pivots(candles, 2, 2)
        self.assertEqual([p.index for p in highs(pivots)], [2])
        self.assertEqual([p.index for p in lows(pivots)], [6])

    def test_no_pivots_on_monotonic_series(self):
        candles = [candle(i, 1 + i * 0.01, 1 + i * 0.01, 1 + i * 0.01, 1 + i * 0.01) for i in range(20)]
        self.assertEqual(find_pivots(candles, 2, 2), [])


class ZoneTests(unittest.TestCase):
    def setUp(self):
        self.candles = pattern_candles("sell")
        self.atr = atr(self.candles, 14)
        self.pivots = find_pivots(self.candles, 2, 2)

    def test_supply_line_groups_three_touches(self):
        zones = build_zones(self.candles, self.pivots, ZoneKind.SUPPLY, self.atr)
        self.assertTrue(zones, "nenhuma linha de oferta encontrada")
        best = zones[0]
        self.assertGreaterEqual(best.touches, 3)
        self.assertGreaterEqual(best.rejections, 2)
        self.assertFalse(best.broken)
        self.assertTrue(2.285 <= best.top <= 2.292, best.top)

    def test_zone_needs_minimum_pivots(self):
        strict = ZoneParams(min_pivots=5)
        self.assertEqual(build_zones(self.candles, self.pivots, ZoneKind.SUPPLY, self.atr, strict), [])

    def test_broken_zone_is_flagged(self):
        candles = build_legs(
            1.0000, [(1.0100, 8), (1.0000, 6), (1.0098, 8), (1.0010, 6), (1.0300, 12)]
        )
        atr_value = atr(candles, 14)
        pivots = find_pivots(candles, 2, 2)
        zones = build_zones(candles, pivots, ZoneKind.SUPPLY, atr_value, ZoneParams(min_touches=1))
        broken = [z for z in zones if z.broken]
        self.assertTrue(broken, "zona superada pelo preço deveria estar marcada como rompida")

    def test_active_zones_ignores_zones_behind_price(self):
        zones = build_zones(self.candles, self.pivots, ZoneKind.SUPPLY, self.atr)
        above = active_zones(zones, ZoneKind.SUPPLY, price=9.0)
        self.assertEqual(above, [])

    def test_strength_grows_with_touches(self):
        zones = build_zones(self.candles, self.pivots, ZoneKind.SUPPLY, self.atr)
        strong = zones[0]
        weak = type(strong)(kind=strong.kind, top=strong.top, bottom=strong.bottom, touches=1)
        self.assertGreater(zone_strength(strong, self.atr), zone_strength(weak, self.atr))


class OrderBlockTests(unittest.TestCase):
    def setUp(self):
        self.candles = pattern_candles("sell")
        self.atr = atr(self.candles, 14)
        self.pivots = find_pivots(self.candles, 2, 2)

    def test_finds_bearish_block_with_bos(self):
        blocks = find_order_blocks(self.candles, self.pivots, self.atr)
        sells = [b for b in blocks if b.side is Side.SELL]
        self.assertTrue(sells)
        block = sells[0]
        self.assertTrue(block.bos)
        self.assertGreater(block.displacement_atr, 1.4)
        self.assertGreater(block.top, block.bottom)

    def test_finds_bullish_block_on_demand_pattern(self):
        candles = pattern_candles("buy")
        atr_value = atr(candles, 14)
        pivots = find_pivots(candles, 2, 2)
        buys = [b for b in find_order_blocks(candles, pivots, atr_value) if b.side is Side.BUY]
        self.assertTrue(buys)

    def test_high_displacement_threshold_filters_everything(self):
        params = OrderBlockParams(displacement_atr=50.0)
        self.assertEqual(find_order_blocks(self.candles, self.pivots, self.atr, params), [])

    def test_mitigation_flag(self):
        blocks = find_order_blocks(self.candles, self.pivots, self.atr)
        # o padrão termina retestando a zona, então o bloco mais antigo foi tocado
        self.assertTrue(any(b.mitigated for b in blocks))


class StructureTests(unittest.TestCase):
    def test_uptrend_detected(self):
        candles = build_legs(1.0, [(1.02, 6), (1.01, 5), (1.05, 8), (1.04, 5), (1.09, 8)])
        self.assertEqual(htf_bias(candles), "up")

    def test_downtrend_detected(self):
        candles = build_legs(1.10, [(1.08, 6), (1.09, 5), (1.04, 8), (1.05, 5), (1.00, 8)])
        self.assertEqual(htf_bias(candles), "down")

    def test_neutral_without_enough_pivots(self):
        self.assertEqual(trend_from_pivots([]), "neutral")


class TrendlineTests(unittest.TestCase):
    def test_descending_resistance_found(self):
        candles = pattern_candles("sell")
        atr_value = atr(candles, 14)
        pivots = find_pivots(candles, 2, 2)
        lines = find_trendlines(candles, pivots, atr_value, "resistance")
        self.assertTrue(lines)
        self.assertLess(lines[0].slope, 0)

    def test_support_lines_have_positive_slope(self):
        candles = pattern_candles("buy")
        atr_value = atr(candles, 14)
        pivots = find_pivots(candles, 2, 2)
        for line in find_trendlines(candles, pivots, atr_value, "support"):
            self.assertGreater(line.slope, 0)


if __name__ == "__main__":
    unittest.main()
