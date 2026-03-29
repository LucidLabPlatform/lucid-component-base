# lucid-component-base

Component contract and runtime context for LUCID components.

This package defines the **SDK layer only**.
It has **zero dependency on `lucid-agent-core`** and does not implement installation, loading, supervision, or MQTT routing.

---

## Purpose

`lucid-component-base` provides:

* The **component lifecycle contract** (`Component` base class)
* The **runtime context interface** (`ComponentContext`)
* Shared state models and enums (`ComponentState`, `ComponentStatus`)
* Built-in MQTT publishing helpers (retained topics, telemetry streams, command results)
* Telemetry gating (per-metric enabled/interval/change-threshold logic)
* Request-ID deduplication for command handlers
* MQTT log handler (`MQTTLogHandler`) with batching and rate limiting
* Git-tag-based package versioning

It does **not** provide:

* Component installation
* Component discovery
* Component supervision
* MQTT client implementation
* Topic schema management

Those responsibilities belong to `lucid-agent-core`.

---

## Prerequisites

- Python 3.11+

## Setup

```bash
make setup-venv   # Create .venv, install project + test deps
```

Or manually:

```bash
pip install -e .
```

## Installation

Build wheel/sdist for release:

```bash
make build
```

Package version is derived from the git tag via `setuptools_scm` (e.g. tag `v1.0.0` → version `1.0.0`).

---

## Versioning

- **lucid-component-base** itself: version from this repo's git tag at build time, available as `lucid_component_base.__version__`.
- **Component subclasses** (e.g. `fixture_cpu`): `Component.version` resolves the **component's** installed package version (e.g. `lucid-component-fixture-cpu`) via `importlib.metadata`, so each component reports its own package version in metadata.

---

## Public API

### `Component`

Base class for all LUCID components. All hardware logic lives here.

#### Lifecycle Contract

| Method | Behaviour |
|---|---|
| `start()` | Idempotent. No-op if already `RUNNING`. Raises `RuntimeError` if `STARTING` or `STOPPING`. |
| `stop()` | Idempotent. No-op if already `STOPPED` or `STOPPING`. |
| `_start()` | **Abstract** — subclass must implement. Called inside `start()`. |
| `_stop()` | **Abstract** — subclass must implement. Called inside `stop()`. |

State transitions: `STOPPED → STARTING → RUNNING` on success, `→ FAILED` on exception.
Exceptions from `_start()` / `_stop()` are captured in `ComponentState.last_error` and re-raised.

Minimal required override:

```python
from lucid_component_base import Component

class MyComponent(Component):

    @property
    def component_id(self) -> str:
        return "my_component"

    def _start(self) -> None:
        # acquire hardware resources, start threads
        ...

    def _stop(self) -> None:
        # release hardware resources, stop threads
        ...
```

#### Optional Overrides

| Method | Purpose |
|---|---|
| `metadata() -> dict` | Extra fields merged into the retained `metadata` topic. |
| `capabilities() -> list[str]` | Declare supported commands, e.g. `["reset", "ping"]`. |
| `get_state_payload() -> dict` | Payload for retained `state` topic (e.g. sensor readings). |
| `get_cfg_payload() -> dict` | Payload for retained `cfg` topic (hardware/operational settings). |

---

### Retained MQTT Topics

All retained topics are published at QoS 1. The topic tree is:

```
lucid/agents/{agent_id}/components/{component_id}/
  metadata          ← { component_id, version, capabilities }
  status            ← { state: "idle" | "running" | "error" }
  state             ← { <metric>: value, ... }   (subclass provides)
  cfg               ← { <hardware settings> }    (subclass provides)
  cfg/logging       ← { log_level }
  cfg/telemetry     ← { <metric>: { enabled, interval_s, change_threshold_percent } }
```

Publishing helpers:

```python
component.publish_metadata()          # retained: metadata
component.publish_status()            # retained: status
component.publish_state()             # retained: state (calls get_state_payload())
component.publish_cfg()               # retained: cfg + cfg/logging + cfg/telemetry
component.publish_cfg_general()       # retained: cfg only
component.publish_cfg_logging()       # retained: cfg/logging only
component.publish_cfg_telemetry()     # retained: cfg/telemetry only
```

---

### Stream Topics

Streams are published at QoS 0 (non-retained):

```
lucid/agents/{agent_id}/components/{component_id}/
  logs                    ← { count, lines: [{ ts, level, logger, message }] }
  telemetry/{metric}      ← { value }
```

#### Telemetry Publishing

```python
component.publish_telemetry("cpu_percent", 42.5)
```

Calls `should_publish_telemetry(metric, value)` first. Telemetry is gated — it only publishes if:
1. The metric is **enabled** in the telemetry config.
2. Either the **interval** has elapsed since the last publish, **or** the value changed beyond the **change threshold percent**.

#### Log Publishing

```python
component.publish_log("info", "Hardware initialised")
```

Direct MQTT log publish (bypasses `MQTTLogHandler`). Use `lucid.component.<component_id>` Python logger for automatic batched MQTT delivery via `MQTTLogHandler`.

---

### Command Handling

Commands arrive on `cmd/{action}` topics. The convention maps topic segments to method names:

| Topic suffix | Handler method |
|---|---|
| `cmd/reset` | `on_cmd_reset` |
| `cmd/ping` | `on_cmd_ping` |
| `cmd/cfg/set` | `on_cmd_cfg_set` |
| `cmd/cfg/logging/set` | `on_cmd_cfg_logging_set` (built-in) |
| `cmd/cfg/telemetry/set` | `on_cmd_cfg_telemetry_set` (built-in) |

All command payloads **must** include `{ "request_id": "<uuid>" }`.

