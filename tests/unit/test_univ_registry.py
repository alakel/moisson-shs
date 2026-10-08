"""
Sprint « universel », lot 2 : registre des clients MCP (identifiants,
alias, chemins par système, détection) et détection d'une application
ouverte.
"""

import argparse
import subprocess
from pathlib import Path

import pytest

from mcp_clients import procs, registry


IDS = ["claude-desktop", "claude-code", "codex", "gemini-cli", "antigravity",
       "antigravity-ide", "hermes"]


def _by_id(adapters):
    return {a.id: a for a in adapters}


class TestIdsAndAliases:
    def test_order_and_ids(self, fake_home):
        assert [a.id for a in registry.make_adapters()] == IDS

    def test_ids_and_aliases_are_unique(self, fake_home):
        names = []
        for a in registry.make_adapters():
            names.append(a.id)
            names.extend(a.aliases)
        assert len(names) == len(set(names))

    @pytest.mark.parametrize("spec,expected", [
        ("desktop", ["claude-desktop"]),
        ("Claude-Desktop", ["claude-desktop"]),
        ("code", ["claude-code"]),
        ("desktop,code", ["claude-desktop", "claude-code"]),
        ("chatgpt", ["codex"]),
        ("gemini, agy", ["gemini-cli", "antigravity"]),
        ("hermes-agent", ["hermes"]),
        ("codex,codex", ["codex"]),
        ("all", ["all"]),
    ])
    def test_parse_client_spec(self, spec, expected):
        assert registry.parse_client_spec(spec) == expected

    def test_unknown_id_lists_valid_ones(self):
        with pytest.raises(argparse.ArgumentTypeError) as err:
            registry.parse_client_spec("cursor")
        assert "cursor" in str(err.value) and "codex" in str(err.value)

    def test_empty_spec_is_refused(self):
        with pytest.raises(argparse.ArgumentTypeError):
            registry.parse_client_spec(" , ")


class TestPaths:
    def test_unix_paths(self, fake_home, monkeypatch):
        monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
        a = _by_id(registry.make_adapters())
        assert a["claude-desktop"].config_path() == \
            fake_home / ".config" / "Claude" / "claude_desktop_config.json"
        assert a["codex"].config_path() == fake_home / ".codex" / "config.toml"
        assert a["gemini-cli"].config_path() == fake_home / ".gemini" / "settings.json"
        assert a["antigravity"].config_path() == \
            fake_home / ".gemini" / "config" / "mcp_config.json"
        assert a["antigravity-ide"].config_path() == \
            fake_home / ".gemini" / "antigravity" / "mcp_config.json"
        assert a["hermes"].config_path() == fake_home / ".hermes" / "config.yaml"
        assert a["claude-code"].config_path() == fake_home / ".claude.json"

    def test_macos_desktop_path(self, fake_home, monkeypatch):
        monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
        a = _by_id(registry.make_adapters())
        assert a["claude-desktop"].config_path() == (
            fake_home / "Library" / "Application Support" / "Claude"
            / "claude_desktop_config.json")

    def test_windows_paths(self, fake_home, monkeypatch, tmp_path):
        monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        a = _by_id(registry.make_adapters())
        assert a["claude-desktop"].config_path() == \
            tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"
        assert a["hermes"].config_path() == tmp_path / "Local" / "hermes" / "config.yaml"

    def test_homes_from_environment(self, fake_home, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hm"))
        a = _by_id(registry.make_adapters())
        assert a["codex"].config_path() == tmp_path / "cx" / "config.toml"
        assert a["hermes"].config_path() == tmp_path / "hm" / "config.yaml"

    def test_overrides(self, fake_home, tmp_path):
        a = _by_id(registry.make_adapters({"codex": tmp_path / "x.toml"}))
        assert a["codex"].config_path() == tmp_path / "x.toml"

    def test_parse_override(self, tmp_path):
        assert registry.parse_client_config(f"gemini={tmp_path}/s.json") == \
            ("gemini-cli", Path(f"{tmp_path}/s.json"))
        with pytest.raises(argparse.ArgumentTypeError):
            registry.parse_client_config("sans-egal")


class TestDetection:
    def test_nothing_installed(self, fake_home):
        found = {a.id: a.detect().found for a in registry.make_adapters()}
        assert not any(found.values())

    def test_config_file_detects_and_reads_declaration(self, fake_home):
        path = fake_home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"mcpServers": {"moisson-shs": {"command": "/p"}}}',
                        encoding="utf-8")
        det = _by_id(registry.make_adapters())["gemini-cli"].detect()
        assert det.found and det.declared and det.writable

    def test_app_folder_detects_desktop(self, fake_home, monkeypatch):
        monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
        (fake_home / "Library" / "Application Support" / "Claude").mkdir(parents=True)
        det = _by_id(registry.make_adapters())["claude-desktop"].detect()
        assert det.found and not det.declared

    def test_cli_on_path_detects(self, fake_home, tmp_path):
        exe = tmp_path / "bin" / "gemini"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
        assert _by_id(registry.make_adapters())["gemini-cli"].detect().found

    def test_unreadable_config_is_reported(self, fake_home):
        path = fake_home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ // commentaire\n}", encoding="utf-8")
        det = _by_id(registry.make_adapters())["gemini-cli"].detect()
        assert det.found and not det.declared and det.reason

    def test_missing_library_is_not_writable(self, fake_home, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "tomlkit", None)
        det = _by_id(registry.make_adapters())["codex"].detect()
        assert not det.writable and "tomlkit" in det.reason


class TestIsRunning:
    def _runner(self, codes):
        calls = []

        def run(cmd, capture_output=True, text=True, timeout=None):
            calls.append(cmd)
            code = codes.pop(0)
            if isinstance(code, BaseException):
                raise code
            return subprocess.CompletedProcess(cmd, code, stdout="", stderr="")
        return run, calls

    def test_running(self):
        run, calls = self._runner([1, 0])
        assert procs.is_running(("Codex", "ChatGPT"), runner=run,
                                system="Darwin") is True
        assert calls == [["pgrep", "-x", "Codex"], ["pgrep", "-x", "ChatGPT"]]

    def test_not_running(self):
        run, _ = self._runner([1])
        assert procs.is_running(("Claude",), runner=run, system="Darwin") is False

    def test_unknown_when_tool_fails(self):
        run, _ = self._runner([FileNotFoundError("pgrep")])
        assert procs.is_running(("Claude",), runner=run, system="Darwin") is None
        run, _ = self._runner([3])
        assert procs.is_running(("Claude",), runner=run, system="Darwin") is None

    def test_windows(self):
        def run(cmd, capture_output=True, text=True, timeout=None):
            return subprocess.CompletedProcess(
                cmd, 0, stdout='"Claude.exe","123","Console"\n', stderr="")
        assert procs.is_running(("Claude",), runner=run, system="Windows") is True

    def test_no_names(self):
        assert procs.is_running((), runner=None) is False
