"""Versions of each provider's CLI on this machine, and updating them.

Every provider is updated by its own updater (`codex update`, `claude update`,
`agy update`); DeepSeek Harness is an npm package and is updated through npm.
The newest published version is read from the npm registry where the provider
publishes there. Nothing here runs unasked: versions appear in /status and
/update, and an update starts only from its button.
"""

from __future__ import annotations

import json
import os
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

UPDATE = "update"
ALL = "all"
# Where each provider publishes releases; None when only its own updater knows.
PACKAGES = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code",
            "dsh": _dsh.PACKAGE, "agy": None}
LABELS = {harness.key: harness.label for harness in _harnesses.PROVIDERS}
VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?")
_running = threading.Lock()
_surveyed: dict = {"at": 0.0, "found": None}


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
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                          **_portable.hidden_window_options())


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


def survey(max_age: float = 0.0) -> list[tuple[str, str, str]]:
    """(provider key, installed version, latest version) for every provider.

    `max_age` accepts an answer that many seconds old, so /status stays quick.
    """
    if _surveyed["found"] is None or time.monotonic() - _surveyed["at"] >= max_age:
        _surveyed.update(found=[(key, installed(key), latest(key)) for key in PACKAGES],
                         at=time.monotonic())
    return list(_surveyed["found"])


def status_text(found: list[tuple[str, str, str]]) -> str:
    """The installed CLIs for /status, each with a newer version when one is published."""
    lines = [f"{LABELS[key]} {have}" + (f" ({published} available)" if newer(published, have) else "")
             for key, have, published in found if have]
    return "CLI versions\n" + "\n".join(lines) if lines else ""


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
        return _npm_update(npm, executable, _dsh.dsh_home() / "cli") if npm else None
    return [executable, "update"] if executable else None


def _npm_update(npm: str, executable: str | None, fresh: Path) -> list[str]:
    """Update the very install the command on PATH belongs to; a first install goes to `fresh`."""
    package = f"{_dsh.PACKAGE}@latest"
    options = ["--no-audit", "--no-fund", "--omit=dev"]
    shim = Path(executable) if executable else fresh / "node_modules" / ".bin" / "dsh"
    if shim.parent.name == ".bin" and shim.parent.parent.name == "node_modules":
        project = shim.parent.parent.parent
        project.mkdir(parents=True, exist_ok=True)
        return [npm, "install", "--prefix", str(project), *options, package]
    if (shim.parent / "node_modules" / _dsh.PACKAGE).is_dir():
        prefix = shim.parent  # Windows keeps a global install beside its command.
    else:
        real = Path(os.path.realpath(shim))
        modules = next((parent for parent in real.parents if parent.name == "node_modules"), None)
        if modules is None:
            return [npm, "install", "-g", *options, package]
        prefix = modules.parent.parent if modules.parent.name == "lib" else modules.parent
    return [npm, "install", "-g", "--prefix", str(prefix), *options, package]


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
    _surveyed.update(found=None)
    after = installed(key)
    if after and after != before:
        return f"{label}: {before or 'not installed'} → {after}."
    said = [line.strip() for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()]
    if completed.returncode or any(line.lower().startswith("error") for line in said[-3:]):
        reason = f" Its last line: {said[-1][:200]}" if said else ""
        return f"{label}: its updater failed; still {before or 'not installed'}.{reason}"
    return f"{label}: {after or 'not installed'}, already the newest its updater offers."


def update_many(keys: list[str], repo_root: Path) -> str:
    results = [update(key, repo_root) for key in keys]
    return "\n".join(results) + "\n\nA running agent keeps its version until it is restarted."


def marker_path(health_state_path: str) -> Path | None:
    """Where a running update is noted, beside the listener's own state."""
    return Path(health_state_path).with_name("telegram_cli_update.pending.json") if health_state_path else None


def interrupted(marker: Path | None) -> dict | None:
    """Who is still owed an answer for an update that a listener restart cut off."""
    if marker is None or not marker.exists():
        return None
    try:
        asked = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        asked = None
    try:
        marker.unlink()
    except OSError:
        pass
    return asked if isinstance(asked, dict) and asked.get("chat_id") else None


def run_in_background(keys: list[str], repo_root: Path, done, marker: Path | None = None,
                      asked: dict | None = None) -> bool:
    """Update without blocking the listener; `done(text)` receives the result.

    Returns False when an update is already running. `marker` records who
    asked, so a restart in the middle can still be answered.
    """
    if not _running.acquire(blocking=False):
        return False
    if marker is not None:
        try:
            marker.write_text(json.dumps(asked or {}), encoding="utf-8")
        except OSError:
            marker = None

    def work():
        try:
            text = update_many(keys, repo_root)
        except Exception as exc:  # The listener must hear about every outcome.
            text = f"Updating failed: {type(exc).__name__}."
        finally:
            _running.release()
        done(text)
        if marker is not None:
            try:
                marker.unlink()
            except OSError:
                pass

    threading.Thread(target=work, name="cli-update", daemon=True).start()
    return True


def keys_for(choice: str) -> list[str]:
    if choice == ALL:
        return [key for key in PACKAGES if binary(key)]
    return [choice] if choice in PACKAGES else []
