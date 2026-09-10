"""The macro poll shares a thread pool with the trade cycle, so its network
calls must be bounded. An unbounded fetch parks a pool thread for good."""

from __future__ import annotations

import unittest
from unittest import mock

import bot_config
import config
from macro import ingest


class FetchFeedTests(unittest.TestCase):
    def test_fetch_passes_a_timeout(self) -> None:
        response = mock.Mock(content=b"<rss></rss>")
        with mock.patch.object(ingest.requests, "get", return_value=response) as get:
            with mock.patch.object(ingest.feedparser, "parse") as parse:
                ingest._fetch_feed("https://example.com/feed")

        self.assertEqual(get.call_args.args, ("https://example.com/feed",))
        timeout = get.call_args.kwargs.get("timeout")
        self.assertIsNotNone(timeout, "feed fetch must not block indefinitely")
        self.assertLessEqual(timeout, 30)
        # feedparser must parse bytes we already hold, never fetch the URL
        # itself — feedparser.parse(url) is the unbounded path.
        parse.assert_called_once_with(b"<rss></rss>")

    def test_fetch_raises_on_http_error(self) -> None:
        response = mock.Mock()
        response.raise_for_status.side_effect = RuntimeError("503")
        with mock.patch.object(ingest.requests, "get", return_value=response):
            with self.assertRaises(RuntimeError):
                ingest._fetch_feed("https://example.com/feed")


class PollFeedsTests(unittest.TestCase):
    def test_one_bad_feed_does_not_abort_the_poll(self) -> None:
        urls = ["https://bad.example/feed", "https://good.example/feed"]
        good = mock.Mock()
        good.feed = {"title": "Good"}
        good.entries = []

        def fake_fetch(url: str):
            if "bad" in url:
                raise TimeoutError("stalled")
            return good

        with mock.patch.object(bot_config, "MACRO_CONTEXT_ENABLED", True):
            with mock.patch.object(config, "MACRO_FEED_URLS", urls):
                with mock.patch.object(ingest, "_fetch_feed", side_effect=fake_fetch) as f:
                    with mock.patch.object(ingest.store, "prune_old_events"):
                        with self.assertLogs("macro.ingest", level="ERROR"):
                            ingested = ingest.poll_feeds()

        self.assertEqual(ingested, 0)
        self.assertEqual(f.call_count, 2, "the healthy feed must still be polled")


if __name__ == "__main__":
    unittest.main()
