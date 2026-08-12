# Telegram Codex Agent

This package uses Telegram as a lightweight remote-control path for a Codex
agent running in tmux on a Linux host.

## Data Flow

```text
Telegram chat -> inbox -> tmux paste -> Codex
Codex session JSONL -> agent_message follower -> Telegram chat
```

There are two long-lived processes:

1. `inbox`, which polls the allowlisted Telegram chat and relays normal
   text to Codex.
2. `codex`, an interactive Codex CLI process in another tmux window.

The listener does not execute arbitrary shell commands from Telegram.

Both processes are recovered by `scripts/ensure_telegram_relay.sh`. Install it
in the user crontab at boot and once per minute so a host restart or lost tmux
window recreates the managed agent and listener:

```cron
@reboot <abs-path>/scripts/ensure_telegram_relay.sh >/dev/null 2>&1 # tele-agent-relay
*/1 * * * * <abs-path>/scripts/ensure_telegram_relay.sh >/dev/null 2>&1 # tele-agent-relay
```

## Reply Behavior

The listener automatically follows the Codex session JSONL associated with the
active tmux pane. Every `event_msg` with payload type `agent_message` is sent to
Telegram exactly once.

- Commentary messages have no phase marker.
- Only `phase=final_answer` receives a trailing `∎` marker.
- Commentary is capped at 1200 characters and final answers at 3600 characters.
- Agent CommonMark is rendered as a conservative Telegram HTML subset: headings,
  emphasis, safe links, inline code, fenced code blocks, lists, and quotations.
  Raw HTML is escaped. A Telegram entity-parsing failure is retried once as
  plain text so formatting cannot block delivery.
- Agents should keep user-facing messages concise and must not call
  `scripts/telegram_agent_reply.sh` for normal conversation replies.
- Reasoning, tool calls, command output, prompts, and other session events are
  never forwarded.

Delivery progress is stored in:

```text
$TELEAGENT_LOG_DIR/telegram_agent_messages.state.json
```

The state uses a byte offset, so listener restarts do not duplicate messages.
When the feature first sees an old or ambiguous session, it starts at EOF rather
than sending historical messages. A newly registered agent session is read from
the start only when its embedded start timestamp matches the agent launch.

## Usage-limit handling

The listener follows structured Codex usage errors only so it can notify the
operator when a particular relayed turn fails. Those events are audit history,
not an availability cache and never a relay gate. A new normal, replay,
control, or timed message is tried against Codex again; an old
`usage_limit_exceeded` event cannot mechanically reject it.

`/codex_usage`, `/codex_reset`, and the usage portion of `/agent_status` open a
fresh isolated Codex UI and query `/status` at command time. If Codex first
returns `refresh requested`, the helper repeats `/status` in that same fresh UI
until current limit rows render. A failed live query reports failure and does
not substitute a prior rollout snapshot.

The event-audit state is stored at:

```text
$TELEAGENT_LOG_DIR/telegram_codex_usage.state.json
```

## Authentication failure and recovery

The listener also follows structured Codex error events. When an error-bearing
event reports `codex_error_info: "unauthorized"` together with the explicit
refresh-token-revoked or refresh-token-already-used message, the listener:

- immediately sends one concise Telegram alert even though the Codex TUI is
  still running;
- marks Codex authentication as blocked and refuses new normal, replay,
  reasoning, and control relays with a mechanical reply;
- leaves due timed messages queued for retry instead of submitting them into
  the broken TUI;
- exposes the auth failure, credential-storage presence check, and current
  recovery phase inside `/agent_status`.

Run `/reauth` to repair the credential without logging into the host. The
listener starts `codex login --device-auth`, sends the official browser URL and
one-time code only to the configured Telegram chat, and never writes that code
to the accumulating listener log. The device login clears the stale credential
as part of the Codex login flow. After successful authorization, the listener
restarts only `tele-agent:codex.0`, preserves the currently displayed
reasoning level when it can detect it, clears the auth block, and sends a
completion notice. The failed task was not queued and must be resent.

The auth marker and device-login progress survive listener restarts:

```text
$TELEAGENT_LOG_DIR/telegram_codex_auth.state.json
$TELEAGENT_LOG_DIR/telegram_codex_reauth.state.json
```

### Manual banked reset

`/codex_reset` is a mechanical two-step operation; it does not invoke a model.
The listener temporarily gives the existing automatic reset watchdog exclusive
access to its isolated reset UI, reads the current number of banked resets and
every displayed expiry, restores the watchdog, and replies with those details.
The same reply reports only the limit windows returned by a fresh Codex
`/status` query. It does not select or spend a reset during this inspection.

