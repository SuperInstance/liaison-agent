#!/usr/bin/env python3
"""Tests for the Liaison Agent — tender lifecycle, bridge, escalation, and CLI.

Run with:  python -m pytest tests/test_liaison_agent.py -v
           (or simply: python tests/test_liaison_agent.py)
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import datetime, timezone


# Ensure the agent package directory is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tender import (
    DataTender,
    LiaisonTender,
    PriorityTender,
    ResearchTender,
    TenderFleet,
    TenderMessage,
    TenderStatus,
    TenderTask,
    TenderType,
)
from fleet_bridge import BridgeEvent, FleetBridge, RoutePriority
from escalation import (
    EscalationLevel,
    EscalationManager,
    EscalationRecord,
    EscalationRule,
    EscalationTrigger,
)


# ═══════════════════════════════════════════════════════════════════════════
# TenderMessage
# ═══════════════════════════════════════════════════════════════════════════

class TestTenderMessage(unittest.TestCase):
    """Tests for TenderMessage dataclass."""

    def test_defaults(self) -> None:
        msg = TenderMessage(origin="cloud", target="edge", type="data", payload={})
        self.assertFalse(msg.compressed)
        self.assertGreater(msg.timestamp, 0)
        self.assertEqual(len(msg.message_id), 12)

    def test_custom_fields(self) -> None:
        msg = TenderMessage(
            origin="edge",
            target="oracle1",
            type="research",
            payload={"key": "val"},
            compressed=True,
        )
        self.assertTrue(msg.compressed)
        self.assertEqual(msg.payload["key"], "val")


# ═══════════════════════════════════════════════════════════════════════════
# TenderTask lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class TestTenderTask(unittest.TestCase):
    """Tests for the TenderTask create → assign → execute → review → complete → archive lifecycle."""

    def test_initial_state(self) -> None:
        task = TenderTask(title="Test")
        self.assertEqual(task.status, TenderStatus.CREATED)
        self.assertIsNone(task.assigned_to)
        self.assertEqual(len(task.history), 0)

    def test_full_lifecycle(self) -> None:
        task = TenderTask(title="Lifecycle test")
        task.assign("agent-1")
        self.assertEqual(task.status, TenderStatus.ASSIGNED)
        self.assertEqual(task.assigned_to, "agent-1")

        task.start_execution()
        self.assertEqual(task.status, TenderStatus.EXECUTING)

        task.mark_review()
        self.assertEqual(task.status, TenderStatus.REVIEW)

        task.complete()
        self.assertEqual(task.status, TenderStatus.COMPLETED)

        task.archive()
        self.assertEqual(task.status, TenderStatus.ARCHIVED)
        self.assertEqual(len(task.history), 5)

    def test_fail_transition(self) -> None:
        task = TenderTask(title="Will fail")
        task.assign("agent-2")
        task.fail("Something went wrong")
        self.assertEqual(task.status, TenderStatus.FAILED)
        self.assertEqual(task.history[-1]["note"], "Something went wrong")

    def test_round_trip_serialisation(self) -> None:
        task = TenderTask(title="Serialise me", priority=8)
        task.assign("agent-x")
        d = task.to_dict()
        restored = TenderTask.from_dict(d)
        self.assertEqual(restored.title, "Serialise me")
        self.assertEqual(restored.priority, 8)
        self.assertEqual(restored.status, TenderStatus.ASSIGNED)
        self.assertEqual(restored.assigned_to, "agent-x")
        self.assertEqual(len(restored.history), 1)


# ═══════════════════════════════════════════════════════════════════════════
# LiaisonTender base
# ═══════════════════════════════════════════════════════════════════════════

class TestLiaisonTender(unittest.TestCase):
    """Tests for the base LiaisonTender class."""

    def test_process_not_implemented(self) -> None:
        base = LiaisonTender("base", "test")
        with self.assertRaises(NotImplementedError):
            base.process()

    def test_create_and_list_tasks(self) -> None:
        base = LiaisonTender("base", "research")
        t1 = base.create_task("Task A", priority=3)
        t2 = base.create_task("Task B", priority=7)
        tasks = base.list_tasks()
        self.assertEqual(len(tasks), 2)
        # Higher priority first
        self.assertEqual(tasks[0].tender_id, t2.tender_id)

    def test_list_tasks_with_filter(self) -> None:
        base = LiaisonTender("base", "data")
        base.create_task("T1")
        base.create_task("T2")
        base.tasks[list(base.tasks.keys())[0]].complete()
        active = base.list_tasks(status_filter=TenderStatus.CREATED)
        self.assertEqual(len(active), 1)

    def test_get_task(self) -> None:
        base = LiaisonTender("base", "priority")
        task = base.create_task("Find me")
        found = base.get_task(task.tender_id)
        self.assertIsNotNone(found)
        self.assertIsNone(base.get_task("nonexistent"))


# ═══════════════════════════════════════════════════════════════════════════
# ResearchTender
# ═══════════════════════════════════════════════════════════════════════════

class TestResearchTender(unittest.TestCase):
    """Tests for ResearchTender message processing."""

    def test_cloud_to_edge(self) -> None:
        rt = ResearchTender()
        rt.receive(TenderMessage(
            origin="cloud", target="edge", type="research",
            payload={
                "title": "ISA Update",
                "changes_affecting_edge": ["opcode 0xFD"],
                "deadline": "2026-04-15",
            },
        ))
        results = rt.process()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].compressed)
        self.assertEqual(results[0].payload["action"], "ISA Update")

    def test_edge_to_cloud(self) -> None:
        rt = ResearchTender()
        rt.receive(TenderMessage(
            origin="edge", target="cloud", type="research",
            payload={
                "benchmarks": {"rooms": "25.5us"},
                "failures": ["COBS drop"],
                "recommendations": ["shorter cable"],
                "cloud_assumption_vs_reality": {
                    "assumption": "45s",
                    "reality": "42s",
                },
            },
        ))
        results = rt.process()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].compressed)
        self.assertIn("reality_check", results[0].payload)


# ═══════════════════════════════════════════════════════════════════════════
# DataTender
# ═══════════════════════════════════════════════════════════════════════════

class TestDataTender(unittest.TestCase):
    """Tests for DataTender batching."""

    def test_buffering(self) -> None:
        dt = DataTender(batch_size=3)
        for i in range(2):
            dt.receive(TenderMessage(
                origin="cloud", target="edge", type="data",
                payload={"idx": i, "total_events": 10},
            ))
        results = dt.process()
        self.assertEqual(len(results), 0)  # not yet at batch_size
        self.assertEqual(len(dt.buffer), 2)

    def test_batch_release(self) -> None:
        dt = DataTender(batch_size=2)
        for i in range(2):
            dt.receive(TenderMessage(
                origin="cloud", target="edge", type="data",
                payload={"idx": i, "total_events": 5},
            ))
        results = dt.process()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].payload["batch_size"], 2)
        self.assertEqual(results[0].payload["total_cloud_events"], 10)
        self.assertEqual(len(dt.buffer), 0)


# ═══════════════════════════════════════════════════════════════════════════
# PriorityTender
# ═══════════════════════════════════════════════════════════════════════════

class TestPriorityTender(unittest.TestCase):
    """Tests for PriorityTender priority translation."""

    def test_cloud_low_ignored(self) -> None:
        pt = PriorityTender()
        pt.receive(TenderMessage(
            origin="cloud", target="edge", type="priority",
            payload={"priority": "low", "task": "cleanup"},
        ))
        results = pt.process()
        self.assertEqual(len(results), 0)

    def test_cloud_critical_immediate(self) -> None:
        pt = PriorityTender()
        pt.receive(TenderMessage(
            origin="cloud", target="edge", type="priority",
            payload={"priority": "critical", "task": "hotfix"},
        ))
        results = pt.process()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].payload["translated"], "immediate")

    def test_edge_degraded_warning(self) -> None:
        pt = PriorityTender()
        pt.receive(TenderMessage(
            origin="edge", target="cloud", type="priority",
            payload={"status": "degraded", "sensors": {"cpu": "72C"}},
        ))
        results = pt.process()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].payload["translated"], "warning")


# ═══════════════════════════════════════════════════════════════════════════
# TenderFleet
# ═══════════════════════════════════════════════════════════════════════════

class TestTenderFleet(unittest.TestCase):
    """Tests for TenderFleet coordination."""

    def test_run_cycle(self) -> None:
        fleet = TenderFleet()
        fleet.tenders["research"].receive(TenderMessage(
            origin="cloud", target="edge", type="research",
            payload={"title": "T"},
        ))
        results = fleet.run_cycle()
        self.assertEqual(results["research"], 1)

    def test_fleet_summary(self) -> None:
        fleet = TenderFleet()
        fleet.create_tender("research", "Task A", priority=5)
        fleet.create_tender("data", "Task B", priority=3)
        summary = fleet.fleet_summary()
        self.assertEqual(summary["total_tasks"], 2)
        self.assertEqual(summary["tasks_by_status"]["created"], 2)

    def test_list_tenders_with_filter(self) -> None:
        fleet = TenderFleet()
        t = fleet.create_tender("priority", "Filter me")
        if t:
            t.complete()
        active = fleet.list_tenders(status_filter="created")
        self.assertEqual(len(active), 0)
        completed = fleet.list_tenders(status_filter="completed")
        self.assertEqual(len(completed), 1)


# ═══════════════════════════════════════════════════════════════════════════
# FleetBridge
# ═══════════════════════════════════════════════════════════════════════════

class TestFleetBridge(unittest.TestCase):
    """Tests for FleetBridge routing and event propagation."""

    def setUp(self) -> None:
        self.fleet = TenderFleet()
        self.bridge = FleetBridge(self.fleet, bridge_name="test-bridge")

    def test_subscribe_and_publish(self) -> None:
        received: list = []
        self.bridge.subscribe("test_event", lambda e: received.append(e))
        event = BridgeEvent(event_type="test_event", payload={"msg": "hello"})
        count = self.bridge.publish(event)
        self.assertEqual(count, 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload["msg"], "hello")

    def test_wildcard_subscriber(self) -> None:
        received: list = []
        self.bridge.subscribe("*", lambda e: received.append(e.event_type))
        self.bridge.publish(BridgeEvent(event_type="alpha"))
        self.bridge.publish(BridgeEvent(event_type="beta"))
        self.assertEqual(received, ["alpha", "beta"])

    def test_route_message(self) -> None:
        msg = TenderMessage(
            origin="cloud", target="edge", type="data",
            payload={"telemetry": True},
        )
        routed_to = self.bridge.route_message(msg)
        self.assertEqual(routed_to, "data")

    def test_broadcast_tender_status(self) -> None:
        received: list = []
        self.bridge.subscribe("tender_status", lambda e: received.append(e))
        task = TenderTask(title="Broadcast test")
        task.assign("agent-1")
        count = self.bridge.broadcast_tender_status(task)
        self.assertEqual(count, 1)
        self.assertEqual(received[0].payload["status"], "assigned")

    def test_fleet_alert(self) -> None:
        received: list = []
        self.bridge.subscribe("fleet_alert", lambda e: received.append(e))
        self.bridge.broadcast_fleet_alert("Test alert")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload["message"], "Test alert")

    def test_bridge_status(self) -> None:
        status = self.bridge.status()
        self.assertEqual(status["bridge_name"], "test-bridge")
        self.assertFalse(status["running"])

    def test_recent_events(self) -> None:
        self.bridge.publish(BridgeEvent(event_type="a"))
        self.bridge.publish(BridgeEvent(event_type="b"))
        events = self.bridge.get_recent_events(limit=1)
        self.assertEqual(len(events), 1)


# ═══════════════════════════════════════════════════════════════════════════
# EscalationManager
# ═══════════════════════════════════════════════════════════════════════════

class TestEscalationManager(unittest.TestCase):
    """Tests for the escalation framework."""

    def setUp(self) -> None:
        self.fleet = TenderFleet()
        self.esc = EscalationManager(self.fleet)

    def test_default_rules_installed(self) -> None:
        self.assertGreater(len(self.esc.rules), 0)

    def test_manual_escalate(self) -> None:
        task = self.fleet.create_tender("research", "Escalate me", priority=5)
        assert task is not None
        task.assign("agent-1")
        record = self.esc.escalate(task.tender_id, "Needs attention")
        self.assertIsNotNone(record)
        self.assertEqual(record.trigger, EscalationTrigger.MANUAL_REQUEST)
        self.assertEqual(len(self.esc.audit_trail), 1)

    def test_manual_escalate_terminal_rejected(self) -> None:
        task = self.fleet.create_tender("data", "Already done")
        assert task is not None
        task.complete()
        record = self.esc.escalate(task.tender_id, "Too late")
        self.assertIsNone(record)

    def test_human_escalate(self) -> None:
        task = self.fleet.create_tender("priority", "Human needed")
        assert task is not None
        task.assign("agent-2")
        record = self.esc.human_escalate(task.tender_id, "Complex issue", context={"env": "prod"})
        self.assertIsNotNone(record)
        self.assertEqual(record.level, EscalationLevel.HUMAN_ESCALATION)
        self.assertIn("git-agent-liaison", record.reason)

    def test_auto_escalation_stuck_task(self) -> None:
        task = self.fleet.create_tender("research", "Stuck task")
        assert task is not None
        task.assign("agent-slow")

        # Simulate task being stuck by moving updated_at far into the past
        past = datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()
        task.updated_at = past

        # Clear default rules and add a single rule with a very short wait
        self.esc.rules.clear()
        short_rule = EscalationRule(
            name="instant-stuck",
            trigger=EscalationTrigger.TASK_STUCK,
            level=EscalationLevel.AUTO_ESCALATE,
            max_wait_seconds=1.0,
            status_filter=[TenderStatus.ASSIGNED],
        )
        self.esc.add_rule(short_rule)
        records = self.esc.check_all()
        self.assertGreater(len(records), 0)
        self.assertEqual(records[0].level, EscalationLevel.AUTO_ESCALATE)

    def test_audit_trail(self) -> None:
        task = self.fleet.create_tender("data", "Audit test")
        assert task is not None
        self.esc.escalate(task.tender_id, "First")
        self.esc.escalate(task.tender_id, "Second")
        trail = self.esc.get_audit_trail(tender_id=task.tender_id)
        self.assertEqual(len(trail), 2)

    def test_escalation_stats(self) -> None:
        task = self.fleet.create_tender("priority", "Stats test")
        assert task is not None
        self.esc.escalate(task.tender_id, "reason")
        stats = self.esc.get_stats()
        self.assertEqual(stats["total_escalations"], 1)
        self.assertIn("manual_request", stats["by_trigger"])


# ═══════════════════════════════════════════════════════════════════════════
# CLI (smoke tests)
# ═══════════════════════════════════════════════════════════════════════════

class TestCLI(unittest.TestCase):
    """Smoke tests for CLI subcommands."""

    def setUp(self) -> None:
        import cli as cli_mod
        import shutil
        self.tmp_dir = f"/tmp/liaison-test-{int(time.time() * 1000)}"
        os.makedirs(self.tmp_dir, exist_ok=True)
        # Save originals
        self._orig = {
            "DATA_DIR": cli_mod.DATA_DIR,
            "STATE_PATH": cli_mod.STATE_PATH,
            "CONFIG_PATH": cli_mod.CONFIG_PATH,
        }
        # Replace module-level constants with temp paths
        cli_mod.DATA_DIR = self.tmp_dir
        cli_mod.STATE_PATH = os.path.join(self.tmp_dir, "state.json")
        cli_mod.CONFIG_PATH = os.path.join(self.tmp_dir, "liaison.yaml")

    def tearDown(self) -> None:
        import cli as cli_mod
        import shutil
        cli_mod.DATA_DIR = self._orig["DATA_DIR"]
        cli_mod.STATE_PATH = self._orig["STATE_PATH"]
        cli_mod.CONFIG_PATH = self._orig["CONFIG_PATH"]
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_onboard(self) -> None:
        from cli import cmd_onboard
        ns = type("NS", (), {})()
        cmd_onboard(ns)
        self.assertTrue(os.path.isdir(self.tmp_dir))
        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "liaison.yaml")))

    def test_status(self) -> None:
        from cli import cmd_status
        ns = type("NS", (), {})()
        cmd_status(ns)  # should not raise

    def test_tender_create_and_list(self) -> None:
        from cli import cmd_tender_create, cmd_tender_list
        ns_create = type("NS", (), {"type": "research", "title": "CLI Test", "priority": 7, "description": ""})()
        cmd_tender_create(ns_create)

        ns_list = type("NS", (), {"status": None})()
        cmd_tender_list(ns_list)  # should not raise

    def test_fleet_status(self) -> None:
        from cli import cmd_fleet_status
        ns = type("NS", (), {})()
        cmd_fleet_status(ns)  # should not raise


if __name__ == "__main__":
    unittest.main()
