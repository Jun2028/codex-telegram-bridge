#!/usr/bin/env python3
"""Run the Antigravity (agy) harness for the Telegram relay.

The runner keeps one long-lived agy conversation per bot. Each Telegram message
runs one headless ``agy`` turn whose NDJSON stream is translated into the
session records the listener forwards to Telegram.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from functools import partial
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from teleagent import agy as agy_runtime  # noqa: E402
from teleagent import harnesses  # noqa: E402
from teleagent import runner as _runner  # noqa: E402

SessionLog = partial(_runner.SessionLog, harness=harnesses.AGY.key)


class AgyAgentRunner(_runner.AgentRunner):
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
        self.agy_bin = agy_bin
        super().__init__(harnesses.AGY, repo=repo, state_dir=state_dir, home=codex_home)
        self.conversation_path = state_dir / harnesses.AGY.session_file
        self.conversation_id = self._load_conversation()

    def _load_conversation(self) -> str:
        try:
            data = json.loads(self.conversation_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        value = data.get("conversation_id")
        return value if isinstance(value, str) else ""

    def _save_conversation(self) -> None:
        temporary = self.conversation_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"conversation_id": self.conversation_id}), encoding="utf-8"
        )
        temporary.replace(self.conversation_path)

    def ready_line(self) -> str:
        return (
            f"agy harness ready · model={self.model}"
            + (f" (from {self.requested_model})" if self.model != self.requested_model else "")
            + (f" · effort={self.effort}" if self.effort else " · effort=default")
        )

    def run_message(self, message: str) -> None:
        print(f"\n▶ {message.splitlines()[0][:160]}", flush=True)
        self.log.turn_context(self.model, self.effort)
        self.log.turn_started(message)
        self.interrupted = False
        command = agy_runtime.build_turn_command(
            prompt=message,
            model=self.model,
            agy_bin=self.agy_bin,
            effort=self.effort or None,
            conversation_id=self.conversation_id or None,
        )
        result = agy_runtime.AgyTurnResult()
        deltas: list[str] = []

        def on_event(event: dict) -> None:
            result.events.append(event)
            agy_runtime.apply_stream_event(result, event, deltas)
            step = event.get("step_update") if event.get("event") == "step_update" else None
            if (isinstance(step, dict) and step.get("step_type") == "agent_response"
                    and step.get("text_delta")):
                print(str(step["text_delta"]), end="", flush=True)  # Shown by /status.

        try:
            # agy takes the prompt as an argument; it has no standard-input mode.
            returncode, stderr = self.stream(command, prompt=None, on_event=on_event)
        except OSError as exc:
            returncode, stderr = 127, f"agy could not start ({type(exc).__name__})"
        if self.interrupted or (returncode != 0 and not result.status):
            print("\n[turn interrupted]", flush=True)
            self.log.agent_message("[agy turn interrupted]", phase="commentary")
            self.log.turn_finished(aborted=self.interrupted)
            return
        if not result.text and deltas:
            result.text = "".join(deltas).strip()
        if not result.status:
            result.status = "SUCCESS" if returncode == 0 else "ERROR"
        if result.conversation_id and result.conversation_id != self.conversation_id:
            self.conversation_id = result.conversation_id
            self._save_conversation()
        if result.status == "SUCCESS" and result.text:
            self.log.agent_message(result.text, phase="final_answer")
        else:
            detail = stderr.strip().splitlines()
            error = result.error or (detail[-1] if detail else f"agy exited rc={returncode}")
            self.log.agent_message(f"agy error: {error}", phase="commentary")
        self.log.turn_finished()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--agy-bin", default=os.environ.get("TELEAGENT_AGY_BIN", "agy"))
    args = parser.parse_args(argv)

    try:
        agent = AgyAgentRunner(
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
    return agent.serve()


if __name__ == "__main__":
    raise SystemExit(main())
