"""Transport services for the Telegram relay."""

from __future__ import annotations


import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from notify import redact, tls_context
from telegram_format import render_telegram_html

from . import settings as _settings


class TransientTelegramError(RuntimeError):
    """Telegram/network condition that should recover on retry."""


def short_error(
    exc: BaseException, env: dict[str, str] | None = None, max_chars: int = 500
) -> str:
    message = str(exc)
    if env is not None:
        message = redact(message, env)
    return message[:max_chars]


def telegram_api(
    token: str, method: str, params: dict[str, Any] | None = None, timeout: int = 35
) -> Any:
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
        with urllib.request.urlopen(
            request, timeout=timeout + 5, context=tls_context()
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        error = f"Telegram {method} failed with HTTP {exc.code}: {body[:300]}"
        if exc.code in _settings.TRANSIENT_HTTP_CODES:
            raise TransientTelegramError(error) from None
        raise RuntimeError(error) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransientTelegramError(f"Telegram {method} failed: {reason}") from None

    if not payload.get("ok"):
        raise RuntimeError(
            f"Telegram {method} failed: {payload.get('description', payload)}"
        )
    return payload.get("result")


def send_reply(
    token: str,
    chat_id: str,
    text: str,
    timeout: int = 15,
    *,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> Any:
    # Telegram counts UTF-16 units. Never silently lose the end of a result or
    # cut an emoji in half. Each part retains the same chat, topic and reply.
    chunks = split_reply(text)
    result = None
    for index, chunk in enumerate(chunks):
        result = _send_reply_chunk(
            token,
            chat_id,
            chunk,
            timeout,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )
    return result


def split_reply(text: str, limit: int = 3800) -> list[str]:
    """Split on line boundaries when possible, retaining every character."""
    if limit < 64:
        raise ValueError("message limit is too small")
    result = []
    remaining = text
    while remaining:
        units = 0
        end = 0
        for character in remaining:
            units += 2 if ord(character) > 0xFFFF else 1
            if units > limit:
                break
            end += 1
        if end < len(remaining):
            newline = remaining.rfind("\n", 0, end)
            if newline >= end // 2:
                end = newline + 1
        result.append(remaining[:end])
        remaining = remaining[end:]
    return result or [""]


def _send_reply_chunk(
    token,
    chat_id,
    plain_text,
    timeout,
    *,
    reply_to_message_id=None,
    message_thread_id=None,
    reply_markup=None,
):
    params = {
        "chat_id": chat_id,
        "text": render_telegram_html(plain_text),
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "reply_markup": json.dumps(
            _settings.REPLY_KEYBOARD_REMOVAL, separators=(",", ":")
        ),
    }
    if message_thread_id is not None:
        params["message_thread_id"] = int(message_thread_id)
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup)
    if reply_to_message_id is not None:
        params["reply_parameters"] = json.dumps(
            {
                "message_id": int(reply_to_message_id),
                "allow_sending_without_reply": True,
            },
            separators=(",", ":"),
        )
    try:
        return telegram_api(token, "sendMessage", params, timeout=timeout)
    except TransientTelegramError:
        raise
    except RuntimeError as exc:
        error = str(exc).lower()
        formatting_error = any(
            marker in error
            for marker in (
                "can't parse entities",
                "cannot parse entities",
                "unsupported start tag",
                "wrong html",
                "entity bounds",
            )
        )
        if not formatting_error:
            raise
        fallback_params = dict(params)
        fallback_params.pop("parse_mode", None)
        fallback_params["text"] = plain_text
        return telegram_api(
            token,
            "sendMessage",
            fallback_params,
            timeout=timeout,
        )
