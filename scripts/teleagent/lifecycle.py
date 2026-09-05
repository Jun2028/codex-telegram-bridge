"""Lifecycle services for the Telegram relay."""

from __future__ import annotations


import argparse
import fcntl
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any
import telegram_agent_registry as agent_registry  # noqa: E402

from . import settings as _settings
from . import processes as _processes
from . import state as _state


def agent_lifecycle_state_path(args: argparse.Namespace) -> Path | None:
    configured = getattr(args, "agent_lifecycle_state_path", None)
    if configured:
        return Path(configured)
    usage_state = getattr(args, "codex_usage_state_path", None)
    if usage_state:
        return Path(usage_state).with_name("telegram_agent_lifecycle.state.json")
    return None


def agent_desired_state(args: argparse.Namespace) -> str:
    path = agent_lifecycle_state_path(args)
    if path is None:
        return _settings.AGENT_DESIRED_RUNNING
    desired = str(_state.read_json_object(path).get("desired") or "")
    if desired == _settings.AGENT_DESIRED_STOPPED:
        return _settings.AGENT_DESIRED_STOPPED
    return _settings.AGENT_DESIRED_RUNNING


def set_agent_desired_state(
    args: argparse.Namespace,
    desired: str,
    source: str,
) -> None:
    if desired not in {
        _settings.AGENT_DESIRED_RUNNING,
        _settings.AGENT_DESIRED_STOPPED,
    }:
        raise ValueError(f"invalid agent desired state: {desired}")
    path = agent_lifecycle_state_path(args)
    if path is None:
        raise RuntimeError("agent lifecycle state path is unavailable")
    _state.write_json_object(
        path,
        {
            "version": 1,
            "desired": desired,
            "source": source,
            "updated_ts": int(time.time()),
        },
    )


def agent_lifecycle_operation_in_progress(args: argparse.Namespace) -> bool:
    """Return whether another process owns the managed-agent lifecycle lock."""
    state = agent_lifecycle_state_path(args)
    if state is None:
        return False
    lock_path = state.with_name("telegram_agent_lifecycle.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def build_codex_agent_command(
    repo_root: Path,
    codex_path: str,
    codex_agent_env: str,
    model: str = _settings.DEFAULT_CODEX_AGENT_MODEL,
    reasoning_effort: str = _settings.DEFAULT_CODEX_AGENT_REASONING_EFFORT,
) -> str:
    repo_q = shlex.quote(str(repo_root))
    instance_q = shlex.quote(os.environ.get("TELEAGENT_INSTANCE", "main"))
    codex_q = shlex.quote(codex_path)
    supervisor_q = shlex.quote(str(repo_root / "scripts" / "codex_agent_supervisor.sh"))
    model_q = shlex.quote(model)
    reasoning_q = shlex.quote(reasoning_effort)
    command = (
        f"cd {repo_q} && export TELEAGENT_INSTANCE={instance_q} && "
        "source scripts/relay_paths.sh && "
        f"{codex_agent_env} TELEAGENT_CODEX_BIN={codex_q} {supervisor_q} "
        f"--model {model_q} --reasoning-effort {reasoning_q}"
    )
    return command


