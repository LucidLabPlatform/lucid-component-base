"""
Tests for MQTTLogHandler — batching, rate limiting, level mapping, exception formatting.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest

from lucid_component_base import Component, ComponentContext
from lucid_component_base.mqtt_log_handler import (
    BATCH_INTERVAL_S,
    MAX_BATCHES_PER_WINDOW,
    MAX_LINES_PER_BATCH,
    TIME_WINDOW_S,
    MQTTLogHandler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeMqtt:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish(self, topic: str, payload: Any, *, qos: int = 0, retain: bool = False) -> None:
        self.calls.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})


class DummyComponent(Component):
    @property
    def component_id(self) -> str:
        return "dummy"

    def _start(self) -> None:
        pass

    def _stop(self) -> None:
        pass


def make_component_and_handler() -> tuple[DummyComponent, MQTTLogHandler, list]:
    mqtt = FakeMqtt()
    ctx = ComponentContext.create(
        agent_id="a1",
        base_topic="lucid/agents/a1",
        component_id="dummy",
        mqtt=mqtt,
        config={},
    )
    comp = DummyComponent(ctx)
    calls: list[tuple[str, dict]] = []
    comp._publish_json = lambda topic, payload, **kwargs: calls.append((topic, payload))
    handler = MQTTLogHandler(
        lambda topic, payload: comp._publish_json(topic, payload),
        "lucid/agents/a1/components/dummy/logs",
    )
    return comp, handler, calls


def make_record(
    name: str = "lucid.component.dummy",
    level: int = logging.INFO,
    msg: str = "test message",
    args: tuple = (),
    exc_info=None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


# ---------------------------------------------------------------------------
# Level mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "levelno, expected",
    [
        (logging.DEBUG, "debug"),
        (logging.INFO, "info"),
        (logging.WARNING, "warning"),
        (logging.ERROR, "error"),
        (logging.CRITICAL, "error"),
        (999, "info"),  # unknown level defaults to info
    ],
)
def test_level_to_mqtt(levelno: int, expected: str):
    assert MQTTLogHandler._level_to_mqtt(levelno) == expected


# ---------------------------------------------------------------------------
# UTC timestamp
# ---------------------------------------------------------------------------


def test_utc_iso_from_epoch():
    ts = 0.0
    result = MQTTLogHandler._utc_iso_from_epoch(ts)
    assert "1970" in result


# ---------------------------------------------------------------------------
# _build_line
# ---------------------------------------------------------------------------


def test_build_line_basic():
    _, handler, _ = make_component_and_handler()
    record = make_record(name="some.logger", level=logging.WARNING, msg="hello %s", args=("world",))
    line = handler._build_line(record)
    assert line["level"] == "warning"
    assert line["logger"] == "some.logger"
    assert line["message"] == "hello world"
    assert "ts" in line


def test_build_line_with_exception():
    _, handler, _ = make_component_and_handler()
    try:
        raise ValueError("oops")
    except ValueError:
        import sys
        exc_info = sys.exc_info()

    record = make_record(exc_info=exc_info)
    line = handler._build_line(record)
    assert "exception" in line
    assert "ValueError" in line["exception"]


def test_build_line_with_exc_text():
    _, handler, _ = make_component_and_handler()
    record = make_record()
    record.exc_text = "some formatted exception text"
    line = handler._build_line(record)
    assert line["exception"] == "some formatted exception text"


# ---------------------------------------------------------------------------
# emit and batch publishing
# ---------------------------------------------------------------------------


def test_emit_adds_to_buffer():
    _, handler, calls = make_component_and_handler()
    handler.emit(make_record())
    assert len(handler._buffer) == 1


def test_emit_and_explicit_publish_batch():
    _, handler, calls = make_component_and_handler()
    handler.emit(make_record(msg="msg1"))
    handler.emit(make_record(msg="msg2"))
    handler._publish_batch()
    assert len(calls) == 1
    payload = calls[0][1]
    assert payload["count"] == 2
    assert len(payload["lines"]) == 2


def test_publish_batch_clears_buffer():
    _, handler, calls = make_component_and_handler()
    handler.emit(make_record())
    handler._publish_batch()
    assert len(handler._buffer) == 0


def test_publish_batch_noop_on_empty_buffer():
    _, handler, calls = make_component_and_handler()
    handler._publish_batch()
    assert len(calls) == 0


def test_emit_triggers_immediate_publish_when_full():
    _, handler, calls = make_component_and_handler()
    for i in range(MAX_LINES_PER_BATCH):
        handler.emit(make_record(msg=f"line {i}"))
    # Should have published immediately when buffer hit MAX_LINES_PER_BATCH
    # After filling the buffer, _publish_batch is called directly
    assert len(calls) >= 1
    total_lines = sum(c[1]["count"] for c in calls)
    assert total_lines == MAX_LINES_PER_BATCH


def test_emit_ignores_own_module_logs():
    """Handler ignores log records from its own module to prevent recursion."""
    import lucid_component_base.mqtt_log_handler as mod
    _, handler, calls = make_component_and_handler()
    record = make_record(name=mod.__name__)
    handler.emit(record)
    # Nothing should be buffered
    assert len(handler._buffer) == 0


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_drops_when_exceeded():
    _, handler, calls = make_component_and_handler()
    # Saturate the window
    now = time.time()
    handler._batch_timestamps = [now] * MAX_BATCHES_PER_WINDOW
    # Prevent the warning from firing (which would reset _dropped_count to 0)
    handler._last_warning_ts = now

    handler.emit(make_record())
    handler._publish_batch()  # should be dropped due to rate limit

    # The buffer should be cleared but nothing published
    assert len(handler._buffer) == 0
    assert len(calls) == 0
    assert handler._dropped_count > 0


def test_rate_limit_warn_on_drops():
    _, handler, calls = make_component_and_handler()
    now = time.time()
    handler._batch_timestamps = [now] * MAX_BATCHES_PER_WINDOW
    handler.emit(make_record())
    handler._publish_batch()

    # Warning should be triggered (last_warning_ts starts at 0)
    assert handler._last_warning_ts > 0


def test_rate_limit_warn_suppressed_within_window():
    _, handler, calls = make_component_and_handler()
    now = time.time()
    handler._batch_timestamps = [now] * MAX_BATCHES_PER_WINDOW
    handler._last_warning_ts = now  # already warned recently

    initial_dropped = handler._dropped_count
    handler.emit(make_record())
    handler._publish_batch()

    # _warn_rate_limit_drop should NOT emit (suppressed)
    # _last_warning_ts should not have changed
    assert handler._last_warning_ts == now


# ---------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------


def test_timer_fires_and_publishes_batch():
    """Emit a record and wait for the timer to fire."""
    _, handler, calls = make_component_and_handler()
    handler.emit(make_record(msg="timer test"))
    # Wait slightly longer than BATCH_INTERVAL_S
    deadline = time.time() + BATCH_INTERVAL_S + 0.5
    while time.time() < deadline and len(calls) == 0:
        time.sleep(0.05)
    assert len(calls) == 1


def test_timer_cancelled_by_full_buffer():
    """When the buffer is flushed on full, any pending timer should be cancelled."""
    _, handler, calls = make_component_and_handler()
    # Start a timer manually
    timer = threading.Timer(10, lambda: None)
    timer.daemon = True
    timer.start()
    with handler._lock:
        handler._batch_timer = timer

    # Fill buffer to trigger immediate flush
    for i in range(MAX_LINES_PER_BATCH):
        handler.emit(make_record(msg=f"line {i}"))

    # Timer should have been cancelled
    with handler._lock:
        assert handler._batch_timer is None


# ---------------------------------------------------------------------------
# _publish_batch_timer
# ---------------------------------------------------------------------------


def test_publish_batch_timer_clears_timer_ref():
    _, handler, calls = make_component_and_handler()
    handler.emit(make_record())
    # Simulate timer callback
    handler._publish_batch_timer()
    with handler._lock:
        assert handler._batch_timer is None


def test_publish_batch_timer_noop_when_buffer_empty():
    _, handler, calls = make_component_and_handler()
    handler._publish_batch_timer()
    assert len(calls) == 0


# ---------------------------------------------------------------------------
# Theme 9 — publish_fn callable interface
# ---------------------------------------------------------------------------


def test_handler_calls_publish_fn_with_topic_and_payload():
    """MQTTLogHandler calls the publish_fn callable with the correct topic and payload."""
    calls: list[tuple[str, dict]] = []
    topic = "lucid/agents/a1/components/dummy/logs"

    handler = MQTTLogHandler(lambda t, p: calls.append((t, p)), topic)

    record = make_record(msg="callable test", level=logging.WARNING)
    handler.emit(record)
    handler._publish_batch()

    assert len(calls) == 1
    assert calls[0][0] == topic
    payload = calls[0][1]
    assert payload["count"] == 1
    assert payload["lines"][0]["message"] == "callable test"
    assert payload["lines"][0]["level"] == "warning"


def test_handler_does_not_reference_component_class():
    """MQTTLogHandler must not import or use Component — it only uses the callable."""
    import inspect
    import lucid_component_base.mqtt_log_handler as mod

    source = inspect.getsource(mod)
    assert "Component" not in source, "MQTTLogHandler must not couple to Component"
