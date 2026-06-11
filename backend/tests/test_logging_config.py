"""Coverage for the central logging configuration (observability.logging_config).

The logging additions across the rest of the backend are behavior-neutral log
statements (no tests warranted — asserting log strings is brittle). This file
covers the only new *logic*: configure_logging()'s level resolution, idempotency,
noisy-logger pinning, and never-raise fallback. The autouse fixture saves and
restores global logging state so these tests don't pollute the rest of the suite.
"""

from __future__ import annotations

import logging

import pytest

from observability import logging_config


@pytest.fixture(autouse=True)
def _restore_logging_state():
    root = logging.getLogger()
    saved_level = root.level
    saved_configured = logging_config._configured
    saved_noisy = {n: logging.getLogger(n).level for n in logging_config._NOISY_LOGGERS}
    try:
        yield
    finally:
        root.setLevel(saved_level)
        logging_config._configured = saved_configured
        for name, lvl in saved_noisy.items():
            logging.getLogger(name).setLevel(lvl)


def test_configure_logging_sets_root_level() -> None:
    logging_config._configured = False
    logging_config.configure_logging(level="WARNING", force=True)
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_pins_noisy_third_party_loggers() -> None:
    logging_config._configured = False
    logging_config.configure_logging(level="DEBUG", force=True)
    assert logging.getLogger("httpx").level == logging.WARNING


def test_configure_logging_is_idempotent_without_force() -> None:
    logging_config._configured = False
    logging_config.configure_logging(level="ERROR", force=True)
    # A second call without force must not reconfigure — level stays ERROR.
    logging_config.configure_logging(level="DEBUG")
    assert logging.getLogger().level == logging.ERROR


def test_configure_logging_falls_back_to_info_without_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(config, "get_settings", _boom)
    logging_config._configured = False
    # level=None → resolve from settings → settings raise → must fall back to INFO.
    logging_config.configure_logging(force=True)
    assert logging.getLogger().level == logging.INFO


def test_log_level_setting_exists() -> None:
    from config import Settings

    assert isinstance(Settings().log_level, str)
