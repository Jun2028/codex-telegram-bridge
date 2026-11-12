#!/usr/bin/env python3
"""Native Claude Code turns in the existing Telegram relay and supervisor."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notify import redact  # noqa: E402
from teleagent import claude, harnesses  # noqa: E402
from teleagent.runner import AgentRunner  # noqa: E402

KNOWN_FAILURES = {"error_max_turns", "error_max_budget_usd", "error_during_execution",
                  "error_max_structured_output_retries"}


class ClaudeAgentRunner(AgentRunner):
    def __init__(self, *, model: str, effort: str, repo: Path, state_dir: Path,
                 codex_home: Path, binary: str) -> None:
        if not claude.is_claude_model(model) or effort not in claude.EFFORTS:
            raise ValueError("invalid Claude Code model or effort")
        self.model, self.effort, self.binary = model, effort, binary
        self.session_path = state_dir / harnesses.CLAUDE.session_file
        self.session_id = ""
        if self.session_path.exists():
            state = json.loads(self.session_path.read_text(encoding="utf-8"))
            # A malformed/mismatched state must not silently start another chat.
            if state.get("model") != model or state.get("repo") != str(repo):
                raise ValueError("Claude Code session selection changed; start a fresh agent")
            self.session_id = state["session_id"]
            if not isinstance(self.session_id, str) or not self.session_id:
                raise ValueError("invalid Claude Code session ID")
        super().__init__(harnesses.CLAUDE, repo=repo, state_dir=state_dir, home=codex_home)
        self.instructions = state_dir / "claude-instructions.md"
        files = [repo / "AGENTS.md", repo / ".agents/skills/telegram-chat/SKILL.md"]
        personality = os.environ.get("TELEAGENT_PERSONALITY_FILE")
        files.append(Path(personality) if personality else
                     next((repo / "config" / name for name in ["personality.md", "personality.default.md"]
                           if (repo / "config" / name).is_file()), repo / "config/personality.default.md"))
        with open(self.instructions, "w", encoding="utf-8", newline="") as handle:
            handle.write("\n\n".join(p.read_text(encoding="utf-8") for p in files if p.is_file()))
        self.instructions.chmod(0o600)
        self.log.turn_context(claude.native_model(model), effort)
        self.effort_warned = False

    def ready_line(self) -> str:
        return f"Claude Code ready · model={claude.native_model(self.model)} · effort={self.effort}"

    def save_session(self, session_id: str) -> None:
        if self.session_id and session_id != self.session_id:
            raise RuntimeError("Claude Code returned a different session; refusing to switch")
        self.session_id = session_id
        temporary = self.session_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"session_id": session_id, "model": self.model,
                                         "repo": str(self.repo)}), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.session_path)

    def run_message(self, message: str) -> None:
        self.log.turn_context(claude.native_model(self.model), self.effort)
        self.log.turn_started(message)
        self.interrupted = False
        command = claude.build_turn_command(self.binary, self.model, self.effort,
                                            self.instructions, self.session_id)
        result: dict = {}
        # Root text waits for the next event: if the turn continues it was an
        # intermediate message, and if the result follows it is the final answer.
        pending: list[str] = []
        # Answers wait for the end of the CLI run, so a startup notice precedes
        # them, or for a later answer's turn to begin.
        answers: list[str] = []

        def flush_pending() -> None:
            for text in pending:
                self.log.agent_message(text, phase="commentary")
            pending.clear()

        def flush_answers() -> None:
            for text in answers:
                self.log.agent_message(text, phase="final_answer")
            answers.clear()

        def on_event(event: dict) -> None:
            if event.get("type") in ("assistant", "user"):
                # Only these continue the turn; status events such as
                # rate_limit_event may still precede the final result.
                flush_pending()
            if event.get("parent_tool_use_id"):
                return  # Child-agent text must not become the root answer.
            if event.get("type") == "assistant":
                content = (event.get("message") or {}).get("content") or []
                text = "\n\n".join(str(block.get("text") or "").strip() for block in content
                                   if isinstance(block, dict) and block.get("type") == "text"
                                   and str(block.get("text") or "").strip())
                if text:
                    pending.append(text)
            if event.get("type") == "system" and event.get("subtype") == "init":
                flush_answers()  # A message that arrived as the last turn ended starts another.
                self.save_session(event["session_id"])
                if event.get("model"):
                    self.log.turn_context(str(event["model"]), self.effort)
                    claude.remember_model(self.state_dir, self.model, str(event["model"]))
            elif event.get("type") == "result":
                if event.get("session_id"):
                    self.save_session(event["session_id"])
                result.clear()
                result.update(event)
                if not event.get("is_error") and event.get("subtype") == "success":
                    final = str(event.get("result") or "")
                    if pending and pending[-1].split() == final.split():
                        pending.pop()  # The result repeats the closing message.
                    flush_pending()
                    answers.append(final)
                # The CLI still answers what it already received, then exits.
                self.close_input()

        def feed_messages() -> None:
            """Add Telegram messages that arrive mid-turn to the running turn."""
            while not done.is_set() and not self.input_open:
                done.wait(0.02)  # Until the CLI has started and received the first message.
            while not done.is_set() and self.input_open:
                try:
                    kind, value = self.events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "message" and self.send_input(claude.input_line(value)):
                    self.log.user_message(value)
                elif kind != "interrupt":  # The key reader already interrupted the turn.
                    self.carry.append((kind, value))
                    if kind == "eof":
                        return

        done = threading.Event()
        feeder = threading.Thread(target=feed_messages, daemon=True)
        feeder.start()
        try:
            try:
                returncode, stderr = self.stream(command, prompt=claude.input_line(message),
                                                 on_event=on_event, keep_input=True)
            finally:
                done.set()
                feeder.join()
            if claude.UNKNOWN_EFFORT_WARNING in stderr and not self.effort_warned:
                # The CLI ignores an effort it does not know and still exits 0.
                self.effort_warned = True
                self.log.agent_message(
                    f"This Claude Code version does not support effort '{self.effort}' and "
                    "is using its default effort. Start a fresh /claude with a supported level.",
                    phase="commentary")
            succeeded = (not self.interrupted and returncode == 0 and not result.get("is_error")
                         and result.get("subtype") == "success")
            flush_pending()
            flush_answers()
            if self.interrupted:
                self.log.agent_message("Claude Code turn interrupted.", phase="commentary")
            elif not succeeded:
                # Do not forward provider stderr, tool input, or credential-bearing URLs.
                subtype = str(result.get("subtype") or "missing result")
                detail = subtype if subtype in KNOWN_FAILURES else f"exit {returncode}"
                self.log.agent_message(f"Claude Code failed ({detail}); inspect local diagnostics.",
                                       phase="commentary")
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            # Keep the pane alive after a failed turn, without changing conversations.
            safe = redact(str(exc), dict(os.environ))
            print(f"Claude Code turn failed: {safe[:250]}", flush=True)
            self.log.agent_message("Claude Code turn failed; inspect local diagnostics.", phase="commentary")
        finally:
            flush_pending()
            flush_answers()
            self.log.turn_finished(aborted=self.interrupted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    args = parser.parse_args()
    try:
        binary = claude.ensure_available()
        return ClaudeAgentRunner(**vars(args), binary=binary).serve()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Claude Code unavailable: {type(exc).__name__}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
