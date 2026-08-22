#!/usr/bin/env python3
"""Scratch-backed registry for Telegram-controlled Codex agents."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from notify import assert_safe_local_path, run_short  # noqa: E402

DEFAULT_SCRATCH = str(Path.home() / ".local" / "share" / "tele-agent")
NEW_PROCESS_LAUNCH_SOURCES = frozenset(
    {
        "start_codex_agent.sh",
        "telegram-start-agent",
        "telegram-restart-agent",
    }
)
SESSION_START_SLOP_SECONDS = 5


def utc_iso(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def telegram_log_dir() -> Path:
    scratch = os.environ.get("TELEAGENT_SCRATCH", DEFAULT_SCRATCH).strip() or DEFAULT_SCRATCH
    path = Path(os.environ.get("TELEAGENT_LOG_DIR", str(Path(scratch) / "logs" / "telegram")))
    assert_safe_local_path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def telegram_agent_dir() -> Path:
    path = Path(os.environ.get("TELEAGENT_AGENT_DIR", str(telegram_log_dir() / "agents")))
    assert_safe_local_path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def outbox_path() -> Path:
    path = Path(os.environ.get("TELEAGENT_AGENT_OUTBOX", str(telegram_log_dir() / "telegram_agent_outbox.jsonl")))
    assert_safe_local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def index_path() -> Path:
    path = telegram_log_dir() / "telegram_agents.index.jsonl"
    assert_safe_local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def active_panes_dir() -> Path:
    path = telegram_log_dir() / "active_panes"
    assert_safe_local_path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_pane_name(target_pane: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", target_pane).strip("_") or "unknown"


def active_pane_path(target_pane: str) -> Path:
    return active_panes_dir() / f"{safe_pane_name(target_pane)}.json"


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    assert_safe_local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    assert_safe_local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def codex_session_metadata(session_path: Path) -> dict[str, Any]:
    try:
        with session_path.open("r", encoding="utf-8") as handle:
            for _ in range(32):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
                    return {"timestamp": record.get("timestamp"), **record["payload"]}
    except OSError:
        return {}
    return {}


def iso_timestamp_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def launch_requires_fresh_session(meta: dict[str, Any]) -> bool:
    return str(meta.get("launch_source") or "") in NEW_PROCESS_LAUNCH_SOURCES


def codex_session_matches_agent(meta: dict[str, Any], session_path: Path) -> bool:
    if not session_path.is_file():
        return False
    session_meta = codex_session_metadata(session_path)
    session_cwd = str(session_meta.get("cwd") or "")
    repo_root = str(meta.get("repo_root") or "")
    if not session_cwd or not repo_root:
        return False
    try:
        if Path(session_cwd).resolve() != Path(repo_root).resolve():
            return False
    except OSError:
        return False

    if not launch_requires_fresh_session(meta):
        return True
    session_started = iso_timestamp_epoch(session_meta.get("timestamp"))
    try:
        agent_started = float(meta.get("created_ts"))
    except (TypeError, ValueError):
        return False
    return session_started is not None and session_started >= agent_started - SESSION_START_SLOP_SECONDS


def agent_paths(agent_id: str) -> tuple[Path, Path, Path]:
    root = telegram_agent_dir() / agent_id
    meta_path = root / "meta.json"
    events_path = root / "events.jsonl"
    return root, meta_path, events_path


def update_active_pane(meta: dict[str, Any]) -> None:
    target_pane = str(meta.get("target_pane") or "")
    if not target_pane:
        return
    active = {
        "agent_id": meta.get("agent_id"),
        "agent_jsonl": meta.get("agent_jsonl"),
        "codex_session_path": meta.get("codex_session_path"),
        "meta_json": meta.get("meta_json"),
        "target_pane": target_pane,
        "updated_iso": utc_iso(),
        "updated_ts": int(time.time()),
    }
    write_json(active_pane_path(target_pane), active)


def load_agent_meta(agent_id: str | None = None, meta_path: str | Path | None = None) -> dict[str, Any] | None:
    if meta_path is None:
        if not agent_id:
            return None
        _, meta_path_obj, _ = agent_paths(agent_id)
    else:
        meta_path_obj = Path(meta_path)
    return read_json(meta_path_obj)


def active_agent_for_pane(target_pane: str) -> dict[str, Any] | None:
    active = read_json(active_pane_path(target_pane))
    if not active:
        return None
    meta = load_agent_meta(meta_path=active.get("meta_json"))
    if meta:
        return meta
    return active


def append_agent_event(meta_or_path: dict[str, Any] | str | Path | None, record: dict[str, Any]) -> None:
    if not meta_or_path:
        return
    if isinstance(meta_or_path, dict):
        path_text = meta_or_path.get("agent_jsonl")
        agent_id = meta_or_path.get("agent_id")
        target_pane = meta_or_path.get("target_pane")
        codex_session_path = meta_or_path.get("codex_session_path")
    else:
        path_text = str(meta_or_path)
        agent_id = None
        target_pane = None
        codex_session_path = None
    if not path_text:
        return
    event = {
        "ts": int(time.time()),
        "ts_iso": utc_iso(),
        **record,
    }
    if agent_id and "agent_id" not in event:
        event["agent_id"] = agent_id
    if target_pane and "target_pane" not in event:
        event["target_pane"] = target_pane
    if codex_session_path and "codex_session_path" not in event:
        event["codex_session_path"] = codex_session_path
    append_jsonl(Path(path_text), event)


def new_agent_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    host = re.sub(r"[^A-Za-z0-9_.-]+", "-", socket.gethostname()).strip("-") or "host"
    return f"codex-{stamp}-{host}-{uuid.uuid4().hex[:8]}"


def create_agent(
    repo_root: Path,
    session: str,
    window: str,
    target_pane: str,
    launch_source: str,
    start_epoch: float | None = None,
) -> dict[str, Any]:
    if start_epoch is None:
        start_epoch = time.time()
    agent_id = new_agent_id()
    root, meta_path, events_path = agent_paths(agent_id)
    root.mkdir(parents=True, exist_ok=True)
    meta = {
        "agent_id": agent_id,
        "agent_jsonl": str(events_path),
        "codex_session_path": None,
        "created_iso": utc_iso(start_epoch),
        "created_ts": int(start_epoch),
        "host": socket.gethostname(),
        "launch_source": launch_source,
        "meta_json": str(meta_path),
        "outbox_path": str(outbox_path()),
        "repo_root": str(repo_root),
        "tmux_session": session,
        "tmux_window": window,
        "target_pane": target_pane,
    }
    write_json(meta_path, meta)
    update_active_pane(meta)
    append_jsonl(index_path(), {"event": "agent_registered", **meta})
    append_agent_event(meta, {"event": "agent_registered"})
    return meta


def ps_rows() -> list[tuple[int, int, str]]:
    output = run_short(["ps", "-eo", "pid=,ppid=,comm="], timeout=5)
    rows: list[tuple[int, int, str]] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2].strip()))
        except ValueError:
            continue
    return rows


def descendants_with_depth(
    root_pid: int,
    rows: list[tuple[int, int, str]] | None = None,
) -> list[tuple[int, int]]:
    if rows is None:
        rows = ps_rows()
    children: dict[int, list[tuple[int, str]]] = {}
    for pid, ppid, comm in rows:
        children.setdefault(ppid, []).append((pid, comm))
    result: list[tuple[int, int]] = []
    stack = [(root_pid, 0)]
    seen: set[int] = set()
    while stack:
        pid, depth = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        result.append((pid, depth))
        stack.extend(
            (child_pid, depth + 1) for child_pid, _ in children.get(pid, [])
        )
    return result


def descendants(root_pid: int) -> list[int]:
    return [pid for pid, _ in descendants_with_depth(root_pid)]


def closest_codex_pids(
    root_pid: int,
    rows: list[tuple[int, int, str]] | None = None,
) -> list[int]:
    if rows is None:
        rows = ps_rows()
    comm_by_pid = {pid: comm for pid, _, comm in rows}
    candidates = [
        (pid, depth)
        for pid, depth in descendants_with_depth(root_pid, rows)
        if comm_by_pid.get(pid) == "codex"
    ]
    if not candidates:
        return []
    closest_depth = min(depth for _, depth in candidates)
    return [pid for pid, depth in candidates if depth == closest_depth]


def tmux_pane_pid(target_pane: str) -> int | None:
    text = run_short(["tmux", "display-message", "-p", "-t", target_pane, "#{pane_pid}"], timeout=5)
    try:
        return int(text.strip())
    except ValueError:
        return None


def session_files_open_by_pid(pid: int) -> list[Path]:
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        fds = list(fd_dir.iterdir())
    except OSError:
        return []
    matches: list[Path] = []
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if "/sessions/" not in target or not target.endswith(".jsonl"):
            continue
        path = Path(target)
        if path.exists():
            matches.append(path)
    return matches


def codex_session_for_pane(
    target_pane: str,
    preferred_session_path: str | Path | None = None,
) -> tuple[Path | None, str]:
    root_pid = tmux_pane_pid(target_pane)
    if root_pid is None:
        return None, "tmux_pane_pid_unavailable"
    rows = ps_rows()
    candidates: list[Path] = []
    # A running Codex may launch short-lived helper Codex processes. Those
    # helpers have their own rollout files, but they do not replace the agent
    # attached to this pane. Only inspect the nearest Codex process layer.
    for pid in closest_codex_pids(root_pid, rows):
        candidates.extend(session_files_open_by_pid(pid))
    if candidates:
        if preferred_session_path:
            try:
                preferred = Path(preferred_session_path).resolve()
                for candidate in candidates:
                    if candidate.resolve() == preferred:
                        return candidate, "process_fd"
            except OSError:
                pass
        # With no established link, the pane's root rollout is the earliest
        # session held by the main Codex process. Later sessions belong to
        # helper/sub-agent work and must not become the Telegram source.
        def session_start_key(path: Path) -> tuple[float, float]:
            started = iso_timestamp_epoch(codex_session_metadata(path).get("timestamp"))
            return (
                started if started is not None else float("inf"),
                path.stat().st_mtime,
            )

        return min(candidates, key=session_start_key), "process_fd"
    return None, "process_fd_not_found"


def codex_newest_session_for_pane(
    target_pane: str,
) -> tuple[Path | None, str]:
    """Return the most recently written rollout held open by the pane's Codex.

    A goal-mode agent may resume an older thread while its registry link still
    names a newer helper rollout. The newest-written open rollout is where its
    current replies actually land, so relay drains should prefer it over a
    stale linked path.
    """
    root_pid = tmux_pane_pid(target_pane)
    if root_pid is None:
        return None, "tmux_pane_pid_unavailable"
    candidates: list[Path] = []
    for pid in closest_codex_pids(root_pid, ps_rows()):
        candidates.extend(session_files_open_by_pid(pid))
    newest: Path | None = None
    try:
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
    except (OSError, ValueError):
        newest = None

    # A resumed goal thread is not always discoverable through open FDs
    # (sub-process boundaries hide it). Fall back to the most recently
    # written rollout across every Codex home this pane may use.
    recent_cutoff = time.time() - 900
    session_roots = [
        Path(home) / "sessions" for home in codex_homes_for_pane(target_pane)
    ]
    session_roots.extend(
        [
            Path.home() / ".codex" / "sessions",
        ]
    )
    seen: set[Path] = set()
    for root in session_roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*.jsonl"):
                try:
                    if path in seen:
                        continue
                    seen.add(path)
                    if path.stat().st_mtime < recent_cutoff:
                        continue
                    if newest is None or path.stat().st_mtime > newest.stat().st_mtime:
                        newest = path
                except OSError:
                    continue
        except OSError:
            continue
    if newest is None:
        return None, "session_not_found"
    return newest, "process_fd_newest"


_TELEGRAM_USER_MESSAGE_MARKER = re.compile(
    rb"\[TELEGRAM USER MESSAGE message_id=\d+"
)


def codex_session_with_latest_user_message(
    target_pane: str,
) -> tuple[Path | None, str]:
    """Find the rollout that most recently received a relayed Telegram marker.

    Goal-mode helpers keep writing separate rollouts, so pure mtime-based
    selection chases those helper files instead of the thread where the agent
    actually answers. The agent answers in whichever rollout recorded the
    newest relay marker, so prefer that file.
    """
    session_roots = [
        Path(home) / "sessions" for home in codex_homes_for_pane(target_pane)
    ]
    session_roots.extend(
        [
            Path.home() / ".codex" / "sessions",
        ]
    )
    recent: list[tuple[float, Path]] = []
    cutoff = time.time() - 900
    for root in session_roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    recent.append((mtime, path))
        except OSError:
            continue
    recent.sort(key=lambda item: item[0], reverse=True)
    for _mtime, path in recent[:5]:
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(max(0, size - 8 * 1024 * 1024))
                if _TELEGRAM_USER_MESSAGE_MARKER.search(handle.read()):
                    return path, "user_marker"
        except OSError:
            continue
    return None, "marker_not_found"


def codex_homes_for_pane(target_pane: str) -> list[Path]:
    """Collect CODEX_HOME values exported by Codex processes in the pane."""
    root_pid = tmux_pane_pid(target_pane)
    if root_pid is None:
        return []
    rows = ps_rows()
    homes: list[Path] = []
    for pid in closest_codex_pids(root_pid, rows):
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        for entry in environ.split(b"\0"):
            if not entry.startswith(b"CODEX_HOME="):
                continue
            value = entry.split(b"=", 1)[1].decode(errors="replace")
            if value:
                homes.append(Path(value))
    return homes


def recent_codex_session(
    start_epoch: float | None = None,
    repo_root: str | Path | None = None,
    extra_homes: list[Path] | tuple[Path, ...] | None = None,
) -> tuple[Path | None, str]:
    bases: set[Path] = {Path.home() / ".codex" / "sessions"}
    for home in extra_homes or ():
        bases.add(home / "sessions")
    if not any(base.exists() for base in bases):
        return None, "sessions_dir_missing"
    files: list[Path] = []
    for base in bases:
        if base.exists():
            files.extend(path for path in base.glob("*/*/*/*.jsonl") if path.is_file())
    if start_epoch is not None:
        files = [
            path
            for path in files
            if (iso_timestamp_epoch(codex_session_metadata(path).get("timestamp")) or 0)
            >= start_epoch - SESSION_START_SLOP_SECONDS
        ]
    if repo_root is not None:
        expected_root = Path(repo_root).resolve()
        files = [
            path
            for path in files
            if str(codex_session_metadata(path).get("cwd") or "")
            and Path(str(codex_session_metadata(path)["cwd"])).resolve() == expected_root
        ]
    if not files:
        return None, "mtime_fallback_not_found"
    return max(files, key=lambda path: path.stat().st_mtime), "mtime_fallback"


def clear_codex_session_link(meta: dict[str, Any], reason: str) -> dict[str, Any]:
    if not meta.get("codex_session_path"):
        return meta
    meta["codex_session_path"] = None
    meta["codex_session_detection"] = reason
    meta["codex_session_detected_iso"] = utc_iso()
    meta["codex_session_detected_ts"] = int(time.time())
    if meta.get("meta_json"):
        write_json(Path(str(meta["meta_json"])), meta)
    update_active_pane(meta)
    append_jsonl(
        index_path(),
        {
            "agent_id": meta.get("agent_id"),
            "codex_session_detection": reason,
            "codex_session_path": None,
            "event": "codex_session_link_cleared",
            "target_pane": meta.get("target_pane"),
            "ts": int(time.time()),
            "ts_iso": utc_iso(),
        },
    )
    append_agent_event(meta, {"event": "codex_session_link_cleared", "reason": reason})
    return meta


def refresh_codex_session_link(
    meta: dict[str, Any],
    target_pane: str | None = None,
    start_epoch: float | None = None,
) -> dict[str, Any]:
    target = target_pane or str(meta.get("target_pane") or "")
    current_path_text = str(meta.get("codex_session_path") or "")
    session_path, method = (
        codex_session_for_pane(target, preferred_session_path=current_path_text)
        if target
        else (None, "no_target_pane")
    )
    if session_path is not None and not codex_session_matches_agent(meta, session_path):
        session_path = None
        method = f"{method}_rejected"
    current_path_valid = bool(
        current_path_text
        and codex_session_matches_agent(meta, Path(current_path_text))
    )
    # The mtime fallback exists only to discover a new agent's first rollout.
    # Once a valid rollout is linked, a temporary loss of its process FD must
    # not let an unrelated newer helper rollout replace it.
    if session_path is None and current_path_valid:
        return meta
    # A newly launched process may create its session only after the first
    # prompt, and the process-FD window can be brief. Its registry timestamp is
    # safe for later discovery because recent_codex_session validates the
    # embedded session timestamp and cwd rather than trusting file mtime.
    fallback_epoch = start_epoch
    if fallback_epoch is None and launch_requires_fresh_session(meta):
        try:
            fallback_epoch = float(meta.get("created_ts"))
        except (TypeError, ValueError):
            fallback_epoch = None
    if session_path is None and fallback_epoch is not None:
        session_path, method = recent_codex_session(
            fallback_epoch,
            repo_root=meta.get("repo_root"),
            extra_homes=codex_homes_for_pane(target),
        )
    if session_path is None:
        if current_path_text and not current_path_valid:
            return clear_codex_session_link(meta, method)
        return meta

    session_text = str(session_path)
    if meta.get("codex_session_path") == session_text and meta.get("codex_session_detection") == method:
        return meta
    meta["codex_session_path"] = session_text
    meta["codex_session_detection"] = method
    meta["codex_session_detected_iso"] = utc_iso()
    meta["codex_session_detected_ts"] = int(time.time())
    if meta.get("meta_json"):
        write_json(Path(str(meta["meta_json"])), meta)
    update_active_pane(meta)
    append_jsonl(index_path(), {
        "agent_id": meta.get("agent_id"),
        "codex_session_detection": method,
        "codex_session_path": session_text,
        "event": "codex_session_linked",
        "target_pane": target,
        "ts": int(time.time()),
        "ts_iso": utc_iso(),
    })
    append_agent_event(meta, {"event": "codex_session_linked", "codex_session_detection": method})
    return meta


def adopt_existing_agent(repo_root: Path, session: str, window: str, target_pane: str, launch_source: str) -> dict[str, Any]:
    meta = active_agent_for_pane(target_pane)
    if meta and meta.get("agent_id"):
        return refresh_codex_session_link(meta, target_pane=target_pane)
    meta = create_agent(
        repo_root=repo_root,
        session=session,
        window=window,
        target_pane=target_pane,
        launch_source=launch_source,
    )
    append_agent_event(meta, {"event": "agent_adopted_existing_pane"})
    return refresh_codex_session_link(meta, target_pane=target_pane)


def shell_env_prefix(meta: dict[str, Any]) -> str:
    env = {
        "TELEAGENT_AGENT_ID": str(meta["agent_id"]),
        "TELEAGENT_AGENT_JSONL": str(meta["agent_jsonl"]),
        "TELEAGENT_AGENT_META": str(meta["meta_json"]),
        "TELEAGENT_AGENT_OUTBOX": str(meta["outbox_path"]),
        "TELEAGENT_AGENT_TARGET_PANE": str(meta["target_pane"]),
        "TELEAGENT_LOG_DIR": str(telegram_log_dir()),
    }
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())


def print_shell_exports(meta: dict[str, Any]) -> None:
    env = {
        "TELEAGENT_AGENT_ID": str(meta["agent_id"]),
        "TELEAGENT_AGENT_JSONL": str(meta["agent_jsonl"]),
        "TELEAGENT_AGENT_META": str(meta["meta_json"]),
        "TELEAGENT_AGENT_OUTBOX": str(meta["outbox_path"]),
        "TELEAGENT_AGENT_TARGET_PANE": str(meta["target_pane"]),
        "TELEAGENT_LOG_DIR": str(telegram_log_dir()),
    }
    for key, value in env.items():
        print(f"export {key}={shlex.quote(value)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Telegram Codex agent registry metadata.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--repo-root", type=Path, required=True)
    create_parser.add_argument("--session", required=True)
    create_parser.add_argument("--window", required=True)
    create_parser.add_argument("--target-pane", required=True)
    create_parser.add_argument("--launch-source", default="manual")
    create_parser.add_argument("--start-epoch", type=float, default=None)
    create_parser.add_argument("--shell-exports", action="store_true")

    adopt_parser = subparsers.add_parser("adopt")
    adopt_parser.add_argument("--repo-root", type=Path, required=True)
    adopt_parser.add_argument("--session", required=True)
    adopt_parser.add_argument("--window", required=True)
    adopt_parser.add_argument("--target-pane", required=True)
    adopt_parser.add_argument("--launch-source", default="adopted-existing")
    adopt_parser.add_argument("--shell-exports", action="store_true")

    link_parser = subparsers.add_parser("link")
    link_parser.add_argument("--agent-id", default="")
    link_parser.add_argument("--meta-json", default="")
    link_parser.add_argument("--target-pane", required=True)
    link_parser.add_argument("--start-epoch", type=float, default=None)

    current_parser = subparsers.add_parser("current")
    current_parser.add_argument("--target-pane", required=True)
    current_parser.add_argument("--refresh", action="store_true")
    current_parser.add_argument("--shell-exports", action="store_true")

    args = parser.parse_args()
    if args.command == "create":
        meta = create_agent(
            repo_root=args.repo_root.resolve(),
            session=args.session,
            window=args.window,
            target_pane=args.target_pane,
            launch_source=args.launch_source,
            start_epoch=args.start_epoch,
        )
        if args.shell_exports:
            print_shell_exports(meta)
        else:
            print(json.dumps(meta, sort_keys=True))
        return 0
    if args.command == "adopt":
        meta = adopt_existing_agent(
            repo_root=args.repo_root.resolve(),
            session=args.session,
            window=args.window,
            target_pane=args.target_pane,
            launch_source=args.launch_source,
        )
        if args.shell_exports:
            print_shell_exports(meta)
        else:
            print(json.dumps(meta, sort_keys=True))
        return 0
    if args.command == "link":
        meta = load_agent_meta(agent_id=args.agent_id or None, meta_path=args.meta_json or None)
        if meta is None:
            raise SystemExit("agent metadata not found")
        meta = refresh_codex_session_link(meta, target_pane=args.target_pane, start_epoch=args.start_epoch)
        print(json.dumps(meta, sort_keys=True))
        return 0
    if args.command == "current":
        meta = active_agent_for_pane(args.target_pane)
        if meta is None:
            return 1
        if args.refresh:
            meta = refresh_codex_session_link(meta, target_pane=args.target_pane)
        if args.shell_exports:
            print_shell_exports(meta)
        else:
            print(json.dumps(meta, sort_keys=True))
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
