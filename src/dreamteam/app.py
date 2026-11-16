"""Courtside: a small terminal sideline for the local Codex roster."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import time

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
from .runtime import BackendError, Rex, Observation, Preview


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
        Binding("enter", "select_cursor", "Open tab"),
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
                "This closes this player's owned Rex pane and ends its running session. "
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
                "Stop team closes verified player tabs. Running tasks will end. "
                "Your Rex server, active terminal, and "
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
    """All backend operations are serialized; session state belongs to Rex."""

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
        Binding("enter", "attach_player", "Open tab"),
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
        self, players: tuple[Player, ...], backend: Rex, *, play_store: PlayStore | None = None,
        config: Path | None = None,
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
            player.id: Observation("unknown", "Waiting for the first Rex observation.")
            for player in self.runtime_players
        }
        self._last_known: dict[str, Observation] = {}
        self._report_refresh = False
        self._refresh_failed = False

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
        self.backend = Rex(play.project)

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
                        yield Static("Enter opens Coach in Rex · N call a play · B brief", id="coach-hint", markup=False)
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
                yield Static(
                    "Enter / O opens the native Rex tab · Return using Rex tabs",
                    id="attach-help",
                    markup=False,
                )
                yield Static("", id="next-action", markup=False)
        yield Static("Checking Rex…", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.screen_stack[0].set_class(self.size.width < 100 or self.size.height < 34, "compact")
        self.query_one("#coach-nav", Button).focus()
        self._render_players()
        self.action_refresh_players()
        self.set_interval(3, self._automatic_refresh)

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
            "Coach" if self.coach_selected else "Open tab"
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
        # by their own Rex runtime; never relabel or reuse them for a different project.
        self.play = play
        try:
            self._route_project(play)
        except ValueError as exc:
            self._routing_error = str(exc)
        self.observations = {
            p.id: Observation("unknown", self._routing_error or "Waiting for this project's first Rex observation.")
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
            hint = Text("Waiting for Rex…", style=MUTED)
        elif activity == "stopped":
            key = "↵  " if self.coach_selected else "s  "
            hint = Text.assemble((key, ACCENT), (f"Start {player.name}", TEXT))
        elif activity == "unknown":
            hint = Text.assemble(("r  ", ACCENT), (f"Check {player.name}'s session", TEXT))
        else:
            hint = Text.assemble(("↵  ", ACCENT), (f"Open {player.name} in Rex", TEXT))
        self.query_one("#next-action", Static).update(hint)

    def _status(self, message: str, *, error: bool = False) -> None:
        self.query_one("#status", Static).update(
            Text(message, style="#f7768e" if error else MUTED)
        )

    def _can_operate(self) -> bool:
        if len(self.screen_stack) > 1:
            return False
        if self.busy:
            self._status("A Rex operation is still running. Wait for it to finish.")
            return False
        return True

    def action_select_coach(self) -> None:
        if len(self.screen_stack) > 1:
            return
        self.coach_selected = True
        self._render_players()
        self.query_one("#coach-nav", Button).focus()

    def action_select_player(self, index: int) -> None:
        if len(self.screen_stack) > 1 or not 0 <= index < len(self.players):
            return
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
            self._status(self._routing_error + " · Restore it and reopen Courtside.", error=True)
            return
        self.busy = True
        self._render_action_hint()
        self._perform(operation, self.selected_session if operation == "refresh" else player)

    def action_start_player(self) -> None:
        if self._can_operate():
            self._status(f"Starting {self.selected_session.name}…")
            self._begin("start", self.selected_session)

    def action_attach_player(self) -> None:
        if not self._can_operate():
            return
        player = self.selected_session
        operation = ("coach_start" if self.coach_selected
                     and self.observations[player.id].activity == "stopped" else "attach")
        self._status(f"Opening {player.name} in Rex…")
        self._begin(operation, player)

    def action_open_alongside(self) -> None:
        self.action_attach_player()

    def action_refresh_players(self) -> None:
        if self._can_operate():
            self._status("Refreshing Rex observations…")
            self._report_refresh = True
            self._begin("refresh")

    def _automatic_refresh(self) -> None:
        if (not self.busy and not self._routing_error
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
                self.exit("Courtside closed. You chose to keep player sessions running.")
            elif choice == "stop":
                self._status("Stopping managed Rex tabs…")
                self.busy = True
                self._render_action_hint()
                self._perform("shutdown", None)

        self.push_screen(QuitTeam(), chosen)

    @work(thread=True, exit_on_error=False)
    def _perform(self, operation: str, player: Player | None) -> None:
        result = None
        error = None
        try:
            if operation == "shutdown":
                reports = [self.backend.shutdown(self.runtime_players)]
                if self.controller_backend is not self.backend:
                    reports.append(self.controller_backend.shutdown(self.controller_runtime_players))
                result = "\n".join(reports)
            elif operation == "refresh":
                observations = self.backend.refresh(self.runtime_players)
                preview, preview_error = None, None
                try:
                    preview = self.backend.preview(player)
                except (BackendError, OSError) as exc:
                    preview_error = str(exc)
                saved_play = self.play_store.load() if self.play_store is not None else None
                result = observations, preview, preview_error, saved_play
            elif operation in {"start", "coach_start"}:
                result = self.backend.start(player)
                if operation == "coach_start" and result.pane_id:
                    self.backend.open(player)
            elif operation == "stop":
                self.backend.stop(player)
            elif operation == "attach":
                result = self.backend.open(player)
        except BackendError as exc:
            if operation == "refresh" and exc.code == "runtime_busy":
                operation = "refresh_deferred"
            else:
                error = str(exc)
        except Exception as exc:
            error = f"Rex operation failed: {exc}"
        self.call_from_thread(self._complete, operation, player, result, error)

    def _complete(self, operation, player, result, error) -> None:
        self.busy = False
        if operation == "refresh_deferred":
            self._render_action_hint()
            return
        if operation == "shutdown":
            if error:
                self._status("Cleanup incomplete: " + error, error=True)
                self._render_action_hint()
            else:
                self.exit(result)
            return
        if error:
            if operation in {"attach", "coach_start"}:
                self._status(error, error=True)
                self._render_action_hint()
                return
            if operation == "refresh":
                self._refresh_failed = True
            for affected in self.runtime_players if operation == "refresh" else (player,):
                self.preview_errors[affected.id] = error
                previous = self.observations[affected.id]
                self.observations[affected.id] = Observation("unknown", error, previous.pane_id, time.time())
            self._render_players()
            self._status(error, error=True)
            return
        if operation == "attach":
            self._status(result)
        elif operation == "refresh":
            observations, preview, preview_error, saved_play = result
            if self.play_store is not None:
                if saved_play is not None and saved_play.project.resolve() != self.coach.cwd.resolve():
                    self._routing_error = "The saved play now targets a different project. Reopen Courtside to switch projects."
                elif saved_play is None and self.play is not None:
                    self._routing_error = "The saved play was removed. Restore it or reopen Courtside."
                else:
                    self.play = saved_play
                if self._routing_error:
                    self._render_players()
                    self._status(self._routing_error, error=True)
                    return
            if preview_error:
                self.preview_errors[player.id] = preview_error
            elif preview is not None:
                self.preview_errors.pop(player.id, None)
                self.previews[player.id] = preview
            for member in self.runtime_players:
                observation = observations.get(member.id, Observation("unknown", "No Rex observation."))
                self.observations[member.id] = observation
                if observation.activity == "stopped":
                    self.previews.pop(member.id, None)
                    self.preview_errors.pop(member.id, None)
                elif observation.activity == "unknown":
                    self.preview_errors[member.id] = observation.detail
            if self._report_refresh or self._refresh_failed:
                self._status("Enter opens a Rex tab · Q chooses stop team or keep running.")
            self._report_refresh = self._refresh_failed = False
        elif operation in {"start", "coach_start"}:
            self.observations[player.id] = result
            self._status(f"{player.name}: {result.detail} Enter opens its Rex tab.")
        elif operation == "stop":
            self.observations[player.id] = Observation("stopped", "The owned tab was stopped.", observed_at=time.time())
            self.previews.pop(player.id, None)
            self.preview_errors.pop(player.id, None)
            self._status(f"{player.name}'s tab stopped.")
        for key, observation in self.observations.items():
            if observation.activity != "unknown":
                self._last_known[key] = observation
        self._render_players()

    def on_click(self, event: events.Click) -> None:
        if event.widget is not None and event.widget.id in {"player-output", "preview-scroll", "preview-label"}:
            event.stop()
            self.action_attach_player()


def view_player(backend: Rex, player: Player) -> int:
    """Select the existing native Rex tab without attaching another terminal."""
    try:
        print(backend.open(player))
        return 0
    except (BackendError, OSError) as exc:
        print(f"{player.name}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Courtside: coach your local Codex roster through Rex."
    )
    parser.add_argument("--config", type=Path, default=Path.cwd() / "dreamteam.toml",
                        help="Path to the roster and saved play")
    parser.add_argument("--view", metavar="PLAYER", help="Select an existing player tab in Rex")
    args = parser.parse_args()
    config = args.config.expanduser().resolve()
    try:
        players = load_roster(config)
        backend = Rex(config.parent)
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
