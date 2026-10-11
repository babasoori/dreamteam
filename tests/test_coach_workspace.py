"""The human Coach is a sidebar destination, separate from player terminals."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, Input, ListItem, ListView, Static, TextArea

from dreamteam.app import CourtsideApp, NewPlay, PlayBrief, StopPlayer
from dreamteam.plays import PlayStore
from dreamteam.roster import Player
from dreamteam.runtime import BackendError, Herdr, Observation, Preview
from dreamteam.terminal_session import TerminalFrame, TerminalSession
from dreamteam.terminal_widget import TerminalWidget


class CoachWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = PlayStore(self.root)
        self.players = tuple(
            Player(player_id, name, position, self.root, role="A focused role.")
            for player_id, name, position in (
                ("penny", "Penny", "pg"),
                ("air_jordan", "Air Jordan", "sg"),
                ("king_james", "King James", "sf"),
                ("sir_charles", "Sir Charles", "pf"),
                ("shaq", "Shaq", "c"),
                ("the_answer", "The Answer", "bench"),
            )
        )
        self.backend = Mock(spec=Herdr)
        self.backend.refresh.side_effect = lambda players: {
            player.id: Observation("idle", "Verified.", f"pane-{player.id}", time.time())
            for player in players
        }
        self.backend.preview.side_effect = lambda player: Preview(f"{player.name} preview", time.time())
        self.backend.control_command.side_effect = lambda player, columns, rows: (
            ["fake-herdr", "control", player.id], {},
        )
        self.backend.start.side_effect = lambda player: Observation("idle", "Verified.", f"pane-{player.id}", time.time())
        self.backend.attachment.side_effect = lambda player: (["fake-herdr", "attach", player.id], {})
        self.backend.shutdown.return_value = "Team stopped."
        self.views = Mock()
        self.views.open.return_value = "Player view opened."

        self.sessions = []

        def create(command, env):
            session = Mock(spec=TerminalSession)
            session.ready = True
            session.poll.return_value = []
            session.close.side_effect = lambda: setattr(session, "ready", False)
            self.sessions.append(session)
            return session

        self.terminal_factory = self.enterContext(patch("dreamteam.app.TerminalSession", side_effect=create))
        self.backend_factory = self.enterContext(patch("dreamteam.app.Herdr", return_value=self.backend))

    def make_app(self):
        return CourtsideApp(self.players, self.backend, play_store=self.store, views=self.views)

    async def settled(self, app, pilot):
        await pilot.pause()
        async with asyncio.timeout(3):
            while app.busy:
                await asyncio.sleep(0.01)
        await pilot.pause()

    async def live_penny(self, app, pilot):
        await pilot.press("1", "enter")
        return await self.connected_terminal(app, pilot)

    async def connected_terminal(self, app, pilot):
        await self.settled(app, pilot)
        session = self.sessions[-1]
        terminal = app.query_one("#player-terminal", TerminalWidget)
        columns, rows = terminal.content_size
        session.poll.return_value = [TerminalFrame(b"Codex ready", columns, rows, True, 0)]
        app._poll_terminal()
        session.poll.return_value = []
        await pilot.pause()
        self.assertTrue(terminal.connected)
        return session, terminal

    def text(self, app, selector):
        return str(app.query_one(selector, Static).render())

    def assert_no_agent_actions(self):
        self.backend.start.assert_not_called()
        self.backend.stop.assert_not_called()
        self.backend.attachment.assert_not_called()
        self.backend.shutdown.assert_not_called()
        self.views.open.assert_not_called()
        self.views.close_all.assert_not_called()

    async def test_startup_selects_coach_and_all_seven_sidebar_destinations_fit_80_by_24(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            self.assertTrue(app.coach_selected)
            self.assertEqual(tuple(player.id for player in app.runtime_players), ("coach", *(player.id for player in self.players)))
            self.backend.refresh.assert_called_with(app.runtime_players)
            self.assertTrue(app.query_one("#coach-panel").display)
            self.assertFalse(app.query_one("#player-panel").display)
            self.assertEqual(app.focused.id, "coach-nav")
            coach = app.query_one("#coach-nav", Button)
            self.assertTrue(app.screen.region.contains_region(coach.region))
            self.assertGreater(coach.region.height, 0)
            roster = app.query_one("#roster", ListView)
            items = list(roster.query(ListItem))
            self.assertEqual(len(items), 6)
            for item in items:
                self.assertGreater(item.region.height, 0)
                self.assertTrue(roster.region.contains_region(item.region), item.id)
                self.assertTrue(app.screen.region.contains_region(item.region), item.id)
            self.assertIn("Coach", self.text(app, "#coach-title"))
            self.backend.preview.assert_called_with(app.coach)
            self.terminal_factory.assert_not_called()
            self.assert_no_agent_actions()

    async def test_number_keys_keep_player_mapping_and_c_returns_to_coach(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            for number, player in enumerate(self.players, 1):
                await pilot.press(str(number))
                self.assertFalse(app.coach_selected)
                self.assertEqual(app.selected_player.id, player.id)
                self.assertTrue(app.query_one("#player-panel").display)
                self.assertFalse(app.query_one("#coach-panel").display)
                await pilot.press("c")
                self.assertTrue(app.coach_selected)
                self.assertEqual(app.focused.id, "coach-nav")
            await pilot.press("up")
            self.assertFalse(app.coach_selected)
            self.assertEqual(app.selected_player.id, "the_answer")
            await pilot.press("down")
            self.assertTrue(app.coach_selected)
            await pilot.press("down")
            self.assertFalse(app.coach_selected)
            self.assertEqual(app.selected_player.id, "penny")
            await pilot.press("up")
            self.assertTrue(app.coach_selected)
            self.terminal_factory.assert_not_called()
            self.assert_no_agent_actions()

    async def test_coach_runtime_commands_target_coach_instead_of_hidden_last_player(self):
        app = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("5", "c", "s")
            await self.settled(app, pilot)
            self.backend.start.assert_called_once_with(app.coach)
            await pilot.press("o")
            await self.settled(app, pilot)
            self.backend.attachment.assert_called_once_with(app.coach)
            self.views.open.assert_called_once_with(app.coach, app.viewer_command(app.coach))
            await pilot.press("x")
            self.assertIsInstance(app.screen, StopPlayer)
            self.assertEqual(app.screen.player.id, "coach")
            await pilot.press("y")
            await self.settled(app, pilot)
            self.backend.stop.assert_called_once_with(app.coach)
            self.assertTrue(app.coach_selected)
            self.assertEqual(app.selected_player.id, "shaq")
            self.backend.control_command.assert_not_called()
            self.terminal_factory.assert_not_called()

    async def test_coach_calls_play_and_opens_saved_brief_with_n_b_and_buttons(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("n")
            self.assertIsInstance(app.screen, NewPlay)
            await pilot.press("escape")
            await pilot.click("#coach-new-play")
            self.assertIsInstance(app.screen, NewPlay)
            app.screen.query_one("#play-project", Input).value = str(self.project)
            app.screen.query_one("#play-goal", TextArea).load_text("Build the offline wrapper.")
            app.screen.query_one("#play-constraints", TextArea).load_text("No broker connections.")
            app.screen.query_one("#play-acceptance", TextArea).load_text("Offline checks pass.")
            await pilot.press("ctrl+s")
            await self.settled(app, pilot)
            self.assertTrue(app.coach_selected)
            self.assertEqual(app.play, self.store.load())
            self.assertIsNone(app.play.ball_holder)
            self.assertIn("Ball: unassigned", self.text(app, "#possession"))
            self.assertEqual(app.play.goal, "Build the offline wrapper.")
            self.assertEqual(app.play.constraints, "No broker connections.")
            self.assertEqual(app.play.acceptance, "Offline checks pass.")
            saved_state = self.store.path.read_bytes()
            for trigger in ("button", "b"):
                if trigger == "button":
                    await pilot.click("#coach-brief")
                else:
                    app.query_one("#coach-nav", Button).focus()
                    await pilot.press(trigger)
                self.assertIsInstance(app.screen, PlayBrief)
                self.assertTrue(app.screen.query_one("#brief-content", TextArea).read_only)
                await pilot.press("escape")
                self.assertTrue(app.coach_selected)
            self.assertEqual(self.store.path.read_bytes(), saved_state)
            self.terminal_factory.assert_not_called()
            self.assert_no_agent_actions()

    async def test_enter_opens_real_coach_terminal_and_typing_does_not_run_ui_actions(self):
        app = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            self.backend.start.assert_not_called()
            await pilot.press("enter")
            session, terminal = await self.connected_terminal(app, pilot)
            self.backend.control_command.assert_called_once()
            self.assertEqual(self.backend.control_command.call_args.args[0], app.coach)
            self.assertTrue(app.coach_selected)
            self.assertIs(app.selected_session, app.coach)
            self.assertTrue(terminal.display)
            self.assertFalse(app.query_one("#coach-hint").display)
            self.assertIs(app.focused, terminal)
            await pilot.press("n", "b", "c", "q", "1", "s", "x", "enter")
            self.assertEqual(
                b"".join(call.args[0] for call in session.send.call_args_list),
                b"nbcq1sx\r",
            )
            self.assertTrue(app.coach_selected)
            self.assertEqual(len(app.screen_stack), 1)
            self.assert_no_agent_actions()
            await pilot.press("ctrl+g")
            await pilot.pause()
            session.close.assert_called_once_with()
            self.assertEqual(app.focused.id, "coach-nav")

    async def test_enter_starts_stopped_coach_then_attaches_without_starting_any_player(self):
        original_refresh = self.backend.refresh.side_effect

        def refresh(players):
            observations = original_refresh(players)
            observations["coach"] = Observation("stopped", "Coach has not started.")
            return observations

        self.backend.refresh.side_effect = refresh
        app = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            self.backend.start.assert_not_called()
            self.terminal_factory.assert_not_called()
            await pilot.press("enter")
            await self.connected_terminal(app, pilot)
            self.backend.start.assert_called_once_with(app.coach)
            self.assertEqual(self.backend.control_command.call_args.args[0], app.coach)
            self.assertEqual(self.terminal_factory.call_count, 1)
            self.assertTrue(app.coach_selected)
            self.views.open.assert_not_called()

    async def test_coach_start_finishing_after_player_selection_does_not_steal_input(self):
        original_refresh = self.backend.refresh.side_effect

        def refresh(players):
            observations = original_refresh(players)
            observations["coach"] = Observation("stopped", "Coach has not started.")
            return observations

        self.backend.refresh.side_effect = refresh
        entered, release = threading.Event(), threading.Event()

        def start(player):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release Coach start.")
            return Observation("idle", "Coach is ready.", "pane-coach", time.time())

        self.backend.start.side_effect = start
        app = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            try:
                await pilot.press("enter")
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                await pilot.press("2")
                self.assertFalse(app.coach_selected)
                self.assertEqual(app.selected_player.id, "air_jordan")
            finally:
                release.set()
            await self.settled(app, pilot)
            self.backend.start.assert_called_once_with(app.coach)
            self.backend.control_command.assert_not_called()
            self.terminal_factory.assert_not_called()
            self.assertFalse(app.coach_selected)
            self.assertEqual(app.focused.id, "roster")

    async def test_busy_runtime_defers_refresh_without_losing_live_coach_or_verified_observations(self):
        app = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("enter")
            session, terminal = await self.connected_terminal(app, pilot)
            observations = dict(app.observations)
            last_known = dict(app._last_known)
            status = self.text(app, "#status")
            self.backend.refresh.side_effect = BackendError(
                "Another Courtside operation is in progress.", code="runtime_busy",
            )
            app._automatic_refresh()
            await self.settled(app, pilot)
            self.assertFalse(app.busy)
            self.assertEqual(app.observations, observations)
            self.assertEqual(app._last_known, last_known)
            self.assertEqual(self.text(app, "#status"), status)
            self.assertIs(app._terminal_session, session)
            self.assertTrue(terminal.connected)
            self.assertIs(app.focused, terminal)
            session.close.assert_not_called()
            await pilot.press("a")
            session.send.assert_called_once_with(b"a")

            # An actual verification failure still releases input and marks it unknown.
            self.backend.refresh.side_effect = BackendError("The terminal identity changed.", code="binding_changed")
            app._automatic_refresh()
            await self.settled(app, pilot)
            session.close.assert_called_once_with()
            self.assertIsNone(app._terminal_session)
            self.assertFalse(terminal.connected)
            self.assertEqual(app.observations["coach"].activity, "unknown")
            self.assertIn("identity changed", self.text(app, "#status"))

    async def test_same_project_play_created_by_coach_is_adopted_without_interrupting_its_session(self):
        app = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("enter")
            session, terminal = await self.connected_terminal(app, pilot)
            coach = app.coach
            play = self.store.create(
                self.root, "Build the offline wrapper.", "No broker connections.", "Offline checks pass.",
            )
            app._automatic_refresh()
            await self.settled(app, pilot)
            self.assertEqual(app.play, play)
            self.assertIs(app.backend, self.backend)
            self.assertIs(app.coach, coach)
            self.assertIs(app._terminal_session, session)
            self.assertTrue(terminal.connected)
            self.assertTrue(app.coach_selected)
            self.assertIn("Play 001", self.text(app, "#possession"))
            self.assertIn("Ball: unassigned", self.text(app, "#possession"))
            self.assertTrue(all(player.cwd == self.root for player in app.runtime_players))
            session.close.assert_not_called()
            self.backend_factory.assert_not_called()
            self.assert_no_agent_actions()

    async def test_external_play_for_another_project_releases_input_without_switching_runtime(self):
        app = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            await pilot.press("enter")
            session, terminal = await self.connected_terminal(app, pilot)
            coach = app.coach
            self.store.create(
                self.project, "Build another project.", "", "Offline checks pass.",
            )
            app._automatic_refresh()
            await self.settled(app, pilot)
            self.assertIsNone(app.play)
            self.assertIsNotNone(app._routing_error)
            self.assertIs(app.backend, self.backend)
            self.assertIs(app.coach, coach)
            self.assertTrue(all(player.cwd == self.root for player in app.runtime_players))
            self.assertIsNone(app._terminal_session)
            self.assertFalse(terminal.connected)
            session.close.assert_called_once_with()
            self.assertIn("reopen", self.text(app, "#status").lower())
            self.backend_factory.assert_not_called()
            await pilot.press("enter")
            await self.settled(app, pilot)
            self.assertEqual(self.terminal_factory.call_count, 1)
            self.assert_no_agent_actions()

    async def test_live_player_to_coach_releases_only_client_for_key_and_mouse(self):
        for trigger in ("ctrl+g", "click"):
            with self.subTest(trigger=trigger):
                app = self.make_app()
                async with app.run_test(size=(100, 32)) as pilot:
                    await self.settled(app, pilot)
                    session, terminal = await self.live_penny(app, pilot)
                    if trigger == "click":
                        await pilot.click("#coach-nav")
                    else:
                        await pilot.press("ctrl+g")
                    await pilot.pause()
                    self.assertTrue(app.coach_selected)
                    self.assertTrue(app.query_one("#coach-panel").display)
                    self.assertFalse(app.query_one("#player-panel").display)
                    self.assertFalse(terminal.connected)
                    self.assertEqual(app.focused.id, "coach-nav")
                    session.close.assert_called_once_with()
                    self.assertIsNone(app._terminal_session)
                    self.assert_no_agent_actions()

    async def test_coach_selection_invalidates_pending_player_connection(self):
        app = self.make_app()
        entered, release = threading.Event(), threading.Event()

        def control(player, columns, rows):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release controller verification.")
            return ["fake-herdr", "control", player.id], {}

        self.backend.control_command.side_effect = control
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            try:
                await pilot.press("1", "enter")
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                await pilot.click("#coach-nav")
                self.assertTrue(app.coach_selected)
            finally:
                release.set()
            await self.settled(app, pilot)
            self.assertTrue(app.coach_selected)
            self.assertEqual(app.focused.id, "coach-nav")
            self.assertIsNone(app._terminal_session)
            self.terminal_factory.assert_not_called()
            self.assert_no_agent_actions()

    async def test_clicking_another_player_from_live_input_keeps_that_player_selected(self):
        app = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.settled(app, pilot)
            penny, terminal = await self.live_penny(app, pilot)
            await pilot.click("#roster-air_jordan")
            await pilot.pause()
            self.assertFalse(app.coach_selected)
            self.assertEqual(app.selected_player.id, "air_jordan")
            self.assertEqual(app.focused.id, "roster")
            self.assertTrue(app.query_one("#player-panel").display)
            self.assertFalse(app.query_one("#coach-panel").display)
            self.assertFalse(terminal.connected)
            penny.close.assert_called_once_with()
            await pilot.press("enter")
            await self.settled(app, pilot)
            self.assertEqual(self.backend.control_command.call_args.args[0].id, "air_jordan")
            self.assertEqual(self.terminal_factory.call_count, 2)
            self.assert_no_agent_actions()


if __name__ == "__main__":
    unittest.main()
