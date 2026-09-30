"""
OPT-09 bis : échéance souple de moisson_search en stdio (serveur MCP).

- argument ``deadline_seconds`` (ajout, invariant 2) : nombre borné,
  0 = aucune échéance ; défaut ``MOISSON_STDIO_SOFT_DEADLINE`` (50 s) ;
- en stdio, l'échéance atteint le contexte d'exécution (portée ouverte dans
  le fil de travail) ; une source muette est arrêtée (motif ``deadline``) et
  le corpus des autres sources est PUBLIÉ avant l'échéance plus une marge,
  avec un succès partiel nommé (texte, ``by_source_status``,
  ``structuredContent.soft_deadline``) ; si aucune source n'aboutit :
  erreur d'outil, aucun fichier ;
- sans source lente : même résultat qu'avant (seuls les champs de
  l'échéance s'ajoutent) ;
- HTTP inchangé : ``deadline_seconds`` sans effet, deadline dure sans
  publication partielle ;
- l'annulation par le client ne publie toujours rien.

Aucun réseau : ``engine.orchestrator.run_source`` remplacé, cache désactivé.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import config
import mcp_server
from engine.models import RunContext, SourceResult


def _items(name, n=2):
    return [{"itemType": "journalArticle", "title": f"{name} titre {i}",
             "date": "2024", "DOI": f"10.0/{name.lower()}.{i}",
             "creators": [{"creatorType": "author", "lastName": f"{name}{i}"}],
             "extra": f"Sources: {name}"} for i in range(n)]


def _mute_isidore_runner(name, cls, query, mps, output_dir, run_id, http_client,
                         run_context=None, source_key=None, **kw):
    """HAL répond ; Isidore et ses sous-sources attendent leur signal (service
    muet) : seule une échéance ou une annulation les arrête."""
    if source_key == "hal":
        return SourceResult(source=source_key, display_name=name,
                            items=_items(name), status="complete",
                            limit_reached=True)
    run_context.check()
    while not run_context.signal.is_set():
        time.sleep(0.01)
    run_context.signal.check()
    return []


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
    monkeypatch.setenv("MOISSON_CACHE", "0")
    monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "cache" / "http"))
    return tmp_path


def _tool():
    tools = asyncio.run(mcp_server.list_tools())
    return next(t for t in tools if t.name == "moisson_search")


# === Argument et schéma ===

class TestArgument:
    def test_default_comes_from_config(self, monkeypatch):
        assert config.STDIO_SOFT_DEADLINE_S == 50
        assert mcp_server._soft_deadline_seconds(None) == 50
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_S", 0.0)
        assert mcp_server._soft_deadline_seconds(None) is None

    @pytest.mark.parametrize("value,expected", [
        (0, None), (0.0, None), (5, 10.0), (10, 10.0), (30.5, 30.5),
        ("20", 20.0), (240, 240.0), (10_000, 900.0),
    ])
    def test_bounded(self, value, expected):
        assert mcp_server._soft_deadline_seconds(value) == expected

    @pytest.mark.parametrize("value", [-1, "abc", True, float("nan"),
                                       float("inf"), [30], {}])
    def test_refused(self, value):
        res = mcp_server._soft_deadline_seconds(value)
        assert isinstance(res, mcp_server.ToolError)
        assert "deadline_seconds" in res

    def test_invalid_argument_is_refused_before_any_harvest(self, out, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("aucune moisson attendue")
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", boom)
        res = asyncio.run(mcp_server._tool_moisson_search(
            {"query": "q", "sources": ["hal"], "deadline_seconds": -5}))
        assert isinstance(res, mcp_server.ToolError)
        assert "Paramètre refusé" in res

    def test_schema(self):
        tool = _tool()
        props = tool.inputSchema["properties"]
        prop = props["deadline_seconds"]
        assert prop["type"] == "number" and prop["minimum"] == 0
        # Pas de « maximum » : le SDK refuserait au lieu de ramener à 900 s
        # (revue OPT-09 bis) ; la borne est dans la description.
        assert "maximum" not in prop
        assert config.STDIO_SOFT_DEADLINE_MAX_S == 900
        assert "900 s" in prop["description"]
        assert "default" not in prop          # règle du serveur
        desc = prop["description"]
        assert "50 s" in desc and "deadline" in desc and "distant" in desc
        # Arguments historiques conservés, aucun nouvel argument obligatoire.
        assert set(props) >= {"query", "max_per_source", "sources",
                              "enrich_oai", "dedup", "from_date", "to_date",
                              "cache", "workers", "mode", "query_variants",
                              "languages", "plan_only"}
        assert tool.inputSchema["required"] == ["query"]
        assert "deux minutes" not in tool.description
        assert "échéance souple" in tool.description


# === La portée atteint le contexte d'exécution ===

def _capture_context(monkeypatch, args, transport="stdio"):
    seen = {}

    def fake_sync(*a, **kw):
        ctx = RunContext()
        seen["soft_s"] = ctx.soft_deadline_s
        seen["left"] = (ctx.soft_deadline - time.monotonic()
                        if ctx.soft_deadline is not None else None)
        return ([], {"total_unique": 0})

    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", transport)
    monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
    monkeypatch.setattr(mcp_server, "_run_moisson_sync", fake_sync)
    res = asyncio.run(mcp_server._tool_moisson_search(args))
    return seen, res


class TestScope:
    def test_stdio_default(self, monkeypatch):
        seen, res = _capture_context(monkeypatch, {"query": "x", "sources": ["hal"]})
        assert seen["soft_s"] == 50 and 49 < seen["left"] <= 50
        blocks, structured = res
        assert structured["soft_deadline"] == {
            "seconds": 50.0, "reached": False, "stopped_sources": [],
            "unreachable_sources": []}

    def test_stdio_argument(self, monkeypatch):
        seen, _ = _capture_context(monkeypatch, {
            "query": "x", "sources": ["hal"], "deadline_seconds": 3})
        assert seen["soft_s"] == 10          # relevé au minimum
        seen, res = _capture_context(monkeypatch, {
            "query": "x", "sources": ["hal"], "deadline_seconds": 0})
        assert seen["soft_s"] is None and "soft_deadline" not in res[1]

    def test_http_ignores_the_argument(self, monkeypatch):
        seen, res = _capture_context(monkeypatch, {
            "query": "x", "sources": ["hal"], "deadline_seconds": 20}, "http")
        assert seen["soft_s"] is None
        assert "soft_deadline" not in res[1]

    def test_scope_closed_after_the_call(self, monkeypatch):
        _capture_context(monkeypatch, {"query": "x", "sources": ["hal"]})
        assert RunContext().soft_deadline is None


# === Source muette : publication des autres sources ===

class TestMuteSourceStdio:
    def test_publishes_before_the_deadline_plus_margin(self, out, monkeypatch):
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_MIN_S", 0.1)
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_GRACE_S", 0.3)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            _mute_isidore_runner)
        t0 = time.monotonic()
        res = asyncio.run(mcp_server._tool_moisson_search({
            "query": "q", "sources": ["hal", "isidore", "openedition"],
            "deadline_seconds": 0.6}))
        elapsed = time.monotonic() - t0
        assert elapsed < 0.6 + 0.3 + 1.5
        assert not mcp_server._is_tool_error(res)
        blocks, structured = res
        assert structured["harvest_status"] == "partial"
        status = structured["by_source_status"]
        assert status["HAL"]["status"] == "complete"
        for name in ("Isidore", "OpenEdition"):
            assert status[name]["status"] == "error"
            assert status[name]["reason"] == "deadline"
        assert structured["soft_deadline"] == {
            "seconds": 0.6, "reached": True,
            "stopped_sources": ["Isidore", "OpenEdition"],
            "unreachable_sources": []}
        assert structured["settings"]["soft_deadline_seconds"] == 0.6
        assert structured["total_unique"] == 2
        text = blocks[0].text
        head, second = text.split("\n")[:2]
        assert head.startswith("Moisson terminée (succès partiel")
        assert second.startswith("Échéance souple de 0.6 s atteinte : 2 source(s)")
        assert "Isidore : 0 — en échec (échéance dépassée" in text
        # Corpus publié (JSON, CSL, statistiques).
        assert len(list(out.glob("corpus_*.csl.json"))) == 1
        assert len(list(out.glob("corpus_*_stats.txt"))) == 1

    def test_all_sources_stopped_is_a_tool_error(self, out, monkeypatch):
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_MIN_S", 0.1)
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_GRACE_S", 0.3)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            _mute_isidore_runner)
        res = asyncio.run(mcp_server._tool_moisson_search({
            "query": "q", "sources": ["isidore", "openedition"],
            "deadline_seconds": 0.4}))
        assert isinstance(res, mcp_server.ToolError) and not res.cancelled
        assert res.startswith("Moisson en échec")
        assert "Échéance souple de 0.4 s atteinte" in res.split("\n")[1]
        assert "aucun corpus n'a été écrit" in res
        assert res.structured["soft_deadline"]["reached"] is True
        assert list(out.glob("corpus_*")) == []
        raw = out / "raw"
        assert not raw.exists() or list(raw.glob("*.json")) == []

    def test_mcp_result_is_an_error_through_the_dispatcher(self, out, monkeypatch):
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_MIN_S", 0.1)
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_GRACE_S", 0.3)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            _mute_isidore_runner)
        res = asyncio.run(mcp_server._tool_moisson_search({
            "query": "q", "sources": ["isidore"], "deadline_seconds": 0.3}))
        call = mcp_server._error_result(res)
        assert call.isError is True
        assert call.structuredContent["by_source_status"]["Isidore"]["reason"] \
            == "deadline"


# === Sans source lente : inchangé ===

class TestUnchangedWithoutSlowSource:
    def test_same_result_as_without_deadline(self, out, monkeypatch):
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda name, *a, **k: _items(name))
        args = {"query": "q", "sources": ["hal", "isidore", "openedition"]}
        with_soft = asyncio.run(mcp_server._tool_moisson_search(dict(args)))
        without = asyncio.run(mcp_server._tool_moisson_search(
            dict(args, deadline_seconds=0)))
        s1, s2 = with_soft[1], without[1]
        assert s1.pop("soft_deadline") == {"seconds": 50.0, "reached": False,
                                           "stopped_sources": [],
                                           "unreachable_sources": []}
        assert s1["settings"].pop("soft_deadline_seconds") == 50.0
        assert "soft_deadline" not in s2
        for key in ("harvest_status", "total_unique", "total_before_dedup",
                    "by_source", "settings"):
            assert s1[key] == s2[key], key
        strip = lambda d: {k: {kk: vv for kk, vv in v.items() if kk != "duration_s"}
                           for k, v in d.items()}
        assert strip(s1["by_source_status"]) == strip(s2["by_source_status"])
        # Aucune ligne d'échéance dans le texte.
        assert "Échéance souple" not in with_soft[0][0].text


# === HTTP inchangé ===

class TestHttpUnchanged:
    def test_argument_has_no_effect_in_http(self, out, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_MIN_S", 0.1)

        def slow(name, cls, query, mps, output_dir, run_id, http_client,
                 run_context=None, **kw):
            time.sleep(0.5)             # au-delà de deadline_seconds
            return _items(name)

        monkeypatch.setattr("engine.orchestrator.run_source", slow)
        res = asyncio.run(mcp_server._tool_moisson_search({
            "query": "q", "sources": ["hal", "isidore"],
            "deadline_seconds": 0.2}))
        assert not mcp_server._is_tool_error(res)
        blocks, structured = res
        assert structured["harvest_status"] == "ok"
        assert "soft_deadline" not in structured
        assert "soft_deadline_seconds" not in structured["settings"]

    def test_hard_deadline_still_publishes_nothing(self, out, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "MOISSON_DEADLINE_SECONDS", 0.3)
        monkeypatch.setattr("engine.orchestrator.run_source",
                            _mute_isidore_runner)
        res = asyncio.run(mcp_server._tool_moisson_search({
            "query": "q", "sources": ["hal", "isidore"],
            "deadline_seconds": 30}))
        assert isinstance(res, mcp_server.ToolError) and res.cancelled
        assert "deadline serveur" in res
        assert list(out.glob("corpus_*")) == []


# === Annulation par le client ===

class TestClientCancellation:
    def test_client_cancel_publishes_nothing(self, out, monkeypatch):
        stopped = threading.Event()

        def runner(*a, run_context=None, source_key=None, **kw):
            if source_key == "hal":
                return _items("HAL")
            while not run_context.signal.is_set():
                time.sleep(0.01)
            stopped.set()
            run_context.signal.check()
            return []

        monkeypatch.setattr("engine.orchestrator.run_source", runner)

        async def go():
            task = asyncio.create_task(mcp_server._tool_moisson_search(
                {"query": "q", "sources": ["hal", "isidore"]}))   # échéance 50 s
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        t0 = time.monotonic()
        asyncio.run(go())
        assert time.monotonic() - t0 < 2.0
        assert stopped.is_set()
        assert list(out.glob("corpus_*")) == []
        raw = out / "raw"
        assert not raw.exists() or list(raw.glob("*.json")) == []
