"""Telemetry init: idempotent, no-exporter is the silent default.

These don't exercise the OTLP exporter (that needs a real collector) —
they pin the contract that ``init()`` is safe to call repeatedly and
that the global tracer always returns spans, exporter or not.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def fresh_telemetry(monkeypatch):
    """Re-import telemetry with a clean ``_initialised`` flag and a
    cleared OTel environment so each test starts from zero."""
    for var in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_CONSOLE_EXPORTER"):
        monkeypatch.delenv(var, raising=False)
    import telemetry as t
    importlib.reload(t)
    t._initialised = False
    return t


def test_init_idempotent(fresh_telemetry):
    fresh_telemetry.init()
    fresh_telemetry.init()  # second call must not raise / re-register
    assert fresh_telemetry._initialised is True


def test_get_tracer_returns_real_object_before_init(fresh_telemetry):
    # The chat handler calls get_tracer() at import time; spans must be
    # creatable even if init() hasn't run yet (lifespan order).
    tracer = fresh_telemetry.get_tracer("test")
    with tracer.start_as_current_span("smoke") as span:
        span.set_attribute("k", 1)
    # No exception = pass.


def test_get_tracer_works_after_init_with_no_exporter(fresh_telemetry):
    fresh_telemetry.init()
    tracer = fresh_telemetry.get_tracer("test")
    with tracer.start_as_current_span("after-init") as span:
        span.set_attribute("k", 2)


def test_truthy_helper(fresh_telemetry):
    truthy = fresh_telemetry._truthy
    assert truthy("true") is True
    assert truthy("TRUE") is True
    assert truthy(" yes ") is True
    assert truthy("on") is True
    assert truthy("1") is True
    assert truthy("false") is False
    assert truthy("0") is False
    assert truthy("") is False
    assert truthy(None) is False


def test_console_exporter_env_enables_processor(fresh_telemetry, monkeypatch, caplog):
    """Setting OTEL_CONSOLE_EXPORTER=true should add a processor and log it.
    We don't assert what the SDK actually exported (that's the SDK's job),
    only that our wiring took the branch."""
    monkeypatch.setenv("OTEL_CONSOLE_EXPORTER", "true")
    with caplog.at_level("INFO", logger="telemetry"):
        fresh_telemetry.init()
    assert any("console exporter enabled" in m for m in caplog.messages)
