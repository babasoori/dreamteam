"""Open native player viewers beside Courtside without taking over its terminal."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import time

from .roster import Player
from .runtime import BackendError


class NativeViews:
    def __init__(self, controller_root: Path, environment: dict | None = None):
        self.root = controller_root.resolve()
        self.directory = self.root / ".dreamteam"
        self.path = self.directory / "views.json"
        self.environment = dict(os.environ if environment is None else environment)
        self._caller = None
        self._host = None

    def _call(self, *args: str) -> dict:
        binary = shutil.which("herdr", path=self.environment.get("PATH"))
        if binary is None:
            raise BackendError("Herdr is missing from PATH.")
        try:
            result = subprocess.run(
                [binary, *args], env=self.environment, cwd=self.root,
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError("Herdr timed out; viewer creation or delivery may have occurred.") from exc
        except OSError as exc:
            raise BackendError(f"Could not run Herdr: {exc}") from exc
        output = result.stdout if result.returncode == 0 else result.stderr
        # Herdr command acknowledgements may have empty stdout; closure is verified separately.
        if result.returncode == 0 and args[:2] in (("pane", "run"), ("pane", "close")) and not output.strip():
            return {}
        try:
            response = json.loads(output)
        except ValueError as exc:
            raise BackendError(f"Herdr returned no valid JSON: {output.strip()[:300]}") from exc
        if not isinstance(response, dict):
            raise BackendError("Herdr returned an unexpected response.")
        if result.returncode or "error" in response:
            error = response.get("error", {})
            raise BackendError(f"Herdr: {error.get('code', 'error')}: {error.get('message', output[:300])}")
        return response

    def _socket(self) -> dict:
        path = self.environment.get("HERDR_SOCKET_PATH")
        if self.environment.get("HERDR_ENV") != "1" or not path or not self.environment.get("HERDR_PANE_ID"):
            raise BackendError(
                "Side-by-side views need Courtside inside Herdr. "
                "Use --view in a separate terminal tab, or launch Courtside inside Herdr."
            )
        info = self._call("status", "server", "--json")
        try:
            socket_path = Path(path).resolve(strict=True)
            if not info.get("running") or not info.get("compatible") or Path(info["socket"]).resolve() != socket_path:
                raise ValueError("different or unavailable server")
            details = socket_path.stat()
            if not stat.S_ISSOCK(details.st_mode):
                raise ValueError("not a socket")
        except (KeyError, TypeError, OSError, ValueError) as exc:
            raise BackendError("Cannot verify the Herdr server containing Courtside.") from exc
        return {"path": str(socket_path), "device": details.st_dev,
                "inode": details.st_ino, "created_ns": details.st_ctime_ns}

    @staticmethod
    def _identity(pane: dict) -> dict:
        keys = ("pane_id", "terminal_id", "workspace_id", "tab_id")
        if not isinstance(pane, dict) or not all(isinstance(pane.get(key), str) and pane[key] for key in keys):
            raise BackendError("Herdr returned an incomplete pane identity.")
        return {key: pane[key] for key in keys}

    def _context(self) -> tuple[dict, dict, dict]:
        host = self._socket()
        try:
            caller = self._call("pane", "current", "--current")["result"]["pane"]
            snapshot = self._call("api", "snapshot")["result"]["snapshot"]
            panes = snapshot["panes"]
            if not isinstance(panes, list) or not isinstance(snapshot["layouts"], list):
                raise ValueError("invalid snapshot")
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendError("Cannot verify Courtside's current pane and layout.") from exc
        identity = self._identity(caller)
        for key, variable in (("pane_id", "HERDR_PANE_ID"), ("tab_id", "HERDR_TAB_ID"),
                              ("workspace_id", "HERDR_WORKSPACE_ID")):
            if self.environment.get(variable) and self.environment[variable] != identity[key]:
                raise BackendError("The calling pane does not match Courtside's Herdr environment.")
        matches = [pane for pane in panes if pane.get("pane_id") == identity["pane_id"]]
        if len(matches) != 1 or self._identity(matches[0]) != identity:
            raise BackendError("Courtside's pane changed while checking its identity.")
        if self._socket() != host or self._host is not None and self._host != host:
            raise BackendError("The host Herdr server changed. Reopen Courtside before creating views.")
        if self._caller is not None and self._caller["terminal_id"] != identity["terminal_id"]:
            raise BackendError("Courtside's calling terminal changed. No pane was opened.")
        self._host, self._caller = host, identity
        return host, identity, snapshot

    @contextmanager
    def _locked(self):
        self.directory.mkdir(exist_ok=True)
        with (self.directory / "views.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackendError("Another Courtside window is opening a view. Try again shortly.") from exc
            try:
                if self.path.exists():
                    try:
                        state = json.loads(self.path.read_text())
                        if (not isinstance(state, dict) or state.get("version") != 1
                                or state.get("controller") != str(self.root)
                                or not isinstance(state.get("views"), dict)):
                            raise ValueError("invalid state")
                    except (OSError, ValueError) as exc:
                        raise BackendError("Cannot verify .dreamteam/views.json; saved views were left untouched.") from exc
                else:
                    state = {"version": 1, "controller": str(self.root), "views": {}}
                yield state
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self, state: dict):
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w") as destination:
            json.dump(state, destination, indent=2)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(self.path)

    def _verify_pane(self, expected: dict, host: dict):
        try:
            pane = self._call("pane", "get", expected["pane_id"])["result"]["pane"]
        except (KeyError, TypeError) as exc:
            raise BackendError("Cannot verify the viewer pane.") from exc
        if self._identity(pane) != expected or self._socket() != host:
            raise BackendError("The pane or host identity changed. No command was sent.")

    @staticmethod
    def _is_shell(info: dict, pane: dict) -> bool:
        shell = info.get("shell_pid")
        foreground = info.get("foreground_processes", [])
        return (info.get("pane_id") == pane["pane_id"]
                and isinstance(shell, int) and shell > 0
                and info.get("foreground_process_group_id") == shell
                and len(foreground) == 1 and foreground[0].get("pid") == shell)

    def _wait_for_shell(self, pane: dict, host: dict):
        deadline = time.monotonic() + 2
        while True:
            self._verify_pane(pane, host)
            try:
                info = self._call("pane", "process-info", "--pane", pane["pane_id"])["result"]["process_info"]
                available = self._is_shell(info, pane)
            except (KeyError, TypeError, AttributeError) as exc:
                raise BackendError("Cannot verify the new viewer's foreground shell.") from exc
            if available:
                return
            if time.monotonic() >= deadline:
                raise BackendError(
                    "The new viewer pane is not at an available shell. No command was sent; inspect that pane."
                )
            time.sleep(0.05)

    @staticmethod
    def _location(player: Player, pane: dict, *, existing: bool) -> str:
        prefix = "already has a viewer pane" if existing else "viewer pane opened"
        return (f"{player.name}: {prefix} in {pane['pane_id']} (tab {pane['tab_id']}). "
                "Courtside stays here; click the pane or use Herdr's pane navigation.")

    def open(self, player: Player, command: list[str]) -> str:
        if not command or not command[0] or not all(isinstance(arg, str) and "\0" not in arg for arg in command):
            raise BackendError("The viewer command is invalid.")
        if (self.environment.get("HERDR_ENV") != "1" or not self.environment.get("HERDR_SOCKET_PATH")
                or not self.environment.get("HERDR_PANE_ID")):
            self._socket()  # Gives manual guidance before creating any state.
        # Read the host inside the same lock as the intent: two Courtside windows
        # must not both decide that the player has no viewer.
        with self._locked() as state:
            host, caller, snapshot = self._context()
            key = json.dumps([host["path"], str(player.cwd.resolve()), player.id])
            saved = state["views"].get(key)
            if saved is not None:
                if not isinstance(saved, dict) or saved.get("host") != host:
                    raise BackendError("The saved viewer belongs to a changed host. No replacement was opened.")
                phase = saved.get("phase")
                if phase not in ("splitting", "created", "launching", "opened"):
                    raise BackendError("The saved viewer state is invalid.")
                if phase != "opened":
                    pane_id = saved.get("pane", {}).get("pane_id", "the host layout")
                    raise BackendError(
                        f"An earlier viewer launch is unconfirmed. Inspect {pane_id}; "
                        "no duplicate pane or command was created."
                    )
                expected = saved.get("pane")
                identity = self._identity(expected)
                matches = [pane for pane in snapshot["panes"]
                           if pane.get("pane_id") == identity["pane_id"]
                           or pane.get("terminal_id") == identity["terminal_id"]]
                if matches:
                    if len(matches) != 1 or self._identity(matches[0]) != identity:
                        raise BackendError("The saved viewer pane changed identity. No replacement was opened.")
                    try:
                        info = self._call("pane", "process-info", "--pane", identity["pane_id"])["result"]["process_info"]
                        ended = self._is_shell(info, identity)
                    except (KeyError, TypeError, AttributeError) as exc:
                        raise BackendError("Cannot verify the saved viewer pane's foreground process.") from exc
                    if ended:
                        raise BackendError(
                            f"{player.name}'s viewer ended in pane {identity['pane_id']}. "
                            "Run the displayed command in that pane, or close it and press Enter again."
                        )
                    return self._location(player, identity, existing=True)
                # Only a confirmed closed viewer is eligible for replacement.
                del state["views"][key]

            layouts = [layout for layout in snapshot["layouts"] if layout.get("tab_id") == caller["tab_id"]]
            rectangles = [pane["rect"] for layout in layouts for pane in layout.get("panes", [])
                          if pane.get("pane_id") == caller["pane_id"]]
            if len(rectangles) != 1:
                raise BackendError("Cannot verify Courtside's pane dimensions.")
            width, height = rectangles[0].get("width", 0), rectangles[0].get("height", 0)
            if width >= 160 and height >= 24:
                direction = "right"
            elif width >= 80 and height >= 48:
                direction = "down"
            else:
                raise BackendError(
                    "Enlarge Courtside's pane to at least 160×24 or 80×48 for side-by-side views, "
                    "or use --view in a separate tab."
                )

            saved = {"host": host, "player": player.id, "cwd": str(player.cwd.resolve()),
                     "caller": caller, "phase": "splitting", "command": list(command)}
            state["views"][key] = saved
            self._verify_pane(caller, host)
            self._save(state)
            try:
                result = self._call("pane", "split", "--pane", caller["pane_id"],
                                    "--direction", direction, "--cwd", str(player.cwd), "--no-focus")
                pane = self._identity(result["result"]["pane"])
            except (KeyError, TypeError) as exc:
                raise BackendError("Viewer creation is unconfirmed. No duplicate will be created.") from exc
            saved.update(phase="created", pane=pane)
            self._save(state)
            if (pane["pane_id"] == caller["pane_id"] or pane["terminal_id"] == caller["terminal_id"]
                    or pane["tab_id"] != caller["tab_id"] or pane["workspace_id"] != caller["workspace_id"]):
                raise BackendError("The new pane was not a sibling. No command was sent.")
            self._wait_for_shell(pane, host)
            saved["phase"] = "launching"
            self._save(state)
            self._call("pane", "run", pane["pane_id"], shlex.join(command))
            saved["phase"] = "opened"
            self._save(state)
            return self._location(player, pane, existing=False)

    def _viewer_process(self, process: dict, saved: dict) -> bool:
        argv = process.get("argv")
        if (not isinstance(argv, list) or len(argv) != 7
                or not all(isinstance(arg, str) for arg in argv)
                or argv[1:4] != ["-m", "dreamteam.app", "--config"]
                or argv[5:] != ["--view", saved.get("player")]):
            return False
        if (not Path(argv[0]).name.lower().startswith("python")
                or Path(argv[4]).resolve().parent != self.root):
            return False
        command = saved.get("command")
        return (command is None or
                isinstance(command, list) and command[1:] == argv[1:]
                and Path(command[0]).resolve() == Path(argv[0]).resolve())

    def _closeable_process(self, info: dict, pane: dict, saved: dict) -> bool:
        if self._is_shell(info, pane):
            return True
        processes = info.get("foreground_processes", [])
        if info.get("pane_id") != pane["pane_id"] or not processes:
            return False
        leader = [process for process in processes
                  if process.get("pid") == info.get("foreground_process_group_id")]
        if len(leader) != 1 or not self._viewer_process(leader[0], saved):
            return False
        for process in processes:
            if self._viewer_process(process, saved):
                continue
            argv = process.get("argv", [])
            # A native attachment shares the viewer's foreground process group.
            if not (len(argv) == 6 and Path(argv[0]).name == "herdr"
                    and argv[1] == "--session" and argv[2].startswith("dreamteam-")
                    and argv[3:5] == ["agent", "attach"]):
                return False
        return True

    def _close_snapshot(self, client: "NativeViews", host: dict) -> list[dict]:
        if client._socket() != host:
            raise BackendError("host socket identity changed")
        try:
            panes = client._call("api", "snapshot")["result"]["snapshot"]["panes"]
            if not isinstance(panes, list) or not all(isinstance(pane, dict) for pane in panes):
                raise ValueError("invalid panes")
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendError("cannot verify host panes") from exc
        if client._socket() != host:
            raise BackendError("host changed while checking panes")
        return panes

    def _close_candidate(self, client: "NativeViews", saved: dict) -> dict | None:
        pane = self._identity(saved.get("pane"))
        caller = self._identity(saved.get("caller"))
        if pane["pane_id"] == caller["pane_id"] or pane["terminal_id"] == caller["terminal_id"]:
            raise BackendError("refusing to close the recorded Courtside terminal")
        host = saved["host"]
        current_socket = self.environment.get("HERDR_SOCKET_PATH")
        if (self.environment.get("HERDR_ENV") == "1" and current_socket
                and Path(current_socket).resolve() == Path(host["path"]).resolve()):
            current = self._identity(self._call("pane", "current", "--current")["result"]["pane"])
            if pane["pane_id"] == current["pane_id"] or pane["terminal_id"] == current["terminal_id"]:
                raise BackendError("refusing to close the current Courtside terminal")
        matches = [p for p in self._close_snapshot(client, host)
                   if p.get("pane_id") == pane["pane_id"] or p.get("terminal_id") == pane["terminal_id"]]
        if not matches:
            return None
        if len(matches) != 1 or self._identity(matches[0]) != pane:
            raise BackendError("pane identity changed")
        try:
            info = client._call("pane", "process-info", "--pane", pane["pane_id"])["result"]["process_info"]
            closeable = self._closeable_process(info, pane, saved)
        except (KeyError, TypeError, AttributeError, IndexError) as exc:
            raise BackendError("cannot identify the foreground viewer process") from exc
        if not closeable:
            raise BackendError("another foreground process is using the pane")
        return pane

    def close_all(self) -> str:
        """Close verified owned viewers; preserve ambiguous or repurposed panes."""
        if not self.path.exists():
            return "No Dreamteam viewer panes to close."
        with self._locked() as state:
            candidates = []
            leftovers = []
            # Check every binding before changing any pane.
            for key, saved in state["views"].items():
                label = saved.get("player", key) if isinstance(saved, dict) else key
                try:
                    if not isinstance(saved, dict) or not isinstance(saved.get("host"), dict):
                        raise BackendError("invalid saved viewer identity")
                    host = saved["host"]
                    if not saved.get("pane"):
                        raise BackendError(f"unconfirmed split on {host.get('path')}; inspect the recorded caller pane")
                    environment = {key: value for key, value in self.environment.items()
                                   if not key.startswith("HERDR_")}
                    environment.update(HERDR_ENV="1", HERDR_SOCKET_PATH=host["path"],
                                       HERDR_PANE_ID=saved["caller"]["pane_id"])
                    client = NativeViews(self.root, environment)
                    self._close_candidate(client, saved)
                    candidates.append((key, saved, client))
                except (BackendError, KeyError, TypeError, ValueError) as exc:
                    leftovers.append(f"{label}: {exc}")
            closed = 0
            for key, saved, client in candidates:
                label = f"{saved.get('player', '?')} ({saved['pane']['pane_id']})"
                try:
                    pane = self._close_candidate(client, saved)
                    if pane is not None:
                        client._verify_pane(pane, saved["host"])
                        close_error = None
                        try:
                            client._call("pane", "close", pane["pane_id"])
                        except BackendError as exc:
                            close_error = exc
                        remaining = self._close_snapshot(client, saved["host"])
                        if any(p.get("pane_id") == pane["pane_id"]
                               or p.get("terminal_id") == pane["terminal_id"] for p in remaining):
                            raise BackendError(str(close_error) if close_error else "closure was not confirmed")
                        closed += 1
                    del state["views"][key]
                    self._save(state)
                except (BackendError, KeyError, TypeError, ValueError) as exc:
                    leftovers.append(f"{label}: {exc}")
            if leftovers:
                raise BackendError(
                    f"Closed {closed} viewer panes. Remaining views need attention: "
                    + "; ".join(leftovers)
                )
            return f"Closed {closed} Dreamteam viewer panes."
