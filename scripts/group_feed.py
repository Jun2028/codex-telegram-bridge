#!/usr/bin/env python3
"""Read the Telegram observe-only group feed.

This is a passive reader for agents: it never sends anything to Telegram and
never changes the feed. Group messages are retained by the inbox in a local
JSONL file, and an agent runs this script (or opens the file) only when it
decides to catch up on group progress.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_FEED_PATH = Path(
    os.environ.get(
        "TELEAGENT_OBSERVE_FEED_PATH",
        str(
            Path.home()
            / ".local"
            / "share"
            / "tele-agent"
            / "logs"
            / "telegram"
            / "telegram_observe_feed.jsonl"
        ),
    )
)


def load_feed(feed_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not feed_path.is_file():
        return records
    with feed_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(
                    f"group_feed: skipping malformed line {line_number}",
                    file=sys.stderr,
                )
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def filter_feed(
    records: list[dict[str, Any]],
    *,
    owner_only: bool,
    since_ts: float | None,
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for record in records:
        if owner_only and not record.get("is_owner"):
            continue
        try:
            record_ts = float(record.get("ts"))
        except (TypeError, ValueError):
            record_ts = 0.0
        if since_ts is not None and record_ts < since_ts:
            continue
        kept.append(record)
    return kept


def record_time(record: dict[str, Any]) -> str:
    try:
        return datetime.fromtimestamp(
            int(record["telegram_date"]), timezone.utc
        ).astimezone().strftime("%m-%d %H:%M")
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        try:
            return datetime.fromtimestamp(
                float(record["ts"]), timezone.utc
            ).astimezone().strftime("%m-%d %H:%M")
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            return "??:??"


def format_entry(record: dict[str, Any]) -> str:
    sender = str(record.get("sender") or "unknown")
    owner_marker = " [OWNER]" if record.get("is_owner") else ""
    edited_marker = " (edited)" if record.get("edited") else ""
    text = str(record.get("text") or "")
    media = record.get("media")
    if not text and isinstance(media, dict) and media:
        text = "[media: " + ", ".join(sorted(media.keys())) + "]"
    if record.get("text_truncated"):
        text += " …"
    lines = [
        f"[{record_time(record)}] {sender}{owner_marker}{edited_marker}",
        f"  {text}" if text else "  (empty)",
    ]
    if record.get("reply_to_message_id") is not None:
        replied_to = str(record.get("reply_to_sender") or "unknown")
        lines.append(f"  ↳ replying to {replied_to}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read the local Telegram observe-only group feed."
    )
    parser.add_argument(
        "--feed",
        type=Path,
        default=DEFAULT_FEED_PATH,
        help="path to the observe feed JSONL file",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=20,
        help="show at most this many most recent messages",
    )
    parser.add_argument(
        "--since-minutes",
        type=float,
        default=0,
        help="only show messages newer than this many minutes",
    )
    parser.add_argument(
        "--owner-only",
        action="store_true",
        help="only show the owner's messages",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit raw JSONL records instead of human-readable text",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.last < 1:
        raise SystemExit("--last must be at least 1")
    since_ts = None
    if args.since_minutes > 0:
        since_ts = time.time() - args.since_minutes * 60.0
    records = filter_feed(
        load_feed(args.feed),
        owner_only=args.owner_only,
        since_ts=since_ts,
    )
    records = records[-args.last :]
    if args.json:
        for record in records:
            print(json.dumps(record, sort_keys=True))
        return 0
    if not records:
        if not args.feed.is_file():
            print("No observe feed yet.", file=sys.stderr)
        else:
            print("No matching messages in the observe feed.", file=sys.stderr)
        return 0
    print("\n\n".join(format_entry(record) for record in records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
