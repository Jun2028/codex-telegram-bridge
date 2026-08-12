from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

class RateLimitError(RuntimeError):
    """A fresh Codex account rate-limit response could not be obtained."""


def read_rate_limits(
    codex_bin: str,
    *,
    codex_home: Path,
    workdir: Path,
    timeout: float = 30,
) -> dict[str, Any]:
    """Fetch current ChatGPT rate limits through Codex app-server JSON-RPC."""
    try:
        process = subprocess.Popen(
            [codex_bin, "app-server"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            cwd=workdir,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            bufsize=1,
        )
    except OSError as exc:
        raise RateLimitError(f"could not start Codex app-server: {exc}") from exc
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RateLimitError("could not open Codex app-server stdio")

    lines: queue.Queue[Optional[str]] = queue.Queue()

    def consume_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=consume_stdout, name="codex-app-server-reader", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(message: Mapping[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def wait_for_response(request_id: int) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RateLimitError("Codex rate-limit request timed out")
            try:
                raw_line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise RateLimitError("Codex rate-limit request timed out") from exc
            if raw_line is None:
                raise RateLimitError(
                    f"Codex app-server exited before response id {request_id}"
                )
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    try:
        send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "telegram_codex_agent",
                        "title": "Telegram Codex Agent",
                        "version": "0.1.0",
                    }
                },
            }
        )
        initialized = wait_for_response(1)
        if initialized.get("error"):
            raise RateLimitError("Codex app-server initialization failed")
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 2})
        response = wait_for_response(2)
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    if isinstance(response.get("error"), dict):
        error = response["error"]
        message = str(error.get("message") or "unknown JSON-RPC error")
        raise RateLimitError(f"Codex rate-limit request failed: {message[:500]}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RateLimitError("Codex rate-limit response did not contain a result object")
    if not isinstance(result.get("rateLimits"), dict) and not isinstance(
        result.get("rateLimitsByLimitId"), dict
    ):
        raise RateLimitError(
            "Codex did not return ChatGPT rate limits for the active authentication mode"
        )

    return {
        "method": "account/rateLimits/read",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "result": result,
    }


def _buckets(snapshot: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    result = snapshot.get("result")
    if not isinstance(result, Mapping):
        return []
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, Mapping) and by_id:
        return [
            (str(limit_id), bucket)
            for limit_id, bucket in by_id.items()
            if isinstance(bucket, Mapping)
        ]
    fallback = result.get("rateLimits")
    if isinstance(fallback, Mapping):
        limit_id = str(fallback.get("limitId") or "codex")
        return [(limit_id, fallback)]
    return []


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _duration(minutes: object) -> str:
    value = _number(minutes)
    if value is None:
        return "quota window"
    if value % (7 * 24 * 60) == 0:
        weeks = value / (7 * 24 * 60)
        return f"{weeks:g}w window"
    if value % (24 * 60) == 0:
        days = value / (24 * 60)
        return f"{days:g}d window"
    if value % 60 == 0:
        hours = value / 60
        return f"{hours:g}h window"
    return f"{value:g}m window"


def _reset_time(value: object) -> str:
    epoch = _number(value)
    if epoch is None:
        return "unknown reset"
    try:
        return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="minutes")
    except (OSError, OverflowError, ValueError):
        return "invalid reset timestamp"


def _window_line(name: str, window: Mapping[str, Any]) -> str:
    used = _number(window.get("usedPercent"))
    remaining = max(0.0, min(100.0, 100.0 - used)) if used is not None else None
    percentage = f"{remaining:g}% left" if remaining is not None else "remaining unknown"
    return (
        f"  {name}: {percentage}; {_duration(window.get('windowDurationMins'))}; "
        f"resets {_reset_time(window.get('resetsAt'))}"
    )


def remaining_percentages(snapshot: Mapping[str, Any]) -> list[float]:
    remaining: list[float] = []
    for _, bucket in _buckets(snapshot):
        for window_name in ("primary", "secondary"):
            window = bucket.get(window_name)
            if not isinstance(window, Mapping):
                continue
            used = _number(window.get("usedPercent"))
            if used is not None:
                remaining.append(max(0.0, min(100.0, 100.0 - used)))
    return remaining


