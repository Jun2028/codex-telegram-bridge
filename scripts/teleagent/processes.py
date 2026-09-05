"""Processes services for the Telegram relay."""

from __future__ import annotations


import notify as _notify
import os
import signal
import shlex
import shutil
import subprocess
import time
from pathlib import Path
import telegram_agent_registry as agent_registry  # noqa: E402

from . import settings as _settings
from . import submission as _submission


def restore_tmux_socket_from_env() -> bool:
    tmux_env = os.environ.get("TMUX", "")
    parts = tmux_env.split(",")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return False
    socket_path = Path(parts[0])
    if socket_path.exists():
        return False
    try:
        server_pid = int(parts[1])
    except ValueError:
        return False
    try:
        socket_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.kill(server_pid, signal.SIGUSR1)
    except OSError:
        return False
    for _ in range(20):
        if socket_path.exists():
            return True
        time.sleep(0.1)
    return socket_path.exists()


def tmux_value(target_pane: str, expression: str) -> str:
    restore_tmux_socket_from_env()
    return _notify.run_short(
        ["tmux", "display-message", "-p", "-t", target_pane, expression], timeout=5
    )


def tmux_pane_command(target_pane: str) -> str:
    return tmux_value(target_pane, "#{pane_current_command}").strip()


def tmux_pane_has_codex_supervisor(target_pane: str) -> bool:
    root_pid = agent_registry.tmux_pane_pid(target_pane)
    if root_pid is None:
        return False
    for pid in agent_registry.descendants(root_pid):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if b"codex_agent_supervisor.sh" in cmdline:
            return True
    return False


def tmux_pane_has_codex_process(target_pane: str) -> bool:
    root_pid = agent_registry.tmux_pane_pid(target_pane)
    if root_pid is not None:
        for pid in agent_registry.descendants(root_pid):
            try:
                command_name = (
                    Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
                )
            except OSError:
                continue
            if command_name in _settings.CODEX_COMMANDS:
                return True
    return registered_codex_process_running(target_pane)


def codex_process_identity(target_pane: str) -> str | None:
    """Return a PID-reuse-safe identity for the Codex process in a tmux pane."""
    try:
        root_pid = agent_registry.tmux_pane_pid(target_pane)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None
    if root_pid is None:
        return None

    identities: list[tuple[int, str]] = []
    for pid in agent_registry.descendants(root_pid):
        try:
            command_name = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
            if command_name not in _settings.CODEX_COMMANDS:
                continue
            # Field 22 of /proc/PID/stat is the process start time in clock
            # ticks.  Pairing it with the PID prevents a recycled PID from
            # looking like the process that accepted an earlier relay.
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            stat_fields = stat_text[stat_text.rfind(")") + 2 :].split()
            start_ticks = stat_fields[19]
        except (IndexError, OSError):
            continue
        identities.append((pid, start_ticks))
    if not identities:
        return None
    return ";".join(f"{pid}:{start_ticks}" for pid, start_ticks in sorted(identities))


def process_has_codex_session(pid: int, session_path: Path) -> bool:
    try:
        command_name = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if command_name not in _settings.CODEX_COMMANDS:
        return False
    for open_session in agent_registry.session_files_open_by_pid(pid):
        try:
            if os.path.samefile(open_session, session_path):
                return True
        except OSError:
            continue
    return False


def registered_codex_process_running(target_pane: str) -> bool:
    """Verify the registered Codex process without depending on a tmux probe."""
    meta = agent_registry.active_agent_for_pane(target_pane)
    if not meta:
        _settings.REGISTERED_CODEX_PID_CACHE.pop(target_pane, None)
        return False
    session_text = str(meta.get("codex_session_path") or "")
    if not session_text:
        _settings.REGISTERED_CODEX_PID_CACHE.pop(target_pane, None)
        return False
    session_path = Path(session_text)
    cached = _settings.REGISTERED_CODEX_PID_CACHE.get(target_pane)
    if (
        cached
        and cached[1] == session_text
        and process_has_codex_session(cached[0], session_path)
    ):
        return True
    _settings.REGISTERED_CODEX_PID_CACHE.pop(target_pane, None)
    for pid, _, command_name in agent_registry.ps_rows():
        if command_name in _settings.CODEX_COMMANDS and process_has_codex_session(
            pid, session_path
        ):
            _settings.REGISTERED_CODEX_PID_CACHE[target_pane] = (pid, session_text)
            return True
    return False


def codex_target_ready(target_pane: str) -> bool:
    if registered_codex_process_running(target_pane):
        return True
    try:
        return tmux_target_exists(target_pane) and tmux_pane_has_codex_process(
            target_pane
        )
    except (OSError, subprocess.SubprocessError):
        # A busy tmux server can exceed the probe timeout. Treat that as a
        # transient unknown state so the listener keeps polling Telegram.
        return False


