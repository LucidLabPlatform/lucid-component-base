"""
Component context — shared runtime passed to all components.

Provides agent_id, base_topic, component_id, MQTT publisher, config, and a topic() helper.
Unified contract: topics under lucid/agents/<agent_id>/components/<component_id>/.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Protocol


class MqttPublisher(Protocol):
    """
    Minimal interface components need from the MQTT client.
    """

    def publish(
        self, topic: str, payload: Any, *, qos: int = 0, retain: bool = False
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ComponentContext:
    """
    Shared runtime context passed to all components.

    Rules:
    - agent_id must be stable and non-empty.
    - base_topic is the agent topic root (e.g. lucid/agents/<agent_id>).
    - component_id identifies the component for topic construction.
    - mqtt must expose the publish() API.
    """

    agent_id: str
    base_topic: str
    component_id: str
    mqtt: MqttPublisher
    config: Dict[str, Any]

    def topic(self, suffix: str) -> str:
        """Build component-scoped topic: {base_topic}/components/{component_id}/{suffix}."""
        return f"{self.base_topic}/components/{self.component_id}/{suffix}"

    def logger(self) -> logging.Logger:
        """Component-scoped logger name."""
        return logging.getLogger(f"lucid.component.{self.component_id}")

    @staticmethod
    def create(
        *,
        agent_id: str,
        base_topic: str,
        component_id: str,
        mqtt: MqttPublisher,
        config: Dict[str, Any],
    ) -> "ComponentContext":
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent_id must be a non-empty string")
        if not isinstance(base_topic, str) or not base_topic:
            raise ValueError("base_topic must be a non-empty string")
        if not isinstance(component_id, str) or not component_id:
            raise ValueError("component_id must be a non-empty string")
        if not isinstance(config, dict):
            raise ValueError("config must be a dict")
        return ComponentContext(
            agent_id=agent_id,
            base_topic=base_topic,
            component_id=component_id,
            mqtt=mqtt,
            config=config,
        )
