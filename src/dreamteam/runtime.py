"""A small, explicitly scoped wrapper around Herdr's JSON CLI."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import unicodedata
import uuid

from .roster import Player


class BackendError(Exception):
    """A runtime operation could not be verified or completed."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class PromptRejected(BackendError):
    """Prompt validation or a confirmed pre-delivery rejection prevented input."""


class PromptUncertain(BackendError):
    """Delivery may have occurred; automatic retry would risk duplicate work."""


@dataclass(frozen=True)
class Observation:
    activity: str
    detail: str
    pane_id: str | None = None
    observed_at: float = 0


@dataclass(frozen=True)
class Preview:
    text: str
    observed_at: float
    revision: int | None = None


def _plain_terminal_text(text: str) -> str:
    """Keep visible text, never executable terminal controls or escape payloads."""
    text = re.sub(r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)", "", text, flags=re.S)
    text = re.sub(r"(?:\x1b[P^_X]|[\x90\x98\x9e\x9f]).*?(?:\x1b\\|\x9c|$)",
                  "", text, flags=re.S)
    text = re.sub(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*(?:[@-~]|$)", "", text)
    text = re.sub(r"\x1b[ -/]*[@-~]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(character for character in text
                   if character in "\n\t" or unicodedata.category(character) not in {"Cc", "Cs", "Cf"})


class Herdr:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.directory = self.project / ".dreamteam"
        self.path = self.directory / "runtime.json"

    def _env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("HERDR_")}
        env["HERDR_CONFIG_PATH"] = str(self.directory / "herdr.toml")
        return env

    @contextmanager
    def _locked(self, *, create: bool = False):
        if not create and not self.path.exists():
            yield None
            return
        if create:
            self.directory.mkdir(exist_ok=True)
        with (self.directory / "runtime.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackendError("Another Courtside operation is in progress. Refresh shortly.", code="runtime_busy") from exc
            try:
                if self.path.exists():
                    try:
                        state = json.loads(self.path.read_text())
                    except (ValueError, OSError) as exc:
                        raise BackendError("Cannot read .dreamteam/runtime.json; bindings were left untouched.") from exc
                    if (not isinstance(state, dict) or state.get("version") != 1
                            or state.get("project") != str(self.project)
                            or not isinstance(state.get("session"), str)
                            or not state["session"].startswith("dreamteam-")
                            or not isinstance(state.get("players"), dict)):
                        raise BackendError("Invalid runtime state or different project. Bindings were left untouched.")
                elif create:
                    state = {
                        "version": 1, "project": str(self.project),
                        "session": "dreamteam-" + uuid.uuid4().hex[:16],
                        "server_requested": False, "socket": None, "players": {},
                    }
                    self._save(state)
                else:
                    state = None
                yield state
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self, state: dict):
        temporary = self.directory / "runtime.json.tmp"
        with temporary.open("w") as destination:
            json.dump(state, destination, indent=2)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(self.path)

    def _command(self, state: dict, *args: str) -> list[str]:
        binary = shutil.which("herdr")
        if binary is None:
            raise BackendError("Herdr is missing from PATH. Install Herdr before starting a player.")
        return [binary, "--session", state["session"], *args]

    def _call(self, state: dict, *args: str, timeout: float = 5) -> dict:
        try:
            result = subprocess.run(
                self._command(state, *args), env=self._env(), cwd=self.project,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError("Herdr timed out. Delivery is uncertain; refresh before retrying.") from exc
        except OSError as exc:
            raise BackendError(f"Could not run Herdr: {exc}") from exc
        output = result.stdout if result.returncode == 0 else result.stderr
        try:
            response = json.loads(output)
        except ValueError as exc:
            message = output.strip()[:500] or f"exit {result.returncode}"
            raise BackendError(f"Herdr returned no valid JSON: {message}") from exc
        if not isinstance(response, dict):
            raise BackendError("Herdr returned an unexpected response.")
        if result.returncode or "error" in response:
            error = response.get("error", {})
            if not isinstance(error, dict):
                raise BackendError("Herdr returned an invalid error response.")
            code = error.get("code") if isinstance(error.get("code"), str) else None
            raise BackendError(f"Herdr: {code or 'error'}: {error.get('message', output[:500])}", code=code)
        return response

    def _socket(self, state: dict) -> dict:
        info = self._call(state, "status", "server", "--json")
        if not info.get("running") or not info.get("compatible") or info.get("session") != state["session"]:
            raise BackendError("The dedicated Herdr session is unavailable or incompatible. Activity is unknown.")
        try:
            socket_path = Path(info["socket"])
            details = socket_path.stat()
            if not stat.S_ISSOCK(details.st_mode):
                raise ValueError("not a socket")
        except (KeyError, OSError, ValueError) as exc:
            raise BackendError("Cannot verify the dedicated Herdr socket.") from exc
        return {"path": str(socket_path), "device": details.st_dev,
                "inode": details.st_ino, "created_ns": details.st_ctime_ns}

    def _snapshot(self, state: dict) -> dict:
        socket = self._socket(state)
        if state.get("socket") is not None and socket != state["socket"]:
            raise BackendError("Herdr's server changed. Saved bindings are stale; automatic recovery is not supported yet.")
        response = self._call(state, "api", "snapshot")
        try:
            snapshot = response["result"]["snapshot"]
            if not all(isinstance(snapshot[key], list) for key in ("panes", "agents", "workspaces")):
                raise ValueError("invalid lists")
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendError("Herdr returned an unexpected session snapshot.") from exc
        if self._socket(state) != socket:
            raise BackendError("Herdr restarted while reading activity. Refresh before acting.")
        return snapshot

    def _ensure_server(self, state: dict) -> dict:
        if state.get("socket"):
            return self._snapshot(state)
        if not state.get("server_requested"):
            # The intent is durable before launching, so an interrupted start is never replayed blindly.
            config = self.directory / "herdr.toml"
            if not config.exists():
                config.write_text(
                    "onboarding = false\n[update]\nversion_check = false\nmanifest_check = false\n"
                    "[ui.sound]\nenabled = false\n"
                )
            command = self._command(state, "server")
            state["server_requested"] = True
            self._save(state)
            try:
                with (self.directory / "herdr.log").open("a") as log:
                    subprocess.Popen(
                        command, env=self._env(), cwd=self.project,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                    )
            except OSError as exc:
                state["server_requested"] = False
                self._save(state)
                raise BackendError(f"Could not start the dedicated Herdr server: {exc}") from exc
        deadline = time.monotonic() + 8
        error = None
        while time.monotonic() < deadline:
            try:
                socket = self._socket(state)
                state["socket"] = socket
                snapshot = self._snapshot(state)
                self._save(state)
                return snapshot
            except BackendError as exc:
                error = exc
                time.sleep(0.2)
        raise BackendError(f"Dedicated server startup is unconfirmed. No duplicate server was launched. {error}")

    def _bound(self, state: dict, player: Player, snapshot: dict):
        binding = state["players"].get(player.id)
        if binding is None:
            return None, None
        if not isinstance(binding, dict) or binding.get("cwd") != str(player.cwd):
            raise BackendError(f"{player.name}'s saved project directory does not match the roster.")
        pane_id = binding.get("pane_id")
        if pane_id is None:
            # A workspace-create response may have been lost. Adopt only the unique recorded intent.
            workspaces = [w for w in snapshot["workspaces"] if w.get("label") == binding.get("label")]
            panes = [p for p in snapshot["panes"] if any(
                p.get("workspace_id") == w.get("workspace_id") for w in workspaces)]
            if len(workspaces) != 1 or len(panes) != 1:
                raise BackendError(f"{player.name}'s pane creation is unconfirmed. Refusing to create a duplicate.")
            pane = panes[0]
            binding.update(pane_id=pane["pane_id"], terminal_id=pane["terminal_id"],
                           workspace_id=pane["workspace_id"], phase="allocated")
            self._save(state)
        else:
            panes = [p for p in snapshot["panes"] if p.get("pane_id") == pane_id]
            if not panes:
                return None, None
            if len(panes) != 1:
                raise BackendError(f"Ambiguous pane for {player.name}.")
            pane = panes[0]
        # Herdr's live cwd is mutable context, not the terminal's identity.
        if (pane.get("terminal_id") != binding.get("terminal_id")
                or pane.get("workspace_id") != binding.get("workspace_id")):
            raise BackendError(f"{player.name}'s pane identity changed. Refusing to act on it.")
        workspaces = [w for w in snapshot["workspaces"] if w.get("workspace_id") == binding["workspace_id"]]
        if len(workspaces) != 1 or workspaces[0].get("label") != binding["label"]:
            raise BackendError(f"{player.name}'s workspace identity changed. Refusing to act on it.")
        agents = [a for a in snapshot["agents"] if
                  a.get("pane_id") == pane["pane_id"] or a.get("name") == player.id]
        if len(agents) > 1:
            raise BackendError(f"Conflicting live identity for {player.name}.")
        agent = agents[0] if agents else None
        if agent is not None and (
            agent.get("name") != player.id or agent.get("agent") != "codex"
            or agent.get("pane_id") != pane["pane_id"]
            or agent.get("terminal_id") != pane["terminal_id"]
        ):
            raise BackendError(f"{player.name}'s pane now contains a different agent. Refusing to act.")
        return pane, agent

    @staticmethod
    def _observation(pane, agent, detail: str = "") -> Observation:
        now = time.time()
        if pane is None:
            return Observation("stopped", "No live player pane. Press S to start.", observed_at=now)
        if agent is None:
            return Observation(
                "unknown", detail or "Codex is no longer detected in the saved pane. Inspect that pane before restarting the player.",
                pane["pane_id"], now,
            )
        activity = agent.get("agent_status", "unknown")
        if activity == "done":
            activity = "idle"
        if activity not in {"idle", "working", "blocked", "unknown"}:
            activity = "unknown"
        return Observation(activity, detail or "Codex session verified. Enter to attach; activity is not task completion.",
                           pane["pane_id"], now)

    def refresh(self, players: tuple[Player, ...]) -> dict[str, Observation]:
        with self._locked() as state:
            if state is None or not state.get("server_requested"):
                return {p.id: self._observation(None, None) for p in players}
            snapshot = self._snapshot(state)
            observations = {}
            for player in players:
                try:
                    observations[player.id] = self._observation(*self._bound(state, player, snapshot))
                except BackendError as exc:
                    observations[player.id] = Observation("unknown", str(exc), observed_at=time.time())
            return observations

    def _saved_coordinator_token(self, state: dict, coordinator: Player) -> str:
        """Hash only a complete, already-persisted launch identity."""
        if (not isinstance(state, dict) or state.get("version") != 1
                or state.get("project") != str(self.project)
                or not isinstance(state.get("session"), str)
                or not state["session"].startswith("dreamteam-")
                or state.get("server_requested") is not True
                or not isinstance(state.get("players"), dict)):
            raise BackendError("Coach has no confirmed runtime binding; dispatch was not started.")
        socket = state.get("socket")
        if (not isinstance(socket, dict) or not isinstance(socket.get("path"), str)
                or not socket["path"] or any(type(socket.get(key)) is not int
                                            for key in ("device", "inode", "created_ns"))):
            raise BackendError("Coach has no confirmed server binding; dispatch was not started.")
        binding = state["players"].get(coordinator.id)
        if (not isinstance(binding, dict) or binding.get("cwd") != str(coordinator.cwd)
                or any(not isinstance(binding.get(key), str) or not binding[key]
                       for key in ("pane_id", "terminal_id", "workspace_id", "label"))
                or binding.get("phase") not in {"launching", "launched"}):
            raise BackendError("Coach has no confirmed terminal binding; dispatch was not started.")
        identity = {
            "session": state["session"], "socket": socket,
            "project": state["project"], "coordinator": coordinator.id,
            "binding": {key: binding[key] for key in
                        ("cwd", "pane_id", "terminal_id", "workspace_id", "label")},
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def _coordinator_identity(self, state: dict | None, coordinator: Player, snapshot: dict) -> str:
        if state is None:
            raise BackendError("Coach is no longer running; this coordinator cannot dispatch work.")
        token = self._saved_coordinator_token(state, coordinator)
        pane, agent = self._bound(state, coordinator, snapshot)
        if pane is None or agent is None:
            raise BackendError("Coach is no longer running; this coordinator cannot dispatch work.")
        if agent.get("launch_pending") or agent.get("interactive_ready") is False:
            raise BackendError("Coach is still starting; dispatch was not started.")
        return token

    def coordinator_token(self, coordinator: Player) -> str:
        """Capture saved launch identity without claiming the Coach is live.

        The MCP child starts while Herdr.start holds the runtime lock. Its
        initialization must read the atomically replaced state file without
        waiting for that lock or calling Herdr. Mutations later verify this
        exact token and a live Codex coordinator under the runtime lock.
        """
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise BackendError("Cannot capture Coach's saved launch identity.") from exc
        return self._saved_coordinator_token(state, coordinator)

    def _require_coordinator(
        self, state: dict | None, coordinator: tuple[Player, str], snapshot: dict,
    ) -> None:
        player, token = coordinator
        if self._coordinator_identity(state, player, snapshot) != token:
            raise BackendError("Coach's session changed; this coordinator can no longer dispatch work.")

    def start(
        self, player: Player, *, coordinator: tuple[Player, str] | None = None,
    ) -> Observation:
        with self._locked(create=coordinator is None) as state:
            if coordinator is None:
                snapshot = self._ensure_server(state)
            else:
                if state is None or not state.get("server_requested"):
                    raise BackendError("Coach is no longer running; this coordinator cannot dispatch work.")
                snapshot = self._snapshot(state)
                self._require_coordinator(state, coordinator, snapshot)
            pane, agent = self._bound(state, player, snapshot)
            if agent is not None:
                return self._observation(pane, agent, "Reusing the existing Codex session.")
            if pane is None:
                # Reject a live unbound name instead of accidentally attaching to someone else.
                if any(a.get("name") == player.id for a in snapshot["agents"]):
                    raise BackendError(f"A live {player.id} has no verified binding. Refusing a duplicate.")
                binding = {
                    "label": f"Dreamteam / {player.name} / {uuid.uuid4().hex[:8]}",
                    "cwd": str(player.cwd), "phase": "creating",
                }
                state["players"][player.id] = binding
                self._save(state)
                created = self._call(state, "workspace", "create", "--cwd", str(player.cwd),
                                     "--label", binding["label"], "--no-focus")["result"]["root_pane"]
                binding.update(pane_id=created["pane_id"], terminal_id=created["terminal_id"],
                               workspace_id=created["workspace_id"], phase="allocated")
                self._save(state)
                pane, agent = self._bound(state, player, self._snapshot(state))
            if pane is None:
                raise BackendError("Player pane disappeared during startup; refresh before retrying.")
            binding = state["players"][player.id]
            if binding.get("phase") != "allocated":
                raise BackendError("A launch was already attempted in this pane. Inspect it before restarting the player.")
            if pane.get("cwd") != str(player.cwd):
                raise BackendError(
                    f"{player.name}'s new pane is not in its configured project directory. Codex was not launched."
                )
            binding["phase"] = "launching"
            self._save(state)
            problem = ""
            try:
                self._call(state, "agent", "start", player.id, "--kind", "codex", "--pane", pane["pane_id"],
                           "--timeout", "15000", "--", *player.args, timeout=18)
                binding["phase"] = "launched"
                self._save(state)
            except BackendError as exc:
                problem = str(exc)
            pane, agent = self._bound(state, player, self._snapshot(state))
            return self._observation(pane, agent, problem)

    def preview(self, player: Player) -> Preview:
        """Read the verified visible viewport without attaching or scrolling."""
        return self._read_verified_output(player, "visible")

    def _read_verified_output(
        self, player: Player, source: str, *, lines: int | None = None, limit: int | None = None,
    ) -> Preview:
        with self._locked() as state:
            if state is None or not state.get("server_requested"):
                return Preview("", time.time())
            binding = state["players"].get(player.id)
            if binding is None:
                return Preview("", time.time())
            # Preview must not reconcile uncertain creation or persist a new binding.
            if not isinstance(binding, dict) or not binding.get("pane_id") or not state.get("socket"):
                raise BackendError(f"{player.name}'s terminal binding is not verified. Refresh its activity.")
            pane, agent = self._bound(state, player, self._snapshot(state))
            if pane is None:
                return Preview("", time.time())
            if agent is None:
                raise BackendError(
                    f"No verified Codex session for {player.name}; Codex is no longer detected in its saved pane. "
                    "Inspect that pane before restarting the player."
                )
            # Unlike Herdr's control commands, pane read emits text, not JSON.
            try:
                result = subprocess.run(
                    self._command(state, "pane", "read", pane["pane_id"],
                                  "--source", source, "--format", "text",
                                  *(() if lines is None else ("--lines", str(lines)))),
                    env=self._env(), cwd=self.project, capture_output=True, text=True, timeout=5,
                )
            except subprocess.TimeoutExpired as exc:
                raise BackendError("Herdr timed out while reading the terminal preview.") from exc
            except OSError as exc:
                raise BackendError(f"Could not read the terminal preview: {exc}") from exc
            if result.returncode:
                message = _plain_terminal_text(result.stderr).strip()[:500] or f"exit {result.returncode}"
                raise BackendError(f"Herdr could not read the terminal preview: {message}")
            # A restart or replaced agent during the read invalidates the captured output.
            current_pane, current_agent = self._bound(state, player, self._snapshot(state))
            if current_pane is None or current_agent is None:
                raise BackendError(f"{player.name}'s session changed while reading its output. Refresh.")
            text = _plain_terminal_text(result.stdout)
            return Preview(text if limit is None else text[-limit:], time.time())

    def read_output(self, player: Player, lines: int = 80) -> Preview:
        """Read a bounded recent transcript for a coach-requested inspection."""
        if type(lines) is not int or not 1 <= lines <= 200:
            raise BackendError("Output lines must be between 1 and 200.")
        return self._read_verified_output(player, "recent-unwrapped", lines=lines, limit=32768)

    def send_prompt(
        self, player: Player, text: str, *, coordinator: tuple[Player, str] | None = None,
    ) -> dict:
        """Submit once to an idle, verified agent; acknowledgment is not completion."""
        if (not isinstance(text, str) or not text.strip() or len(text) > 32768
                or any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\t"
                       for char in text)):
            raise PromptRejected("A prompt must contain 1–32768 characters without terminal control characters.")
        attempted = False
        try:
            with self._locked() as state:
                if state is None or not state.get("server_requested"):
                    raise PromptRejected(f"Start {player.name} before sending a prompt.")
                binding = state["players"].get(player.id)
                if (not isinstance(binding, dict) or not binding.get("pane_id")
                        or not state.get("socket")):
                    raise PromptRejected(f"{player.name}'s terminal binding is not verified.")
                snapshot = self._snapshot(state)
                if coordinator is not None:
                    self._require_coordinator(state, coordinator, snapshot)
                pane, agent = self._bound(state, player, snapshot)
                if pane is None or agent is None:
                    raise PromptRejected(f"No verified Codex session for {player.name}.")
                if self._observation(pane, agent).activity != "idle":
                    raise PromptRejected(f"{player.name} is not idle. Inspect its terminal before sending another task.")
                if agent.get("launch_pending") or agent.get("interactive_ready") is False:
                    raise PromptRejected(f"{player.name} is not ready for input.")
                # Herdr 0.9.3 agent targets accept names/pane IDs, not terminal IDs.
                # Keep the verified pane target and require the acknowledgement
                # and subsequent snapshot to identify the same terminal.
                attempted = True
                try:
                    response = self._call(
                        state, "agent", "prompt", pane["pane_id"], text, timeout=8,
                    )
                except BackendError as exc:
                    if exc.code in {
                        "empty_agent_prompt", "agent_not_found", "agent_target_ambiguous",
                        "agent_not_ready", "agent_blocked",
                    }:
                        raise PromptRejected(f"Herdr rejected the prompt before delivery: {exc}") from exc
                    raise
                result = response.get("result")
                delivered_agent = result.get("agent") if isinstance(result, dict) else None
                expected = {
                    "name": player.id, "agent": "codex", "pane_id": pane["pane_id"],
                    "terminal_id": pane["terminal_id"], "workspace_id": pane["workspace_id"],
                }
                if (not isinstance(result, dict) or result.get("type") != "agent_prompted"
                        or not isinstance(delivered_agent, dict)
                        or any(delivered_agent.get(key) != value for key, value in expected.items())):
                    raise PromptUncertain("The prompt acknowledgment did not verify the intended terminal. Do not resend.")
                current_pane, current_agent = self._bound(state, player, self._snapshot(state))
                if current_pane is None or current_agent is None:
                    raise PromptUncertain("The agent changed after submission. Delivery is uncertain; do not resend.")
                return {
                    "delivery": "submitted", "player_id": player.id,
                    "pane_id": pane["pane_id"], "terminal_id": pane["terminal_id"],
                    "acknowledged_at": time.time(),
                }
        except (PromptRejected, PromptUncertain):
            raise
        except Exception as exc:
            if attempted:
                raise PromptUncertain(f"Prompt delivery is uncertain; do not resend. {exc}") from exc
            raise PromptRejected(f"Prompt was not sent: {exc}") from exc

    def attachment(self, player: Player) -> tuple[list[str], dict[str, str]]:
        with self._locked() as state:
            if state is None or not state.get("server_requested"):
                raise BackendError(f"Start {player.name} before attaching.")
            pane, agent = self._bound(state, player, self._snapshot(state))
            if pane is None:
                raise BackendError(f"No live pane for {player.name}. Start the player before attaching.")
            if agent is None:
                raise BackendError(
                    f"Codex is no longer detected in {player.name}'s saved pane. "
                    "Inspect that pane before restarting the player."
                )
            return self._command(state, "agent", "attach", pane["pane_id"]), self._env()

    def control_command(self, player: Player, columns: int, rows: int) -> tuple[list[str], dict[str, str]]:
        """Pin inline control to a verified terminal, never a mutable pane alias."""
        if type(columns) is not int or type(rows) is not int or not (2 <= columns <= 1000 and 2 <= rows <= 1000):
            raise BackendError("The inline terminal dimensions are invalid.")
        with self._locked() as state:
            if state is None or not state.get("server_requested"):
                raise BackendError(f"Start {player.name} before opening its terminal.")
            pane, agent = self._bound(state, player, self._snapshot(state))
            if pane is None or agent is None:
                raise BackendError(f"No verified Codex session for {player.name}. Refresh its activity.")
            return self._command(
                state, "terminal", "session", "control", pane["terminal_id"],
                "--cols", str(columns), "--rows", str(rows),
            ), self._env()

    def stop(self, player: Player) -> None:
        with self._locked() as state:
            if state is None or not state.get("server_requested"):
                return
            pane, _ = self._bound(state, player, self._snapshot(state))
            if pane is not None:
                self._call(state, "pane", "close", pane["pane_id"])
            state["players"].pop(player.id, None)
            self._save(state)

    def _cleanup_preservation_reason(self, state: dict, pane: dict, agent: dict | None) -> str | None:
        caller_socket = os.environ.get("HERDR_SOCKET_PATH")
        if (caller_socket and os.environ.get("HERDR_PANE_ID") == pane["pane_id"]
                and Path(caller_socket).resolve() == Path(state["socket"]["path"]).resolve()):
            return "current Courtside terminal"
        if agent is not None:
            return None
        try:
            info = self._call(state, "pane", "process-info", "--pane", pane["pane_id"])["result"]["process_info"]
            shell = info.get("shell_pid")
            foreground = info.get("foreground_processes", [])
            idle_shell = (
                info.get("pane_id") == pane["pane_id"]
                and isinstance(shell, int) and shell > 0
                and info.get("foreground_process_group_id") == shell
                and len(foreground) == 1 and foreground[0].get("pid") == shell
            )
        except (BackendError, KeyError, TypeError, AttributeError):
            return "foreground process could not be verified"
        return None if idle_shell else "Codex ended and another foreground process owns this pane"

    def _reset_stopped_runtime(self, state: dict):
        state.update(session="dreamteam-" + uuid.uuid4().hex[:16],
                     server_requested=False, socket=None, players={})
        self._save(state)

    def shutdown(self, players: tuple[Player, ...]) -> str:
        """Close verified player panes and stop only an empty dedicated server."""
        with self._locked() as state:
            label = self.project.name
            if state is None or not state.get("server_requested"):
                return f"{label}: no active Dreamteam runtime."
            session = state["session"]
            status = self._call(state, "status", "server", "--json")
            if status.get("session") != session:
                raise BackendError("Cleanup could not verify the dedicated session name.")
            if status.get("running") is False:
                self._reset_stopped_runtime(state)
                return f"{label}: dedicated session {session} is already stopped."
            if not state.get("socket"):
                raise BackendError("Cleanup requires a verified dedicated server binding.")
            snapshot = self._snapshot(state)
            roster = {player.id: player for player in players}
            planned = []
            missing = []
            preserved = {}
            seen = set()
            # Validate every binding before closing or changing any saved state.
            for player_id, binding in state["players"].items():
                if player_id not in roster or not isinstance(binding, dict) or not binding.get("pane_id"):
                    raise BackendError(f"Cleanup cannot verify the saved binding for {player_id}. No panes were closed.")
                player = roster[player_id]
                pane, agent = self._bound(state, player, snapshot)
                if pane is None:
                    if any(item.get("terminal_id") == binding.get("terminal_id") for item in snapshot["panes"]):
                        raise BackendError(f"{player.name}'s terminal moved. No panes were closed.")
                    missing.append(player_id)
                    continue
                if pane["terminal_id"] in seen:
                    raise BackendError("Cleanup found duplicate terminal bindings. No panes were closed.")
                seen.add(pane["terminal_id"])
                reason = self._cleanup_preservation_reason(state, pane, agent)
                if reason:
                    preserved[pane["pane_id"]] = reason
                else:
                    planned.append(player)
            coach_binding = state["players"].get("coach")
            if isinstance(coach_binding, dict) and coach_binding.get("pane_id") in preserved:
                raise BackendError("Coach could not be stopped. No player panes were closed; cleanup remains incomplete.")
            planned.sort(key=lambda player: player.id != "coach")
            for player_id in missing:
                state["players"].pop(player_id)
            if missing:
                self._save(state)
            closed = 0
            for player in planned:
                try:
                    pane, agent = self._bound(state, player, self._snapshot(state))
                    if pane is None:
                        binding = state["players"][player.id]
                        if any(item.get("terminal_id") == binding.get("terminal_id")
                               for item in self._snapshot(state)["panes"]):
                            raise BackendError(f"{player.name}'s terminal moved during cleanup.")
                        state["players"].pop(player.id)
                        self._save(state)
                        continue
                    reason = self._cleanup_preservation_reason(state, pane, agent)
                    if reason:
                        preserved[pane["pane_id"]] = reason
                        if player.id == "coach":
                            break
                        continue
                    close_error = ""
                    try:
                        self._call(state, "pane", "close", pane["pane_id"])
                    except BackendError as exc:
                        close_error = str(exc)
                    after = self._snapshot(state)
                    remains = any(
                        item.get("pane_id") == pane["pane_id"]
                        or item.get("terminal_id") == pane["terminal_id"]
                        for item in after["panes"]
                    )
                    if remains:
                        preserved[pane["pane_id"]] = close_error or "close was not confirmed"
                        if player.id == "coach":
                            break
                    else:
                        closed += 1
                        state["players"].pop(player.id)
                        self._save(state)
                except BackendError as exc:
                    pane_id = state["players"][player.id].get("pane_id", player.id)
                    preserved[pane_id] = str(exc)
                    if player.id == "coach":
                        break
            try:
                remaining = self._snapshot(state)["panes"]
            except BackendError as exc:
                raise BackendError(
                    f"{label}: closed {closed} player panes; cleanup is unconfirmed: {exc}. "
                    f"Saved bindings and session {session} were preserved."
                ) from exc
            if remaining:
                caller_socket = os.environ.get("HERDR_SOCKET_PATH")
                for pane in remaining:
                    is_caller = (
                        caller_socket and os.environ.get("HERDR_PANE_ID") == pane["pane_id"]
                        and Path(caller_socket).resolve() == Path(state["socket"]["path"]).resolve()
                    )
                    reason = "current Courtside terminal" if is_caller else "untracked or otherwise preserved pane"
                    preserved.setdefault(pane["pane_id"], reason)
                detail = "; ".join(f"{pane_id}: {reason}" for pane_id, reason in preserved.items())
                summary = (f"{label}: closed {closed} player panes; preserved {detail}. "
                           f"Dedicated session {session} remains running.")
                if state["players"]:
                    raise BackendError(summary)
                return summary
            # Stop only this exact, verified, now-empty named session.
            stop_error = ""
            try:
                self._call(state, "session", "stop", session, "--json")
            except BackendError as exc:
                stop_error = str(exc)
            try:
                stopped = self._call(state, "status", "server", "--json")
            except BackendError as exc:
                stop_error = str(exc)
                stopped = {}
            if stopped.get("session") == session and stopped.get("running") is False:
                self._reset_stopped_runtime(state)
                return f"{label}: closed {closed} player panes; stopped dedicated session {session}."
            raise BackendError(
                (f"{label}: closed {closed} player panes; stopping session {session} is unconfirmed. "
                 f"Runtime state was preserved. {stop_error}").rstrip()
            )
