"""Commands that take the provider as an argument: /switch, /usage and /auth.

Every provider is addressed with the same word in the same position, so a new
harness needs no new command. The older provider-named commands (`/claude`,
`/dsh`, `/claude_usage`, `/codex_usage`, `/claude_auth`, `/reauth`) are other
spellings of these and resolve through the same functions.
"""

from __future__ import annotations

import shutil

from . import agy as _agy
from . import claude as _claude
from . import dsh as _dsh
from . import harnesses as _harnesses
from . import messages as _messages
from . import processes as _processes
from . import settings as _settings
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
    "claude": (("fable", "opus", "sonnet", "haiku"), ("low", "medium", "high", "xhigh", "max")),
    "dsh": (("flash", "pro"), ("off", "low", "high", "max")),
    "agy": (("g-flash", "argon", "sonnet", "opus", "oss"),
            ("default", "low", "medium", "high", "xhigh", "max")),
}
PICK = "pick"
SIGN_IN_STEP = "signin"
Buttons = list[list[tuple[str, str]]]
# What stands between this machine and starting a provider.
READY, SIGN_IN, MISSING = "", "sign in", "not installed"


def readiness(key: str) -> str:
    """What stops this machine from starting the provider now; READY when nothing does."""
    if key == "claude":
        try:
            executable = _claude.binary()
        except RuntimeError:
            return MISSING
        return READY if _claude.signed_in(executable) else SIGN_IN
    if key == "dsh":
        return _dsh.blocker()
    if key == "agy":
        return READY if shutil.which(_agy.agy_binary()) else MISSING
    try:
        _processes.codex_executable()
    except RuntimeError:
        return MISSING
    return READY


def _rows(buttons: list[tuple[str, str]], width: int = 3) -> Buttons:
    return [buttons[index:index + width] for index in range(0, len(buttons), width)]


def model_label(provider: str, model: str) -> str:
    """The provider's own name for a model the picker lists by a short word."""
    if provider == "claude":
        return _claude.display_name(model, _processes.agent_registry.telegram_log_dir())
    if provider == "dsh":
        return _dsh.native_model(_dsh.normalize_model(model))
    if provider == "codex":
        return _settings.CODEX_AGENT_MODEL_ALIASES.get(model, model)
    return model


def picker(parts: list[str], state=None, name=None) -> tuple[str, Buttons] | str:
    """One step of choosing provider, model and effort by tapping.

    `parts` is what has been chosen so far. Returns the next question with its
    buttons as (label, callback data), or the finished command to run. The
    first screen already says which providers this machine cannot start, and a
    provider that only needs signing in offers that instead of its models.
    """
    state = state or readiness
    name = name or model_label
    provider = PROVIDERS.get(parts[0]) if parts else None
    if provider is None or provider.key not in CHOICES:
        buttons = []
        for key in CHOICES:
            blocker = state(key)
            buttons.append((PROVIDERS[key].label + (f" · {blocker}" if blocker else ""), f"{PICK}:{key}"))
        return "Choose a provider. Switching starts a fresh conversation.", _rows(buttons, width=2)
    models, efforts = CHOICES[provider.key]
    back = ("« Back", ":".join([PICK, *parts[:-1]]))
    if len(parts) == 2 and parts[1] == SIGN_IN_STEP and provider in ACCOUNT_PROVIDERS:
        return f"/auth {provider.key} login"
    if len(parts) == 1:
        blocker = state(provider.key)
        if blocker == SIGN_IN:
            return f"{provider.label} is not signed in on this machine.", [
                [("Sign in", f"{PICK}:{provider.key}:{SIGN_IN_STEP}")], [(back[0], PICK)]]
        if blocker:
            return f"{provider.label} cannot start on this machine: {blocker}.", [[(back[0], PICK)]]
    if len(parts) < 2 or parts[1] not in models:
        return f"{provider.label}: choose a model.", _rows(
            [(name(provider.key, model), f"{PICK}:{provider.key}:{model}") for model in models],
            width=2) + [[(back[0], PICK)]]
    # Offer only the levels this model accepts.
    efforts = tuple(effort for effort in efforts if _valid(provider.key, parts[1], effort))
    if len(parts) < 3 or parts[2] not in efforts:
        return f"{provider.label} {name(provider.key, parts[1])}: choose the effort.", _rows(
            [(effort, f"{PICK}:{provider.key}:{parts[1]}:{effort}") for effort in efforts]
        ) + [[back]]
    return "/switch " + _payload(provider.key, parts[1], parts[2])


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
    if harness is _harnesses.CODEX and not rest.strip() and _harnesses.for_model(model) is not harness:
        # The default agent is another provider; plain /switch codex starts Codex's own default.
        model = _settings.LATEST_OPENAI_CODEX_AGENT_MODEL
        effort = _settings.LATEST_OPENAI_CODEX_AGENT_REASONING_EFFORT
    if _harnesses.for_model(model) is not harness:
        raise ValueError(f"{model} is not a {harness.label} model; /models lists the choices.")
    return harness, model, effort, model
