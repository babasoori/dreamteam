"""Rex identity, launch, delivery and cleanup contracts; no real agents."""
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock

from dreamteam.roster import Player
from dreamteam.runtime import Rex, BackendError, PromptRejected, PromptUncertain, _plain_terminal_text


class FakeRex(Rex):
    def __init__(self, project):
        super().__init__(project)
        self.calls = []
        self.blocks = []
        self.layouts = {}
        self.next_id = 0
        self.processes = {}
        self.statuses = {}
        self.host = {"endpoint": "unix:///test/rex.sock", "host_id": "host-1",
                     "instance_id": "instance-1", "session_id": "session:one"}
        self.mode = True
        self.fail_launch = False
        self.fail_write = False
        self.partial_write = False
        self.fail_close = False
        self.on_launch = None
        self.output = "one\ntwo\nthree\n"

    def _call(self, state, *args, raw=False, input=None, timeout=8):
        self.calls.append((args, input))
        if args[0] == "whoami":
            return copy.deepcopy(self.host)
        if args[:2] == ("block", "ls"):
            return {"blocks": copy.deepcopy(self.blocks)}
        if args[0] in {"run", "split"}:
            self.next_id += 1
            number = self.next_id
            block_id = f"block:{number}"
            label = args[args.index("--label") + 1]
            cwd = args[args.index("-c") + 1]
            binary = args[args.index("--") + 1]
            anchor = args[args.index("-b") + 1] if args[0] == "split" else None
            window = next(b["window_id"] for b in self.blocks if b["block_id"] == anchor) if anchor else f"window:{number}"
            if anchor:
                direction = args[args.index("--split") + 1]
                def split(node):
                    if node.get("block_id") == anchor:
                        return {"split": {"direction": "horizontal" if direction == "right" else "vertical",
                                          "before": node, "after": {"block_id": block_id}}}
                    if "split" in node:
                        node["split"]["before"] = split(node["split"]["before"])
                        node["split"]["after"] = split(node["split"]["after"])
                    return node
                self.layouts[window] = split(self.layouts[window])
            else:
                self.layouts[window] = {"block_id": block_id}
            self.blocks.append({"block_id": block_id, "label": label, "host_id": "host-1",
                                "creator_name": "com.superlogical.terminal", "window_id": window})
            child = {"pid": number + 100, "name": "codex", "path": binary, "cwd": cwd}
            self.processes[block_id] = {"child": child, "foreground": dict(child), "last_exit": None}
            self.statuses[block_id] = [{"state": "idle", "app": "codex", "owner": {"pid": number + 100, "name": "codex"}}]
            if self.on_launch:
                self.on_launch()
            if self.fail_launch:
                raise BackendError("lost launch response")
            return {"block_ids": [block_id, anchor] if anchor else [block_id], "window_id": window}
        if args[:2] == ("window", "rename"):
            return ""
        if args[:2] == ("session", "inspect"):
            return {"windows": [{"window_id": key, "layers": [{"kind": "tiled", "layout": copy.deepcopy(value)}]}
                                for key, value in self.layouts.items()]}
        if args[:2] == ("block", "call"):
            block_id, method = args[5], args[6]
            if method == "process":
                return copy.deepcopy(self.processes[block_id])
            if method == "program_status":
                return {"records": copy.deepcopy(self.statuses[block_id])}
            if method == "modes":
                return {"modes": [{"name": "bracketed_paste", "set": self.mode}]}
            if method == "write":
                if self.fail_write:
                    raise BackendError("lost write response")
                return {"bytes_written": len(json.loads(input)["data"].encode()) - int(self.partial_write)}
        if args[0] == "capture":
            return self.output
        if args[:2] == ("block", "close"):
            if self.fail_close:
                raise BackendError("lost close response")
            self.blocks = [b for b in self.blocks if b["block_id"] != args[-1]]
            def remove(node):
                if node.get("block_id") == args[-1]:
                    return None
                if "split" in node:
                    before = remove(node["split"]["before"])
                    after = remove(node["split"]["after"])
                    if before is None or after is None:
                        return before or after
                    node["split"].update(before=before, after=after)
                return node
            self.layouts = {k: updated for k, node in self.layouts.items() if (updated := remove(node))}
            return ""
        if args[0] == "focus":
            return ""
        raise AssertionError(args)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.rex = FakeRex(self.root)
        self.player = Player("penny", "Penny", "pg", self.root)
        self.coach = Player("coach", "Coach", "coach", self.root)
        self.enterContext(patch("dreamteam.runtime.shutil.which", return_value="/test/codex"))

    def start(self):
        state = json.loads(self.rex.path.read_text()) if self.rex.path.exists() else {"players": {}}
        if "coach" not in state["players"]:
            fail, callback = self.rex.fail_launch, self.rex.on_launch
            self.rex.fail_launch, self.rex.on_launch = False, None
            self.rex.start(self.coach)
            self.rex.fail_launch, self.rex.on_launch = fail, callback
            self.rex.calls.clear()
        return self.rex.start(self.player)

    def writes(self):
        return [c for c in self.rex.calls if "write" in c[0]]

    def test_passive_observation_creates_no_files_or_server(self):
        self.assertEqual(self.rex.refresh((self.player,))["penny"].activity, "stopped")
        self.assertEqual(self.rex.read_output(self.player).text, "")
        self.assertFalse(self.rex.directory.exists())
        self.assertEqual(self.rex.calls, [])

    def test_start_and_reopen_reuse_exact_block(self):
        self.assertEqual(self.start().activity, "idle")
        self.start()
        self.assertEqual(len([c for c in self.rex.calls if c[0][0] in {"run", "split"}]), 1)
        self.assertEqual(json.loads(self.rex.path.read_text())["players"]["penny"]["block_id"], "block:2")
        launch = next(c[0] for c in self.rex.calls if c[0][0] in {"run", "split"})
        self.assertIn("--shell=none", launch)
        self.assertIn("--focus=false", launch)

    def test_lost_launch_response_reconciles_without_duplicate(self):
        self.rex.fail_launch = True
        with self.assertRaises(BackendError):
            self.start()
        self.rex.fail_launch = False
        self.assertEqual(self.start().activity, "idle")
        self.assertEqual(len(self.rex.blocks), 2)

    def test_unconfirmed_missing_launch_does_not_retry(self):
        self.rex.fail_launch = True
        with self.assertRaises(BackendError):
            self.start()
        self.rex.blocks.clear()
        with self.assertRaisesRegex(BackendError, "unconfirmed"):
            self.start()
        self.assertEqual(len([c for c in self.rex.calls if c[0][0] in {"run", "split"}]), 1)

    def test_legacy_runtime_and_views_are_untouched(self):
        self.rex.directory.mkdir()
        old = self.rex.directory / "runtime.json"
        old.write_text('{"legacy":"herdr"}')
        self.start()
        self.rex.shutdown((self.coach, self.player))
        self.assertEqual(old.read_text(), '{"legacy":"herdr"}')

    def test_changed_server_prevents_actions(self):
        self.start()
        self.rex.host["instance_id"] = "different"
        for operation in (lambda: self.start(), lambda: self.rex.stop(self.player), lambda: self.rex.open(self.player)):
            with self.assertRaisesRegex(BackendError, "server identity"):
                operation()
        self.assertEqual(len(self.rex.blocks), 2)

    def test_changed_block_or_process_prevents_input_and_cleanup(self):
        for change in ("label", "host", "pid", "path"):
            with self.subTest(change=change):
                self.rex = FakeRex(self.root)
                if self.rex.path.exists():
                    self.rex.path.unlink()
                self.start()
                if change == "label":
                    self.rex.blocks[1]["label"] = "repurposed"
                elif change == "host":
                    self.rex.blocks[1]["host_id"] = "other"
                elif change == "pid":
                    self.rex.processes["block:2"]["child"]["pid"] = 900
                else:
                    self.rex.processes["block:2"]["child"]["path"] = "/bin/sh"
                with self.assertRaises(PromptRejected):
                    self.rex.send_prompt(self.player, "task")
                with self.assertRaises(BackendError):
                    self.rex.stop(self.player)
                self.assertEqual(self.writes(), [])

    def test_live_cwd_may_change_after_identity_is_recorded(self):
        self.start()
        self.rex.processes["block:2"]["child"]["cwd"] = "/elsewhere"
        self.assertEqual(self.rex.refresh((self.player,))["penny"].activity, "idle")

    def test_missing_or_wrong_status_owner_is_unknown(self):
        self.start()
        for records in ([], [{"state": "idle", "app": "codex", "owner": {"pid": 999, "name": "codex"}}]):
            self.rex.statuses["block:2"] = records
            self.assertEqual(self.rex.refresh((self.player,))["penny"].activity, "unknown")
            with self.assertRaises(PromptRejected):
                self.rex.send_prompt(self.player, "task")

    def test_busy_blocked_and_foreground_subprocess_reject_input(self):
        self.start()
        for status in ("working", "blocked", "error"):
            self.rex.statuses["block:2"][0]["state"] = status
            with self.assertRaises(PromptRejected):
                self.rex.send_prompt(self.player, "task")
        self.rex.statuses["block:2"][0]["state"] = "idle"
        self.rex.processes["block:2"]["foreground"]["pid"] = 999
        with self.assertRaises(PromptRejected):
            self.rex.send_prompt(self.player, "task")
        self.assertEqual(self.writes(), [])

    def test_bracketed_paste_and_utf8_receipt(self):
        self.start()
        receipt = self.rex.send_prompt(self.player, "first\nÉté\tlast")
        self.assertEqual(receipt["delivery"], "submitted")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(json.loads(self.writes()[0][1])["data"], "\x1b[200~first\nÉté\tlast\x1b[201~\r")

    def test_missing_paste_mode_rejects_before_write(self):
        self.start()
        self.rex.mode = False
        with self.assertRaises(PromptRejected):
            self.rex.send_prompt(self.player, "task")
        self.assertEqual(self.writes(), [])

    def test_input_controls_are_rejected(self):
        for text in ("", "a\x1b[201~b", "x\r", "\0", "a" * 32769):
            with self.assertRaises(PromptRejected):
                self.rex.send_prompt(self.player, text)
        self.assertEqual(self.rex.calls, [])

    def test_lost_or_partial_write_ack_is_uncertain(self):
        self.start()
        self.rex.fail_write = True
        with self.assertRaises(PromptUncertain):
            self.rex.send_prompt(self.player, "task")
        self.rex.fail_write = False
        self.rex.partial_write = True
        with self.assertRaises(PromptUncertain):
            self.rex.send_prompt(self.player, "task")
        self.assertEqual(len(self.writes()), 2)

    def test_coach_token_can_be_read_during_spawn_and_is_revoked_on_stop(self):
        tokens = []
        self.rex.on_launch = lambda: tokens.append(self.rex.coordinator_token(self.coach))
        self.rex.start(self.coach)
        self.rex.on_launch = None
        self.rex.start(self.player, coordinator=(self.coach, tokens[0]))
        self.rex.stop(self.coach)
        with self.assertRaises(PromptRejected):
            self.rex.send_prompt(self.player, "task", coordinator=(self.coach, tokens[0]))
        self.assertEqual(self.writes(), [])

    def test_coordinator_replacement_rejects_old_token(self):
        self.rex.start(self.coach)
        token = self.rex.coordinator_token(self.coach)
        self.rex.stop(self.coach)
        self.rex.start(self.coach)
        with self.assertRaisesRegex(BackendError, "changed"):
            self.rex.start(self.player, coordinator=(self.coach, token))

    def test_stop_verifies_close_and_retains_failed_binding(self):
        self.start()
        self.rex.fail_close = True
        with self.assertRaisesRegex(BackendError, "unconfirmed"):
            self.rex.stop(self.player)
        self.assertIn("penny", json.loads(self.rex.path.read_text())["players"])
        self.rex.fail_close = False
        self.rex.stop(self.player)
        self.assertNotIn("penny", json.loads(self.rex.path.read_text())["players"])

    def test_shutdown_closes_coach_first_and_preserves_untracked_tabs(self):
        self.start()
        self.rex.start(self.coach)
        self.rex.blocks.append({"block_id": "block:unrelated"})
        self.rex.shutdown((self.player, self.coach))
        closed = [c[0][-1] for c in self.rex.calls if c[0][:2] == ("block", "close")]
        self.assertEqual(closed, ["block:1", "block:2"])
        self.assertEqual(self.rex.blocks, [{"block_id": "block:unrelated"}])
        self.assertFalse(self.rex.path.exists())

    def test_current_terminal_is_preserved(self):
        self.start()
        with patch.dict(os.environ, {"REX_BLOCK": "block:2"}):
            with self.assertRaisesRegex(BackendError, "current terminal"):
                self.rex.stop(self.player)

    def test_open_focuses_existing_tab_without_spawning(self):
        self.start()
        self.rex.open(self.player)
        self.rex.open(self.player)
        self.assertEqual(len(self.rex.blocks), 2)
        self.assertEqual(self.rex.calls[-1][0], ("focus", "-s", "session:one", "block:2"))

    def test_output_is_bounded_and_controls_removed(self):
        self.start()
        self.rex.output = "old\n\x1b[31mred\x1b[0m\nlast\n"
        self.assertEqual(self.rex.read_output(self.player, 2).text, "red\nlast")
        self.assertEqual(_plain_terminal_text("x\x1b]52;c;secret\x07y"), "xy")

    def test_server_restart_during_snapshot_is_rejected(self):
        self.start()
        original = self.rex._call
        def changed(state, *args, **kwargs):
            result = original(state, *args, **kwargs)
            if args[:2] == ("block", "ls"):
                self.rex.host["instance_id"] = "new-instance"
            return result
        with patch.object(self.rex, "_call", side_effect=changed):
            with self.assertRaisesRegex(BackendError, "restarted during"):
                self.rex.open(self.player)

    def test_exited_tab_requires_explicit_close_before_restart(self):
        self.start()
        self.rex.processes["block:2"] = {"child": None, "foreground": None, "last_exit": {"exit_code": 0}}
        observation = self.rex.refresh((self.player,))["penny"]
        self.assertEqual(observation.activity, "stopped")
        self.assertIn("dreamteam stop", observation.detail)
        with self.assertRaisesRegex(BackendError, "previous Codex exited"):
            self.start()
        self.rex.stop(self.player)
        self.assertEqual(self.start().activity, "idle")

    def test_coach_close_failure_prevents_player_shutdown(self):
        self.start()
        self.rex.start(self.coach)
        self.rex.fail_close = True
        with self.assertRaises(BackendError):
            self.rex.shutdown((self.player, self.coach))
        closed = [c[0][-1] for c in self.rex.calls if c[0][:2] == ("block", "close")]
        self.assertEqual(closed, ["block:1"])
        self.assertEqual(len(self.rex.blocks), 2)

    def test_subprocess_uses_explicit_endpoint_stdin_and_no_autostart(self):
        backend = Rex(self.root)
        result = Mock(returncode=0, stdout='{"bytes_written":3}', stderr="")
        with patch("dreamteam.runtime.subprocess.run", return_value=result) as run:
            state = {"endpoint": "unix:///test/rex.sock", "session_id": "session:one"}
            backend._method(state, "block:2", "write", {"data": "abc"})
        args, kwargs = run.call_args
        self.assertIn("--autostart=false", args[0])
        self.assertIn("unix:///test/rex.sock", args[0])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(json.loads(kwargs["input"]), {"data": "abc"})


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.rex = FakeRex(root)
        self.coach = Player("coach", "Coach", "coach", root)
        self.penny = Player("penny", "Penny", "pg", root)
        self.shaq = Player("shaq", "Shaq", "c", root)
        self.answer = Player("answer", "The Answer", "bench", root)
        self.enterContext(patch("dreamteam.runtime.shutil.which", return_value="/test/codex"))

    def launches(self):
        return [args for args, _ in self.rex.calls if args[0] in {"run", "split"}]

    def test_coach_left_first_player_right_second_below_in_one_tab(self):
        for player in (self.coach, self.penny, self.shaq):
            self.rex.start(player)
        launches = self.launches()
        self.assertEqual([c[0] for c in launches], ["run", "split", "split"])
        self.assertEqual(launches[1][1:7], ("-b", "block:1", "--split", "right", "--ratio", "50"))
        self.assertEqual(launches[2][1:7], ("-b", "block:2", "--split", "below", "--ratio", "50"))
        self.assertEqual({b["window_id"] for b in self.rex.blocks}, {"window:1"})
        self.assertEqual(self.rex.layouts["window:1"], {"split": {"direction": "horizontal",
            "before": {"block_id": "block:1"}, "after": {"split": {"direction": "vertical",
                "before": {"block_id": "block:2"}, "after": {"block_id": "block:3"}}}}})
        self.assertTrue(all("--focus=false" in c for c in launches))
        names = [a for a, _ in self.rex.calls if a[:2] == ("window", "rename")]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0][-1].startswith("Dreamteam"))

    def test_player_without_coach_never_gets_a_separate_tab(self):
        with self.assertRaisesRegex(BackendError, "Start Coach first"):
            self.rex.start(self.penny)
        self.assertEqual(self.launches(), [])

    def test_repeated_start_reuses_all_panes(self):
        for _ in range(2):
            for player in (self.coach, self.penny, self.shaq):
                self.rex.start(player)
        self.assertEqual(len(self.launches()), 3)

    def test_lost_split_ack_recovers_without_splitting_again(self):
        self.rex.start(self.coach)
        self.rex.fail_launch = True
        with self.assertRaises(BackendError):
            self.rex.start(self.penny)
        self.rex.fail_launch = False
        self.assertEqual(self.rex.start(self.penny).pane_id, "block:2")
        self.assertEqual(len(self.launches()), 2)

    def test_after_closing_bottom_player_new_player_splits_below_survivor(self):
        for player in (self.coach, self.penny, self.shaq):
            self.rex.start(player)
        self.rex.stop(self.shaq)
        self.rex.start(self.answer)
        self.assertEqual(self.launches()[-1][1:5], ("-b", "block:2", "--split", "below"))

    def test_unmanaged_right_pane_is_not_split(self):
        self.rex.start(self.coach)
        self.rex.layouts["window:1"] = {"split": {"direction": "horizontal",
            "before": {"block_id": "block:1"}, "after": {"block_id": "block:unrelated"}}}
        with self.assertRaisesRegex(BackendError, "unmanaged"):
            self.rex.start(self.penny)
        self.assertEqual(len(self.launches()), 1)

    def test_rearranged_coach_layout_is_not_overwritten(self):
        self.rex.start(self.coach)
        self.rex.start(self.penny)
        self.rex.layouts["window:1"]["split"]["direction"] = "vertical"
        with self.assertRaisesRegex(BackendError, "Restore Coach"):
            self.rex.start(self.shaq)
        self.assertEqual(len(self.launches()), 2)

    def test_old_tab_layout_requires_explicit_shutdown(self):
        self.rex.start(self.coach)
        state = json.loads(self.rex.path.read_text())
        del state["layout"]
        self.rex._save(state)
        with self.assertRaisesRegex(BackendError, "old separate-tab"):
            self.rex.start(self.coach)
        self.assertEqual(len(self.launches()), 1)
        self.rex.shutdown((self.coach,))
        self.rex.start(self.coach)
        self.assertEqual(len(self.launches()), 2)

    def test_cannot_replace_coach_while_old_players_remain(self):
        self.rex.start(self.coach)
        self.rex.start(self.penny)
        self.rex.stop(self.coach)
        with self.assertRaisesRegex(BackendError, "remaining team"):
            self.rex.start(self.coach)
        self.assertEqual(len(self.launches()), 2)


if __name__ == "__main__":
    unittest.main()
