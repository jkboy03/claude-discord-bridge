"""claude-discord bridge bot.

DMs from the allowlisted Discord user are run through `claude -p` (Claude
Code's headless mode). Output comes back as structured JSON events with no
TUI chrome — assistant text is posted to Discord as plain markdown.

Bot-level commands (use Discord's slash UI, or `!cmd` text form):
  /new          start a fresh session (drop saved session_id)
  /auto on/off  toggle bypassPermissions (default: on)
  /model <name> switch model for next turn (e.g. opus, sonnet, haiku)
  /effort <lvl> low/medium/high/xhigh/max
  /tools on/off show tool-call notifications inline
  /status       show session/model/permission state
  /help         this message

Anything else is sent to claude as a prompt. Claude Code's own slash
commands (like /init, /review, /security-review) pass through as prompts.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

TOKEN = os.environ.get("BRIDGE_DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    print("FATAL: set BRIDGE_DISCORD_BOT_TOKEN in claude-discord/.env", file=sys.stderr)
    sys.exit(1)
USER_ID_RAW = os.environ.get("BRIDGE_ALLOWED_USER_ID", "").strip()
if not USER_ID_RAW.isdigit():
    print("FATAL: set BRIDGE_ALLOWED_USER_ID in claude-discord/.env", file=sys.stderr)
    sys.exit(1)
ALLOWED_USER_ID = int(USER_ID_RAW)

WORKDIR = (os.environ.get("BRIDGE_WORKDIR") or str(Path.home())).rstrip("/")
CLAUDE_BIN = os.environ.get("BRIDGE_CLAUDE_BIN") or shutil.which("claude") or ""
if not CLAUDE_BIN or not Path(CLAUDE_BIN).exists():
    print(
        "FATAL: claude binary not found. Set BRIDGE_CLAUDE_BIN in .env "
        "or install Claude Code and ensure `claude` is on PATH.",
        file=sys.stderr,
    )
    sys.exit(1)
DISCORD_LIMIT = 1900  # leave headroom under 2000-char limit


class SessionState:
    """Per-bot state (single allowed user, so one global instance)."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.model: str | None = None  # None = use default
        self.effort: str | None = None  # None = use default
        self.permission_mode: str = "bypassPermissions"  # auto-mode by default
        self.show_tools: bool = False  # toggle to show tool-call notifications

    def claude_args(self, prompt: str) -> list[str]:
        args = [
            CLAUDE_BIN,
            "--print",
            "--output-format", "stream-json",
            "--verbose",  # required for stream-json + print
            "--permission-mode", self.permission_mode,
        ]
        if self.session_id:
            args += ["--resume", self.session_id]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        args.append(prompt)
        return args


state = SessionState()

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
_lock = asyncio.Lock()


def _is_authorized(user_id: int) -> bool:
    return user_id == ALLOWED_USER_ID


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("not authorized", ephemeral=True)


HELP_TEXT = (
    "**claude-discord bridge** — your DMs are sent to Claude Code via headless mode.\n"
    "```\n"
    "Plain text       Sent as a prompt. Claude Code's own /init, /review,\n"
    "                 /security-review etc. pass through as prompts.\n"
    "/new             Start a fresh session (forget prior context).\n"
    "/auto on|off     Toggle auto-mode (bypassPermissions). Default: on.\n"
    "/model <name>    Switch model: opus / sonnet / haiku / <full-name>.\n"
    "/effort <level>  low / medium / high / xhigh / max.\n"
    "/tools on|off    Show tool-call notifications. Default: off.\n"
    "/status          Show current session/model/mode.\n"
    "/help            This message.\n"
    "\n"
    "Tip: use Discord's slash-command UI for autocomplete + parameter\n"
    "hints. The legacy `!cmd` text form still works if you prefer typing.\n"
    "```"
)


def split_at_boundary(text: str, max_size: int = DISCORD_LIMIT) -> list[str]:
    """Split text into <=max_size chunks, preferring paragraph > line > word boundaries."""
    if not text or not text.strip():
        return []
    if len(text) <= max_size:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_size:
        window = remaining[:max_size]
        cut = window.rfind("\n\n")
        if cut < max_size // 4:
            cut = -1
        if cut < 0:
            cut = window.rfind("\n")
            if cut < max_size // 4:
                cut = -1
        if cut < 0:
            cut = window.rfind(" ")
            if cut < max_size // 4:
                cut = -1
        if cut < 0:
            cut = max_size

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n ")
    if remaining.strip():
        chunks.append(remaining)
    return [c for c in chunks if c.strip()]


