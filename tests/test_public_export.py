from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_public


class PublicExportTests(unittest.TestCase):
    def test_new_source_is_private_until_reviewed(self):
        for name in (
            "scripts/new_worker.py",
            "scripts/new_job.sh",
            "tests/test_new.py",
            "docs/new.md",
            "config/relay.env",
            ".secrets/notify.env",
        ):
            self.assertFalse(export_public.allowed(name), name)
        self.assertTrue(export_public.allowed("scripts/notify.py"))

    def test_private_modules_cannot_be_allowlisted_accidentally(self):
        for name in export_public.PRIVATE_FILES | {
            "private/hpc/accounting.py",
            "../private.py",
            "/private.py",
        }:
            self.assertFalse(export_public.allowed(name, {name}), name)

    def test_public_readme_omits_private_feature_section(self):
        readme = export_public.SOURCE.joinpath("README.md").read_text()
        public = export_public.public_readme(readme)
        self.assertNotIn("PRIVATE-HPC-", public)
        self.assertNotIn("docs/hpc_notifications.md", public)
        self.assertNotIn("tmux_run_with_report.sh", public)
        self.assertIn("One bot = one persistent agent", public)


if __name__ == "__main__":
    unittest.main()
