"""Models services for the Telegram relay."""

from __future__ import annotations


import json
import re
import shlex
import time
from pathlib import Path
import telegram_agent_registry as agent_registry  # noqa: E402

from . import settings as _settings
from . import processes as _processes


def parse_live_reasoning_effort(payload: str) -> str:
    normalized = " ".join(payload.strip().lower().split())
    normalized = _settings.LIVE_CODEX_REASONING_ALIASES.get(normalized, normalized)
    if normalized not in _settings.LIVE_CODEX_REASONING_EFFORTS:
        raise ValueError(
            "live reasoning must be low, medium, high, xhigh, max, or ultra; "
            "use /restart_agent for none or minimal"
        )
    return normalized


def normalize_codex_agent_model(value: str, *, live: bool = False) -> str:
    normalized = value.strip().lower()
    aliases = (
        _settings.LIVE_CODEX_AGENT_MODEL_ALIASES
        if live
        else _settings.CODEX_AGENT_MODEL_ALIASES
    )
    supported = (
        _settings.LIVE_CODEX_AGENT_MODELS
        if live
        else _settings.SUPPORTED_CODEX_AGENT_MODELS
    )
    model = aliases.get(normalized, normalized)
    if model not in supported:
        if live:
            raise ValueError(
                "unknown model; use latest/astra (gpt-6-astra), "
                "sol (gpt-5.6-sol), luna (gpt-5.6-luna), "
                "spark (gpt-5.3-codex-spark), or "
                "ds-flash (deepseek-v4-flash) / ds-pro (deepseek-v4-pro)"
            )
        raise ValueError(
            "unknown model; use latest/astra (gpt-6-astra), "
            "sol (gpt-5.6-sol), luna (gpt-5.6-luna), "
            "spark (gpt-5.3-codex-spark), or "
            "ds-flash (deepseek-v4-flash) / ds-pro (deepseek-v4-pro)"
        )
    return model


def validate_model_reasoning_effort(model: str, effort: str) -> None:
    if (
        model == _settings.ASTRA_CODEX_AGENT_MODEL
        and effort not in _settings.ASTRA_CODEX_REASONING_EFFORTS
    ):
        raise ValueError(
            "GPT-6 Astra reasoning must be low, medium, high, xhigh, or max"
        )
    if model == _settings.SPARK_CODEX_AGENT_MODEL and effort not in {
        "low",
        "medium",
        "high",
        "xhigh",
    }:
        raise ValueError("Spark reasoning must be low, medium, high, or xhigh")
    if (
        model
        in {
            _settings.DEEPSEEK_FLASH_CODEX_AGENT_MODEL,
            _settings.DEEPSEEK_PRO_CODEX_AGENT_MODEL,
        }
        and effort != "max"
    ):
        raise ValueError("DeepSeek reasoning must be max")


def parse_live_model_payload(payload: str) -> tuple[str, str | None]:
    try:
        tokens = shlex.split(payload)
    except ValueError as exc:
        raise ValueError(f"invalid quoting: {exc}") from exc
    if not 1 <= len(tokens) <= 2:
        raise ValueError(
            "usage: /model latest|astra|sol|luna|spark|ds-flash|ds-pro "
            "[low|medium|high|xhigh|max|ultra]"
        )
    model = normalize_codex_agent_model(tokens[0], live=True)
    reasoning_effort = (
        parse_live_reasoning_effort(tokens[1]) if len(tokens) == 2 else None
    )
    if (
        model
        in {
            _settings.DEEPSEEK_FLASH_CODEX_AGENT_MODEL,
            _settings.DEEPSEEK_PRO_CODEX_AGENT_MODEL,
        }
        and reasoning_effort is None
    ):
        reasoning_effort = "max"
    if reasoning_effort is not None:
        validate_model_reasoning_effort(model, reasoning_effort)
    return model, reasoning_effort


