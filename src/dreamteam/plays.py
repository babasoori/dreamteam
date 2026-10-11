"""One saved coaching play, independent of the agent runtime."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory


class PlayError(ValueError):
    """A play cannot be read or created safely."""


@dataclass(frozen=True)
class Play:
    id: str
    project: Path
    goal: str
    constraints: str
    acceptance: str
    created_at: str
    status: str = "planned"
    ball_holder: str | None = None


def brief_text(play: Play) -> str:
    """Derive Penny's undelivered, plan-only brief from the saved play."""
    return (
        f"# Play {play.id} — Penny's planning brief\n\n"
        "Prepared for manual delivery by the coach. Preparing this brief does not "
        "send it to a player or assign possession.\n\n"
        "You are Penny, the point guard (PG). Plan the work and prepare clear handoffs.\n\n"
        f"Project: {play.project}\n\n"
        f"## Goal\n\n{play.goal}\n\n"
        f"## Constraints\n\n{play.constraints or 'No additional constraints specified.'}\n\n"
        f"## Acceptance criteria\n\n{play.acceptance}\n\n"
        "## On receipt\n\n"
        "Acknowledge receipt, then wait for the coach's explicit go-ahead before "
        "inspecting or planning.\n\n"
        "## First assignment after go-ahead: plan only\n\n"
        "Read the project's instructions and inspect the existing code. Confirm that "
        "you are working in the project directory above. Propose the smallest useful "
        "implementation, a handoff for Air Jordan, and focused checks for the "
        "acceptance criteria. Identify any missing information or blockers.\n\n"
        "Do not edit files, install dependencies, connect to external services, or "
        "perform trading operations. Implementation is outside this assignment and "
        "requires a separate assignment from the coach. Agent activity is not "
        "completion; the coach decides acceptance.\n"
    )


def _text(value: str, label: str, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise PlayError(f"{label} must be {'non-empty ' if required else ''}text.")
    if any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) <= 159 for char in value):
        raise PlayError(f"{label} cannot contain terminal control characters.")
    return value.strip()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate state field: {key}")
        result[key] = value
    return result


class PlayStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = self.root / ".dreamteam" / "possessions"
        self.play_directory = self.directory / "001"
        self.path = self.play_directory / "state.json"

    def brief_path(self, play: Play) -> Path:
        if play.id != "001":
            raise PlayError("This milestone supports only play 001.")
        return self.play_directory / "brief.md"

    def load(self) -> Play | None:
        if not os.path.lexists(self.play_directory):
            return None
        try:
            state = json.loads(self.path.read_text(), object_pairs_hook=_unique_object)
            fields = {
                "version", "id", "project", "goal", "constraints", "acceptance",
                "created_at", "status", "ball_holder",
            }
            if not isinstance(state, dict) or set(state) != fields:
                raise ValueError("Unexpected state fields.")
            if type(state["version"]) is not int or state["version"] != 1:
                raise ValueError("Unsupported state version.")
            if state["id"] != "001" or state["status"] != "planned" or state["ball_holder"] is not None:
                raise ValueError("Unsupported play state.")
            project = Path(_text(state["project"], "Project"))
            if not project.is_absolute() or "\0" in str(project):
                raise ValueError("Saved project must be a valid absolute path.")
            created_at = _text(state["created_at"], "Creation time")
            if datetime.fromisoformat(created_at).tzinfo is None:
                raise ValueError("Creation time needs a timezone.")
            return Play(
                id=state["id"], project=project,
                goal=_text(state["goal"], "Goal"),
                constraints=_text(state["constraints"], "Constraints", required=False),
                acceptance=_text(state["acceptance"], "Acceptance criteria"),
                created_at=created_at,
            )
        except (OSError, ValueError, TypeError) as exc:
            raise PlayError(
                f"Cannot read saved play at {self.path}. Existing files were left untouched: {exc}"
            ) from exc

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".create.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PlayError("Another play is being created. Refresh before trying again.") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def create(self, project: str | Path, goal: str, constraints: str, acceptance: str) -> Play:
        if not isinstance(project, (str, Path)) or not str(project).strip():
            raise PlayError("Project must be an existing directory.")
        try:
            project_path = Path(project).expanduser()
            if not project_path.is_absolute():
                project_path = self.root / project_path
            project_path = project_path.resolve()
            if not project_path.is_dir():
                raise PlayError(f"Project directory does not exist: {project_path}")
        except (OSError, RuntimeError, ValueError) as exc:
            raise PlayError(f"Cannot use project directory: {exc}") from exc
        play = Play(
            id="001", project=project_path,
            goal=_text(goal, "Goal"),
            constraints=_text(constraints, "Constraints", required=False),
            acceptance=_text(acceptance, "Acceptance criteria"),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        try:
            with self._locked():
                if os.path.lexists(self.play_directory):
                    self.load()  # Preserve and explain malformed state instead of replacing it.
                    raise PlayError("Play 001 already exists. Reopen its brief; a second play is not supported yet.")
                state = {"version": 1, **asdict(play), "project": str(play.project)}
                with TemporaryDirectory(prefix=".001-", dir=self.directory) as staging:
                    staged = Path(staging)
                    for name, content in (
                        ("state.json", json.dumps(state, indent=2) + "\n"),
                        ("brief.md", brief_text(play)),
                    ):
                        with (staged / name).open("w") as destination:
                            destination.write(content)
                            destination.flush()
                            os.fsync(destination.fileno())
                    staged.rename(self.play_directory)
            return play
        except OSError as exc:
            raise PlayError(f"Could not save the play: {exc}") from exc
