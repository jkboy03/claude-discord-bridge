"""Unified multi-agent Discord bridge.

Runs multiple Discord bot accounts in one asyncio process. Configure agents with
BRIDGE_AGENTS=claudette,luna and per-agent environment prefixes, for example:
CLAUDETTE_TOKEN, CLAUDETTE_BACKEND=claude, CLAUDETTE_WORKDIR,
CLAUDETTE_ALLOWED_USER_ID, CLAUDETTE_CLAUDE_BIN; LUNA_BACKEND=codex,
LUNA_CODEX_BIN, etc.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DISCORD_LIMIT = 1900
STREAM_BUFFER_LIMIT = 16 * 1024 * 1024
DISCORD_FILE_LIMIT = 24 * 1024 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SENDABLE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".txt", ".md", ".pdf", ".csv", ".json", ".html", ".zip", ".svg",
    ".mp3", ".wav",
}
SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
CODEX_EFFORTS = {"low", "medium", "high", "xhigh"}
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
BACKENDS = {"codex", "claude"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _split_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(",", " ").split() if x.isdigit()}


def _safe_prefix(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or "attachment"


def split_at_boundary(text: str, max_size: int = DISCORD_LIMIT) -> list[str]:
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
    return [chunk for chunk in chunks if chunk.strip()]


def parse_file_line(line: str) -> Path | None:
    stripped = line.strip()
    if stripped.startswith("MEDIA:"):
        raw_path = stripped[len("MEDIA:"):].strip()
    elif stripped.startswith("FILE:"):
        raw_path = stripped[len("FILE:"):].strip()
    else:
        return None
    if raw_path.startswith("file://"):
        raw_path = raw_path[len("file://"):]
    return Path(raw_path).expanduser() if raw_path else None


def is_sendable_file(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        return (
            resolved.is_file()
            and resolved.suffix.lower() in SENDABLE_EXTENSIONS
            and resolved.stat().st_size <= DISCORD_FILE_LIMIT
        )
    except OSError:
        return False


def _read_claude_defaults() -> tuple[str | None, str | None]:
    """Best-effort read of ~/.claude/settings.json for (model, effort)."""
    path = Path.home() / ".claude" / "settings.json"
    if not path.is_file():
        return (None, None)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return (None, None)
    return (data.get("model") or None, data.get("effortLevel") or None)


def _read_codex_defaults() -> tuple[str | None, str | None]:
    """Best-effort read of ~/.codex/config.toml for (model, effort)."""
    path = Path.home() / ".codex" / "config.toml"
    if not path.is_file():
        return (None, None)
    try:
        text = path.read_text()
    except OSError:
        return (None, None)

    def read_key(key: str) -> str | None:
        match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", text)
        return match.group(1) if match else None

    return (read_key("model"), read_key("model_reasoning_effort"))


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
    for key, win in CONTEXT_WINDOWS.items():
        if model in key or key.startswith(f"claude-{model}-"):
            return win
    return DEFAULT_CONTEXT_WINDOW


def _bot_only_mention_prefix(config: "AgentConfig", channel_id: int | None) -> str:
    """Return a Discord-mention prefix for the manager bot(s) when the
    target channel is in bot_only_channel_ids. Empty string otherwise.

    Auto-mentioning the manager guarantees the gateway delivers the worker
    reply's full content to the manager's session even if its bot app
    lacks Message Content Intent.
    """
    if channel_id is None or channel_id not in config.bot_only_channel_ids:
        return ""
    if not config.allowed_bot_user_ids:
        return ""
    return " ".join(f"<@{uid}>" for uid in sorted(config.allowed_bot_user_ids))


async def send_text(channel, text: str, *, mention_prefix: str = "") -> None:
    """Send text to a Discord channel, splitting at boundaries.

    `mention_prefix` is prepended to the first chunk only. Used to @mention
    the manager bot in bot-only orchestration channels so its gateway
    delivers full content even without Message Content Intent active.
    """
    text_lines: list[str] = []
    file_paths: list[Path] = []
    for line in text.splitlines():
        file_path = parse_file_line(line)
        if file_path is None:
            text_lines.append(line)
        elif is_sendable_file(file_path):
            file_paths.append(file_path.resolve())
        else:
            text_lines.append(f"_warning: file not found or not sendable: `{file_path}`_")

    chunks = split_at_boundary("\n".join(text_lines).strip())
    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(0.4)
        if i == 0 and mention_prefix:
            chunk = f"{mention_prefix} {chunk}"
        await channel.send(chunk)
    for path in file_paths:
        await channel.send(file=discord.File(path))


class AttachmentStore:
    """Shared inbound attachment handling for all configured agents."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def save(self, agent_name: str, message: discord.Message) -> list[Path]:
        attachments = getattr(message, "attachments", [])
        if not attachments:
            return []
        msg_dir = self.root / _safe_filename(agent_name) / str(message.id)
        msg_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for idx, attachment in enumerate(attachments, start=1):
            filename = _safe_filename(attachment.filename or f"attachment-{idx}")
            path = msg_dir / f"{idx:02d}-{filename}"
            await attachment.save(path)
            paths.append(path)
        return paths


