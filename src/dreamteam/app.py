"""Courtside: a small terminal sideline for the local Codex roster."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, ListItem, ListView, Static, TextArea

from .plays import Play, PlayStore, brief_text
from .coordination import coach_player, coordinated_player
from .roster import Player, load_roster
from .runtime import BackendError, Herdr, Observation, Preview
from .views import NativeViews
from .terminal_session import TerminalClosed, TerminalFrame, TerminalSession, TerminalSessionError
from .terminal_widget import TerminalWidget


# Ultra Dark base, Nord text and status colors, Tokyo Night accents.
ACCENT = "#7aa2f7"
VIOLET = "#bb9af7"
TEXT = "#d8dee9"
MUTED = "#9aa6b8"
ACTIVITY_COLORS = {
    "stopped": MUTED,
    "idle": "#a3be8c",
    "working": ACCENT,
    "running": ACCENT,
    "blocked": "#ebcb8b",
    "unknown": VIOLET,
}


ACTIVITY_MARKERS = {"stopped": "○", "idle": "●", "working": "●", "blocked": "!", "unknown": "?"}


def position_name(player: Player) -> str:
    return "6th man" if player.position == "bench" else player.position.upper()


def clock_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S") if timestamp else "not yet observed"


class RosterList(ListView):
    BINDINGS = [
        Binding("enter", "select_cursor", "Type here"),
        Binding("up", "app.previous_player", show=False),
        Binding("down", "app.next_player", show=False),
    ]

    def action_select_cursor(self) -> None:
        self.app.action_attach_player()


class CoachNav(Button):
    BINDINGS = [
        Binding("enter", "app.attach_player", show=False),
        Binding("up", "app.previous_player", show=False),
        Binding("down", "app.next_player", show=False),
    ]


class StopPlayer(ModalScreen[bool]):
    """Stopping is the one session action that needs explicit confirmation."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", show=False),
        Binding("y", "confirm", "Stop session"),
    ]
    CSS = """
    StopPlayer { align: center middle; background: #080a0f 80%; }
    #stop-dialog {
        width: 58; max-width: 95%; height: auto; padding: 1 2;
        border: round #4c566a; background: #16161e;
    }
    #stop-title { text-style: bold; color: #d8dee9; margin-bottom: 1; }
    #stop-buttons { height: 1; margin-top: 1; align: left middle; }
    #stop-buttons Button { height: 1; padding: 0 1; margin-right: 2; min-width: 12; border: none; background: #16161e; color: #9aa6b8; }
    #stop-buttons Button:focus { background: #202b45; color: #d8dee9; text-style: bold; }
    """

    def __init__(self, player: Player) -> None:
        super().__init__()
        self.player = player

    def compose(self) -> ComposeResult:
        with Vertical(id="stop-dialog"):
            yield Static(f"Stop {self.player.name}?", id="stop-title", markup=False)
            yield Static(
                "This closes this player's owned Herdr pane and ends its running session. "
                "Other players keep running.",
                markup=False,
            )
            with Horizontal(id="stop-buttons"):
                yield Button("n  Keep running", id="cancel-stop")
                yield Button("y  Stop session", id="confirm-stop")

    def on_mount(self) -> None:
        self.query_one("#cancel-stop", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-stop")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)



class NewPlay(ModalScreen[Play | None]):
    """Prepare one play without delivering a prompt or starting a session."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save play", priority=True),
    ]
    CSS = """
    NewPlay { align: center middle; background: #080a0f 80%; }
    #play-dialog {
        width: 88; max-width: 96%; height: auto; max-height: 100%;
        padding: 0 2; border: round #4c566a; background: #16161e;
    }
    #play-title { height: 1; text-style: bold; color: #d8dee9; }
    #play-hint { height: 1; color: #9aa6b8; margin-bottom: 1; }
    #play-fields { height: auto; max-height: 16; }
    .field-label { height: 1; color: #88c0d0; }
    #play-fields Input, #play-fields TextArea {
        background: #080a0f; color: #d8dee9; border: none; padding: 0 1;
        margin-bottom: 1;
    }
    #play-fields Input:focus, #play-fields TextArea:focus { border: none; background: #161c29; }
    #play-fields Input { height: 1; }
    #play-fields TextArea { height: 2; }
    #play-fields #play-acceptance { height: 3; margin-bottom: 0; }
    #play-fields .text-area--cursor { color: #080a0f; background: #7aa2f7; }
    #play-fields .text-area--cursor-line { background: transparent; }
    #play-fields .text-area--selection { background: #202b45; }
    #play-error { height: auto; max-height: 3; color: #f7768e; }
    #play-buttons { height: 1; margin: 1 0 0 0; }
    #play-buttons Button {
        height: 1; min-width: 12; padding: 0 1; margin-right: 2; border: none;
        background: #16161e; color: #9aa6b8;
    }
    #play-buttons Button:focus { background: #202b45; color: #d8dee9; }
    """

    def __init__(self, store: PlayStore, project: Path) -> None:
        super().__init__()
        self.store = store
        self.project = project

    def compose(self) -> ComposeResult:
        with Vertical(id="play-dialog"):
            yield Static("New play", id="play-title")
            yield Static("Tab fields · Ctrl+S save · Esc cancel", id="play-hint")
            with VerticalScroll(id="play-fields"):
                yield Static("Project folder", classes="field-label")
                yield Input(str(self.project), id="play-project")
                yield Static("Goal", classes="field-label")
                yield TextArea(id="play-goal", tab_behavior="focus")
                yield Static("Constraints (optional)", classes="field-label")
                yield TextArea(id="play-constraints", tab_behavior="focus")
                yield Static("Done when", classes="field-label")
                yield TextArea(id="play-acceptance", tab_behavior="focus")
            yield Static("", id="play-error", markup=False)
            with Horizontal(id="play-buttons"):
                yield Button("Ctrl+S  Save play", id="save-play")
                yield Button("Esc  Cancel", id="cancel-play")

    def on_mount(self) -> None:
        self.query_one("#play-project", Input).focus()

    def action_save(self) -> None:
        try:
            play = self.store.create(
                self.query_one("#play-project", Input).value,
                self.query_one("#play-goal", TextArea).text,
                self.query_one("#play-constraints", TextArea).text,
                self.query_one("#play-acceptance", TextArea).text,
            )
        except (OSError, ValueError) as exc:
            self.query_one("#play-error", Static).update(str(exc))
            return
        self.dismiss(play)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-play":
            self.action_save()
        else:
            self.action_cancel()


class PlayBrief(ModalScreen[None]):
    """The saved planning brief, ready for manual delivery in Penny's terminal."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+y", "copy", "Copy brief", priority=True),
    ]
    CSS = """
    PlayBrief { align: center middle; background: #080a0f 80%; }
    #brief-dialog {
        width: 96; max-width: 96%; height: 90%; padding: 0 2;
        border: round #4c566a; background: #16161e;
    }
    #brief-title { height: 1; color: #d8dee9; text-style: bold; }
    #brief-path { height: auto; max-height: 3; color: #9aa6b8; }
    #brief-content {
        height: 1fr; margin: 1 0; border: none; padding: 0;
        background: #16161e; color: #d8dee9;
    }
    #brief-content:focus { border: none; }
    #brief-content {
        scrollbar-size: 1 1; scrollbar-color: #4c566a; scrollbar-background: #16161e;
        scrollbar-color-hover: #7aa2f7; scrollbar-background-hover: #16161e;
        scrollbar-color-active: #7aa2f7; scrollbar-background-active: #16161e;
    }
    #brief-content .text-area--selection { background: #202b45; }
    #brief-content .text-area--cursor-line { background: transparent; }
    #brief-help { height: auto; color: #88c0d0; }
    """

    def __init__(self, play: Play, path: Path) -> None:
        super().__init__()
        self.play = play
        self.path = path

    def compose(self) -> ComposeResult:
        with Vertical(id="brief-dialog"):
            yield Static(f"Play {self.play.id} · Penny's planning brief", id="brief-title")
            yield Static(str(self.path), id="brief-path", markup=False)
            yield TextArea(brief_text(self.play), read_only=True, show_cursor=False, id="brief-content")
            yield Static(
                "Ctrl+Y copy · Esc return · Deliver in Penny's terminal; wait for your go-ahead.",
                id="brief-help",
            )

    def on_mount(self) -> None:
        self.query_one("#brief-content", TextArea).focus()

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(brief_text(self.play))
        self.query_one("#brief-help", Static).update(
            "Copy requested · Paste into Penny's terminal. Saved file above is the fallback. Esc return."
        )

    def action_close(self) -> None:
        self.dismiss()


