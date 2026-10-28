"""Embedded terminal rendering and input: no processes or agent sessions."""
import unittest
from unittest.mock import patch

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Button

from dreamteam.terminal_widget import TerminalWidget, encode_key


class KeyEncodingTests(unittest.TestCase):
    def encode(self, key, character=None, **kwargs):
        return encode_key(events.Key(key, character), **kwargs)

    def test_agent_controls_and_navigation(self):
        expected = {
            "ctrl+c": b"\x03", "ctrl+q": b"\x11", "ctrl+d": b"\x04",
            "ctrl+space": b"\0", "tab": b"\t", "shift+tab": b"\x1b[Z",
            "enter": b"\r", "backspace": b"\x7f", "escape": b"\x1b",
            "up": b"\x1b[A", "home": b"\x1b[H", "end": b"\x1b[F",
            "ctrl+left": b"\x1b[1;5D", "alt+backspace": b"\x1b\x7f",
            "delete": b"\x1b[3~", "pagedown": b"\x1b[6~",
            "f1": b"\x1bOP", "f12": b"\x1b[24~",
            "shift+enter": b"\x1b[13;2u",
        }
        for key, data in expected.items():
            with self.subTest(key=key):
                self.assertEqual(self.encode(key), data)
        self.assertEqual(self.encode("up", application_cursor=True), b"\x1bOA")
        self.assertEqual(self.encode("alt+x", "x"), b"\x1bx")
        self.assertEqual(self.encode("X", "X"), b"X")
        self.assertEqual(self.encode("é", "é"), "é".encode())

    def test_unknown_keys_do_not_generate_input(self):
        self.assertIsNone(self.encode("super+f24"))


class ViewportTests(unittest.TestCase):
    def test_incremental_ansi_erase_cursor_and_utf8(self):
        terminal = TerminalWidget()
        terminal.resize_terminal(12, 3)
        terminal.feed(b"hello\x1b[")
        terminal.feed(b"2DXY\x1b[2;1H")
        terminal.feed("界".encode()[:1])
        terminal.feed("界".encode()[1:])
        self.assertEqual(terminal.terminal_screen.display[0], "helXY       ")
        self.assertEqual(terminal.terminal_screen.display[1], "界          ")
        self.assertEqual((terminal.terminal_screen.cursor.x, terminal.terminal_screen.cursor.y), (2, 1))
        terminal.feed("\x1b[2J\x1b[Hdone")
        self.assertEqual(terminal.terminal_screen.display, ["done        ", "            ", "            "])

    def test_color_attributes_and_ignored_osc_are_not_terminal_escape_output(self):
        terminal = TerminalWidget()
        terminal.feed("\x1b]52;c;SGVsbG8=\x07\x1b[38;2;122;162;247m\x1b[1mHi\x1b[0m")
        char = terminal.terminal_screen.buffer[0][0]
        self.assertEqual(char.fg, "7aa2f7")
        self.assertTrue(char.bold)
        self.assertNotIn("\x1b", "".join(terminal.terminal_screen.display))
        self.assertTrue(terminal.terminal_screen.display[0].startswith("Hi"))

    def test_resize_and_reset_clear_parser_modes_and_previous_player(self):
        terminal = TerminalWidget()
        terminal.feed("Penny\x1b[?1h\x1b[?2004h")
        terminal.resize_terminal(24, 6)
        self.assertEqual((terminal.columns, terminal.rows), (24, 6))
        self.assertIn(2004 << 5, terminal.terminal_screen.mode)
        terminal.reset_terminal()
        self.assertEqual(terminal.terminal_screen.display, [" " * 24] * 6)
        self.assertNotIn(2004 << 5, terminal.terminal_screen.mode)
        terminal.feed("Air Jordan")
        self.assertTrue(terminal.terminal_screen.display[0].startswith("Air Jordan"))


