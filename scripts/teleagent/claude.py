"""Native Claude Code selection and print-mode protocol."""

from __future__ import annotations

import os
import re
import json
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# Aliases the CLI itself resolves to the newest model of each family; newest family first.
MODELS = {"cc-fable": "fable", "cc-opus": "opus", "cc-sonnet": "sonnet", "cc-haiku": "haiku"}
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultracode"}


def is_claude_model(model: str) -> bool:
    return model.strip().lower() in MODELS


def native_model(model: str) -> str:
    return MODELS[model]


SEEN_MODELS = "claude_models.json"
_MODEL_ID = re.compile(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?(?:\[[^\]]*\])?$")


def remember_model(state_dir: Path, model: str, resolved: str) -> None:
    """Record the model the CLI actually ran for an alias such as `fable`."""
    path = state_dir / SEEN_MODELS
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    if isinstance(seen, dict) and seen.get(MODELS[model]) == resolved:
        return
    seen = {**(seen if isinstance(seen, dict) else {}), MODELS[model]: resolved}
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(seen, handle)
    os.replace(temporary, path)


def display_name(alias: str, state_dir: Path) -> str:
    """`Fable 5.2` once the CLI has reported what `fable` is here; the alias until then."""
    try:
        resolved = str(json.loads((state_dir / SEEN_MODELS).read_text(encoding="utf-8")).get(alias) or "")
    except (OSError, ValueError, AttributeError):
        resolved = ""
    found = _MODEL_ID.match(resolved)
    if not found:
        return resolved or alias
    family, major, minor = found.groups()
    return f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")


def parse_launch_payload(payload: str) -> tuple[str, str]:
    tokens = shlex.split(payload)
    if len(tokens) > 2:
        raise ValueError("/claude accepts at most a model and effort, never a prompt")
    model = tokens[0].lower() if tokens else "opus"
    model = model if model.startswith("cc-") else "cc-" + model
    effort = tokens[1].lower() if len(tokens) == 2 else "medium" if model == "cc-opus" else "high"
    if model not in MODELS:
        raise ValueError("Claude Code model must be fable, opus, sonnet, or haiku")
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
    # Preflight before replacing a running agent.
    if not signed_in(executable):
        raise RuntimeError("Claude Code is not signed in on this machine")
    return executable


def signed_in(executable: str) -> bool:
    """Ask the CLI itself. Its output is never forwarded."""
    try:
        return subprocess.run([executable, "auth", "status"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def logout() -> str:
    result = subprocess.run([binary(), "auth", "logout"], capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Claude sign-out failed; account state was not confirmed")
    return "Claude Code signed out on this machine."


# Marker the CLI prints on stderr when it ignores an effort level it does not know.
UNKNOWN_EFFORT_WARNING = "Unknown --effort value"

USAGE_WINDOW_LABELS = {"five_hour": "5-hour window", "seven_day": "7-day window",
                       "seven_day_overage_included": "Fable 7-day window"}
USAGE_PROBE_PROMPT = "Reply with OK."
# The CLI reports Fable's own weekly window only on a Fable turn; that turn also
# reports the shared windows. Haiku covers accounts without Fable.
USAGE_PROBE_MODELS = ("fable", "haiku")


def _format_reset(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return ""
    # The listener's own time zone, written as an offset: zone names differ by OS.
    local = time.localtime(value)
    offset = time.strftime("%z", local)
    zone = f" UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else ""
    return time.strftime(", resets %a %d %b %H:%M", local) + zone


def _format_window(name: str, window: dict) -> str:
    label = USAGE_WINDOW_LABELS.get(name, name.replace("_", " "))
    used = window.get("utilization")
    line = (f"{label}: {round(used * 100)}% used"
            if isinstance(used, (int, float)) and not isinstance(used, bool)
            else f"{label}: usage not reported")
    line += _format_reset(window.get("resetsAt"))
    status = window.get("status")
    if isinstance(status, str) and status and status != "allowed":
        line += f" ({status.replace('_', ' ')})"
    return line


def format_rate_limits(info: dict) -> str:
    """Render one `rate_limit_info` object without account identifiers."""
    lines = ["Claude usage, checked live:"]
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        lines += [_format_window(str(name), window)
                  for name, window in windows.items() if isinstance(window, dict)]
    if len(lines) == 1:
        # Older or API-key responses describe only the binding window.
        name = info.get("rateLimitType")
        if not isinstance(name, str) or not name:
            raise ValueError("no rate-limit window reported")
        lines.append(_format_window(name, info))
    if info.get("isUsingOverage") is True:
        lines.append("Extra usage is being consumed.")
    return "\n".join(lines)


def usage() -> str:
    """Read Claude's live rate-limit windows.

    Print mode does not run the interactive `/usage` command: the model answers
    it in prose. The CLI does report its limits as a `rate_limit_event` on each
    stream-json turn, so one minimal turn in an empty directory, without tools
    or history, reads the current values.
    """
    failure = "Claude's live usage query failed; no cached value was substituted"
    info = None
    for model in USAGE_PROBE_MODELS:
        with tempfile.TemporaryDirectory(prefix="claude-usage-") as directory:
            result = subprocess.run([binary(), "--print", "--output-format", "stream-json",
                "--verbose", "--model", model, "--effort", "low", "--tools", "",
                "--no-session-persistence"], input=USAGE_PROBE_PROMPT, text=True,
                capture_output=True, cwd=directory, timeout=60)
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (isinstance(event, dict) and event.get("type") == "rate_limit_event"
                    and isinstance(event.get("rate_limit_info"), dict)):
                info = event["rate_limit_info"]
        if not result.returncode and info is not None:
            break
        info = None
    if info is None:
        # Never forward stdout or stderr: either may carry account details.
        raise RuntimeError(failure)
    try:
        return format_rate_limits(info)
    except ValueError:
        raise RuntimeError(failure) from None


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
    try:
        executable = binary()
    except RuntimeError:
        return "Claude Code CLI unavailable"
    try:
        result = subprocess.run([executable, "auth", "status"], capture_output=True,
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
