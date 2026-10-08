"""
Sprint « universel », lot 3 : écrivain JSON ``mcpServers`` commun à Claude
Desktop, Gemini CLI et Antigravity (CLI et IDE).

Invariants : le reste du fichier est conservé (clés de premier niveau
inconnues comme ``preferences``, autres serveurs, champs inconnus de notre
entrée) ; une écriture crée une sauvegarde ; un fichier illisible (JSON
avec commentaires) n'est jamais touché : extrait à coller, masqué.
"""

import json
import os
import stat

import pytest

from mcp_clients import registry
from mcp_clients.common import API_KEY_ENV_VARS, SERVER_KEY

OA, S2, CORE, SERP = API_KEY_ENV_VARS
JSON_IDS = ["claude-desktop", "gemini-cli", "antigravity", "antigravity-ide"]


def _adapter(client_id, path):
    return {a.id: a for a in registry.make_adapters({client_id: path})}[client_id]


def _base(oa="oa-cle-secrete"):
    return {"command": "/chemin avec espace/venv/bin/python",
            "args": ["/Drive/Projet é/mcp_server.py"],
            "env": {OA: oa, S2: "", CORE: "", SERP: ""}}


def _existing(path, servers=None, indent=2, **top):
    doc = dict(top)
    doc["mcpServers"] = servers if servers is not None else {
        "autre": {"command": "npx", "env": {"API_KEY": "sk-autre-secret"}}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=indent) + "\n", encoding="utf-8")
    return doc


@pytest.fixture(params=JSON_IDS)
def client_id(request, fake_home):
    return request.param


class TestInstall:
    def test_unknown_keys_and_other_servers_are_kept(self, client_id, tmp_path):
        path = tmp_path / "cfg.json"
        before = _existing(path, preferences={"sidebar": True},
                           coworkUserFilesPath="/x", general={"a": 1})
        a = _adapter(client_id, path)
        entry = a.build_entry(_base(), None)
        res = a.install(entry)
        assert res.ok and res.action == "added"
        after = json.loads(path.read_text(encoding="utf-8"))
        for key in ("preferences", "coworkUserFilesPath", "general"):
            assert after[key] == before[key]
        assert after["mcpServers"]["autre"] == before["mcpServers"]["autre"]
        ours = after["mcpServers"][SERVER_KEY]
        assert ours == entry
        assert set(API_KEY_ENV_VARS) <= set(ours["env"])
        assert res.backup is not None and res.backup.exists()
        assert res.others == ["autre"]

    def test_unknown_fields_of_our_entry_are_kept(self, client_id, tmp_path):
        path = tmp_path / "cfg.json"
        _existing(path, servers={SERVER_KEY: {
            "command": "/vieux", "args": [], "disabled": False,
            "env": {OA: "ancienne", "MOISSON_KEY_FALLBACK": "openalex"}}})
        a = _adapter(client_id, path)
        previous = a.read_entry()
        entry = a.build_entry(_base(oa="nouvelle"), previous)
        assert a.install(entry).action == "updated"
        ours = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_KEY]
        assert ours["disabled"] is False
        assert ours["command"] == "/chemin avec espace/venv/bin/python"

    def test_indentation_is_kept(self, client_id, tmp_path):
        path = tmp_path / "cfg.json"
        _existing(path, indent=4)
        a = _adapter(client_id, path)
        a.install(a.build_entry(_base(), None))
        assert '\n    "mcpServers"' in path.read_text(encoding="utf-8")

    def test_new_file_is_created_private(self, client_id, tmp_path):
        path = tmp_path / "neuf" / "cfg.json"
        a = _adapter(client_id, path)
        res = a.install(a.build_entry(_base(), None))
        assert res.ok and res.backup is None
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert SERVER_KEY in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]

    def test_world_readable_file_becomes_private_with_keys(self, client_id, tmp_path):
        path = tmp_path / "cfg.json"
        _existing(path)
        os.chmod(path, 0o644)
        a = _adapter(client_id, path)
        res = a.install(a.build_entry(_base(), None))
        assert res.secured and stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_comments_give_a_masked_snippet(self, client_id, tmp_path):
        path = tmp_path / "cfg.json"
        original = '{\n  // commentaire\n  "mcpServers": {}\n}\n'
        path.write_text(original, encoding="utf-8")
        a = _adapter(client_id, path)
        res = a.install(a.build_entry(_base(), None))
        assert res.action == "snippet" and res.snippet
        assert path.read_text(encoding="utf-8") == original
        assert "oa-cle-secrete" not in res.snippet
        assert "<OPENALEX_API_KEY>" in res.snippet
        assert list(tmp_path.glob("*.moisson-bak-*")) == []

    def test_dry_run_writes_nothing(self, client_id, tmp_path):
        path = tmp_path / "cfg.json"
        _existing(path)
        original = path.read_text(encoding="utf-8")
        a = _adapter(client_id, path)
        res = a.install(a.build_entry(_base(), None), dry_run=True)
        assert res.action == "dry-run" and res.others == ["autre"]
        assert path.read_text(encoding="utf-8") == original
        assert list(tmp_path.glob("*.moisson-bak-*")) == []

    def test_servers_key_of_wrong_type_gives_snippet(self, client_id, tmp_path):
        path = tmp_path / "cfg.json"
        path.write_text('{"mcpServers": []}', encoding="utf-8")
        a = _adapter(client_id, path)
        assert a.install(a.build_entry(_base(), None)).action == "snippet"


class TestUninstall:
    def test_only_our_entry_is_removed(self, client_id, tmp_path):
        path = tmp_path / "cfg.json"
        _existing(path, servers={
            "autre": {"command": "npx"}, SERVER_KEY: {"command": "/p"}},
            preferences={"x": 1})
        a = _adapter(client_id, path)
        res = a.uninstall()
        assert res.ok and res.action == "removed" and res.backup.exists()
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after == {"preferences": {"x": 1},
                         "mcpServers": {"autre": {"command": "npx"}}}

    def test_absent_entry(self, client_id, tmp_path):
        path = tmp_path / "cfg.json"
        _existing(path)
        assert _adapter(client_id, path).uninstall().action == "absent"

    def test_absent_file(self, client_id, tmp_path):
        assert _adapter(client_id, tmp_path / "x.json").uninstall().action == "absent"


class TestExtraFields:
    def test_gemini_timeout_is_raised_not_lowered(self, fake_home, tmp_path):
        a = _adapter("gemini-cli", tmp_path / "s.json")
        assert a.build_entry(_base(), None)["timeout"] == 960000
        assert a.build_entry(_base(), {"timeout": 2000000})["timeout"] == 2000000

    def test_desktop_entry_has_no_extra_field(self, fake_home, tmp_path):
        a = _adapter("claude-desktop", tmp_path / "c.json")
        assert set(a.build_entry(_base(), None)) == {"command", "args", "env"}
