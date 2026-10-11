"""An embedded terminal viewport; Herdr remains the PTY and input owner."""

from __future__ import annotations

from functools import lru_cache
from typing import Callable

import pyte
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widget import Widget


# pyte uses Linux names; Rich uses the corresponding ANSI color names.
_COLOR_NAMES = {"brown": "yellow", "brightblack": "bright_black",
                "brightred": "bright_red", "brightgreen": "bright_green",
                "brightbrown": "bright_yellow", "brightblue": "bright_blue",
                "brightmagenta": "bright_magenta", "brightcyan": "bright_cyan",
                "brightwhite": "bright_white"}
_CURSOR_KEYS = {"up": "A", "down": "B", "right": "C", "left": "D",
                "home": "H", "end": "F"}
_TILDE_KEYS = {"insert": 2, "delete": 3, "pageup": 5, "pagedown": 6,
               "f5": 15, "f6": 17, "f7": 18, "f8": 19, "f9": 20,
               "f10": 21, "f11": 23, "f12": 24}
_CONTROL_NAMES = {"space": 0, "at": 0, "left_square_brace": 27,
                  "left_square_bracket": 27, "backslash": 28,
                  "right_square_brace": 29, "right_square_bracket": 29,
                  "circumflex_accent": 30, "circumflex": 30,
                  "underscore": 31, "question_mark": 127}


def encode_key(event: events.Key, *, application_cursor: bool = False) -> bytes | None:
    """Translate Textual keys to conventional VT/xterm input sequences."""
    parts = event.key.split("+")
    key = parts[-1]
    modifiers = set(parts[:-1])
    modifier = 1 + ("shift" in modifiers) + 2 * ("alt" in modifiers) + 4 * ("ctrl" in modifiers)
    if key in _CURSOR_KEYS:
        final = _CURSOR_KEYS[key]
        if modifier > 1:
            return f"\x1b[1;{modifier}{final}".encode()
        return ("\x1bO" if application_cursor else "\x1b[").encode() + final.encode()
    if key in _TILDE_KEYS:
        middle = f";{modifier}" if modifier > 1 else ""
        return f"\x1b[{_TILDE_KEYS[key]}{middle}~".encode()
    if key in ("f1", "f2", "f3", "f4"):
        final = chr(ord("P") + int(key[1:]) - 1)
        return (f"\x1b[1;{modifier}{final}" if modifier > 1 else f"\x1bO{final}").encode()
    if key == "tab":
        return b"\x1b[Z" if "shift" in modifiers else b"\t"
    if key == "enter":
        if "shift" in modifiers or "ctrl" in modifiers:
            return f"\x1b[13;{modifier}u".encode()
        data = b"\r"
    elif key == "backspace":
        data = b"\x08" if "ctrl" in modifiers else b"\x7f"
    elif key == "escape":
        data = b"\x1b"
    elif "ctrl" in modifiers:
        if len(key) == 1 and "a" <= key.lower() <= "z":
            data = bytes([ord(key.lower()) - ord("a") + 1])
        elif key in _CONTROL_NAMES:
            data = bytes([_CONTROL_NAMES[key]])
        else:
            return None
    elif event.character is not None:
        data = event.character.encode("utf-8")
    elif key == "space":
        data = b" "
    else:
        return None
    return b"\x1b" + data if "alt" in modifiers else data


def _color(value: str) -> str | None:
    if value == "default":
        return None
    if len(value) == 6 and all(char in "0123456789abcdefABCDEF" for char in value):
        return "#" + value
    return _COLOR_NAMES.get(value, value)


@lru_cache(maxsize=4096)
def _style(character: pyte.screens.Char, cursor: bool) -> Style:
    return Style(
        color=_color(character.fg), bgcolor=_color(character.bg),
        bold=character.bold, italic=character.italics,
        underline=character.underscore, strike=character.strikethrough,
        reverse=not character.reverse if cursor else character.reverse,
        blink=character.blink,
    )


class _Screen(pyte.Screen):
    def __init__(self, columns: int, lines: int, reply: Callable[[bytes], None]) -> None:
        self._reply = reply
        super().__init__(columns, lines)

    def write_process_input(self, data: str) -> None:
        self._reply(data.encode("utf-8"))


