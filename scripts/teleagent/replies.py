"""Durable control results: retry sending a reply, never repeat its action."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from . import state, transport


class DeliveryError(RuntimeError):
    operation = "reply_delivery"


def queue_path(args) -> Path | None:
    value = getattr(args, "control_reply_state_path", None)
    return Path(value) if value else None


def _pending(data):
    pending = data.setdefault("pending", [])
    if not isinstance(pending, list) or any(
        not isinstance(item, dict)
        or not all(key in item for key in ("id", "chat_id", "text"))
        for item in pending
    ):
        raise state.StateReadError(
            "Invalid control reply queue; existing replies were preserved."
        )
    return pending


def send(args, token, chat_id, text, **options):
    path = queue_path(args)
    if path is None:
        return transport.send_reply(token, chat_id, text, **options)
    chunks = transport.split_reply(text)
    with state.locked(path):
        data = state.read_json_object(path)
        pending = _pending(data)
        for index, chunk in enumerate(chunks):
            part_options = dict(options)
            if index != len(chunks) - 1:
                part_options.pop("reply_markup", None)
            pending.append(
                {
                    "id": uuid.uuid4().hex,
                    "chat_id": str(chat_id),
                    "text": chunk,
                    "options": part_options,
                    "queued_ts": time.time(),
                }
            )
        state.write_json_object(path, data)
    return {"queued": True}


def drain(args, token, env=None, *, now=None):
    path = queue_path(args)
    if path is None:
        return
    checked = time.time() if now is None else now
    with state.locked(path):
        pending = list(_pending(state.read_json_object(path)))
    blocked = set()
    attempts = 0
    for item in pending:
        destination = (
            item["chat_id"],
            item.get("options", {}).get("message_thread_id"),
        )
        if destination in blocked:
            continue
        if float(item.get("retry_after", 0)) > checked or attempts >= 8:
            blocked.add(destination)
            continue
        attempts += 1
        error = ""
        try:
            transport.send_reply(
                token, item["chat_id"], item["text"], **item.get("options", {})
            )
        except Exception as exc:
            error = transport.short_error(exc, env) or type(exc).__name__
            blocked.add(destination)
        with state.locked(path):
            data = state.read_json_object(path)
            current = _pending(data)
            if error:
                for saved in current:
                    if saved["id"] == item["id"]:
                        count = int(saved.get("attempts", 0)) + 1
                        saved.update(
                            attempts=count,
                            last_error=error,
                            retry_after=checked + min(60, 2 ** min(count, 6)),
                        )
            else:
                data["pending"] = [
                    saved for saved in current if saved["id"] != item["id"]
                ]
            state.write_json_object(path, data)
    with state.locked(path):
        failures = any(
            item.get("last_error") for item in _pending(state.read_json_object(path))
        )
    if failures:
        raise DeliveryError(
            "Replies are waiting for Telegram delivery; retrying automatically."
        )