The same Telegram sender must send `/Confirm` within five minutes. Plain
`Confirm` is rejected locally and is not relayed to Codex. The
listener then reopens the picker, revalidates availability, spends exactly one
`Full reset`, restores the automatic watchdog, and marks the old failure event
resolved for audit. It replaces the running Telegram Codex agent only when the
persisted desired state is `running`; an intentionally stopped agent remains
stopped. Missing, expired, duplicated, or failed confirmations are fail-closed.

Confirmation state is stored at:

```text
$TELEAGENT_LOG_DIR/telegram_codex_reset.state.json
```

The shared automatic watchdog lives at
`<path-to-codex-utils>/usage_reset_watchdog.sh` (a companion repo), uses its
own runtime directory and tmux session `codex-usage-reset-watchdog`.
`scripts/manual_codex_usage_reset.sh` must use those same defaults so
`/codex_reset` can safely take and return the watchdog lock. The watchdog also
opens a fresh Codex `/status` UI on every decision
cycle and parses the weekly row rendered by that exact command; it does not
join against or trigger from any rollout/session snapshot.

## Start

For a new Linux installation, create a Telegram bot, send `/start` to it, then
run the bootstrap. It prompts only for the DeepSeek API key and bot token,
discovers the chat ID, configures DeepSeek Flash, and starts both processes:

```bash
scripts/bootstrap_new_machine.sh
```

The bootstrap also creates an ignored `config/personality.md` from the neutral
tracked default when no host-specific personality exists. Edit only that local
file to give different installations different names or voices.

For an already configured installation, start or adopt the managed processes
directly:

```bash
cd <path-to-repo>
scripts/start_codex_agent.sh
```

This creates or reuses:

```text
tele-agent:inbox.0
tele-agent:codex.0
```

Without `config/relay.env`, the direct launcher uses `gpt-5.6-sol` with
`model_reasoning_effort="high"`,
`--no-alt-screen`, stable `fast_mode`, `--sandbox danger-full-access`, and
`--ask-for-approval never`. The model and effort can be overridden with
`TELEAGENT_CODEX_MODEL` and
`TELEAGENT_CODEX_REASONING_EFFORT`. The default remains the latest configured
model; selecting Spark is always an explicit operator action.

## Crash Recovery

`scripts/codex_agent_supervisor.sh` owns the Codex child process. If Codex exits
unexpectedly, it:

- logs the exit code and runtime to
  `$TELEAGENT_LOG_DIR/codex_agent.supervisor.log`;
- restarts with exponential backoff from 5 seconds up to 60 seconds;
- sends a short unlabeled restart notice on the first failure and then only
  every fifth consecutive failure;
- always starts an empty Codex chat; lifecycle commands never carry a task
  prompt.

The Telegram listener also watches the managed `tele-agent:codex.0` pane.
If the pane or Codex process disappears entirely, it recreates the agent without
waiting for another user message. If a Telegram message arrives during the
supervisor backoff window, the listener waits up to 75 seconds for Codex to
return and then relays the same message.

Use `--no-agent-watchdog` only for deliberate listener-only debugging. A tmux
server or host reboot still requires an external login/service restart; the
in-tmux watchdog cannot survive destruction of the tmux server itself.

Listener-only restart:

```bash
scripts/start_telegram_inbox.sh \
  --session tele-agent \
  --target-pane tele-agent:codex.0 \
  --restart
```

## Telegram Commands

```text
/start_agent [MODEL] [LEVEL]
/kill_agent
/restart_agent [MODEL] [LEVEL]
/model latest|spark|ds-flash [LEVEL]
/reasoning LEVEL
/interrupt PROMPT
/timed HOURS MESSAGE
/timed list
/timed remove [NUMBER]
/reauth
/codex_reset
/Confirm
/resume_goal
/codex SLASH_COMMAND
/replay_messages IDS
/replay_last [N]
/replay_last_long [N]
/recent_messages [N]
/agent_status
/status
/ping
/help
```

## Quick Actions

The listener attaches a reply keyboard to its outgoing messages with five
buttons: `/status`, `Any news?`, `Fix it.`, `It is stagnant? Unhealthy?`, and
`Faster.`. Tapping `/status` runs the listener command; the other four are
ordinary text and are relayed to the running Codex agent like any normal
message. The keyboard is re-attached on every reply, so it stays available
above the input field.