async def send_text(channel, text: str) -> None:
    """Send text as plain Discord markdown, paragraph-aware chunked."""
    chunks = split_at_boundary(text)
    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(0.4)
        await channel.send(chunk)


def format_tool_call(name: str, inp: dict) -> str:
    """One-line summary of a tool call for an inline notification."""
    if name == "Bash":
        cmd = inp.get("command", "").strip().splitlines()[0][:80]
        return f"$ {cmd}"
    if name in ("Read", "Edit", "Write"):
        path = inp.get("file_path", inp.get("path", ""))
        return f"{name}({path})"
    if name in ("Glob", "Grep"):
        pat = inp.get("pattern", "")
        return f"{name}({pat})"
    if name == "WebFetch":
        return f"WebFetch({inp.get('url', '')[:60]})"
    return f"{name}(...)"


async def run_claude_turn(channel, prompt: str) -> None:
    """Run one Claude turn, stream assistant text + tool notifications to Discord."""
    args = state.claude_args(prompt)
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=WORKDIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    last_session_id: str | None = None
    error_payload: str | None = None

    async with channel.typing():
        assert proc.stdout is not None
        async for line_bytes in proc.stdout:
            try:
                event = json.loads(line_bytes.decode().strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            etype = event.get("type")

            # Track session id for resume on next turn.
            if etype == "system" and event.get("subtype") == "init":
                last_session_id = event.get("session_id") or last_session_id

            # Assistant turn complete (one per turn — tools cause multiple turns).
            elif etype == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []) or []:
                    btype = block.get("type")
                    if btype == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            await send_text(channel, text)
                    elif btype == "tool_use" and state.show_tools:
                        name = block.get("name", "?")
                        inp = block.get("input", {}) or {}
                        await channel.send(f"_↳ {format_tool_call(name, inp)}_")

            # Final result event (also has session_id; use it as authoritative).
            elif etype == "result":
                last_session_id = event.get("session_id") or last_session_id
                if event.get("is_error"):
                    error_payload = event.get("result") or "(unknown error)"

    stderr_data = b""
    if proc.stderr is not None:
        try:
            stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
    await proc.wait()

    if last_session_id:
        state.session_id = last_session_id

    if proc.returncode != 0 and error_payload is None:
        tail = stderr_data.decode(errors="replace").strip().splitlines()[-3:]
        error_payload = "claude exited with code {} — {}".format(
            proc.returncode, " | ".join(tail) if tail else "(no stderr)",
        )
    if error_payload:
        await channel.send(f"_⚠ {error_payload}_")


@client.event
async def on_ready() -> None:
    print(f"[bridge] logged in as {client.user} (id={client.user.id})")
    print(f"[bridge] allowed user id: {ALLOWED_USER_ID}")
    print(f"[bridge] mode: claude -p (headless), workdir={WORKDIR}")
    try:
        synced = await tree.sync()
        print(f"[bridge] synced {len(synced)} global slash commands")
    except Exception as e:
        print(f"[bridge] slash command sync FAILED ({type(e).__name__}: {e}) — "
              "did you invite the bot with `applications.commands` scope?")


# ---------- Discord slash commands (registered globally; show in DMs) ----------
# These mirror the Claude Code config commands so typing `/` on phone/desktop
# pops them in Discord's autocomplete.

@tree.command(name="new", description="Start a fresh Claude session (drop prior context)")
async def slash_new(interaction: discord.Interaction):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    state.session_id = None
    await interaction.response.send_message("New session — next prompt starts fresh.", ephemeral=True)


