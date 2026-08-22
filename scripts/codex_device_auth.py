#!/usr/bin/env python3
"""Run Codex device authentication and publish only safe progress state."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
DEVICE_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device\b")
DEVICE_CODE_RE = re.compile(r"(?<![A-Z0-9])[A-Z0-9]{4}-[A-Z0-9]{4,8}(?![A-Z0-9])")
SUCCESS_TEXT = "Successfully logged in"


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_attempt_state(path: Path, attempt_id: str, updates: dict[str, Any]) -> bool:
    """Merge updates only while this worker still owns the active attempt."""
    state = read_state(path)
    if state.get("attempt_id") != attempt_id:
        return False
    state.update(updates)
    state["updated_ts"] = int(time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)
    return True


def clean_output_line(line: str) -> str:
    return " ".join(ANSI_RE.sub("", line).strip().split())


def safe_failure_detail(lines: list[str], verification_url: str, user_code: str) -> str:
    safe_lines: list[str] = []
    for line in lines:
        cleaned = clean_output_line(line)
        if not cleaned or SUCCESS_TEXT in cleaned:
            continue
        if verification_url:
            cleaned = cleaned.replace(verification_url, "[device URL]")
        if user_code:
            cleaned = cleaned.replace(user_code, "[device code]")
        if cleaned.startswith("Welcome to Codex") or cleaned.startswith("OpenAI's command-line"):
            continue
        safe_lines.append(cleaned)
    detail = " ".join(safe_lines[-4:]).strip()
    return detail[:500] or "Codex device authentication exited before completing."


def run_device_auth(state_path: Path, attempt_id: str, codex_bin: str) -> int:
    if not write_attempt_state(
        state_path,
        attempt_id,
        {"phase": "requesting_code", "worker_pid": os.getpid()},
    ):
        return 2

    try:
        process = subprocess.Popen(
            [codex_bin, "login", "--device-auth"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        write_attempt_state(
            state_path,
            attempt_id,
            {
                "phase": "failed",
                "failed_ts": int(time.time()),
                "error": f"Could not start Codex device authentication: {str(exc)[:300]}",
            },
        )
        return 1

    write_attempt_state(state_path, attempt_id, {"codex_login_pid": process.pid})
    output_lines: list[str] = []
    verification_url = ""
    user_code = ""
    assert process.stdout is not None
    for line in process.stdout:
        output_lines.append(line)
        cleaned = clean_output_line(line)
        url_match = DEVICE_URL_RE.search(cleaned)
        if url_match:
            verification_url = url_match.group(0)
        code_match = DEVICE_CODE_RE.search(cleaned)
        if code_match:
            user_code = code_match.group(0)
        if verification_url and user_code:
            if not write_attempt_state(
                state_path,
                attempt_id,
                {
                    "phase": "awaiting_user",
                    "verification_url": verification_url,
                    "user_code": user_code,
                    "code_expires_ts": int(time.time()) + 15 * 60,
                },
            ):
                process.terminate()
                process.wait(timeout=10)
                return 2

    returncode = process.wait()
    combined = "".join(output_lines)
    if returncode == 0 and SUCCESS_TEXT in clean_output_line(combined):
        write_attempt_state(
            state_path,
            attempt_id,
            {
                "phase": "authenticated",
                "authenticated_ts": int(time.time()),
                "verification_url": None,
                "user_code": None,
                "code_expires_ts": None,
                "error": None,
            },
        )
        return 0

    write_attempt_state(
        state_path,
        attempt_id,
        {
            "phase": "failed",
            "failed_ts": int(time.time()),
            "verification_url": None,
            "user_code": None,
            "code_expires_ts": None,
            "error": safe_failure_detail(output_lines, verification_url, user_code),
            "returncode": returncode,
        },
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--codex-bin", required=True)
    args = parser.parse_args()
    return run_device_auth(args.state.resolve(), args.attempt_id, args.codex_bin)


if __name__ == "__main__":
    raise SystemExit(main())
