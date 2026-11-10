"""The relay's environment, computed without a shell.

`scripts/relay_paths.sh` does this for the bash launchers. Hosts without bash
use this module through `scripts/relay.py`; `tests/test_environment.py` checks
on Linux that both produce the same values. Standard library only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_PLAIN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_BRACED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^${}]*))?\}")
ACCESS_MODES = ("full-access", "chat-only")
# A named bot never inherits these from the shell that launched it.
_INSTANCE_PRIVATE = (
    "TELEAGENT_CODEX_BIN", "TELEAGENT_CODEX_CHECK_FOR_UPDATE_ON_STARTUP",
    "TELEAGENT_CODEX_ACCESS_MODE", "TELEAGENT_CODEX_HOME", "TELEAGENT_CODEX_MODEL",
    "TELEAGENT_CODEX_SOURCE_HOME", "TELEAGENT_CHAT_ONLY_CODEX_HOME",
    "TELEAGENT_CHAT_ONLY_WORKDIR", "TELEAGENT_CODEX_REASONING_EFFORT",
    "TELEAGENT_CODEX_WINDOW", "TELEAGENT_DS_CODEX_HOME", "TELEAGENT_DS_KEY_FILE",
    "TELEAGENT_DSH_BIN", "TELEAGENT_DSH_HOME", "TELEAGENT_DSH_VERSION",
    "TELEAGENT_INBOX_TARGET", "TELEAGENT_INSTANCE_CONTEXT", "TELEAGENT_LOG_DIR",
    "TELEAGENT_MACHINE_CONTEXT", "TELEAGENT_SCRATCH", "TELEAGENT_SECRET_ENV",
    "TELEAGENT_MINI_APP_URL_FILE", "TELEAGENT_PERSONALITY_FILE", "TELEAGENT_TMUX_SESSION",
)
_AGENT_BINDING = (
    "TELEAGENT_AGENT_DIR", "TELEAGENT_AGENT_ID", "TELEAGENT_AGENT_JSONL",
    "TELEAGENT_AGENT_META", "TELEAGENT_AGENT_OUTBOX", "TELEAGENT_AGENT_TARGET_PANE",
)


def _lookup(env: dict[str, str], name: str) -> str:
    if name == "HOME" and not env.get("HOME"):
        return str(Path.home())
    return env.get(name, "")


def expand(value: str, env: dict[str, str]) -> str:
    """Expand the `$NAME`, `${NAME}` and `${NAME:-default}` forms the config files use."""
    text = value.strip()
    if text[:1] == "'":
        return text[1:].split("'", 1)[0]  # Single quotes are literal.
    if text[:1] == '"':
        text = text[1:].rsplit('"', 1)[0]
    else:
        text = re.split(r"\s+#", text, maxsplit=1)[0].strip()
    text = _PLAIN.sub(lambda match: _lookup(env, match[1]), text)
    while True:
        expanded = _BRACED.sub(
            lambda match: _lookup(env, match[1]) or (match[2] or ""), text
        )
        if expanded == text:
            return text
        text = expanded


def read_env_file(path: Path, env: dict[str, str]) -> None:
    """Apply the assignments of a `relay.env` file; other shell syntax is ignored."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match and not line.lstrip().startswith("#"):
            env[match[1]] = expand(match[2], env)


def _default(env: dict[str, str], name: str, value: str) -> None:
    if not env.get(name):
        env[name] = value


def _resolved(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(path)))


