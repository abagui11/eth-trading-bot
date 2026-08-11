"""Tests for the swappable funding-source adapters and outage surfacing.

The adapter boundary is where venue quirks are supposed to die, so most of
these assert normalization: canonical symbols in, decimal-fraction rates and
UTC ISO settlement stamps out, oldest-first, with the 8h interval intact.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config
from intelligence import funding, store
from intelligence.funding_sources import (
    FUNDING_INTERVAL_HOURS,
    BinanceFundingSource,
    BybitFundingSource,
    FundingSourceError,
    OkxFundingSource,
    fetch_funding_history,
    get_source,
    source_chain,
)

# Three real consecutive OKX BTC-USDT-SWAP prints, newest-first as the venue
# returns them (settlement times 00:00/08:00/16:00 UTC).
_OKX_PAYLOAD = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "fundingRate": "0.0001",
            "realizedRate": "0.0001",
            "fundingTime": "1786435200000",
            "instId": "BTC-USDT-SWAP",
        },
        {
            "fundingRate": "0.0001",
            "realizedRate": "0.0001",
            "fundingTime": "1786406400000",
            "instId": "BTC-USDT-SWAP",
        },
        {
            "fundingRate": "0.000096723689051",
            "realizedRate": "0.000096723689051",
            "fundingTime": "1786377600000",
            "instId": "BTC-USDT-SWAP",
        },
    ],
}


def _fake_response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class TestOkxAdapter(unittest.TestCase):
    def _fetch(self, payload=_OKX_PAYLOAD, product_id="BTC-USD"):
        with mock.patch(
            "intelligence.funding_sources.requests.get",
            return_value=_fake_response(payload),
        ) as get:
            series = OkxFundingSource().fetch(product_id)
        return series, get

    def test_maps_canonical_product_to_venue_symbol(self) -> None:
        """OKX's BTC-USDT-SWAP form must be built here and go no further."""
        _, get = self._fetch()
        self.assertEqual(get.call_args.kwargs["params"]["instId"], "BTC-USDT-SWAP")

        with mock.patch(
            "intelligence.funding_sources.requests.get",
            return_value=_fake_response(_OKX_PAYLOAD),
        ) as get:
            OkxFundingSource().fetch("ETH-USD")
        self.assertEqual(get.call_args.kwargs["params"]["instId"], "ETH-USDT-SWAP")

    def test_normalized_rows_carry_no_venue_symbol(self) -> None:
        series, _ = self._fetch()
        for row in series:
            self.assertEqual(set(row), {"ts", "rate"})

    def test_rate_stays_a_decimal_fraction(self) -> None:
        """0.0001 means 0.01% — the unit the classifier and store expect."""
        series, _ = self._fetch()
        self.assertAlmostEqual(series[-1]["rate"], 0.0001)
        self.assertLess(abs(series[-1]["rate"]), 0.01)

    def test_negative_rate_keeps_its_sign(self) -> None:
        payload = {
            "code": "0",
            "data": [
                {
                    "fundingRate": "-0.00025",
                    "realizedRate": "-0.00025",
                    "fundingTime": "1786435200000",
                }
            ],
        }
        series, _ = self._fetch(payload)
        self.assertAlmostEqual(series[0]["rate"], -0.00025)

    def test_timestamp_is_utc_iso_settlement_time(self) -> None:
        series, _ = self._fetch()
        # 1786435200000 ms == 2026-08-11T08:00:00Z, an 8h settlement boundary.
        self.assertEqual(series[-1]["ts"], "2026-08-11T08:00:00Z")

    def test_series_is_oldest_first(self) -> None:
        """OKX returns newest-first; the classifier reads oldest-first."""
        series, _ = self._fetch()
        self.assertEqual([r["ts"] for r in series], sorted(r["ts"] for r in series))

    def test_prints_are_eight_hours_apart(self) -> None:
        series, _ = self._fetch()
        stamps = [
            datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ") for r in series
        ]
        for earlier, later in zip(stamps, stamps[1:]):
            self.assertEqual(later - earlier, timedelta(hours=FUNDING_INTERVAL_HOURS))

    def test_prefers_realized_rate_over_announced(self) -> None:
        payload = {
            "code": "0",
            "data": [
                {
                    "fundingRate": "0.0009",
                    "realizedRate": "0.0002",
                    "fundingTime": "1786435200000",
                }
            ],
        }
        series, _ = self._fetch(payload)
        self.assertAlmostEqual(series[0]["rate"], 0.0002)

    def test_falls_back_to_announced_rate_when_realized_missing(self) -> None:
        payload = {
            "code": "0",
            "data": [
                {
                    "fundingRate": "0.0009",
                    "realizedRate": "",
                    "fundingTime": "1786435200000",
                }
            ],
        }
        series, _ = self._fetch(payload)
        self.assertAlmostEqual(series[0]["rate"], 0.0009)

    def test_api_level_error_code_raises(self) -> None:
        """HTTP 200 with a non-zero code is still a failure."""
        with mock.patch(
            "intelligence.funding_sources.requests.get",
            return_value=_fake_response({"code": "51001", "msg": "bad instId"}),
        ):
            with self.assertRaises(FundingSourceError):
                OkxFundingSource().fetch("BTC-USD")

    def test_limit_clamped_to_venue_cap(self) -> None:
        with mock.patch(
            "intelligence.funding_sources.requests.get",
            return_value=_fake_response(_OKX_PAYLOAD),
        ) as get:
            OkxFundingSource().fetch("BTC-USD", limit=500)
        self.assertEqual(get.call_args.kwargs["params"]["limit"], 100)

    def test_unknown_product_raises(self) -> None:
        with self.assertRaises(FundingSourceError):
            OkxFundingSource().fetch("DOGE-USD")


