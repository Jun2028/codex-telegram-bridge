# Notifications

Notifications are optional and secret-driven. They never commit or print tokens.

For Telegram-to-Codex remote control, read `docs/telegram_codex_agent.md`.

## How Telegram Works

`scripts/notify.py` loads environment variables from the process and then overlays
`.secrets/notify.env` by default. If both Telegram values are present, it sends a
plain text message to the Telegram Bot API `sendMessage` endpoint:

- `TELEAGENT_BOT_TOKEN`: bot token from BotFather.
- `TELEAGENT_CHAT_ID`: destination chat, group, or channel id.

The message title is prefixed with the level, for example `[INFO] Long task`.
Messages are capped before sending so Telegram rejects fewer status reports. The
notifier redacts token-like values from generated message bodies and writes only
metadata/results to `logs/readable/notifications.jsonl`.

If Telegram is missing or fails, SMTP email is tried as a fallback when configured.
Telegram image and document uploads are supported by `scripts/notify.py`, but
they require an explicit per-command flag or an env opt-in. Keep
`TELEAGENT_NOTIFY_ALLOW_IMAGES=0` and `TELEAGENT_NOTIFY_ALLOW_FILES=0` unless
remote upload is intentional for that run.

## Telegram Message Rules

Telegram is for short operational updates, not raw logs or config dumps.

- Do not send command strings, raw command dumps, log tails, stack traces, or
  path-heavy wrapper payloads to Telegram. Keep those details local; Codex
  `agent_message` events are forwarded automatically for agent conversations.
- Completed-work messages must still say what happened. Summaries
  should include 2-5 human-readable result lines, such as pass/fail state,
  produced review artifacts, validation count, measured runtime/rate, and the
  next gate. Do not replace real results with a fixed placeholder like
  "command and full log are kept locally".
- Do not upload Markdown files as Telegram documents. Telegram clients may not
  open `.md` attachments reliably. Send a short inline summary and a local path
  instead, or create an explicit `.txt`, `.pdf`, or image artifact when remote
  viewing matters.
- Do not send long config files, long logs, full prompts, full environment
  dumps, or large excerpts through Telegram messages.
- Sanitize every Telegram message before sending: include only the decision,
  current state, command/run id, short path, and next action.
- For failures, summarize the root cause and point to the local log path. Do not
  paste stack traces unless they are short and non-sensitive.
- Prefer contact sheets or one reviewed artifact over raw output batches.

## Configure

Preferred setup command:

```bash
scripts/setup_telegram_notify.py
```

This command validates the bot token, discovers or validates the chat id, writes
`.secrets/notify.env` with mode `600`, and sends a live test notification.

Manual fallback:

```bash
cp -n config/notify.env.template .secrets/notify.env
chmod 600 .secrets/notify.env
```

Then fill these values in `.secrets/notify.env`:

- `TELEAGENT_BOT_TOKEN`
- `TELEAGENT_CHAT_ID`

Email fallback is used when Telegram is missing or fails and these are set:

- `TELEAGENT_SMTP_HOST`
- `TELEAGENT_SMTP_PORT`
- `TELEAGENT_SMTP_FROM`
- `TELEAGENT_SMTP_TO`
- optional auth: `TELEAGENT_SMTP_USER`, `TELEAGENT_SMTP_PASSWORD`

Images are disabled by default with `TELEAGENT_NOTIFY_ALLOW_IMAGES=0`. Enable them only when a reviewed image should be sent remotely.
File uploads are disabled by default with `TELEAGENT_NOTIFY_ALLOW_FILES=0`.
Uploads are capped by `TELEAGENT_NOTIFY_MAX_UPLOAD_MB`, default `20`.

## Agent Quickstart

Future agents should use this exact sequence.

1. Do not print `.secrets/notify.env`. Check only permissions or dry-run output.
2. Check whether Telegram is already configured:

   ```bash
   python3 scripts/notify.py --title "notify test" --message "dry run" --dry-run
   ```

