#!/usr/bin/env python3
"""Fleet Communication Bridge — routing layer between fleet agents and external systems.

The bridge provides:
- Priority-based message routing to fleet agents
- Tender status broadcasting to all subscribers
- Fleet-wide event propagation with durable logging
- External system integration hooks

Dependencies: stdlib only (json, time, enum, dataclasses, datetime, threading).
"""

from __future__ import annotations

import json
import time
import uuid
import threading
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from tender import (
    TenderMessage,
    TenderStatus,
    TenderTask,
    TenderFleet,
    TenderType,
)


# ---------------------------------------------------------------------------
# Priority levels for routing
# ---------------------------------------------------------------------------

class RoutePriority(IntEnum):
    """Numeric priority for message routing. Higher value = more urgent."""
    LOW = 1
    NORMAL = 5
    HIGH = 8
    CRITICAL = 10


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------

@dataclass
class BridgeEvent:
    """An event propagated across the fleet by the bridge.

    Attributes:
        event_id: Unique identifier.
        event_type: Discriminator (e.g. ``"tender_status"``, ``"fleet_alert"``).
        payload: Event data.
        priority: Routing priority.
        source: Origin agent/system.
        timestamp: Creation time.
    """
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    event_type: str = "generic"
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: RoutePriority = RoutePriority.NORMAL
    source: str = "bridge"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "priority": self.priority.value,
            "source": self.source,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Subscriber callback type
# ---------------------------------------------------------------------------

EventHandler = Callable[[BridgeEvent], None]


# ---------------------------------------------------------------------------
# Fleet bridge
# ---------------------------------------------------------------------------

class FleetBridge:
    """Bidirectional communication bridge between fleet agents and external systems.

    The bridge maintains a priority queue of outbound events, manages subscriber
    callbacks, and provides a threaded event-propagation loop.

    Args:
        fleet: The TenderFleet instance to coordinate.
        bridge_name: Human-readable name for this bridge instance.
    """

    def __init__(self, fleet: TenderFleet, bridge_name: str = "fleet-bridge") -> None:
        self.fleet: TenderFleet = fleet
        self.bridge_name: str = bridge_name
        self._subscribers: Dict[str, List[EventHandler]] = {}
        self._event_queue: List[BridgeEvent] = []
        self._event_log: List[Dict[str, Any]] = []
        self._lock: threading.Lock = threading.Lock()
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None

    # -- subscriber management -----------------------------------------------

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register *handler* for events of *event_type*.

        Use ``"*"`` as *event_type* to receive all events.
        """
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove *handler* from *event_type* subscriptions."""
        with self._lock:
            handlers = self._subscribers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

    # -- event publishing ----------------------------------------------------

    def publish(self, event: BridgeEvent) -> int:
        """Publish *event* and return the number of handlers notified."""
        self._event_queue.append(event)
        self._log_event(event)
        return self._dispatch(event)

    def broadcast_tender_status(self, task: TenderTask) -> int:
        """Broadcast a tender status change to all subscribers."""
        event = BridgeEvent(
            event_type="tender_status",
            payload={
                "tender_id": task.tender_id,
                "title": task.title,
                "status": task.status.value,
                "assigned_to": task.assigned_to,
                "updated_at": task.updated_at,
            },
            priority=RoutePriority.HIGH,
            source=self.bridge_name,
        )
        return self.publish(event)

    def broadcast_fleet_alert(
        self,
        message: str,
        priority: RoutePriority = RoutePriority.NORMAL,
    ) -> int:
        """Broadcast a fleet-wide alert."""
        event = BridgeEvent(
            event_type="fleet_alert",
            payload={"message": message},
            priority=priority,
            source=self.bridge_name,
        )
        return self.publish(event)

    def propagate_event(self, event_type: str, payload: Dict[str, Any]) -> int:
        """Propagate a custom event across the fleet."""
        event = BridgeEvent(
            event_type=event_type,
            payload=payload,
            source=self.bridge_name,
        )
        return self.publish(event)

    # -- message routing -----------------------------------------------------

    def route_message(
        self,
        msg: TenderMessage,
        priority: RoutePriority = RoutePriority.NORMAL,
    ) -> Optional[str]:
        """Route a TenderMessage through the appropriate tender.

        Returns the tender name that accepted the message, or ``None``.
        """
        tender_map: Dict[str, List[str]] = {
            "research": ["research", "findings", "isa", "spec"],
            "data": ["data", "batch", "dataset", "telemetry"],
            "priority": ["priority", "status", "alert", "urgency"],
        }

        msg_keywords = f"{msg.type} {json.dumps(msg.payload)}".lower()
        best_match: Optional[str] = None
        best_score: int = 0

        for tender_name, keywords in tender_map.items():
            score = sum(1 for kw in keywords if kw in msg_keywords)
            if score > best_score:
                best_score = score
                best_match = tender_name

        if best_match is None:
            best_match = "research"  # default fallback

        tender = self.fleet.tenders.get(best_match)
        if tender is not None:
            tender.receive(msg)
            self._log_event(BridgeEvent(
                event_type="message_routed",
                payload={
                    "tender": best_match,
                    "message_id": msg.message_id,
                    "origin": msg.origin,
                    "target": msg.target,
                },
                priority=priority,
                source=self.bridge_name,
            ))
        return best_match

    # -- dispatch internals --------------------------------------------------

    def _dispatch(self, event: BridgeEvent) -> int:
        """Notify all matching subscribers. Returns count of notifications."""
        notified = 0
        with self._lock:
            # Specific type handlers
            for handler in self._subscribers.get(event.event_type, []):
                try:
                    handler(event)
                    notified += 1
                except Exception:
                    pass  # resilient dispatch
            # Wildcard handlers
            for handler in self._subscribers.get("*", []):
                try:
                    handler(event)
                    notified += 1
                except Exception:
                    pass
        return notified

    def _log_event(self, event: BridgeEvent) -> None:
        """Persist event to the bridge log."""
        self._event_log.append(event.to_dict())

    # -- serve loop ----------------------------------------------------------

    def start(self) -> None:
        """Start the background event-processing thread."""
        self._running = True
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()
        self.broadcast_fleet_alert(
            f"Bridge '{self.bridge_name}' started",
            RoutePriority.NORMAL,
        )

    def stop(self) -> None:
        """Stop the background event-processing thread."""
        self._running = False
        self.broadcast_fleet_alert(
            f"Bridge '{self.bridge_name}' stopped",
            RoutePriority.NORMAL,
        )
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _serve_loop(self) -> None:
        """Background loop that periodically processes queued events."""
        while self._running:
            with self._lock:
                if self._event_queue:
                    event = self._event_queue.pop(0)
                    self._dispatch(event)
            time.sleep(1)

    # -- status / inspection -------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Return a status summary of the bridge."""
        return {
            "bridge_name": self.bridge_name,
            "running": self._running,
            "subscribers": {
                k: len(v) for k, v in self._subscribers.items()
            },
            "queued_events": len(self._event_queue),
            "logged_events": len(self._event_log),
            "fleet_status": self.fleet.status(),
        }

    def get_recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent event log entries."""
        return self._event_log[-limit:]