class TestBybitAdapter(unittest.TestCase):
    payload = {
        "retCode": 0,
        "result": {
            "list": [
                {"fundingRate": "0.0001", "fundingRateTimestamp": "1786435200000"},
                {"fundingRate": "-0.0002", "fundingRateTimestamp": "1786406400000"},
            ]
        },
    }

    def test_normalizes_to_the_same_shape(self) -> None:
        with mock.patch(
            "intelligence.funding_sources.requests.get",
            return_value=_fake_response(self.payload),
        ) as get:
            series = BybitFundingSource().fetch("BTC-USD")
        self.assertEqual(get.call_args.kwargs["params"]["symbol"], "BTCUSDT")
        self.assertEqual(get.call_args.kwargs["params"]["category"], "linear")
        self.assertEqual(
            series,
            [
                {"ts": "2026-08-11T00:00:00Z", "rate": -0.0002},
                {"ts": "2026-08-11T08:00:00Z", "rate": 0.0001},
            ],
        )

    def test_api_level_error_code_raises(self) -> None:
        with mock.patch(
            "intelligence.funding_sources.requests.get",
            return_value=_fake_response({"retCode": 10001, "retMsg": "nope"}),
        ):
            with self.assertRaises(FundingSourceError):
                BybitFundingSource().fetch("BTC-USD")


class TestBinanceAdapter(unittest.TestCase):
    def test_normalizes_to_the_same_shape(self) -> None:
        payload = [
            {"fundingTime": 1786406400000, "fundingRate": "-0.0002"},
            {"fundingTime": 1786435200000, "fundingRate": "0.0001"},
        ]
        with mock.patch(
            "intelligence.funding_sources.requests.get",
            return_value=_fake_response(payload),
        ) as get:
            series = BinanceFundingSource().fetch("ETH-USD")
        self.assertEqual(get.call_args.kwargs["params"]["symbol"], "ETHUSDT")
        self.assertEqual(
            series,
            [
                {"ts": "2026-08-11T00:00:00Z", "rate": -0.0002},
                {"ts": "2026-08-11T08:00:00Z", "rate": 0.0001},
            ],
        )


class TestSourcesAgree(unittest.TestCase):
    """The same economic print through three adapters must come out identical."""

    def test_identical_print_normalizes_identically(self) -> None:
        expected = [{"ts": "2026-08-11T08:00:00Z", "rate": 0.0001}]

        cases = [
            (OkxFundingSource(), {"code": "0", "data": [
                {"realizedRate": "0.0001", "fundingTime": "1786435200000"}]}),
            (BybitFundingSource(), {"retCode": 0, "result": {"list": [
                {"fundingRate": "0.0001", "fundingRateTimestamp": "1786435200000"}]}}),
            (BinanceFundingSource(), [
                {"fundingRate": "0.0001", "fundingTime": "1786435200000"}]),
        ]
        for source, payload in cases:
            with self.subTest(source=source.name):
                with mock.patch(
                    "intelligence.funding_sources.requests.get",
                    return_value=_fake_response(payload),
                ):
                    self.assertEqual(source.fetch("BTC-USD"), expected)


