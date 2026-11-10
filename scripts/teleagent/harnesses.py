"""The agent harnesses this relay can run, described in one place.

A harness is the program that owns the agent pane: the Codex TUI, or a small
runner that drives another provider's CLI. Facts the rest of the relay needs
about a harness live here so a new provider is one new entry instead of a new
branch in every module. `docs/architecture.md` lists the remaining steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import claude as _claude
from . import dsh as _dsh


def _is_agy_model(model: str) -> bool:
    from . import settings  # settings derives its process names from this module

    return settings.is_agy_model(model)


@dataclass(frozen=True)
class Harness:
    key: str
    # Name shown to the operator in Telegram replies.
    label: str
    # /proc/<pid>/comm of the pane runtime; how the relay recognises the harness.
    process_comm: str
    # Environment variable the launcher uses to hand over the executable.
    launch_binary_env: str
    # Whether the OpenAI Codex sign-in gates this harness. A failed or pending
    # Codex sign-in must not block a provider that authenticates on its own.
    uses_codex_auth: bool
    # Whether /model, /reasoning, /codex and Goal mode act on the running pane.
    live_codex_controls: bool
    # Launch-model predicate. Codex is the fallback and matches nothing itself.
    owns_model: Callable[[str], bool]


CODEX = Harness(
    key="codex",
    label="Codex",
    process_comm="codex",
    launch_binary_env="TELEAGENT_CODEX_BIN",
    uses_codex_auth=True,
    live_codex_controls=True,
    owns_model=lambda model: False,
)

CLAUDE = Harness(
    key="claude",
    label="Claude Code",
    process_comm="codex-claude",
    launch_binary_env="TELEAGENT_CLAUDE_BIN",
    uses_codex_auth=False,
    live_codex_controls=False,
    owns_model=_claude.is_claude_model,
)

DSH = Harness(
    key="dsh",
    label="DeepSeek Harness",
    process_comm="codex-dsh",
    launch_binary_env="TELEAGENT_DSH_BIN",
    uses_codex_auth=False,
    live_codex_controls=False,
    owns_model=_dsh.is_dsh_model,
)

AGY = Harness(
    key="agy",
    label="Antigravity",
    process_comm="codex-agy",
    # The agy runner is started through the Codex launcher slot.
    launch_binary_env="TELEAGENT_CODEX_BIN",
    uses_codex_auth=False,
    live_codex_controls=False,
    owns_model=_is_agy_model,
)

# Order matters only for `for_model`: the first owner wins, Codex is the fallback.
ALL: tuple[Harness, ...] = (CLAUDE, DSH, AGY, CODEX)

PROCESS_COMMANDS: frozenset[str] = frozenset(harness.process_comm for harness in ALL)


def for_model(model: str) -> Harness:
    """Return the harness that runs a normalized launch model."""
    for harness in ALL:
        if harness.owns_model(model):
            return harness
    return CODEX


def for_process_comm(command_name: str) -> Harness | None:
    for harness in ALL:
        if harness.process_comm == command_name:
            return harness
    return None
