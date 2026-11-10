"""Shared machinery for agent runners.

A runner keeps one provider conversation alive for the relay. It reads the
relay's keystrokes from standard input, runs one provider CLI turn for each
submitted message, and writes a session log the listener forwards to Telegram.
A provider adapter subclasses `AgentRunner` and supplies only `run_message`.

Input protocol, identical under tmux and under the direct host:

  bracketed paste   message text, newlines included
  Enter             submit the pasted or typed text
  Escape            interrupt the running turn
  Ctrl-U            discard text that was not submitted
  Ctrl-D / EOF      end the runner
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    import termios
except ImportError:  # Windows: input arrives on a pipe, never a Unix terminal.
    termios = None

from . import host, portable
from .harnesses import Harness

PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
ESCAPE_SEQUENCE_WAIT_SECONDS = 0.05


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def set_process_name(name: str) -> None:
    """Name this process so a tmux host recognises the harness. Linux only."""
    try:
        with open("/proc/self/comm", "w", encoding="ascii") as handle:
            handle.write(name)  # No newline: the kernel would keep it in the name.
    except OSError:
        pass


class SessionLog:
    """The session records the listener reads: turns, user input and agent messages."""

    def __init__(self, home: Path, repo: Path, *, harness: str) -> None:
        # One level below the harness directory: some CLIs scan their session
        # root and reject unrelated flat .jsonl files there.
        project_dir = home / "sessions" / harness
        sessions_dir = project_dir / "telegram"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        if harness == "dsh":
            # Earlier releases wrote DeepSeek Harness logs flat, where the
            # harness rejects them; move those out of its way.
            for legacy in project_dir.glob("rollout-dsh-*.jsonl"):
                legacy.replace(sessions_dir / legacy.name)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        self.path = sessions_dir / f"rollout-{harness}-{stamp}-{uuid.uuid4()}.jsonl"
        self._handle = self.path.open("a", encoding="utf-8")
        # Hosts without /proc learn the open session log from the runner itself.
        host.announce_session(self.path)
        self.write({"type": "session_meta", "timestamp": utc_now_iso(), "payload": {
            "id": str(uuid.uuid4()), "cwd": str(repo), "source": f"{harness}-cli",
            "originator": f"tele-agent/{harness}"}})

    def write(self, record: dict) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def _event(self, payload: dict) -> None:
        self.write({"type": "event_msg", "timestamp": utc_now_iso(), "payload": payload})

    def agent_message(self, text: str, *, phase: str = "final_answer") -> None:
        self._event({"type": "agent_message", "id": str(uuid.uuid4()),
                     "message": text, "phase": phase})

    def turn_context(self, model: str, effort: str) -> None:
        self.write({"type": "turn_context", "timestamp": utc_now_iso(),
                    "payload": {"model": model, "effort": effort}})

    def turn_started(self, message: str) -> None:
        self._event({"type": "task_started", "turn_id": str(uuid.uuid4())})
        self.write({"type": "response_item", "timestamp": utc_now_iso(), "payload": {
            "type": "message", "role": "user", "id": str(uuid.uuid4()),
            "content": [{"type": "input_text", "text": message}]}})

    def turn_finished(self, *, aborted: bool = False) -> None:
        self._event({"type": "turn_aborted" if aborted else "task_complete"})


class StdinBytes:
    """Standard input one byte at a time, with a timed read that works on any OS.

    `select` cannot wait on a pipe on Windows, so a thread does the blocking
    reads and the caller waits on a queue.
    """

    def __init__(self) -> None:
        self.bytes: queue.Queue = queue.Queue()
        if os.name == "nt":
            import msvcrt

            # Text mode would rewrite line endings and end input at Ctrl-Z.
            msvcrt.setmode(0, os.O_BINARY)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                char = os.read(0, 1)
            except OSError:
                char = b""
            self.bytes.put(char)
            if not char:
                return

    def read(self, timeout: float | None = None) -> bytes | None:
        """Return one byte, b"" at end of input, or None when the wait timed out."""
        try:
            return self.bytes.get(timeout=timeout)
        except queue.Empty:
            return None


def read_keys(events: queue.Queue, interrupt: Callable[[], None]) -> None:
    """Turn the input protocol into ("message", text), ("interrupt", "") and ("eof", "")."""
    pending = bytearray()
    pasting = False
    source = StdinBytes()
    while True:
        char = source.read()
        if not char or (char == b"\x04" and not pasting):
            events.put(("eof", ""))
            return
        if pasting:
            pending.extend(char)
            if pending.endswith(PASTE_END):
                del pending[-len(PASTE_END):]
                pasting = False
            continue
        if char == b"\x1b":
            sequence = bytearray(char)
            while PASTE_START.startswith(sequence) and len(sequence) < len(PASTE_START):
                following = source.read(timeout=ESCAPE_SEQUENCE_WAIT_SECONDS)
                if not following:
                    break
                sequence.extend(following)
            if sequence == PASTE_START:
                pasting = True
                continue
            pending.clear()
            interrupt()
            events.put(("interrupt", ""))
        elif char == b"\x15":  # The relay clears the composer with Ctrl-U.
            pending.clear()
        elif char in (b"\n", b"\r"):
            if pending:
                events.put(("message", pending.decode("utf-8", errors="replace")))
            pending.clear()
        else:
            pending.extend(char)


class AgentRunner:
    """One provider conversation. Subclasses implement `run_message`."""

    def __init__(self, harness: Harness, *, repo: Path, state_dir: Path, home: Path) -> None:
        self.harness = harness
        self.repo = repo
        self.state_dir = state_dir
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log = SessionLog(home, repo, harness=harness.key)
        self.process: subprocess.Popen | None = None
        self.interrupted = False
        self.lock = threading.RLock()

    def ready_line(self) -> str:
        return f"{self.harness.label} ready"

    def run_message(self, message: str) -> None:
        raise NotImplementedError

    def interrupt(self) -> None:
        """Stop the running CLI turn; the runner and its conversation stay."""
        with self.lock:
            process = self.process
            if process is None or process.poll() is not None:
                return
            self.interrupted = True
            portable.stop_process(process)

            def force_stop() -> None:
                if process.poll() is None:
                    process.kill()

            timer = threading.Timer(5, force_stop)
            timer.daemon = True
            timer.start()

    def stream(
        self,
        command: list[str],
        *,
        prompt: str | None,
        on_event: Callable[[dict], None],
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run one CLI turn and return its exit code and standard error.

        The prompt travels on standard input, never in the argument list, unless
        the adapter passes `prompt=None` because its CLI only takes arguments.
        Every JSON object the CLI prints is handed to `on_event`.
        """
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="") as prompt_file, \
             tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errors:
            if prompt is not None:
                prompt_file.write(prompt)
                prompt_file.seek(0)
            with self.lock:
                self.process = subprocess.Popen(
                    command, stdin=prompt_file if prompt is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8",
                    errors="replace", cwd=self.repo, env=env)
            process = self.process
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        on_event(event)
                returncode = process.wait()
            finally:
                with self.lock:
                    portable.stop_process(process)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    process.stdout.close()
                    self.process = None
            errors.seek(0)
            return returncode, errors.read()

    def serve(self) -> int:
        """Read the relay's input until end of input, running one turn per message."""
        set_process_name(self.harness.process_comm)
        events: queue.Queue = queue.Queue()
        previous_terminal = None
        if termios is not None and os.isatty(0):
            # Under tmux: read keys unbuffered and ask for bracketed pastes.
            previous_terminal = termios.tcgetattr(0)
            mode = termios.tcgetattr(0)
            mode[3] &= ~(termios.ICANON | termios.ECHO)
            mode[6][termios.VMIN] = 1
            mode[6][termios.VTIME] = 0
            termios.tcsetattr(0, termios.TCSANOW, mode)
            print("\x1b[?2004h", end="", flush=True)
        threading.Thread(target=read_keys, args=(events, self.interrupt), daemon=True).start()
        signal.signal(signal.SIGTERM, lambda *_: (self.interrupt(), events.put(("eof", ""))))
        print(self.ready_line(), flush=True)
        try:
            while True:
                kind, value = events.get()
                if kind == "eof":
                    return 0
                if kind == "message":
                    self.run_message(value)
        finally:
            self.log.close()
            if previous_terminal is not None:
                print("\x1b[?2004l", end="", flush=True)
                termios.tcsetattr(0, termios.TCSANOW, previous_terminal)
