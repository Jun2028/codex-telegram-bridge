"""Durable inbound delivery and independent I/O for ONE agent per bot.

The poller stores updates before acknowledging Telegram. One worker owns agent
controls, composer input, recovery and timers. Another forwards replies without
waiting for slow controls. Neither worker creates a Codex conversation.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from . import state


class MaintenanceError(RuntimeError):
    def __init__(self, operation, cause):
        self.operation = operation
        super().__init__(f"{operation} check failed ({type(cause).__name__})")


class Inbox:
    def __init__(self, path: Path):
        self.path = path

    def recover(self) -> int:
        """Do not repeat an action whose outcome is unknown after a crash."""
        with state.locked(self.path):
            data = state.read_json_object(self.path)
            interrupted = [
                item for item in data.get("pending", []) if item.get("started_ts")
            ]
            data["pending"] = [
                item for item in data.get("pending", []) if not item.get("started_ts")
            ]
            for item in interrupted:
                item["error"] = (
                    "Listener restarted during delivery; check /queue before resending."
                )
            data["failed"] = (data.get("failed", []) + interrupted)[-100:]
            state.write_json_object(self.path, data)
            return len(interrupted)

    def accept(self, update: dict) -> bool:
        update_id = int(update["update_id"])
        with state.locked(self.path):
            data = state.read_json_object(self.path)
            pending = data.setdefault("pending", [])
            if update_id in data.get("completed", []) or any(
                item["update"]["update_id"] == update_id
                for item in pending + data.get("failed", [])
            ):
                return False
            pending.append({"update": update, "received_ts": time.time()})
            state.write_json_object(self.path, data)
            return True

    def take(self) -> dict | None:
        with state.locked(self.path):
            data = state.read_json_object(self.path)
            pending = data.get("pending", [])
            if not pending:
                return None
            item = pending[0]
            if item.get("started_ts"):
                raise RuntimeError(
                    "A prior request has an unconfirmed outcome; it will not be executed again."
                )
            item["started_ts"] = time.time()
            state.write_json_object(self.path, data)
            return item["update"]

    def finish(self, update_id: int, error: str = "") -> None:
        with state.locked(self.path):
            data = state.read_json_object(self.path)
            pending = data.get("pending", [])
            item = next(
                (item for item in pending if item["update"]["update_id"] == update_id),
                None,
            )
            data["pending"] = [
                item for item in pending if item["update"]["update_id"] != update_id
            ]
            if error and item:
                item["error"] = error
                data["failed"] = (data.get("failed", []) + [item])[-100:]
            data["completed"] = (data.get("completed", []) + [update_id])[-1000:]
            state.write_json_object(self.path, data)


class RelayWorkers:
    def __init__(
        self,
        inbox: Inbox,
        dispatch: Callable,
        maintain: Callable,
        deliver: Callable,
        on_error: Callable,
        health_path: Path,
    ):
        self.inbox, self.dispatch, self.maintain = inbox, dispatch, maintain
        self.deliver, self.on_error, self.health_path = deliver, on_error, health_path
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.threads: list[threading.Thread] = []
        self.health: dict = {}
        self.health_lock = threading.Lock()
        self.health_written = 0.0

    def mark(self, **values) -> None:
        with self.health_lock:
            error_changed = any(
                key.endswith("_error") and self.health.get(key) != value
                for key, value in values.items()
            )
            self.health.update(values)
            if (
                "control_update_id" in values
                or error_changed
                or time.monotonic() - self.health_written >= 2
            ):
                state.write_json_object(self.health_path, self.health)
                self.health_written = time.monotonic()

    def control_once(self) -> None:
        update = self.inbox.take()
        if update is not None:
            self.mark(
                control_started_ts=time.time(), control_update_id=update["update_id"]
            )
            try:
                self.dispatch(update)
            except Exception as exc:
                detail = None
                try:
                    detail = self.on_error("handle_update_failed", exc, update)
                finally:
                    self.inbox.finish(
                        update["update_id"],
                        detail
                        or "Request outcome unconfirmed; check /queue before retrying.",
                    )
            else:
                self.inbox.finish(update["update_id"])
            finally:
                self.mark(control_update_id=None, control_finished_ts=time.time())
        self.maintain()

    def _run(self, name: str, action: Callable, interval: float) -> None:
        while not self.stop.is_set():
            try:
                action()
                self.mark(**{name + "_ok_ts": time.time(), name + "_error": None})
            except Exception as exc:
                self.on_error(name + "_failed", exc, None)
                self.mark(
                    **{
                        name + "_error_ts": time.time(),
                        name + "_error": type(exc).__name__,
                    }
                )
            self.wake.wait(interval)
            self.wake.clear()

    def start(self) -> None:
        for name, action, interval in (
            ("control", self.control_once, 0.5),
            ("delivery", self.deliver, 0.5),
        ):
            thread = threading.Thread(
                target=self._run,
                args=(name, action, interval),
                name="relay-" + name,
                daemon=True,
            )
            self.threads.append(thread)
            thread.start()

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        for thread in self.threads:
            thread.join(timeout=3)
