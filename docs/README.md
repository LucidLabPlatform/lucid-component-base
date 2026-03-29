# lucid-component-base

Component SDK for LUCID. All LUCID components extend the `Component` base class defined here. Zero dependency on agent-core — this library only provides the lifecycle contract and MQTT publish helpers.

## Install

```bash
pip install lucid-component-base
# Or from git:
pip install "lucid-component-base @ git+https://github.com/LucidLabPlatform/lucid-component-base@v1.1.3"
```

## Minimal Component Example

```python
from lucid_component_base import Component, ComponentContext

class MyComponent(Component):
    @property
    def component_id(self) -> str:
        return "my_component"

    def capabilities(self) -> list[str]:
        return ["reset", "ping", "activate"]

    def get_state_payload(self) -> dict:
        return {"active": self._active, "value": self._value}

    def _start(self) -> None:
        self._active = False
        self._value = 0
        # Hardware init here
        self.publish_metadata()
        self.publish_status()
        self.publish_state(self.get_state_payload())
        self.publish_cfg(self.get_cfg_payload())

    def _stop(self) -> None:
        # Hardware cleanup here
        pass

    def on_cmd_reset(self, payload_str: str) -> None:
        import json
        payload = json.loads(payload_str)
        self._active = False
        self._value = 0
        self.publish_result("reset", payload["request_id"], ok=True)

    def on_cmd_activate(self, payload_str: str) -> None:
        import json
        payload = json.loads(payload_str)
        self._active = True
        self.publish_state(self.get_state_payload())
        self.publish_result("activate", payload["request_id"], ok=True)
```

## Lifecycle

```
component.start()
    │
    ├── Sets state → STARTING
    ├── Calls _start()  ← your hardware init
    └── Sets state → RUNNING (or FAILED if _start() raises)

component.stop()
    │
    ├── Sets state → STOPPING
    ├── Calls _stop()  ← your hardware cleanup
    └── Sets state → STOPPED
```

Both `start()` and `stop()` are **idempotent** — safe to call multiple times.

## Telemetry Gating

Publishing telemetry every loop iteration would flood MQTT. The SDK gates per-metric:

```python
# Configure per-metric gating (called from _start() or on_cmd_cfg_telemetry_set)
self.set_telemetry_config({
    "cpu_percent": {"enabled": True, "interval_s": 10, "change_threshold_percent": 5}
})

# In your telemetry loop:
value = get_cpu()
self.publish_telemetry("cpu_percent", value)
# Only publishes if: enabled AND (interval elapsed OR |delta| > threshold)
```

## Request Deduplication

Every command handler automatically deduplicates `request_id`. If the same `request_id` arrives twice (e.g., due to MQTT QoS 1 redelivery), the second call is rejected with an error result — your handler only runs once.

This is handled automatically via `_make_cmd_handler()` which wraps your `on_cmd_*` methods.

## Adding Custom Commands

1. Add the action name to `capabilities()`.
2. Implement `on_cmd_{action}(self, payload_str: str) -> None`.
   - Replace `-` and `/` in action names with `_` for the method name.
   - e.g., `ndi/input/set` → `on_cmd_ndi_input_set`

```python
def on_cmd_set_brightness(self, payload_str: str) -> None:
    payload = json.loads(payload_str)
    request_id = payload["request_id"]
    brightness = payload.get("value", 128)
    if not 0 <= brightness <= 255:
        self.publish_result("set-brightness", request_id, ok=False,
                            error="brightness must be 0-255")
        return
    self._brightness = brightness
    self.publish_result("set-brightness", request_id, ok=True)
```

## MQTT Topic Contract

All topics under: `lucid/agents/{agent_id}/components/{component_id}/`

| Suffix | QoS | Retained | Direction | Payload |
|--------|-----|----------|-----------|---------|
| `metadata` | 1 | Yes | → CC | `{component_id, version, capabilities[]}` |
| `status` | 1 | Yes | → CC | `{state: "idle"\|"running"\|"error", error?}` |
| `state` | 1 | Yes | → CC | Custom dict from `get_state_payload()` |
| `cfg` | 1 | Yes | → CC | Custom dict from `get_cfg_payload()` |
| `cfg/logging` | 1 | Yes | → CC | `{log_level}` |
| `cfg/telemetry` | 1 | Yes | → CC | `{metric: {enabled, interval_s, change_threshold_percent}}` |
| `logs` | 0 | No | → CC | `{count, lines: [{ts, level, logger, message, exception?}]}` |
| `telemetry/{metric}` | 0 | No | → CC | `{value}` |
| `cmd/{action}` | 1 | No | CC → | `{request_id, ...action params}` |
| `evt/{action}/result` | 1 | No | → CC | `{request_id, ok, error?}` |

## Built-in Command Handlers

These are provided automatically — you don't need to implement them:

| Command | What it does |
|---------|-------------|
| `cmd/cfg/logging/set` | Sets log level, publishes updated `cfg/logging` |
| `cmd/cfg/telemetry/set` | Updates per-metric telemetry config, publishes updated `cfg/telemetry` |

## MQTTLogHandler

The `MQTTLogHandler` is automatically configured for `logging.getLogger("lucid.component.<component_id>")` when `start()` is called on a component. It batches up to 50 log lines per MQTT message, publishes every 0.25 s, and is rate-limited to 25 batches per 2-second window to prevent broker flooding.

Each published `logs` message has the shape:

```json
{
  "count": 3,
  "lines": [
    { "ts": "2026-01-01T00:00:00+00:00", "level": "info", "logger": "lucid.component.my_component", "message": "..." },
    ...
  ]
}
```

`CRITICAL` log level is mapped to `"error"` in the MQTT `level` field.

To use the logger in a component:

```python
import logging

class MyComponent(Component):
    def _start(self) -> None:
        # After start() is called, this logger publishes to MQTT automatically
        logging.getLogger(f"lucid.component.{self.component_id}").info("Started")
```

## ComponentContext

Immutable runtime context injected by the agent.

| Field | Type | Description |
|---|---|---|
| `agent_id` | `str` | Stable agent identifier |
| `base_topic` | `str` | Agent topic root, e.g. `lucid/agents/<agent_id>` |
| `component_id` | `str` | Component identifier for topic construction |
| `mqtt` | `MqttPublisher` | MQTT client exposing `publish()` |
| `config` | `Dict[str, Any]` | Component-specific configuration dict |

Factory (validates all fields):

```python
ctx = ComponentContext.create(
    agent_id="pi-001",
    base_topic="lucid/agents/pi-001",
    component_id="led_strip",
    mqtt=mqtt_client,
    config={"pin": 18, "num_leds": 60},
)
```

## Public API

See the package [README.md](../../lucid-component-base/README.md) for the full API reference.
