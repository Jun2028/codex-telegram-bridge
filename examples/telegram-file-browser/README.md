# Telegram file-browser demo

This Mini App browses the example's `demo-files/` folder, previews images, and edits UTF-8 text files inside Telegram. The existing bot handles `/files`; a separate Node service reads and writes files on the configured machine. Opening or saving a file does not require a model call.

File access is disabled unless the operator or an authorized agent configures it for that machine and bot. Pulling this repository does not start a file service, publish a URL, or enable Files. Never reuse another machine's private URL, key or `.runtime/` directory. Named bots do not inherit the main bot's Files setting.

## Configure this machine and bot

Requires Node.js 18+ and the working Python/Telegram relay. No npm packages are needed. Run the following from the repository root, on the machine being configured:

1. Start `node examples/telegram-file-browser/server.cjs`. It listens on `127.0.0.1:8766`; `DEMO_PORT` overrides the port. The service creates a random local access key. Keep it running.
2. Route an HTTPS origin to that loopback address with your reverse proxy or tunnel. Do not expose the loopback port directly. The origin must serve the app at `/`.
3. Add the following setting to this bot's ignored `config/relay.env`, or `config/relay-<instance>.env` for a named bot. The path must be readable from the listener's environment:

   ```bash
   export TELEAGENT_MINI_APP_URL_FILE="$TELEAGENT_REPO/examples/telegram-file-browser/.runtime/launch-url"
   ```

4. Load the selected bot's environment and register its launch button:

   ```bash
   # Set TELEAGENT_INSTANCE first if this is a named bot.
   source scripts/relay_paths.sh
   python3 examples/telegram-file-browser/connect-menu.py --origin https://files.example.com
   ```

   Replace the example origin with this service's HTTPS origin. The connector reads the selected `TELEAGENT_SECRET_ENV`; `--secret-env` and `--repo` allow explicit paths. Credentials are read from the local secret file, never command arguments. Without `--origin`, it reads a local Cloudflare temporary address from `.runtime/tunnel.log`.

5. Restart only this bot's listener using `scripts/deploy_listener.sh`, then request `/status` in its owner's private chat. The normal command menu and Refresh, Queue and Help remain; Files is added alongside them. `/files` and the pinned **Open file browser** button also launch the app.

Rerun the connector when the HTTPS address changes. Fresh control replies read the updated URL without a listener restart. Existing messages may retain an old URL; use `/files` to get a current button. The machine must remain awake and online. Use separate example directories, ports and private runtime state for separate file services.

To disable Files, set `export TELEAGENT_MINI_APP_URL_FILE=""` in this bot's ignored configuration and restart its listener; stop the file service/tunnel to invalidate access through previously issued buttons. An explicit empty setting also clears a value inherited from the current shell. Do not commit runtime files or copy them to another machine.

## Share a real folder

By default the service shares only the example's `demo-files/`. To browse a
real folder on this machine, set it in this bot's ignored `config/relay.env`:

```bash
export TELEAGENT_FILES_ROOT="$HOME"
```

and start the service with the bot's environment loaded, so it can find the
bot's secret file:

```bash
source scripts/relay_paths.sh
node examples/telegram-file-browser/server.cjs
```

A real folder is treated differently from the sample one:

- **Only the bot's owner can open it.** Telegram signs the data it hands a Mini
  App with the bot's token. The service checks that signature on every request
  and requires the signed-in user to be the owner of this bot's private chat
  (or `TELEAGENT_OWNER_USER_ID`). The private link is still required, but it is
  no longer enough: someone who obtains it cannot list or read anything. A
  page left open for more than a day must be reopened.
- **It is read-only** unless `TELEAGENT_FILES_WRITABLE=1` is also set.
- **Downloads use a ticket** valid for one file for two minutes, so neither the
  access key nor the sign-in data appears in a download address.
- The service refuses to start if it cannot read the bot's token and owner.

Files and folders whose names start with a dot, and linked folders, are never
listed or served, so `.ssh`, `.secrets` and similar stay out of reach. Previews
and downloads are limited to 10 MB.

## Access boundary

Only the shared folder is exposed: `demo-files/` unless `TELEAGENT_FILES_ROOT` names another. Hidden files, linked folders and paths outside it are rejected. Edits, where allowed, keep a private backup under `.runtime/backups/` and reject stale revisions. The sample notes, CSV and plot are synthetic.

The sample folder authenticates with a private bearer key in the launch URL, not Telegram user identity: anyone who obtains that URL can read and edit the sample files. A real folder additionally requires Telegram's signed proof that the bot's owner opened it, as described above. In both cases keep the launch URL and the generated `Open demo.html` private; never post them in a group, log or shared artifact. The relay sends launch buttons only to its configured owner's private chat.

Run checks with `node --test examples/telegram-file-browser/test.cjs` and `python3 -m unittest discover -s tests -p test_telegram_mini_app.py`. CI runs both. The server uses Node's standard library; Telegram supplies the client-side Mini App bridge.

Scope: folder navigation, image previews, text editing, saving and small downloads. Uploads, deletion, renaming and a full IDE are not implemented. External editors do not share the save lock; coordinate concurrent writes before using this for active project files.
