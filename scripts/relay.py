#!/usr/bin/env python3
"""Run the Telegram relay on any operating system, without bash or tmux.

    python scripts/relay.py start     start the listener in the background
    python scripts/relay.py stop      stop the listener and its agent
    python scripts/relay.py status    show whether they are running
    python scripts/relay.py run       run the listener in this terminal

Set TELEAGENT_INSTANCE to choose a named bot. On Linux with tmux the bash
launchers (`start_telegram_inbox.sh`, `start_codex_agent.sh`) remain the way
to run the Codex TUI; this launcher uses the direct host, where the relay
manages the agent process itself.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from teleagent import environment, host, portable  # noqa: E402

QUICK_EXIT_SECONDS = 300


def relay_environment() -> dict[str, str]:
    env = environment.build(SCRIPTS.parent)
    env["TELEAGENT_HOST"] = host.DIRECT
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(SCRIPTS) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def agent_target(env: dict[str, str]) -> str:
    return f"{env['TELEAGENT_TMUX_SESSION']}:{env.get('TELEAGENT_CODEX_WINDOW', 'codex')}.0"


def listener_command(env: dict[str, str]) -> list[str]:
    command = [
        sys.executable, str(SCRIPTS / "telegram_inbox.py"),
        "--session", env["TELEAGENT_TMUX_SESSION"],
        "--target-pane", agent_target(env),
        "--codex-window", env.get("TELEAGENT_CODEX_WINDOW", "codex"),
        "--relay-mode", env.get("TELEAGENT_INBOX_MODE", "tmux-enter"),
        "--submit-delay", env.get("TELEAGENT_SUBMIT_DELAY", "2.0"),
        "--secret-env", env["TELEAGENT_SECRET_ENV"],
    ]
    for option, name in (("--max-agent-message-chars", "TELEAGENT_MAX_AGENT_MESSAGE_CHARS"),
                         ("--max-final-message-chars", "TELEAGENT_MAX_FINAL_MESSAGE_CHARS")):
        if env.get(name):
            command += [option, env[name]]
    return command


def supervise(env: dict[str, str]) -> int:
    """Keep the listener running, as telegram_inbox_supervisor.sh does under tmux."""
    log_dir = Path(env["TELEAGENT_LOG_DIR"])
    log_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(log_dir / "relay.lock", os.O_RDWR | os.O_CREAT, 0o600)
    if not portable.lock_file(lock_fd, blocking=False):
        print("The relay is already running for this bot.")
        return 3
    pid_file, stop_file = log_dir / "telegram_inbox.pid", log_dir / "relay.stop"
    stop_file.unlink(missing_ok=True)
    (log_dir / "relay.pid").write_text(str(os.getpid()), encoding="utf-8")
    failures = 0
    try:
        with open(log_dir / "telegram_inbox.supervisor.log", "ab", buffering=0) as log:
            while not stop_file.exists():
                started = time.monotonic()
                log.write(time.strftime("[%Y-%m-%dT%H:%M:%S%z] starting telegram inbox\n").encode())
                child = subprocess.Popen(listener_command(env), cwd=env["TELEAGENT_REPO"], env=env,
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                pid_file.write_text(f"{child.pid}\n", encoding="utf-8")
                while child.poll() is None:
                    if stop_file.exists():
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            portable.terminate_tree(child.pid)
                            child.wait()
                    time.sleep(0.5)
                pid_file.unlink(missing_ok=True)
                if stop_file.exists():
                    break
                failures = 1 if time.monotonic() - started >= QUICK_EXIT_SECONDS else failures + 1
                delay = min(60, 5 * 2 ** min(failures - 1, 4))
                log.write(time.strftime(
                    f"[%Y-%m-%dT%H:%M:%S%z] listener exited rc={child.returncode}; "
                    f"restarting in {delay}s\n").encode())
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not stop_file.exists():
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for path in (pid_file, stop_file, log_dir / "relay.pid"):
            path.unlink(missing_ok=True)
        portable.unlock_file(lock_fd)
        os.close(lock_fd)
    return 0


def relay_running(env: dict[str, str]) -> bool:
    try:
        fd = os.open(Path(env["TELEAGENT_LOG_DIR"]) / "relay.lock", os.O_RDWR)
    except OSError:
        return False
    try:
        if portable.lock_file(fd, blocking=False):
            portable.unlock_file(fd)
            return False
        return True
    finally:
        os.close(fd)


def start(env: dict[str, str]) -> int:
    if relay_running(env):
        print("The relay is already running for this bot.")
        return 0
    log_dir = Path(env["TELEAGENT_LOG_DIR"])
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "relay.out", "ab") as output:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run"],
                         cwd=env["TELEAGENT_REPO"], env=env, stdin=subprocess.DEVNULL,
                         stdout=output, stderr=subprocess.STDOUT, close_fds=True,
                         **portable.detached_process_options())
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if (log_dir / "telegram_inbox.pid").exists():
            print(f"Relay started for bot '{env['TELEAGENT_INSTANCE']}'. Logs: {log_dir}")
            return 0
        time.sleep(0.2)
    print(f"The relay did not start; see {log_dir / 'telegram_inbox.supervisor.log'}")
    return 1


def stop(env: dict[str, str]) -> int:
    log_dir = Path(env["TELEAGENT_LOG_DIR"])
    os.environ.update(TELEAGENT_HOST=host.DIRECT, TELEAGENT_LOG_DIR=str(log_dir))
    was_running = relay_running(env)
    if was_running:
        (log_dir / "relay.stop").write_text("stop", encoding="utf-8")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and relay_running(env):
            time.sleep(0.2)
    agent_stopped = host.stop(agent_target(env))
    print("Relay stopped." if was_running or agent_stopped else "The relay was not running.")
    return 0


def status(env: dict[str, str]) -> int:
    os.environ.update(TELEAGENT_HOST=host.DIRECT, TELEAGENT_LOG_DIR=env["TELEAGENT_LOG_DIR"])
    agent = host.record(agent_target(env))
    print(f"bot: {env['TELEAGENT_INSTANCE']}")
    print(f"listener: {'running' if relay_running(env) else 'stopped'}")
    print(f"agent: {agent.get('process_comm') if agent else 'stopped'}")
    print(f"logs: {env['TELEAGENT_LOG_DIR']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["start", "stop", "status", "run"])
    args = parser.parse_args(argv)
    try:
        env = relay_environment()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2
    return {"start": start, "stop": stop, "status": status, "run": supervise}[args.action](env)


if __name__ == "__main__":
    raise SystemExit(main())