class ViewHelp(ModalScreen[None]):
    """Keep Courtside present when a native split is unavailable."""

    BINDINGS = [Binding("escape", "close", "Close"),
                Binding("ctrl+y", "copy", "Copy command", priority=True)]
    CSS = """
    ViewHelp { align: center middle; background: #080a0f 80%; }
    #view-dialog { width: 86; max-width: 96%; height: auto; max-height: 95%;
                   padding: 1 2; border: round #4c566a; background: #16161e; }
    #view-error { height: auto; max-height: 5; color: #ebcb8b; margin-bottom: 1; }
    #view-command { height: 5; border: none; background: #080a0f; color: #d8dee9; }
    #view-instructions { height: auto; margin-bottom: 1; }
    #view-copy { height: auto; color: #88c0d0; margin-top: 1; }
    """

    def __init__(self, player: Player, command: list[str], reason: str) -> None:
        super().__init__()
        self.player = player
        self.command = shlex.join(command)
        self.reason = reason

    def compose(self) -> ComposeResult:
        with Vertical(id="view-dialog"):
            yield Static(self.reason, id="view-error", markup=False)
            yield Static(
                f"Keep Courtside here. Open a terminal split or tab and run this command for {self.player.name}. "
                "Only one view can accept typing; Courtside's output preview can stay open in every tab.",
                id="view-instructions", markup=False,
            )
            yield TextArea(self.command, read_only=True, id="view-command")
            yield Static("Ctrl+Y copy command · Esc close", id="view-copy", markup=False)

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.command)
        self.query_one("#view-copy", Static).update("Copy requested · Esc close")

    def action_close(self) -> None:
        self.dismiss(None)



