"""Testes de entrada, stop, alvo, RR e dimensionamento."""

from __future__ import annotations

import unittest

from robo_forex.config import RiskSettings, SymbolSpec
from robo_forex.models import Side
from robo_forex.risk import (
    entry_from_zone,
    pick_target,
    pip_value_per_lot,
    rr_of,
    size_position,
    stop_from_zone,
)


class LevelTests(unittest.TestCase):
    def test_sell_stop_above_zone(self):
        cfg = RiskSettings(stop_buffer_atr=0.25)
        self.assertAlmostEqual(stop_from_zone(Side.SELL, 1.1000, 0.0040, cfg), 1.1010)

    def test_buy_stop_below_zone(self):
        cfg = RiskSettings(stop_buffer_atr=0.5)
        self.assertAlmostEqual(stop_from_zone(Side.BUY, 1.1000, 0.0040, cfg), 1.0980)

    def test_entry_offset_pushes_order_into_zone(self):
        cfg = RiskSettings(entry_offset_atr=0.5)
        self.assertAlmostEqual(entry_from_zone(Side.SELL, 1.1000, 0.0040, cfg), 1.1020)
        self.assertAlmostEqual(entry_from_zone(Side.BUY, 1.1000, 0.0040, cfg), 1.0980)

    def test_rr_calculation(self):
        self.assertAlmostEqual(rr_of(Side.SELL, 1.1000, 1.1010, 1.0970), 3.0)
        self.assertAlmostEqual(rr_of(Side.BUY, 1.1000, 1.0990, 1.1030), 3.0)
        self.assertEqual(rr_of(Side.BUY, 1.1, 1.1, 1.2), 0.0)


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskSettings(min_rr=3.0, target_padding_atr=0.0)

    def test_nearest_structure_meeting_min_rr_is_used(self):
        candidates = [(1.0990, "fundo raso"), (1.0950, "fundo bom"), (1.0800, "fundo distante")]
        choice = pick_target(Side.SELL, 1.1000, 1.1010, candidates, 0.0030, self.cfg)
        self.assertIsNotNone(choice)
        self.assertAlmostEqual(choice.price, 1.0950)
        self.assertAlmostEqual(choice.rr, 5.0)
        self.assertIn("fundo bom", choice.basis)

    def test_falls_back_to_rr_target(self):
        choice = pick_target(Side.SELL, 1.1000, 1.1010, [(1.0995, "raso")], 0.0030, self.cfg)
        self.assertIsNotNone(choice)
        self.assertAlmostEqual(choice.price, 1.0970)
        self.assertIn("RR fixo", choice.basis)

    def test_structure_only_mode_can_reject(self):
        cfg = RiskSettings(min_rr=3.0, target_mode="structure", target_padding_atr=0.0)
        self.assertIsNone(pick_target(Side.SELL, 1.1000, 1.1010, [(1.0995, "raso")], 0.003, cfg))

    def test_rr_mode_ignores_structure(self):
        cfg = RiskSettings(min_rr=2.0, target_mode="rr")
        choice = pick_target(Side.BUY, 1.1000, 1.0990, [(1.2000, "topo")], 0.0030, cfg)
        self.assertAlmostEqual(choice.price, 1.1020)

    def test_wrong_side_candidates_are_ignored(self):
        choice = pick_target(Side.SELL, 1.1000, 1.1010, [(1.2000, "acima")], 0.0030, self.cfg)
        self.assertIn("RR fixo", choice.basis)  # candidato acima da entrada é descartado


class SizingTests(unittest.TestCase):
    def test_pip_value_quote_is_account_currency(self):
        value, warning = pip_value_per_lot(SymbolSpec("EURUSD"), 1.10, "USD")
        self.assertAlmostEqual(value, 10.0)
        self.assertIsNone(warning)

    def test_pip_value_base_is_account_currency(self):
        value, warning = pip_value_per_lot(SymbolSpec("USDJPY"), 150.0, "USD")
        self.assertAlmostEqual(value, 1000.0 / 150.0)
        self.assertIsNone(warning)

    def test_pip_value_cross_pair_warns_without_rate(self):
        value, warning = pip_value_per_lot(SymbolSpec("GBPNZD"), 2.29, "USD")
        self.assertAlmostEqual(value, 10.0)
        self.assertIsNotNone(warning)

    def test_pip_value_cross_pair_uses_conversion(self):
        value, warning = pip_value_per_lot(SymbolSpec("GBPNZD"), 2.29, "USD", conversion_rate=0.58)
        self.assertAlmostEqual(value, 5.8)
        self.assertIsNone(warning)

    def test_position_respects_risk_percent(self):
        cfg = RiskSettings(account_balance=10_000.0, risk_percent=1.0)
        position, warnings = size_position(SymbolSpec("EURUSD"), 1.1000, 1.1020, cfg)
        self.assertAlmostEqual(position.risk_amount, 100.0)
        self.assertAlmostEqual(position.stop_pips, 20.0, places=6)
        self.assertAlmostEqual(position.lots, 0.5)
        self.assertEqual(warnings, [])

    def test_jpy_pair_uses_two_decimal_pip(self):
        spec = SymbolSpec("USDJPY")
        self.assertAlmostEqual(spec.pip, 0.01)
        self.assertEqual(spec.digits, 3)

    def test_index_spec_defaults(self):
        spec = SymbolSpec("HK50", kind="index")
        self.assertAlmostEqual(spec.pip, 1.0)
        self.assertEqual(spec.feed_symbol, "HK50")


if __name__ == "__main__":
    unittest.main()
