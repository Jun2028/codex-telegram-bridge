#!/usr/bin/env python3
"""Run the agy (Antigravity) harness inside a tele-agent tmux pane.

The runner keeps one long-lived agy conversation per bot.  Telegram messages
arrive on stdin (the relay types them into the pane); each assembled message
runs one headless ``agy`` turn whose NDJSON stream is printed to the pane and
translated into tele-agent session records that the listener forwards to
Telegram.

The process sets its comm to ``codex-agy`` so the relay's pane checks and
session discovery recognise it as an agent runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from teleagent import agy as agy_runtime  # noqa: E402


PROCESS_COMM = "codex-agy"
SUBMIT_IDLE_SECONDS = 1.0


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def set_process_comm(name: str = PROCESS_COMM) -> None:
    """Best-effort comm rename so pane checks accept this runtime."""
    try:
        with open("/proc/self/comm", "w", encoding="ascii") as handle:
            # No trailing newline: the kernel keeps it in the comm field and
            # `ps` would render it as "codex-agy?".
            handle.write(name)
    except OSError:
        pass


class SessionLog:
    """Append tele-agent session records for one agy agent."""

    def __init__(self, codex_home: Path, repo: Path) -> None:
        sessions_dir = codex_home / "sessions" / "agy"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        self.path = sessions_dir / f"rollout-agy-{stamp}-{uuid.uuid4()}.jsonl"
        self._handle = self.path.open("a", encoding="utf-8")
        self.write(
            {
                "type": "session_meta",
                "timestamp": utc_now_iso(),
                "payload": {
                    "id": str(uuid.uuid4()),
                    "cwd": str(repo),
                    "source": "agy-cli",
                    "originator": "tele-agent/agy",
                },
            }
        )

    def write(self, record: dict) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def agent_message(self, text: str, *, phase: str = "final_answer") -> None:
        self.write(
            {
                "type": "event_msg",
                "timestamp": utc_now_iso(),
                "payload": {
                    "type": "agent_message",
                    "id": str(uuid.uuid4()),
                    "message": text,
                    "phase": phase,
                },
            }
        )

    def turn_context(self, model: str, effort: str) -> None:
        self.write(
            {
                "type": "turn_context",
                "timestamp": utc_now_iso(),
                "payload": {"model": model, "effort": effort},
            }
        )


class MessageAssembler:
    """Assemble relay keystrokes into submitted messages.

    The relay types the message text (which may contain newlines) and then
    presses Enter after a pause.  A turn is submitted once input stays idle:
    the burst may contain newlines as message content, so only silence marks
    the end of a message.
    """

    def __init__(self, idle_seconds: float = SUBMIT_IDLE_SECONDS) -> None:
        self.idle_seconds = idle_seconds
        self.pending: list[str] = []

    def feed(self, line: str) -> None:
        """Feed one input line."""
        self.pending.append(line)

    def take(self) -> str | None:
        """Return the accumulated message once input has gone idle."""
        if not self.pending:
            return None
        message = "\n".join(self.pending).strip()
        self.pending = []
        return message or None


class StdinReader(threading.Thread):
    """Read pane input, splitting interrupts from message text."""

    def __init__(self, events: "queue.Queue[tuple[str, str]]") -> None:
        super().__init__(daemon=True)
        self.events = events

    def run(self) -> None:
        while True:
            try:
                raw = sys.stdin.readline()
            except (OSError, ValueError):
                break
            if raw == "":
                self.events.put(("eof", ""))
                break
            line = raw.rstrip("\n")
            if "\x1b" in line:
                remainder = line.replace("\x1b", "").strip()
                self.events.put(("interrupt", ""))
                if remainder:
                    self.events.put(("line", remainder))
                continue
            self.events.put(("line", line))


class AgyAgentRunner:
    def __init__(
        self,
        *,
        model: str,
        effort: str,
        repo: Path,
        state_dir: Path,
        codex_home: Path,
        agy_bin: str,
    ) -> None:
        self.requested_model = model
        self.model = agy_runtime.resolve_model(model, agy_bin)
        self.effort = effort
        self.repo = repo
        self.state_dir = state_dir
        self.codex_home = codex_home
        self.agy_bin = agy_bin
        self.log = SessionLog(codex_home, repo)
        self.conversation_path = state_dir / "agy_conversation.json"
        self.conversation_id = self._load_conversation()
        self._current_process: subprocess.Popen | None = None

    def _load_conversation(self) -> str:
        try:
            data = json.loads(self.conversation_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        value = data.get("conversation_id")
        return value if isinstance(value, str) else ""

    def _save_conversation(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.conversation_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"conversation_id": self.conversation_id}), encoding="utf-8"
        )
        tmp.replace(self.conversation_path)

    def banner(self) -> None:
        print(
            f"agy harness ready · model={self.model}"
            + (f" (from {self.requested_model})" if self.model != self.requested_model else "")
            + (f" · effort={self.effort}" if self.effort else " · effort=default"),
            flush=True,
        )
        print("Type a message.  ESC interrupts a running turn.", flush=True)

    def interrupt(self) -> None:
        process = self._current_process
        if process is not None and process.poll() is None:
            process.terminate()
            print("\n[interrupt requested]", flush=True)

    def run_message(self, message: str) -> None:
        print(f"\n▶ {message.splitlines()[0][:160]}", flush=True)
        self.log.turn_context(self.model, self.effort)
        command = agy_runtime.build_turn_command(
            prompt=message,
            model=self.model,
            agy_bin=self.agy_bin,
            effort=self.effort or None,
            conversation_id=self.conversation_id or None,
        )
        result = agy_runtime.AgyTurnResult()
        deltas: list[str] = []
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
            )
            self._current_process = process
            assert process.stdout is not None
            try:
                for event in agy_runtime.iter_stream_events(process.stdout):
                    result.events.append(event)
                    agy_runtime.apply_stream_event(result, event, deltas)
                    if event.get("event") == "step_update":
                        step = event.get("step_update")
                        if (
                            isinstance(step, dict)
                            and step.get("step_type") == "agent_response"
                            and step.get("text_delta")
                        ):
                            print(str(step["text_delta"]), end="", flush=True)
            finally:
                returncode = process.wait()
                self._current_process = None
            interrupted = returncode not in (0,) and not result.status
            if interrupted:
                print("\n[turn interrupted]", flush=True)
                self.log.agent_message("[agy turn interrupted]", phase="commentary")
                return
            if not result.text and deltas:
                result.text = "".join(deltas).strip()
            if not result.status:
                result.status = "SUCCESS" if returncode == 0 else "ERROR"
            print("", flush=True)
            if result.conversation_id and result.conversation_id != self.conversation_id:
                self.conversation_id = result.conversation_id
                self._save_conversation()
            if result.status == "SUCCESS" and result.text:
                self.log.agent_message(result.text, phase="final_answer")
                return
            stderr_file.seek(0)
            detail = stderr_file.read().strip().splitlines()
            error = result.error or (detail[-1] if detail else f"agy exited rc={returncode}")
            self.log.agent_message(f"agy error: {error}", phase="commentary")

    def serve(self) -> int:
        events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        StdinReader(events).start()
        assembler = MessageAssembler()
        self.banner()
        while True:
            try:
                kind, payload = events.get(timeout=assembler.idle_seconds)
            except queue.Empty:
                message = assembler.take()
                if message:
                    self.run_message(message)
                continue
            if kind == "eof":
                return 0
            if kind == "interrupt":
                self.interrupt()
                # Drop any half-typed text; the relay follows an interrupt
                # with the replacement prompt.
                assembler.take()
                continue
            if kind != "line":
                continue
            assembler.feed(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument(
        "--agy-bin", default=os.environ.get("TELEAGENT_AGY_BIN", "agy")
    )
    args = parser.parse_args(argv)

    set_process_comm()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        runner = AgyAgentRunner(
            model=args.model,
            effort=args.effort,
            repo=args.repo,
            state_dir=args.state_dir,
            codex_home=args.codex_home,
            agy_bin=args.agy_bin,
        )
    except agy_runtime.AgyError as exc:
        print(f"agy unavailable: {exc}", file=sys.stderr, flush=True)
        return 2
    return runner.serve()


if __name__ == "__main__":
    raise SystemExit(main())
