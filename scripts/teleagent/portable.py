"""Operating-system differences, kept in one place.

Everything else in the relay calls these instead of `fcntl`, `/proc` or
signals directly, so the same code runs on Linux, macOS and Windows.
Standard library only: this module is imported before anything else.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"

if WINDOWS:
    import ctypes
    import msvcrt
else:
    import fcntl
    import signal


def lock_file(fd: int, *, blocking: bool = True) -> bool:
    """Take an exclusive advisory lock on an open file; False if it is held."""
    if not WINDOWS:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        return True
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if not blocking:
                return False
            time.sleep(0.05)


def unlock_file(fd: int) -> None:
    if not WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass  # Closing the descriptor releases the lock as well.


def sync_directory(path: os.PathLike[str] | str) -> None:
    """Persist a rename. Windows cannot open a directory for fsync and does not need it."""
    if WINDOWS:
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if WINDOWS:
        # os.kill(pid, 0) would send a console control event on Windows.
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            alive = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(alive) and code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def terminate_tree(pid: int) -> None:
    """Stop a process the relay started, with its children."""
    if pid <= 0:
        return
    if WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            timeout=20,
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def stop_process(process: subprocess.Popen) -> None:
    """Ask a child the caller started to end.

    Windows CLIs are often a wrapper script around the real program, and
    ending the wrapper leaves the program running, so the whole tree goes.
    """
    if process.poll() is not None:
        return
    if WINDOWS:
        terminate_tree(process.pid)
    else:
        process.terminate()


def python_executable() -> str:
    """The console interpreter, even when this process runs under the windowless one.

    A process without a console gives every console program it starts a new,
    visible terminal. Long-lived relay processes therefore run under the
    console interpreter with a hidden console, which their children inherit.
    """
    executable = Path(sys.executable)
    if WINDOWS and executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.exists():
            return str(console)
    return sys.executable


def hidden_window_options() -> dict:
    """`subprocess` options that keep a short-lived helper from flashing a terminal."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


def detached_process_options() -> dict:
    """`subprocess.Popen` options for a process that outlives its starter."""
    if WINDOWS:
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        }
    return {"start_new_session": True}
