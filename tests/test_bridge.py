"""Rigorous test suite for claude-discord bridge.

Organized by risk class:

  1. Pure-function correctness — splitter, formatters, model→window mapping,
     argv construction, settings.json reader, status/context renderers.
  2. Authorization — only ALLOWED_USER_ID can do anything; bots, guild
     messages, and other users are silently ignored.
  3. Pre-lock interception — /stop, /exit, /quit MUST work even when the
     per-user lock is held by a running turn.  This is the bridge's most
     important safety property and is locked by an explicit memory rule.
  4. Slash command callbacks — every Discord slash command rejects
     unauthorized users and mutates state correctly.
  5. Subprocess streaming — run_claude_turn handles every stream-json
     event type, the 16 MB buffer trap, oversize-event drain, JSON decode
     errors, terminate→kill fallback, and the stopped-vs-crashed
     post-mortem distinction.

Every test runs without a network and without invoking real `claude`.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import bot
from conftest import (
    FakeChoice,
    FakeInteraction,
    FakeMessage,
    FakeProc,
    FakeStream,
)


ALLOWED = int(bot.os.environ["BRIDGE_ALLOWED_USER_ID"])  # 111111111111111111
INTRUDER = ALLOWED + 1


# =============================================================================
# 1. PURE FUNCTIONS
# =============================================================================

class TestSplitAtBoundary:
    def test_empty_returns_empty_list(self):
        assert bot.split_at_boundary("") == []

    def test_whitespace_only_returns_empty_list(self):
        assert bot.split_at_boundary("   \n\n  ") == []

    def test_short_text_single_chunk(self):
        assert bot.split_at_boundary("hi") == ["hi"]

    def test_at_limit_single_chunk(self):
        text = "x" * bot.DISCORD_LIMIT
        assert bot.split_at_boundary(text) == [text]

    def test_just_over_limit_splits(self):
        text = "x" * (bot.DISCORD_LIMIT + 5)
        chunks = bot.split_at_boundary(text)
        assert len(chunks) == 2
        assert all(len(c) <= bot.DISCORD_LIMIT for c in chunks)

    def test_prefers_paragraph_boundary(self):
        # Build text where a \n\n exists in the back half of the window.
        head = "a" * 1000
        tail = "b" * 1000
        text = head + "\n\n" + tail + "c" * 200
        chunks = bot.split_at_boundary(text, max_size=1500)
        # First chunk should end at the paragraph break, not mid-word.
        assert chunks[0] == head
        assert chunks[1].startswith("b")

    def test_prefers_line_when_no_paragraph(self):
        text = "a" * 1000 + "\n" + "b" * 1500
        chunks = bot.split_at_boundary(text, max_size=1500)
        assert chunks[0] == "a" * 1000
        assert chunks[1].startswith("b")

    def test_falls_back_to_space(self):
        text = "a" * 1000 + " " + "b" * 1500
        chunks = bot.split_at_boundary(text, max_size=1500)
        assert chunks[0] == "a" * 1000
        assert chunks[1].startswith("b")

    def test_no_boundary_hard_cuts_at_limit(self):
        # No spaces, no newlines — must hard-cut at max_size.
        text = "x" * 5000
        chunks = bot.split_at_boundary(text, max_size=1900)
        assert all(len(c) <= 1900 for c in chunks)
        assert "".join(chunks) == text  # nothing dropped

    def test_chunks_never_exceed_limit(self):
        # Adversarial input — long word at the wrong boundary.
        text = ("paragraph one.\n\n"
                + "a" * 3000
                + "\n\nparagraph three.")
        chunks = bot.split_at_boundary(text, max_size=1900)
        for c in chunks:
            assert len(c) <= 1900, f"chunk {c[:30]!r} exceeded limit"

    def test_skips_empty_chunks(self):
        # text fits in one chunk → splitter takes the short-text fast path,
        # which returns the input verbatim. The empty-chunk filter only
        # kicks in when the splitter actually loops.
        text = "\n\n\n\nhello\n\n\n\n"
        chunks = bot.split_at_boundary(text)
        assert chunks == [text]

    def test_no_pure_whitespace_chunks_when_split(self):
        # Construct a 3000-char text with a paragraph break so the splitter
        # actually loops, and ensure no resulting chunk is pure whitespace.
        text = "a" * 1500 + "\n\n" + "   \n\n   " + "b" * 1500
        chunks = bot.split_at_boundary(text, max_size=1900)
        assert all(c.strip() for c in chunks)


class TestFormatToolCall:
    def test_bash_first_line_truncated(self):
        out = bot.format_tool_call("Bash", {"command": "ls -la\nfoo\nbar"})
        assert out.startswith("$ ")
        assert "\n" not in out
        assert "ls -la" in out

    def test_bash_long_command_truncated_to_80(self):
        out = bot.format_tool_call("Bash", {"command": "x" * 200})
        # `$ ` prefix + up to 80 chars
        assert len(out) <= 82

    def test_bash_empty_command(self):
        # Regression — empty Bash command must not crash.
        out = bot.format_tool_call("Bash", {"command": ""})
        assert out == "$ "

    def test_bash_whitespace_only_command(self):
        out = bot.format_tool_call("Bash", {"command": "   \n  "})
        assert out == "$ "

    def test_bash_no_command_key(self):
        out = bot.format_tool_call("Bash", {})
        assert out == "$ "

    def test_read_uses_file_path(self):
        out = bot.format_tool_call("Read", {"file_path": "/foo/bar.py"})
        assert out == "Read(/foo/bar.py)"

    def test_read_falls_back_to_path(self):
        out = bot.format_tool_call("Read", {"path": "/x.py"})
        assert out == "Read(/x.py)"

    def test_edit_and_write(self):
        assert bot.format_tool_call("Edit", {"file_path": "a"}) == "Edit(a)"
        assert bot.format_tool_call("Write", {"file_path": "b"}) == "Write(b)"

    def test_glob_grep_pattern(self):
        assert bot.format_tool_call("Glob", {"pattern": "*.py"}) == "Glob(*.py)"
        assert bot.format_tool_call("Grep", {"pattern": "TODO"}) == "Grep(TODO)"

    def test_webfetch_url_truncated(self):
        long = "https://example.com/" + "a" * 200
        out = bot.format_tool_call("WebFetch", {"url": long})
        assert out.startswith("WebFetch(")
        assert len(out) <= len("WebFetch(") + 60 + 1

    def test_unknown_tool_falls_back(self):
        assert bot.format_tool_call("MysteryTool", {"x": 1}) == "MysteryTool(...)"


class TestContextWindowFor:
    def test_known_model(self):
        assert bot._context_window_for("claude-opus-4-7") == 200_000

    def test_unset_returns_default(self):
        assert bot._context_window_for(None) == bot.DEFAULT_CONTEXT_WINDOW

    def test_alias_opus(self):
        assert bot._context_window_for("opus") == 200_000

    def test_alias_sonnet(self):
        assert bot._context_window_for("sonnet") == 200_000

    def test_alias_haiku(self):
        assert bot._context_window_for("haiku") == 200_000

    def test_unknown_falls_back_to_default(self):
        assert bot._context_window_for("totally-fake-model-xyz") == bot.DEFAULT_CONTEXT_WINDOW


class TestClaudeArgs:
    def test_basic_args(self):
        bot.state.session_id = None
        bot.state.model = None
        bot.state.effort = None
        args = bot.state.claude_args("hi")
        assert args[0] == bot.CLAUDE_BIN
        assert "--print" in args
        assert "--output-format" in args
        assert "stream-json" in args
        assert "--verbose" in args
        assert args[-1] == "hi"
        assert "--resume" not in args
        assert "--model" not in args
        assert "--effort" not in args

    def test_with_session_id_includes_resume(self):
        bot.state.session_id = "sess-abc"
        args = bot.state.claude_args("p")
        assert "--resume" in args
        assert "sess-abc" in args

    def test_with_model_and_effort(self):
        bot.state.model = "opus"
        bot.state.effort = "high"
        args = bot.state.claude_args("p")
        assert "--model" in args and "opus" in args
        assert "--effort" in args and "high" in args

    def test_permission_mode_threaded(self):
        bot.state.permission_mode = "default"
        args = bot.state.claude_args("p")
        i = args.index("--permission-mode")
        assert args[i + 1] == "default"

    def test_prompt_is_last_arg(self):
        bot.state.session_id = "abc"
        bot.state.model = "sonnet"
        bot.state.effort = "low"
        args = bot.state.claude_args("the prompt")
        assert args[-1] == "the prompt"


class TestReadClaudeDefaults:
    def test_missing_file_returns_none_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
        assert bot._read_claude_defaults() == (None, None)

    def test_invalid_json_returns_none_none(self, monkeypatch, tmp_path):
        d = tmp_path / ".claude"
        d.mkdir()
        (d / "settings.json").write_text("{not json")
        monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
        assert bot._read_claude_defaults() == (None, None)

    def test_valid_json_returns_values(self, monkeypatch, tmp_path):
        d = tmp_path / ".claude"
        d.mkdir()
        (d / "settings.json").write_text(json.dumps({
            "model": "opus", "effortLevel": "high"
        }))
        monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
        assert bot._read_claude_defaults() == ("opus", "high")

    def test_missing_keys_return_none(self, monkeypatch, tmp_path):
        d = tmp_path / ".claude"
        d.mkdir()
        (d / "settings.json").write_text(json.dumps({"theme": "dark"}))
        monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
        assert bot._read_claude_defaults() == (None, None)


class TestContextSummaryAndBlock:
    def test_summary_no_usage(self):
        bot.state.last_usage = None
        msg, pct = bot._context_summary()
        assert "no usage" in msg.lower()
        assert pct is None

    def test_summary_computes_percent(self):
        bot.state.last_usage = {
            "input_tokens": 50_000,
            "cache_creation_input_tokens": 10_000,
            "cache_read_input_tokens": 40_000,
            "output_tokens": 1_000,
        }
        bot.state.last_active_model = "claude-opus-4-7"
        msg, pct = bot._context_summary()
        assert "100,000" in msg
        assert "200,000" in msg
        assert pct == pytest.approx(50.0)

    def test_block_no_usage(self):
        bot.state.last_usage = None
        out = bot._context_block()
        assert "no usage" in out.lower()

    def test_block_renders_breakdown(self):
        bot.state.last_usage = {
            "input_tokens": 1000,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 3000,
            "output_tokens": 500,
        }
        bot.state.last_active_model = "claude-sonnet-4-6"
        out = bot._context_block()
        assert "claude-sonnet-4-6" in out
        assert "1,000" in out
        assert "2,000" in out
        assert "3,000" in out
        assert "500" in out


class TestStatusBlock:
    def test_renders_unset_state(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
        bot.state.session_id = None
        bot.state.model = None
        bot.state.effort = None
        out = bot._status_block()
        assert "(none" in out  # session none
        assert "unset" in out

    def test_renders_explicit_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
        bot.state.session_id = "sess-xyz"
        bot.state.model = "opus"
        bot.state.effort = "max"
        out = bot._status_block()
        assert "sess-xyz" in out
        assert "opus" in out
        assert "max" in out

    def test_auto_off_message(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
        bot.state.permission_mode = "default"
        out = bot._status_block()
        assert "OFF" in out

    def test_running_yes_when_proc_alive(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
        proc = FakeProc()
        proc.returncode = None
        bot.state.current_proc = proc
        out = bot._status_block()
        assert "yes" in out

    def test_running_no_when_proc_done(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot.Path, "home", lambda: tmp_path)
        proc = FakeProc()
        proc.returncode = 0
        bot.state.current_proc = proc
        out = bot._status_block()
        # The line should say `running:    no`
        assert "running:    no" in out


class TestIsAuthorized:
    def test_allowed_user_passes(self):
        assert bot._is_authorized(ALLOWED) is True

    def test_other_user_blocked(self):
        assert bot._is_authorized(INTRUDER) is False

    def test_zero_blocked(self):
        assert bot._is_authorized(0) is False


# =============================================================================
# 2. AUTHORIZATION — slash commands
# =============================================================================

class TestSlashCommandAuth:
    """Every slash command must reject unauthorized users WITHOUT mutating state."""

    async def test_new_blocks_intruder(self):
        bot.state.session_id = "preserved"
        i = FakeInteraction(INTRUDER)
        await bot.slash_new.callback(i)
        assert i.response.sent[0][0] == "not authorized"
        assert i.response.sent[0][1] is True  # ephemeral
        assert bot.state.session_id == "preserved"

    async def test_auto_blocks_intruder(self):
        bot.state.permission_mode = "default"
        i = FakeInteraction(INTRUDER)
        await bot.slash_auto.callback(i, FakeChoice("on"))
        assert i.response.sent[0][0] == "not authorized"
        assert bot.state.permission_mode == "default"  # unchanged

    async def test_model_blocks_intruder(self):
        bot.state.model = None
        i = FakeInteraction(INTRUDER)
        await bot.slash_model.callback(i, FakeChoice("opus"))
        assert i.response.sent[0][0] == "not authorized"
        assert bot.state.model is None

    async def test_effort_blocks_intruder(self):
        i = FakeInteraction(INTRUDER)
        await bot.slash_effort.callback(i, FakeChoice("high"))
        assert i.response.sent[0][0] == "not authorized"
        assert bot.state.effort is None

    async def test_tools_blocks_intruder(self):
        i = FakeInteraction(INTRUDER)
        await bot.slash_tools.callback(i, FakeChoice("on"))
        assert i.response.sent[0][0] == "not authorized"
        assert bot.state.show_tools is False

    async def test_status_blocks_intruder(self):
        i = FakeInteraction(INTRUDER)
        await bot.slash_status.callback(i)
        assert i.response.sent[0][0] == "not authorized"

    async def test_context_blocks_intruder(self):
        i = FakeInteraction(INTRUDER)
        await bot.slash_context.callback(i)
        assert i.response.sent[0][0] == "not authorized"

    async def test_stop_blocks_intruder(self):
        # Even with a live proc, intruder cannot stop it.
        proc = FakeProc()
        proc.returncode = None
        bot.state.current_proc = proc
        i = FakeInteraction(INTRUDER)
        await bot.slash_stop.callback(i)
        assert i.response.sent[0][0] == "not authorized"
        assert bot.state.current_proc is proc  # not terminated

    async def test_help_blocks_intruder(self):
        i = FakeInteraction(INTRUDER)
        await bot.slash_help.callback(i)
        assert i.response.sent[0][0] == "not authorized"


class TestSlashCommandHappyPath:
    async def test_new_clears_session(self):
        bot.state.session_id = "old"
        i = FakeInteraction(ALLOWED)
        await bot.slash_new.callback(i)
        assert bot.state.session_id is None
        assert "fresh" in i.response.sent[0][0].lower()

    async def test_auto_on_off(self):
        i = FakeInteraction(ALLOWED)
        await bot.slash_auto.callback(i, FakeChoice("off"))
        assert bot.state.permission_mode == "default"
        await bot.slash_auto.callback(i, FakeChoice("on"))
        assert bot.state.permission_mode == "bypassPermissions"

    async def test_model_set_and_clear(self):
        i = FakeInteraction(ALLOWED)
        await bot.slash_model.callback(i, FakeChoice("opus"))
        assert bot.state.model == "opus"
        await bot.slash_model.callback(i, FakeChoice(""))  # default
        assert bot.state.model is None

    async def test_effort_set_and_clear(self):
        i = FakeInteraction(ALLOWED)
        await bot.slash_effort.callback(i, FakeChoice("max"))
        assert bot.state.effort == "max"
        await bot.slash_effort.callback(i, FakeChoice(""))
        assert bot.state.effort is None

    async def test_tools_toggle(self):
        i = FakeInteraction(ALLOWED)
        await bot.slash_tools.callback(i, FakeChoice("on"))
        assert bot.state.show_tools is True
        await bot.slash_tools.callback(i, FakeChoice("off"))
        assert bot.state.show_tools is False

    async def test_status_happy_path(self):
        i = FakeInteraction(ALLOWED)
        await bot.slash_status.callback(i)
        assert i.response.sent
        assert i.response.sent[0][1] is True  # ephemeral
        assert "session:" in i.response.sent[0][0]

    async def test_context_happy_path(self):
        i = FakeInteraction(ALLOWED)
        await bot.slash_context.callback(i)
        assert i.response.sent

    async def test_help_happy_path(self):
        i = FakeInteraction(ALLOWED)
        await bot.slash_help.callback(i)
        assert "claude-discord bridge" in i.response.sent[0][0]

    async def test_stop_happy_path_no_proc(self):
        bot.state.current_proc = None
        i = FakeInteraction(ALLOWED)
        await bot.slash_stop.callback(i)
        assert "no turn" in i.response.sent[0][0].lower()


# =============================================================================
# 3. PRE-LOCK INTERCEPTION — the critical safety property
# =============================================================================

class TestOnMessageGating:
    """on_message must drop bots, guild messages, and non-allowed users
    BEFORE any other processing."""

    async def test_drops_bot_messages(self):
        msg = FakeMessage("/new", ALLOWED, is_bot=True)
        bot.state.session_id = "preserved"
        await bot.on_message(msg)
        assert msg.channel.sent == []
        assert bot.state.session_id == "preserved"

    async def test_drops_guild_messages(self):
        msg = FakeMessage("/new", ALLOWED, dm=False)
        bot.state.session_id = "preserved"
        await bot.on_message(msg)
        assert msg.channel.sent == []
        assert bot.state.session_id == "preserved"

    async def test_drops_intruder(self):
        msg = FakeMessage("/new", INTRUDER)
        bot.state.session_id = "preserved"
        await bot.on_message(msg)
        assert msg.channel.sent == []
        assert bot.state.session_id == "preserved"


class TestPreLockInterceptors:
    """/stop, /exit, /quit MUST work even when the per-user lock is held.

    This is the bridge's single most important safety property.  If the lock
    gates these, the user has no way to abort a runaway turn — the whole
    point of /stop.  And /exit/quit must be no-ops (memory rule, locked).

    These tests acquire the lock, fire the message, and use a tight timeout
    so a regression deadlocks the test instead of silently passing.
    """

    async def test_stop_works_while_lock_held(self):
        await bot._lock.acquire()
        try:
            msg = FakeMessage("/stop", ALLOWED)
            await asyncio.wait_for(bot.on_message(msg), timeout=2.0)
            assert len(msg.channel.sent) == 1
            assert "no turn running" in msg.channel.sent[0]
        finally:
            bot._lock.release()

    async def test_stop_terminates_live_proc_while_lock_held(self):
        proc = FakeProc()
        proc.returncode = None
        bot.state.current_proc = proc
        await bot._lock.acquire()
        try:
            msg = FakeMessage("/stop", ALLOWED)
            await asyncio.wait_for(bot.on_message(msg), timeout=5.0)
            assert proc._terminated is True
            assert "stopped" in msg.channel.sent[0]
        finally:
            bot._lock.release()

    async def test_bang_stop_also_intercepted(self):
        await bot._lock.acquire()
        try:
            msg = FakeMessage("!stop", ALLOWED)
            await asyncio.wait_for(bot.on_message(msg), timeout=2.0)
            assert len(msg.channel.sent) == 1
        finally:
            bot._lock.release()

    async def test_exit_is_noop_while_lock_held(self):
        await bot._lock.acquire()
        try:
            msg = FakeMessage("/exit", ALLOWED)
            await asyncio.wait_for(bot.on_message(msg), timeout=2.0)
            assert len(msg.channel.sent) == 1
            assert "no-op" in msg.channel.sent[0].lower()
        finally:
            bot._lock.release()

    async def test_quit_is_noop_while_lock_held(self):
        await bot._lock.acquire()
        try:
            msg = FakeMessage("/quit", ALLOWED)
            await asyncio.wait_for(bot.on_message(msg), timeout=2.0)
            assert "no-op" in msg.channel.sent[0].lower()
        finally:
            bot._lock.release()

    async def test_bang_exit_also_noop(self):
        msg = FakeMessage("!exit", ALLOWED)
        await bot.on_message(msg)
        assert "no-op" in msg.channel.sent[0].lower()

    async def test_bang_quit_also_noop(self):
        msg = FakeMessage("!quit", ALLOWED)
        await bot.on_message(msg)
        assert "no-op" in msg.channel.sent[0].lower()

    async def test_exit_does_not_reach_run_claude_turn(self, monkeypatch):
        called = False

        async def boom(*a, **kw):
            nonlocal called
            called = True

        monkeypatch.setattr(bot, "run_claude_turn", boom)
        msg = FakeMessage("/exit", ALLOWED)
        await bot.on_message(msg)
        assert called is False

    async def test_exit_with_args_still_noop(self):
        # "/exit now please" should still no-op.
        msg = FakeMessage("/exit now please", ALLOWED)
        await bot.on_message(msg)
        assert "no-op" in msg.channel.sent[0].lower()

    async def test_exit_uppercase_blocked(self):
        msg = FakeMessage("/EXIT", ALLOWED)
        await bot.on_message(msg)
        assert "no-op" in msg.channel.sent[0].lower()


# =============================================================================
# 4. _handle command routing (post-lock, plain DMs)
# =============================================================================

class TestHandleRouting:
    async def test_help_routed(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot._handle(ch, "/help")
        assert "claude-discord bridge" in ch.sent[0]

    async def test_status_routed(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot._handle(ch, "/status")
        assert "session:" in ch.sent[0]

    async def test_context_routed(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot._handle(ch, "/context")
        assert ch.sent  # something was sent

    async def test_new_clears_session(self):
        from conftest import FakeChannel
        bot.state.session_id = "old"
        ch = FakeChannel()
        await bot._handle(ch, "/new")
        assert bot.state.session_id is None

    async def test_bang_form_normalized(self):
        from conftest import FakeChannel
        bot.state.session_id = "old"
        ch = FakeChannel()
        await bot._handle(ch, "!new")
        assert bot.state.session_id is None

    async def test_auto_on_off(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot._handle(ch, "/auto off")
        assert bot.state.permission_mode == "default"
        await bot._handle(ch, "/auto on")
        assert bot.state.permission_mode == "bypassPermissions"

    async def test_auto_default_is_on(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        bot.state.permission_mode = "default"
        await bot._handle(ch, "/auto")
        assert bot.state.permission_mode == "bypassPermissions"

    async def test_auto_bad_arg_shows_usage(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot._handle(ch, "/auto banana")
        assert "usage" in ch.sent[0].lower()

    async def test_model_set(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot._handle(ch, "/model opus")
        assert bot.state.model == "opus"

    async def test_model_clear(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        bot.state.model = "opus"
        await bot._handle(ch, "/model")
        assert bot.state.model is None

    async def test_effort_validates(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot._handle(ch, "/effort bogus")
        assert "must be" in ch.sent[0].lower()
        assert bot.state.effort is None

    async def test_effort_accepts_valid_levels(self):
        from conftest import FakeChannel
        for level in ("low", "medium", "high", "xhigh", "max"):
            bot.state.effort = None
            ch = FakeChannel()
            await bot._handle(ch, f"/effort {level}")
            assert bot.state.effort == level

    async def test_tools_toggle(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot._handle(ch, "/tools on")
        assert bot.state.show_tools is True
        await bot._handle(ch, "/tools off")
        assert bot.state.show_tools is False

    async def test_empty_input_no_op(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot._handle(ch, "   ")
        assert ch.sent == []

    async def test_unknown_slash_falls_through_to_claude(self, monkeypatch):
        """Claude Code skill commands like /init or /review pass through."""
        from conftest import FakeChannel
        called_with = []

        async def fake_run(channel, prompt):
            called_with.append(prompt)

        monkeypatch.setattr(bot, "run_claude_turn", fake_run)
        ch = FakeChannel()
        await bot._handle(ch, "/init my project")
        assert called_with == ["/init my project"]

    async def test_plain_text_falls_through_to_claude(self, monkeypatch):
        from conftest import FakeChannel
        called_with = []

        async def fake_run(channel, prompt):
            called_with.append(prompt)

        monkeypatch.setattr(bot, "run_claude_turn", fake_run)
        ch = FakeChannel()
        await bot._handle(ch, "what is the meaning of life")
        assert called_with == ["what is the meaning of life"]


# =============================================================================
# 5. _do_stop semantics
# =============================================================================

class TestDoStop:
    async def test_no_proc_returns_friendly_msg(self):
        bot.state.current_proc = None
        msg = await bot._do_stop()
        assert "no turn" in msg.lower()

    async def test_finished_proc_returns_friendly_msg(self):
        proc = FakeProc()
        proc.returncode = 0
        bot.state.current_proc = proc
        msg = await bot._do_stop()
        assert "no turn" in msg.lower()

    async def test_live_proc_terminated_cleanly(self):
        proc = FakeProc()
        proc.returncode = None
        bot.state.current_proc = proc
        msg = await bot._do_stop()
        assert proc._terminated is True
        assert "stopped" in msg.lower()
        assert proc.returncode == -15

    async def test_terminate_hangs_falls_back_to_kill(self):
        proc = FakeProc()
        proc.returncode = None
        proc._terminate_hangs = True
        bot.state.current_proc = proc
        msg = await bot._do_stop()
        assert proc._killed is True
        assert "stopped" in msg.lower()

    async def test_process_lookup_handled(self):
        class GhostProc(FakeProc):
            def terminate(self):
                raise ProcessLookupError("gone")

        proc = GhostProc()
        proc.returncode = None
        bot.state.current_proc = proc
        msg = await bot._do_stop()
        assert "already finished" in msg.lower()


# =============================================================================
# 6. send_text pacing
# =============================================================================

class TestSendText:
    async def test_single_chunk_no_sleep(self, monkeypatch):
        from conftest import FakeChannel
        sleeps = []

        async def fake_sleep(t):
            sleeps.append(t)

        monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)
        ch = FakeChannel()
        await bot.send_text(ch, "short")
        assert sleeps == []
        assert ch.sent == ["short"]

    async def test_multi_chunk_paces_between(self, monkeypatch):
        from conftest import FakeChannel
        sleeps = []

        async def fake_sleep(t):
            sleeps.append(t)

        monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)
        ch = FakeChannel()
        # Force 3 chunks via long text without breaks.
        text = "a" * 5000
        await bot.send_text(ch, text)
        assert len(ch.sent) >= 2
        # One sleep per gap → len(chunks) - 1 sleeps.
        assert len(sleeps) == len(ch.sent) - 1
        assert all(s == 0.4 for s in sleeps)

    async def test_empty_text_sends_nothing(self):
        from conftest import FakeChannel
        ch = FakeChannel()
        await bot.send_text(ch, "")
        assert ch.sent == []


# =============================================================================
# 7. run_claude_turn — subprocess streaming
# =============================================================================

def make_event(d: dict) -> bytes:
    return (json.dumps(d) + "\n").encode()


class TestRunClaudeTurn:
    async def _patch_subprocess(self, monkeypatch, proc):
        async def fake_create(*args, **kwargs):
            return proc
        monkeypatch.setattr(bot.asyncio, "create_subprocess_exec", fake_create)

    async def test_streams_assistant_text(self, monkeypatch):
        from conftest import FakeChannel
        events = [
            make_event({"type": "system", "subtype": "init",
                        "session_id": "s1", "model": "claude-opus-4-7"}),
            make_event({"type": "assistant", "message": {
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "hello world"}],
                "usage": {"input_tokens": 100, "output_tokens": 50,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0},
            }}),
            make_event({"type": "result", "session_id": "s1", "is_error": False}),
        ]
        proc = FakeProc(stdout_lines=events)
        await self._patch_subprocess(monkeypatch, proc)

        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        assert "hello world" in ch.sent[0]
        assert bot.state.session_id == "s1"
        assert bot.state.last_active_model == "claude-opus-4-7"
        assert bot.state.last_usage["input_tokens"] == 100

    async def test_tool_calls_hidden_when_show_tools_off(self, monkeypatch):
        from conftest import FakeChannel
        bot.state.show_tools = False
        events = [
            make_event({"type": "assistant", "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/foo"}},
                ],
            }}),
            make_event({"type": "result", "session_id": "s", "is_error": False}),
        ]
        proc = FakeProc(stdout_lines=events)
        await self._patch_subprocess(monkeypatch, proc)
        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        assert all("Read" not in m for m in ch.sent)

    async def test_tool_calls_shown_when_show_tools_on(self, monkeypatch):
        from conftest import FakeChannel
        bot.state.show_tools = True
        events = [
            make_event({"type": "assistant", "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls /tmp"}},
                ],
            }}),
            make_event({"type": "result", "session_id": "s", "is_error": False}),
        ]
        proc = FakeProc(stdout_lines=events)
        await self._patch_subprocess(monkeypatch, proc)
        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        assert any("ls /tmp" in m for m in ch.sent)

    async def test_invalid_json_silently_skipped(self, monkeypatch):
        from conftest import FakeChannel
        events = [
            b"this is not json at all\n",
            make_event({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "ok"}],
            }}),
            make_event({"type": "result", "session_id": "s", "is_error": False}),
        ]
        proc = FakeProc(stdout_lines=events)
        await self._patch_subprocess(monkeypatch, proc)
        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        assert any("ok" in m for m in ch.sent)

    async def test_session_id_persisted_from_result(self, monkeypatch):
        from conftest import FakeChannel
        events = [
            make_event({"type": "result", "session_id": "from-result", "is_error": False}),
        ]
        proc = FakeProc(stdout_lines=events)
        await self._patch_subprocess(monkeypatch, proc)
        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        assert bot.state.session_id == "from-result"

    async def test_error_payload_emitted(self, monkeypatch):
        from conftest import FakeChannel
        events = [
            make_event({"type": "result", "session_id": "s", "is_error": True,
                        "result": "rate limit hit"}),
        ]
        proc = FakeProc(stdout_lines=events)
        await self._patch_subprocess(monkeypatch, proc)
        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        assert any("rate limit hit" in m for m in ch.sent)

    async def test_nonzero_exit_emits_stderr_tail(self, monkeypatch):
        from conftest import FakeChannel
        # No result event, non-zero exit.
        proc = FakeProc(
            stdout_lines=[],
            stderr_data=b"line1\nline2\nfinal error\n",
            final_rc=2,
        )
        await self._patch_subprocess(monkeypatch, proc)
        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        joined = " ".join(ch.sent)
        assert "code 2" in joined
        assert "final error" in joined

    async def test_stopped_proc_suppresses_error(self, monkeypatch):
        from conftest import FakeChannel
        # Negative returncode = signal-killed = /stop. Should NOT emit
        # the "exited with code -15" warning.
        proc = FakeProc(stdout_lines=[], final_rc=-15)
        proc._terminated = True  # so wait() returns -15
        await self._patch_subprocess(monkeypatch, proc)
        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        assert all("exited" not in m for m in ch.sent)

    async def test_oversize_event_drained_warning_sent(self, monkeypatch):
        """LimitOverrunError on readline → drain past newline → keep going."""
        from conftest import FakeChannel

        class OversizeStream(FakeStream):
            def __init__(self, after_event: bytes):
                # Buffer holds the rest of the oversized line + the next event.
                super().__init__(b"X" * 100 + b"\n" + after_event)
                self._raised = False

            async def readline(self):
                if not self._raised:
                    self._raised = True
                    # Pretend we already consumed 50 bytes of the partial line.
                    raise asyncio.LimitOverrunError("too long", 50)
                return await super().readline()

        next_event = make_event({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "after oversize"}],
        }})
        proc = FakeProc(stdout_lines=[])
        proc.stdout = OversizeStream(next_event)
        await self._patch_subprocess(monkeypatch, proc)

        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        assert any("skipped" in m and "MB" in m for m in ch.sent)
        assert any("after oversize" in m for m in ch.sent)

    async def test_proc_assigned_to_state_during_run(self, monkeypatch):
        """state.current_proc must be set so /stop can find it mid-stream."""
        from conftest import FakeChannel
        observed = []

        events = [
            make_event({"type": "result", "session_id": "s", "is_error": False}),
        ]
        proc = FakeProc(stdout_lines=events)
        await self._patch_subprocess(monkeypatch, proc)

        # Patch send to peek at state.current_proc mid-turn.
        ch = FakeChannel()
        orig_send = ch.send

        async def peek(content):
            observed.append(bot.state.current_proc)
            await orig_send(content)

        ch.send = peek

        await bot.run_claude_turn(ch, "hi")
        # After turn ends, current_proc is cleared.
        assert bot.state.current_proc is None

    async def test_oversize_event_drain_swallows_recovery_error(self, monkeypatch):
        """If the recovery readuntil itself raises, run_claude_turn must keep going."""
        from conftest import FakeChannel

        class HostileStream(FakeStream):
            def __init__(self):
                super().__init__(b"")
                self._raised = False

            async def readline(self):
                if not self._raised:
                    self._raised = True
                    raise asyncio.LimitOverrunError("too long", 10)
                return b""  # EOF after recovery

            async def readexactly(self, n):
                return b"x" * n

            async def readuntil(self, sep):
                raise asyncio.IncompleteReadError(b"", expected=None)

        proc = FakeProc(stdout_lines=[])
        proc.stdout = HostileStream()
        await self._patch_subprocess(monkeypatch, proc)
        ch = FakeChannel()
        # Must not raise.
        await bot.run_claude_turn(ch, "hi")
        assert any("skipped" in m for m in ch.sent)

    async def test_stderr_read_timeout_does_not_hang(self, monkeypatch):
        """If proc.stderr.read() hangs, the 2s timeout breaks us out."""
        from conftest import FakeChannel

        class HangingStderr(FakeStream):
            async def read(self, n=-1):
                await asyncio.sleep(60)

        proc = FakeProc(stdout_lines=[], final_rc=0)
        proc.stderr = HangingStderr()
        await self._patch_subprocess(monkeypatch, proc)
        # Patch wait_for to reduce the 2s production timeout to 0.05s.
        orig_wait_for = asyncio.wait_for

        async def fast_wait(coro, timeout):
            return await orig_wait_for(coro, timeout=0.05)

        monkeypatch.setattr(bot.asyncio, "wait_for", fast_wait)
        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        # Reaches end without hanging — that's the assertion.

    async def test_long_assistant_text_chunked(self, monkeypatch):
        from conftest import FakeChannel
        long = "a" * 5000
        events = [
            make_event({"type": "assistant", "message": {
                "content": [{"type": "text", "text": long}],
            }}),
            make_event({"type": "result", "session_id": "s", "is_error": False}),
        ]
        proc = FakeProc(stdout_lines=events)
        await self._patch_subprocess(monkeypatch, proc)
        ch = FakeChannel()
        await bot.run_claude_turn(ch, "hi")
        # Should have been split.
        assert len(ch.sent) >= 2
        assert all(len(c) <= bot.DISCORD_LIMIT for c in ch.sent)


# =============================================================================
# 8. End-to-end on_message → claude flow
# =============================================================================

class TestOnMessageE2E:
    async def test_plain_dm_invokes_claude(self, monkeypatch):
        called = []

        async def fake_run(channel, prompt):
            called.append(prompt)

        monkeypatch.setattr(bot, "run_claude_turn", fake_run)

        msg = FakeMessage("hello claude", ALLOWED)
        await bot.on_message(msg)
        assert called == ["hello claude"]

    async def test_handle_exception_surfaced_to_user(self, monkeypatch):
        async def boom(channel, prompt):
            raise RuntimeError("simulated")

        monkeypatch.setattr(bot, "run_claude_turn", boom)
        msg = FakeMessage("hi", ALLOWED)
        with pytest.raises(RuntimeError):
            await bot.on_message(msg)
        # Error message echoed to the channel BEFORE the re-raise.
        assert any("bridge error" in m and "RuntimeError" in m for m in msg.channel.sent)

    async def test_lock_released_after_handle(self, monkeypatch):
        async def fake_run(channel, prompt):
            pass

        monkeypatch.setattr(bot, "run_claude_turn", fake_run)
        msg = FakeMessage("hi", ALLOWED)
        await bot.on_message(msg)
        assert not bot._lock.locked()
