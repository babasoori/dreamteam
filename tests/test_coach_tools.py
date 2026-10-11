"""Narrow stdio MCP contracts, with fake coordination and no agent processes."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import select
from threading import Event, Thread
import time
import unittest
from unittest.mock import Mock, patch

from dreamteam.coach_tools import CoachTools, MAX_MESSAGE, PROTOCOL_VERSION, serve
from dreamteam.runtime import BackendError


INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {},
               "clientInfo": {"name": "test", "version": "1"}},
}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.config = Path("/tmp/dreamteam-test/team.toml").resolve()
        self.project = self.config.parent
        self.team = Mock()
        self.team.project = self.project
        self.team.status.return_value = {"players": [], "tasks": []}
        self.team.read.return_value = {"player_id": "penny", "output": "Verified visible response"}
        self.team.dispatch.return_value = {"delivery": "submitted", "id": "task-1"}
        self.team.wait.return_value = {"finished_wait": False, "note": "Not completion."}
        self.team.report.return_value = {"progress": "reported_done"}
        self.team.close_stopped_task.return_value = {"progress": "cancelled"}
        self.factory = self.enterContext(patch("dreamteam.coach_tools.Team", return_value=self.team))
        self.bridge = self.make_bridge("coach")

    def make_bridge(self, actor, **kwargs):
        bridge = CoachTools(self.config, self.project, actor, coordinator_token="original-coach", **kwargs)
        bridge.handle(INIT)
        bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return bridge

    def tool(self, name, arguments=None, bridge=None):
        return (bridge or self.bridge).handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    def test_initialization_negotiates_supported_version_and_requires_ready_notification(self):
        bridge = CoachTools(self.config, self.project, "coach")
        response = bridge.handle(INIT)
        self.assertEqual(response["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(response["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertIn("Delegate requested work to the visible roster", response["result"]["instructions"])
        early = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(early["error"]["code"], -32002)
        bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIn("tools", bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"])
        self.assertEqual(bridge.handle(INIT)["error"]["code"], -32600)
        self.factory.assert_not_called()

    def test_ping_and_unknown_notifications_are_side_effect_free(self):
        self.assertEqual(self.bridge.handle({"jsonrpc": "2.0", "id": "ping", "method": "ping"})["result"], {})
        self.assertIsNone(self.bridge.handle({"jsonrpc": "2.0", "method": "notifications/anything"}))
        self.assertIsNone(self.bridge.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "dispatch_task", "arguments": {"player_id": "penny", "task": "Do work", "request_id": "id"}},
        }))
        self.factory.assert_not_called()

    def test_invalid_requests_and_methods_return_protocol_errors(self):
        for message in (None, [], {}, {"jsonrpc": "2.0", "id": True, "method": "ping"}):
            self.assertEqual(self.bridge.handle(message)["error"]["code"], -32600)
        self.assertEqual(self.bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "exec"})["error"]["code"], -32601)
        self.assertEqual(self.bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": []})["error"]["code"], -32602)
        self.factory.assert_not_called()

    def test_coach_and_player_tools_have_distinct_capabilities(self):
        listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        coach = self.bridge.handle(listing)["result"]["tools"]
        self.assertEqual({tool["name"] for tool in coach}, {
            "team_status", "read_player_output", "dispatch_task", "wait_for_report",
            "record_play", "record_player_report", "close_stopped_task",
        })
        player = self.make_bridge("penny")
        self.assertEqual([tool["name"] for tool in player.handle(listing)["result"]["tools"]], ["report_progress"])
        self.assertIn("error", self.tool("dispatch_task", {"player_id": "penny", "task": "work", "request_id": "one"}, player))
        self.assertIn("error", self.tool("report_progress", {"task_id": "one", "state": "done", "summary": "done"}))
        self.factory.assert_not_called()

    def test_every_tool_call_rechecks_pinned_project_and_preserves_startup_token(self):
        self.tool("team_status")
        self.tool("read_player_output", {"player_id": "penny"})
        self.assertEqual(self.factory.call_count, 2)
        for call in self.factory.call_args_list:
            self.assertEqual(call.args, (self.config, self.project))
            self.assertEqual(call.kwargs["coordinator_token"], "original-coach")
            self.assertFalse(call.kwargs["cancelled"]())
        self.factory.side_effect = ValueError("The Coach project no longer matches the saved play.")
        response = self.tool("team_status")
        self.assertTrue(response["result"]["isError"])
        self.assertIn("no longer matches", response["result"]["content"][0]["text"])
        self.assertEqual(self.team.status.call_count, 1)

    def test_missing_coach_generation_fails_closed(self):
        bridge = self.make_bridge("coach")
        bridge.coordinator_token = None
        response = self.tool("team_status", bridge=bridge)
        self.assertTrue(response["result"]["isError"])
        self.factory.assert_not_called()

    def test_dispatch_and_wait_use_exact_bounded_arguments(self):
        response = self.tool("dispatch_task", {"player_id": "penny", "task": "Inspect only.", "request_id": "inspect-1"})
        self.team.dispatch.assert_called_once_with("penny", "Inspect only.", "inspect-1")
        self.assertEqual(response["result"]["structuredContent"]["delivery"], "submitted")
        self.assertFalse(response["result"]["isError"])
        self.tool("wait_for_report", {"task_id": "task-1"})
        self.team.wait.assert_called_once_with("task-1", 20)
        self.tool("wait_for_report", {"task_id": "task-1", "timeout": 30})
        self.assertEqual(self.team.wait.call_args.args, ("task-1", 30))

    def test_uncertain_or_unsent_delivery_is_a_tool_error_without_retry(self):
        for delivery in ("uncertain", "not_sent"):
            with self.subTest(delivery=delivery):
                self.team.dispatch.reset_mock()
                self.team.dispatch.return_value = {"delivery": delivery, "delivery_detail": "Inspect before retry."}
                response = self.tool("dispatch_task", {"player_id": "penny", "task": "Task", "request_id": "one"})
                self.assertTrue(response["result"]["isError"])
                self.team.dispatch.assert_called_once()

    def test_player_report_identity_is_fixed_and_coach_observation_is_distinct(self):
        player = self.make_bridge("penny")
        arguments = {"task_id": "task-1", "state": "done", "summary": "Checked the output."}
        self.tool("report_progress", arguments, player)
        self.team.player.assert_called_once_with("penny")
        self.team.report.assert_called_once_with("task-1", "penny", "done", "Checked the output.")
        self.team.report.reset_mock()
        self.tool("record_player_report", {**arguments, "player_id": "air_jordan"})
        self.team.report.assert_called_once_with("task-1", "air_jordan", "done", "Checked the output.", source="coach_observed")
        tools = self.bridge.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})["result"]["tools"]
        description = next(tool["description"] for tool in tools if tool["name"] == "record_player_report")
        self.assertIn("only after reading", description)
        self.assertIn("human", description.replace("user's", "human"))

    def test_arguments_do_not_allow_identity_overrides_or_arbitrary_commands(self):
        cases = (
            ("team_status", {"command": "sh"}),
            ("read_player_output", {"player_id": "../penny"}),
            ("dispatch_task", {"player_id": "penny", "task": "Task", "request_id": "one", "project": "/other"}),
            ("dispatch_task", {"player_id": "penny", "task": "x" * 16001, "request_id": "one"}),
            ("wait_for_report", {"task_id": "one", "timeout": True}),
            ("wait_for_report", {"task_id": "one", "timeout": 31}),
            ("wait_for_report", {"task_id": "one", "timeout": float("nan")}),
            ("record_player_report", {"player_id": "penny", "task_id": "one", "state": "accepted", "summary": "yes"}),
        )
        for name, arguments in cases:
            self.assertEqual(self.tool(name, arguments)["error"]["code"], -32602)
        player = self.make_bridge("penny")
        self.assertEqual(self.tool("report_progress", {
            "task_id": "one", "state": "done", "summary": "yes", "player_id": "air_jordan",
        }, player)["error"]["code"], -32602)
        self.factory.assert_not_called()

    def test_record_play_uses_only_pinned_project_and_checks_cancellation(self):
        @dataclass
        class Play:
            project: Path
            goal: str
        self.team.play_store.create.return_value = Play(self.project, "Wrapper")
        response = self.tool("record_play", {"goal": "Wrapper", "acceptance": "Tests pass"})
        self.team._check_active.assert_called_once()
        self.team.play_store.create.assert_called_once_with(str(self.project), "Wrapper", "", "Tests pass")
        self.assertEqual(response["result"]["structuredContent"]["project"], str(self.project))

    def test_close_stopped_task_is_coach_only_and_records_exact_reason(self):
        args = {"task_id": "task-1", "reason": "User stopped the verified player."}
        response = self.tool("close_stopped_task", args)
        self.team.close_stopped_task.assert_called_once_with("task-1", args["reason"])
        self.assertEqual(response["result"]["structuredContent"]["progress"], "cancelled")
        self.assertIn("error", self.tool("close_stopped_task", args, self.make_bridge("penny")))

    def test_client_cancellation_prevents_fresh_team_or_any_action(self):
        cancelled = Event()
        bridge = self.make_bridge("coach", cancelled=cancelled.is_set)
        cancelled.set()
        response = self.tool("dispatch_task", {"player_id": "penny", "task": "Task", "request_id": "one"}, bridge)
        self.assertTrue(response["result"]["isError"])
        self.factory.assert_not_called()


class StdioTests(unittest.TestCase):
    def setUp(self):
        self.config = Path("/tmp/dreamteam-test/team.toml").resolve()
        self.project = self.config.parent
        self.runtime = self.enterContext(patch("dreamteam.coach_tools.Herdr"))
        self.runtime.return_value.coordinator_token.return_value = "startup-token"
        self.team = Mock()
        self.team.status.return_value = {"players": []}
        self.factory = self.enterContext(patch("dreamteam.coach_tools.Team", return_value=self.team))
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        self.reader = os.fdopen(input_read, "rb", buffering=0)
        self.input = os.fdopen(input_write, "wb", buffering=0)
        self.output = os.fdopen(output_read, "rb", buffering=0)
        self.writer = os.fdopen(output_write, "wb", buffering=0)
        self.server = Thread(target=serve, args=(self.reader, self.writer, self.config, self.project, "coach"), daemon=True)
        self.server.start()
        self.addCleanup(self.close)

    def close(self):
        self.input.close()
        self.server.join(timeout=2)
        self.assertFalse(self.server.is_alive(), "MCP server remained alive after stdin closed")
        for stream in (self.reader, self.writer, self.output):
            stream.close()

    def send(self, message):
        self.input.write(json.dumps(message).encode() + b"\n")

    def receive(self):
        self.assertTrue(select.select([self.output], [], [], 2)[0], "No MCP response")
        return json.loads(self.output.readline())

    def initialize(self):
        self.send(INIT)
        self.assertEqual(self.receive()["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def test_stdio_handshake_tools_and_eof_reap_server(self):
        self.initialize()
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "team_status"}})
        self.assertEqual(self.receive()["result"]["structuredContent"], {"players": []})
        self.runtime.return_value.coordinator_token.assert_called_once()
        self.assertEqual(self.factory.call_args.kwargs["coordinator_token"], "startup-token")
        self.input.close()
        self.server.join(timeout=1)
        self.assertFalse(self.server.is_alive())

    def test_malformed_records_report_parse_error_then_recover(self):
        self.initialize()
        for line in (b"not json\n", b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n', b"NaN\n"):
            self.input.write(line)
            self.assertEqual(self.receive()["error"]["code"], -32700)
        self.input.write(b"[" * 2000 + b"]" * 2000 + b"\n")
        # Python JSON parsers differ on accepting deep arrays, but neither an
        # invalid RPC array nor parser depth exhaustion may kill the connection.
        self.assertIn(self.receive()["error"]["code"], (-32700, -32600))
        self.input.write(b"x" * (MAX_MESSAGE + 3) + b"\n")
        self.assertEqual(self.receive()["error"]["code"], -32700)
        self.send({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(self.receive()["id"], 9)
        self.factory.assert_not_called()

    def test_eof_cancels_current_cooperative_call_and_discards_queued_mutation(self):
        self.initialize()
        entered = Event()
        stopped = Event()

        def create(*args, **kwargs):
            team = Mock()
            def dispatch(*_arguments):
                entered.set()
                deadline = time.monotonic() + 2
                while not kwargs["cancelled"]() and time.monotonic() < deadline:
                    time.sleep(.01)
                if kwargs["cancelled"]():
                    stopped.set()
                    raise BackendError("Client disconnected before submission.")
                self.fail("Cancellation was not observed.")
            team.dispatch.side_effect = dispatch
            return team
        self.factory.side_effect = create
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "dispatch_task", "arguments": {"player_id": "penny", "task": "Task", "request_id": "one"}}}
        self.send(request)
        self.assertTrue(entered.wait(1))
        self.send({**request, "id": 3})
        self.input.close()
        self.server.join(timeout=1)
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.server.is_alive())
        self.factory.assert_called_once()

    def test_cancelled_queued_request_never_starts_and_next_request_works(self):
        self.initialize()
        entered = Event()
        release = Event()
        self.addCleanup(release.set)
        def first_status():
            entered.set()
            self.assertTrue(release.wait(1))
            return {"serial": 1}
        self.team.status.side_effect = first_status
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "team_status"}})
        self.assertTrue(entered.wait(1))
        self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "dispatch_task", "arguments": {"player_id": "penny", "task": "Task", "request_id": "one"}}})
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}})
        time.sleep(.05)
        release.set()
        self.send({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        self.assertEqual(self.receive()["id"], 2)
        self.assertEqual(self.receive()["id"], 4)
        self.team.dispatch.assert_not_called()
        self.factory.assert_called_once()

    def test_cancellation_during_start_prevents_later_send_and_preserves_connection(self):
        self.initialize()
        entered = Event()
        stopped = Event()
        send_prompt = Mock()

        def create(*args, **kwargs):
            team = Mock()
            team.status.return_value = {"players": []}
            def dispatch(*_arguments):
                entered.set()
                deadline = time.monotonic() + 2
                while not kwargs["cancelled"]() and time.monotonic() < deadline:
                    time.sleep(.01)
                if kwargs["cancelled"]():
                    stopped.set()
                    raise BackendError("Cancelled before prompt submission.")
                send_prompt()
                return {"delivery": "submitted"}
            team.dispatch.side_effect = dispatch
            return team
        self.factory.side_effect = create
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "dispatch_task", "arguments": {"player_id": "penny", "task": "Task", "request_id": "one"}}})
        self.assertTrue(entered.wait(1))
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2, "reason": "User cancelled."}})
        self.assertTrue(stopped.wait(1))
        self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "team_status"}})
        self.assertEqual(self.receive()["id"], 3)
        send_prompt.assert_not_called()
        self.assertEqual(self.factory.call_count, 2)
        self.assertFalse(self.factory.call_args.kwargs["cancelled"]())

    def test_cancelling_initialize_or_unknown_id_has_no_effect(self):
        self.send(INIT)
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
        self.assertEqual(self.receive()["id"], 1)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for value in (999, None, True):
            self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": value}})
        self.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(self.receive()["id"], 3)
        self.factory.assert_not_called()

    def test_tools_execute_serially(self):
        self.initialize()
        entered = Event()
        release = Event()
        def first_status():
            entered.set()
            self.assertTrue(release.wait(1))
            return {"serial": 1}
        self.team.status.side_effect = first_status
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "team_status"}}
        self.send(request)
        self.assertTrue(entered.wait(1))
        self.send({**request, "id": 3})
        time.sleep(.05)
        self.team.status.assert_called_once()
        release.set()
        self.assertEqual(self.receive()["id"], 2)
        self.assertEqual(self.receive()["id"], 3)


if __name__ == "__main__":
    unittest.main()
