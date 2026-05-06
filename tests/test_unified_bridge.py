from __future__ import annotations

import os
from pathlib import Path

import pytest

import unified_bridge as ub


class AsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Channel:
    def __init__(self, channel_id: int = 555):
        self.id = channel_id
        self.sent = []
        self.files = []

    async def send(self, content=None, *, file=None):
        if file is not None:
            self.files.append(file)
        else:
            self.sent.append(content)

    def typing(self):
        return AsyncCM()


class Attachment:
    def __init__(self, filename: str, data: bytes = b"x"):
        self.filename = filename
        self.data = data

    async def save(self, path):
        Path(path).write_bytes(self.data)


class Message:
    def __init__(self, msg_id=123, attachments=None):
        self.id = msg_id
        self.attachments = attachments or []


def clear_agent_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("CLAUDETTE_", "LUNA_")) or key in {"BRIDGE_AGENTS", "BRIDGE_ATTACHMENT_DIR"}:
            monkeypatch.delenv(key, raising=False)


class TestConfig:
    def test_loads_multiple_agents_from_prefixed_env(self, monkeypatch, tmp_path):
        clear_agent_env(monkeypatch)
        monkeypatch.setenv("BRIDGE_AGENTS", "claudette,luna")
        monkeypatch.setenv("BRIDGE_ATTACHMENT_DIR", str(tmp_path / "attachments"))
        monkeypatch.setenv("CLAUDETTE_TOKEN", "claude-token")
        monkeypatch.setenv("CLAUDETTE_BACKEND", "claude")
        monkeypatch.setenv("CLAUDETTE_ALLOWED_USER_ID", "111")
        monkeypatch.setenv("CLAUDETTE_WORKDIR", str(tmp_path))
        monkeypatch.setenv("CLAUDETTE_CLAUDE_BIN", "/usr/bin/true")
        monkeypatch.setenv("LUNA_TOKEN", "codex-token")
        monkeypatch.setenv("LUNA_BACKEND", "codex")
        monkeypatch.setenv("LUNA_ALLOWED_USER_ID", "222")
        monkeypatch.setenv("LUNA_WORKDIR", str(tmp_path))
        monkeypatch.setenv("LUNA_CODEX_BIN", "/usr/bin/true")
        monkeypatch.setenv("LUNA_DEFAULT_SEARCH", "on")

        configs = ub.load_agent_configs()

        assert [(c.name, c.backend, c.allowed_user_id) for c in configs] == [
            ("claudette", "claude", 111),
            ("luna", "codex", 222),
        ]
        assert all(c.attachment_dir == tmp_path / "attachments" for c in configs)
        assert configs[1].default_search is True

    def test_missing_private_ids_are_not_defaulted(self, monkeypatch):
        clear_agent_env(monkeypatch)
        monkeypatch.setenv("BRIDGE_AGENTS", "claudette")
        monkeypatch.setenv("CLAUDETTE_TOKEN", "token")
        monkeypatch.setenv("CLAUDETTE_BACKEND", "claude")
        monkeypatch.setenv("CLAUDETTE_CLAUDE_BIN", "/usr/bin/true")

        with pytest.raises(ValueError, match="CLAUDETTE_ALLOWED_USER_ID"):
            ub.load_agent_configs()


class TestAttachmentsAndFiles:
    async def test_shared_attachment_store_saves_safe_names(self, tmp_path):
        store = ub.AttachmentStore(tmp_path)
        paths = await store.save("claudette", Message(9, [Attachment("bad name.png"), Attachment("notes.md")]))

        assert [p.name for p in paths] == ["01-bad_name.png", "02-notes.md"]
        assert all(p.exists() for p in paths)
        assert paths[0].parent == tmp_path / "claudette" / "9"

    def test_splits_images_from_non_images_and_builds_prompt(self, tmp_path):
        image = tmp_path / "a.PNG"
        doc = tmp_path / "b.pdf"
        image.write_bytes(b"img")
        doc.write_bytes(b"doc")

        images, files = ub.split_attachment_paths([image, doc])
        prompt = ub.build_prompt("read these", files)

        assert images == [image]
        assert files == [doc]
        assert str(doc) in prompt
        assert str(image) not in prompt

    async def test_send_text_uploads_media_and_file_lines(self, tmp_path):
        out = tmp_path / "out.txt"
        out.write_text("hello")
        ch = Channel()

        await ub.send_text(ch, f"here\nFILE:{out}\nmissing\nMEDIA:{tmp_path / 'nope.png'}")

        assert ch.files, "send_text should upload valid FILE:/MEDIA: paths"
        assert "here" in ch.sent[0]
        assert any("not found" in msg for msg in ch.sent)


