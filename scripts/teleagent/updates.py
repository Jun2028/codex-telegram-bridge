"""Versions of each provider's CLI on this machine, and updating them.

Every provider is updated by its own updater (`codex update`, `claude update`,
`agy update`); DeepSeek Harness is an npm package and is updated through npm.
The newest published version is read from the npm registry where the provider
publishes there. Updater output is never forwarded; the result is the version
the CLI reports afterwards.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import agy as _agy
from . import claude as _claude
from . import dsh as _dsh
from . import harnesses as _harnesses
from . import portable as _portable
from . import processes as _processes
from . import state as _state

UPDATE = "update"
ALL = "all"
# Where each provider publishes releases; None when only its own updater knows.
PACKAGES = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code",
            "dsh": _dsh.PACKAGE, "agy": None}
LABELS = {harness.key: harness.label for harness in _harnesses.PROVIDERS}
VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?")
CHECK_INTERVAL = 24 * 3600
_running = threading.Lock()


def binary(key: str) -> str | None:
    try:
        if key == "claude":
            return _claude.binary()
        if key == "dsh":
            return _dsh.dsh_binary()
        if key == "agy":
            return shutil.which(_agy.agy_binary())
        return _processes.codex_executable()
    except RuntimeError:
        return None


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL, **_portable.hidden_window_options())


def installed(key: str) -> str:
    """The version this machine's CLI reports, or "" when it is absent or silent."""
    executable = binary(key)
    if not executable:
        return ""
    try:
        completed = _run([executable, "--version"], 60)
    except (OSError, subprocess.SubprocessError):
        return ""
    found = VERSION.search(completed.stdout or completed.stderr or "")
    return found.group(0) if found else ""


def latest(key: str) -> str:
    """The newest published version, or "" when it cannot be learned."""
    package = PACKAGES.get(key)
    if not package:
        return ""
    url = "https://registry.npmjs.org/" + urllib.parse.quote(package, safe="@") + "/latest"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return str(json.load(response).get("version") or "")
    except (OSError, ValueError):
        return ""


def order(version: str) -> tuple:
    """Sort key: numeric parts first, and a pre-release sorts before its release."""
    main, _, pre = version.partition("-")
    numbers = tuple(int(part) if part.isdigit() else 0 for part in main.split("."))
    tags = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split(".")) if pre else ()
    return numbers, 0 if pre else 1, tags


def newer(candidate: str, current: str) -> bool:
    return bool(candidate and current) and order(candidate) > order(current)


def survey() -> list[tuple[str, str, str]]:
    """(provider key, installed version, latest version) for every provider."""
    return [(key, installed(key), latest(key)) for key in PACKAGES]


def report(found: list[tuple[str, str, str]]) -> tuple[str, list[list[tuple[str, str]]]]:
    """The /update reply and its buttons as (label, callback data)."""
    lines, buttons = [], []
    for key, have, published in found:
        label = LABELS[key]
        if not have:
            lines.append(f"{label}: not installed")
            if key == "dsh" and _dsh.npm():
                buttons.append((f"Install {label}", f"{UPDATE}:{key}"))
            continue
        if newer(published, have):
            lines.append(f"{label}: {have} → {published} available")
            buttons.append((f"Update {label}", f"{UPDATE}:{key}"))
        elif published:
            lines.append(f"{label}: {have}, up to date")
        else:
            lines.append(f"{label}: {have}")
            buttons.append((f"Update {label}", f"{UPDATE}:{key}"))
    rows = [[button] for button in buttons]
    if len(buttons) > 1:
        rows.append([("Update all", f"{UPDATE}:{ALL}")])
    return "\n".join(lines), rows


def _update_command(key: str, repo_root: Path) -> list[str] | None:
    executable = binary(key)
    if key == "dsh":
        npm = _dsh.npm()
        if not npm:
            return None
        local = _dsh.dsh_home() / "cli"
        if executable and local not in Path(executable).parents:
            return [npm, "install", "-g", "--no-audit", "--no-fund", f"{_dsh.PACKAGE}@latest"]
        local.mkdir(parents=True, exist_ok=True)
        return [npm, "install", "--prefix", str(local), "--no-audit", "--no-fund", "--omit=dev",
                f"{_dsh.PACKAGE}@latest"]
    return [executable, "update"] if executable else None


def update(key: str, repo_root: Path) -> str:
    """Run one provider's updater and say what version it left."""
    label = LABELS[key]
    before = installed(key)
    command = _update_command(key, repo_root)
    if command is None:
        return f"{label}: not installed on this machine."
    try:
        completed = _run(command, 900)
    except subprocess.TimeoutExpired:
        return f"{label}: the updater did not finish in 15 minutes."
    except OSError as exc:
        return f"{label}: the updater could not start ({type(exc).__name__})."
    after = installed(key)
    if after and after != before:
        return f"{label}: {before or 'not installed'} → {after}."
    if completed.returncode:
        return f"{label}: the updater failed (exit {completed.returncode}); still {before or 'not installed'}."
    return f"{label}: {after or 'not installed'}, already the newest its updater offers."


def update_many(keys: list[str], repo_root: Path) -> str:
    results = [update(key, repo_root) for key in keys]
    return "\n".join(results) + "\n\nA running agent keeps its version until it is restarted."


def run_in_background(keys: list[str], repo_root: Path, done) -> bool:
    """Update without blocking the listener; `done(text)` receives the result.

    Returns False when an update is already running.
    """
    if not _running.acquire(blocking=False):
        return False

    def work():
        try:
            text = update_many(keys, repo_root)
        except Exception as exc:  # The listener must hear about every outcome.
            text = f"Updating failed: {type(exc).__name__}."
        finally:
            _running.release()
        done(text)

    threading.Thread(target=work, name="cli-update", daemon=True).start()
    return True


def keys_for(choice: str) -> list[str]:
    if choice == ALL:
        return [key for key in PACKAGES if binary(key)]
    return [choice] if choice in PACKAGES else []


def daily_notice(path: Path, now: float | None = None) -> tuple[str, list[list[tuple[str, str]]]] | None:
    """Once a day, the report when a newer version has appeared since the last notice."""
    now = time.time() if now is None else now
    data = _state.read_json_object(path)
    if now - float(data.get("checked_ts") or 0) < CHECK_INTERVAL:
        return None
    found = survey()
    available = {key: published for key, have, published in found if newer(published, have)}
    announced = data.get("announced") if isinstance(data.get("announced"), dict) else {}
    _state.write_json_object(path, {"checked_ts": now, "announced": available})
    if not any(announced.get(key) != version for key, version in available.items()):
        return None
    text, rows = report([entry for entry in found if entry[0] in available])
    return "Updates are available on this machine.\n\n" + text, rows
