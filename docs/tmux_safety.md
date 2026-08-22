# tmux safety manual

The relay, Codex supervisor, inbox, and other long-running work may
share one tmux server. A server-wide command can therefore terminate the agent,
its active conversation, and unrelated panes at once.

## Non-negotiable rules

1. Never run an unqualified `tmux kill-server` from a managed tmux pane.
2. Treat the inherited `TMUX` variable as authoritative: setting only
   `TMUX_TMPDIR` does **not** isolate a child tmux client while `TMUX` remains
   set.
3. Prefer targeted cleanup: `tmux kill-pane -t session:window.pane`, then
   `tmux kill-window`, then `tmux kill-session`. Use the narrowest exact target.
4. Never use broad session globs, blanket process kills, or `tmux kill-server`
   against a live operator server.
5. Before changing a managed pane, capture it and list exact sessions. Testing
   and observation never imply permission to interrupt unrelated work.

## Safe destructive tests

Run any test that may create or destroy a tmux server through:

```bash
scripts/tmux_isolated_test.sh -- bash -c '
  tmux new-session -d -s fixture "sleep 30"
  tmux kill-server
'
```

The wrapper creates a private `TMUX_TMPDIR`, removes inherited `TMUX`, and
cleans up only that private server. An unqualified `tmux kill-server` inside the
wrapped command cannot reach the live relay server.

For manual isolation without the wrapper, both pieces are required:

```bash
tmux_test_root=$(mktemp -d /tmp/codex-telegram-bridge-tmux-test.XXXXXX)
env -u TMUX TMUX_TMPDIR="$tmux_test_root" tmux new-session -d -s fixture
env -u TMUX TMUX_TMPDIR="$tmux_test_root" tmux kill-server
rmdir "$tmux_test_root"
```

Do not reuse the live server's socket path, and do not rely on `TMUX_TMPDIR`
alone. When a test finishes, verify the managed relay still exists with an
exact `tmux has-session -t tele-agent` check.
