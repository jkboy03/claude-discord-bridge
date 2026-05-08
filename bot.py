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
# claude -p --output-format stream-json emits one JSON object per line. Tool
# results (large Read outputs, WebFetch payloads, etc.) routinely blow past
# asyncio's default 64KB StreamReader buffer, which raises
# `ValueError: Separator is not found, and chunk exceed the limit` and kills
# the whole turn. 16 MB covers anything Claude Code reasonably emits.
STREAM_BUFFER_LIMIT = 16 * 1024 * 1024


class SessionState:
    """Per-bot state (single allowed user, so one global instance)."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.model: str | None = None  # None = use default
        self.effort: str | None = None  # None = use default
        self.permission_mode: str = "bypassPermissions"  # auto-mode by default
        self.show_tools: bool = False  # toggle to show tool-call notifications
        # Context-window telemetry: snapshot of the most recent assistant
        # message's `usage` payload (input + cache tokens = effective context).
        self.last_usage: dict | None = None
        self.last_active_model: str | None = None  # actual model from init event
        # Live subprocess so /stop can interrupt mid-turn from outside the lock.
        self.current_proc: asyncio.subprocess.Process | None = None

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


def _read_claude_defaults() -> tuple[str | None, str | None]:
    """Read ~/.claude/settings.json for the user's configured default model
    and effortLevel. Returns (model, effort), either may be None if unset.
    Re-read each call so live edits via Claude Code's `/config` are picked up.
    """
    settings = Path.home() / ".claude" / "settings.json"
    if not settings.is_file():
        return (None, None)
    try:
        data = json.loads(settings.read_text())
    except (OSError, json.JSONDecodeError):
        return (None, None)
    return (data.get("model") or None, data.get("effortLevel") or None)


# Per-model context window. Keep conservative defaults; override here when
# new models ship or beta longer-context tiers stabilize.
CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-7": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
}
DEFAULT_CONTEXT_WINDOW = 200_000


def _context_window_for(model: str | None) -> int:
    if not model:
        return DEFAULT_CONTEXT_WINDOW
    if model in CONTEXT_WINDOWS:
        return CONTEXT_WINDOWS[model]
    # Match aliases ("opus", "sonnet", "haiku") and partials.
    for key, win in CONTEXT_WINDOWS.items():
        if model in key or key.startswith(f"claude-{model}-"):
            return win
    return DEFAULT_CONTEXT_WINDOW


def _context_summary() -> tuple[str, float | None]:
    """Returns (one-line context summary, percent_used or None)."""
    if not state.last_usage:
        return ("(no usage yet — send a prompt first)", None)
    u = state.last_usage
    used = (
        int(u.get("input_tokens", 0) or 0)
        + int(u.get("cache_creation_input_tokens", 0) or 0)
        + int(u.get("cache_read_input_tokens", 0) or 0)
    )
    window = _context_window_for(state.last_active_model or state.model)
    pct = (used / window * 100) if window else 0.0
    return (f"{used:,} / {window:,} tokens ({pct:.1f}%)", pct)


def _context_block() -> str:
    if not state.last_usage:
        return (
            "_no usage recorded yet — send a prompt first, then `/context` "
            "shows the context-window breakdown_"
        )
    u = state.last_usage
    inp = int(u.get("input_tokens", 0) or 0)
    cache_create = int(u.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
    output = int(u.get("output_tokens", 0) or 0)
    used = inp + cache_create + cache_read
    model = state.last_active_model or state.model or "(unknown)"
    window = _context_window_for(state.last_active_model or state.model)
    pct = (used / window * 100) if window else 0.0
    free = max(window - used, 0)
    return (
        "```\n"
        f"model:           {model}\n"
        f"context used:    {used:,} / {window:,}  ({pct:.1f}%)\n"
        f"context free:    {free:,}\n"
        f"  input:         {inp:,}\n"
        f"  cache create:  {cache_create:,}\n"
        f"  cache read:    {cache_read:,}\n"
        f"output (turn):   {output:,}\n"
        "```"
        "_note: this is the headless `usage` payload, not the TUI's_ "
        "_/context breakdown — system prompt + tool defs are bundled into `input`._"
    )


def _status_block() -> str:
    """Render the /status output. Resolves (default) values to the real
    underlying setting where we can read it, so the user sees what Claude
    Code will actually use, not just the word 'default'.
    """
    cfg_model, cfg_effort = _read_claude_defaults()
    if state.model:
        model_line = state.model
    elif cfg_model:
        model_line = f"{cfg_model}  (default — from ~/.claude/settings.json)"
    else:
        model_line = "(unset — Claude Code uses its built-in default; pin one with /model or in ~/.claude/settings.json)"
    if state.effort:
        effort_line = state.effort
    elif cfg_effort:
        effort_line = f"{cfg_effort}  (default — from ~/.claude/settings.json)"
    else:
        effort_line = "(unset — Claude Code uses its built-in default; pin one with /effort or in ~/.claude/settings.json)"
    auto_on = state.permission_mode == "bypassPermissions"
    auto_line = "ON  (bypassPermissions — tools run without confirmation)" if auto_on \
        else "OFF (permissions enforced — but headless can't prompt, so tools needing approval will fail)"
    session_line = state.session_id or "(none — next prompt starts a fresh session)"
    ctx_line, _ = _context_summary()
    running_line = (
        "yes — send /stop to abort"
        if (state.current_proc is not None and state.current_proc.returncode is None)
        else "no"
    )
    return (
        "```\n"
        f"session:    {session_line}\n"
        f"model:      {model_line}\n"
        f"effort:     {effort_line}\n"
        f"auto_mode:  {auto_line}\n"
        f"show_tools: {'on' if state.show_tools else 'off'}\n"
        f"context:    {ctx_line}\n"
        f"running:    {running_line}\n"
        "```"
    )


HELP_TEXT = (
    "**claude-discord bridge** — your DMs are sent to Claude Code via headless mode.\n"
    "```\n"
    "Plain text       Sent as a prompt. Claude Code's own /init, /review,\n"
    "                 /security-review etc. pass through as prompts.\n"
    "/new             Start a fresh session (forget prior context).\n"
    "/stop            Abort the currently running turn (works mid-stream).\n"
    "/context         Show context-window usage from the last turn.\n"
    "/auto on|off     Toggle auto-mode (bypassPermissions). Default: on.\n"
    "/model <name>    Switch model: opus / sonnet / haiku / <full-name>.\n"
    "/effort <level>  low / medium / high / xhigh / max.\n"
    "/tools on|off    Show tool-call notifications. Default: off.\n"
    "/status          Show session/model/mode + context %.\n"
    "/help            This message.\n"
    "\n"
    "Steering: Claude Code has no /steer command. Closest equivalent is\n"
    "/stop the current turn, then DM a new prompt — the session resumes\n"
    "with your redirect on top of the prior context.\n"
    "\n"
    "/exit and /quit are intentionally no-ops in the bridge so a typo\n"
    "can't kill the bot or any running claude session.\n"
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


_last_send_at: dict[int, float] = {}
_MIN_SEND_INTERVAL = 0.4


async def _safe_send(target, content: str, *, max_attempts: int = 5) -> None:
    """channel.send with per-channel throttle + retry on Discord 429.

    discord.py auto-retries per-bucket 429s but NOT code 40062
    ("Service resource is being rate limited") — those bubble up as
    raw HTTPException and crash the turn. We catch, parse retry_after
    from the response body, sleep, and retry.
    """
    now = asyncio.get_event_loop().time()
    last = _last_send_at.get(target.id, 0.0)
    gap = _MIN_SEND_INTERVAL - (now - last)
    if gap > 0:
        await asyncio.sleep(gap)

    for attempt in range(max_attempts):
        try:
            await target.send(content)
            _last_send_at[target.id] = asyncio.get_event_loop().time()
            return
        except discord.HTTPException as e:
            if e.status != 429 or attempt == max_attempts - 1:
                raise
            delay: float | None = None
            try:
                body = json.loads(e.text) if isinstance(e.text, str) else {}
                ra = body.get("retry_after") if isinstance(body, dict) else None
                if isinstance(ra, (int, float)):
                    delay = float(ra)
            except (json.JSONDecodeError, ValueError):
                pass
            if delay is None:
                resp = getattr(e, "response", None)
                if resp is not None:
                    try:
                        delay = float(resp.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        delay = None
            if not delay or delay <= 0:
                delay = 5.0
            print(
                f"[bridge] discord 429 (code={getattr(e, 'code', '?')}); "
                f"sleeping {delay:.1f}s then retry {attempt + 2}/{max_attempts}",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(delay + 0.5)
            _last_send_at[target.id] = asyncio.get_event_loop().time()


async def send_text(channel, text: str) -> None:
    """Send text as plain Discord markdown, paragraph-aware chunked."""
    chunks = split_at_boundary(text)
    for chunk in chunks:
        await _safe_send(channel, chunk)


def format_tool_call(name: str, inp: dict) -> str:
    """One-line summary of a tool call for an inline notification."""
    if name == "Bash":
        lines = inp.get("command", "").strip().splitlines()
        cmd = lines[0][:80] if lines else ""
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
        limit=STREAM_BUFFER_LIMIT,
    )
    state.current_proc = proc

    last_session_id: str | None = None
    error_payload: str | None = None
    stopped_by_user = False

    async with channel.typing():
        assert proc.stdout is not None
        while True:
            try:
                line_bytes = await proc.stdout.readline()
            except asyncio.LimitOverrunError as e:
                # Single event exceeded STREAM_BUFFER_LIMIT. Drain past the
                # newline so we can keep reading subsequent events instead of
                # crashing the turn.
                await proc.stdout.readexactly(e.consumed)
                try:
                    await proc.stdout.readuntil(b"\n")
                except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
                    pass
                await _safe_send(channel,
                    f"_⚠ skipped one stream-json event larger than "
                    f"{STREAM_BUFFER_LIMIT // (1024 * 1024)} MB_"
                )
                continue
            if not line_bytes:
                break
            try:
                event = json.loads(line_bytes.decode().strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            etype = event.get("type")

            # Track session id for resume on next turn.
            if etype == "system" and event.get("subtype") == "init":
                last_session_id = event.get("session_id") or last_session_id
                # Authoritative model name from the runtime — used for context %.
                m = event.get("model")
                if m:
                    state.last_active_model = m

            # Assistant turn complete (one per turn — tools cause multiple turns).
            elif etype == "assistant":
                msg = event.get("message", {})
                # Snapshot usage so /context + /status reflect the latest API call.
                u = msg.get("usage")
                if isinstance(u, dict):
                    state.last_usage = u
                m = msg.get("model")
                if m:
                    state.last_active_model = m
                for block in msg.get("content", []) or []:
                    btype = block.get("type")
                    if btype == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            await send_text(channel, text)
                    elif btype == "tool_use" and state.show_tools:
                        name = block.get("name", "?")
                        inp = block.get("input", {}) or {}
                        await _safe_send(channel,f"_↳ {format_tool_call(name, inp)}_")

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
    # /stop sets this flag via terminate/kill — clear it here so the post-mortem
    # below recognizes the negative returncode as intentional, not a crash.
    if proc.returncode is not None and proc.returncode < 0:
        stopped_by_user = True
    state.current_proc = None

    if last_session_id:
        state.session_id = last_session_id

    if stopped_by_user:
        # /stop already echoed an ack; suppress the "exited with code -15" noise.
        return
    if proc.returncode != 0 and error_payload is None:
        tail = stderr_data.decode(errors="replace").strip().splitlines()[-3:]
        error_payload = "claude exited with code {} — {}".format(
            proc.returncode, " | ".join(tail) if tail else "(no stderr)",
        )
    if error_payload:
        await _safe_send(channel,f"_⚠ {error_payload}_")


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


@tree.command(name="status", description="Show session, model, mode, and context %")
async def slash_status(interaction: discord.Interaction):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    await interaction.response.send_message(_status_block(), ephemeral=True)


@tree.command(name="context", description="Show context-window usage from the last turn")
async def slash_context(interaction: discord.Interaction):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    await interaction.response.send_message(_context_block(), ephemeral=True)


@tree.command(name="stop", description="Abort the currently running Claude turn")
async def slash_stop(interaction: discord.Interaction):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    msg = await _do_stop()
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="help", description="Show bridge command help")
async def slash_help(interaction: discord.Interaction):
    if not _is_authorized(interaction.user.id):
        return await _deny(interaction)
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)


async def _do_stop() -> str:
    """Terminate the live Claude subprocess, if any. Safe to call from outside
    the per-user lock — that is the whole point: /stop must work mid-turn."""
    proc = state.current_proc
    if proc is None or proc.returncode is not None:
        return "_no turn running_"
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return "_stopped — session preserved, send a new prompt to continue_"
    except ProcessLookupError:
        return "_turn already finished_"


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

    # Pre-lock interceptors. /stop MUST be processed without waiting on the
    # per-user lock (the lock is held by the running turn it's trying to abort).
    # /exit + /quit are no-oped here so a typo can't kill the bot or the
    # session — would be catastrophic mid-task.
    stripped = content.strip()
    pre = stripped[1:] if stripped.startswith(("!", "/")) else ""
    pre_cmd = pre.split(maxsplit=1)[0].lower() if pre else ""
    if pre_cmd == "stop":
        await _safe_send(channel,await _do_stop())
        return
    if pre_cmd in ("exit", "quit"):
        await _safe_send(channel,
            "_/exit and /quit are no-ops in the bridge — they won't kill the "
            "bot or any claude session. use /stop to abort the current turn._"
        )
        return

    async with _lock:
        try:
            await _handle(channel, content)
        except Exception as e:
            await _safe_send(channel,f"_bridge error: {type(e).__name__}: {e}_")
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
        await _safe_send(channel,HELP_TEXT)
        return
    if cmd in ("/status",):
        await _safe_send(channel,_status_block())
        return
    if cmd in ("/context",):
        await _safe_send(channel,_context_block())
        return
    if cmd in ("/new",):
        state.session_id = None
        await _safe_send(channel,"_new session — next prompt starts fresh_")
        return
    if cmd in ("/auto",):
        if arg.lower() in ("", "on"):
            state.permission_mode = "bypassPermissions"
            await _safe_send(channel,"_auto mode on_")
        elif arg.lower() == "off":
            state.permission_mode = "default"
            await _safe_send(channel,"_auto mode off_")
        else:
            await _safe_send(channel,"_usage: /auto on  |  /auto off_")
        return
    if cmd in ("/model",):
        state.model = arg or None
        await _safe_send(channel,f"_model: {state.model or '(default)'}_")
        return
    if cmd in ("/effort",):
        if arg and arg not in ("low", "medium", "high", "xhigh", "max"):
            await _safe_send(channel,"_effort must be: low, medium, high, xhigh, max_")
            return
        state.effort = arg or None
        await _safe_send(channel,f"_effort: {state.effort or '(default)'}_")
        return
    if cmd in ("/tools",):
        if arg.lower() in ("", "on"):
            state.show_tools = True
            await _safe_send(channel,"_tool notifications on_")
        elif arg.lower() == "off":
            state.show_tools = False
            await _safe_send(channel,"_tool notifications off_")
        else:
            await _safe_send(channel,"_usage: /tools on  |  /tools off_")
        return

    # Anything else (including Claude Code skill commands like /init, /review,
    # /security-review) goes to claude as a prompt.
    await run_claude_turn(channel, content)


def main() -> None:
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
