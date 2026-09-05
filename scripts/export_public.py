#!/usr/bin/env python3
"""Export committed, allowlisted source into a separate public checkout.

This does not push, copy private Git history or read local secret/config files.
Review the public diff before committing it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
ROOT_FILES = {"README.md", "LICENSE", ".gitignore"}
CONFIG_FILES = {
    "config/notify.env.template",
    "config/relay.env.template",
    "config/codex-chat-only.toml.template",
    "config/chat-only-agent-instructions.md",
    "config/personality.default.md",
    "config/deepseek/models.json",
    "config/deepseek/deepseek-v4-flash.json",
    "config/deepseek/deepseek-v4-pro.json",
}


def allowed(name: str) -> bool:
    path = Path(name)
    return (
        name in ROOT_FILES
        or name in CONFIG_FILES
        or path.parts[0] == "scripts"
        and path.suffix in {".py", ".sh"}
        or path.parts[0] == "tests"
        and path.suffix == ".py"
        or path.parts[0] == "docs"
        and path.suffix == ".md"
        or name == ".github/workflows/check.yml"
    )


def public_readme(text: str) -> str:
    text = text.replace("# tele-agent\n", "# Codex Telegram Bridge\n", 1)
    if "This private repository is the source" not in text:
        return text
    start = text.index("This private repository is the source")
    end = text.index("See [architecture", start)
    return text[:start] + text[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--source-ref", default="HEAD")
    args = parser.parse_args()
    target = args.destination.resolve()
    if target == SOURCE or SOURCE in target.parents or not (target / ".git").exists():
        parser.error("destination must be a separate Git checkout")
    revision = subprocess.check_output(
        [
            "git",
            "-C",
            str(SOURCE),
            "rev-parse",
            "--verify",
            args.source_ref + "^{commit}",
        ],
        text=True,
    ).strip()
    names = (
        subprocess.check_output(
            ["git", "-C", str(SOURCE), "ls-tree", "-rz", "--name-only", revision]
        )
        .decode()
        .split("\0")
    )
    exported = []
    for name in names:
        if not name or not allowed(name):
            continue
        if name == "LICENSE" and (target / name).exists():
            continue  # Preserve the public project’s existing attribution.
        data = subprocess.check_output(
            ["git", "-C", str(SOURCE), "show", revision + ":" + name]
        )
        if name == "README.md":
            data = public_readme(data.decode()).encode()
        elif name == ".gitignore":
            data = data.replace(b".work-hopper/\n", b"")
        elif name == "config/notify.env.template":
            data = data.replace(
                b"# Tele-agent notification secrets.",
                b"# Codex Telegram Bridge notification secrets.",
            )
        # Source only. Refuse obvious embedded credentials even in an allowed file.
        if re.search(
            rb"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b|\bsk-[A-Za-z0-9]{32,}\b|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----",
            data,
        ):
            raise SystemExit(f"Credential-like content requires review: {name}")
        output = target / name
        if output.is_symlink() or any(
            parent.is_symlink() for parent in output.parents if parent != target
        ):
            raise SystemExit(f"Refusing symlink destination: {name}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        mode = subprocess.check_output(
            ["git", "-C", str(SOURCE), "ls-tree", revision, "--", name], text=True
        ).split()[0]
        output.chmod(0o755 if mode == "100755" else 0o644)
        exported.append(name)
    print(
        f"Exported {len(exported)} source files from {revision[:12]}; review the public diff before pushing."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
