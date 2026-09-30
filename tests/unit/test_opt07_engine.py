"""
Tests OPT-07 — moteur commun et statuts par source (constat C30).

Aucun accès réseau : sessions HTTP simulées sous ``utils.http.HTTPClient``
(la politique HTTP réelle s'applique : réessais, 4xx, politesse), doublure de
l'API Isidore, ``get_json`` simulé pour Google Scholar (jamais SerpAPI).

Couvre :
- contrats (``SearchRequest``, ``RunContext``, ``SourceResult``,
  ``HarvestResult``) et déduction des statuts ;
- une panne n'est jamais annoncée comme « aucun article » : 503 persistant →
  ``error`` ; clé absente → ``skipped`` ; erreur à la page 2 → ``partial`` ;
  fenêtre Isidore → ``partial`` (``provider_window``) ; panne Isidore
  partagée → ``error`` pour chaque consommateur ; corps non JSON →
  ``invalid_body`` ;
- Google Scholar : crédits sous le plancher → ``skipped`` ; une page servie
  par le cache n'est pas comptée comme requête facturée ;
- adaptateurs : la CLI et ``_run_moisson_sync`` produisent les mêmes notices
  sur les mêmes réponses ; échec total → CLI code 3 sans fichier, MCP erreur
  d'outil (``ToolError``) sans fichier ; succès partiel nommé dans le texte
  et dans ``structuredContent`` (``by_source_status``, ``by_source``
  conservé).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from engine import models  # noqa: E402
from engine.models import (  # noqa: E402
    HarvestResult, RunContext, SearchReport, SearchRequest, SourceResult,
    resolve_source_status,
)
from engine import orchestrator  # noqa: E402
from utils.http import HTTPClient, reset_http_policy  # noqa: E402


# === Doublures ===

class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None,
                 headers: Optional[Dict[str, str]] = None, body: bytes = None,
                 from_cache: bool = False):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {"Content-Type": "application/json"}
        self.from_cache = from_cache
        self.url = ""
        self.reason = ""
        if body is None:
            body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.content = body
        self.text = body.decode("utf-8", "replace")

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    """Session requests simulée : ``handler(url, params, n) -> FakeResponse``."""

    def __init__(self, handler: Callable):
        self.handler = handler
        self.calls: List[tuple] = []
        self.headers: Dict[str, str] = {}

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        params = dict(params or {})
        self.calls.append((url, params))
        return self.handler(url, params, len(self.calls))

    def mount(self, *a, **k):
        pass

    def close(self):
        pass


def fake_client(handler: Callable) -> HTTPClient:
    client = HTTPClient(use_cache=False)
    client.session = FakeSession(handler)
    return client


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch):
    """Politesse et backoff instantanés ; créneaux de politesse oubliés."""
    monkeypatch.setattr("utils.http.time.sleep", lambda s: None)
    reset_http_policy()
    yield
    reset_http_policy()


def hal_doc(i: int) -> Dict:
    return {"docid": i, "halId_s": f"hal-{i:05d}", "title_s": [f"Titre HAL {i}"],
            "producedDate_s": "2020", "docType_s": "ART",
            "authFullName_s": [f"Auteur{i} Jean"],
            "uri_s": f"https://hal.science/hal-{i:05d}"}


def hal_page(start: int, n: int, total: int, cursor: str) -> Dict:
    return {"response": {"numFound": total,
                         "docs": [hal_doc(start + k) for k in range(n)]},
            "nextCursorMark": cursor}


def crossref_page(start: int, n: int, cursor: Optional[str]) -> Dict:
    items = [{"DOI": f"10.1/cr{start + k}", "title": [f"Titre CR {start + k}"],
              "type": "journal-article",
              "issued": {"date-parts": [[2020]]},
              "author": [{"family": f"Nom{start + k}", "given": "Jean"}]}
             for k in range(n)]
    message = {"items": items}
    if cursor:
        message["next-cursor"] = cursor
    return {"message": message}


# --- doublure Isidore (client simplifié, comme l'API : fenêtre de 200) ---

def isidore_record(i: int, host: str = "www.example.org") -> Dict:
    url = f"https://{host}/article/w{i}"
    return {
        "@uri": f"10670/1.w{i}",
        "isidore": {
            "title": [{"@xml:lang": "fr", "$": f"Notice Isidore {i}"}],
            "url": url,
            "enrichedCreators": {"creator": {
                "@origin": "Dupont, Jean", "firstname": "Jean",
                "lastname": f"Dupont{i}"}},
            "date": {"@startDate": "2020-01-01", "normalizedDate": "2020-01-01",
                     "year": "2020"},
            "types": {"type": ["article"]},
            "ore": {"aggregates": [{"$": url}]},
            "language": "fr",
        },
    }


class FakeIsidoreClient:
    """
    Client HTTP simplifié servant ``resource/search`` : pages de ``replies``
    notices dans une fenêtre de ``window`` résultats (au-delà : dernière page
    répétée, comme l'API). ``fail`` : fonction ``page -> FakeResponse | None``.
    """

    def __init__(self, records: List[Dict], window: int = 200,
                 fail: Optional[Callable[[int], Optional[FakeResponse]]] = None,
                 on_page: Optional[Callable[[int], None]] = None):
        self.records = records
        self.window = window
        self.fail = fail
        self.on_page = on_page
        self.pages: List[int] = []

    def get(self, url, params=None, **kw):
        params = dict(params or {})
        page = int(params.get("page", 1))
        replies = int(params.get("replies", 100))
        self.pages.append(page)
        if self.on_page is not None:
            self.on_page(page)
        if self.fail is not None:
            resp = self.fail(page)
            if resp is not None:
                return resp
        served = self.records[: self.window] if self.window else self.records
        last_page = max(1, -(-len(served) // replies))
        eff = min(page, last_page)
        chunk = served[(eff - 1) * replies: eff * replies]
        block = {"@current": str(eff)}
        if eff < last_page:
            block["@next"] = str(eff + 1)
        payload = {"response": {"replies": {
            "meta": {"@items": str(len(self.records)), "@pageItems": str(len(chunk))},
            "content": {"reply": chunk},
            "page": block,
        }}}
        return FakeResponse(payload=payload)

    def close(self):
        pass


def run(sources, client, **kw) -> HarvestResult:
    request = SearchRequest(query="q", sources=list(sources),
                            max_per_source=kw.pop("max_per_source", 50), **kw)
    ctx = RunContext(http_client=client)
    return orchestrator.harvest(request, ctx)


def by_name(result: HarvestResult) -> Dict[str, SourceResult]:
    return {r.display_name: r for r in result.sources}


# === Contrats ===

class TestContracts:
    def test_statuses_and_defaults(self):
        assert models.STATUSES == ("complete", "empty", "partial", "skipped",
                                   "error", "cancelled")
        req = SearchRequest(query="q", sources=["hal"])
        assert req.mode == "bibliographie" and req.cache is None
        assert req.workers == 1 and req.dedup is True
        with pytest.raises(ValueError):
            SearchRequest(query="q", sources=["hal"], mode="inconnu")

    def test_run_context_signal_and_deadline(self):
        import time
        ctx = RunContext(deadline=time.monotonic() - 0.01)
        assert ctx.cancelled() and ctx.cancel_reason() == "deadline"
        assert RunContext.with_timeout(60).remaining() > 59
        ctx2 = RunContext()
        assert not ctx2.cancelled()
        ctx2.cancel()
        assert ctx2.cancel_reason() == "cancelled"
        with pytest.raises(models.HarvestCancelled):
            ctx2.check()

    @pytest.mark.parametrize("report, n, expected", [
        (None, 3, "complete"),
        (None, 0, "empty"),
        (SearchReport(status="error", reason="http_error", pages=0), 0, "error"),
        (SearchReport(status="error", reason="http_error", pages=1), 0, "partial"),
        (SearchReport(status="error", reason="http_error", pages=0), 2, "partial"),
        (SearchReport(status="skipped", reason="missing_key"), 0, "skipped"),
        (SearchReport(status="partial", reason="provider_window"), 0, "partial"),
    ])
    def test_resolve_status(self, report, n, expected):
        assert resolve_source_status(report, n)[0] == expected

    def test_all_failed_rules(self):
        req = SearchRequest(query="q", sources=["hal", "isidore"])
        err = SourceResult("hal", "HAL", status="error")
        empty = SourceResult("isidore", "Isidore", status="empty")
        partial0 = SourceResult("isidore", "Isidore", status="partial")
        assert HarvestResult("r", req, [err, empty], [], 0).all_failed is False
        assert HarvestResult("r", req, [err, partial0], [], 0).all_failed is True
        ok = HarvestResult("r", req, [err, empty], [{"title": "x"}], 1)
        assert ok.all_failed is False and ok.overall_status == "partial"


# === Statuts réels des connecteurs ===

class TestSourceStatuses:
    def test_persistent_503_is_error_not_empty(self):
        client = fake_client(lambda url, p, n: FakeResponse(status=503, payload={}))
        res = by_name(run(["hal"], client))["HAL"]
        assert res.status == "error"
        assert res.reason == "http_error" and res.detail == "HTTP 503"
        assert res.items == []
        assert len(client.session.calls) == config.HTTP_MAX_RETRIES

    def test_error_on_page_two_is_partial(self):
        def handler(url, params, n):
            if params.get("cursor") == "*":
                return FakeResponse(payload=crossref_page(0, 100, "c2"))
            return FakeResponse(status=500, payload={})
        client = fake_client(handler)
        res = by_name(run(["crossref"], client, max_per_source=300))["CrossRef"]
        assert res.status == "partial" and res.reason == "http_error"
        assert res.item_count == 100 and res.pages == 1

    def test_complete_with_limit_and_exhausted(self):
        def handler(url, params, n):
            if params.get("cursor") == "*":
                return FakeResponse(payload=crossref_page(0, 100, "c2"))
            return FakeResponse(payload=crossref_page(100, 30, None))
        client = fake_client(handler)
        res = by_name(run(["crossref"], client, max_per_source=50))["CrossRef"]
        assert res.status == "complete" and res.limit_reached
        client = fake_client(handler)
        res = by_name(run(["crossref"], client, max_per_source=500))["CrossRef"]
        assert res.status == "complete" and res.exhausted and res.item_count == 130

    def test_empty_result_is_empty(self):
        client = fake_client(lambda url, p, n: FakeResponse(
            payload={"response": {"numFound": 0, "docs": []}, "nextCursorMark": "*"}))
        res = by_name(run(["hal"], client))["HAL"]
        assert res.status == "empty" and res.exhausted

    def test_non_json_body_is_invalid_body(self):
        client = fake_client(lambda url, p, n: FakeResponse(
            body=b"<html>maintenance</html>", headers={"Content-Type": "text/html"}))
        res = by_name(run(["hal"], client))["HAL"]
        assert res.status == "error" and res.reason == "invalid_body"

    def test_missing_key_is_skipped(self, monkeypatch):
        from connectors import openalex
        from utils.secrets import MissingAPIKey

        def no_key(*a, **k):
            raise MissingAPIKey("OPENALEX_API_KEY absente")
        monkeypatch.setattr(openalex, "get_api_key", no_key)
        client = fake_client(lambda *a: pytest.fail("aucune requête attendue"))
        result = run(["openalex"], client)
        res = by_name(result)["OpenAlex"]
        assert res.status == "skipped" and res.reason == "missing_key"
        assert result.all_failed

    def test_isidore_window_is_partial_provider_window(self):
        client = FakeIsidoreClient([isidore_record(i) for i in range(5000)])
        res = by_name(run(["isidore"], client, max_per_source=1000))["Isidore"]
        assert res.status == "partial" and res.reason == "provider_window"
        assert res.provider_window and res.item_count == 200
        # OPT-12 : la fenêtre en une requête (replies=200 ; auparavant [1, 2]).
        assert client.pages == [1]

    def test_isidore_cap_is_complete(self):
        client = FakeIsidoreClient([isidore_record(i) for i in range(5000)])
        res = by_name(run(["isidore"], client, max_per_source=50))["Isidore"]
        assert res.status == "complete" and res.limit_reached

    def test_subsource_absent_from_window_is_partial_not_silent(self):
        # Persée n'apparaît pas dans la fenêtre : 0 notice, mais la fenêtre
        # du fournisseur est la cause (jamais « complète » ni « vide »).
        client = FakeIsidoreClient([isidore_record(i) for i in range(5000)])
        res = by_name(run(["persee"], client, max_per_source=50))["Persée"]
        assert res.status == "partial" and res.reason == "provider_window"
        assert res.item_count == 0

    def test_shared_isidore_outage_is_error_for_every_consumer(self,
                                                              monkeypatch):
        # OPT-09 : le flux d'une moisson retente une fois la page en échec
        # (reprise différée) ; délai nul ici pour ne pas attendre 5 s.
        import config
        monkeypatch.setattr(config, "ISIDORE_DEFERRED_RETRY_S", 0.0)
        client = FakeIsidoreClient(
            [isidore_record(i) for i in range(500)],
            fail=lambda page: FakeResponse(status=503, payload={}))
        # Le client simplifié ne rejoue pas la politique : une panne franche
        # sur la page 1, partagée par les six consommateurs.
        from requests import HTTPError

        def failing_get(url, params=None, **kw):
            err = HTTPError("HTTP 503")
            err.response = FakeResponse(status=503, payload={})
            client.pages.append(int((params or {}).get("page", 1)))
            raise err
        client.get = failing_get
        result = run(["isidore", "openedition", "cairn", "persee",
                      "theses_fr", "erudit"], client)
        statuses = {r.display_name: (r.status, r.reason) for r in result.sources}
        assert statuses == {name: ("error", "http_error") for name in statuses}
        assert len(statuses) == 6
        # Une tentative partagée, puis une seule reprise différée partagée
        # (OPT-09), et non une série par consommateur.
        assert client.pages == [1, 1]
        assert result.all_failed

    def test_isidore_error_after_first_page_is_partial(self, monkeypatch):
        # Scénario sur deux pages : pages de 100 imposées (OPT-12 : défaut
        # 200, toute la fenêtre en une page).
        import config
        monkeypatch.setattr(config, "ISIDORE_REPLIES_PER_PAGE", 100)
        client = FakeIsidoreClient(
            [isidore_record(i) for i in range(500)],
            fail=lambda page: (FakeResponse(body=b"<html/>",
                                            headers={"Content-Type": "text/html"})
                               if page == 2 else None))
        res = by_name(run(["isidore"], client, max_per_source=150))["Isidore"]
        assert res.status == "partial" and res.reason == "invalid_body"
        assert res.item_count == 100


# === Google Scholar (aucune requête réelle) ===

class _ScholarHTTP:
    session = object()

    def close(self):
        pass


def _scholar_page(n: int) -> Dict:
    return {"organic_results": [
        {"title": f"Titre Scholar {i}", "link": f"https://example.org/{i}",
         "result_id": f"r{i}",
         "publication_info": {"summary": f"J Dupont{i} - Revue, 2020 - example.org"}}
        for i in range(n)]}


class TestGoogleScholar:
    def _connector(self, **kw):
        from connectors.google_scholar import GoogleScholarConnector
        kw.setdefault("api_key", "CLE-FACTICE")
        kw.setdefault("min_credits", 0)
        return GoogleScholarConnector(http_client=_ScholarHTTP(), **kw)

    def test_cached_page_is_not_billed(self, monkeypatch):
        from connectors import google_scholar as gs

        def fake(url, params=None, on_response=None, **k):
            if on_response is not None:
                on_response(FakeResponse(payload={}, from_cache=True))
            return _scholar_page(5)
        monkeypatch.setattr(gs, "get_json", fake)
        c = self._connector()
        items = c.search("discours", max_results=20)
        assert len(items) == 5
        assert c.last_status.counters["billed_requests"] == 0
        assert c.last_status.counters["cached_pages"] == 1

    def test_network_page_is_billed(self, monkeypatch):
        from connectors import google_scholar as gs

        def fake(url, params=None, on_response=None, **k):
            if on_response is not None:
                on_response(FakeResponse(payload={}, from_cache=False))
            return _scholar_page(5)
        monkeypatch.setattr(gs, "get_json", fake)
        c = self._connector()
        c.search("discours", max_results=20)
        assert c.last_status.counters["billed_requests"] == 1

    def test_revalidated_page_is_billed(self, monkeypatch):
        from connectors import google_scholar as gs

        def fake(url, params=None, on_response=None, **k):
            resp = FakeResponse(payload={}, from_cache=True)
            resp.revalidated = True
            on_response(resp)
            return _scholar_page(5)
        monkeypatch.setattr(gs, "get_json", fake)
        c = self._connector()
        c.search("discours", max_results=20)
        assert c.last_status.counters["billed_requests"] == 1

    def test_credits_below_floor_is_skipped(self, monkeypatch):
        from connectors import google_scholar as gs

        def fake(url, params=None, **k):
            assert url == gs.SERPAPI_ACCOUNT, "aucune requête facturée"
            return {"plan_name": "Test", "total_searches_left": 2}
        monkeypatch.setattr(gs, "get_json", fake)
        c = self._connector(min_credits=100)
        assert c.search("discours", max_results=20) == []
        status, reason, _ = resolve_source_status(c.last_status, 0)
        assert (status, reason) == ("skipped", "insufficient_credits")

    def test_metered_source_refused_by_request_is_skipped(self):
        request = SearchRequest(query="q", sources=["google_scholar"],
                                allow_metered=False)
        result = orchestrator.harvest(request, RunContext(
            http_client=fake_client(lambda *a: pytest.fail("aucune requête"))))
        res = result.sources[0]
        assert (res.status, res.reason) == ("skipped", "metered_not_allowed")


# === Adaptateurs CLI et MCP ===

def _hal_ranked_page(params, total: int = 120) -> "FakeResponse":
    """
    HAL en pagination par rang (``start``/``rows``, mode bibliographie
    d'OPT-12) : documents ``start`` à ``start + rows`` d'un univers de
    ``total`` documents, dans l'ordre des docid (même univers que les pages
    par curseur).
    """
    start, rows = int(params.get("start", 0)), int(params.get("rows", 10))
    n = max(0, min(rows, total - start))
    return FakeResponse(payload={"response": {
        "numFound": total, "docs": [hal_doc(start + k) for k in range(n)]}})


def _mixed_handler(url, params, n):
    """HAL : 120 notices en 2 pages ; CrossRef : 503 persistant."""
    if "archives-ouvertes" in url:
        if "start" in params:
            return _hal_ranked_page(params)
        if params.get("cursorMark") == "*":
            return FakeResponse(payload=hal_page(0, 100, 120, "c2"))
        if params.get("cursorMark") == "c2":
            return FakeResponse(payload=hal_page(100, 20, 120, "c3"))
        return FakeResponse(payload={"response": {"numFound": 120, "docs": []},
                                     "nextCursorMark": "c3"})
    if "crossref" in url:
        return FakeResponse(status=503, payload={})
    raise AssertionError(f"hôte inattendu : {url}")


class _ClientFactory:
    """Remplace HTTPClient dans un adaptateur : client à session simulée."""

    def __init__(self, handler):
        self.handler = handler

    def __call__(self, use_cache=None, **kw):
        return fake_client(self.handler)


def _canonical(items: List[Dict]) -> List[str]:
    return sorted(json.dumps(i, sort_keys=True, ensure_ascii=False) for i in items)


class TestAdapters:
    def test_cli_and_mcp_produce_the_same_notices(self, tmp_path, monkeypatch):
        import main as main_module
        import mcp_server

        cli_out = tmp_path / "cli"
        monkeypatch.setattr(main_module, "HTTPClient", _ClientFactory(_mixed_handler))
        monkeypatch.setattr(sys, "argv", [
            "main.py", "discours numérique", "--sources", "hal", "crossref",
            "--max", "200", "--output", str(cli_out)])
        assert main_module.main() == 0
        # OPT-16 : le manifeste (<stem>.manifest.json) s'ajoute aux fichiers.
        cli_corpus = [p for p in cli_out.glob("corpus_*.json")
                      if not p.name.endswith((".csl.json", ".manifest.json"))]
        assert len(cli_corpus) == 1
        cli_items = json.loads(cli_corpus[0].read_text(encoding="utf-8"))

        mcp_out = tmp_path / "mcp"
        mcp_out.mkdir()
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", mcp_out)
        monkeypatch.setattr(mcp_server, "HTTPClient", _ClientFactory(_mixed_handler))
        blocks, structured = mcp_server._run_moisson_sync(
            "discours numérique", 200, ["hal", "crossref"], False, True)
        mcp_corpus = [p for p in mcp_out.glob("corpus_*.json")
                      if not p.name.endswith((".csl.json", ".manifest.json"))]
        mcp_items = json.loads(mcp_corpus[0].read_text(encoding="utf-8"))

        assert len(cli_items) == 120
        assert _canonical(cli_items) == _canonical(mcp_items)
        # Succès partiel nommé : CrossRef en échec, HAL complet.
        st = structured["by_source_status"]
        assert st["HAL"]["status"] == "complete"
        assert st["CrossRef"]["status"] == "error"
        assert st["CrossRef"]["reason"] == "http_error"
        assert structured["by_source"] == {"HAL": 120, "CrossRef": 0}
        assert structured["harvest_status"] == "partial"
        assert "succès partiel" in blocks[0].text
        assert "CrossRef : 0 — en échec (erreur HTTP : HTTP 503)" in blocks[0].text

    def test_total_failure_cli_exit_code_and_no_file(self, tmp_path, monkeypatch,
                                                     capsys):
        import main as main_module
        monkeypatch.setattr(main_module, "HTTPClient", _ClientFactory(
            lambda url, p, n: FakeResponse(status=503, payload={})))
        monkeypatch.setattr(sys, "argv", [
            "main.py", "q", "--sources", "hal", "crossref", "--output",
            str(tmp_path)])
        assert main_module.main() == main_module.EXIT_ALL_SOURCES_FAILED == 3
        assert list(tmp_path.glob("corpus_*")) == []
        out = capsys.readouterr().out
        assert "ÉCHEC" in out and "HAL : 0 — en échec" in out

    def test_total_failure_mcp_is_tool_error_and_no_file(self, tmp_path,
                                                         monkeypatch):
        import mcp_server
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "HTTPClient", _ClientFactory(
            lambda url, p, n: FakeResponse(status=503, payload={})))
        out = mcp_server._run_moisson_sync("q", 50, ["hal", "crossref"],
                                           False, True)
        assert isinstance(out, mcp_server.ToolError)
        assert "aucune source n'a pu être moissonnée" in out
        assert out.structured["harvest_status"] == "error"
        assert {v["status"] for v in out.structured["by_source_status"].values()} == {"error"}
        assert list(tmp_path.glob("corpus_*")) == []

    def test_all_empty_is_not_an_error(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "HTTPClient", _ClientFactory(
            lambda url, p, n: FakeResponse(payload={
                "response": {"numFound": 0, "docs": []}, "nextCursorMark": "*"})))
        blocks, structured = mcp_server._run_moisson_sync("q", 50, ["hal"],
                                                          False, True)
        assert structured["by_source_status"]["HAL"]["status"] == "empty"
        assert structured["harvest_status"] == "ok"
        assert structured["total_unique"] == 0

    def test_display_names_come_from_source_labels(self, monkeypatch):
        from utils.source_labels import SOURCE_DISPLAY_NAMES
        seen = []

        def fake_run_source(name, cls, *a, source_key=None, **k):
            seen.append((source_key, name))
            return []
        monkeypatch.setattr(orchestrator, "run_source", fake_run_source)
        keys = list(SOURCE_DISPLAY_NAMES)
        orchestrator.harvest(SearchRequest(query="q", sources=keys),
                             RunContext(http_client=fake_client(lambda *a: None)))
        assert dict(seen) == SOURCE_DISPLAY_NAMES

    def test_legacy_list_runner_is_coerced(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_source",
                            lambda name, *a, **k: [{"itemType": "book",
                                                    "title": name}])
        result = orchestrator.harvest(
            SearchRequest(query="q", sources=["hal"], dedup=False),
            RunContext(http_client=fake_client(lambda *a: None)))
        assert result.sources[0].status == "complete"
        assert result.items == [{"itemType": "book", "title": "HAL"}]

    def test_schedule_is_replaceable(self, monkeypatch):
        order = []
        monkeypatch.setattr(orchestrator, "run_source",
                            lambda name, *a, **k: order.append(name) or [])
        orchestrator.harvest(
            SearchRequest(query="q", sources=["openedition", "hal"]),
            RunContext(http_client=fake_client(lambda *a: None)),
            schedule=lambda sources: [list(sources)])
        assert order == ["OpenEdition", "HAL"]
        order.clear()
        # Topologie plate (OPT-09) : le premier consommateur du flux Isidore
        # et les sources indépendantes d'abord (ordre demandé à priorité
        # égale), les autres consommateurs du flux ensuite.
        orchestrator.harvest(
            SearchRequest(query="q", sources=["openedition", "cairn", "hal"]),
            RunContext(http_client=fake_client(lambda *a: None)))
        assert order == ["OpenEdition", "HAL", "Cairn"]