3. If dry-run says `"telegram": false`, run the interactive setup:

   ```bash
   scripts/setup_telegram_notify.py
   ```

4. If dry-run says `"telegram": true`, send a real non-sensitive test:

   ```bash
   python3 scripts/notify.py --title "notify live test" --message "Bridge notification test"
   ```

5. For long commands, use the reporting wrapper:

   ```bash
   scripts/tmux_run_with_report.sh --title "Long task" -- bash -lc 'your-command --with-arguments'
   ```

6. For commands that should run in the persistent tmux session:

   ```bash
   scripts/tmux_send_reported.sh --title "Long task" "your-command --with-arguments"
   ```

7. For periodic heartbeat reports:

   ```bash
   scripts/start_tmux_auto_reporter.sh --session tele-agent --interval 1800
   ```

Use concise titles. Do not include raw prompts, image paths meant to stay private,
tokens, full environment dumps, long config snippets, or long log excerpts in
notification messages.

## Image And File Uploads

Send a reviewed image through Telegram:

```bash
python3 scripts/notify.py \
  --title "image test" \
  --message "safe reviewed image" \
  --image /path/to/reviewed-image.png \
  --allow-images
```

Send a reviewed file through Telegram:

```bash
python3 scripts/notify.py \
  --title "file test" \
  --message "safe reviewed file" \
  --file /path/to/reviewed-report.txt \
  --allow-files
```

Do not send private raw generations, unreviewed material images, token files,
full environment dumps, or large run directories. Prefer a small contact sheet or
single reviewed artifact, and include `--allow-images` or `--allow-files` only on
the specific command that should upload.

Do not send `.md` files as Telegram documents. If the artifact is Markdown,
send a concise message with the local path, or make an explicit `.txt`/`.pdf`
export for Telegram.

## Telegram To Tmux Inbox

Start the Codex agent window and point Telegram inbound messages at it:

```bash
scripts/start_codex_agent.sh
```

Start a persistent Telegram listener in its own tmux window:

```bash
scripts/start_telegram_inbox.sh --session tele-agent --target-pane tele-agent:0.0
```

Recover both the managed Codex agent and listener after an unexpected exit or
host restart:

```bash
scripts/ensure_telegram_relay.sh
```

Once it is running, send normal text or a PDF/TXT/MD document to the Telegram
bot to relay it to the target tmux agent pane. Slash commands are reserved:

```text
/ping
/status
/agent_status
/reauth
/resume_goal
/codex /goal resume
/replay_messages 357 358
/replay_last 2
/replay_last_long 2
/recent_messages
/start_agent [LEVEL]
/kill_agent
/restart_agent [LEVEL]
/reasoning max
/timed 2 audit the goal agent and act on the result
/timed list
/timed remove 1
/timed remove
/help
```

If Codex reports that its refresh credential was revoked, the still-running
listener sends a mechanical alert and stops pasting new work into the broken
agent. `/agent_status` shows the detected auth failure and recovery phase.
`/reauth` starts Codex device-code sign-in and sends the browser link and
one-time code to the configured chat. After success it replaces the agent only
when its desired state is `running`; an intentionally stopped agent remains
stopped. Failed normal messages are never silently queued.

The default relay mode is `tmux-enter`, but it refuses to press Enter into a
plain shell pane such as `bash`. Prefer `scripts/start_codex_agent.sh`; it starts
Codex in `tele-agent:codex.0` and restarts the listener to target that pane.
From Telegram, `/start_agent [LEVEL]` starts only a stopped or missing agent and
refuses if one is already running. `/kill_agent` stops it persistently while
leaving the listener online. `/restart_agent [LEVEL]` replaces only a running
agent and refuses when stopped. These commands never accept a task prompt. Send
the task afterward as normal Telegram text. While the desired state is stopped,
normal messages are refused and neither watchdog recreates Codex.

