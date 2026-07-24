# DEBT.md — lucid-component-base

> Two sections. **Dead code** = unreachable / unused / orphaned, plain list, no
> analysis. **Vibecode debt** = works, but nobody in this session could say *why*
> — unjustified, distinct from `IMPROVEMENTS.md` (which is known-wrong).
>
> Scope: `pyproject.toml`, `_version.py`, `context.py`, `mqtt_log_handler.py`,
> `base.py` sections 1–3. Telemetry gating, `__init__.py`, and tests were not
> audited in depth.

---

## Section 1 — Dead code

- `src/lucid_component_base/context.py:56` — `ComponentContext.agent_id`: stored
  and validated (`context.py:78-79`), never read anywhere in `src/`. (Public field.)
- `src/lucid_component_base/context.py:65` — `ComponentContext.logger()`: never
  called in `src/`.
- `src/lucid_component_base/base.py:78` — `ComponentState.to_dict()`: called only
  in `tests/`, never in production paths.
- `src/lucid_component_base/base.py:544` — `Component._parse_cmd_payload()`: never
  called in `src/`. (Docstring says it is for subclasses.)
- `src/lucid_component_base/base.py:43` — `CmdPayloadError`: raised only inside the
  never-called `_parse_cmd_payload` (`base.py:559, 561`); never reached internally.
  (Public API — exported in `__init__.py`.)

---

## Section 2 — Vibecode debt (works, unexplained)

- `pyproject.toml:21` — `version_scheme = "no-guess-dev"`: the specific scheme
  choice is not justified by anything in the repo. [INFERRED intent only]
- `base.py:325-329` — the `_stop(final=...)` → `except TypeError` → `_stop()`
  fallback: exists for subclasses whose `_stop()` predates the `final` kwarg, but
  no such subclass is in this repo to confirm the need. [INFERRED target]
- `base.py:471-473` — component logger `propagate = False` with the MQTT handler as
  its only sink: means component logs have no local fallback, but no comment
  explains whether that trade-off was intended.
- `README.md` vs `docs/README.md` — two near-duplicate API references coexist; no
  statement of which is canonical or why both are kept.
