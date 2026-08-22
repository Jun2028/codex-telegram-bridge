#!/usr/bin/env python3
"""Interactively configure Telegram notifications without printing secrets."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

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


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def resolve_secret_path(repo_root: Path, secret_env: Path | None) -> Path:
    path = secret_env or repo_root / ".secrets" / "notify.env"
    if not path.is_absolute():
        path = repo_root / path
    assert_safe_local_path(path)
    return path


def telegram_api(token: str, method: str, params: dict[str, Any] | None = None, timeout: int = 15) -> Any:
    data = None
    if params is not None:
        data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("description", body)
        except json.JSONDecodeError:
            detail = body[:300]
        raise RuntimeError(f"Telegram {method} failed with HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram {method} failed: {exc.reason}") from None

    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description', payload)}")
    return payload.get("result")


def tls_context() -> ssl.SSLContext:
    candidates = [
        os.environ.get("TELEAGENT_CA_BUNDLE", "").strip(),
        os.environ.get("SSL_CERT_FILE", "").strip(),
        os.environ.get("REQUESTS_CA_BUNDLE", "").strip(),
        os.environ.get("CURL_CA_BUNDLE", "").strip(),
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/cert.pem",
    ]
    try:
        import certifi  # type: ignore

        candidates.append(certifi.where())
    except Exception:
        pass

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def chat_label(chat: dict[str, Any]) -> str:
    parts = [str(chat.get("id", "")), chat.get("type", "")]
    title = chat.get("title") or " ".join(part for part in [chat.get("first_name"), chat.get("last_name")] if part)
    username = chat.get("username")
    if title:
        parts.append(title)
    if username:
        parts.append(f"@{username}")
    return " | ".join(part for part in parts if part)


def discover_chats(token: str) -> list[dict[str, Any]]:
    updates = telegram_api(token, "getUpdates", {"limit": 20, "timeout": 0})
    seen: set[str] = set()
    chats: list[dict[str, Any]] = []
    for update in updates or []:
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
        )
        if not message:
            continue
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if chat_id and chat_id not in seen:
            seen.add(chat_id)
            chats.append(chat)
    return chats


def write_env(path: Path, template_path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    source = path if path.exists() else template_path
    lines = source.read_text(encoding="utf-8").splitlines() if source.exists() else []
    output: list[str] = []
    seen: set[str] = set()

    for raw_line in lines:
        stripped = raw_line.strip()
        candidate = stripped[len("export ") :].strip() if stripped.startswith("export ") else stripped
        if candidate and not candidate.startswith("#") and "=" in candidate:
            key = candidate.split("=", 1)[0].strip()
            if key in updates:
                prefix = "export " if stripped.startswith("export ") else ""
                output.append(f"{prefix}{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(raw_line)

    if output and output[-1] != "":
        output.append("")
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")

    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    path.chmod(0o600)


def choose_chat(chats: list[dict[str, Any]], assume_yes: bool, bot_username: str = "") -> str:
    if not chats:
        bot_hint = f" Open https://t.me/{bot_username} ." if bot_username else ""
        raise RuntimeError(
            "No Telegram chat was visible to the bot."
            f"{bot_hint} Send /start or any short message to that exact bot, then rerun this setup command."
        )
    if len(chats) == 1 and assume_yes:
        return str(chats[0]["id"])

    print("Visible Telegram chats:")
    for index, chat in enumerate(chats, start=1):
        print(f"  {index}. {chat_label(chat)}")

    if len(chats) == 1:
        answer = input("Use this chat? [Y/n] ").strip().lower()
        if answer in {"", "y", "yes"}:
            return str(chats[0]["id"])
        raise RuntimeError("Chat selection cancelled.")

    while True:
        answer = input(f"Choose chat number [1-{len(chats)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(chats):
            return str(chats[int(answer) - 1]["id"])
        print("Invalid selection.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure .secrets/notify.env for Telegram and send a live test."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--secret-env",
        type=Path,
        default=os.environ.get("TELEAGENT_SECRET_ENV"),
    )
    parser.add_argument("--chat-id", default="", help="Known Telegram chat id. Non-secret.")
    parser.add_argument("--yes", action="store_true", help="Auto-select a single discovered chat.")
    parser.add_argument("--skip-live-test", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    assert_safe_local_path(repo_root)
    secret_path = resolve_secret_path(repo_root, args.secret_env)
    template_path = repo_root / "config" / "notify.env.template"

    current = parse_env_file(secret_path)
    token = (
        os.environ.get("TELEAGENT_BOT_TOKEN", "").strip()
        or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        or current.get("TELEAGENT_BOT_TOKEN", "").strip()
    )
    if token:
        print("Telegram bot token found in environment or secret file; validating without printing it.")
    elif sys.stdin.isatty():
        token = getpass.getpass("Paste Telegram bot token from BotFather (input hidden): ").strip()
    else:
        raise SystemExit(
            "Missing Telegram bot token. Run this script in a terminal and paste the BotFather token "
            "when prompted, or export TELEAGENT_BOT_TOKEN before running it."
        )
    if not token:
        raise SystemExit("Empty Telegram bot token.")

    bot = telegram_api(token, "getMe")
    bot_username = str(bot.get("username") or "")
    bot_name = bot_username or bot.get("first_name") or bot.get("id")
    print(f"Validated Telegram bot: {bot_name}")

    chat_id = (
        args.chat_id.strip()
        or os.environ.get("TELEAGENT_CHAT_ID", "").strip()
        or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        or current.get("TELEAGENT_CHAT_ID", "").strip()
    )
    if not chat_id:
        chats = discover_chats(token)
        if not chats and sys.stdin.isatty():
            print(
                "To discover the chat id, open Telegram now and send /start "
                "or any short message to the bot."
            )
            input("Press Enter after the message has been sent...")
            chats = discover_chats(token)
        chat_id = choose_chat(chats, args.yes, bot_username)

    chat = telegram_api(token, "getChat", {"chat_id": chat_id})
    print(f"Validated destination chat: {chat_label(chat)}")

    write_env(
        secret_path,
        template_path,
        {
            "TELEAGENT_BOT_TOKEN": token,
            "TELEAGENT_CHAT_ID": chat_id,
            "TELEAGENT_NOTIFY_ALLOW_IMAGES": current.get("TELEAGENT_NOTIFY_ALLOW_IMAGES", "0") or "0",
        },
    )
    print(f"Wrote Telegram notification secrets to {secret_path} with mode 600.")

    if args.skip_live_test:
        return 0

    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "notify.py"),
            "--repo-root",
            str(repo_root),
            "--title",
            "notify live test",
            "--message",
            "Bridge Telegram notification test",
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
