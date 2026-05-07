# Claude Discord Bridge

[![CI](https://github.com/jkboy03/claude-discord-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/jkboy03/claude-discord-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Use [Claude Code](https://claude.com/claude-code) from anywhere via Discord DMs. Send a message to your bot from your phone, your laptop, or any device with Discord — Claude Code runs on your home machine and replies in the same DM.

> **What this is:** a small Python service that wraps Claude Code's headless mode (`claude -p`) behind a Discord bot. Your DMs become Claude Code prompts. Claude's responses come back as plain Discord messages.
>
> **What this is not:** an Anthropic-API wrapper, a re-implementation of Claude Code, or a multi-tenant service. It runs on your own machine, uses your own Claude Code authentication (your Claude Max / Pro / Team subscription), and only responds to **one** Discord user (you).

### Two run modes

| Mode | Entry point | Use when |
|------|-------------|----------|
| **Single-bot** | [`bot.py`](./bot.py) | One Discord bot, one backend (Claude). Simplest setup. Most users start here. |
| **Unified multi-bot** | [`unified_bridge.py`](./unified_bridge.py) | Two or more bots in one process. Mix Claude + Codex backends. Optional manager-bot orchestration where a third bot drives the others in a shared channel. |

Both ship in this repo. Pick one based on your needs — they don't run side-by-side. Single-bot setup is documented first; the unified bridge has its own [top-level section](#unified-multi-bot-bridge) with a complete walkthrough.

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
16. [**Unified multi-bot bridge**](#unified-multi-bot-bridge) — multiple bots in one process, manager-bot orchestration
17. [License](#license)

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

## Unified multi-bot bridge

`unified_bridge.py` runs **multiple Discord bot accounts in one Python process**, optionally with **manager-bot orchestration** so a third bot can drive the workers in a shared channel.

It is the same repo's "advanced" entry point. If you only want one bot, use [`bot.py`](#table-of-contents) and stop reading. If you want two bots — say a Claude-backed coding assistant and a Codex-backed scripting assistant — keep reading.

### When to use it

- You want **two or more Discord bots** without running multiple processes/services.
- You want **one bot to be backed by Claude and another by Codex** (different models, different strengths).
- You're building an **agent-orchestration pattern** where a "manager" bot delegates to "worker" bots in a shared channel and you watch it happen live.

### Architecture

```
                                         ┌── claude --print ──── Anthropic
                                         │                       (your sub)
   ┌──────────┐  Discord WS ──┐ ┌───────────────┐
   │ Discord  │ ──────────── ▶│ unified_bridge ├──── codex exec ──── OpenAI
   │ gateway  │ ◀────────────┐│   (one PID)    │                   (your sub)
   └──────────┘               └───────────────┘
        ▲                       │ shared:
        │                       │  • attachment store
   one process holds            │  • per-channel session keys
   N gateway connections        │  • bot-to-bot __stop__ routing
   (one per BRIDGE_AGENTS)      │  • outbound FILE:/MEDIA: uploads
```

For each authorized inbound message:

1. The bridge checks the agent's authorization rules (DM allowlist, channel allowlist, bot allowlist, bot-only channel mode).
2. It strips the alias prefix or `@mention` for shared-channel messages.
3. It spawns the configured backend subprocess (`claude --print` or `codex exec`) with the right arguments — including `--resume <session_id>` keyed by the Discord channel, so each channel keeps its own context.
4. It streams the backend's stdout (NDJSON) and posts assistant text back to the Discord channel.

### Setup walkthrough

**Step 1. Decide your topology.**

The simplest case is two bots both reachable in your DMs, each backed by a different LLM. The richer case is "manager + workers in a shared channel" (covered in [Manager-bot orchestration](#manager-bot-orchestration) below). Either way, the install steps below are the same.

**Step 2. Create one Discord application per bot.**

For each bot you want, repeat these steps at <https://discord.com/developers/applications>:

1. **New Application** → name it (e.g. "Worker A"). Click **Create**.
2. Left sidebar → **Bot** → **Reset Token**. Copy the token. *You only see it once.*
3. Same page → **Privileged Gateway Intents** → enable **Message Content Intent**. Free for personal-use bots in fewer than 100 servers, no verification required. Save.
4. Copy the **Application ID** (left sidebar → **General Information**) — that's the bot's Discord user ID. You'll need it later for cross-bot allowlists.
5. Build the OAuth invite URL:
   `https://discord.com/oauth2/authorize?client_id=<APP_ID>&permissions=274877990912&scope=bot+applications.commands`
   (`274877990912` = Send Messages + Read Message History + Use Slash Commands. Add more if you need them.)
6. Open the URL, pick the server you want, authorize.

> **Personal scratch server tip.** If you don't already have a private Discord server for this, create one (Discord client → "+" → Create My Own → For me and my friends). It takes 30 seconds and gives you a private space to invite all your bots to without polluting any real server.

**Step 3. Install the bridge.**

```bash
git clone https://github.com/jkboy03/claude-discord-bridge.git
cd claude-discord-bridge
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Make sure `claude` (and `codex`, if you're running a Codex bot) are installed and authenticated:

```bash
claude --version
claude          # interactive — log in with /login if you haven't
codex --version # if you have a codex bot
```

**Step 4. Write your env file.**

Copy `.env.example` and fill in. We **strongly recommend** keeping the real env file outside the repo — for example at `~/.config/unified-discord-bridge/env` — and feeding it via a systemd `EnvironmentFile=` directive, so secrets never live in your git working tree.

```bash
mkdir -p ~/.config/unified-discord-bridge
cp .env.example ~/.config/unified-discord-bridge/env
chmod 600 ~/.config/unified-discord-bridge/env
${EDITOR:-nano} ~/.config/unified-discord-bridge/env
```

Minimum config for two bots:

```env
BRIDGE_AGENTS=worker_a,worker_b
BRIDGE_ATTACHMENT_DIR=/home/you/.cache/discord-agent-bridge/attachments

WORKER_A_TOKEN=<paste from step 2.2>
WORKER_A_BACKEND=claude
WORKER_A_ALLOWED_USER_ID=<your Discord user ID>
WORKER_A_DEFAULT_MODEL=sonnet

WORKER_B_TOKEN=<paste from step 2.2 of the other app>
WORKER_B_BACKEND=codex
WORKER_B_ALLOWED_USER_ID=<your Discord user ID>
WORKER_B_DEFAULT_MODEL=gpt-5.5
```

See `.env.example` and the [Env var reference](#env-var-reference) below for every supported flag.

**Step 5. Run as a systemd user service (Linux).**

A reference unit is at [`examples/unified-discord-bridge.service`](./examples/unified-discord-bridge.service). Edit the paths to match your install:

```bash
cp examples/unified-discord-bridge.service ~/.config/systemd/user/
${EDITOR:-nano} ~/.config/systemd/user/unified-discord-bridge.service
# update WorkingDirectory, EnvironmentFile, and ExecStart paths

systemctl --user daemon-reload
systemctl --user enable --now unified-discord-bridge.service
journalctl --user -u unified-discord-bridge.service -f
```

You should see two `[<agent>] logged in as ...` lines — one per bot.

**Step 6. (Optional) Foreground debug launcher.**

`examples/run-unified-bridge.sh` runs the bridge in the foreground, automatically stopping the systemd unit if it's active and restarting it on Ctrl+C. Useful for trying a code change without rolling a new release.

```bash
cp examples/run-unified-bridge.sh ~/bin/   # or any PATH dir
chmod +x ~/bin/run-unified-bridge.sh

# Configurable via env vars (or edit defaults inline):
UNIFIED_BRIDGE_ENV=~/.config/unified-discord-bridge/env \
  UNIFIED_BRIDGE_DIR=~/claude-discord-bridge \
  ~/bin/run-unified-bridge.sh
```

### Manager-bot orchestration

The bridge can be configured so a **third bot** orchestrates the worker bots in a shared channel. The classic shape:

```
            ┌──────────────────────────────────────────┐
            │  #orchestration channel                  │
            │                                          │
   You ────▶│  @Manager: refactor the auth module      │
   (DM)     │                                          │
            │  Manager → "worker_a: read auth/*.py"    │
            │  Worker A → "<@manager> here are the     │
            │              files and what they do…"    │
            │  Manager → "worker_b: write the patch"   │
            │  Worker B → "<@manager> done, see…"      │
            │  Manager → DM you: "done: refactor       │
            │                     complete, summary…"  │
            └──────────────────────────────────────────┘
```

You only DM the manager. Workers ignore your DMs. In the orchestration channel, you can either stay silent (manager-only workspace) or speak — depending on the manager's setup. Workers won't talk to you in the channel either way; they only respond to the manager.

#### The four primitives

The bridge ships four authorization/isolation primitives that make this pattern work:

1. **`<PREFIX>_BOT_ONLY_CHANNEL_IDS`** — channels where the *human* user is ignored entirely. Only senders in `<PREFIX>_ALLOWED_BOT_USER_IDS` (i.e. the manager bot) are processed. The orchestration channel becomes the manager's private workspace as far as the worker is concerned.

2. **`<PREFIX>_ACCEPT_DMS=false`** — worker bot ignores DMs from the user entirely. Combined with primitive (1), you can only ever reach the worker by going through the manager.

3. **Per-channel session keys.** Each Discord channel ID gets its own backend session (Codex thread / Claude `session_id`). A user DM with the worker (if DMs are enabled) and a manager-driven conversation in the orchestration channel never share state. This is automatic — no config needed.

4. **`__stop__` from authorized bots.** Any sender in `<PREFIX>_ALLOWED_BOT_USER_IDS` can post `<alias>: __stop__` (or `<@worker_id> __stop__`) and the bridge will abort the running backend turn before acquiring the per-agent lock. The worker's session is preserved — the manager can immediately send a redirect or a follow-up. This is the bot-to-bot equivalent of the human's `/stop` slash command.

Auto-mention prefix: when a worker replies in a `BOT_ONLY_CHANNEL_IDS` channel, the bridge automatically prepends `<@manager_id>` to the first chunk of the reply. This guarantees Discord delivers the full message content to the manager via the @mention path even if the manager's bot doesn't have Message Content Intent.

Worker final-report behavior: in bot-only orchestration channels, streamed assistant messages are visible as progress, but the bridge defers the manager @mention until the final assistant message of the backend turn. If a worker needs to return more detail than fits in one Discord message, have it write the full report to a markdown file in its project or notes directory and keep the final in-channel reply short with a pointer such as `Full detail: /path/to/report.md`. This keeps the manager-triggering mention in one concise message instead of spreading the actionable summary across chunks.

#### Setup for manager + two workers

Assume:
- Manager bot user ID: `<MANAGER_ID>`
- Worker A and Worker B Discord apps already created (Step 2 above)
- Orchestration channel ID: `<CHANNEL_ID>` (right-click channel in Discord with Developer Mode on → Copy Channel ID)
- All three bots invited to the server and present in `<CHANNEL_ID>`

Worker config in your env file:

```env
BRIDGE_AGENTS=worker_a,worker_b
BRIDGE_ATTACHMENT_DIR=/home/you/.cache/discord-agent-bridge/attachments

WORKER_A_TOKEN=...
WORKER_A_BACKEND=claude
WORKER_A_ALLOWED_USER_ID=<your Discord user ID>
WORKER_A_ACCEPT_DMS=false
WORKER_A_BOT_ONLY_CHANNEL_IDS=<CHANNEL_ID>
WORKER_A_ALLOWED_BOT_USER_IDS=<MANAGER_ID>
WORKER_A_ALIASES=worker_a

WORKER_B_TOKEN=...
WORKER_B_BACKEND=codex
WORKER_B_ALLOWED_USER_ID=<your Discord user ID>
WORKER_B_ACCEPT_DMS=false
WORKER_B_BOT_ONLY_CHANNEL_IDS=<CHANNEL_ID>
WORKER_B_ALLOWED_BOT_USER_IDS=<MANAGER_ID>
WORKER_B_ALIASES=worker_b
```

The **manager bot is run by something other than this repo** — it is your own orchestrator. Common shapes:

- A separate process running an LLM agent loop (LangGraph, your own Python, an off-the-shelf agent framework) that authenticates as a Discord bot and posts to the channel.
- An [OpenClaw](https://github.com/anthropics/openclaw) or similar gateway agent wired to the Discord channel.
- Another instance of `unified_bridge.py` configured with `BRIDGE_AGENTS=manager` and `manager`'s backend pointed at whatever LLM you want — though usually you want richer tooling than the bridge itself provides for the orchestration brain.

The manager addresses workers by alias prefix at line start:

```
worker_a: read foo.py and summarize the design

worker_b: implement worker_a's plan in src/auth.py

worker_a: __stop__       # abort whatever worker_a is doing
```

Workers reply in the same channel with `<@manager_id>` prepended (see auto-mention prefix above). The manager reads those replies and decides what to do next.

#### What the bridge does NOT enforce

The bridge provides authorization and the `__stop__` primitive. It does **not** enforce orchestration discipline. **The manager's prompt** is responsible for:

- A step budget (e.g. max 10 worker hops per top-level user request).
- A "done" signal (e.g. a final reply that starts with `done:`).
- A per-step orchestration timeout (e.g. issue `__stop__` if a worker doesn't complete a delegated step in your policy window). The bridge also has subprocess idle/wall-time watchdogs, but those are backend safety rails, not a substitute for manager planning.
- Failure handling (retry once, switch worker, or escalate).
- Avoiding bot-to-bot loops (don't react to your own posts; always serial across workers).

These are prompt-engineering concerns, not bridge concerns.

### Slash command reference

When the bridge is running, both worker bots register Discord slash commands. Type `/` in any channel where the bot is present to see them with autocomplete.

| Command | On both | On Codex bots | On Claude bots | Effect |
|---------|:-------:|:-------------:|:--------------:|--------|
| `/new` | ✅ | | | Drop the saved session/thread for *this channel*. Next prompt starts fresh. |
| `/stop` | ✅ | | | Abort the running backend turn. Session is preserved. |
| `/status` | ✅ | | | Show full bot state for this channel — model, effort, session ID, mode, context %, running. Resolves `(default)` to actual configured value from `~/.codex/config.toml` or `~/.claude/settings.json`. |
| `/help` | ✅ | | | Compact command reference. |
| `/model <name>` | ✅ | | | Pick the backend model from a dropdown. Codex options: `gpt-5.5`, `gpt-5.4`, `gpt-5.3`, `default`. Claude options: `opus`, `sonnet`, `haiku`, `default`. |
| `/effort <level>` | ✅ | | | Reasoning effort. Codex: `low`/`medium`/`high`/`xhigh`/`default`. Claude adds `max`. |
| `/tools <on|off>` | ✅ | | | Toggle inline tool-call notifications. |
| `/context` | ✅ | | | Detailed token/usage block from the last turn, with model-aware context-window % for Claude. |
| `/goal [text|clear]` | | ✅ | | Set, show, or clear a sticky goal that's prefixed onto every prompt. |
| `/sandbox <mode>` | | ✅ | | Codex sandbox mode: `read-only`, `workspace-write`, `danger-full-access`. |
| `/search <on|off>` | | ✅ | | Toggle Codex web search. |
| `/review [text]` | | ✅ | | Run `codex exec review` (optional prompt). |
| `/auto <on|off>` | | | ✅ | Toggle Claude `--permission-mode bypassPermissions` (auto-approve tool use). |

Models, efforts, and modes are *also* settable via the `<PREFIX>_DEFAULT_MODEL` etc. env vars (see below) for cases that need persistence across restarts. Slash commands are runtime overrides for the current process.

### Env var reference

#### Top-level (whole bridge)

| Variable | Required | Description |
|----------|----------|-------------|
| `BRIDGE_AGENTS` | ✅ | Comma- or space-separated list of agent names. Each name becomes a `<PREFIX>_*` block. |
| `BRIDGE_ATTACHMENT_DIR` | | Where Discord-uploaded attachments are saved before workers read them. Default `~/.discord-agent-bridge/attachments`. |

#### Per-agent (replace `<PREFIX>` with the agent name uppercased)

| Variable | Required | Backend | Description |
|----------|----------|---------|-------------|
| `<PREFIX>_TOKEN` | ✅ | both | Discord bot token. Falls back to `<PREFIX>_DISCORD_BOT_TOKEN`. |
| `<PREFIX>_BACKEND` | ✅ | both | `codex` or `claude`. |
| `<PREFIX>_ALLOWED_USER_ID` | ✅ | both | Your numeric Discord user ID. |
| `<PREFIX>_WORKDIR` | | both | cwd for the backend subprocess. Default `$HOME`. |
| `<PREFIX>_ATTACHMENT_DIR` | | both | Override `BRIDGE_ATTACHMENT_DIR` for this agent. |
| `<PREFIX>_CODEX_BIN` | required if `codex` not on PATH | codex | Absolute path to the `codex` binary. |
| `<PREFIX>_CLAUDE_BIN` | required if `claude` not on PATH | claude | Absolute path to the `claude` binary. |
| `<PREFIX>_DEFAULT_MODEL` | | both | Initial model. Override at runtime with `/model`. |
| `<PREFIX>_DEFAULT_EFFORT` | | both | Initial effort level. Override with `/effort`. |
| `<PREFIX>_DEFAULT_SANDBOX` | | codex | `read-only`/`workspace-write`/`danger-full-access`. Default `workspace-write`. |
| `<PREFIX>_DEFAULT_SEARCH` | | codex | `on`/`off`. Default `off`. |
| `<PREFIX>_CLAUDE_PERMISSION_MODE` | | claude | `bypassPermissions` or `default`. Default `bypassPermissions` (auto-mode on). |
| `<PREFIX>_ALIASES` | | both | Names this bot answers to in shared channels. Comma-separated. Default: agent name. |
| `<PREFIX>_ALLOWED_CHANNEL_IDS` | | both | Channel IDs where you AND authorized bots may speak. |
| `<PREFIX>_ALLOWED_BOT_USER_IDS` | | both | Other bots whose messages this agent honors in allowed channels. |
| `<PREFIX>_BOT_ONLY_CHANNEL_IDS` | | both | Channels where the human user is IGNORED — only `ALLOWED_BOT_USER_IDS` senders are processed. |
| `<PREFIX>_ACCEPT_DMS` | | both | `true`/`false`. Default `true`. Set `false` so a worker only responds to its manager. |
| `<PREFIX>_IDLE_TIMEOUT_SECONDS` | | both | Max stdout silence from the backend subprocess before the bridge terminates it and sends a final warning. Default `600`. Set `0` to disable. |
| `<PREFIX>_TURN_TIMEOUT_SECONDS` | | both | Optional total wall-time cap for one backend turn. Default `0` (disabled). On timeout the bridge terminates the subprocess and sends a final warning. |

### Operational details

**Attachments.** Discord uploads are downloaded to `BRIDGE_ATTACHMENT_DIR/<agent>/<message_id>/`. Codex receives images via `--image`; non-image files are listed in the prompt by absolute path. Claude receives all attachment paths in the prompt and the attachment directory via `--add-dir`.

**Outbound files.** Either backend can have a worker print `FILE:/abs/path/file.md` or `MEDIA:/abs/path/image.png` on its own line. The bridge picks those up and uploads the file to the same Discord channel.

**Per-channel sessions.** Each Discord channel keeps a separate backend session (Codex thread / Claude `session_id`). DM and channel conversations don't share context. `/new` only resets the current channel. Across orchestrations or across topics, you get clean state.

**Timeouts and stop behavior.** `/stop` and bot-to-bot `__stop__` terminate the running backend subprocess and preserve the saved session/thread ID. The idle watchdog does the same automatically when a backend stops emitting stdout for `<PREFIX>_IDLE_TIMEOUT_SECONDS`; if a final assistant message was pending, the bridge flushes it with the manager @mention and appends a timeout warning. If there was no pending assistant text, the warning itself is sent with the manager @mention. If `<PREFIX>_TURN_TIMEOUT_SECONDS` is set, the same behavior applies when the whole turn exceeds that wall-clock cap.

**Logging.** When run via the systemd unit, all stdout/stderr goes to journald. Tail with:
```bash
journalctl --user -u unified-discord-bridge.service -f
```

**Rate limits.** The bridge paces multi-chunk replies at 0.4s per chunk. With 2-3 bots in a channel and serial orchestration, you stay well under Discord's 5 messages/sec cap.

### Troubleshooting (unified bridge)

**Slash command says "you are not authorized."**
This is from your *manager*'s side (e.g. an OpenClaw agent), not the bridge. The bridge gates by `interaction.user.id == ALLOWED_USER_ID`, which should always pass for you. If you see this from the manager, configure its own user allowlist. For OpenClaw specifically: `openclaw config set 'channels.discord.allowFrom' '["<your-user-id>"]'` then restart its gateway.

**Manager bot doesn't see worker replies.**
Either (a) Message Content Intent isn't enabled on the manager bot's Discord app, or (b) the auto-mention prefix isn't reaching it. Auto-mention requires `<MANAGER_ID>` to be in `<WORKER_PREFIX>_ALLOWED_BOT_USER_IDS` AND the channel to be in `<WORKER_PREFIX>_BOT_ONLY_CHANNEL_IDS`. Verify both, then have the manager poll `openclaw message read` (or whatever read API your manager uses) as a fallback.

**Worker responds in DM instead of channel.**
The manager addressed the worker via `--target user:<id>` instead of `--target channel:<id>`. Fix the manager's prompt or tool config.

**`/model` dropdown doesn't include my model.**
The dropdown is hard-coded to common choices. For others, set `<PREFIX>_DEFAULT_MODEL` in the env file and restart the bridge. Discord allows up to 25 choices per slash command parameter, so the dropdown can be extended in code if you want a richer menu.

**Manager bot says "I don't have a session named worker_a" or similar.**
The manager is treating the worker as if it were a local agent/session in its own runtime. The worker is a Discord bot reachable only via Discord messages; the manager needs to use a "send Discord message" tool, not a "delegate to subagent" tool. Update the manager's instructions.

**Two bots responded to one message.**
Check `<PREFIX>_ALIASES` for overlap. Aliases should be unique per bot. Match is case-insensitive at line start.

**Session collision between DM and channel.**
This is what per-channel sessions prevent. If you see it anyway, you're running an older build of the bridge — pull the latest.

### Differences from `bot.py`

| | `bot.py` | `unified_bridge.py` |
|--|----------|---------------------|
| Bots per process | 1 | many (BRIDGE_AGENTS list) |
| Backends | claude only | claude + codex (mix-and-match per agent) |
| Manager-bot orchestration | no | yes (BOT_ONLY channels, ACCEPT_DMS, `__stop__`) |
| Per-channel sessions | no — single session per process | yes — keyed by Discord channel ID |
| Slash command surface | full | full + `/sandbox`, `/search`, `/goal`, `/review`, `/auto` (per backend) |
| Auto-mention manager prefix | no | yes (in BOT_ONLY channels) |
| Outbound `FILE:`/`MEDIA:` uploads | no | yes |
| Attachment handling | basic | shared attachment store across bots |
| Code complexity | ~700 lines | ~1170 lines |

`bot.py` is intentionally kept as the simple, single-bot entry point. It is not a deprecated path. New users who only need one bot should start there.
