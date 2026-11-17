"""Launch Dreamteam in Rex's native UI; no dashboard or event loop."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .coordination import Team, coach_player
from .plays import PlayStore, brief_text
from .roster import load_roster
from .runtime import BackendError


def main() -> None:
    parser = argparse.ArgumentParser(description="Coach on the left, players on the right, in one native Rex tab.")
    parser.add_argument("--config", type=Path, default=Path.cwd() / "dreamteam.toml",
                        help="Roster and saved-play location")
    parser.add_argument("--view", metavar="PLAYER", help="Select an existing pane (same as open PLAYER)")
    commands = parser.add_subparsers(dest="action")
    start = commands.add_parser("start", help="Start/reuse Coach and an optional player")
    start.add_argument("player", nargs="?", default="coach")
    view = commands.add_parser("open", help="Focus an existing pane without starting an agent")
    view.add_argument("player", nargs="?", default="coach")
    commands.add_parser("status", help="Print roster activity and assignments as JSON")
    stop = commands.add_parser("stop", help="Stop a managed player, Coach, or the whole team")
    stop.add_argument("player", help="Player ID, coach, or team")
    play = commands.add_parser("play", help="Save a play without launching agents")
    play.add_argument("--project", type=Path, required=True)
    play.add_argument("--goal", required=True)
    play.add_argument("--constraints", default="")
    play.add_argument("--acceptance", required=True)
    commands.add_parser("brief", help="Print the saved planning brief")
    args = parser.parse_args()
    if args.view and args.action:
        parser.error("Use --view or a subcommand, not both.")
    config = args.config.expanduser().resolve()
    try:
        load_roster(config)
        store = PlayStore(config.parent)
        if args.action == "play":
            result = asdict(store.create(args.project, args.goal, args.constraints, args.acceptance))
            result["project"] = str(result["project"])
            print(json.dumps(result, indent=2))
            return
        saved = store.load()
        if args.action == "brief":
            if saved is None:
                raise ValueError("No play is saved. Ask Coach to define one or use dreamteam play.")
            print(brief_text(saved))
            return
        project = saved.project if saved else config.parent
        team = Team(config, project)
        coach = coach_player(config, project)
        action = "open" if args.view else (args.action or "start")
        player_id = args.view or getattr(args, "player", "coach")
        if action == "status":
            result = team.status()
            result["coach"] = asdict(team.backend.refresh((coach,))[coach.id])
            print(json.dumps(result, indent=2))
            return
        if action == "stop" and player_id == "team":
            print(team.backend.shutdown((coach, *team.players)))
            return
        player = coach if player_id == "coach" else team.player(player_id)
        if action == "stop":
            team.backend.stop(player)
            print(f"Stopped {player.name}'s managed pane.")
        elif action == "open":
            print(team.backend.open(player))
        else:
            observation = team.backend.start(coach)
            if player.id != "coach":
                observation = team.backend.start(player)
            if not observation.pane_id:
                raise BackendError(f"{player.name} did not start: {observation.detail}")
            print(team.backend.open(player))
            print(f"{player.name}: {observation.detail}")
            print("Use Rex's sidebar and pane navigation. Closing this launcher does not stop the team.")
    except (OSError, ValueError, BackendError) as exc:
        print(f"dreamteam: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
