# ARCHITECTURE — lucid-component-base

> Audit artifact. Documentation only — no code was changed to produce this file.
>
> **Coverage of this audit:** `pyproject.toml`, `_version.py`, `context.py`,
> `mqtt_log_handler.py`, and `base.py` sections 1–3 (lifecycle, publishing,
> command-handling). **Not** given a dedicated findings pass: `base.py` section 4
> (telemetry gating), `__init__.py`, and `tests/`. Facts about those are stated
> only where directly readable from code.
>
> Markers: **[INFERRED]** = reasoning about intent, not stated in code.
> **[UNVERIFIED]** = could not confirm within this repo (e.g. depends on an
> external consumer).

---

## Purpose

`lucid-component-base` is the **contract core / SDK layer** for LUCID components.
It defines the base class every component subclasses (`Component`) and the runtime
handle the agent injects (`ComponentContext`), plus the MQTT publish/subscribe
helpers, lifecycle state machine, command dedup, telemetry gating, and a logging
handler. It has **zero runtime dependencies** and contains **no MQTT client, no
component discovery, no supervision** — those belong to `lucid-agent-core`
(per `README.md`; the boundary is stated, not enforceable from this repo).

It is consumed by `lucid-component-template`, `lucid-component-fixture-cpu`, and
all `lucid-component-*` repos. [UNVERIFIED] — those consumers are not in this repo.

---

## Module map

- **`src/lucid_component_base/__init__.py`** — Public export surface. Declares
  `__all__` = `__version__, CmdPayloadError, Component, ComponentContext,
  ComponentNotReady, ComponentState, ComponentStatus, MqttPublisher`. Exists to
  fix the package's public API in one place. *Why:* clear.

- **`src/lucid_component_base/_version.py`** — Resolves `__version__` at import
  time via `importlib.metadata.version("lucid-component-base")`, falling back to
  `"0.0.0"` on `PackageNotFoundError`. Exists as the runtime read-side of the
  build-time, git-tag-derived version (see `pyproject.toml`). *Why:* clear.

- **`src/lucid_component_base/context.py`** — Defines `MqttPublisher` (a
  `Protocol`: `publish` / `subscribe` / `unsubscribe`) and `ComponentContext`
  (frozen, slotted dataclass: `agent_id, base_topic, component_id, mqtt, config`),
  with a `topic()` builder, a `logger()` helper, and a validating `create()`
  factory. Exists so components talk to MQTT and build topics without importing
  any MQTT library — the source of the "zero dependency" property. *Why:* clear.

- **`src/lucid_component_base/mqtt_log_handler.py`** — `MQTTLogHandler`, a
  `logging.Handler` that batches log records (`MAX_LINES_PER_BATCH = 50`,
  `BATCH_INTERVAL_S = 0.25`) and rate-limits batches (`MAX_BATCHES_PER_WINDOW = 25`
  per `TIME_WINDOW_S = 2.0`), publishing to the `logs` topic. Exists to keep a
  chatty component from flooding the broker while preserving a plain
  `logging.getLogger(...).info(...)` developer experience. *Why:* clear.

- **`src/lucid_component_base/base.py`** — The core. Contains lifecycle
  (`Component.start`/`stop`, `ComponentStatus`, `ComponentState`,
  `ComponentNotReady`), the MQTT publish helpers, command dispatch + dedup
  (`_make_cmd_handler`, `_BoundedSet`, `_parse_cfg_set_payload`, built-in
  `on_cmd_cfg_logging_set` / `on_cmd_cfg_telemetry_set`), and telemetry gating
  (`set_telemetry_config`, `should_publish_telemetry`, `publish_telemetry`).
  Exists to encode everything a component *is*. *Why:* clear.

**Modules where "why" could not be answered: None found.** (Every module's
existence is justifiable from the code. Specific *elements* that are unjustified
or dead are recorded in `DEBT.md`, not here.)

Two documentation files also exist: **`README.md`** (package-root API reference)
and **`docs/README.md`** (a near-duplicate API reference). They overlap
substantially; whether both are meant to be maintained is unclear. [INFERRED] one
may be stale.

---

## Data flow: boot → addressable → controllable → shutdown

All steps below are external-caller-driven; the caller (`lucid-agent-core`) is
**not** in this repo, so the "who calls" side is [INFERRED] where noted.

1. **Context construction.** `ComponentContext.create(...)` (`context.py:69`)
   validates and builds the immutable context. Called by the agent. [INFERRED]

2. **Component construction.** `Component.__init__(context)` (`base.py:139`) sets
   `_state = ComponentState()` (defaults to `ComponentStatus.STOPPED`), telemetry
   / dedup structures, `_log_level = "ERROR"`. No MQTT I/O yet.