def wait_for_supervised_codex(target_pane: str, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        if codex_target_ready(target_pane):
            return True
        if not tmux_pane_has_codex_supervisor(target_pane):
            return False
        time.sleep(1.0)
    return False


def tmux_target_exists(target_pane: str) -> bool:
    restore_tmux_socket_from_env()
    completed = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target_pane, "#{pane_id}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.returncode == 0


def tmux_window_exists(session: str, window: str) -> bool:
    restore_tmux_socket_from_env()
    completed = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        return False
    return window in {line.strip() for line in completed.stdout.splitlines()}


def ensure_tmux_session(repo_root: Path, session: str) -> None:
    restore_tmux_socket_from_env()
    if (
        subprocess.run(["tmux", "has-session", "-t", session], check=False).returncode
        == 0
    ):
        return
    subprocess.run(
        [str(repo_root / "scripts" / "start_tmux.sh"), session], check=True, timeout=10
    )


def codex_executable() -> str:
    found = shutil.which("codex")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "codex"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("codex CLI not found on PATH or ~/.local/bin/codex")


def codex_home_for_model(model: str) -> Path:
    scratch = Path(
        os.environ.get(
            "TELEAGENT_SCRATCH",
            str(Path.home() / ".local" / "share" / "tele-agent"),
        )
    )
    if model in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        return (
            Path(
                os.environ.get(
                    "TELEAGENT_DS_CODEX_HOME",
                    str(scratch / "tele-agent-ds-codex-home"),
                )
            )
            .expanduser()
            .resolve()
        )
    if os.environ.get("TELEAGENT_CODEX_ACCESS_MODE", "full-access") == "chat-only":
        return (
            Path(
                os.environ.get(
                    "TELEAGENT_CHAT_ONLY_CODEX_HOME",
                    str(scratch / "chat-only-codex-home"),
                )
            )
            .expanduser()
            .resolve()
        )
    return (
        Path(os.environ.get("TELEAGENT_CODEX_HOME", str(Path.home() / ".codex")))
        .expanduser()
        .resolve()
    )


def tmux_bash_shell_command() -> str:
    bash_path = shutil.which("bash")
    if not bash_path:
        raise RuntimeError("bash is required for managed tele-agent tmux panes")
    return shlex.join(["exec", bash_path, "--noprofile", "--norc"])


def tmux_tail(target_pane: str, lines: int = 80) -> str:
    return _notify.run_short(
        ["tmux", "capture-pane", "-pt", target_pane, "-S", f"-{lines}"], timeout=8
    )


def codex_goal_blocked(target_pane: str) -> bool:
    tail = tmux_tail(target_pane, lines=40)
    recent = "\n".join(tail.strip().splitlines()[-10:])
    return "Goal blocked" in recent and "/goal resume" in recent


def codex_goal_active(target_pane: str) -> bool:
    tail = tmux_tail(target_pane, lines=40)
    recent = "\n".join(tail.strip().splitlines()[-12:])
    return "Goal active" in recent


def codex_tui_idle(target_pane: str) -> bool:
    """Recognize an idle composer even when an interrupted rollout lacks a terminal event."""
    tail = tmux_tail(target_pane, lines=24)
    recent_lines = tail.strip().splitlines()[-10:]
    recent = "\n".join(recent_lines)
    if "Goal active" in recent:
        return False
    return any(line.strip() == "› Ask Codex to do anything" for line in recent_lines)


def relay_codex_control(target_pane: str, text: str, submit_delay: float) -> str:
    command_text = text.strip()
    if not command_text:
        return "not relayed: empty Codex control command"
    if not codex_target_ready(target_pane):
        return f"not relayed: tmux target not found: {target_pane}"

    if not tmux_pane_has_codex_process(target_pane):
        current_command = tmux_pane_command(target_pane)
        return (
            f"not relayed: target {target_pane} is {current_command or 'unknown'}, not Codex. "
            "Use /start_agent first."
        )

    return _submission.paste_to_tmux(
        target_pane,
        command_text,
        press_enter=True,
        allow_shell_pane=False,
        submit_delay=submit_delay,
    )


def tmux_send_keys(target_pane: str, *keys: str, literal: bool = False) -> None:
    restore_tmux_socket_from_env()
    if (
        not literal and "Enter" in keys
        and _submission.codex_model_switch_prompt_visible(target_pane)
    ):
        raise subprocess.SubprocessError("automatic model-switch selection blocked")
    command = ["tmux", "send-keys", "-t", target_pane]
    if literal:
        command.append("-l")
    command.extend(keys)
    subprocess.run(command, check=True, timeout=_settings.TMUX_MUTATION_TIMEOUT_SECONDS)


def wait_for_tmux_text(target_pane: str, expected: str, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() <= deadline:
        pane_text = tmux_tail(target_pane, lines=80)
        if expected in pane_text:
            return pane_text
        time.sleep(0.1)
    raise RuntimeError(f"Codex selector did not show {expected!r}")


def resume_blocked_goal_if_needed(target_pane: str, submit_delay: float) -> str | None:
    if not codex_target_ready(target_pane):
        return None
    if not codex_goal_blocked(target_pane):
        return None

    result = relay_codex_control(target_pane, "/goal resume", submit_delay)
    if result.startswith("relayed to "):
        time.sleep(max(1.0, submit_delay))
    return result
