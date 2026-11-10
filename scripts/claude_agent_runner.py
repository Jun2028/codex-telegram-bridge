#!/usr/bin/env python3
"""Native Claude Code turns in the existing Telegram relay and supervisor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
        self.instructions.write_text(
            "\n\n".join(p.read_text(encoding="utf-8") for p in files if p.is_file()),
            encoding="utf-8", newline="")
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

        def on_event(event: dict) -> None:
            if event.get("parent_tool_use_id"):
                return  # Child-agent text must not become the root answer.
            if event.get("type") == "system" and event.get("subtype") == "init":
                self.save_session(event["session_id"])
                if event.get("model"):
                    self.log.turn_context(str(event["model"]), self.effort)
            elif event.get("type") == "result":
                if event.get("session_id"):
                    self.save_session(event["session_id"])
                result.clear()
                result.update(event)

        try:
            returncode, stderr = self.stream(command, prompt=message, on_event=on_event)
            if claude.UNKNOWN_EFFORT_WARNING in stderr and not self.effort_warned:
                # The CLI ignores an effort it does not know and still exits 0.
                self.effort_warned = True
                self.log.agent_message(
                    f"This Claude Code version does not support effort '{self.effort}' and "
                    "is using its default effort. Start a fresh /claude with a supported level.",
                    phase="commentary")
            if self.interrupted:
                self.log.agent_message("Claude Code turn interrupted.", phase="commentary")
            elif returncode == 0 and not result.get("is_error") and result.get("subtype") == "success":
                self.log.agent_message(str(result.get("result") or ""), phase="final_answer")
            else:
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
