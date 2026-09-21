"""Does `/democard fillable` actually put a fillable card in front of testers?

Written to answer one pre-demo question: when the admin types
`/democard fillable`, does every approved account get a card whose Accept
places a real trade? The answer decides whether a test account can enter a
trade on camera, so it is worth driving through the real handler rather than
reasoning about `parse_args` alone.

The MintedIdea tests cover the follow-up problem: the book usually has
nothing fillable (expired, taken, refusable), so `real` now falls back to
minting a fresh idea at the current price, and `mint` asks for one outright.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bot
import bot_config
import config
import demo_card
import notify
import pool
import research
import strategy_catalog
import telegram_ui
import trade_ideas_bridge

ADMIN = 555000
TESTER_A = 777001
TESTER_B = 777002
UNFUNDED = 777003

IDEA_ROW = {
    "id": 29, "product_id": "ETH-USD", "direction": "short",
    "entry": 2400.0, "stop_loss": 2450.0,
    "take_profits_json": "[2300.0]", "title": "ETH short",
    "status": "open",
}
IDEA = {"id": 29, "product_id": "ETH-USD", "direction": "short",
        "status": "open", "would_fill": True, "preview": {"born_rr": 1.4}}


class DemocardFillableBroadcastTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patches = [
            patch.object(config, "LEDGER_DB", db),
            patch.object(config, "POOL_FORUM_CHAT_ID", None),
            patch.object(config, "POOL_ADMIN_TELEGRAM_IDS", []),
            patch.object(bot_config, "POOL_ENABLED", True),
            patch.object(bot_config, "POOL_ADMIN_TELEGRAM_IDS", (ADMIN,)),
            patch.object(bot_config, "POOL_RISK_PCT", 0.007),
            patch.object(bot_config, "POOL_MIN_EQUITY_USD", 500.0),
            # Shipped as True, so a tester's own Accept takes the clip rather
            # than reserving against a house fill that may never land.
            patch.object(bot_config, "LIVE_MILL_ANY_ACCEPT_FILLS", True),
            patch.object(bot_config, "LIVE_MILL_FILL_TELEGRAM_IDS", (ADMIN,)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        pool.init_db()

        # Three approved accounts, two of them funded. The admin is not an
        # account here, so "everyone" means the testers.
        for uid in (TESTER_A, TESTER_B, UNFUNDED):
            pool.approve_user(uid, admin_id=ADMIN)
        for uid in (TESTER_A, TESTER_B):
            pool.credit(uid, 1000.0, admin_id=ADMIN, ref="t")
        # Only A has deployed capital to the mill. B is funded but undeployed,
        # which is the state a fresh test account is actually in.
        pool.subscribe_strategy(TESTER_A, "mill")
        pool.set_allocation(TESTER_A, "mill", 1000.0)

        self.sent: list[tuple[int, str, object]] = []

    # -- harness -----------------------------------------------------------

    def _dm(self, uid, text, keyboard):
        self.sent.append((int(uid), str(text), keyboard))
        return True

    def _run(self, args, *, fillable=None, minted_id=None, spot=2410.0):
        """Drive `/democard <args>` as the admin, with the mill stubbed.

        `fillable` is what the book offers (defaults to one fillable idea);
        `minted_id` is the row id the mint insert would return. Previews say
        yes for any id, so what these tests decide is *which* idea gets sent,
        not whether the fill gate works — the gate has its own tests.
        """
        update = MagicMock()
        update.effective_user.id = ADMIN
        update.effective_user.username = "admin"
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()
        context = MagicMock()
        context.args = list(args)
        context.bot.send_message = AsyncMock()

        rows = [IDEA] if fillable is None else list(fillable)
        with patch.object(trade_ideas_bridge, "fillable_ideas",
                          return_value=rows), \
                patch.object(trade_ideas_bridge, "preview_fill",
                             return_value={"would_fill": True,
                                           "born_rr": 1.4}), \
                patch.object(trade_ideas_bridge, "_idea_row",
                             side_effect=lambda i: {**IDEA_ROW,
                                                    "id": int(i)}), \
                patch.object(trade_ideas_bridge, "mint_idea",
                             return_value=minted_id) as self.mint, \
                patch.object(research, "get_spot_price",
                             return_value=spot), \
                patch.object(notify, "send_pool_dm_with_keyboard", self._dm):
            asyncio.run(bot.cmd_democard(update, context))

        reply = "\n".join(
            str(c.args[0]) for c in update.message.reply_text.call_args_list
        )
        return reply

    def _callbacks(self, keyboard) -> list[str]:
        return [b.callback_data
                for row in keyboard.inline_keyboard for b in row]

    # -- what the words actually do ---------------------------------------

    def test_fillable_is_a_synonym_of_real(self) -> None:
        opts = demo_card.parse_args(["fillable"], default_id=ADMIN)
        self.assertTrue(opts["live_idea"])
        self.assertFalse(opts["mirror"])

    def test_fillable_alone_does_not_broadcast(self) -> None:
        """`fillable` says *what kind* of card; `all` says *who gets it*."""
        opts = demo_card.parse_args(["fillable"], default_id=ADMIN)
        self.assertFalse(opts["everyone"])
        self.assertEqual(opts["telegram_id"], ADMIN)

        both = demo_card.parse_args(["fillable", "all"], default_id=ADMIN)
        self.assertTrue(both["everyone"])
        self.assertTrue(both["live_idea"])

    # -- the demo question -------------------------------------------------

    def test_fillable_alone_cards_only_the_admin(self) -> None:
        """So a tester watching their phone sees nothing."""
        self._run(["fillable"])
        self.assertEqual([uid for uid, _, _ in self.sent], [])

    def test_fillable_all_cards_every_approved_account(self) -> None:
        reply = self._run(["fillable", "all"])
        carded = sorted(uid for uid, _, _ in self.sent)
        self.assertEqual(carded, [TESTER_A, TESTER_B, UNFUNDED])
        self.assertIn("sent", reply.lower())

    def test_every_broadcast_card_can_really_fill(self) -> None:
        """A live card must carry the real Accept and say it is not a demo."""
        self._run(["fillable", "all"])
        self.assertTrue(self.sent, "nothing was sent")
        for uid, text, keyboard in self.sent:
            self.assertIn("LIVE CARD", text, uid)
            self.assertNotIn("DEMO CARD", text, uid)
            data = self._callbacks(keyboard)
            self.assertIn("idea:accept:29", data, uid)
            self.assertFalse(
                any(d.startswith(telegram_ui.CB_POOL_DEMO_PREFIX)
                    for d in data),
                f"{uid} got the demo callback, which cannot fill",
            )

    def test_the_card_is_sized_per_account(self) -> None:
        """A shared render would show everyone the same size line."""
        self._run(["fillable", "all"])
        bodies = {uid: text for uid, text, _ in self.sent}
        self.assertNotEqual(
            bodies[TESTER_A], bodies[UNFUNDED],
            "funded and unfunded accounts got an identical card",
        )

    def test_the_admin_reply_does_not_call_a_live_broadcast_a_demo(self) -> None:
        """The one combination worse than either on its own."""
        reply = self._run(["fillable", "all"])
        self.assertNotIn("Demo card sent", reply, reply)

    def test_an_unfunded_account_is_named_in_the_reply(self) -> None:
        """Before filming, not on playback."""
        reply = self._run(["fillable", "all"])
        self.assertIn("unfunded", reply.lower(), reply)

    # -- targeting one test account instead of everyone --------------------

    def test_fillable_plus_an_id_cards_only_that_account(self) -> None:
        """What a demo actually wants: one real card, one test account."""
        self._run(["fillable", str(TESTER_A)])
        self.assertEqual([uid for uid, _, _ in self.sent], [TESTER_A])

    # -- can the test account really enter? --------------------------------

    def _accept(self, uid: int, verdict: dict):
        with patch.object(trade_ideas_bridge, "idea_pool_open",
                          return_value=True), \
                patch.object(trade_ideas_bridge, "request_manual_fill",
                             return_value=verdict) as fill:
            reply, _markup = bot._pool_mill_accept(29, uid)
        return reply, fill

    def test_a_deployed_test_account_actually_enters_the_trade(self) -> None:
        self._run(["fillable", str(TESTER_A)])
        reply, fill = self._accept(
            TESTER_A, {"executed": True,
                       "result": {"trade_id": 7, "fill": 2400.0}},
        )
        fill.assert_called_once_with(29, TESTER_A)
        self.assertIn("You're in", reply)

    def test_a_funded_but_undeployed_account_cannot_enter(self) -> None:
        """The trap: money in the account is not the same as money deployed."""
        self.assertEqual(pool.get_allocation(TESTER_B, "mill"), 0.0)
        reply, fill = self._accept(
            TESTER_B, {"executed": True,
                       "result": {"trade_id": 7, "fill": 2400.0}},
        )
        fill.assert_not_called()
        self.assertIn("Allocate to Trade Mill?", reply)
        self.assertEqual(pool.pending_intents("mill_29"), [])
        self.assertEqual(float(pool.get_account(TESTER_B)["reserved_usd"]), 0.0)

    def test_the_fill_right_depends_on_the_flag_being_on(self) -> None:
        """With it off, a tester's Accept only reserves against a house fill."""
        self.assertTrue(trade_ideas_bridge.may_fill(TESTER_A))
        with patch.object(bot_config, "LIVE_MILL_ANY_ACCEPT_FILLS", False):
            self.assertFalse(trade_ideas_bridge.may_fill(TESTER_A))

    # -- minting: a live card that always exists ----------------------------

    def test_mint_is_a_live_card_word(self) -> None:
        opts = demo_card.parse_args(["mint"], default_id=ADMIN)
        self.assertTrue(opts["mint"])
        self.assertTrue(opts["live_idea"])

    def test_real_falls_back_to_minting_when_nothing_would_fill(self) -> None:
        """The demo case: `/democard fillable <id>` with a dead book."""
        reply = self._run(["fillable", str(TESTER_A)],
                          fillable=[], minted_id=501)
        self.mint.assert_called_once()
        self.assertEqual([uid for uid, _, _ in self.sent], [TESTER_A])
        _, text, keyboard = self.sent[0]
        self.assertIn("LIVE CARD", text)
        self.assertIn("idea:accept:501", self._callbacks(keyboard))
        self.assertIn("minted fresh", reply)

    def test_real_prefers_the_book_when_something_fills(self) -> None:
        """Minting is the fallback, not a replacement for real mill ideas."""
        self._run(["fillable", str(TESTER_A)], minted_id=501)
        self.mint.assert_not_called()
        self.assertIn("idea:accept:29", self._callbacks(self.sent[0][2]))

    def test_mint_skips_the_scan_and_goes_straight_to_a_fresh_idea(self) -> None:
        self._run(["mint", str(TESTER_A)], minted_id=501)
        self.mint.assert_called_once()
        self.assertIn("idea:accept:501", self._callbacks(self.sent[0][2]))

    def test_a_named_idea_is_never_overridden_by_a_mint(self) -> None:
        """Asking for #57 means #57, even when the book looks dead."""
        self._run(["real", "57", str(TESTER_A)], fillable=[], minted_id=501)
        self.mint.assert_not_called()
        self.assertIn("idea:accept:57", self._callbacks(self.sent[0][2]))

    def test_minting_respects_the_product_and_side_arguments(self) -> None:
        self._run(["mint", "eth", "short", str(TESTER_A)], minted_id=501)
        kwargs = self.mint.call_args.kwargs
        self.assertEqual(kwargs["product_id"], "ETH-USD")
        self.assertEqual(kwargs["direction"], "short")

    def test_no_spot_price_means_an_honest_refusal_not_a_bad_card(self) -> None:
        reply = self._run(["mint", str(TESTER_A)], minted_id=501, spot=0.0)
        self.assertEqual(self.sent, [])
        self.assertIn("could not mint", reply)

    def test_a_minted_broadcast_sends_one_idea_not_one_per_account(self) -> None:
        """Minting lives in the handler so a fan-out cannot mint per tester."""
        self._run(["mint", "all"], fillable=[], minted_id=501)
        self.assertEqual(self.mint.call_count, 1)
        for uid, _, keyboard in self.sent:
            self.assertIn("idea:accept:501", self._callbacks(keyboard), uid)


