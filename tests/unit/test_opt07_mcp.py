"""
Tests OPT-07 — contrat MCP : erreurs réelles, succès partiel nommé, budgets
des outils de recherche, boucle asyncio réactive (constats C17, C30, C31).

Aucun accès réseau, aucune socket : le transport MCP réel du SDK 1.x est
exercé en mémoire (``create_connected_server_and_client_session``).

Couvre :
- échec total de ``moisson_search`` → ``isError=True`` (aller-retour SDK
  réel), avec le statut de chaque source dans ``structuredContent`` ; succès
  partiel → ``isError`` faux, ``by_source_status`` et ``by_source`` ;
- les autres outils qui renvoyaient « Erreur : … » en texte ordinaire
  renvoient ``isError=True`` avec le même texte ;
- métriques et audit alignés sur le statut réel (ok, partial, error,
  cancelled) ; aucun chemin disque en HTTP ;
- HTTP : sémaphore et échéance étendus à search_by_author,
  corpus_resolve_dois et fetch_by_doi (refus « serveur occupé », échéance →
  erreur d'outil, aucun fichier) ; plafond serveur de search_by_author ;
- ``corpus_list`` sur un gros répertoire : la boucle asyncio reste réactive
  (lecture dans un fil de travail).
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp.types as types  # noqa: E402
import mcp_server  # noqa: E402
from engine.models import SourceResult  # noqa: E402


def _items(name, n=2):
    return [{"itemType": "journalArticle", "title": f"{name} titre {i}",
             "date": "2024", "DOI": f"10.0/{name.lower()}.{i}",
             "creators": [{"creatorType": "author", "lastName": f"{name}{i}"}],
             "extra": f"Sources: {name}"} for i in range(n)]


def _failing_run_source(name, cls, query, mps, output_dir, run_id, http_client,
                        run_context=None, source_key=None, **kw):
    return SourceResult(source=source_key, display_name=name, status="error",
                        reason="http_error", detail="HTTP 503")


def _partial_run_source(name, cls, query, mps, output_dir, run_id, http_client,
                        run_context=None, source_key=None, **kw):
    if source_key == "hal":
        return SourceResult(source=source_key, display_name=name,
                            items=_items(name), status="complete",
                            limit_reached=True)
    return SourceResult(source=source_key, display_name=name, status="error",
                        reason="network_error", detail="ConnectionError")


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
    return tmp_path


def _metrics_reset(monkeypatch):
    monkeypatch.setattr(mcp_server, "_METRICS", {
        "calls_total": 0, "errors_total": 0, "partial_total": 0,
        "cancelled_total": 0, "by_tool": {}, "durations_ms": []})


# === Aller-retour SDK réel (transport en mémoire) ===

async def _sdk_call(name, args):
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(
            mcp_server.server) as client:
        return await client.call_tool(name, args)


class TestIsErrorThroughSdk:
    def test_total_failure_is_error_with_statuses(self, out, monkeypatch):
        monkeypatch.setattr("engine.orchestrator.run_source", _failing_run_source)
        res = anyio.run(_sdk_call, "moisson_search",
                        {"query": "q", "sources": ["hal", "isidore"]})
        assert res.isError is True
        text = res.content[0].text
        assert "aucune source n'a pu être moissonnée" in text
        assert "HAL : 0 — en échec (erreur HTTP : HTTP 503)" in text
        st = res.structuredContent["by_source_status"]
        assert set(st) == {"HAL", "Isidore"}
        assert {v["status"] for v in st.values()} == {"error"}
        assert list(out.glob("corpus_*")) == []

    def test_partial_success_is_named_not_an_error(self, out, monkeypatch):
        monkeypatch.setattr("engine.orchestrator.run_source", _partial_run_source)
        res = anyio.run(_sdk_call, "moisson_search",
                        {"query": "q", "sources": ["hal", "crossref"]})
        assert res.isError is False
        sc = res.structuredContent
        assert sc["harvest_status"] == "partial"
        assert sc["by_source"] == {"HAL": 2, "CrossRef": 0}
        assert sc["by_source_status"]["CrossRef"]["reason"] == "network_error"
        assert sc["by_source_status"]["HAL"]["limit_reached"] is True
        assert "succès partiel" in res.content[0].text
        assert "CrossRef : 0 — en échec (erreur réseau : ConnectionError)" in res.content[0].text

    def test_missing_file_is_error_same_text(self, out):
        res = anyio.run(_sdk_call, "corpus_analyze",
                        {"corpus_path": str(out / "absent.json")})
        assert res.isError is True
        assert res.content[0].text == "Fichier introuvable : absent.json"

    def test_refused_parameter_is_error(self, out):
        src = out / "corpus_s.json"
        src.write_text(json.dumps(_items("HAL")), encoding="utf-8")
        res = anyio.run(_sdk_call, "corpus_resolve_dois",
                        {"corpus_path": str(src), "title_threshold": 0.95,
                         "max_seconds": 5000})
        assert res.isError is True
        assert res.content[0].text.startswith("Paramètre refusé")

    def test_success_is_not_error(self, out):
        src = out / "corpus_s.json"
        src.write_text(json.dumps(_items("HAL")), encoding="utf-8")
        res = anyio.run(_sdk_call, "corpus_analyze", {"corpus_path": str(src)})
        assert res.isError is False
        assert "Total items : 2" in res.content[0].text


# === Métriques et audit ===

class TestMetricsAndAudit:
    def test_statuses_counted(self, out, monkeypatch):
        _metrics_reset(monkeypatch)
        monkeypatch.setattr("engine.orchestrator.run_source", _partial_run_source)
        asyncio.run(mcp_server.call_tool("moisson_search",
                                         {"query": "q", "sources": ["hal", "crossref"]}))
        monkeypatch.setattr("engine.orchestrator.run_source", _failing_run_source)
        asyncio.run(mcp_server.call_tool("moisson_search",
                                         {"query": "q", "sources": ["hal"]}))
        asyncio.run(mcp_server.call_tool("corpus_list", {}))
        snap = mcp_server._metrics_snapshot()
        assert snap["calls_total"] == 3
        assert snap["partial_total"] == 1 and snap["errors_total"] == 1
        assert snap["by_tool"]["moisson_search"]["partial"] == 1
        assert snap["by_tool"]["moisson_search"]["errors"] == 1

    def test_http_audit_records_real_status_without_paths(self, out, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr("engine.orchestrator.run_source", _partial_run_source)
        res = asyncio.run(mcp_server.call_tool(
            "moisson_search", {"query": "q", "sources": ["hal", "crossref"]}))
        blocks, structured = res
        assert str(out) not in json.dumps(structured)
        assert str(out) not in blocks[0].text
        audit = next((out / ".audit").glob("audit-*.jsonl"))
        record = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
        assert record["status"] == "partial"
        assert record["source_statuses"] == {"HAL": "complete", "CrossRef": "error"}
        assert str(out) not in json.dumps(record)

    def test_http_total_failure_error_result_has_no_path(self, out, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr("engine.orchestrator.run_source", _failing_run_source)
        res = asyncio.run(mcp_server.call_tool(
            "moisson_search", {"query": "q", "sources": ["hal"]}))
        assert isinstance(res, types.CallToolResult) and res.isError
        dumped = res.model_dump_json()
        assert str(out) not in dumped


# === Budgets et échéance des outils de recherche (HTTP) ===

@pytest.fixture
def http_out(out, monkeypatch):
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
    return out


def _busy(monkeypatch, coro_factory):
    async def go():
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        monkeypatch.setattr(mcp_server, "_MOISSON_SEM", sem)
        return await coro_factory()
    monkeypatch.setattr(mcp_server, "MOISSON_ACQUIRE_TIMEOUT", 0.05)
    return asyncio.run(go())


class TestLongOperationGuards:
    @pytest.mark.parametrize("tool, args", [
        ("search_by_author", {"name": "Anne Dupont"}),
        ("fetch_by_doi", {"doi": "10.1234/abc"}),
        ("corpus_resolve_dois", {"corpus_path": "corpus_x.json"}),
    ])
    def test_busy_semaphore_refuses(self, http_out, monkeypatch, tool, args):
        (http_out / "corpus_x.json").write_text("[]", encoding="utf-8")
        fn = getattr(mcp_server, f"_tool_{tool}")
        result = _busy(monkeypatch, lambda: fn(dict(args)))
        assert isinstance(result, mcp_server.ToolError)
        assert "occupé" in result

    def test_search_by_author_deadline_cancels_and_publishes_nothing(
            self, http_out, monkeypatch):
        monkeypatch.setattr(mcp_server, "MOISSON_DEADLINE_SECONDS", 0.2)
        seen = {}

        def slow(name, idhal=None, orcid=None, max_per_source=200,
                 http_client=None, cancel=None, **kw):
            seen["max"] = max_per_source
            seen["cancel"] = cancel
            while not cancel.is_set():
                time.sleep(0.01)
            seen["stopped_at"] = time.monotonic()
            return _items("HAL", 3)

        monkeypatch.setattr(mcp_server, "search_by_author", slow)
        monkeypatch.setattr(mcp_server, "MOISSON_MAX_ITEMS_PER_SOURCE", 50)
        t0 = time.monotonic()
        result = asyncio.run(mcp_server._tool_search_by_author(
            {"name": "Anne Dupont", "max_per_source": 10000}))
        assert isinstance(result, mcp_server.ToolError) and result.cancelled
        assert "deadline" in result
        assert seen["max"] == 50                      # plafond serveur
        assert seen["stopped_at"] - t0 < 1.0
        assert list(http_out.glob("corpus_*")) == []  # aucune publication

    def test_fetch_by_doi_cancel_reaches_the_requests(self, http_out, monkeypatch):
        from utils import lookups
        monkeypatch.setattr(mcp_server, "MOISSON_DEADLINE_SECONDS", 0.2)
        seen = {}

        def slow_lookup(doi, client):
            seen["signal"] = client.cancel_signal
            while not client.cancel_signal.is_set():
                time.sleep(0.01)
            client.cancel_signal.check()

        monkeypatch.setattr(lookups, "lookup_doi", slow_lookup)
        result = asyncio.run(mcp_server._tool_fetch_by_doi({"doi": "10.1/x"}))
        assert isinstance(result, mcp_server.ToolError) and result.cancelled
        from utils.http import get_default_client
        assert get_default_client().cancel_signal is None  # client partagé intact

    def test_resolve_dois_stop_event_and_budget_below_deadline(
            self, http_out, monkeypatch):
        from utils import doi_resolver as dr
        monkeypatch.setattr(mcp_server, "MOISSON_DEADLINE_SECONDS", 60.0)
        seen = {}

        def spy(items, **kwargs):
            seen.update(kwargs)
            return list(items), dr._new_report(len(items)) | {
                "resolution_rate_pct": 0.0, "duration_s": 0.0,
                "status_counts": {}, "error_list": [], "rejections": [],
                "ambiguities": []}

        monkeypatch.setattr(dr, "resolve_dois", spy)
        (http_out / "corpus_x.json").write_text(json.dumps(_items("HAL")),
                                                encoding="utf-8")
        result = asyncio.run(mcp_server._tool_corpus_resolve_dois(
            {"corpus_path": "corpus_x.json", "max_seconds": 240}))
        assert isinstance(result, tuple)
        assert seen["stop_event"] is not None and not seen["stop_event"].is_set()
        assert seen["max_seconds"] <= 60.0 - mcp_server._DOI_PUBLISH_MARGIN_S

    def test_resolve_dois_cancelled_publishes_nothing(self, out, monkeypatch):
        from utils import doi_resolver as dr

        def cancelled(items, stop_event=None, **kwargs):
            stop_event.event.set()
            return list(items), dr._new_report(len(items))

        monkeypatch.setattr(dr, "resolve_dois", cancelled)
        (out / "corpus_x.json").write_text(json.dumps(_items("HAL")),
                                           encoding="utf-8")
        result = asyncio.run(mcp_server._tool_corpus_resolve_dois(
            {"corpus_path": str(out / "corpus_x.json")}))
        assert isinstance(result, mcp_server.ToolError) and result.cancelled
        assert sorted(p.name for p in out.iterdir()) == ["corpus_x.json"]

    def test_stdio_client_cancellation_reaches_the_thread(self, out, monkeypatch):
        """stdio : l'annulation par le client (CancelledError) est propagée au
        fil de travail, attendu avant de rendre la main ; rien n'est publié."""
        stopped = threading.Event()

        def slow(name, cls, query, mps, output_dir, run_id, http_client,
                 run_context=None, **kw):
            while not run_context.cancelled():
                time.sleep(0.01)
            stopped.set()
            return []

        monkeypatch.setattr("engine.orchestrator.run_source", slow)

        async def go():
            task = asyncio.create_task(mcp_server._tool_moisson_search(
                {"query": "q", "sources": ["hal"]}))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(go())
        assert stopped.is_set()
        assert mcp_server.LAST_CANCEL_STOP_S is not None
        assert mcp_server.LAST_CANCEL_STOP_S < 1.0
        assert list(out.glob("corpus_*")) == []


