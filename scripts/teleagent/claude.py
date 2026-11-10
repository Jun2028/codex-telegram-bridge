"""Native Claude Code selection and print-mode protocol."""

from __future__ import annotations

import os
import json
import shlex
import shutil
import subprocess
import tempfile
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


def binary() -> str:
    executable = shutil.which(os.environ.get("TELEAGENT_CLAUDE_BIN", "claude"))
    if not executable:
        raise RuntimeError("Claude Code CLI is not installed on this machine")
    return executable


def ensure_available() -> str:
    if os.environ.get("TELEAGENT_CODEX_ACCESS_MODE", "full-access") != "full-access":
        raise RuntimeError("Claude Code requires full-access mode; this instance is chat-only")
    executable = binary()
    # Preflight before replacing a running agent. Never forward auth output.
    result = subprocess.run([executable, "auth", "status"], capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Claude Code is not signed in on this machine")
    return executable


def logout() -> str:
    result = subprocess.run([binary(), "auth", "logout"], capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Claude sign-out failed; account state was not confirmed")
    return "Claude Code signed out on this machine."


def usage() -> str:
    # Ask Claude's builtin command in an empty directory without tools/history.
    with tempfile.TemporaryDirectory(prefix="claude-usage-") as directory:
        result = subprocess.run([binary(), "--print", "--output-format", "json",
            "--model", "sonnet", "--effort", "low", "--tools", "",
            "--no-session-persistence"], input="/usage", text=True,
            capture_output=True, cwd=directory, timeout=60)
    try:
        data = json.loads(result.stdout)
        report = data.get("result")
        if result.returncode or data.get("is_error") or not isinstance(report, str) or not report.strip():
            raise ValueError()
    except (ValueError, AttributeError):
        raise RuntimeError("Claude's live usage query failed; no cached value was substituted") from None
    return report.strip()[:6000]


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
