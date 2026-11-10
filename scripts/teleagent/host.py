"""Where the agent runs: a tmux pane, or a process the relay manages itself.

On Linux and macOS with tmux installed the agent lives in a tmux pane, which
is required for the Codex TUI. Everywhere else, including Windows, the relay
uses the *direct* host: `teleagent.direct_pane` owns the agent runner, feeds it
the same keystrokes tmux would, and records what tmux and `/proc` would have
answered. Set `TELEAGENT_HOST=tmux|direct` to choose explicitly.

Each direct pane keeps its files in `$TELEAGENT_LOG_DIR/host/<target>/`:

  pane.lock   held for the pane's whole life; a free lock means it is gone
  pane.json   pane and runner process ids and the harness process name
  input.jsonl keystrokes and pastes from the listener
  screen.log  the runner's terminal output
  session.json the session log the runner opened
  stop        asks the pane to end

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import portable

TMUX = "tmux"
DIRECT = "direct"
PANE_PROCESS_NAME = "teleagent-pane"
# Keys the listener sends, as the bytes a terminal would deliver to the runner.
KEY_BYTES = {"Enter": b"\n", "C-m": b"\n", "C-u": b"\x15", "Escape": b"\x1b"}
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"


def kind() -> str:
    configured = os.environ.get("TELEAGENT_HOST", "").strip().lower()
    if configured in {TMUX, DIRECT}:
        return configured
    return TMUX if not portable.WINDOWS and shutil.which("tmux") else DIRECT


def is_direct() -> bool:
    return kind() == DIRECT


def runtime_root() -> Path:
    log_dir = os.environ.get("TELEAGENT_LOG_DIR") or str(
        Path.home() / ".local" / "share" / "tele-agent" / "runtime"
    )
    return Path(log_dir) / "host"


def pane_dir(target: str) -> Path:
    return runtime_root() / re.sub(r"[^A-Za-z0-9_.-]", "_", target)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _pane_alive(directory: Path) -> bool:
    """A pane holds its lock for as long as it runs, so a free lock means it ended."""
    try:
        fd = os.open(directory / "pane.lock", os.O_RDWR)
    except OSError:
        return False
    try:
        if portable.lock_file(fd, blocking=False):
            portable.unlock_file(fd)
            return False
        return True
    finally:
        os.close(fd)


def _record_in(directory: Path) -> dict | None:
    if not _pane_alive(directory):
        return None
    record = _read_json(directory / "pane.json")
    if not isinstance(record.get("pane_pid"), int):
        return None
    record["dir"] = str(directory)
    return record


def record(target: str) -> dict | None:
    """Return the live pane for a target, or None when nothing runs there."""
    return _record_in(pane_dir(target))


def all_records() -> list[dict]:
    try:
        directories = [path for path in runtime_root().iterdir() if path.is_dir()]
    except OSError:
        return []
    return [found for found in map(_record_in, directories) if found]


def process_rows() -> list[tuple[int, int, str]]:
    """The `ps` rows for managed panes: (pid, parent pid, process name)."""
    rows: list[tuple[int, int, str]] = []
    for found in all_records():
        rows.append((found["pane_pid"], 0, PANE_PROCESS_NAME))
        child = found.get("child_pid")
        if isinstance(child, int) and portable.pid_alive(child):
            rows.append((child, found["pane_pid"], str(found.get("process_comm") or "")))
    return rows


def _record_for_pid(pid: int) -> dict | None:
    for found in all_records():
        if pid in (found.get("pane_pid"), found.get("child_pid")):
            return found
    return None


def process_comm(pid: int) -> str:
    """The process name `/proc/<pid>/comm` would report; raises OSError if unknown."""
    for row_pid, _parent, name in process_rows():
        if row_pid == pid:
            return name
    raise ProcessLookupError(pid)


def process_start(pid: int) -> str:
    """A value that changes when the pid is reused by a new runner."""
    found = _record_for_pid(pid)
    if found is None:
        raise ProcessLookupError(pid)
    return str(found.get("child_started_ts") or found.get("started_ts") or "")


def session_files(pid: int) -> list[Path]:
    """The session log the runner with this pid opened."""
    found = _record_for_pid(pid)
    if found is None or found.get("child_pid") != pid:
        return []
    # The pane clears session.json before each runner start, so what is there
    # belongs to the current runner even when a launcher shim changed its pid.
    announced = _read_json(Path(found["dir"]) / "session.json")
    path = Path(str(announced.get("path") or ""))
    return [path] if path.is_file() else []


def announce_session(path: Path) -> None:
    """Called by a runner: tell the listener which session log is open."""
    directory = os.environ.get("TELEAGENT_HOST_PANE_DIR")
    if directory:
        write_json(Path(directory) / "session.json", {"path": str(path), "pid": os.getpid()})


def _append_input(target: str, event: dict) -> None:
    directory = pane_dir(target)
    if record(target) is None:
        raise subprocess.SubprocessError(f"no managed agent at {target}")
    line = (json.dumps(event) + "\n").encode("utf-8")
    fd = os.open(directory / "input.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def send_keys(target: str, *keys: str, literal: bool = False) -> None:
    if literal:
        _append_input(target, {"text": "".join(keys)})
        return
    unknown = [key for key in keys if key not in KEY_BYTES]
    if unknown:
        raise subprocess.SubprocessError(f"unsupported key for a direct agent: {unknown[0]}")
    _append_input(target, {"keys": list(keys)})


def paste(target: str, text: str) -> None:
    _append_input(target, {"paste": text})


def tail(target: str, lines: int = 80) -> str:
    try:
        data = (pane_dir(target) / "screen.log").read_bytes()[-65536:]
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])


def start(
    target: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    process_comm: str,
    wait_seconds: float = 15.0,
) -> None:
    """Start a pane that runs and supervises `command`; return once it is live."""
    directory = pane_dir(target)
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("stop", "session.json", "pane.json"):
        (directory / name).unlink(missing_ok=True)
    scripts = Path(__file__).resolve().parents[1]
    pane_env = dict(os.environ)
    pane_env.update(env)
    pane_env["PYTHONPATH"] = str(scripts) + os.pathsep + pane_env.get("PYTHONPATH", "")
    pane_env["PYTHONUTF8"] = "1"  # Session logs and prompts are UTF-8 on every OS.
    with open(directory / "pane.out", "ab") as output:
        pane = subprocess.Popen(
            [sys.executable, "-m", "teleagent.direct_pane", "--dir", str(directory),
             "--target", target, "--comm", process_comm, "--cwd", str(cwd), "--", *command],
            cwd=str(cwd),
            env=pane_env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **portable.detached_process_options(),
        )
    # Reap the pane when it ends; it deliberately outlives this call.
    threading.Thread(target=pane.wait, daemon=True).start()
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if record(target) is not None:
            return
        time.sleep(0.1)
    raise subprocess.SubprocessError(f"the managed agent at {target} did not start")


def stop(target: str, wait_seconds: float = 10.0) -> bool:
    """End a pane and its runner. Returns False when nothing was running."""
    found = record(target)
    if found is None:
        return False
    directory = Path(found["dir"])
    (directory / "stop").write_text("stop", encoding="utf-8")
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not _pane_alive(directory):
            return True
        time.sleep(0.1)
    for key in ("child_pid", "pane_pid"):
        if isinstance(found.get(key), int):
            portable.terminate_tree(found[key])
    return True
