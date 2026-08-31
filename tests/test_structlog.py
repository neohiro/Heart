"""Test shared structlog configuration in Heart/tools/_structlog.py."""
import logging

import structlog

from _structlog import _drop_none, configure_logger


def test_drop_none_removes_none_values():
    event_dict = {"event": "test", "counter_id": None, "phase": "scrape", "level": "info"}
    out = _drop_none(None, "info", event_dict)
    assert "counter_id" not in out
    assert out["event"] == "test"
    assert out["phase"] == "scrape"
    # level is a string, not None — _drop_none must preserve it.
    assert out["level"] == "info"


def test_drop_none_preserves_falsy_values():
    event_dict = {"event": "test", "successes": 0, "total": 5, "phase": "scrape"}
    out = _drop_none(None, "info", event_dict)
    assert out["successes"] == 0
    assert out["total"] == 5


def test_configure_logger_returns_bound_logger():
    LOG = configure_logger("heart.test_configure", key_order=["event", "phase"])
    assert LOG is not None
    # Idempotent: re-configuring should not error.
    LOG2 = configure_logger("heart.test_configure", key_order=["event", "phase"])
    assert LOG2 is not None


def test_configure_logger_emits_to_stdlib(caplog):
    caplog.set_level(logging.INFO, logger="heart.test_configure")
    LOG = configure_logger("heart.test_configure", key_order=["event", "phase", "counter_id"])
    LOG.info("test_event", phase="scrape", counter_id="abc123")
    assert any("test_event" in r.getMessage() for r in caplog.records)
    assert any("counter_id='abc123'" in r.getMessage() for r in caplog.records)