# === Boucle asyncio réactive (C15, C17) ===

class TestEventLoopResponsive:
    def test_corpus_list_does_not_block_the_loop(self, out):
        # Notices à nombreux champs courts (coût de décodage élevé) :
        # ~1,5 Mo et ~10 ms de json.loads par fichier, 40 fichiers.
        big = [{"itemType": "journalArticle", "title": "x" * 50,
                "creators": [{"creatorType": "author", "lastName": f"N{i}",
                              "firstName": "J"} for i in range(20)],
                "tags": [{"tag": f"t{k}"} for k in range(10)]}
               for _ in range(1000)]
        text = json.dumps(big)
        for i in range(40):
            (out / f"corpus_gros_{i:02d}.json").write_text(text, encoding="utf-8")

        async def go():
            gaps = []
            stop = asyncio.Event()

            async def ticker():
                last = time.perf_counter()
                while not stop.is_set():
                    await asyncio.sleep(0.005)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now

            tick = asyncio.create_task(ticker())
            await asyncio.sleep(0.02)
            t0 = time.perf_counter()
            # OPT-16 : corpus_list affiche 20 corpus par défaut ; limit=40
            # pour les voir tous (premier appel : index construit à froid,
            # chaque corpus ancien est relu une fois pour son décompte).
            result = await mcp_server.call_tool("corpus_list", {"limit": 40})
            duration = time.perf_counter() - t0
            stop.set()
            await tick
            return result, duration, max(gaps)

        result, duration, max_gap = asyncio.run(go())
        assert "corpus_gros_00.json" in result[0].text
        assert duration > 0.15, duration          # travail réel mesurable
        assert max_gap < duration / 2, (max_gap, duration)

    def test_list_resources_and_read_resource_use_threads(self, out, monkeypatch):
        (out / "corpus_a.json").write_text(json.dumps(_items("HAL")),
                                           encoding="utf-8")
        threads = []
        real_list = mcp_server._list_resources_sync
        real_read = mcp_server._read_resource_sync

        def spy_list():
            threads.append(threading.current_thread() is threading.main_thread())
            return real_list()

        def spy_read(name, http):
            threads.append(threading.current_thread() is threading.main_thread())
            return real_read(name, http)

        monkeypatch.setattr(mcp_server, "_list_resources_sync", spy_list)
        monkeypatch.setattr(mcp_server, "_read_resource_sync", spy_read)
        resources = asyncio.run(mcp_server.list_resources())
        assert [str(r.uri) for r in resources] == ["corpus://corpus_a.json"]
        text = asyncio.run(mcp_server.read_resource("corpus://corpus_a.json"))
        assert json.loads(text) == _items("HAL")
        assert threads == [False, False]
