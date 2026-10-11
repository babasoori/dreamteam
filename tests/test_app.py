"""Keyboard flows with a fake backend; these tests never launch agent sessions."""
import asyncio
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, Static

from dreamteam.app import CourtsideApp, QuitTeam, StopPlayer, ViewHelp
from dreamteam.roster import Player
from dreamteam.runtime import BackendError, Herdr, Observation, Preview


class AppTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self):
        players = (
            Player("penny", "Penny", "pg", Path.cwd(), role="Plan the work."),
            Player("air_jordan", "Air Jordan", "sg", Path.cwd(), role="Implement the change."),
        )
        backend = Mock(spec=Herdr)
        backend.refresh.return_value = {
            player.id: Observation("idle", "Verified.", f"pane-{player.id}", time.time())
            for player in players
        }
        backend.refresh.return_value["coach"] = Observation("idle", "Verified.", "pane-coach", time.time())
        backend.start.side_effect = lambda player: backend.refresh.return_value[player.id]
        backend.attachment.side_effect = lambda player: (
            ["herdr", "--session", "dreamteam-test", "agent", "attach", f"pane-{player.id}"],
            {"HERDR_CONFIG_PATH": "/unused/herdr.toml"},
        )
        backend.preview.side_effect = lambda player: Preview(f"{player.name} terminal output.", time.time())
        views = Mock()
        views.open.return_value = "Player pane is open alongside Courtside."
        views.close_all.return_value = "Managed viewers closed."
        backend.shutdown.return_value = "Team stopped."
        return CourtsideApp(players, backend, views=views), backend

    async def settled(self, app, pilot):
        await pilot.pause()
        async with asyncio.timeout(3):
            while app.busy:
                await asyncio.sleep(0.01)
        await pilot.pause()

    async def select_penny(self, app, pilot):
        """These tests exercise player actions after leaving the Coach workspace."""
        await self.settled(app, pilot)
        app.action_select_player(0)
        await self.settled(app, pilot)
        app.action_refresh_players()
        await self.settled(app, pilot)

    def widget_text(self, app, selector):
        return str(app.query_one(selector, Static).render())

    async def test_selection_has_no_session_effect_and_start_targets_selected_player(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            await pilot.press("2")
            self.assertEqual(app.selected_player.id, "air_jordan")
            backend.start.assert_not_called()
            backend.attachment.assert_not_called()
            backend.stop.assert_not_called()
            await pilot.press("s")
            await self.settled(app, pilot)
            backend.start.assert_called_once_with(app.players[1])

    async def test_busy_start_blocks_duplicate_start_and_quit(self):
        app, backend = self.make_app()
        release = threading.Event()
        entered = threading.Event()

        def start(player):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release its fake start.")
            return backend.refresh.return_value[player.id]

        backend.start.side_effect = start
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            try:
                await pilot.press("s")
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                self.assertTrue(app.busy)
                with patch.object(app, "exit") as exit_app:
                    await pilot.press("2", "s", "q")
                    exit_app.assert_not_called()
                backend.start.assert_called_once_with(app.players[0])
                self.assertEqual(app.selected_player.id, "air_jordan")
            finally:
                release.set()
            await self.settled(app, pilot)
            self.assertFalse(app.busy)

    async def test_backend_failure_marks_unknown_and_retains_last_known(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            previous = app.observations["penny"]
            backend.refresh.side_effect = BackendError("Herdr is unavailable.")
            await pilot.press("r")
            await self.settled(app, pilot)
            current = app.observations["penny"]
            self.assertEqual(current.activity, "unknown")
            self.assertEqual(current.pane_id, previous.pane_id)
            self.assertEqual(app._last_known["penny"], previous)
            self.assertIn("Last known: idle", self.widget_text(app, "#observed"))
            self.assertIn("Herdr is unavailable", self.widget_text(app, "#session-detail"))
            self.assertFalse(app.busy)

    async def test_stop_cancel_and_confirmation_keep_original_target(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            await pilot.press("x")
            self.assertIsInstance(app.screen, StopPlayer)
            await pilot.press("escape")
            backend.stop.assert_not_called()
            await pilot.press("x")
            await pilot.press("2")
            self.assertEqual(app.selected_player.id, "penny")
            # A delayed confirmation must retain its original player even if app state changes.
            app.selected_index = 1
            await pilot.press("y")
            await self.settled(app, pilot)
            backend.stop.assert_called_once_with(app.players[0])
            self.assertEqual(app.observations["penny"].activity, "stopped")
            self.assertEqual(app.observations["air_jordan"].activity, "idle")

    async def test_quit_waits_for_choice_and_cancel_leaves_everything_running(self):
        app, backend = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.select_penny(app, pilot)
            with patch.object(app, "exit") as exit_app:
                await pilot.press("q")
                self.assertIsInstance(app.screen, QuitTeam)
                self.assertEqual(app.focused.id, "quit-stop")
                for button_id in ("quit-stop", "quit-keep", "quit-cancel"):
                    button = app.screen.query_one("#" + button_id, Button)
                    self.assertTrue(app.screen.region.contains_region(button.region))
                exit_app.assert_not_called()
                app.views.close_all.assert_not_called()
                backend.shutdown.assert_not_called()
                await pilot.press("escape")
                self.assertEqual(len(app.screen_stack), 1)
                exit_app.assert_not_called()
                app.views.close_all.assert_not_called()
                backend.shutdown.assert_not_called()
            backend.stop.assert_not_called()
            backend.start.assert_not_called()
            backend.attachment.assert_not_called()

    async def test_keep_running_is_an_explicit_quit_choice(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            with patch.object(app, "exit") as exit_app:
                await pilot.press("q")
                app.screen.query_one("#quit-keep", Button).focus()
                await pilot.press("enter")
                await pilot.pause()
                exit_app.assert_called_once()
                self.assertIn("keep player sessions and viewer panes running", exit_app.call_args.args[0])
            app.views.close_all.assert_not_called()
            backend.shutdown.assert_not_called()
            backend.stop.assert_not_called()

    async def test_stop_team_closes_views_then_stops_team_before_exiting(self):
        app, backend = self.make_app()
        order = []
        app.views.close_all.side_effect = lambda: order.append("views") or "Viewers closed."
        backend.shutdown.side_effect = lambda players: order.append(("team", players)) or "Team stopped."
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            with patch.object(app, "exit") as exit_app:
                await pilot.press("q", "enter")
                await self.settled(app, pilot)
                self.assertEqual(order, ["views", ("team", app.runtime_players)])
                exit_app.assert_called_once_with("Viewers closed.\nTeam stopped.")
            backend.shutdown.assert_called_once_with(app.runtime_players)
            backend.stop.assert_not_called()

    async def test_cleanup_failure_stays_in_courtside_and_reports_incomplete_cleanup(self):
        for stage in ("views", "players"):
            with self.subTest(stage=stage):
                app, backend = self.make_app()
                operation = app.views.close_all if stage == "views" else backend.shutdown
                operation.side_effect = BackendError("A managed terminal could not be verified.")
                async with app.run_test(size=(100, 32)) as pilot:
                    await self.select_penny(app, pilot)
                    with patch.object(app, "exit") as exit_app:
                        await pilot.press("q", "enter")
                        await self.settled(app, pilot)
                        exit_app.assert_not_called()
                    self.assertEqual(len(app.screen_stack), 1)
                    self.assertFalse(app.busy)
                    self.assertIn("Cleanup incomplete", self.widget_text(app, "#status"))
                    self.assertIn("could not be verified", self.widget_text(app, "#status"))
                    app.views.close_all.assert_called_once_with()
                    backend.shutdown.assert_called_once_with(app.runtime_players)

    async def test_open_alongside_keeps_courtside_and_targets_selected_player(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            await pilot.press("2")
            with patch.object(app, "suspend") as suspend, patch("dreamteam.app.subprocess.run") as run:
                await pilot.press("o")
                await self.settled(app, pilot)
            backend.attachment.assert_called_once_with(app.players[1])
            app.views.open.assert_called_once_with(app.players[1], app.viewer_command(app.players[1]))
            suspend.assert_not_called()
            run.assert_not_called()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.selected_player.id, "air_jordan")
            self.assertFalse(app.busy)
            self.assertIn("alongside Courtside", self.widget_text(app, "#status"))
            backend.stop.assert_not_called()

    async def test_view_failure_keeps_known_agent_and_offers_separate_terminal_command(self):
        for failure_at in ("verification", "native split"):
            with self.subTest(failure_at=failure_at):
                app, backend = self.make_app()
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.select_penny(app, pilot)
                    previous = app.observations["penny"]
                    if failure_at == "verification":
                        backend.attachment.side_effect = BackendError("Player is not ready.")
                    else:
                        app.views.open.side_effect = BackendError("No native terminal host.")
                    with patch.object(app, "suspend") as suspend:
                        await pilot.press("o")
                        await self.settled(app, pilot)
                    self.assertIsInstance(app.screen, ViewHelp)
                    self.assertEqual(app.screen.player.id, "penny")
                    self.assertIn("--view penny", app.screen.command)
                    self.assertEqual(app.observations["penny"], previous)
                    self.assertFalse(app.busy)
                    suspend.assert_not_called()
                    if failure_at == "verification":
                        app.views.open.assert_not_called()
                    backend.start.assert_not_called()
                    backend.stop.assert_not_called()
                    await pilot.press("escape")
                    self.assertEqual(len(app.screen_stack), 1)

    async def test_passive_preview_survives_failure_without_losing_agent_activity(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            self.assertIn("Penny terminal output.", self.widget_text(app, "#player-output"))
            backend.preview.side_effect = BackendError("Cannot read terminal output.")
            await pilot.press("r")
            await self.settled(app, pilot)
            self.assertEqual(app.observations["penny"].activity, "idle")
            self.assertIn("stale", self.widget_text(app, "#preview-label"))
            self.assertIn("Cannot read terminal output", self.widget_text(app, "#player-output"))
            self.assertIn("Penny terminal output.", self.widget_text(app, "#player-output"))
            backend.preview.side_effect = lambda player: Preview("Recovered output.", time.time())
            await pilot.press("r")
            await self.settled(app, pilot)
            self.assertNotIn("stale", self.widget_text(app, "#preview-label"))
            self.assertEqual(self.widget_text(app, "#player-output"), "Recovered output.")
            backend.attachment.assert_not_called()
            app.views.open.assert_not_called()
            backend.start.assert_not_called()
            backend.stop.assert_not_called()

    async def test_late_preview_is_kept_with_its_player_after_selection_changes(self):
        app, backend = self.make_app()
        entered = threading.Event()
        release = threading.Event()

        def preview(player):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release its fake preview.")
            return Preview(f"Late output from {player.name}.", time.time())

        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            backend.preview.side_effect = preview
            try:
                await pilot.press("r")
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                await pilot.press("2")
                self.assertEqual(app.selected_player.id, "air_jordan")
            finally:
                release.set()
            await self.settled(app, pilot)
            self.assertEqual(app.previews["penny"].text, "Late output from Penny.")
            self.assertNotIn("Penny", self.widget_text(app, "#player-output"))
            self.assertIn("Air Jordan", self.widget_text(app, "#preview-label"))
            backend.preview.side_effect = lambda player: Preview(f"{player.name} fresh output.", time.time())
            await pilot.press("r")
            await self.settled(app, pilot)
            self.assertEqual(self.widget_text(app, "#player-output"), "Air Jordan fresh output.")
            backend.start.assert_not_called()
            backend.attachment.assert_not_called()

    async def test_modal_pauses_preview_and_blocks_session_actions(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            prior_refreshes = backend.refresh.call_count
            prior_previews = backend.preview.call_count
            await pilot.press("x")
            self.assertIsInstance(app.screen, StopPlayer)
            app._automatic_refresh()
            app.action_refresh_players()
            app.action_attach_player()
            app.action_start_player()
            app.action_quit()
            await pilot.pause()
            self.assertEqual(backend.refresh.call_count, prior_refreshes)
            self.assertEqual(backend.preview.call_count, prior_previews)
            backend.start.assert_not_called()
            backend.attachment.assert_not_called()
            app.views.open.assert_not_called()
            app.views.close_all.assert_not_called()
            backend.shutdown.assert_not_called()
            backend.stop.assert_not_called()
            self.assertIsInstance(app.screen, StopPlayer)
            await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
