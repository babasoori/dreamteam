"""Offline contracts for Coach task delivery, reports, and command-line use."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import tomllib
import unittest
from unittest.mock import Mock, call, patch

from dreamteam.coordination import COACH_INSTRUCTIONS, Team, coach_player, coordinated_player, command_prefix, main
from dreamteam.plays import PlayStore
from dreamteam.runtime import BackendError, Herdr, Observation, Preview, PromptRejected, PromptUncertain


ROSTER = """version = 1

[players.penny]
name = "Penny"
position = "pg"
agent = "codex"
cwd = "."

[players.air_jordan]
name = "Air Jordan"
position = "sg"
agent = "codex"
cwd = "."
"""


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="dreamteam coach ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / 'team "roster".toml'
        self.config.write_text(ROSTER)
        self.backend = Mock(spec=Herdr)
        self.backend.refresh.side_effect = lambda players: {
            player.id: Observation("idle", "Verified.", f"pane-{player.id}", time.time())
            for player in players
        }
        self.backend.start.side_effect = lambda player: Observation(
            "idle", "Verified.", f"pane-{player.id}", time.time(),
        )
        self.backend.send_prompt.return_value = {
            "delivery": "submitted", "player_id": "penny",
            "terminal_id": "term-penny", "acknowledged_at": 123.0,
        }
        self.backend.read_output.return_value = Preview("Penny's visible output.", 123.0)
        self.factory = self.enterContext(patch("dreamteam.coordination.Herdr", return_value=self.backend))

    def team(self, project=None):
        return Team(self.config, project or self.root)

    def assert_no_delivery(self):
        self.backend.start.assert_not_called()
        self.backend.send_prompt.assert_not_called()

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["dreamteam-coordination", "--config", str(self.config), "--project", str(self.root), *arguments]
        code = 0
        with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                main()
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_and_read_observe_named_players_without_creating_state(self):
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        team = self.team()
        status = team.status()
        output = team.read("penny")
        self.assertEqual(status["project"], str(self.root))
        self.assertIsNone(status["play"])
        self.assertEqual(status["tasks"], [])
        self.assertEqual(status["coach_instructions"], COACH_INSTRUCTIONS)
        self.assertEqual([player["id"] for player in status["players"]], ["penny", "air_jordan"])
        self.assertEqual(status["players"][0]["activity"]["activity"], "idle")
        self.assertTrue(status["players"][0]["role"])
        self.assertIn("acceptance", status["note"])
        self.assertEqual(output, {"player_id": "penny", "observed_at": 123.0, "output": "Penny's visible output."})
        self.backend.refresh.assert_called_once_with(team.players)
        self.backend.read_output.assert_called_once_with(team.player("penny"))
        self.assertEqual(before, sorted(path.relative_to(self.root) for path in self.root.rglob("*")))
        self.assert_no_delivery()

    def test_coach_arguments_are_valid_toml_and_tools_pin_literal_paths(self):
        for project in (self.root, self.root / "Coach's 🏀 project"):
            with self.subTest(project=project):
                coach = coach_player(self.config, project)
                self.assertEqual((coach.id, coach.name, coach.position), ("coach", "Coach", "coach"))
                self.assertEqual(coach.cwd, project.resolve())
                self.assertLess(len(shlex.join(["codex", *coach.args]).encode()), 850)
                self.assertTrue(all(arg == "-c" for arg in coach.args[::2]))
                settings = tomllib.loads("\n".join(coach.args[1::2]))
                instructions = settings["developer_instructions"]
                server = settings["mcp_servers"]["dreamteam"]
                self.assertEqual(server["command"], sys.executable)
                self.assertEqual(server["args"], [
                    "-m", "dreamteam.coach_tools", "--config", str(self.config),
                    "--project", str(project.resolve()), "--actor", "coach",
                ])
                self.assertNotIn("cwd", server)
                self.assertTrue(server["required"])
                self.assertIn("team_status", instructions)
                self.assertIn("dispatch_task", COACH_INSTRUCTIONS)
                self.assertIn("normal Codex approvals", COACH_INSTRUCTIONS)
                self.assertNotIn("sandbox_mode", settings)
                self.assertNotIn("approval_policy", settings)
                for forbidden in ("OPENAI_API_KEY", "--api-key", "--yolo",
                                  "--dangerously-bypass-approvals-and-sandbox"):
                    self.assertNotIn(forbidden, instructions)
                self.assertFalse((project / ".dreamteam").exists())
        self.factory.assert_not_called()
        self.assert_no_delivery()

    def test_player_tools_preserve_custom_args_and_bind_report_actor(self):
        from dreamteam.roster import Player
        player = Player("penny", "Penny", "pg", self.root, ("--model", "chosen"))
        profiled = coordinated_player(player, self.config, self.root)
        self.assertEqual(profiled.args[:2], player.args)
        settings = tomllib.loads("\n".join(profiled.args[3::2]))
        self.assertEqual(settings["mcp_servers"]["dreamteam"]["args"][-2:], ["--actor", "penny"])
        self.assertNotIn("developer_instructions", settings)
        self.assertEqual(profiled.cwd, player.cwd)

    def test_disconnected_client_cannot_start_or_send(self):
        cancelled = False
        team = Team(self.config, self.root, cancelled=lambda: cancelled)
        cancelled = True
        with self.assertRaisesRegex(BackendError, "disconnected"):
            team.dispatch("penny", "Task", "disconnected")
        self.assert_no_delivery()
        cancelled = False
        def start(player):
            nonlocal cancelled
            cancelled = True
            return Observation("idle", "Ready")
        self.backend.start.side_effect = start
        task = team.dispatch("penny", "Task", "disconnected-after-start")
        self.assertEqual(task["delivery"], "not_sent")
        self.backend.send_prompt.assert_not_called()
        with self.assertRaisesRegex(BackendError, "disconnected"):
            team.wait(task["id"], 30)

    def test_coach_observed_reports_retain_provenance(self):
        team = self.team()
        task = team.dispatch("penny", "Return a plan", "legacy-player")
        result = team.report(task["id"], "penny", "done", "Visible plan reviewed",
                             source="coach_observed")
        self.assertEqual(result["reports"][-1]["source"], "coach_observed")
        self.assertEqual(result["progress"], "reported_done")

    def test_stopped_assignment_can_be_closed_without_claiming_completion(self):
        team = self.team()
        task = team.dispatch("penny", "Old task", "old")
        for activity in ("working", "idle", "unknown"):
            self.backend.refresh.side_effect = lambda players, a=activity: {
                p.id: Observation(a, "Observed") for p in players}
            with self.assertRaisesRegex(ValueError, "confirmed stopped"):
                team.close_stopped_task(task["id"], "User stopped this work")
        self.backend.refresh.side_effect = lambda players: {
            p.id: Observation("stopped", "Stopped") for p in players}
        closed = team.close_stopped_task(task["id"], "User stopped this work")
        self.assertEqual(closed["progress"], "cancelled")
        self.assertTrue(team.wait(task["id"], 0)["finished_wait"])
        with self.assertRaisesRegex(ValueError, "cannot revive"):
            team.report(task["id"], "penny", "done", "Late report")
        new = team.dispatch("penny", "New bounded task", "new")
        self.assertEqual(new["delivery"], "submitted")

    def test_preparing_assignment_requires_replacement_coach_before_closing(self):
        team = Team(self.config, self.root, coordinator_token="old-coach")
        # Simulate a killed sender after its durable intent but before final receipt.
        self.backend.start.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            team.dispatch("penny", "Old task", "interrupted")
        task_id = team._load()["tasks"][0]["id"]
        self.backend.refresh.side_effect = lambda players: {
            p.id: Observation("stopped", "Stopped") for p in players}
        with self.assertRaisesRegex(ValueError, "Stop and restart Coach"):
            team.close_stopped_task(task_id, "Abandoned")
        replacement = Team(self.config, self.root, coordinator_token="new-coach")
        self.assertEqual(replacement.close_stopped_task(task_id, "Abandoned")["progress"], "cancelled")

    def test_dispatch_starts_one_named_player_then_sends_one_scoped_assignment(self):
        project = self.root / "fastbreak"
        project.mkdir()
        play = PlayStore(self.root).create(
            project, "Build a CLI wrapper.", "Do not connect to a broker.", "Offline doctor and tests pass.",
        )
        team = self.team(project)
        order = Mock()
        order.attach_mock(self.backend.start, "start")
        order.attach_mock(self.backend.send_prompt, "send")
        task = team.dispatch("penny", "Inspect the API and return a bounded plan.", "plan-001")
        player, prompt = self.backend.send_prompt.call_args.args
        self.assertEqual(player, team.player("penny"))
        self.assertEqual(player.cwd, project)
        self.assertEqual(order.mock_calls, [call.start(player), call.send(player, prompt)])
        for text in (task["id"], player.role, str(project), play.goal, play.constraints,
                     play.acceptance, "Inspect the API and return a bounded plan."):
            self.assertIn(text, prompt)
        report_line = next(line for line in prompt.splitlines() if " -m dreamteam.coordination " in line and " report " in line)
        self.assertEqual(
            shlex.split(report_line),
            command_prefix(self.config, project) +
            ["report", task["id"], "--player", "penny", "--state", "STATE", "--summary", "SUMMARY"],
        )
        self.assertEqual(task["delivery"], "submitted")
        self.assertEqual(task["progress"], "assigned")
        self.assertEqual(task["reports"], [])
        self.assertEqual(task["receipt"]["terminal_id"], "term-penny")

    def test_retry_with_same_request_returns_current_record_without_resending(self):
        team = self.team()
        first = team.dispatch("penny", "Plan the wrapper.", "repeat-001")
        team.report(first["id"], "penny", "done", "Plan ready for the coach's review.")
        retry = self.team().dispatch("penny", "  Plan the wrapper.\n", "repeat-001")
        self.assertEqual(retry["id"], first["id"])
        self.assertEqual(retry["progress"], "reported_done")
        self.backend.start.assert_called_once()
        self.backend.send_prompt.assert_called_once()
        self.assertEqual(len(team.status()["tasks"]), 1)

    def test_reusing_request_for_another_task_or_player_is_rejected(self):
        team = self.team()
        team.dispatch("penny", "Plan the wrapper.", "same-id")
        for player, task in (("penny", "Implement it instead."), ("air_jordan", "Plan the wrapper.")):
            with self.subTest(player=player):
                with self.assertRaisesRegex(ValueError, "different assignment"):
                    team.dispatch(player, task, "same-id")
        self.backend.start.assert_called_once()
        self.backend.send_prompt.assert_called_once()
        self.assertEqual(len(team.status()["tasks"]), 1)

    def test_unresolved_assignment_blocks_same_player_but_allows_independent_player(self):
        team = self.team()
        first = team.dispatch("penny", "Plan the wrapper.", "plan-1")
        for state in (None, "working", "blocked"):
            with self.subTest(state=state):
                if state is not None:
                    team.report(first["id"], "penny", state, "Still assigned.")
                with self.assertRaisesRegex(ValueError, "unresolved"):
                    team.dispatch("penny", "Another plan.", "plan-2")
        self.backend.send_prompt.assert_called_once()
        other = team.dispatch("air_jordan", "Inspect CLI conventions only.", "inspect-1")
        self.assertEqual(other["delivery"], "submitted")
        team.report(first["id"], "penny", "done", "Plan is ready for review.")
        next_task = team.dispatch("penny", "Clarify the proposed tests.", "plan-2")
        self.assertNotEqual(first["id"], next_task["id"])
        self.assertEqual(self.backend.send_prompt.call_count, 3)

    def test_busy_or_failed_start_is_not_sent_and_same_request_is_not_automatically_retried(self):
        team = self.team()
        failures = [
            Observation("working", "Working."),
            Observation("blocked", "Needs input."),
            Observation("unknown", "Not verified."),
            BackendError("Start failed."),
            OSError("Executable unavailable."),
        ]
        for number, failure in enumerate(failures):
            with self.subTest(failure=failure):
                self.backend.start.reset_mock()
                self.backend.start.side_effect = failure if isinstance(failure, Exception) else lambda player, result=failure: result
                request = f"start-{number}"
                task = team.dispatch("penny", "Plan the wrapper.", request)
                self.assertEqual(task["delivery"], "not_sent")
                self.assertEqual(task["progress"], "assigned")
                self.assertTrue(task["delivery_detail"])
                retry = team.dispatch("penny", "Plan the wrapper.", request)
                self.assertEqual(retry["id"], task["id"])
                self.backend.start.assert_called_once()
                self.backend.send_prompt.assert_not_called()

    def test_rejection_and_uncertain_delivery_are_distinct_and_neither_is_replayed(self):
        team = self.team()
        for request, error, delivery in (
            ("rejected", PromptRejected("Not ready; no prompt was sent."), "not_sent"),
            ("uncertain", PromptUncertain("Acknowledgment was lost; do not resend."), "uncertain"),
        ):
            with self.subTest(delivery=delivery):
                self.backend.send_prompt.side_effect = error
                task = team.dispatch("penny", "Plan the wrapper.", request)
                self.assertEqual(task["delivery"], delivery)
                calls = self.backend.send_prompt.call_count
                starts = self.backend.start.call_count
                retry = self.team().dispatch("penny", "Plan the wrapper.", request)
                self.assertEqual(retry["id"], task["id"])
                self.assertEqual(self.backend.send_prompt.call_count, calls)
                self.assertEqual(self.backend.start.call_count, starts)
        with self.assertRaisesRegex(ValueError, "unresolved"):
            team.dispatch("penny", "Plan the wrapper.", "new-id-after-uncertainty")

    def test_report_requires_owner_and_valid_state_and_done_is_not_human_acceptance(self):
        team = self.team()
        task = team.dispatch("penny", "Plan the wrapper.", "report-1")
        before = team.path.read_bytes()
        for task_id, player, state, summary in (
            (task["id"], "air_jordan", "done", "Wrong owner."),
            ("unknown-task", "penny", "done", "Unknown assignment."),
            (task["id"], "penny", "accepted", "Not a player report."),
            (task["id"], "penny", "working", " "),
        ):
            with self.subTest(player=player, state=state, task_id=task_id):
                with self.assertRaises(ValueError):
                    team.report(task_id, player, state, summary)
                self.assertEqual(team.path.read_bytes(), before)
        team.report(task["id"], "penny", "working", "Inspecting API signatures.")
        done = team.report(task["id"], "penny", "done", "Plan and test checklist ready.")
        self.assertEqual(done["progress"], "reported_done")
        self.assertEqual([report["state"] for report in done["reports"]], ["working", "done"])
        self.assertFalse(done.get("accepted", False))
        self.assertIn("human acceptance", team.status()["note"])
        with self.assertRaisesRegex(ValueError, "cannot move back"):
            team.report(task["id"], "penny", "working", "Reopening without a new task.")

    def test_not_sent_assignment_cannot_claim_a_player_report(self):
        self.backend.send_prompt.side_effect = PromptRejected("No delivery.")
        team = self.team()
        task = team.dispatch("penny", "Plan the wrapper.", "not-sent")
        with self.assertRaisesRegex(ValueError, "not sent"):
            team.report(task["id"], "penny", "done", "Claimed completion.")
        self.assertEqual(team.status()["tasks"][0]["progress"], "assigned")

    def test_report_arriving_during_delivery_is_preserved_when_receipt_is_saved(self):
        team = self.team()
        errors = []

        def send(player, prompt):
            def report():
                try:
                    observer = self.team()
                    pending = observer.status()["tasks"][-1]
                    observer.report(pending["id"], player.id, "done", "Completed before acknowledgment.")
                except BaseException as exc:
                    errors.append(exc)

            reporter = Thread(target=report, daemon=True)
            reporter.start()
            reporter.join(timeout=2)
            self.assertFalse(reporter.is_alive(), "Delivery must not hold the report ledger lock.")
            if errors:
                raise errors[0]
            return {"delivery": "submitted", "terminal_id": "term-penny"}

        self.backend.send_prompt.side_effect = send
        task = team.dispatch("penny", "Read a tiny file.", "fast-result")
        self.assertEqual(task["delivery"], "submitted")
        self.assertEqual(task["progress"], "reported_done")
        self.assertEqual(task["summary"], "Completed before acknowledgment.")
        self.assertEqual(len(task["reports"]), 1)
        self.assertTrue(team.wait(task["id"], 0)["finished_wait"])

    def test_interrupted_preparing_assignment_is_persisted_and_never_replayed(self):
        team = self.team()
        self.backend.start.side_effect = KeyboardInterrupt("Process interrupted after intent was saved.")
        with self.assertRaises(KeyboardInterrupt):
            team.dispatch("penny", "Plan the wrapper.", "interrupted")
        persisted = self.team().status()["tasks"][0]
        self.assertEqual(persisted["delivery"], "preparing")
        self.backend.start.reset_mock()
        self.backend.start.side_effect = lambda player: Observation("idle", "Ready.")
        retry = self.team().dispatch("penny", "Plan the wrapper.", "interrupted")
        self.assertEqual(retry["id"], persisted["id"])
        self.assertEqual(retry["delivery"], "preparing")
        self.assert_no_delivery()
        with self.assertRaisesRegex(ValueError, "unresolved"):
            team.dispatch("penny", "Plan the wrapper.", "replacement-id")

    def test_wait_timeout_does_not_mean_done_and_blocked_report_ends_wait(self):
        team = self.team()
        task = team.dispatch("penny", "Plan the wrapper.", "wait-1")
        self.backend.refresh.reset_mock()
        result = team.wait(task["id"], 0)
        self.assertFalse(result["finished_wait"])
        self.assertEqual(result["task"]["progress"], "assigned")
        self.assertIn("does not mean completion", result["note"])
        self.backend.refresh.assert_not_called()
        team.report(task["id"], "penny", "blocked", "Need the intended CLI command list.")
        blocked = team.wait(task["id"], 0)
        self.assertTrue(blocked["finished_wait"])
        self.assertEqual(blocked["task"]["progress"], "blocked")
        for timeout in (-1, 31):
            with self.assertRaises(ValueError):
                team.wait(task["id"], timeout)

    def test_project_pin_mismatch_fails_before_runtime_and_saved_play_routes_all_players(self):
        project = self.root / "fastbreak"
        project.mkdir()
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            self.team(project)
        self.factory.assert_not_called()
        PlayStore(self.root).create(project, "Build a wrapper.", "No broker.", "Offline tests pass.")
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            self.team(self.root)
        self.factory.assert_not_called()
        team = self.team(project)
        self.assertTrue(all(player.cwd == project for player in team.players))
        self.factory.assert_called_once_with(project)
        self.assert_no_delivery()

    def test_invalid_target_task_and_request_are_rejected_before_creating_ledger(self):
        team = self.team()
        for player, task, request in (
            ("coach", "Plan.", "request-1"),
            ("missing", "Plan.", "request-1"),
            ("penny", " ", "request-1"),
            ("penny", "Plan.\x1b[2J", "request-1"),
            ("penny", "Plan.", "../request"),
        ):
            with self.subTest(player=player, request=request):
                with self.assertRaises(ValueError):
                    team.dispatch(player, task, request)
        self.assertFalse(team.directory.exists())
        self.assert_no_delivery()

    def test_cli_task_file_report_wait_round_trip_uses_json_and_one_delivery(self):
        task_file = self.root / "task file.txt"
        task_file.write_text("Inspect the API.\nReturn a short plan.")
        code, stdout, stderr = self.cli(
            "dispatch", "penny", "--request-id", "cli-plan", "--task-file", str(task_file),
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        task = json.loads(stdout)
        self.assertEqual(task["delivery"], "submitted")
        self.assertIn(task_file.read_text(), self.backend.send_prompt.call_args.args[1])
        code, stdout, stderr = self.cli(
            "report", task["id"], "--player", "penny", "--state", "done", "--summary", "Plan ready.",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["progress"], "reported_done")
        code, stdout, stderr = self.cli("wait", task["id"], "--timeout", "0")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["finished_wait"])
        self.backend.send_prompt.assert_called_once()

    def test_cli_unsent_dispatch_is_nonzero_and_returns_record_without_retrying(self):
        self.backend.send_prompt.side_effect = PromptRejected("Player is not ready.")
        for _ in range(2):
            code, stdout, stderr = self.cli(
                "dispatch", "penny", "--request-id", "cli-rejected", "--task", "Inspect only.",
            )
            self.assertEqual(code, 1)
            self.assertEqual(stderr, "")
            self.assertEqual(json.loads(stdout)["delivery"], "not_sent")
        self.backend.start.assert_called_once()
        self.backend.send_prompt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
