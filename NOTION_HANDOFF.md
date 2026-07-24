# NOTION_HANDOFF — lucid-component-base

> Condensed digest for planning. Derived from code read this session. Coverage:
> `pyproject.toml`, `_version.py`, `context.py`, `mqtt_log_handler.py`, `base.py`
> sections 1–3. **Not** audited in depth: telemetry gating, `__init__.py`, tests.

## Summary
`lucid-component-base` is the zero-dependency contract core / SDK for LUCID
components: a `Component` base class plus a `ComponentContext` handle, MQTT
publish/subscribe helpers, a lifecycle state machine, command dedup, telemetry
gating, and a batching/rate-limiting log handler. The core model (idempotent
lifecycle, retained-state vs stream vs event topics, request-id dedup) is sound
and coherent. But the *external reflection* of state is lossy and several error
paths are underbaked — the `status` topic collapses to idle/running/error and
never emits `last_error`, an invalid `log_level` reports false success, dedup
blocks retry-after-failure, and the publish choke point both silently drops and
can break lifecycle on a broker hiccup. It is in a "works in the happy path,
needs hardening before it's a dependable contract" state. 13 GitHub issues filed.

## Contract spec — enforced
- Subclass must override `component_id`, `_start()`, `_stop()` (else
  `NotImplementedError`).
- `create()` validates `agent_id`/`base_topic`/`component_id` non-empty str,
  `config` dict (else `ValueError`); `ComponentContext` is frozen/immutable.
- `start()` idempotent when RUNNING; raises `RuntimeError` if STARTING/STOPPING.
  `stop()` idempotent when STOPPED/STOPPING.
- `_start()` non-ready exception (`ComponentNotReady`) → STOPPED, not raised; any
  other exception → FAILED, re-raised. Every transition auto-publishes `status`.
- Duplicate `request_id` rejected + handler exceptions → `ok=False` result — *when
  wired through `_make_cmd_handler`*.
- `cfg/logging/set` rejects unknown keys; `set_telemetry_config` raises on
  non-numeric / non-positive `interval_s` / bad `change_threshold_percent`.

## Contract spec — convention only
- `base_topic = lucid/agents/<agent_id>` and its consistency with `agent_id` —
  unchecked.
- `component_id` uniqueness — unchecked.
- `mqtt` implements publish/subscribe/unsubscribe — unchecked (`create()` skips
  `mqtt`; Protocol not runtime-enforced).
- Every command carries `request_id` — unenforced; missing → dedup silently off.
- Handlers wired through `_make_cmd_handler` — [INFERRED] external; nothing here
  connects broker → handler.
- Subclass publishes metadata/status/state/cfg/schema and calls
  subscribe()/unsubscribe() — instructed by docstring only.
- `cmd/cfg/set` advertised but **no base handler**; subclass must implement.
- Topic→`on_cmd_*` method dispatch lives in agent-core, not here. [UNVERIFIED]

## Invariants at risk
- Command idempotency depends on external `_make_cmd_handler` wiring + a present
  `request_id`; both are assumptions, not guarantees.
- `mqtt=None` (or wrong shape) passes construction, fails at first publish.
- Lifecycle `_state` is mutated without a lock (telemetry/dedup are locked) —
  safe only if a single thread drives lifecycle. [INFERRED]
- `base_topic`/`agent_id` can silently disagree.

## P1 items
- Invalid `log_level` returns `ok=True` with the stale value — command lies. *hours* [#13]
- `status` collapses real state + never publishes `last_error` — no observability. *day+* [#8]
- No `LICENSE` file though MIT declared — blocks clean release. *<15min* [#1]

## P2 items
- Dedup burns `request_id` before success → retry-after-failure rejected. *hours* [#13]
- Missing `request_id` disables dedup → QoS-1 double-execution. *hours* [#13]
- `_publish_json` silent-drop + unguarded broker publish can force FAILED. *hours* [#12]
- `stop()` `except TypeError` too broad → hides `_stop()` bugs, double cleanup. *hours* [#9]
- `create()` doesn't validate `mqtt`. *<15min* [#6]
- `last_error` never cleared on success. *<15min* [#10]
- `cmd/cfg/set` advertised but unimplemented in base. *hours* [contract gap]
- Logging subsystem end-to-end rethink. *day+* [#7]
- Test deps only in Makefile, not metadata. *<15min* [#2]

## P3 items
- Consolidate 3 JSON parse paths; `_parse_cmd_payload` dead. *hours* [#13]
- `_publish_handler_failure` envelope-by-string-match. *hours* [#13]
- `agent_id` stored/validated/never read. *hours* [#4]
- `logger()` unused vs 4× duplicated logger name. *<15min* [#5]
- Add `component_type` topic segment (breaking). *day+* [#3]
- Confirm/enforce lifecycle threading model. *hours* [#11]
- License table form → SPDX string. *<15min* [#1]
- README vs docs/README duplication. *hours* [no issue]

## Dead code count + vibecode debt count
- Dead code: **5**
- Vibecode debt: **4**

## AI-layer notes — where this contract helps or blocks meta-tools
Answered from code (retained/stream/event semantics in `base.py` + `context.py`).

- **Queryable current state — PARTIAL.** Retained topics (`metadata`, `status`,
  `state`, `cfg*`, `schema`) are published `qos=1, retain=True`, so a meta-tool
  connecting late gets the last value from the broker — this *does* give
  `get_state`/`search_catalog` a queryable snapshot. **But:** (a) `state` content
  is entirely subclass-defined (base default `{}`) — the contract guarantees the
  *topic*, not any field; (b) `status` is **lossy** — collapsed to
  idle/running/error with no `last_error` on the wire, so `get_state` cannot see
  "not ready", transitions, or failure reasons [#8]; (c) there is no retained
  "component exists / is alive" liveness beacon beyond `status` — no LWT/heartbeat
  in this repo.
- **Command completion signal — YES, with caveats.** `evt/<action>/result` carries
  `{request_id, ok, error}` (or the cfg-set shape) at `qos=1`, and
  `_make_cmd_handler` publishes `ok=False` on handler exception — so `execute` /
  `wait_for` have an explicit, request-id-keyed completion signal including
  failures. **Caveats:** results are **not retained**, so a meta-tool must be
  subscribed at/ before command time — a late subscriber never sees the result;
  dedup may reject a retried `request_id` even after a failure [#13]; and the
  completion guarantee assumes the agent wired the handler through
  `_make_cmd_handler` [INFERRED].
- **`get_history` — NOT supported by this repo.** Nothing persists past state or
  events; streams are `qos=0` non-retained, results are non-retained. Any history
  layer must live outside `lucid-component-base`.
- **Net:** the contract is a good substrate for `search_catalog` (retained
  metadata/schema) and `execute`/`wait_for` (result events), a *weak* substrate
  for `get_state` until `status` is enriched (#8), and **no** substrate for
  `get_history`.

## Open questions for the lab
- Is `base_topic`/`agent_id`/`component_id` uniqueness guaranteed upstream, or
  should this repo enforce it? [#4]
- Who wires `cmd/*` → `_make_cmd_handler`, and is dedup therefore actually always
  on? [#13, INFERRED]
- Is lifecycle single-threaded by contract, or does `_state` need locking? [#11]
- Should `status` carry full state (`to_dict()`) so meta-tools can observe
  reality? [#8]
- Is `cmd/cfg/set` supposed to be a base built-in, or always subclass-provided?
- Which of `README.md` / `docs/README.md` is canonical?
- Is `>=3.11` a hard floor? (Confirmed "yes" verbally this session; no
  3.11-only syntax found in code.)