def start_codex_agent(
    repo_root: Path,
    session: str,
    window: str,
    restart: bool = False,
    model: str = _settings.DEFAULT_CODEX_AGENT_MODEL,
    reasoning_effort: str = _settings.DEFAULT_CODEX_AGENT_REASONING_EFFORT,
) -> tuple[str, str, dict[str, Any] | None]:
    _processes.ensure_tmux_session(repo_root, session)
    target_pane = f"{session}:{window}.0"
    agent_codex_home = _processes.codex_home_for_model(model)
    shell_command = _processes.tmux_bash_shell_command()

    if model in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        prepare_script = repo_root / "scripts" / "prepare_telegram_ds_codex_home.sh"
    elif (
        os.environ.get("TELEAGENT_INSTANCE", "main") != "main"
        or os.environ.get("TELEAGENT_CODEX_ACCESS_MODE", "full-access") == "chat-only"
    ):
        prepare_script = repo_root / "scripts" / "prepare_telegram_codex_home.sh"
    else:
        prepare_script = None
    if prepare_script is not None:
        subprocess.run(
            [str(prepare_script)],
            check=True,
            timeout=30,
        )

    if not _processes.tmux_window_exists(session, window):
        subprocess.run(
            [
                "tmux",
                "new-window",
                "-t",
                session,
                "-n",
                window,
                "-c",
                str(repo_root),
                shell_command,
            ],
            check=True,
            timeout=10,
        )
    else:
        if restart:
            subprocess.run(
                ["tmux", "kill-window", "-t", f"{session}:{window}"],
                check=True,
                timeout=5,
            )
            subprocess.run(
                [
                    "tmux",
                    "new-window",
                    "-t",
                    session,
                    "-n",
                    window,
                    "-c",
                    str(repo_root),
                    shell_command,
                ],
                check=True,
                timeout=10,
            )
        else:
            current_command = _processes.tmux_pane_command(target_pane)
            if _processes.tmux_pane_has_codex_process(
                target_pane
            ) or _processes.tmux_pane_has_codex_supervisor(target_pane):
                meta = agent_registry.adopt_existing_agent(
                    repo_root=repo_root,
                    session=session,
                    window=window,
                    target_pane=target_pane,
                    launch_source="telegram-start-agent-reuse",
                    codex_home=agent_codex_home,
                )
                agent_registry.append_agent_event(
                    meta, {"event": "agent_reused_by_start_agent"}
                )
                return (
                    target_pane,
                    f"Supervised Codex agent already appears to be running in {target_pane}.",
                    meta,
                )
            if current_command not in _settings.SHELL_COMMANDS:
                return (
                    target_pane,
                    (
                        f"Not started: {target_pane} is busy with {current_command or 'unknown'}. "
                        "Use /restart_agent to interrupt it."
                    ),
                    None,
                )

    start_epoch = time.time()
    meta = agent_registry.create_agent(
        repo_root=repo_root,
        session=session,
        window=window,
        target_pane=target_pane,
        launch_source="telegram-restart-agent" if restart else "telegram-start-agent",
        start_epoch=start_epoch,
        codex_home=agent_codex_home,
    )
    codex_agent_env = agent_registry.shell_env_prefix(meta)
    command = build_codex_agent_command(
        repo_root=repo_root,
        codex_path=_processes.codex_executable(),
        codex_agent_env=codex_agent_env,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", target_pane, command, "C-m"], check=True, timeout=5
    )
    for _ in range(20):
        if _processes.tmux_pane_has_codex_process(target_pane):
            break
        time.sleep(0.5)
    meta = agent_registry.refresh_codex_session_link(
        meta, target_pane=target_pane, start_epoch=start_epoch
    )
    return target_pane, f"Started Codex in {target_pane}.", meta


def managed_codex_agent_present(target_pane: str) -> bool:
    if _processes.registered_codex_process_running(target_pane):
        return True
    if not _processes.tmux_target_exists(target_pane):
        return False
    return _processes.tmux_pane_has_codex_process(
        target_pane
    ) or _processes.tmux_pane_has_codex_supervisor(target_pane)


def stop_codex_agent(
    session: str,
    window: str,
) -> tuple[str, dict[str, Any] | None]:
    target_pane = f"{session}:{window}.0"
    if not _processes.tmux_window_exists(session, window):
        return f"Codex agent is already stopped; {target_pane} does not exist.", None

    current_command = _processes.tmux_pane_command(target_pane)
    managed = _processes.tmux_pane_has_codex_process(
        target_pane
    ) or _processes.tmux_pane_has_codex_supervisor(target_pane)
    if not managed:
        if current_command in _settings.SHELL_COMMANDS:
            return (
                f"Codex agent is already stopped; {target_pane} contains only a shell.",
                None,
            )
        raise RuntimeError(
            f"refusing to kill {target_pane}: it is occupied by "
            f"{current_command or 'an unknown non-Codex process'}"
        )

    meta = agent_registry.active_agent_for_pane(target_pane)
    if meta:
        agent_registry.append_agent_event(
            meta,
            {
                "event": "telegram_agent_stop_requested",
                "target_pane": target_pane,
            },
        )
    subprocess.run(
        ["tmux", "kill-window", "-t", f"{session}:{window}"],
        check=True,
        timeout=_settings.TMUX_MUTATION_TIMEOUT_SECONDS,
    )
    _settings.REGISTERED_CODEX_PID_CACHE.clear()
    return f"Stopped the Codex agent in {target_pane}.", meta


