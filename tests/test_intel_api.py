"""Tests for the /api/v1 intelligence endpoints (token-only, fail closed)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import bot_config
import config
from dashboard import data
from intelligence import store

_TOKEN = "svc-token"

_ALL_ROUTES = (
    "/api/v1/intelligence/latest",
    "/api/v1/intelligence/history",
    "/api/v1/signals/macro",
    "/api/v1/signals/zmove",
    "/api/v1/signals/funding",
    "/api/v1/ideas/hq",
    "/api/v1/charts/cycle",
)


class IntelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self._orig_db = config.LEDGER_DB
        config.LEDGER_DB = root / "ledger.db"
        store.init_db()

        self._tokens_patch = mock.patch.object(
            config, "SERVICE_API_TOKENS", [_TOKEN]
        )
        self._tokens_patch.start()
        self.auth = {"Authorization": f"Bearer {_TOKEN}"}

        # Keep the suite off the network: the spot quotes are memoized module
        # side, so a live fetch here would leak into later tests.
        self._spot_patch = mock.patch(
            "dashboard.data.research.get_spot_prices",
            return_value={"ETH-USD": 2000.0, "BTC-USD": 60000.0},
        )
        self._spot_patch.start()
        data.reset_spot_cache()

        from dashboard.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        data.reset_spot_cache()
        self._spot_patch.stop()
        self._tokens_patch.stop()
        config.LEDGER_DB = self._orig_db
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_all_routes_reject_missing_or_bad_token(self) -> None:
        for path in _ALL_ROUTES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                self.assertEqual(
                    self.client.get(
                        path, headers={"Authorization": "Bearer wrong"}
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    self.client.get(
                        path, headers={"Authorization": _TOKEN}
                    ).status_code,
                    401,
                    "non-Bearer scheme must be rejected",
                )

    def test_unconfigured_tokens_fail_closed_with_503(self) -> None:
        with mock.patch.object(config, "SERVICE_API_TOKENS", []), mock.patch.object(
            config, "MACRO_WEBHOOK_SECRET", None
        ):
            for path in _ALL_ROUTES:
                with self.subTest(path=path):
                    response = self.client.get(path, headers=self.auth)
                    self.assertEqual(response.status_code, 503)

    def test_macro_webhook_secret_also_accepted(self) -> None:
        with mock.patch.object(config, "SERVICE_API_TOKENS", []), mock.patch.object(
            config, "MACRO_WEBHOOK_SECRET", "hook-secret"
        ):
            response = self.client.get(
                "/api/v1/intelligence/latest",
                headers={"Authorization": "Bearer hook-secret"},
            )
            self.assertEqual(response.status_code, 200)

    def test_latest_empty_ok(self) -> None:
        response = self.client.get("/api/v1/intelligence/latest", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("stances", payload)
        self.assertEqual(payload["stances"], [])
        self.assertIsNone(payload["long_thesis"])

    def test_latest_returns_stances_and_funding(self) -> None:
        store.insert_stances(
            "2026-08-10T15:00:00Z",
            [
                {
                    "product_id": "BTC-USD",
                    "timeframe": "H4",
                    "stance": "bullish",
                    "confidence": 0.8,
                    "rationale": "structure up",
                },
                {
                    "product_id": "ETH-USD",
                    "timeframe": "H1",
                    "stance": "neutral",
                    "confidence": 0.5,
                    "rationale": "chop",
                },
            ],
        )
        store.insert_medium_summary(
            "2026-08-10T15:00:00Z", "BTC leads.", btc_eth_note="ETH lags."
        )
        store.insert_funding_regime(
            "BTC-USD", "bull_persist", streak_periods=12, as_of_ts="2026-08-10T08:00:00Z"
        )

        response = self.client.get("/api/v1/intelligence/latest", headers=self.auth)
        payload = response.json()
        self.assertEqual(len(payload["stances"]), 2)
        self.assertEqual(payload["medium"]["summary"], "BTC leads.")
        self.assertEqual(
            payload["funding_regimes"]["BTC-USD"]["regime"], "bull_persist"
        )

    def test_history_pagination(self) -> None:
        for hour in (14, 15):
            store.insert_stances(
                f"2026-08-10T{hour}:00:00Z",
                [
                    {
                        "product_id": "BTC-USD",
                        "timeframe": "H4",
                        "stance": "neutral",
                    }
                ],
            )
        response = self.client.get(
            "/api/v1/intelligence/history?limit=1", headers=self.auth
        )
        self.assertEqual(len(response.json()), 1)

    def test_signals_zmove(self) -> None:
        store.insert_zmove_event("ETH-USD", "price", -2.7, "2026-08-10T13:00:00Z")
        response = self.client.get("/api/v1/signals/zmove", headers=self.auth)
        payload = response.json()
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["metric"], "price")

    def test_signals_funding(self) -> None:
        store.upsert_funding_rates(
            "ETH-USD", [{"ts": "2026-08-10T08:00:00Z", "rate": -0.005}]
        )
        store.insert_funding_regime(
            "ETH-USD", "chop", streak_periods=1, as_of_ts="2026-08-10T08:00:00Z"
        )
        response = self.client.get("/api/v1/signals/funding", headers=self.auth)
        payload = response.json()
        self.assertEqual(payload["products"]["ETH-USD"]["regime"]["regime"], "chop")
        self.assertEqual(len(payload["products"]["ETH-USD"]["series"]), 1)

    def test_signals_macro_shape(self) -> None:
        response = self.client.get("/api/v1/signals/macro", headers=self.auth)
        payload = response.json()
        self.assertIn("active", payload)
        self.assertIn("recent", payload)

    def test_ideas_hq_returns_list_when_authed(self) -> None:
        response = self.client.get("/api/v1/ideas/hq", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

    def test_cycle_chart_404_when_absent(self) -> None:
        response = self.client.get("/api/v1/charts/cycle", headers=self.auth)
        self.assertEqual(response.status_code, 404)

    def test_execute_mill_requires_token(self) -> None:
        response = self.client.post(
            "/api/v1/execute/mill",
            json={
                "idea_id": 1,
                "product_id": "ETH-USD",
                "direction": "long",
                "entry": 2000.0,
                "stop_loss": 1970.0,
            },
        )
        self.assertEqual(response.status_code, 401)

    def _mill_body(self, **overrides) -> dict:
        body = {
            "idea_id": 7,
            "product_id": "ETH-USD",
            "direction": "short",
            "entry": 2000.0,
            "stop_loss": 2030.0,
            "take_profits": [1950.0],
            "signal_key": "zmove:ETH-USD:abc",
            "confidence": 0.6,
        }
        body.update(overrides)
        return body

    def test_execute_mill_forwards_to_executor(self) -> None:
        # Revalidation is off: this asserts the route wiring, not whether the
        # body's synthetic levels survive the live ETH mark.
        with mock.patch(
            "execute.maybe_execute_live", return_value={"mode": "shadow"}
        ) as mocked, mock.patch(
            "execute.mill_capacity",
            return_value={"open": 0, "max_open": 3, "slots_free": 3, "halted": None},
        ), mock.patch.object(bot_config, "LIVE_REVALIDATE_ON_FILL", False):
            response = self.client.post(
                "/api/v1/execute/mill", headers=self.auth, json=self._mill_body()
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["executed"])
        self.assertIsNone(payload["skip_reason"])
        suggestion = mocked.call_args.args[0]
        self.assertEqual(suggestion.action, "deriv_sell")
        self.assertEqual(suggestion.product_id, "ETH-USD")
        self.assertEqual(suggestion.stop_loss, 2030.0)
        self.assertEqual(suggestion.order_block_ref, "zmove:ETH-USD:abc")
        self.assertEqual(mocked.call_args.kwargs["source"], "mill")
        self.assertEqual(mocked.call_args.kwargs["cycle_id"], "mill_7")
        self.assertEqual(mocked.call_args.kwargs["fill_type"], "auto")

    def test_execute_mill_auto_needs_conviction(self) -> None:
        """Below the floor the mill never reaches the executor."""
        with mock.patch("execute.maybe_execute_live") as mocked, mock.patch(
            "execute.mill_capacity",
            return_value={"open": 0, "max_open": 3, "slots_free": 3, "halted": None},
        ):
            response = self.client.post(
                "/api/v1/execute/mill",
                headers=self.auth,
                json=self._mill_body(confidence=0.4),
            )
        self.assertEqual(response.json()["skip_reason"], "low_conviction")
        mocked.assert_not_called()

    def test_execute_mill_auto_only_fills_an_empty_book(self) -> None:
        """Slots 2 and 3 belong to manual Accepts, not the FIFO auto path."""
        with mock.patch("execute.maybe_execute_live") as mocked, mock.patch(
            "execute.mill_capacity",
            return_value={"open": 1, "max_open": 3, "slots_free": 2, "halted": None},
        ):
            response = self.client.post(
                "/api/v1/execute/mill", headers=self.auth, json=self._mill_body()
            )
        self.assertEqual(response.json()["skip_reason"], "book_not_empty")
        mocked.assert_not_called()

    def test_execute_mill_manual_requires_known_operator(self) -> None:
        with mock.patch("execute.maybe_execute_live") as mocked, mock.patch(
            "execute.mill_capacity",
            return_value={"open": 0, "max_open": 3, "slots_free": 3, "halted": None},
        ):
            response = self.client.post(
                "/api/v1/execute/mill",
                headers=self.auth,
                json=self._mill_body(fill_type="manual", accepted_by=999),
            )
        self.assertEqual(response.json()["skip_reason"], "not_authorized")
        mocked.assert_not_called()

    def test_execute_mill_rejects_bad_fill_type(self) -> None:
        response = self.client.post(
            "/api/v1/execute/mill",
            headers=self.auth,
            json=self._mill_body(fill_type="sideways"),
        )
        self.assertEqual(response.status_code, 422)

    def test_execute_mill_rejects_bad_direction(self) -> None:
        response = self.client.post(
            "/api/v1/execute/mill",
            headers=self.auth,
            json={
                "idea_id": 1,
                "product_id": "ETH-USD",
                "direction": "watch",
                "entry": 2000.0,
                "stop_loss": 1970.0,
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_public_dashboard_routes_stay_open(self) -> None:
        """The /api/v1 lockdown must not leak onto the public dashboard."""
        for path in ("/api/status", "/api/performance", "/api/macro"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
