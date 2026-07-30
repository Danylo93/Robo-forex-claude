"""Testes do robô de alta frequência."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hft.brokers.oanda import market_order_payload, parse_price, to_instrument
from hft.brokers.paper import PaperBroker
from hft.cli import main
from hft.config import HftSettings, InstrumentSettings
from hft.engine import Engine
from hft.features import FeatureWindow
from hft.models import AccountState, Intent, Position, Side, Tick, Trade
from hft.report import render, summarize, verdict
from hft.risk import RiskManager
from hft.strategies import ImbalanceMomentumStrategy, MeanReversionStrategy, build_strategy
from hft.ticks import read_tick_csv, synthetic_ticks, ticks_from_candles

T0 = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)  # quinta, dentro da sessão


def tick(offset_s: float = 0.0, bid: float = 1.09995, ask: float = 1.10005, bs=1e6, asz=1e6):
    return Tick(ts=T0 + timedelta(seconds=offset_s), bid=bid, ask=ask, bid_size=bs, ask_size=asz)


def offline_settings(**over) -> HftSettings:
    settings = HftSettings()
    settings.risk.news_blackout = False
    settings.strategy.warmup_ticks = 30
    settings.strategy.window_ticks = 30
    for key, value in over.items():
        section, _, field = key.partition(".")
        setattr(getattr(settings, section), field, value)
    return settings


class TickTests(unittest.TestCase):
    def test_mid_and_spread(self):
        t = tick(bid=1.1000, ask=1.1002)
        self.assertAlmostEqual(t.mid, 1.1001)
        self.assertAlmostEqual(t.spread, 0.0002)

    def test_microprice_leans_to_heavier_side(self):
        t = tick(bid=1.1000, ask=1.1002, bs=3e6, asz=1e6)
        self.assertGreater(t.microprice, t.mid)  # pressão compradora

    def test_imbalance_bounds(self):
        self.assertAlmostEqual(tick(bs=1e6, asz=0).imbalance, 1.0)
        self.assertAlmostEqual(tick(bs=0, asz=1e6).imbalance, -1.0)
        self.assertAlmostEqual(tick(bs=0, asz=0).imbalance, 0.0)

    def test_price_for_side(self):
        t = tick(bid=1.1000, ask=1.1002)
        self.assertAlmostEqual(t.price_for(Side.BUY), 1.1002)
        self.assertAlmostEqual(t.price_for(Side.SELL), 1.1000)


class AccountTests(unittest.TestCase):
    def make_trade(self, net: float) -> Trade:
        return Trade(
            symbol="EURUSD", side=Side.BUY, lots=0.1, opened_at=T0, closed_at=T0,
            entry=1.1, exit=1.1, gross_pnl=net, commission=0.0, net_pnl=net, pips=net,
            exit_reason="alvo",
        )

    def test_register_updates_balance_and_streak(self):
        account = AccountState(balance=1000.0)
        account.register(self.make_trade(10.0))
        account.register(self.make_trade(-4.0))
        account.register(self.make_trade(-6.0))
        self.assertAlmostEqual(account.balance, 1000.0)
        self.assertEqual(account.consecutive_losses, 2)
        self.assertAlmostEqual(account.peak_equity, 1010.0)
        self.assertAlmostEqual(account.drawdown, 10.0)

    def test_roll_day_resets_daily_pnl(self):
        account = AccountState(balance=1000.0)
        account.register(self.make_trade(-20.0))
        self.assertAlmostEqual(account.day_pnl, -20.0)
        account.roll_day()
        self.assertAlmostEqual(account.day_pnl, 0.0)
        self.assertAlmostEqual(account.net_pnl, -20.0)

    def test_position_unrealized(self):
        position = Position(
            side=Side.BUY, entry=1.1000, lots=1.0, opened_at=T0,
            take_profit=1.1010, stop_loss=1.0990, max_hold_seconds=60,
        )
        value = position.unrealized(tick(bid=1.1005, ask=1.1006), 0.0001, 10.0)
        self.assertAlmostEqual(value, 50.0, places=6)  # 5 pips x 10


class ConfigTests(unittest.TestCase):
    def test_jpy_pip_autodetect(self):
        self.assertAlmostEqual(InstrumentSettings(symbol="USDJPY").pip, 0.01)

    def test_round_lots_respects_step_and_bounds(self):
        instrument = InstrumentSettings(min_lots=0.01, lot_step=0.01, max_lots=2.0)
        self.assertAlmostEqual(instrument.round_lots(0.234), 0.23)
        self.assertAlmostEqual(instrument.round_lots(0.001), 0.01)
        self.assertAlmostEqual(instrument.round_lots(50.0), 2.0)

    def test_cost_per_trade_pips(self):
        settings = HftSettings()
        settings.costs.commission_per_lot_per_side = 3.5
        settings.costs.extra_slippage_pips = 0.1
        settings.instrument.pip_value_per_lot = 10.0
        # 0.6 spread + 0.7 comissão + 0.2 derrapagem
        self.assertAlmostEqual(settings.cost_per_trade_pips(0.6), 1.5)

    def test_load_and_unknown_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            path.write_text(json.dumps({"risk": {"max_spread_pips": 0.4}}), encoding="utf-8")
            self.assertAlmostEqual(HftSettings.load(path).risk.max_spread_pips, 0.4)
            path.write_text(json.dumps({"risk": {"xpto": 1}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                HftSettings.load(path)


class FeatureTests(unittest.TestCase):
    def test_not_ready_before_warmup(self):
        window = FeatureWindow(size=10, warmup=20, pip=0.0001)
        for i in range(5):
            features = window.update(tick(i))
        self.assertFalse(features.ready)

    def test_z_score_negative_after_drop(self):
        window = FeatureWindow(size=20, warmup=20, pip=0.0001)
        for i in range(20):
            window.update(tick(i, bid=1.1000 + i * 1e-5, ask=1.1001 + i * 1e-5))
        features = window.update(tick(21, bid=1.0980, ask=1.0981))
        self.assertTrue(features.ready)
        self.assertLess(features.z_score, -1.0)
        self.assertLess(features.momentum_bps, 0)

    def test_spread_in_pips(self):
        window = FeatureWindow(size=10, warmup=1, pip=0.0001)
        features = window.update(tick(bid=1.1000, ask=1.1002))
        self.assertAlmostEqual(features.spread_pips, 2.0, places=6)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.settings = offline_settings()
        self.window = FeatureWindow(20, 20, 0.0001)
        for i in range(25):
            self.window.update(tick(i))

    def features_with(self, **over):
        features = self.window.compute()
        for key, value in over.items():
            setattr(features, key, value)
        return features

    def test_mean_reversion_buys_oversold(self):
        strategy = MeanReversionStrategy(self.settings)
        features = self.features_with(z_score=-3.0, volatility_bps=1.0, imbalance=0.0, ready=True)
        intent = strategy.entry(tick(), features)
        self.assertIsNotNone(intent)
        self.assertIs(intent.side, Side.BUY)

    def test_mean_reversion_sells_overbought(self):
        strategy = MeanReversionStrategy(self.settings)
        features = self.features_with(z_score=3.0, volatility_bps=1.0, imbalance=0.0, ready=True)
        self.assertIs(strategy.entry(tick(), features).side, Side.SELL)

    def test_mean_reversion_skips_when_book_confirms_move(self):
        strategy = MeanReversionStrategy(self.settings)
        features = self.features_with(z_score=-3.0, imbalance=-0.9, ready=True)
        self.assertIsNone(strategy.entry(tick(), features))

    def test_mean_reversion_ignores_not_ready(self):
        strategy = MeanReversionStrategy(self.settings)
        self.assertIsNone(strategy.entry(tick(), self.features_with(z_score=-9.0, ready=False)))

    def test_mean_reversion_exit_on_return(self):
        strategy = MeanReversionStrategy(self.settings)
        position = Position(Side.BUY, 1.1, 0.1, T0, 1.1002, 1.0998, 60)
        self.assertIsNotNone(strategy.exit(tick(), self.features_with(z_score=0.0), position))
        self.assertIsNone(strategy.exit(tick(), self.features_with(z_score=-2.0), position))

    def test_momentum_follows_flow(self):
        strategy = ImbalanceMomentumStrategy(self.settings)
        features = self.features_with(imbalance=0.6, momentum_bps=2.0, ready=True)
        self.assertIs(strategy.entry(tick(), features).side, Side.BUY)
        features = self.features_with(imbalance=-0.6, momentum_bps=-2.0, ready=True)
        self.assertIs(strategy.entry(tick(), features).side, Side.SELL)

    def test_momentum_exit_when_flow_flips(self):
        strategy = ImbalanceMomentumStrategy(self.settings)
        position = Position(Side.BUY, 1.1, 0.1, T0, 1.1002, 1.0998, 60)
        self.assertEqual(
            strategy.exit(tick(), self.features_with(imbalance=-0.9), position), "fluxo inverteu"
        )

    def test_unknown_strategy_raises(self):
        settings = offline_settings()
        settings.strategy.name = "nao_existe"
        with self.assertRaises(ValueError):
            build_strategy(settings)


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.settings = offline_settings()
        self.settings.strategy.min_edge_multiple = 0.0
        self.settings.strategy.cooldown_seconds = 0.0
        self.account = AccountState(balance=1000.0)
        self.risk = RiskManager(self.settings, self.account)
        self.window = FeatureWindow(20, 1, 0.0001)
        self.window.update(tick())
        self.intent = Intent(Side.BUY, 1.0, 2.0, 60.0, "teste", edge_bps=100.0)

    def features(self, **over):
        features = self.window.compute()
        features.ready = True
        for key, value in over.items():
            setattr(features, key, value)
        return features

    def test_allows_normal_conditions(self):
        decision = self.risk.check(tick(), self.features(spread_pips=0.5), self.intent)
        self.assertTrue(decision.allowed, decision.reason)
        self.assertGreater(decision.lots, 0)

    def test_blocks_wide_spread(self):
        decision = self.risk.check(tick(), self.features(spread_pips=5.0), self.intent)
        self.assertFalse(decision.allowed)
        self.assertIn("spread", decision.reason)

    def test_blocks_weekend(self):
        saturday = Tick(ts=datetime(2026, 8, 1, 10, tzinfo=timezone.utc), bid=1.1, ask=1.1001)
        decision = self.risk.check(saturday, self.features(spread_pips=0.5), self.intent)
        self.assertFalse(decision.allowed)
        self.assertIn("fim de semana", decision.reason)

    def test_blocks_outside_session(self):
        night = Tick(ts=datetime(2026, 7, 30, 3, tzinfo=timezone.utc), bid=1.1, ask=1.1001)
        self.assertFalse(self.risk.check(night, self.features(spread_pips=0.5), self.intent).allowed)

    def test_cooldown(self):
        self.settings.strategy.cooldown_seconds = 30.0
        self.risk.last_trade_at = T0
        decision = self.risk.check(tick(5), self.features(spread_pips=0.5), self.intent)
        self.assertFalse(decision.allowed)
        self.assertIn("intervalo", decision.reason)

    def test_trades_per_hour_cap(self):
        self.settings.risk.max_trades_per_hour = 2
        self.risk.trade_stamps = [T0, T0]
        decision = self.risk.check(tick(60), self.features(spread_pips=0.5), self.intent)
        self.assertFalse(decision.allowed)
        self.assertIn("hora", decision.reason)

    def test_edge_gate_blocks_thin_edge(self):
        self.settings.strategy.min_edge_multiple = 1.5
        weak = Intent(Side.BUY, 1.0, 2.0, 60.0, "fraco", edge_bps=0.01)
        decision = self.risk.check(tick(), self.features(spread_pips=0.6), weak)
        self.assertFalse(decision.allowed)
        self.assertIn("vantagem", decision.reason)

    def test_sizing_by_risk_percent(self):
        self.settings.risk.risk_percent_per_trade = 1.0  # 10 na conta de 1000
        self.settings.risk.fixed_lots = 0.0
        intent = Intent(Side.BUY, 1.0, 2.0, 60.0, "teste", edge_bps=100.0)
        # 10 de risco / (2 pips x 10 por pip) = 0.5 lote
        self.assertAlmostEqual(self.risk.size(intent), 0.5)

    def test_fixed_lots_wins(self):
        self.settings.risk.fixed_lots = 0.03
        self.assertAlmostEqual(self.risk.size(self.intent), 0.03)

    def test_daily_loss_halts(self):
        self.settings.risk.max_daily_loss_percent = 1.0  # 10 na conta de 1000
        self.risk.register(self._loss(-12.0))
        self.assertTrue(self.account.halted)
        self.assertIn("perda diária", self.account.halt_reason)
        self.assertFalse(self.risk.check(tick(), self.features(), self.intent).allowed)

    def test_consecutive_losses_halt(self):
        self.settings.risk.max_consecutive_losses = 2
        self.settings.risk.max_daily_loss_percent = 90.0
        self.settings.risk.max_drawdown_percent = 90.0
        self.risk.register(self._loss(-1.0))
        self.risk.register(self._loss(-1.0))
        self.assertTrue(self.account.halted)
        self.assertIn("perdas seguidas", self.account.halt_reason)

    def test_drawdown_halt(self):
        self.settings.risk.max_daily_loss_percent = 90.0
        self.settings.risk.max_drawdown_percent = 2.0
        self.risk.register(self._loss(-25.0))
        self.assertTrue(self.account.halted)
        self.assertIn("drawdown", self.account.halt_reason)

    def test_new_day_resumes(self):
        self.settings.risk.max_daily_loss_percent = 1.0
        self.risk.check(tick(), self.features(), self.intent)  # fixa o dia atual
        self.risk.register(self._loss(-12.0))
        self.assertTrue(self.account.halted)
        tomorrow = Tick(ts=T0 + timedelta(days=1), bid=1.1, ask=1.10005)
        decision = self.risk.check(tomorrow, self.features(spread_pips=0.5), self.intent)
        self.assertFalse(self.account.halted)
        self.assertTrue(decision.allowed, decision.reason)

    def test_news_blackout(self):
        from robo_forex.config import NewsSettings, SymbolSpec
        from robo_forex.models import NewsEvent
        from robo_forex.news import NewsFilter

        class Calendar:
            def events(self):
                return [NewsEvent(ts=T0 + timedelta(minutes=5), currency="EUR",
                                  title="CPI", impact="high")]

        self.settings.risk.news_blackout = True
        self.risk.news_filter = NewsFilter(NewsSettings(), Calendar())
        self.risk.news_spec = SymbolSpec("EURUSD")
        decision = self.risk.check(tick(), self.features(spread_pips=0.5), self.intent)
        self.assertFalse(decision.allowed)
        self.assertIn("CPI", decision.reason)

    def _loss(self, amount: float) -> Trade:
        return Trade(
            symbol="EURUSD", side=Side.BUY, lots=0.1, opened_at=T0, closed_at=T0 + timedelta(1),
            entry=1.1, exit=1.1, gross_pnl=amount, commission=0.0, net_pnl=amount,
            pips=amount, exit_reason="stop",
        )


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.settings = offline_settings()
        self.settings.costs.extra_slippage_pips = 0.1
        self.settings.costs.commission_per_lot_per_side = 3.5
        self.broker = PaperBroker(self.settings)

    def test_buy_pays_ask_plus_slippage(self):
        fill = self.broker.open(Side.BUY, 1.0, tick(bid=1.1000, ask=1.1002), 0, 0)
        self.assertAlmostEqual(fill.price, 1.1002 + 0.1 * 0.0001, places=8)
        self.assertAlmostEqual(fill.commission, 3.5)

    def test_sell_receives_bid_minus_slippage(self):
        fill = self.broker.open(Side.SELL, 1.0, tick(bid=1.1000, ask=1.1002), 0, 0)
        self.assertAlmostEqual(fill.price, 1.1000 - 0.1 * 0.0001, places=8)

    def test_close_uses_opposite_side(self):
        position = Position(Side.BUY, 1.1, 0.5, T0, 1.1002, 1.0998, 60)
        fill = self.broker.close(position, tick(bid=1.1000, ask=1.1002), "alvo")
        self.assertIs(fill.side, Side.SELL)
        self.assertAlmostEqual(fill.commission, 3.5 * 0.5)

    def test_commission_accumulates(self):
        self.broker.open(Side.BUY, 1.0, tick(), 0, 0)
        self.broker.open(Side.BUY, 1.0, tick(), 0, 0)
        self.assertAlmostEqual(self.broker.commission_paid, 7.0)


class EngineTests(unittest.TestCase):
    def make_engine(self, **over):
        settings = offline_settings(**over)
        settings.strategy.min_edge_multiple = 0.0
        settings.strategy.cooldown_seconds = 0.0
        settings.costs.latency_ms = 0.0
        settings.risk.fixed_lots = 0.1
        return Engine(settings, PaperBroker(settings), build_strategy(settings)), settings

    def test_take_profit_closes_with_profit(self):
        engine, settings = self.make_engine()
        engine.window.seen = 999
        engine.position = Position(
            side=Side.BUY, entry=1.1000, lots=1.0, opened_at=T0,
            take_profit=1.1010, stop_loss=1.0990, max_hold_seconds=600,
        )
        trade = engine.on_tick(tick(1, bid=1.1012, ask=1.1013))
        self.assertIsNotNone(trade)
        self.assertEqual(trade.exit_reason, "alvo")
        self.assertGreater(trade.gross_pnl, 0)

    def test_stop_loss_closes_with_loss(self):
        engine, _ = self.make_engine()
        engine.position = Position(
            side=Side.BUY, entry=1.1000, lots=1.0, opened_at=T0,
            take_profit=1.1010, stop_loss=1.0990, max_hold_seconds=600,
        )
        trade = engine.on_tick(tick(1, bid=1.0985, ask=1.0986))
        self.assertEqual(trade.exit_reason, "stop")
        self.assertLess(trade.net_pnl, 0)

    def test_timeout_closes(self):
        engine, _ = self.make_engine()
        engine.position = Position(
            side=Side.SELL, entry=1.1000, lots=1.0, opened_at=T0,
            take_profit=1.0990, stop_loss=1.1010, max_hold_seconds=5,
        )
        trade = engine.on_tick(tick(30, bid=1.1000, ask=1.1001))
        self.assertEqual(trade.exit_reason, "tempo máximo")

    def test_latency_delays_execution(self):
        engine, settings = self.make_engine()
        settings.costs.latency_ms = 500.0
        engine.pending = None
        intent = Intent(Side.BUY, 1.0, 2.0, 60.0, "teste", edge_bps=100.0)
        engine.strategy = type("S", (), {
            "entry": lambda self, t, f: intent,
            "exit": lambda self, t, f, p: None,
        })()
        engine.window.seen = 999
        engine.on_tick(tick(0))
        self.assertIsNotNone(engine.pending)
        self.assertIsNone(engine.position)
        engine.on_tick(tick(0.2))  # ainda dentro da latência
        self.assertIsNone(engine.position)
        engine.on_tick(tick(0.6))  # latência cumprida
        self.assertIsNotNone(engine.position)

    def test_full_run_on_synthetic_ticks(self):
        engine, settings = self.make_engine()
        settings.strategy.entry_z = 1.5
        settings.risk.max_spread_pips = 5.0
        ticks = synthetic_ticks(
            count=6000, pip=0.0001, spread_pips=0.2, reversion=0.05, seed=3, start=T0
        )
        trades = engine.run(ticks)
        self.assertGreater(len(trades), 0)
        self.assertEqual(engine.position, None)
        for trade in trades:
            self.assertAlmostEqual(trade.net_pnl, trade.gross_pnl - trade.commission, places=9)

    def test_halted_engine_stops_entering(self):
        engine, settings = self.make_engine()
        engine.account.halted = True
        engine.account.halt_reason = "teste"
        ticks = synthetic_ticks(count=500, spread_pips=0.2, reversion=0.05, seed=5, start=T0)
        engine.run(ticks)
        self.assertEqual(engine.account.trades, [])

    def test_events_are_emitted(self):
        events = []
        settings = offline_settings()
        settings.strategy.min_edge_multiple = 0.0
        settings.risk.fixed_lots = 0.1
        settings.risk.max_spread_pips = 5.0
        settings.strategy.entry_z = 1.5
        settings.costs.latency_ms = 0.0
        engine = Engine(
            settings, PaperBroker(settings), build_strategy(settings),
            on_event=lambda e, d: events.append(e),
        )
        engine.run(synthetic_ticks(count=3000, spread_pips=0.2, reversion=0.05, seed=3, start=T0))
        self.assertIn("entrada", events)
        self.assertIn("saida", events)


class OandaHelperTests(unittest.TestCase):
    def test_instrument_format(self):
        self.assertEqual(to_instrument("EURUSD"), "EUR_USD")
        self.assertEqual(to_instrument("EUR_USD"), "EUR_USD")
        self.assertEqual(to_instrument("SPX500"), "SPX500")

    def test_payload_buy_with_tp_sl(self):
        payload = market_order_payload("EUR_USD", 10_000, 1.1050, 1.0950, 5)
        order = payload["order"]
        self.assertEqual(order["units"], "10000")
        self.assertEqual(order["takeProfitOnFill"]["price"], "1.10500")
        self.assertEqual(order["stopLossOnFill"]["price"], "1.09500")

    def test_payload_sell_without_levels(self):
        order = market_order_payload("EUR_USD", -5000, 0.0, 0.0, 5)["order"]
        self.assertEqual(order["units"], "-5000")
        self.assertNotIn("takeProfitOnFill", order)

    def test_parse_price(self):
        payload = {
            "prices": [
                {
                    "time": "2026-07-30T10:00:00.000000000Z",
                    "bids": [{"price": "1.10000", "liquidity": 10_000_000}],
                    "asks": [{"price": "1.10012", "liquidity": 5_000_000}],
                }
            ]
        }
        parsed = parse_price(payload)
        self.assertAlmostEqual(parsed.bid, 1.10000)
        self.assertAlmostEqual(parsed.ask, 1.10012)
        self.assertGreater(parsed.imbalance, 0)

    def test_parse_price_empty(self):
        self.assertIsNone(parse_price({"prices": []}))


class TickSourceTests(unittest.TestCase):
    def test_synthetic_is_deterministic(self):
        a = synthetic_ticks(count=100, seed=11)
        b = synthetic_ticks(count=100, seed=11)
        self.assertEqual([t.bid for t in a], [t.bid for t in b])
        self.assertNotEqual([t.bid for t in a], [t.bid for t in synthetic_ticks(count=100, seed=12)])

    def test_synthetic_spread_is_respected(self):
        ticks = synthetic_ticks(count=50, pip=0.0001, spread_pips=1.0)
        for t in ticks:
            self.assertAlmostEqual(t.spread, 0.0001, places=7)
            self.assertGreater(t.ask, t.bid)

    def test_book_informativeness_zero_has_no_bias(self):
        ticks = synthetic_ticks(count=2000, book_informativeness=0.0, seed=4)
        avg = sum(t.imbalance for t in ticks) / len(ticks)
        self.assertLess(abs(avg), 0.1)

    def test_read_tick_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ticks.csv"
            path.write_text(
                "time,bid,ask,bid_size,ask_size\n"
                "2026-07-30 10:00:00,1.1000,1.1001,100,200\n"
                "2026-07-30 10:00:01,1.1002,1.1003,150,150\n",
                encoding="utf-8",
            )
            ticks = read_tick_csv(path)
        self.assertEqual(len(ticks), 2)
        self.assertAlmostEqual(ticks[0].ask, 1.1001)
        self.assertLess(ticks[0].imbalance, 0)

    def test_read_tick_csv_requires_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("time,price\n2026-07-30 10:00:00,1.1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_tick_csv(path)

    def test_ticks_from_candles_respects_extremes(self):
        from robo_forex.models import Candle

        candles = [
            Candle(ts=T0, open=1.1000, high=1.1010, low=1.0990, close=1.1005),
            Candle(ts=T0 + timedelta(minutes=1), open=1.1005, high=1.1008, low=1.1000, close=1.1002),
        ]
        ticks = list(ticks_from_candles(candles, pip=0.0001, spread_pips=0.0, per_candle=8))
        mids = [t.mid for t in ticks]
        self.assertLessEqual(max(mids), 1.1010 + 1e-9)
        self.assertGreaterEqual(min(mids), 1.0990 - 1e-9)
        self.assertTrue(all(a.ts <= b.ts for a, b in zip(ticks, ticks[1:])))


class ReportTests(unittest.TestCase):
    def build(self):
        settings = offline_settings()
        settings.strategy.min_edge_multiple = 0.0
        settings.strategy.entry_z = 1.5
        settings.risk.fixed_lots = 0.1
        settings.risk.max_spread_pips = 5.0
        settings.costs.latency_ms = 0.0
        engine = Engine(settings, PaperBroker(settings), build_strategy(settings))
        engine.run(synthetic_ticks(count=4000, spread_pips=0.2, reversion=0.05, seed=3, start=T0))
        return engine, settings

    def test_summary_fields(self):
        engine, settings = self.build()
        report = summarize(engine, settings)
        self.assertGreater(report.count, 0)
        self.assertAlmostEqual(
            report.net_pnl, report.gross_pnl - report.commission, places=6
        )
        data = report.to_dict()
        self.assertIn("net_pnl", data)
        self.assertIn("exits", data)

    def test_render_has_cost_section(self):
        engine, settings = self.build()
        text = render(summarize(engine, settings), settings, 0.2)
        self.assertIn("Decomposição do resultado", text)
        self.assertIn("Custo por operação", text)

    def test_verdict_small_sample(self):
        engine, settings = self.build()
        report = summarize(engine, settings)
        report.trades = report.trades[:5]
        self.assertIn("AMOSTRA PEQUENA", verdict(report, 1.5))

    def test_verdict_negative(self):
        engine, settings = self.build()
        report = summarize(engine, settings)
        for trade in report.trades:
            trade.net_pnl = -1.0
        report.trades = report.trades * 10
        self.assertIn("NÃO PAGA", verdict(report, 1.5))


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_backtest_runs(self):
        code, out = self.run_cli(
            ["backtest", "--count", "3000", "--reversion", "0.05", "--tick-spread", "0.2",
             "--no-news", "--commission", "0", "--lots", "0.1"]
        )
        self.assertEqual(code, 0)
        self.assertIn("ROBÔ HFT", out)
        self.assertIn("Custo por operação", out)

    def test_validate_sweep(self):
        code, out = self.run_cli(
            ["validate", "--count", "2500", "--spreads", "0.0,1.0", "--reversion", "0.05",
             "--no-news", "--commission", "0", "--lots", "0.1"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Ponto de equilíbrio", out)

    def test_live_refuses_paper_provider(self):
        code, _ = self.run_cli(["live", "--seconds", "1"])
        self.assertEqual(code, 2)

    def test_live_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            path.write_text(
                json.dumps({"broker": {"provider": "oanda", "mode": "live"}}), encoding="utf-8"
            )
            code, _ = self.run_cli(["-c", str(path), "live", "--seconds", "1"])
        self.assertEqual(code, 2)

    def test_init_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hft.json"
            code, _ = self.run_cli(["init-config", "--out", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("strategy", json.loads(path.read_text(encoding="utf-8")))

    def test_bad_config_returns_2(self):
        code, _ = self.run_cli(["-c", "/tmp/nao-existe-hft.json", "backtest"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