def current_codex_model_and_reasoning_effort(
    target_pane: str,
    session_path: str | Path | None = None,
) -> tuple[str, str] | None:
    pane_text = _processes.tmux_tail(target_pane, lines=30)
    matches = re.findall(
        r"\b(gpt-[\w.-]+|deepseek-v4-(?:flash|pro))\s+"
        r"(low|medium|high|xhigh|max|ultra)(?:\s+[·/]|\s*$)",
        pane_text,
        flags=re.MULTILINE,
    )
    if matches:
        return matches[-1]
    if session_path is None:
        meta = agent_registry.active_agent_for_pane(target_pane)
        if meta:
            session_path = meta.get("codex_session_path")
    return codex_session_model_and_reasoning_effort(session_path)


def codex_session_model_and_reasoning_effort(
    session_path: str | Path | None,
) -> tuple[str, str] | None:
    """Return the newest effective model/effort recorded in the session JSONL.

    Codex has written ``turn_context`` both as a top-level record and as an
    ``event_msg`` payload, so both shapes are accepted.  This is authoritative
    when the TUI status line cannot be scraped.
    """
    if not session_path:
        return None
    try:
        handle = open(session_path, encoding="utf-8")
    except OSError:
        return None
    with handle:
        found: tuple[str, str] | None = None
        for raw_line in handle:
            if '"turn_context"' not in raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if (
                record.get("type") != "turn_context"
                and payload.get("type") != "turn_context"
            ):
                continue
            model = payload.get("model")
            effort = payload.get("effort")
            if isinstance(model, str) and model and isinstance(effort, str) and effort:
                found = (model, effort)
    return found


def current_codex_reasoning_effort(target_pane: str) -> str | None:
    current = current_codex_model_and_reasoning_effort(target_pane)
    return current[1] if current else None


def select_codex_reasoning_effort(target_pane: str, effort: str) -> None:
    _processes.tmux_send_keys(target_pane, "Home")
    _processes.wait_for_tmux_text(target_pane, "› 1. Low")
    reasoning_index = _settings.LIVE_CODEX_REASONING_EFFORTS[effort]
    for selected_index in range(1, reasoning_index + 1):
        _processes.tmux_send_keys(target_pane, "Down")
        _processes.wait_for_tmux_text(target_pane, f"› {selected_index + 1}.")
    _processes.tmux_send_keys(target_pane, "Enter")

    if effort in {"max", "ultra"}:
        _processes.wait_for_tmux_text(target_pane, "Advanced Reasoning")
        _processes.tmux_send_keys(target_pane, "Home")
        _processes.wait_for_tmux_text(target_pane, "› 1. Max")
        if effort == "ultra":
            _processes.tmux_send_keys(target_pane, "Down")
            _processes.wait_for_tmux_text(target_pane, "› 2. Ultra")
        _processes.tmux_send_keys(target_pane, "Enter")


def restore_codex_composer(target_pane: str) -> None:
    for _ in range(3):
        try:
            _processes.tmux_send_keys(target_pane, "Escape")
        except Exception:
            break
    try:
        _processes.tmux_send_keys(target_pane, "C-u")
    except Exception:
        pass