`/status` returns a compact snapshot: host and SGT time, desired and actual
agent state, target pane/process, the current model and reasoning effort from
the Codex footer, authentication state, agent uptime, tmux session, the linked
Codex session path, current context usage (tokens used, window size, and tokens
remaining with percentage), and the cumulative compaction count from the
session log. Uptime is measured from the registered agent launch time, falling
back to the Codex session start time when no registry entry exists. Detailed
agent, usage, and auth diagnostics remain under `/agent_status`; qstat/disk/tmux
dumps are no longer part of `/status`.

Normal non-command text and PDF/TXT/MD documents are relayed to Codex. When the
persisted desired state is `running`, a missing agent may be recovered before
relay. After `/kill_agent`, normal messages are refused until `/start_agent`;
the listener, schedules, and command handling remain online. The listener never
pastes into a pane occupied by another non-Codex process.

If a normal message reaches the listener while a Codex turn is active or an
earlier Telegram payload is still awaiting submission confirmation, it is
stored in a persistent FIFO and delivered automatically when the agent is idle.
A listener or host restart does not discard that queue. `/interrupt PROMPT`
bypasses this FIFO: it aborts the active Codex turn, clears any unconfirmed
composer payload, and submits `PROMPT` immediately. Already deferred FIFO
messages remain queued for later delivery.

For an accepted document, the listener calls Telegram `getFile`, downloads at
most 20 MiB into the private scratch directory
`$TELEAGENT_SCRATCH/inbound_documents/`, validates the PDF
header or UTF-8 text, and records private file permissions. Codex receives the
local path, original filename, type, byte size, SHA-256, and caption. A document
without a caption is still relayed. Unsupported extensions, malformed content,
and oversized files receive a concise rejection and are not sent to Codex.
Document contents are explicitly framed as user-provided data rather than
higher-priority instructions.

For each normal Telegram message, the listener checkpoints the linked Codex
session JSONL, atomically inserts the complete payload through a private
bracketed tmux paste buffer, then verifies that the message ID appears in a new
Codex user-turn event. It presses Enter once. If the event is not visible within
the short synchronous confirmation window, it retries Enter once without
clearing or repasting the composer; this handles an Enter that lands during the
brief transition after a Codex turn finishes. If the event is still absent, the
relay is recorded as pending rather than failed. Later polling first rechecks
the same session checkpoint. While a Codex turn is active, it does not touch the
composer. Once Codex is idle, it captures the pane including tmux scrollback and
retries Enter only when the same complete message marker is still in the latest
composer prompt for the same registered agent. It keeps retrying at a bounded
interval instead of treating slow JSONL persistence as a failure. If the marker
has moved into pane history, the TUI has accepted the payload and the listener
keeps following the checkpoint without warning or blocking newer input in that
same process. A replacement process still blocks and triggers the existing
one-time replay recovery. The listener never clears or appends to an unresolved
composer, preventing two messages from being merged into one Codex turn.
Confirmation latency is logged when the user-turn event eventually appears.

Agent lifecycle and model commands have non-overlapping meanings:

- `/start_agent [MODEL] [LEVEL]` starts only when stopped or missing. If an
  agent is already running, it refuses and directs the operator to
  `/restart_agent`.
- `/kill_agent` stops only the managed Codex agent and persists `stopped`; the
  Telegram listener remains online and neither its internal watchdog nor the
  external relay watchdog may recreate the agent.
- `/restart_agent [MODEL] [LEVEL]` requires a running agent and replaces it
  with a fresh chat; the previous conversation context is not preserved. When
  stopped, it refuses and directs the operator to `/start_agent`.
- `/model latest|spark|ds-flash [LEVEL]` changes the model in the running chat
  without restarting it. `latest` resolves to the configured default
  (`gpt-5.6-sol`); `spark` resolves to `gpt-5.3-codex-spark`, whose Codex usage
  window is reported separately by `/codex_usage` and `/agent_status`; `ds-flash`
  resolves to `deepseek-v4-flash` for a DeepSeek-backed pane (for example the
  managed agent after `/restart_agent ds-flash`) and supports only `max`
  reasoning. On an OpenAI pane the selector cannot offer `deepseek-v4-flash`,
  so run `/restart_agent ds-flash` first to relaunch the managed agent under
  the DeepSeek harness. When `LEVEL` is omitted, the live switch explicitly
  selects `high` for OpenAI models and `max` for `ds-flash`.

