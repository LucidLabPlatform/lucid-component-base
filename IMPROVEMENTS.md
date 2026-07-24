# IMPROVEMENTS.md — ranked findings from the audit

> Every issue surfaced this session, ranked. Each line: **what** — **why it
> matters** — *effort* (`<15min` / `hours` / `day+`). GitHub issue numbers in
> brackets. Contract gaps from `CONTRACT.md` are included.
>
> Ranking: **P1** = must fix before build · **P2** = fix during build ·
> **P3** = someday.
>
> Scope note: base.py telemetry gating (section 4), `__init__.py`, and tests had
> no dedicated findings pass — absence of P-items there is a coverage gap, not a
> clean bill.

---

## P1 — must fix before build

- **Invalid `log_level` returns `ok=True` with the old value** — `apply_log_level`
  only warns on a bad level; `on_cmd_cfg_logging_set` then reports success with
  the unchanged level, so the caller is lied to about the outcome. — *hours* — [#13, item 2]
- **`status` topic collapses real state to idle/running/error and never publishes
  `last_error`** — you cannot observe "not ready", "starting", "stopping", or
  *why* a component failed; foundational for any state-query meta-tool.
  (Also absorbs the orphaned `to_dict()`.) — *day+* — [#8]
- **No `LICENSE` file though MIT is declared** — package claims a license it does
  not ship; blocks a clean/lawful release. — *<15min* — [#1]

## P2 — fix during build

- **Dedup burns `request_id` before the handler succeeds** — a failed command's
  id is retained, so a legitimate retry is rejected as a duplicate; retry-after-
  failure is impossible. Needs a semantics decision. — *hours* — [#13, item 1]
- **Missing `request_id` silently disables dedup** — QoS-1 redelivery of a
  request_id-less command executes twice. — *hours* — [#13, item 3]
- **`_publish_json`: silent drop on serialize failure + unguarded broker publish**
  — a serialization error is swallowed; a broker-publish exception propagates up
  through `_set_state` and can push a component to `FAILED` on a broker hiccup.
  — *hours* — [#12]
- **`stop()`'s `except TypeError` shim is too broad** — a `TypeError` raised
  *inside* a correct `_stop()` is swallowed and `_stop()` is called twice.
  — *hours* — [#9]
- **`create()` does not validate `mqtt`** — `mqtt=None` constructs fine and fails
  later at first publish, far from the cause. — *<15min* — [#6]
- **`last_error` never cleared on a successful `start()`/`stop()`** — a healthy
  component retains a stale error string (user-visible once #8 publishes it).
  — *<15min* — [#10]
- **`cmd/cfg/set` advertised in `schema()` + docs but no base handler** — the
  topic is declared-but-dead unless every subclass implements it. — *hours* — [contract gap]
- **Logging subsystem needs an end-to-end rethink** — two divergent write paths
  to `logs`, inconsistent level casing, `publish_log` bypasses batching AND rate
  limiting, silent drops, no local fallback, ERROR-by-default. — *day+* — [#7]
- **Test dependencies live only in the Makefile** — `pytest`/`pytest-cov`/`build`
  aren't in packaging metadata; no `pip install -e ".[test]"` path. — *<15min* — [#2]

## P3 — someday

- **Command handling: consolidate three JSON parse paths** — `_parse_cmd_payload`
  (dead internally), `_parse_cfg_set_payload`, and inline parsing in
  `_make_cmd_handler`; every command is parsed twice; two parsers use different
  error contracts. — *hours* — [#13, item 4]
- **`_publish_handler_failure` selects the result envelope by string-matching the
  action name** (`startswith("cfg/") and endswith("/set")`) — fragile as actions
  grow. — *hours* — [#13, item 5]
- **`ComponentContext.agent_id` is stored, validated, and never read** — redundant
  with `base_topic`; consistency unenforced. Confirm no fleet consumer first.
  — *hours* — [#4]
- **`ComponentContext.logger()` is never used while `base.py` re-derives the
  logger name at 4 sites** — either use it or drop it. — *<15min* — [#5]
- **Add a `component_type` segment to the topic path** — surfacing type in the
  topic; breaking contract change across all consumers (id-uniqueness model
  unchanged). — *day+* — [#3]
- **Confirm/enforce the lifecycle threading model** — `_state` transitions are
  unlocked while telemetry/dedup are locked; document single-thread assumption or
  guard it. — *hours* — [#11]
- **Modernize license metadata** — `license = {text = "MIT"}` table form →
  SPDX string `license = "MIT"` (deprecation). — *<15min* — [#1, secondary]
- **`README.md` and `docs/README.md` are near-duplicates** — decide which is
  canonical to prevent drift. — *hours* — [no issue filed]
