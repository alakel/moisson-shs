"""
Tests OPT-07 — corrections issues de la revue (moteur, statuts, annulation).

Aucun accès réseau, aucune socket (sessions et API simulées ; transport MCP
du SDK en mémoire). Chaque classe correspond à un constat de revue :

- échec total : une sous-source d'Isidore absente de la fenêtre de 200
  résultats (``partial`` / ``provider_window``, 0 notice) n'est PAS une
  panne (pas d'``isError``, code CLI 0, corpus vide publié) ;
- annulation par le client via le SDK réel (``notifications/cancelled``) :
  le gestionnaire attend la fin réelle du fil, ``LAST_CANCEL_STOP_S`` la
  mesure, et en HTTP la place du sémaphore n'est rendue qu'à la fin du fil ;
- aucune publication après une annulation ou une échéance survenue pendant
  la déduplication ou juste avant l'écriture ; un fil qui a déjà publié
  quand l'échéance tombe rend son résultat réel ;
- payloads bruts ``raw/`` supprimés à l'annulation (source terminée) et en
  cas d'échec total ;
- corps non JSON servi dans un vrai ``requests.Response`` → motif
  ``invalid_body`` (et non ``network_error``) ;
- notices reçues mais toutes illisibles (mappage) → jamais « aucun
  résultat » ;
- Google Scholar tronqué par ``MOISSON_SERPAPI_MAX_PAGES`` → ``partial``
  (budget), pas « complète ».
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List

import anyio
import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, os.path.dirname(__file__))

import mcp.types as types  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.models import (  # noqa: E402
    HarvestResult, RunContext, SearchReport, SearchRequest, SourceResult,
    classify_exception, resolve_source_status,
)
from engine.outputs import write_raw  # noqa: E402
from utils.http import OperationCancelled, reset_http_policy  # noqa: E402

from test_opt07_engine import (  # noqa: E402
    FakeIsidoreClient, FakeResponse, _ScholarHTTP, _scholar_page, by_name,
    _hal_ranked_page, fake_client, hal_page, isidore_record, run,
)


# Attente réelle, capturée avant toute neutralisation de ``time.sleep``
# (``utils.http.time`` EST le module ``time`` : la fixture ``_no_waits``
# neutralise l'attente de tout le processus).
_real_sleep = time.sleep


@pytest.fixture(autouse=True)
def _fresh_policy():
    """Créneaux de politesse oubliés entre deux tests."""
    reset_http_policy()
    yield
    reset_http_policy()


@pytest.fixture
def _no_waits(monkeypatch):
    """Politesse et backoff instantanés (réponses HTTP simulées)."""
    monkeypatch.setattr("utils.http.time.sleep", lambda s: None)


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
    monkeypatch.setattr(mcp_server, "LAST_CANCEL_STOP_S", None)
    return tmp_path


def _items(name: str, n: int = 3) -> List[Dict]:
    return [{"itemType": "journalArticle", "title": f"{name} titre {i}",
             "date": "2020", "DOI": f"10.0/{name.lower()}.{i}",
             "creators": [{"creatorType": "author", "lastName": f"{name}{i}"}],
             "extra": f"Sources: {name}"} for i in range(n)]


def _corpus_files(directory: Path) -> List[str]:
    return sorted(p.name for p in directory.glob("corpus_*"))


def _data_files(directory: Path) -> List[str]:
    """Fichiers de données d'un corpus publié (JSON, CSL-JSON, statistiques),
    sans le manifeste ajouté par OPT-16 (qui est vérifié à part)."""
    names = _corpus_files(directory)
    assert sum(n.endswith(".manifest.json") for n in names) == 1, names
    return [n for n in names if not n.endswith(".manifest.json")]


def _raw_files(directory: Path) -> List[str]:
    raw = directory / "raw"
    return sorted(p.name for p in raw.glob("*.json")) if raw.is_dir() else []


def _isidore_factory(records):
    """Remplace HTTPClient dans un adaptateur par la doublure Isidore."""
    def factory(use_cache=None, **kw):
        client = FakeIsidoreClient(records)
        client.cache_enabled = False
        return client
    return factory


# === Échec total : fenêtre Isidore ≠ panne ===

@pytest.mark.usefixtures("_no_waits")
class TestTotalFailureRule:
    def test_bounded_partial_is_not_a_failure(self):
        req = SearchRequest(query="q", sources=["cairn", "persee"])

        def res(status, reason=None):
            return SourceResult("cairn", "Cairn", status=status, reason=reason)

        def failed(*results):
            return HarvestResult("r", req, list(results), [], 0).all_failed

        # Limite du fournisseur : exécutée normalement, pas une panne.
        assert failed(res("partial", "provider_window")) is False
        assert failed(res("partial", "scan_bound")) is False
        assert failed(res("error", "http_error"),
                      res("partial", "provider_window")) is False
        # Partielle née d'une panne, sans notice : échec.
        for reason in ("http_error", "network_error", "invalid_body",
                       "budget_exhausted", "exception", None):
            assert failed(res("partial", reason)) is True, reason
        assert failed(res("error"), res("skipped"), res("cancelled")) is True
        assert res("partial", "provider_window").executed is True
        assert res("partial", "http_error").executed is False

    def test_proxies_absent_from_window_mcp_is_not_an_error(self, out,
                                                            monkeypatch):
        # Isidore sain (5 000 annoncés, fenêtre de 200) sans aucune notice
        # cairn.info ni persee.fr : les deux proxies sont partiels à 0.
        monkeypatch.setattr(mcp_server, "HTTPClient", _isidore_factory(
            [isidore_record(i) for i in range(5000)]))
        result = mcp_server._run_moisson_sync("discours", 50,
                                              ["cairn", "persee"], False, True)
        assert not isinstance(result, mcp_server.ToolError)
        blocks, structured = result
        st = structured["by_source_status"]
        assert {k: (v["status"], v["reason"], v["items"]) for k, v in st.items()} == {
            "Cairn": ("partial", "provider_window", 0),
            "Persée": ("partial", "provider_window", 0),
        }
        assert structured["harvest_status"] == "partial"
        assert structured["total_unique"] == 0
        text = blocks[0].text
        # OPT-17 : une limite du fournisseur n'est pas un succès partiel
        # (réservé aux vraies pertes) ; elle est nommée comme telle.
        assert "succès partiel" not in text and "en échec" not in text
        assert "limite du fournisseur" in text and "sans panne" in text
        assert "fenêtre de résultats du fournisseur atteinte" in text
        # Corpus vide publié (comportement d'avant OPT-07, statut en plus).
        # OPT-16 : le manifeste (<stem>.manifest.json) s'ajoute aux fichiers.
        corpus = [p for p in out.glob("corpus_*.json")
                  if not p.name.endswith((".csl.json", ".manifest.json"))]
        assert len(corpus) == 1
        assert json.loads(corpus[0].read_text(encoding="utf-8")) == []

    def test_proxy_absent_from_window_through_sdk_is_not_is_error(
            self, out, monkeypatch):
        from mcp.shared.memory import create_connected_server_and_client_session
        monkeypatch.setattr(mcp_server, "HTTPClient", _isidore_factory(
            [isidore_record(i) for i in range(5000)]))

        async def go():
            async with create_connected_server_and_client_session(
                    mcp_server.server) as client:
                return await client.call_tool(
                    "moisson_search", {"query": "discours", "sources": ["cairn"]})

        res = asyncio.run(go())
        assert res.isError is False
        assert res.structuredContent["harvest_status"] == "partial"
        assert res.structuredContent["by_source_status"]["Cairn"]["reason"] == \
            "provider_window"

    def test_proxy_absent_from_window_cli_exit_zero(self, tmp_path,
                                                    monkeypatch, capsys):
        import main as main_module
        monkeypatch.setattr(main_module, "HTTPClient", _isidore_factory(
            [isidore_record(i) for i in range(5000)]))
        monkeypatch.setattr(sys, "argv", [
            "main.py", "discours", "--sources", "cairn", "--max", "50",
            "--output", str(tmp_path)])
        assert main_module.main() == 0
        assert len(_data_files(tmp_path)) == 3
        stdout = capsys.readouterr().out
        assert "ÉCHEC" not in stdout
        assert "Cairn : 0 — partielle" in stdout

    def test_real_outage_still_is_total_failure(self, out, monkeypatch):
        """Contrôle : une panne Isidore partagée reste un échec total."""
        from requests import HTTPError

        class _Down:
            cache_enabled = False

            def get(self, url, params=None, **kw):
                err = HTTPError("HTTP 503")
                err.response = FakeResponse(status=503, payload={})
                raise err

            def close(self):
                pass

        monkeypatch.setattr(mcp_server, "HTTPClient", lambda **kw: _Down())
        result = mcp_server._run_moisson_sync("q", 50, ["cairn", "persee"],
                                              False, True)
        assert isinstance(result, mcp_server.ToolError)
        assert result.structured["harvest_status"] == "error"


# === Annulation par le client via le SDK réel ===

class _Threads:
    """Suit les fils de moisson : démarrages, fins, simultanéité."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = 0
        self.max = 0
        self.starts: List[float] = []
        self.ends: List[float] = []

    def enter(self):
        with self.lock:
            self.running += 1
            self.max = max(self.max, self.running)
            self.starts.append(time.monotonic())

    def leave(self):
        with self.lock:
            self.running -= 1
            self.ends.append(time.monotonic())


