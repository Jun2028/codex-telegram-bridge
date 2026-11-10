#!/usr/bin/env python3
"""Run the Telegram relay on any operating system, without bash or tmux.

    python scripts/relay.py start     start whatever is not running, in the background
    python scripts/relay.py stop      stop the listener, its agent and the file browser
    python scripts/relay.py status    show what is running
    python scripts/relay.py run       run the listener in this terminal
    python scripts/relay.py autostart on|off|status
                                      Windows: start at sign-in and restart if stopped

`start` is safe to repeat: it only starts what is missing, which is how the
autostart task keeps the bot up. Use --instance NAME, or TELEAGENT_INSTANCE, to
choose a named bot. On Linux with tmux the bash
launchers (`start_telegram_inbox.sh`, `start_codex_agent.sh`) remain the way
to run the Codex TUI; this launcher uses the direct host, where the relay
manages the agent process itself.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from teleagent import environment, harnesses, host, portable  # noqa: E402

QUICK_EXIT_SECONDS = 300
FILE_BROWSER = Path("examples") / "telegram-file-browser"
TUNNEL_ADDRESS = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


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
        portable.python_executable(), str(SCRIPTS / "telegram_inbox.py"),
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


def file_browser_targets(env: dict[str, str]) -> tuple[str, str]:
    session = env["TELEAGENT_TMUX_SESSION"]
    return f"{session}:files.0", f"{session}:files-tunnel.0"


def ensure_file_browser(env: dict[str, str]) -> None:
    """Keep the file browser, its tunnel and its Telegram button current.

    Opt in with TELEAGENT_FILES_TUNNEL=cloudflare. A Cloudflare quick tunnel is
    issued a new address every time it starts, so the Files button is
    registered again whenever the address changes.
    """
    if env.get("TELEAGENT_FILES_TUNNEL") != "cloudflare":
        return
    node, cloudflared = shutil.which("node"), shutil.which("cloudflared")
    if not node or not cloudflared:
        print("File browser not started: it needs node and cloudflared on PATH.")
        return
    repo = Path(env["TELEAGENT_REPO"])
    demo = repo / FILE_BROWSER
    service, tunnel = file_browser_targets(env)
    port = env.get("DEMO_PORT", "8766")
    if host.record(service) is None:
        settings = ("TELEAGENT_SECRET_ENV", "TELEAGENT_FILES_ROOT", "TELEAGENT_FILES_WRITABLE",
                    "TELEAGENT_OWNER_USER_ID", "DEMO_PORT")
        host.start(service, [node, str(demo / "server.cjs")], cwd=repo, process_comm="files",
                   env={name: env[name] for name in settings if env.get(name)})
    tunnel_log = host.pane_dir(tunnel) / "screen.log"
    if host.record(tunnel) is None:
        tunnel_log.unlink(missing_ok=True)  # Only this run's address must be found below.
        host.start(tunnel, [cloudflared, "tunnel", "--no-autoupdate", "--url",
                            f"http://127.0.0.1:{port}"], cwd=repo, env={},
                   process_comm="files-tunnel")
    origin = ""
    deadline = time.monotonic() + 90
    while not origin and time.monotonic() < deadline:
        try:
            found = TUNNEL_ADDRESS.findall(tunnel_log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            found = []
        origin = found[-1] if found else ""
        if not origin:
            time.sleep(1)
    if not origin:
        print("File browser: the tunnel has not supplied an address yet; `start` will retry.")
        return
    registered = demo / ".runtime" / "public-origin.txt"
    try:
        current = registered.read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    if current == origin:
        return
    # The connector loads the bot token itself from the secret file; the
    # address goes only to the owner's private chat.
    result = subprocess.run(
        [portable.python_executable(), str(demo / "connect-menu.py"), "--repo", str(repo),
         "--secret-env", env["TELEAGENT_SECRET_ENV"], "--origin", origin],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        **portable.hidden_window_options())
    print("File browser: Files button updated." if result.returncode == 0
          else "File browser: could not register the Files button; `start` will retry.")


def start(env: dict[str, str]) -> int:
    os.environ.update(TELEAGENT_HOST=host.DIRECT, TELEAGENT_LOG_DIR=env["TELEAGENT_LOG_DIR"])
    result = start_listener(env)
    ensure_file_browser(env)
    return result


def start_listener(env: dict[str, str]) -> int:
    if relay_running(env):
        print("The relay is already running for this bot.")
        return 0
    log_dir = Path(env["TELEAGENT_LOG_DIR"])
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "relay.out", "ab") as output:
        subprocess.Popen([portable.python_executable(), str(Path(__file__).resolve()), "run"],
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
    for target in file_browser_targets(env):
        host.stop(target)
    print("Relay stopped." if was_running or agent_stopped else "The relay was not running.")
    return 0


def status(env: dict[str, str]) -> int:
    os.environ.update(TELEAGENT_HOST=host.DIRECT, TELEAGENT_LOG_DIR=env["TELEAGENT_LOG_DIR"])
    agent = host.record(agent_target(env))
    print(f"bot: {env['TELEAGENT_INSTANCE']}")
    print(f"listener: {'running' if relay_running(env) else 'stopped'}")
    running = harnesses.for_process_comm(str(agent.get("process_comm"))) if agent else None
    print(f"agent: {running.label if running else 'running' if agent else 'stopped'}")
    if env.get("TELEAGENT_FILES_TUNNEL") == "cloudflare":
        service, tunnel = (host.record(target) is not None for target in file_browser_targets(env))
        print(f"file browser: {'running' if service else 'stopped'}, "
              f"tunnel {'running' if tunnel else 'stopped'}")
    print(f"logs: {env['TELEAGENT_LOG_DIR']}")
    return 0


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Keeps the tele-agent Telegram relay running.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>
    <TimeTrigger>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary><Enabled>true</Enabled>
      <Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled><Hidden>false</Hidden>
  </Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>
"""