def split_attachment_paths(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    image_paths: list[Path] = []
    file_paths: list[Path] = []
    for path in paths:
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            image_paths.append(path)
        else:
            file_paths.append(path)
    return image_paths, file_paths


def build_prompt(content: str, file_paths: list[Path]) -> str:
    body = content.strip()
    if not file_paths:
        return body
    attachment_lines = [
        "Attached Discord file(s) were downloaded locally. Inspect them directly by absolute path:",
        *[f"- {path}" for path in file_paths],
    ]
    if body:
        return body + "\n\n" + "\n".join(attachment_lines)
    return "Please inspect the attached Discord file(s).\n\n" + "\n".join(attachment_lines)


@dataclass
class AgentConfig:
    name: str
    prefix: str
    token: str
    backend: str
    allowed_user_id: int
    workdir: Path
    attachment_dir: Path
    codex_bin: str = ""
    claude_bin: str = ""
    allowed_channel_ids: set[int] = field(default_factory=set)
    allowed_bot_user_ids: set[int] = field(default_factory=set)
    # Channels where the human user is ignored. Only senders in
    # allowed_bot_user_ids may speak. Subset of allowed_channel_ids.
    bot_only_channel_ids: set[int] = field(default_factory=set)
    # When False the bridge ignores all DMs (use for worker bots
    # that should only respond to a manager bot in shared channels).
    accept_dms: bool = True
    default_model: str | None = None
    default_effort: str | None = None
    default_sandbox: str = "workspace-write"
    default_search: bool = False
    claude_permission_mode: str = "bypassPermissions"
    aliases: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls, name: str, *, shared_attachment_dir: Path | None = None) -> "AgentConfig":
        prefix = _safe_prefix(name)

        def env(key: str, default: str = "") -> str:
            return os.environ.get(f"{prefix}_{key}", default).strip()

        token = env("TOKEN") or env("DISCORD_BOT_TOKEN")
        backend = env("BACKEND", "codex").lower()
        user_raw = env("ALLOWED_USER_ID")
        if not token:
            raise ValueError(f"{prefix}_TOKEN is required")
        if backend not in BACKENDS:
            raise ValueError(f"{prefix}_BACKEND must be one of: {', '.join(sorted(BACKENDS))}")
        if not user_raw.isdigit():
            raise ValueError(f"{prefix}_ALLOWED_USER_ID is required and must be numeric")

        workdir = Path(env("WORKDIR") or str(Path.home())).expanduser()
        attachment_dir = shared_attachment_dir or Path(
            env("ATTACHMENT_DIR") or os.environ.get("BRIDGE_ATTACHMENT_DIR", "")
            or (Path.home() / ".discord-agent-bridge" / "attachments")
        ).expanduser()
        sandbox = env("DEFAULT_SANDBOX", "workspace-write")
        if sandbox not in SANDBOX_MODES:
            sandbox = "workspace-write"

        codex_bin = env("CODEX_BIN") or shutil.which("codex") or ""
        claude_bin = env("CLAUDE_BIN") or shutil.which("claude") or ""
        if backend == "codex" and not codex_bin:
            raise ValueError(f"{prefix}_CODEX_BIN is required or codex must be on PATH")
        if backend == "claude" and not claude_bin:
            raise ValueError(f"{prefix}_CLAUDE_BIN is required or claude must be on PATH")

        return cls(
            name=name,
            prefix=prefix,
            token=token,
            backend=backend,
            allowed_user_id=int(user_raw),
            workdir=workdir,
            attachment_dir=attachment_dir,
            codex_bin=codex_bin,
            claude_bin=claude_bin,
            allowed_channel_ids=_split_ids(env("ALLOWED_CHANNEL_IDS")),
            allowed_bot_user_ids=_split_ids(env("ALLOWED_BOT_USER_IDS")),
            bot_only_channel_ids=_split_ids(env("BOT_ONLY_CHANNEL_IDS")),
            accept_dms=_env_bool(f"{prefix}_ACCEPT_DMS", True),
            default_model=env("DEFAULT_MODEL") or None,
            default_effort=env("DEFAULT_EFFORT") or None,
            default_sandbox=sandbox,
            default_search=_env_bool(f"{prefix}_DEFAULT_SEARCH", False),
            claude_permission_mode=env("CLAUDE_PERMISSION_MODE", "bypassPermissions") or "bypassPermissions",
            aliases={x.lower() for x in (env("ALIASES") or name).replace(",", " ").split() if x.strip()},
        )


class BackendRunner(Protocol):
    config: AgentConfig
    show_tools: bool
    current_proc: asyncio.subprocess.Process | None

    def status(self, channel_id: int | None = None) -> str: ...
    def help(self) -> str: ...
    async def new(self, channel_id: int | None = None) -> str: ...
    async def run(self, channel, prompt: str, attachment_paths: list[Path]) -> None: ...
    async def handle_command(self, channel, cmd: str, arg: str) -> bool: ...