def _slow_after_cancel(threads: _Threads, tail: float):
    """Exécutant de source : attend l'annulation, puis simule une requête
    en vol non interruptible de ``tail`` secondes ; requête « rapide » :
    notices immédiates."""
    def stub(name, cls, query, mps, output_dir, run_id, http_client,
             run_context=None, source_key=None, **kw):
        threads.enter()
        try:
            if query == "rapide":
                return SourceResult(source=source_key, display_name=name,
                                    items=_items(name), status="complete")
            while not run_context.cancelled():
                _real_sleep(0.01)
            _real_sleep(tail)
            return SourceResult(source=source_key, display_name=name,
                                status="cancelled", reason="cancelled")
        finally:
            threads.leave()
    return stub


def _spy_handler_exit(monkeypatch, marks: Dict[str, float]):
    real = mcp_server._run_guarded

    async def spy(*a, **k):
        try:
            return await real(*a, **k)
        finally:
            marks.setdefault("handler_exit", time.monotonic())
    monkeypatch.setattr(mcp_server, "_run_guarded", spy)


async def _cancel_through_sdk(marks: Dict, second_query: str = None):
    """Appel moisson_search puis ``notifications/cancelled`` du client ;
    éventuellement une seconde moisson juste après."""
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(
            mcp_server.server) as client:
        async def first():
            try:
                await client.call_tool("moisson_search",
                                       {"query": "lente", "sources": ["hal"]})
            except Exception as e:  # noqa: BLE001 - réponse d'annulation
                marks["first_error"] = str(e)

        async with anyio.create_task_group() as tg:
            tg.start_soon(first)
            await anyio.sleep(0.3)
            request_id = client._request_id - 1
            marks["cancel_at"] = time.monotonic()
            await client.send_notification(types.ClientNotification(
                types.CancelledNotification(
                    method="notifications/cancelled",
                    params=types.CancelledNotificationParams(
                        requestId=request_id, reason="test"))))
        if second_query is not None:
            with anyio.fail_after(10):
                marks["second"] = await client.call_tool(
                    "moisson_search", {"query": second_query, "sources": ["hal"]})