class TestRunnerArgsAndRouting:
    def config(self, tmp_path, backend="codex"):
        return ub.AgentConfig(
            name="luna" if backend == "codex" else "claudette",
            prefix="LUNA" if backend == "codex" else "CLAUDETTE",
            token="token",
            backend=backend,
            allowed_user_id=111,
            workdir=tmp_path,
            attachment_dir=tmp_path / "attachments",
            codex_bin="/usr/bin/true",
            claude_bin="/usr/bin/true",
            aliases={"luna" if backend == "codex" else "claudette"},
        )

    def test_codex_uses_image_args_and_non_image_prompt(self, tmp_path):
        runner = ub.CodexRunner(self.config(tmp_path, "codex"))
        image = tmp_path / "img.png"
        doc = tmp_path / "doc.md"
        prompt = ub.build_prompt("inspect", [doc])
        args = runner.codex_args(prompt, image_paths=[image])

        assert "--image" in args and str(image) in args
        assert any(str(doc) in arg for arg in args)
        assert args.index("--image") > args.index(prompt)
        assert args[:3] == ["/usr/bin/true", "exec", "--json"]

    def test_claude_uses_add_dir_and_resume(self, tmp_path):
        runner = ub.ClaudeRunner(self.config(tmp_path, "claude"))
        runner._session_ids[42] = "sess-1"
        args = runner.claude_args("hello", channel_id=42)

        assert "--resume" in args and "sess-1" in args
        assert f"--add-dir={tmp_path / 'attachments'}" in args
        assert args[-1] == "hello"

    def test_codex_per_channel_thread_isolation(self, tmp_path):
        runner = ub.CodexRunner(self.config(tmp_path, "codex"))
        runner._thread_ids[100] = "thread-A"
        runner._thread_ids[200] = "thread-B"

        args_a = runner.codex_args("ping", channel_id=100)
        args_b = runner.codex_args("ping", channel_id=200)
        args_dm = runner.codex_args("ping", channel_id=999)

        assert "thread-A" in args_a and "thread-B" not in args_a
        assert "thread-B" in args_b and "thread-A" not in args_b
        # Unknown channel = fresh thread (no resume).
        assert "resume" not in args_dm
        assert "thread-A" not in args_dm and "thread-B" not in args_dm

    def test_claude_per_channel_session_isolation(self, tmp_path):
        runner = ub.ClaudeRunner(self.config(tmp_path, "claude"))
        runner._session_ids[100] = "sess-A"
        runner._session_ids[200] = "sess-B"

        args_a = runner.claude_args("hi", channel_id=100)
        args_b = runner.claude_args("hi", channel_id=200)
        args_dm = runner.claude_args("hi", channel_id=999)

        assert "sess-A" in args_a and "sess-B" not in args_a
        assert "sess-B" in args_b and "sess-A" not in args_b
        assert "--resume" not in args_dm

    async def test_agent_bridge_common_commands_route_to_runner(self, tmp_path):
        bridge = ub.AgentBridge(self.config(tmp_path, "codex"), ub.AttachmentStore(tmp_path / "attachments"))
        ch = Channel()

        await bridge.handle(ch, "/new")
        await bridge.handle(ch, "/tools on")
        await bridge.handle(ch, "/status")

        assert bridge.runner.show_tools is True
        assert any("new Codex thread" in msg for msg in ch.sent)
        assert any("backend: codex" in msg for msg in ch.sent)