def autostart(env: dict[str, str], action: str) -> int:
    """Windows: a Task Scheduler entry that runs `start` at sign-in and every five minutes.

    This is what the cron watchdog is on Linux. It also makes the bot
    independent of whichever terminal or program first started it.
    """
    if not portable.WINDOWS:
        print("On Linux the cron watchdog does this: scripts/ensure_telegram_relay.sh.")
        return 2
    name = f"tele-agent-{env['TELEAGENT_INSTANCE']}"
    if action == "status":
        found = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True).returncode == 0
        print(f"autostart: {'on' if found else 'off'} (task {name})")
        return 0
    if action == "off":
        subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True)
        print(f"autostart: off (task {name} removed)")
        return 0
    windowless = Path(sys.executable).with_name("pythonw.exe")
    interpreter = windowless if windowless.exists() else Path(sys.executable)
    arguments = f'"{Path(__file__).resolve()}" --instance {env["TELEAGENT_INSTANCE"]} start'
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
    xml = TASK_XML.format(user=escape(user), command=escape(str(interpreter)), arguments=escape(arguments))
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as handle:
        handle.write(xml)
    try:
        created = subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", handle.name, "/F"],
                                 capture_output=True, text=True)
    finally:
        os.unlink(handle.name)
    if created.returncode != 0:
        print("autostart: Task Scheduler refused the task: " + (created.stderr or created.stdout).strip()[:300])
        return 1
    subprocess.run(["schtasks", "/Run", "/TN", name], capture_output=True)
    print(f"autostart: on (task {name}; runs at sign-in and every five minutes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", help="named bot; the same as TELEAGENT_INSTANCE")
    parser.add_argument("action", choices=["start", "stop", "status", "run", "autostart"])
    parser.add_argument("option", nargs="?", choices=["on", "off", "status"],
                        help="for autostart")
    args = parser.parse_args(argv)
    if args.instance:
        os.environ["TELEAGENT_INSTANCE"] = args.instance
    try:
        env = relay_environment()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2
    if args.action == "autostart":
        return autostart(env, args.option or "status")
    return {"start": start, "stop": stop, "status": status, "run": supervise}[args.action](env)


if __name__ == "__main__":
    raise SystemExit(main())
