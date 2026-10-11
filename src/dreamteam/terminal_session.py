"""Bounded bridge to Herdr's NDJSON terminal controller.

The subprocess is only a terminal client. Closing it releases input ownership;
it never closes the server-owned player pane or starts another agent.
"""

from dataclasses import dataclass
import base64
import binascii
import io
import json
import os
from queue import Empty, Full, Queue
import select
import subprocess
from threading import Event, Lock, Thread, current_thread
import time


class TerminalSessionError(Exception):
    """The terminal controller could not accept an operation."""


@dataclass(frozen=True)
class TerminalFrame:
    data: bytes
    columns: int
    rows: int
    full: bool
    sequence: int


@dataclass(frozen=True)
class TerminalClosed:
    reason: str | None = None


class TerminalSession:
    """One verified terminal's writable client, polled from the UI thread."""

    # Preserve every ANSI delta. A slow UI applies backpressure instead of
    # collecting an unbounded transcript or silently discarding screen updates.
    MAX_RECORD = 2 * 1024 * 1024
    MAX_INPUT = 1024 * 1024
    START_TIMEOUT = 8.0

    def __init__(self, command: list[str], env: dict[str, str]):
        if "--takeover" in command:
            raise ValueError("Courtside does not take over another terminal controller.")
        self.command = list(command)
        self.env = dict(env)
        self._frames: Queue[TerminalFrame] = Queue(maxsize=8)
        self._stop = Event()
        self._ready = Event()
        self._write_lock = Lock()
        self._process: subprocess.Popen | None = None
        self._reader: Thread | None = None
        self._stderr_reader: Thread | None = None
        self._stderr = bytearray()
        self._closed: TerminalClosed | None = None
        self._closed_delivered = False
        self._started = False
        self._started_at = 0.0
        self._received_frame = False

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and not self._stop.is_set() and self._closed is None

    def start(self) -> None:
        if self._started:
            raise TerminalSessionError("This terminal controller has already been started.")
        if self._stop.is_set():
            raise TerminalSessionError("This terminal controller is closed.")
        self._started = True
        self._started_at = time.monotonic()
        try:
            self._process = subprocess.Popen(
                self.command, env=self.env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
            os.set_blocking(self._process.stdin.fileno(), False)
        except OSError as exc:
            self.close()
            raise TerminalSessionError(f"Could not open the player terminal: {exc}") from exc
        self._stderr_reader = Thread(target=self._read_stderr, name="dreamteam-terminal-errors", daemon=True)
        self._reader = Thread(target=self._read_frames, name="dreamteam-terminal-frames", daemon=True)
        self._stderr_reader.start()
        self._reader.start()

    def poll(self) -> list[TerminalFrame | TerminalClosed]:
        """Drain a bounded batch; a final close record follows all queued frames."""
        if (self._started and not self._received_frame and self._closed is None
                and not self._stop.is_set()
                and time.monotonic() - self._started_at > self.START_TIMEOUT):
            self.close()
            self._closed = TerminalClosed("The terminal did not connect in time. Try again after checking its existing view.")
        records: list[TerminalFrame | TerminalClosed] = []
        for _ in range(8):
            try:
                records.append(self._frames.get_nowait())
            except Empty:
                break
        if self._closed is not None and self._frames.empty() and not self._closed_delivered:
            self._closed_delivered = True
            records.append(self._closed)
        return records

    def send(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("Terminal input must be bytes.")
        if not data:
            return
        if len(data) > self.MAX_INPUT:
            raise TerminalSessionError("Paste is too large; send at most 1 MiB at a time.")
        self._require_ready()
        self._write({"type": "terminal.input", "bytes": base64.b64encode(data).decode("ascii")})

    def resize(self, columns: int, rows: int) -> None:
        if any(type(value) is not int or not 1 <= value <= 65535 for value in (columns, rows)):
            raise ValueError("Terminal dimensions must be between 1 and 65535.")
        self._require_ready()
        self._write({"type": "terminal.resize", "cols": columns, "rows": rows})

    def _require_ready(self) -> None:
        if not self.ready:
            raise TerminalSessionError("The player terminal is not connected.")

    def _write(self, record: dict) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise TerminalSessionError("The player terminal is not connected.")
        payload = json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
        deadline = time.monotonic() + 0.2
        if not self._write_lock.acquire(timeout=0.2):
            raise TerminalSessionError("The terminal input channel is busy.")
        try:
            view = memoryview(payload)
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TerminalSessionError("Terminal input stalled; reconnect before sending more.")
                writable = select.select([], [process.stdin], [], remaining)[1]
                if not writable:
                    raise TerminalSessionError("Terminal input stalled; reconnect before sending more.")
                try:
                    count = os.write(process.stdin.fileno(), view)
                except BlockingIOError:
                    continue
                view = view[count:]
        except TerminalSessionError:
            self._stop.set()
            self._ready.clear()
            raise
        except (OSError, ValueError) as exc:
            self._stop.set()
            self._ready.clear()
            raise TerminalSessionError(f"The terminal input channel closed: {exc}") from exc
        finally:
            self._write_lock.release()

    def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while chunk := process.stderr.read(4096):
                self._stderr.extend(chunk)
                del self._stderr[:-8192]
        except (OSError, ValueError):
            pass

    @staticmethod
    def _frame(record: dict) -> TerminalFrame:
        if record.get("encoding") != "ansi":
            raise ValueError("unsupported terminal frame encoding")
        for key in ("width", "height"):
            if type(record.get(key)) is not int or not 1 <= record[key] <= 65535:
                raise ValueError("invalid terminal frame dimensions")
        if type(record.get("seq")) is not int or record["seq"] < 0:
            raise ValueError("invalid terminal frame sequence")
        if type(record.get("full")) is not bool or not isinstance(record.get("bytes"), str):
            raise ValueError("invalid terminal frame payload")
        data = base64.b64decode(record["bytes"], validate=True)
        return TerminalFrame(data, record["width"], record["height"], record["full"], record["seq"])

    def _read_frames(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        reason = None
        reader = io.BufferedReader(process.stdout)
        try:
            # BufferedReader.readline supplies a strict record-size bound even
            # though the Popen pipes themselves are unbuffered.
            while not self._stop.is_set():
                line = reader.readline(self.MAX_RECORD + 1)
                if not line:
                    break
                if len(line) > self.MAX_RECORD or not line.endswith(b"\n"):
                    raise ValueError("oversized or incomplete terminal record")
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("invalid terminal record")
                if record.get("type") == "terminal.closed":
                    value = record.get("reason")
                    reason = value if isinstance(value, str) else None
                    break
                if record.get("type") != "terminal.frame":
                    raise ValueError("unexpected terminal record")
                frame = self._frame(record)
                while not self._stop.is_set():
                    try:
                        self._frames.put(frame, timeout=0.05)
                        self._received_frame = True
                        self._ready.set()
                        break
                    except Full:
                        continue
        except (OSError, ValueError, binascii.Error) as exc:
            if not self._stop.is_set():
                reason = f"Terminal stream failed: {exc}"
        finally:
            reader.close()
            self._ready.clear()
            # EOF and protocol failures reap the bridge too, even when the UI
            # has not yet polled the final message.
            self._reap()
            if self._stderr_reader is not None:
                self._stderr_reader.join(timeout=0.1)
            error = bytes(self._stderr).decode("utf-8", errors="replace").strip()
            if not reason and process.returncode and not self._stop.is_set():
                reason = error or f"Terminal controller exited with status {process.returncode}."
            if reason and "already has an attached client" in reason:
                reason = "This player is controlled in another terminal. Detach that view first, then reconnect here."
            self._closed = TerminalClosed(reason)

    def _reap(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            process.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.4)
            except ProcessLookupError:
                process.wait(timeout=0.4)

    def close(self) -> None:
        """Release and reap this client only; leave the owned player running."""
        if not self._stop.is_set():
            self._stop.set()
            self._ready.clear()
            try:
                self._write({"type": "terminal.release"})
            except TerminalSessionError:
                pass
        process = self._process
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            self._reap()
            for thread in (self._reader, self._stderr_reader):
                if thread is not None and thread is not current_thread():
                    thread.join(timeout=0.1)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        if self._closed is None:
            self._closed = TerminalClosed()
