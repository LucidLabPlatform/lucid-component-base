# lucid-component-base

Component contract and runtime context for LUCID components.

This package defines the **SDK layer only**.
It has **zero dependency on `lucid-agent-core`** and does not implement installation, loading, supervision, or MQTT routing.

---

## Purpose

`lucid-component-base` provides:

* The **component lifecycle contract**
* The **runtime context interface**
* Shared state models and enums
* Git-tag–based package versioning

It does **not** provide:

* Component installation
* Component discovery
* Component supervision
* MQTT client implementation
* Topic schema management

Those responsibilities belong to `lucid-agent-core`.

---

## Installation

Editable install:

```bash
pip install -e .
```

When building from a git-tagged release, the package version is derived from the git tag via `setuptools_scm`.

---

## Versioning

Package version is determined from the repository git tag at build time.

The resolved version is available as:

```python
from lucid_component_base import __version__
```

The `Component.version` property returns this installed package version.

---

## Public API

### `Component`

Base class for all LUCID components.

Lifecycle guarantees:

* `start()` is idempotent
* `stop()` is idempotent
* `_start()` and `_stop()` must be implemented by subclasses
* Exceptions are captured in `ComponentState` and re-raised

Minimal required override:

```python
@property
def component_id(self) -> str:
    ...
```

---

### `ComponentContext`

Immutable runtime context injected by the agent.

Fields:

* `agent_id: str`
* `base_topic: str`
* `component_id: str`
* `mqtt: MqttPublisher`
* `config: object`

Helper:

```python
context.topic("state")
# -> {base_topic}/components/{component_id}/state
```

The context exposes only what a component is allowed to access.

---

### `ComponentStatus`

Enum:

* `stopped`
* `starting`
* `running`
* `stopping`
* `failed`

---

### `ComponentState`

Dataclass representing lifecycle state:

* `status`
* `last_error`
* `started_at`
* `stopped_at`
* `updated_at`

`Component.state` returns a `ComponentState` instance.

---

## Example

```python
from lucid_component_base import Component

class MyComponent(Component):

    @property
    def component_id(self) -> str:
        return "my_component"

    def _start(self) -> None:
        print("Starting")

    def _stop(self) -> None:
        print("Stopping")
```

---

## Architectural Boundary

* `lucid-component-base` defines **what a component is**
* `lucid-agent-core` defines **how components are installed, loaded, and supervised**

This separation is intentional and enforced.