class TestSdkClientCancellation:
    TAIL = 0.5

    def test_stdio_handler_waits_for_the_real_end_of_the_thread(
            self, out, monkeypatch):
        threads = _Threads()
        monkeypatch.setattr("engine.orchestrator.run_source",
                            _slow_after_cancel(threads, self.TAIL))
        marks: Dict = {}
        _spy_handler_exit(monkeypatch, marks)

        asyncio.run(_cancel_through_sdk(marks))

        assert "cancelled" in marks["first_error"].lower()
        assert len(threads.ends) == 1
        # Le gestionnaire ne rend la main qu'après la fin réelle du fil.
        assert marks["handler_exit"] >= threads.ends[0]
        assert threads.running == 0
        # Mesure réelle du délai d'arrêt (la requête en vol simulée).
        assert mcp_server.LAST_CANCEL_STOP_S is not None
        assert mcp_server.LAST_CANCEL_STOP_S >= self.TAIL - 0.1
        assert _corpus_files(out) == []

    def test_http_semaphore_is_released_only_after_the_thread(
            self, out, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "MOISSON_MAX_CONCURRENT", 1)
        monkeypatch.setattr(mcp_server, "MOISSON_ACQUIRE_TIMEOUT", 5.0)
        monkeypatch.setattr(mcp_server, "MOISSON_DEADLINE_SECONDS", 60.0)
        threads = _Threads()
        monkeypatch.setattr("engine.orchestrator.run_source",
                            _slow_after_cancel(threads, self.TAIL))
        marks: Dict = {}

        asyncio.run(_cancel_through_sdk(marks, second_query="rapide"))

        # La seconde moisson n'a démarré qu'après la fin du premier fil :
        # jamais deux fils pour MOISSON_MAX_CONCURRENT = 1.
        assert threads.max == 1
        assert len(threads.starts) == 2
        assert threads.starts[1] >= threads.ends[0]
        assert marks["second"].isError is False