# Mirrors the mill's schema (trade_ideas/trade_ideas/store.py).
_MILL_SCHEMA = """
CREATE TABLE ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    product_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    title TEXT NOT NULL,
    blurb TEXT NOT NULL,
    signal_key TEXT NOT NULL UNIQUE,
    stance_context TEXT,
    confidence REAL,
    meta_json TEXT,
    status TEXT NOT NULL DEFAULT 'offered',
    created_at TEXT NOT NULL,
    entry REAL,
    stop_loss REAL,
    take_profits_json TEXT,
    risk_reward REAL,
    chart_path TEXT,
    sent_at TEXT,
    live_fill_type TEXT,
    live_filled_by INTEGER
);
"""


class MintedIdeaRowTests(unittest.TestCase):
    """What `mint_idea` actually writes, against a real ideas database."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "ideas.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript(_MILL_SCHEMA)
        self._env = patch.dict(os.environ, {"IDEAS_DB": str(self.db)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _mint(self) -> int:
        with patch.object(research, "get_spot_price", return_value=64_000.0):
            result = demo_card.mint_live_idea("BTC-USD", "buy")
        self.assertTrue(result["ok"], result)
        return int(result["idea_id"])

    def test_the_minted_row_is_a_complete_live_idea(self) -> None:
        idea_id = self._mint()
        row = trade_ideas_bridge._idea_row(idea_id)
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["product_id"], "BTC-USD")
        self.assertEqual(row["direction"], "long")
        self.assertIsNotNone(row["entry"])
        self.assertIsNotNone(row["stop_loss"])
        # Accept-able: live status, no fill recorded yet.
        self.assertTrue(trade_ideas_bridge.idea_pool_open(idea_id))
        # And honest about what it is.
        self.assertIn("Operator-minted", row["title"])

    def test_the_minted_geometry_clears_the_rr_floor(self) -> None:
        """Born fillable: the gate's own R:R measure, above LIVE_MIN_FILL_RR."""
        row = trade_ideas_bridge._idea_row(self._mint())
        born = trade_ideas_bridge._rr_at_mint(row)
        self.assertIsNotNone(born)
        self.assertGreaterEqual(born, float(bot_config.LIVE_MIN_FILL_RR))

    def test_the_house_can_never_take_a_minted_idea_on_its_own(self) -> None:
        """No confidence score → invisible to auto-fill and the re-offer
        sweep. It fills from an Accept or it expires; those are the outcomes."""
        idea_id = self._mint()
        row = trade_ideas_bridge._idea_row(idea_id)
        self.assertIsNone(row["confidence"])
        with patch.object(bot_config, "LIVE_MILL_REOFFER_ENABLED", True), \
                patch.object(trade_ideas_bridge, "_sweep_floor",
                             return_value="1970-01-01T00:00:00Z"):
            candidates = trade_ideas_bridge.reoffer_candidates()
        self.assertNotIn(idea_id, [int(c["id"]) for c in candidates])

    def test_two_mints_never_collide_on_the_dedupe_key(self) -> None:
        """signal_key is UNIQUE; a fixed key would make the second mint a
        silent no-op and the live order dedupe would refuse the repeat."""
        first = self._mint()
        second = self._mint()
        self.assertNotEqual(first, second)
        with sqlite3.connect(self.db) as conn:
            keys = [r[0] for r in conn.execute(
                "SELECT signal_key FROM ideas"
            )]
        self.assertEqual(len(keys), len(set(keys)))

    def test_no_ideas_db_is_a_soft_failure(self) -> None:
        with patch.dict(os.environ, {"IDEAS_DB": ""}), \
                patch.object(research, "get_spot_price",
                             return_value=64_000.0):
            result = demo_card.mint_live_idea("BTC-USD", "buy")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "mint_failed")


if __name__ == "__main__":
    unittest.main()