def build(repo: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Return the full environment for one bot, as relay_paths.sh would export it."""
    env = dict(os.environ if base is None else base)
    home = _lookup(env, "HOME")
    _default(env, "TELEAGENT_REPO", str(repo))
    repo_text = env["TELEAGENT_REPO"]
    _default(env, "TELEAGENT_INSTANCE", "main")
    instance = env["TELEAGENT_INSTANCE"]
    main = instance == "main"
    if not main:
        for name in ("TELEAGENT_CODEX_HOME", "TELEAGENT_CODEX_SOURCE_HOME", "TELEAGENT_DS_KEY_FILE"):
            env.pop(name, None)
    read_env_file(Path(repo_text) / "config" / "relay.env", env)
    main_codex_home = env.get("TELEAGENT_CODEX_HOME") or f"{home}/.codex"
    main_ds_key_file = env.get("TELEAGENT_DS_KEY_FILE", "")
    if not main:
        if env.get("TELEAGENT_PRESERVE_AGENT_BINDING") != "1":
            for name in _AGENT_BINDING:
                env.pop(name, None)
        for name in _INSTANCE_PRIVATE:
            env.pop(name, None)
    env.pop("TELEAGENT_PRESERVE_AGENT_BINDING", None)
    if not main:
        read_env_file(Path(repo_text) / "config" / f"relay-{instance}.env", env)
        _default(env, "TELEAGENT_DS_KEY_FILE", main_ds_key_file)

    _default(env, "TELEAGENT_CODEX_ACCESS_MODE", "full-access")
    if env["TELEAGENT_CODEX_ACCESS_MODE"] not in ACCESS_MODES:
        raise ValueError("TELEAGENT_CODEX_ACCESS_MODE must be full-access or chat-only")

    if main:
        _default(env, "TELEAGENT_TMUX_SESSION", "tele-agent")
        _default(env, "TELEAGENT_SCRATCH", f"{home}/.local/share/tele-agent")
        _default(env, "TELEAGENT_LOG_DIR", env["TELEAGENT_SCRATCH"] + "/runtime")
        _default(env, "TELEAGENT_CODEX_HOME", f"{home}/.codex")
        _default(env, "TELEAGENT_CODEX_SOURCE_HOME", env["TELEAGENT_CODEX_HOME"])
    else:
        # A named bot takes its paths only from its own TELEAGENT_INSTANCE_* overrides.
        env["TELEAGENT_TMUX_SESSION"] = (
            env.get("TELEAGENT_INSTANCE_TMUX_SESSION") or f"tele-agent-{instance}"
        )
        env["TELEAGENT_SCRATCH"] = (
            env.get("TELEAGENT_INSTANCE_SCRATCH") or f"{home}/.local/share/tele-agent-{instance}"
        )
        env["TELEAGENT_LOG_DIR"] = (
            env.get("TELEAGENT_INSTANCE_LOG_DIR") or env["TELEAGENT_SCRATCH"] + "/runtime"
        )
        _default(env, "TELEAGENT_CODEX_SOURCE_HOME", main_codex_home)
        _default(env, "TELEAGENT_CODEX_HOME", env["TELEAGENT_SCRATCH"] + "/codex-home")

    scratch = env["TELEAGENT_SCRATCH"]
    _default(env, "TELEAGENT_CHAT_ONLY_CODEX_HOME", scratch + "/chat-only-codex-home")
    _default(env, "TELEAGENT_CHAT_ONLY_WORKDIR", scratch + "/chat-only-workspace")
    _default(env, "TELEAGENT_DSH_HOME", scratch + "/dsh-home")
    _default(env, "TELEAGENT_DSH_VERSION", "0.2.0-rc.2")
    _default(env, "TELEAGENT_MACHINE_CONTEXT", f"{home}/.config/tele-agent/machine.md")
    _default(env, "TELEAGENT_INSTANCE_CONTEXT", scratch + "/machine-context.md")

    # A chat-only or named bot must not share the main Codex home.
    source_home = _resolved(env["TELEAGENT_CODEX_SOURCE_HOME"])
    if env["TELEAGENT_CODEX_ACCESS_MODE"] == "chat-only":
        if _resolved(env["TELEAGENT_CHAT_ONLY_CODEX_HOME"]) == source_home:
            raise ValueError("chat-only mode must use a private TELEAGENT_CHAT_ONLY_CODEX_HOME")
    elif not main and _resolved(env["TELEAGENT_CODEX_HOME"]) == source_home:
        raise ValueError(f"non-main instance '{instance}' must use a private TELEAGENT_CODEX_HOME")

    _default(env, "TELEAGENT_AGENT_DIR", env["TELEAGENT_LOG_DIR"] + "/agents")
    if main:
        _default(env, "TELEAGENT_SECRET_ENV", repo_text + "/.secrets/notify.env")
    else:
        env["TELEAGENT_SECRET_ENV"] = (
            env.get("TELEAGENT_INSTANCE_SECRET_ENV") or f"{repo_text}/.secrets/notify-{instance}.env"
        )

    if not env.get("TELEAGENT_PERSONALITY_FILE"):
        named = Path(repo_text) / "config" / f"personality-{instance}.md"
        own = Path(repo_text) / "config" / "personality.md"
        if named.is_file():
            env["TELEAGENT_PERSONALITY_FILE"] = f"{repo_text}/config/personality-{instance}.md"
        elif main and own.is_file():
            env["TELEAGENT_PERSONALITY_FILE"] = f"{repo_text}/config/personality.md"
        else:
            env["TELEAGENT_PERSONALITY_FILE"] = f"{repo_text}/config/personality.default.md"
    return env