class TestSourceChain(unittest.TestCase):
    def test_default_primary_is_okx(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(source_chain()[0].name, "okx")

    def test_env_overrides_primary_and_fallbacks(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"FUNDING_SOURCE": "bybit", "FUNDING_SOURCE_FALLBACKS": "okx"},
            clear=True,
        ):
            self.assertEqual([s.name for s in source_chain()], ["bybit", "okx"])

    def test_unknown_names_are_skipped_not_fatal(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"FUNDING_SOURCE": "okx", "FUNDING_SOURCE_FALLBACKS": "ftx"},
            clear=True,
        ):
            self.assertEqual([s.name for s in source_chain()], ["okx"])

    def test_get_source_rejects_unknown(self) -> None:
        with self.assertRaises(FundingSourceError):
            get_source("mtgox")


class _StubSource:
    def __init__(self, name, series=None, error=None):
        self.name = name
        self._series = series or []
        self._error = error
        self.calls = 0

    def fetch(self, product_id, *, limit=90):
        self.calls += 1
        if self._error:
            raise self._error
        return self._series


class TestFetchWithFallback(unittest.TestCase):
    rows = [{"ts": "2026-08-11T08:00:00Z", "rate": 0.0001}]

    def test_primary_success_skips_fallbacks(self) -> None:
        primary = _StubSource("okx", self.rows)
        backup = _StubSource("bybit", self.rows)
        series, name = fetch_funding_history("BTC-USD", sources=[primary, backup])
        self.assertEqual((series, name), (self.rows, "okx"))
        self.assertEqual(backup.calls, 0)

    def test_falls_through_to_next_source(self) -> None:
        primary = _StubSource("okx", error=RuntimeError("451"))
        backup = _StubSource("bybit", self.rows)
        series, name = fetch_funding_history("BTC-USD", sources=[primary, backup])
        self.assertEqual((series, name), (self.rows, "bybit"))

    def test_empty_series_is_treated_as_failure(self) -> None:
        """An empty 200 must not look like a healthy 'no funding' answer."""
        primary = _StubSource("okx", [])
        backup = _StubSource("bybit", self.rows)
        _, name = fetch_funding_history("BTC-USD", sources=[primary, backup])
        self.assertEqual(name, "bybit")

    def test_total_outage_raises_instead_of_returning_empty(self) -> None:
        chain = [
            _StubSource("okx", error=RuntimeError("451")),
            _StubSource("bybit", error=RuntimeError("403")),
        ]
        with self.assertRaises(FundingSourceError) as ctx:
            fetch_funding_history("BTC-USD", sources=chain)
        self.assertIn("451", str(ctx.exception))
        self.assertIn("403", str(ctx.exception))

    def test_fallback_use_is_logged_not_silent(self) -> None:
        chain = [
            _StubSource("okx", error=RuntimeError("451")),
            _StubSource("bybit", self.rows),
        ]
        with self.assertLogs("intelligence.funding_sources", level="WARNING") as logs:
            fetch_funding_history("BTC-USD", sources=chain)
        self.assertTrue(any("fallback source bybit" in m for m in logs.output))


