# Rendering diagnostics

1. Trace `scripts/teleagent/delivery.py` → `scripts/teleagent/transport.py` →
   `scripts/telegram_format.py`. Confirm the active listener's executable and
   Python environment, not just the interactive shell's interpreter.
2. Check `markdown_it` in that environment. A missing parser deliberately keeps
   delivery alive by escaping the original text; asterisks and quote markers
   then appear literally. Install the repo's `requirements.txt` there. Runtime
   dependencies and CI use the same manifest; bootstrap checks rendering.
3. If the parser exists, render a minimal example locally. Check whether
   Telegram rejected the HTML and triggered the plain-text fallback. Preserve
   that delivery fallback; do not solve a formatting issue by dropping messages.
4. Run `python3 -m unittest discover -s tests -p test_telegram_formatting.py -q`
   with the relevant interpreter. For listener/tmux changes follow
   `docs/tmux_safety.md` and the operator guide. Reload only the affected listener
   after dependency changes: imports are cached. Never reset delivery offsets,
   restart the agent, clear queues, or alter its goal merely to fix formatting.
5. Verify a small formatting sample in the originating chat as part of an
   authorized fix. When available, check Telegram's returned `entities` for
   `bold`, `italic`, `code`, `pre`, `text_link` and `blockquote`; an HTTP success
   alone does not prove rich text survived. Use plain text while diagnosis is
   unresolved and state the actual validation limits.