Start and restart use the configured Telegram model when `MODEL` is omitted.
The bootstrap configures `deepseek-v4-flash`; a manual installation with no
override uses `gpt-5.6-sol`. A one-token legacy effort such as
`/restart_agent max` still applies to that default model. Explicit model forms
include `/restart_agent spark`, `/restart_agent spark xhigh`, and
`/restart_agent ds-flash`. Spark supports `low`, `medium`, `high`, and `xhigh`;
the latest model also supports `none`, `minimal`, `max`, and `ultra` where
appropriate. `ds-flash` relaunches the managed agent with a DeepSeek Codex home
(provider and catalog bundled under `config/deepseek/`, key supplied through
`TELEAGENT_DS_KEY_FILE`) and is fixed to `max` reasoning. Lifecycle commands
never accept or infer an initial prompt. Send the task later as an ordinary
Telegram message.

The desired lifecycle state is stored in:

```text
$TELEAGENT_LOG_DIR/telegram_agent_lifecycle.state.json
```

`/reasoning LEVEL` changes reasoning effort in the current Codex chat without
restarting it. The live selector supports `low`, `medium`, `high`, `xhigh`,
`max`, and `ultra`; use `/restart_agent` for startup-only `none` or `minimal`.
The listener drives Codex's model/reasoning selector, keeps the active model
unchanged, verifies the footer value, and fails closed if the installed CLI UI
does not match the expected selector. While Spark is active, `/reasoning`
rejects `max` and `ultra` locally because Spark's selector ends at `xhigh`.
While `ds-flash` is active, `/reasoning` rejects every level except `max`
because the DeepSeek harness gates DeepSeek agents to max reasoning.

If Codex Goal mode is blocked, the listener resumes it before relaying the next
normal Telegram message. Replay commands combine selected previous Telegram
messages into one ordered request.

`/timed HOURS MESSAGE` accepts positive integer or decimal hours, persists the
schedule in scratch, and injects `MESSAGE` through the ordinary confirmed relay
path when due. For example, `/timed 0.5 check the current run` schedules a wake
half an hour later. Listener restarts do not discard pending timed messages. The
original sender and reply context are retained, and the due message is delivered
to the current Codex conversation rather than a separate process. A delivery
that cannot immediately reach Codex remains queued and retries automatically.
Immediately before Codex delivery, the bot posts a visible **Timed message
fired** blockquote replying to the original `/timed` command, so the prompt and
reply both remain visible in Telegram history. Telegram's Bot API cannot create
a message authored by the user's account; the visible prompt is necessarily
bot-authored.

`/timed list` shows every active timed message for the current Telegram chat,
ordered by due time and numbered consistently. Each Telegram-formatted entry
includes its status, due time in SGT, and a bounded message preview rendered as
a separate blockquote. Delivered, failed, and cancelled
history is retained internally for delivery safety but hidden from the list.
`/timed remove NUMBER` removes the corresponding numbered active entry;
`/timed remove` removes all active entries for the current chat. Removing scheduler
state cannot recall a message that has already been submitted to Codex, so the
listener reports that case explicitly.

## Steering a Separate Persistent-Goal TUI

This relay may be asked to steer another Codex TUI, such as a persistent goal
agent. That is an operator intervention, not ordinary Telegram message
delivery. It requires the user's explicit authorization for the exact target
and text.

Treat an unqualified request to “steer” as immediate delivery. The safe order
is freeze automatic control, interrupt, verify the replacement turn, and resume
only after its effective model identity is proven:

1. Capture the target pane and the root session JSONL. Record the exact
   objective, current goal status, and the effective model and reasoning effort
   from the latest actual `turn_context`. Saved configuration and the TUI model
   label are not evidence of the effective model.
2. Disable any supervisor, watchdog, or keep-waiting mechanism that could
   automatically resume or restart the target during the intervention. Confirm
   that no queued follow-up input exists.
3. With the target composer empty, use its displayed `Esc` action. Do not paste
   or queue the steering text first.
4. Wait for the root JSONL to record `turn_aborted` and, in Goal mode,
   `thread_goal_updated` with `status: paused`. Also wait until the TUI is
   visibly idle and input-responsive.
5. Atomically paste the exact steering text from a named tmux buffer and press
   `Enter` once. Verify it appears byte-for-byte as a root `user_message`.
6. Immediately inspect the new turn's actual `turn_context`. The required
   effective model and reasoning effort are target-specific; read them from
   the target's own controller/launch spec (for example a pinned
   `agent-model.json`), never from the TUI label. If it reports any other
   model or effort, abort that turn immediately, wait for the goal to be
   paused, then stop the target runtime so no fallback model can execute. Do
   not send `/goal resume`, do not try another model, and do not re-enable
   automatic control.
