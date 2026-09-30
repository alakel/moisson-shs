"""
Tests de l'option cache à trois états (OPT-03, constat C08).

Absente → héritée de MOISSON_CACHE ; vraie ; fausse. Sur la baseline, la CLI
(``main.py``) et l'outil MCP ``moisson_search`` transformaient l'absence
d'option en ``False`` explicite, ce qui neutralisait ``MOISSON_CACHE=1`` (que
``moisson-shs setup`` inscrit pourtant dans la configuration de Claude
Desktop). Aucun accès réseau : la construction du client HTTP est
interceptée avant toute moisson.
"""

import asyncio
import sys

import pytest

from utils.http import HTTPClient, resolve_cache_option


class _Captured(Exception):
    """Interrompt main() / _run_moisson_sync juste après le choix du cache."""


def _capturing_client(store):
    """Faux HTTPClient : note l'option reçue et sa résolution, puis stoppe."""
    class _Fake:
        def __init__(self, use_cache=None, **kwargs):
            store["use_cache"] = use_cache
            store["effective"] = resolve_cache_option(use_cache)
            raise _Captured()
    return _Fake


# === Résolution ===

class TestResolveCacheOption:
    @pytest.mark.parametrize("env,expected", [
        (None, False), ("0", False), ("1", True), ("true", True),
        ("YES", True), ("on", True), ("non", False),
    ])
    def test_absent_inherits_environment(self, monkeypatch, env, expected):
        if env is None:
            monkeypatch.delenv("MOISSON_CACHE", raising=False)
        else:
            monkeypatch.setenv("MOISSON_CACHE", env)
        assert resolve_cache_option(None) is expected

    def test_explicit_values_override_environment(self, monkeypatch):
        monkeypatch.setenv("MOISSON_CACHE", "1")
        assert resolve_cache_option(False) is False
        monkeypatch.setenv("MOISSON_CACHE", "0")
        assert resolve_cache_option(True) is True

    def test_http_client_three_states(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "c"))
        monkeypatch.setenv("MOISSON_CACHE", "1")
        c = HTTPClient()
        assert c.cache_enabled is True
        c.close()
        c = HTTPClient(use_cache=False)
        assert c.cache_enabled is False
        c.close()
        monkeypatch.setenv("MOISSON_CACHE", "0")
        c = HTTPClient(use_cache=True)
        assert c.cache_enabled is True
        c.close()

    def test_ttl_constants(self):
        import os
        import config
        # Durée de vie du cache côté MCP (6 h), surchargeable par
        # MOISSON_MCP_CACHE_TTL ; appliquée par moisson_search depuis OPT-09
        # (tests dans test_opt09_mcp_defaults.py).
        if "MOISSON_MCP_CACHE_TTL" not in os.environ:
            assert config.MCP_CACHE_TTL == 6 * 3600
        # TTL de la CLI inchangé : 24 h par défaut (MOISSON_CACHE_TTL).
        if "MOISSON_CACHE_TTL" not in os.environ:
            assert config.HTTP_CACHE_TTL == 86400
            c = HTTPClient(use_cache=False)
            assert c.cache_ttl == 86400
            c.close()


# === CLI ===

class TestCliCacheOption:
    def _run_main(self, monkeypatch, tmp_path, extra_args):
        import main as main_module
        store = {}
        monkeypatch.setattr(main_module, "HTTPClient",
                            _capturing_client(store))
        argv = ["main.py", "requête test", "--output", str(tmp_path),
                "--sources", "hal"] + extra_args
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(_Captured):
            main_module.main()
        return store

    def test_absent_option_inherits_moisson_cache(self, monkeypatch,
                                                  tmp_path):
        monkeypatch.setenv("MOISSON_CACHE", "1")
        store = self._run_main(monkeypatch, tmp_path, [])
        assert store["use_cache"] is None
        assert store["effective"] is True

    def test_absent_option_without_env_is_disabled(self, monkeypatch,
                                                   tmp_path):
        monkeypatch.delenv("MOISSON_CACHE", raising=False)
        store = self._run_main(monkeypatch, tmp_path, [])
        assert store["use_cache"] is None
        assert store["effective"] is False

    def test_cache_flag(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MOISSON_CACHE", "0")
        store = self._run_main(monkeypatch, tmp_path, ["--cache"])
        assert store["use_cache"] is True and store["effective"] is True

    def test_no_cache_flag_overrides_environment(self, monkeypatch,
                                                 tmp_path):
        monkeypatch.setenv("MOISSON_CACHE", "1")
        store = self._run_main(monkeypatch, tmp_path, ["--no-cache"])
        assert store["use_cache"] is False and store["effective"] is False

    def test_cache_and_no_cache_are_exclusive(self, monkeypatch, tmp_path):
        import main as main_module
        monkeypatch.setattr(sys, "argv", ["main.py", "q", "--output",
                                          str(tmp_path), "--cache",
                                          "--no-cache"])
        with pytest.raises(SystemExit):
            main_module.main()


# === MCP ===

class TestMcpCacheOption:
    def test_schema_has_no_false_default(self):
        import mcp_server
        tools = asyncio.run(mcp_server.list_tools())
        tool = next(t for t in tools if t.name == "moisson_search")
        prop = tool.inputSchema["properties"]["cache"]
        assert prop["type"] == "boolean"
        # Pas de défaut figé : l'absence signifie « réglage du serveur »
        assert "default" not in prop

    def _capture_tool_arg(self, monkeypatch, args):
        import mcp_server
        captured = {}

        def fake_sync(query, max_per_source, sources, enrich_oai, do_dedup,
                      from_date=None, to_date=None, cache="non transmis",
                      *rest, **kwargs):
            captured["cache"] = cache
            return ([], {"total_unique": 0})

        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", fake_sync)
        asyncio.run(mcp_server._tool_moisson_search(args))
        return captured["cache"]

    def test_absent_argument_transmitted_as_none(self, monkeypatch):
        assert self._capture_tool_arg(
            monkeypatch, {"query": "x", "sources": ["hal"]}) is None

    def test_explicit_argument_transmitted(self, monkeypatch):
        assert self._capture_tool_arg(
            monkeypatch, {"query": "x", "sources": ["hal"],
                          "cache": False}) is False
        assert self._capture_tool_arg(
            monkeypatch, {"query": "x", "sources": ["hal"],
                          "cache": True}) is True

    def test_run_moisson_sync_inherits_environment(self, monkeypatch,
                                                   tmp_path):
        # OPT-09 : l'argument absent est résolu par le serveur MCP avant la
        # création du client (mcp_cache_option : MOISSON_CACHE s'il est
        # défini, sinon activé). MOISSON_CACHE=1 reste donc hérité ; le client
        # reçoit la valeur résolue au lieu de None.
        import mcp_server
        store = {}
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "HTTPClient",
                            _capturing_client(store))
        monkeypatch.setenv("MOISSON_CACHE", "1")
        with pytest.raises(_Captured):
            mcp_server._run_moisson_sync("q", 5, ["hal"], False, True)
        assert store["use_cache"] is True
        assert store["effective"] is True
