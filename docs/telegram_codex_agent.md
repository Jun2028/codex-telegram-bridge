# Telegram operator guide

One bot controls one persistent Codex agent. Use ordinary Telegram messages for
coding, experiments and machine operations. The listener provides delivery and
controls; Codex performs the requested work with the configured host access.

## Existing Codex installation

Install Python 3.11+, tmux and a Codex CLI compatible with your chosen model.
Optional `markdown-it-py` improves CommonMark rendering; the relay includes a
fallback. Sign in using the installed Codex CLI before starting the agent.

```bash
cp config/relay.env.template config/relay.env
# Edit this ignored file for your installed provider, for example:
# export TELEAGENT_CODEX_MODEL="gpt-6-astra"
# export TELEAGENT_CODEX_REASONING_EFFORT="xhigh"
python3 scripts/setup_telegram_notify.py
python3 scripts/notify.py --title "notify test" --message "dry run" --dry-run
scripts/start_codex_agent.sh
```

The launcher starts `tele-agent:codex.0`, keeps it under the agent supervisor and
starts/retargets the inbox. For a separate listener start, use
`scripts/start_telegram_inbox.sh --target-pane tele-agent:codex.0`.
`/start_agent` starts only a stopped agent; `/restart_agent` replaces only a
running agent and starts a fresh chat. Neither accepts a task prompt.

## Daily use

Use `/status` or its Refresh button for actual activity, model, authentication,
queue counts and the age of the last reply check. `/ping` checks the listener.
`/queue` lists waiting work, pending receipt checks and delivery problems with
dates and reasons. An unconfirmed receipt does not mean the task failed;
check before resending. Cancel an unsent item with
`/cancel ID`; cancelling a queued item does not interrupt the running task.

During an ordinary turn, input from the bound chat can steer that task. Inputs
from another chat/topic wait in the single FIFO. Goal-mode work is not paused
by the queue. Use the explicit `/interrupt PROMPT` control when you intend to
abort and replace the current turn. `/kill_agent` persistently stops the agent
while leaving the Telegram listener online.

The agent's normal commentary and final replies are forwarded automatically.
Tool output, reasoning, prompts and raw logs are not forwarded. Finals in the
private chat receive `∎` unless `TELEAGENT_SUPPRESS_FINAL_MARKER=1`; group routes
send concise finals without that marker. Long text is split at Telegram's
UTF-16 limit instead of silently truncated. Formatting rejection falls back
to plain text. A rare ambiguous network acknowledgment can produce a duplicate;
see [delivery guarantees](architecture.md).

## Model controls

`/models` lists aliases. Use `/model NAME [LEVEL]` or `/reasoning LEVEL`.
`latest` and `astra` select GPT-6 Astra; other aliases are `sol`, `luna`, `spark`,
`ds-flash` and `ds-pro`. Supported reasoning values depend on the model.
The last actual `turn_context` is the authority for the model shown in status.
Before the first turn, status may fall back to the configured/live selector.

Managed launches disable Codex's low-quota model-switch reminder with
`notice.hide_rate_limit_model_nudge=true`. Automated relay input and Enter
retries refuse the "Approaching rate limits" model selector. Low quota must
not change the model; use the explicit model controls to change it.

Goal status is read from the bound session's persistent goal database. It
includes paused, usage-limited, budget-limited and complete states; unavailable
state is shown as unknown, never inferred from terminal text.

Changes within the same Codex home preserve the conversation. Moving to another
provider home relaunches the agent with a fresh chat. `/restart_agent MODEL LEVEL`
is the explicit way to restart with selected options.

## Groups, replies and topics

The relay discovers its username using Telegram getMe and identifies the owner
from the configured private chat. Existing `TELEAGENT_OWNER_USER_ID` and
`TELEAGENT_BOT_USERNAME` overrides are respected. Discovery failures retry
automatically while private controls remain available. Mention the exact bot,
use an addressed command such as
`/status@YourBot`, or reply to that bot. Captions are recognized. With
`TELEAGENT_REQUIRE_GROUP_MENTION=0`, the owner's unaddressed group messages are
also accepted; keep the default when several bots share a group.