@tree.command(name="auto", description="Toggle auto-mode (bypassPermissions)")
@app_commands.describe(mode="on or off")
@app_commands.choices(mode=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
])
async def slash_auto(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    if mode.value == "on":
        state.permission_mode = "bypassPermissions"
        await interaction.response.send_message("Auto mode ON.", ephemeral=True)
    else:
        state.permission_mode = "default"
        await interaction.response.send_message("Auto mode OFF.", ephemeral=True)


@tree.command(name="model", description="Switch the model used for the next turn")
@app_commands.describe(name="opus / sonnet / haiku, or a full model name")
@app_commands.choices(name=[
    app_commands.Choice(name="opus", value="opus"),
    app_commands.Choice(name="sonnet", value="sonnet"),
    app_commands.Choice(name="haiku", value="haiku"),
    app_commands.Choice(name="default", value=""),
])
async def slash_model(interaction: discord.Interaction, name: app_commands.Choice[str]):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    state.model = name.value or None
    await interaction.response.send_message(f"Model: {state.model or '(default)'}", ephemeral=True)


@tree.command(name="effort", description="Set thinking effort level")
@app_commands.choices(level=[
    app_commands.Choice(name="low", value="low"),
    app_commands.Choice(name="medium", value="medium"),
    app_commands.Choice(name="high", value="high"),
    app_commands.Choice(name="xhigh", value="xhigh"),
    app_commands.Choice(name="max", value="max"),
    app_commands.Choice(name="default", value=""),
])
async def slash_effort(interaction: discord.Interaction, level: app_commands.Choice[str]):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    state.effort = level.value or None
    await interaction.response.send_message(f"Effort: {state.effort or '(default)'}", ephemeral=True)


@tree.command(name="tools", description="Show or hide tool-call notifications")
@app_commands.choices(mode=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
])
async def slash_tools(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    state.show_tools = mode.value == "on"
    await interaction.response.send_message(
        f"Tool notifications: {'on' if state.show_tools else 'off'}.", ephemeral=True
    )


@tree.command(name="status", description="Show session, model, and mode state")
async def slash_status(interaction: discord.Interaction):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    await interaction.response.send_message(
        "```\n"
        f"session_id:      {state.session_id or '(none — fresh on next turn)'}\n"
        f"model:           {state.model or '(default)'}\n"
        f"effort:          {state.effort or '(default)'}\n"
        f"permission_mode: {state.permission_mode}\n"
        f"show_tools:      {state.show_tools}\n"
        "```",
        ephemeral=True,
    )


@tree.command(name="help", description="Show bridge command help")
async def slash_help(interaction: discord.Interaction):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return
    if message.author.id != ALLOWED_USER_ID:
        return

    content = message.content
    channel = message.channel

    async with _lock:
        try:
            await _handle(channel, content)
        except Exception as e:
            await channel.send(f"_bridge error: {type(e).__name__}: {e}_")
            raise


async def _handle(channel, content: str) -> None:
    """Handle a plain DM. Bot-config commands work via either `!cmd` or `/cmd`
    (typed as text — the rich slash-command UI handles the same logic via
    interactions). Unknown slash commands fall through to claude as prompts."""
    stripped = content.strip()
    if not stripped:
        return

    # Normalize bot-config commands: accept either `!new` or `/new` form.
    norm = stripped
    if norm.startswith("!"):
        norm = "/" + norm[1:]  # treat ! as / for matching
    head = norm.split(maxsplit=1)
    cmd = head[0].lower()
    arg = head[1].strip() if len(head) > 1 else ""

    if cmd in ("/help",):
        await channel.send(HELP_TEXT)
        return
    if cmd in ("/status",):
        await channel.send(
            "```\n"
            f"session_id:      {state.session_id or '(none — fresh on next turn)'}\n"
            f"model:           {state.model or '(default)'}\n"
            f"effort:          {state.effort or '(default)'}\n"
            f"permission_mode: {state.permission_mode}\n"
            f"show_tools:      {state.show_tools}\n"
            "```"
        )
        return
    if cmd in ("/new",):
        state.session_id = None
        await channel.send("_new session — next prompt starts fresh_")
        return
    if cmd in ("/auto",):
        if arg.lower() in ("", "on"):
            state.permission_mode = "bypassPermissions"
            await channel.send("_auto mode on_")
        elif arg.lower() == "off":
            state.permission_mode = "default"
            await channel.send("_auto mode off_")
        else:
            await channel.send("_usage: /auto on  |  /auto off_")
        return
    if cmd in ("/model",):
        state.model = arg or None
        await channel.send(f"_model: {state.model or '(default)'}_")
        return
    if cmd in ("/effort",):
        if arg and arg not in ("low", "medium", "high", "xhigh", "max"):
            await channel.send("_effort must be: low, medium, high, xhigh, max_")
            return
        state.effort = arg or None
        await channel.send(f"_effort: {state.effort or '(default)'}_")
        return
    if cmd in ("/tools",):
        if arg.lower() in ("", "on"):
            state.show_tools = True
            await channel.send("_tool notifications on_")
        elif arg.lower() == "off":
            state.show_tools = False
            await channel.send("_tool notifications off_")
        else:
            await channel.send("_usage: /tools on  |  /tools off_")
        return

    # Anything else (including Claude Code skill commands like /init, /review,
    # /security-review) goes to claude as a prompt.
    await run_claude_turn(channel, content)


def main() -> None:
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
