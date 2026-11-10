"""The direct host's pane: runs one agent runner and stands in for tmux.

It keeps the runner alive like `codex_agent_supervisor.sh`, forwards the
listener's keystrokes to the runner's standard input, and saves the runner's
output for `/status`. See `teleagent.host` for the files it shares.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import host, portable

# The runner ended on purpose (0) or reported that its CLI is unavailable (2).
FINAL_EXIT_CODES = {0, 2}
HEALTHY_SECONDS = 60
MAX_QUICK_FAILURES = 5


def event_bytes(event: dict) -> bytes:
    """Translate one listener input event into terminal bytes."""
    if isinstance(event.get("paste"), str):
        return host.PASTE_START + event["paste"].encode("utf-8") + host.PASTE_END
    if isinstance(event.get("text"), str):
        return event["text"].encode("utf-8")
    keys = event.get("keys")
    if isinstance(keys, list):
        return b"".join(host.KEY_BYTES.get(str(key), b"") for key in keys)
    return b""


class InputReader:
    """Reads complete lines appended to input.jsonl after the pane started."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.touch()
        self.offset = path.stat().st_size  # Input meant for an earlier pane never replays.
        self.partial = b""

    def read(self) -> list[dict]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return []
        self.offset += len(data)
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events


def desired_running(log_dir: str) -> bool:
    """Crash recovery honours /kill_agent and maintenance stops."""
    try:
        state = json.loads(
            (Path(log_dir) / "telegram_agent_lifecycle.state.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return True
    return not isinstance(state, dict) or state.get("desired", "running") == "running"


def run(directory: Path, target: str, comm: str, cwd: str, command: list[str]) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(directory / "pane.lock", os.O_RDWR | os.O_CREAT, 0o600)
    if not portable.lock_file(lock_fd, blocking=False):
        os.close(lock_fd)
        print(f"another pane already owns {target}", flush=True)
        return 3
    record = {"pane_pid": os.getpid(), "target": target, "process_comm": comm,
              "started_ts": time.time(), "child_pid": None, "command": command[:1]}
    reader = InputReader(directory / "input.jsonl")
    stop_path = directory / "stop"
    env = dict(os.environ, TELEAGENT_HOST_PANE_DIR=str(directory), PYTHONUNBUFFERED="1")
    failures = 0
    exit_code = 0
    with open(directory / "screen.log", "ab", buffering=0) as screen:
        while True:
            started = time.time()
            (directory / "session.json").unlink(missing_ok=True)
            child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=screen, stderr=subprocess.STDOUT)
            record.update(child_pid=child.pid, child_started_ts=started)
            host.write_json(directory / "pane.json", record)
            stopping = False
            while child.poll() is None:
                if stop_path.exists():
                    stopping = True
                    portable.stop_process(child)
                    try:
                        child.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        portable.terminate_tree(child.pid)
                        child.wait()
                    break
                try:
                    for event in reader.read():
                        child.stdin.write(event_bytes(event))
                    child.stdin.flush()
                except OSError:
                    pass  # The runner is exiting; the poll above handles it.
                time.sleep(0.05)
            exit_code = child.returncode
            record.update(child_pid=None)
            host.write_json(directory / "pane.json", record)
            if stopping or exit_code in FINAL_EXIT_CODES:
                break
            failures = 1 if time.time() - started >= HEALTHY_SECONDS else failures + 1
            if failures > MAX_QUICK_FAILURES or not desired_running(
                os.environ.get("TELEAGENT_LOG_DIR", "")
            ):
                break
            screen.write(f"[pane] runner exited {exit_code}; restarting\n".encode())
            time.sleep(min(60, 2 ** failures))
    stop_path.unlink(missing_ok=True)
    portable.unlock_file(lock_fd)
    os.close(lock_fd)
    return exit_code if isinstance(exit_code, int) and exit_code >= 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--comm", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a runner command is required after --")
    return run(args.dir, args.target, args.comm, args.cwd, command)


if __name__ == "__main__":
    sys.exit(main())
