"""
Component base class and lifecycle state.

Unified MQTT contract: retained metadata, status, state, cfg;
stream logs, telemetry/<metric>; commands cmd/reset, cmd/ping, cmd/cfg/set;
results evt/<action>/result.
"""
from __future__ import annotations

import json
import logging
import time
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


def _status_to_contract(status: ComponentStatus) -> str:
    """Map internal status to contract: idle | running | error."""
    if status == ComponentStatus.RUNNING:
        return "running"
    if status == ComponentStatus.FAILED:
        return "error"
    return "idle"


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

    Lifecycle contract:
    - start() is idempotent when RUNNING.
    - stop() is idempotent when STOPPED.
    - stop() may be called even if start() failed.

    Unified MQTT: subclasses use publish_metadata, publish_status, publish_state,
    publish_cfg, publish_log, publish_telemetry (gated),
    publish_result, publish_cfg_set_result. Telemetry gating is implemented centrally.
    """

    # Default telemetry config; override via set_telemetry_config()
    # Structure: { "metrics": { "metric_name": { "enabled": bool, "interval_s": int, "change_threshold_percent": float } } }
    _DEFAULT_TELEMETRY_CFG: Dict[str, Any] = {
        "metrics": {},
    }

    def __init__(self, context: ComponentContext) -> None:
        self.context = context
        self._state = ComponentState()
        self._telemetry_cfg: Dict[str, Any] = dict(self._DEFAULT_TELEMETRY_CFG)
        self._telemetry_last: Dict[str, tuple[Any, float]] = {}  # metric -> (value, last_publish_ts)
        self._logs_enabled: bool = False  # Default: logs disabled
        self._mqtt_logging_setup: bool = False  # Track if MQTT logging handler has been set up
        # MQTT logging will be set up after component_id is available (in start())

    @property
    def component_id(self) -> str:
        """Unique component identifier. Override in subclasses."""
        raise NotImplementedError

    @property
    def version(self) -> str:
        """Package version from installed package metadata."""
        return __version__

    def metadata(self) -> Dict[str, Any]:
        """
        Component metadata snapshot. Override to add capabilities.
        Contract: { component_id, version, capabilities?: ["reset","ping", ...] }
        """
        return {
            "component_id": self.component_id,
            "version": self.version,
        }

    @property
    def state(self) -> ComponentState:
        return self._state

    def capabilities(self) -> list[str]:
        """Override in subclasses to declare cmd support, e.g. ["reset","ping"]."""
        return []

    def get_state_payload(self) -> Dict[str, Any]:
        """Current state for retained state topic. Override in subclasses. Contract: { "<metric>": value }."""
        return {}

    def start(self) -> None:
        # Set up MQTT logging now that component_id is available
        if not self._mqtt_logging_setup:
            self._setup_mqtt_logging()
            self._mqtt_logging_setup = True
        
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
    # Unified MQTT publishing
    # -------------------------

    def publish_metadata(self) -> None:
        """Publish retained metadata. Contract: { component_id, version, capabilities }."""
        payload = dict(self.metadata())
        payload["capabilities"] = self.capabilities()
        self._publish_retained("metadata", payload)

    def publish_status(self) -> None:
        """Publish retained status. Contract: { state: "idle"|"running"|"error" }."""
        payload = {"state": _status_to_contract(self._state.status)}
        self._publish_retained("status", payload)

    def publish_state(self, state_payload: Optional[Dict[str, Any]] = None) -> None:
        """Publish retained state. Contract: { "<metric>": value }. Uses get_state_payload() if None."""
        payload = state_payload if state_payload is not None else self.get_state_payload()
        self._publish_retained("state", payload)

    def publish_cfg(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        """
        Publish retained cfg. Always includes ALL configurable keys with current values or defaults.
        Contract: { telemetry: { metrics: { metric_name: { enabled, interval_s, change_threshold_percent } } } }.
        If cfg is None, uses current telemetry config and ensures all state metrics are present with per-metric configs.
        """
        if cfg is None:
            # Get available metrics from state
            state_payload = self.get_state_payload()
            available_metrics = set(state_payload.keys()) if isinstance(state_payload, dict) else set()
            
            # Get current metrics config
            current_metrics = dict(self._telemetry_cfg.get("metrics", {}))
            
            # Ensure all state metrics are present with full config structure
            metrics_cfg = {}
            for metric_name in available_metrics:
                if metric_name in current_metrics and isinstance(current_metrics[metric_name], dict):
                    # Use existing config
                    metrics_cfg[metric_name] = {
                        "enabled": bool(current_metrics[metric_name].get("enabled", False)),
                        "interval_s": int(current_metrics[metric_name].get("interval_s", 2)),
                        "change_threshold_percent": float(current_metrics[metric_name].get("change_threshold_percent", 2.0)),
                    }
                else:
                    # Default config for new metric
                    metrics_cfg[metric_name] = {
                        "enabled": False,
                        "interval_s": 2,
                        "change_threshold_percent": 2.0,
                    }
            
            cfg = {
                "logs_enabled": self._logs_enabled,
                "telemetry": {
                    "metrics": metrics_cfg,
                },
            }
        else:
            # Ensure cfg always has telemetry with all metrics
            if "telemetry" not in cfg:
                cfg["telemetry"] = {}
            if "metrics" not in cfg["telemetry"]:
                cfg["telemetry"]["metrics"] = {}
            
            # Get available metrics from state
            state_payload = self.get_state_payload()
            available_metrics = set(state_payload.keys()) if isinstance(state_payload, dict) else set()
            
            # Ensure all state metrics are present
            for metric_name in available_metrics:
                if metric_name not in cfg["telemetry"]["metrics"]:
                    cfg["telemetry"]["metrics"][metric_name] = {
                        "enabled": False,
                        "interval_s": 2,
                        "change_threshold_percent": 2.0,
                    }
                else:
                    # Ensure metric config has all required fields
                    metric_cfg = cfg["telemetry"]["metrics"][metric_name]
                    if not isinstance(metric_cfg, dict):
                        metric_cfg = {}
                        cfg["telemetry"]["metrics"][metric_name] = metric_cfg
                    if "enabled" not in metric_cfg:
                        metric_cfg["enabled"] = False
                    if "interval_s" not in metric_cfg:
                        metric_cfg["interval_s"] = 2
                    if "change_threshold_percent" not in metric_cfg:
                        metric_cfg["change_threshold_percent"] = 2.0
            
            # Ensure logs_enabled is present
            if "logs_enabled" not in cfg:
                cfg["logs_enabled"] = self._logs_enabled
        
        self._publish_retained("cfg", cfg)

    def publish_log(self, level: str, message: str) -> None:
        """Publish logs stream. level: debug|info|warning|error."""
        topic = self.context.topic("logs")
        payload = {"level": level, "message": message}
        self._publish_json(topic, payload, retain=False, qos=0)
    
    def _setup_mqtt_logging(self) -> None:
        """Set up MQTT logging handler for component logs."""
        try:
            from lucid_component_base.mqtt_log_handler import MQTTLogHandler
            
            # Create a logger specific to this component
            component_logger = logging.getLogger(f"lucid.component.{self.component_id}")
            
            # Only add handler if not already added
            for handler in component_logger.handlers:
                if isinstance(handler, MQTTLogHandler):
                    return  # Already added
            
            # Create and add handler
            handler = MQTTLogHandler(self, self.context.topic("logs"))
            handler.setLevel(logging.DEBUG)  # Handler level, actual filtering done by logger level
            component_logger.addHandler(handler)
            component_logger.setLevel(logging.NOTSET)  # Inherit from root logger
            logger.debug("MQTT logging handler added for component %s", self.component_id)
        except Exception as exc:
            logger.warning("Failed to set up MQTT logging for component %s: %s", self.component_id, exc)

    def publish_telemetry(self, metric: str, value: Any) -> None:
        """
        Publish telemetry stream only if gating allows.
        Contract: topic telemetry/<metric>, payload { "value": value }.
        Always update retained state via publish_state from subclass if needed.
        """
        if not self.should_publish_telemetry(metric, value):
            return
        topic = self.context.topic(f"telemetry/{metric}")
        payload = {"value": value}
        self._publish_json(topic, payload, retain=False, qos=0)
        self._telemetry_last[metric] = (value, time.time())

    def publish_result(self, action: str, request_id: str, ok: bool, error: Optional[str] = None) -> None:
        """Publish evt/<action>/result. Contract: { request_id, ok, error }."""
        topic = self.context.topic(f"evt/{action}/result")
        payload = {"request_id": request_id, "ok": ok, "error": error}
        self._publish_json(topic, payload, retain=False, qos=1)

    def publish_cfg_set_result(
        self,
        request_id: str,
        ok: bool,
        applied: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> None:
        """Publish evt/cfg/set/result. Contract: { request_id, ok, applied, error, ts }."""
        topic = self.context.topic("evt/cfg/set/result")
        payload = {
            "request_id": request_id,
            "ok": ok,
            "applied": applied,
            "error": error,
            "ts": ts if ts is not None else _utc_iso(),
        }
        self._publish_json(topic, payload, retain=False, qos=1)

    def set_telemetry_config(self, cfg: Dict[str, Any]) -> None:
        """
        Update telemetry config for gating.
        Structure: { "metrics": { "metric_name": { "enabled": bool, "interval_s": int, "change_threshold_percent": float } } }
        """
        metrics = cfg.get("metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}
        
        # Ensure each metric config has all required fields
        normalized_metrics = {}
        for metric_name, metric_cfg in metrics.items():
            if isinstance(metric_cfg, dict):
                normalized_metrics[metric_name] = {
                    "enabled": bool(metric_cfg.get("enabled", False)),
                    "interval_s": int(metric_cfg.get("interval_s", 2)),
                    "change_threshold_percent": float(metric_cfg.get("change_threshold_percent", 2.0)),
                }
            elif isinstance(metric_cfg, bool):
                # Backward compatibility: allow simple boolean
                normalized_metrics[metric_name] = {
                    "enabled": metric_cfg,
                    "interval_s": 2,
                    "change_threshold_percent": 2.0,
                }
        
        self._telemetry_cfg = {"metrics": normalized_metrics}

    def should_publish_telemetry(self, metric: str, value: Any) -> bool:
        """
        True if telemetry stream should be published based on per-metric config.
        Checks: metric enabled, and (delta > threshold or interval exceeded).
        """
        metrics = self._telemetry_cfg.get("metrics") or {}
        metric_cfg = metrics.get(metric)
        if not isinstance(metric_cfg, dict):
            return False
        
        if not metric_cfg.get("enabled", False):
            return False
        
        interval_s = max(1, metric_cfg.get("interval_s", 2))
        threshold = max(0.0, metric_cfg.get("change_threshold_percent", 2.0))
        now = time.time()
        last = self._telemetry_last.get(metric)
        
        if last is None:
            return True
        
        last_value, last_ts = last
        if now - last_ts >= interval_s:
            return True
        
        try:
            if isinstance(last_value, (int, float)) and isinstance(value, (int, float)):
                if last_value == 0:
                    return value != 0
                delta_pct = abs(value - last_value) / abs(last_value) * 100.0
                return delta_pct >= threshold
        except TypeError:
            pass
        return value != last_value

    def _publish_retained(self, suffix: str, payload: Dict[str, Any]) -> None:
        topic = self.context.topic(suffix)
        self._publish_json(topic, payload, retain=True, qos=1)

    def _publish_json(self, topic: str, payload: Dict[str, Any], *, retain: bool = False, qos: int = 0) -> None:
        try:
            body = json.dumps(payload)
        except (TypeError, ValueError) as e:
            logger.error("JSON encode failed for %s: %s", topic, e)
            return
        self.context.mqtt.publish(topic, body, qos=qos, retain=retain)

    # -------------------------
    # Subclass hooks
    # -------------------------
    def _start(self) -> None:
        raise NotImplementedError

    def _stop(self) -> None:
        raise NotImplementedError

    def _set_state(self, status: ComponentStatus) -> None:
        self._state.status = status
        self._state.updated_at = _utc_iso()
        logger.debug("Component %s state=%s", self.component_id, status.value)
        # Keep MQTT status topic in sync (e.g. "running" after auto-start)
        self.publish_status()
  