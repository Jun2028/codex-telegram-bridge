"""The DeepSeek flash slug: the provider's GA `deepseek-flash` name."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PREPARE_SCRIPT = REPO_ROOT / "scripts" / "prepare_telegram_ds_codex_home.sh"
FLASH_MODEL = "deepseek-flash"
PRO_MODEL = "deepseek-v4-pro"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from teleagent import settings as relay_settings  # noqa: E402


class DeepSeekFlashWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repo = self.root / "repo"
        (self.repo / "config").mkdir(parents=True)
        (self.repo / "config" / "relay.env").write_text("", encoding="utf-8")
        self.key_file = self.root / "deepseek.env"
        self.key_file.write_text(
            "export DEEPSEEK_API_KEY=not-a-real-key\n", encoding="utf-8"
        )
        self.key_file.chmod(0o600)
        self.scratch = self.root / "scratch"
        self.ds_home = self.scratch / "ds-home"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_prepare(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "TELEAGENT_REPO": str(self.repo),
                "TELEAGENT_INSTANCE": "main",
                "TELEAGENT_SCRATCH": str(self.scratch),
                "TELEAGENT_DS_CODEX_HOME": str(self.ds_home),
                "TELEAGENT_DS_KEY_FILE": str(self.key_file),
                "TELEAGENT_CODEX_ACCESS_MODE": "full-access",
                "DS_UTILS_ROOT": str(REPO_ROOT / "config" / "deepseek"),
            }
        )
        env.pop("TELEAGENT_CODEX_MODEL", None)
        return subprocess.run(
            ["bash", str(PREPARE_SCRIPT), *arguments],
            env=env,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def pinned_model(self) -> str:
        with open(self.ds_home / "agent-model.json", encoding="utf-8") as handle:
            return json.load(handle)["model"]

    def configured_model(self) -> str:
        config = (self.ds_home / "config.toml").read_text(encoding="utf-8")
        for line in config.splitlines():
            if line.startswith("model = "):
                return line.split('"')[1]
        raise AssertionError("config.toml has no model line")

    def test_default_request_prepares_the_flash_slug(self) -> None:
        result = self.run_prepare()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pinned_model(), FLASH_MODEL)
        self.assertEqual(self.configured_model(), FLASH_MODEL)

    def test_flash_request_pins_the_flash_slug_unchanged(self) -> None:
        result = self.run_prepare("--model", FLASH_MODEL)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pinned_model(), FLASH_MODEL)
        self.assertEqual(self.configured_model(), FLASH_MODEL)

    def test_pro_request_pins_the_pro_slug(self) -> None:
        result = self.run_prepare("--model", PRO_MODEL)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pinned_model(), PRO_MODEL)
        self.assertEqual(self.configured_model(), PRO_MODEL)

    def test_non_deepseek_model_is_rejected(self) -> None:
        result = self.run_prepare("--model", "gpt-5.6-sol")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported DeepSeek model", result.stderr)
        self.assertFalse(self.ds_home.exists())

    def test_shell_and_python_wiring_agree(self) -> None:
        env = os.environ.copy()
        env["TELEAGENT_REPO"] = str(self.repo)
        env["TELEAGENT_INSTANCE"] = "main"
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"source {REPO_ROOT / 'scripts' / 'relay_paths.sh'} && "
                'printf "%s %s" "$TELEAGENT_DEEPSEEK_FLASH_MODEL" '
                '"$TELEAGENT_DEEPSEEK_PRO_MODEL"',
            ],
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            timeout=30,
        )

        self.assertEqual(
            tuple(result.stdout.split()),
            (
                relay_settings.DEEPSEEK_FLASH_CODEX_AGENT_MODEL,
                relay_settings.DEEPSEEK_PRO_CODEX_AGENT_MODEL,
            ),
        )
        self.assertEqual(
            relay_settings.DEEPSEEK_FLASH_CODEX_AGENT_MODEL, FLASH_MODEL
        )
        self.assertEqual(relay_settings.DEEPSEEK_PRO_CODEX_AGENT_MODEL, PRO_MODEL)
        self.assertEqual(
            relay_settings.DEEPSEEK_CODEX_AGENT_MODELS,
            frozenset({FLASH_MODEL, PRO_MODEL}),
        )

    def test_repo_catalog_and_spec_cover_every_deepseek_model(self) -> None:
        catalog = json.loads(
            (REPO_ROOT / "config" / "deepseek" / "models.json").read_text(
                encoding="utf-8"
            )
        )
        slugs = {entry["slug"] for entry in catalog["models"]}

        for model in (FLASH_MODEL, PRO_MODEL):
            with self.subTest(model=model):
                self.assertIn(model, slugs)
                spec = json.loads(
                    (
                        REPO_ROOT / "config" / "deepseek" / f"{model}.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(spec["model"], model)
                self.assertEqual(spec["provider"], "deepseek")
                self.assertEqual(spec["reasoningEffort"], "max")


if __name__ == "__main__":
    unittest.main()
