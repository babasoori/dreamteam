# Dreamteam

One native Rex tab: **Coach on the left, players stacked on the right.** Dreamteam launches the agents and tracks plays, assignments, and progress reports. Rex provides the sidebar, tabs, splits, resizing, focus, and live terminals.

```text
Dreamteam · project
┌─────────────────────┬─────────────────────┐
│                     │ Penny               │
│ Coach               ├─────────────────────┤
│                     │ Shaq                │
└─────────────────────┴─────────────────────┘
```

There is no Courtside dashboard, custom sidebar, terminal preview, or Textual dependency.

## Launch

Requires Python 3.12+, uv, a running local [Rex](https://www.superlogical.com/) session with its CLI on PATH, and an installed, signed-in native Codex CLI. The adapter was checked against Rex Server & CLI `0.1.0-preview.19` (API revision 14).

From this repository, inside Rex:

```sh
uv run dreamteam
```

This starts or reuses Coach in a tab named `Dreamteam · <project>` and selects its pane. The launcher then returns to your shell; agents stay running. Talk directly to Coach:

> Ask Penny to inspect this project and propose the smallest implementation plan. Do not modify files. Read his result before assigning a review to Shaq.

The first dispatched player splits to Coach’s right. Each subsequent player splits below the lowest existing player. Coach-driven launches preserve focus. Use Rex’s own sidebar and pane navigation to switch, resize, or zoom. Players appear on demand; the whole roster is not launched at startup.

From another directory:

```sh
uv run --project /Users/babasoori/Developer/dreamteam dreamteam \
  --config /Users/babasoori/Developer/dreamteam/dreamteam.toml
```

The saved play determines the target project. Without a saved play, Coach works in the roster directory, and players use their roster directories. The launch directory does not override a saved play.

## Commands

Use these in a shell pane. Put `--config PATH` before the subcommand when using another roster.

| Command | Action |
| --- | --- |
| `dreamteam` | Start/reuse Coach and focus its pane |
| `dreamteam start penny` | Ensure Coach exists, start/reuse Penny in the right column, and focus Penny |
| `dreamteam open coach` | Focus an existing pane without starting an agent |
| `dreamteam open penny` | Focus Penny; `--view penny` also works |
| `dreamteam status` | Print Coach, roster activity, and assignments as JSON |
| `dreamteam stop penny` | Stop only Penny’s verified managed pane |
| `dreamteam stop team` | Stop Coach first, then the managed players |
| `dreamteam brief` | Print the saved planning brief |

The roster IDs are `penny`, `air_jordan`, `king_james`, `sir_charles`, `shaq`, and `the_answer`; `coach` identifies the coordinator. Run `dreamteam --help` for usage.

Selecting a pane changes focus within its saved Rex session. If the app displays another session, select the reported session in Rex. Dreamteam does not modify Rex’s sidebar settings, key bindings, or client Remote Control settings.

Closing the launcher shell does not stop agents. Stop commands are explicit actions and do not display a confirmation dialog. Closing the entire Dreamteam tab through Rex also ends its agents. Unresolved assignments remain in the ledger; closing a pane never marks work completed.

## Plays and reports

Ask Coach to define a play through its tools, or save one before launching:

```sh
uv run dreamteam play \
  --project /absolute/path/to/project \
  --goal "Inspect the project and propose a small change" \
  --constraints "Read only; no external services" \
  --acceptance "A reviewed implementation plan"
uv run dreamteam
```

Saving a play or printing its brief does not launch agents or dispatch work. Relative project paths resolve from the roster directory. One planned, unassigned play is supported; replacing plays, confirmed passes, and acceptance controls remain future work.

Penny plans, Air Jordan implements, King James integrates, Sir Charles checks, Shaq reviews, and The Answer brings a fresh angle. All currently use Codex. Coach reads status, assignments, explicit reports, and bounded recent output through session-local Dreamteam MCP tools. It acts on requests rather than supervising continuously.

Players report working, blocked, or done through their own scoped tools. **Submitted means Rex queued the input bytes; it does not prove Codex consumed the assignment.** Idle is not completion, and reported done still needs review and human acceptance. Request IDs deduplicate assignments; uncertain deliveries are never automatically replayed.

No model SDK, API key, or third-party Python runtime dependencies are needed. Global Codex configuration and approval settings stay unchanged. The Rex documentation MCP helps develop Dreamteam but is not required to run it.

## Layout and lifecycle

The first launch binds to the current local Rex server instance and session. Coach creates one named tab. Each player’s split uses an explicit verified anchor in that tab, never the app’s currently focused pane. Repeated starts reuse saved blocks and processes. An interrupted launch is reconciled by its unique label before another creation is allowed.

The initial split is 50/50. Later splits divide the bottom player pane in half; resize or zoom with Rex as the team grows. Adding a player requires Coach on the left and a vertical column of managed players on the right. Rearranged or unmanaged panes block automatic additions rather than being moved or overwritten.

Rex program-status records must belong to the managed Codex process before automatic input is allowed. Input also requires Codex to own the foreground and enable bracketed paste. Unknown startup/authentication states require inspection in the native pane. Do not type into a player’s prompt while Coach dispatches: Rex does not offer an atomic agent-ready-and-submit operation.

Output reads are bounded to 80 recent lines and 32 KiB after capture. In an alternate-screen program, Rex may return only the current screen, not a full conversation transcript.

Stopping the team verifies closure before clearing bindings and leaves your Rex server, session, and unrelated tabs alone. If Coach is stopped while players remain, stop the remaining team before launching a replacement Coach. An exited agent’s retained pane must be closed with `dreamteam stop PLAYER` before restarting it.

Changed server instances, labels, processes, or moving a managed pane to another tab block automatic actions. Recovery after a server restart is not implemented. Inspect existing sessions before changing runtime files.

## Saved state and migration

- Roster: `dreamteam.toml`.
- Play: `.dreamteam/possessions/001/state.json` beside the roster.
- Assignments: `.dreamteam/coordination/` in the target project.
- Rex bindings and layout: `.dreamteam/rex-runtime.json` in the target project, with `rex-runtime.lock` for serialization.

These runtime directories should remain ignored by Git. Roster, play, and assignment formats are unchanged.

Existing separate-tab Rex bindings from the previous version are not rearranged automatically. Inspect any active work, run `dreamteam stop team` when ready to end those conversations, then run `dreamteam` to create the new layout. This starts fresh conversations.

Legacy Herdr `runtime.json`, `views.json`, `herdr.toml`, and `herdr.log` files remain untouched. Existing Herdr processes and conversations are not stopped or transferred. Old unresolved assignments remain in the ledger; inspect them and the old sessions before retiring or replacing work.

## Verification

```sh
uv run python -m unittest discover -s tests
```

105 tests cover launch commands, play routing, exact split anchors, layout preservation, launch reconciliation, identity checks, cleanup, input framing, uncertain delivery, request-ID deduplication, report ownership, cancellation, and Coach guards.

A real Rex test with three temporary Python terminal fixtures verified Coach-left/player-top-right/player-bottom-right geometry, one shared named tab, focus preservation, pane reuse, multiline UTF-8 delivery, output capture, and cleanup preserving the original tabs. No AI agents were launched for that check. Full real Codex Coach/player dispatch and approval-dialog interaction on Rex remain unverified.
