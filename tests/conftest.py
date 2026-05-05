"""Shared test fixtures + env-var stubbing.

bot.py validates env vars at import time and exits the process on any miss.
We must set BRIDGE_* before bot is imported anywhere — conftest runs before
test modules, so doing it at module scope here is sufficient.

We also stub BRIDGE_CLAUDE_BIN to /usr/bin/true so the executable-exists
check passes without requiring Claude Code to be installed in CI.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Set BEFORE bot is imported. setdefault means a real env var (e.g. CI)
# can still override, but in practice tests get these stubs.
os.environ.setdefault("BRIDGE_DISCORD_BOT_TOKEN", "test-token-not-real")
os.environ.setdefault("BRIDGE_ALLOWED_USER_ID", "111111111111111111")
os.environ.setdefault("BRIDGE_CLAUDE_BIN", "/usr/bin/true")
os.environ.setdefault("BRIDGE_WORKDIR", "/tmp")

# Make the bridge dir importable as a package root.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import bot  # noqa: E402  (must come AFTER os.environ setup)


# Stand-in for discord.DMChannel so isinstance checks in bot.on_message
# accept our FakeChannel (defined below).  Must happen before any test runs.
class _DMChannelStub:
    pass


bot.discord.DMChannel = _DMChannelStub


# ---------------------------------------------------------------------------
# State reset — bot.state is a module global, so every test must start clean.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state():
    """Reset SessionState + free the lock between tests."""
    yield
    bot.state.session_id = None
    bot.state.model = None
    bot.state.effort = None
    bot.state.permission_mode = "bypassPermissions"
    bot.state.show_tools = False
    bot.state.last_usage = None
    bot.state.last_active_model = None
    bot.state.current_proc = None
    # Drain the asyncio.Lock if a test left it acquired.
    while bot._lock.locked():
        bot._lock.release()


# ---------------------------------------------------------------------------
# Fakes for Discord + asyncio.subprocess
# ---------------------------------------------------------------------------

class _AsyncCM:
    """Minimal async context manager (channel.typing())."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeChannel(_DMChannelStub):
    """Stand-in for discord.DMChannel — collects sent messages."""
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content):
        self.sent.append(content)

    def typing(self):
        return _AsyncCM()


class FakeAuthor:
    def __init__(self, user_id: int, is_bot: bool = False):
        self.id = user_id
        self.bot = is_bot


class FakeMessage:
    """Stand-in for discord.Message."""
    def __init__(self, content: str, author_id: int, *, is_bot: bool = False, dm: bool = True):
        self.content = content
        self.author = FakeAuthor(author_id, is_bot)
        self.channel = FakeChannel() if dm else FakeGuildChannel()


class FakeGuildChannel:
    """Non-DMChannel — used for guild-message rejection tests."""
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content):
        self.sent.append(content)

    def typing(self):
        return _AsyncCM()


class FakeResponse:
    def __init__(self):
        self.sent: list[tuple[str, bool]] = []

    async def send_message(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))


class FakeInteraction:
    """Stand-in for discord.Interaction in slash-command callbacks."""
    def __init__(self, user_id: int):
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()


class FakeChoice:
    """Stand-in for discord.app_commands.Choice."""
    def __init__(self, value: str, name: str | None = None):
        self.value = value
        self.name = name or value


class FakeStream:
    """Stand-in for asyncio.StreamReader. Fed lines via __init__."""
    def __init__(self, data: bytes = b""):
        self.buf = data
        self.pos = 0
        # If non-None, raise this exception ONCE on next readline,
        # then advance past `_oversize_skip` bytes on the recovery path.
        self._raise_once: BaseException | None = None
        self._oversize_skip = 0

    async def readline(self):
        if self._raise_once is not None:
            exc = self._raise_once
            self._raise_once = None
            raise exc
        if self.pos >= len(self.buf):
            return b""
        nl = self.buf.find(b"\n", self.pos)
        if nl == -1:
            chunk = self.buf[self.pos:]
            self.pos = len(self.buf)
            return chunk
        chunk = self.buf[self.pos:nl + 1]
        self.pos = nl + 1
        return chunk

    async def readexactly(self, n):
        chunk = self.buf[self.pos:self.pos + n]
        self.pos += n
        return chunk

    async def readuntil(self, sep):
        idx = self.buf.find(sep, self.pos)
        if idx == -1:
            chunk = self.buf[self.pos:]
            self.pos = len(self.buf)
            return chunk
        chunk = self.buf[self.pos:idx + len(sep)]
        self.pos = idx + len(sep)
        return chunk

    async def read(self, n: int = -1):
        if n < 0:
            chunk = self.buf[self.pos:]
            self.pos = len(self.buf)
            return chunk
        chunk = self.buf[self.pos:self.pos + n]
        self.pos += n
        return chunk


class FakeProc:
    """Stand-in for asyncio.subprocess.Process.

    `stdout_lines` is a list of byte-lines (each should end with b"\\n");
    they're concatenated and exposed via stdout. `final_rc` is the exit
    code returned from wait() unless terminate()/kill() override it.
    """
    def __init__(
        self,
        stdout_lines: list[bytes] | None = None,
        stderr_data: bytes = b"",
        final_rc: int = 0,
    ):
        self.stdout = FakeStream(b"".join(stdout_lines or []))
        self.stderr = FakeStream(stderr_data)
        self.returncode: int | None = None
        self._final_rc = final_rc
        self._terminated = False
        self._killed = False
        self._terminate_hangs = False  # set True to test kill-fallback

    async def wait(self):
        if self._terminate_hangs and not self._killed:
            # Simulate a hung process — never resolve until killed.
            import asyncio
            await asyncio.sleep(60)
        if self._killed:
            self.returncode = -9
        elif self._terminated:
            self.returncode = -15
        else:
            self.returncode = self._final_rc
        return self.returncode

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._killed = True


@pytest.fixture
def channel():
    return FakeChannel()


@pytest.fixture
def fake_proc():
    return FakeProc()
