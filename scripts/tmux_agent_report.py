#!/usr/bin/env python3
"""Collect a compact tmux status report and send it through notify.py."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BLOCKED_PATH_FRAGMENTS_ENV = "TELEAGENT_BLOCKED_PATH_FRAGMENTS"


def assert_safe_local_path(path: str | Path) -> None:
    text = str(path)
    blocked = tuple(
        fragment
        for fragment in os.environ.get(BLOCKED_PATH_FRAGMENTS_ENV, "").split(os.pathsep)
        if fragment
    )
    if any(fragment in text for fragment in blocked):
        raise SystemExit(f"Forbidden shared storage path: {text}")


def run_short(args: list[str], timeout: int = 10) -> str:
    try:
        completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return f"(failed: {exc})"
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    return output.strip() or "(no output)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--session", default=os.environ.get("TELEAGENT_TMUX_SESSION", "tele-agent"))
    parser.add_argument("--title", default="bridge tmux agent report")
    parser.add_argument("--level", default="info", choices=["info", "success", "warning", "error"])
    parser.add_argument("--tmux-lines", type=int, default=80)
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    assert_safe_local_path(repo_root)

    tmux_tail = run_short(["tmux", "capture-pane", "-pt", f"{args.session}:0", "-S", f"-{args.tmux_lines}"])
    qstat = run_short(["qstat", "-u", os.environ.get("USER", "")])
    scratch = os.environ.get(
        "TELEAGENT_SCRATCH",
        str(Path.home() / ".local" / "share" / "tele-agent"),
    )
    project_du = run_short(["du", "-sh", scratch])
    df = run_short(["df", "-h", scratch])

    message_parts = [
        "extra:\n" + "\n".join(args.extra) if args.extra else "",
        f"tmux session: {args.session}",
        "qstat:\n" + qstat,
        "scratch usage (quota-relevant):\n" + project_du,
        "scratch filesystem df (global):\n" + df,
        f"tmux tail ({args.tmux_lines} lines):\n" + tmux_tail,
    ]
    message = "\n\n".join(part for part in message_parts if part.strip())

    cmd = [
        sys.executable,
        str(repo_root / "scripts" / "notify.py"),
        "--repo-root",
        str(repo_root),
        "--title",
        args.title,
        "--level",
        args.level,
        "--message",
        message,
    ]
    if args.dry_run:
        cmd.append("--dry-run")
    completed = subprocess.run(cmd, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
