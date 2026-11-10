"""Commands that take the provider as an argument: /switch, /usage and /auth.

Every provider is addressed with the same word in the same position, so a new
harness needs no new command. The older provider-named commands (`/claude`,
`/dsh`, `/claude_usage`, `/codex_usage`, `/claude_auth`, `/reauth`) are other
spellings of these and resolve through the same functions.
"""

from __future__ import annotations

from . import claude as _claude
from . import dsh as _dsh
from . import harnesses as _harnesses
from . import messages as _messages
from .harnesses import Harness

PROVIDERS: dict[str, Harness] = {harness.key: harness for harness in _harnesses.ALL}
PROVIDER_CHOICES = "codex|claude|dsh|agy"
# Providers whose account the relay can inspect and sign in.
ACCOUNT_PROVIDERS = (_harnesses.CODEX, _harnesses.CLAUDE)
AUTH_ACTIONS = ("status", "login", "logout", "cancel")
ACCOUNT_COMMANDS = frozenset({"/usage", "/auth", "/reauth"})
SWITCH_COMMANDS = frozenset({"/switch", "/claude", "/dsh"})
USAGE_HELP = "Use /usage [codex|claude]."
AUTH_HELP = "Use /auth [codex|claude] [status|login|logout|cancel]."
SWITCH_HELP = f"Use /switch {PROVIDER_CHOICES} [MODEL] [LEVEL]."


def resolve_account_command(command: str, payload: str, running: Harness) -> tuple[str, str]:
    """Turn /usage, /auth and /reauth into `/<provider>_usage` or `/<provider>_auth ACTION`.

    Without a provider word the command applies to the running agent's
    provider. Other commands pass through unchanged.
    """
    if command not in ACCOUNT_COMMANDS:
        return command, payload
    tokens = payload.lower().split()
    provider = running if running in ACCOUNT_PROVIDERS else _harnesses.CODEX
    if tokens and tokens[0] in PROVIDERS:
        provider = PROVIDERS[tokens.pop(0)]
        if provider not in ACCOUNT_PROVIDERS:
            raise ValueError(
                f"{provider.label} has no account controls in Telegram; use codex or claude."
            )
    if command == "/usage":
        if tokens:
            raise ValueError(USAGE_HELP)
        return f"/{provider.key}_usage", ""
    if command == "/reauth":
        return f"/{provider.key}_auth", "login"
    if len(tokens) > 1 or (tokens and tokens[0] not in AUTH_ACTIONS):
        raise ValueError(AUTH_HELP)
    return f"/{provider.key}_auth", tokens[0] if tokens else "status"


def parse_switch(command: str, payload: str) -> tuple[Harness, str, str, str]:
    """Return the harness, launch model, effort and the provider's own model name."""
    if command == "/switch":
        word, _, rest = payload.strip().partition(" ")
        harness = PROVIDERS.get(word.lower())
        if harness is None:
            raise ValueError(SWITCH_HELP)
    else:
        harness, rest = PROVIDERS[command.lstrip("/")], payload
    if harness is _harnesses.CLAUDE:
        model, effort = _claude.parse_launch_payload(rest)
        return harness, model, effort, _claude.native_model(model)
    if harness is _harnesses.DSH:
        model, effort = _dsh.parse_launch_payload(rest)
        return harness, model, effort, _dsh.native_model(model)
    if harness is _harnesses.AGY and not rest.strip():
        raise ValueError(
            "Name an Antigravity model: /switch agy g-flash|argon|sonnet|opus|oss [LEVEL]."
        )
    model, effort, _explicit = _messages.parse_agent_launch_payload(rest)
    if _harnesses.for_model(model) is not harness:
        raise ValueError(f"{model} is not a {harness.label} model; /models lists the choices.")
    return harness, model, effort, model
