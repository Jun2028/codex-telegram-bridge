#!/usr/bin/env python3
"""Run Codex for the Telegram relay without its terminal interface.

Each Telegram message runs one ``codex exec --json`` turn, resuming the same
Codex session. This is the Codex harness for hosts where the interactive Codex
TUI cannot run, such as Windows without tmux. It has no live model switch and
no Goal mode; start a fresh agent to change model or effort.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from notify import redact  # noqa: E402
from teleagent import harnesses  # noqa: E402
from teleagent.runner import AgentRunner  # noqa: E402


def build_turn_command(binary: str, model: str, effort: str, session_id: str = "") -> list[str]:
    """One non-interactive Codex turn; the prompt is read from standard input."""
    command = [binary, "exec"]
    if session_id:
        command += ["resume", session_id]
    command += ["--json", "--skip-git-repo-check", "--model", model,
                "--dangerously-bypass-approvals-and-sandbox"]
    if effort:
        command += ["-c", f'model_reasoning_effort="{effort}"']
    return command + ["-"]


class CodexExecRunner(AgentRunner):
    def __init__(self, *, model: str, effort: str, repo: Path, state_dir: Path,
                 codex_home: Path, binary: str) -> None:
        self.model, self.effort, self.binary = model, effort, binary
        self.codex_home = codex_home
        super().__init__(harnesses.CODEX_EXEC, repo=repo, state_dir=state_dir, home=codex_home)
        self.session_path = state_dir / harnesses.CODEX_EXEC.session_file
        self.session_id = self._load_session_id()
        self.log.turn_context(model, effort)

    def _load_session_id(self) -> str:
        try:
            state = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        # A different model or repository is a different conversation.
        if state.get("model") != self.model or state.get("repo") != str(self.repo):
            return ""
        value = state.get("session_id")
        return value if isinstance(value, str) else ""

    def _save_session_id(self, session_id: str) -> None:
        self.session_id = session_id
        temporary = self.session_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"session_id": session_id, "model": self.model,
                                         "repo": str(self.repo)}), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.session_path)

    def ready_line(self) -> str:
        return f"Codex ready · model={self.model} · effort={self.effort or 'default'}"

    def run_message(self, message: str) -> None:
        self.log.turn_context(self.model, self.effort)
        self.log.turn_started(message)
        self.interrupted = False
        command = build_turn_command(self.binary, self.model, self.effort, self.session_id)
        outcome = {"completed": False, "failure": ""}
        answer: list[str] = []

        def on_event(event: dict) -> None:
            kind = event.get("type")
            if kind == "thread.started" and isinstance(event.get("thread_id"), str):
                if event["thread_id"] != self.session_id:
                    self._save_session_id(event["thread_id"])
            elif kind == "item.completed":
                item = event.get("item")
                # Startup `error` items are configuration notices, not answers.
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    if answer:  # Only the last message of a turn is the answer.
                        self.log.agent_message(answer.pop(), phase="commentary")
                    answer.append(str(item.get("text") or ""))
            elif kind == "turn.completed":
                outcome["completed"] = True
            elif kind == "turn.failed":
                error = event.get("error")
                outcome["failure"] = str(error.get("message") if isinstance(error, dict) else error)

        env = dict(os.environ, CODEX_HOME=str(self.codex_home))
        try:
            returncode, _stderr = self.stream(command, prompt=message, on_event=on_event, env=env)
        except OSError as exc:
            returncode, outcome["failure"] = 127, f"Codex could not start ({type(exc).__name__})"
        if self.interrupted:
            self.log.agent_message("Codex turn interrupted.", phase="commentary")
        elif returncode == 0 and outcome["completed"] and answer and answer[-1]:
            self.log.agent_message(answer[-1], phase="final_answer")
        else:
            # Codex's own failure text names the cause, such as an unsupported
            # model; standard error is never forwarded.
            detail = redact(outcome["failure"], dict(os.environ))[:300] or f"exit {returncode}"
            self.log.agent_message(f"Codex failed: {detail}", phase="commentary")
        self.log.turn_finished(aborted=self.interrupted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--codex-bin", default=os.environ.get("TELEAGENT_CODEX_BIN", "codex"))
    args = parser.parse_args(argv)
    binary = shutil.which(args.codex_bin)
    if not binary:
        print("Codex unavailable: the codex CLI was not found", flush=True)
        return 2
    return CodexExecRunner(model=args.model, effort=args.effort, repo=args.repo,
                           state_dir=args.state_dir, codex_home=args.codex_home,
                           binary=binary).serve()


if __name__ == "__main__":
    raise SystemExit(main())
