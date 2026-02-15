"""
Component context — shared runtime passed to all components.

Provides agent_id, base_topic, component_id, MQTT publisher, config, and a topic() helper.
No dependency on agent-core or TopicSchema.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol


class MqttPublisher(Protocol):
    """
    Minimal interface components need from the MQTT client.
    Keep it small to prevent tight coupling.
    """

    def publish(self, topic: str, payload, *, qos: int = 0, retain: bool = False) -> None: ...


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
    config: object

    def topic(self, suffix: str) -> str:
        """Build a component-scoped topic: {base_topic}/components/{component_id}/{suffix}."""
        return f"{self.base_topic}/components/{self.component_id}/{suffix}"

    def logger(self) -> logging.Logger:
        """
        Component-scoped logger name.
        """
        return logging.getLogger(f"lucid.component.{self.component_id}")

    @staticmethod
    def create(
        *,
        agent_id: str,
        base_topic: str,
        component_id: str,
        mqtt: MqttPublisher,
        config: object,
    ) -> "ComponentContext":
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent_id must be a non-empty string")
        if not isinstance(base_topic, str) or not base_topic:
            raise ValueError("base_topic must be a non-empty string")
        if not isinstance(component_id, str) or not component_id:
            raise ValueError("component_id must be a non-empty string")
        return ComponentContext(
            agent_id=agent_id,
            base_topic=base_topic,
            component_id=component_id,
            mqtt=mqtt,
            config=config,
        )
