"""Private, atomic state files and locks shared by relay services.

Read/modify/write operations take ``locked(path)``. Atomic replacement protects
concurrent readers from partial writes; JSON remains inspectable by operators.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any
from notify import assert_safe_local_path

_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_held = threading.local()


@contextmanager
def locked(path: Path):
    key = str(path.absolute())
    with _locks_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_held, "paths", set())
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _held.paths = held | {key}
            yield
        finally:
            _held.paths = held
            os.close(fd)


def serialized(function):
    """Serialize a mutation whose first argument is its state path."""

    @wraps(function)
    def wrapped(path, *args, **kwargs):
        with locked(Path(path)):
            return function(path, *args, **kwargs)

    return wrapped


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json_object(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(value, sort_keys=True) + "\n")


def state_path(repo_root: Path, rel_or_abs: str) -> Path:
    path = Path(rel_or_abs)
    if not path.is_absolute():
        path = repo_root / path
    assert_safe_local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_offset(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_offset(path: Path, offset: int) -> None:
    atomic_write(path, f"{offset}\n")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with locked(path):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
