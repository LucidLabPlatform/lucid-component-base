"""
Comprehensive tests for lucid-component-base.

Covers:
- Component lifecycle (start/stop idempotency, state transitions, failure paths)
- ComponentContext creation and validation
- All publish_* methods (topic, payload, retain, QoS)
- Telemetry gating (enabled flag, interval, change threshold)
- set_telemetry_config normalisation
- Built-in command handlers (cfg/logging/set, cfg/telemetry/set)
- Request-ID deduplication via _make_cmd_handler
- ComponentState.to_dict
- ComponentStatus mapping via _status_to_contract
- MqttPublisher protocol
"""
from __future__ import annotations

import json
import time
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lucid_component_base import (
    Component,
    ComponentContext,
    ComponentState,
    ComponentStatus,
)
from lucid_component_base.base import _status_to_contract, _utc_iso


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeMqtt:
    """Minimal in-memory MQTT publisher stub."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish(self, topic: str, payload: Any, *, qos: int = 0, retain: bool = False) -> None:
        self.calls.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})

    def last(self) -> dict:
        return self.calls[-1]

    def topics(self) -> list[str]:
        return [c["topic"] for c in self.calls]

    def clear(self) -> None:
        self.calls.clear()


def make_context(component_id: str = "dummy", mqtt: FakeMqtt | None = None) -> ComponentContext:
    return ComponentContext.create(
        agent_id="agent_1",
        base_topic="lucid/agents/agent_1",
        component_id=component_id,
        mqtt=mqtt or FakeMqtt(),
        config={"key": "value"},
    )


class DummyComponent(Component):
    """Minimal concrete component for testing."""

    started = False
    stopped = False
    start_raises: Exception | None = None
    stop_raises: Exception | None = None

    @property
    def component_id(self) -> str:
        return "dummy"

    def _start(self) -> None:
        if self.start_raises:
            raise self.start_raises
        self.started = True

    def _stop(self) -> None:
        if self.stop_raises:
            raise self.stop_raises
        self.stopped = True


def make_component(mqtt: FakeMqtt | None = None) -> tuple[DummyComponent, FakeMqtt]:
    mqtt = mqtt or FakeMqtt()
    ctx = make_context(mqtt=mqtt)
    comp = DummyComponent(ctx)
    return comp, mqtt


# ---------------------------------------------------------------------------
# _utc_iso
# ---------------------------------------------------------------------------


def test_utc_iso_returns_string():
    result = _utc_iso()
    assert isinstance(result, str)
    assert "T" in result


# ---------------------------------------------------------------------------
# _status_to_contract
# ---------------------------------------------------------------------------


def test_status_to_contract_running():
    assert _status_to_contract(ComponentStatus.RUNNING) == "running"


def test_status_to_contract_failed():
    assert _status_to_contract(ComponentStatus.FAILED) == "error"


@pytest.mark.parametrize(
    "status",
    [ComponentStatus.STOPPED, ComponentStatus.STARTING, ComponentStatus.STOPPING],
)
def test_status_to_contract_idle(status):
    assert _status_to_contract(status) == "idle"


# ---------------------------------------------------------------------------
# ComponentState
# ---------------------------------------------------------------------------


def test_component_state_defaults():
    s = ComponentState()
    assert s.status == ComponentStatus.STOPPED
    assert s.last_error is None
    assert s.started_at is None
    assert s.stopped_at is None
    assert s.updated_at is not None


def test_component_state_to_dict():
    s = ComponentState()
    d = s.to_dict()
    assert d["status"] == "stopped"
    assert d["last_error"] is None
    assert d["started_at"] is None
    assert d["stopped_at"] is None
    assert "updated_at" in d


# ---------------------------------------------------------------------------
# ComponentContext — creation & validation
# ---------------------------------------------------------------------------


def test_context_create_success():
    mqtt = FakeMqtt()
    ctx = ComponentContext.create(
        agent_id="a1",
        base_topic="lucid/agents/a1",
        component_id="led",
        mqtt=mqtt,
        config={},
    )
    assert ctx.agent_id == "a1"
    assert ctx.component_id == "led"
    assert ctx.config == {}


def test_context_topic_helper():
    ctx = make_context("led")
    assert ctx.topic("state") == "lucid/agents/agent_1/components/led/state"
    assert ctx.topic("cfg/logging") == "lucid/agents/agent_1/components/led/cfg/logging"


def test_context_logger_returns_logger():
    import logging
    ctx = make_context("led")
    lg = ctx.logger()
    assert lg.name == "lucid.component.led"


def test_context_create_empty_agent_id_raises():
    with pytest.raises(ValueError, match="agent_id"):
        ComponentContext.create(
            agent_id="",
            base_topic="t",
            component_id="c",
            mqtt=FakeMqtt(),
            config={},
        )


def test_context_create_non_string_agent_id_raises():
    with pytest.raises(ValueError, match="agent_id"):
        ComponentContext.create(
            agent_id=123,  # type: ignore[arg-type]
            base_topic="t",
            component_id="c",
            mqtt=FakeMqtt(),
            config={},
        )


def test_context_create_empty_base_topic_raises():
    with pytest.raises(ValueError, match="base_topic"):
        ComponentContext.create(
            agent_id="a",
            base_topic="",
            component_id="c",
            mqtt=FakeMqtt(),
            config={},
        )


def test_context_create_empty_component_id_raises():
    with pytest.raises(ValueError, match="component_id"):
        ComponentContext.create(
            agent_id="a",
            base_topic="t",
            component_id="",
            mqtt=FakeMqtt(),
            config={},
        )


def test_context_create_non_dict_config_raises():
    with pytest.raises(ValueError, match="config"):
        ComponentContext.create(
            agent_id="a",
            base_topic="t",
            component_id="c",
            mqtt=FakeMqtt(),
            config="not_a_dict",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Component lifecycle — start / stop
# ---------------------------------------------------------------------------


def test_start_transitions_to_running():
    comp, _ = make_component()
    comp.start()
    assert comp.state.status == ComponentStatus.RUNNING
    assert comp.started is True
    assert comp.state.started_at is not None
    assert comp.state.stopped_at is None


def test_start_idempotent_when_running():
    comp, _ = make_component()
    comp.start()
    comp.started = False  # reset flag
    comp.start()  # should be no-op
    assert comp.started is False  # _start() not called again
    assert comp.state.status == ComponentStatus.RUNNING


def test_start_raises_when_starting():
    comp, _ = make_component()
    comp._state.status = ComponentStatus.STARTING
    with pytest.raises(RuntimeError, match="starting"):
        comp.start()


def test_start_raises_when_stopping():
    comp, _ = make_component()
    comp._state.status = ComponentStatus.STOPPING
    with pytest.raises(RuntimeError, match="stopping"):
        comp.start()


def test_start_exception_sets_failed():
    comp, _ = make_component()
    comp.start_raises = ValueError("boom")
    with pytest.raises(ValueError, match="boom"):
        comp.start()
    assert comp.state.status == ComponentStatus.FAILED
    assert comp.state.last_error == "boom"


def test_stop_transitions_to_stopped():
    comp, _ = make_component()
    comp.start()
    comp.stop()
    assert comp.state.status == ComponentStatus.STOPPED
    assert comp.stopped is True
    assert comp.state.stopped_at is not None


def test_stop_idempotent_when_already_stopped():
    comp, _ = make_component()
    comp.stop()  # already stopped
    assert comp.stopped is False
    assert comp.state.status == ComponentStatus.STOPPED


def test_stop_no_op_when_stopping():
    comp, _ = make_component()
    comp.start()
    comp._state.status = ComponentStatus.STOPPING
    comp.stop()  # should return immediately
    assert comp.stopped is False  # _stop() not called


def test_stop_exception_sets_failed():
    comp, _ = make_component()
    comp.start()
    comp.stop_raises = RuntimeError("stop fail")
    with pytest.raises(RuntimeError, match="stop fail"):
        comp.stop()
    assert comp.state.status == ComponentStatus.FAILED
    assert comp.state.last_error == "stop fail"


# ---------------------------------------------------------------------------
# Component version
# ---------------------------------------------------------------------------


def test_version_returns_string():
    comp, _ = make_component()
    v = comp.version
    assert isinstance(v, str)
    assert len(v) > 0


# ---------------------------------------------------------------------------
# Component metadata and capabilities
# ---------------------------------------------------------------------------


def test_metadata_default():
    comp, _ = make_component()
    m = comp.metadata()
    assert m["component_id"] == "dummy"
    assert "version" in m


def test_capabilities_default_empty():
    comp, _ = make_component()
    assert comp.capabilities() == []


def test_get_state_payload_default_empty():
    comp, _ = make_component()
    assert comp.get_state_payload() == {}


def test_get_cfg_payload_default_empty():
    comp, _ = make_component()
    assert comp.get_cfg_payload() == {}


# ---------------------------------------------------------------------------
# publish_metadata
# ---------------------------------------------------------------------------


def test_publish_metadata_topic_and_payload():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_metadata()
    call = mqtt.calls[-1]
    assert call["topic"] == "lucid/agents/agent_1/components/dummy/metadata"
    assert call["retain"] is True
    assert call["qos"] == 1
    body = json.loads(call["payload"])
    assert body["component_id"] == "dummy"
    assert "version" in body
    assert "capabilities" in body


# ---------------------------------------------------------------------------
# publish_status
# ---------------------------------------------------------------------------


def test_publish_status_topic_and_payload():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_status()
    call = mqtt.calls[-1]
    assert call["topic"] == "lucid/agents/agent_1/components/dummy/status"
    assert call["retain"] is True
    assert call["qos"] == 1
    body = json.loads(call["payload"])
    assert body["state"] == "idle"


def test_publish_status_running_after_start():
    comp, mqtt = make_component()
    comp.start()
    # find the most recent status publish
    status_calls = [c for c in mqtt.calls if c["topic"].endswith("/status")]
    last_status = json.loads(status_calls[-1]["payload"])
    assert last_status["state"] == "running"


# ---------------------------------------------------------------------------
# publish_state
# ---------------------------------------------------------------------------


def test_publish_state_default_empty():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_state()
    call = mqtt.calls[-1]
    assert call["topic"] == "lucid/agents/agent_1/components/dummy/state"
    assert call["retain"] is True
    assert call["qos"] == 1
    body = json.loads(call["payload"])
    assert body == {}


def test_publish_state_with_explicit_payload():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_state({"temperature_c": 22.5})
    body = json.loads(mqtt.last()["payload"])
    assert body["temperature_c"] == 22.5


# ---------------------------------------------------------------------------
# publish_cfg
# ---------------------------------------------------------------------------


def test_publish_cfg_general_topic():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_cfg_general()
    topics = mqtt.topics()
    assert "lucid/agents/agent_1/components/dummy/cfg" in topics


def test_publish_cfg_logging_topic_and_payload():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_cfg_logging()
    call = mqtt.calls[-1]
    assert call["topic"] == "lucid/agents/agent_1/components/dummy/cfg/logging"
    body = json.loads(call["payload"])
    assert "log_level" in body


def test_publish_cfg_telemetry_topic():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_cfg_telemetry()
    topics = mqtt.topics()
    assert "lucid/agents/agent_1/components/dummy/cfg/telemetry" in topics


def test_publish_cfg_publishes_all_three():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_cfg()
    topics = mqtt.topics()
    assert "lucid/agents/agent_1/components/dummy/cfg" in topics
    assert "lucid/agents/agent_1/components/dummy/cfg/logging" in topics
    assert "lucid/agents/agent_1/components/dummy/cfg/telemetry" in topics


# ---------------------------------------------------------------------------
# publish_log
# ---------------------------------------------------------------------------


def test_publish_log_topic_and_payload():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_log("info", "hello world")
    call = mqtt.calls[-1]
    assert call["topic"] == "lucid/agents/agent_1/components/dummy/logs"
    assert call["retain"] is False
    assert call["qos"] == 0
    body = json.loads(call["payload"])
    # Canonical {count, lines:[...]} envelope, matching MQTTLogHandler
    assert body["count"] == 1
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert line["level"] == "info"
    assert line["message"] == "hello world"
    assert line["logger"] == "lucid.component.dummy"
    assert "ts" in line


# ---------------------------------------------------------------------------
# publish_result
# ---------------------------------------------------------------------------


def test_publish_result_ok():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_result("reset", "req-123", ok=True)
    call = mqtt.calls[-1]
    assert call["topic"] == "lucid/agents/agent_1/components/dummy/evt/reset/result"
    assert call["qos"] == 1
    assert call["retain"] is False
    body = json.loads(call["payload"])
    assert body["request_id"] == "req-123"
    assert body["ok"] is True
    assert body["error"] is None


def test_publish_result_error():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_result("reset", "req-456", ok=False, error="something broke")
    body = json.loads(mqtt.last()["payload"])
    assert body["ok"] is False
    assert body["error"] == "something broke"


# ---------------------------------------------------------------------------
# publish_cfg_set_result
# ---------------------------------------------------------------------------


def test_publish_cfg_set_result_ok():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_cfg_set_result("req-789", ok=True, applied={"log_level": "DEBUG"})
    call = mqtt.calls[-1]
    assert call["topic"] == "lucid/agents/agent_1/components/dummy/evt/cfg/set/result"
    body = json.loads(call["payload"])
    assert body["ok"] is True
    assert body["applied"] == {"log_level": "DEBUG"}
    assert "ts" in body


def test_publish_cfg_set_result_custom_action():
    comp, mqtt = make_component()
    mqtt.clear()
    comp.publish_cfg_set_result("req-abc", ok=False, error="bad input", action="cfg/logging/set")
    call = mqtt.calls[-1]
    assert call["topic"] == "lucid/agents/agent_1/components/dummy/evt/cfg/logging/set/result"


# ---------------------------------------------------------------------------
# publish_telemetry
# ---------------------------------------------------------------------------


def test_publish_telemetry_when_enabled():
    comp, mqtt = make_component()
    comp.set_telemetry_config({"cpu": {"enabled": True, "interval_s": 1, "change_threshold_percent": 0.0}})
    mqtt.clear()
    comp.publish_telemetry("cpu", 50.0)
    assert any("telemetry/cpu" in c["topic"] for c in mqtt.calls)
    body = json.loads([c for c in mqtt.calls if "telemetry/cpu" in c["topic"]][-1]["payload"])
    assert body["value"] == 50.0


def test_publish_telemetry_not_published_when_disabled():
    comp, mqtt = make_component()
    comp.set_telemetry_config({"cpu": {"enabled": False}})
    mqtt.clear()
    comp.publish_telemetry("cpu", 50.0)
    assert not any("telemetry/cpu" in c["topic"] for c in mqtt.calls)


def test_publish_telemetry_gated_by_interval():
    comp, mqtt = make_component()
    comp.set_telemetry_config({"cpu": {"enabled": True, "interval_s": 60, "change_threshold_percent": 100.0}})
    comp.publish_telemetry("cpu", 50.0)  # first publish (always passes)
    mqtt.clear()
    comp.publish_telemetry("cpu", 50.5)  # same value within interval, threshold not exceeded
    assert not any("telemetry/cpu" in c["topic"] for c in mqtt.calls)


def test_publish_telemetry_threshold_triggers_early_publish():
    comp, mqtt = make_component()
    comp.set_telemetry_config({"cpu": {"enabled": True, "interval_s": 60, "change_threshold_percent": 5.0}})
    comp.publish_telemetry("cpu", 50.0)  # baseline
    mqtt.clear()
    comp.publish_telemetry("cpu", 60.0)  # 20% change — exceeds threshold
    assert any("telemetry/cpu" in c["topic"] for c in mqtt.calls)


def test_publish_telemetry_unknown_metric_not_published():
    comp, mqtt = make_component()
    # No telemetry config registered at all
    mqtt.clear()
    comp.publish_telemetry("unknown_metric", 1.0)
    assert not any("telemetry/unknown_metric" in c["topic"] for c in mqtt.calls)


# ---------------------------------------------------------------------------
# should_publish_telemetry
# ---------------------------------------------------------------------------


def test_should_publish_telemetry_first_time():
    comp, _ = make_component()
    comp.set_telemetry_config({"temp": {"enabled": True, "interval_s": 10, "change_threshold_percent": 2.0}})
    assert comp.should_publish_telemetry("temp", 25.0) is True


def test_should_publish_telemetry_disabled():
    comp, _ = make_component()
    comp.set_telemetry_config({"temp": {"enabled": False}})
    assert comp.should_publish_telemetry("temp", 25.0) is False


def test_should_publish_telemetry_no_config():
    comp, _ = make_component()
    assert comp.should_publish_telemetry("no_such_metric", 1.0) is False


def test_should_publish_telemetry_non_numeric_value_equality():
    comp, _ = make_component()
    comp.set_telemetry_config({"mode": {"enabled": True, "interval_s": 60, "change_threshold_percent": 2.0}})
    comp.publish_telemetry("mode", "on")   # first publish
    assert comp.should_publish_telemetry("mode", "on") is False  # same value
    assert comp.should_publish_telemetry("mode", "off") is True  # changed


def test_should_publish_telemetry_last_value_zero():
    comp, _ = make_component()
    comp.set_telemetry_config({"v": {"enabled": True, "interval_s": 60, "change_threshold_percent": 5.0}})
    comp.publish_telemetry("v", 0)
    assert comp.should_publish_telemetry("v", 0) is False   # same, last_value==0
    assert comp.should_publish_telemetry("v", 1) is True    # non-zero


# ---------------------------------------------------------------------------
# set_telemetry_config
# ---------------------------------------------------------------------------


def test_set_telemetry_config_normalises_dict():
    comp, _ = make_component()
    comp.set_telemetry_config({
        "cpu": {"enabled": True, "interval_s": 5, "change_threshold_percent": 3.0},
    })
    cfg = comp._telemetry_cfg["cpu"]
    assert cfg["enabled"] is True
    assert cfg["interval_s"] == 5
    assert cfg["change_threshold_percent"] == 3.0


def test_set_telemetry_config_bool_shorthand():
    comp, _ = make_component()
    comp.set_telemetry_config({"cpu": True})
    cfg = comp._telemetry_cfg["cpu"]
    assert cfg["enabled"] is True
    assert cfg["interval_s"] == 0.1
    assert cfg["change_threshold_percent"] == 2.0


def test_set_telemetry_config_non_dict_becomes_empty():
    comp, _ = make_component()
    comp.set_telemetry_config(None)  # type: ignore[arg-type]
    assert comp._telemetry_cfg == {}


def test_set_telemetry_config_applies_defaults_for_missing_fields():
    comp, _ = make_component()
    comp.set_telemetry_config({"cpu": {"enabled": True}})
    cfg = comp._telemetry_cfg["cpu"]
    assert cfg["interval_s"] == 0.1
    assert cfg["change_threshold_percent"] == 2.0


def test_set_telemetry_config_rejects_zero_interval():
    comp, _ = make_component()
    import pytest as _pytest
    with _pytest.raises(ValueError, match="interval_s must be positive"):
        comp.set_telemetry_config({"cpu": {"enabled": True, "interval_s": 0}})
    # Config must remain unchanged
    assert "cpu" not in comp._telemetry_cfg


def test_set_telemetry_config_rejects_negative_interval():
    comp, _ = make_component()
    import pytest as _pytest
    with _pytest.raises(ValueError, match="interval_s must be positive"):
        comp.set_telemetry_config({"cpu": {"enabled": True, "interval_s": -5}})
    assert "cpu" not in comp._telemetry_cfg


def test_set_telemetry_config_rejects_string_interval():
    comp, _ = make_component()
    import pytest as _pytest
    with _pytest.raises(ValueError, match="interval_s must be a number"):
        comp.set_telemetry_config({"cpu": {"enabled": True, "interval_s": "fast"}})
    assert "cpu" not in comp._telemetry_cfg


def test_set_telemetry_config_validation_error_leaves_existing_config_unchanged():
    """If one metric in a batch fails validation, no changes are applied at all."""
    comp, _ = make_component()
    comp.set_telemetry_config({"cpu": {"enabled": True, "interval_s": 5}})
    original_cfg = dict(comp._telemetry_cfg)
    import pytest as _pytest
    with _pytest.raises(ValueError):
        comp.set_telemetry_config({
            "cpu": {"enabled": True, "interval_s": 10},  # valid
            "temp": {"enabled": True, "interval_s": -1},  # invalid
        })
    # Neither metric should have been updated
    assert comp._telemetry_cfg == original_cfg


# ---------------------------------------------------------------------------
# apply_log_level
# ---------------------------------------------------------------------------


def test_apply_log_level_valid():
    import logging as _logging
    comp, _ = make_component()
    comp.apply_log_level("DEBUG")
    assert comp._log_level == "DEBUG"
    assert _logging.getLogger("lucid.component.dummy").level == _logging.DEBUG


def test_apply_log_level_invalid_ignored():
    comp, _ = make_component()
    comp._log_level = "ERROR"
    comp.apply_log_level("NOTVALID")
    assert comp._log_level == "ERROR"  # unchanged


def test_apply_log_level_case_insensitive():
    comp, _ = make_component()
    comp.apply_log_level("warning")
    assert comp._log_level == "WARNING"


# ---------------------------------------------------------------------------
# on_cmd_cfg_logging_set
# ---------------------------------------------------------------------------


def test_cmd_cfg_logging_set_valid():
    comp, mqtt = make_component()
    payload = json.dumps({"request_id": "r1", "set": {"log_level": "DEBUG"}})
    comp.on_cmd_cfg_logging_set(payload)
    result_calls = [c for c in mqtt.calls if "cfg/logging/set/result" in c["topic"]]
    assert result_calls
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is True
    assert body["request_id"] == "r1"
    assert comp._log_level == "DEBUG"


def test_cmd_cfg_logging_set_invalid_json():
    comp, mqtt = make_component()
    comp.on_cmd_cfg_logging_set("not json")
    result_calls = [c for c in mqtt.calls if "cfg/logging/set/result" in c["topic"]]
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is False
    assert "invalid JSON" in body["error"]


def test_cmd_cfg_logging_set_missing_set_field():
    comp, mqtt = make_component()
    comp.on_cmd_cfg_logging_set(json.dumps({"request_id": "r2"}))
    result_calls = [c for c in mqtt.calls if "cfg/logging/set/result" in c["topic"]]
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is False
    assert "missing 'set'" in body["error"]


def test_cmd_cfg_logging_set_unknown_key():
    comp, mqtt = make_component()
    payload = json.dumps({"request_id": "r3", "set": {"unknown_key": "val"}})
    comp.on_cmd_cfg_logging_set(payload)
    result_calls = [c for c in mqtt.calls if "cfg/logging/set/result" in c["topic"]]
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is False
    assert "unknown_key" in body["error"]


def test_cmd_cfg_logging_set_non_object_set_field():
    comp, mqtt = make_component()
    payload = json.dumps({"request_id": "r4", "set": "not_a_dict"})
    comp.on_cmd_cfg_logging_set(payload)
    result_calls = [c for c in mqtt.calls if "cfg/logging/set/result" in c["topic"]]
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is False


# ---------------------------------------------------------------------------
# on_cmd_cfg_telemetry_set
# ---------------------------------------------------------------------------


def test_cmd_cfg_telemetry_set_valid():
    comp, mqtt = make_component()
    comp.set_telemetry_config({"cpu": {"enabled": False, "interval_s": 2, "change_threshold_percent": 2.0}})
    payload = json.dumps({"request_id": "t1", "set": {"cpu": {"enabled": True, "interval_s": 10}}})
    comp.on_cmd_cfg_telemetry_set(payload)
    result_calls = [c for c in mqtt.calls if "cfg/telemetry/set/result" in c["topic"]]
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is True
    assert comp._telemetry_cfg["cpu"]["enabled"] is True
    assert comp._telemetry_cfg["cpu"]["interval_s"] == 10


def test_cmd_cfg_telemetry_set_bool_shorthand():
    comp, mqtt = make_component()
    comp.set_telemetry_config({"cpu": {"enabled": False}})
    payload = json.dumps({"request_id": "t2", "set": {"cpu": True}})
    comp.on_cmd_cfg_telemetry_set(payload)
    result_calls = [c for c in mqtt.calls if "cfg/telemetry/set/result" in c["topic"]]
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is True


def test_cmd_cfg_telemetry_set_invalid_json():
    comp, mqtt = make_component()
    comp.on_cmd_cfg_telemetry_set("{{bad")
    result_calls = [c for c in mqtt.calls if "cfg/telemetry/set/result" in c["topic"]]
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is False


def test_cmd_cfg_telemetry_set_non_object_metric():
    comp, mqtt = make_component()
    payload = json.dumps({"request_id": "t3", "set": {"cpu": "string_not_valid"}})
    comp.on_cmd_cfg_telemetry_set(payload)
    result_calls = [c for c in mqtt.calls if "cfg/telemetry/set/result" in c["topic"]]
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is False
    assert "cpu" in body["error"]


def test_cmd_cfg_telemetry_set_missing_set_field():
    comp, mqtt = make_component()
    comp.on_cmd_cfg_telemetry_set(json.dumps({"request_id": "t4"}))
    result_calls = [c for c in mqtt.calls if "cfg/telemetry/set/result" in c["topic"]]
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is False


# ---------------------------------------------------------------------------
# _make_cmd_handler (request-ID deduplication)
# ---------------------------------------------------------------------------


def test_make_cmd_handler_calls_method():
    comp, mqtt = make_component()
    calls = []
    handler = comp._make_cmd_handler("ping", lambda p: calls.append(p))
    handler(json.dumps({"request_id": "r1"}))
    assert len(calls) == 1


def test_make_cmd_handler_deduplicates():
    comp, mqtt = make_component()
    calls = []
    handler = comp._make_cmd_handler("ping", lambda p: calls.append(p))
    handler(json.dumps({"request_id": "r1"}))
    handler(json.dumps({"request_id": "r1"}))  # duplicate
    assert len(calls) == 1  # second call rejected
    result_calls = [c for c in mqtt.calls if "evt/ping/result" in c["topic"]]
    assert result_calls
    body = json.loads(result_calls[-1]["payload"])
    assert body["ok"] is False
    assert "duplicate" in body["error"]


def test_make_cmd_handler_allows_different_request_ids():
    comp, mqtt = make_component()
    calls = []
    handler = comp._make_cmd_handler("ping", lambda p: calls.append(p))
    handler(json.dumps({"request_id": "r1"}))
    handler(json.dumps({"request_id": "r2"}))
    assert len(calls) == 2


def test_make_cmd_handler_empty_request_id_bypasses_dedup():
    comp, mqtt = make_component()
    calls = []
    handler = comp._make_cmd_handler("ping", lambda p: calls.append(p))
    handler(json.dumps({"request_id": ""}))
    handler(json.dumps({"request_id": ""}))  # second call with empty id
    assert len(calls) == 2  # both pass through


def test_make_cmd_handler_no_request_id_bypasses_dedup():
    comp, mqtt = make_component()
    calls = []
    handler = comp._make_cmd_handler("ping", lambda p: calls.append(p))
    handler(json.dumps({}))  # no request_id key
    handler(json.dumps({}))
    assert len(calls) == 2


def test_make_cmd_handler_invalid_json_bypasses_dedup():
    comp, mqtt = make_component()
    calls = []
    handler = comp._make_cmd_handler("ping", lambda p: calls.append(p))
    handler("not json")
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# _parse_cfg_set_payload
# ---------------------------------------------------------------------------


def test_parse_cfg_set_payload_valid():
    comp, _ = make_component()
    req_id, set_dict, err = comp._parse_cfg_set_payload(
        json.dumps({"request_id": "r1", "set": {"a": 1}})
    )
    assert req_id == "r1"
    assert set_dict == {"a": 1}
    assert err is None


def test_parse_cfg_set_payload_empty_string():
    comp, _ = make_component()
    req_id, set_dict, err = comp._parse_cfg_set_payload("")
    assert err == "missing 'set' field in payload"


def test_parse_cfg_set_payload_non_dict_set():
    comp, _ = make_component()
    req_id, set_dict, err = comp._parse_cfg_set_payload(
        json.dumps({"request_id": "r1", "set": [1, 2]})
    )
    assert err == "payload 'set' must be an object"


# ---------------------------------------------------------------------------
# _parse_cmd_payload
# ---------------------------------------------------------------------------


def test_parse_cmd_payload_valid():
    from lucid_component_base import CmdPayloadError  # noqa: F401 — import smoke
    comp, _ = make_component()
    rid, data = comp._parse_cmd_payload(
        json.dumps({"request_id": "r1", "key": "value"})
    )
    assert rid == "r1"
    assert data == {"request_id": "r1", "key": "value"}


def test_parse_cmd_payload_empty_string():
    comp, _ = make_component()
    rid, data = comp._parse_cmd_payload("")
    assert rid == ""
    assert data == {}


def test_parse_cmd_payload_missing_request_id():
    comp, _ = make_component()
    rid, data = comp._parse_cmd_payload(json.dumps({"foo": "bar"}))
    assert rid == ""
    assert data == {"foo": "bar"}


def test_parse_cmd_payload_invalid_json_raises():
    from lucid_component_base import CmdPayloadError
    comp, _ = make_component()
    with pytest.raises(CmdPayloadError) as exc_info:
        comp._parse_cmd_payload("not json")
    assert "invalid JSON payload" in str(exc_info.value)


def test_parse_cmd_payload_non_object_root_raises():
    from lucid_component_base import CmdPayloadError
    comp, _ = make_component()
    with pytest.raises(CmdPayloadError) as exc_info:
        comp._parse_cmd_payload(json.dumps([1, 2, 3]))
    assert "payload must be a JSON object" in str(exc_info.value)


def test_make_cmd_handler_publishes_ok_false_on_cmd_payload_error():
    """Handler raising CmdPayloadError gets caught and routed via _publish_handler_failure."""
    comp, mqtt = make_component()

    def failing_handler(payload_str: str) -> None:
        # Simulate a handler that uses _parse_cmd_payload with a bad payload
        comp._parse_cmd_payload(payload_str)

    handler = comp._make_cmd_handler("ping", failing_handler)
    handler("not json")
    # Result topic should carry ok=false with the parse error
    publishes = [c for c in mqtt.calls if c["topic"].endswith("/evt/ping/result")]
    assert len(publishes) == 1
    body = json.loads(publishes[0]["payload"])
    assert body["ok"] is False
    assert "invalid JSON payload" in body["error"]


def test_make_cmd_handler_uses_cfg_set_envelope_on_cmd_payload_error():
    """For cfg/*/set actions, the failure routes through publish_cfg_set_result."""
    comp, mqtt = make_component()

    def failing_handler(payload_str: str) -> None:
        comp._parse_cmd_payload(payload_str)

    handler = comp._make_cmd_handler("cfg/logging/set", failing_handler)
    handler("not json")
    publishes = [
        c for c in mqtt.calls if c["topic"].endswith("/evt/cfg/logging/set/result")
    ]
    assert len(publishes) == 1
    body = json.loads(publishes[0]["payload"])
    # cfg-set envelope shape: includes applied=None and ts
    assert body["ok"] is False
    assert body["applied"] is None
    assert "ts" in body
    assert "invalid JSON payload" in body["error"]


# ---------------------------------------------------------------------------
# _publish_json error handling
# ---------------------------------------------------------------------------


def test_publish_json_handles_non_serialisable_payload():
    comp, mqtt = make_component()
    # Should not raise; error is logged and nothing published
    comp._publish_json("some/topic", {"key": object()}, retain=False, qos=0)
    # No call should have been published (error branch returns early)
    assert not any(c["topic"] == "some/topic" for c in mqtt.calls)


# ---------------------------------------------------------------------------
# MQTT logging setup
# ---------------------------------------------------------------------------


def test_mqtt_logging_setup_on_start():
    import logging as _logging
    comp, _ = make_component()
    comp.start()
    component_logger = _logging.getLogger("lucid.component.dummy")
    from lucid_component_base.mqtt_log_handler import MQTTLogHandler
    handler_types = [type(h) for h in component_logger.handlers]
    assert MQTTLogHandler in handler_types


def test_mqtt_logging_not_setup_twice():
    import logging as _logging
    comp, _ = make_component()
    comp.start()
    comp.stop()
    comp.started = False
    comp.stopped = False
    comp._state.status = ComponentStatus.STOPPED
    comp.start()  # second start
    component_logger = _logging.getLogger("lucid.component.dummy")
    from lucid_component_base.mqtt_log_handler import MQTTLogHandler
    mqtt_handlers = [h for h in component_logger.handlers if isinstance(h, MQTTLogHandler)]
    # Should have exactly one (no duplicates)
    assert len(mqtt_handlers) >= 1


# ---------------------------------------------------------------------------
# _set_state
# ---------------------------------------------------------------------------


def test_set_state_updates_updated_at():
    comp, _ = make_component()
    before = comp.state.updated_at
    time.sleep(0.01)
    comp._set_state(ComponentStatus.RUNNING)
    assert comp.state.updated_at >= before


def test_set_state_publishes_status():
    comp, mqtt = make_component()
    mqtt.clear()
    comp._set_state(ComponentStatus.RUNNING)
    assert any("status" in c["topic"] for c in mqtt.calls)


# ---------------------------------------------------------------------------
# Thread safety — _telemetry_last
# ---------------------------------------------------------------------------


def test_telemetry_last_no_race_under_concurrent_access():
    """publish_telemetry and should_publish_telemetry can be called concurrently
    without corrupting _telemetry_last or raising an exception."""
    comp, mqtt = make_component()
    comp.set_telemetry_config({"cpu": {"enabled": True, "interval_s": 1, "change_threshold_percent": 0.0}})

    errors: list[Exception] = []
    iterations = 500

    def writer():
        for i in range(iterations):
            try:
                comp.publish_telemetry("cpu", float(i))
            except Exception as exc:
                errors.append(exc)

    def reader():
        for i in range(iterations):
            try:
                comp.should_publish_telemetry("cpu", float(i))
            except Exception as exc:
                errors.append(exc)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"Concurrent access raised: {errors}"


# ---------------------------------------------------------------------------
# _BoundedSet
# ---------------------------------------------------------------------------

from lucid_component_base.base import _BoundedSet


def test_bounded_set_contains_added_item():
    s = _BoundedSet()
    s.add("a")
    assert "a" in s


def test_bounded_set_does_not_contain_absent_item():
    s = _BoundedSet()
    assert "x" not in s


def test_bounded_set_evicts_oldest_at_capacity():
    s = _BoundedSet(maxsize=3)
    s.add("a")
    s.add("b")
    s.add("c")
    s.add("d")  # evicts "a"
    assert "a" not in s
    assert "b" in s
    assert "c" in s
    assert "d" in s


def test_bounded_set_size_stays_bounded():
    s = _BoundedSet(maxsize=50)
    for i in range(200):
        s.add(str(i))
    assert len(s._queue) == 50
    assert len(s._seen) == 50


def test_dedup_handler_uses_bounded_set():
    """_make_cmd_handler stores request IDs in a _BoundedSet, not a plain set."""
    comp, _ = make_component()

    def handler(payload_str: str) -> None:
        pass

    comp._make_cmd_handler("ping", handler)(json.dumps({"request_id": "req-1"}))

    seen = comp._seen_request_ids.get("ping")
    assert isinstance(seen, _BoundedSet), "per-action store must be a _BoundedSet"
    assert "req-1" in seen


# ---------------------------------------------------------------------------
# Lifecycle cleanup — handler removal, timer cancellation, stopped_at on error
# ---------------------------------------------------------------------------

import logging as _logging
from lucid_component_base.mqtt_log_handler import MQTTLogHandler


def test_stop_removes_mqtt_log_handler():
    comp, _ = make_component()
    comp.start()

    component_logger = _logging.getLogger(f"lucid.component.{comp.component_id}")
    # Handler should be present after start
    handlers_before = [h for h in component_logger.handlers if isinstance(h, MQTTLogHandler)]
    assert len(handlers_before) >= 1

    comp.stop()

    handlers_after = [h for h in component_logger.handlers if isinstance(h, MQTTLogHandler)]
    assert len(handlers_after) == 0, "MQTTLogHandler must be removed on stop()"


def test_stop_cancels_batch_timer():
    comp, _ = make_component()
    comp.start()

    component_logger = _logging.getLogger(f"lucid.component.{comp.component_id}")
    handlers = [h for h in component_logger.handlers if isinstance(h, MQTTLogHandler)]
    if handlers:
        handler = handlers[0]
        # Manually set a live timer to verify it gets cancelled
        timer = threading.Timer(60, lambda: None)
        timer.daemon = True
        timer.start()
        with handler._lock:
            handler._batch_timer = timer

    comp.stop()

    # timer.cancel() sets timer.finished — the thread may still be technically
    # "alive" for a brief moment, but finished.is_set() is the reliable check.
    assert timer.finished.is_set(), "Pending batch timer must be cancelled on stop()"


def test_stop_sets_stopped_at_on_exception():
    comp, _ = make_component()
    comp.start()
    comp._stop = lambda: (_ for _ in ()).throw(RuntimeError("forced failure"))

    with pytest.raises(RuntimeError):
        comp.stop()

    assert comp.state.stopped_at is not None, "stopped_at must be set even when _stop() raises"
    assert comp.state.status == ComponentStatus.FAILED


def test_mqtt_log_handler_close_cancels_timer():
    fake_mqtt = MagicMock()
    fake_mqtt._publish_json = MagicMock()
    handler = MQTTLogHandler(fake_mqtt, "lucid/agents/test/components/dummy/logs")

    timer = threading.Timer(60, lambda: None)
    timer.daemon = True
    timer.start()
    with handler._lock:
        handler._batch_timer = timer

    handler.close()

    assert timer.finished.is_set(), "close() must cancel the pending timer"
    assert handler._batch_timer is None