def _compat_window_name(minutes: object) -> str:
    value = _number(minutes)
    if value == 7 * 24 * 60:
        return "Weekly limit"
    if value is not None and value % 60 == 0:
        return f"{value / 60:g}h limit"
    if value is not None:
        return f"{value:g}m limit"
    return "Rate limit"


def _compat_reset_time(value: object) -> str:
    epoch = _number(value)
    if epoch is None:
        return "unknown"
    try:
        reset = datetime.fromtimestamp(epoch).astimezone()
    except (OSError, OverflowError, ValueError):
        return "unknown"
    return f"{reset:%H:%M} on {reset.day} {reset:%b}"


def format_compat_status(snapshot: Mapping[str, Any]) -> str:
    """Emit the prior helper protocol while sourcing only a fresh JSON-RPC response."""
    lines = [
        "LIVE_QUERY=success",
        f"CHECKED_AT={snapshot.get('fetched_at') or 'unknown'}",
        "CODEX_VERSION=app-server",
    ]
    for limit_id, bucket in _buckets(snapshot):
        bucket_label = str(bucket.get("limitName") or "")
        prefix = f"{bucket_label} " if bucket_label and limit_id != "codex" else ""
        for window_name in ("primary", "secondary"):
            window = bucket.get(window_name)
            if not isinstance(window, Mapping):
                continue
            used = _number(window.get("usedPercent"))
            if used is None:
                continue
            remaining = max(0.0, min(100.0, 100.0 - used))
            lines.append(
                f"STATUS={prefix}{_compat_window_name(window.get('windowDurationMins'))}: "
                f"{remaining:g}% left (resets {_compat_reset_time(window.get('resetsAt'))})"
            )
    lines.append("CREDITS_VISIBILITY=reported_by_account_rate_limits_read")
    return "\n".join(lines)


def format_rate_limits(snapshot: Mapping[str, Any]) -> str:
    method = str(snapshot.get("method") or "account/rateLimits/read")
    fetched_at = str(snapshot.get("fetched_at") or "unknown")
    lines = [f"Fresh Codex {method} response at {fetched_at}:"]
    buckets = _buckets(snapshot)
    if not buckets:
        raise RateLimitError("Codex rate-limit response contained no quota buckets")
    for limit_id, bucket in buckets:
        label = str(bucket.get("limitName") or limit_id)
        lines.append(label)
        found_window = False
        for window_name in ("primary", "secondary"):
            window = bucket.get(window_name)
            if isinstance(window, Mapping):
                lines.append(_window_line(window_name, window))
                found_window = True
        if not found_window:
            lines.append("  no quota windows returned")
        reached = bucket.get("rateLimitReachedType")
        if reached:
            lines.append(f"  reached state: {reached}")

    result = snapshot.get("result")
    credits = result.get("rateLimitResetCredits") if isinstance(result, Mapping) else None
    if isinstance(credits, Mapping) and isinstance(credits.get("availableCount"), int):
        lines.append(f"Earned rate-limit resets available: {credits['availableCount']}")
    lines.append("No cached rollout or TUI /status snapshot was used.")
    return "\n".join(lines)


def safe_rate_limit_error(error: BaseException, token: str = "") -> str:
    message = " ".join(str(error).split())
    return (message.replace(token, "[REDACTED]") if token else message)[:500]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch current Codex account rate limits")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", help="print the JSON-RPC result as JSON")
    parser.add_argument(
        "--compat-status",
        action="store_true",
        help="emit legacy STATUS lines backed by the fresh JSON-RPC response",
    )
    args = parser.parse_args(argv)
    executable = shutil.which(args.codex_bin) or args.codex_bin
    try:
        snapshot = read_rate_limits(
            executable,
            codex_home=args.codex_home.expanduser().resolve(),
            workdir=args.workdir.expanduser().resolve(),
        )
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.compat_status:
            print(format_compat_status(snapshot))
        else:
            print(format_rate_limits(snapshot))
        return 0
    except (OSError, RateLimitError) as exc:
        print(f"error: {safe_rate_limit_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
