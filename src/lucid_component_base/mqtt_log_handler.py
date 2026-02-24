"""
MQTT logging handler for components.

Publishes log messages to MQTT /logs topic with rate limiting to prevent flooding.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Batching and rate limiting configuration
MAX_LINES_PER_BATCH = 50  # Max log lines per MQTT message
BATCH_INTERVAL_S = 0.25  # Publish batch every 0.25 seconds if not full
MAX_BATCHES_PER_WINDOW = 25  # Max batches per time window
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
        self._buffer: list[dict[str, Any]] = []  # List of structured log line dicts
        self._last_publish_ts = time.time()
        self._batch_timer: Optional[threading.Timer] = None
        
        # Rate limiting state
        self._batch_timestamps: list[float] = []  # Timestamps of published batches
        self._dropped_count = 0
        self._last_warning_ts = 0.0

    @staticmethod
    def _level_to_mqtt(levelno: int) -> str:
        level_map = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warning",
            logging.ERROR: "error",
            logging.CRITICAL: "error",
        }
        return level_map.get(levelno, "info")

    @staticmethod
    def _utc_iso_from_epoch(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    def _build_line(self, record: logging.LogRecord) -> dict[str, Any]:
        line: dict[str, Any] = {
            "ts": self._utc_iso_from_epoch(record.created),
            "level": self._level_to_mqtt(record.levelno),
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            try:
                line["exception"] = "".join(traceback.format_exception(*record.exc_info)).strip()
            except Exception:
                pass
        elif record.exc_text:
            line["exception"] = str(record.exc_text)

        return line

    def _warn_rate_limit_drop(self, now: float) -> None:
        if now - self._last_warning_ts < 10.0:
            return
        logger.warning(
            "MQTT log handler dropped %d log lines due to rate limiting",
            self._dropped_count,
        )
        self._dropped_count = 0
        self._last_warning_ts = now
        
    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record - add to buffer and publish batch if needed."""
        try:
            # Ignore handler-internal logs to avoid recursion.
            if record.name == __name__:
                return

            # Add to buffer
            with self._lock:
                self._buffer.append(self._build_line(record))
                
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
            self._warn_rate_limit_drop(now)
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
            "count": len(batch),
            "lines": batch,
        }
        
        try:
            self.component._publish_json(self.topic, payload, retain=False, qos=0)
        except Exception:
            # Don't log errors from the log handler itself to avoid recursion
            pass
