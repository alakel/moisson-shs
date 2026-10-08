"""
Sprint « universel », lot 5 : Codex (``~/.codex/config.toml``, tomlkit).

Fixture ``codex_config_sample.toml`` : réplique nettoyée du fichier du
poste (notre entrée ne porte que la clé OpenAlex et ``enabled``, autres
serveurs avec clés en table et en ligne, projets aux chemins avec espaces
et accents, commentaires).
"""

import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (matrice CI)
    import tomli as tomllib

import pytest

from mcp_clients import registry
from mcp_clients.codex import STARTUP_TIMEOUT_SEC, TOOL_TIMEOUT_SEC
from mcp_clients.common import API_KEY_ENV_VARS, SERVER_KEY

OA, S2, CORE, SERP = API_KEY_ENV_VARS
SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "codex_config_sample.toml"


@pytest.fixture
def cfg(fake_home, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _adapter(path):
    return {a.id: a for a in registry.make_adapters({"codex": path})}["codex"]


def _base(env):
    return {"command": "/nouveau venv/bin/python",
            "args": ["/Drive/Projet é/mcp_server.py"], "env": env}


def _foreign_blocks(text):
    """Texte hors de nos deux tables (comparaison au texte près)."""
    out, ours = [], False
    for line in text.splitlines():
        if line.startswith("["):
            ours = line.startswith(f"[mcp_servers.{SERVER_KEY}")
        if not ours:
            out.append(line)
    return [line for line in out if line.strip()]


class TestInstall:
    def test_existing_entry_completed_and_rest_untouched(self, cfg):
        before = cfg.read_text(encoding="utf-8")
        a = _adapter(cfg)
        previous = a.read_entry()
        assert previous["env"] == {OA: "oa-existante"}
        env = {OA: "oa-existante", S2: "", CORE: "core-k", SERP: ""}
        entry = a.build_entry(_base(env), previous)
        res = a.install(entry)
        assert res.ok and res.action == "updated" and res.backup.exists()
        after = cfg.read_text(encoding="utf-8")
        parsed = tomllib.loads(after)
        ours = parsed["mcp_servers"][SERVER_KEY]
        assert ours["env"] == env
        assert ours["startup_timeout_sec"] == STARTUP_TIMEOUT_SEC
        assert ours["tool_timeout_sec"] == TOOL_TIMEOUT_SEC
        assert ours["enabled"] is True
        assert ours["command"] == "/nouveau venv/bin/python"
        # Tout le reste est identique au texte près, commentaires compris.
        assert _foreign_blocks(after) == _foreign_blocks(before)
        assert "# clé d'un autre serveur" in after
        assert set(res.others) == {"Context7", "MCP_DOCKER", "corpus_pro",
                                   "github-official-docker", "node_repl"}

    def test_timeouts_raised_never_lowered(self, cfg):
        a = _adapter(cfg)
        entry = a.build_entry(_base({}), {"startup_timeout_sec": 120,
                                          "tool_timeout_sec": 30})
        assert entry["startup_timeout_sec"] == 120
        assert entry["tool_timeout_sec"] == TOOL_TIMEOUT_SEC

    def test_legacy_ms_key_is_left_alone(self, cfg):
        entry = _adapter(cfg).build_entry(_base({}), {"startup_timeout_ms": 90000})
        assert entry["startup_timeout_ms"] == 90000
        assert "startup_timeout_sec" not in entry

    def test_new_entry_in_file_without_mcp_servers(self, fake_home, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('model = "x"\n\n[projects."/p q"]\ntrust_level = "trusted"\n',
                        encoding="utf-8")
        a = _adapter(path)
        entry = a.build_entry(_base({OA: "", S2: "", CORE: "", SERP: ""}), None)
        assert a.install(entry).action == "added"
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        assert parsed["mcp_servers"][SERVER_KEY] == entry
        assert parsed["projects"] == {"/p q": {"trust_level": "trusted"}}

    def test_new_file(self, fake_home, tmp_path):
        path = tmp_path / "neuf" / "config.toml"
        a = _adapter(path)
        entry = a.build_entry(_base({OA: "k", S2: "", CORE: "", SERP: ""}), None)
        assert a.install(entry).ok
        assert tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"][SERVER_KEY] == entry

    def test_inline_env_is_updated_in_place(self, fake_home, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            f'[mcp_servers.{SERVER_KEY}]\ncommand = "/p"\nargs = []\n'
            f'env = {{ {OA} = "a" }}\n', encoding="utf-8")
        a = _adapter(path)
        env = {OA: "a", S2: "s", CORE: "", SERP: ""}
        entry = a.build_entry(_base(env), a.read_entry())
        assert a.install(entry).ok
        text = path.read_text(encoding="utf-8")
        assert tomllib.loads(text)["mcp_servers"][SERVER_KEY]["env"] == env
        assert "env = {" in text            # toujours en ligne

    def test_windows_path_with_backslashes(self, fake_home, tmp_path):
        path = tmp_path / "config.toml"
        a = _adapter(path)
        base = {"command": "C:\\Users\\A B\\venv\\Scripts\\python.exe",
                "args": ["C:\\Projet\\mcp_server.py"], "env": {}}
        entry = a.build_entry(base, None)
        assert a.install(entry).ok
        ours = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"][SERVER_KEY]
        assert ours["command"] == base["command"]

    def test_dry_run(self, cfg):
        before = cfg.read_text(encoding="utf-8")
        a = _adapter(cfg)
        res = a.install(a.build_entry(_base({}), a.read_entry()), dry_run=True)
        assert res.action == "dry-run"
        assert cfg.read_text(encoding="utf-8") == before


class TestWithoutTomlkit:
    def test_snippet_and_file_untouched(self, cfg, monkeypatch):
        monkeypatch.setitem(sys.modules, "tomlkit", None)
        before = cfg.read_text(encoding="utf-8")
        a = _adapter(cfg)
        env = {OA: "oa-existante", S2: "", CORE: "", SERP: ""}
        res = a.install(a.build_entry(_base(env), a.read_entry()))
        assert res.action == "snippet" and "tomlkit" in res.detail
        assert cfg.read_text(encoding="utf-8") == before
        assert "oa-existante" not in res.snippet
        parsed = tomllib.loads(res.snippet)
        assert parsed["mcp_servers"][SERVER_KEY]["env"][OA] == "<OPENALEX_API_KEY>"

    def test_status_still_reads(self, cfg, monkeypatch):
        monkeypatch.setitem(sys.modules, "tomlkit", None)
        assert _adapter(cfg).read_entry()["env"] == {OA: "oa-existante"}


class TestUninstall:
    def test_both_tables_removed(self, cfg):
        before = cfg.read_text(encoding="utf-8")
        res = _adapter(cfg).uninstall()
        assert res.action == "removed"
        after = cfg.read_text(encoding="utf-8")
        assert f"[mcp_servers.{SERVER_KEY}" not in after
        assert _foreign_blocks(after) == _foreign_blocks(before)
        assert SERVER_KEY not in tomllib.loads(after)["mcp_servers"]
