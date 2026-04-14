#!/usr/bin/env python3
"""Fleet Liaison Tender — manage research tasks, data distribution, and priority routing.

This module provides the core tender lifecycle: create → assign → execute → review → archive.
All tender types (Research, Data, Priority) inherit from LiaisonTender and integrate with
the fleet-wide TenderFleet manager.

Dependencies: stdlib only (json, time, enum, dataclasses, datetime).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Tender status lifecycle
# ---------------------------------------------------------------------------

class TenderStatus(str, Enum):
    """Full tender lifecycle states."""
    CREATED = "created"
    ASSIGNED = "assigned"
    EXECUTING = "executing"
    REVIEW = "review"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    FAILED = "failed"


class TenderType(str, Enum):
    """Supported tender type identifiers."""
    RESEARCH = "research"
    DATA = "data"
    PRIORITY = "priority"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TenderMessage:
    """A single message carried between cloud and edge vessels.

    Attributes:
        origin: Where the message originated (e.g. ``"cloud"`` or ``"edge"``).
        target: Intended recipient vessel name.
        type: Semantic message type (research, data, context, priority).
        payload: Arbitrary dict payload.
        compressed: Whether the payload has been compressed for transit.
        timestamp: Unix timestamp of creation.
        message_id: Unique identifier for this message.
    """
    origin: str
    target: str
    type: str
    payload: dict
    compressed: bool = False
    timestamp: float = field(default_factory=time.time)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class TenderTask:
    """A trackable task within the tender lifecycle.

    Attributes:
        tender_id: Unique task identifier.
        title: Human-readable summary.
        tender_type: Which tender type owns this task.
        priority: Numeric priority (higher = more urgent).
        status: Current lifecycle state.
        assigned_to: Agent or vessel handling the task.
        payload: Full task payload.
        created_at: Timestamp of creation.
        updated_at: Timestamp of last mutation.
        history: List of status-change records.
    """
    tender_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    title: str = "Untitled Task"
    tender_type: TenderType = TenderType.RESEARCH
    priority: int = 5
    status: TenderStatus = TenderStatus.CREATED
    assigned_to: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    history: List[Dict[str, str]] = field(default_factory=list)

    # -- lifecycle transitions ------------------------------------------------

    def transition(self, new_status: TenderStatus, note: str = "") -> None:
        """Move the task to *new_status* and record the change in history."""
        self.history.append({
            "from": self.status.value,
            "to": new_status.value,
            "note": note,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def assign(self, agent: str) -> None:
        """Assign the task to *agent* and transition to ASSIGNED."""
        self.assigned_to = agent
        self.transition(TenderStatus.ASSIGNED, f"Assigned to {agent}")

    def start_execution(self) -> None:
        """Transition to EXECUTING."""
        self.transition(TenderStatus.EXECUTING)

    def mark_review(self) -> None:
        """Transition to REVIEW."""
        self.transition(TenderStatus.REVIEW)

    def complete(self) -> None:
        """Transition to COMPLETED."""
        self.transition(TenderStatus.COMPLETED, "Task completed successfully")

    def fail(self, reason: str = "") -> None:
        """Transition to FAILED with an optional *reason*."""
        self.transition(TenderStatus.FAILED, reason or "Task failed")

    def archive(self) -> None:
        """Transition to ARCHIVED."""
        self.transition(TenderStatus.ARCHIVED)

    # -- serialisation --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe dictionary representation."""
        return {
            "tender_id": self.tender_id,
            "title": self.title,
            "tender_type": self.tender_type.value,
            "priority": self.priority,
            "status": self.status.value,
            "assigned_to": self.assigned_to,
            "payload": self.payload,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TenderTask:
        """Reconstruct a TenderTask from a dictionary."""
        data = dict(data)  # shallow copy
        data["tender_type"] = TenderType(data["tender_type"])
        data["status"] = TenderStatus(data["status"])
        return cls(**data)


# ---------------------------------------------------------------------------
# Base tender
# ---------------------------------------------------------------------------

class LiaisonTender:
    """Base class for fleet liaison tenders.

    Subclasses implement :meth:`process` to transform inbound messages
    into outbound messages.
    """

    def __init__(self, name: str, tender_type: str) -> None:
        self.name: str = name
        self.tender_type: str = tender_type
        self.queue_in: List[TenderMessage] = []
        self.queue_out: List[TenderMessage] = []
        self.tasks: Dict[str, TenderTask] = {}
        self.filters: Dict[str, list] = {}

    # -- message interface ----------------------------------------------------

    def receive(self, msg: TenderMessage) -> None:
        """Receive a message and queue for processing."""
        self.queue_in.append(msg)

    def process(self) -> List[TenderMessage]:
        """Process inbound queue and produce outgoing messages."""
        raise NotImplementedError

    def send(self, msg: TenderMessage) -> None:
        """Queue a message for delivery."""
        self.queue_out.append(msg)

    def status(self) -> Dict[str, Any]:
        """Return a summary dict of the tender's current state."""
        return {
            "name": self.name,
            "type": self.tender_type,
            "inbox": len(self.queue_in),
            "outbox": len(self.queue_out),
            "tasks": len(self.tasks),
        }

    # -- task lifecycle -------------------------------------------------------

    def create_task(
        self,
        title: str,
        priority: int = 5,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TenderTask:
        """Create a new TenderTask, store it, and return it."""
        task = TenderTask(
            title=title,
            tender_type=TenderType(self.tender_type),
            priority=priority,
            payload=payload or {},
        )
        self.tasks[task.tender_id] = task
        return task

    def get_task(self, tender_id: str) -> Optional[TenderTask]:
        """Retrieve a task by its ID, or ``None`` if not found."""
        return self.tasks.get(tender_id)

    def list_tasks(
        self, status_filter: Optional[TenderStatus] = None,
    ) -> List[TenderTask]:
        """Return tasks, optionally filtered by *status_filter*."""
        tasks = list(self.tasks.values())
        if status_filter is not None:
            tasks = [t for t in tasks if t.status == status_filter]
        return sorted(tasks, key=lambda t: t.priority, reverse=True)


# ---------------------------------------------------------------------------
# Concrete tender types
# ---------------------------------------------------------------------------

class ResearchTender(LiaisonTender):
    """Carries findings between cloud and edge labs.

    Cloud specs are compressed into actionable edge items; edge findings
    are formatted for cloud consumption.
    """

    def __init__(self) -> None:
        super().__init__("research-tender", "research")

    def process(self) -> List[TenderMessage]:
        results: List[TenderMessage] = []
        while self.queue_in:
            msg = self.queue_in.pop(0)
            if msg.origin == "cloud":
                results.append(TenderMessage(
                    origin="cloud",
                    target="jetsonclaw1",
                    type="research",
                    payload=self._compress_spec(msg.payload),
                    compressed=True,
                ))
            elif msg.origin == "edge":
                results.append(TenderMessage(
                    origin="edge",
                    target="oracle1",
                    type="research",
                    payload=self._format_findings(msg.payload),
                ))
        self.queue_out.extend(results)
        return results

    @staticmethod
    def _compress_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
        """Compress cloud spec for edge consumption."""
        return {
            "action": spec.get("title", "untitled"),
            "changes": spec.get("changes_affecting_edge", []),
            "ignore": spec.get("changes_not_affecting_edge", []),
            "isa_changes": spec.get("isa_modifications", []),
            "deadline": spec.get("deadline"),
        }

    @staticmethod
    def _format_findings(findings: Dict[str, Any]) -> Dict[str, Any]:
        """Format edge findings for cloud."""
        return {
            "source": "jetsonclaw1",
            "benchmarks": findings.get("benchmarks", {}),
            "failure_modes": findings.get("failures", []),
            "timing_data": findings.get("timing", {}),
            "recommendations": findings.get("recommendations", []),
            "reality_check": findings.get("cloud_assumption_vs_reality", {}),
        }


class DataTender(LiaisonTender):
    """Batches and packages big data for edge consumption.

    Messages are buffered until *batch_size* is reached, then packaged
    into a single compressed batch message.
    """

    def __init__(self, batch_size: int = 50) -> None:
        super().__init__("data-tender", "data")
        self.batch_size: int = batch_size
        self.buffer: List[Dict[str, Any]] = []

    def process(self) -> List[TenderMessage]:
        results: List[TenderMessage] = []
        while self.queue_in:
            msg = self.queue_in.pop(0)
            if msg.origin == "cloud" and msg.target == "edge":
                self.buffer.append(msg.payload)
                if len(self.buffer) >= self.batch_size:
                    results.append(TenderMessage(
                        origin="cloud",
                        target="jetsonclaw1",
                        type="data",
                        payload=self._package_batch(self.buffer),
                        compressed=True,
                    ))
                    self.buffer = []
        self.queue_out.extend(results)
        return results

    @staticmethod
    def _package_batch(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Package a list of payloads into a single batch."""
        return {
            "batch_size": len(items),
            "items": items,
            "edge_relevant_only": True,
            "total_cloud_events": sum(i.get("total_events", 1) for i in items),
        }


class PriorityTender(LiaisonTender):
    """Translates urgency between cloud and edge realities.

    Cloud priorities (low/medium/high/critical) are mapped to edge
    dispositions (ignore/queue/handle_soon/immediate).  Edge statuses
    are mapped back to cloud alert levels.
    """

    def __init__(self) -> None:
        super().__init__("priority-tender", "priority")
        self.priority_map_cloud_to_edge: Dict[str, str] = {
            "low": "ignore",
            "medium": "queue",
            "high": "handle_soon",
            "critical": "immediate",
        }
        self.priority_map_edge_to_cloud: Dict[str, str] = {
            "nominal": "info",
            "degraded": "warning",
            "failing": "high",
            "down": "critical",
        }

    def process(self) -> List[TenderMessage]:
        results: List[TenderMessage] = []
        while self.queue_in:
            msg = self.queue_in.pop(0)
            if msg.origin == "cloud":
                cloud_priority = msg.payload.get("priority", "low")
                edge_priority = self.priority_map_cloud_to_edge.get(
                    cloud_priority, "queue",
                )
                if edge_priority != "ignore":
                    results.append(TenderMessage(
                        origin="cloud",
                        target="jetsonclaw1",
                        type="priority",
                        payload={
                            "original": cloud_priority,
                            "translated": edge_priority,
                            "task": msg.payload.get("task"),
                            "reason": msg.payload.get("reason"),
                        },
                    ))
            elif msg.origin == "edge":
                edge_status = msg.payload.get("status", "nominal")
                cloud_alert = self.priority_map_edge_to_cloud.get(edge_status, "info")
                results.append(TenderMessage(
                    origin="edge",
                    target="oracle1",
                    type="priority",
                    payload={
                        "original": edge_status,
                        "translated": cloud_alert,
                        "sensor_data": msg.payload.get("sensors"),
                    },
                ))
        self.queue_out.extend(results)
        return results


# ---------------------------------------------------------------------------
# Fleet manager
# ---------------------------------------------------------------------------

class TenderFleet:
    """Manages all liaison tenders and coordinates processing cycles.

    Attributes:
        tenders: Mapping of tender type name to tender instance.
        event_log: Fleet-wide event log entries.
    """

    def __init__(self) -> None:
        self.tenders: Dict[str, LiaisonTender] = {
            "research": ResearchTender(),
            "data": DataTender(),
            "priority": PriorityTender(),
        }
        self.event_log: List[Dict[str, Any]] = []

    def run_cycle(self) -> Dict[str, int]:
        """Process all tender queues and return per-tender counts."""
        results: Dict[str, int] = {}
        for name, tender in self.tenders.items():
            processed = tender.process()
            results[name] = len(processed)
            self._log(f"Processed {len(processed)} messages in '{name}' tender")
        return results

    def status(self) -> Dict[str, Any]:
        """Return a fleet-wide status summary."""
        return {name: tender.status() for name, tender in self.tenders.items()}

    def fleet_summary(self) -> Dict[str, Any]:
        """Return a comprehensive summary suitable for fleet-status CLI."""
        total_tasks = 0
        status_counts: Dict[str, int] = {}
        for tender in self.tenders.values():
            for task in tender.tasks.values():
                total_tasks += 1
                key = task.status.value
                status_counts[key] = status_counts.get(key, 0) + 1
        return {
            "tenders": self.status(),
            "total_tasks": total_tasks,
            "tasks_by_status": status_counts,
            "events": len(self.event_log),
        }

    def create_tender(
        self, tender_type: str, title: str, priority: int = 5,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[TenderTask]:
        """Create a task in the appropriate tender by type string."""
        tender = self.tenders.get(tender_type)
        if tender is None:
            return None
        return tender.create_task(title, priority, payload)

    def get_tender(self, tender_id: str) -> Optional[TenderTask]:
        """Search all tenders for a task with the given ID."""
        for tender in self.tenders.values():
            task = tender.get_task(tender_id)
            if task is not None:
                return task
        return None

    def list_tenders(
        self, status_filter: Optional[str] = None,
    ) -> List[TenderTask]:
        """List tasks across all tenders, optionally filtered by status."""
        filter_val = TenderStatus(status_filter) if status_filter else None
        tasks: List[TenderTask] = []
        for tender in self.tenders.values():
            tasks.extend(tender.list_tasks(filter_val))
        return sorted(tasks, key=lambda t: t.priority, reverse=True)

    def _log(self, message: str) -> None:
        """Append an event to the fleet log."""
        self.event_log.append({
            "message": message,
            "at": datetime.now(timezone.utc).isoformat(),
        })
