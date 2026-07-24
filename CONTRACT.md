# CONTRACT.md — The LUCID Component Contract

> The standalone spec of what a repo must implement or emit to be a valid LUCID
> component, as derived **from the code in this repo only**. Use this as the
> checklist other `lucid-component-*` repos are audited against.
>
> Two sections: **ENFORCED IN CODE** (validated / raises / fails without it) and
> **CONVENTION ONLY** (everyone does it, nothing checks it). Contracts are quoted
> verbatim. **[INFERRED]** / **[UNVERIFIED]** marks apply as elsewhere.
>
> Scope note: telemetry gating (base.py section 4), `__init__.py`, and tests were
> not given a dedicated findings pass; enforced telemetry validation below is
> stated from directly-readable code.

---

## ENFORCED IN CODE

These fail loudly (raise / no-op / reject) if you get them wrong.

### Identity & construction
- **`component_id` property must be overridden.** Base raises `NotImplementedError`
  (`base.py:158`).
- **`_start()` and `_stop()` must be overridden.** Base raises `NotImplementedError`
  (`base.py:843, 846`).
- **Context fields validated at construction.** `ComponentContext.create` raises
  `ValueError` unless `agent_id`, `base_topic`, `component_id` are non-empty
  strings and `config` is a dict (`context.py:78-85`).
- **`ComponentContext` is immutable.** `@dataclass(frozen=True, slots=True)`
  (`context.py:43`) — assigning to a field raises.

### Lifecycle
- **`start()` is idempotent when `RUNNING`** (no-op) and **raises `RuntimeError`
  if called while `STARTING`/`STOPPING`** (`base.py:285-290`).
- **`stop()` is idempotent when `STOPPED`/`STOPPING`** (no-op) (`base.py:317-320`).
- **`_start()` raising a non-`ComponentNotReady` exception → state `FAILED` and
  the exception is re-raised** (`base.py:304-307`).
- **`_start()` raising `ComponentNotReady` → state `STOPPED`, not re-raised**
  (`base.py:299-302`).
- **Every state transition auto-publishes `status`** via `_set_state`
  (`base.py:848-853`).

### Command handling (when wired through `_make_cmd_handler`)
- **Duplicate `request_id` per action is rejected** — a failure result is
  published and the handler is not invoked (`base.py:799-812`).
- **A handler exception publishes an `ok=False` result** instead of dying silently
  (`base.py:813-821`).
- **`cfg/logging/set` rejects unknown keys** (any key other than `log_level`)
  with an `ok=False` result (`base.py:594-604`).
- **`cfg/*/set` parse failures publish `ok=False`** via `_parse_cfg_set_payload`
  (`base.py:565-578`, callers `base.py:582, 623`).

### Telemetry
- **`set_telemetry_config` raises `ValueError`** if any `interval_s` or
  `change_threshold_percent` is non-numeric, boolean, or (for `interval_s`)
  non-positive (`base.py:701-712`). Validation happens for all metrics before any
  is applied.
- **Telemetry is gated.** `publish_telemetry` publishes only if
  `should_publish_telemetry` passes (enabled AND interval-elapsed OR
  change-threshold exceeded) (`base.py:484-496, 722-755`).

### Wire formats emitted (retained, `qos=1`)
Verbatim payloads a valid component's topics will carry — see ARCHITECTURE.md
for the full table. Enforced in the sense that the base class constructs exactly
these shapes:
- `status` → `{"state": <"idle"|"running"|"error">}`
- `cfg/logging` → `{"log_level": <str>}`
- `evt/<action>/result` → `{"request_id", "ok", "error"}` or, for cfg-set
  actions, `{"request_id", "ok", "applied", "error", "ts"}`

---

## CONVENTION ONLY

These are relied upon everywhere but **nothing in code checks them**. A component
can violate every one of these and construct/start without error.

- **`base_topic` = `lucid/agents/<agent_id>`.** Docstring only (`context.py:5, 50`).
  Nothing validates the format, and nothing checks `base_topic` is consistent
  with `agent_id`. [IMPROVEMENTS #4]
- **`component_id` unique per agent.** Nothing checks uniqueness.
- **`mqtt` implements `publish`/`subscribe`/`unsubscribe`.** `MqttPublisher` is a
  `Protocol` (not runtime-enforced) and `create()` does not validate `mqtt` —
  `mqtt=None` constructs fine and fails later. [IMPROVEMENTS #6]
- **Every command payload includes `request_id`.** README says "must"; a missing
  / empty `request_id` silently **bypasses** dedup (`base.py:798`). [IMPROVEMENTS #13]
- **Handlers are wired through `_make_cmd_handler`.** Dedup + exception-safety
  only exist if the agent does this wiring. Nothing in this repo connects a
  broker subscription to a handler. **[INFERRED / UNVERIFIED]**
- **Subclass calls `subscribe()` for its `cmd/*` topics and `unsubscribe()` in
  `_stop()`.** Docstring instructs (`base.py:526-542`); nothing enforces it.
- **Subclass publishes `metadata`/`status`/`state`/`cfg`/`schema` in `_start()`.**
  The base provides the helpers but does not call them; a component that never
  publishes is still "started." [INFERRED] from README examples.
- **`cmd/cfg/set` is handled.** Declared in `schema()` `subscribes` (`base.py:230`)
  and in `docs/README.md`, but **no built-in `on_cmd_cfg_set`** exists. A subclass
  must implement it; otherwise the advertised topic is dead. [CONTRACT GAP —
  IMPROVEMENTS P2]
- **`capabilities()` matches the `on_cmd_*` methods actually implemented.**
  `capabilities()` feeds `schema()`'s advertised `evt/*/result` list; nothing
  checks that a declared capability has a corresponding handler, or vice-versa.
- **Command action → method name mapping** (`cmd/ndi/input/set` →
  `on_cmd_ndi_input_set`, per `docs/README.md`). This dispatch mapping is **not in
  this repo** — no code here parses a topic into a method name. **[UNVERIFIED]**
  it lives in `lucid-agent-core`.
- **Log-level policy: `ERROR` by default.** The component logger defaults to
  `ERROR` (`base.py:146, 470`); INFO/DEBUG/WARNING never reach MQTT unless
  reconfigured. Convention, not validated.

---

## Contract gaps summary (for the auditor's checklist)

| Gap | Kind | Ref |
|---|---|---|
| `cmd/cfg/set` advertised but no base handler | dead-advertised topic | IMPROVEMENTS P2 |
| `request_id` "required" but unenforced; missing → dedup off | integrity | #13 |
| `mqtt` shape never validated | late failure | #6 |
| `agent_id` / `base_topic` consistency unchecked | integrity | #4 |
| Dedup guarantee depends on external wiring | [INFERRED] boundary | #13 |
| No check that `capabilities()` ↔ `on_cmd_*` agree | drift | — |
