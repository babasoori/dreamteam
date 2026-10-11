"""Headless coaching flows; fake runtimes never launch agents or connect to brokers."""

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import tomllib
import unittest
from unittest.mock import Mock, call, patch

from textual.widgets import Button, Input, Static, TextArea

from dreamteam.app import CourtsideApp, NewPlay, PlayBrief, QuitTeam
from dreamteam.plays import PlayError, PlayStore, brief_text
from dreamteam.roster import Player
from dreamteam.runtime import Herdr, Observation, Preview


class PlayAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.control = self.root / "dreamteam"
        self.project = self.root / "fastbreak"
        self.control.mkdir()
        self.project.mkdir()
        self.store = PlayStore(self.control)
        self.players = tuple(
            Player(player_id, name, position, self.control, role="A focused role.")
            for player_id, name, position in (
                ("penny", "Penny", "pg"),
                ("air_jordan", "Air Jordan", "sg"),
                ("king_james", "King James", "sf"),
                ("sir_charles", "Sir Charles", "pf"),
                ("shaq", "Shaq", "c"),
                ("the_answer", "The Answer", "bench"),
            )
        )
        self.original = self.backend("idle", "old-project-pane")
        self.target = self.backend("stopped")
        self.factory = self.enterContext(patch("dreamteam.app.Herdr", return_value=self.target))

    def backend(self, activity, pane_id=None):
        backend = Mock(spec=Herdr)
        backend.refresh.side_effect = lambda players: {
            player.id: Observation(activity, "Fake observation.", pane_id, time.time())
            for player in players
        }
        backend.preview.side_effect = lambda player: Preview(f'{player.name} output.', time.time())
        backend.shutdown.return_value = "Backend stopped."
        return backend

    def make_app(self):
        views = Mock()
        views.close_all.return_value = "Managed viewers closed."
        return CourtsideApp(self.players, self.original, play_store=self.store, views=views)

    def saved_play(self):
        return self.store.create(
            self.project,
            "Build the Fastbreak CLI foundation.",
            "Official API; no broker connection.",
            "Help and offline doctor work; simulated connection checks pass.",
        )

    async def settled(self, app, pilot):
        await pilot.pause()
        async with asyncio.timeout(3):
            while app.busy:
                await asyncio.sleep(0.01)
        await pilot.pause()

    def text(self, app, selector):
        return str(app.screen.query_one(selector, Static).render())

    def assert_no_session_actions(self):
        for backend in (self.original, self.target):
            backend.start.assert_not_called()
            backend.attachment.assert_not_called()
            backend.stop.assert_not_called()
            backend.shutdown.assert_not_called()

    def fill_form(self, app, *, project=None, goal="Build the CLI.", acceptance="Offline checks pass."):
        app.screen.query_one("#play-project", Input).value = str(project or self.project)
        app.screen.query_one("#play-goal", TextArea).load_text(goal)
        app.screen.query_one("#play-constraints", TextArea).load_text("No broker connection.")
        app.screen.query_one("#play-acceptance", TextArea).load_text(acceptance)

    def assert_visible(self, app, selector, widget_type):
        widget = app.screen.query_one(selector, widget_type)
        self.assertGreater(widget.region.width, 0, selector)
        self.assertGreater(widget.region.height, 0, selector)
        self.assertTrue(app.screen.region.contains_region(widget.region), selector)

    def assert_report_profiles(self, players, project, config):
        for player in players:
            profiles = []
            for index, argument in enumerate(player.args[:-1]):
                if argument != "-c":
                    continue
                key, value = player.args[index + 1].split("=", 1)
                if key == "mcp_servers.dreamteam":
                    profiles.append(tomllib.loads("value=" + value)["value"])
            self.assertEqual(len(profiles), 1, "Routing must retain exactly one report-tool profile.")
            settings = profiles[0]
            self.assertEqual(set(settings), {"command", "args", "required"})
            self.assertEqual(settings["args"], [
                "-m", "dreamteam.coach_tools", "--config", str(config),
                "--project", str(project), "--actor", player.id,
            ])
            self.assertTrue(Path(settings["command"]).is_absolute())
            self.assertIs(settings["required"], True)

    async def test_manual_start_uses_player_report_profile_and_mount_does_not_restart_sessions(self):
        configured = replace(self.players[0], args=("--model", "configured-model"))
        self.players = (configured, *self.players[1:])
        self.original.start.side_effect = lambda player: Observation("idle", "Verified.", "old-project-pane")
        app = self.make_app()
        self.assert_report_profiles(app.players, self.control, app.config)
        self.assert_report_profiles(app.controller_players, self.control, app.config)
        self.assertEqual(configured.args, ("--model", "configured-model"))
        self.assertEqual(app.players[0].args[:2], configured.args)
        self.assertEqual(
            [(player.id, player.name, player.cwd) for player in app.players],
            [(player.id, player.name, player.cwd) for player in self.players],
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            self.assert_no_session_actions()
            await pilot.press("1", "s")
            await self.settled(app, pilot)
            self.original.start.assert_called_once_with(app.players[0])
            self.assert_report_profiles((self.original.start.call_args.args[0],), self.control, app.config)
            self.original.stop.assert_not_called()
            self.target.start.assert_not_called()

    async def test_create_routes_all_players_and_clears_old_project_observations(self):
        # Unknown target observations must not inherit old-project terminal identities.
        self.target.refresh.side_effect = lambda players: {
            player.id: Observation("unknown", "Target has not been observed.")
            for player in players
        }
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            self.assertEqual(app._last_known["penny"].pane_id, "old-project-pane")
            await pilot.press("n")
            self.assertIsInstance(app.screen, NewPlay)
            for selector, widget_type in (
                ("#play-project", Input),
                ("#play-acceptance", TextArea),
                ("#save-play", Button),
                ("#cancel-play", Button),
            ):
                self.assert_visible(app, selector, widget_type)
            self.fill_form(app)
            await pilot.press("ctrl+s")
            await self.settled(app, pilot)

            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.play, self.store.load())
            self.assertEqual(app.play.project, self.project)
            self.assertEqual(app.play.status, "planned")
            self.assertIsNone(app.play.ball_holder)
            self.assertEqual(self.store.brief_path(app.play).read_text(), brief_text(app.play))
            self.factory.assert_called_once_with(self.project)
            self.assertIs(app.backend, self.target)
            self.assertTrue(all(player.cwd == self.project for player in app.players))
            self.assert_report_profiles(app.players, self.project, app.config)
            self.assert_report_profiles(app.controller_players, self.control, app.config)
            self.target.refresh.assert_called_with(app.runtime_players)
            self.assertEqual(app._last_known, {})
            self.assertTrue(all(obs.pane_id is None for obs in app.observations.values()))
            self.assertIn("unassigned", self.text(app, "#possession"))

            saved_state = self.store.path.read_bytes()
            await pilot.press("2")
            self.assertEqual(app.selected_player.id, "air_jordan")
            self.assertIsNone(app.play.ball_holder)
            self.assertEqual(self.store.path.read_bytes(), saved_state)
            self.assert_no_session_actions()

    async def test_stop_team_cleans_active_project_then_original_controller_roster(self):
        self.saved_play()
        app = self.make_app()
        order = Mock()
        order.attach_mock(app.views.close_all, "views")
        order.attach_mock(self.target.shutdown, "project")
        order.attach_mock(self.original.shutdown, "controller")
        self.target.shutdown.return_value = "Fastbreak stopped."
        self.original.shutdown.return_value = "Old controller sessions stopped."
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            with patch.object(app, "exit") as exit_app:
                await pilot.press("q", "enter")
                await self.settled(app, pilot)
                self.assertEqual(order.mock_calls, [
                    call.views(), call.project(app.runtime_players), call.controller(app.controller_runtime_players),
                ])
                exit_app.assert_called_once_with(
                    "Managed viewers closed.\nFastbreak stopped.\nOld controller sessions stopped."
                )
            self.assertTrue(all(player.cwd == self.project for player in self.target.shutdown.call_args.args[0]))
            self.assertTrue(all(player.cwd == self.control for player in self.original.shutdown.call_args.args[0]))

    async def test_invalid_form_then_cancel_does_not_save_or_route(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("n")
            for form in (
                {"project": self.project / "missing"},
                {"goal": "   "},
                {"acceptance": "   "},
            ):
                with self.subTest(form=form):
                    self.fill_form(app, **form)
                    await pilot.press("ctrl+s")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, NewPlay)
                    self.assertTrue(self.text(app, "#play-error").strip())
                    self.assert_visible(app, "#save-play", Button)
                    self.assert_visible(app, "#cancel-play", Button)
                    self.assertIsNone(self.store.load())
                    self.assertFalse(self.store.directory.exists())
                    self.factory.assert_not_called()
                    self.assert_no_session_actions()
            await pilot.press("escape")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertIsNone(app.play)
            self.assertIs(app.backend, self.original)
            self.assertFalse(self.store.directory.exists())

    async def test_editing_shortcut_characters_does_not_trigger_app_actions(self):
        app = self.make_app()
        characters = "nsqx123456"
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("n")
            with patch.object(app, "exit") as exit_app:
                for selector, widget_type in (
                    ("#play-project", Input),
                    ("#play-goal", TextArea),
                    ("#play-constraints", TextArea),
                    ("#play-acceptance", TextArea),
                ):
                    widget = app.screen.query_one(selector, widget_type)
                    if isinstance(widget, Input):
                        widget.value = ""
                    else:
                        widget.load_text("")
                    widget.focus()
                    await pilot.press(*characters)
                    value = widget.value if isinstance(widget, Input) else widget.text
                    self.assertEqual(value, characters)
                    self.assertIsInstance(app.screen, NewPlay)
                    self.assertEqual(app.selected_player.id, "penny")
                    self.assertEqual(len(app.screen_stack), 2)
                    self.assert_no_session_actions()
                exit_app.assert_not_called()
            project_input = app.screen.query_one("#play-project", Input)
            project_input.focus()
            await pilot.press("tab")
            self.assertEqual(app.focused.id, "play-goal")
            await pilot.press("shift+tab")
            self.assertEqual(app.focused.id, "play-project")
            await pilot.press("escape")
            self.assertIsNone(self.store.load())

    async def test_saved_play_routes_before_first_refresh(self):
        play = self.saved_play()
        app = self.make_app()
        self.factory.assert_called_once_with(self.project)
        self.assertEqual(app.play, play)
        self.assertIs(app.backend, self.target)
        self.assertTrue(all(player.cwd == self.project for player in app.players))
        self.assert_report_profiles(app.players, self.project, app.config)
        self.assert_report_profiles(app.controller_players, self.control, app.config)
        self.original.refresh.assert_not_called()
        self.target.refresh.assert_not_called()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            self.original.refresh.assert_not_called()
            self.target.refresh.assert_called_with(app.runtime_players)
            self.assertIn(str(self.project), self.text(app, "#directory"))
            self.assertIn("planned", self.text(app, "#possession"))
            self.assert_no_session_actions()

    async def test_brief_is_read_only_copy_does_not_deliver_or_assign(self):
        play = self.saved_play()
        saved_state = self.store.path.read_bytes()
        saved_brief = self.store.brief_path(play).read_bytes()
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("2", "b")
            self.assertIsInstance(app.screen, PlayBrief)
            content = app.screen.query_one("#brief-content", TextArea)
            self.assertTrue(content.read_only)
            self.assertEqual(content.text, brief_text(play))
            self.assert_visible(app, "#brief-content", TextArea)
            self.assert_visible(app, "#brief-help", Static)
            with patch.object(app, "copy_to_clipboard") as copy:
                await pilot.press("n", "s", "x", "q", "1", "enter", "ctrl+y")
                copy.assert_called_once_with(brief_text(play))
            self.assertEqual(content.text, brief_text(play))
            self.assertIsNone(app.play.ball_holder)
            self.assertEqual(app.play.status, "planned")
            self.assertEqual(app.selected_player.id, "air_jordan")
            self.assertEqual(self.store.path.read_bytes(), saved_state)
            self.assertEqual(self.store.brief_path(play).read_bytes(), saved_brief)
            self.assert_no_session_actions()
            await pilot.press("escape")
            self.assertEqual(len(app.screen_stack), 1)

    async def test_new_play_does_not_overwrite_saved_play(self):
        play = self.saved_play()
        saved_state = self.store.path.read_bytes()
        saved_brief = self.store.brief_path(play).read_bytes()
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("n")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.play, play)
            self.assertEqual(self.store.path.read_bytes(), saved_state)
            self.assertEqual(self.store.brief_path(play).read_bytes(), saved_brief)
            self.assertIn("already saved", self.text(app, "#status"))
            self.assert_no_session_actions()

    async def test_brief_without_play_does_not_create_state(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("b")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertIsNone(app.play)
            self.assertFalse(self.store.directory.exists())
            self.assertIn("Press N", self.text(app, "#status"))
            self.factory.assert_not_called()
            self.assert_no_session_actions()

    async def test_project_disappearing_after_save_blocks_runtime_but_keeps_brief(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            old_refreshes = self.original.refresh.call_count
            play = self.saved_play()
            saved_state = self.store.path.read_bytes()
            self.project.rmdir()
            app._play_created(play)
            await pilot.pause()
            self.assertIsNotNone(app._routing_error)
            self.assertEqual(app.play, play)
            self.assertFalse(app.busy)
            self.assertEqual(app._last_known, {})
            self.assertTrue(all(obs.pane_id is None for obs in app.observations.values()))
            self.assertIn(str(self.project), self.text(app, "#directory"))
            self.factory.assert_not_called()

            await pilot.press("1")
            await pilot.pause()
            await pilot.press("s", "enter", "r", "x", "y")
            app._automatic_refresh()
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(self.original.refresh.call_count, old_refreshes)
            self.target.refresh.assert_not_called()
            self.assert_no_session_actions()

            await pilot.press("b")
            self.assertIsInstance(app.screen, PlayBrief)
            self.assertEqual(app.screen.query_one("#brief-content", TextArea).text, brief_text(play))
            await pilot.press("escape")
            with patch.object(app, "exit") as exit_app:
                await pilot.press("q")
                self.assertIsInstance(app.screen, QuitTeam)
                exit_app.assert_not_called()
                app.screen.query_one("#quit-keep", Button).focus()
                await pilot.press("enter")
                await pilot.pause()
                exit_app.assert_called_once()
                self.assertIn("keep player sessions", exit_app.call_args.args[0])
            self.assert_no_session_actions()
            self.assertEqual(self.store.path.read_bytes(), saved_state)

    def test_missing_saved_project_fails_before_backend_actions(self):
        self.saved_play()
        saved_state = self.store.path.read_bytes()
        self.project.rmdir()
        with self.assertRaisesRegex(ValueError, "project is unavailable"):
            self.make_app()
        self.factory.assert_not_called()
        self.original.refresh.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), saved_state)
        self.assert_no_session_actions()

    def test_corrupt_saved_state_fails_before_backend_actions(self):
        self.store.play_directory.mkdir(parents=True)
        self.store.path.write_text("{ malformed state")
        with self.assertRaises(PlayError):
            self.make_app()
        self.factory.assert_not_called()
        self.original.refresh.assert_not_called()
        self.assertEqual(self.store.path.read_text(), "{ malformed state")
        self.assert_no_session_actions()


if __name__ == "__main__":
    unittest.main()
