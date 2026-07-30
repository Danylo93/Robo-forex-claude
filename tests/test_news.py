"""Testes do filtro de notícias."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from robo_forex.config import NewsSettings, SymbolSpec
from robo_forex.models import NewsEvent
from robo_forex.news import FileCalendar, NewsFilter, normalize_impact, parse_ts

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


class FakeCalendar:
    def __init__(self, events):
        self._events = events

    def events(self):
        return list(self._events)


class BrokenCalendar:
    def events(self):
        raise OSError("sem rede")


def event(minutes: int, currency: str = "USD", impact: str = "high") -> NewsEvent:
    return NewsEvent(
        ts=NOW + timedelta(minutes=minutes), currency=currency, title="NFP", impact=impact
    )


class ParsingTests(unittest.TestCase):
    def test_parse_iso_with_offset(self):
        ts = parse_ts("2026-07-26T19:50:00-04:00")
        self.assertEqual(ts, datetime(2026, 7, 26, 23, 50, tzinfo=timezone.utc))

    def test_parse_naive_is_utc(self):
        self.assertEqual(parse_ts("2026-07-26 10:00:00").tzinfo, timezone.utc)

    def test_parse_epoch_and_invalid(self):
        self.assertEqual(parse_ts("0"), datetime(1970, 1, 1, tzinfo=timezone.utc))
        self.assertIsNone(parse_ts("não é data"))

    def test_impact_aliases(self):
        self.assertEqual(normalize_impact("High"), "high")
        self.assertEqual(normalize_impact("Holiday"), "high")
        self.assertEqual(normalize_impact(""), "low")


class BlackoutTests(unittest.TestCase):
    def setUp(self):
        self.settings = NewsSettings(minutes_before=60, minutes_after=30, impacts=["high"])
        self.spec = SymbolSpec("EURUSD")

    def filter_with(self, events, **kwargs):
        settings = NewsSettings(**{**self.settings.__dict__, **kwargs})
        return NewsFilter(settings, FakeCalendar(events))

    def test_blocks_event_ahead_inside_window(self):
        verdict = self.filter_with([event(30)]).check(self.spec, NOW)
        self.assertTrue(verdict.blocked)
        self.assertIn("NFP", verdict.reason)

    def test_blocks_event_just_released(self):
        self.assertTrue(self.filter_with([event(-20)]).check(self.spec, NOW).blocked)

    def test_allows_event_outside_window(self):
        self.assertFalse(self.filter_with([event(180)]).check(self.spec, NOW).blocked)
        self.assertFalse(self.filter_with([event(-90)]).check(self.spec, NOW).blocked)

    def test_ignores_other_currencies(self):
        self.assertFalse(self.filter_with([event(10, "AUD")]).check(self.spec, NOW).blocked)

    def test_ignores_low_impact_by_default(self):
        self.assertFalse(self.filter_with([event(10, "USD", "low")]).check(self.spec, NOW).blocked)

    def test_medium_impact_can_be_included(self):
        f = self.filter_with([event(10, "USD", "medium")], impacts=["high", "medium"])
        self.assertTrue(f.check(self.spec, NOW).blocked)

    def test_disabled_filter_never_blocks(self):
        f = self.filter_with([event(0)], enabled=False)
        self.assertFalse(f.check(self.spec, NOW).blocked)

    def test_fail_open_allows_with_warning(self):
        f = NewsFilter(NewsSettings(fail_open=True), BrokenCalendar())
        verdict = f.check(self.spec, NOW)
        self.assertFalse(verdict.blocked)
        self.assertTrue(verdict.reason.startswith("aviso"))

    def test_fail_closed_blocks(self):
        f = NewsFilter(NewsSettings(fail_open=False), BrokenCalendar())
        self.assertTrue(f.check(self.spec, NOW).blocked)

    def test_upcoming_lists_only_horizon(self):
        f = self.filter_with([event(60), event(60 * 30)])
        self.assertEqual(len(f.upcoming(self.spec, NOW, hours=12)), 1)

    def test_cross_pair_matches_either_currency(self):
        f = self.filter_with([event(10, "NZD")])
        self.assertTrue(f.check(SymbolSpec("GBPNZD"), NOW).blocked)


class FileCalendarTests(unittest.TestCase):
    def test_reads_json(self):
        rows = [{"title": "CPI", "country": "GBP", "date": "2026-07-30T12:30:00+00:00",
                 "impact": "High"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            path.write_text(json.dumps(rows), encoding="utf-8")
            events = FileCalendar(path).events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].currency, "GBP")
        self.assertTrue(events[0].is_high)

    def test_reads_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.csv"
            path.write_text(
                "date,currency,title,impact\n2026-07-30 12:30,USD,NFP,High\n", encoding="utf-8"
            )
            events = FileCalendar(path).events()
        self.assertEqual(events[0].title, "NFP")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            FileCalendar("/tmp/não-existe-calendario.json").events()


if __name__ == "__main__":
    unittest.main()