If Codex Goal mode blocks with `Goal blocked (/goal resume)`, the listener
automatically sends `/goal resume` before relaying the next normal Telegram text.
Use `/resume_goal` to resume manually, or `/codex /goal resume` for an explicit
Codex slash command. These commands are only relayed when the target pane is
running Codex, never to a plain shell pane.

Inbound PDF, TXT, and Markdown documents are downloaded through Telegram's
`getFile` interface into
`$TELEAGENT_SCRATCH/inbound_documents/` (defaults to
`$HOME/.local/share/tele-agent/inbound_documents/`). The listener
accepts at most 20 MiB, validates PDF headers or UTF-8 text, gives files private
directory/file permissions, and relays the local path, size, SHA-256, and any
caption to Codex. Unsupported types and malformed or oversized files are
rejected without being relayed. This inbound behavior is independent of the
outbound upload restrictions above.

Telegram photos are handled the same way: the largest available size is
downloaded through `getFile` into `$TELEAGENT_SCRATCH/inbound_photos/`
(default cap 10 MiB), validated as a JPEG/PNG/WebP image, and relayed to
Codex with its local path, dimensions, SHA-256, and caption. Photos with or
without captions are relayed; malformed or oversized images are rejected.

If a long correction was split across multiple Telegram messages and Codex only
handled one part, use `/replay_messages` with the relevant Telegram `message_id`
values. The listener reads those records from the scratch JSONL log and relays
them as one ordered batch, for example `/replay_messages 357 358`.

For normal phone use, prefer `/replay_last 2` or `/replay_last_long 2`. The
`long` variant skips short follow-up messages and replays the most recent long
Telegram instructions as one batch. Use `/recent_messages` only when you need to
inspect ids, timestamps, character counts, and short previews.

Successful normal-message relay is silent from the listener itself. The listener
then tails the linked Codex session JSONL and forwards each concise
`agent_message`. Intermediate messages have no marker; only the final answer has
`∎` appended. Agents must not call `scripts/telegram_agent_reply.sh`
for normal conversation replies.

`/timed HOURS MESSAGE` stores a delayed message in scratch-backed listener state
and relays it through the same confirmed Codex path when it is due. `HOURS` may
be a positive integer or decimal, such as `2` or `0.5`. The schedule survives
listener restarts and preserves the original Telegram sender and reply context,
so the due message enters the existing Codex conversation like a new user
message instead of launching a separate agent. Immediately before relay, the
bot posts a visible **Timed message fired** blockquote as a reply to the
original `/timed` command. Telegram does not permit a bot to author that visible
message as the user.

`/timed list` returns a due-time-ordered, Telegram-formatted numbered list of
every active timed message for the current chat, with each message body in its
own blockquote. Terminal delivered, failed, and cancelled history is hidden.
Use `/timed remove NUMBER` to
remove one displayed entry, or `/timed remove` to clear all active entries for the
current chat. If an entry was already submitted to Codex, removing its scheduler
record cannot recall the delivered user turn and the listener says so.

Forwarded agent messages use normal CommonMark. The listener parses a restricted
subset into Telegram-safe HTML, escapes raw HTML, and retries a rejected formatted
message once as plain text. Agents should not emit Telegram-specific HTML or
MarkdownV2 themselves.

The tested tmux submit path launches Codex with `--no-alt-screen`, inserts each
complete payload through a private bracketed tmux paste buffer, and uses the
listener default `--submit-delay 2.0` before pressing Enter. If the Telegram
message ID is not yet present in the linked Codex session JSONL, the listener
retries Enter once without repasting the payload. If it is still absent, the
listener persists a pending confirmation. Later polling rechecks the session
and, after a short grace period, searches the pane and its scrollback, then
waits while any Codex turn is active. Once Codex is idle, it retries Enter only
if the same complete marker is still in the latest composer prompt for the same
registered agent. Slow JSONL persistence remains pending and is retried at a
bounded interval; it is not reported as a lost message. A marker in pane history
means the TUI accepted the payload, so the listener keeps following it without
a warning and does not block newer input in that same process. A replacement
process still triggers the existing one-time replay recovery. A newer message
is never allowed to clear or append to an unresolved composer.

