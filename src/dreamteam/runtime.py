"""A small, explicitly scoped wrapper around Rex's CLI."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
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


class Rex:
    """Manage only explicitly recorded blocks on the existing local Rex server."""

    def __init__(self, project: Path):
        self.project = project.resolve()
        self.directory = self.project / ".dreamteam"
        self.path = self.directory / "rex-runtime.json"

    def _call(self, state, *args, raw=False, input=None, timeout=8):
        binary = shutil.which("rex")
        if binary is None:
            raise BackendError("Rex is missing from PATH. Launch Courtside inside Rex.")
        command = [binary, "--autostart=false", "--timeout", "5s"]
        if state is not None:
            command += ["-S", state["endpoint"]]
        command += list(args)
        try:
            result = subprocess.run(command, input=input, capture_output=True, text=True,
                                    cwd=self.project, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"Rex operation unconfirmed: {exc}") from exc
        if result.returncode:
            raise BackendError("Rex: " + _plain_terminal_text(result.stderr).strip()[:500])
        if raw:
            return result.stdout
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise ValueError("expected an object")
            return data
        except ValueError as exc:
            raise BackendError("Rex returned an invalid JSON response.") from exc

    def _validate(self, state):
        if (not isinstance(state, dict) or state.get("version") != 1
                or state.get("project") != str(self.project)
                or not isinstance(state.get("players"), dict)
                or any(not isinstance(state.get(k), str) or not state[k]
                       for k in ("endpoint", "host_id", "instance_id", "session_id"))
                or not state["session_id"].startswith("session:")):
            raise BackendError("Invalid Rex runtime state; bindings were left untouched.")
        return state

    @contextmanager
    def _locked(self, *, create=False):
        if not create and not self.path.exists():
            yield None
            return
        self.directory.mkdir(exist_ok=True)
        with (self.directory / "rex-runtime.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackendError("Another team operation is running.", code="runtime_busy") from exc
            try:
                if self.path.exists():
                    try:
                        state = self._validate(json.loads(self.path.read_text()))
                    except (OSError, ValueError) as exc:
                        raise BackendError("Cannot read Rex bindings; state was preserved.") from exc
                elif create:
                    info = self._call(None, "whoami", "--json")
                    endpoint = info.get("endpoint", "")
                    if not endpoint.startswith("unix://") or not info.get("session_id"):
                        raise BackendError("Start Courtside inside a local Rex session.")
                    state = self._validate({"version": 1, "project": str(self.project),
                        **{k: info.get(k) for k in ("endpoint", "host_id", "instance_id", "session_id")},
                        "players": {}})
                    self._save(state)
                else:
                    state = None
                yield state
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self, state):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(state, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def _snapshot(self, state):
        info = self._call(state, "whoami", "--json")
        if any(info.get(k) != state[k] for k in ("host_id", "instance_id")):
            raise BackendError("Rex server identity changed. Saved bindings require inspection.")
        data = self._call(state, "block", "ls", "-s", state["session_id"], "--json")
        blocks = data.get("blocks")
        if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
            raise BackendError("Rex returned invalid block identities.")
        after = self._call(state, "whoami", "--json")
        if any(after.get(k) != state[k] for k in ("host_id", "instance_id")):
            raise BackendError("Rex restarted during observation; bindings were preserved.")
        return blocks

    def _method(self, state, block, method, args=None):
        command = ["block", "call", "-s", state["session_id"], "-b", block, method]
        if args is not None:
            command += ["-"]
        return self._call(state, *command, input=json.dumps(args) if args is not None else None)

    def _bound(self, state, player, blocks):
        binding = state["players"].get(player.id)
        if binding is None:
            return None, None
        if not isinstance(binding, dict) or binding.get("cwd") != str(player.cwd):
            raise BackendError(f"{player.name}'s saved project does not match.")
        if not binding.get("block_id"):
            matches = [b for b in blocks if b.get("label") == binding.get("label")]
            if len(matches) != 1:
                raise BackendError(f"{player.name}'s launch is unconfirmed; no duplicate will be created.")
            binding["block_id"] = matches[0]["block_id"]
            self._save(state)
        matches = [b for b in blocks if b.get("block_id") == binding["block_id"]]
        if not matches:
            return None, None
        if len(matches) != 1:
            raise BackendError("Ambiguous Rex block identity.")
        block = matches[0]
        if (block.get("label") != binding.get("label")
                or block.get("creator_name") != "com.superlogical.terminal"
                or block.get("host_id") != state["host_id"]):
            raise BackendError(f"{player.name}'s block identity changed; refusing to act.")
        process = self._method(state, block["block_id"], "process")
        child = process.get("child")
        if child is None and process.get("last_exit") is not None:
            return block, None
        if not isinstance(child, dict) or child.get("path") != binding.get("executable"):
            raise BackendError(f"{player.name}'s process identity cannot be verified.")
        if binding.get("pid") is None:
            if child.get("cwd") != binding["cwd"] or type(child.get("pid")) is not int:
                raise BackendError(f"{player.name}'s initial process directory cannot be verified.")
            binding["pid"] = child["pid"]
            binding["phase"] = "launched"
            self._save(state)
        elif child.get("pid") != binding["pid"]:
            raise BackendError(f"{player.name}'s process changed; refusing to act.")
        return block, process

    def _observation(self, state, block, process):
        now = time.time()
        if block is None or process is None:
            detail = ("Codex exited. Press X to close its saved tab, then S to restart."
                      if block else "No running Codex session. Press S to start.")
            return Observation("stopped", detail, block["block_id"] if block else None, now)
        child = process["child"]
        records = self._method(state, block["block_id"], "program_status").get("records", [])
        roots = [r for r in records if not r.get("id") and r.get("owner") ==
                 {"pid": child["pid"], "name": child["name"]}
                 and r.get("effective_app", r.get("app")) == "codex"]
        activity = roots[0].get("state") if len(roots) == 1 else "unknown"
        activity = {"done": "idle", "error": "blocked"}.get(activity, activity)
        if activity not in {"idle", "working", "blocked"}:
            activity = "unknown"
        detail = ("Rex has no verified Codex status yet. Open its tab to finish startup."
                  if activity == "unknown" else "Codex status reported through Rex; activity is not task completion.")
        return Observation(activity, detail, block["block_id"], now)

    def refresh(self, players: tuple[Player, ...]) -> dict[str, Observation]:
        with self._locked() as state:
            if state is None:
                return {p.id: self._observation(None, None, None) for p in players}
            blocks = self._snapshot(state)
            result = {}
            for player in players:
                try:
                    result[player.id] = self._observation(state, *self._bound(state, player, blocks))
                except BackendError as exc:
                    result[player.id] = Observation("unknown", str(exc), observed_at=time.time())
            return result

    def _saved_coordinator_token(self, state, coordinator):
        self._validate(state)
        binding = state["players"].get(coordinator.id)
        if (not isinstance(binding, dict) or not binding.get("nonce")
                or binding.get("cwd") != str(coordinator.cwd)):
            raise BackendError("Coach has no saved launch identity.")
        # Written before spawn so the MCP child can initialize without taking the lock.
        identity = [state[k] for k in ("project", "host_id", "instance_id", "session_id")]
        return hashlib.sha256(json.dumps([*identity, binding["nonce"]]).encode()).hexdigest()

    def coordinator_token(self, coordinator: Player) -> str:
        try:
            return self._saved_coordinator_token(json.loads(self.path.read_text()), coordinator)
        except (OSError, ValueError) as exc:
            raise BackendError("Cannot capture Coach's saved launch identity.") from exc

    def _require_coordinator(self, state, coordinator, blocks):
        player, token = coordinator
        if state is None or self._saved_coordinator_token(state, player) != token:
            raise BackendError("Coach's session changed; dispatch is blocked.")
        block, process = self._bound(state, player, blocks)
        if block is None or process is None:
            raise BackendError("Coach is no longer running; dispatch is blocked.")

    def start(self, player: Player, *, coordinator: tuple[Player, str] | None = None) -> Observation:
        with self._locked(create=coordinator is None) as state:
            if state is None:
                raise BackendError("Coach is no longer running.")
            blocks = self._snapshot(state)
            if coordinator is not None:
                self._require_coordinator(state, coordinator, blocks)
            block, process = self._bound(state, player, blocks)
            if block is not None and process is not None:
                return self._observation(state, block, process)
            if block is not None:
                raise BackendError("The previous Codex exited. Stop its saved tab before starting again.")
            binary = shutil.which("codex")
            if binary is None:
                raise BackendError("Codex is missing from PATH.")
            nonce = uuid.uuid4().hex
            binding = {"nonce": nonce, "label": f"Dreamteam / {player.name} / {nonce[:8]}",
                       "cwd": str(player.cwd), "executable": str(Path(binary).resolve()),
                       "phase": "launching"}
            state["players"][player.id] = binding
            self._save(state)
            # argv is passed directly, never interpolated into shell source.
            created = self._call(state, "run", "-s", state["session_id"], "-c", str(player.cwd),
                                 "--label", binding["label"], "--focus=false", "--keep-open",
                                 "--shell=none", "--json", "--", binding["executable"], *player.args)
            ids = created.get("block_ids")
            if (not isinstance(ids, list) or len(ids) != 1
                    or not isinstance(ids[0], str) or not ids[0].startswith("block:")):
                raise BackendError("Rex launch is unconfirmed; refresh before acting.")
            binding["block_id"] = ids[0]
            self._save(state)
            deadline = time.monotonic() + 8
            while True:
                observation = self._observation(state, *self._bound(state, player, self._snapshot(state)))
                if observation.activity != "unknown" or time.monotonic() >= deadline:
                    return observation
                time.sleep(0.2)

    def read_output(self, player: Player, lines: int = 80) -> Preview:
        if type(lines) is not int or not 1 <= lines <= 200:
            raise BackendError("Output lines must be between 1 and 200.")
        with self._locked() as state:
            if state is None:
                return Preview("", time.time())
            block, process = self._bound(state, player, self._snapshot(state))
            if block is None:
                return Preview("", time.time())
            output = self._call(state, "capture", "-s", state["session_id"], "-b", block["block_id"],
                                "--trim", "--unwrap", raw=True)
            after, _ = self._bound(state, player, self._snapshot(state))
            if after is None:
                raise BackendError("The player disappeared while reading output.")
            return Preview("\n".join(_plain_terminal_text(output).splitlines()[-lines:])[-32768:], time.time())

    def preview(self, player: Player) -> Preview:
        return self.read_output(player)

    def send_prompt(self, player: Player, text: str, *, coordinator: tuple[Player, str] | None = None) -> dict:
        if (not isinstance(text, str) or not text.strip() or len(text) > 32768
                or any(unicodedata.category(c) in {"Cc", "Cs"} and c not in "\n\t" for c in text)):
            raise PromptRejected("A prompt must contain 1–32768 characters without terminal controls.")
        attempted = False
        try:
            with self._locked() as state:
                if state is None:
                    raise PromptRejected("Start the player before sending input.")
                blocks = self._snapshot(state)
                if coordinator is not None:
                    self._require_coordinator(state, coordinator, blocks)
                block, process = self._bound(state, player, blocks)
                if self._observation(state, block, process).activity != "idle":
                    raise PromptRejected("The player is not confirmed idle; inspect its Rex tab.")
                if (process.get("foreground") or {}).get("pid") != process["child"]["pid"]:
                    raise PromptRejected("Codex does not own the foreground; no input was sent.")
                modes = self._method(state, block["block_id"], "modes").get("modes", [])
                if not any(m.get("name") == "bracketed_paste" and m.get("set") is True for m in modes):
                    raise PromptRejected("Codex is not ready for bracketed paste; no input was sent.")
                # One queued write frames multiline text as a paste, then submits it.
                payload = "\x1b[200~" + text + "\x1b[201~\r"
                attempted = True
                receipt = self._method(state, block["block_id"], "write", {"data": payload})
                if receipt.get("bytes_written") != len(payload.encode()):
                    raise PromptUncertain("Rex did not confirm the whole input; do not resend.")
                after, live = self._bound(state, player, self._snapshot(state))
                if after is None or live is None:
                    raise PromptUncertain("The process ended after input; do not resend.")
                return {"delivery": "submitted", "player_id": player.id,
                        "pane_id": block["block_id"], "acknowledged_at": time.time(),
                        "note": "Rex queued the bytes; this does not prove Codex consumed the assignment."}
        except (PromptRejected, PromptUncertain):
            raise
        except Exception as exc:
            if attempted:
                raise PromptUncertain(f"Delivery is uncertain; do not resend. {exc}") from exc
            raise PromptRejected(f"Prompt was not sent: {exc}") from exc

    def open(self, player: Player) -> str:
        with self._locked() as state:
            if state is None:
                raise BackendError("Start the player first.")
            block, _ = self._bound(state, player, self._snapshot(state))
            if block is None:
                raise BackendError("No saved Rex tab. Start the player first.")
            self._call(state, "focus", "-s", state["session_id"], block["block_id"], raw=True)
            return (f"Selected {player.name}'s native Rex tab in {state['session_id']}. "
                    "Use Rex's tabs to return to Courtside.")

    def _stop(self, state, player):
        block, _ = self._bound(state, player, self._snapshot(state))
        if block is not None:
            if block["block_id"] == os.environ.get("REX_BLOCK"):
                raise BackendError("Refusing to close the current terminal.")
            problem = None
            try:
                self._call(state, "block", "close", "-s", state["session_id"], block["block_id"], raw=True)
            except BackendError as exc:
                problem = exc
            if any(b.get("block_id") == block["block_id"] for b in self._snapshot(state)):
                raise BackendError(f"Player closure is unconfirmed; bindings retained. {problem or ''}")
        state["players"].pop(player.id, None)
        self._save(state)

    def stop(self, player: Player) -> None:
        with self._locked() as state:
            if state is not None:
                self._stop(state, player)

    def shutdown(self, players: tuple[Player, ...]) -> str:
        with self._locked() as state:
            if state is None:
                return "No Dreamteam Rex tabs to close."
            roster = {p.id: p for p in players}
            blocks = self._snapshot(state)
            for key in state["players"]:
                if key not in roster:
                    raise BackendError("Unknown saved player; cleanup requires inspection.")
                self._bound(state, roster[key], blocks)
            # Stop Coach first so its MCP process cannot create more players.
            for key in sorted(list(state["players"]), key=lambda key: key != "coach"):
                self._stop(state, roster[key])
            self.path.unlink()
            return "Closed managed Rex tabs. Your Rex server, session, and other tabs remain running."
