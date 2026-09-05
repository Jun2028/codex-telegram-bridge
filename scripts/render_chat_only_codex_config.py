#!/usr/bin/env python3
"""Render the fail-closed Codex config used by chat-only relay instances."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = REPO_ROOT / "config" / "codex-chat-only.toml.template"
DEFAULT_INSTRUCTIONS = REPO_ROOT / "config" / "chat-only-agent-instructions.md"


def toml_string(value: str) -> str:
    """Return a JSON basic string, which is also a valid TOML basic string."""

    return json.dumps(value, ensure_ascii=False)


def render_config(
    *,
    template_path: Path,
    instructions_path: Path,
    personality_path: Path,
    provider: str,
    model_catalog: Path | None,
    model_spec: Path | None,
    trusted_project: Path | None,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    instructions = instructions_path.read_text(encoding="utf-8").rstrip()
    personality = personality_path.read_text(encoding="utf-8").strip()
    if not personality:
        raise ValueError(f"personality file is empty: {personality_path}")

    developer_instructions = (
        f"{instructions}\n\n## Instance personality\n\n{personality}\n"
    )
    provider_settings = ""
    provider_tables = ""

    if provider == "deepseek":
        if model_catalog is None or model_spec is None:
            raise ValueError("DeepSeek rendering requires model catalog and model spec")
        provider_settings = "\n".join(
            (
                'model_provider = "deepseek"',
                f"model_catalog_json = {toml_string(str(model_catalog))}",
            )
        )
        provider_tables = "\n".join(
            (
                "[model_providers.deepseek]",
                'name = "deepseek"',
                'base_url = "https://api.deepseek.com/"',
                'wire_api = "responses"',
                'env_key = "DEEPSEEK_API_KEY"',
                "",
                "[shell_environment_policy.filters]",
                '"DEEPSEEK_API_KEY" = "exclude"',
                '"OPENAI_API_KEY" = "exclude"',
                "",
                "[shell_environment_policy.set]",
                f"AGENT_MODEL_SPEC_PATH = {toml_string(str(model_spec))}",
            )
        )
    elif provider != "openai":
        raise ValueError(f"unsupported provider: {provider}")

    replacements = {
        "__TELEAGENT_PROVIDER_SETTINGS__": provider_settings,
        "__TELEAGENT_DEVELOPER_INSTRUCTIONS__": toml_string(
            developer_instructions
        ),
        "__TELEAGENT_PROVIDER_TABLES__": provider_tables,
    }
    rendered = template
    for placeholder, value in replacements.items():
        if rendered.count(placeholder) != 1:
            raise ValueError(
                f"template must contain exactly one {placeholder} placeholder"
            )
        rendered = rendered.replace(placeholder, value)
    if trusted_project is not None:
        resolved_project = trusted_project.resolve(strict=False)
        rendered = "\n".join(
            (
                rendered.rstrip(),
                "",
                f"[projects.{toml_string(str(resolved_project))}]",
                'trust_level = "trusted"',
            )
        )
    return rendered.rstrip() + "\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--personality", required=True, type=Path)
    parser.add_argument("--provider", choices=("openai", "deepseek"), default="openai")
    parser.add_argument("--model-catalog", type=Path)
    parser.add_argument("--model-spec", type=Path)
    parser.add_argument("--trusted-project", type=Path)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--instructions", type=Path, default=DEFAULT_INSTRUCTIONS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rendered = render_config(
            template_path=args.template,
            instructions_path=args.instructions,
            personality_path=args.personality,
            provider=args.provider,
            model_catalog=args.model_catalog,
            model_spec=args.model_spec,
            trusted_project=args.trusted_project,
        )
        atomic_write(args.output, rendered)
    except (OSError, ValueError) as error:
        raise SystemExit(f"cannot render chat-only Codex config: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