class TerminalHarness(App):
    BINDINGS = [
        Binding("q", "team_control"), Binding("n", "team_control"),
        Binding("s", "team_control"), Binding("x", "team_control"),
        Binding("1", "team_control"), Binding("ctrl+c", "team_control"),
        Binding("ctrl+q", "team_control", priority=True),
    ]
    CSS = "TerminalWidget { height: 8; }"

    def __init__(self):
        super().__init__()
        self.input_bytes = []
        self.resize_events = []
        self.release_count = 0
        self.attach_count = 0
        self.team_controls = 0

    def compose(self) -> ComposeResult:
        yield Button("Roster", id="roster")
        yield TerminalWidget(id="terminal")

    def check_action(self, action, parameters):
        # The parent must disable priority app bindings during terminal focus.
        if action == "team_control" and isinstance(self.focused, TerminalWidget):
            return False
        return True

    def action_team_control(self):
        self.team_controls += 1

    def on_terminal_widget_input(self, event):
        self.input_bytes.append(event.data)

    def on_terminal_widget_resize(self, event):
        self.resize_events.append((event.columns, event.rows))

    def on_terminal_widget_released(self, event):
        self.release_count += 1
        self.query_one("#roster").focus()

    def on_terminal_widget_attach_requested(self, event):
        self.attach_count += 1


class WidgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_delta_updates_repaint_changed_rows_and_both_cursor_positions(self):
        app = TerminalHarness()
        async with app.run_test(size=(80, 20)) as pilot:
            terminal = app.query_one(TerminalWidget)
            terminal.resize_terminal(80, 8)
            terminal.connected = True
            terminal.focus()
            terminal.feed("ready")
            await pilot.pause()
            rendered = {}
            render_line = terminal.render_line

            def render(y):
                strip = render_line(y)
                rendered[y] = strip
                return strip

            with patch.object(terminal, "render_line", side_effect=render):
                # Moving the cursor alone doesn't dirty pyte's character cells.
                terminal.feed("\x1b[3;2H")
                await pilot.pause()
                self.assertEqual(set(rendered), {0, 2})
                self.assertFalse(any(segment.style.reverse for segment in rendered[0]))
                self.assertTrue(any(segment.style.reverse for segment in rendered[2]))

                rendered.clear()
                terminal.feed("\x1b[?25l")
                await pilot.pause()
                self.assertEqual(set(rendered), {2})
                self.assertFalse(any(segment.style.reverse for segment in rendered[2]))

                rendered.clear()
                terminal.feed("\x1b[5;1Hchanged")
                await pilot.pause()
                self.assertEqual(set(rendered), {4})
                self.assertTrue(rendered[4].text.startswith("changed"))

                rendered.clear()
                terminal.resize_terminal(80, 8)
                terminal.feed("\x1b[31m")
                await pilot.pause()
                self.assertEqual(rendered, {})

    async def test_all_typing_belongs_to_focused_player_until_control_g(self):
        app = TerminalHarness()
        async with app.run_test(size=(80, 20)) as pilot:
            terminal = app.query_one(TerminalWidget)
            terminal.connected = True
            terminal.focus()
            await pilot.press("q", "n", "s", "x", "1", "ctrl+c", "ctrl+q", "tab", "up")
            self.assertEqual(b"".join(app.input_bytes), b"qnsx1\x03\x11\t\x1b[A")
            self.assertIs(app.focused, terminal)
            self.assertEqual(app.team_controls, 0)
            await pilot.press("ctrl+g")
            self.assertEqual(app.release_count, 1)
            self.assertEqual(app.focused.id, "roster")
            self.assertNotIn(b"\x07", app.input_bytes)

    async def test_combining_mark_on_next_row_repaints_previous_row(self):
        app = TerminalHarness()
        async with app.run_test(size=(40, 16)) as pilot:
            terminal = app.query_one(TerminalWidget)
            terminal.resize_terminal(4, 2)
            terminal.feed("abce\r\n")
            await pilot.pause()
            with patch.object(terminal, "render_line", wraps=terminal.render_line) as render:
                # Split UTF-8 must still invalidate the row whose cell changes.
                mark = "\u0301".encode()
                terminal.feed(mark[:1])
                terminal.feed(mark[1:])
                await pilot.pause()
                self.assertIn(0, {call.args[0] for call in render.call_args_list})
            self.assertTrue(terminal.render_line(0).text.startswith("abcé"))

    async def test_leaving_the_terminal_releases_input_once(self):
        app = TerminalHarness()
        async with app.run_test() as pilot:
            terminal = app.query_one(TerminalWidget)
            terminal.connected = True
            terminal.focus()
            await pilot.pause()
            await pilot.click("#roster")
            await pilot.pause()
            self.assertEqual(app.release_count, 1)
            self.assertFalse(terminal.connected)
            terminal.focus()
            app.query_one("#roster").focus()
            await pilot.pause()
            self.assertEqual(app.release_count, 1)

    async def test_codex_paste_is_bracketed_without_input_modes_and_cannot_break_out(self):
        app = TerminalHarness()
        async with app.run_test() as pilot:
            terminal = app.query_one(TerminalWidget)
            terminal.connected = True
            terminal.focus()
            # Herdr's rendered frames never carry Codex's bracketed-paste mode.
            terminal.post_message(events.Paste("hello\n界"))
            await pilot.pause()
            self.assertEqual(app.input_bytes, [b"\x1b[200~hello\n" + "界".encode() + b"\x1b[201~"])
            terminal.post_message(events.Paste("a\x1b[201~\ncommand\x1b[200~b"))
            await pilot.pause()
            self.assertEqual(app.input_bytes[-1], b"\x1b[200~a[201~\ncommand[200~b\x1b[201~")
            self.assertEqual(app.input_bytes[-1].count(b"\x1b[201~"), 1)

    async def test_click_requests_attach_but_disconnected_keys_and_paste_do_not_type(self):
        app = TerminalHarness()
        async with app.run_test() as pilot:
            terminal = app.query_one(TerminalWidget)
            await pilot.click("#terminal")
            await pilot.press("q", "a")
            terminal.post_message(events.Paste("not connected"))
            await pilot.pause()
            self.assertEqual(app.attach_count, 1)
            self.assertEqual(app.input_bytes, [])
            self.assertEqual(app.team_controls, 0)

    async def test_device_status_reply_is_forwarded_only_with_input_ownership(self):
        app = TerminalHarness()
        async with app.run_test() as pilot:
            terminal = app.query_one(TerminalWidget)
            terminal.feed("\x1b[6n")
            await pilot.pause()
            self.assertEqual(app.input_bytes, [])
            terminal.connected = True
            terminal.feed("\x1b[3;4H\x1b[6n")
            await pilot.pause()
            self.assertEqual(app.input_bytes, [b"\x1b[3;4R"])

    async def test_rendering_preserves_wide_cells_combining_marks_and_cursor(self):
        app = TerminalHarness()
        async with app.run_test(size=(40, 16)) as pilot:
            terminal = app.query_one(TerminalWidget)
            terminal.resize_terminal(40, 8)
            terminal.connected = True
            terminal.focus()
            terminal.feed("界e\u0301\x1b[31mX")
            await pilot.pause()
            strip = terminal.render_line(0)
            self.assertEqual(strip.cell_length, 40)
            text = strip.text
            self.assertTrue(text.startswith("界éX"))
            self.assertNotIn("\x1b", text)
            segments = list(strip)
            self.assertEqual(segments[0].style.bgcolor.get_truecolor(), (8, 10, 15))
            self.assertEqual(segments[2].style.color.name, "red")
            self.assertTrue(segments[3].style.reverse)
            terminal.feed("\x1b[?25l")
            self.assertFalse(any(segment.style.reverse for segment in terminal.render_line(0)))

    async def test_widget_resize_requests_viewport_size_and_frame_resize_does_not_echo(self):
        app = TerminalHarness()
        async with app.run_test(size=(80, 20)) as pilot:
            terminal = app.query_one(TerminalWidget)
            await pilot.pause()
            self.assertIn((80, 8), app.resize_events)
            count = len(app.resize_events)
            terminal.resize_terminal(70, 6)
            await pilot.pause()
            self.assertEqual(len(app.resize_events), count)
            await pilot.resize_terminal(100, 24)
            await pilot.pause()
            self.assertEqual(app.resize_events[-1], (100, 8))


if __name__ == "__main__":
    unittest.main()