def ensure_codex_target_for_agent_message(
    args: argparse.Namespace, record: dict[str, Any]
) -> str | None:
    """Ensure normal Telegram text has a live Codex pane before relay.

    Returns a user-facing failure string when the target is busy with a
    non-Codex process that should not receive pasted text.
    """
    if args.relay_mode == "log":
        return None
    if agent_desired_state(args) == _settings.AGENT_DESIRED_STOPPED:
        return (
            "not relayed: the Telegram Codex agent is stopped. Use /start_agent first."
        )
    if _processes.registered_codex_process_running(args.target_pane):
        return None

    if _processes.tmux_target_exists(args.target_pane):
        current_command = _processes.tmux_pane_command(args.target_pane)
        if _processes.tmux_pane_has_codex_process(args.target_pane):
            return None
        if _processes.tmux_pane_has_codex_supervisor(args.target_pane):
            if _processes.wait_for_supervised_codex(
                args.target_pane, args.agent_recovery_wait
            ):
                record["waited_for_supervisor_recovery"] = True
                return None
            return (
                f"not relayed: the supervised Codex agent in {args.target_pane} did not recover "
                f"within {args.agent_recovery_wait:g}s. Use /restart_agent."
            )
        if current_command not in _settings.SHELL_COMMANDS:
            return (
                f"not relayed: target {args.target_pane} is busy with {current_command or 'unknown'}. "
                "Use /restart_agent to interrupt it."
            )
        record["auto_start_agent_reason"] = f"target_shell:{current_command}"
    else:
        record["auto_start_agent_reason"] = "target_missing"

    target_pane, result, meta = start_codex_agent(
        repo_root=args.repo_root,
        session=args.session,
        window=args.codex_window,
        restart=False,
    )
    args.target_pane = target_pane
    record["auto_start_agent_result"] = result
    record["target_pane"] = target_pane
    if meta:
        record["agent_id"] = meta.get("agent_id")
        record["agent_jsonl"] = meta.get("agent_jsonl")
        record["codex_session_path"] = meta.get("codex_session_path")
        agent_registry.append_agent_event(
            meta,
            {
                "event": "telegram_auto_start_for_agent_message",
                "message_id": record.get("message_id"),
                "reason": record.get("auto_start_agent_reason"),
                "result": result,
                "update_id": record.get("update_id"),
            },
        )
    return None


def maintain_managed_codex_agent(
    args: argparse.Namespace, log_path: Path
) -> str | None:
    managed_target = f"{args.session}:{args.codex_window}.0"
    if (
        not args.agent_watchdog
        or args.relay_mode == "log"
        or args.target_pane != managed_target
    ):
        return None
    if agent_lifecycle_operation_in_progress(args):
        _state.append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": "codex_watchdog_lifecycle_change_deferred",
                "target_pane": managed_target,
            },
        )
        return None
    if agent_desired_state(args) == _settings.AGENT_DESIRED_STOPPED:
        return None
    if _processes.registered_codex_process_running(managed_target):
        return None
    try:
        if _processes.tmux_target_exists(managed_target):
            current_command = _processes.tmux_pane_command(managed_target)
            if _processes.tmux_pane_has_codex_process(
                managed_target
            ) or _processes.tmux_pane_has_codex_supervisor(managed_target):
                return None
            if current_command not in _settings.SHELL_COMMANDS:
                return None
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not turn a transient tmux timeout into either a listener crash or
        # a false "agent missing" decision that could interrupt a live agent.
        _state.append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": "codex_watchdog_tmux_probe_deferred",
                "target_pane": managed_target,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )
        return None

    now = time.time()
    if now < float(getattr(args, "agent_watchdog_retry_after", 0.0)):
        return None
    args.agent_watchdog_retry_after = now + 30.0
    try:
        target_pane, result, meta = start_codex_agent(
            repo_root=args.repo_root,
            session=args.session,
            window=args.codex_window,
            restart=False,
        )
        args.target_pane = target_pane
        record = {
            "ts": int(time.time()),
            "event": "codex_watchdog_restarted_agent",
            "target_pane": target_pane,
            "result": result,
            "agent_id": meta.get("agent_id") if meta else None,
            "codex_session_path": meta.get("codex_session_path") if meta else None,
        }
        _state.append_jsonl(log_path, record)
        if meta:
            agent_registry.append_agent_event(meta, record)
        args.agent_watchdog_retry_after = 0.0
        return "Codex agent was not running; the watchdog restarted it automatically."
    except Exception as exc:
        _state.append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": "codex_watchdog_restart_failed",
                "target_pane": managed_target,
                "error": str(exc)[:500],
            },
        )
        return None
