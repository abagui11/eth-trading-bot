"""Tests for flattening model markdown into Telegram-safe plain text."""

from __future__ import annotations

import telegram_text


def test_headings_rules_and_emphasis_are_dropped():
    out = telegram_text.to_plain_text(
        "## Rate Hikes\n\n---\n\n**The short answer:** hikes *compress* the cycle."
    )
    assert "#" not in out
    assert "*" not in out
    assert "---" not in out
    assert out.startswith("Rate Hikes")
    assert "The short answer: hikes compress the cycle." in out


def test_table_becomes_labelled_lines():
    out = telegram_text.to_plain_text(
        "| Cycle Phase | Rate Hike Effect |\n"
        "|---|---|\n"
        "| **Early bull** | Dampens momentum |\n"
        "| **Bear market** | Amplifies drawdowns |\n"
    )
    assert "|" not in out
    assert "Early bull" in out
    assert "  Rate Hike Effect: Dampens momentum" in out
    assert "  Rate Hike Effect: Amplifies drawdowns" in out


def test_bullets_links_and_code_are_readable():
    out = telegram_text.to_plain_text(
        "- `/research macro` — current posture\n"
        "- see [the docs](https://example.com/a)\n"
        "> quoted line\n"
    )
    assert "• /research macro — current posture" in out
    assert "• see the docs (https://example.com/a)" in out
    assert "quoted line" in out
    assert ">" not in out


def test_underscores_inside_words_survive():
    out = telegram_text.to_plain_text("hints are avoid_new_long and h12_sfp_count")
    assert out == "hints are avoid_new_long and h12_sfp_count"


def test_fenced_code_is_left_alone():
    out = telegram_text.to_plain_text("```\nrisk = 1_000 * 0.5\n```")
    assert out == "risk = 1_000 * 0.5"