Accumulating Telegram relay logs are scratch-backed by default:

```text
$TELEAGENT_LOG_DIR/telegram_inbox.jsonl
$TELEAGENT_LOG_DIR/telegram_agent_outbox.jsonl
$TELEAGENT_LOG_DIR/telegram_agent_messages.state.json
$TELEAGENT_LOG_DIR/telegram_agent_lifecycle.state.json
$TELEAGENT_LOG_DIR/telegram_relay_confirmation.state.json
$TELEAGENT_LOG_DIR/telegram_timed_messages.state.json
$TELEAGENT_LOG_DIR/agents/<agent_id>/events.jsonl
```

Each spawned or adopted Codex agent has its own `agent_id`, `meta.json`, and
per-agent `events.jsonl`; `meta.json` records the detected
`~/.codex/sessions/...jsonl` path when available. `logs/readable/` may contain
small symlinks or `*.scratch_path` pointers for convenience.

## Exact Human Input Needed

If `.secrets/notify.env` does not already contain Telegram credentials, the human
must do these exact steps:

1. In Telegram, open `@BotFather`.
2. Send `/newbot`.
3. Choose any display name, for example `Bridge Notify`.
4. Choose a unique username ending in `bot`, for example
   `bridge_notify_bot`.
5. Copy the token BotFather returns.
6. Open a chat with the new bot and send `/start`. For a group destination, add
   the bot to the group and send `/start` in that group.
7. In this source tree, run:

   ```bash
   cd <path-to-source>
   scripts/setup_telegram_notify.py
   ```

8. Paste the BotFather token when prompted. The prompt hides the input.
9. Press Enter after the `/start` message has been sent.
10. If more than one chat is listed, enter the number for the destination chat.

The script will write `.secrets/notify.env`, validate the destination with
Telegram, and send `Bridge Telegram notification test`.

## Commands

Dry-run provider detection without sending:

```bash
python3 scripts/notify.py --title "notify test" --message "dry run" --dry-run
```

Send a manual tmux status report:

```bash
python3 scripts/tmux_agent_report.py --title "Bridge status" --session tele-agent
```

Start a periodic reporter in its own tmux window:

```bash
scripts/start_tmux_auto_reporter.sh --session tele-agent --interval 1800
```

Restart that reporter after changing interval or secrets:

```bash
scripts/start_tmux_auto_reporter.sh --session tele-agent --interval 900 --restart
```

Run a command with automatic start/finish/failure reports:

```bash
scripts/tmux_run_with_report.sh --title "Long task" -- bash -lc 'your-command --with-arguments'
```

Send a reported command into the persistent tmux session:

```bash
scripts/tmux_send_reported.sh --title "Long task" "your-command --with-arguments"
```

For complex commands, put the command in a small script and pass that script
through `tmux_run_with_report.sh`. This avoids shell quoting mistakes in tmux.

## Logs

Notification attempts are recorded in:

- `logs/readable/notifications.jsonl`
- `logs/readable/auto_reports/`

The logs record provider success/failure and report paths, but not token values.
Dry-run entries also include a short message preview so configuration tests are inspectable.

## Behavior

- Telegram is attempted first by default.
- SMTP email is attempted if Telegram is missing or fails.
- `scripts/tmux_agent_report.py` captures available system status, scratch
  usage, filesystem space, and recent tmux pane output.
- Disk-space checks are host-specific; use the platform's quota tool where
  available.
- Commands wrapped by `scripts/tmux_run_with_report.sh` may write concise result
  lines to `$TELEAGENT_RUN_SUMMARY_PATH`; the wrapper sends those lines at
  completion without exposing commands or logs.