def set_codex_model(
    target_pane: str,
    model: str,
    reasoning_effort: str | None = None,
) -> tuple[str, str]:
    selected_model = normalize_codex_agent_model(model, live=True)
    default_effort = (
        "max"
        if selected_model
        in {
            _settings.DEEPSEEK_FLASH_CODEX_AGENT_MODEL,
            _settings.DEEPSEEK_PRO_CODEX_AGENT_MODEL,
        }
        else "high"
    )
    selected_effort = parse_live_reasoning_effort(reasoning_effort or default_effort)
    validate_model_reasoning_effort(selected_model, selected_effort)
    if not _processes.tmux_target_exists(target_pane):
        raise RuntimeError(f"tmux target not found: {target_pane}")
    if not _processes.tmux_pane_has_codex_process(target_pane):
        raise RuntimeError(f"target {target_pane} is not running Codex")

    try:
        _processes.tmux_send_keys(target_pane, "C-u")
        _processes.tmux_send_keys(target_pane, "/model", literal=True)
        time.sleep(1.0)
        _processes.tmux_send_keys(target_pane, "Enter")
        model_menu = _processes.wait_for_tmux_text(
            target_pane, "Select Model and Effort"
        )
        match = re.search(
            rf"^\s*(?:›\s*)?(\d+)\.\s+{re.escape(selected_model)}(?:\s|$)",
            model_menu,
            flags=re.MULTILINE,
        )
        if not match:
            if selected_model in {
                _settings.DEEPSEEK_FLASH_CODEX_AGENT_MODEL,
                _settings.DEEPSEEK_PRO_CODEX_AGENT_MODEL,
            }:
                raise RuntimeError(
                    "the running pane is not DeepSeek-backed; "
                    "use /restart_agent ds-flash or ds-pro to relaunch it as DeepSeek"
                )
            raise RuntimeError(f"Codex model selector does not offer {selected_model}")

        model_index = int(match.group(1))
        _processes.tmux_send_keys(target_pane, "Home")
        _processes.wait_for_tmux_text(target_pane, "› 1.")
        for selected_index in range(1, model_index):
            _processes.tmux_send_keys(target_pane, "Down")
            _processes.wait_for_tmux_text(target_pane, f"› {selected_index + 1}.")
        _processes.tmux_send_keys(target_pane, "Enter")
        _processes.wait_for_tmux_text(
            target_pane,
            f"Select Reasoning Level for {selected_model}",
        )

        select_codex_reasoning_effort(target_pane, selected_effort)

        deadline = time.time() + 5.0
        while time.time() <= deadline:
            current = current_codex_model_and_reasoning_effort(target_pane)
            if current == (selected_model, selected_effort):
                return current
            time.sleep(0.1)
        current = current_codex_model_and_reasoning_effort(target_pane)
        actual = f"{current[0]} {current[1]}" if current else "unknown"
        raise RuntimeError(
            f"Codex did not confirm model={selected_model}, "
            f"reasoning={selected_effort}; current={actual}"
        )
    except Exception:
        restore_codex_composer(target_pane)
        raise


def set_codex_reasoning_effort(target_pane: str, effort: str) -> str:
    selected_effort = parse_live_reasoning_effort(effort)
    if not _processes.tmux_target_exists(target_pane):
        raise RuntimeError(f"tmux target not found: {target_pane}")
    if not _processes.tmux_pane_has_codex_process(target_pane):
        raise RuntimeError(f"target {target_pane} is not running Codex")
    current = current_codex_model_and_reasoning_effort(target_pane)
    if current:
        validate_model_reasoning_effort(current[0], selected_effort)

    try:
        _processes.tmux_send_keys(target_pane, "C-u")
        _processes.tmux_send_keys(target_pane, "/model", literal=True)
        # Codex TUI treats an immediate Enter after pasted text as an edit
        # event; a short delay makes it submit the slash command reliably.
        time.sleep(1.0)
        _processes.tmux_send_keys(target_pane, "Enter")
        _processes.wait_for_tmux_text(target_pane, "Select Model and Effort")

        # /model opens with the active model selected. Confirm it unchanged,
        # then choose only the reasoning level for the current chat.
        _processes.tmux_send_keys(target_pane, "Enter")
        _processes.wait_for_tmux_text(target_pane, "Select Reasoning Level for")
        select_codex_reasoning_effort(target_pane, selected_effort)

        deadline = time.time() + 5.0
        while time.time() <= deadline:
            actual_effort = current_codex_reasoning_effort(target_pane)
            if actual_effort == selected_effort:
                return actual_effort
            time.sleep(0.1)
        raise RuntimeError(
            f"Codex did not confirm reasoning={selected_effort}; "
            f"current={current_codex_reasoning_effort(target_pane) or 'unknown'}"
        )
    except Exception:
        # Leave the TUI in its composer instead of stranding it inside a
        # partially navigated selector when a future CLI changes the UI.
        restore_codex_composer(target_pane)
        raise
