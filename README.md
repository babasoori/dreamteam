# Dreamteam

Coach a roster of Codex players from **Courtside**, a basketball-themed status board. Dreamteam manages plays, assignments, and explicit progress reports. **Rex owns the native agent terminals.**

## Run

Requires Python 3.12+, uv, a running local [Rex](https://www.superlogical.com/) session with its CLI on PATH, and an installed, signed-in native Codex CLI. The Rex adapter was checked against Server & CLI `0.1.0-preview.19` (API revision 14).

Run inside a Rex terminal:

```sh
cd /Users/babasoori/Developer/dreamteam
uv sync
uv run dreamteam
```

From another project, use the roster explicitly:

```sh
uv run --project /Users/babasoori/Developer/dreamteam dreamteam \
  --config /Users/babasoori/Developer/dreamteam/dreamteam.toml
```

A saved play determines the project target; the launch directory does not override it. Opening Courtside starts no agents. Press Enter on Coach to start its Codex conversation and select its native Rex tab. Use Rex’s tabs to return to Courtside.

No model SDK or API key is required. Coach and players use the installed Codex CLI, with session-local Dreamteam MCP tools. Global Codex configuration and approval settings are unchanged. The Rex documentation MCP is useful for developing Dreamteam; it is not a runtime dependency.

## Controls

| Key | Action |
| --- | --- |
| C / click Coach | Select Coach on the status board |
| 1–6 / ↑↓ | Select a player |
| N | Save a play with project, goal, constraints, and acceptance criteria |
| B | Read Penny’s saved planning brief; Ctrl+Y requests a copy |
| S | Start the selected agent, or reuse its verified session |
| Enter / O / click output | Select the existing native Rex tab; starts Coach if necessary |
| R | Refresh observed activity and read-only output |
| X | Confirm stopping the selected agent’s managed tab |
| Q | Stop team and quit, keep sessions running, or cancel |

Players start in background tabs without taking focus. Select their tabs to interact directly with Codex, including approval dialogs. There is no embedded terminal, extra viewer process, or Ctrl+G attachment mode. Enter and O select the same existing tab; they do not create another agent.

You can also select an existing tab from the CLI:

```sh
uv run dreamteam --view penny
```

The other IDs are `coach`, `air_jordan`, `king_james`, `sir_charles`, `shaq`, and `the_answer`. This selects the tab within its saved Rex session; if your app is displaying a different session, select the reported session in Rex. Dreamteam does not change client Remote Control settings.

## Coaching

Penny plans, Air Jordan implements, King James integrates, Sir Charles checks, Shaq reviews, and The Answer brings a fresh angle. Names and roles come from `dreamteam.toml`; all currently use Codex.

For example, tell Coach:

> Ask Penny to inspect this project and propose the smallest implementation plan. Do not modify files. Read his result before assigning a review to Shaq.

Coach reads the saved play, roster activity, assignments, explicit reports, and recent output through Dreamteam tools. It is request-driven, not a continuous supervisor. Players report working, blocked, or done through their own scoped tools. A legacy report recorded by Coach retains `coach_observed` provenance.

The ledger distinguishes prompt delivery from progress. **Submitted means Rex queued the input bytes. It does not prove Codex consumed the assignment.** Idle does not mean completed, and reported done still requires review and human acceptance. Repeating a request ID returns its recorded assignment. An uncertain delivery is never automatically resent.

The play form saves one planned, unassigned play. Saving or copying a brief does not dispatch work. Confirmed passes, acceptance controls, editing or replacing the saved play, and multiple plays are not implemented yet. See [the design](design.html).

## Runtime and state

On first Start, Dreamteam binds the target project to the current local Rex server instance and session. Each player runs directly in a separate native terminal block. Repeated Start reuses the verified block and process; a lost launch response is reconciled by its unique recorded label. An ambiguous launch blocks duplicate creation.

Runtime bindings live in the target project’s ignored `.dreamteam/rex-runtime.json`, with `rex-runtime.lock` for serialization. The saved play remains in `.dreamteam/possessions/001/state.json` beside the roster. Assignments remain in the target project’s `.dreamteam/coordination/` directory.

Activity comes from Rex’s program-status records, matched to the managed Codex process. A Codex version that does not emit those records remains unknown and cannot receive automatic prompts. Open its native tab to inspect startup or authentication. Input additionally requires Codex to own the foreground and enable bracketed paste. Do not type in the same prompt while Coach is dispatching: Rex queues terminal input and does not provide an atomic agent-ready-and-submit operation.

Read-only previews refresh every three seconds when Courtside is available. Output is limited to the most recent 80 lines and 32 KiB after capture. Rex’s alternate-screen capture contains the current screen, not an agent transcript; missing or failed reads are labeled unavailable or stale.

**X** closes only the verified selected block. **Q → Stop team and quit** stops Coach first, then the managed players, and verifies closure before clearing bindings. The Rex server, session, unrelated tabs, and current Courtside terminal remain running. Incomplete cleanup keeps Courtside open with an error. **Keep sessions running** preserves the conversations for reopening Courtside.

An exited agent’s retained tab can be closed with X before restarting with S. Changing a label, replacing a process, or restarting the Rex server invalidates automatic identity checks. Automatic adoption after server restart is not implemented; inspect the existing sessions before changing saved bindings. Dreamteam does not create or stop Rex servers.

## Migrating from Herdr

The roster, saved play, and assignment history keep their formats. Rex runtime bindings use a separate file. Existing Herdr `runtime.json`, `views.json`, `herdr.toml`, and `herdr.log` files are neither read nor overwritten; existing Herdr processes are not stopped or transferred.

This migration starts new Rex-hosted Codex conversations. It does not move running Herdr conversations. Old unresolved assignments remain unresolved in the shared ledger: inspect their reports and old sessions before retiring them or assigning replacement work. Stopping a Rex player does not stop an old Herdr player.

## Verification

```sh
uv run python -m unittest discover -s tests
```

The 117 passing tests cover roster and play validation, project routing, forms, native-tab navigation, stop/quit behavior, stale previews, nonblocking UI work, launch reconciliation, identity mismatches, scoped cleanup, multiline input, uncertain delivery, request-ID deduplication, explicit report ownership, cancellation, and Coach identity guards.

A temporary terminal fixture on real Rex verified startup, reuse of the same block, program-status observation, bracketed multiline UTF-8 input, output capture, and cleanup. It ran a small Python process, not an AI agent, and its tab was closed afterward. Full real Codex Coach/player dispatch and approval-dialog interaction on Rex remain unverified.
