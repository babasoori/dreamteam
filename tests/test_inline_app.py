"""Courtside's embedded player terminal, using no real agents or Herdr panes."""
import asyncio
from pathlib import Path
import subprocess
import threading
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, ListView, Static

from dreamteam.app import CourtsideApp, QuitTeam
from dreamteam.plays import Play
from dreamteam.roster import Player
from dreamteam.runtime import BackendError, Herdr, Observation, Preview
from dreamteam.terminal_session import TerminalClosed, TerminalFrame, TerminalSessionError
from dreamteam.terminal_widget import TerminalWidget


class FakeSession:
    def __init__(self, command, env):
        self.command = command
        self.env = env
        self.ready = False
        self.started = False
        self.close_count = 0
        self.frames = []
        self.inputs = []
        self.resizes = []
        self.on_close = None
        self.send_error = None
        self.close_error = None

    def start(self):
        self.started = True

    def poll(self):
        frames, self.frames = self.frames, []
        return frames

    def send(self, data):
        if self.send_error is not None:
            raise self.send_error
        if not self.ready:
            raise AssertionError("Input sent before the controller was ready.")
        self.inputs.append(data)

    def resize(self, columns, rows):
        if not self.ready:
            raise AssertionError("Resize sent before the controller was ready.")
        self.resizes.append((columns, rows))

    def close(self):
        self.close_count += 1
        self.ready = False
        if self.close_error is not None:
            raise self.close_error
        if self.on_close is not None:
            self.on_close()


class InlineAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sessions = []

        def create(command, env):
            session = FakeSession(command, env)
            self.sessions.append(session)
            return session

        patcher = patch("dreamteam.app.TerminalSession", side_effect=create)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)

    def make_app(self):
        players = (
            Player("penny", "Penny", "pg", Path.cwd(), role="Plan the work."),
            Player("air_jordan", "Air Jordan", "sg", Path.cwd(), role="Implement."),
        )
        backend = Mock(spec=Herdr)
        backend.refresh.return_value = {
            player.id: Observation("idle", "Verified.", f"pane-{player.id}", time.time())
            for player in players
        }
        backend.refresh.return_value["coach"] = Observation("idle", "Verified.", "pane-coach", time.time())
        backend.control_command.side_effect = lambda player, columns, rows: (
            ["fake-herdr", "control", player.id], {"TEST": "isolated"},
        )
        backend.preview.side_effect = lambda player: Preview(f"{player.name} preview", time.time())
        backend.shutdown.return_value = "Team stopped."
        views = Mock()
        views.close_all.return_value = "Viewers closed."
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

    async def connect(self, app, pilot, text=b"Codex ready"):
        await pilot.press("enter")
        await self.settled(app, pilot)
        session = self.sessions[-1]
        terminal = app.query_one("#player-terminal", TerminalWidget)
        columns, rows = terminal.content_size
        session.ready = True
        session.frames.append(TerminalFrame(b"\x1b[2J\x1b[H" + text, columns, rows, True, 0))
        app._poll_terminal()
        await pilot.pause()
        self.assertTrue(terminal.connected)
        return session, terminal

    def text(self, app, selector):
        return str(app.query_one(selector, Static).render())

    async def test_enter_opens_live_terminal_and_keeps_coach_navigation_visible_without_creating_views(self):
        app, backend = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.select_penny(app, pilot)
            app.play = Play(
                "001", Path.cwd(), "Build the wrapper.", "", "Tests pass.",
                "2026-10-11T00:00:00+00:00",
            )
            session, terminal = await self.connect(app, pilot)
            self.assertTrue(session.started)
            self.assertEqual(backend.control_command.call_args.args[0], app.players[0])
            self.assertEqual(session.command, ["fake-herdr", "control", "penny"])
            self.assertIn("Codex ready", "\n".join(terminal.terminal_screen.display))
            self.assertIs(app.focused, terminal)
            coach = app.query_one("#coach-nav", Button)
            self.assertTrue(coach.display)
            self.assertGreater(coach.region.height, 0)
            self.assertTrue(app.screen.region.contains_region(coach.region))
            self.assertFalse(app.query_one("#coach-panel").display)
            self.assertTrue(app.query_one("#player-panel").display)
            self.assertGreater(terminal.region.height, 0)
            self.assertIn("Ctrl+G", self.text(app, "#attach-help"))
            app.views.open.assert_not_called()
            backend.attachment.assert_not_called()
            backend.start.assert_not_called()
        self.assertEqual(session.close_count, 1)
        backend.shutdown.assert_not_called()

    async def test_typing_including_app_shortcuts_belongs_to_player_until_control_g(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            session, terminal = await self.connect(app, pilot)
            with patch.object(app, "exit") as exit_app:
                await pilot.press("q", "n", "b", "s", "x", "1", "2", "ctrl+q", "ctrl+c", "tab", "up", "enter")
                self.assertEqual(b"".join(session.inputs), b"qnbsx12\x11\x03\t\x1b[A\r")
                self.assertEqual(app.selected_player.id, "penny")
                self.assertEqual(len(app.screen_stack), 1)
                self.assertIs(app.focused, terminal)
                exit_app.assert_not_called()
                backend.start.assert_not_called()
                backend.stop.assert_not_called()
                await pilot.press("ctrl+g")
                await pilot.pause()
                self.assertFalse(terminal.connected)
                self.assertEqual(session.close_count, 1)
                self.assertEqual(app.focused.id, "coach-nav")
                self.assertTrue(app.coach_selected)
                self.assertNotIn(b"\x07", session.inputs)
                await pilot.press("2")
                self.assertEqual(app.selected_player.id, "air_jordan")

    async def test_input_waits_for_first_frame_and_can_cancel_while_connecting(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            await pilot.press("enter")
            await self.settled(app, pilot)
            session = self.sessions[-1]
            await pilot.press("q", "x", "2", "enter")
            self.assertEqual(session.inputs, [])
            self.assertEqual(app.selected_player.id, "penny")
            self.assertEqual(len(app.screen_stack), 1)
            await pilot.press("ctrl+g")
            await pilot.pause()
            self.assertEqual(session.close_count, 1)
            self.assertEqual(app.focused.id, "coach-nav")
            self.assertTrue(app.coach_selected)
            backend.stop.assert_not_called()

    async def test_leaving_before_first_frame_releases_control_and_ignores_late_output(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            await pilot.press("enter")
            await self.settled(app, pilot)
            session = self.sessions[-1]
            terminal = app.query_one("#player-terminal", TerminalWidget)
            self.assertFalse(terminal.connected)
            app.query_one("#roster", ListView).focus()
            await pilot.pause()
            self.assertEqual(session.close_count, 1)
            self.assertIsNone(app._terminal_session)
            session.ready = True
            session.frames.append(TerminalFrame(b"Late first frame", 80, 20, True, 0))
            app._poll_terminal()
            await pilot.pause()
            self.assertFalse(terminal.connected)
            self.assertEqual(app.focused.id, "roster")
            self.assertEqual(self.factory.call_count, 1)
            self.assertEqual(app.selected_player.id, "penny")
            backend.stop.assert_not_called()

    async def test_roster_selection_releases_old_controller_and_next_enter_targets_new_player(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            penny, terminal = await self.connect(app, pilot)
            roster = app.query_one("#roster", ListView)
            roster.focus()
            roster.index = 1
            await pilot.pause()
            self.assertEqual(penny.close_count, 1)
            self.assertEqual(app.selected_player.id, "air_jordan")
            self.assertFalse(terminal.connected)
            jordan, terminal = await self.connect(app, pilot, b"Air Jordan ready")
            await pilot.press("j")
            self.assertEqual(penny.inputs, [])
            self.assertEqual(jordan.inputs, [b"j"])
            self.assertEqual(backend.control_command.call_args.args[0], app.players[1])
            self.assertNotIn("Codex ready", "\n".join(terminal.terminal_screen.display))
            backend.stop.assert_not_called()
            app.views.open.assert_not_called()

    async def test_late_verification_cannot_open_controller_for_previous_selection(self):
        app, backend = self.make_app()
        entered, release = threading.Event(), threading.Event()

        def control(player, columns, rows):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release controller verification.")
            return ["fake-herdr", "control", player.id], {}

        backend.control_command.side_effect = control
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            try:
                await pilot.press("enter")
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                roster = app.query_one("#roster", ListView)
                roster.focus()
                roster.index = 1
                await pilot.pause()
            finally:
                release.set()
            await self.settled(app, pilot)
            self.assertEqual(app.selected_player.id, "air_jordan")
            self.factory.assert_not_called()
            self.assertIsNone(app._terminal_session)
            app.views.open.assert_not_called()

    async def test_terminal_resize_reaches_same_controller_and_live_refresh_skips_preview(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            session, terminal = await self.connect(app, pilot)
            backend.preview.reset_mock()
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            app._poll_terminal()
            self.assertEqual(session.resizes[-1], tuple(terminal.content_size))
            app._automatic_refresh()
            await self.settled(app, pilot)
            backend.preview.assert_not_called()
            self.assertIs(app._terminal_session, session)
            self.assertIn("Codex ready", "\n".join(terminal.terminal_screen.display))

    async def test_streamed_frames_keep_live_label_and_unchanged_rows_out_of_layout(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            session, terminal = await self.connect(app, pilot)
            label = app.query_one("#preview-label", Static)
            with patch.object(label, "update", wraps=label.update) as update_label:
                with patch.object(terminal, "render_line", wraps=terminal.render_line) as render:
                    for sequence, data in enumerate((b"\x1b[3;1Hfirst", b" second"), 1):
                        session.frames.append(TerminalFrame(
                            data, terminal.columns, terminal.rows, False, sequence,
                        ))
                    app._poll_terminal()
                    update_label.assert_not_called()
                    await pilot.pause()
                    self.assertEqual({call.args[0] for call in render.call_args_list}, {0, 2})
            self.assertTrue(terminal.terminal_screen.display[2].startswith("first second"))
            self.assertIn("LIVE · Type here", label.content)

    async def test_lost_player_verification_releases_input_without_stopping_team(self):
        for state in ("stopped", "unknown", "unavailable"):
            with self.subTest(state=state):
                app, backend = self.make_app()
                async with app.run_test(size=(100, 32)) as pilot:
                    await self.select_penny(app, pilot)
                    session, terminal = await self.connect(app, pilot)
                    if state == "unavailable":
                        backend.refresh.side_effect = BackendError("Herdr is unavailable.")
                    else:
                        backend.refresh.return_value["penny"] = Observation(
                            state, "Codex is no longer verified.", observed_at=time.time(),
                        )
                    app._automatic_refresh()
                    await self.settled(app, pilot)
                    self.assertEqual(session.close_count, 1)
                    self.assertIsNone(app._terminal_session)
                    self.assertFalse(terminal.connected)
                    self.assertEqual(app.focused.id, "roster")
                    backend.stop.assert_not_called()
                    backend.shutdown.assert_not_called()

    async def test_ownership_conflict_returns_to_coach_without_opening_another_view(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            await pilot.press("enter")
            await self.settled(app, pilot)
            session = self.sessions[-1]
            session.frames.append(TerminalClosed("This player is controlled in another terminal. Detach that view first."))
            app._poll_terminal()
            await pilot.pause()
            self.assertEqual(session.close_count, 1)
            self.assertIsNone(app._terminal_session)
            self.assertEqual(app.focused.id, "roster")
            self.assertIn("another terminal", self.text(app, "#status"))
            app.views.open.assert_not_called()
            backend.start.assert_not_called()

    async def test_verification_failure_does_not_spawn_controller_or_change_observed_activity(self):
        app, backend = self.make_app()
        backend.control_command.side_effect = BackendError("Penny's terminal identity changed.")
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            await pilot.press("enter")
            await self.settled(app, pilot)
            self.factory.assert_not_called()
            self.assertIsNone(app._terminal_session)
            self.assertEqual(app.focused.id, "roster")
            self.assertEqual(app.observations["penny"].activity, "idle")
            self.assertIn("identity changed", self.text(app, "#status"))

    async def test_unavailable_play_project_cancels_connection_and_restores_coach(self):
        app, backend = self.make_app()
        with TemporaryDirectory() as temporary:
            missing = Path(temporary) / "project-that-was-removed"
            async with app.run_test(size=(100, 32)) as pilot:
                await self.select_penny(app, pilot)
                app.play = Play(
                    "001", missing, "Build a wrapper.", "", "Tests pass.",
                    "2026-10-11T00:00:00+00:00",
                )
                await pilot.press("enter")
                await self.settled(app, pilot)
                self.factory.assert_not_called()
                backend.control_command.assert_not_called()
                self.assertFalse(app.busy)
                terminal = app.query_one("#player-terminal", TerminalWidget)
                self.assertFalse(terminal.display)
                self.assertFalse(terminal.connected)
                self.assertEqual(app.focused.id, "roster")
                self.assertIn("project is unavailable", self.text(app, "#status"))
                self.assertIn(str(missing), self.text(app, "#status"))

    async def test_input_channel_failure_releases_controller_and_never_retries_input(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            session, terminal = await self.connect(app, pilot)
            session.send_error = TerminalSessionError("Terminal input stalled.")
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(session.close_count, 1)
            self.assertEqual(session.inputs, [])
            self.assertFalse(terminal.connected)
            self.assertIn("input stalled", self.text(app, "#status"))
            self.assertEqual(app.focused.id, "roster")

    async def test_failed_client_cleanup_retains_ownership_and_blocks_reconnect_and_team_shutdown(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            session, terminal = await self.connect(app, pilot)
            session.close_error = subprocess.TimeoutExpired("fake-herdr", 0.4)
            try:
                await pilot.press("ctrl+g")
                await pilot.pause()
                self.assertIs(app._terminal_session, session)
                self.assertFalse(terminal.connected)
                self.assertGreaterEqual(session.close_count, 1)
                app.action_attach_player()
                await self.settled(app, pilot)
                self.assertEqual(self.factory.call_count, 1)
                with patch.object(app, "exit") as exit_app:
                    app.action_quit()
                    await pilot.pause()
                    if isinstance(app.screen, QuitTeam):
                        await pilot.press("enter")
                        await self.settled(app, pilot)
                    exit_app.assert_not_called()
                self.assertIs(app._terminal_session, session)
                backend.shutdown.assert_not_called()
                app.views.close_all.assert_not_called()
            finally:
                session.close_error = None
                app._release_terminal()
                await pilot.pause()
            self.assertIsNone(app._terminal_session)

    async def test_quit_keep_releases_inline_client_and_leaves_team_running(self):
        app, backend = self.make_app()
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            session, terminal = await self.connect(app, pilot)
            with patch.object(app, "exit") as exit_app:
                app.action_quit()
                await pilot.pause()
                self.assertIsInstance(app.screen, QuitTeam)
                app.screen.query_one("#quit-keep", Button).focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(session.close_count, 1)
                self.assertFalse(terminal.connected)
                exit_app.assert_called_once()
            backend.shutdown.assert_not_called()
            app.views.close_all.assert_not_called()

    async def test_quit_stop_releases_inline_client_before_viewers_and_team(self):
        app, backend = self.make_app()
        order = []
        app.views.close_all.side_effect = lambda: order.append("views") or "Viewers closed."
        backend.shutdown.side_effect = lambda players: order.append("team") or "Team stopped."
        async with app.run_test(size=(100, 32)) as pilot:
            await self.select_penny(app, pilot)
            session, terminal = await self.connect(app, pilot)
            session.on_close = lambda: order.append("inline")
            with patch.object(app, "exit") as exit_app:
                app.action_quit()
                await pilot.pause()
                await pilot.press("enter")
                await self.settled(app, pilot)
                self.assertEqual(order, ["inline", "views", "team"])
                exit_app.assert_called_once()
                self.assertFalse(terminal.connected)


if __name__ == "__main__":
    unittest.main()