# === Aucune publication après annulation ou échéance ===

def _fast_runner(output_dir_written: List[Path] = None):
    def stub(name, cls, query, mps, output_dir, run_id, http_client,
             run_context=None, source_key=None, **kw):
        items = _items(name)
        if output_dir is not None:
            path = write_raw(output_dir, name, run_id, items)
            if output_dir_written is not None:
                output_dir_written.append(path)
        return SourceResult(source=source_key, display_name=name,
                            items=items, status="complete")
    return stub


class TestNoPublicationAfterCancellation:
    def test_cancel_during_dedup_raises_and_cleans_raw(self, tmp_path,
                                                       monkeypatch):
        written: List[Path] = []
        monkeypatch.setattr(orchestrator, "run_source", _fast_runner(written))
        ctx = RunContext(output_dir=tmp_path,
                         http_client=fake_client(lambda *a: None))
        real = orchestrator.deduplicate_items

        def dedup_then_cancel(items):
            out = real(items)
            ctx.cancel()              # annulation pendant la déduplication
            return out
        monkeypatch.setattr(orchestrator, "deduplicate_items", dedup_then_cancel)

        with pytest.raises(OperationCancelled) as exc:
            orchestrator.harvest(SearchRequest(query="q",
                                               sources=["hal", "crossref"]), ctx)
        assert exc.value.result is not None
        assert len(written) == 2 and not any(p.exists() for p in written)

    def test_http_deadline_during_dedup_publishes_nothing(self, out,
                                                          monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "MOISSON_DEADLINE_SECONDS", 0.3)
        monkeypatch.setattr(orchestrator, "run_source", _fast_runner())
        real = orchestrator.deduplicate_items

        def slow_dedup(items):
            _real_sleep(0.8)          # déduplication d'un gros corpus
            return real(items)
        monkeypatch.setattr(orchestrator, "deduplicate_items", slow_dedup)

        result = asyncio.run(mcp_server._tool_moisson_search(
            {"query": "q", "sources": ["hal", "crossref"]}))
        assert isinstance(result, mcp_server.ToolError) and result.cancelled
        assert "deadline" in result
        assert _corpus_files(out) == []
        assert _raw_files(out) == []

    def test_run_moisson_sync_checks_before_writing(self, out, monkeypatch):
        """Annulation tombée entre la fin de la moisson et l'écriture."""
        def harvest_then_cancel(request, ctx):
            result = HarvestResult(
                run_id=ctx.run_id, request=request,
                sources=[SourceResult("hal", "HAL", items=_items("HAL"))],
                items=_items("HAL"), total_before_dedup=3)
            write_raw(ctx.output_dir, "HAL", ctx.run_id, result.items)
            ctx.cancel()
            return result
        monkeypatch.setattr(mcp_server, "harvest", harvest_then_cancel)

        with pytest.raises(OperationCancelled):
            mcp_server._run_moisson_sync("q", 10, ["hal"], False, True,
                                         cancel_event=threading.Event())
        assert _corpus_files(out) == []
        assert _raw_files(out) == []

    def test_thread_that_already_published_returns_its_real_result(
            self, out, monkeypatch):
        """L'échéance tombe pendant l'écriture (après le dernier contrôle) :
        le corpus est publié ; l'outil le dit au lieu d'annoncer à tort
        « aucun corpus partiel publié »."""
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "MOISSON_DEADLINE_SECONDS", 0.2)
        monkeypatch.setattr(orchestrator, "run_source", _fast_runner())
        real_write = mcp_server.write_outputs

        def slow_write(*a, **k):
            _real_sleep(0.5)
            return real_write(*a, **k)
        monkeypatch.setattr(mcp_server, "write_outputs", slow_write)

        result = asyncio.run(mcp_server._tool_moisson_search(
            {"query": "q", "sources": ["hal"]}))
        assert not isinstance(result, mcp_server.ToolError)
        blocks, structured = result
        assert structured["total_unique"] == 3
        assert len(_data_files(out)) == 3


