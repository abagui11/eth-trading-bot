"""Shared test fixtures."""

from __future__ import annotations

import pytest

import config


@pytest.fixture(autouse=True)
def no_twitter(monkeypatch):
    """Never post real tweets from tests, even with live keys in .env."""
    monkeypatch.setattr(config, "TWITTER_ENABLED", False)
