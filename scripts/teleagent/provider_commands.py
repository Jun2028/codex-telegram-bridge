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

PROVIDERS: dict[str, Harness] = {harness.key: harness for harness in _harnesses.PROVIDERS}
PROVIDER_CHOICES = "|".join(PROVIDERS)
# Providers whose account the relay can inspect and sign in.
ACCOUNT_PROVIDERS = (_harnesses.CODEX, _harnesses.CLAUDE)
AUTH_ACTIONS = ("status", "login", "logout", "cancel")
ACCOUNT_COMMANDS = frozenset({"/usage", "/auth", "/reauth"})
SWITCH_COMMANDS = frozenset({"/switch", "/claude", "/dsh"})
USAGE_HELP = "Use /usage [codex|claude]."
AUTH_HELP = "Use /auth [codex|claude] [status|login|logout|cancel]."
SWITCH_HELP = f"Use /switch {PROVIDER_CHOICES} [MODEL] [LEVEL]."


# What the tap-to-select picker offers, in display order. Every combination must
# parse as a /switch command; tests/test_harness_registry.py checks that.
CHOICES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "codex": (("sol", "astra", "luna", "spark", "gpt-5.6-sol", "ds-flash", "ds-pro"),
              ("low", "medium", "high", "xhigh", "max")),
    "claude": (("sonnet", "opus", "haiku"), ("low", "medium", "high", "xhigh", "max")),
    "dsh": (("flash", "pro"), ("off", "low", "high", "max")),
    "agy": (("g-flash", "argon", "sonnet", "opus", "oss"),
            ("default", "low", "medium", "high", "xhigh", "max")),
}
PICK = "pick"
Buttons = list[list[tuple[str, str]]]


def _rows(buttons: list[tuple[str, str]], width: int = 3) -> Buttons:
    return [buttons[index:index + width] for index in range(0, len(buttons), width)]


def picker(parts: list[str]) -> tuple[str, Buttons] | str:
    """One step of choosing provider, model and effort by tapping.

    `parts` is what has been chosen so far. Returns the next question with its
    buttons as (label, callback data), or the finished `/switch` payload.
    """
    provider = PROVIDERS.get(parts[0]) if parts else None
    if provider is None or provider.key not in CHOICES:
        return "Choose a provider. Switching starts a fresh conversation.", _rows(
            [(PROVIDERS[key].label, f"{PICK}:{key}") for key in CHOICES], width=2)
    models, efforts = CHOICES[provider.key]
    back = ("« Back", ":".join([PICK, *parts[:-1]]))
    if len(parts) < 2 or parts[1] not in models:
        return f"{provider.label}: choose a model.", _rows(
            [(model, f"{PICK}:{provider.key}:{model}") for model in models]) + [[(back[0], PICK)]]
    # Offer only the levels this model accepts.
    efforts = tuple(effort for effort in efforts if _valid(provider.key, parts[1], effort))
    if len(parts) < 3 or parts[2] not in efforts:
        return f"{provider.label} {parts[1]}: choose the effort.", _rows(
            [(effort, f"{PICK}:{provider.key}:{parts[1]}:{effort}") for effort in efforts]
        ) + [[back]]
    return _payload(provider.key, parts[1], parts[2])


def _payload(provider: str, model: str, effort: str) -> str:
    return " ".join(filter(None, [provider, model, "" if effort == "default" else effort]))


def _valid(provider: str, model: str, effort: str) -> bool:
    try:
        parse_switch("/switch", _payload(provider, model, effort))
    except ValueError:
        return False
    return True


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