# === Payloads bruts : annulation et échec total ===

@pytest.mark.usefixtures("_no_waits")
class TestRawCleanup:
    def test_cancel_after_a_finished_source_removes_its_raw(self, tmp_path,
                                                           monkeypatch):
        """La 1re source se termine et écrit son raw/ ; l'annulation arrive
        avant la 2e : raw/ supprimé, 2e source jamais démarrée."""
        written: List[Path] = []
        ran: List[str] = []
        ctx = RunContext(output_dir=tmp_path,
                         http_client=fake_client(lambda *a: None))
        runner = _fast_runner(written)

        def stub(name, *a, **k):
            ran.append(name)
            res = runner(name, *a, **k)
            ctx.cancel()
            return res
        monkeypatch.setattr(orchestrator, "run_source", stub)

        with pytest.raises(OperationCancelled):
            orchestrator.harvest(SearchRequest(query="q",
                                               sources=["hal", "crossref"]), ctx)
        assert ran == ["HAL"]
        assert len(written) == 1 and not written[0].exists()

    def test_total_failure_leaves_no_raw_mcp(self, out, monkeypatch):
        from test_opt07_engine import _ClientFactory
        monkeypatch.setattr(mcp_server, "HTTPClient", _ClientFactory(
            lambda url, p, n: FakeResponse(status=503, payload={})))
        result = mcp_server._run_moisson_sync("q", 50, ["hal", "crossref"],
                                              False, True)
        assert isinstance(result, mcp_server.ToolError)
        assert _corpus_files(out) == []
        assert _raw_files(out) == []

    def test_total_failure_leaves_no_raw_cli(self, tmp_path, monkeypatch):
        import main as main_module
        from test_opt07_engine import _ClientFactory
        monkeypatch.setattr(main_module, "HTTPClient", _ClientFactory(
            lambda url, p, n: FakeResponse(status=503, payload={})))
        monkeypatch.setattr(sys, "argv", [
            "main.py", "q", "--sources", "hal", "crossref", "--output",
            str(tmp_path)])
        assert main_module.main() == main_module.EXIT_ALL_SOURCES_FAILED
        assert _corpus_files(tmp_path) == []
        assert _raw_files(tmp_path) == []

    def test_partial_success_keeps_raw(self, out, monkeypatch):
        """Contrôle : une moisson publiée garde ses payloads bruts."""
        monkeypatch.setattr(orchestrator, "run_source", _fast_runner())
        mcp_server._run_moisson_sync("q", 10, ["hal"], False, True)
        assert len(_raw_files(out)) == 1


# === Corps non JSON dans un vrai requests.Response ===

class _HtmlSession:
    """Session simulée renvoyant un vrai ``requests.Response`` HTML (200)."""
    headers: Dict[str, str] = {}

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        r = requests.models.Response()
        r.status_code = 200
        r._content = b"<html><body>Maintenance</body></html>"
        r.headers["Content-Type"] = "text/html"
        r.encoding = "utf-8"
        r.url = url
        return r

    def mount(self, *a, **k):
        pass

    def close(self):
        pass


@pytest.mark.usefixtures("_no_waits")
class TestInvalidBody:
    def test_requests_json_decode_error_is_invalid_body(self):
        resp = _HtmlSession().get("https://example.org/")
        with pytest.raises(requests.exceptions.JSONDecodeError) as exc:
            resp.json()
        reason, detail = classify_exception(exc.value)
        assert reason == "invalid_body"
        assert "illisible" in detail
        # Les autres erreurs requests restent des erreurs réseau.
        assert classify_exception(requests.ConnectionError())[0] == "network_error"
        assert classify_exception(requests.exceptions.InvalidURL())[0] == "network_error"

    @pytest.mark.parametrize("source, name", [("hal", "HAL"),
                                              ("crossref", "CrossRef")])
    def test_html_page_served_200_is_invalid_body(self, source, name):
        client = fake_client(lambda *a: None)
        client.session = _HtmlSession()
        res = by_name(run([source], client))[name]
        assert (res.status, res.reason) == ("error", "invalid_body")
        assert "réponse illisible" in res.describe()


