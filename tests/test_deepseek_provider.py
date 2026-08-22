from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_deepseek_provider.py"
SPEC = importlib.util.spec_from_file_location("check_deepseek_provider", SCRIPT)
assert SPEC and SPEC.loader
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


class DeepSeekProviderPreflightTests(unittest.TestCase):
    def test_missing_key_fails_without_network(self) -> None:
        with self.assertRaisesRegex(provider.PreflightError, "key_missing"):
            provider.check_provider(model="deepseek-v4-pro", api_key="")

    def test_requires_selected_model_in_catalog(self) -> None:
        with mock.patch.object(
            provider,
            "_request_json",
            return_value={"data": [{"id": "deepseek-v4-flash"}]},
        ):
            with self.assertRaisesRegex(provider.PreflightError, "model_unlisted"):
                provider.check_provider(model="deepseek-v4-pro", api_key="secret")

    def test_validates_models_then_responses(self) -> None:
        with mock.patch.object(
            provider,
            "_request_json",
            side_effect=[
                {"data": [{"id": "deepseek-v4-pro"}]},
                {"id": "resp_123", "status": "incomplete"},
            ],
        ) as request_json:
            provider.check_provider(model="deepseek-v4-pro", api_key="secret")

        self.assertEqual(request_json.call_count, 2)
        self.assertEqual(request_json.call_args_list[0].kwargs.get("method", "GET"), "GET")
        self.assertEqual(request_json.call_args_list[1].kwargs["method"], "POST")

    def test_http_error_is_reduced_to_status_code(self) -> None:
        error = provider.urllib.error.HTTPError(
            "https://api.deepseek.com/models", 401, "unauthorized", {}, None
        )
        with mock.patch.object(provider.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(provider.PreflightError, "http_401"):
                provider._request_json(
                    url="https://api.deepseek.com/models",
                    api_key="secret",
                    timeout=1,
                )


if __name__ == "__main__":
    unittest.main()
