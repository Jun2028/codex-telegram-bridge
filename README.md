# tele-agent

Run a Codex coding agent through a private Telegram chat. Telegram messages and
PDF/TXT/MD documents are relayed to a supervised Codex TUI in tmux, and normal
Codex replies are sent back automatically.

## What you need

- A Linux x86_64 or arm64 machine with internet access.
- One DeepSeek API key.
- One Telegram bot token from [BotFather](https://t.me/BotFather).

No OpenAI account, Telegram chat ID, Node.js installation, or hand-written
Codex configuration is required for the default setup.

## How model providers are connected

The Telegram listener does not call a model API itself. It controls the Codex
CLI, and Codex calls the configured provider. The included DeepSeek profile
sets a provider URL, reads `DEEPSEEK_API_KEY`, and uses the OpenAI Responses
wire protocol. A different backend can be configured the same way when it
implements a compatible Responses endpoint; an arbitrary incompatible API
needs an adapter. See the
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
for provider fields.

The included profile enables Codex's stable multi-agent implementation. The
newer `multi_agent_v2` path remains disabled because it is not yet reliable
with this provider.

## Quick start

1. Create a Telegram bot with BotFather.
2. Open the new bot in Telegram and send it `/start`.
3. Clone or unzip this source, then run:

   ```bash
   scripts/bootstrap_new_machine.sh
   ```

4. Paste the DeepSeek API key and Telegram bot token when prompted. Both inputs
   are hidden. If the bot can see more than one chat, select the intended chat.
5. Send `/ping`, then send a normal task to the bot.

The bootstrap checks or installs the Linux prerequisites and a compatible Codex CLI,
discovers the Telegram chat ID, stores both secrets with mode `0600`, configures
`deepseek-v4-flash` at `max` reasoning, starts the supervised agent and inbox,
and installs a crontab watchdog when crontab is available.

## Agent personality

The tracked `config/personality.default.md` is deliberately neutral. Each host
can define a different identity and voice in `config/personality.md`; that local
file is ignored by Git and does not affect relay behavior or safety rules.

## Secret storage

- DeepSeek: `~/.config/tele-agent/deepseek.env`
- Telegram: `.secrets/notify.env`

Both paths are excluded from Git. The source archive contains neither file.

## Operation

Useful Telegram commands include `/status`, `/agent_status`, `/restart_agent`,
`/kill_agent`, `/start_agent`, `/interrupt PROMPT`, `/model`, `/reasoning`,
`/timed`, and `/help`.
The listener only accepts the Telegram chat selected during setup.

See [docs/telegram_codex_agent.md](docs/telegram_codex_agent.md) for lifecycle
and relay behavior, and [docs/notifications.md](docs/notifications.md) for file
delivery and notification behavior.

## Verification

Run the local suite with:

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/*.sh
```

## Manual configuration

Advanced deployments can copy `config/relay.env.template` to
`config/relay.env` and override `TELEAGENT_*` values.

## License

MIT. See [LICENSE](LICENSE).
