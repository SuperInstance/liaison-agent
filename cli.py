#!/usr/bin/env python3
"""CLI Interface — command-line control surface for the Liaison Agent.

Subcommands:
    serve                  Start the liaison service (bridge + event loop)
    tender create          Create a new tender task
    tender list            List tender tasks, optionally filtered by status
    tender assign          Assign a tender to an agent
    tender complete        Mark a tender as completed
    escalate               Manually escalate a tender
    fleet-status           Show fleet-wide tender status summary
    onboard                Initialise agent configuration
    status                 Show current agent status

Dependencies: stdlib only (argparse, json, os, sys, time).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from tender import (
    TenderFleet,
    TenderStatus,
    TenderType,
)
from fleet_bridge import FleetBridge, RoutePriority
from escalation import (
    EscalationLevel,
    EscalationManager,
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(AGENT_DIR, "liaison.yaml")
DATA_DIR = os.path.join(AGENT_DIR, ".liaison")
STATE_PATH = os.path.join(DATA_DIR, "state.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_data_dir() -> None:
    """Create the data directory if it does not exist."""
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_state() -> Dict[str, Any]:
    """Load persisted state from disk."""
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    """Persist state to disk."""
    _ensure_data_dir()
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _build_fleet() -> TenderFleet:
    """Construct a TenderFleet, restoring tasks from disk if available."""
    fleet = TenderFleet()
    state = _load_state()
    for tid, tdata in state.get("tasks", {}).items():
        tender_type = tdata.get("tender_type", "research")
        tender = fleet.tenders.get(tender_type)
        if tender is not None:
            from tender import TenderTask
            task = TenderTask.from_dict(tdata)
            tender.tasks[task.tender_id] = task
    return fleet


def _persist_fleet(fleet: TenderFleet) -> None:
    """Persist all fleet tasks to disk."""
    state = _load_state()
    tasks: Dict[str, Any] = {}
    for tender in fleet.tenders.values():
        for task in tender.tasks.values():
            tasks[task.tender_id] = task.to_dict()
    state["tasks"] = tasks
    _save_state(state)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> None:
    """Start the liaison service with the fleet bridge."""
    fleet = _build_fleet()
    bridge = FleetBridge(fleet, bridge_name=args.name)
    esc = EscalationManager(fleet)

    # Simple subscriber that logs to stdout
    def _on_event(event):
        print(f"[bridge] {event.event_type}: {json.dumps(event.payload, default=str)}")

    bridge.subscribe("*", _on_event)

    bridge.start()
    print(f"Liaison agent '{args.name}' running. Press Ctrl+C to stop.")
    print(f"Bridge status: {json.dumps(bridge.status(), indent=2)}")

    try:
        while True:
            time.sleep(5)
            records = esc.check_all()
            for rec in records:
                print(
                    f"[escalation] {rec.tender_id} → {rec.level.name}: {rec.reason}"
                )
            _persist_fleet(fleet)
    except KeyboardInterrupt:
        bridge.stop()
        _persist_fleet(fleet)
        print("\nLiaison agent stopped. State saved.")


def cmd_tender_create(args: argparse.Namespace) -> None:
    """Create a new tender task."""
    fleet = _build_fleet()
    priority = getattr(args, "priority", 5)
    task = fleet.create_tender(
        tender_type=args.type,
        title=args.title,
        priority=priority,
        payload={"description": args.description or ""},
    )
    if task is None:
        print(f"Error: unknown tender type '{args.type}'")
        print(f"Valid types: {[t.value for t in TenderType]}")
        sys.exit(1)
    _persist_fleet(fleet)
    print(f"Created tender {task.tender_id} ({args.type}, priority={priority})")
    print(f"  Title: {args.title}")


def cmd_tender_list(args: argparse.Namespace) -> None:
    """List tender tasks."""
    fleet = _build_fleet()
    status_filter = getattr(args, "status", None)
    tasks = fleet.list_tenders(status_filter=status_filter)

    if not tasks:
        print("No tenders found.")
        return

    for task in tasks:
        assigned = task.assigned_to or "unassigned"
        print(
            f"  {task.tender_id}  [{task.status.value:<10}]  "
            f"p={task.priority}  {task.tender_type.value:<8}  "
            f"→ {assigned}  {task.title}"
        )
    print(f"\nTotal: {len(tasks)}")


def cmd_tender_assign(args: argparse.Namespace) -> None:
    """Assign a tender to an agent."""
    fleet = _build_fleet()
    task = fleet.get_tender(args.id)
    if task is None:
        print(f"Error: tender '{args.id}' not found")
        sys.exit(1)
    task.assign(args.to)
    _persist_fleet(fleet)
    print(f"Tender {args.id} assigned to {args.to}")


def cmd_tender_complete(args: argparse.Namespace) -> None:
    """Mark a tender as completed."""
    fleet = _build_fleet()
    task = fleet.get_tender(args.id)
    if task is None:
        print(f"Error: tender '{args.id}' not found")
        sys.exit(1)
    task.complete()
    _persist_fleet(fleet)
    print(f"Tender {args.id} marked as completed.")


def cmd_escalate(args: argparse.Namespace) -> None:
    """Manually escalate a tender."""
    fleet = _build_fleet()
    esc = EscalationManager(fleet)
    level = EscalationLevel.HUMAN_ESCALATION if args.human else EscalationLevel.AUTO_ESCALATE

    if args.human:
        record = esc.human_escalate(args.id, args.reason)
    else:
        record = esc.escalate(args.id, args.reason, level=level)

    if record is None:
        print(f"Error: tender '{args.id}' not found or in terminal state")
        sys.exit(1)

    _persist_fleet(fleet)
    print(f"Escalated {args.id} → {record.level.name}")
    print(f"  Reason: {record.reason}")


def cmd_fleet_status(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Show fleet-wide tender status."""
    fleet = _build_fleet()
    summary = fleet.fleet_summary()
    print("Fleet Tender Status")
    print("=" * 50)

    for name, info in summary["tenders"].items():
        print(f"  {name}: inbox={info['inbox']}, outbox={info['outbox']}, tasks={info['tasks']}")

    print(f"\nTotal tasks: {summary['total_tasks']}")
    if summary["tasks_by_status"]:
        print("By status:")
        for status, count in sorted(summary["tasks_by_status"].items()):
            print(f"  {status}: {count}")
    print(f"Fleet events logged: {summary['events']}")