class TerminalWidget(Widget, can_focus=True):
    """Focus routes typing to one existing player; Ctrl+G returns to coaching.

    The parent owns the Herdr connection. Set connected only once it has granted
    input ownership, feed ANSI frames on the UI thread, and handle the messages.
    This widget never starts a process, opens a pane, or acquires a connection.
    """

    DEFAULT_CSS = """
    TerminalWidget {
        width: 1fr;
        height: 1fr;
        padding: 0;
        background: #080a0f;
        color: #d8dee9;
        overflow: hidden hidden;
    }
    TerminalWidget:focus { background-tint: transparent; }
    """
    connected = reactive(False)

    class Input(Message):
        def __init__(self, data: bytes) -> None:
            self.data = data
            super().__init__()

    class Released(Message):
        """Release this input generation, optionally opening the Coach workspace."""

        def __init__(self, *, to_coach: bool = True, generation: int | None = None) -> None:
            self.to_coach = to_coach
            self.generation = generation
            super().__init__()

    class AttachRequested(Message):
        """The coach clicked an inactive terminal."""

    class Resize(Message):
        def __init__(self, columns: int, rows: int) -> None:
            self.columns = columns
            self.rows = rows
            super().__init__()

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.input_generation = 0
        self.terminal_screen = _Screen(80, 24, self._send_input)
        self._stream = pyte.ByteStream(self.terminal_screen)

    @property
    def columns(self) -> int:
        return self.terminal_screen.columns

    @property
    def rows(self) -> int:
        return self.terminal_screen.lines

    def _send_input(self, data: bytes) -> None:
        if self.connected and data:
            self.post_message(self.Input(data))

    def feed(self, data: bytes | str) -> None:
        """Apply an ANSI frame, retaining screen state between delta frames."""
        self._stream.feed(data.encode("utf-8") if isinstance(data, str) else data)
        self.terminal_screen.dirty.clear()
        self.refresh()

    def reset_terminal(self) -> None:
        """Clear the viewport and parser before connecting to a different stream."""
        self.terminal_screen = _Screen(self.columns, self.rows, self._send_input)
        self._stream = pyte.ByteStream(self.terminal_screen)
        self.refresh()

    def resize_terminal(self, columns: int, rows: int) -> None:
        """Match the server frame dimensions without generating a resize request."""
        self.terminal_screen.resize(columns=max(1, columns), lines=max(1, rows))
        self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        columns, rows = self.content_size
        if columns > 0 and rows > 0:
            self.post_message(self.Resize(columns, rows))

    def on_focus(self, event: events.Focus) -> None:
        self.refresh()

    def on_blur(self, event: events.Blur) -> None:
        if self.connected:
            self._release(to_coach=False)
        self.refresh()

    def _release(self, *, to_coach: bool = True) -> None:
        self.connected = False
        self.post_message(self.Released(to_coach=to_coach, generation=self.input_generation))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        if not self.connected:
            self.post_message(self.AttachRequested())

    def on_key(self, event: events.Key) -> None:
        # Even unsupported keys must stay here rather than invoking team controls.
        event.stop()
        event.prevent_default()
        if event.key == "ctrl+g":
            self._release()
        elif self.connected:
            data = encode_key(event, application_cursor=(1 << 5) in self.terminal_screen.mode)
            if data is not None:
                self._send_input(data)

    def on_paste(self, event: events.Paste) -> None:
        event.stop()
        event.prevent_default()
        if self.connected:
            # Herdr renders cells/cursor but omits input modes. This viewport
            # controls verified Codex sessions, whose parser accepts bracketed
            # paste; raw newlines would risk submitting an unfinished prompt.
            # Strip ESC so pasted content cannot terminate its own paste frame.
            data = event.text.replace("\x1b", "").encode("utf-8")
            self._send_input(b"\x1b[200~" + data + b"\x1b[201~")

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if y >= self.rows:
            return Strip.blank(width, self.rich_style)
        cursor = self.terminal_screen.cursor
        show_cursor = self.connected and self.has_focus and not cursor.hidden
        segments = []
        line = self.terminal_screen.buffer[y]
        for column in range(self.columns):
            char = line[column]
            # pyte represents a wide character's continuation cell as empty.
            if not char.data:
                continue
            data = "".join(c for c in char.data if ord(c) >= 32 and not 127 <= ord(c) < 160)
            segments.append(Segment(
                data or " ",
                self.rich_style + _style(
                    char, show_cursor and y == cursor.y and column == min(cursor.x, self.columns - 1)
                ),
            ))
        return Strip(segments).adjust_cell_length(width, self.rich_style)
