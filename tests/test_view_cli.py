"""Separate native viewer entrypoint; no real terminals or agent sessions are launched."""

import contextlib
import io
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from dreamteam.app import main, view_player
from dreamteam.plays import PlayStore
from dreamteam.roster import Player
from dreamteam.runtime import BackendError, Herdr


class ViewCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.control = self.root / "controller"
        self.control.mkdir()
        self.project = self.root / "fastbreak"
        self.project.mkdir()
        self.config = self.control / "dreamteam.toml"
        self.config.write_text(
            'version = 1\n[players.penny]\nname = "Penny"\n'
            'position = "pg"\nagent = "codex"\ncwd = "."\n'
        )
        self.backend = Mock(spec=Herdr)
        self.player = Player("penny", "Penny", "pg", self.project)
        self.command = ["herdr", "--session", "dreamteam-test", "agent", "attach", "w1:p1"]
        self.environment = {"HERDR_CONFIG_PATH": "/unused/herdr.toml"}
        self.backend.attachment.return_value = self.command, self.environment

    def arguments(self, *extra):
        return patch("sys.argv", ["dreamteam", "--config", str(self.config), *extra])

    def test_explicit_config_resolves_viewer_independently_of_launch_directory(self):
        for player_id in ("penny", "coach"):
            with self.subTest(player=player_id):
                with self.arguments("--view", player_id), patch(
                    "dreamteam.app.Herdr", return_value=self.backend
                ) as factory, patch("dreamteam.app.view_player", return_value=0) as viewer, patch(
                    "dreamteam.app.CourtsideApp.run"
                ) as run:
                    with self.assertRaises(SystemExit) as raised:
                        main()
                    self.assertEqual(raised.exception.code, 0)
                factory.assert_called_once_with(self.control)
                viewer.assert_called_once()
                self.assertIs(viewer.call_args.args[0], self.backend)
                player = viewer.call_args.args[1]
                self.assertEqual((player.id, player.cwd), (player_id, self.control))
                run.assert_not_called()
                self.backend.start.assert_not_called()

    def test_saved_play_routes_viewer_to_same_project_as_courtside(self):
        PlayStore(self.control).create(
            self.project, "Build a wrapper.", "No broker connection.", "Offline checks pass."
        )
        original, target = Mock(spec=Herdr), Mock(spec=Herdr)
        with self.arguments("--view", "penny"), patch(
            "dreamteam.app.Herdr", side_effect=[original, target]
        ) as factory, patch("dreamteam.app.view_player", return_value=0) as viewer, patch(
            "dreamteam.app.CourtsideApp.run"
        ) as run:
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, 0)
        self.assertEqual([call.args[0] for call in factory.call_args_list], [self.control, self.project])
        self.assertIs(viewer.call_args.args[0], target)
        self.assertEqual(viewer.call_args.args[1].cwd, self.project)
        original.attachment.assert_not_called()
        original.start.assert_not_called()
        target.start.assert_not_called()
        run.assert_not_called()

    def test_unknown_player_is_rejected_without_opening_view(self):
        errors = io.StringIO()
        with self.arguments("--view", "allen_iverson"), patch(
            "dreamteam.app.Herdr", return_value=self.backend
        ), patch("dreamteam.app.view_player") as viewer, contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Unknown player. Choose: coach, penny", errors.getvalue())
        viewer.assert_not_called()
        self.backend.attachment.assert_not_called()
        self.backend.start.assert_not_called()

    def test_missing_config_and_missing_play_project_fail_before_attachment(self):
        cases = ("missing config", "missing project")
        for failure in cases:
            with self.subTest(failure=failure):
                if failure == "missing config":
                    self.config.unlink()
                else:
                    self.config.write_text(
                        'version = 1\n[players.penny]\nname = "Penny"\n'
                        'position = "pg"\nagent = "codex"\ncwd = "."\n'
                    )
                    PlayStore(self.control).create(
                        self.project, "Build a wrapper.", "", "Offline checks pass."
                    )
                    self.project.rmdir()
                errors = io.StringIO()
                with self.arguments("--view", "penny"), patch(
                    "dreamteam.app.Herdr", return_value=self.backend
                ), patch("dreamteam.app.view_player") as viewer, contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as raised:
                        main()
                self.assertEqual(raised.exception.code, 2)
                viewer.assert_not_called()
                self.backend.attachment.assert_not_called()
                self.backend.start.assert_not_called()
                self.assertIn("dreamteam:", errors.getvalue())

    def test_detach_exits_viewer_without_reconnect_prompt_or_stopping_player(self):
        with patch(
            "dreamteam.app.subprocess.run", return_value=subprocess.CompletedProcess(self.command, 0)
        ) as run, patch("builtins.input") as prompt:
            self.assertEqual(view_player(self.backend, self.player), 0)
        self.backend.attachment.assert_called_once_with(self.player)
        run.assert_called_once_with(self.command, env=self.environment, check=False)
        prompt.assert_not_called()
        self.assertNotIn("--takeover", run.call_args.args[0])
        self.backend.start.assert_not_called()
        self.backend.stop.assert_not_called()
        self.backend.shutdown.assert_not_called()

    def test_failed_attachment_exits_once_with_guidance_and_no_takeover(self):
        errors = io.StringIO()
        with patch(
            "dreamteam.app.subprocess.run", return_value=subprocess.CompletedProcess(self.command, 1)
        ) as run, patch("builtins.input") as prompt, contextlib.redirect_stderr(errors):
            self.assertEqual(view_player(self.backend, self.player), 1)
        self.backend.attachment.assert_called_once_with(self.player)
        run.assert_called_once_with(self.command, env=self.environment, check=False)
        prompt.assert_not_called()
        self.assertIn("detach that view first", errors.getvalue())
        self.assertNotIn("--takeover", run.call_args.args[0])
        self.backend.start.assert_not_called()
        self.backend.stop.assert_not_called()

    def test_unready_player_exits_without_starting_or_waiting_for_input(self):
        self.backend.attachment.side_effect = BackendError("Penny is not ready.")
        errors = io.StringIO()
        with patch("dreamteam.app.subprocess.run") as run, patch(
            "builtins.input"
        ) as prompt, contextlib.redirect_stderr(errors):
            self.assertEqual(view_player(self.backend, self.player), 1)
        self.backend.attachment.assert_called_once_with(self.player)
        run.assert_not_called()
        prompt.assert_not_called()
        self.backend.start.assert_not_called()
        self.backend.stop.assert_not_called()
        self.assertIn("Penny is not ready.", errors.getvalue())

    def test_keyboard_interrupt_ends_viewer_without_reconnect_or_team_cleanup(self):
        with patch(
            "dreamteam.app.subprocess.run", side_effect=KeyboardInterrupt
        ), patch("builtins.input") as prompt:
            self.assertEqual(view_player(self.backend, self.player), 130)
        prompt.assert_not_called()
        self.backend.stop.assert_not_called()
        self.backend.shutdown.assert_not_called()

    def test_main_propagates_viewer_exit_status(self):
        with self.arguments("--view", "penny"), patch(
            "dreamteam.app.Herdr", return_value=self.backend
        ), patch("dreamteam.app.view_player", return_value=1), patch(
            "dreamteam.app.CourtsideApp.run"
        ) as run:
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
