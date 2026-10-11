# Dreamteam

Coach a roster of Codex players from **Courtside**, a basketball-themed terminal UI. Prompt your own Coach conversation to assign work, then select a player to follow or join their live Codex session. Herdr owns the terminal sessions.

Courtside supports six named players, an interactive Codex Coach, live terminals inside the TUI, tool-based assignments and progress reports, and one saved play with a project target. Confirmed passes and acceptance controls are next in [the design](design.html).

## Run

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), [Herdr](https://herdr.dev), and an installed, signed-in Codex CLI. This milestone was verified on macOS with Herdr 0.9.3 and Codex CLI 0.162.1.

From this repository:

```sh
uv sync
uv run dreamteam
```

From Fastbreak, use Dreamteam's project and roster explicitly:

```sh
cd /Users/babasoori/Developer/fastbreak
uv run --project /Users/babasoori/Developer/dreamteam dreamteam \
  --config /Users/babasoori/Developer/dreamteam/dreamteam.toml
```

`--project` selects Dreamteam's application and dependencies. `--config` selects the roster and its saved play, which currently targets Fastbreak. The launch directory does not override a saved play's target.

No API key or model SDK is needed. Coach and players use your installed, signed-in Codex CLI. Opening Courtside starts no agents. **Enter** on Coach starts that conversation; Coach starts a player when you ask it to dispatch work. **S** also starts the selected session manually. Dreamteam adds [session-local MCP tools](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) and instructions without changing global Codex configuration or sandbox settings.

## Courtside controls

| Key | Action |
| --- | --- |
| C / click Coach | Select your Coach workspace |
| 1–6 | Select a player |
| ↑ / ↓ | Move between Coach and the players |
| N | Create a play: project folder, goal, constraints, and acceptance criteria |
| B | View Penny's saved planning brief; Ctrl+Y requests a clipboard copy |
| S | Start Coach or the selected player, or reuse their verified session |
| Enter | Open the selected live Codex terminal; starts Coach if needed |
| Click terminal preview | Type directly into the selected Codex conversation |
| Ctrl+G (in a live terminal) | Release input and select Coach |
| O | Open or reuse an optional separate native Herdr player pane |
| R | Refresh observed activity |
| X | Confirm stopping only Coach or the selected player |
| Q | Choose Stop team and quit (default), Keep running and quit, or Cancel |

Courtside opens with **Coach** selected above the starting five. Press **Enter** and prompt its own interactive Codex session. For example:

> Ask Penny to inspect Fastbreak's downloaded TWS API offline and propose the smallest Python wrapper and CLI plan. Do not modify files or connect to a broker.

Coach checks the saved play, roster activity, assignments, and reports through Dreamteam tools, then sends a concrete assignment to the named player. Press **Ctrl+G**, then **1** to select Penny and watch his output; **Enter** lets you type to him directly. Press **Ctrl+G**, then **Enter** to return to the Coach conversation. Switching views leaves the work running.

Coach's awareness comes from tool snapshots, explicit player reports, and recent terminal output. It checks on each request and can wait briefly for a report; it is not a continuous background supervisor. Ask “What is Penny working on?” or “Read Penny's result and have Shaq review it.” The assignment ledger records transport delivery separately from reported progress. The play's planned/unassigned fields remain the saved brief's state; confirmed possession transfers are still a later feature. An idle terminal or a reported result does not mean you have accepted the work.

New player sessions receive a progress-report tool. Already-running sessions keep their conversation; if they lack that tool, Coach can read their explicit visible result and record it with `coach_observed` provenance. Neither attaching nor refreshing silently restarts a player.

Select a player, press **S** to start if needed, then **Enter** or click the preview to type in that player's existing Codex conversation. While the live terminal has focus, letters, numbers, arrows, Tab, and Ctrl+C go to Codex. **Ctrl+G** selects the Coach workspace and releases the player's input client; Courtside shortcuts become available again. Pasted multiline text stays a paste rather than submitting each line.

Inline interaction creates no extra pane, tab, workspace, or agent. Leaving input mode closes and reaps its Herdr client and releases input ownership; the player session remains available. Before entering, and after leaving, Courtside shows a read-only snapshot refreshed every three seconds. It shows the current viewport, not a full scrollback transcript. Failed reads are labeled stale or unavailable.

For a separate native view inside Herdr, **O** opens a sibling player pane without changing focus. Click it to type, or use your Herdr pane-navigation keys (default **Ctrl+B, then H/J/K/L**). Opening the same player again reuses the recorded pane, including from another Courtside instance on that Herdr server. A horizontal split needs at least 160×24; a vertical split needs 80×48. If there is not enough room, or Courtside runs outside Herdr, it shows a command you can copy into a terminal split or tab:

```sh
cd /Users/babasoori/Developer/dreamteam
uv run dreamteam --view penny
```

Use `coach` or the other roster IDs for additional views: `air_jordan`, `king_james`, `sir_charles`, `shaq`, and `the_answer`. From another folder, provide `--config /Users/babasoori/Developer/dreamteam/dreamteam.toml` to the installed command. Each player has one interactive attachment, whether inline or native; multiple Courtside previews can observe it. Dreamteam never takes over an attachment automatically.

Detach from a native attachment with **Ctrl+B, then Q**. Inside an outer Herdr terminal with the default prefix, use **Ctrl+B twice, then Q**. Detaching, attachment failure, or agent exit ends the viewer process and returns to your shell. Rerun the viewer command to reconnect, or close that managed pane and use Courtside's O action to open another. If the player is already attached in an older tab, detach that older view once, then use Enter to type inline or O for a separate pane. Changing a custom outer prefix changes the required literal-prefix sequence.

To load this update, quit the old Courtside with **Q** and relaunch `uv run dreamteam`. After restarting, **Q** opens the shutdown choices. Choose **Keep running** only when you explicitly want sessions to persist. **X** stops the selected Coach or player.

Penny (PG), Air Jordan (SG), King James (SF), Sir Charles (PF), Shaq (C), and The Answer (sixth man) all use Codex. You are the coach. Names and positions come from `dreamteam.toml`; its directories resolve relative to that file. A saved play routes all six players to that play's project folder. Coach uses those roles when assigning bounded work. The optional saved Penny brief remains available for manual delivery.

The interface follows coding-agent CLIs and terminal tools such as [Fresh](https://getfresh.dev/): a compact project header, slim roster rail, sparse main workspace, and plain keyboard hints. The palette blends Ghostty’s Ultra Dark, [Nord](https://www.nordtheme.com/docs/colors-and-palettes/), and [Tokyo Night](https://github.com/folke/tokyonight.nvim): a near-black base, cool gray text, blue selection and key hints, and restrained violet accents. Coach and all six players fit at 80×24, with more breathing room at 120×35. It honors your terminal's color settings, including `NO_COLOR`.

## Call a play

Launch with your roster as shown above. Select **Coach** in the sidebar (or press **C**), then **N** or **Call a play**. Enter the target project folder, goal, optional constraints, and what counts as done. Use **Tab / Shift+Tab** between fields, **Ctrl+S** to save, or **Esc** to cancel. Relative project paths resolve from the roster directory.

Saving prepares play **001** with status **planned** and the ball **unassigned**. It does not launch players, connect to a broker, send a prompt, or confirm delivery. Selecting another player only changes the view. Reopening Courtside restores the same play and project; an unavailable project blocks startup rather than silently routing to another folder.

Press **B** to read Penny's brief. **Ctrl+Y** requests a clipboard copy using the terminal's clipboard support; the saved file is the fallback if your terminal does not support copying. Return with **Esc**, select Penny with **1**, then **S** and **Enter** to open his Codex session in the target project. Deliver the brief there. Penny is instructed to acknowledge receipt and wait for your go-ahead before inspecting or planning. Implementation needs a separate assignment.

You can also describe the play in Coach's terminal. Coach can create the saved play through its tool if none exists, and dispatch your requested assignment directly. The manual form and brief remain available; their save/copy actions alone never dispatch work.

The authoritative play is saved under `.dreamteam/possessions/001/state.json` beside your roster, with a derived `brief.md`. The viewer and copy action use the saved state. This slice supports one play and rejects replacement; editing, confirmed passes, and acceptance are not implemented yet.

Fastbreak is the first development target. Its initial play is a small Python wrapper over the downloaded official TWS API, exposed through `fastbreak --help`, an offline `doctor`, and a bounded `status --json`. Connection behavior is tested with simulated responses before a separately requested paper-TWS check. Strategies and order execution are later plays.

## Session behavior

Dreamteam creates a dedicated named Herdr server on the first Start. Its private configuration and verified Coach/player bindings live in the target project's `.dreamteam/` directory; keep that directory ignored by Git. Observing an unstarted project creates no runtime files. Herdr stores its named-session data in its normal configuration directory. Dreamteam does not change your global Herdr or Codex configuration. Switching from the roster's project to a play's project uses a separate runtime; existing sessions in the previous project stay untouched.

Reopening Courtside reconnects to sessions you chose to keep running. Repeated Start reuses a verified player. **Ctrl+G** returns from a player to coaching. **Q → Stop team and quit** closes verified managed viewer panes and Coach and player panes, removes their empty tabs/workspaces, and stops empty dedicated servers for both the active play and controller project. It checks the result before clearing saved bindings. Next Start creates a fresh runtime and conversation. **Keep sessions running and quit** is the explicit persistence option. Both choices release and reap the inline terminal client before leaving.

Cleanup never closes the current Courtside terminal or unrelated/repurposed panes. Those panes keep their host server alive and are listed in the terminal report after quitting. Unconfirmed managed cleanup keeps Courtside open with an error so you can retry or inspect the remaining resources. Manually created terminal tabs return to their own shells; Dreamteam does not own those tabs. Stopped Herdr session metadata may remain on disk, but no server process is kept for an empty runtime.

Detaching a viewer and exiting Codex itself are different actions: detach preserves the player session; exiting Codex ends that agent process. The live terminal directory may change without changing terminal identity. A missing Codex process is reported explicitly.

Unreachable or ambiguous sessions show **unknown**, with the last known observation retained. Idle is activity, not completed work. A changed server socket or mismatched pane, terminal, project, or agent identity blocks actions on stale bindings. Automatic recovery after a Herdr server or machine restart is not implemented yet; do not discard bindings while their sessions may still be running.

Coach tools run as a local stdio child of its Codex session: status, recent output, bounded dispatch, waits, play creation, and recorded progress. Player tools can report only for their own player ID. There is no HTTP listener or independent Coach daemon. Assignments live in the target project's ignored `.dreamteam/coordination/` directory. Repeating a request ID returns the recorded assignment; uncertain delivery is never automatically retried. Stopping Coach prevents its tool process from launching or prompting more players; stopping the team closes Coach before the players. Cancelling a tool request prevents later startup or delivery steps, but cannot undo input already submitted. If you abandon a task after stopping its player, ask Coach to close that stopped assignment: it is recorded as cancelled, and late reports cannot revive it. Unresolved delivery preparation requires a replacement Coach before it can be closed.

Codex handles its own prompts and approvals in the selected terminal. The optional native view remains available with O. Live approval-dialog interaction through the embedded viewport has not yet been verified.

## Verification

```sh
uv run python -m unittest discover -s tests
```

Tests cover play validation and persistence, project routing and isolation, form and brief keyboard flows, roster validation, duplicate prevention, uncertain launch handling, stale or mismatched targets, selected-player actions, stop confirmation, UI responsiveness during backend work, passive previews and stale output, opening and reusing separate viewer panes, host/project isolation, uncertain view creation, explicit quit choices, one-shot viewers, verified cleanup, preservation of unrelated terminals, inline input routing, focus isolation, terminal resizing, paste framing, controller conflicts, client release/reaping, Coach/player focus, per-session tools, request-ID deduplication, explicit report ownership, cancelled requests, stopped-task recovery, and preventing dispatch after Coach shutdown.

The live macOS check exercised Penny's native prompt/response, detach, reopening Courtside, reattachment to the same conversation, repeated Start, a separate Air Jordan session, and stopping Air Jordan while Penny stayed running. No project files were changed by either player.

An isolated Herdr shell check also exercised real inline typing, resize, Ctrl+G, reconnection, quit, and active-app teardown through Courtside. All three controller processes were reaped; the player pane count stayed one before and after. The temporary test server was stopped and deleted. This check did not send input to the existing Penny session or exercise a Codex approval dialog.

An isolated real Coach/Penny check verified the complete tool flow: Coach read team status,
dispatched a read-only task, waited for Penny's explicit working/done reports, read his output,
and returned the result while distinguishing self-report from human acceptance. Both test
panes and the dedicated server were closed, saved test metadata removed, and no test-project
processes remained. The existing Fastbreak sessions and broker APIs were not used.