3. **Boot.** `Component.start()` (`base.py:279`):
   - First call runs `_setup_mqtt_logging()` (`base.py:447`) — attaches
     `MQTTLogHandler` to `logging.getLogger(f"lucid.component.{component_id}")`,
     sets that logger to `ERROR`, `propagate = False`.
   - Guards: no-op if `RUNNING`; `raise RuntimeError` if `STARTING`/`STOPPING`.
   - `_set_state(STARTING)` (which publishes `status`), then calls subclass
     `_start()` (`base.py:842`, abstract).

4. **Addressable.** Inside `_start()`, a subclass is expected to call
   `publish_metadata()`, `publish_status()`, `publish_state()`, `publish_cfg()`,
   `publish_schema()`, and register `subscribe()` calls for its `cmd/*` topics.
   The base class does **not** call `subscribe()` itself — the subclass must
   (`subscribe()` docstring, `base.py:526`). [INFERRED] from docstrings + the
   consumer examples in the READMEs; no such subclass exists in this repo.
   - On success: `started_at` set, `_set_state(RUNNING)` (publishes `status`).
   - On `ComponentNotReady`: `last_error` set, `_set_state(STOPPED)`, **not
     re-raised** (component is idle, retryable).
   - On any other exception: `last_error` set, `_set_state(FAILED)`, **re-raised**.

5. **Controllable.** Command messages on `cmd/<action>` route to `on_cmd_<action>`
   methods. Two are built into the base (`on_cmd_cfg_logging_set` `base.py:580`,
   `on_cmd_cfg_telemetry_set` `base.py:621`); others are subclass-provided.
   Dedup + exception-safety come from wrapping a handler in `_make_cmd_handler`
   (`base.py:778`) — **[INFERRED]** the agent performs this wiring; nothing in
   this repo connects a broker subscription to `_make_cmd_handler`.
   Outcomes publish to `evt/<action>/result`.

