#!/usr/bin/env python3
"""Render a conservative CommonMark subset as Telegram-safe HTML."""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover - exercised only on minimal installations
    MarkdownIt = None  # type: ignore[assignment,misc]


TELEGRAM_LANGUAGE_RE = re.compile(r"[A-Za-z0-9_+.-]{1,32}")
SAFE_LINK_SCHEMES = {"http", "https", "mailto", "tel", "tg"}
BLOCK_FINAL_TOKEN_TYPES = {
    "blockquote_close",
    "bullet_list_close",
    "code_block",
    "fence",
    "heading_close",
    "hr",
    "ordered_list_close",
}


@dataclass
class TokenNode:
    token: Any | None
    children: list["TokenNode"] = field(default_factory=list)


def _token_tree(tokens: Iterable[Any]) -> TokenNode:
    root = TokenNode(None)
    stack = [root]
    for token in tokens:
        if token.nesting == -1:
            if len(stack) > 1:
                stack.pop()
            continue
        node = TokenNode(token)
        stack[-1].children.append(node)
        if token.nesting == 1:
            stack.append(node)
    return root


def _safe_href(value: str) -> str | None:
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in SAFE_LINK_SCHEMES:
        return None
    return html.escape(value, quote=True)


def _render_inline(tokens: Iterable[Any]) -> str:
    output: list[str] = []
    format_stack: list[tuple[str, str, str] | None] = []
    open_tags = {
        "strong_open": ("strong", "<b>", "</b>"),
        "em_open": ("em", "<i>", "</i>"),
        "s_open": ("s", "<s>", "</s>"),
    }
    close_tags = {"strong_close": "strong", "em_close": "em", "s_close": "s"}

    def close_active_formats() -> None:
        output.extend(item[2] for item in reversed(format_stack) if item is not None)

    def reopen_active_formats() -> None:
        output.extend(item[1] for item in format_stack if item is not None)

    for token in tokens:
        token_type = token.type
        if token_type in {"text", "text_special"}:
            output.append(html.escape(token.content, quote=False))
        elif token_type in open_tags:
            item = open_tags[token_type]
            format_stack.append(item)
            output.append(item[1])
        elif token_type in close_tags:
            if format_stack:
                item = format_stack.pop()
                if item is not None:
                    output.append(item[2])
        elif token_type == "code_inline":
            # Telegram does not permit code/pre entities to overlap emphasis or
            # links. Temporarily close the active formats around the code span.
            close_active_formats()
            output.append(f"<code>{html.escape(token.content, quote=False)}</code>")
            reopen_active_formats()
        elif token_type in {"softbreak", "hardbreak"}:
            output.append("\n")
        elif token_type == "link_open":
            href = _safe_href(token.attrGet("href") or "")
            if href is not None:
                item = ("link", f'<a href="{href}">', "</a>")
                format_stack.append(item)
                output.append(item[1])
            else:
                format_stack.append(None)
        elif token_type == "link_close":
            if format_stack:
                item = format_stack.pop()
                if item is not None:
                    output.append(item[2])
        elif token_type == "image":
            alt = token.content or "image"
            output.append(html.escape(alt, quote=False))
        elif token_type == "html_inline":
            output.append(html.escape(token.content, quote=False))
        elif token.children:
            output.append(_render_inline(token.children))
        elif token.content:
            output.append(html.escape(token.content, quote=False))
    close_active_formats()
    return "".join(output)


def _render_container(nodes: Iterable[TokenNode], separator: str = "\n\n") -> str:
    rendered = [_render_block(node).strip("\n") for node in nodes]
    return separator.join(part for part in rendered if part)


def _render_list(node: TokenNode, ordered: bool) -> str:
    lines: list[str] = []
    start = 1
    if ordered and node.token is not None:
        try:
            start = int(node.token.attrGet("start") or 1)
        except (TypeError, ValueError):
            start = 1
    item_number = start
    for child in node.children:
        if child.token is None or child.token.type != "list_item_open":
            continue
        body = _render_container(child.children, separator="\n").strip()
        prefix = f"{item_number}. " if ordered else "• "
        continuation = " " * len(prefix)
        body_lines = body.splitlines() or [""]
        lines.append(prefix + body_lines[0])
        lines.extend(continuation + line for line in body_lines[1:])
        item_number += 1
    return "\n".join(lines)


def _render_block(node: TokenNode) -> str:
    token = node.token
    if token is None:
        return _render_container(node.children)
    token_type = token.type
    if token_type == "inline":
        return _render_inline(token.children or [])
    if token_type == "heading_open":
        body = _render_container(node.children, separator="")
        if "<code>" in body:
            parts = re.split(r"(<code>.*?</code>)", body)
            return "".join(
                part if part.startswith("<code>") else f"<b>{part}</b>"
                for part in parts
                if part
            )
        return f"<b>{body}</b>"
    if token_type == "paragraph_open":
        return _render_container(node.children, separator="")
    if token_type == "bullet_list_open":
        return _render_list(node, ordered=False)
    if token_type == "ordered_list_open":
        return _render_list(node, ordered=True)
    if token_type == "list_item_open":
        return _render_container(node.children, separator="\n")
    if token_type == "blockquote_open":
        body = _render_container(node.children, separator="\n").strip()
        if "<pre" in body or "<blockquote" in body:
            flattened = body.replace("<blockquote>", "").replace("</blockquote>", "")
            return f"Quote:\n{flattened}" if flattened else ""
        return f"<blockquote>{body}</blockquote>" if body else ""
    if token_type in {"fence", "code_block"}:
        language = (token.info or "").strip().split(maxsplit=1)[0] if token.info else ""
        escaped_code = html.escape(token.content, quote=False)
        if language and TELEGRAM_LANGUAGE_RE.fullmatch(language):
            return (
                f'<pre><code class="language-{html.escape(language, quote=True)}">'
                f"{escaped_code}</code></pre>"
            )
        return f"<pre>{escaped_code}</pre>"
    if token_type == "hr":
        return "────────"
    if token_type == "html_block":
        return html.escape(token.content, quote=False)
    if token.children:
        return _render_container(node.children)
    if token.content:
        return html.escape(token.content, quote=False)
    return ""


def render_telegram_html(text: str) -> str:
    """Convert CommonMark to the small HTML subset accepted by Telegram.

    Raw HTML is disabled and escaped. If markdown-it-py is unavailable, the
    entire message is safely escaped and delivered without rich formatting.
    """

    if MarkdownIt is None:
        return html.escape(text, quote=False)
    parser = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    try:
        parser.enable("strikethrough")
    except (KeyError, ValueError):
        pass
    rendered = _render_block(_token_tree(parser.parse(text))).strip()
    return rendered or html.escape(text, quote=False)


def telegram_final_marker_suffix(text: str) -> str:
    """Keep ∎ inline for prose, but outside a trailing block construct."""

    if MarkdownIt is not None:
        parser = MarkdownIt("commonmark", {"html": False, "linkify": False})
        tokens = parser.parse(text)
        if tokens and tokens[-1].type in BLOCK_FINAL_TOKEN_TYPES:
            return "\n\n∎"
        return " ∎"
    trailing_block = re.search(
        r"(?:^|\n)(?:`{3,}|~{3,}|>|[-+*]|\d+[.)]|#{1,6})[^\n]*\s*$", text
    )
    return "\n\n∎" if trailing_block else " ∎"
