"""The local, Codex-only starting five and sixth man."""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


ROLES = {
    "pg": "Plan the work and prepare clear handoffs.",
    "sg": "Implement the agreed change.",
    "sf": "Connect interfaces and check the end-to-end flow.",
    "pf": "Run focused checks and reproduce failures.",
    "c": "Independently review the exact candidate.",
    "bench": "Bring a fresh angle to a blocker when the coach calls.",
}


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    position: str
    cwd: Path
    args: tuple[str, ...] = ()
    role: str = ""


def load_roster(path: Path) -> tuple[Player, ...]:
    with path.open("rb") as source:
        config = tomllib.load(source)
    if config.get("version") != 1:
        raise ValueError("dreamteam.toml must specify version = 1.")
    entries = config.get("players")
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 6:
        raise ValueError("Define between one and six players.")
    players = []
    positions = set()
    for player_id, entry in entries.items():
        if player_id == "coach":
            raise ValueError("coach is reserved for the coordinating session; keep it outside the player roster.")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", player_id):
            raise ValueError(f"Invalid player ID: {player_id}")
        if not isinstance(entry, dict) or entry.get("agent") != "codex":
            raise ValueError(f"{player_id}: this roster supports agent = 'codex'.")
        position = entry.get("position")
        if not isinstance(position, str) or position not in ROLES or position in positions:
            raise ValueError(f"{player_id}: use a distinct pg, sg, sf, pf, c, or bench position.")
        positions.add(position)
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 for c in name):
            raise ValueError(f"{player_id}: provide a printable player name.")
        cwd = entry.get("cwd", ".")
        args = entry.get("args", [])
        if not isinstance(cwd, str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError(f"{player_id}: cwd must be text and args a list of strings.")
        directory = (path.parent / cwd).resolve()
        if not directory.is_dir():
            raise ValueError(f"{player_id}: directory does not exist: {directory}")
        players.append(Player(player_id, name.strip(), position, directory, tuple(args), ROLES[position]))
    return tuple(players)
