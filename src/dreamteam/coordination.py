"""Local commands available to the interactive Coach and its named players.

Codex owns the conversations. This module owns bounded task delivery and explicit
progress reports; terminal activity alone never marks work complete.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import asdict, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

from .plays import PlayStore
from .roster import Player, load_roster
from .runtime import BackendError, Herdr


def command_prefix(config: Path, project: Path) -> list[str]:
    return [sys.executable, "-m", "dreamteam.coordination", "--config",
            str(config.resolve()), "--project", str(project.resolve())]


def _tool_args(config: Path, project: Path, actor: str) -> tuple[str, ...]:
    # A compact inline table stays within a fresh macOS PTY's canonical line
    # buffer. All paths are absolute; the MCP child needs no cwd override.
    settings = {
        "command": sys.executable,
        "args": ["-m", "dreamteam.coach_tools", "--config", str(config.resolve()),
                 "--project", str(project.resolve()), "--actor", actor],
        "required": True,
    }
    table = "{" + ",".join(key + "=" + json.dumps(value, ensure_ascii=False)
                           for key, value in settings.items()) + "}"
    return ("-c", "mcp_servers.dreamteam=" + table)


def coordinated_player(player: Player, config: Path, project: Path) -> Player:
    """Give future player sessions their own bounded progress-report tool."""
    return replace(player, args=player.args + _tool_args(config, project, player.id))


COACH_INSTRUCTIONS = """You are the Dreamteam Coach console. The human directs the team and accepts results.
Delegate requested work to the visible roster through Dreamteam tools; do not implement it
yourself, use hidden subagents, or merely suggest commands for the user to copy.
At EVERY user request, call dreamteam.team_status for the saved play, roles, activity and reports.
Use read_player_output for actual evidence. Honor project instructions, play constraints and
the latest user scope. For Fastbreak, no broker connection, orders or strategy work without
explicit authorization. Use normal Codex approvals; never weaken sandbox settings or edit
runtime/assignment JSON or global configuration.

Use dispatch_task with a bounded task, expected result and unique request_id. Reuse the SAME ID
to inspect a repeated/uncertain request; never blindly resend under a new ID. Avoid overlapping
file edits or assigning a busy/unresolved player. Tell the user who received work and that
they can select that sidebar entry to follow it. Submitted means delivery, not completion.
Use wait_for_report for a bounded wait, then status/output; avoid endless polling. Idle and
timeouts never prove success. Reported done needs review and human acceptance.