def cmd_onboard(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Set up the agent configuration."""
    _ensure_data_dir()
    config = {
        "agent_name": "liaison-agent",
        "version": "1.0.0",
        "bridge": {
            "name": "fleet-bridge",
            "port": 0,
        },
        "escalation": {
            "liaison_channel": "git-agent-liaison",
            "check_interval_seconds": 30,
        },
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        fh.write("# Liaison Agent Configuration\n")
        fh.write("# Auto-generated by `liaison-agent onboard`\n\n")
        json.dump(config, fh, indent=2)
    print(f"Configuration written to {CONFIG_PATH}")
    print(f"Data directory: {DATA_DIR}")
    print("Onboard complete. Run `liaison-agent serve` to start.")


def cmd_status(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Show current agent status."""
    fleet = _build_fleet()
    state = _load_state()
    config_path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else "not found"

    print("Liaison Agent Status")
    print("=" * 50)
    print(f"  Config:     {config_path}")
    print(f"  Data dir:   {DATA_DIR}")
    print(f"  State file: {STATE_PATH} ({'exists' if os.path.exists(STATE_PATH) else 'empty'})")
    print(f"  Fleet events: {len(fleet.event_log)}")

    summary = fleet.fleet_summary()
    print(f"  Total tasks: {summary['total_tasks']}")
    if summary["tasks_by_status"]:
        parts = [f"{s}={c}" for s, c in sorted(summary["tasks_by_status"].items())]
        print(f"  Status breakdown: {', '.join(parts)}")

    print("\nTender types:")
    for name in fleet.tenders:
        print(f"  - {name}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="liaison-agent",
        description="Fleet Liaison Agent — fleet tender management, communication bridge, escalation framework",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    p_serve = sub.add_parser("serve", help="Start the liaison service")
    p_serve.add_argument("--name", default="fleet-bridge", help="Bridge instance name")
    p_serve.set_defaults(func=cmd_serve)

    # tender create
    p_tc = sub.add_parser("tender", help="Tender management commands")
    t_sub = p_tc.add_subparsers(dest="tender_command")

    p_create = t_sub.add_parser("create", help="Create a new tender")
    p_create.add_argument("--type", required=True, help="Tender type (research|data|priority)")
    p_create.add_argument("--title", required=True, help="Task title")
    p_create.add_argument("--priority", type=int, default=5, help="Priority (1-10, default 5)")
    p_create.add_argument("--description", default="", help="Optional description")
    p_create.set_defaults(func=cmd_tender_create)

    p_list = t_sub.add_parser("list", help="List tenders")
    p_list.add_argument("--status", default=None, help="Filter by status")
    p_list.set_defaults(func=cmd_tender_list)

    p_assign = t_sub.add_parser("assign", help="Assign a tender to an agent")
    p_assign.add_argument("id", help="Tender ID")
    p_assign.add_argument("--to", required=True, dest="to", help="Agent name")
    p_assign.set_defaults(func=cmd_tender_assign)

    p_complete = t_sub.add_parser("complete", help="Mark tender complete")
    p_complete.add_argument("id", help="Tender ID")
    p_complete.set_defaults(func=cmd_tender_complete)

    # escalate
    p_esc = sub.add_parser("escalate", help="Escalate a tender")
    p_esc.add_argument("id", help="Tender ID")
    p_esc.add_argument("--reason", required=True, help="Escalation reason")
    p_esc.add_argument("--human", action="store_true", help="Escalate to human (git-agent liaison)")
    p_esc.set_defaults(func=cmd_escalate)

    # fleet-status
    p_fs = sub.add_parser("fleet-status", help="Show fleet-wide tender status")
    p_fs.set_defaults(func=cmd_fleet_status)

    # onboard
    p_on = sub.add_parser("onboard", help="Set up the agent")
    p_on.set_defaults(func=cmd_onboard)

    # status
    p_st = sub.add_parser("status", help="Show agent status")
    p_st.set_defaults(func=cmd_status)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    """Parse arguments and dispatch to the appropriate handler."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Handle nested subcommands (tender create / tender list / etc.)
    if args.command == "tender" and not hasattr(args, "func"):
        parser.parse_args(["tender", "--help"])
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
