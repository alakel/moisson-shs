"""
OPT-09 : défauts de l'outil MCP moisson_search (constat C12, décisions §10).

- workers = 4, max_per_source = 200, cache actif (TTL MCP_CACHE_TTL, 6 h)
  quand l'argument est absent ; cache=false explicite respecté ; MOISSON_CACHE
  défini dans l'environnement du serveur respecté ;
- CLI inchangée (1000 par source, workers 1, mode collecte, cache hérité de
  l'environnement) ;
- en HTTP, le plafond serveur MOISSON_MAX_ITEMS_PER_SOURCE reste ;
- descriptions réécrites (plafond PAR source, exhaustivité, cache, workers,
  fenêtre Isidore).

Aucun accès réseau : client HTTP et moteur interceptés.
"""

import asyncio
import sys

import pytest

import config
import mcp_server
from utils.http import HTTPClient


def _tool():
    tools = asyncio.run(mcp_server.list_tools())
    return next(t for t in tools if t.name == "moisson_search")


# === Schéma et descriptions ===

class TestSchemaAndDescriptions:
    def test_defaults_in_schema(self):
        props = _tool().inputSchema["properties"]
        assert props["max_per_source"]["default"] == 200
        assert props["workers"]["default"] == 4
        # Cache : absence = règle du serveur (activé sauf MOISSON_CACHE)
        assert "default" not in props["cache"]
        # Arguments historiques conservés, aucun nouvel argument obligatoire.
        assert set(props) >= {"query", "max_per_source", "sources", "enrich_oai",
                              "dedup", "from_date", "to_date", "cache",
                              "workers", "mode"}
        assert _tool().inputSchema["required"] == ["query"]

    def test_constants(self):
        assert config.MCP_DEFAULT_MAX_PER_SOURCE == 200
        assert config.MCP_DEFAULT_WORKERS == 4
        assert config.MCP_CACHE_TTL == 6 * 3600
        assert config.DEFAULT_MAX_PER_SOURCE == 1000   # CLI inchangée

    def test_descriptions_explain_the_defaults(self):
        tool = _tool()
        desc = tool.description
        props = tool.inputSchema["properties"]
        assert "200 notices PAR source" in desc
        assert "exhaustif" in desc and "max_per_source" in desc
        assert "6 h" in desc and "parallèle" in desc
        assert "200 premiers résultats" in desc          # fenêtre Isidore
        m = props["max_per_source"]["description"]
        assert "PAR source" in m and "exhaustif" in m and "200" in m
        c = props["cache"]["description"]
        assert "ACTIVÉ par défaut" in c and "6 h" in c and "false" in c
        w = props["workers"]["description"]
        assert "défaut : 4" in w and "politesse" in w


# === Défauts transmis par l'outil ===

def _capture_tool_call(monkeypatch, args, transport="stdio"):
    captured = {}

    def fake_sync(query, max_per_source, sources, enrich_oai, do_dedup,
                  from_date=None, to_date=None, cache="absent", workers="absent",
                  progress=None, cancel_event=None, **kwargs):
        captured.update(max_per_source=max_per_source, cache=cache,
                        workers=workers)
        return ([], {"total_unique": 0})

    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", transport)
    monkeypatch.setattr(mcp_server, "_run_moisson_sync", fake_sync)
    asyncio.run(mcp_server._tool_moisson_search(args))
    return captured


class TestToolDefaults:
    def test_absent_arguments_use_mcp_defaults(self, monkeypatch):
        got = _capture_tool_call(monkeypatch, {"query": "x", "sources": ["hal"]})
        assert got == {"max_per_source": 200, "workers": 4, "cache": None}

    def test_explicit_arguments_are_kept(self, monkeypatch):
        got = _capture_tool_call(monkeypatch, {
            "query": "x", "sources": ["hal"], "max_per_source": 1000,
            "workers": 1, "cache": False})
        assert got == {"max_per_source": 1000, "workers": 1, "cache": False}

    def test_http_server_cap_still_applies(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "MOISSON_MAX_ITEMS_PER_SOURCE", 3000)
        got = _capture_tool_call(monkeypatch, {
            "query": "x", "sources": ["hal"], "max_per_source": 10**6}, "http")
        assert got["max_per_source"] == 3000
        got = _capture_tool_call(monkeypatch, {"query": "x", "sources": ["hal"]},
                                 "http")
        assert got["max_per_source"] == 200
        monkeypatch.setattr(mcp_server, "MOISSON_MAX_ITEMS_PER_SOURCE", 50)
        got = _capture_tool_call(monkeypatch, {"query": "x", "sources": ["hal"]},
                                 "http")
        assert got["max_per_source"] == 50


# === Cache à trois états côté MCP ===

class TestMcpCacheOption:
    @pytest.mark.parametrize("env,value,expected", [
        (None, None, True),      # défaut MCP : activé
        ("0", None, False),      # exploitant : désactivé
        ("1", None, True),
        (None, False, False),    # refus explicite
        ("1", False, False),
        ("0", True, True),       # demande explicite
    ])
    def test_resolution(self, monkeypatch, env, value, expected):
        if env is None:
            monkeypatch.delenv("MOISSON_CACHE", raising=False)
        else:
            monkeypatch.setenv("MOISSON_CACHE", env)
        assert mcp_server.mcp_cache_option(value) is expected

    def test_cache_dir_is_distinct_from_the_cli(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "http"))
        assert mcp_server.mcp_cache_dir() == str(tmp_path / "http_mcp")
        monkeypatch.delenv("MOISSON_CACHE_DIR")
        assert mcp_server.mcp_cache_dir().endswith("moisson_shs/http_mcp")