class CodexRunner:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        # Per-channel session state. None as a key is used as the legacy/global
        # bucket for callers that don't pass a channel_id (e.g. unit tests).
        self._thread_ids: dict[int | None, str] = {}
        self._last_usage: dict[int | None, dict] = {}
        self.model = config.default_model
        self.effort = config.default_effort
        self.sandbox = config.default_sandbox
        self.search_enabled = config.default_search
        self.show_tools = False
        self.goal: str | None = None
        self.current_proc: asyncio.subprocess.Process | None = None

    def _mention_prefix_for(self, channel_id: int | None) -> str:
        return _bot_only_mention_prefix(self.config, channel_id)

    def help(self) -> str:
        return (
            f"**{self.config.name}** — Codex backend via `codex exec --json`.\n"
            "`/new`, `/goal [text|clear]`, `/stop`, `/review [text]`, `/model <name|default>`, "
            "`/effort low|medium|high|xhigh|default`, `/sandbox <mode>`, `/search on|off`, "
            "`/tools on|off`, `/context`, `/status`, `/help`."
        )

    def status(self, channel_id: int | None = None) -> str:
        running = self.current_proc is not None and self.current_proc.returncode is None
        thread = self._thread_ids.get(channel_id) or "(none)"
        cfg_model, cfg_effort = _read_codex_defaults()
        if self.model:
            model_line = self.model
        elif cfg_model:
            model_line = f"{cfg_model}  (from ~/.codex/config.toml)"
        else:
            model_line = "(unset — codex picks built-in default)"
        if self.effort:
            effort_line = self.effort
        elif cfg_effort:
            effort_line = f"{cfg_effort}  (from ~/.codex/config.toml)"
        else:
            effort_line = "(unset — codex picks built-in default)"
        return (
            "```\n"
            f"agent:   {self.config.name}\nbackend: {self.config.backend}\n"
            f"thread:  {thread}\nmodel:   {model_line}\n"
            f"effort:  {effort_line}\nsandbox: {self.sandbox}\n"
            f"search:  {'on' if self.search_enabled else 'off'}\ntools:   {'on' if self.show_tools else 'off'}\n"
            f"goal:    {self.goal or '(none)'}\nrunning: {'yes' if running else 'no'}\n```"
        )

    async def new(self, channel_id: int | None = None) -> str:
        self._thread_ids.pop(channel_id, None)
        return "_new Codex thread — next prompt starts fresh_"

    def _model_args(self) -> list[str]:
        args: list[str] = []
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--config", f'model_reasoning_effort="{self.effort}"']
        return args

    def codex_args(
        self,
        prompt: str,
        channel_id: int | None = None,
        image_paths: list[Path] | None = None,
    ) -> list[str]:
        image_args: list[str] = []
        for path in image_paths or []:
            image_args += ["--image", str(path)]
        thread_id = self._thread_ids.get(channel_id)
        if thread_id:
            return [
                self.config.codex_bin, "exec", "resume", "--json", "--skip-git-repo-check",
                *self._model_args(), thread_id, prompt, *image_args,
            ]
        args = [
            self.config.codex_bin, "exec", "--json", "--skip-git-repo-check", "-C",
            str(self.config.workdir), "-s", self.sandbox, *self._model_args(),
        ]
        if self.search_enabled:
            args.append("--search")
        args.append(prompt)
        args.extend(image_args)
        return args

    def review_args(self, prompt: str | None) -> list[str]:
        args = [self.config.codex_bin, "exec", "review", "--json", "--skip-git-repo-check", *self._model_args()]
        if prompt:
            args.append(prompt)
        return args

    def _compose_prompt(self, prompt: str) -> str:
        if not self.goal:
            return prompt
        return f"Current bridge goal:\n{self.goal}\n\nUser prompt:\n{prompt}"

    async def run(self, channel, prompt: str, attachment_paths: list[Path]) -> None:
        image_paths, file_paths = split_attachment_paths(attachment_paths)
        await self._run_codex(channel, build_prompt(prompt, file_paths), image_paths=image_paths)

    async def _run_codex(self, channel, prompt: str, *, review: bool = False, image_paths: list[Path] | None = None) -> None:
        channel_id = getattr(channel, "id", None)
        mention_prefix = self._mention_prefix_for(channel_id)
        rendered = prompt if review else self._compose_prompt(prompt)
        args = self.review_args(rendered) if review else self.codex_args(rendered, channel_id, image_paths)
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(self.config.workdir), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=STREAM_BUFFER_LIMIT,
        )
        self.current_proc = proc
        last_thread_id: str | None = None
        error_payload: str | None = None
        async with channel.typing():
            assert proc.stdout is not None
            while True:
                try:
                    line_bytes = await proc.stdout.readline()
                except asyncio.LimitOverrunError as e:
                    await proc.stdout.readexactly(e.consumed)
                    try:
                        await proc.stdout.readuntil(b"\n")
                    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
                        pass
                    await channel.send(f"_skipped one Codex JSONL event larger than {STREAM_BUFFER_LIMIT // (1024 * 1024)} MB_")
                    continue
                if not line_bytes:
                    break
                try:
                    event = json.loads(line_bytes.decode().strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                etype = event.get("type")
                if etype == "thread.started":
                    last_thread_id = event.get("thread_id") or last_thread_id
                elif etype in {"item.started", "item.completed"}:
                    item = event.get("item", {}) or {}
                    itype = item.get("type")
                    if itype == "agent_message" and etype == "item.completed":
                        text = (item.get("text") or "").strip()
                        if text:
                            await send_text(channel, text, mention_prefix=mention_prefix)
                    elif self.show_tools and itype != "agent_message":
                        await channel.send(f"_> {format_codex_item(item)}_")
                elif etype == "turn.completed" and isinstance(event.get("usage"), dict):
                    self._last_usage[channel_id] = event["usage"]
                elif etype == "error":
                    error_payload = event.get("message") or event.get("error") or "unknown error"
        stderr_data = b""
        if proc.stderr is not None:
            try:
                stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        await proc.wait()
        stopped_by_user = proc.returncode is not None and proc.returncode < 0
        self.current_proc = None
        if last_thread_id and not review:
            self._thread_ids[channel_id] = last_thread_id
        if stopped_by_user:
            return
        if proc.returncode != 0 and error_payload is None:
            tail = stderr_data.decode(errors="replace").strip().splitlines()[-3:]
            error_payload = f"codex exited with code {proc.returncode} - {' | '.join(tail) if tail else '(no stderr)'}"
        if error_payload:
            await channel.send(f"_warning: {error_payload}_")

    async def handle_command(self, channel, cmd: str, arg: str) -> bool:
        channel_id = getattr(channel, "id", None)
        if cmd == "/goal":
            if not arg:
                await channel.send(f"_goal: {self.goal or '(none)'}_")
            elif arg.lower() in {"clear", "off", "none", "reset"}:
                self.goal = None
                await channel.send("_goal cleared_")
            else:
                self.goal = arg
                await channel.send("_goal set_")
            return True
        if cmd == "/review":
            async with getattr(channel, "_bridge_lock", asyncio.Lock()):
                await self._run_codex(channel, arg, review=True)
            return True
        if cmd == "/model":
            self.model = None if arg.lower() in {"", "default"} else arg
            await channel.send(f"_model: {self.model or '(default)'}_")
            return True
        if cmd == "/effort":
            if arg.lower() in {"", "default"}:
                self.effort = None
            elif arg in CODEX_EFFORTS:
                self.effort = arg
            else:
                await channel.send("_effort must be: low, medium, high, xhigh, default_")
                return True
            await channel.send(f"_effort: {self.effort or '(default)'}_")
            return True
        if cmd == "/sandbox":
            if arg not in SANDBOX_MODES:
                await channel.send("_sandbox must be: read-only, workspace-write, danger-full-access_")
            else:
                self.sandbox = arg
                await channel.send(f"_sandbox: {self.sandbox}_")
            return True
        if cmd == "/search":
            if arg.lower() not in {"on", "off"}:
                await channel.send("_usage: /search on  |  /search off_")
            else:
                self.search_enabled = arg.lower() == "on"
                await channel.send(f"_search: {'on' if self.search_enabled else 'off'}_")
            return True
        if cmd == "/context":
            await channel.send(self._usage_block(channel_id))
            return True
        return False

    def _usage_block(self, channel_id: int | None = None) -> str:
        u = self._last_usage.get(channel_id)
        if not u:
            return "_no usage recorded yet - send a prompt first_"
        return "```\ninput:      {:,}\ncached:     {:,}\noutput:     {:,}\nreasoning:  {:,}\n```".format(
            int(u.get("input_tokens", 0) or 0), int(u.get("cached_input_tokens", 0) or 0),
            int(u.get("output_tokens", 0) or 0), int(u.get("reasoning_output_tokens", 0) or 0),
        )


class ClaudeRunner:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._session_ids: dict[int | None, str] = {}
        self._last_usage: dict[int | None, dict] = {}
        self._last_active_model: dict[int | None, str] = {}
        self.model = config.default_model
        self.effort = config.default_effort
        self.permission_mode = config.claude_permission_mode
        self.show_tools = False
        self.current_proc: asyncio.subprocess.Process | None = None

    def _mention_prefix_for(self, channel_id: int | None) -> str:
        return _bot_only_mention_prefix(self.config, channel_id)

    def help(self) -> str:
        return (
            f"**{self.config.name}** — Claude backend via headless `claude --print`.\n"
            "`/new`, `/auto on|off`, `/stop`, `/model <name|default>`, "
            "`/effort low|medium|high|xhigh|max|default`, `/tools on|off`, "
            "`/context`, `/status`, `/help`."
        )

    def status(self, channel_id: int | None = None) -> str:
        running = self.current_proc is not None and self.current_proc.returncode is None
        session = self._session_ids.get(channel_id) or "(none)"
        cfg_model, cfg_effort = _read_claude_defaults()
        if self.model:
            model_line = self.model
        elif cfg_model:
            model_line = f"{cfg_model}  (from ~/.claude/settings.json)"
        else:
            model_line = "(unset — Claude Code picks built-in default)"
        if self.effort:
            effort_line = self.effort
        elif cfg_effort:
            effort_line = f"{cfg_effort}  (from ~/.claude/settings.json)"
        else:
            effort_line = "(unset — Claude Code picks built-in default)"
        usage = self._last_usage.get(channel_id)
        if usage:
            used = (
                int(usage.get("input_tokens", 0) or 0)
                + int(usage.get("cache_creation_input_tokens", 0) or 0)
                + int(usage.get("cache_read_input_tokens", 0) or 0)
            )
            window = _context_window_for(self._last_active_model.get(channel_id) or self.model)
            pct = (used / window * 100) if window else 0.0
            ctx_line = f"{used:,} / {window:,} tokens ({pct:.1f}%)"
        else:
            ctx_line = "(no usage yet — send a prompt first)"
        return (
            "```\n"
            f"agent:           {self.config.name}\nbackend:         {self.config.backend}\n"
            f"session_id:      {session}\nmodel:           {model_line}\n"
            f"effort:          {effort_line}\npermission_mode: {self.permission_mode}\n"
            f"tools:           {'on' if self.show_tools else 'off'}\n"
            f"context:         {ctx_line}\nrunning:         {'yes' if running else 'no'}\n```"
        )

    def _usage_block(self, channel_id: int | None = None) -> str:
        usage = self._last_usage.get(channel_id)
        if not usage:
            return "_no usage recorded yet — send a prompt first_"
        inp = int(usage.get("input_tokens", 0) or 0)
        cache_create = int(usage.get("cache_creation_input_tokens", 0) or 0)
        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        output = int(usage.get("output_tokens", 0) or 0)
        used = inp + cache_create + cache_read
        active_model = self._last_active_model.get(channel_id) or self.model or "(unknown)"
        window = _context_window_for(self._last_active_model.get(channel_id) or self.model)
        pct = (used / window * 100) if window else 0.0
        free = max(window - used, 0)
        return (
            "```\n"
            f"model:           {active_model}\n"
            f"context used:    {used:,} / {window:,}  ({pct:.1f}%)\n"
            f"context free:    {free:,}\n"
            f"  input:         {inp:,}\n"
            f"  cache create:  {cache_create:,}\n"
            f"  cache read:    {cache_read:,}\n"
            f"output (turn):   {output:,}\n"
            "```"
        )

    async def new(self, channel_id: int | None = None) -> str:
        self._session_ids.pop(channel_id, None)
        return "_new Claude session — next prompt starts fresh_"

    def claude_args(self, prompt: str, channel_id: int | None = None) -> list[str]:
        args = [
            self.config.claude_bin, "--print", "--output-format", "stream-json", "--verbose",
            "--permission-mode", self.permission_mode, f"--add-dir={self.config.attachment_dir}",
        ]
        session_id = self._session_ids.get(channel_id)
        if session_id:
            args += ["--resume", session_id]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        args.append(prompt)
        return args

    async def run(self, channel, prompt: str, attachment_paths: list[Path]) -> None:
        await self._run_claude(channel, build_prompt(prompt, attachment_paths))

    async def _run_claude(self, channel, prompt: str) -> None:
        channel_id = getattr(channel, "id", None)
        mention_prefix = self._mention_prefix_for(channel_id)
        proc = await asyncio.create_subprocess_exec(
            *self.claude_args(prompt, channel_id), cwd=str(self.config.workdir), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=STREAM_BUFFER_LIMIT,
        )
        self.current_proc = proc
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
                if etype == "system" and event.get("subtype") == "init":
                    last_session_id = event.get("session_id") or last_session_id
                elif etype == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []) or []:
                        btype = block.get("type")
                        if btype == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                await send_text(channel, text, mention_prefix=mention_prefix)
                        elif btype == "tool_use" and self.show_tools:
                            await channel.send(f"_↳ {format_claude_tool(block.get('name', '?'), block.get('input', {}) or {})}_")
                elif etype == "result":
                    last_session_id = event.get("session_id") or last_session_id
                    if isinstance(event.get("usage"), dict):
                        self._last_usage[channel_id] = event["usage"]
                    if event.get("modelUsage") and isinstance(event["modelUsage"], dict):
                        # First key is the model identifier the API used.
                        try:
                            model_used = next(iter(event["modelUsage"]))
                            self._last_active_model[channel_id] = model_used
                        except StopIteration:
                            pass
                    if event.get("is_error"):
                        error_payload = event.get("result") or "(unknown error)"
        stderr_data = b""
        if proc.stderr is not None:
            try:
                stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        await proc.wait()
        stopped_by_user = proc.returncode is not None and proc.returncode < 0
        self.current_proc = None
        if last_session_id:
            self._session_ids[channel_id] = last_session_id
        if stopped_by_user:
            return
        if proc.returncode != 0 and error_payload is None:
            tail = stderr_data.decode(errors="replace").strip().splitlines()[-3:]
            error_payload = f"claude exited with code {proc.returncode} — {' | '.join(tail) if tail else '(no stderr)'}"
        if error_payload:
            await channel.send(f"_warning: {error_payload}_")

    async def handle_command(self, channel, cmd: str, arg: str) -> bool:
        channel_id = getattr(channel, "id", None)
        if cmd == "/context":
            await channel.send(self._usage_block(channel_id))
            return True
        if cmd == "/auto":
            if arg.lower() in {"", "on"}:
                self.permission_mode = "bypassPermissions"
                await channel.send("_auto mode on_")
            elif arg.lower() == "off":
                self.permission_mode = "default"
                await channel.send("_auto mode off_")
            else:
                await channel.send("_usage: /auto on  |  /auto off_")
            return True
        if cmd == "/model":
            self.model = None if arg.lower() in {"", "default"} else arg
            await channel.send(f"_model: {self.model or '(default)'}_")
            return True
        if cmd == "/effort":
            if arg.lower() in {"", "default"}:
                self.effort = None
            elif arg in CLAUDE_EFFORTS:
                self.effort = arg
            else:
                await channel.send("_effort must be: low, medium, high, xhigh, max, default_")
                return True
            await channel.send(f"_effort: {self.effort or '(default)'}_")
            return True
        return False


