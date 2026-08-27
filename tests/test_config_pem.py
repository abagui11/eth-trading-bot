"""CDP PEM overlay: systemd EnvironmentFile must not win over .env."""

from __future__ import annotations

from pathlib import Path

import config


def test_overlay_dotenv_keys_replaces_mangled_systemd_pem(tmp_path: Path):
    pem_file = (
        "-----BEGIN EC PRIVATE KEY-----\\n"
        "MHcCAQEEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoGCCqGSM49\\n"
        "-----END EC PRIVATE KEY-----\\n"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(f"COINBASE_CDP_PRIVATE_KEY={pem_file}\n", encoding="utf-8")
    environ = {"COINBASE_CDP_PRIVATE_KEY": "-----BEGIN EC PRIVATE KEY-----nMANGLED"}
    config.overlay_dotenv_keys(env_file, environ, "COINBASE_CDP_PRIVATE_KEY")
    assert environ["COINBASE_CDP_PRIVATE_KEY"] != "-----BEGIN EC PRIVATE KEY-----nMANGLED"
    assert "BEGIN EC PRIVATE KEY" in environ["COINBASE_CDP_PRIVATE_KEY"]
    assert environ["COINBASE_CDP_PRIVATE_KEY"].count("\\n") >= 2


def test_overlay_dotenv_keys_skips_missing_file(tmp_path: Path):
    environ: dict[str, str] = {"COINBASE_CDP_PRIVATE_KEY": "keep"}
    config.overlay_dotenv_keys(tmp_path / "nope.env", environ, "COINBASE_CDP_PRIVATE_KEY")
    assert environ["COINBASE_CDP_PRIVATE_KEY"] == "keep"
