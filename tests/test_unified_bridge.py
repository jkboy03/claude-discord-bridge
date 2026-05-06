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
    def __init__(self):
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
        args = runner.codex_args(prompt, [image])

        assert "--image" in args and str(image) in args
        assert any(str(doc) in arg for arg in args)
        assert args.index("--image") > args.index(prompt)
        assert args[:3] == ["/usr/bin/true", "exec", "--json"]

    def test_claude_uses_add_dir_and_resume(self, tmp_path):
        runner = ub.ClaudeRunner(self.config(tmp_path, "claude"))
        runner.session_id = "sess-1"
        args = runner.claude_args("hello")

        assert "--resume" in args and "sess-1" in args
        assert f"--add-dir={tmp_path / 'attachments'}" in args
        assert args[-1] == "hello"

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
    def test_channel_prompt_requires_agent_alias(self, tmp_path):
        bridge = ub.AgentBridge(TestRunnerArgsAndRouting().config(tmp_path, "codex"), ub.AttachmentStore(tmp_path / "attachments"))

        assert bridge._channel_prompt_for_agent("luna: say hi") == "say hi"
        assert bridge._channel_prompt_for_agent("Luna only: say hi") == "say hi"
        assert bridge._channel_prompt_for_agent("claudette: say hi") is None