def format_codex_item(item: dict) -> str:
    itype = item.get("type", "?")
    if itype == "command_execution":
        command = (item.get("command") or "").strip().splitlines()
        first = command[0][:120] if command else ""
        status = item.get("status")
        exit_code = item.get("exit_code")
        suffix = ""
        if status and status != "in_progress":
            suffix = f" [{status}"
            if exit_code is not None:
                suffix += f", exit {exit_code}"
            suffix += "]"
        return f"$ {first}{suffix}"
    if itype == "file_change":
        return f"file_change({item.get('path', '')})"
    return f"{itype}(...)"


def format_claude_tool(name: str, inp: dict) -> str:
    if name == "Bash":
        cmd = inp.get("command", "").strip().splitlines()[0][:80]
        return f"$ {cmd}"
    if name in ("Read", "Edit", "Write"):
        return f"{name}({inp.get('file_path', inp.get('path', ''))})"
    if name in ("Glob", "Grep"):
        return f"{name}({inp.get('pattern', '')})"
    if name == "WebFetch":
        return f"WebFetch({inp.get('url', '')[:60]})"
    return f"{name}(...)"


async def do_stop(runner: BackendRunner) -> str:
    proc = runner.current_proc
    if proc is None or proc.returncode is not None:
        return "_no turn running_"
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return "_stopped - session preserved, send a new prompt to continue_"
    except ProcessLookupError:
        return "_turn already finished_"


