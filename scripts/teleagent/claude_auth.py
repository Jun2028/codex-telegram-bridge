"""Private Telegram controls for the native Claude authentication flow."""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from . import claude, replies, state

CODE = re.compile(r"[A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,}")
ACTIVE = {"starting", "awaiting_code", "exchanging"}


def path_for(args) -> Path:
    return Path(args.codex_usage_state_path).with_name("telegram_claude_auth.state.json")


def code_path(path: Path, attempt: str) -> Path:
    return path.with_name(f".claude-auth-{attempt}.code")


def validated_url(url: str) -> bool:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    return (parsed.scheme == "https" and parsed.hostname in {"claude.ai", "claude.com", "console.anthropic.com"}
            and parsed.path in {"/cai/oauth/authorize", "/oauth/authorize"}
            and not parsed.username and not parsed.password
            and query.get("response_type") == ["code"] and query.get("code_challenge_method") == ["S256"]
            and bool(query.get("state")) and bool(query.get("code_challenge"))
            and not {"access_token", "refresh_token", "code_verifier"}.intersection(query))


def update_attempt(path: Path, attempt: str, **updates) -> bool:
    with state.locked(path):
        data = state.read_json_object(path)
        if data.get("attempt_id") != attempt or data.get("phase") in {"cancelled", "authenticated", "failed"}:
            return False
        data.update(updates, updated_ts=time.time())
        state.write_json_object(path, data)
        return True


def start_login(repo: Path, path: Path, chat_id: str, sender_id: str) -> str:
    binary = claude.binary()
    with state.locked(path):
        data = state.read_json_object(path)
        if data.get("phase") in ACTIVE:
            if time.time() - data.get("started_ts", 0) < 620:
                return "Claude sign-in is already in progress. Use /claude_auth status or /claude_auth cancel."
            cancel_login(path)
        attempt = uuid.uuid4().hex
        data = dict(attempt_id=attempt, phase="starting", chat_id=str(chat_id),
                    sender_id=str(sender_id), started_ts=time.time())
        state.write_json_object(path, data)
        try:
            worker = subprocess.Popen([sys.executable, str(repo / "scripts/claude_auth_worker.py"),
                "--state", str(path), "--attempt", attempt, "--binary", binary],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
            data["worker_pid"] = worker.pid
            state.write_json_object(path, data)
            threading.Thread(target=worker.wait, daemon=True).start()
        except OSError:
            data.update(phase="failed", error="Could not start Claude's login process.")
            state.write_json_object(path, data)
            return data["error"]
    return "Preparing Claude sign-in. I’ll send the browser link here shortly."


def cancel_login(path: Path) -> None:
    with state.locked(path):
        data = state.read_json_object(path)
        if data.get("phase") not in ACTIVE:
            return
        attempt = str(data.get("attempt_id") or "")
        pid = data.get("worker_pid")
        if isinstance(pid, int):
            try:
                args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if (str(path).encode() in args and attempt.encode() in args
                        and any(Path(os.fsdecode(arg)).name == "claude_auth_worker.py" for arg in args)
                        and os.getpgid(pid) == pid):
                    os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
        data.update(phase="cancelled", verification_url=None, updated_ts=time.time())
        state.write_json_object(path, data)
        code_path(path, attempt).unlink(missing_ok=True)


def login_status(path: Path) -> str:
    data = state.read_json_object(path)
    phase = data.get("phase")
    if phase == "awaiting_code" and validated_url(str(data.get("verification_url") or "")):
        return (f"[Sign in to Claude]({data['verification_url']}).\n"
                "Send the one-time code shown afterward here as `/claude_code CODE`. "
                "Do not send your password or an API key.")
    if phase in ACTIVE:
        return "Claude sign-in is preparing the link." if phase == "starting" else "Claude is completing sign-in."
    return claude.authentication_status()


def consume_code_update(update: dict, args, token: str, owner_chat: str, owner_user: str = "") -> bool:
    """Consume before durable ingress so login codes never enter normal logs/queues."""
    message = update.get("message") or update.get("edited_message") or {}
    text = str(message.get("text") or "").strip()
    explicit = text.split(maxsplit=1)[0].split("@", 1)[0] == "/claude_code" if text else False
    code = text.split(maxsplit=1)[1].strip() if explicit and len(text.split(maxsplit=1)) == 2 else text
    # Quoted codes must never reach durable ingress or an agent either.
    for quoted in (message.get("reply_to_message") or {}, message.get("quote") or {}):
        for key in ("text", "caption"):
            if isinstance(quoted.get(key), str):
                quoted[key] = CODE.sub("[Claude sign-in code removed]", quoted[key])
    if not explicit and not CODE.fullmatch(code):
        if isinstance(message.get("text"), str):
            message["text"] = CODE.sub("[Claude sign-in code removed]", message["text"])
        return False
    message["text"] = "[Claude sign-in code removed]"
    path = path_for(args)
    source = str((message.get("chat") or {}).get("id") or "")
    sender = str((message.get("from") or {}).get("id") or "")
    if source != str(owner_chat) or sender != str(owner_user or owner_chat):
        return True  # Never disclose a private login flow into another chat.
    with state.locked(path):
        data = state.read_json_object(path)
        url = str(data.get("verification_url") or "")
        expected = parse_qs(urlsplit(url).query).get("state", [""])[0]
        if update.get("update_id") is not None and data.get("submitted_update_id") == update["update_id"]:
            reply = "This Claude sign-in code was already submitted."
        elif (data.get("phase") != "awaiting_code" or data.get("chat_id") != source
              or data.get("sender_id") != sender or not CODE.fullmatch(code)
              or not validated_url(url) or not expected or code.split("#", 1)[-1] != expected):
            reply = "That code does not match a waiting Claude sign-in. Use /claude_auth login to start one."
        else:
            target = code_path(path, data["attempt_id"])
            state.atomic_write(target, code + "\n")
            data.update(phase="exchanging", submitted_update_id=update.get("update_id"))
            state.write_json_object(path, data)
            reply = "Completing Claude sign-in."
    replies.send(args, token, source, reply)
    return True


def notify_progress(args, token: str, owner_chat: str) -> None:
    path = path_for(args)
    with state.locked(path):
        data = state.read_json_object(path)
        if data.get("chat_id") != str(owner_chat):
            return
        phase = data.get("phase")
        if phase == "awaiting_code" and not data.get("instructions_sent"):
            reply, flag = login_status(path), "instructions_sent"
        elif phase in {"authenticated", "failed"} and not data.get("completion_sent"):
            reply = "Claude sign-in complete." if phase == "authenticated" else "Claude sign-in failed or expired. Use /claude_auth login to try again."
            flag = "completion_sent"
        else:
            return
        replies.send(args, token, owner_chat, reply)
        data[flag] = True
        state.write_json_object(path, data)