7. Only after the steering turn's effective model and effort are verified,
   atomically paste `/goal resume`, press `Enter` once, and verify an active
   goal with the objective unchanged. Re-enable automatic control only after
   that verification.

Never use `Tab` before an immediate steer. `Tab` is only for a user-requested
non-immediate queue, and a TUI must not be interrupted while queued input is
pending. Never repaste text merely because it remains visible; retry only
`Enter` once after inspecting the pane and JSONL.

If interruption leaves the target TUI hung or non-responsive, stop sending
keys. Do not blindly queue more text or repeatedly paste `/goal resume`.
Restarting or resetting the target requires separate explicit authorization.
If the required effective model is unavailable, leave the target stopped; an
available fallback model is never permission to run it.

## Session Linking Safety

Each spawned or adopted agent has an `agent_id`, metadata, and event log under:

```text
$TELEAGENT_LOG_DIR/agents/<agent_id>/
```

The registry normally locates the session JSONL through the live Codex process
file descriptor. A launch-time fallback is allowed only for the newly started
process and uses the embedded session timestamp, never file mtime alone. Codex
may create that JSONL lazily after its first submitted message; for a newly
launched registry-bound pane, the listener permits that bootstrap submission
and then discovers and verifies the new rollout before confirming delivery.
Before forwarding or linking, the listener verifies that:

- the path is under a Codex home `sessions/` directory (default
  `~/.codex/sessions/` or the target's `CODEX_HOME`);
- the session cwd is the tele-agent repo root;
- a session linked to a newly launched process was created at the agent launch
  time, including links discovered through process file descriptors;
- a fallback session was created at the agent launch time.

This prevents a stale Telegram pane record from forwarding messages belonging
to an unrelated Codex session. On any changed or ambiguous link, the follower
starts at EOF unless the session's embedded timestamp proves that the newly
registered agent created it, preventing historical replay after restart.

## Logs

Canonical accumulating relay logs live under the configured scratch directory:

```text
$TELEAGENT_LOG_DIR/telegram_inbox.jsonl
$TELEAGENT_LOG_DIR/telegram_inbox.offset
$TELEAGENT_LOG_DIR/telegram_agent_messages.state.json
$TELEAGENT_LOG_DIR/telegram_codex_usage.state.json
$TELEAGENT_LOG_DIR/telegram_codex_auth.state.json
$TELEAGENT_LOG_DIR/telegram_codex_reauth.state.json
$TELEAGENT_LOG_DIR/telegram_codex_reset.state.json
$TELEAGENT_LOG_DIR/telegram_relay_confirmation.state.json
$TELEAGENT_LOG_DIR/telegram_relay_queue.state.json
$TELEAGENT_LOG_DIR/telegram_timed_messages.state.json
$TELEAGENT_LOG_DIR/codex_agent.supervisor.log
$TELEAGENT_LOG_DIR/telegram_agents.index.jsonl
$TELEAGENT_LOG_DIR/agents/<agent_id>/events.jsonl
```

Logs record delivery metadata and offsets, not the forwarded response body.
Secrets remain only in `.secrets/notify.env`.

## Safety

- Only the configured chat id is accepted.
- Telegram never maps directly to arbitrary shell execution.
- Do not send tokens, secrets, raw private materials, long logs, full prompts,
  stack traces, or unreviewed outputs.
- For outbound agent uploads, do not substitute or convert Markdown artifacts;
  follow the requested format and the notification policy. Inbound `.md`
  documents from the configured user are accepted by the listener.
- No Telegram API call is needed for local unit tests.

## Local Verification

Without starting the live listener or sending Telegram messages:

```bash
python3 -m unittest -v tests/test_telegram_auth_recovery.py \
  tests/test_telegram_inbound_documents.py \
  tests/test_telegram_usage_limit.py \
  tests/test_telegram_agent_registry.py
bash -n scripts/codex_agent_supervisor.sh scripts/start_telegram_inbox.sh \
  scripts/start_codex_agent.sh scripts/manual_codex_usage_reset.sh
python3 scripts/notify.py --title "notify test" --message "dry run" --dry-run
```

The tests cover exact usage-limit detection, false-positive avoidance at a
rounded 100%, persistence, expiry/recovery, session-link replay prevention,
live reasoning selection, and mechanical relay bypass without a Telegram API
call.
