"""Antigravity CLI (agy) harness support for the Telegram relay.

The agy harness mirrors the DeepSeek harness: a model family that the relay
can select for an agent pane.  Instead of running the Codex TUI, an agy-backed
pane runs a small runner that drives ``agy`` in headless (print) mode and
translates its NDJSON stream into tele-agent agent messages.

Short aliases such as ``g-flash`` (latest Gemini Flash) and ``argon`` (latest
Argon) resolve against the signed-in account's live model table, so they keep
tracking new releases without a code change.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator


class AgyError(RuntimeError):
    """Base error for agy harness operations."""


class AgyModelUnavailable(AgyError):
    """Raised when an alias cannot be resolved against the live model table."""


# Short alias -> ordered family markers that must all appear in a model id.
FAMILY_ALIASES: dict[str, tuple[str, ...]] = {
    "g-flash": ("gemini", "flash"),
    "argon": ("gemini", "argon"),
    "sonnet": ("claude", "sonnet"),
    "opus": ("claude", "opus"),
    "oss": ("gpt-oss",),
}

LEVEL_SUFFIXES = ("-low", "-medium", "-high", "-xhigh", "-max")


def agy_binary() -> str:
    """Return the configured agy executable."""
    return os.environ.get("TELEAGENT_AGY_BIN", "agy")


def parse_models_table(text: str) -> list[tuple[str, str]]:
    """Parse ``agy models`` output into (model id, display name) pairs."""
    models: list[tuple[str, str]] = []
    for raw in text.splitlines():
        if "\t" not in raw:
            continue
        model_id, _, name = raw.partition("\t")
        model_id = model_id.strip()
        name = name.strip()
        if model_id and name:
            models.append((model_id, name))
    return models


def list_models(agy_bin: str | None = None, timeout: float = 40.0) -> list[tuple[str, str]]:
    """Return the signed-in account's live agy model table."""
    binary = agy_bin or agy_binary()
    try:
        completed = subprocess.run(
            [binary, "models"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AgyError(f"agy CLI not found: {binary}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise AgyError(
            f"agy models failed (rc={completed.returncode}): "
            f"{detail[-1] if detail else 'no output'}"
        )
    return parse_models_table(completed.stdout)


def _version_key(model_id: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", model_id))


def _family_matches(model_id: str, markers: tuple[str, ...]) -> bool:
    lowered = model_id.lower()
    return all(marker in lowered for marker in markers)


def _base_model_id(model_id: str) -> str:
    """Strip a thinking-level suffix so the level can come from --effort."""
    for suffix in LEVEL_SUFFIXES:
        if model_id.lower().endswith(suffix):
            return model_id[: -len(suffix)]
    return model_id


def resolve_model_id(name: str, models: list[tuple[str, str]]) -> str:
    """Resolve an alias such as ``g-flash`` to a concrete agy model id.

    Unknown names pass through unchanged so explicit model ids keep working.
    Alias resolution always picks the newest version available on the
    account, preferring the base model name over level-suffixed variants.
    """
    normalized = name.strip().lower()
    markers = FAMILY_ALIASES.get(normalized)
    if markers is None:
        return name.strip()
    candidates = [
        model_id for model_id, _ in models if _family_matches(model_id, markers)
    ]
    if not candidates:
        raise AgyModelUnavailable(
            f"agy does not offer {normalized!r} on this account"
        )
    newest = max(_version_key(model_id) for model_id in candidates)
    latest = sorted(
        model_id for model_id in candidates if _version_key(model_id) == newest
    )
    for model_id in latest:
        if _base_model_id(model_id) == model_id:
            return model_id
    return _base_model_id(latest[0])


def resolve_model(name: str, agy_bin: str | None = None) -> str:
    """Resolve an alias against the live agy model table."""
    if name.strip().lower() not in FAMILY_ALIASES:
        return name.strip()
    return resolve_model_id(name, list_models(agy_bin))


def build_turn_command(
    *,
    prompt: str,
    model: str,
    agy_bin: str | None = None,
    effort: str | None = None,
    conversation_id: str | None = None,
    skip_permissions: bool = True,
) -> list[str]:
    """Build the argv for one agy print-mode turn."""
    command = [
        agy_bin or agy_binary(),
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--model",
        model,
    ]
    if effort:
        command += ["--effort", effort]
    if conversation_id:
        command += ["--conversation", conversation_id]
    if skip_permissions:
        command.append("--dangerously-skip-permissions")
    return command


def iter_stream_events(lines: Iterable[str]) -> Iterator[dict]:
    """Yield JSON objects from agy's NDJSON stream, skipping noise lines."""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


@dataclass
class AgyTurnResult:
    """Result of one headless agy turn."""

    conversation_id: str = ""
    status: str = ""
    text: str = ""
    error: str = ""
    events: list[dict] = field(default_factory=list)


def _result_payload(event: dict) -> dict:
    nested = event.get("result")
    return nested if isinstance(nested, dict) else event


def apply_stream_event(result: AgyTurnResult, event: dict, deltas: list[str]) -> None:
    """Fold one stream event into the accumulated turn result."""
    kind = str(event.get("event") or "")
    if kind == "init":
        result.conversation_id = result.conversation_id or str(
            event.get("conversation_id") or ""
        )
        return
    if kind == "step_update":
        step = event.get("step_update")
        if isinstance(step, dict):
            if step.get("step_type") == "agent_response" and step.get("text_delta"):
                deltas.append(str(step["text_delta"]))
            if step.get("state") == "DONE" and not result.conversation_id:
                result.conversation_id = str(step.get("conversation_id") or "")
        return
    if kind == "result":
        payload = _result_payload(event)
        result.status = str(payload.get("status") or "")
        result.text = str(payload.get("response") or result.text)
        result.error = str(payload.get("error") or "")
        result.conversation_id = result.conversation_id or str(
            payload.get("conversation_id") or ""
        )


def run_turn(
    *,
    prompt: str,
    model: str,
    effort: str | None = None,
    conversation_id: str | None = None,
    agy_bin: str | None = None,
    skip_permissions: bool = True,
    on_event: Callable[[dict], None] | None = None,
) -> AgyTurnResult:
    """Run one headless agy turn and return the accumulated result."""
    command = build_turn_command(
        prompt=prompt,
        model=model,
        agy_bin=agy_bin,
        effort=effort,
        conversation_id=conversation_id,
        skip_permissions=skip_permissions,
    )
    result = AgyTurnResult()
    deltas: list[str] = []
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
        )
        assert process.stdout is not None
        try:
            for event in iter_stream_events(process.stdout):
                result.events.append(event)
                apply_stream_event(result, event, deltas)
                if on_event is not None:
                    on_event(event)
        finally:
            returncode = process.wait()
        if not result.text and deltas:
            result.text = "".join(deltas).strip()
        if not result.status:
            result.status = "SUCCESS" if returncode == 0 else "ERROR"
        if returncode != 0 and not result.error:
            stderr_file.seek(0)
            detail = stderr_file.read().strip().splitlines()
            result.error = detail[-1] if detail else f"agy exited rc={returncode}"
    return result


def _main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"models", "resolve", "run"}:
        print("usage: python -m teleagent.agy models | resolve ALIAS | run MODEL PROMPT")
        return 2
    if argv[0] == "models":
        for model_id, name in list_models():
            print(f"{model_id}\t{name}")
        return 0
    if argv[0] == "resolve":
        try:
            print(resolve_model(argv[1] if len(argv) > 1 else ""))
        except AgyError as exc:
            print(f"agy error: {exc}", file=sys.stderr)
            return 1
        return 0
    if len(argv) < 3:
        print("usage: python -m teleagent.agy run MODEL PROMPT")
        return 2
    outcome = run_turn(model=argv[1], prompt=argv[2])
    print(json.dumps(outcome.__dict__, ensure_ascii=False))
    return 0 if outcome.status == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