- `scripts/tmux_auto_report_loop.sh` repeats that report at the configured interval.
- `scripts/start_tmux_auto_reporter.sh` runs the loop in a dedicated tmux window named `reporter` by default.

## Image generation through the ChatGPT desktop app

`scripts/app_image_gen.py` generates an image using the local ChatGPT desktop
app's built-in "Create image" tool (gpt-image). It talks to the already-running
app over its Chrome DevTools endpoint and never uses `OPENAI_API_KEY`, so image
generation is billed through the ChatGPT subscription, not the paid API.

Host requirements:

- The desktop app is installed, signed in, and running with
  `--remote-debugging-port=9222`.

Usage:

```bash
python3 scripts/app_image_gen.py --out /path/out.png "your image prompt"
```

The script fails fast when the app is not usable on a host, with exit codes:
`0` success, `2` app unavailable or not signed in, `3` usage error, `4`
generation failed or timed out. The app install, display stack, and debug port
are host provisioning and are intentionally not part of this script.

## Voice notes via local Whisper

The inbox accepts Telegram voice notes and transcribes them locally before
relaying the text to the agent. Binaries and models are host provisioning and
live outside the repo by default:

- `whisper-cli` at `~/whisper.cpp/build/bin/whisper-cli`
- model at `~/whisper.cpp/models/ggml-small.bin`
- `opusdec` at `~/voice/usr/bin/opusdec` with libs under
  `~/voice/usr/lib/x86_64-linux-gnu`

All four paths are configurable via
`--whisper-bin`, `--whisper-model`, `--opusdec-bin`, `--opusdec-lib-dir` (or
the matching `TELEAGENT_WHISPER_BIN`, `TELEAGENT_WHISPER_MODEL`,
`TELEAGENT_OPUSDEC_BIN`, `TELEAGENT_OPUSDEC_LIB_DIR` environment variables).
If any tool is missing on a host, the listener fails fast and replies
"Voice note not relayed" with the specific missing component instead of
hanging. Voice download size is capped by `--max-inbound-voice-bytes`.

## Observe-only group feeds

Group observation is a standalone process, fully decoupled from the relay
inbox. `scripts/group_feed_listener.py` long-polls Telegram with its own bot
token and appends observed messages to a local JSONL feed. It has no tmux or
Codex connection, and nothing it archives can queue, paste, or steer an
agent. Telegram only allows one `getUpdates` consumer per bot token, so this
listener must use its own token (`TELEAGENT_GROUP_BOT_TOKEN` by default), not
the token the relay inbox is polling.

Run it on each machine whose agents should read the shared group:

```bash
python3 scripts/group_feed_listener.py \
  --chat-id GROUP_ID \
  --owner-id USER_ID
```

Configuration:

- `--token-env` selects the environment variable holding the observer bot
  token; the token can also live in the normal secret env file.
- `--feed` / `TELEAGENT_OBSERVE_FEED_PATH` sets the archive path; the default
  is `$TELEAGENT_LOG_DIR/telegram_group_feed.jsonl`.
- `--offset-file` / `TELEAGENT_OBSERVE_OFFSET_PATH` persists the poll offset;
  the default is the feed path with a `.offset` suffix.
- `--chat-id` archives only the matching chat; omit it to archive every chat
  visible to the observer bot.
- `--owner-id` / `TELEAGENT_OBSERVE_OWNER_ID` marks that Telegram user's
  messages as priority in the feed.

An agent catches up by running `python3 scripts/group_feed.py --last 20`
(add `--owner-only` or `--since-minutes N`), or by opening the JSONL file
directly. Messages are data only; they are never treated as commands.

Telegram bots default to privacy mode, which means they only receive group
messages that mention them or reply to them. For the feed to retain the whole
conversation, disable privacy mode with BotFather: `/setprivacy`, pick the
observer bot, then `Disable`. This only affects inbound visibility; group
messages still never control the agent.