class AgentBridge:
    def __init__(self, config: AgentConfig, attachment_store: AttachmentStore) -> None:
        self.config = config
        self.attachment_store = attachment_store
        self.runner: BackendRunner = CodexRunner(config) if config.backend == "codex" else ClaudeRunner(config)
        self.lock = asyncio.Lock()
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        self.client = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        self._install_events()
        self._install_slash_commands()

    def _channel_prompt_for_agent(self, content: str) -> str | None:
        """Return stripped prompt when an allowed shared-channel message targets
        this agent. Match aliases only at the start of the message — never
        anywhere in the body — so a worker reply that mentions another agent's
        name in passing does not trigger that agent."""
        text = content.lstrip()
        if not text:
            return None
        lowered = text.lower()
        mention = f"<@{self.client.user.id}>" if self.client.user else ""
        mention_nick = f"<@!{self.client.user.id}>" if self.client.user else ""
        for raw_prefix in (mention, mention_nick):
            if raw_prefix and text.startswith(raw_prefix):
                return text[len(raw_prefix):].lstrip(" :—-\n")
        prefixes: set[str] = set()
        for alias in self.config.aliases:
            prefixes.update({f"{alias}:", f"{alias} only:", f"@{alias}:", f"@{alias} "})
        for prefix in sorted(prefixes, key=len, reverse=True):
            if lowered.startswith(prefix):
                return text[len(prefix):].lstrip(" :—-\n")
        return None

    def _is_authorized_message(self, message: discord.Message) -> bool:
        is_dm = isinstance(message.channel, discord.DMChannel)
        channel_id = getattr(message.channel, "id", None)
        is_allowed_channel = channel_id in self.config.allowed_channel_ids
        is_bot_only_channel = channel_id in self.config.bot_only_channel_ids
        is_allowed_user = message.author.id == self.config.allowed_user_id and not message.author.bot
        is_allowed_bot = (
            (is_allowed_channel or is_bot_only_channel)
            and message.author.bot
            and message.author.id in self.config.allowed_bot_user_ids
        )
        if is_dm:
            return self.config.accept_dms and is_allowed_user
        if is_bot_only_channel:
            # Humans ignored entirely in this channel; only an allowlisted bot
            # (e.g. the manager agent) may speak.
            return is_allowed_bot
        if is_allowed_channel:
            return is_allowed_user or is_allowed_bot
        return False

    def _install_events(self) -> None:
        @self.client.event
        async def on_ready() -> None:
            print(f"[{self.config.name}] logged in as {self.client.user} (backend={self.config.backend}, workdir={self.config.workdir})")
            try:
                synced = await self.tree.sync()
                print(f"[{self.config.name}] synced {len(synced)} global slash commands")
            except Exception as e:
                print(f"[{self.config.name}] slash command sync FAILED ({type(e).__name__}: {e})")

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            if not self._is_authorized_message(message):
                return
            content = message.content
            if not isinstance(message.channel, discord.DMChannel):
                routed = self._channel_prompt_for_agent(content)
                if routed is None:
                    return
                content = routed
            # Pre-lock interception: a manager bot can issue
            # "<alias>: __stop__" to abort the running turn without queueing
            # behind the per-agent lock. Authorization is already enforced
            # above (sender must be in allowed_bot_user_ids).
            if message.author.bot and content.strip() == "__stop__":
                await message.channel.send(await do_stop(self.runner))
                return
            paths = await self.attachment_store.save(self.config.name, message)
            await self.handle(message.channel, content, paths)

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("not authorized", ephemeral=True)

    def _install_slash_commands(self) -> None:
        @self.tree.command(name="new", description="Start a fresh backend session/thread")
        async def slash_new(interaction: discord.Interaction):
            if interaction.user.id != self.config.allowed_user_id:
                return await self._deny(interaction)
            channel_id = getattr(interaction.channel, "id", None)
            await interaction.response.send_message(await self.runner.new(channel_id), ephemeral=True)

        @self.tree.command(name="stop", description="Abort the currently running backend turn")
        async def slash_stop(interaction: discord.Interaction):
            if interaction.user.id != self.config.allowed_user_id:
                return await self._deny(interaction)
            await interaction.response.send_message(await do_stop(self.runner), ephemeral=True)

        @self.tree.command(name="status", description="Show bridge state")
        async def slash_status(interaction: discord.Interaction):
            if interaction.user.id != self.config.allowed_user_id:
                return await self._deny(interaction)
            channel_id = getattr(interaction.channel, "id", None)
            await interaction.response.send_message(self.runner.status(channel_id), ephemeral=True)

        @self.tree.command(name="help", description="Show bridge command help")
        async def slash_help(interaction: discord.Interaction):
            if interaction.user.id != self.config.allowed_user_id:
                return await self._deny(interaction)
            await interaction.response.send_message(self.runner.help(), ephemeral=True)

        @self.tree.command(name="model", description="Set the backend model for next turns")
        @app_commands.describe(name="Model name (e.g. sonnet, gpt-5.5). Use 'default' to clear.")
        async def slash_model(interaction: discord.Interaction, name: str):
            if interaction.user.id != self.config.allowed_user_id:
                return await self._deny(interaction)
            new_model = None if name.strip().lower() in {"", "default"} else name.strip()
            self.runner.model = new_model
            label = self.runner.model or "(default)"
            await interaction.response.send_message(f"_model: {label}_", ephemeral=True)

        # /tools, /context, /effort apply to both backends.
        tools_choices = [
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]

        @self.tree.command(name="tools", description="Show or hide tool-call notifications inline")
        @app_commands.choices(mode=tools_choices)
        async def slash_tools(interaction: discord.Interaction, mode: app_commands.Choice[str]):
            if interaction.user.id != self.config.allowed_user_id:
                return await self._deny(interaction)
            self.runner.show_tools = mode.value == "on"
            await interaction.response.send_message(
                f"_tool notifications {'on' if self.runner.show_tools else 'off'}_",
                ephemeral=True,
            )

        @self.tree.command(name="context", description="Show context/usage from the last turn")
        async def slash_context(interaction: discord.Interaction):
            if interaction.user.id != self.config.allowed_user_id:
                return await self._deny(interaction)
            channel_id = getattr(interaction.channel, "id", None)
            await interaction.response.send_message(self.runner._usage_block(channel_id), ephemeral=True)

        if self.config.backend == "codex":
            codex_effort_choices = [
                app_commands.Choice(name=v, value=v)
                for v in ("low", "medium", "high", "xhigh", "default")
            ]

            @self.tree.command(name="effort", description="Set reasoning effort for next turns")
            @app_commands.choices(level=codex_effort_choices)
            async def slash_effort_codex(interaction: discord.Interaction, level: app_commands.Choice[str]):
                if interaction.user.id != self.config.allowed_user_id:
                    return await self._deny(interaction)
                self.runner.effort = None if level.value == "default" else level.value
                await interaction.response.send_message(
                    f"_effort: {self.runner.effort or '(default)'}_",
                    ephemeral=True,
                )

            sandbox_choices = [
                app_commands.Choice(name=v, value=v) for v in sorted(SANDBOX_MODES)
            ]

            @self.tree.command(name="sandbox", description="Set sandbox mode for new Codex threads")
            @app_commands.choices(mode=sandbox_choices)
            async def slash_sandbox(interaction: discord.Interaction, mode: app_commands.Choice[str]):
                if interaction.user.id != self.config.allowed_user_id:
                    return await self._deny(interaction)
                self.runner.sandbox = mode.value
                await interaction.response.send_message(
                    f"_sandbox: {self.runner.sandbox}_", ephemeral=True
                )

            @self.tree.command(name="search", description="Toggle Codex web search for new threads")
            @app_commands.choices(mode=tools_choices)
            async def slash_search(interaction: discord.Interaction, mode: app_commands.Choice[str]):
                if interaction.user.id != self.config.allowed_user_id:
                    return await self._deny(interaction)
                self.runner.search_enabled = mode.value == "on"
                await interaction.response.send_message(
                    f"_search: {'on' if self.runner.search_enabled else 'off'}_",
                    ephemeral=True,
                )

            @self.tree.command(name="goal", description="Set, show, or clear sticky bridge goal")
            @app_commands.describe(text="Goal text. Use 'clear' to clear, or omit to show current.")
            async def slash_goal(interaction: discord.Interaction, text: str | None = None):
                if interaction.user.id != self.config.allowed_user_id:
                    return await self._deny(interaction)
                if text is None or not text.strip():
                    msg = f"_goal: {self.runner.goal or '(none)'}_"
                elif text.strip().lower() in {"clear", "off", "none", "reset"}:
                    self.runner.goal = None
                    msg = "_goal cleared_"
                else:
                    self.runner.goal = text.strip()
                    msg = "_goal set_"
                await interaction.response.send_message(msg, ephemeral=True)

            @self.tree.command(name="review", description="Run codex exec review")
            @app_commands.describe(prompt="Optional review prompt; omit for repo-wide review.")
            async def slash_review(interaction: discord.Interaction, prompt: str | None = None):
                if interaction.user.id != self.config.allowed_user_id:
                    return await self._deny(interaction)
                await interaction.response.send_message("_running codex review…_", ephemeral=True)
                async with self.lock:
                    await self.runner._run_codex(interaction.channel, prompt or "", review=True)

        else:  # claude backend
            claude_effort_choices = [
                app_commands.Choice(name=v, value=v)
                for v in ("low", "medium", "high", "xhigh", "max", "default")
            ]

            @self.tree.command(name="effort", description="Set reasoning effort for next turns")
            @app_commands.choices(level=claude_effort_choices)
            async def slash_effort_claude(interaction: discord.Interaction, level: app_commands.Choice[str]):
                if interaction.user.id != self.config.allowed_user_id:
                    return await self._deny(interaction)
                self.runner.effort = None if level.value == "default" else level.value
                await interaction.response.send_message(
                    f"_effort: {self.runner.effort or '(default)'}_",
                    ephemeral=True,
                )

            auto_choices = [
                app_commands.Choice(name="on", value="on"),
                app_commands.Choice(name="off", value="off"),
            ]

            @self.tree.command(name="auto", description="Toggle auto-mode (bypassPermissions)")
            @app_commands.choices(mode=auto_choices)
            async def slash_auto(interaction: discord.Interaction, mode: app_commands.Choice[str]):
                if interaction.user.id != self.config.allowed_user_id:
                    return await self._deny(interaction)
                self.runner.permission_mode = (
                    "bypassPermissions" if mode.value == "on" else "default"
                )
                await interaction.response.send_message(
                    f"_auto mode {mode.value}_", ephemeral=True
                )

    async def handle(self, channel, content: str, attachment_paths: list[Path] | None = None) -> None:
        attachment_paths = attachment_paths or []
        channel_id = getattr(channel, "id", None)
        stripped = content.strip()
        if not stripped and not attachment_paths:
            return
        norm = "/" + stripped[1:] if stripped.startswith("!") else stripped
        head = norm.split(maxsplit=1)
        cmd = head[0].lower() if head else ""
        arg = head[1].strip() if len(head) > 1 else ""
        if cmd == "/help":
            await channel.send(self.runner.help())
            return
        if cmd == "/status":
            await channel.send(self.runner.status(channel_id))
            return
        if cmd == "/new":
            await channel.send(await self.runner.new(channel_id))
            return
        if cmd == "/stop":
            await channel.send(await do_stop(self.runner))
            return
        if cmd == "/tools":
            if arg.lower() in {"", "on"}:
                self.runner.show_tools = True
                await channel.send("_tool notifications on_")
            elif arg.lower() == "off":
                self.runner.show_tools = False
                await channel.send("_tool notifications off_")
            else:
                await channel.send("_usage: /tools on  |  /tools off_")
            return
        if cmd and await self.runner.handle_command(channel, cmd, arg):
            return
        async with self.lock:
            await self.runner.run(channel, content, attachment_paths)

    async def start(self) -> None:
        await self.client.start(self.config.token)


def load_agent_configs() -> list[AgentConfig]:
    names = [x.strip() for x in os.environ.get("BRIDGE_AGENTS", "").replace(",", " ").split() if x.strip()]
    if not names:
        raise ValueError("BRIDGE_AGENTS is required, e.g. BRIDGE_AGENTS=claudette,luna")
    shared_dir_raw = os.environ.get("BRIDGE_ATTACHMENT_DIR", "").strip()
    shared_dir = Path(shared_dir_raw).expanduser() if shared_dir_raw else None
    return [AgentConfig.from_env(name, shared_attachment_dir=shared_dir) for name in names]


async def amain() -> None:
    configs = load_agent_configs()
    store = AttachmentStore(configs[0].attachment_dir)
    bridges = [AgentBridge(config, store) for config in configs]
    print("[bridge] starting agents: " + ", ".join(f"{c.name}:{c.backend}" for c in configs))
    await asyncio.gather(*(bridge.start() for bridge in bridges))


def main() -> None:
    try:
        asyncio.run(amain())
    except ValueError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
