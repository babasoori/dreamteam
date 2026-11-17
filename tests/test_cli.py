"""Native launcher and saved-play routing; no terminal UI or real agents."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch, call

from dreamteam.app import main
from dreamteam.coordination import coach_player
from dreamteam.plays import PlayStore
from dreamteam.roster import Player
from dreamteam.runtime import BackendError, Observation, Rex


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="dreamteam launcher ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "dreamteam.toml"
        self.config.write_text('version=1\n[players.penny]\nname="Penny"\nposition="pg"\nagent="codex"\ncwd="."\n')
        self.player = Player("penny", "Penny", "pg", self.root)
        self.backend = Mock(spec=Rex)
        self.backend.start.side_effect = lambda p: Observation("idle", "Ready", "block:" + p.id)
        self.backend.open.side_effect = lambda p: "Selected " + p.name
        self.backend.refresh.side_effect = lambda players: {p.id: Observation("idle", "Ready") for p in players}
        self.backend.shutdown.return_value = "Closed managed panes."
        self.team = Mock()
        self.team.backend = self.backend
        self.team.players = (self.player,)
        def player(key):
            if key != "penny":
                raise ValueError("Unknown player")
            return self.player
        self.team.player.side_effect = player
        self.team.status.return_value = {"players": [], "tasks": []}
        self.factory = self.enterContext(patch("dreamteam.app.Team", return_value=self.team))

    def run_cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["dreamteam", "--config", str(self.config), *args]), redirect_stdout(output), redirect_stderr(errors):
            try:
                main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), errors.getvalue()

    def test_default_starts_and_focuses_coach_then_returns(self):
        code, output, _ = self.run_cli()
        self.assertEqual(code, 0)
        coach = coach_player(self.config, self.root)
        self.assertEqual(self.backend.mock_calls[:2], [call.start(coach), call.open(coach)])
        self.assertIn("sidebar", output)
        self.backend.shutdown.assert_not_called()

    def test_start_player_starts_coach_first_and_focuses_requested_player(self):
        self.assertEqual(self.run_cli("start", "penny")[0], 0)
        coach = coach_player(self.config, self.root)
        self.assertEqual(self.backend.mock_calls, [call.start(coach), call.start(self.player), call.open(self.player)])

    def test_unknown_player_does_not_start_coach(self):
        self.assertEqual(self.run_cli("start", "missing")[0], 1)
        self.backend.start.assert_not_called()

    def test_open_and_view_never_spawn(self):
        for arguments in (("open", "penny"), ("--view", "penny")):
            self.assertEqual(self.run_cli(*arguments)[0], 0)
        self.assertEqual(self.backend.open.call_count, 2)
        self.backend.start.assert_not_called()

    def test_conflicting_view_and_command_are_rejected(self):
        self.assertEqual(self.run_cli("--view", "penny", "start")[0], 2)
        self.factory.assert_not_called()

    def test_status_includes_coach_without_launching(self):
        code, output, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["coach"]["activity"], "idle")
        self.backend.start.assert_not_called()
        self.backend.open.assert_not_called()

    def test_stop_player_does_not_close_team(self):
        self.assertEqual(self.run_cli("stop", "penny")[0], 0)
        self.backend.stop.assert_called_once_with(self.player)
        self.backend.shutdown.assert_not_called()
        self.backend.start.assert_not_called()

    def test_stop_team_is_explicit_and_coach_is_included(self):
        self.assertEqual(self.run_cli("stop", "team")[0], 0)
        self.backend.shutdown.assert_called_once_with((coach_player(self.config, self.root), self.player))
        self.backend.start.assert_not_called()

    def test_create_play_and_read_brief_do_not_touch_runtime(self):
        project = self.root / "target"
        project.mkdir()
        code, output, _ = self.run_cli("play", "--project", "target", "--goal", "Inspect code",
                                      "--constraints", "Read only", "--acceptance", "Review plan")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["project"], str(project))
        code, output, _ = self.run_cli("brief")
        self.assertEqual(code, 0)
        self.assertIn("Inspect code", output)
        self.assertIn("Read only", output)
        self.factory.assert_not_called()

    def test_saved_play_selects_project_independent_of_cwd(self):
        project = self.root / "target"
        project.mkdir()
        PlayStore(self.root).create(project, "Inspect", "", "Reviewed")
        self.assertEqual(self.run_cli()[0], 0)
        self.factory.assert_called_once_with(self.config, project)
        self.assertEqual(self.backend.start.call_args.args[0].cwd, project)

    def test_invalid_play_does_not_launch(self):
        code, _, _ = self.run_cli("play", "--project", "missing", "--goal", "Inspect", "--acceptance", "Reviewed")
        self.assertEqual(code, 1)
        self.factory.assert_not_called()

    def test_missing_config_does_not_launch(self):
        self.config.unlink()
        self.assertEqual(self.run_cli()[0], 1)
        self.factory.assert_not_called()

    def test_unconfirmed_launch_does_not_focus_or_retry(self):
        self.backend.start.side_effect = BackendError("Launch unconfirmed")
        code, _, error = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("unconfirmed", error)
        self.backend.start.assert_called_once()
        self.backend.open.assert_not_called()

    def test_unknown_activity_still_opens_native_pane_for_startup(self):
        self.backend.start.side_effect = lambda p: Observation("unknown", "Complete startup", "block:coach")
        self.assertEqual(self.run_cli()[0], 0)
        self.backend.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
