from __future__ import annotations

import logging

from lucid_component_base import Component, ComponentContext
from lucid_component_base.mqtt_log_handler import MQTTLogHandler


class _FakeMqtt:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict, bool, int]] = []

    def publish(self, topic: str, payload, *, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload, retain, qos))


class _DummyComponent(Component):
    @property
    def component_id(self) -> str:
        return "dummy"

    def _start(self) -> None:
        pass

    def _stop(self) -> None:
        pass


def _context() -> ComponentContext:
    return ComponentContext.create(
        agent_id="agent_1",
        base_topic="lucid/agents/agent_1",
        component_id="dummy",
        mqtt=_FakeMqtt(),
        config={},
    )


def test_component_mqtt_log_handler_publishes_structured_lines():
    comp = _DummyComponent(_context())
    calls: list[tuple[str, dict]] = []
    comp._publish_json = lambda topic, payload, **kwargs: calls.append((topic, payload))
    handler = MQTTLogHandler(comp, "lucid/agents/agent_1/components/dummy/logs")

    record = logging.LogRecord(
        name="lucid.component.dummy",
        level=logging.ERROR,
        pathname=__file__,
        lineno=48,
        msg="component %s",
        args=("up",),
        exc_info=None,
        func="test_fn",
        sinfo=None,
    )
    handler.emit(record)
    handler._publish_batch()

    assert len(calls) == 1
    payload = calls[0][1]
    assert payload["count"] == 1
    line = payload["lines"][0]
    assert line["level"] == "error"
    assert line["message"] == "component up"
    for key in ("ts", "logger"):
        assert key in line
    for key in ("module", "function", "file", "line", "thread", "process", "stack", "formatted"):
        assert key not in line
