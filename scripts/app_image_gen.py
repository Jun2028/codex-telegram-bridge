#!/usr/bin/env python3
"""Generate an image through the local ChatGPT desktop app (subscription).

This driver talks to the already-running desktop app over its Chrome DevTools
endpoint. It uses the app's own "Create image" tool (gpt-image), so no OpenAI
API key is consumed and no per-token API billing happens.

Host requirements are intentionally checked up front, with fail-fast exit
codes, so headless or unsupported machines error immediately instead of
hanging:

  0  success (prints the saved PNG path)
  2  app is not available on this host (not running, no page, not signed in)
  3  usage/config error
  4  generation failed or timed out

The app, display stack, and debug port are host provisioning (outside this
script's job). See docs/notifications.md for how this is wired into Telegram.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_CDP_HTTP = "http://127.0.0.1:9222"


class AppImageError(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _http_get_json(url: str, timeout: float = 5.0) -> list[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise AppImageError(
            "app_not_running",
            "ChatGPT desktop app debug endpoint is not reachable; "
            "start the app with --remote-debugging-port",
        ) from exc
    if not isinstance(payload, list):
        raise AppImageError("app_bad_response", "unexpected CDP response")
    return payload


def pick_page(targets: list[dict]) -> dict:
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise AppImageError(
            "app_page_not_found",
            "ChatGPT desktop app has no open page target",
        )
    # The main chat surface is the plain index.html page; the avatar-overlay
    # page is the onboarding/setup route.
    for page in pages:
        url = page.get("url", "")
        if url.endswith("/index.html") and "avatar-overlay" not in url:
            return page
    return pages[0]


def ws_url_from_http(http_url: str, page_id: str) -> str:
    return (
        http_url.replace("http://", "ws://", 1)
        .replace("https://", "wss://", 1)
        .rstrip("/")
        + f"/devtools/page/{page_id}"
    )


class WS:
    """Minimal RFC6455 client, just enough for CDP."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self._timeout = timeout
        scheme, rest = url.split("://", 1)
        hostport, path = rest.split("/", 1)
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
            port = int(port)
        else:
            host, port = hostport, 443 if scheme == "wss" else 80
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        response = self._read_until(b"\r\n\r\n")
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise AppImageError("app_ws_handshake", "CDP upgrade failed")

    def _read_until(self, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise AppImageError("app_ws_closed", "CDP socket closed")
            data += chunk
        return data

    def _read_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self._sock.recv(count - len(data))
            if not chunk:
                raise AppImageError("app_ws_closed", "CDP socket closed")
            data += chunk
        return data

    @staticmethod
    def _encode_frame(payload: bytes, opcode: int = 1) -> bytes:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bytes(header) + mask + masked

    def send(self, obj: dict) -> None:
        self._sock.sendall(self._encode_frame(json.dumps(obj).encode("utf-8")))

    def recv(self) -> str:
        while True:
            first = self._read_exact(2)
            fin_opcode = first[0]
            opcode = fin_opcode & 0x0F
            length = first[1] & 0x7F
            if length == 126:
                length = int.from_bytes(self._read_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read_exact(8), "big")
            payload = self._read_exact(length)
            if opcode == 9:  # ping -> pong
                self._sock.sendall(self._encode_frame(payload, 0xA))
                continue
            if opcode == 8:
                raise AppImageError("app_ws_closed", "CDP closed the socket")
            if opcode in (1, 2):
                return payload.decode("utf-8")
            # continuation frames carry no id of their own; CDP messages are
            # small enough that this is sufficient in practice.

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class CDP:
    def __init__(self, ws: WS) -> None:
        self._ws = ws
        self._next_id = 1

    def evaluate(self, expression: str) -> str:
        call_id = self._next_id
        self._next_id += 1
        self._ws.send({
            "id": call_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        })
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") != call_id:
                continue
            result = message.get("result", {})
            value = result.get("result", {})
            if result.get("exceptionDetails"):
                text = result["exceptionDetails"].get("text", "unknown error")
                raise AppImageError("app_js_error", text, 4)
            raw = value.get("value")
            if raw is None:
                return ""
            if isinstance(raw, str):
                return raw
            return json.dumps(raw)

    def trusted_click(self, predicate: str) -> None:
        rect_expr = (
            "(() => { const all=[]; const walk=(r)=>{ for(const e of "
            "r.querySelectorAll('*')){ if(e.shadowRoot) walk(e.shadowRoot); "
            "all.push(e); } }; walk(document); const el = "
            f"({predicate}); if(!el) return null; const r=el."
            "getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+"
            "r.height/2}; })()"
        )
        raw = self.evaluate(rect_expr)
        if not raw or raw == "null":
            raise AppImageError("app_element_missing", "expected UI element not found")
        point = json.loads(raw)
        for event in ("mousePressed", "mouseReleased"):
            self._ws.send({
                "id": self._next_id,
                "method": "Input.dispatchMouseEvent",
                "params": {
                    "type": event,
                    "x": point["x"],
                    "y": point["y"],
                    "button": "left",
                    "clickCount": 1,
                },
            })
            self._next_id += 1

    def type_text(self, text: str) -> None:
        focus_expr = (
            "(() => { const all=[]; const walk=(r)=>{ for(const e of "
            "r.querySelectorAll('*')){ if(e.shadowRoot) walk(e.shadowRoot); "
            "all.push(e); } }; walk(document); const el=all.find(e=>"
            "(e.tagName==='DIV'&&e.isContentEditable)||e.tagName==='TEXTAREA');"
            " if(!el) return false; el.focus(); return true; })()"
        )
        if self.evaluate(focus_expr) != "true":
            raise AppImageError(
                "app_composer_missing", "composer input was not found"
            )
        self._ws.send({
            "id": self._next_id,
            "method": "Input.insertText",
            "params": {"text": text},
        })
        self._next_id += 1


def _walk_snippet() -> str:
    return (
        "(() => { const all=[]; const walk=(r)=>{ for(const e of "
        "r.querySelectorAll('*')){ if(e.shadowRoot) walk(e.shadowRoot); "
        "all.push(e); } }; walk(document); "
    )


def wait_logged_in(cdp: CDP, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = cdp.evaluate("document.body ? document.body.innerText : ''")
        if body and "Sign in to ChatGPT" not in body and "Sign up" not in body:
            return
        time.sleep(2)
    raise AppImageError(
        "not_logged_in",
        "ChatGPT desktop app is not signed in; sign in once in the app",
    )


def enable_image_mode(cdp: CDP) -> None:
    item_predicate = (
        "all.find(e=>e.tagName==='BUTTON'&&(e.innerText||'')."
        "includes('Create image'))"
    )
    item_visible = (
        _walk_snippet()
        + f"return !!({item_predicate}); }})()"
    )
    for _ in range(3):
        if cdp.evaluate(item_visible) == "true":
            cdp.trusted_click(item_predicate)
            return
        cdp.trusted_click(
            "all.find(e=>e.tagName==='BUTTON'&&(e.getAttribute("
            "'aria-label')||'').includes('Add files')) || all.find(e=>"
            "e.tagName==='BUTTON'&&(e.getAttribute('aria-label')||'')."
            "includes('Add photos'))"
        )
        for _ in range(6):
            time.sleep(0.5)
            if cdp.evaluate(item_visible) == "true":
                cdp.trusted_click(item_predicate)
                return
    raise AppImageError(
        "create_image_unavailable",
        "could not find the app's Create image tool; make sure the app "
        "is in ChatGPT mode and your account has image access",
    )


def wait_composer_idle(cdp: CDP, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        busy = cdp.evaluate(
            _walk_snippet()
            + "return all.some(e=>e.tagName==='BUTTON'&&(e.innerText||'')."
            "trim()==='Stop'); })()"
        )
        if busy == "false":
            return
        time.sleep(3)
    raise AppImageError(
        "app_busy",
        "composer stayed busy; a previous generation did not finish",
        4,
    )


def send_prompt(cdp: CDP, prompt: str) -> set[str]:
    snapshot_raw = cdp.evaluate(
        _walk_snippet()
        + "return all.filter(e=>e.tagName==='IMG').map(e=>e.src||'')."
        "filter(s=>s.startsWith('blob:')); })()"
    )
    try:
        snapshot = set(json.loads(snapshot_raw))
    except (TypeError, json.JSONDecodeError):
        snapshot = set()
    cdp.type_text(prompt)
    time.sleep(0.5)
    cdp.trusted_click(
        "all.find(e=>e.tagName==='BUTTON'&&((e.getAttribute('aria-label')||"
        "e.innerText||'').trim()==='Send'))"
    )
    return snapshot


def poll_for_image(cdp: CDP, timeout: float, known_srcs: set[str]) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = cdp.evaluate(
            _walk_snippet()
            + "const known=" + json.dumps(sorted(known_srcs)) + "; "
            "const img=all.find(e=>e.tagName==='IMG'&&(e.src||'')."
            "startsWith('blob:')&&!known.includes(e.src)); const txt="
            "document.body?document.body.innerText:''; "
            "if(img&&img.naturalWidth>0) return 'done'; "
            "if(/something went wrong|couldn't generate|can't create|"
            "unable to generate/i.test(txt)) return 'error:'+txt.slice(-160); "
            "return 'waiting'; })()"
        )
        if state == "done":
            return
        if state.startswith("error:"):
            raise AppImageError("generation_failed", state[6:], 4)
        time.sleep(3)
    raise AppImageError(
        "generation_timeout",
        "timed out waiting for the generated image",
        4,
    )


def extract_image(cdp: CDP, known_srcs: set[str]) -> bytes:
    known = json.dumps(sorted(known_srcs))
    expression = (
        "(async () => { const all=[]; const walk=(r)=>{ for(const e of "
        "r.querySelectorAll('*')){ if(e.shadowRoot) walk(e.shadowRoot); "
        "all.push(e); } }; walk(document); const known=" + known + "; "
        "const img=all.find(e=>e.tagName==='IMG'&&(e.src||'').startsWith("
        "'blob:')&&!known.includes(e.src)); if(!img) return 'no-img'; "
        "const c=document.createElement('canvas'); c.width=img.naturalWidth; "
        "c.height=img.naturalHeight; c.getContext('2d').drawImage(img,0,0,"
        "c.width,c.height); return c.toDataURL('image/png'); })()"
    )
    data_url = cdp.evaluate(expression)
    if not data_url.startswith("data:image/png;base64,"):
        raise AppImageError("image_extract_failed", data_url[:80] or "no data", 4)
    return base64.b64decode(data_url.split(",", 1)[1])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an image through the ChatGPT desktop app"
    )
    parser.add_argument("prompt", help="image description")
    parser.add_argument("--out", type=Path, default=None,
                        help="output PNG path (default: ./app_image_gen_<ts>.png)")
    parser.add_argument("--cdp", default=DEFAULT_CDP_HTTP,
                        help="CDP HTTP endpoint of the app")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="seconds to wait for the image")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.prompt.strip():
        print("ERROR usage prompt_is_empty", file=sys.stderr)
        return 3
    out = args.out or Path(f"app_image_gen_{int(time.time())}.png")
    try:
        targets = _http_get_json(f"{args.cdp}/json")
        page = pick_page(targets)
        ws_url = ws_url_from_http(args.cdp, page["id"])
        ws = WS(ws_url)
        try:
            cdp = CDP(ws)
            wait_logged_in(cdp)
            wait_composer_idle(cdp)
            enable_image_mode(cdp)
            known_srcs = send_prompt(cdp, args.prompt)
            poll_for_image(cdp, args.timeout, known_srcs)
            png = extract_image(cdp, known_srcs)
        finally:
            ws.close()
        out.write_bytes(png)
        print(out.resolve())
        return 0
    except AppImageError as exc:
        print(f"ERROR {exc.code} {exc.message}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(f"ERROR app_not_running {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
