"""
MQTT logging handler for components.

Publishes log messages to MQTT /logs topic with rate limiting to prevent flooding.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Batching and rate limiting configuration
MAX_LINES_PER_BATCH = 20  # Max log lines per MQTT message
BATCH_INTERVAL_S = 0.5  # Publish batch every 0.5 seconds if not full
MAX_BATCHES_PER_WINDOW = 5  # Max batches per time window
TIME_WINDOW_S = 2.0  # 2 second window for rate limiting


class MQTTLogHandler(logging.Handler):
    """
    Logging handler that publishes log messages to MQTT /logs topic.
    
    Batches multiple log lines into single messages and implements rate limiting:
    - Batches up to MAX_LINES_PER_BATCH lines per message
    - Publishes batches every BATCH_INTERVAL_S seconds or when batch is full
    - Limits to MAX_BATCHES_PER_WINDOW batches per TIME_WINDOW_S seconds
    """
    
    def __init__(self, component: Any, topic: str) -> None:
        """
        Initialize MQTT log handler.
        
        Args:
            component: Component instance with _publish_json() method
            topic: MQTT topic to publish logs to
        """
        super().__init__()
        self.component = component
        self.topic = topic
        
        # Batching state
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []  # List of {level, message} dicts
        self._last_publish_ts = time.time()
        self._batch_timer: Optional[threading.Timer] = None
        
        # Rate limiting state
        self._batch_timestamps: list[float] = []  # Timestamps of published batches
        self._dropped_count = 0
        self._last_warning_ts = 0.0
        
    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record - add to buffer and publish batch if needed."""
        try:
            # Check if logs are enabled
            if not getattr(self.component, "_logs_enabled", False):
                return  # Logs disabled, skip publishing
            
            # Map Python log levels to MQTT log levels
            level_map = {
                logging.DEBUG: "debug",
                logging.INFO: "info",
                logging.WARNING: "warning",
                logging.ERROR: "error",
                logging.CRITICAL: "error",
            }
            mqtt_level = level_map.get(record.levelno, "info")
            
            # Format message
            message = self.format(record)
            
            # Add to buffer
            with self._lock:
                self._buffer.append({
                    "level": mqtt_level,
                    "message": message,
                })
                
                # If buffer is full, publish immediately
                if len(self._buffer) >= MAX_LINES_PER_BATCH:
                    self._publish_batch()
                else:
                    # Schedule a timer to publish if not already scheduled
                    if self._batch_timer is None:
                        self._batch_timer = threading.Timer(
                            BATCH_INTERVAL_S,
                            self._publish_batch_timer
                        )
                        self._batch_timer.daemon = True
                        self._batch_timer.start()
                        
        except Exception:
            # Silently ignore errors to prevent recursion
            pass
    
    def _publish_batch_timer(self) -> None:
        """Called by timer to publish batch."""
        with self._lock:
            self._batch_timer = None
            if self._buffer:
                self._publish_batch()
    
    def _publish_batch(self) -> None:
        """Publish current buffer as a batch if rate limit allows."""
        if not self._buffer:
            return
        
        # Check rate limit
        now = time.time()
        cutoff = now - TIME_WINDOW_S
        self._batch_timestamps = [ts for ts in self._batch_timestamps if ts > cutoff]
        
        if len(self._batch_timestamps) >= MAX_BATCHES_PER_WINDOW:
            # Rate limit exceeded - drop oldest entries from buffer
            dropped = len(self._buffer)
            self._buffer = []
            self._dropped_count += dropped
            
            # Warn occasionally
            if now - self._last_warning_ts >= 10.0:
                logger.warning(
                    "MQTT log handler: dropped %d log lines due to rate limiting",
                    self._dropped_count
                )
                self._dropped_count = 0
                self._last_warning_ts = now
            return
        
        # Copy buffer and clear it
        batch = list(self._buffer)
        self._buffer = []
        self._last_publish_ts = now
        
        # Cancel timer if it's running
        if self._batch_timer:
            self._batch_timer.cancel()
            self._batch_timer = None
        
        # Record this batch publish
        self._batch_timestamps.append(now)
        
        # Publish batch using component's publish method
        payload = {
            "lines": batch,
        }
        
        try:
            self.component._publish_json(self.topic, payload, retain=False, qos=0)
        except Exception:
            # Don't log errors from the log handler itself to avoid recursion
            pass