Built-in commands (`cfg/logging/set`, `cfg/telemetry/set`) are fully implemented in the base class. Custom commands are added by defining `on_cmd_<action>` methods on the subclass.

Command results are published to `evt/{action}/result`:

```python
component.publish_result("reset", request_id, ok=True)
# → lucid/agents/.../components/.../evt/reset/result
# → { request_id, ok, error }
```

For cfg-style results with an `applied` field:

```python
component.publish_cfg_set_result(request_id, ok=True, applied={"key": "value"})
# → { request_id, ok, applied, error, ts }
```

#### Request-ID Deduplication

Use `_make_cmd_handler(action, method)` to wrap a handler with deduplication. Duplicate `request_id` values for the same `action` are rejected — a failure result is published and the handler is not invoked. Empty request IDs bypass deduplication.

```python
handler = self._make_cmd_handler("reset", self._handle_reset)
```

---

### Telemetry Gating

Telemetry configuration is per-metric. Register metrics via `set_telemetry_config()`:

```python
component.set_telemetry_config({
    "cpu_percent": {
        "enabled": True,
        "interval_s": 5,
        "change_threshold_percent": 2.0,
    },
})
```

Fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `False` | Whether this metric publishes at all |
| `interval_s` | int | `2` | Minimum seconds between publishes |
| `change_threshold_percent` | float | `2.0` | Minimum % change to trigger an early publish |

Subclasses set `_DEFAULT_TELEMETRY_CFG` at class level to pre-register metrics:

```python
class MyComponent(Component):
    _DEFAULT_TELEMETRY_CFG = {
        "temperature": {"enabled": True, "interval_s": 10, "change_threshold_percent": 1.0},
    }
```

---

### `ComponentContext`

Immutable runtime context injected by the agent.

Fields:

| Field | Type | Description |
|---|---|---|
| `agent_id` | `str` | Stable agent identifier |
| `base_topic` | `str` | Agent topic root, e.g. `lucid/agents/<agent_id>` |
| `component_id` | `str` | Component identifier for topic construction |
| `mqtt` | `MqttPublisher` | MQTT client exposing `publish()` |
| `config` | `dict` | Component-specific configuration dict |

Helper:

```python
context.topic("state")
# -> lucid/agents/<agent_id>/components/<component_id>/state
```

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

---

### `ComponentStatus`

Enum of lifecycle states:

| Value | Meaning |
|---|---|
| `stopped` | Not running (initial state) |
| `starting` | `start()` called, `_start()` in progress |
| `running` | `_start()` completed successfully |
| `stopping` | `stop()` called, `_stop()` in progress |
| `failed` | `_start()` or `_stop()` raised an exception |

The status is mapped to the MQTT contract value via `_status_to_contract()`:

| Internal status | MQTT `state` field |
|---|---|
| `running` | `"running"` |
| `failed` | `"error"` |
| anything else | `"idle"` |

---

### `ComponentState`

Dataclass representing full lifecycle state:

| Field | Type | Description |
|---|---|---|
| `status` | `ComponentStatus` | Current lifecycle state |
| `last_error` | `str \| None` | Last exception message |
| `started_at` | `str \| None` | ISO-8601 UTC timestamp of last successful start |
| `stopped_at` | `str \| None` | ISO-8601 UTC timestamp of last stop |
| `updated_at` | `str` | ISO-8601 UTC timestamp of last state change |

`Component.state` returns a live `ComponentState` instance.

---

### `MQTTLogHandler`

`logging.Handler` subclass that publishes structured log records to the component's `logs` MQTT topic.

Batching and rate-limiting behaviour:

| Parameter | Default | Description |
|---|---|---|
| `MAX_LINES_PER_BATCH` | 50 | Max log lines per MQTT message |
| `BATCH_INTERVAL_S` | 0.25 s | Timer interval for flushing a partial batch |
| `MAX_BATCHES_PER_WINDOW` | 25 | Max batches in the rate-limiting window |
| `TIME_WINDOW_S` | 2.0 s | Rolling window for rate limiting |

Each log line in the published `lines` array contains: `ts`, `level`, `logger`, `message`, and optionally `exception`.

The handler is automatically added to `logging.getLogger("lucid.component.<component_id>")` when `start()` is called. `CRITICAL` is mapped to `"error"` in the MQTT level field.

---

## Full Example

```python
from lucid_component_base import Component, ComponentContext

class TemperatureSensor(Component):

    _DEFAULT_TELEMETRY_CFG = {
        "temperature_c": {"enabled": True, "interval_s": 10, "change_threshold_percent": 1.0},
    }

    @property
    def component_id(self) -> str:
        return "temperature_sensor"

    def capabilities(self) -> list[str]:
        return ["reset", "ping"]

    def get_state_payload(self) -> dict:
        return {"temperature_c": self._last_temp}

    def _start(self) -> None:
        self._last_temp = 0.0
        # start polling thread, etc.

    def _stop(self) -> None:
        # stop polling thread
        pass

    def on_cmd_reset(self, payload_str: str) -> None:
        import json
        data = json.loads(payload_str)
        self._last_temp = 0.0
        self.publish_result("reset", data["request_id"], ok=True)

    def report_temperature(self, value: float) -> None:
        self._last_temp = value
        self.publish_telemetry("temperature_c", value)
        self.publish_state()
```

---

## Development

```bash
make setup-venv   # Create .venv, install project + test deps
make build        # Build wheel and sdist
make clean        # Remove build artifacts
```

## Testing

```bash
make test           # Run all tests
make test-coverage  # Tests with coverage report
```

## Architectural Boundary

* `lucid-component-base` defines **what a component is**
* `lucid-agent-core` defines **how components are installed, loaded, and supervised**

This separation is intentional and enforced.
