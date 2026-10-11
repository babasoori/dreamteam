"""Protocol and client lifecycle checks; no user Herdr session is touched."""

import base64
import json
import os
import sys
import time
import unittest

from dreamteam.terminal_session import (
    TerminalClosed, TerminalFrame, TerminalSession, TerminalSessionError,
)


FRAME_HELPER = """
import base64, json, sys
def frame(data=b"ready", sequence=1):
    print(json.dumps({"type": "terminal.frame", "seq": sequence, "encoding": "ansi",
                      "width": 80, "height": 24, "full": sequence == 1,
                      "bytes": base64.b64encode(data).decode()}), flush=True)
"""


class TerminalSessionTests(unittest.TestCase):
    def launch(self, source):
        session = TerminalSession([sys.executable, "-u", "-c", FRAME_HELPER + source], dict(os.environ))
        self.addCleanup(session.close)
        session.start()
        return session

    def wait_records(self, session, predicate, timeout=3):
        records = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records.extend(session.poll())
            if predicate(records):
                return records
            time.sleep(0.01)
        self.fail(f"Terminal events did not arrive: {records}")

    def wait_ready(self, session):
        return self.wait_records(session, lambda records: any(isinstance(x, TerminalFrame) for x in records))

    def test_decodes_ansi_and_preserves_geometry(self):
        session = self.launch("frame(b'\\x1b[2Jhello\\r\\n')\nfor line in sys.stdin:\n    if json.loads(line)['type'] == 'terminal.release': break\n")
        records = self.wait_ready(session)
        self.assertEqual(records[0], TerminalFrame(b"\x1b[2Jhello\r\n", 80, 24, True, 1))
        self.assertTrue(session.ready)
        session.close()
        self.assertFalse(session.ready)
        self.assertIsNotNone(session._process.returncode)

    def test_sends_binary_input_and_structured_resize(self):
        session = self.launch("""
frame()
for line in sys.stdin:
    record = json.loads(line)
    if record["type"] == "terminal.release":
        break
    frame(json.dumps(record).encode(), 2)
""")
        self.wait_ready(session)
        data = "aé".encode() + b"\x00\x1b[A\r"
        session.send(data)
        session.resize(112, 33)
        records = self.wait_records(session, lambda events: len(events) == 2)
        payloads = [json.loads(event.data) for event in records]
        self.assertEqual(base64.b64decode(payloads[0]["bytes"]), data)
        self.assertEqual(payloads[0]["type"], "terminal.input")
        self.assertEqual(payloads[1], {"type": "terminal.resize", "cols": 112, "rows": 33})

    def test_cannot_type_before_connection_or_after_close(self):
        session = TerminalSession([sys.executable, "-c", "pass"], dict(os.environ))
        with self.assertRaisesRegex(TerminalSessionError, "not connected"):
            session.send(b"a")
        session.close()
        with self.assertRaisesRegex(TerminalSessionError, "closed"):
            session.start()
        with self.assertRaisesRegex(TerminalSessionError, "not connected"):
            session.send(b"b")

    def test_declines_takeover_and_duplicate_start(self):
        with self.assertRaisesRegex(ValueError, "take over"):
            TerminalSession(["herdr", "terminal", "session", "control", "term_1", "--takeover"], {})
        session = self.launch("frame()\nfor line in sys.stdin: break")
        with self.assertRaisesRegex(TerminalSessionError, "already"):
            session.start()

    def test_closed_reason_delivered_once_and_child_reaped(self):
        session = self.launch("print(json.dumps({'type': 'terminal.closed', 'reason': 'Terminal is already attached.'}), flush=True)")
        records = self.wait_records(session, lambda events: any(isinstance(x, TerminalClosed) for x in events))
        self.assertEqual(records, [TerminalClosed("Terminal is already attached.")])
        self.assertEqual(session.poll(), [])
        self.assertIsNotNone(session._process.returncode)
        self.assertFalse(session.ready)

    def test_stderr_failure_is_visible(self):
        session = self.launch("print('terminal already attached elsewhere', file=sys.stderr, flush=True)\nsys.exit(1)")
        records = self.wait_records(session, lambda events: bool(events))
        self.assertEqual(records, [TerminalClosed("terminal already attached elsewhere")])
        self.assertEqual(session._process.returncode, 1)

    def test_malformed_or_oversized_record_closes_bridge(self):
        for output in ("not json", json.dumps({"type": "terminal.frame", "encoding": "jpeg"}), "x" * 300):
            with self.subTest(output=output[:20]):
                session = TerminalSession([sys.executable, "-u", "-c", f"import time; print({output!r}, flush=True); time.sleep(20)"], dict(os.environ))
                session.MAX_RECORD = 256
                self.addCleanup(session.close)
                session.start()
                records = self.wait_records(session, lambda events: bool(events))
                self.assertIsInstance(records[0], TerminalClosed)
                self.assertIn("Terminal stream failed", records[0].reason)
                self.assertFalse(session.ready)
                self.assertIsNotNone(session._process.returncode)

    def test_bounded_queue_preserves_every_delta(self):
        session = self.launch("for sequence in range(1, 101): frame(str(sequence).encode(), sequence)")
        # The producer must stop at the bounded queue; polling resumes it.
        time.sleep(0.1)
        self.assertLessEqual(session._frames.qsize(), 8)
        records = self.wait_records(session, lambda events: any(isinstance(x, TerminalClosed) for x in events))
        frames = [event for event in records if isinstance(event, TerminalFrame)]
        self.assertEqual([frame.sequence for frame in frames], list(range(1, 101)))
        self.assertIsInstance(records[-1], TerminalClosed)

    def test_close_releases_before_forcing_exit(self):
        session = self.launch("""
frame()
for line in sys.stdin:
    if json.loads(line)["type"] == "terminal.release":
        sys.exit(0)
sys.exit(7)
""")
        self.wait_ready(session)
        session.close()
        session.close()
        self.assertEqual(session._process.returncode, 0)
        self.assertFalse(session._reader.is_alive())
        self.assertFalse(session._stderr_reader.is_alive())

    def test_stuck_bridge_is_killed_and_reaped_without_hanging(self):
        session = self.launch("""
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
frame()
time.sleep(30)
""")
        self.wait_ready(session)
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 1.8)
        self.assertIsNotNone(session._process.returncode)
        self.assertFalse(session._reader.is_alive())

    def test_blocked_input_is_bounded(self):
        session = self.launch("""
import time
frame()
time.sleep(30)
""")
        self.wait_ready(session)
        started = time.monotonic()
        with self.assertRaisesRegex(TerminalSessionError, "stalled"):
            session.send(b"x" * (1024 * 512))
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertFalse(session.ready)

    def test_silent_start_times_out_and_reaps_client(self):
        session = self.launch("import time; time.sleep(30)")
        session.START_TIMEOUT = 0.03
        records = self.wait_records(session, lambda events: bool(events))
        self.assertIsInstance(records[0], TerminalClosed)
        self.assertIn("did not connect in time", records[0].reason)
        self.assertIsNotNone(session._process.returncode)

    def test_ownership_conflict_explains_detach_without_takeover(self):
        session = self.launch("print(json.dumps({'type': 'terminal.closed', 'reason': 'terminal x already has an attached client; retry with --takeover'}), flush=True)")
        records = self.wait_records(session, lambda events: bool(events))
        self.assertIn("Detach that view first", records[0].reason)
        self.assertNotIn("takeover", records[0].reason)

    def test_validates_dimensions_and_input_limit(self):
        session = self.launch("frame()\nfor line in sys.stdin: break")
        self.wait_ready(session)
        for dimensions in ((0, 2), (2, -1), (65536, 2), (True, 2)):
            with self.assertRaises(ValueError):
                session.resize(*dimensions)
        with self.assertRaisesRegex(TerminalSessionError, "too large"):
            session.send(b"x" * (session.MAX_INPUT + 1))
        with self.assertRaises(TypeError):
            session.send("not bytes")


if __name__ == "__main__":
    unittest.main()
