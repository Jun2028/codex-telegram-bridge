#!/usr/bin/env python3
"""Drive Claude's own browser login; publish safe progress, never credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import subprocess
import termios
import time

from teleagent import claude_auth as auth


def run(path: Path, attempt: str, binary: str, timeout: float = 600) -> int:
    if not auth.update_attempt(path, attempt, worker_pid=os.getpid()):
        return 2
    master, slave = pty.openpty()
    mode = termios.tcgetattr(slave)
    mode[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, mode)
    try:
        process = subprocess.Popen([binary, "auth", "login"], stdin=slave, stdout=slave, stderr=slave,
                                   env=dict(os.environ, BROWSER="true"))
    except OSError:
        os.close(slave)
        os.close(master)
        auth.update_attempt(path, attempt, phase="failed", verification_url=None)
        return 1
    os.close(slave)
    def interrupted(*_):
        raise InterruptedError()
    signal.signal(signal.SIGTERM, interrupted)
    output = ""
    published = False
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([master], [], [], .2)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    process.wait(timeout=5)
                    break
                if not chunk:
                    process.wait(timeout=5)
                    break
                output = (output + chunk.decode(errors="replace"))[-16000:]
                if not published:
                    for url in re.findall(r"https://[^\s\x1b\x07]+", output):
                        if auth.validated_url(url):
                            if not auth.update_attempt(path, attempt, phase="awaiting_code", verification_url=url):
                                return 2
                            published = True
                            break
            code_file = auth.code_path(path, attempt)
            try:
                code = code_file.read_text().strip()
            except FileNotFoundError:
                continue
            code_file.unlink()
            if auth.CODE.fullmatch(code):
                os.write(master, (code + "\n").encode())
        if process.poll() is not None and process.returncode == 0:
            status = subprocess.run([binary, "auth", "status"], capture_output=True, timeout=20)
            if status.returncode == 0 and json.loads(status.stdout).get("loggedIn"):
                auth.update_attempt(path, attempt, phase="authenticated", verification_url=None)
                return 0
        auth.update_attempt(path, attempt, phase="failed", verification_url=None)
        return 1
    except InterruptedError:
        return 2
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        auth.update_attempt(path, attempt, phase="failed", verification_url=None)
        return 1
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        os.close(master)
        auth.code_path(path, attempt).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--binary", required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.state, args.attempt, args.binary))
