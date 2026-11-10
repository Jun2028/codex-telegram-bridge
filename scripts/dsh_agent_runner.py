#!/usr/bin/env python3
"""Run the native DeepSeek Harness for the Telegram relay.

Each Telegram message runs one ``dsh --profile headless --json`` turn. DSH
events are converted into the session records the listener already forwards.
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

from teleagent import dsh as dsh_runtime  # noqa: E402
from teleagent import harnesses  # noqa: E402
from teleagent import runner as _runner  # noqa: E402

SessionLog = partial(_runner.SessionLog, harness=harnesses.DSH.key)


class DshAgentRunner(_runner.AgentRunner):
    def __init__(
        self,
        *,
        model: str,
        effort: str,
        repo: Path,
        state_dir: Path,
        codex_home: Path,
        dsh_bin: str,
        dsh_home: Path,
    ) -> None:
        self.requested_model = model
        self.model = dsh_runtime.native_model(model)
        self.effort = effort
        self.dsh_bin = dsh_bin
        self.dsh_home = dsh_home
        super().__init__(harnesses.DSH, repo=repo, state_dir=state_dir, home=codex_home)
        self.log.turn_context(self.model, self.effort)
        self.overlay_path = state_dir / "dsh-model.patch.yml"
        dsh_runtime.write_model_overlay(self.overlay_path, model, effort)
        self.session_path = state_dir / harnesses.DSH.session_file
        self.session_id = self._load_session_id()

    def _load_session_id(self) -> str:
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        value = data.get("session_id")
        return value if isinstance(value, str) else ""

    def _save_session_id(self) -> None:
        temporary = self.session_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"session_id": self.session_id}), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.session_path)

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env["DSH_HOME"] = str(self.dsh_home)
        env["DSH_PERMISSION_MODE"] = "danger-full-access"
        env["DSH_TELEMETRY_DISABLED"] = "1"
        # HPC /tmp can be mounted noexec; DSH's optional native-addon cache
        # then fails to map its binding. Load the installed binding in place.
        env["NARB_DISABLE_NATIVE_CACHE"] = "1"
        return env

    def ready_line(self) -> str:
        return f"DeepSeek Harness ready · model={self.model} · effort={self.effort}"

    @staticmethod
    def _safe_error(value: str, fallback: str) -> str:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        detail = next(
            (line for line in reversed(lines) if line.startswith("dsh:")),
            lines[-1] if lines else fallback,
        )
        return " ".join(detail.split())[:600]

    def run_message(self, message: str) -> None:
        print(f"\n▶ {message.splitlines()[0][:160]}", flush=True)
        self.log.turn_context(self.model, self.effort)
        self.log.turn_started(message)
        self.interrupted = False
        command = dsh_runtime.build_turn_command(
            dsh_bin=self.dsh_bin,
            overlay_path=self.overlay_path,
            session_id=self.session_id or None,
        )
        result = dsh_runtime.DshTurnResult()

        def on_event(event: dict) -> None:
            result.events.append(event)
            dsh_runtime.apply_stream_event(result, event)
            if event.get("type") == "tool_call":
                print(f"[dsh] {event.get('tool') or 'tool'}", flush=True)

        try:
            returncode, stderr = self.stream(
                command, prompt=message, on_event=on_event, env=self._environment()
            )
        except OSError as exc:
            returncode, stderr = 127, f"dsh: could not start ({type(exc).__name__})"
        if result.session_id and result.session_id != self.session_id:
            self.session_id = result.session_id
            self._save_session_id()
        if self.interrupted:
            print("\n[turn interrupted]", flush=True)
            self.log.agent_message("[DeepSeek Harness turn interrupted]", phase="commentary")
            self.log.turn_finished(aborted=True)
            return
        if returncode == 0 and result.text:
            self.log.agent_message(result.text, phase="final_answer")
            self.log.turn_finished()
            return
        error = self._safe_error(
            result.error or stderr, f"DeepSeek Harness exited rc={returncode}"
        )
        print(f"\n[dsh error] {error}", flush=True)
        self.log.agent_message(f"DeepSeek Harness error: {error}", phase="commentary")
        self.log.turn_finished()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default=dsh_runtime.DSH_DEFAULT_REASONING_EFFORT)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--dsh-home", type=Path, required=True)
    parser.add_argument("--dsh-bin", required=True)
    args = parser.parse_args(argv)

    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.state_dir.chmod(0o700)
    args.dsh_home.mkdir(parents=True, exist_ok=True)
    args.dsh_home.chmod(0o700)
    try:
        agent = DshAgentRunner(
            model=args.model,
            effort=args.effort,
            repo=args.repo,
            state_dir=args.state_dir,
            codex_home=args.codex_home,
            dsh_bin=args.dsh_bin,
            dsh_home=args.dsh_home,
        )
    except (OSError, ValueError, dsh_runtime.DshError) as exc:
        print(f"DeepSeek Harness unavailable: {exc}", file=sys.stderr, flush=True)
        return 2
    return agent.serve()


if __name__ == "__main__":
    raise SystemExit(main())
