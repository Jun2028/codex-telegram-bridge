#!/usr/bin/env python3
"""Native Claude Code turns in the existing Telegram relay and supervisor."""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

try:
    import termios
except ImportError:  # Windows: input arrives on a pipe, never a Unix terminal.
    termios = None

from dsh_agent_runner import SessionLog, set_process_comm
from notify import redact
from teleagent import claude


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


def read_stdin(events: queue.Queue, interrupt) -> None:
    """Keep bracketed pastes intact; only the following Enter submits them."""
    pending = bytearray()
    pasting = False
    paste_start, paste_end = b"\x1b[200~", b"\x1b[201~"
    source = StdinBytes()
    while True:
        char = source.read()
        if not char or char == b"\x04":
            events.put(("eof", ""))
            return
        if pasting:
            pending.extend(char)
            if pending.endswith(paste_end):
                del pending[-len(paste_end):]
                pasting = False
            continue
        if char == b"\x1b":
            sequence = bytearray(char)
            while paste_start.startswith(sequence) and len(sequence) < len(paste_start):
                following = source.read(timeout=.05)
                if not following:
                    break
                sequence.extend(following)
            if sequence == paste_start:
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


class ClaudeAgentRunner:
    def __init__(self, *, model: str, effort: str, repo: Path, state_dir: Path,
                 codex_home: Path, binary: str) -> None:
        if not claude.is_claude_model(model) or effort not in claude.EFFORTS:
            raise ValueError("invalid Claude Code model or effort")
        self.model, self.effort, self.repo = model, effort, repo
        self.state_dir, self.binary = state_dir, binary
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session_path = state_dir / "claude_session.json"
        self.session_id = ""
        if self.session_path.exists():
            state = json.loads(self.session_path.read_text())
            # A malformed/mismatched state must not silently start another chat.
            if state.get("model") != model or state.get("repo") != str(repo):
                raise ValueError("Claude Code session selection changed; start a fresh agent")
            self.session_id = state["session_id"]
            if not isinstance(self.session_id, str) or not self.session_id:
                raise ValueError("invalid Claude Code session ID")
        self.instructions = state_dir / "claude-instructions.md"
        files = [repo / "AGENTS.md", repo / ".agents/skills/telegram-chat/SKILL.md"]
        personality = os.environ.get("TELEAGENT_PERSONALITY_FILE")
        files.append(Path(personality) if personality else
                     next((repo / "config" / name for name in ["personality.md", "personality.default.md"]
                           if (repo / "config" / name).is_file()), repo / "config/personality.default.md"))
        self.instructions.write_text(
            "\n\n".join(p.read_text(encoding="utf-8") for p in files if p.is_file()),
            encoding="utf-8", newline="")
        self.instructions.chmod(0o600)
        self.log = SessionLog(codex_home, repo, harness="claude")
        self.log.turn_context(claude.native_model(model), effort)
        self.process: subprocess.Popen | None = None
        self.interrupted = False
        self.effort_warned = False
        self.lock = threading.RLock()

    def save_session(self, session_id: str) -> None:
        if self.session_id and session_id != self.session_id:
            raise RuntimeError("Claude Code returned a different session; refusing to switch")
        self.session_id = session_id
        temporary = self.session_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"session_id": session_id, "model": self.model,
                                         "repo": str(self.repo)}))
        temporary.chmod(0o600)
        temporary.replace(self.session_path)

    def interrupt(self) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.interrupted = True
                self.process.terminate()
                process = self.process
                def force_stop() -> None:
                    if process.poll() is None:
                        process.kill()
                timer = threading.Timer(5, force_stop)
                timer.daemon = True
                timer.start()

    def run_message(self, message: str) -> None:
        self.log.turn_context(claude.native_model(self.model), self.effort)
        self.log.turn_started(message)
        self.interrupted = False
        command = claude.build_turn_command(self.binary, self.model, self.effort,
                                            self.instructions, self.session_id)
        result = None
        try:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="") as prompt, \
                 tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errors:
                prompt.write(message)
                prompt.seek(0)
                with self.lock:
                    self.process = subprocess.Popen(command, stdin=prompt, stdout=subprocess.PIPE,
                                                    stderr=errors, text=True, encoding="utf-8",
                                                    errors="replace", cwd=self.repo)
                try:
                    assert self.process.stdout is not None
                    for line in self.process.stdout:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(event, dict) or event.get("parent_tool_use_id"):
                            continue  # Child-agent text must not become the root answer.
                        if event.get("type") == "system" and event.get("subtype") == "init":
                            self.save_session(event["session_id"])
                            if event.get("model"):
                                self.log.turn_context(str(event["model"]), self.effort)
                        elif event.get("type") == "result":
                            if event.get("session_id"):
                                self.save_session(event["session_id"])
                            result = event
                    returncode = self.process.wait()
                finally:
                    with self.lock:
                        if self.process.poll() is None:
                            self.process.terminate()
                        try:
                            self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            self.process.wait()
                        self.process.stdout.close()
                        self.process = None
                errors.seek(0)
                if claude.UNKNOWN_EFFORT_WARNING in errors.read() and not self.effort_warned:
                    # The CLI ignores an effort it does not know and still exits 0.
                    self.effort_warned = True
                    self.log.agent_message(
                        f"This Claude Code version does not support effort '{self.effort}' and "
                        "is using its default effort. Start a fresh /claude with a supported level.",
                        phase="commentary")
                if self.interrupted:
                    self.log.agent_message("Claude Code turn interrupted.", phase="commentary")
                elif returncode == 0 and result and not result.get("is_error") and result.get("subtype") == "success":
                    self.log.agent_message(str(result.get("result") or ""), phase="final_answer")
                else:
                    # Do not forward provider stderr, tool input, or credential-bearing URLs.
                    subtype = str((result or {}).get("subtype") or "missing result")
                    known = {"error_max_turns", "error_max_budget_usd", "error_during_execution",
                             "error_max_structured_output_retries"}
                    detail = subtype if subtype in known else f"exit {returncode}"
                    self.log.agent_message(f"Claude Code failed ({detail}); inspect local diagnostics.", phase="commentary")
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            # Keep the pane alive after a failed turn, without changing conversations.
            safe = redact(str(exc), dict(os.environ))
            print(f"Claude Code turn failed: {safe[:250]}", flush=True)
            self.log.agent_message("Claude Code turn failed; inspect local diagnostics.", phase="commentary")
        finally:
            self.log.turn_finished(aborted=self.interrupted)

    def serve(self) -> int:
        events: queue.Queue = queue.Queue()
        previous_terminal = None
        if termios is not None and os.isatty(0):
            previous_terminal = termios.tcgetattr(0)
            mode = termios.tcgetattr(0)
            mode[3] &= ~(termios.ICANON | termios.ECHO)
            mode[6][termios.VMIN] = 1
            mode[6][termios.VTIME] = 0
            termios.tcsetattr(0, termios.TCSANOW, mode)
            print("\x1b[?2004h", end="", flush=True)
        threading.Thread(target=read_stdin, args=(events, self.interrupt), daemon=True).start()
        signal.signal(signal.SIGTERM, lambda *_: (self.interrupt(), events.put(("eof", ""))))
        print(f"Claude Code ready · model={claude.native_model(self.model)} · effort={self.effort}", flush=True)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    args = parser.parse_args()
    set_process_comm("codex-claude")
    try:
        binary = claude.ensure_available()
        return ClaudeAgentRunner(**vars(args), binary=binary).serve()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Claude Code unavailable: {type(exc).__name__}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
