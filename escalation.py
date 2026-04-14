#!/usr/bin/env python3
"""Escalation Framework — automatic and manual escalation for stuck or critical tenders.

Provides:
- Configurable escalation rules and triggers
- Automatic escalation when tasks remain in a non-terminal state too long
- Human escalation path via git-agent liaison
- Full audit trail of all escalation events

Dependencies: stdlib only (json, time, enum, dataclasses, datetime).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from tender import TenderStatus, TenderTask, TenderFleet


# ---------------------------------------------------------------------------
# Escalation levels
# ---------------------------------------------------------------------------

class EscalationLevel(IntEnum):
    """Escalation severity. Higher values indicate more severe escalation."""
    NONE = 0
    AUTO_REMINDER = 1
    AUTO_ESCALATE = 2
    SUPERVISOR_ALERT = 3
    HUMAN_ESCALATION = 4
    CRITICAL_OVERRIDE = 5


class EscalationTrigger(str, Enum):
    """Named triggers that can fire an escalation."""
    TASK_STUCK = "task_stuck"
    DEADLINE_APPROACHING = "deadline_approaching"
    DEADLINE_MISSED = "deadline_missed"
    PRIORITY_BOOST = "priority_boost"
    MANUAL_REQUEST = "manual_request"
    REPEATED_FAILURE = "repeated_failure"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EscalationRule:
    """A rule that defines when and how to escalate a tender.

    Attributes:
        rule_id: Unique identifier.
        name: Human-readable rule name.
        trigger: Which event type fires this rule.
        level: Escalation severity when the rule fires.
        max_wait_seconds: If a task stays in a non-terminal state longer than
            this, the rule fires (only for ``TASK_STUCK`` trigger).
        status_filter: Only apply to tasks in these statuses.
        note_template: Template string for the escalation note.
    """
    rule_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = "default_rule"
    trigger: EscalationTrigger = EscalationTrigger.TASK_STUCK
    level: EscalationLevel = EscalationLevel.AUTO_REMINDER
    max_wait_seconds: float = 3600.0  # 1 hour default
    status_filter: List[TenderStatus] = field(
        default_factory=lambda: [TenderStatus.ASSIGNED, TenderStatus.EXECUTING]
    )
    note_template: str = (
        "Auto-escalated: task in {status} for {elapsed}s (limit {limit}s)"
    )


@dataclass
class EscalationRecord:
    """Immutable record of a single escalation event.

    Attributes:
        record_id: Unique identifier.
        tender_id: The task that was escalated.
        rule_id: Which rule fired (empty for manual escalations).
        trigger: What triggered the escalation.
        level: Severity at the time of escalation.
        reason: Free-text explanation.
        timestamp: When the escalation occurred.
    """
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    tender_id: str = ""
    rule_id: str = ""
    trigger: EscalationTrigger = EscalationTrigger.MANUAL_REQUEST
    level: EscalationLevel = EscalationLevel.AUTO_REMINDER
    reason: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dictionary representation."""
        return {
            "record_id": self.record_id,
            "tender_id": self.tender_id,
            "rule_id": self.rule_id,
            "trigger": self.trigger.value,
            "level": self.level.value,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Escalation manager
# ---------------------------------------------------------------------------

class EscalationManager:
    """Manages escalation rules, automatic checks, and the audit trail.

    Args:
        fleet: The TenderFleet to monitor.
        liaison_channel: Name of the human escalation channel (default
            ``"git-agent-liaison"``).
    """

    # Statuses considered terminal — no escalation needed
    TERMINAL_STATUSES = {
        TenderStatus.COMPLETED,
        TenderStatus.ARCHIVED,
        TenderStatus.FAILED,
    }

    def __init__(
        self,
        fleet: TenderFleet,
        liaison_channel: str = "git-agent-liaison",
    ) -> None:
        self.fleet: TenderFleet = fleet
        self.liaison_channel: str = liaison_channel
        self.rules: Dict[str, EscalationRule] = {}
        self.audit_trail: List[EscalationRecord] = []

        # Install default rules
        self._install_defaults()

    # -- rule management -----------------------------------------------------

    def add_rule(self, rule: EscalationRule) -> None:
        """Register an escalation rule."""
        self.rules[rule.rule_id] = rule

    def remove_rule(self, rule_id: str) -> bool:
        """Remove a rule by ID. Returns ``True`` if found and removed."""
        return self.rules.pop(rule_id, None) is not None

    def list_rules(self) -> List[Dict[str, Any]]:
        """Return serialised list of all rules."""
        return [
            {
                "rule_id": r.rule_id,
                "name": r.name,
                "trigger": r.trigger.value,
                "level": r.level.value,
                "max_wait_seconds": r.max_wait_seconds,
                "status_filter": [s.value for s in r.status_filter],
            }
            for r in self.rules.values()
        ]

    # -- automatic escalation ------------------------------------------------

    def check_all(self) -> List[EscalationRecord]:
        """Evaluate all rules against every active task in the fleet.

        Returns newly-created escalation records.
        """
        now = time.time()
        records: List[EscalationRecord] = []
        tasks = self.fleet.list_tenders()

        for task in tasks:
            if task.status in self.TERMINAL_STATUSES:
                continue
            for rule in self.rules.values():
                record = self._evaluate(rule, task, now)
                if record is not None:
                    self._record(record)
                    records.append(record)

        return records

    def _evaluate(
        self,
        rule: EscalationRule,
        task: TenderTask,
        now: float,
    ) -> Optional[EscalationRecord]:
        """Evaluate a single rule against a single task."""
        if task.status not in rule.status_filter:
            return None

        if rule.trigger == EscalationTrigger.TASK_STUCK:
            updated = datetime.fromisoformat(task.updated_at).timestamp()
            elapsed = now - updated
            if elapsed >= rule.max_wait_seconds:
                reason = rule.note_template.format(
                    status=task.status.value,
                    elapsed=int(elapsed),
                    limit=int(rule.max_wait_seconds),
                )
                return EscalationRecord(
                    tender_id=task.tender_id,
                    rule_id=rule.rule_id,
                    trigger=rule.trigger,
                    level=rule.level,
                    reason=reason,
                )

        return None

    # -- manual escalation ---------------------------------------------------

    def escalate(
        self,
        tender_id: str,
        reason: str,
        level: EscalationLevel = EscalationLevel.AUTO_ESCALATE,
    ) -> Optional[EscalationRecord]:
        """Manually escalate a tender with a given reason.

        If the tender is found and is not in a terminal state, an
        :class:`EscalationRecord` is created and returned.
        """
        task = self.fleet.get_tender(tender_id)
        if task is None:
            return None
        if task.status in self.TERMINAL_STATUSES:
            return None

        record = EscalationRecord(
            tender_id=tender_id,
            trigger=EscalationTrigger.MANUAL_REQUEST,
            level=level,
            reason=reason,
        )
        self._record(record)

        # Bump task priority for high-severity escalations
        if level.value >= EscalationLevel.SUPERVISOR_ALERT.value:
            task.priority = min(task.priority + 3, 10)

        return record

    def human_escalate(
        self,
        tender_id: str,
        reason: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[EscalationRecord]:
        """Escalate to a human via the git-agent liaison channel.

        Returns an :class:`EscalationRecord` if the tender was found.
        """
        record = self.escalate(
            tender_id, reason, level=EscalationLevel.HUMAN_ESCALATION,
        )
        if record is not None:
            liaison_note = (
                f"[{self.liaison_channel}] Human escalation: {reason}\n"
                f"Tender: {tender_id}"
            )
            if context:
                liaison_note += f"\nContext: {context}"
            record.reason = liaison_note
        return record

    # -- audit trail ---------------------------------------------------------

    def _record(self, record: EscalationRecord) -> None:
        """Append an escalation record to the audit trail."""
        self.audit_trail.append(record)

    def get_audit_trail(
        self, tender_id: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return the audit trail, optionally filtered by *tender_id*."""
        trail = self.audit_trail
        if tender_id is not None:
            trail = [r for r in trail if r.tender_id == tender_id]
        return [r.to_dict() for r in trail[-limit:]]

    def get_stats(self) -> Dict[str, Any]:
        """Return escalation statistics."""
        by_level: Dict[str, int] = {}
        by_trigger: Dict[str, int] = {}
        for rec in self.audit_trail:
            lk = rec.level.name
            by_level[lk] = by_level.get(lk, 0) + 1
            tk = rec.trigger.value
            by_trigger[tk] = by_trigger.get(tk, 0) + 1
        return {
            "total_escalations": len(self.audit_trail),
            "active_rules": len(self.rules),
            "by_level": by_level,
            "by_trigger": by_trigger,
        }

    # -- defaults ------------------------------------------------------------

    def _install_defaults(self) -> None:
        """Pre-configure sensible default escalation rules."""
        self.add_rule(EscalationRule(
            name="stuck-assigned",
            trigger=EscalationTrigger.TASK_STUCK,
            level=EscalationLevel.AUTO_REMINDER,
            max_wait_seconds=1800,  # 30 min
            status_filter=[TenderStatus.ASSIGNED],
            note_template="Task assigned but not started for {elapsed}s",
        ))
        self.add_rule(EscalationRule(
            name="stuck-executing",
            trigger=EscalationTrigger.TASK_STUCK,
            level=EscalationLevel.AUTO_ESCALATE,
            max_wait_seconds=7200,  # 2 hours
            status_filter=[TenderStatus.EXECUTING],
            note_template="Task executing for {elapsed}s without completion",
        ))
        self.add_rule(EscalationRule(
            name="stuck-review",
            trigger=EscalationTrigger.TASK_STUCK,
            level=EscalationLevel.SUPERVISOR_ALERT,
            max_wait_seconds=3600,  # 1 hour
            status_filter=[TenderStatus.REVIEW],
            note_template="Task in review for {elapsed}s — needs attention",
        ))
