"""DeepSeek Harness CLI support for the Telegram relay."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator


DSH_FLASH_AGENT_MODEL = "dsh-flash"
DSH_PRO_AGENT_MODEL = "dsh-pro"
DSH_AGENT_MODELS = frozenset({DSH_FLASH_AGENT_MODEL, DSH_PRO_AGENT_MODEL})
DSH_DEFAULT_AGENT_MODEL = DSH_FLASH_AGENT_MODEL
DSH_DEFAULT_REASONING_EFFORT = "max"
DSH_REASONING_EFFORTS = frozenset({"off", "low", "high", "max"})

DSH_NATIVE_MODELS = {
    DSH_FLASH_AGENT_MODEL: "deepseek-flash",
    DSH_PRO_AGENT_MODEL: "deepseek-v4-pro",
}

DSH_MODEL_ALIASES = {
    "flash": DSH_FLASH_AGENT_MODEL,
    "dsh-flash": DSH_FLASH_AGENT_MODEL,
    "ds-flash": DSH_FLASH_AGENT_MODEL,
    "deepseek-flash": DSH_FLASH_AGENT_MODEL,
    "pro": DSH_PRO_AGENT_MODEL,
    "dsh-pro": DSH_PRO_AGENT_MODEL,
    "ds-pro": DSH_PRO_AGENT_MODEL,
    "deepseek-v4-pro": DSH_PRO_AGENT_MODEL,
}


class DshError(RuntimeError):
    """Base error for DeepSeek Harness operations."""


class DshUnavailable(DshError):
    """The DeepSeek Harness CLI could not be found or started."""


def is_dsh_model(model: str) -> bool:
    return model.strip().lower() in DSH_AGENT_MODELS


def normalize_model(value: str) -> str:
    normalized = value.strip().lower()
    model = DSH_MODEL_ALIASES.get(normalized)
    if model is None:
        raise ValueError("DeepSeek Harness model must be flash or pro")
    return model


def native_model(model: str) -> str:
    normalized = model.strip().lower()
    try:
        return DSH_NATIVE_MODELS[normalized]
    except KeyError:
        raise ValueError(f"unsupported DeepSeek Harness model: {model}") from None


def parse_launch_payload(payload: str) -> tuple[str, str]:
    """Parse ``FLASH|PRO [OFF|LOW|HIGH|MAX]`` for ``/dsh``."""
    try:
        tokens = shlex.split(payload)
    except ValueError as exc:
        raise ValueError(f"invalid quoting: {exc}") from exc
    if len(tokens) > 2:
        raise ValueError("/dsh accepts at most a model and reasoning level")
    if not tokens:
        return DSH_DEFAULT_AGENT_MODEL, DSH_DEFAULT_REASONING_EFFORT

    first = tokens[0].lower()
    if len(tokens) == 1 and first in DSH_REASONING_EFFORTS:
        return DSH_DEFAULT_AGENT_MODEL, first

    model = normalize_model(first)
    effort = (
        tokens[1].lower()
        if len(tokens) == 2
        else DSH_DEFAULT_REASONING_EFFORT
    )
    if effort not in DSH_REASONING_EFFORTS:
        raise ValueError(
            "DeepSeek Harness reasoning must be off, low, high, or max"
        )
    return model, effort


def dsh_home() -> Path:
    scratch = Path(
        os.environ.get(
            "TELEAGENT_SCRATCH",
            str(Path.home() / ".local" / "share" / "tele-agent"),
        )
    ).expanduser()
    return Path(
        os.environ.get("TELEAGENT_DSH_HOME", str(scratch / "dsh-home"))
    ).expanduser().resolve()


def dsh_binary() -> str | None:
    configured = os.environ.get("TELEAGENT_DSH_BIN", "").strip()
    if configured:
        resolved = shutil.which(configured)
        return resolved or (
            configured
            if Path(configured).is_file() and os.access(configured, os.X_OK)
            else None
        )

    resolved = shutil.which("dsh")
    if resolved:
        return resolved
    local = dsh_home() / "cli" / "node_modules" / ".bin" / "dsh"
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def ensure_binary(repo_root: Path) -> str:
    binary = dsh_binary()
    if binary:
        return binary
    installer = repo_root / "scripts" / "install_dsh_harness.sh"
    try:
        completed = subprocess.run(
            [str(installer)],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DshUnavailable(f"could not install DeepSeek Harness: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise DshUnavailable(
            detail[-1] if detail else "DeepSeek Harness installation failed"
        )
    binary = dsh_binary()
    if not binary:
        raise DshUnavailable("DeepSeek Harness installation did not create dsh")
    return binary


def verify_runs(binary: str) -> None:
    """Fail early when the harness cannot start, instead of on the first message.

    On a Node.js that is too old the CLI exits silently with success, so the
    only reliable sign is that it prints its version.
    """
    try:
        completed = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                   timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DshUnavailable(f"DeepSeek Harness could not start: {type(exc).__name__}") from exc
    if completed.returncode == 0 and completed.stdout.strip():
        return
    node = shutil.which("node")
    version = ""
    if node:
        try:
            version = subprocess.run([node, "--version"], capture_output=True, text=True,
                                     timeout=20, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    raise DshUnavailable(
        "DeepSeek Harness did not start. It needs a current Node.js (22.20 works); "
        f"this machine has {version or 'none on PATH'}"
    )


def write_model_overlay(path: Path, model: str, effort: str) -> None:
    """Write the per-agent DSH selection without storing credentials."""
    native = native_model(model)
    effort_yaml = json.dumps(effort)
    model_yaml = json.dumps(native)
    content = (
        "# tele-agent DeepSeek Harness model selection\n"
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: deepseek-official\n"
        f"    model: {model_yaml}\n"
        f"    reasoningEffort: {effort_yaml}\n"
        "- id: llm-deepseek\n"
        "  config:\n"
        f"    reasoningEffort: {effort_yaml}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def build_turn_command(
    *,
    dsh_bin: str,
    overlay_path: Path,
    session_id: str | None = None,
) -> list[str]:
    """Build one machine-readable headless DSH turn."""
    command = [
        dsh_bin,
        "--profile",
        "headless",
        "--patch",
        str(overlay_path),
        "--json",
    ]
    if session_id:
        command += ["--session-id", session_id]
    command.append("-")
    return command


def iter_stream_events(lines: Iterable[str]) -> Iterator[dict]:
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


@dataclass
class DshTurnResult:
    session_id: str = ""
    text: str = ""
    error: str = ""
    events: list[dict] = field(default_factory=list)


def apply_stream_event(result: DshTurnResult, event: dict) -> None:
    kind = str(event.get("type") or "")
    if kind == "session":
        result.session_id = str(event.get("sessionId") or result.session_id)
    elif kind == "final":
        result.text = str(event.get("text") or "")
    elif kind == "error":
        result.error = str(event.get("message") or "")


def run_turn(
    *,
    prompt: str,
    dsh_bin: str,
    overlay_path: Path,
    session_id: str | None = None,
    on_event: Callable[[dict], None] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[DshTurnResult, int, str]:
    """Run one headless turn and return result, exit code and stderr."""
    command = build_turn_command(
        dsh_bin=dsh_bin,
        overlay_path=overlay_path,
        session_id=session_id,
    )
    effective_env = dict(os.environ if env is None else env)
    effective_env.setdefault("NARB_DISABLE_NATIVE_CACHE", "1")
    completed = subprocess.run(
        command,
        input=prompt,
        capture_output=True,
        text=True,
        env=effective_env,
        check=False,
    )
    result = DshTurnResult()
    for event in iter_stream_events(completed.stdout.splitlines()):
        result.events.append(event)
        apply_stream_event(result, event)
        if on_event is not None:
            on_event(event)
    return result, completed.returncode, completed.stderr
