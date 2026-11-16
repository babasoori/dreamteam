"""Keyboard flows with a fake backend; these tests never launch agent sessions."""
import asyncio
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, Static

from dreamteam.app import CourtsideApp, QuitTeam, StopPlayer
from dreamteam.roster import Player
from dreamteam.runtime import BackendError, Rex, Observation, Preview


class AppTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self):
        players = (
            Player("penny", "Penny", "pg", Path.cwd(), role="Plan the work."),
            Player("air_jordan", "Air Jordan", "sg", Path.cwd(), role="Implement the change."),
        )
        backend = Mock(spec=Rex)
        backend.refresh.return_value = {
            player.id: Observation("idle", "Verified.", f"pane-{player.id}", time.time())
            for player in players
        }
        backend.refresh.return_value["coach"] = Observation("idle", "Verified.", "pane-coach", time.time())
        backend.start.side_effect = lambda player: backend.refresh.return_value[player.id]
        backend.open.return_value = "Selected native Rex tab."
        backend.preview.side_effect = lambda player: Preview(f"{player.name} terminal output.", time.time())
        backend.shutdown.return_value = "Team stopped."
        return CourtsideApp(players, backend), backend

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

    async def test_all_six_players_and_coach_fit_small_terminal(self):
        from textual.widgets import ListItem, ListView
        players = tuple(Player(key, name, position, Path.cwd()) for key, name, position in (
            ("penny", "Penny", "pg"), ("air_jordan", "Air Jordan", "sg"),
            ("king_james", "King James", "sf"), ("sir_charles", "Sir Charles", "pf"),
            ("shaq", "Shaq", "c"), ("the_answer", "The Answer", "bench")))
        _, backend = self.make_app()
        backend.refresh.side_effect = lambda members: {
            p.id: Observation("stopped", "Not started.") for p in members}
        app = CourtsideApp(players, backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            self.assertTrue(app.coach_selected)
            roster = app.query_one("#roster", ListView)
            items = list(roster.query(ListItem))
            self.assertEqual(len(items), 6)
            for number, item in enumerate(items, 1):
                self.assertTrue(app.screen.region.contains_region(item.region))
                self.assertGreater(item.region.height, 0)
                await pilot.press(str(number))
                self.assertEqual(app.selected_session.id, players[number - 1].id)
            await pilot.press("c")
            self.assertEqual(app.selected_session.id, "coach")
            backend.start.assert_not_called()
            backend.open.assert_not_called()

    async def test_selection_has_no_session_effect_and_start_targets_selected_player(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            await pilot.press("2")
            self.assertEqual(app.selected_player.id, "air_jordan")
            backend.start.assert_not_called()
            backend.open.assert_not_called()
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
            backend.refresh.side_effect = BackendError("Rex is unavailable.")
            await pilot.press("r")
            await self.settled(app, pilot)
            current = app.observations["penny"]
            self.assertEqual(current.activity, "unknown")
            self.assertEqual(current.pane_id, previous.pane_id)
            self.assertEqual(app._last_known["penny"], previous)
            self.assertIn("Last known: idle", self.widget_text(app, "#observed"))
            self.assertIn("Rex is unavailable", self.widget_text(app, "#session-detail"))
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
                backend.shutdown.assert_not_called()
                await pilot.press("escape")
                self.assertEqual(len(app.screen_stack), 1)
                exit_app.assert_not_called()
                backend.shutdown.assert_not_called()
            backend.stop.assert_not_called()
            backend.start.assert_not_called()
            backend.open.assert_not_called()

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
                self.assertIn("keep player sessions running", exit_app.call_args.args[0])
            backend.shutdown.assert_not_called()
            backend.stop.assert_not_called()

    async def test_stop_team_then_exit(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            with patch.object(app, "exit") as exit_app:
                await pilot.press("q", "enter")
                await self.settled(app, pilot)
                backend.shutdown.assert_called_once_with(app.runtime_players)
                exit_app.assert_called_once_with("Team stopped.")

    async def test_cleanup_failure_stays_open(self):
        app, backend = self.make_app()
        backend.shutdown.side_effect = BackendError("Unverified pane.")
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            with patch.object(app, "exit") as exit_app:
                await pilot.press("q", "enter")
                await self.settled(app, pilot)
                exit_app.assert_not_called()
                self.assertIn("Cleanup incomplete", self.widget_text(app, "#status"))

    async def test_enter_and_o_focus_existing_native_tab(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            await pilot.press("2", "enter")
            await self.settled(app, pilot)
            backend.open.assert_called_once_with(app.players[1])
            await pilot.press("o")
            await self.settled(app, pilot)
            self.assertEqual(backend.open.call_count, 2)
            backend.start.assert_not_called()
            self.assertEqual(len(app.query("#player-terminal")), 0)

    async def test_open_failure_preserves_observation(self):
        app, backend = self.make_app()
        backend.open.side_effect = BackendError("Unavailable Rex tab.")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.select_penny(app, pilot)
            previous = app.observations["penny"]
            await pilot.press("enter")
            await self.settled(app, pilot)
            self.assertEqual(app.observations["penny"], previous)
            self.assertIn("Unavailable Rex tab", self.widget_text(app, "#status"))
            self.assertFalse(app.busy)

    async def test_enter_on_stopped_coach_starts_and_opens_coach(self):
        app, backend = self.make_app()
        backend.refresh.return_value["coach"] = Observation("stopped", "Not started.")
        backend.start.side_effect = lambda p: Observation("unknown", "Starting", "block:coach")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            self.assertTrue(app.coach_selected)
            await pilot.press("enter")
            await self.settled(app, pilot)
            backend.start.assert_called_once_with(app.coach)
            backend.open.assert_called_once_with(app.coach)

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
            backend.open.assert_not_called()
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
            backend.open.assert_not_called()

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
            backend.open.assert_not_called()
            backend.shutdown.assert_not_called()
            backend.stop.assert_not_called()
            self.assertIsInstance(app.screen, StopPlayer)
            await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