class _Stop(Exception):
    pass


def _capturing_client(store):
    class _Fake:
        def __init__(self, use_cache=None, cache_ttl=None, cache_dir=None, **kw):
            store.update(use_cache=use_cache, cache_ttl=cache_ttl,
                         cache_dir=cache_dir)
            raise _Stop()
    return _Fake


class TestRunMoissonSyncDefaults:
    def test_absent_cache_builds_an_mcp_cache_client(self, monkeypatch, tmp_path):
        store = {}
        monkeypatch.delenv("MOISSON_CACHE", raising=False)
        monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "http"))
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "HTTPClient", _capturing_client(store))
        with pytest.raises(_Stop):
            mcp_server._run_moisson_sync("q", 5, ["hal"], False, True)
        assert store == {"use_cache": True, "cache_ttl": 6 * 3600,
                         "cache_dir": str(tmp_path / "http_mcp")}

    def test_explicit_false_is_respected(self, monkeypatch, tmp_path):
        store = {}
        monkeypatch.setenv("MOISSON_CACHE", "1")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "HTTPClient", _capturing_client(store))
        with pytest.raises(_Stop):
            mcp_server._run_moisson_sync("q", 5, ["hal"], False, True,
                                         cache=False)
        assert store["use_cache"] is False

    def test_real_client_gets_the_mcp_ttl(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MOISSON_CACHE", raising=False)
        monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "http"))
        seen = {}
        real = HTTPClient

        def spy(**kw):
            client = real(**kw)
            seen["client"] = client
            return client

        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "HTTPClient", spy)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda name, *a, **k: [])
        result = mcp_server._run_moisson_sync("q", 5, ["hal"], False, True)
        client = seen["client"]
        assert client.cache_enabled is True and client.cache_ttl == 6 * 3600
        assert (tmp_path / "http_mcp.sqlite").exists()
        assert not (tmp_path / "http.sqlite").exists()
        blocks, structured = result
        assert structured["settings"] == {
            "max_per_source": 5, "workers": 4, "cache": True,
            "cache_ttl_seconds": 6 * 3600}

    def test_unwritable_cache_falls_back_to_no_cache(self, monkeypatch, tmp_path):
        """Cache actif par défaut : un répertoire de cache inutilisable ne fait
        pas échouer la moisson, qui se fait sans cache."""
        blocker = tmp_path / "fichier"
        blocker.write_text("x")
        monkeypatch.delenv("MOISSON_CACHE", raising=False)
        monkeypatch.setenv("MOISSON_CACHE_DIR", str(blocker / "sous" / "http"))
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda name, *a, **k: [])
        blocks, structured = mcp_server._run_moisson_sync(
            "q", 5, ["hal"], False, True)
        assert structured["settings"]["cache"] is False

    def test_workers_default_reaches_the_engine(self, monkeypatch, tmp_path):
        seen = {}

        def fake_harvest(request, ctx):
            seen["workers"] = request.workers
            seen["cache"] = request.cache
            raise _Stop()

        monkeypatch.delenv("MOISSON_CACHE", raising=False)
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "harvest", fake_harvest)
        with pytest.raises(_Stop):
            mcp_server._run_moisson_sync("q", 5, ["hal"], False, True)
        assert seen == {"workers": 4, "cache": True}
        with pytest.raises(_Stop):
            mcp_server._run_moisson_sync("q", 5, ["hal"], False, True,
                                         cache=False, workers=1)
        assert seen == {"workers": 1, "cache": False}

    def test_summary_names_the_settings(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MOISSON_CACHE", "0")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda name, *a, **k: [])
        blocks, structured = mcp_server._run_moisson_sync(
            "q", 200, ["hal"], False, True)
        text = blocks[0].text
        assert ("Réglages : 200 notices au plus par source, 4 source(s) à la "
                "fois, cache inactif") in text
        assert structured["settings"]["cache"] is False
        assert structured["settings"]["cache_ttl_seconds"] is None


# === CLI inchangée ===

class TestCliUnchanged:
    def test_cli_defaults(self, monkeypatch, tmp_path):
        import main as main_module
        seen = {}

        class _FakeClient:
            cache_enabled = False

            def __init__(self, use_cache=None, **kw):
                seen["use_cache"] = use_cache
                seen["client_kwargs"] = kw

            def close(self):
                pass

        def fake_harvest(request, ctx):
            seen.update(max=request.max_per_source, workers=request.workers,
                        mode=request.mode, cache=request.cache)
            raise _Stop()

        monkeypatch.delenv("MOISSON_CACHE", raising=False)
        monkeypatch.setattr(main_module, "HTTPClient", _FakeClient)
        monkeypatch.setattr(main_module, "harvest", fake_harvest)
        monkeypatch.setattr(sys, "argv", ["main.py", "q", "--sources", "hal",
                                          "--output", str(tmp_path)])
        with pytest.raises(_Stop):
            main_module.main()
        assert seen["max"] == 1000 and seen["workers"] == 1
        assert seen["mode"] == "collecte"
        assert seen["use_cache"] is None and seen["cache"] is None
        assert seen["client_kwargs"] == {}      # TTL de la CLI (24 h)