class TestSharedChannelRouting:
    def _bridge(self, tmp_path, **overrides):
        cfg = TestRunnerArgsAndRouting().config(tmp_path, "codex")
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return ub.AgentBridge(cfg, ub.AttachmentStore(tmp_path / "attachments"))

    def test_channel_prompt_requires_agent_alias(self, tmp_path):
        bridge = self._bridge(tmp_path)

        assert bridge._channel_prompt_for_agent("luna: say hi") == "say hi"
        assert bridge._channel_prompt_for_agent("Luna only: say hi") == "say hi"
        assert bridge._channel_prompt_for_agent("claudette: say hi") is None

    def test_alias_in_message_body_does_not_trigger(self, tmp_path):
        """Worker replies that mention another agent in passing must not route."""
        bridge = self._bridge(tmp_path)

        # alias appears mid-body, not at start
        assert bridge._channel_prompt_for_agent("here is what luna: said earlier") is None
        assert bridge._channel_prompt_for_agent("ok\nluna: do x") is None
        # Leading whitespace is fine; alias must still be the first token.
        assert bridge._channel_prompt_for_agent("  luna: do x") == "do x"

    def test_bot_only_channel_ignores_human_user(self, tmp_path):
        manager_bot_id = 9001
        target_channel = 700
        bridge = self._bridge(
            tmp_path,
            allowed_channel_ids=set(),
            bot_only_channel_ids={target_channel},
            allowed_bot_user_ids={manager_bot_id},
        )

        class Author:
            def __init__(self, author_id, is_bot):
                self.id = author_id
                self.bot = is_bot

        class FakeChannel:
            def __init__(self, channel_id):
                self.id = channel_id

        class FakeMsg:
            def __init__(self, channel_id, author_id, is_bot):
                self.channel = FakeChannel(channel_id)
                self.author = Author(author_id, is_bot)

        # Human (allowed_user_id) speaking in the bot-only channel: ignored.
        assert bridge._is_authorized_message(FakeMsg(target_channel, 111, False)) is False
        # Manager bot in bot-only channel: authorized.
        assert bridge._is_authorized_message(FakeMsg(target_channel, manager_bot_id, True)) is True
        # Random bot (not in allowlist) in bot-only channel: ignored.
        assert bridge._is_authorized_message(FakeMsg(target_channel, 9999, True)) is False

    async def test_stop_marker_aborts_running_proc(self, tmp_path):
        """do_stop terminates current_proc; the on_message branch that handles
        '__stop__' from a manager bot delegates to that. Verify the building
        block."""
        bridge = self._bridge(tmp_path)

        class FakeProc:
            def __init__(self):
                self.returncode = None
                self.terminated = False
                self.killed = False

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                return self.returncode

        proc = FakeProc()
        bridge.runner.current_proc = proc
        msg = await ub.do_stop(bridge.runner)

        assert proc.terminated is True
        assert "stopped" in msg

    def test_mention_prefix_only_in_bot_only_channels(self, tmp_path):
        cfg = TestRunnerArgsAndRouting().config(tmp_path, "codex")
        cfg.bot_only_channel_ids = {7000}
        cfg.allowed_bot_user_ids = {123, 456}

        # In a bot-only channel, both manager mentions get prefixed.
        prefix = ub._bot_only_mention_prefix(cfg, 7000)
        assert "<@123>" in prefix and "<@456>" in prefix

        # In a regular channel or DM, no mention prefix.
        assert ub._bot_only_mention_prefix(cfg, 9999) == ""
        assert ub._bot_only_mention_prefix(cfg, None) == ""

        # Bot-only channel but no manager configured: still no prefix.
        cfg.allowed_bot_user_ids = set()
        assert ub._bot_only_mention_prefix(cfg, 7000) == ""

    async def test_send_text_prepends_mention_in_bot_only_channel(self, tmp_path):
        ch = Channel()
        await ub.send_text(ch, "hello world", mention_prefix="<@9001>")

        assert ch.sent[0].startswith("<@9001> hello world")

    async def test_send_text_no_mention_when_prefix_empty(self, tmp_path):
        ch = Channel()
        await ub.send_text(ch, "hello world")

        assert ch.sent[0] == "hello world"

    def test_accept_dms_false_ignores_dms(self, tmp_path):
        bridge = self._bridge(tmp_path, accept_dms=False)

        class Author:
            def __init__(self, author_id, is_bot):
                self.id = author_id
                self.bot = is_bot

        class FakeMsg:
            def __init__(self, author_id, is_bot=False):
                self.channel = ub.discord.DMChannel.__new__(ub.discord.DMChannel)
                self.author = Author(author_id, is_bot)

        # Even the allowlisted user gets ignored when accept_dms is off.
        assert bridge._is_authorized_message(FakeMsg(111)) is False