class TestFundingHealthAndStaleness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = config.LEDGER_DB
        config.LEDGER_DB = Path(self._tmp.name) / "test_ledger.db"
        store.init_db()

    def tearDown(self) -> None:
        config.LEDGER_DB = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def test_fresh_print_is_not_stale(self) -> None:
        ts = (self._now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertFalse(funding.is_stale(ts))

    def test_two_missed_settlements_is_stale(self) -> None:
        ts = (self._now() - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(funding.is_stale(ts))

    def test_missing_or_malformed_timestamp_is_stale(self) -> None:
        self.assertTrue(funding.is_stale(None))
        self.assertTrue(funding.is_stale(""))
        self.assertTrue(funding.is_stale("not-a-timestamp"))

    def test_status_unavailable_with_no_data(self) -> None:
        status = funding.funding_status("BTC-USD")
        self.assertFalse(status["available"])
        self.assertIsNone(status["regime"])

    def test_status_available_after_a_fresh_scan(self) -> None:
        ts = (self._now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.insert_funding_regime(
            "BTC-USD", "bull_persist", streak_periods=9, as_of_ts=ts
        )
        store.record_funding_health(
            "BTC-USD", status="ok", source="okx", funding_ts=ts
        )
        status = funding.funding_status("BTC-USD")
        self.assertTrue(status["available"])
        self.assertEqual(status["source"], "okx")

    def test_stale_regime_is_not_reported_available(self) -> None:
        """Old Binance-era rows must not pass as a live signal."""
        ts = (self._now() - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.insert_funding_regime(
            "BTC-USD", "bull_persist", streak_periods=9, as_of_ts=ts
        )
        status = funding.funding_status("BTC-USD")
        self.assertTrue(status["stale"])
        self.assertFalse(status["available"])

    def test_error_health_preserves_last_success_marks(self) -> None:
        ts = "2026-08-11T08:00:00Z"
        store.record_funding_health(
            "BTC-USD", status="ok", source="okx", funding_ts=ts
        )
        store.record_funding_health("BTC-USD", status="error", error="451 blocked")
        health = store.funding_health("BTC-USD")
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["last_error"], "451 blocked")
        self.assertEqual(health["last_ok_funding_ts"], ts)
        self.assertIsNotNone(health["last_ok_at"])


class TestScanSurfacesOutage(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = config.LEDGER_DB
        config.LEDGER_DB = Path(self._tmp.name) / "test_ledger.db"
        store.init_db()

    def tearDown(self) -> None:
        config.LEDGER_DB = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_total_outage_logs_error_and_records_health(self) -> None:
        boom = FundingSourceError("all sources failed: 451")
        with mock.patch(
            "intelligence.funding.fetch_funding_history", side_effect=boom
        ):
            with self.assertLogs("intelligence.funding", level="ERROR") as logs:
                results = funding.run_funding_scan()

        self.assertEqual(results, [])
        self.assertTrue(any("FUNDING OUTAGE" in m for m in logs.output))
        health = store.funding_health("BTC-USD")
        self.assertEqual(health["status"], "error")
        self.assertIn("451", health["last_error"])

    def test_successful_scan_records_source(self) -> None:
        rows = [
            {"ts": f"2026-08-{i + 1:02d}T00:00:00Z", "rate": 0.0001}
            for i in range(12)
        ]
        with mock.patch(
            "intelligence.funding.fetch_funding_history",
            return_value=(rows, "okx"),
        ):
            results = funding.run_funding_scan()

        self.assertTrue(results)
        health = store.funding_health("BTC-USD")
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["source"], "okx")


class TestStancePromptSurfacesOutage(unittest.TestCase):
    """The regression that started this: a dead feed used to yield ''."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = config.LEDGER_DB
        config.LEDGER_DB = Path(self._tmp.name) / "test_ledger.db"
        store.init_db()

    def tearDown(self) -> None:
        config.LEDGER_DB = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_no_funding_data_yields_explicit_unavailable_block(self) -> None:
        from intelligence.stance import _funding_context_block

        with self.assertLogs("intelligence.stance", level="ERROR"):
            block = _funding_context_block()

        self.assertNotEqual(block.strip(), "")
        self.assertIn("UNAVAILABLE", block)

    def test_healthy_funding_yields_a_regime_block(self) -> None:
        from intelligence.stance import _funding_context_block

        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        for pid in ("BTC-USD", "ETH-USD"):
            store.insert_funding_regime(
                pid, "bull_persist", streak_periods=9, as_of_ts=ts
            )
            store.record_funding_health(
                pid, status="ok", source="okx", funding_ts=ts
            )

        block = _funding_context_block()
        self.assertIn("bull_persist", block)
        self.assertIn("okx", block)
        self.assertNotIn("UNAVAILABLE", block)


if __name__ == "__main__":
    unittest.main()