New players have report_progress. For legacy sessions without it, read their explicit visible
result before record_player_report; this retains coach_observed provenance. Never invent a
report. If the user abandoned stopped work, close_stopped_task retires it as cancelled, not done.
record_play creates a bounded play only if none exists. Ask only for required missing scope.
Awareness comes from snapshots/reports, not continuous background monitoring. If tools are
unavailable, explain the blocker rather than claiming dispatch or bypassing it with shell.
"""

def coach_player(config: Path, project: Path) -> Player:
    """A separate coordinating conversation, outside the six-player roster."""
    instructions = ("You are Dreamteam Coach. On every request first call dreamteam.team_status "
                    "and follow its coach_instructions. Delegate through its visible roster tools.")
    return Player("coach", "Coach", "coach", project.resolve(),
                  ("-c", "developer_instructions=" + json.dumps(instructions, ensure_ascii=False))
                  + _tool_args(config, project, "coach"),
                  "Turn your instructions into assignments and follow the team's progress.")


def _text(value: str, label: str, maximum: int = 16000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1–{maximum} characters.")
    if any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) < 160
           or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f"{label} contains terminal control characters.")
    return value.strip()


class Team:
    def __init__(self, config: Path, project: Path, *,
                 cancelled: Callable[[], bool] | None = None,
                 coordinator_token: str | None = None):
        self.cancelled = cancelled
        self.coordinator_token = coordinator_token
        self.config = config.expanduser().resolve()
        self.project = project.expanduser().resolve()
        self.play_store = PlayStore(self.config.parent)
        self.play = self.play_store.load()
        expected = self.play.project if self.play is not None else self.config.parent
        if self.project != expected.resolve() or not self.project.is_dir():
            raise ValueError("The Coach project no longer matches the saved play. Reopen Courtside.")
        roster = load_roster(self.config)
        routed = tuple(replace(p, cwd=self.project) for p in roster) if self.play else roster
        self.players = tuple(coordinated_player(p, self.config, self.project) for p in routed)
        self.backend = Herdr(self.project)
        identity = hashlib.sha256(str(self.config).encode()).hexdigest()[:16]
        self.directory = self.project / ".dreamteam" / "coordination"
        self.path = self.directory / f"{identity}.json"
        self.lock_path = self.directory / f"{identity}.lock"

    def _check_active(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise BackendError("The coordinating client disconnected; no further work will be sent.")

    def player(self, player_id: str) -> Player:
        player = next((p for p in self.players if p.id == player_id), None)
        if player is None:
            raise ValueError("Choose a roster player: " + ", ".join(p.id for p in self.players))
        return player

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "config": str(self.config), "project": str(self.project), "tasks": []}
        data = json.loads(self.path.read_text())
        if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("config") != str(self.config) or data.get("project") != str(self.project)
                or not isinstance(data.get("tasks"), list)
                or any(not isinstance(task, dict) for task in data["tasks"])):
            raise ValueError("The assignment ledger is invalid; it was not changed.")
        return data

    def _save(self, data: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as destination:
            json.dump(data, destination, indent=2)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(self.path)

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield self._load()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def status(self) -> dict:
        observations = self.backend.refresh(self.players)
        data = self._load()
        play = asdict(self.play) if self.play else None
        if play:
            play["project"] = str(play["project"])
        return {
            "project": str(self.project), "play": play, "coach_instructions": COACH_INSTRUCTIONS,
            "players": [
                {"id": p.id, "name": p.name, "position": p.position, "role": p.role,
                 "activity": asdict(observations[p.id])}
                for p in self.players
            ],
            "tasks": data["tasks"],
            "note": "Activity is not completion. Reported results still need review and human acceptance.",
        }

    def read(self, player_id: str) -> dict:
        player = self.player(player_id)
        output = self.backend.read_output(player)
        return {"player_id": player.id, "observed_at": output.observed_at, "output": output.text}

    def _prompt(self, player: Player, task: dict) -> str:
        command = shlex.join(command_prefix(self.config, self.project))
        play_scope = ""
        if self.play is not None:
            play_scope = (f"\nCurrent play: {self.play.goal}\nConstraints: {self.play.constraints or 'None specified'}"
                          f"\nAcceptance criteria: {self.play.acceptance}\n")
        return (
            f"Dreamteam assignment {task['id']} from the user's Coach console.\n"
            f"You are {player.name} ({player.position}); role: {player.role}\n"
            f"Project: {player.cwd}\n{play_scope}\n"
            f"Assigned task (begin this bounded work now):\n{task['task']}\n\n"
            "Honor the task scope and project instructions. Do not delegate to hidden subagents "
            "or other roster players; send any proposed handoff back to Coach. "
            "Do not connect to a broker or trade without explicit user authorization.\n"
            "Report working when you begin and done or blocked before your final response using "
            "the dreamteam report_progress tool with task_id " + task["id"] + ", state and summary. "
            "If this already-running session has no report tool, use this command instead, "
            "replacing STATE with working, blocked, or done "
            "and SUMMARY with a concise account of changes, checks, results, and blockers:\n"
            f"{command} report {task['id']} --player {player.id} --state STATE --summary \"SUMMARY\"\n"
            "Record working when you begin, and done or blocked before your final response. "
            "If reporting is blocked, say so visibly in your response. A done report is your "
            "self-report, not the user's acceptance."
        )

    def dispatch(self, player_id: str, task_text: str, request_id: str) -> dict:
        self._check_active()
        player = self.player(player_id)
        task_text = _text(task_text, "Task")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", request_id):
            raise ValueError("Use a request ID of 1–64 letters, digits, underscores, or hyphens.")
        with self._locked() as data:
            existing = next((t for t in data["tasks"] if t.get("request_id") == request_id), None)
            if existing:
                if existing["player_id"] != player_id or existing["task"] != task_text:
                    raise ValueError("This request ID already names a different assignment.")
                return existing
            active = next((t for t in reversed(data["tasks"]) if t.get("player_id") == player_id
                           and t.get("delivery") != "not_sent" and t.get("progress") not in {"reported_done", "cancelled"}), None)
            if active:
                raise ValueError(f"{player.name} has unresolved assignment {active['id']}; inspect it before dispatching more work.")
            task = {"id": uuid.uuid4().hex[:12], "request_id": request_id,
                    "player_id": player_id, "task": task_text, "created_at": time.time(),
                    "delivery": "preparing", "progress": "assigned", "summary": "", "reports": [],
                    "coordinator_token": self.coordinator_token}
            data["tasks"].append(task)
            self._save(data)
        # Persist intent before starting/sending. A crash never permits automatic replay.
        delivery = "not_sent"
        detail = ""
        receipt = None
        try:
            self._check_active()
            guard = ({"coordinator": (coach_player(self.config, self.project), self.coordinator_token)}
                     if self.coordinator_token is not None else {})
            observation = self.backend.start(player, **guard)
            if observation.activity != "idle":
                raise BackendError(f"{player.name} is {observation.activity}: {observation.detail}")
            self._check_active()
            delivery = "uncertain"
            receipt = self.backend.send_prompt(player, self._prompt(player, task), **guard)
            delivery = "submitted"
        except (BackendError, OSError, ValueError) as exc:
            # Import locally to keep the distinction at the actual delivery boundary.
            from .runtime import PromptRejected
            if isinstance(exc, PromptRejected):
                delivery = "not_sent"
            detail = str(exc)
        with self._locked() as data:
            record = next(t for t in data["tasks"] if t["id"] == task["id"])
            record.update(delivery=delivery, delivery_detail=detail, receipt=receipt)
            self._save(data)
            return record

    def report(self, task_id: str, player_id: str, state: str, summary: str,
               *, source: str = "player") -> dict:
        self._check_active()
        self.player(player_id)
        if source not in {"player", "coach_observed"}:
            raise ValueError("Unknown report source.")
        if state not in {"working", "blocked", "done"}:
            raise ValueError("Report working, blocked, or done.")
        summary = _text(summary, "Summary", 8000)
        with self._locked() as data:
            task = next((t for t in data["tasks"] if t.get("id") == task_id), None)
            if task is None or task.get("player_id") != player_id:
                raise ValueError("The assignment does not belong to this player.")
            if task.get("progress") == "cancelled":
                raise ValueError("This assignment was cancelled; a late report cannot revive it.")
            if task.get("delivery") == "not_sent":
                raise ValueError("This assignment was not sent.")
            if task.get("progress") == "reported_done" and state != "done":
                raise ValueError("A finished report cannot move back to working or blocked.")
            task["progress"] = "reported_done" if state == "done" else state
            task["summary"] = summary
            task["reports"].append({"state": state, "summary": summary, "at": time.time(), "source": source})
            self._save(data)
            return task

    def close_stopped_task(self, task_id: str, reason: str) -> dict:
        """Explicitly retire abandoned work, never infer success from a stop."""
        self._check_active()
        reason = _text(reason, "Reason", 8000)
        with self._locked() as data:
            task = next((t for t in data["tasks"] if t.get("id") == task_id), None)
            if task is None:
                raise ValueError("Unknown assignment.")
            if task.get("progress") == "cancelled":
                return task
            if task.get("progress") == "reported_done":
                raise ValueError("This task already has a result; review it rather than cancelling it.")
            # A same-generation dispatch may still be between intent and startup.
            # Only a replacement Coach can retire that intent: runtime guards
            # prevent the former Coach from starting or sending any more work.
            owner = task.get("coordinator_token")
            if task.get("delivery") == "preparing" and (
                    not owner or not self.coordinator_token or owner == self.coordinator_token):
                raise ValueError("Delivery preparation is unresolved. Stop and restart Coach before closing this task.")
            player = self.player(task["player_id"])
            observation = self.backend.refresh((player,))[player.id]
            if observation.activity != "stopped":
                raise ValueError("The player must be confirmed stopped before closing its assignment.")
            self._check_active()
            task["progress"] = "cancelled"
            task["summary"] = reason
            task["reports"].append({"state": "cancelled", "summary": reason,
                                    "at": time.time(), "source": "coach_observed"})
            self._save(data)
            return task

    def wait(self, task_id: str, timeout: float) -> dict:
        if not 0 <= timeout <= 30:
            raise ValueError("Wait timeout must be between 0 and 30 seconds.")
        deadline = time.monotonic() + timeout
        while True:
            self._check_active()
            task = next((t for t in self._load()["tasks"] if t.get("id") == task_id), None)
            if task is None:
                raise ValueError("Unknown assignment.")
            if task.get("progress") in {"reported_done", "blocked", "cancelled"}:
                return {"finished_wait": True, "task": task}
            if time.monotonic() >= deadline:
                return {"finished_wait": False, "task": task, "note": "Wait timed out; this does not mean completion."}
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def main() -> None:
    parser = argparse.ArgumentParser(description="Local team commands for the Dreamteam Coach.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True, help="Pin the expected project")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    read = actions.add_parser("read")
    read.add_argument("player")
    dispatch = actions.add_parser("dispatch")
    dispatch.add_argument("player")
    dispatch.add_argument("--request-id", required=True)
    task = dispatch.add_mutually_exclusive_group(required=True)
    task.add_argument("--task")
    task.add_argument("--task-file", type=Path)
    report = actions.add_parser("report")
    report.add_argument("task_id")
    report.add_argument("--player", required=True)
    report.add_argument("--state", choices=("working", "blocked", "done"), required=True)
    report.add_argument("--summary", required=True)
    wait = actions.add_parser("wait")
    wait.add_argument("task_id")
    wait.add_argument("--timeout", type=float, default=20)
    play = actions.add_parser("play")
    play.add_argument("--goal", required=True)
    play.add_argument("--constraints", default="")
    play.add_argument("--acceptance", required=True)
    args = parser.parse_args()
    try:
        team = Team(args.config, args.project)
        if args.action == "status":
            result = team.status()
        elif args.action == "read":
            result = team.read(args.player)
        elif args.action == "dispatch":
            text = args.task if args.task is not None else args.task_file.read_text()
            result = team.dispatch(args.player, text, args.request_id)
        elif args.action == "report":
            result = team.report(args.task_id, args.player, args.state, args.summary)
        elif args.action == "wait":
            result = team.wait(args.task_id, args.timeout)
        else:
            result = asdict(team.play_store.create(str(team.project), args.goal, args.constraints, args.acceptance))
            result["project"] = str(result["project"])
        print(json.dumps(result, indent=2))
        if args.action == "dispatch" and result["delivery"] != "submitted":
            raise SystemExit(1)
    except (OSError, ValueError, BackendError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
