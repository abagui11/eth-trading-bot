"""HQ cards must say whether the order is resting or already going on.

Eva's entry is a pullback into an M5 block, so most HQ cards are limit orders
that have not filled and may never fill. The old lead — "Potential entry near
$X" — read like a position the house already held, which is the one thing
about these broadcasts that misled. These tests pin the distinction and the
markup that carries it, because a caption that opens an unclosed tag fails the
whole send rather than degrading.
"""

from __future__ import annotations

import asyncio
import html
from unittest.mock import AsyncMock, MagicMock, patch

import display_summary
from models import Suggestion
from notify import build_caption, build_caption_html, send_suggestion_to_chat


def _sug(action: str = "spot_sell", product_id: str = "BTC-USD") -> Suggestion:
    return Suggestion(
        action=action,
        size=1307.45,
        entry=78783.23,
        stop_loss=79500.0,
        take_profits=[77840.58, 77056.84, 76219.18],
        risk_reward=1.32,
        rationale="H4 bearish breaker with an M5 order-block fib entry.",
        order_block={},
        product_id=product_id,
    )


# --- what the card says -------------------------------------------------


def test_a_resting_short_says_it_has_not_filled_and_names_the_trigger() -> None:
    body = build_caption(_sug(), resting=True, spot=77637.82)

    assert "Not filled yet" in body
    assert "may never fill" in body
    # Both ends of the move, so "potential" is a number and not a mood.
    assert "$78,783.23" in body
    assert "$77,637.82" in body
    assert "1.48% away" in body
    # A short waits for a rally; saying "falls" here would invert the setup.
    # Scoped to the trigger clause — the levels line names both directions.
    assert "only if BTC rises to $78,783.23" in body
    assert "only if BTC falls" not in body
    assert "spot limit sell" in body
    # The vague old lead must be gone, not merely pushed down the card.
    assert "Potential entry near" not in body


def test_a_resting_long_waits_for_a_fall() -> None:
    body = build_caption(_sug(action="deriv_buy"), resting=True, spot=79900.0)

    assert "only if BTC falls to $78,783.23" in body
    assert "only if BTC rises" not in body
    # Futures wording has to name the exposure: "buy" alone reads as spot.
    assert "futures limit buy that opens a long" in body


def test_a_market_fill_says_it_is_going_on_now() -> None:
    body = build_caption(_sug(action="deriv_sell"), resting=False, spot=78800.0)

    assert "Going on now at market" in body
    assert "not a resting order" in body
    assert "futures market sell that opens a short" in body
    # Nothing about this one is conditional.
    assert "may never fill" not in body
    assert "only if" not in body


def test_an_unknown_route_keeps_the_old_lead_rather_than_guessing() -> None:
    """Watchdog cards and ops resends do not know which path was taken.

    Claiming either one would be a statement about the house book that no
    caller made, so the card stays with the wording it has always had.
    """
    body = build_caption(_sug())

    assert "Potential entry near $78,783.23." in body
    assert "Not filled yet" not in body
    assert "Going on now" not in body


def test_a_scale_in_still_says_it_is_an_add() -> None:
    suggestion = _sug(action="deriv_buy")
    suggestion.entry_tranche = "0.718"

    body = build_caption(suggestion, resting=True, spot=79900.0)

    assert "Adding to the open position." in body
    assert "Not filled yet" in body


def test_no_trade_cards_get_no_banner() -> None:
    assert (
        display_summary.execution_banner(
            Suggestion.no_trade("nothing here"), resting=True, spot=1.0
        )
        is None
    )


def test_a_missing_mark_still_names_the_trigger_price() -> None:
    """Spot is best-effort. Losing it must not cost the whole sentence."""
    body = build_caption(_sug(), resting=True, spot=None)

    assert "rises to $78,783.23" in body
    assert "away" not in body


# --- the markup that carries it -----------------------------------------


def test_only_the_verdict_is_bold() -> None:
    markup = build_caption_html(_sug(), resting=True, spot=77637.82)
    note = display_summary.execution_banner(_sug(), resting=True, spot=77637.82)

    assert note is not None
    assert f"<b>{note.headline}</b>" in markup
    # One emphasis per card — bolding the levels too would flatten it.
    assert markup.count("<b>") == 1
    assert markup.count("</b>") == 1
    assert note.detail in markup


def test_model_prose_cannot_open_a_tag() -> None:
    """The blurb is LLM output. Unescaped, a stray '<' fails the send."""
    markup = build_caption_html(
        _sug(),
        display_summary_text="Structure looks <b>strong</b> & coiled > here",
        resting=True,
        spot=77637.82,
    )

    assert "<b>strong</b>" not in markup
    assert "&lt;b&gt;strong&lt;/b&gt;" in markup
    assert "&amp; coiled &gt; here" in markup
    # Still exactly our one banner tag.
    assert markup.count("<b>") == 1


def test_an_unknown_route_produces_no_markup_at_all() -> None:
    markup = build_caption_html(_sug())

    assert "<b>" not in markup
    assert markup == html.escape(build_caption(_sug()))


def test_the_visible_caption_stays_inside_telegrams_limit() -> None:
    """Telegram counts the 1024 after parsing entities, so tags are free —
    but the visible text still has to fit with the banner added."""
    body = build_caption(
        _sug(),
        display_summary_text="x" * 400,
        resting=True,
        spot=77637.82,
        offer_id="offer-abcd1234",
    )

    assert len(body) <= 1024
    # The Accept line is last and is what truncation would eat first.
    assert "offer abcd1234" in body


# --- delivery ------------------------------------------------------------


def test_the_send_declares_html_and_never_slices_the_markup() -> None:
    """A sliced caption can cut a tag in half and 400 the entire send."""
    bot = MagicMock()
    bot.send_message = AsyncMock()

    asyncio.run(
        send_suggestion_to_chat(
            bot,
            123,
            _sug(),
            [],
            "",
            resting=True,
            spot=77637.82,
        )
    )

    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "<b>" in kwargs["text"] and "</b>" in kwargs["text"]
    assert kwargs["text"] == build_caption_html(
        _sug(), telegram_id=123, resting=True, spot=77637.82
    )


def test_the_chart_caption_is_also_html_and_unsliced(tmp_path) -> None:
    """The photo caption is the one subscribers actually read."""
    chart = tmp_path / "decision.png"
    chart.write_bytes(b"not really a png")
    bot = MagicMock()
    bot.send_photo = AsyncMock()

    asyncio.run(
        send_suggestion_to_chat(
            bot,
            123,
            _sug(),
            [str(chart)],
            "",
            resting=True,
            spot=77637.82,
        )
    )

    kwargs = bot.send_photo.call_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "<b>Not filled yet" in kwargs["caption"]
    assert "</b>" in kwargs["caption"]


def test_the_route_reaches_the_card_from_broadcast() -> None:
    """agent passes the same verdict the live path routed on. If that stops
    arriving, every card silently reverts to the vague lead."""
    import notify

    with patch.object(notify, "Bot") as bot_cls:
        bot_cls.return_value = MagicMock()
        with patch.object(
            notify, "broadcast_to_subscribers", new=AsyncMock()
        ) as fanout:
            notify.broadcast(_sug(), [], pnl_footer="", resting=True, spot=77637.82)

    assert fanout.call_args.kwargs["resting"] is True
    assert fanout.call_args.kwargs["spot"] == 77637.82