The owner can operate the agent from groups. Other addressed members can
submit tasks when the owner is confirmed as a member; the owner alone may
stop/restart the agent, change models, cancel work or use advanced controls.
Membership-check failures reject access. Making the bot a group administrator
allows Telegram to perform these checks reliably.

Replies retain the original topic and source-message reference. PM input cannot
retarget a running group turn and group input cannot retarget a PM turn. Mixed
inputs in a single turn send remaining output privately. Account controls and
recovery codes belong in the owner's PM. `/agent_status` is a private detailed
account diagnostic; `/status` is the quick group-safe view.

All chats share the agent's context. The bridge isolates destinations and replay
records, not model memory. Separate bots have separate agent histories.

## Documents, photos and voice

Send a PDF, TXT, MD or HTML document with an optional caption. The listener
validates its type/size and downloads it under the private scratch inbox.
Photos and voice notes are also accepted. Voice transcription needs the configured
local whisper/opus tools; missing tools produce an actionable reply. Attachments
are user data, and never acquire higher instruction priority.

Keep Markdown artifacts as `.md` when sharing them. Use reviewed uploads as
described in [notifications.md](notifications.md).

## Scheduled tasks and history

`/timed HOURS MESSAGE` accepts positive decimal hours. `/timed list` shows tasks
for this chat; `/timed remove N` removes one and `/timed remove` removes all.
At delivery time, a timer waits for the agent to be free and uses the original
chat/topic. An interrupted, unconfirmed paste requires inspection before retry.

`/recent_messages [N]` lists requests from this chat. `/replay_last [N]`,
`/replay_last_long [N]` and `/replay_messages IDS` resubmit selected requests.
Group records are always separate from private replay records. The optional
`TELEAGENT_PER_CHAT_INBOX_RECORDS=1` also splits private records from the audit
log. Optional `TELEAGENT_PEOPLE_MEMORY=1` keeps bounded per-person continuity;
its private summary is not inserted into group prompts.

## Multiple bots on one host

Use one named instance per bot:

```bash
TELEAGENT_INSTANCE=beta scripts/start_codex_agent.sh
```

Each instance gets `tele-agent-beta`, a separate runtime/scratch directory,
`.secrets/notify-beta.env`, ignored `config/relay-beta.env`, a private Codex
home, and its own conversation. Non-main homes cannot point to the main home
or another instance's home. Credential/capability sharing does not share
sessions, histories, goals or thread databases.

An instance can set `TELEAGENT_CODEX_ACCESS_MODE=chat-only` in its local config.
That profile uses a minimal home and empty working directory with tool access
disabled. It cannot open relayed local documents. This restricts model tools;
use an OS/container boundary when the runtime itself must lack host-file access.

## Recovery and deployment

`/reauth` runs the fixed device-code sign-in flow in private. `/codex_usage`
queries the account. `/codex_reset` followed by `/Confirm` explicitly redeems a
banked reset when supported. Authentication recovery respects an intentionally
stopped agent.

The Codex supervisor restarts unexpected exits with bounded backoff; the
listener watchdog recreates a missing managed pane. For host restarts, install
these per-instance crontab entries (the bootstrap does this when available):

```cron
@reboot TELEAGENT_INSTANCE=beta /absolute/repo/scripts/ensure_telegram_relay.sh >/dev/null 2>&1
*/1 * * * * TELEAGENT_INSTANCE=beta /absolute/repo/scripts/ensure_telegram_relay.sh >/dev/null 2>&1
```

For an inbox code update, use `scripts/deploy_listener.sh`. It restarts only the
instance's inbox and verifies the agent pane did not change. It preserves
state files and queued work. Do not restart Codex merely to update reply routing.
Use [architecture.md](architecture.md) to locate failed delivery records.
