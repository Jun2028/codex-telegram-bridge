"""Native Claude Code selection and print-mode protocol."""

from __future__ import annotations

import os
import json
import shlex
import shutil
import subprocess
from pathlib import Path

MODELS = {"cc-sonnet": "sonnet", "cc-opus": "opus", "cc-haiku": "haiku"}
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultracode"}


def is_claude_model(model: str) -> bool:
    return model.strip().lower() in MODELS


def native_model(model: str) -> str:
    return MODELS[model]


def parse_launch_payload(payload: str) -> tuple[str, str]:
    tokens = shlex.split(payload)
    if len(tokens) > 2:
        raise ValueError("/claude accepts at most a model and effort, never a prompt")
    model = tokens[0].lower() if tokens else "sonnet"
    model = model if model.startswith("cc-") else "cc-" + model
    effort = tokens[1].lower() if len(tokens) == 2 else "high"
    if model not in MODELS:
        raise ValueError("Claude Code model must be sonnet, opus, or haiku")
    if effort not in EFFORTS:
        raise ValueError("Claude Code effort must be low, medium, high, xhigh, max, or ultracode")
    return model, effort


def ensure_available() -> str:
    if os.environ.get("TELEAGENT_CODEX_ACCESS_MODE", "full-access") != "full-access":
        raise RuntimeError("Claude Code requires full-access mode; this instance is chat-only")
    binary = shutil.which(os.environ.get("TELEAGENT_CLAUDE_BIN", "claude"))
    if not binary:
        raise RuntimeError("Claude Code CLI not installed (TELEAGENT_CLAUDE_BIN may select it)")
    # Preflight before replacing a running agent. Never forward auth output.
    result = subprocess.run([binary, "auth", "status"], capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Claude Code is not signed in on this machine")
    return binary


def main() -> int:
    try:
        ensure_available()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # The exception never contains the captured authentication output.
        print(f"Claude Code unavailable: {exc}" if isinstance(exc, RuntimeError)
              else "Claude Code availability check failed")
        return 2
    return 0


def authentication_status() -> str:
    """Read Claude's own account state without exposing account identifiers."""
    binary = shutil.which(os.environ.get("TELEAGENT_CLAUDE_BIN", "claude"))
    if not binary:
        return "Claude Code CLI unavailable"
    try:
        result = subprocess.run([binary, "auth", "status"], capture_output=True,
                                text=True, timeout=20)
        data = json.loads(result.stdout)
        return "Claude Code signed in" if result.returncode == 0 and data.get("loggedIn") else "Claude Code sign-in required"
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return "Claude Code authentication check unavailable"


def build_turn_command(binary: str, model: str, effort: str,
                       instructions: Path, session_id: str = "") -> list[str]:
    command = [binary, "--print", "--output-format", "stream-json", "--verbose",
               "--model", native_model(model), "--effort", effort,
               "--dangerously-skip-permissions", "--append-system-prompt-file", str(instructions)]
    if session_id:
        command += ["--resume", session_id]
    return command  # Prompt travels on stdin, never in argv.


if __name__ == "__main__":
    raise SystemExit(main())
