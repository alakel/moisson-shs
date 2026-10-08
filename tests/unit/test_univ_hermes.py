"""
Sprint « universel », lot 6 : Hermes Agent (``config.yaml``, ruamel.yaml en
aller-retour). Commentaires, guillemets et références ``${VAR}`` des autres
serveurs conservés ; extrait à coller si ruamel manque ou si le fichier ne
se relit pas à l'identique.
"""

import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from mcp_clients import registry
from mcp_clients.common import API_KEY_ENV_VARS, SERVER_KEY
from mcp_clients.hermes import CONNECT_TIMEOUT, TOOL_TIMEOUT

OA, S2, CORE, SERP = API_KEY_ENV_VARS
SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "hermes_config_sample.yaml"


def _load(text):
    return YAML(typ="safe", pure=True).load(text)


@pytest.fixture
def cfg(fake_home, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _adapter(path):
    return {a.id: a for a in registry.make_adapters({"hermes": path})}["hermes"]


def _base(env):
    return {"command": "/venv avec espace/bin/python",
            "args": ["/Drive/Projet é/mcp_server.py"], "env": env}


ENV = {OA: "oa-k", S2: "", CORE: "", SERP: ""}


class TestInstall:
    def test_added_and_rest_untouched(self, cfg):
        before = cfg.read_text(encoding="utf-8")
        a = _adapter(cfg)
        entry = a.build_entry(_base(ENV), None)
        res = a.install(entry)
        assert res.ok and res.action == "added" and res.backup.exists()
        after = cfg.read_text(encoding="utf-8")
        for line in before.splitlines():
            assert line in after.splitlines()      # rien de retiré ni retouché
        parsed = _load(after)
        assert parsed["mcp_servers"][SERVER_KEY] == entry
        assert entry["connect_timeout"] == CONNECT_TIMEOUT
        assert entry["timeout"] == TOOL_TIMEOUT and entry["enabled"] is True
        assert set(res.others) == {"github", "filesystem"}

    def test_update_keeps_previous_fields_and_raises_timeouts(self, cfg):
        a = _adapter(cfg)
        a.install(a.build_entry(_base(ENV), None))
        previous = a.read_entry()
        previous["timeout"] = 1800
        entry = a.build_entry(_base({**ENV, CORE: "c"}), previous)
        assert entry["timeout"] == 1800
        assert a.install(entry).action == "updated"
        assert _load(cfg.read_text(encoding="utf-8"))["mcp_servers"][SERVER_KEY]["env"][CORE] == "c"

    def test_new_file(self, fake_home, tmp_path):
        path = tmp_path / "hermes" / "config.yaml"
        a = _adapter(path)
        entry = a.build_entry(_base(ENV), None)
        assert a.install(entry).ok
        assert _load(path.read_text(encoding="utf-8")) == {"mcp_servers": {SERVER_KEY: entry}}

    def test_hermes_home(self, fake_home, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
        a = {x.id: x for x in registry.make_adapters()}["hermes"]
        assert a.install(a.build_entry(_base(ENV), None)).ok
        assert (tmp_path / "hh" / "config.yaml").is_file()

    def test_unreproducible_layout_gives_snippet(self, fake_home, tmp_path):
        path = tmp_path / "config.yaml"
        odd = "mcp_servers:   {}\nmodel:    {default:   x}\n"
        path.write_text(odd, encoding="utf-8")
        a = _adapter(path)
        res = a.install(a.build_entry(_base(ENV), None))
        assert res.action == "snippet" and path.read_text(encoding="utf-8") == odd
        assert "oa-k" not in res.snippet
        assert _load(res.snippet)["mcp_servers"][SERVER_KEY]["env"][OA] == "<OPENALEX_API_KEY>"

    def test_without_ruamel(self, cfg, monkeypatch):
        monkeypatch.setitem(sys.modules, "ruamel.yaml", None)
        monkeypatch.setitem(sys.modules, "ruamel", None)
        before = cfg.read_text(encoding="utf-8")
        a = _adapter(cfg)
        res = a.install(a.build_entry(_base(ENV), None))
        assert res.action == "snippet" and "ruamel" in res.detail
        assert cfg.read_text(encoding="utf-8") == before


class TestUninstall:
    def test_back_to_original(self, cfg):
        before = cfg.read_text(encoding="utf-8")
        a = _adapter(cfg)
        a.install(a.build_entry(_base(ENV), None))
        assert a.uninstall().action == "removed"
        assert cfg.read_text(encoding="utf-8") == before

    def test_absent(self, cfg):
        assert _adapter(cfg).uninstall().action == "absent"
