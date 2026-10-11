"""Native viewer identity and retry tests; no real panes are created."""
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dreamteam.roster import Player
from dreamteam.runtime import BackendError
from dreamteam.views import NativeViews


class NativeViewsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.player = Player("penny", "Penny", "pg", self.root)
        self.environment = {
            "HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/test/host.sock",
            "HERDR_PANE_ID": "w1:p1", "PATH": "/tools",
        }
        self.manager = NativeViews(self.root, self.environment)
        self.host = {"path": "/test/host.sock", "device": 1, "inode": 2, "created_ns": 3}
        self.caller = {"pane_id": "w1:p1", "terminal_id": "coach",
                       "workspace_id": "w1", "tab_id": "w1:t1"}
        self.pane = {"pane_id": "w1:p2", "terminal_id": "viewer",
                     "workspace_id": "w1", "tab_id": "w1:t1"}
        self.snapshot = {
            "panes": [deepcopy(self.caller)],
            "layouts": [{"tab_id": "w1:t1", "panes": [
                {"pane_id": "w1:p1", "rect": {"width": 200, "height": 50}}]}],
        }
        self.process = {"pane_id": "w1:p2", "shell_pid": 10,
                        "foreground_process_group_id": 10,
                        "foreground_processes": [{"pid": 10, "name": "zsh"}]}
        self.command = ["/python", "-m", "dreamteam.app", "--config",
                        str(self.root / "dreamteam.toml"), "--view", "penny"]
        self.socket_patch = patch.object(NativeViews, "_socket", return_value=self.host)
        self.socket_mock = self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        self.call_patch = patch.object(NativeViews, "_call", side_effect=self.respond)
        self.calls = self.call_patch.start()
        self.addCleanup(self.call_patch.stop)

    def respond(self, *args):
        if args == ("pane", "current", "--current"):
            return {"result": {"pane": deepcopy(self.caller)}}
        if args == ("api", "snapshot"):
            return {"result": {"snapshot": deepcopy(self.snapshot)}}
        if args[:2] == ("pane", "get"):
            pane = next(p for p in self.snapshot["panes"] if p["pane_id"] == args[2])
            return {"result": {"pane": deepcopy(pane)}}
        if args[:2] == ("pane", "split"):
            self.snapshot["panes"].append(deepcopy(self.pane))
            return {"result": {"pane": deepcopy(self.pane)}}
        if args[:2] == ("pane", "process-info"):
            return {"result": {"process_info": deepcopy(self.process)}}
        if args[:2] == ("pane", "run"):
            self.process["foreground_process_group_id"] = 20
            self.process["foreground_processes"] = [{"pid": 20, "name": "python", "argv": self.command}]
            return {"result": {"type": "ok"}}
        if args[:2] == ("pane", "close"):
            self.snapshot["panes"] = [p for p in self.snapshot["panes"] if p["pane_id"] != args[2]]
            return {}
        raise AssertionError(f"Unexpected command: {args}")

    def mutations(self):
        return [call.args for call in self.calls.call_args_list
                if call.args[:2] in (("pane", "split"), ("pane", "run"))]

    def binding(self):
        state = json.loads(self.manager.path.read_text())
        return next(iter(state["views"].values()))

    def test_opens_native_sibling_without_focus_or_takeover(self):
        message = self.manager.open(self.player, self.command)
        split, run = self.mutations()
        self.assertEqual(split, ("pane", "split", "--pane", "w1:p1",
                                "--direction", "right", "--cwd", str(self.root), "--no-focus"))
        self.assertEqual(run, ("pane", "run", "w1:p2", shlex.join(self.command)))
        self.assertIn("Courtside stays here", message)
        self.assertEqual(self.binding()["phase"], "opened")
        self.assertEqual(self.binding()["host"], self.host)

    def test_second_courtside_reuses_view_without_sending_input(self):
        self.manager.open(self.player, self.command)
        manager = NativeViews(self.root, self.environment)
        message = manager.open(self.player, self.command)
        self.assertIn("already has a viewer", message)
        self.assertEqual(len(self.mutations()), 2)

    def test_ended_viewer_gives_guidance_without_sending_to_existing_shell(self):
        self.manager.open(self.player, self.command)
        self.process["foreground_process_group_id"] = 10
        self.process["foreground_processes"] = [{"pid": 10, "name": "zsh"}]
        with self.assertRaisesRegex(BackendError, "viewer ended in pane w1:p2"):
            NativeViews(self.root, self.environment).open(self.player, self.command)
        self.assertEqual(len(self.mutations()), 2)

    def test_narrow_tall_pane_splits_down(self):
        self.snapshot["layouts"][0]["panes"][0]["rect"] = {"width": 100, "height": 60}
        self.manager.open(self.player, self.command)
        self.assertIn("down", self.mutations()[0])

    def test_small_pane_gives_manual_guidance_without_mutation(self):
        self.snapshot["layouts"][0]["panes"][0]["rect"] = {"width": 100, "height": 30}
        with self.assertRaisesRegex(BackendError, "Enlarge"):
            self.manager.open(self.player, self.command)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.manager.path.exists())

    def test_split_timeout_is_not_replayed(self):
        def respond(*args):
            result = self.respond(*args)
            if args[:2] == ("pane", "split"):
                raise BackendError("Herdr timed out")
            return result
        self.calls.side_effect = respond
        with self.assertRaisesRegex(BackendError, "timed out"):
            self.manager.open(self.player, self.command)
        self.assertEqual(self.binding()["phase"], "splitting")
        with self.assertRaisesRegex(BackendError, "unconfirmed"):
            NativeViews(self.root, self.environment).open(self.player, self.command)
        self.assertEqual(len(self.mutations()), 1)

    def test_run_timeout_is_not_replayed(self):
        def respond(*args):
            if args[:2] == ("pane", "run"):
                raise BackendError("Herdr timed out")
            return self.respond(*args)
        self.calls.side_effect = respond
        with self.assertRaisesRegex(BackendError, "timed out"):
            self.manager.open(self.player, self.command)
        self.assertEqual(self.binding()["phase"], "launching")
        with self.assertRaisesRegex(BackendError, "unconfirmed"):
            NativeViews(self.root, self.environment).open(self.player, self.command)
        self.assertEqual(len(self.mutations()), 2)

    def test_wrong_caller_identity_cannot_target_another_pane(self):
        self.manager.environment["HERDR_PANE_ID"] = "w1:p99"
        with self.assertRaisesRegex(BackendError, "calling pane"):
            self.manager.open(self.player, self.command)
        self.assertEqual(self.mutations(), [])

    def test_changed_viewer_identity_is_not_reused_or_replaced(self):
        self.manager.open(self.player, self.command)
        self.snapshot["panes"][1]["terminal_id"] = "unrelated"
        with self.assertRaisesRegex(BackendError, "changed identity"):
            self.manager.open(self.player, self.command)
        self.assertEqual(len(self.mutations()), 2)

    def test_moved_viewer_is_not_duplicated(self):
        self.manager.open(self.player, self.command)
        self.snapshot["panes"][1]["pane_id"] = "w2:p9"
        with self.assertRaisesRegex(BackendError, "changed identity"):
            self.manager.open(self.player, self.command)
        self.assertEqual(len(self.mutations()), 2)

    def test_changed_server_refuses_saved_binding(self):
        self.manager.open(self.player, self.command)
        self.socket_mock.return_value = {**self.host, "inode": 99}
        with self.assertRaisesRegex(BackendError, "changed host"):
            NativeViews(self.root, self.environment).open(self.player, self.command)
        self.assertEqual(len(self.mutations()), 2)

    def test_confirmed_closed_viewer_can_be_replaced(self):
        self.manager.open(self.player, self.command)
        self.snapshot["panes"] = [deepcopy(self.caller)]
        self.pane = {**self.pane, "pane_id": "w1:p3", "terminal_id": "new-viewer"}
        self.process["pane_id"] = "w1:p3"
        self.process["foreground_process_group_id"] = 10
        self.process["foreground_processes"] = [{"pid": 10, "name": "zsh"}]
        message = self.manager.open(self.player, self.command)
        self.assertIn("w1:p3", message)
        self.assertEqual(len(self.mutations()), 4)

    def test_foreground_command_is_never_overwritten(self):
        self.process["foreground_process_group_id"] = 20
        self.process["foreground_processes"] = [{"pid": 20, "name": "vim"}]
        with patch("dreamteam.views.time.monotonic", side_effect=[0, 3]):
            with self.assertRaisesRegex(BackendError, "available shell"):
                self.manager.open(self.player, self.command)
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(self.binding()["phase"], "created")

    def test_command_arguments_are_shell_quoted(self):
        command = ["/path with spaces/python", "-c", "print('$(touch /tmp/nope)')"]
        self.manager.open(self.player, command)
        self.assertEqual(shlex.split(self.mutations()[1][-1]), command)

    def test_host_environment_is_captured_without_mutating_input(self):
        self.environment["HERDR_PANE_ID"] = "changed"
        self.assertEqual(self.manager.environment["HERDR_PANE_ID"], "w1:p1")
        self.assertEqual(self.manager.environment["HERDR_SOCKET_PATH"], "/test/host.sock")

    def test_outside_herdr_creates_no_state(self):
        self.socket_patch.stop()
        with self.assertRaisesRegex(BackendError, "separate terminal tab"):
            NativeViews(self.root, {}).open(self.player, self.command)
        self.assertFalse(self.manager.directory.exists())
        self.calls.assert_not_called()

    def test_concurrent_open_does_not_mutate(self):
        self.manager.directory.mkdir()
        with (self.manager.directory / "views.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(BackendError, "Another Courtside"):
                self.manager.open(self.player, self.command)
        self.calls.assert_not_called()

    def test_installed_pane_run_empty_success_is_an_acknowledgement(self):
        self.call_patch.stop()
        with patch("dreamteam.views.shutil.which", return_value="/tools/herdr"), patch(
            "dreamteam.views.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ):
            self.assertEqual(self.manager._call("pane", "run", "w1:p2", "echo ready"), {})
            with self.assertRaisesRegex(BackendError, "no valid JSON"):
                self.manager._call("pane", "get", "w1:p2")

    def test_subprocess_uses_host_environment_and_reports_timeout(self):
        self.call_patch.stop()
        with patch("dreamteam.views.shutil.which", return_value="/tools/herdr"), patch(
            "dreamteam.views.subprocess.run", side_effect=subprocess.TimeoutExpired("herdr", 5)
        ) as run:
            with self.assertRaisesRegex(BackendError, "may have occurred"):
                self.manager._call("pane", "split", "--current", "--direction", "right")
        self.assertEqual(run.call_args.kwargs["env"], self.manager.environment)
        self.assertNotIn("--session", run.call_args.args[0])

    def test_close_all_closes_only_verified_viewer_and_clears_binding(self):
        self.manager.open(self.player, self.command)
        message = self.manager.close_all()
        self.assertIn("Closed 1", message)
        self.assertEqual(json.loads(self.manager.path.read_text())["views"], {})
        self.assertEqual(self.snapshot["panes"], [self.caller])
        self.assertEqual([c.args for c in self.calls.call_args_list if c.args[:2] == ("pane", "close")],
                         [("pane", "close", "w1:p2")])

    def test_close_all_accepts_native_attachment_child(self):
        self.manager.open(self.player, self.command)
        self.process["foreground_processes"].append({
            "pid": 21, "name": "herdr",
            "argv": ["/tools/herdr", "--session", "dreamteam-player", "agent", "attach", "w1:p9"],
        })
        self.assertIn("Closed 1", self.manager.close_all())

    def test_close_all_accepts_confirmed_idle_owned_shell(self):
        self.manager.open(self.player, self.command)
        self.process["foreground_process_group_id"] = 10
        self.process["foreground_processes"] = [{"pid": 10, "name": "zsh"}]
        self.assertIn("Closed 1", self.manager.close_all())

    def test_close_all_preserves_unrelated_foreground_work(self):
        self.manager.open(self.player, self.command)
        self.process["foreground_processes"] = [{"pid": 20, "name": "vim", "argv": ["vim", "notes"]}]
        with self.assertRaisesRegex(BackendError, "another foreground process"):
            self.manager.close_all()
        self.assertEqual(len(self.snapshot["panes"]), 2)
        self.assertTrue(json.loads(self.manager.path.read_text())["views"])
        self.assertFalse(any(c.args[:2] == ("pane", "close") for c in self.calls.call_args_list))

    def test_close_all_preserves_unconfirmed_split_intent(self):
        self.manager.open(self.player, self.command)
        state = json.loads(self.manager.path.read_text())
        binding = next(iter(state["views"].values()))
        binding["phase"] = "splitting"
        del binding["pane"]
        self.manager.path.write_text(json.dumps(state))
        with self.assertRaisesRegex(BackendError, "unconfirmed split"):
            self.manager.close_all()
        self.assertTrue(json.loads(self.manager.path.read_text())["views"])
        self.assertFalse(any(c.args[:2] == ("pane", "close") for c in self.calls.call_args_list))

    def test_close_all_never_closes_current_courtside(self):
        self.manager.open(self.player, self.command)
        self.caller = deepcopy(self.pane)
        with self.assertRaisesRegex(BackendError, "current Courtside"):
            self.manager.close_all()
        self.assertFalse(any(c.args[:2] == ("pane", "close") for c in self.calls.call_args_list))

    def test_close_all_preserves_changed_host(self):
        self.manager.open(self.player, self.command)
        self.socket_mock.return_value = {**self.host, "inode": 99}
        with self.assertRaisesRegex(BackendError, "socket identity changed"):
            self.manager.close_all()
        self.assertFalse(any(c.args[:2] == ("pane", "close") for c in self.calls.call_args_list))

    def test_close_all_records_partial_cleanup_and_reports_leftovers(self):
        self.manager.open(self.player, self.command)
        state = json.loads(self.manager.path.read_text())
        extra = deepcopy(next(iter(state["views"].values())))
        extra["player"] = "shaq"
        extra["pane"] = {**self.pane, "pane_id": "w1:p3", "terminal_id": "old"}
        state["views"]["other"] = extra
        self.snapshot["panes"].append({**extra["pane"], "terminal_id": "different"})
        self.manager.path.write_text(json.dumps(state))
        with self.assertRaisesRegex(BackendError, "Closed 1 viewer panes"):
            self.manager.close_all()
        self.assertEqual(list(json.loads(self.manager.path.read_text())["views"]), ["other"])

    def test_close_all_timeout_reconciles_confirmed_disappearance(self):
        self.manager.open(self.player, self.command)
        def respond(*args):
            result = self.respond(*args)
            if args[:2] == ("pane", "close"):
                raise BackendError("Herdr timed out")
            return result
        self.calls.side_effect = respond
        self.assertIn("Closed 1", self.manager.close_all())
        self.assertEqual(json.loads(self.manager.path.read_text())["views"], {})

    def test_close_all_failed_close_keeps_binding(self):
        self.manager.open(self.player, self.command)
        def respond(*args):
            if args[:2] == ("pane", "close"):
                raise BackendError("Herdr refused closure")
            return self.respond(*args)
        self.calls.side_effect = respond
        with self.assertRaisesRegex(BackendError, "refused closure"):
            self.manager.close_all()
        self.assertTrue(json.loads(self.manager.path.read_text())["views"])

    def test_close_all_does_not_create_state_when_unused(self):
        self.assertIn("No Dreamteam", self.manager.close_all())
        self.assertFalse(self.manager.directory.exists())
        self.calls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
