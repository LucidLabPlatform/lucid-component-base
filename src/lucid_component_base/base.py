"""
Component base class and lifecycle state.

All LUCID components extend Component and implement _start()/_stop().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from ._version import __version__
from .context import ComponentContext

logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ComponentStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass
class ComponentState:
    status: ComponentStatus = ComponentStatus.STOPPED
    last_error: Optional[str] = None
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    updated_at: str = field(default_factory=_utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "updated_at": self.updated_at,
        }


class Component:
    """
    Base class for all LUCID components.

    Lifecycle contract (must hold for all components):
    - start() is idempotent:
        calling start() when RUNNING does nothing.
    - stop() is idempotent:
        calling stop() when STOPPED does nothing.
    - stop() may be called even if start() failed; component must best-effort release resources.
    - Exceptions from _start/_stop are captured into state and re-raised.

    Components should publish their state/metadata through the provided context and topic schema,
    but the base class does not enforce MQTT publishing yet. (That can be added once topic model is locked.)
    """

    def __init__(self, context: ComponentContext) -> None:
        self.context = context
        self._state = ComponentState()

    @property
    def component_id(self) -> str:
        """
        Unique component identifier (must be stable).
        Override in subclasses.
        """
        raise NotImplementedError

    @property
    def version(self) -> str:
        """Package version from installed package metadata (baked into wheel at build time)."""
        return __version__

    def metadata(self) -> Dict[str, Any]:
        """
        Component metadata snapshot. Override as needed, but keep it JSON-serializable.
        """
        return {
            "component_id": self.component_id,
            "version": self.version,
        }

    @property
    def state(self) -> ComponentState:
        return self._state

    def start(self) -> None:
        if self._state.status == ComponentStatus.RUNNING:
            return
        if self._state.status in (ComponentStatus.STARTING, ComponentStatus.STOPPING):
            raise RuntimeError(f"Cannot start component in state: {self._state.status.value}")

        self._set_state(ComponentStatus.STARTING)

        try:
            self._start()
            self._state.started_at = _utc_iso()
            self._state.stopped_at = None
            self._set_state(ComponentStatus.RUNNING)
        except Exception as exc:
            self._state.last_error = str(exc)
            self._set_state(ComponentStatus.FAILED)
            raise

    def stop(self) -> None:
        if self._state.status == ComponentStatus.STOPPED:
            return
        if self._state.status == ComponentStatus.STOPPING:
            return

        self._set_state(ComponentStatus.STOPPING)

        try:
            self._stop()
            self._state.stopped_at = _utc_iso()
            self._set_state(ComponentStatus.STOPPED)
        except Exception as exc:
            self._state.last_error = str(exc)
            self._set_state(ComponentStatus.FAILED)
            raise

    # -------------------------
    # To be implemented by subclasses
    # -------------------------
    def _start(self) -> None:
        """
        Subclass hook for start logic.
        """
        raise NotImplementedError

    def _stop(self) -> None:
        """
        Subclass hook for stop logic.
        """
        raise NotImplementedError

    # -------------------------
    # Internal
    # -------------------------
    def _set_state(self, status: ComponentStatus) -> None:
        self._state.status = status
        self._state.updated_at = _utc_iso()
        logger.debug("Component %s state=%s", self.component_id, status.value)
