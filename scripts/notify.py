#!/usr/bin/env python3
"""Secret-safe Telegram notifications with SMTP email fallback."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
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
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def load_env(repo_root: Path, secret_path: Path | None) -> dict[str, str]:
    merged = dict(os.environ)
    if secret_path is None:
        secret_path = repo_root / ".secrets" / "notify.env"
    if not secret_path.is_absolute():
        secret_path = repo_root / secret_path
    assert_safe_local_path(secret_path)
    merged.update(parse_env_file(secret_path))
    return merged


def env_value(env: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = env.get(key, "").strip()
        if value:
            return value
    return ""


def bool_env(env: dict[str, str], key: str, default: bool = False) -> bool:
    value = env.get(key, "")
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def redact(text: str, env: dict[str, str]) -> str:
    result = text
    for key, value in env.items():
        key_lower = key.lower()
        if not value or len(value) < 8:
            continue
        if any(marker in key_lower for marker in ("token", "password", "secret", "key")):
            result = result.replace(value, "[redacted]")
    return result


def run_short(args: list[str], timeout: int = 8) -> str:
    try:
        completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return ""
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    return output.strip()


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


@dataclass
class NotifyResult:
    provider: str
    ok: bool
    detail: str


@dataclass
class Attachment:
    path: Path
    kind: str
    method: str
    field_name: str

    def log_record(self) -> dict[str, Any]:
        return {
            "basename": self.path.name,
            "kind": self.kind,
            "size_bytes": self.path.stat().st_size,
        }


def telegram_configured(env: dict[str, str]) -> bool:
    return bool(
        env_value(env, "TELEAGENT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
        and env_value(env, "TELEAGENT_CHAT_ID", "TELEGRAM_CHAT_ID")
    )


def email_configured(env: dict[str, str]) -> bool:
    return bool(
        env_value(env, "TELEAGENT_SMTP_HOST", "SMTP_HOST")
        and env_value(env, "TELEAGENT_SMTP_TO", "SMTP_TO")
        and env_value(env, "TELEAGENT_SMTP_FROM", "SMTP_FROM")
    )


def build_multipart(
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
) -> tuple[bytes, str]:
    boundary = f"----tele-agent-{int(time.time())}-{os.getpid()}"
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks: list[bytes] = []

    for key, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def send_telegram(
    env: dict[str, str],
    title: str,
    message: str,
    timeout: int,
    attachment: Attachment | None = None,
) -> NotifyResult:
    token = env_value(env, "TELEAGENT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    chat_id = env_value(env, "TELEAGENT_CHAT_ID", "TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return NotifyResult("telegram", False, "not configured")

    text = f"{title}\n\n{telegram_safe_message(title, message)}"
    if attachment is None:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text[:3900],
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    else:
        data, content_type = build_multipart(
            {
                "chat_id": chat_id,
                "caption": text[:1024],
            },
            attachment.field_name,
            attachment.path,
        )
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{attachment.method}",
            data=data,
            headers={"Content-Type": content_type},
            method="POST",
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return NotifyResult("telegram", False, f"http {exc.code}: {body[:300]}")
    except Exception as exc:
        return NotifyResult("telegram", False, str(exc))

    if payload.get("ok"):
        return NotifyResult("telegram", True, "sent")
    return NotifyResult("telegram", False, str(payload)[:300])


def telegram_safe_message(title: str, message: str) -> str:
    """Keep Telegram operational notifications readable and log-free."""
    if "summary:" in message and not any(
        marker in message
        for marker in (
            "command:",
            "last log lines:",
            "qstat:",
            "tmux:",
            "scratch filesystem df",
            "scratch usage",
        )
    ):
        return message

    command_markers = (
        "command:",
        "last log lines:",
        "qstat:",
        "tmux:",
        "scratch filesystem df",
        "scratch usage",
        "details: command and full log are kept locally",
    )
    if not any(marker in message for marker in command_markers):
        return message

    if "summary:" in message:
        before_summary, summary = message.split("summary:", 1)
        safe_prefix = []
        for raw_line in before_summary.splitlines():
            lowered = raw_line.lower()
            if any(marker in lowered for marker in ("command:", "last log lines:", "qstat:", "tmux:", "scratch filesystem")):
                continue
            if raw_line.strip():
                safe_prefix.append(raw_line.strip())
        safe_summary = []
        for raw_line in summary.splitlines():
            lowered = raw_line.lower()
            if any(marker in lowered for marker in ("command:", "last log lines:", "traceback", "qstat:", "tmux:")):
                continue
            if raw_line.strip():
                safe_summary.append(raw_line.strip())
            if len(safe_summary) >= 6:
                break
        if safe_summary:
            return "\n".join(safe_prefix + ["summary:"] + safe_summary)

    fields: dict[str, str] = {}
    for raw_line in message.splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        fields[key.strip()] = value.strip()

    status = fields.get("exit_status")
    pbs_jobid = fields.get("pbs_jobid")
    pbs_queue = fields.get("pbs_queue")

    if "started" in title.lower():
        summary = "Started. Detailed commands and logs are kept local; the agent will summarize progress here."
    elif status == "0":
        summary = "Completed successfully. Detailed commands and logs are kept local; the agent will summarize results here."
    elif status:
        summary = "Finished with an error. Detailed logs are kept local; the agent will summarize the root cause here."
    else:
        summary = "Status update. Detailed commands and logs are kept local; the agent will summarize progress here."

    details = []
    if pbs_jobid:
        details.append(f"PBS job: {pbs_jobid}")
    if pbs_queue:
        details.append(f"queue: {pbs_queue}")
    if details:
        return f"{summary}\n" + "; ".join(details)
    return summary


def send_email(env: dict[str, str], title: str, message: str, timeout: int) -> NotifyResult:
    host = env_value(env, "TELEAGENT_SMTP_HOST", "SMTP_HOST")
    port = int(env_value(env, "TELEAGENT_SMTP_PORT", "SMTP_PORT") or "587")
    sender = env_value(env, "TELEAGENT_SMTP_FROM", "SMTP_FROM")
    recipient = env_value(env, "TELEAGENT_SMTP_TO", "SMTP_TO")
    user = env_value(env, "TELEAGENT_SMTP_USER", "SMTP_USER")
    password = env_value(env, "TELEAGENT_SMTP_PASSWORD", "SMTP_PASSWORD")
    if not host or not sender or not recipient:
        return NotifyResult("email", False, "not configured")

    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(message)

    try:
        if bool_env(env, "TELEAGENT_SMTP_SSL", False):
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=tls_context()) as smtp:
                if user or password:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                if bool_env(env, "TELEAGENT_SMTP_STARTTLS", True):
                    smtp.starttls(context=tls_context())
                if user or password:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except Exception as exc:
        return NotifyResult("email", False, str(exc))
    return NotifyResult("email", True, "sent")


def append_jsonl(repo_root: Path, env: dict[str, str], record: dict[str, Any]) -> None:
    rel = env.get("TELEAGENT_NOTIFY_LOG_JSONL", "logs/readable/notifications.jsonl").strip()
    path = Path(rel)
    if not path.is_absolute():
        path = repo_root / path
    assert_safe_local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def build_message(args: argparse.Namespace, env: dict[str, str]) -> str:
    pieces = []
    if args.message:
        pieces.append(args.message)
    if args.message_file:
        path = Path(args.message_file)
        if not path.is_absolute():
            path = Path.cwd() / path
        assert_safe_local_path(path)
        pieces.append(path.read_text(encoding="utf-8"))
    if args.include_qstat:
        qstat = run_short(["qstat", "-u", os.environ.get("USER", "")])
        pieces.append("qstat:\n" + (qstat or "(no qstat output)"))
    if args.include_tmux:
        session = args.tmux_session or env.get("TELEAGENT_TMUX_SESSION", "tele-agent")
        pane = run_short(["tmux", "capture-pane", "-pt", f"{session}:0", "-S", f"-{args.tmux_lines}"])
        pieces.append(f"tmux:{session} tail:\n" + (pane or "(no tmux output)"))
    if args.include_df:
        scratch = env.get(
            "TELEAGENT_SCRATCH",
            str(Path.home() / ".local" / "share" / "tele-agent"),
        )
        project_du = run_short(["du", "-sh", scratch])
        df = run_short(["df", "-h", scratch])
        pieces.append(
            "scratch usage (quota-relevant):\n"
            + (project_du or "(no du output)")
            + "\n\nscratch filesystem df (global):\n"
            + (df or "(no df output)")
        )
    message = "\n\n".join(piece.strip() for piece in pieces if piece.strip())
    return redact(message or "(empty notification)", env)


def resolve_local_path(path_arg: Path) -> Path:
    path = path_arg if path_arg.is_absolute() else Path.cwd() / path_arg
    path = path.resolve()
    assert_safe_local_path(path)
    if not path.is_file():
        raise SystemExit(f"Attachment is not a file: {path}")
    return path


def max_upload_bytes(env: dict[str, str]) -> int:
    # Telegram's standard Bot API caps uploaded documents at 50 MB per file.
    # Keep the default at that ceiling; larger files must be split into parts.
    raw = env.get("TELEAGENT_NOTIFY_MAX_UPLOAD_MB", "50").strip() or "50"
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"Invalid TELEAGENT_NOTIFY_MAX_UPLOAD_MB: {raw}") from None
    return int(value * 1024 * 1024)


def prepare_attachment(args: argparse.Namespace, env: dict[str, str]) -> Attachment | None:
    if not args.image and not args.file:
        return None

    if args.image:
        if not (args.allow_images or bool_env(env, "TELEAGENT_NOTIFY_ALLOW_IMAGES", False)):
            raise SystemExit("Image upload disabled. Re-run with --allow-images for this explicit send.")
        path = resolve_local_path(args.image)
        mime_type = mimetypes.guess_type(path.name)[0] or ""
        if not mime_type.startswith("image/"):
            raise SystemExit(f"--image path does not look like an image: {path}")
        attachment = Attachment(path=path, kind="image", method="sendPhoto", field_name="photo")
    else:
        if not (args.allow_files or bool_env(env, "TELEAGENT_NOTIFY_ALLOW_FILES", False)):
            raise SystemExit("File upload disabled. Re-run with --allow-files for this explicit send.")
        path = resolve_local_path(args.file)
        attachment = Attachment(path=path, kind="file", method="sendDocument", field_name="document")

    max_bytes = max_upload_bytes(env)
    size = attachment.path.stat().st_size
    if size > max_bytes:
        raise SystemExit(
            f"Attachment too large: {size} bytes exceeds TELEAGENT_NOTIFY_MAX_UPLOAD_MB={max_bytes / 1024 / 1024:.1f}"
        )
    return attachment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--secret-env", type=Path, default=None)
    parser.add_argument("--title", required=True)
    parser.add_argument("--message", default="")
    parser.add_argument("--message-file")
    attachment_group = parser.add_mutually_exclusive_group()
    attachment_group.add_argument("--image", type=Path, help="send an image through Telegram")
    attachment_group.add_argument("--file", type=Path, help="send a file through Telegram")
    parser.add_argument("--allow-images", action="store_true", help="explicitly allow this image upload")
    parser.add_argument("--allow-files", action="store_true", help="explicitly allow this file upload")
    parser.add_argument("--level", default="info", choices=["info", "success", "warning", "error"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prefer", choices=["telegram", "email"], default="telegram")
    parser.add_argument("--send-all", action="store_true", help="send to all configured providers instead of first-success fallback")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--include-qstat", action="store_true")
    parser.add_argument("--include-tmux", action="store_true")
    parser.add_argument("--tmux-session", default="")
    parser.add_argument("--tmux-lines", type=int, default=60)
    parser.add_argument("--include-df", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    assert_safe_local_path(repo_root)
    env = load_env(repo_root, args.secret_env)
    title = f"[{args.level.upper()}] {args.title}"
    message = build_message(args, env)
    attachment = prepare_attachment(args, env)

    providers = ["telegram", "email"] if args.prefer == "telegram" else ["email", "telegram"]
    configured = {
        "telegram": telegram_configured(env),
        "email": email_configured(env),
    }

    if args.dry_run:
        record = {
            "ts": int(time.time()),
            "dry_run": True,
            "title": title,
            "message_preview": message[:500],
            "configured": configured,
        }
        if attachment:
            record["attachment"] = attachment.log_record()
        append_jsonl(repo_root, env, record)
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0

    results: list[NotifyResult] = []
    for provider in providers:
        if provider == "telegram":
            result = send_telegram(env, title, message, args.timeout, attachment)
        else:
            if attachment:
                result = NotifyResult("email", False, "attachments are only sent through Telegram")
            else:
                result = send_email(env, title, message, args.timeout)
        results.append(result)
        if result.ok and not args.send_all:
            break

    record = {
        "ts": int(time.time()),
        "dry_run": False,
        "title": title,
        "level": args.level,
        "results": [result.__dict__ for result in results],
    }
    if attachment:
        record["attachment"] = attachment.log_record()
    append_jsonl(repo_root, env, record)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if any(result.ok for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
