#!/usr/bin/env python3
"""Poll a Telegram chat and archive messages to a local feed.

This is deliberately a standalone process with no connection to the
tele-agent relay inbox, tmux, or any Codex agent. It long-polls Telegram,
appends observed messages to a local JSONL feed, and never sends anything
to an agent.

Telegram allows only one getUpdates consumer per bot token, so run this
listener with its own bot token (for example TELEAGENT_GROUP_BOT_TOKEN),
not the token the relay inbox is polling.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from notify import (  # noqa: E402
    assert_safe_local_path,
    env_value,
    load_env,
    redact,
    tls_context,
)


DEFAULT_SCRATCH = Path(
    os.environ.get(
        "TELEAGENT_SCRATCH",
        str(Path.home() / ".local" / "share" / "tele-agent"),
    )
)
DEFAULT_LOG_DIR = Path(
    os.environ.get("TELEAGENT_LOG_DIR", str(DEFAULT_SCRATCH / "logs" / "telegram"))
)
DEFAULT_FEED_PATH = Path(
    os.environ.get(
        "TELEAGENT_OBSERVE_FEED_PATH",
        str(DEFAULT_LOG_DIR / "telegram_group_feed.jsonl"),
    )
)
DEFAULT_OFFSET_PATH = Path(
    os.environ.get(
        "TELEAGENT_OBSERVE_OFFSET_PATH",
        str(DEFAULT_LOG_DIR / "telegram_group_feed.offset"),
    )
)
DEFAULT_TOKEN_ENV = "TELEAGENT_GROUP_BOT_TOKEN"
TRANSIENT_HTTP_CODES = {409, 429, 500, 502, 503, 504}
MEDIA_KINDS = ("document", "voice", "photo", "audio", "video", "animation")


def telegram_api(
    token: str,
    method: str,
    params: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=tls_context()
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in TRANSIENT_HTTP_CODES:
            raise TransientGroupFeedError(
                f"Telegram {method} failed with HTTP {exc.code}"
            ) from None
        raise GroupFeedError(
            f"Telegram {method} failed with HTTP {exc.code}"
        ) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransientGroupFeedError(
            f"Telegram {method} network failure: {reason}"
        ) from None
    if not isinstance(payload, dict):
        raise GroupFeedError("Telegram returned a non-object response")
    if payload.get("ok") is not True:
        description = str(payload.get("description") or "unknown Telegram error")
        if "conflict" in description.lower():
            raise TransientGroupFeedError(description)
        raise GroupFeedError(description)
    result = payload.get("result")
    if not isinstance(result, dict) and not isinstance(result, list):
        raise GroupFeedError(f"Telegram {method} returned an invalid result")
    return result


class GroupFeedError(RuntimeError):
    """Permanent observer error; safe to fail the process."""


class TransientGroupFeedError(RuntimeError):
    """Temporary Telegram failure; retry with backoff."""


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    path.chmod(0o600)


def read_offset(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(f"{offset}\n", encoding="utf-8")
    path.chmod(0o600)


def sender_label(message: dict[str, Any]) -> str:
    sender = message.get("from")
    if not isinstance(sender, dict):
        return "unknown"
    parts = []
    if sender.get("username"):
        parts.append(f"@{sender['username']}")
    name = " ".join(
        str(sender.get(key, "")).strip() for key in ("first_name", "last_name")
    ).strip()
    if name:
        parts.append(name)
    if sender.get("id"):
        parts.append(str(sender["id"]))
    return " | ".join(parts) or "unknown"


def visible_message_text(message: dict[str, Any]) -> str:
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def archive_message(
    feed_path: Path,
    update: dict[str, Any],
    message: dict[str, Any],
    env: dict[str, str],
    owner_id: str,
    max_log_chars: int,
) -> dict[str, Any]:
    chat = message.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    sender = message.get("from")
    sender = sender if isinstance(sender, dict) else {}
    sender_id = str(sender.get("id", ""))
    raw_text = visible_message_text(message)
    redacted_text = redact(raw_text, env)
    telegram_date = message.get("date")
    record: dict[str, Any] = {
        "ts": int(time.time()),
        "ts_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "telegram_date": telegram_date,
        "telegram_iso": (
            datetime.fromtimestamp(int(telegram_date), timezone.utc).isoformat(
                timespec="seconds"
            )
            if telegram_date
            else None
        ),
        "update_id": update.get("update_id"),
        "message_id": message.get("message_id"),
        "chat_id": str(chat.get("id", "")),
        "chat_title": str(chat.get("title") or ""),
        "chat_type": str(chat.get("type") or ""),
        "sender": sender_label(message),
        "sender_id": sender_id,
        "is_owner": bool(owner_id) and sender_id == str(owner_id),
        "priority": bool(owner_id) and sender_id == str(owner_id),
        "edited": update.get("edited_message") is not None,
        "text": redacted_text[:max_log_chars],
        "text_full": redacted_text,
        "text_truncated": len(redacted_text) > max_log_chars,
    }
    for kind in MEDIA_KINDS:
        value = message.get(kind)
        if value is None:
            continue
        if isinstance(value, dict):
            record.setdefault("media", {})[kind] = {
                "file_name": redact(str(value.get("file_name") or ""), env),
                "mime_type": str(value.get("mime_type") or ""),
                "file_size": value.get("file_size"),
                "duration_seconds": value.get("duration"),
            }
        else:
            record.setdefault("media", {})[kind] = "present"
    replied = message.get("reply_to_message")
    if isinstance(replied, dict):
        replied_text = redact(visible_message_text(replied), env)
        record["reply_to_message_id"] = replied.get("message_id")
        record["reply_to_sender"] = sender_label(replied)
        record["reply_to_text"] = replied_text[:max_log_chars]
        record["reply_to_text_truncated"] = len(replied_text) > max_log_chars
    append_jsonl(feed_path, record)
    return record


def get_updates(
    token: str,
    offset: int | None,
    timeout: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "timeout": timeout,
        "limit": 100,
        "allowed_updates": json.dumps(["message", "edited_message"]),
    }
    if offset is not None:
        params["offset"] = offset
    result = telegram_api(token, "getUpdates", params, timeout=timeout + 5)
    if not isinstance(result, list):
        raise GroupFeedError("Telegram getUpdates returned a non-list result")
    return [item for item in result if isinstance(item, dict)]


def process_update(
    update: dict[str, Any],
    args: argparse.Namespace,
    env: dict[str, str],
    log_path: Path | None,
) -> dict[str, Any] | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    chat_id = str((chat or {}).get("id", ""))
    if args.chat_id and chat_id != args.chat_id:
        return None
    archived = archive_message(
        args.feed,
        update,
        message,
        env,
        args.owner_id,
        args.max_log_chars,
    )
    if log_path is not None:
        append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": "archived",
                "update_id": update.get("update_id"),
                "message_id": archived.get("message_id"),
                "chat_id": chat_id,
                "sender_id": archived.get("sender_id"),
                "priority": archived.get("priority"),
            },
        )
    return archived


def poll_once(
    token: str,
    args: argparse.Namespace,
    env: dict[str, str],
    log_path: Path | None,
) -> list[dict[str, Any]]:
    offset = read_offset(args.offset_file)
    updates = get_updates(token, offset, args.poll_timeout)
    archived: list[dict[str, Any]] = []
    for update in updates:
        try:
            record = process_update(update, args, env, log_path)
        except Exception as exc:
            # A single malformed update must not wedge the poller; Telegram
            # will only redeliver it if the offset is not advanced.
            if log_path is not None:
                append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "event": "archive_failed",
                        "update_id": update.get("update_id"),
                        "error": str(exc),
                    },
                )
            continue
        if record is not None:
            archived.append(record)
        try:
            update_id = int(update["update_id"])
        except (KeyError, TypeError, ValueError):
            continue
        write_offset(args.offset_file, update_id + 1)
    return archived


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Long-poll Telegram and archive observed messages to a local feed."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--secret-env", type=Path, default=None)
    parser.add_argument(
        "--token-env",
        default=os.environ.get("TELEAGENT_GROUP_TOKEN_ENV", DEFAULT_TOKEN_ENV),
        help="environment variable name holding the observer bot token",
    )
    parser.add_argument(
        "--feed",
        type=Path,
        default=DEFAULT_FEED_PATH,
        help="local JSONL feed receiving observed messages",
    )
    parser.add_argument(
        "--offset-file",
        type=Path,
        default=DEFAULT_OFFSET_PATH,
        help="persistent getUpdates offset for this observer",
    )
    parser.add_argument(
        "--chat-id",
        default=os.environ.get("TELEAGENT_OBSERVE_CHAT_ID", "").strip(),
        help="only archive messages from this Telegram chat id",
    )
    parser.add_argument(
        "--owner-id",
        default=os.environ.get("TELEAGENT_OBSERVE_OWNER_ID", "").strip(),
        help="Telegram user id whose messages are marked priority",
    )
    parser.add_argument(
        "--log-jsonl",
        default="",
        help="optional observer log path; empty disables logging",
    )
    parser.add_argument("--poll-timeout", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--max-log-chars", type=int, default=4000)
    parser.add_argument("--process-existing", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    assert_safe_local_path(repo_root)
    env = load_env(repo_root, args.secret_env)
    token = env_value(env, args.token_env, "")
    if not token:
        raise SystemExit(
            f"observer bot token is not configured; set {args.token_env} "
            "or provide it in the secret env file"
        )
    if args.max_log_chars < 1:
        raise SystemExit("--max-log-chars must be positive")
    args.feed = args.feed.expanduser().resolve()
    args.offset_file = args.offset_file.expanduser().resolve()
    assert_safe_local_path(args.feed.parent)
    assert_safe_local_path(args.offset_file.parent)
    args.feed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = Path(args.log_jsonl).expanduser().resolve() if args.log_jsonl else None
    if log_path is not None:
        assert_safe_local_path(log_path.parent)
        append_jsonl(
            log_path,
            {
                "ts": int(time.time()),
                "event": "started",
                "feed": str(args.feed),
                "offset_file": str(args.offset_file),
                "chat_id": args.chat_id or None,
            },
        )

    offset = read_offset(args.offset_file)
    if offset is None and not args.process_existing:
        try:
            existing = get_updates(token, None, 0)
            if existing:
                offset = max(int(item["update_id"]) for item in existing) + 1
                write_offset(args.offset_file, offset)
        except (TransientGroupFeedError, GroupFeedError) as exc:
            print(f"group_feed_listener: initial getUpdates failed: {exc}", file=sys.stderr)
            if args.once:
                return 1

    print(
        f"group_feed_listener: archiving chat "
        f"{args.chat_id or '(all visible chats)'} -> {args.feed}",
        flush=True,
    )
    if args.once:
        poll_once(token, args, env, log_path)
        return 0

    backoff = 0.0
    while True:
        try:
            poll_once(token, args, env, log_path)
            backoff = 0.0
        except TransientGroupFeedError as exc:
            backoff = min(max(backoff * 2.0, args.poll_interval), 60.0)
            if log_path is not None:
                append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "event": "transient_error",
                        "error": str(exc),
                        "retry_in": backoff,
                    },
                )
            time.sleep(backoff)
        except GroupFeedError as exc:
            if log_path is not None:
                append_jsonl(
                    log_path,
                    {
                        "ts": int(time.time()),
                        "event": "fatal_error",
                        "error": str(exc),
                    },
                )
            raise SystemExit(f"group_feed_listener: {exc}")
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
