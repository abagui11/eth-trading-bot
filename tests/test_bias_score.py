"""Unit tests for deterministic news bias scoring and the batched LLM refine."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from macro import bias_score
from macro.bias_score import best_bias, deterministic_bias


def _items_json(ids: list[int], *, indent: int | None = None) -> str:
    payload = {
        "items": [
            {"id": i, "side": "bearish", "pct": 60, "one_liner": f"line {i}"}
            for i in ids
        ]
    }
    return json.dumps(payload, indent=indent)


def _fake_response(text: str, *, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(output_tokens=len(text) // 4),
    )


class _FakeClient:
    """Stands in for anthropic.Anthropic, replying per call from a queue."""

    def __init__(self, replies: list[SimpleNamespace]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._replies.pop(0) if self._replies else _fake_response(
            _items_json([])
        )


class BiasScoreTests(unittest.TestCase):
    def test_bullish_headline(self) -> None:
        out = deterministic_bias("Fed signals rate cut as ETF inflows surge", "bullish")
        self.assertEqual(out["side"], "bullish")
        self.assertGreaterEqual(out["pct"], 50)

    def test_bearish_headline(self) -> None:
        out = deterministic_bias("Exchange hack triggers mass liquidation near Hormuz", "bearish")
        self.assertEqual(out["side"], "bearish")
        self.assertGreaterEqual(out["pct"], 50)

    def test_best_bias_prefers_llm(self) -> None:
        event = {
            "title": "x",
            "bias_side_det": "bullish",
            "bias_pct_det": 40,
            "bias_side_llm": "bearish",
            "bias_pct_llm": 71,
            "bias_one_liner": "sell the news",
        }
        best = best_bias(event)
        self.assertEqual(best["side"], "bearish")
        self.assertEqual(best["pct"], 71)
        self.assertEqual(best["source"], "llm")


class SalvageTests(unittest.TestCase):
    """A reply cut off at max_tokens must not cost the entries that arrived."""

    def test_truncated_reply_keeps_complete_entries(self) -> None:
        # Cut mid one_liner of the last entry, the exact production failure.
        full = _items_json([1, 2, 3], indent=2)
        truncated = full[: full.index('"line 3"') + 5]
        with self.assertRaises(json.JSONDecodeError):
            json.loads(truncated)

        items, salvaged = bias_score._parse_items(truncated)
        self.assertTrue(salvaged)
        self.assertEqual([i["id"] for i in items], [1, 2])

    def test_truncated_reply_mid_object_keeps_earlier_entries(self) -> None:
        full = _items_json([7, 8, 9], indent=2)
        truncated = full[: full.index('"id": 9') + 7]
        items, salvaged = bias_score._parse_items(truncated)
        self.assertTrue(salvaged)
        self.assertEqual([i["id"] for i in items], [7, 8])

    def test_intact_reply_is_not_salvaged(self) -> None:
        items, salvaged = bias_score._parse_items(_items_json([1, 2]))
        self.assertFalse(salvaged)
        self.assertEqual([i["id"] for i in items], [1, 2])

    def test_reply_with_trailing_commentary_still_parses(self) -> None:
        raw = _items_json([4]) + "\n\nHope that helps!"
        items, _ = bias_score._parse_items(raw)
        self.assertEqual([i["id"] for i in items], [4])

    def test_garbage_reply_yields_nothing(self) -> None:
        items, salvaged = bias_score._parse_items("I cannot do that.")
        self.assertTrue(salvaged)
        self.assertEqual(items, [])


class RefineBatchTests(unittest.TestCase):
    def _events(self, count: int) -> list[dict]:
        return [{"id": i, "title": f"headline {i}"} for i in range(1, count + 1)]

    def _run(self, events: list[dict], replies: list[SimpleNamespace]) -> _FakeClient:
        client = _FakeClient(replies)
        with mock.patch.object(
            bias_score.anthropic, "Anthropic", return_value=client
        ):
            self.refined = bias_score.refine_bias_batch(events)
        return client

    def test_truncated_chunk_still_persists_what_arrived(self) -> None:
        full = _items_json([1, 2, 3, 4, 5], indent=2)
        truncated = full[: full.index('"line 5"') + 6]
        self._run(
            self._events(5),
            [_fake_response(truncated, stop_reason="max_tokens")],
        )
        self.assertEqual([r["id"] for r in self.refined], [1, 2, 3, 4])

    def test_batch_is_chunked_and_results_merged(self) -> None:
        count = bias_score._CHUNK_SIZE * 2 + 3
        ids = list(range(1, count + 1))
        replies = [
            _fake_response(_items_json(ids[i : i + bias_score._CHUNK_SIZE]))
            for i in range(0, count, bias_score._CHUNK_SIZE)
        ]
        client = self._run(self._events(count), replies)

        self.assertEqual(len(client.calls), 3)
        self.assertEqual([r["id"] for r in self.refined], ids)
        # Batched, not per-event: far fewer calls than headlines.
        self.assertLess(len(client.calls), count)

    def test_one_failing_chunk_does_not_lose_the_others(self) -> None:
        count = bias_score._CHUNK_SIZE + 2
        ids = list(range(1, count + 1))
        first = _fake_response(_items_json(ids[: bias_score._CHUNK_SIZE]))
        broken = _fake_response("total nonsense", stop_reason="max_tokens")
        self._run(self._events(count), [first, broken])
        self.assertEqual(
            [r["id"] for r in self.refined], ids[: bias_score._CHUNK_SIZE]
        )

    def test_api_exception_in_one_chunk_is_contained(self) -> None:
        count = bias_score._CHUNK_SIZE + 1
        ids = list(range(1, count + 1))

        class _Boom(_FakeClient):
            def _create(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    raise RuntimeError("api down")
                return _fake_response(_items_json(ids[bias_score._CHUNK_SIZE :]))

        client = _Boom([])
        with mock.patch.object(
            bias_score.anthropic, "Anthropic", return_value=client
        ):
            refined = bias_score.refine_bias_batch(self._events(count))
        self.assertEqual([r["id"] for r in refined], ids[bias_score._CHUNK_SIZE :])

    def test_token_budget_clears_the_observed_failure_point(self) -> None:
        # Production truncated a 40-headline reply at 2048 output tokens. A
        # full chunk must be budgeted well above the per-item cost that caused it.
        budget = bias_score._chunk_max_tokens(bias_score._CHUNK_SIZE)
        self.assertGreater(budget / bias_score._CHUNK_SIZE, 2048 / 40)
        self.assertLessEqual(budget, bias_score._MAX_CHUNK_TOKENS)

    def test_max_tokens_sent_scales_with_chunk_size(self) -> None:
        client = self._run(self._events(3), [_fake_response(_items_json([1, 2, 3]))])
        self.assertEqual(
            client.calls[0]["max_tokens"], bias_score._chunk_max_tokens(3)
        )

    def test_no_events_makes_no_calls(self) -> None:
        with mock.patch.object(bias_score.anthropic, "Anthropic") as ctor:
            self.assertEqual(bias_score.refine_bias_batch([]), [])
        ctor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
