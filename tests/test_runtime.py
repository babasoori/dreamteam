"""Session identity and retry guarantees, with no real Herdr processes."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dreamteam.roster import Player
from dreamteam.runtime import BackendError, Herdr, Preview, PromptRejected, PromptUncertain


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.player = Player("penny", "Penny", "pg", self.project)
        self.backend = Herdr(self.project)
        self.socket = {"path": "/unused/test.sock", "device": 1, "inode": 2, "created_ns": 3}
        self.binding = {
            "label": "Dreamteam / Penny / original", "cwd": str(self.project),
            "phase": "launched", "pane_id": "pane-penny",
            "terminal_id": "terminal-penny", "workspace_id": "workspace-penny",
        }
        self.state = {
            "version": 1, "project": str(self.project), "session": "dreamteam-test",
            "server_requested": True, "socket": self.socket,
            "players": {"penny": self.binding},
        }
        self.snapshot = {
            "panes": [{
                "pane_id": "pane-penny", "terminal_id": "terminal-penny",
                "workspace_id": "workspace-penny", "tab_id": "tab-penny", "cwd": str(self.project),
            }],
            "agents": [{
                "name": "penny", "agent": "codex", "pane_id": "pane-penny",
                "terminal_id": "terminal-penny", "agent_status": "idle",
            }],
            "workspaces": [{
                "workspace_id": "workspace-penny", "label": self.binding["label"],
            }],
        }
        self.preview_text = "Codex output"
        self.backend.directory.mkdir()
        self.write_state()
        self.socket_mock = self.enterContext(patch.object(Herdr, "_socket", return_value=self.socket))
        self.calls = self.enterContext(patch.object(Herdr, "_call", side_effect=self.respond))
        self.reads = self.enterContext(patch("dreamteam.runtime.subprocess.run", side_effect=self.read_preview))

    def read_preview(self, command, **kwargs):
        return subprocess.CompletedProcess(command, 0, self.preview_text, "")

    def write_state(self):
        self.backend.path.write_text(json.dumps(self.state))

    def respond(self, state, *args, **kwargs):
        if args == ("api", "snapshot"):
            return {"result": {"snapshot": deepcopy(self.snapshot)}}
        if args == ("pane", "close", "pane-penny"):
            return {"result": {}}
        raise AssertionError(f"Unexpected Herdr operation: {args}")

    def mutations(self):
        return [call.args[1:] for call in self.calls.call_args_list
                if call.args[1:] != ("api", "snapshot")]


    def add_coordinator(self):
        coach = Player("coach", "Coach", "coach", self.project)
        binding = {
            "label": "Dreamteam / Coach / original", "cwd": str(self.project),
            "phase": "launched", "pane_id": "pane-coach",
            "terminal_id": "terminal-coach", "workspace_id": "workspace-coach",
        }
        self.state["players"]["coach"] = binding
        self.snapshot["panes"].append({
            "pane_id": "pane-coach", "terminal_id": "terminal-coach",
            "workspace_id": "workspace-coach", "tab_id": "tab-coach", "cwd": str(self.project),
        })
        self.snapshot["agents"].append({
            "name": "coach", "agent": "codex", "pane_id": "pane-coach",
            "terminal_id": "terminal-coach", "agent_status": "working",
        })
        self.snapshot["workspaces"].append({
            "workspace_id": "workspace-coach", "label": binding["label"],
        })
        self.write_state()
        return coach

    def coordinator_shutdown_responses(self, coordinator, *, coach_close_fails=False, race=False):
        self.server_running = True
        self.race_attempts = 0

        def respond(state, *args, **kwargs):
            if args == ("status", "server", "--json"):
                return {"session": state["session"], "running": self.server_running}
            if args[:2] == ("pane", "close"):
                if args[2] == "pane-coach":
                    if race:
                        self.race_attempts += 1
                        with self.assertRaisesRegex(BackendError, "operation is in progress") as failure:
                            Herdr(self.project).start(self.player, coordinator=coordinator)
                        self.assertEqual(failure.exception.code, "runtime_busy")
                    if coach_close_fails:
                        raise BackendError("Coach close failed")
                closed = next(p for p in self.snapshot["panes"] if p["pane_id"] == args[2])
                self.snapshot["panes"].remove(closed)
                self.snapshot["agents"] = [a for a in self.snapshot["agents"] if a["pane_id"] != args[2]]
                self.snapshot["workspaces"] = [w for w in self.snapshot["workspaces"]
                                              if w["workspace_id"] != closed["workspace_id"]]
                return {"result": {}}
            if args[:2] == ("session", "stop"):
                self.assertEqual(self.snapshot["panes"], [])
                self.server_running = False
                return {"stopped": True}
            return self.respond(state, *args, **kwargs)

        self.calls.side_effect = respond

    def test_coordinator_initialization_pins_saved_launch_without_lock_or_herdr(self):
        coach = self.add_coordinator()
        self.state["players"]["coach"]["phase"] = "launching"
        self.write_state()
        self.snapshot["agents"] = [self.snapshot["agents"][0]]
        before = self.backend.path.read_bytes()
        with self.backend._locked():
            token = Herdr(self.project).coordinator_token(coach)
        self.assertEqual(len(token), 64)
        self.assertEqual(token, self.backend.coordinator_token(coach))
        self.assertEqual(self.backend.path.read_bytes(), before)
        self.calls.assert_not_called()
        self.socket_mock.assert_not_called()
        with self.assertRaisesRegex(BackendError, "no longer running"):
            self.backend.start(self.player, coordinator=(coach, token))
        self.assertEqual(self.mutations(), [])

    def test_coordinator_capture_rejects_incomplete_or_foreign_saved_state(self):
        coach = self.add_coordinator()
        original = deepcopy(self.state)
        mutations = (
            lambda: self.state.update(project=str(self.project / "foreign")),
            lambda: self.state.update(socket=None),
            lambda: self.state["socket"].update(inode=True),
            lambda: self.state["players"]["coach"].pop("terminal_id"),
            lambda: self.state["players"]["coach"].update(phase="allocated"),
            lambda: self.state["players"]["coach"].update(cwd="/foreign"),
        )
        for mutate in mutations:
            self.state = deepcopy(original)
            mutate()
            self.write_state()
            before = self.backend.path.read_bytes()
            with self.assertRaises(BackendError):
                self.backend.coordinator_token(coach)
            self.assertEqual(self.backend.path.read_bytes(), before)
        self.calls.assert_not_called()
        self.socket_mock.assert_not_called()

    def test_guarded_dispatch_allows_working_coach_but_not_launch_pending(self):
        coach = self.add_coordinator()
        token = self.backend.coordinator_token(coach)
        for field in ("launch_pending", "interactive_ready"):
            self.snapshot["agents"][1][field] = field == "launch_pending"
            with self.assertRaisesRegex(BackendError, "still starting"):
                self.backend.start(self.player, coordinator=(coach, token))
            with self.assertRaisesRegex(PromptRejected, "still starting"):
                self.backend.send_prompt(self.player, "Task", coordinator=(coach, token))
            self.snapshot["agents"][1].pop(field)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.backend.start(self.player, coordinator=(coach, token)).activity, "idle")
        self.enable_prompt_response()
        receipt = self.backend.send_prompt(self.player, "Task", coordinator=(coach, token))
        self.assertEqual(receipt["delivery"], "submitted")

    def test_guarded_start_never_creates_or_restarts_a_missing_runtime(self):
        coach = self.add_coordinator()
        token = self.backend.coordinator_token(coach)
        self.backend.path.unlink()
        before = set(self.backend.directory.iterdir())
        with patch("dreamteam.runtime.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(BackendError, "no longer running"):
                self.backend.start(self.player, coordinator=(coach, token))
            spawn.assert_not_called()
        self.assertEqual(set(self.backend.directory.iterdir()), before)
        self.calls.assert_not_called()
        self.assertFalse(self.backend.path.exists())

    def test_coach_stop_between_start_and_send_rejects_input(self):
        coach = self.add_coordinator()
        coordinator = coach, self.backend.coordinator_token(coach)
        self.backend.start(self.player, coordinator=coordinator)
        self.coordinator_shutdown_responses(coordinator)
        self.backend.stop(coach)
        with self.assertRaises(PromptRejected):
            self.backend.send_prompt(self.player, "Task", coordinator=coordinator)
        self.assertEqual(self.mutations(), [("pane", "close", "pane-coach")])
        with self.assertRaises(BackendError):
            self.backend.start(self.player, coordinator=coordinator)

    def test_old_coordinator_cannot_dispatch_after_coach_or_server_restart(self):
        coach = self.add_coordinator()
        token = self.backend.coordinator_token(coach)
        self.state["players"]["coach"]["terminal_id"] = "terminal-coach-new"
        self.snapshot["panes"][1]["terminal_id"] = "terminal-coach-new"
        self.snapshot["agents"][1]["terminal_id"] = "terminal-coach-new"
        self.write_state()
        for operation in (
            lambda: self.backend.start(self.player, coordinator=(coach, token)),
            lambda: self.backend.send_prompt(self.player, "Task", coordinator=(coach, token)),
        ):
            with self.assertRaisesRegex(BackendError, "session changed"):
                operation()
        new_token = self.backend.coordinator_token(coach)
        self.assertNotEqual(token, new_token)
        self.assertEqual(self.backend.start(self.player, coordinator=(coach, new_token)).activity, "idle")
        self.state["session"] = "dreamteam-new-server"
        self.write_state()
        with self.assertRaisesRegex(BackendError, "session changed"):
            self.backend.start(self.player, coordinator=(coach, new_token))
        self.assertEqual(self.mutations(), [])

    def test_shutdown_stops_coach_first_and_fences_pending_dispatch(self):
        coach = self.add_coordinator()
        coordinator = coach, self.backend.coordinator_token(coach)
        self.coordinator_shutdown_responses(coordinator, race=True)
        summary = self.backend.shutdown((self.player, coach))
        self.assertIn("stopped dedicated session", summary)
        self.assertEqual(self.race_attempts, 1)
        closes = [call for call in self.shutdown_mutations() if call[:2] == ("pane", "close")]
        self.assertEqual(closes, [("pane", "close", "pane-coach"), ("pane", "close", "pane-penny")])
        with patch("dreamteam.runtime.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(BackendError, "no longer running"):
                self.backend.start(self.player, coordinator=coordinator)
            spawn.assert_not_called()
        self.assertFalse(json.loads(self.backend.path.read_text())["server_requested"])

    def test_failed_coach_shutdown_leaves_players_running_and_reports_incomplete(self):
        coach = self.add_coordinator()
        coordinator = coach, self.backend.coordinator_token(coach)
        self.coordinator_shutdown_responses(coordinator, coach_close_fails=True)
        with self.assertRaisesRegex(BackendError, "Coach close failed"):
            self.backend.shutdown((self.player, coach))
        self.assertEqual(self.shutdown_mutations(), [("pane", "close", "pane-coach")])
        self.assertEqual(len(self.snapshot["panes"]), 2)
        self.assertEqual(set(json.loads(self.backend.path.read_text())["players"]), {"penny", "coach"})

    def test_unstarted_project_observation_does_not_create_state(self):
        project = self.project / "fastbreak"
        project.mkdir()
        backend = Herdr(project)
        player = replace(self.player, cwd=project)
        for directory_exists in (False, True):
            with self.subTest(directory_exists=directory_exists):
                if directory_exists:
                    backend.directory.mkdir()
                observation = backend.refresh((player,))[player.id]
                self.assertEqual(observation.activity, "stopped")
                with self.assertRaisesRegex(BackendError, "before attaching"):
                    backend.attachment(player)
                preview = backend.preview(player)
                self.assertEqual(preview.text, "")
                self.assertIsNone(preview.revision)
                backend.stop(player)
                self.assertEqual(backend.directory.exists(), directory_exists)
                if directory_exists:
                    self.assertEqual(list(backend.directory.iterdir()), [])
        self.calls.assert_not_called()
        self.socket_mock.assert_not_called()

    def test_start_new_project_uses_its_own_session_and_directory(self):
        project = self.project / "fastbreak"
        project.mkdir()
        backend = Herdr(project)
        player = replace(self.player, cwd=project)
        previous = self.backend.path.read_bytes()
        pane = {**self.snapshot["panes"][0], "cwd": str(project)}
        agent = deepcopy(self.snapshot["agents"][0])
        snapshot = {"panes": [], "agents": [], "workspaces": []}

        def respond(state, *args, **kwargs):
            self.assertEqual(state["project"], str(project))
            self.assertNotEqual(state["session"], self.state["session"])
            if args == ("api", "snapshot"):
                return {"result": {"snapshot": deepcopy(snapshot)}}
            if args[:2] == ("workspace", "create"):
                self.assertEqual(args[args.index("--cwd") + 1], str(project))
                snapshot["panes"] = [pane]
                snapshot["workspaces"] = [{
                    "workspace_id": pane["workspace_id"],
                    "label": args[args.index("--label") + 1],
                }]
                return {"result": {"root_pane": pane}}
            if args[:2] == ("agent", "start"):
                self.assertEqual(args[2], player.id)
                self.assertEqual(args[args.index("--pane") + 1], pane["pane_id"])
                snapshot["agents"] = [agent]
                return {"result": {}}
            raise AssertionError(f"Unexpected Herdr operation: {args}")

        self.calls.side_effect = respond
        with patch("dreamteam.runtime.shutil.which", return_value="/tools/herdr"), patch(
            "dreamteam.runtime.subprocess.Popen"
        ) as launch:
            observation = backend.start(player)
        self.assertEqual(observation.activity, "idle")
        self.assertEqual(launch.call_args.kwargs["cwd"], project)
        self.assertEqual(launch.call_args.kwargs["env"]["HERDR_CONFIG_PATH"],
                         str(backend.directory / "herdr.toml"))
        persisted = json.loads(backend.path.read_text())
        self.assertEqual(persisted["players"][player.id]["cwd"], str(project))
        self.assertNotEqual(persisted["session"], self.state["session"])
        self.assertEqual(self.backend.path.read_bytes(), previous)

    def test_another_projects_runtime_state_is_rejected(self):
        project = self.project / "fastbreak"
        project.mkdir()
        backend = Herdr(project)
        player = replace(self.player, cwd=project)
        backend.directory.mkdir()
        original = self.backend.path.read_bytes()
        backend.path.write_bytes(original)
        operations = (
            lambda: backend.refresh((player,)),
            lambda: backend.start(player),
            lambda: backend.attachment(player),
            lambda: backend.preview(player),
            lambda: backend.stop(player),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(BackendError, "different project"):
                    operation()
                self.assertEqual(backend.path.read_bytes(), original)
        self.calls.assert_not_called()
        self.socket_mock.assert_not_called()

    def test_changed_player_directory_cannot_reuse_existing_session(self):
        player = replace(self.player, cwd=self.project / "fastbreak")
        original = self.backend.path.read_bytes()
        for operation in (self.backend.start, self.backend.attachment, self.backend.preview, self.backend.stop):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(BackendError, "directory does not match"):
                    operation(player)
        observation = self.backend.refresh((player,))[player.id]
        self.assertEqual(observation.activity, "unknown")
        self.assertEqual(self.backend.path.read_bytes(), original)
        self.assertEqual(self.mutations(), [])

    def test_duplicate_start_after_reload_reuses_exact_session(self):
        before = json.loads(self.backend.path.read_text())
        first = self.backend.start(self.player)
        second = Herdr(self.project).start(self.player)
        self.assertEqual(first.pane_id, "pane-penny")
        self.assertEqual(second.pane_id, first.pane_id)
        self.assertEqual(second.activity, "idle")
        self.assertIn("Reusing", second.detail)
        self.assertEqual(json.loads(self.backend.path.read_text()), before)
        self.assertEqual(self.mutations(), [])

    def test_unconfirmed_launch_is_not_submitted_again(self):
        self.binding["phase"] = "launching"
        self.snapshot["agents"] = []
        self.write_state()
        for backend in (self.backend, Herdr(self.project)):
            with self.assertRaisesRegex(BackendError, "already attempted"):
                backend.start(self.player)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(json.loads(self.backend.path.read_text())["players"]["penny"]["phase"],
                         "launching")

    def test_changed_server_rejects_start_attach_and_stop(self):
        self.socket_mock.return_value = {**self.socket, "inode": 99}
        before = self.backend.path.read_bytes()
        for operation in (self.backend.start, self.backend.attachment, self.backend.preview, self.backend.stop):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(BackendError, "stale"):
                    operation(self.player)
        self.calls.assert_not_called()
        self.assertEqual(self.backend.path.read_bytes(), before)

    def test_changed_player_identity_rejects_operations(self):
        original = deepcopy(self.snapshot)
        cases = (
            ("panes", "terminal_id", "another-terminal"),
            ("panes", "workspace_id", "another-workspace"),
            ("agents", "name", "another-player"),
            ("agents", "agent", "another-agent"),
            ("agents", "pane_id", "another-pane"),
            ("agents", "terminal_id", "another-terminal"),
            ("workspaces", "label", "another-workspace"),
        )
        for collection, field, value in cases:
            self.snapshot = deepcopy(original)
            self.snapshot[collection][0][field] = value
            for operation in (self.backend.start, self.backend.attachment, self.backend.preview, self.backend.stop):
                with self.subTest(field=field, operation=operation.__name__):
                    before = self.backend.path.read_bytes()
                    with self.assertRaises(BackendError):
                        operation(self.player)
                    self.assertEqual(self.backend.path.read_bytes(), before)
        self.assertEqual(self.mutations(), [])

    def test_live_directory_changes_preserve_verified_session(self):
        original = self.backend.path.read_bytes()
        for cwd in (str(self.project / "src"), str(self.project.parent / "dreamteam"), None):
            with self.subTest(cwd=cwd):
                self.write_state()
                self.snapshot["panes"][0]["cwd"] = cwd
                self.snapshot["panes"][0]["foreground_cwd"] = cwd
                self.calls.reset_mock()
                self.reads.reset_mock()

                observation = self.backend.refresh((self.player,))[self.player.id]
                self.assertEqual(observation.activity, "idle")
                reused = self.backend.start(self.player)
                self.assertEqual(reused.pane_id, self.binding["pane_id"])
                self.assertIn("Reusing", reused.detail)
                command, _ = self.backend.attachment(self.player)
                self.assertEqual(command[-3:], ["agent", "attach", self.binding["pane_id"]])
                self.assertEqual(self.backend.preview(self.player).text, self.preview_text)
                self.assertEqual(self.backend.path.read_bytes(), original)
                self.assertEqual(self.mutations(), [])
                self.reads.assert_called_once()
                self.assertIn("read", self.reads.call_args.args[0])

                self.backend.stop(self.player)
                self.assertEqual(self.mutations(), [("pane", "close", self.binding["pane_id"])])
                self.assertEqual(json.loads(self.backend.path.read_text())["players"], {})

    def test_exited_agent_reports_missing_codex_after_directory_changes(self):
        original = self.backend.path.read_bytes()
        self.snapshot["panes"][0]["cwd"] = str(self.project.parent / "dreamteam")
        self.snapshot["agents"] = []
        observation = self.backend.refresh((self.player,))[self.player.id]
        self.assertEqual(observation.activity, "unknown")
        self.assertIn("Codex is no longer detected", observation.detail)
        self.assertNotIn("Stop", observation.detail)
        for operation in (self.backend.attachment, self.backend.preview):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(BackendError, "Codex is no longer detected"):
                    operation(self.player)
        with self.assertRaisesRegex(BackendError, "already attempted"):
            self.backend.start(self.player)
        self.reads.assert_not_called()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.backend.path.read_bytes(), original)

    def test_newly_allocated_pane_in_wrong_directory_does_not_launch_codex(self):
        self.binding["phase"] = "allocated"
        self.snapshot["panes"][0]["cwd"] = str(self.project / "another-project")
        self.snapshot["agents"] = []
        self.write_state()
        original = self.backend.path.read_bytes()
        with self.assertRaisesRegex(BackendError, "configured project directory"):
            self.backend.start(self.player)
        self.assertEqual(self.backend.path.read_bytes(), original)
        self.assertEqual(self.mutations(), [])
        self.reads.assert_not_called()

    def test_reloaded_attachment_and_stop_use_owned_pane(self):
        backend = Herdr(self.project)
        with patch("dreamteam.runtime.shutil.which", return_value="/tools/herdr"):
            command, environment = backend.attachment(self.player)
        self.assertEqual(command, [
            "/tools/herdr", "--session", "dreamteam-test", "agent", "attach", "pane-penny",
        ])
        self.assertEqual(environment["HERDR_CONFIG_PATH"],
                         str(self.backend.directory / "herdr.toml"))
        backend.stop(self.player)
        self.assertEqual(self.mutations(), [("pane", "close", "pane-penny")])
        persisted = json.loads(self.backend.path.read_text())
        self.assertEqual(persisted["players"], {})
        self.assertEqual(persisted["session"], "dreamteam-test")

    def test_inline_control_pins_verified_terminal_and_sanitizes_environment(self):
        original = self.backend.path.read_bytes()
        host_context = {
            "HERDR_SESSION": "another-session", "HERDR_SOCKET_PATH": "/other/server.sock",
            "HERDR_PANE_ID": "pane-other", "HERDR_TAB_ID": "tab-other",
            "HERDR_WORKSPACE_ID": "workspace-other", "HERDR_ENV": "1",
            "HERDR_CONFIG_PATH": "/other/herdr.toml", "HERDR_UNKNOWN": "inherited",
        }
        with patch("dreamteam.runtime.shutil.which", return_value="/tools/herdr"), patch.dict(
            "os.environ", host_context
        ), patch("dreamteam.runtime.subprocess.Popen") as launch:
            command, environment = self.backend.control_command(self.player, 117, 38)
        self.assertEqual(command, [
            "/tools/herdr", "--session", "dreamteam-test",
            "terminal", "session", "control", "terminal-penny",
            "--cols", "117", "--rows", "38",
        ])
        self.assertNotIn("--takeover", command)
        self.assertNotIn("pane-penny", command)
        self.assertEqual(
            {key: value for key, value in environment.items() if key.startswith("HERDR_")},
            {"HERDR_CONFIG_PATH": str(self.backend.directory / "herdr.toml")},
        )
        launch.assert_not_called()
        self.reads.assert_not_called()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.backend.path.read_bytes(), original)

    def test_inline_control_does_not_initialize_an_unstarted_project(self):
        project = self.project / "unstarted"
        project.mkdir()
        backend = Herdr(project)
        player = replace(self.player, cwd=project)
        with self.assertRaisesRegex(BackendError, "Start Penny"):
            backend.control_command(player, 80, 24)
        self.assertFalse(backend.directory.exists())
        self.calls.assert_not_called()
        self.socket_mock.assert_not_called()
        self.reads.assert_not_called()

    def test_inline_control_requires_started_runtime(self):
        self.state["server_requested"] = False
        self.write_state()
        original = self.backend.path.read_bytes()
        with self.assertRaisesRegex(BackendError, "Start Penny"):
            self.backend.control_command(self.player, 80, 24)
        self.calls.assert_not_called()
        self.socket_mock.assert_not_called()
        self.assertEqual(self.backend.path.read_bytes(), original)

    def test_inline_control_requires_live_pane_and_codex(self):
        original = deepcopy(self.snapshot)
        for collection in ("panes", "agents"):
            with self.subTest(missing=collection):
                self.snapshot = deepcopy(original)
                self.snapshot[collection] = []
                before = self.backend.path.read_bytes()
                with self.assertRaisesRegex(BackendError, "No verified Codex"):
                    self.backend.control_command(self.player, 80, 24)
                self.assertEqual(self.backend.path.read_bytes(), before)
        self.assertEqual(self.mutations(), [])
        self.reads.assert_not_called()

    def test_inline_control_rejects_replaced_server(self):
        self.socket_mock.return_value = {**self.socket, "inode": 99}
        original = self.backend.path.read_bytes()
        with self.assertRaisesRegex(BackendError, "stale"):
            self.backend.control_command(self.player, 80, 24)
        self.calls.assert_not_called()
        self.reads.assert_not_called()
        self.assertEqual(self.backend.path.read_bytes(), original)

    def test_inline_control_rejects_changed_terminal_and_agent_identity(self):
        original = deepcopy(self.snapshot)
        cases = (
            ("panes", "terminal_id", "another-terminal"),
            ("panes", "workspace_id", "another-workspace"),
            ("agents", "name", "another-player"),
            ("agents", "agent", "another-agent"),
            ("agents", "pane_id", "another-pane"),
            ("agents", "terminal_id", "another-terminal"),
            ("workspaces", "label", "another-workspace"),
        )
        for collection, field, value in cases:
            with self.subTest(collection=collection, field=field):
                self.snapshot = deepcopy(original)
                self.snapshot[collection][0][field] = value
                before = self.backend.path.read_bytes()
                with self.assertRaises(BackendError):
                    self.backend.control_command(self.player, 80, 24)
                self.assertEqual(self.backend.path.read_bytes(), before)
        self.assertEqual(self.mutations(), [])
        self.reads.assert_not_called()

    def test_inline_control_rejects_invalid_dimensions_before_runtime_access(self):
        original = self.backend.path.read_bytes()
        for dimensions in (
            (0, 24), (80, -1), (1, 24), (80, 1), (1001, 24), (80, 1001),
            (True, 24), (80, False), (80.0, 24), (80, "24"),
        ):
            with self.subTest(dimensions=dimensions):
                with self.assertRaisesRegex(BackendError, "dimensions"):
                    self.backend.control_command(self.player, *dimensions)
        self.calls.assert_not_called()
        self.socket_mock.assert_not_called()
        self.reads.assert_not_called()
        self.assertEqual(self.backend.path.read_bytes(), original)

    def enable_prompt_response(self, *, error=None, during_send=None, response=None):
        acknowledgment = response if response is not None else {
            "result": {"type": "agent_prompted", "agent": {
                **deepcopy(self.snapshot["agents"][0]),
                "workspace_id": self.binding["workspace_id"],
            }},
        }

        def respond(state, *args, **kwargs):
            if args[:2] == ("agent", "prompt"):
                if during_send is not None:
                    during_send()
                if error is not None:
                    raise error
                return deepcopy(acknowledgment)
            return self.respond(state, *args, **kwargs)
        self.calls.side_effect = respond
        return acknowledgment

    def test_prompt_submits_once_to_verified_pane_and_returns_delivery_only(self):
        self.enable_prompt_response()
        before = self.backend.path.read_bytes()
        text = "Plan the wrapper.\nDo not implement yet."
        result = self.backend.send_prompt(self.player, text)
        self.assertEqual(result["delivery"], "submitted")
        self.assertEqual(result["player_id"], "penny")
        self.assertEqual(result["pane_id"], "pane-penny")
        self.assertEqual(result["terminal_id"], "terminal-penny")
        self.assertGreater(result["acknowledged_at"], 0)
        self.assertNotIn("completed", result)
        prompts = [call for call in self.calls.call_args_list if call.args[1:3] == ("agent", "prompt")]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0].args[1:], ("agent", "prompt", "pane-penny", text))
        self.assertEqual(prompts[0].kwargs, {"timeout": 8})
        self.assertEqual(self.mutations(), [("agent", "prompt", "pane-penny", text)])
        self.assertEqual(self.backend.path.read_bytes(), before)
        self.reads.assert_not_called()

    def test_prompt_accepts_done_as_idle_but_does_not_claim_task_completion(self):
        self.snapshot["agents"][0]["agent_status"] = "done"
        self.enable_prompt_response()
        result = self.backend.send_prompt(self.player, "Next task")
        self.assertEqual(result["delivery"], "submitted")
        self.assertNotIn("done", result.values())

    def test_prompt_refuses_non_idle_or_unready_agents_without_input(self):
        for status in ("working", "blocked", "unknown", "invalid", None):
            with self.subTest(status=status):
                self.snapshot["agents"][0]["agent_status"] = status
                with self.assertRaisesRegex(PromptRejected, "not idle"):
                    self.backend.send_prompt(self.player, "Task")
        self.snapshot["agents"][0]["agent_status"] = "idle"
        for field, value in (("launch_pending", True), ("interactive_ready", False)):
            with self.subTest(field=field):
                self.snapshot["agents"][0][field] = value
                with self.assertRaisesRegex(PromptRejected, "not ready"):
                    self.backend.send_prompt(self.player, "Task")
                self.snapshot["agents"][0].pop(field)
        self.assertEqual(self.mutations(), [])

    def test_prompt_rejects_invalid_text_before_any_runtime_access(self):
        for text in ("", "  \n", None, "task\x1b[201~\r", "task\0", "x" * 32769):
            with self.subTest(text=repr(text)[:30]):
                with self.assertRaises(PromptRejected):
                    self.backend.send_prompt(self.player, text)
        self.calls.assert_not_called()
        self.socket_mock.assert_not_called()

    def test_prompt_does_not_start_or_initialize_a_missing_runtime(self):
        project = self.project / "new-project"
        project.mkdir()
        backend = Herdr(project)
        with self.assertRaisesRegex(PromptRejected, "Start Penny"):
            backend.send_prompt(replace(self.player, cwd=project), "Task")
        self.assertFalse(backend.directory.exists())
        self.calls.assert_not_called()
        self.socket_mock.assert_not_called()

    def test_prompt_requires_confirmed_binding_without_reconciling_creation(self):
        self.binding.pop("pane_id")
        self.write_state()
        before = self.backend.path.read_bytes()
        with self.assertRaisesRegex(PromptRejected, "not verified"):
            self.backend.send_prompt(self.player, "Task")
        self.assertEqual(self.backend.path.read_bytes(), before)
        self.calls.assert_not_called()

    def test_prompt_rejects_stale_or_missing_identity_before_delivery(self):
        original = deepcopy(self.snapshot)
        cases = (
            ("panes", "terminal_id", "other-terminal"),
            ("agents", "agent", "other-agent"),
            ("agents", "name", "other-player"),
            ("workspaces", "label", "other-workspace"),
        )
        for collection, field, value in cases:
            with self.subTest(collection=collection, field=field):
                self.snapshot = deepcopy(original)
                self.snapshot[collection][0][field] = value
                with self.assertRaises(PromptRejected):
                    self.backend.send_prompt(self.player, "Task")
        for collection in ("panes", "agents"):
            self.snapshot = deepcopy(original)
            self.snapshot[collection] = []
            with self.assertRaises(PromptRejected):
                self.backend.send_prompt(self.player, "Task")
        self.snapshot = original
        self.socket_mock.return_value = {**self.socket, "inode": 99}
        with self.assertRaisesRegex(PromptRejected, "stale"):
            self.backend.send_prompt(self.player, "Task")
        self.assertEqual(self.mutations(), [])

    def test_prompt_known_server_rejection_is_not_retried(self):
        for code in ("agent_blocked", "agent_not_ready", "agent_not_found",
                     "agent_target_ambiguous", "empty_agent_prompt"):
            with self.subTest(code=code):
                self.calls.reset_mock()
                self.enable_prompt_response(error=BackendError(code, code=code))
                with self.assertRaisesRegex(PromptRejected, "before delivery"):
                    self.backend.send_prompt(self.player, "Task")
                self.assertEqual(self.mutations(), [("agent", "prompt", "pane-penny", "Task")])

    def test_prompt_ambiguous_submission_failure_is_never_retried(self):
        failures = (
            BackendError("Herdr timed out"),
            BackendError("timeout", code="timeout"),
            BackendError("actor closed", code="agent_prompt_failed"),
            BackendError("Herdr returned no valid JSON"),
            OSError("transport interrupted"),
            subprocess.TimeoutExpired(["herdr"], 8),
        )
        for failure in failures:
            with self.subTest(failure=str(failure)):
                self.calls.reset_mock()
                self.enable_prompt_response(error=failure)
                with self.assertRaisesRegex(PromptUncertain, "do not resend"):
                    self.backend.send_prompt(self.player, "Task")
                self.assertEqual(self.mutations(), [("agent", "prompt", "pane-penny", "Task")])

    def test_prompt_bad_acknowledgment_is_uncertain_not_success_or_retry(self):
        original = self.enable_prompt_response()
        responses = [None, {}, {"result": {}}, {"result": {"type": "ok"}}]
        for field in ("name", "agent", "pane_id", "terminal_id", "workspace_id"):
            changed = deepcopy(original)
            changed["result"]["agent"][field] = "different"
            responses.append(changed)
        for response in responses:
            with self.subTest(response=response):
                self.calls.reset_mock()
                def respond(state, *args, **kwargs):
                    if args[:2] == ("agent", "prompt"):
                        return response
                    return self.respond(state, *args, **kwargs)
                self.calls.side_effect = respond
                with self.assertRaises(PromptUncertain):
                    self.backend.send_prompt(self.player, "Task")
                self.assertEqual(self.mutations(), [("agent", "prompt", "pane-penny", "Task")])

    def test_prompt_identity_change_after_submission_is_uncertain(self):
        for changed in ("agent", "server"):
            with self.subTest(changed=changed):
                self.snapshot["agents"][0]["name"] = "penny"
                self.socket_mock.return_value = self.socket
                self.calls.reset_mock()
                def mutate():
                    if changed == "agent":
                        self.snapshot["agents"][0]["name"] = "another-player"
                    else:
                        self.socket_mock.return_value = {**self.socket, "inode": 99}
                self.enable_prompt_response(during_send=mutate)
                with self.assertRaisesRegex(PromptUncertain, "do not resend"):
                    self.backend.send_prompt(self.player, "Task")
                self.assertEqual(self.mutations(), [("agent", "prompt", "pane-penny", "Task")])

    def test_output_reads_bounded_recent_unwrapped_text_and_sanitizes_it(self):
        before = self.backend.path.read_bytes()
        self.preview_text = "old " * 9000 + "\x1b[31mLAST\x1b[0m\x1b]52;c;SECRET\a"
        output = self.backend.read_output(self.player)
        self.assertEqual(len(output.text), 32768)
        self.assertTrue(output.text.endswith("LAST"))
        self.assertNotIn("\x1b", output.text)
        self.assertNotIn("SECRET", output.text)
        self.assertEqual(self.reads.call_args.args[0][1:], [
            "--session", "dreamteam-test", "pane", "read", "pane-penny",
            "--source", "recent-unwrapped", "--format", "text", "--lines", "80",
        ])
        self.assertEqual(self.backend.path.read_bytes(), before)
        self.assertEqual(self.mutations(), [])
        snapshots = [call for call in self.calls.call_args_list if call.args[1:] == ("api", "snapshot")]
        self.assertEqual(len(snapshots), 2)

    def test_output_line_limits_validate_before_runtime_access(self):
        for lines in (0, -1, 201, True, 80.0, "80"):
            with self.assertRaisesRegex(BackendError, "between 1 and 200"):
                self.backend.read_output(self.player, lines)
        self.calls.assert_not_called()
        self.reads.assert_not_called()
        self.backend.read_output(self.player, 200)
        self.assertEqual(self.reads.call_args.args[0][-2:], ["--lines", "200"])

    def test_output_rejects_changed_identity_during_read(self):
        def read(command, **kwargs):
            self.snapshot["agents"][0]["terminal_id"] = "other-terminal"
            return subprocess.CompletedProcess(command, 0, "untrusted output", "")
        self.reads.side_effect = read
        with self.assertRaises(BackendError):
            self.backend.read_output(self.player)
        self.assertEqual(self.mutations(), [])

    def test_runtime_done_means_idle_not_task_acceptance(self):
        self.snapshot["agents"][0]["agent_status"] = "done"
        observation = self.backend.refresh((self.player,))["penny"]
        self.assertEqual(observation.activity, "idle")
        self.assertIn("not task completion", observation.detail)
        self.assertEqual(self.mutations(), [])

    def test_known_server_start_failures_remain_retryable(self):
        original_pane = deepcopy(self.snapshot["panes"][0])
        original_agent = deepcopy(self.snapshot["agents"][0])

        def respond(state, *args, **kwargs):
            if args == ("api", "snapshot"):
                return {"result": {"snapshot": deepcopy(self.snapshot)}}
            if args[:2] == ("workspace", "create"):
                self.snapshot["panes"] = [deepcopy(original_pane)]
                self.snapshot["workspaces"] = [{
                    "workspace_id": original_pane["workspace_id"],
                    "label": args[args.index("--label") + 1],
                }]
                return {"result": {"root_pane": deepcopy(original_pane)}}
            if args[:2] == ("agent", "start"):
                self.snapshot["agents"] = [deepcopy(original_agent)]
                return {"result": {}}
            raise AssertionError(f"Unexpected Herdr operation: {args}")

        self.calls.side_effect = respond
        for failure in ("missing binary", "process launch error"):
            with self.subTest(failure=failure):
                self.state.update(server_requested=False, socket=None, players={})
                self.snapshot = {"panes": [], "agents": [], "workspaces": []}
                self.write_state()
                with patch("dreamteam.runtime.shutil.which",
                           return_value=None if failure == "missing binary" else "/tools/herdr"), patch(
                    "dreamteam.runtime.subprocess.Popen", side_effect=OSError("cannot execute")
                ) as failed_launch:
                    with self.assertRaises(BackendError):
                        self.backend.start(self.player)
                    self.assertEqual(failed_launch.call_count,
                                     0 if failure == "missing binary" else 1)
                persisted = json.loads(self.backend.path.read_text())
                self.assertFalse(persisted["server_requested"])
                self.assertIsNone(persisted["socket"])
                with patch("dreamteam.runtime.shutil.which", return_value="/tools/herdr"), patch(
                    "dreamteam.runtime.subprocess.Popen"
                ) as retry_launch, patch("dreamteam.runtime.time.sleep") as sleep:
                    observation = Herdr(self.project).start(self.player)
                    retry_launch.assert_called_once()
                    sleep.assert_not_called()
                self.assertEqual(observation.activity, "idle")
                self.assertEqual(observation.pane_id, "pane-penny")
                self.assertTrue(json.loads(self.backend.path.read_text())["server_requested"])

    def test_disappeared_new_pane_does_not_receive_agent_launch(self):
        created = deepcopy(self.snapshot["panes"][0])
        self.state["players"] = {}
        self.snapshot = {"panes": [], "agents": [], "workspaces": []}
        self.write_state()

        def respond(state, *args, **kwargs):
            if args == ("api", "snapshot"):
                return {"result": {"snapshot": deepcopy(self.snapshot)}}
            if args[:2] == ("workspace", "create"):
                return {"result": {"root_pane": created}}
            raise AssertionError(f"Unexpected Herdr operation: {args}")

        self.calls.side_effect = respond
        with self.assertRaisesRegex(BackendError, "disappeared"):
            self.backend.start(self.player)
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(self.mutations()[0][:2], ("workspace", "create"))
        binding = json.loads(self.backend.path.read_text())["players"]["penny"]
        self.assertEqual(binding["pane_id"], "pane-penny")
        self.assertEqual(binding["phase"], "allocated")

    def test_preview_reads_visible_viewport_without_attaching_or_mutating(self):
        # A native attachment is allowed to remain the interactive owner.
        self.snapshot["panes"][0]["attached"] = True
        before = self.backend.path.read_bytes()
        preview = self.backend.preview(self.player)
        self.assertIsInstance(preview, Preview)
        self.assertEqual(preview.text, "Codex output")
        self.assertIsNone(preview.revision)
        self.assertGreater(preview.observed_at, 0)
        self.assertEqual(self.backend.path.read_bytes(), before)
        self.assertEqual([call.args[1:] for call in self.calls.call_args_list], [
            ("api", "snapshot"),
            ("api", "snapshot"),
        ])
        self.reads.assert_called_once()
        self.assertEqual(self.reads.call_args.args[0][1:], [
            "--session", "dreamteam-test", "pane", "read", "pane-penny",
            "--source", "visible", "--format", "text",
        ])
        environment = self.reads.call_args.kwargs["env"]
        self.assertEqual(environment["HERDR_CONFIG_PATH"], str(self.backend.directory / "herdr.toml"))
        self.assertNotIn("HERDR_SOCKET_PATH", environment)
        self.assertEqual(self.reads.call_args.kwargs["cwd"], self.project)

    def test_preview_is_empty_for_a_stopped_or_unbound_player(self):
        self.snapshot["panes"] = []
        self.assertEqual(self.backend.preview(self.player).text, "")
        self.state["players"] = {}
        self.write_state()
        self.calls.reset_mock()
        self.assertEqual(self.backend.preview(self.player).text, "")
        self.calls.assert_not_called()

    def test_preview_requires_a_confirmed_binding_and_agent(self):
        self.snapshot["agents"] = []
        with self.assertRaisesRegex(BackendError, "No verified Codex"):
            self.backend.preview(self.player)
        self.binding.pop("pane_id")
        self.write_state()
        before = self.backend.path.read_bytes()
        self.calls.reset_mock()
        with self.assertRaisesRegex(BackendError, "binding is not verified"):
            self.backend.preview(self.player)
        self.calls.assert_not_called()
        self.assertEqual(self.backend.path.read_bytes(), before)

    def test_preview_rejects_server_or_agent_changes_during_a_read(self):
        original_read = self.read_preview
        original_snapshot = deepcopy(self.snapshot)

        for change in ("server", "agent", "disappeared", "terminal"):
            with self.subTest(change=change):
                self.snapshot = deepcopy(original_snapshot)
                self.socket_mock.return_value = self.socket

                def read(command, **kwargs):
                    result = original_read(command, **kwargs)
                    if change == "server":
                        self.socket_mock.return_value = {**self.socket, "inode": 99}
                    elif change == "agent":
                        self.snapshot["agents"][0]["name"] = "another-player"
                    elif change == "terminal":
                        self.snapshot["panes"][0]["terminal_id"] = "another-terminal"
                    else:
                        self.snapshot["panes"] = []
                        self.snapshot["agents"] = []
                    return result

                self.reads.side_effect = read
                with self.assertRaises(BackendError):
                    self.backend.preview(self.player)

    def test_preview_propagates_read_errors_without_saving_state(self):
        before = self.backend.path.read_bytes()
        cases = (
            subprocess.CompletedProcess(["herdr"], 1, "", "read failed"),
            OSError("cannot execute"),
            subprocess.TimeoutExpired(["herdr"], 5),
        )
        for failure in cases:
            with self.subTest(failure=type(failure).__name__):
                if isinstance(failure, Exception):
                    self.reads.side_effect = failure
                else:
                    self.reads.side_effect = None
                    self.reads.return_value = failure
                with self.assertRaisesRegex(BackendError, "terminal preview"):
                    self.backend.preview(self.player)
                self.assertEqual(self.backend.path.read_bytes(), before)

    def test_preview_strips_ansi_payloads_and_terminal_controls(self):
        self.preview_text = (
            "safe \x1b[31mred\x1b[0m "
            "\x1b]8;;https://example.test\x1b\\link\x1b]8;;\x1b\\"
            "\x1b]52;c;clipboard-payload\x07"
            "\x1bPdevice-payload\x1b\\\x9dtitle-payload\x9c"
            "\x9b2J\x00\x08\x7f\u202e\r\nnext\t✓"
        )
        self.assertEqual(self.backend.preview(self.player).text, "safe red link\nnext\t✓")
        self.preview_text = "visible\x1b]52;c;unterminated-payload"
        self.assertEqual(self.backend.preview(self.player).text, "visible")

    def test_invalid_runtime_file_is_preserved_without_runtime_calls(self):
        self.backend.path.write_text("{interrupted file")
        with self.assertRaisesRegex(BackendError, "Cannot read"):
            self.backend.start(self.player)
        self.assertEqual(self.backend.path.read_text(), "{interrupted file")
        self.calls.assert_not_called()


    def enable_shutdown_responses(self):
        self.server_running = True
        self.close_mode = "removed"
        self.stop_mode = "stopped"
        self.shell_info = {
            "pane_id": self.binding["pane_id"], "shell_pid": 100,
            "foreground_process_group_id": 100,
            "foreground_processes": [{"pid": 100, "name": "zsh"}],
        }

        def respond(state, *args, **kwargs):
            if args == ("status", "server", "--json"):
                return {"session": state["session"], "running": self.server_running}
            if args[:2] == ("pane", "process-info"):
                return {"result": {"process_info": deepcopy(self.shell_info)}}
            if args == ("pane", "close", self.binding["pane_id"]):
                if self.close_mode != "timeout_remaining":
                    self.snapshot["panes"] = [pane for pane in self.snapshot["panes"]
                                              if pane["pane_id"] != self.binding["pane_id"]]
                    self.snapshot["agents"] = []
                    self.snapshot["workspaces"] = []
                if self.close_mode.startswith("timeout"):
                    raise BackendError("Close timed out")
                return {"result": {"type": "ok"}}
            if args == ("session", "stop", self.state["session"], "--json"):
                self.assertEqual(self.snapshot["panes"], [])
                if self.stop_mode != "timeout_running":
                    self.server_running = False
                if self.stop_mode.startswith("timeout"):
                    raise BackendError("Stop timed out")
                return {"stopped": True}
            return self.respond(state, *args, **kwargs)

        self.calls.side_effect = respond

    def shutdown_mutations(self):
        return [call.args[1:] for call in self.calls.call_args_list
                if call.args[1:3] in (("pane", "close"), ("session", "stop"))]

    def test_shutdown_closes_owned_panes_stops_empty_server_and_resets_intent(self):
        self.enable_shutdown_responses()
        summary = self.backend.shutdown((self.player,))
        self.assertIn("closed 1 player panes", summary)
        self.assertIn("stopped dedicated session", summary)
        self.assertEqual(self.shutdown_mutations(), [
            ("pane", "close", self.binding["pane_id"]),
            ("session", "stop", self.state["session"], "--json"),
        ])
        persisted = json.loads(self.backend.path.read_text())
        self.assertFalse(persisted["server_requested"])
        self.assertIsNone(persisted["socket"])
        self.assertEqual(persisted["players"], {})
        self.assertNotEqual(persisted["session"], self.state["session"])

    def test_shutdown_preserves_untracked_pane_and_its_server(self):
        self.enable_shutdown_responses()
        self.snapshot["panes"].append({
            **self.snapshot["panes"][0], "pane_id": "untracked", "terminal_id": "other-terminal",
        })
        summary = self.backend.shutdown((self.player,))
        self.assertIn("untracked", summary)
        self.assertIn("remains running", summary)
        self.assertEqual(self.shutdown_mutations(), [("pane", "close", self.binding["pane_id"])])
        persisted = json.loads(self.backend.path.read_text())
        self.assertTrue(persisted["server_requested"])
        self.assertEqual(persisted["session"], self.state["session"])
        self.assertEqual(persisted["players"], {})

    def test_shutdown_identifies_an_untracked_calling_terminal(self):
        self.enable_shutdown_responses()
        self.snapshot["panes"].append({
            **self.snapshot["panes"][0], "pane_id": "courtside", "terminal_id": "courtside-terminal",
        })
        with patch.dict("os.environ", {
            "HERDR_SOCKET_PATH": self.socket["path"], "HERDR_PANE_ID": "courtside",
        }):
            summary = self.backend.shutdown((self.player,))
        self.assertIn("courtside: current Courtside terminal", summary)
        self.assertIn("remains running", summary)

    def test_shutdown_preserves_the_current_calling_terminal(self):
        self.enable_shutdown_responses()
        original = self.backend.path.read_bytes()
        with patch.dict("os.environ", {
            "HERDR_SOCKET_PATH": self.socket["path"], "HERDR_PANE_ID": self.binding["pane_id"],
        }):
            with self.assertRaisesRegex(BackendError, "current Courtside terminal"):
                self.backend.shutdown((self.player,))
        self.assertEqual(self.shutdown_mutations(), [])
        self.assertEqual(self.backend.path.read_bytes(), original)

    def test_shutdown_only_closes_confirmed_shell_when_codex_has_exited(self):
        for idle in (False, True):
            with self.subTest(idle=idle):
                self.write_state()
                self.snapshot["panes"] = [{
                    "pane_id": self.binding["pane_id"], "terminal_id": self.binding["terminal_id"],
                    "workspace_id": self.binding["workspace_id"],
                }]
                self.snapshot["agents"] = []
                self.snapshot["workspaces"] = [{
                    "workspace_id": self.binding["workspace_id"], "label": self.binding["label"],
                }]
                self.calls.reset_mock()
                self.enable_shutdown_responses()
                if not idle:
                    self.shell_info["foreground_process_group_id"] = 200
                    self.shell_info["foreground_processes"] = [{"pid": 200, "name": "python"}]
                if idle:
                    summary = self.backend.shutdown((self.player,))
                    self.assertIn("stopped dedicated session", summary)
                else:
                    with self.assertRaisesRegex(BackendError, "another foreground process"):
                        self.backend.shutdown((self.player,))
                    self.assertEqual(self.shutdown_mutations(), [])
                    self.assertIn(self.player.id, json.loads(self.backend.path.read_text())["players"])

    def test_shutdown_preflights_every_binding_before_closing_anything(self):
        self.enable_shutdown_responses()
        self.state["players"]["unknown"] = deepcopy(self.binding)
        self.write_state()
        original = self.backend.path.read_bytes()
        with self.assertRaisesRegex(BackendError, "saved binding for unknown"):
            self.backend.shutdown((self.player,))
        self.assertEqual(self.shutdown_mutations(), [])
        self.assertEqual(self.backend.path.read_bytes(), original)

    def test_shutdown_rejects_changed_server_or_player_identity(self):
        self.enable_shutdown_responses()
        original = self.backend.path.read_bytes()
        self.socket_mock.return_value = {**self.socket, "inode": 999}
        with self.assertRaisesRegex(BackendError, "server changed"):
            self.backend.shutdown((self.player,))
        self.socket_mock.return_value = self.socket
        self.snapshot["panes"][0]["terminal_id"] = "replacement"
        with self.assertRaisesRegex(BackendError, "pane identity changed"):
            self.backend.shutdown((self.player,))
        self.assertEqual(self.shutdown_mutations(), [])
        self.assertEqual(self.backend.path.read_bytes(), original)

    def test_shutdown_does_not_forget_a_moved_terminal(self):
        self.enable_shutdown_responses()
        original = self.backend.path.read_bytes()
        self.snapshot["panes"][0]["pane_id"] = "moved-pane"
        with self.assertRaisesRegex(BackendError, "terminal moved"):
            self.backend.shutdown((self.player,))
        self.assertEqual(self.shutdown_mutations(), [])
        self.assertEqual(self.backend.path.read_bytes(), original)

    def test_shutdown_reconciles_close_timeout_without_retrying_delivery(self):
        original = deepcopy(self.snapshot)
        for mode in ("timeout_removed", "timeout_remaining"):
            with self.subTest(mode=mode):
                self.write_state()
                self.snapshot = deepcopy(original)
                self.calls.reset_mock()
                self.enable_shutdown_responses()
                self.close_mode = mode
                if mode == "timeout_remaining":
                    with self.assertRaisesRegex(BackendError, "Close timed out") as failure:
                        self.backend.shutdown((self.player,))
                    summary = str(failure.exception)
                else:
                    summary = self.backend.shutdown((self.player,))
                closes = [args for args in self.shutdown_mutations() if args[:2] == ("pane", "close")]
                self.assertEqual(len(closes), 1)
                persisted = json.loads(self.backend.path.read_text())
                if mode == "timeout_removed":
                    self.assertIn("stopped dedicated session", summary)
                    self.assertFalse(persisted["server_requested"])
                else:
                    self.assertIn("Close timed out", summary)
                    self.assertIn(self.player.id, persisted["players"])
                    self.assertTrue(persisted["server_requested"])

    def test_shutdown_reconciles_stop_timeout_without_false_success(self):
        original = deepcopy(self.snapshot)
        for mode in ("timeout_stopped", "timeout_running"):
            with self.subTest(mode=mode):
                self.write_state()
                self.snapshot = deepcopy(original)
                self.calls.reset_mock()
                self.enable_shutdown_responses()
                self.stop_mode = mode
                if mode == "timeout_running":
                    with self.assertRaisesRegex(BackendError, "stopping session") as failure:
                        self.backend.shutdown((self.player,))
                    summary = str(failure.exception)
                else:
                    summary = self.backend.shutdown((self.player,))
                persisted = json.loads(self.backend.path.read_text())
                if mode == "timeout_stopped":
                    self.assertIn("stopped dedicated session", summary)
                    self.assertFalse(persisted["server_requested"])
                else:
                    self.assertIn("stopping session", summary)
                    self.assertIn("unconfirmed", summary)
                    self.assertTrue(persisted["server_requested"])
                    self.assertEqual(persisted["session"], self.state["session"])

    def test_shutdown_already_stopped_server_resets_intent_without_mutations(self):
        self.enable_shutdown_responses()
        self.server_running = False
        summary = self.backend.shutdown((self.player,))
        self.assertIn("already stopped", summary)
        self.assertEqual(self.shutdown_mutations(), [])
        self.assertFalse(json.loads(self.backend.path.read_text())["server_requested"])


class ErrorCodeTests(unittest.TestCase):
    def test_structured_herdr_rejection_retains_code(self):
        response = json.dumps({"error": {"code": "agent_blocked", "message": "Answer the existing question first."}})
        with patch("dreamteam.runtime.shutil.which", return_value="/tools/herdr"), patch(
            "dreamteam.runtime.subprocess.run",
            return_value=subprocess.CompletedProcess(["herdr"], 1, "", response),
        ):
            with self.assertRaises(BackendError) as failure:
                Herdr(Path.cwd())._call({"session": "dreamteam-test"}, "agent", "prompt", "pane", "task")
        self.assertEqual(failure.exception.code, "agent_blocked")


if __name__ == "__main__":
    unittest.main()