# === Échec de mappage ===

def _hal_two_pages(url, params, n):
    # Mode bibliographie (OPT-12, défaut de SearchRequest) : pagination par
    # rang, même univers de 120 documents.
    if "start" in params:
        return _hal_ranked_page(params)
    if params.get("cursorMark") == "*":
        return FakeResponse(payload=hal_page(0, 100, 120, "c2"))
    if params.get("cursorMark") == "c2":
        return FakeResponse(payload=hal_page(100, 20, 120, "c3"))
    return FakeResponse(payload={"response": {"numFound": 120, "docs": []},
                                 "nextCursorMark": "c3"})


@pytest.mark.usefixtures("_no_waits")
class TestMappingFailures:
    def test_all_notices_unmappable_is_never_empty(self, monkeypatch):
        import connectors.hal as hal

        def broken(doc):
            raise KeyError("title_s")
        monkeypatch.setattr(hal, "map_hal_to_zotero", broken)
        result = run(["hal"], fake_client(_hal_two_pages), max_per_source=500)
        res = result.sources[0]
        assert res.status == "partial" and res.reason == "invalid_body"
        assert res.counters["mapping_errors"] == 120
        assert "120 notice(s)" in res.detail
        assert any("mappage" in w for w in res.warnings)
        assert result.all_failed and result.overall_status == "error"

    def test_some_notices_unmappable_keeps_status_with_warning(self,
                                                               monkeypatch):
        import connectors.hal as hal
        real = hal.map_hal_to_zotero

        def flaky(doc):
            if doc["docid"] % 10 == 0:
                raise KeyError("title_s")
            return real(doc)
        monkeypatch.setattr(hal, "map_hal_to_zotero", flaky)
        res = run(["hal"], fake_client(_hal_two_pages),
                  max_per_source=500).sources[0]
        assert res.status == "complete" and res.item_count == 108
        assert res.counters["mapping_errors"] == 12
        assert any("12 notice(s)" in w for w in res.warnings)

    @pytest.mark.parametrize("report, n, expected", [
        (SearchReport(pages=2, counters={"mapping_errors": 5}), 0,
         ("partial", "invalid_body")),
        (SearchReport(pages=0, counters={"mapping_errors": 5}), 0,
         ("error", "invalid_body")),
        (SearchReport(pages=2, counters={"mapping_errors": 5}), 3,
         ("complete", None)),
        (SearchReport(pages=1, status="skipped", reason="missing_key",
                      counters={"mapping_errors": 1}), 0,
         ("skipped", "missing_key")),
    ])
    def test_resolve_status_with_mapping_errors(self, report, n, expected):
        assert resolve_source_status(report, n)[:2] == expected


# === Google Scholar : plafond de requêtes facturées ===

@pytest.mark.usefixtures("_no_waits")
class TestScholarBudgetTruncation:
    def _connector(self, **kw):
        from connectors.google_scholar import GoogleScholarConnector
        kw.setdefault("api_key", "CLE-FACTICE")
        kw.setdefault("min_credits", 0)
        return GoogleScholarConnector(http_client=_ScholarHTTP(), **kw)

    def _full_pages(self, monkeypatch):
        from connectors import google_scholar as gs
        monkeypatch.setattr(gs, "get_json",
                            lambda url, params=None, **k: _scholar_page(20))

    def test_truncated_by_max_pages_is_partial_budget(self, monkeypatch):
        self._full_pages(monkeypatch)
        c = self._connector(max_pages=2)
        items = c.search("discours", max_results=100)
        assert len(items) == 40
        status, reason, detail = resolve_source_status(c.last_status, len(items))
        assert (status, reason) == ("partial", "budget_exhausted")
        assert "MOISSON_SERPAPI_MAX_PAGES" in detail
        assert c.last_status.limit_reached is False

    def test_requested_cap_within_budget_is_complete(self, monkeypatch):
        self._full_pages(monkeypatch)
        c = self._connector(max_pages=2)
        items = c.search("discours", max_results=40)
        assert len(items) == 40
        status, _, _ = resolve_source_status(c.last_status, len(items))
        assert status == "complete" and c.last_status.limit_reached