6. **Shutdown.** `Component.stop(*, final=False)` (`base.py:309`): no-op if
   `STOPPED`/`STOPPING`; else `_set_state(STOPPING)`, call subclass `_stop(final=)`
   (with a `TypeError` fallback to `_stop()` — see `DEBT.md` / IMPROVEMENTS #9),
   record `stopped_at`, `_set_state(STOPPED)` (or `FAILED`, re-raised). A
   `finally` always runs `_teardown_mqtt_logging()` (`base.py:340`).

---

## External contracts (verbatim)

**No HTTP routes. No DB tables. No env vars or config files read.**
`ComponentContext.config` is an opaque `Dict[str, Any]`; nothing in this repo
keys into it. The MQTT client itself is external (a `Protocol`, no concrete impl).

### Topic root — `context.py:63`
```python
f"{self.base_topic}/components/{self.component_id}/{suffix}"
```
`base_topic` is conventionally `lucid/agents/<agent_id>` (`context.py:5`
docstring) — **ASSUMED**, not enforced.

### Retained topics — via `_publish_retained` (`base.py:825`), `qos=1, retain=True`

| suffix | payload (verbatim from code) |
|---|---|
| `metadata` | `metadata()` = `{"component_id": ..., "version": ...}` (`base.py:177`) with `payload["capabilities"] = self.capabilities()` merged in at publish time (`base.py:368-369`) |
| `status` | `{"state": _status_to_contract(self._state.status)}` (`base.py:374`) |
| `state` | `get_state_payload()` — default `{}` (`base.py:269`); subclass-defined |
| `cfg` | `get_cfg_payload()` — default `{}` (`base.py:277`); subclass-defined |
| `cfg/logging` | `{"log_level": self._log_level}` (`base.py:391`) |
| `cfg/telemetry` | `self._telemetry_cfg` (`base.py:396`) |
| `schema` | `self.schema()` (see below) |

`_status_to_contract` (`base.py:61`): `RUNNING → "running"`, `FAILED → "error"`,
**everything else → `"idle"`**.

### Stream topics — `qos=0, retain=False`

`logs` (single-line path, `base.py:437-445`):
```python
topic = self.context.topic("logs")
line = {"ts": _utc_iso(), "level": level,
        "logger": f"lucid.component.{self.component_id}", "message": message}
payload = {"count": 1, "lines": [line]}
```
`logs` (handler path, `mqtt_log_handler.py:189-192`): `{"count": len(batch),
"lines": batch}`, each line built by `_build_line` (`mqtt_log_handler.py:78`) as
`{"ts", "level", "logger", "message"}` plus optional `"exception"`. Level strings
here are lowercased and `CRITICAL → "error"` (`_level_to_mqtt`,
`mqtt_log_handler.py:64`); the single-line path does **not** normalize level.

`telemetry/<metric>` (`base.py:492-494`):
```python
topic = self.context.topic(f"telemetry/{metric}")
payload = {"value": value}
```

### Command-result topics — `evt/<action>/result`, `qos=1, retain=False`

`publish_result` (`base.py:503`):
```python
payload = {"request_id": request_id, "ok": ok, "error": error}
```
`publish_cfg_set_result` (`base.py:517-523`):
```python
payload = {"request_id": request_id, "ok": ok, "applied": applied,
           "error": error, "ts": ts if ts is not None else _utc_iso()}
```

### Self-described schema — `schema()` (`base.py:190-265`), published to `schema`

`publishes` keys: `metadata`, `status`, `state`, `cfg`, `cfg/logging`,
`cfg/telemetry`, `logs`, `schema`, plus one `evt/<action>/result` per action in:
```python
["ping", "reset", "cfg/set", "cfg/logging/set", "cfg/telemetry/set"] + self.capabilities()
```
`subscribes` keys (verbatim): `cmd/ping`, `cmd/reset`, `cmd/cfg/set`,
`cmd/cfg/logging/set`, `cmd/cfg/telemetry/set`.

Selected field schemas verbatim:
```python
"status": {"fields": {"state": {"type": "string", "enum": ["idle", "running", "error"]}}}
"cmd/cfg/logging/set": {"fields": {"set": {"type": "object",
    "fields": {"log_level": {"type": "string",
        "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]}}}}}
```

> **Contract gap:** `cmd/cfg/set` is listed under `subscribes` (`base.py:230`) and
> in `docs/README.md`, but **no `on_cmd_cfg_set` handler exists** in the base
> (only `on_cmd_cfg_logging_set` and `on_cmd_cfg_telemetry_set`). A subclass must
> implement it or the topic is declared-but-dead. Verified by grep.

### Enums (verbatim)

`ComponentStatus` (`base.py:53`): `STOPPED="stopped"`, `STARTING="starting"`,
`RUNNING="running"`, `STOPPING="stopping"`, `FAILED="failed"`.

---

## Invariants and assumptions

| Invariant | Status | Where |
|---|---|---|
| `agent_id`, `base_topic`, `component_id` are non-empty strings; `config` is a dict | **ENFORCED** | `ComponentContext.create` raises `ValueError` (`context.py:78-85`) |
| `_start()` / `_stop()` / `component_id` are implemented by subclass | **ENFORCED** | `raise NotImplementedError` (`base.py:158, 843, 846`) |
| `start()` not called while `STARTING`/`STOPPING` | **ENFORCED** | `raise RuntimeError` (`base.py:287`) |
| telemetry `interval_s` / `change_threshold_percent` are positive numbers | **ENFORCED** | `set_telemetry_config` raises `ValueError` (`base.py:701-712`) |
| duplicate `request_id` for an action is rejected | **ENFORCED** *only if* the agent wires handlers through `_make_cmd_handler` | `base.py:799-812`; wiring is **[INFERRED]** external |
| `mqtt` actually implements `publish`/`subscribe`/`unsubscribe` | **ASSUMED** | `create()` never validates `mqtt` (see IMPROVEMENTS #6) |
| `base_topic` equals `lucid/agents/<agent_id>` (and agrees with `agent_id`) | **ASSUMED** | docstring only; consistency unchecked (IMPROVEMENTS #4) |
| `component_id` is unique per agent | **ASSUMED** | nothing checks |
| every command payload includes `request_id` | **ASSUMED** | README says "must"; missing id silently bypasses dedup (IMPROVEMENTS #13) |
| subclass calls `subscribe()` for its `cmd/*` and `unsubscribe()` in `_stop()` | **ASSUMED** | docstring instructs; nothing enforces — **[INFERRED]** |
| lifecycle transitions are driven by a single thread | **ASSUMED** | `_state` is unlocked while telemetry/dedup are locked — **[INFERRED]** (IMPROVEMENTS #11) |

---

## Reading order that worked

1. `pyproject.toml` — build + versioning model.
2. `_version.py` — runtime version resolution (read-side of #1).
3. `context.py` — `MqttPublisher` Protocol + `ComponentContext`; no dep on base.
4. `mqtt_log_handler.py` — self-contained handler; base wires it up.
5. `base.py` — top-to-bottom: exceptions → status/state → `_BoundedSet` →
   `Component` (lifecycle → publishing → command/dedup → telemetry gating).
6. `__init__.py` — confirms the public surface.
7. `tests/` — executable spec, cross-check against the READMEs.
8. `README.md` + `docs/README.md` — read last, against the code (one contract
   gap — `cmd/cfg/set` — was found this way).
