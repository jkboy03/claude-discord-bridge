# Claude Discord Bridge

[![CI](https://github.com/jkboy03/claude-discord-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/jkboy03/claude-discord-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Use [Claude Code](https://claude.com/claude-code) from anywhere via Discord DMs. Send a message to your bot from your phone, your laptop, or any device with Discord — Claude Code runs on your home machine and replies in the same DM.

> **What this is:** a small Python service that wraps Claude Code's headless mode (`claude -p`) behind a Discord bot. Your DMs become Claude Code prompts. Claude's responses come back as plain Discord messages.
>
> **What this is not:** an Anthropic-API wrapper, a re-implementation of Claude Code, or a multi-tenant service. It runs on your own machine, uses your own Claude Code authentication (your Claude Max / Pro / Team subscription), and only responds to **one** Discord user (you).

---

## Table of contents

1. [Features](#features)
2. [How it works](#how-it-works)
3. [Prerequisites](#prerequisites)
4. [Setup — Discord application](#setup--discord-application)
5. [Setup — Server (so the bot can DM you)](#setup--server-so-the-bot-can-dm-you)
6. [Setup — Get your Discord user ID](#setup--get-your-discord-user-id)
7. [Setup — Install the bridge](#setup--install-the-bridge)
8. [Setup — Configure `.env`](#setup--configure-env)
9. [Run — systemd (recommended)](#run--systemd-recommended-linux)
10. [Run — alternatives](#run--alternatives-macos--no-systemd)
11. [Usage](#usage)
12. [Troubleshooting](#troubleshooting)
13. [Security notes](#security-notes)
14. [Development & testing](#development--testing)
15. [Contributing](#contributing)
16. [License](#license)

---

## Features

- **Real Claude Code, headless** — uses `claude -p` (Claude Code's official non-interactive mode). Same auth, same MCP servers, same hooks, same skills, same memory, same model selection.
- **Slash commands native to Discord** — type `/` in the DM to see `/new`, `/auto`, `/model`, `/effort`, `/tools`, `/status`, `/help` with proper dropdowns.
- **Bang commands too** — `!new`, `!auto on`, etc. work for users who skip the autocomplete.
- **Session continuity** — the bridge tracks Claude Code's `session_id` and resumes it on the next DM, so multi-turn context is preserved across messages.
- **Auto-mode by default** — runs with `--permission-mode bypassPermissions` so tool use just works on your phone (you can't approve permission prompts mid-DM). Toggle with `/auto off` if you want manual control.
- **Allowlist auth** — only your Discord user ID is honored. Everyone else's DMs are silently dropped.
- **Clean output** — no TUI chrome, no ANSI codes, no spinner artifacts. Claude's prose lands in Discord as plain markdown.

---

## How it works

```
┌──────────────┐     DM      ┌─────────────┐     spawn       ┌──────────────────┐
│  Your phone  │────────────▶│   bot.py    │────────────────▶│   claude -p      │
│   Discord    │             │  (Python)   │  --resume <id>  │  (headless mode) │
│              │◀────────────│             │◀────────────────│                  │
└──────────────┘   reply     └─────────────┘  stream-json    └──────────────────┘
                                                                      │
                                                                      ▼
                                                             ┌─────────────────┐
                                                             │  Anthropic API  │
                                                             │  (your Claude   │
                                                             │   subscription) │
                                                             └─────────────────┘
```

For each DM:

1. The bot receives the message via Discord's gateway.
2. If the sender is the allowlisted user, the bot spawns `claude -p --output-format stream-json --resume <session_id> "<prompt>"`.
3. Claude Code emits structured JSON events on stdout. The bot extracts `assistant` event text content and posts it back to the DM.
4. The `result` event provides the new `session_id`, which the bot stores for the next turn.

No TUI scraping. No regex chrome stripping. No tmux. Just Claude Code's official non-interactive interface.

---

## Prerequisites

- **Linux or macOS** with Python **3.10+** (Windows works with WSL2; native Windows is untested).
- **Claude Code installed and logged in.** Run `claude` once interactively and log in via `/login` (or set `ANTHROPIC_API_KEY`). Verify with `claude --version`.
- **A Discord account.** Free tier is fine.
- **Internet access on the host.** No inbound ports needed — the bot opens an outbound WebSocket to Discord.
- **(Optional) systemd** for auto-start. macOS users can use `launchd`, `tmux`/`screen`, or `nohup`.

---

## Setup — Discord application

Everything in this section happens at https://discord.com/developers/applications.

### 1. Create the application

1. Click **New Application**.
2. Name it (e.g. `Claude Bridge`). Click **Create**.

### 2. Get the bot token

1. Left sidebar → **Bot**.
2. Click **Reset Token** → **Yes, do it!**.
3. Copy the long token string. **You only see this once** — save it somewhere safe (you'll paste it into `.env` later).

### 3. Enable the privileged intent

Still on the **Bot** page:

1. Scroll to **Privileged Gateway Intents**.
2. Toggle ON: **MESSAGE CONTENT INTENT**.
3. Click **Save Changes** at the bottom.

> **Why:** as of August 2022 Discord requires bots to explicitly opt into reading message content. Without this toggle the bot's gateway connection is rejected and you'll see `discord.errors.PrivilegedIntentsRequired` in the logs.

### 4. Fix the "Private application cannot have a default authorization link" error (if you hit it)

If at any point Discord shows you that error when saving:

1. Left sidebar → **Installation**.
2. Find **Install Link** → set it to **None**.
3. Save.

You can keep your bot **Private** (Public Bot toggle off on the Bot page) — that's the right setting for a personal bridge. The error is just Discord refusing to expose a default install link for a private app.

### 5. Build the install (OAuth) URL

The bot needs **two scopes** to work:

- `bot` — adds the bot to a server.
- `applications.commands` — lets the bot register slash commands so they appear in Discord's `/` autocomplete.

Use the URL generator:

1. Left sidebar → **OAuth2** → **URL Generator**.
2. Under **Scopes**, check both: ✅ `bot` and ✅ `applications.commands`.
3. Under **Bot Permissions** (appears after you check `bot`), check: ✅ **Send Messages** and ✅ **Read Message History**.
4. Copy the **Generated URL** at the bottom.

Or build it directly. Replace `YOUR_APP_ID` with your application's client ID (visible on the **General Information** page):

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=67584&scope=bot+applications.commands
```

Don't open this URL yet — first you need a server to install the bot into.

---

## Setup — Server (so the bot can DM you)

To open a DM channel with a bot, you must share at least one Discord server with it. (This is a Discord platform rule.) The simplest path is a one-person personal server.

### Create a personal scratch server

1. In the Discord app: top-left ➕ → **Create My Own** → **For me and my friends**.
2. Name it anything (`bridge`, `claude`, `personal`).
3. Skip the invite step.

### Add the bot to it

1. Open the install URL from step 5 above in any browser.
2. Pick your scratch server from the dropdown.
3. Click **Authorize**.

The bot now appears in the server's member list. From this point on you can DM it from any device that's signed in to your Discord account — phone, desktop, web, all of them.

---

## Setup — Get your Discord user ID

The bridge gates all DMs by your numeric user ID, so you need to copy it once.

1. Discord settings → **Advanced** → toggle **Developer Mode** on.
2. Tap your own avatar (mobile) or right-click your name (desktop) → **Copy User ID**.

It's an 18–19 digit number (e.g. `123456789012345678`). Paste this into `.env` later.

---

## Setup — Install the bridge

```bash
git clone https://github.com/jkboy03/claude-discord-bridge.git
cd claude-discord-bridge

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Verify Claude Code is installed and logged in:

```bash
claude --version    # should print something like "2.x.x (Claude Code)"
claude -p "say hi"  # should print "Hi!" or similar
```

If `claude -p` asks you to log in, run `claude` interactively and use `/login` first.

---

## Setup — Configure `.env`

```bash
cp .env.example .env
chmod 600 .env       # readable only by you
$EDITOR .env
```

Fill in:

- `BRIDGE_DISCORD_BOT_TOKEN` — the token from step 2 of the Discord setup.
- `BRIDGE_ALLOWED_USER_ID` — your numeric user ID.

Optional:

- `BRIDGE_WORKDIR` — directory passed as `cwd` to `claude -p`. Defaults to your home directory. Set this to a project root if you want Claude Code to auto-resolve `CLAUDE.md` and project hooks from there.
- `BRIDGE_CLAUDE_BIN` — path to the `claude` binary. Auto-detected via `which claude` if unset.

---

## Run — systemd (recommended, Linux)

Install the service file with your install path substituted in:

```bash
sed "s|__INSTALL_DIR__|$PWD|g" claude-discord-bridge.service \
  > ~/.config/systemd/user/claude-discord-bridge.service

systemctl --user daemon-reload
systemctl --user enable --now claude-discord-bridge.service
```

Verify:

```bash
systemctl --user status claude-discord-bridge.service
journalctl --user -u claude-discord-bridge.service -f
```

You should see:

```
[bridge] logged in as YourBotName#1234 (id=...)
[bridge] allowed user id: ...
[bridge] mode: claude -p (headless), workdir=/home/you
[bridge] synced 7 global slash commands
```

To enable auto-start on boot (so you don't need to be logged in for the service to run):

```bash
sudo loginctl enable-linger $USER
```

### Useful commands

```bash
systemctl --user restart claude-discord-bridge   # bounce the bot
systemctl --user stop    claude-discord-bridge   # stop
systemctl --user disable claude-discord-bridge   # don't start on login
journalctl --user -u claude-discord-bridge -f    # live logs
journalctl --user -u claude-discord-bridge -n 200  # last 200 lines
```

---

## Run — alternatives (macOS / no systemd)

### `nohup` (quick & dirty)

```bash
source venv/bin/activate
nohup python bot.py > bridge.log 2>&1 &
disown
```

Stop: `pkill -f 'python bot.py'`

### `tmux` / `screen` (session-based)

```bash
tmux new -s bridge
source venv/bin/activate
python bot.py
# Detach: Ctrl-b then d
# Reattach: tmux attach -t bridge
```

### macOS `launchd`

Create `~/Library/LaunchAgents/com.example.claude-discord-bridge.plist` with the content below (substitute paths), then `launchctl load -w ~/Library/LaunchAgents/com.example.claude-discord-bridge.plist`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.claude-discord-bridge</string>
  <key>WorkingDirectory</key><string>/Users/you/code/claude-discord-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/code/claude-discord-bridge/venv/bin/python</string>
    <string>/Users/you/code/claude-discord-bridge/bot.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/claude-discord-bridge.log</string>
  <key>StandardErrorPath</key><string>/tmp/claude-discord-bridge.err</string>
</dict></plist>
```

---

## Usage

DM your bot anything. Plain text is sent to Claude Code as a prompt. Bot-config commands (below) are intercepted by the bridge and don't reach Claude.

### Slash commands (rich autocomplete)

Type `/` in the DM and Discord shows them with descriptions and dropdowns:

| Command | Effect |
| --- | --- |
| `/new` | Start a fresh session — drops the saved `session_id` so the next prompt starts with no prior context. |
| `/auto on` / `/auto off` | Toggle `--permission-mode bypassPermissions`. Default: **on** (required for headless tool use; you can't answer permission prompts mid-DM). |
| `/model opus` / `sonnet` / `haiku` / `default` | Switch the model used for the next turn. |
| `/effort low/medium/high/xhigh/max/default` | Set the thinking-effort level. |
| `/tools on` / `/tools off` | Show/hide tool-call notifications inline. Default: **off** (clean prose). |
| `/status` | Print current session/model/mode state. |
| `/help` | Print the command reference. |

### Bang commands (text)

Same effect as the slash commands, useful when typing fast or when slash autocomplete misbehaves. `!new`, `!auto on`, `!model opus`, `!effort high`, `!tools on`, `!status`, `!help`.

### Claude Code's own slash commands

`/init`, `/review`, `/security-review`, `/clear`, `/compact`, `/skill-name`, etc. are passed through to Claude Code as prompts and handled by Claude Code itself. They behave the same as in interactive mode.

### Things you can ask

```
read README.md and tell me what's missing
run pytest in this directory and summarize failures
search the web for the latest discord.py version and update requirements.txt
git status
why did the last build fail? check the logs in /var/log/build/
```

Tool-using prompts (file edits, shell commands, web searches) work because `--permission-mode bypassPermissions` is on by default. Turn it off with `/auto off` if you want manual permission gates (note: in headless mode there's no way to answer prompts, so most tool use will be denied with auto off).

---

## Troubleshooting

### Bot crashes with `PrivilegedIntentsRequired`

You forgot step 3 (enable **MESSAGE CONTENT INTENT**). Re-enable it on the Discord developer portal Bot page → Save. Restart the service.

### Slash commands don't appear in DMs

Check the bot's startup logs for `synced N global slash commands`. If sync fails (`Forbidden 50001`), the bot was invited without the `applications.commands` scope.

Re-authorize with both scopes:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=67584&scope=bot+applications.commands
```

Pick your existing scratch server and click **Authorize** again. This expands the bot's scope without re-adding it. Then force-quit Discord and reopen — global commands take a few seconds to a few minutes to appear in the autocomplete cache.

### Bot connects then disconnects in a loop

You're running two processes with the same bot token. Discord allows only **one gateway connection per token** at a time. Stop the other process, or generate a separate bot token for the bridge.

### `FATAL: claude binary not found`

Either install Claude Code (`npm install -g @anthropic-ai/claude-code`) or set `BRIDGE_CLAUDE_BIN` in `.env` to the absolute path.

### `FATAL: set BRIDGE_DISCORD_BOT_TOKEN` / `set BRIDGE_ALLOWED_USER_ID`

Check `.env` exists and has both values filled in. Did you copy `.env.example` to `.env`?

### Messages from random people work / don't get blocked

They shouldn't — every DM is gated by `BRIDGE_ALLOWED_USER_ID`. If you see foreign DMs being processed, double-check that env var matches your actual numeric user ID (Developer Mode → Copy User ID).

### Long replies get cut off

Discord caps single messages at 2000 characters. The bridge auto-splits at paragraph → newline → word boundaries, so long Claude responses come in as multiple sequential messages.

### Bot is silent for a long time then replies

Headless `claude -p` spawns a fresh process per turn (~1–2s overhead) and Claude itself can take a while to reply on complex prompts (especially with `xhigh` effort). Discord's "is typing…" indicator stays active throughout. There's no hung state — be patient on heavy tasks, or use `/effort low` for quick chats.

### `permission_mode: bypassPermissions` makes me nervous

It should — your bot has shell-level access to your machine via Claude Code's tools. Mitigations:

- Keep the bot in a **private** server with no other members.
- Make sure `BRIDGE_ALLOWED_USER_ID` is exactly your user ID (drops everyone else).
- Treat your `BRIDGE_DISCORD_BOT_TOKEN` like an SSH key — anyone who has it can connect a process as your bot, but they can't directly DM as you, so the user-ID gate still holds. Rotate via **Reset Token** if it leaks.
- Use `/auto off` if you want default Claude Code permission gates (most tool use will be denied in headless mode, but it's the safest setting).

---

## Security notes

The threat model is: **the bot has the same level of access to your machine as Claude Code does.** That includes file I/O, shell commands, and network access in your home directory. Things to think about:

- **Discord token** ≠ keys to your machine. Stealing the token lets someone impersonate the bot, but they still can't bypass the `BRIDGE_ALLOWED_USER_ID` gate to issue commands. They can crash/spam, not RCE.
- **Your Discord user ID + a way to message you** *would* be a problem, but only if they could compromise your Discord account itself. Use 2FA on your Discord account.
- **Your `.env` file** holds the token. `chmod 600 .env`. Don't commit it (`.gitignore` is set up to exclude it).
- **The host machine** trusts whoever can DM Claude. Don't run the bridge on a multi-tenant box where others could plausibly pivot through.
- **Audit log**: `journalctl --user -u claude-discord-bridge` keeps a full record of every prompt and response. Useful for forensics if you ever suspect a problem.

For responsible disclosure of security issues, see [SECURITY.md](SECURITY.md) — please email rather than file a public issue.

---

## Development & testing

The bridge ships with a comprehensive test suite (~120 tests, 96% line coverage) that runs in seconds without needing a real Discord token, a working `claude` install, or any network access.

```bash
# One-time: install dev dependencies inside the venv
pip install pytest pytest-asyncio pytest-cov ruff

# Run everything
pytest --cov=bot --cov-report=term-missing

# Lint
ruff check bot.py tests/
```

What the suite covers:

- **Pure functions** — message chunking, tool-call formatting, model→context-window mapping, claude argv construction, settings.json parsing, status/context block rendering.
- **Authorization** — every slash command and the `on_message` handler reject unauthorized users, bots, and guild messages without mutating state.
- **Pre-lock interception** — `/stop`, `/exit`, `/quit` all work even when the per-user lock is held by a running turn (`/exit` + `/quit` are intentional no-ops; `/stop` terminates the live `claude` subprocess). These are the bot's safety boundary and have dedicated tests with deadlock-detection timeouts.
- **Subprocess streaming** — every `stream-json` event type, the 16 MB `StreamReader` buffer trap (with oversize-event drain + recovery), invalid JSON skip, terminate→kill fallback, stopped-vs-crashed post-mortem distinction, stderr-read timeout.
- **End-to-end** — DM → lock → `_handle` → `run_claude_turn` (mocked subprocess) round-trip with state assertions.

CI runs on every push and PR against Python 3.10 / 3.11 / 3.12 with a 90% coverage gate. PRs cannot merge with a red build.

---

## Contributing

PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow. Short version: open an issue first for non-trivial changes, write a test that fails without your fix, run `pytest` + `ruff` locally, then PR with a clear description.

---

## License

MIT — see [LICENSE](./LICENSE).

## Unified Multi-Agent Bridge

`unified_bridge.py` can run multiple Discord bot accounts from one LaunchAgent/process while sharing the same attachment and upload code. This supports a Claude-backed bot and a Codex-backed bot without duplicating bridge infrastructure.

```text
Discord bot clients -> unified_bridge.py -> Claude or Codex runner -> Discord reply
```

Configure with `BRIDGE_AGENTS` and per-agent environment prefixes:

```env
BRIDGE_AGENTS=claude_agent,codex_agent
BRIDGE_ATTACHMENT_DIR=/Users/you/.discord-agent-bridge/attachments

CLAUDE_AGENT_TOKEN=***
CLAUDE_AGENT_BACKEND=claude
CLAUDE_AGENT_ALLOWED_USER_ID=123456789012345678
CLAUDE_AGENT_WORKDIR=/Users/you/Projects
CLAUDE_AGENT_CLAUDE_BIN=/Users/you/.local/bin/claude
CLAUDE_AGENT_ALIASES=claudette,claude

CODEX_AGENT_TOKEN=***
CODEX_AGENT_BACKEND=codex
CODEX_AGENT_ALLOWED_USER_ID=123456789012345678
CODEX_AGENT_WORKDIR=/Users/you/Projects
CODEX_AGENT_CODEX_BIN=/Applications/Codex.app/Contents/Resources/codex
CODEX_AGENT_DEFAULT_SANDBOX=workspace-write
CODEX_AGENT_ALIASES=luna,codex
```

Inbound attachments are downloaded once through shared code. Codex receives image attachments with `--image`; non-image files are listed in the prompt by absolute path. Claude receives attachment paths in the prompt and the attachment directory via `--add-dir`.

Outbound files work for both backends by printing either marker on its own line:

```text
FILE:/absolute/path/to/file.md
MEDIA:/absolute/path/to/image.png
```

For shared Discord channels, set `<PREFIX>_ALLOWED_CHANNEL_IDS`, `<PREFIX>_ALLOWED_BOT_USER_IDS`, and `<PREFIX>_ALIASES`. Prefix messages like `luna: ...` or `claudette: ...` so only the targeted bot responds. DMs still route by allowlisted user ID.

Manual run:

```bash
source venv/bin/activate
python unified_bridge.py
```

macOS LaunchAgent example: `com.neetware.discord-agent-bridge.plist` + `run-launchagent.sh`.
