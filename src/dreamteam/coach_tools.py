"""Per-conversation, local stdio MCP tools for the Dreamteam roster.

The client owns this process. There is no listener, daemon, model API, or shell
tool; each invocation rechecks the pinned project through a fresh Team.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from queue import Empty, Full, Queue
import re
import sys
from threading import Event, Lock, Thread
from typing import BinaryIO, Callable

from .coordination import COACH_INSTRUCTIONS, Team, coach_player
from .runtime import BackendError, Herdr


PROTOCOL_VERSION = "2025-06-18"
MAX_MESSAGE = 128 * 1024


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _string(maximum: int, *, minimum: int = 1) -> dict:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


PLAYER_ID = {**_string(32), "pattern": r"^[a-z][a-z0-9_-]{0,31}$"}
TASK_ID = {**_string(64), "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"}
STATE = {"type": "string", "enum": ["working", "blocked", "done"]}


def _tool(name: str, description: str, properties: dict, required: list[str], *, read_only=False) -> dict:
    return {
        "name": name, "description": description,
        "inputSchema": {
            "type": "object", "properties": properties,
            "required": required, "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": read_only, "openWorldHint": False},
    }


COACH_TOOLS = (
    _tool("team_status", "Read the saved play, roster activity, assignments and explicit reports. "
          "Idle activity is not completion, and reports are not human acceptance.", {}, [], read_only=True),
    _tool("read_player_output", "Read a named player's bounded recent terminal output. "
          "Use this to inspect actual evidence before assessing or recording its report.",
          {"player_id": PLAYER_ID}, ["player_id"], read_only=True),
    _tool("dispatch_task", "Assign one bounded task to a roster player; start it if needed. "
          "Use a unique request_id per assignment and reuse that same ID to check a repeated request. "
          "Submitted means delivery only. Never retry uncertain delivery with a new ID.",
          {"player_id": PLAYER_ID, "task": _string(16000), "request_id": TASK_ID},
          ["player_id", "task", "request_id"]),
    _tool("wait_for_report", "Wait at most 30 seconds for an explicit done or blocked player report. "
          "A timeout or idle terminal does not mean completion.",
          {"task_id": TASK_ID, "timeout": {"type": "number", "minimum": 0, "maximum": 30, "default": 20}},
          ["task_id"], read_only=True),
    _tool("record_play", "Save the user's bounded play and acceptance criteria. "
          "An existing play is never silently replaced.",
          {"goal": _string(16000), "constraints": _string(16000, minimum=0), "acceptance": _string(16000)},
          ["goal", "acceptance"]),
    _tool("close_stopped_task", "Close an unresolved assignment only after its player is confirmed stopped. "
          "Record why it was stopped. This cancels the ledger assignment; it never marks work done or kills a session.",
          {"task_id": TASK_ID, "reason": _string(8000)}, ["task_id", "reason"]),
    _tool("record_player_report", "Record a legacy player's report only after reading that player's actual "
          "terminal output. Mark only the progress and evidence it visibly reported. This is a Coach-observed "
          "report, distinct from a player self-report and from the user's acceptance.",
          {"player_id": PLAYER_ID, "task_id": TASK_ID, "state": STATE, "summary": _string(8000)},
          ["player_id", "task_id", "state", "summary"]),
)
PLAYER_TOOLS = (
    _tool("report_progress", "Report working, blocked, or done for your assigned task. "
          "Describe actual work, checks, and blockers. Your identity is fixed by this connection; "
          "a done report is a self-report, not the user's acceptance.",
          {"task_id": TASK_ID, "state": STATE, "summary": _string(8000)},
          ["task_id", "state", "summary"]),
)


def _validate_arguments(arguments: object, schema: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ProtocolError(-32602, "Tool arguments must be an object.")
    properties = schema["properties"]
    if set(arguments) - properties.keys() or set(schema["required"]) - arguments.keys():
        raise ProtocolError(-32602, "Unexpected or missing tool arguments.")
    for key, value in arguments.items():
        field = properties[key]
        if field["type"] == "string":
            if (not isinstance(value, str) or len(value) < field.get("minLength", 0)
                    or len(value) > field.get("maxLength", 16000)):
                raise ProtocolError(-32602, f"Invalid text argument: {key}.")
            if "pattern" in field and re.fullmatch(field["pattern"], value) is None:
                raise ProtocolError(-32602, f"Invalid identifier: {key}.")
        elif field["type"] == "number":
            if (type(value) not in (int, float)
                    or not field["minimum"] <= value <= field["maximum"]):
                raise ProtocolError(-32602, f"Invalid numeric argument: {key}.")
        if "enum" in field and value not in field["enum"]:
            raise ProtocolError(-32602, f"Invalid value for {key}.")
    return arguments


class CoachTools:
    def __init__(
        self, config: Path, project: Path, actor: str, *, cancelled: Callable[[], bool] | None = None,
        coordinator_token: str | None = None,
    ):
        if actor != "coach" and re.fullmatch(PLAYER_ID["pattern"], actor) is None:
            raise ValueError("Actor must be coach or a named roster player.")
        self.config = config.expanduser().resolve()
        self.project = project.expanduser().resolve()
        self.actor = actor
        self.cancelled = cancelled or (lambda: False)
        self.coordinator_token = coordinator_token
        self.initialized = False
        self.ready = False
        self.tools = COACH_TOOLS if actor == "coach" else PLAYER_TOOLS

    def handle(self, message: object) -> dict | None:
        request_id = message.get("id") if isinstance(message, dict) else None
        try:
            if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                    or not isinstance(message.get("method"), str)
                    or ("id" in message and type(request_id) not in (str, int))):
                raise ProtocolError(-32600, "Invalid JSON-RPC request.")
            method = message["method"]
            if "id" not in message:
                if method == "notifications/initialized" and self.initialized:
                    self.ready = True
                return None
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ProtocolError(-32602, "Request params must be an object.")
            if method == "ping":
                result = {}
            elif method == "initialize":
                if self.initialized:
                    raise ProtocolError(-32600, "This connection is already initialized.")
                client = params.get("clientInfo")
                if (not isinstance(params.get("protocolVersion"), str)
                        or not isinstance(params.get("capabilities"), dict)
                        or not isinstance(client, dict)
                        or not isinstance(client.get("name"), str)
                        or not isinstance(client.get("version"), str)):
                    raise ProtocolError(-32602, "Invalid initialization parameters.")
                self.initialized = True
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "dreamteam", "version": "0.1.0"},
                    "instructions": (COACH_INSTRUCTIONS if self.actor == "coach" else
                                     "Report only your own assigned work. Reported done still needs human acceptance."),
                }
            else:
                if not self.ready:
                    raise ProtocolError(-32002, "Initialize this connection before using tools.")
                if method == "tools/list":
                    if set(params) - {"_meta"}:
                        raise ProtocolError(-32602, "Tool pagination is not supported.")
                    result = {"tools": list(self.tools)}
                elif method == "tools/call":
                    if set(params) - {"name", "arguments", "_meta"}:
                        raise ProtocolError(-32602, "Unexpected tool-call parameters.")
                    name = params.get("name")
                    tool = next((tool for tool in self.tools if tool["name"] == name), None)
                    if tool is None:
                        raise ProtocolError(-32602, "Unknown or unavailable tool for this actor.")
                    arguments = _validate_arguments(params.get("arguments", {}), tool["inputSchema"])
                    result = self._call_tool(name, arguments)
                else:
                    raise ProtocolError(-32601, "Method not found.")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ProtocolError as exc:
            return {"jsonrpc": "2.0", "id": request_id if type(request_id) in (str, int) else None,
                    "error": {"code": exc.code, "message": str(exc)}}

    def _call_tool(self, name: str, arguments: dict) -> dict:
        try:
            if self.cancelled():
                raise ValueError("The MCP client disconnected; no new action was started.")
            if self.actor == "coach" and self.coordinator_token is None:
                raise ValueError("The Coach terminal identity was not pinned at tool-server startup.")
            team = Team(self.config, self.project, cancelled=self.cancelled,
                        coordinator_token=self.coordinator_token)
            if self.actor != "coach":
                team.player(self.actor)
            if name == "team_status":
                result = team.status()
            elif name == "read_player_output":
                result = team.read(arguments["player_id"])
            elif name == "dispatch_task":
                result = team.dispatch(arguments["player_id"], arguments["task"], arguments["request_id"])
            elif name == "wait_for_report":
                result = team.wait(arguments["task_id"], arguments.get("timeout", 20))
            elif name == "record_play":
                team._check_active()
                play = team.play_store.create(
                    str(team.project), arguments["goal"], arguments.get("constraints", ""), arguments["acceptance"],
                )
                result = asdict(play)
                result["project"] = str(play.project)
            elif name == "close_stopped_task":
                result = team.close_stopped_task(arguments["task_id"], arguments["reason"])
            elif name == "record_player_report":
                result = team.report(arguments["task_id"], arguments["player_id"],
                                     arguments["state"], arguments["summary"], source="coach_observed")
            else:
                result = team.report(arguments["task_id"], self.actor, arguments["state"], arguments["summary"])
            is_error = name == "dispatch_task" and result.get("delivery") != "submitted"
            return {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True)}],
                "structuredContent": result, "isError": is_error,
            }
        except Exception as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


def _parse(line: bytes) -> object:
    def invalid_constant(_value):
        raise ValueError("Invalid JSON numeric constant.")
    return json.loads(line, object_pairs_hook=_unique_object, parse_constant=invalid_constant)


def serve(reader: BinaryIO, writer: BinaryIO, config: Path, project: Path, actor: str) -> None:
    """Serialize tools and cancel pending work as soon as stdin closes."""
    closed = Event()
    requests: Queue[tuple[object, str | int | None, Event | None]] = Queue(maxsize=32)
    pending: dict[str | int, tuple[str, Event]] = {}
    pending_lock = Lock()
    active: Event | None = None

    def cancelled() -> bool:
        return closed.is_set() or (active is not None and active.is_set())
    # Capture once before initialization, while the launching Coach may still
    # hold the runtime lock. Never adopt a replacement Coach on a later call.
    token = Herdr(project).coordinator_token(coach_player(config, project)) if actor == "coach" else None
    bridge = CoachTools(config, project, actor, cancelled=cancelled, coordinator_token=token)

    def read_requests():
        try:
            while not closed.is_set():
                line = reader.readline(MAX_MESSAGE + 1)
                if not line:
                    break
                if len(line) > MAX_MESSAGE:
                    item = ProtocolError(-32700, "MCP message is too large.")
                else:
                    try:
                        if not line.endswith(b"\n"):
                            raise ValueError("Missing message delimiter.")
                        item = _parse(line)
                    except (ValueError, UnicodeError, RecursionError):
                        item = ProtocolError(-32700, "Invalid JSON message.")
                key = None
                cancellation = None
                if isinstance(item, dict) and item.get("jsonrpc") == "2.0":
                    if "id" not in item and item.get("method") == "notifications/cancelled":
                        params = item.get("params")
                        cancelled_id = params.get("requestId") if isinstance(params, dict) else None
                        if type(cancelled_id) in (str, int):
                            with pending_lock:
                                request = pending.get(cancelled_id)
                                if request is not None and request[0] != "initialize":
                                    request[1].set()
                        continue
                    if type(item.get("id")) in (str, int) and isinstance(item.get("method"), str):
                        key = item["id"]
                        with pending_lock:
                            if key in pending:
                                item = ProtocolError(-32600, "Request ID is already in use.")
                                key = None
                            else:
                                cancellation = Event()
                                pending[key] = (item["method"], cancellation)
                try:
                    requests.put_nowait((item, key, cancellation))
                except Full:
                    break
                if len(line) > MAX_MESSAGE:
                    # Recover framing without ever buffering the whole record.
                    while line and not line.endswith(b"\n"):
                        line = reader.readline(MAX_MESSAGE + 1)
        except (OSError, ValueError):
            pass
        finally:
            closed.set()

    input_thread = Thread(target=read_requests, name="dreamteam-mcp-input", daemon=True)
    input_thread.start()
    try:
        while not closed.is_set():
            try:
                request, key, active = requests.get(timeout=0.1)
            except Empty:
                continue
            try:
                if closed.is_set():
                    break
                if active is not None and active.is_set():
                    continue
                if isinstance(request, ProtocolError):
                    response = {"jsonrpc": "2.0", "id": None,
                                "error": {"code": request.code, "message": str(request)}}
                else:
                    response = bridge.handle(request)
                if response is not None and not cancelled():
                    try:
                        writer.write(json.dumps(response, ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n")
                        writer.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        closed.set()
            finally:
                if key is not None:
                    with pending_lock:
                        pending.pop(key, None)
                active = None
    finally:
        closed.set()
        input_thread.join(timeout=0.2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Dreamteam tools for one Codex conversation.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    args = parser.parse_args()
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, args.config, args.project, args.actor)
    except (BackendError, OSError, ValueError) as exc:
        print(f"Dreamteam tool server could not start: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
