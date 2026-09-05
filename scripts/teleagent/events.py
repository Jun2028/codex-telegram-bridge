"""Normalize Codex events and keep a turn's Telegram destination immutable."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable


def turn_identity(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if record.get("type") == "turn_context" or (
        record.get("type") == "event_msg" and payload.get("type") == "task_started"
    ):
        return str(payload.get("turn_id") or payload.get("id") or "") or None
    return None


@dataclass
class TurnDelivery:
    """One bot has one agent; a destination belongs to a turn, never a chat hint.

    A cross-chat input in an already-bound turn is ambiguous. Remaining output
    goes to the operator's private chat, never to a group containing another
    chat's context. Normal cross-chat work must wait in the relay FIFO.
    """

    owner_chat_id: str
    turn_id: str = ""
    route_id: str = ""
    chat_id: str = ""
    is_group: bool = False
    topic_id: int | None = None
    source_message_id: int | None = None
    locked: bool = False
    conflicted: bool = False
    recent: list[str] = field(default_factory=list)

    @classmethod
    def restore(cls, state: dict[str, Any], owner_chat_id: str) -> TurnDelivery:
        return cls(
            owner_chat_id=owner_chat_id,
            turn_id=str(state.get("turn_id") or ""),
            route_id=str(state.get("active_route_id") or ""),
            chat_id=str(state.get("active_chat_id") or ""),
            is_group=bool(state.get("active_is_group")),
            topic_id=state.get("active_topic_id"),
            source_message_id=state.get("source_message_id"),
            locked=bool(state.get("route_locked", state.get("active_route_id"))),
            conflicted=bool(state.get("turn_conflicted")),
            recent=list(state.get("recent_message_keys") or [])[-128:],
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "active_route_id": self.route_id,
            "active_chat_id": self.chat_id,
            "active_is_group": self.is_group,
            "active_topic_id": self.topic_id,
            "source_message_id": self.source_message_id,
            "route_locked": self.locked,
            "turn_conflicted": self.conflicted,
            "recent_message_keys": self.recent[-128:],
        }

    def begin(self, identity: str) -> None:
        if identity == self.turn_id:
            return
        if self.locked and (not self.turn_id or self.turn_id.startswith("route:")):
            # Migration/older schemas can bind the user message before Codex
            # supplies its turn ID. Adopt that ID without dropping the route.
            self.turn_id = identity
            return
        self.turn_id = identity
        self.route_id = self.chat_id = ""
        self.is_group = self.locked = self.conflicted = False
        self.topic_id = self.source_message_id = None

    def bind(
        self, route_id: str, lookup: Callable[[str], dict[str, Any] | None]
    ) -> None:
        route = lookup(route_id)
        destination = str((route or {}).get("chat_id") or self.owner_chat_id)
        topic = (route or {}).get("message_thread_id")
        if self.locked:
            if self.route_id == route_id:
                return  # Codex emits the same user message in several schemas.
            if (destination, topic) != (self.chat_id, self.topic_id):
                self.conflicted = True
                self.chat_id = self.owner_chat_id
                self.is_group = False
                self.topic_id = self.source_message_id = None
            return
        if not self.turn_id or self.turn_id.startswith("route:"):
            self.turn_id = "route:" + route_id
        self.route_id = route_id
        self.chat_id = destination
        self.topic_id = topic
        self.source_message_id = (route or {}).get("source_message_id")
        self.is_group = bool((route or {}).get("is_group"))
        self.locked = True

    def message_key(self, text: str, phase: str) -> str:
        # IDs are absent in some Codex event schemas. Content is deduplicated
        # within a turn, while identical replies in later turns remain valid.
        return hashlib.sha256(
            (self.turn_id + "\0" + phase + "\0" + text).encode("utf-8")
        ).hexdigest()

    def delivered(self, key: str, phase: str) -> None:
        self.recent.append(key)
        self.recent = self.recent[-128:]
        if phase == "final_answer":
            self.locked = False
            self.chat_id = self.owner_chat_id
            self.is_group = False
            self.topic_id = self.source_message_id = None
