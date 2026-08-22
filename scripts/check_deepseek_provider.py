#!/usr/bin/env python3
"""Sanitized launch-time health check for the DeepSeek Responses API."""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "https://api.deepseek.com"
CA_BUNDLE_CANDIDATES = (
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/ssl/cert.pem",
)


def _ssl_context() -> ssl.SSLContext:
    """Build a verify context even when the host Python has a broken default CA path."""
    context = ssl.create_default_context()
    configured = os.environ.get("SSL_CERT_FILE") or os.environ.get(
        "CURL_CA_BUNDLE"
    )
    if configured and os.path.isfile(configured):
        context.load_verify_locations(configured)
        return context
    for candidate in CA_BUNDLE_CANDIDATES:
        if os.path.isfile(candidate):
            context.load_verify_locations(candidate)
            break
    return context


class PreflightError(RuntimeError):
    """A provider failure safe to report without response bodies or secrets."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _request_json(
    *,
    url: str,
    api_key: str,
    timeout: float,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "codex-telegram-bridge-deepseek-preflight/1",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context()
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise PreflightError(f"http_{exc.code}") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            raise PreflightError("network_timeout") from None
        raise PreflightError("network_unreachable") from None
    except (TimeoutError, socket.timeout):
        raise PreflightError("network_timeout") from None
    except OSError:
        raise PreflightError("network_unreachable") from None

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PreflightError("response_invalid") from None


def check_provider(
    *, model: str, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 20
) -> None:
    if not api_key:
        raise PreflightError("key_missing")

    root = base_url.rstrip("/")
    catalog = _request_json(
        url=f"{root}/models", api_key=api_key, timeout=timeout
    )
    if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
        raise PreflightError("models_invalid")
    model_ids = {
        entry.get("id")
        for entry in catalog["data"]
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    if model not in model_ids:
        raise PreflightError("model_unlisted")

    result = _request_json(
        url=f"{root}/responses",
        api_key=api_key,
        timeout=timeout,
        method="POST",
        payload={
            "model": model,
            "input": "Reply exactly OK.",
            "max_output_tokens": 16,
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("id"), str):
        raise PreflightError("responses_invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()

    try:
        check_provider(
            model=args.model,
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=args.base_url,
            timeout=args.timeout,
        )
    except PreflightError as exc:
        print(
            f"DEEPSEEK_PREFLIGHT_FAILED code={exc.code} model={args.model}",
            file=sys.stderr,
        )
        return 1

    print(f"DEEPSEEK_PREFLIGHT_OK model={args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
