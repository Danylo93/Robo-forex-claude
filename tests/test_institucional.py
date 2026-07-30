"""Testes da camada institucional: livro L2, market making, latência e runtime."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hft.cli import main
from hft.config import HftSettings
from hft.latency import Histogram, LatencyTracker
from hft.lowlat import ObjectPool, RingBuffer, TradingRuntime, busy_poll, spin_wait
from hft.marketmaker import (
    MakerFill,
    MarketMaker,
    MarketMakerParams,
    QuotingEngine,
    avellaneda_stoikov,
    render_maker_report,
)
from hft.models import Side, Tick
from hft.orderbook import CHANGE, DELETE, NEW, OrderBook
from hft.ticks import synthetic_ticks

T0 = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)


def book_with_levels(depth: int = 10) -> OrderBook:
    book = OrderBook(depth=depth)
    book.snapshot(
        bids=[(1.1000, 1_000_000), (1.0999, 2_000_000), (1.0998, 3_000_000)],
        asks=[(1.1002, 1_500_000), (1.1003, 2_500_000), (1.1004, 500_000)],
        ts=T0,
    )
    return book


class OrderBookTests(unittest.TestCase):
    def test_snapshot_orders_levels(self):
        book = book_with_levels()
        self.assertEqual([p for p, _ in book.bids], [1.1000, 1.0999, 1.0998])
        self.assertEqual([p for p, _ in book.asks], [1.1002, 1.1003, 1.1004])
        self.assertTrue(book.valid)

    def test_top_of_book_metrics(self):
        book = book_with_levels()
        self.assertAlmostEqual(book.mid, 1.1001)
        self.assertAlmostEqual(book.spread, 0.0002)
        self.assertLess(book.microprice, book.mid)  # mais volume no ask empurra para baixo

    def test_apply_new_change_delete(self):
        book = book_with_levels()
        book.apply(Side.BUY, 1.1001, 900_000, NEW)
        self.assertEqual(book.best_bid[0], 1.1001)
        book.apply(Side.BUY, 1.1001, 100_000, CHANGE)
        self.assertAlmostEqual(book.best_bid[1], 100_000)
        book.apply(Side.BUY, 1.1001, 0, DELETE)
        self.assertEqual(book.best_bid[0], 1.1000)

    def test_zero_size_removes_level(self):
        book = book_with_levels()
        book.apply(Side.SELL, 1.1002, 0, CHANGE)
        self.assertAlmostEqual(book.best_ask[0], 1.1003)

    def test_imbalance_sign(self):
        book = book_with_levels()
        # bids somam 6M contra 4,5M nos asks -> pressão compradora
        self.assertAlmostEqual(book.imbalance(3), (6.0 - 4.5) / 10.5)
        book.apply(Side.SELL, 1.1002, 20_000_000, CHANGE)
        self.assertLess(book.imbalance(3), 0)

    def test_sweep_and_slippage(self):
        book = book_with_levels()
        average, filled = book.sweep(Side.BUY, 2_000_000)
        self.assertAlmostEqual(filled, 2_000_000)
        expected = (1_500_000 * 1.1002 + 500_000 * 1.1003) / 2_000_000
        self.assertAlmostEqual(average, expected)
        self.assertGreater(book.slippage(Side.BUY, 2_000_000), 0)

    def test_sweep_beyond_depth_returns_partial(self):
        book = book_with_levels()
        _, filled = book.sweep(Side.BUY, 99_000_000)
        self.assertAlmostEqual(filled, 4_500_000)

    def test_empty_book_is_invalid(self):
        book = OrderBook()
        self.assertFalse(book.valid)
        self.assertIsNone(book.to_tick())
        self.assertEqual(book.mid, 0.0)
        self.assertEqual(book.imbalance(), 0.0)

    def test_crossed_book_is_invalid(self):
        book = OrderBook()
        book.snapshot(bids=[(1.1005, 100)], asks=[(1.1000, 100)])
        self.assertFalse(book.valid)

    def test_to_tick(self):
        tick = book_with_levels().to_tick()
        self.assertAlmostEqual(tick.bid, 1.1000)
        self.assertAlmostEqual(tick.ask, 1.1002)
        self.assertAlmostEqual(tick.bid_size, 1_000_000)

    def test_depth_limit_applied(self):
        book = OrderBook(depth=2)
        book.snapshot(
            bids=[(1.10, 1), (1.09, 1), (1.08, 1)], asks=[(1.11, 1), (1.12, 1), (1.13, 1)]
        )
        self.assertEqual(len(book.bids), 2)
        self.assertEqual(len(book.asks), 2)

    def test_queue_ahead(self):
        book = book_with_levels()
        self.assertAlmostEqual(book.queue_ahead(Side.BUY, 1.1000), 1_000_000)
        self.assertAlmostEqual(book.queue_ahead(Side.BUY, 1.5), 0.0)

    def test_clear(self):
        book = book_with_levels()
        book.clear()
        self.assertFalse(book.valid)


class MarketMakerTests(unittest.TestCase):
    def setUp(self):
        self.settings = HftSettings()
        self.params = MarketMakerParams(
            base_half_spread_pips=0.5, max_skew_pips=1.0, max_inventory_lots=0.5, vol_widen=0.0
        )
        self.maker = MarketMaker(self.settings, self.params)

    def test_flat_inventory_quotes_around_mid(self):
        quote = self.maker.quote(mid=1.1000, sigma=0.0, inventory_lots=0.0)
        self.assertAlmostEqual(quote.reservation, 1.1000)
        self.assertAlmostEqual(quote.bid, 1.1000 - 0.5 * 0.0001)
        self.assertAlmostEqual(quote.ask, 1.1000 + 0.5 * 0.0001)

    def test_long_inventory_skews_quotes_down(self):
        quote = self.maker.quote(mid=1.1000, sigma=0.0, inventory_lots=0.25)
        self.assertLess(quote.reservation, 1.1000)  # empurra o estoque para fora
        self.assertLess(quote.skew_pips, 0)

    def test_short_inventory_skews_quotes_up(self):
        quote = self.maker.quote(mid=1.1000, sigma=0.0, inventory_lots=-0.25)
        self.assertGreater(quote.reservation, 1.1000)

    def test_full_inventory_stops_quoting_that_side(self):
        quote = self.maker.quote(mid=1.1000, sigma=0.0, inventory_lots=0.5)
        self.assertFalse(quote.quote_bid)
        self.assertTrue(quote.quote_ask)
        self.assertIsNone(quote.price(Side.BUY))

    def test_short_limit_stops_ask(self):
        quote = self.maker.quote(mid=1.1000, sigma=0.0, inventory_lots=-0.5)
        self.assertFalse(quote.quote_ask)

    def test_volatility_widens_spread(self):
        params = MarketMakerParams(base_half_spread_pips=0.4, vol_widen=1.0)
        maker = MarketMaker(self.settings, params)
        calm = maker.quote(mid=1.1, sigma=0.0, inventory_lots=0.0)
        wild = maker.quote(mid=1.1, sigma=0.0005, inventory_lots=0.0)
        self.assertGreater(wild.half_spread, calm.half_spread)

    def test_adverse_flow_pulls_one_side(self):
        quote = self.maker.quote(mid=1.1, sigma=0.0, inventory_lots=0.0, imbalance=0.9)
        self.assertFalse(quote.quote_ask)
        quote = self.maker.quote(mid=1.1, sigma=0.0, inventory_lots=0.0, imbalance=-0.9)
        self.assertFalse(quote.quote_bid)

    def test_half_spread_bounds(self):
        params = MarketMakerParams(
            base_half_spread_pips=99.0, min_half_spread_pips=0.2, max_half_spread_pips=1.0
        )
        maker = MarketMaker(self.settings, params)
        quote = maker.quote(mid=1.1, sigma=0.0, inventory_lots=0.0)
        self.assertAlmostEqual(quote.half_spread, 1.0 * 0.0001)

    def test_invalid_mid(self):
        self.assertIsNone(self.maker.quote(mid=0.0, sigma=0.0, inventory_lots=0.0))

    def test_avellaneda_stoikov_reference_formula(self):
        reservation, half = avellaneda_stoikov(
            mid=1.1, sigma=0.001, inventory=1.0, gamma=0.8, kappa=10_000.0, tau=1.0
        )
        self.assertLess(reservation, 1.1)  # comprado -> reserva abaixo do meio
        self.assertGreater(half, 0)

    def test_adverse_selection_metric(self):
        # comprou a 1.1000 e o meio já estava a 1.0999: seleção adversa negativa
        fill = MakerFill(ts=T0, side=Side.BUY, price=1.1000, lots=0.1, mid_at_fill=1.0999)
        self.assertLess(fill.adverse_bps, 0)
        good = MakerFill(ts=T0, side=Side.SELL, price=1.1000, lots=0.1, mid_at_fill=1.0999)
        self.assertGreater(good.adverse_bps, 0)


class QuotingEngineTests(unittest.TestCase):
    def setUp(self):
        self.settings = HftSettings()
        self.params = MarketMakerParams(
            base_half_spread_pips=0.4, quote_size_lots=0.1, max_inventory_lots=0.5,
            horizon_seconds=3600, vol_widen=0.5,
        )

    def run_engine(self, count=8000, seed=9, **over):
        params = MarketMakerParams(**{**self.params.__dict__, **over})
        engine = QuotingEngine(self.settings, MarketMaker(self.settings, params), params)
        ticks = synthetic_ticks(count=count, pip=0.0001, spread_pips=0.6, seed=seed, start=T0)
        return engine, engine.run(ticks)

    def test_generates_quotes_and_fills(self):
        engine, report = self.run_engine()
        self.assertGreater(report.quotes, 100)
        self.assertGreater(report.fill_count, 10)
        self.assertGreater(report.fill_ratio, 0)

    def test_inventory_stays_within_limit(self):
        engine, report = self.run_engine()
        self.assertLessEqual(report.max_inventory, self.params.max_inventory_lots + 1e-9)

    def test_flatten_at_end_zeroes_inventory(self):
        engine, report = self.run_engine()
        self.assertAlmostEqual(report.inventory_lots, 0.0)

    def test_rebate_enters_net_pnl(self):
        _, without = self.run_engine(rebate_per_lot=0.0)
        _, with_rebate = self.run_engine(rebate_per_lot=5.0)
        self.assertGreater(with_rebate.net_pnl, without.net_pnl)
        self.assertAlmostEqual(with_rebate.rebates, with_rebate.fill_count * 0.5)

    def test_fees_reduce_net_pnl(self):
        _, report = self.run_engine(fee_per_lot=5.0)
        self.assertAlmostEqual(report.fees, report.fill_count * 0.5)
        self.assertAlmostEqual(report.net_pnl, report.realized_pnl + report.rebates - report.fees)

    def test_adverse_selection_is_negative_on_crossing_fills(self):
        """O modelo pessimista só executa quando o preço atravessa a cotação."""
        _, report = self.run_engine()
        self.assertLess(report.avg_adverse_bps, 0)

    def test_max_loss_halts(self):
        engine, report = self.run_engine(count=20000)
        self.assertEqual(report.halted_reason, "")
        engine2 = QuotingEngine(
            self.settings,
            MarketMaker(self.settings, self.params),
            self.params,
            max_loss=1.0,
        )
        report2 = engine2.run(
            synthetic_ticks(count=20000, pip=0.0001, spread_pips=0.6, seed=9, start=T0)
        )
        self.assertIn("limite", report2.halted_reason)

    def test_requote_threshold_reduces_traffic(self):
        _, chatty = self.run_engine(requote_threshold_pips=0.01)
        _, quiet = self.run_engine(requote_threshold_pips=2.0)
        self.assertGreater(chatty.quotes, quiet.quotes)

    def test_report_dict_and_render(self):
        _, report = self.run_engine()
        data = report.to_dict()
        self.assertIn("net_pnl", data)
        self.assertIn("avg_adverse_bps", data)
        text = render_maker_report(report, self.settings)
        self.assertIn("MARKET MAKING", text)
        self.assertIn("Decomposição do resultado", text)
        self.assertIn("seleção adversa", text)

    def test_latency_is_tracked(self):
        engine, report = self.run_engine()
        self.assertGreater(report.latency.histograms["tick_to_trade"].count, 0)


class LatencyTests(unittest.TestCase):
    def test_histogram_percentiles(self):
        histogram = Histogram("teste")
        for value in range(1, 1001):
            histogram.record(value)
        self.assertEqual(histogram.count, 1000)
        self.assertEqual(histogram.min, 1)
        self.assertEqual(histogram.max, 1000)
        self.assertAlmostEqual(histogram.percentile(50), 500, delta=2)
        self.assertAlmostEqual(histogram.percentile(99), 990, delta=2)

    def test_histogram_capacity_keeps_worst(self):
        histogram = Histogram("teste", capacity=10)
        for value in range(1, 101):
            histogram.record(value)
        self.assertEqual(histogram.count, 10)
        self.assertEqual(histogram.max, 100)
        self.assertGreater(histogram.min, 50)  # descartou as amostras rápidas

    def test_empty_histogram(self):
        histogram = Histogram("vazio")
        self.assertEqual(histogram.percentile(99), 0)
        self.assertEqual(histogram.mean, 0.0)

    def test_tracker_stage_context(self):
        tracker = LatencyTracker()
        with tracker.stage("strategy"):
            sum(range(1000))
        self.assertEqual(tracker.histograms["strategy"].count, 1)
        self.assertGreater(tracker.histograms["strategy"].max, 0)

    def test_tracker_start_stop(self):
        tracker = LatencyTracker()
        started = tracker.start("risk")
        elapsed = tracker.stop("risk", started)
        self.assertGreater(elapsed, 0)
        self.assertEqual(tracker.stop("risk", 0), 0)

    def test_report_and_dict(self):
        tracker = LatencyTracker()
        for value in (1000, 2000, 3000):
            tracker.record("wire", value)
        self.assertIn("wire", tracker.report())
        self.assertIn("wire", tracker.to_dict())
        self.assertEqual(tracker.to_dict()["wire"]["count"], 3)

    def test_report_without_samples(self):
        self.assertIn("sem amostras", LatencyTracker().report())


class LowLatencyTests(unittest.TestCase):
    def test_ring_buffer_fifo(self):
        ring: RingBuffer[int] = RingBuffer(capacity=4)
        for i in range(3):
            self.assertTrue(ring.push(i))
        self.assertEqual(len(ring), 3)
        self.assertEqual(ring.pop(), 0)
        self.assertEqual(list(ring.drain()), [1, 2])
        self.assertEqual(len(ring), 0)
        self.assertIsNone(ring.pop())

    def test_ring_buffer_drops_when_full(self):
        ring: RingBuffer[int] = RingBuffer(capacity=2)
        ring.push(1)
        ring.push(2)
        self.assertFalse(ring.push(3))
        self.assertEqual(ring.dropped, 1)
        self.assertTrue(ring.full)

    def test_ring_buffer_wraps(self):
        ring: RingBuffer[int] = RingBuffer(capacity=3)
        for i in range(3):
            ring.push(i)
        ring.pop()
        ring.push(99)
        self.assertEqual(list(ring.drain()), [1, 2, 99])

    def test_object_pool_reuses(self):
        pool = ObjectPool(lambda: [], size=2, reset=lambda item: item.clear())
        first = pool.acquire()
        first.append(1)
        pool.release(first)
        second = pool.acquire()
        self.assertEqual(second, [])
        self.assertGreaterEqual(pool.reused, 2)

    def test_object_pool_grows_when_empty(self):
        pool = ObjectPool(lambda: object(), size=1)
        pool.acquire()
        pool.acquire()
        self.assertEqual(pool.created, 2)

    def test_trading_runtime_restores_gc(self):
        import gc

        self.assertTrue(gc.isenabled())
        with TradingRuntime(disable_gc=True, warmup_calls=10) as runtime:
            self.assertFalse(gc.isenabled())
            self.assertTrue(any("coletor" in note for note in runtime.notes))
        self.assertTrue(gc.isenabled())

    def test_trading_runtime_invalid_cpu_is_reported_not_raised(self):
        with TradingRuntime(disable_gc=False, cpu=9999, warmup_calls=10) as runtime:
            self.assertTrue(runtime.notes)

    def test_spin_wait_elapses(self):
        import time

        start = time.perf_counter_ns()
        spin_wait(50_000)
        self.assertGreaterEqual(time.perf_counter_ns() - start, 50_000)

    def test_busy_poll_processes_items(self):
        items = list(range(5))
        seen: list[int] = []
        clock_value = [0.0]

        def clock() -> float:
            clock_value[0] += 0.01
            return clock_value[0]

        processed = busy_poll(
            poll=lambda: items.pop(0) if items else None,
            on_item=seen.append,
            duration_s=0.05,
            idle_spin_ns=1,
            clock=clock,
        )
        self.assertEqual(processed, len(seen))
        self.assertLessEqual(len(seen), 5)


class InstitutionalCliTests(unittest.TestCase):
    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_mm_command(self):
        code, out = self.run_cli(
            ["mm", "--count", "4000", "--no-news", "--half-spread", "0.4", "--rebate", "2.0"]
        )
        self.assertEqual(code, 0)
        self.assertIn("MARKET MAKING", out)
        self.assertIn("seleção adversa", out)

    def test_mm_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mm.json"
            code, _ = self.run_cli(
                ["mm", "--count", "3000", "--no-news", "--json", str(path)]
            )
            self.assertEqual(code, 0)
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("avg_adverse_bps", data)

    def test_latency_command(self):
        code, out = self.run_cli(["latency", "--count", "2000", "--no-news"])
        self.assertEqual(code, 0)
        self.assertIn("tick_to_trade", out)
        self.assertIn("p99", out)

    def test_latency_without_gc_tuning(self):
        code, out = self.run_cli(["latency", "--count", "1000", "--no-gc-tuning", "--no-news"])
        self.assertEqual(code, 0)
        self.assertNotIn("coletor de lixo desligado", out)


if __name__ == "__main__":
    unittest.main()