class QuitTeam(ModalScreen[str | None]):
    """Make background session persistence an explicit choice."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    QuitTeam { align: center middle; background: #080a0f 80%; }
    #quit-dialog { width: 72; max-width: 96%; height: auto; padding: 1 2;
                   border: round #4c566a; background: #16161e; }
    #quit-title { height: 1; text-style: bold; }
    #quit-detail { height: auto; margin: 1 0; }
    #quit-dialog Button { height: 1; width: 100%; border: none; margin-top: 1;
                          background: #16161e; color: #9aa6b8; }
    #quit-dialog Button:focus { background: #202b45; color: #d8dee9; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-dialog"):
            yield Static("Finish coaching?", id="quit-title", markup=False)
            yield Static(
                "Stop team closes verified player sessions and managed viewer panes, then stops "
                "empty Dreamteam servers. Running tasks will end. Your active terminal and "
                "unrelated panes are preserved; any remaining resources are reported.",
                id="quit-detail", markup=False,
            )
            yield Button("Stop team and quit", id="quit-stop")
            yield Button("Keep sessions running and quit", id="quit-keep")
            yield Button("Cancel", id="quit-cancel")

    def on_mount(self) -> None:
        self.query_one("#quit-stop", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {"quit-stop": "stop", "quit-keep": "keep", "quit-cancel": None}
        self.dismiss(choices[event.button.id])

    def action_cancel(self) -> None:
        self.dismiss(None)


class CourtsideApp(App[str | None]):
    """All backend operations are serialized; session state belongs to Herdr."""

    TITLE = "Dreamteam · Courtside"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("1", "select_player(0)", show=False),
        Binding("2", "select_player(1)", show=False),
        Binding("3", "select_player(2)", show=False),
        Binding("4", "select_player(3)", show=False),
        Binding("5", "select_player(4)", show=False),
        Binding("6", "select_player(5)", show=False),
        Binding("up", "previous_player", show=False),
        Binding("down", "next_player", show=False),
        Binding("c", "select_coach", "Coach"),
        Binding("n", "new_play", "New play"),
        Binding("b", "view_brief", "Brief"),
        Binding("s", "start_player", "Start"),
        Binding("enter", "attach_player", "Type here"),
        Binding("o", "open_alongside", "View"),
        Binding("r", "refresh_players", "Refresh"),
        Binding("x", "stop_player", "Stop"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", show=False),
        Binding("ctrl+q", "quit", show=False, priority=True),
    ]
    CSS = """
    Screen {
        background: #080a0f; color: #d8dee9;
        scrollbar-background: #080a0f; scrollbar-color: #2e3440;
        scrollbar-background-hover: #080a0f; scrollbar-color-hover: #4c566a;
        scrollbar-background-active: #080a0f; scrollbar-color-active: #7aa2f7;
    }
    #masthead { height: 3; padding: 0 2; }
    #identity { height: 1; }
    #brand { width: 1fr; height: 1; }
    #tagline { width: auto; height: 1; color: #9aa6b8; }
    #workspace { height: 1fr; padding: 0 2; }
    #lineup {
        width: 29; padding: 1 1 0 0; margin-right: 3;
        border-right: solid #2e3440;
    }
    #coach-nav {
        height: 2; min-height: 1; width: 1fr; min-width: 0; padding: 0 1;
        border: none; background: #080a0f; color: #d8dee9; content-align: left middle; text-align: left;
    }
    #coach-nav:hover { background: #161c29; }
    #coach-nav.selected { background: #202b45; text-style: bold; }
    .coach-selected #roster ListItem.-highlight { background: #080a0f; }
    #roster-label { height: 2; padding-left: 2; color: #9aa6b8; }
    #roster { height: 1fr; background: #080a0f; border: none; }
    #roster:focus { background-tint: transparent; }
    #roster ListItem { height: 3; padding: 0 1; background: #080a0f; }
    #roster ListItem:hover { background: #161c29; }
    #roster ListItem.-highlight { background: #202b45; }
    #roster ListItem.-highlight > Static { color: #d8dee9; }
    #roster ListItem.bench { height: 5; }
    #roster-help { height: 2; padding-left: 2; color: #9aa6b8; }
    #main-panel { width: 1fr; padding: 1 0 0 0; }
    #player-panel, #coach-panel { width: 1fr; height: auto; }
    #coach-panel { display: none; }
    .coach-selected #coach-panel { display: block; }
    .coach-selected #player-panel { display: none; }
    #coach-actions { height: 1; margin-top: 1; }
    #coach-actions Button {
        height: 1; min-height: 1; min-width: 0; width: auto; padding: 0 1;
        margin-right: 2; border: none; background: #080a0f; color: #88c0d0;
    }
    #coach-actions Button:hover, #coach-actions Button:focus { background: #202b45; }
    #coach-actions Button:disabled { color: #586273; text-opacity: 100%; }
    #session-content {
        height: auto; max-height: 11; scrollbar-size: 1 1;
        scrollbar-color: #4c566a; scrollbar-background: #080a0f;
        scrollbar-color-hover: #7aa2f7; scrollbar-background-hover: #080a0f;
        scrollbar-color-active: #7aa2f7; scrollbar-background-active: #080a0f;
    }
    #coach-controls { height: auto; margin-bottom: 1; }
    #coach-title { height: 1; color: #bb9af7; text-style: bold; }
    #coach-hint { height: 1; color: #9aa6b8; }
    #possession { height: auto; max-height: 2; color: #9aa6b8; text-wrap: nowrap; text-overflow: ellipsis; }
    #player-terminal { display: none; }
    .terminal-active #player-terminal { display: block; }
    .terminal-active #preview-scroll, .terminal-active #session-content,
    .terminal-active #attach-help, .terminal-active #next-action { display: none; }
    .terminal-active #main-panel { padding-top: 0; }
    .terminal-active #coach-title, .terminal-active #coach-actions,
    .terminal-active #coach-hint { display: none; }
    .terminal-active #coach-controls { margin-bottom: 0; }
    .terminal-active #preview-label { margin-top: 0; }
    #player-name { color: #d8dee9; text-style: bold; height: 1; }
    #player-position { color: #9aa6b8; height: 1; }
    #directory { color: #9aa6b8; height: 1; text-overflow: ellipsis; text-wrap: nowrap; }
    #pane { display: none; }
    #player-role { height: 1; color: #9aa6b8; text-overflow: ellipsis; text-wrap: nowrap; }
    #activity { height: 1; }
    #session-detail { height: auto; max-height: 2; max-width: 76; }
    #observed { color: #9aa6b8; height: auto; max-height: 2; }
    #preview-label { height: 1; margin-top: 1; color: #88c0d0; }
    #preview-scroll { height: 1fr; scrollbar-size: 1 1; }
    #player-output { height: auto; min-height: 1; text-wrap: nowrap; }
    #attach-help { color: #9aa6b8; height: auto; margin: 1 0; }
    #next-action {
        height: 3; padding: 1 0 0 0; border-top: solid #2e3440;
        color: #d8dee9;
    }
    #status { height: auto; min-height: 1; max-height: 4; padding: 0 2; color: #9aa6b8; }
    Footer { background: #080a0f; color: #9aa6b8; padding: 0 1; }
    FooterKey { background: #080a0f; }
    FooterKey .footer-key--key {
        background: #080a0f; color: #88c0d0; text-style: none;
    }
    FooterKey .footer-key--description { background: #080a0f; color: #9aa6b8; }
    FooterKey:hover { background: #202b45; }
    .compact #masthead { height: 3; padding: 0 1; }
    .compact #workspace { padding: 0 1; }
    .compact #lineup { width: 25; margin-right: 2; padding-top: 1; }
    .compact #coach-nav { height: 1; }
    .compact #roster-label { height: 1; margin-bottom: 1; }
    .compact #roster ListItem { height: 2; }
    .compact #roster ListItem.bench { height: 4; }
    .compact #roster-help { height: 1; }
    .compact #main-panel { padding-top: 1; }
    .compact #player-role { display: none; }
    .compact #session-content { max-height: 9; }
    .compact #preview-label { margin-top: 0; }
    .compact #session-detail { margin-top: 0; }
    .compact #observed, .compact #pane { margin-top: 0; }
    .compact #attach-help { margin: 0; }
    .compact #next-action { height: 3; padding-top: 1; }
    """

    def __init__(
        self, players: tuple[Player, ...], backend: Herdr, *, play_store: PlayStore | None = None,
        config: Path | None = None, views: NativeViews | None = None,
    ) -> None:
        super().__init__()
        if not players:
            raise ValueError("Courtside needs at least one player.")
        self._configured_players = players
        self.backend = backend
        self.controller_backend = backend
        self.play_store = play_store
        controller_root = play_store.root if play_store is not None else Path.cwd()
        self.config = (config or controller_root / "dreamteam.toml").resolve()
        self.controller_players = tuple(
            coordinated_player(player, self.config, self.config.parent) for player in players
        )
        self.players = self.controller_players
        self.coach = coach_player(self.config, self.config.parent)
        self.controller_coach = self.coach
        self.runtime_players = (self.coach, *self.players)
        self.controller_runtime_players = (self.controller_coach, *self.controller_players)
        self.views = views if views is not None else NativeViews(self.config.parent)
        self.previews: dict[str, Preview] = {}
        self.preview_errors: dict[str, str] = {}
        self._preview_player_id: str | None = None
        self._routing_error: str | None = None
        self.play = play_store.load() if play_store is not None else None
        if self.play is not None:
            self._route_project(self.play)
        self.selected_index = 0
        self.coach_selected = True
        self.busy = False
        self.observations = {
            player.id: Observation("unknown", "Waiting for the first Herdr observation.")
            for player in self.runtime_players
        }
        self._last_known: dict[str, Observation] = {}
        self._report_refresh = False
        self._refresh_failed = False
        self._terminal_session: TerminalSession | None = None
        self._terminal_player_id: str | None = None
        self._terminal_generation = 0
        self._terminal_size = (80, 24)
        self._terminal_sent_size: tuple[int, int] | None = None
        self._terminal_close_failed = False

    def _route_project(self, play: Play) -> None:
        if not any(p.id == "penny" and p.position == "pg" for p in self.players):
            raise ValueError("New Play requires Penny in the point guard position.")
        if not play.project.is_dir():
            raise ValueError(f"Play project is unavailable: {play.project}")
        self.players = tuple(
            coordinated_player(replace(player, cwd=play.project), self.config, play.project)
            for player in self._configured_players
        )
        self.coach = coach_player(self.config, play.project)
        self.runtime_players = (self.coach, *self.players)
        self.backend = Herdr(play.project)

    @property
    def selected_player(self) -> Player:
        return self.players[self.selected_index]

    @property
    def selected_session(self) -> Player:
        return self.coach if self.coach_selected else self.selected_player

    def compose(self) -> ComposeResult:
        with Vertical(id="masthead"):
            with Horizontal(id="identity"):
                yield Static(
                    Text.assemble((">_ ", VIOLET), ("Dreamteam", f"bold {TEXT}"), (" / Courtside", MUTED)),
                    id="brand",
                )
                yield Static(f"You direct · Coach + {len(self.players)} players", id="tagline", markup=False)
            yield Static("", id="directory", markup=False)
        with Horizontal(id="workspace"):
            with Vertical(id="lineup"):
                yield CoachNav("› c  Coach", id="coach-nav")
                yield Static("Starting five", id="roster-label", markup=False)
                yield RosterList(
                    *(
                        ListItem(
                            Static("", id=f"roster-{player.id}", markup=False),
                            classes="bench" if player.position == "bench" else "",
                        )
                        for player in self.players
                    ),
                    id="roster",
                )
                yield Static("C / 1–6 / ↑↓  select", id="roster-help", markup=False)
            with Vertical(id="main-panel"):
                with Vertical(id="coach-panel"):
                    with Vertical(id="coach-controls"):
                        yield Static("Coach · Codex", id="coach-title", markup=False)
                        yield Static("", id="possession", markup=False)
                        yield Static("Prompt Coach below · N call a play · B brief", id="coach-hint", markup=False)
                        with Horizontal(id="coach-actions"):
                            yield Button("N  Call a play", id="coach-new-play")
                            yield Button("B  Open brief", id="coach-brief")
                with Vertical(id="player-panel"):
                    with VerticalScroll(id="session-content"):
                        yield Static("", id="player-name", markup=False)
                        yield Static("", id="player-position", markup=False)
                        yield Static("", id="player-role", markup=False)
                        yield Static("", id="activity", markup=False)
                        yield Static("", id="session-detail", markup=False)
                        yield Static("", id="observed", markup=False)
                        yield Static("", id="pane", markup=False)
                yield Static("", id="preview-label", markup=False)
                with VerticalScroll(id="preview-scroll"):
                    yield Static("", id="player-output", markup=False)
                yield TerminalWidget(id="player-terminal")
                yield Static(
                    "Enter or click output to type here · Ctrl+G coach · O separate pane",
                    id="attach-help",
                    markup=False,
                )
                yield Static("", id="next-action", markup=False)
        yield Static("Checking Herdr…", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.screen_stack[0].set_class(self.size.width < 100 or self.size.height < 34, "compact")
        self.query_one("#coach-nav", Button).focus()
        self._render_players()
        self.action_refresh_players()
        self.set_interval(3, self._automatic_refresh)
        self.set_interval(1 / 30, self._poll_terminal)

    def on_resize(self) -> None:
        self.screen_stack[0].set_class(self.size.width < 100 or self.size.height < 34, "compact")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.index is not None and event.list_view.has_focus:
            self.action_select_player(event.list_view.index)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.index is not None:
            self.action_select_player(event.list_view.index)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "coach-nav":
            self.action_select_coach()
        elif event.button.id == "coach-new-play":
            self.action_new_play()
        elif event.button.id == "coach-brief":
            self.action_view_brief()

    def _render_navigation(self) -> None:
        self.screen_stack[0].set_class(self.coach_selected, "coach-selected")
        coach = self.query_one("#coach-nav", Button)
        coach.set_class(self.coach_selected, "selected")
        coach.label = "› c  Coach" if self.coach_selected else "  c  Coach"
        self.bind("enter", "attach_player", description=(
            "Coach" if self.coach_selected else "Type here"
        ))
        self.refresh_bindings()

    def _render_coach(self) -> None:
        self.query_one("#coach-new-play", Button).disabled = self.play is not None
        self.query_one("#coach-brief", Button).disabled = self.play is None or self.play_store is None
        observation = self.observations[self.coach.id]
        self.query_one("#coach-title", Static).update(
            f"Coach · Codex · {ACTIVITY_MARKERS.get(observation.activity, '?')} {observation.activity}"
        )
        if self.play is None:
            possession = Text("No play called yet · Coach can help define it.", style=MUTED)
        else:
            possession = Text.assemble(
                (f"Play {self.play.id} · {self.play.status} · Ball: {self.play.ball_holder or 'unassigned'}\n", VIOLET),
                (self.play.goal, TEXT),
            )
        self.query_one("#possession", Static).update(possession)

    def _render_players(self) -> None:
        for number, player in enumerate(self.players, 1):
            observation = self.observations[player.id]
            selected = not self.coach_selected and player.id == self.selected_player.id
            row = Text()
            if player.position == "bench":
                row.append("\nBench\n", style=MUTED)
            row.append("› " if selected else "  ", style=ACCENT)
            row.append(f"{number} ", style=MUTED)
            row.append(player.name, style=f"bold {TEXT}" if selected else TEXT)
            marker = ACTIVITY_MARKERS.get(observation.activity, "?")
            position = "6th" if player.position == "bench" else position_name(player)
            row.append(f"\n    {position} · {marker} {observation.activity}",
                       style=ACTIVITY_COLORS.get(observation.activity, MUTED))
            self.query_one(f"#roster-{player.id}", Static).update(row)
        player = self.selected_session
        observation = self.observations[player.id]
        self.query_one("#player-name", Static).update(player.name)
        self.query_one("#player-position", Static).update(f"{position_name(player)} · Codex")
        self.query_one("#player-role", Static).update(player.role)
        self.query_one("#activity", Static).update(
            Text(f"{ACTIVITY_MARKERS.get(observation.activity, '?')} {observation.activity}",
                 style=ACTIVITY_COLORS.get(observation.activity, MUTED))
        )
        self.query_one("#session-detail", Static).update(observation.detail)
        observed = f"Observed: {clock_time(observation.observed_at)}"
        last = self._last_known.get(player.id)
        if observation.activity == "unknown" and last is not None:
            observed += f"\nLast known: {last.activity} at {clock_time(last.observed_at)}"
        self.query_one("#observed", Static).update(observed)
        self.query_one("#pane", Static).update(f"Terminal  {observation.pane_id or 'not started'}")
        directory = self.play.project if self.play is not None else player.cwd
        self.query_one("#directory", Static).update(str(directory).replace(str(Path.home()), "~", 1))
        self._render_navigation()
        self._render_coach()
        self._render_preview()
        self._render_action_hint()

    def _render_preview(self) -> None:
        if self._terminal_player_id is not None:
            self._render_terminal_label()
            return
        player = self.selected_session
        preview = self.previews.get(player.id)
        error = self.preview_errors.get(player.id)
        label = f"{player.name} · read-only output"
        if error:
            label += " · stale" if preview is not None else " · unavailable"
        elif preview is not None:
            label += f" · {clock_time(preview.observed_at)}"
        self.query_one("#preview-label", Static).update(label)
        output = preview.text.rstrip() if preview is not None else ""
        if error:
            output = output + f"\n\nPreview unavailable: {error}"
        elif not output:
            output = (("Press Enter to start Coach." if self.coach_selected else "Press S to start this player.")
                      if self.observations[player.id].activity == "stopped"
                      else "Waiting for terminal output…")
        widget = self.query_one("#player-output", Static)
        scroller = self.query_one("#preview-scroll", VerticalScroll)
        follow = scroller.is_vertical_scroll_end or self._preview_player_id != player.id
        self._preview_player_id = player.id
        if str(widget.content) != output:
            widget.update(Text(output))
            if follow:
                self.call_after_refresh(scroller.scroll_end, animate=False, x_axis=False)

    def viewer_command(self, player: Player) -> list[str]:
        return [sys.executable, "-m", "dreamteam.app", "--config", str(self.config), "--view", player.id]

    def action_new_play(self) -> None:
        if not self._can_operate():
            return
        self.action_select_coach()
        if self.play_store is None:
            self._status("Play storage is unavailable.", error=True)
            return
        if self.play is not None:
            self._status("A play is already saved. Press B for its brief; pass and acceptance controls are next.")
            return
        if not any(p.id == "penny" and p.position == "pg" for p in self.players):
            self._status("New Play requires Penny in the point guard position.", error=True)
            return
        self.push_screen(NewPlay(self.play_store, self.coach.cwd), self._play_created)

    def _play_created(self, play: Play | None) -> None:
        if play is None:
            return
        # No runtime operation can overlap the modal. Old project sessions remain owned
        # by their own Herdr runtime; never relabel or reuse them for a different project.
        self.play = play
        try:
            self._route_project(play)
        except ValueError as exc:
            self._routing_error = str(exc)
        self.observations = {
            p.id: Observation("unknown", self._routing_error or "Waiting for this project's first Herdr observation.")
            for p in self.runtime_players
        }
        self._last_known.clear()
        self.previews.clear()
        self.preview_errors.clear()
        self._report_refresh = False
        self._refresh_failed = False
        self._render_players()
        self._status(f"Play {play.id} saved · B opens Penny's brief · No assignment has been delivered.")
        self._begin("refresh")

    def action_view_brief(self) -> None:
        if not self._can_operate():
            return
        self.action_select_coach()
        if self.play is None or self.play_store is None:
            self._status("Press N to call a play first.")
            return
        self.push_screen(PlayBrief(self.play, self.play_store.brief_path(self.play)))

    def _render_action_hint(self) -> None:
        player = self.selected_session
        activity = self.observations[player.id].activity
        if self._routing_error:
            hint = Text("Project unavailable · Restore it and reopen Courtside", style=MUTED)
        elif self.busy:
            hint = Text("Waiting for Herdr…", style=MUTED)
        elif activity == "stopped":
            key = "↵  " if self.coach_selected else "s  "
            hint = Text.assemble((key, ACCENT), (f"Start {player.name}", TEXT))
        elif activity == "unknown":
            hint = Text.assemble(("r  ", ACCENT), (f"Check {player.name}'s session", TEXT))
        else:
            hint = Text.assemble(("↵  ", ACCENT), (f"Type to {player.name} here", TEXT))
        self.query_one("#next-action", Static).update(hint)

    def _status(self, message: str, *, error: bool = False) -> None:
        self.query_one("#status", Static).update(
            Text(message, style="#f7768e" if error else MUTED)
        )

    def _can_operate(self) -> bool:
        if len(self.screen_stack) > 1:
            return False
        if self.busy:
            self._status("A Herdr operation is still running. Wait for it to finish.")
            return False
        return self._release_terminal()

    def action_select_coach(self) -> None:
        if len(self.screen_stack) > 1 or not self._release_terminal():
            return
        if not self.coach_selected:
            self._terminal_generation += 1
        self.coach_selected = True
        self._render_players()
        self.query_one("#coach-nav", Button).focus()

    def action_select_player(self, index: int) -> None:
        if len(self.screen_stack) > 1 or not 0 <= index < len(self.players):
            return
        if not self._release_terminal():
            self.query_one("#roster", ListView).index = self.selected_index
            return
        if self.coach_selected or self.selected_index != index:
            self._terminal_generation += 1
        self.coach_selected = False
        self.selected_index = index
        roster = self.query_one("#roster", ListView)
        roster.index = index
        roster.focus()
        self._render_players()

    def action_previous_player(self) -> None:
        if self.coach_selected:
            self.action_select_player(len(self.players) - 1)
        elif self.selected_index == 0:
            self.action_select_coach()
        else:
            self.action_select_player(self.selected_index - 1)

    def action_next_player(self) -> None:
        if self.coach_selected:
            self.action_select_player(0)
        elif self.selected_index == len(self.players) - 1:
            self.action_select_coach()
        else:
            self.action_select_player(self.selected_index + 1)

    def _begin(self, operation: str, player: Player | None = None) -> None:
        if self.play is not None and not self.play.project.is_dir():
            self._routing_error = f"Play project is unavailable: {self.play.project}"
        if self._routing_error:
            if operation == "control":
                self.busy = False
                self._release_terminal()
            self._status(self._routing_error + " · Restore it and reopen Courtside.", error=True)
            self._render_action_hint()
            return
        self.busy = True
        self._render_action_hint()
        self._perform(
            operation, self.selected_session if operation == "refresh" else player,
            self._terminal_generation if operation in {"control", "coach_start"} else None,
            self._terminal_size, self._terminal_player_id is None,
        )

    def action_start_player(self) -> None:
        if self._can_operate():
            player = self.selected_session
            self._status(f"Starting {player.name}…")
            self._begin("start", player)

    def action_attach_player(self) -> None:
        if not self._can_operate():
            return
        player = self.selected_session
        if self.coach_selected and self.observations[player.id].activity == "stopped":
            self._terminal_generation += 1
            self._status("Starting Coach…")
            self._begin("coach_start", player)
            return
        self._terminal_generation += 1
        generation = self._terminal_generation
        self._terminal_player_id = player.id
        self._terminal_close_failed = False
        self._terminal_sent_size = None
        terminal = self.query_one("#player-terminal", TerminalWidget)
        terminal.connected = False
        terminal.input_generation = generation
        terminal.reset_terminal()
        self.screen_stack[0].add_class("terminal-active")
        terminal.focus()
        self.busy = True
        self._render_players()
        self._status(f"Connecting {player.name} here… Ctrl+G returns to coaching.")
        # Use the expanded terminal's actual viewport after Textual lays it out.
        self.call_after_refresh(self._begin_control, player, generation)

    def _begin_control(self, player: Player, generation: int) -> None:
        if generation != self._terminal_generation or player.id != self.selected_session.id:
            self.busy = False
            self._render_action_hint()
            return
        terminal = self.query_one("#player-terminal", TerminalWidget)
        self._terminal_size = (min(1000, max(2, terminal.content_size.width)),
                               min(1000, max(2, terminal.content_size.height)))
        self._begin("control", player)

    def action_open_alongside(self) -> None:
        if self._can_operate():
            player = self.selected_session
            self._status(f"Opening {player.name} alongside Courtside…")
            self._begin("attach", player)

    def action_refresh_players(self) -> None:
        if self._can_operate():
            self._status("Refreshing Herdr observations…")
            self._report_refresh = True
            self._begin("refresh")

    def _automatic_refresh(self) -> None:
        if (not self.busy and not self._routing_error and not self._terminal_close_failed
                and len(self.screen_stack) == 1):
            self._begin("refresh")

    def action_stop_player(self) -> None:
        if not self._can_operate():
            return
        player = self.selected_session

        def confirmed(stop: bool | None) -> None:
            if stop:
                self._status(f"Stopping {player.name}'s owned session…")
                self._begin("stop", player)

        self.push_screen(StopPlayer(player), confirmed)

    def action_quit(self) -> None:
        if not self._can_operate():
            return

        def chosen(choice: str | None) -> None:
            if choice == "keep":
                self.exit("Courtside closed. You chose to keep player sessions and viewer panes running.")
            elif choice == "stop":
                self._status("Stopping managed views, players, and empty servers…")
                self.busy = True
                self._render_action_hint()
                self._perform("shutdown", None)

        self.push_screen(QuitTeam(), chosen)

    @work(thread=True, exit_on_error=False)
    def _perform(
        self, operation: str, player: Player | None, generation: int | None = None,
        terminal_size: tuple[int, int] = (80, 24), include_preview: bool = True,
    ) -> None:
        result: Any = None
        error: str | None = None
        try:
            if operation == "shutdown":
                reports = []
                failures = []
                operations = [self.views.close_all, lambda: self.backend.shutdown(self.runtime_players)]
                if self.controller_backend is not self.backend:
                    operations.append(lambda: self.controller_backend.shutdown(self.controller_runtime_players))
                for cleanup in operations:
                    try:
                        reports.append(cleanup())
                    except (BackendError, OSError) as exc:
                        failures.append(str(exc))
                result = "\n".join(report for report in reports if report)
                if failures:
                    raise BackendError(result + "\n" + "\n".join(failures))
            elif operation == "refresh":
                observations = self.backend.refresh(self.runtime_players)
                preview = None
                preview_error = None
                if include_preview:
                    try:
                        preview = self.backend.preview(player)
                    except (BackendError, OSError) as exc:
                        preview_error = str(exc)
                saved_play = self.play_store.load() if self.play_store is not None else None
                result = observations, preview, preview_error, saved_play
            elif operation == "control":
                result = self.backend.control_command(player, *terminal_size)
            elif operation in {"start", "coach_start"}:
                result = self.backend.start(player)
            elif operation == "stop":
                self.backend.stop(player)
            elif operation == "attach":
                self.backend.attachment(player)  # Verify the target before creating a viewer.
                result = self.views.open(player, self.viewer_command(player))
        except BackendError as exc:
            if operation == "refresh" and exc.code == "runtime_busy":
                operation = "refresh_deferred"
            else:
                error = str(exc)
        except OSError as exc:
            error = str(exc)
        except Exception as exc:
            error = f"Unexpected Herdr error: {type(exc).__name__}: {exc}"
        self.call_from_thread(self._complete, operation, player, result, error, generation)

    def _complete(
        self,
        operation: str,
        player: Player | None,
        result: Any,
        error: str | None,
        generation: int | None = None,
    ) -> None:
        if operation == "refresh_deferred":
            self.busy = False
            self._render_action_hint()
            if self._report_refresh:
                self._status("Team operation in progress · Refresh will retry shortly.")
                self._report_refresh = False
            return
        if operation == "control":
            self.busy = False
            if (generation != self._terminal_generation
                    or player.id != self.selected_session.id
                    or player.id != self._terminal_player_id):
                self._render_action_hint()
                return
            if error is not None:
                if self._release_terminal():
                    self._status(f"Could not open {player.name} here: {error}", error=True)
                return
            command, environment = result
            self._terminal_session = TerminalSession(command, environment)
            try:
                self._terminal_session.start()
            except (TerminalSessionError, OSError) as exc:
                if self._release_terminal():
                    self._status(str(exc), error=True)
            self._render_action_hint()
            return
        if operation == "shutdown":
            self.busy = False
            if error:
                self._status("Cleanup incomplete: " + error, error=True)
                self._render_action_hint()
            else:
                self.exit(result)
            return
        if error is not None:
            if operation == "refresh":
                self._refresh_failed = True
            affected = self.runtime_players if operation == "refresh" else (player,)
            if operation == "attach":
                self.busy = False
                self._render_action_hint()
                self._status("Player view was not opened. See the separate-terminal command.", error=True)
                self.push_screen(ViewHelp(player, self.viewer_command(player), error))
                return
            released = True
            if operation == "refresh" and self._terminal_player_id is not None:
                released = self._release_terminal()
            for affected_player in affected:
                self.preview_errors[affected_player.id] = error
                previous = self.observations[affected_player.id]
                self.observations[affected_player.id] = Observation(
                    "unknown", error, previous.pane_id, time.time()
                )
            self.busy = False
            self._render_players()
            if released:
                self._status(error, error=True)
            return
        if operation == "attach":
            self._status(result)
        elif operation == "refresh":
            result, preview, preview_error, saved_play = result
            if self.play_store is not None:
                if saved_play is not None and saved_play.project.resolve() != self.coach.cwd.resolve():
                    self._routing_error = "The saved play now targets a different project. Reopen Courtside to switch projects."
                elif saved_play is None and self.play is not None:
                    self._routing_error = "The saved play was removed. Restore it or reopen Courtside."
                else:
                    self.play = saved_play
                if self._routing_error:
                    self.busy = False
                    released = self._release_terminal()
                    self._render_players()
                    if released:
                        self._status(self._routing_error, error=True)
                    return
            if preview_error is not None:
                self.preview_errors[player.id] = preview_error
            elif preview is not None:
                self.preview_errors.pop(player.id, None)
                self.previews[player.id] = preview
            for roster_player in self.runtime_players:
                self.observations[roster_player.id] = result.get(
                    roster_player.id,
                    Observation("unknown", "Herdr returned no observation for this player."),
                )
                observation = self.observations[roster_player.id]
                if observation.activity == "stopped":
                    self.previews.pop(roster_player.id, None)
                    self.preview_errors.pop(roster_player.id, None)
                elif observation.activity == "unknown":
                    self.preview_errors[roster_player.id] = observation.detail
            terminal_invalid = False
            if self._terminal_player_id is not None:
                live_observation = self.observations[self._terminal_player_id]
                terminal_invalid = live_observation.activity in {"unknown", "stopped"}
                if terminal_invalid and self._release_terminal():
                    self._status("Player identity is no longer verified; input released. " + live_observation.detail, error=True)
            if (self._report_refresh or self._refresh_failed) and not terminal_invalid:
                self._status("You coach · Q chooses stop team or keep running.")
            self._report_refresh = False
            self._refresh_failed = False
        elif operation in {"start", "coach_start"}:
            self.observations[player.id] = result
            if result.activity in {"unknown", "stopped"}:
                self._status(f"{player.name}: Codex is not confirmed. Inspect activity before retrying.", error=True)
            else:
                self._status(f"{player.name}'s session is available. Enter lets you type here; O opens a separate pane.")
        elif operation == "stop":
            self.observations[player.id] = Observation(
                "stopped", "The owned session was stopped by the coach.", observed_at=time.time()
            )
            self.previews.pop(player.id, None)
            self.preview_errors.pop(player.id, None)
            self._status(f"{player.name}'s session stopped.")
        for player_id, observation in self.observations.items():
            if observation.activity != "unknown":
                self._last_known[player_id] = observation
        self.busy = False
        self._render_players()
        if (operation == "coach_start" and self.coach_selected
                and generation == self._terminal_generation
                and self.observations[self.coach.id].activity not in {"unknown", "stopped"}):
            self.action_attach_player()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.focused, TerminalWidget):
            return False
        if self.coach_selected and action == "select_coach":
            return False
        return True

    def on_click(self, event: events.Click) -> None:
        if event.widget is not None and event.widget.id in {"player-output", "preview-scroll", "preview-label"}:
            event.stop()
            if self._terminal_player_id is None:
                self.action_attach_player()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        if (self.is_running and isinstance(event.widget, TerminalWidget)
                and not event.widget.has_focus and self._terminal_player_id is not None):
            self._release_terminal()

    def _render_terminal_label(self) -> None:
        player = next((p for p in self.runtime_players if p.id == self._terminal_player_id), None)
        if player is None:
            return
        terminal = self.query_one("#player-terminal", TerminalWidget)
        state = "LIVE · Type here" if terminal.connected else "Connecting"
        if self._terminal_close_failed:
            state = "Input stopped · release incomplete"
        label = self.query_one("#preview-label", Static)
        text = f"{player.name} · {state} · Ctrl+G coach"
        if label.content != text:
            label.update(text)

    def _poll_terminal(self) -> None:
        if not self.is_running:
            return
        session = self._terminal_session
        if session is None or self._terminal_close_failed:
            return
        terminal = self.query_one("#player-terminal", TerminalWidget)
        try:
            for record in session.poll():
                if isinstance(record, TerminalClosed):
                    if self._release_terminal():
                        self._status(record.reason or "Input released · Player remains running. Q manages team shutdown.",
                                     error=bool(record.reason))
                    return
                if not isinstance(record, TerminalFrame):
                    continue
                if self._terminal_player_id != self.selected_session.id:
                    self._release_terminal()
                    return
                if record.full:
                    terminal.reset_terminal()
                terminal.resize_terminal(record.columns, record.rows)
                if not terminal.connected and session.ready:
                    if not terminal.has_focus:
                        self._release_terminal()
                        return
                    terminal.connected = True
                    self._status(f"Typing to {self.selected_session.name} · Ctrl+G returns to Coach.")
                terminal.feed(record.data)
                self._render_terminal_label()
            self._resize_terminal_client()
        except (TerminalSessionError, OSError, ValueError) as exc:
            if self._release_terminal():
                self._status(str(exc), error=True)

    def _resize_terminal_client(self) -> None:
        session = self._terminal_session
        if (session is not None and session.ready and not self._terminal_close_failed
                and self._terminal_size != self._terminal_sent_size):
            session.resize(*self._terminal_size)
            self._terminal_sent_size = self._terminal_size

    def on_terminal_widget_resize(self, event: TerminalWidget.Resize) -> None:
        event.stop()
        if not self.is_running:
            return
        self._terminal_size = (min(1000, max(2, event.columns)), min(1000, max(2, event.rows)))
        try:
            self._resize_terminal_client()
        except (TerminalSessionError, OSError) as exc:
            if self._release_terminal():
                self._status(str(exc), error=True)

    def on_terminal_widget_input(self, event: TerminalWidget.Input) -> None:
        event.stop()
        session = self._terminal_session
        terminal = self.query_one("#player-terminal", TerminalWidget)
        if session is None or not terminal.connected or self._terminal_close_failed:
            return
        try:
            session.send(event.data)
        except (TerminalSessionError, OSError) as exc:
            if self._release_terminal():
                self._status(str(exc), error=True)

    def on_terminal_widget_released(self, event: TerminalWidget.Released) -> None:
        event.stop()
        if event.generation is not None and event.generation != self._terminal_generation:
            return
        if self._release_terminal():
            if event.to_coach:
                self.action_select_coach()
                self._status("Coach controls active · Player remains running. Q manages team shutdown.")

    def on_terminal_widget_attach_requested(self, event: TerminalWidget.AttachRequested) -> None:
        event.stop()
        if self._terminal_player_id is None:
            self.action_attach_player()

    def _release_terminal(self) -> bool:
        """Release only our input client; failed cleanup stays owned and visible."""
        if self._terminal_player_id is None and self._terminal_session is None:
            return True
        self._terminal_generation += 1
        terminal = self.query_one("#player-terminal", TerminalWidget)
        terminal.connected = False
        if self._terminal_session is not None:
            try:
                self._terminal_session.close()
            except Exception as exc:
                self._terminal_close_failed = True
                terminal.focus()
                self._render_terminal_label()
                self._status("Could not release the terminal client: " + str(exc)
                             + " · Ctrl+G retries; other actions remain blocked.", error=True)
                return False
        self._terminal_session = None
        self._terminal_player_id = None
        self._terminal_close_failed = False
        self._terminal_sent_size = None
        self.screen_stack[0].remove_class("terminal-active")
        if self.coach_selected:
            self.query_one("#coach-nav", Button).focus()
        else:
            self.query_one("#roster", ListView).focus()
        self._render_players()
        return True

    def on_unmount(self) -> None:
        if self._terminal_session is not None:
            try:
                self._terminal_session.close()
            except Exception as exc:
                print(f"Courtside could not release its terminal client: {exc}", file=sys.stderr)
            else:
                self._terminal_session = None

def view_player(backend: Herdr, player: Player) -> int:
    """Attach once; detach, failure, or agent exit also ends the viewer process."""
    try:
        command, environment = backend.attachment(player)
        result = subprocess.run(command, env=environment, check=False)
        if result.returncode:
            print("\nAttachment unavailable. If this terminal is already attached elsewhere, "
                  "detach that view first (Ctrl+B, then Q; inside Herdr: Ctrl+B twice, then Q).",
                  file=sys.stderr)
        return result.returncode
    except (BackendError, OSError) as exc:
        print(f"{player.name}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Courtside: coach your local Codex roster through Herdr."
    )
    parser.add_argument("--config", type=Path, default=Path.cwd() / "dreamteam.toml",
                        help="Path to the roster and saved play")
    parser.add_argument("--view", metavar="PLAYER", help="Open one player in this terminal; keep Courtside in another pane")
    args = parser.parse_args()
    config = args.config.expanduser().resolve()
    try:
        players = load_roster(config)
        backend = Herdr(config.parent)
        app = CourtsideApp(players, backend, play_store=PlayStore(config.parent), config=config)
    except (OSError, ValueError, BackendError) as exc:
        print(f"dreamteam: {exc}", file=sys.stderr)
        if not config.exists():
            print("Run dreamteam from a project containing dreamteam.toml.", file=sys.stderr)
        raise SystemExit(2) from exc
    if args.view:
        player = next((p for p in app.runtime_players if p.id == args.view), None)
        if player is None:
            parser.error("Unknown player. Choose: " + ", ".join(p.id for p in app.runtime_players))
        raise SystemExit(view_player(app.backend, player))
    else:
        report = app.run()
        if report:
            print(report)


if __name__ == "__main__":
    main()
