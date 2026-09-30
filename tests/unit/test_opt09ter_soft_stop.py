"""
OPT-09 ter : les connecteurs paginés appliquent le contrat
``RunContext.soft_stop`` (échéance souple de ``moisson_search`` en stdio).

Avant : une source paginée arrêtée par l'échéance souple perdait les notices
des pages déjà reçues (l'annulation traversait la boucle du connecteur ;
``run_source`` rendait une liste vide, source ``error/deadline``). Après :

- entre deux pages, ou pendant une requête en vol (délai coupé par
  l'échéance), le connecteur s'arrête, rend les notices déjà reçues et
  publie ``partial``, motif ``deadline``, sans demander de page de plus :
  HAL (collecte et bibliographie), CrossRef, OpenAlex, Semantic Scholar,
  CORE, Google Scholar, lecture du flux Isidore (source directe et
  sous-sources), boucles d'enrichissement OAI (OpenEdition, Cairn, Persée,
  Érudit : notices déjà enrichies conservées, les autres avec leurs
  métadonnées Isidore) ;
- sans notice reçue, l'arrêt est propagé (statut historique
  ``error/deadline`` de l'orchestrateur) ;
- l'annulation par le client et l'échéance dure sont propagées telles
  quelles (la moisson est annulée, rien n'est publié) ; sans échéance souple
  (CLI, HTTP, résolveurs), rien ne change.

Aucun accès réseau ni socket : transport ``requests`` en mémoire, OAI
(sickle) doublé. Les arrêts « entre deux pages » et « en vol » sont
déclenchés de façon déterministe en faisant échoir le signal propre à la
source (``RunContext.for_source``) ; les tests de bout en bout (moteur,
serveur MCP) utilisent une vraie échéance.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

import config
import connectors.cairn as cairn_module
import connectors.core as core_module
import connectors.crossref as crossref_module
import connectors.erudit as erudit_module
import connectors.hal as hal_module
import connectors.openalex as openalex_module
import connectors.openedition as openedition_module
import connectors.persee as persee_module
import mcp_server
from connectors.cairn import CairnConnector
from connectors.core import CoreConnector
from connectors.crossref import CrossrefConnector
from connectors.erudit import EruditConnector
from connectors.google_scholar import GoogleScholarConnector
from connectors.hal import HALConnector
from connectors.isidore import IsidoreConnector
from connectors.isidore_scan import ScanRegistry
from connectors.openalex import OpenAlexConnector
from connectors.openedition import OpenEditionConnector
from connectors.persee import PerseeConnector
from connectors.semantic_scholar import SemanticScholarConnector
from engine import orchestrator
from engine.models import (
    MODE_BIBLIOGRAPHIE, MODE_COLLECTE, REASON_DEADLINE, REASON_PROVIDER_WINDOW,
    STATUS_COMPLETE, STATUS_PARTIAL, RunContext, SearchReport, SearchRequest,
)
from utils import http as http_module
from utils.http import (
    CANCEL_REASON_CANCELLED, CANCEL_REASON_DEADLINE, HTTPClient,
    OperationCancelled, reset_http_policy,
)


# === Outils communs ===

def _response(request, payload, status=200):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(payload).encode("utf-8")
    resp.headers["Content-Type"] = "application/json"
    resp.encoding = "utf-8"
    resp.url = request.url
    resp.request = request
    return resp


def _query(request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}


class _Api(BaseAdapter):
    """
    Service en mémoire : ``build(n, request)`` rend le corps JSON de la
    n-ième requête ; ``hook(n, request)``, appelé avant, peut faire échoir un
    signal, annuler l'exécution ou lever une exception réseau.
    """

    def __init__(self, build, hook=None):
        super().__init__()
        self.build = build
        self.hook = hook
        self.sent = 0

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        self.sent += 1
        if self.hook is not None:
            self.hook(self.sent, request)
        return _response(request, self.build(self.sent, request))

    def close(self):
        pass


def _expire(signal) -> None:
    """Fait échoir un signal d'annulation (horloge monotone)."""
    signal.deadline = time.monotonic() - 0.001


def _timeout(request):
    raise requests.exceptions.ReadTimeout("délai coupé (simulé)", request=request)


@pytest.fixture(autouse=True)
def _no_politeness(monkeypatch):
    """Politesse par hôte ramenée à zéro (les tests comptent des pages, pas
    des secondes)."""
    reset_http_policy()
    monkeypatch.setattr(http_module, "REQUEST_DELAY", 0.0)
    for host in list(http_module.HOST_MIN_INTERVALS):
        monkeypatch.setitem(http_module.HOST_MIN_INTERVALS, host, 0.0)
    for key in list(http_module.REQUEST_CLASS_INTERVALS):
        monkeypatch.setitem(http_module.REQUEST_CLASS_INTERVALS, key, 0.0)
    yield
    reset_http_policy()


def _client(prefix, adapter) -> HTTPClient:
    client = HTTPClient(use_cache=False)
    client.session.mount(prefix, adapter)
    return client


def _soft_ctx(client, seconds=60.0, **kwargs) -> RunContext:
    return RunContext(soft_deadline=time.monotonic() + seconds,
                      soft_deadline_s=seconds, soft_grace_s=0.5,
                      http_client=client, **kwargs)


# === Corps de page par fournisseur (PAGE notices distinctes par page) ===

PAGE = 3
MAX = 30        # dix pages : la pagination continuerait sans arrêt


def _hal_page(doc):
    def build(n, request):
        q = _query(request)
        start = int(q.get("start", (n - 1) * PAGE))
        docs = []
        for i in range(int(q.get("rows", PAGE))):
            d = copy.deepcopy(doc)
            d["docid"] = 900000 + start + i
            d["halId_s"] = f"hal-0{start + i}"
            d["title_s"] = [f"Titre HAL opt09ter {start + i}"]
            # Notices distinctes pour la déduplication finale (pas de DOI
            # dans la fixture) : auteur et URL propres.
            d["authFullName_s"] = [f"Auteur{start + i} Nom{start + i}"]
            d["uri_s"] = f"https://hal.science/hal-0{start + i}v1"
            d["fileMain_s"] = f"https://hal.science/hal-0{start + i}/document"
            d["score"] = 10.0 - (start + i) / 100
            docs.append(d)
        return {"response": {"numFound": 1000, "docs": docs},
                "nextCursorMark": f"c{n}"}
    return build


def _crossref_page(work):
    def build(n, request):
        items = []
        for i in range(PAGE):
            k = (n - 1) * PAGE + i
            w = copy.deepcopy(work)
            w["DOI"] = f"10.9999/crossref.opt09ter.{k}"
            w["title"] = [f"Titre CrossRef {k}"]
            w["score"] = 30.0 - k / 10
            items.append(w)
        return {"message": {"items": items, "next-cursor": f"c{n}"}}
    return build


def _openalex_page(work):
    def build(n, request):
        results = []
        for i in range(PAGE):
            k = (n - 1) * PAGE + i
            w = copy.deepcopy(work)
            w["id"] = f"https://openalex.org/W9{k:06d}"
            w["doi"] = f"https://doi.org/10.9999/openalex.opt09ter.{k}"
            w["ids"] = {"openalex": w["id"], "doi": w["doi"]}
            w["title"] = w["display_name"] = f"Titre OpenAlex {k}"
            w["relevance_score"] = 500.0 - k
            results.append(w)
        return {"results": results, "meta": {"next_cursor": f"c{n}"}}
    return build


def _s2_page(paper):
    def build(n, request):
        data = []
        for i in range(PAGE):
            k = (n - 1) * PAGE + i
            p = copy.deepcopy(paper)
            p["title"] = f"Titre S2 {k}"
            p["externalIds"] = {"DOI": f"10.9999/s2.opt09ter.{k}",
                                "CorpusId": 7000000 + k}
            data.append(p)
        return {"data": data, "token": f"t{n}"}
    return build


def _core_page(work, size=PAGE):
    def build(n, request):
        offset = int(_query(request).get("offset", 0))
        results = []
        for i in range(size):
            w = copy.deepcopy(work)
            w["id"] = str(80000000 + offset + i)
            w["doi"] = f"10.9999/core.opt09ter.{offset + i}"
            w["title"] = f"Titre CORE {offset + i}"
            results.append(w)
        return {"results": results, "totalHits": 50000}
    return build


def _scholar_page():
    def build(n, request):
        start = int(_query(request).get("start", 0))
        results = [{
            "title": f"Titre Scholar {start + i}",
            "result_id": f"opt09ter{start + i}",
            "link": f"https://example.org/scholar/{start + i}",
            "publication_info": {"summary": "A Auteur - Revue X, 2020 - "
                                            "example.org"},
        } for i in range(PAGE)]
        return {"organic_results": results,
                "search_information": {"total_results": 1000}}
    return build


class _Spec:
    def __init__(self, prefix, make, build, search=None, ranked=True):
        self.prefix = prefix
        self.make = make            # (http, ctx) -> connecteur
        self.build = build          # fixtures -> build(n, request)
        self.search = search or {}
        self.ranked = ranked        # rangs natifs publiés


SPECS = {
    "hal_collecte": _Spec(
        "https://api.archives-ouvertes.fr/",
        lambda http, ctx: HALConnector(http_client=http, run_context=ctx),
        lambda fx: _hal_page(fx["hal"]), {"mode": MODE_COLLECTE}),
    "hal_bibliographie": _Spec(
        "https://api.archives-ouvertes.fr/",
        lambda http, ctx: HALConnector(http_client=http, run_context=ctx),
        lambda fx: _hal_page(fx["hal"]), {"mode": MODE_BIBLIOGRAPHIE}),
    "crossref": _Spec(
        "https://api.crossref.org/",
        lambda http, ctx: CrossrefConnector(http_client=http, run_context=ctx),
        lambda fx: _crossref_page(fx["crossref"]), {"mode": MODE_BIBLIOGRAPHIE}),
    "openalex": _Spec(
        "https://api.openalex.org/",
        lambda http, ctx: OpenAlexConnector(http_client=http, run_context=ctx,
                                            api_key="cle-factice"),
        lambda fx: _openalex_page(fx["openalex"])),
    "semantic_scholar": _Spec(
        "https://api.semanticscholar.org/",
        lambda http, ctx: SemanticScholarConnector(
            http_client=http, run_context=ctx, api_key="cle-factice",
            min_interval=0.0),
        lambda fx: _s2_page(fx["s2"]), {"mode": MODE_BIBLIOGRAPHIE}),
    "core": _Spec(
        "https://api.core.ac.uk/",
        lambda http, ctx: CoreConnector(http_client=http, run_context=ctx,
                                        api_key="cle-factice", min_interval=0.0),
        lambda fx: _core_page(fx["core"]), ranked=False),
    "google_scholar": _Spec(
        "https://serpapi.com/",
        lambda http, ctx: GoogleScholarConnector(
            http_client=http, run_context=ctx, api_key="cle-factice",
            min_interval=0.0, max_pages=MAX // PAGE, page_size=PAGE,
            min_credits=0),
        lambda fx: _scholar_page(), ranked=False),
}


@pytest.fixture
def fx(hal_first_doc, crossref_work, openalex_work, s2_paper, core_work,
       monkeypatch):
    """Corps de référence, et lots ramenés à ``PAGE`` notices par page."""
    monkeypatch.setattr(hal_module, "HAL_ROWS_PER_BATCH", PAGE)
    monkeypatch.setattr(crossref_module, "ROWS_PER_BATCH", PAGE)
    monkeypatch.setattr(openalex_module, "PER_PAGE", PAGE)
    monkeypatch.setattr(core_module, "LIMIT", PAGE)
    return {"hal": hal_first_doc, "crossref": crossref_work,
            "openalex": openalex_work, "s2": s2_paper, "core": core_work}


def _run(spec, fx, ctx_factory, hook_factory):
    """Connecteur réel sur le service en mémoire ; renvoie (connecteur,
    service, notices ou exception)."""
    api = _Api(spec.build(fx))
    client = _client(spec.prefix, api)
    ctx = ctx_factory(client)
    view = ctx.for_source()
    api.hook = hook_factory(ctx, view)
    conn = spec.make(view.http_client, view)
    try:
        return conn, api, conn.search("discours numérique", max_results=MAX,
                                      **spec.search)
    except OperationCancelled as exc:
        return conn, api, exc


# === Connecteurs paginés ===

@pytest.mark.parametrize("name", list(SPECS))
class TestPaginatedConnectors:
    def test_between_pages_keeps_received_pages(self, name, fx):
        """Échéance souple entre la page 2 et la page 3 : les 2 pages reçues
        sont rendues, aucune page de plus, partial/deadline."""
        spec = SPECS[name]

        def hook_factory(ctx, view):
            def hook(n, request):
                if n == 2:
                    _expire(view.signal)     # l'échéance tombe pendant la page 2
            return hook

        conn, api, items = _run(spec, fx, _soft_ctx, hook_factory)
        assert not isinstance(items, BaseException), items
        assert len(items) == 2 * PAGE
        assert api.sent == 2                      # aucune page supplémentaire
        report = conn.last_status
        assert (report.status, report.reason) == (STATUS_PARTIAL, REASON_DEADLINE)
        assert report.pages == 2
        assert "échéance souple" in report.detail
        assert not report.limit_reached and not report.exhausted
        if spec.ranked:
            # Rangs natifs alignés sur les notices rendues (OPT-12).
            assert [r["rank"] for r in report.native_ranks] == \
                list(range(1, 2 * PAGE + 1))

    def test_in_flight_request_cut_keeps_received_pages(self, name, fx):
        """Requête de la page 3 coupée par l'échéance (délai réseau borné) :
        les 2 pages reçues sont rendues."""
        spec = SPECS[name]

        def hook_factory(ctx, view):
            def hook(n, request):
                if n == 3:
                    _expire(view.signal)
                    _timeout(request)
            return hook

        conn, api, items = _run(spec, fx, _soft_ctx, hook_factory)
        assert not isinstance(items, BaseException), items
        assert len(items) == 2 * PAGE
        assert api.sent == 3                      # la page coupée, rien après
        report = conn.last_status
        assert (report.status, report.reason) == (STATUS_PARTIAL, REASON_DEADLINE)

    def test_client_cancellation_is_propagated(self, name, fx):
        """Annulation par le client : inchangée (propagée, rien rendu)."""
        spec = SPECS[name]

        def hook_factory(ctx, view):
            def hook(n, request):
                if n == 2:
                    ctx.cancel_event.set()
            return hook

        conn, api, result = _run(spec, fx, _soft_ctx, hook_factory)
        assert isinstance(result, OperationCancelled)
        assert result.reason == CANCEL_REASON_CANCELLED
        assert api.sent == 2

    def test_hard_deadline_is_propagated(self, name, fx):
        """Échéance dure (transport HTTP) : propagée, jamais un arrêt souple."""
        spec = SPECS[name]

        def ctx_factory(client):
            return _soft_ctx(client, deadline=time.monotonic() + 120)

        def hook_factory(ctx, view):
            def hook(n, request):
                if n == 2:
                    _expire(ctx.signal)
                    _expire(view.signal)
            return hook

        conn, api, result = _run(spec, fx, ctx_factory, hook_factory)
        assert isinstance(result, OperationCancelled)
        assert result.reason == CANCEL_REASON_DEADLINE
        assert api.sent == 2

    def test_without_soft_deadline_unchanged(self, name, fx):
        """Sans échéance souple (CLI, HTTP) : l'échéance de l'exécution
        est propagée comme avant."""
        spec = SPECS[name]

        def hook_factory(ctx, view):
            assert view is ctx                     # aucune vue de source
            def hook(n, request):
                if n == 2:
                    _expire(ctx.signal)
            return hook

        conn, api, result = _run(spec, fx,
                                 lambda client: RunContext(http_client=client),
                                 hook_factory)
        assert isinstance(result, OperationCancelled)
        assert result.reason == CANCEL_REASON_DEADLINE

    def test_stop_before_any_notice_is_propagated(self, name, fx):
        """Échéance avant toute notice : propagée (l'orchestrateur garde
        ``error/deadline``)."""
        spec = SPECS[name]

        def hook_factory(ctx, view):
            def hook(n, request):
                _expire(view.signal)
                _timeout(request)
            return hook

        conn, api, result = _run(spec, fx, _soft_ctx, hook_factory)
        assert isinstance(result, OperationCancelled)
        assert result.reason == CANCEL_REASON_DEADLINE
        assert api.sent == 1

    def test_no_stop_unchanged(self, name, fx):
        """Sans arrêt, échéance souple active ou non : mêmes notices."""
        spec = SPECS[name]

        def no_hook(ctx, view):
            return None

        _, api1, soft = _run(spec, fx, _soft_ctx, no_hook)
        _, api2, plain = _run(spec, fx,
                              lambda client: RunContext(http_client=client),
                              no_hook)
        assert len(soft) == MAX
        assert [it.get("title") for it in soft] == [it.get("title") for it in plain]
        assert api1.sent == api2.sent == MAX // PAGE


# === Flux Isidore (source directe et sous-sources) ===

def _isidore_build(isidore_response, per_page=2, total=100):
    base = isidore_response["response"]["replies"]["content"]["reply"][0]

    def build(n, request):
        page = int(_query(request).get("page", n))
        replies = []
        for i in range(per_page):
            k = (page - 1) * per_page + i
            text = json.dumps(base, ensure_ascii=False)
            text = (text.replace("lidil.6033", f"lidil.{7000 + k}")
                        .replace("lidil/6033", f"lidil/{7000 + k}")
                        .replace("lidil.5895", f"lidil.{8000 + k}"))
            rec = json.loads(text)
            rec["@uri"] = f"20.500.13089/opt09ter{k}"
            rec["isidore"]["title"] = [{"@xml:lang": "fr",
                                        "$": f"Titre Isidore opt09ter {k}"}]
            replies.append(rec)
        page_block = {"@current": str(page)}
        if page * per_page < total:
            page_block["@next"] = str(page + 1)
        return {"response": {"replies": {
            "meta": {"@items": str(total), "@pageItems": str(per_page)},
            "content": {"reply": replies}, "page": page_block}}}
    return build


def _isidore_run(isidore_response, hook_factory, ctx_factory=_soft_ctx,
                 make=None):
    api = _Api(_isidore_build(isidore_response))
    client = _client("https://api.isidore.science/", api)
    ctx = ctx_factory(client)
    view = ctx.for_source()
    api.hook = hook_factory(ctx, view)
    registry = ScanRegistry(replies=2, window=0)
    make = make or (lambda http, c: IsidoreConnector(
        http_client=http, run_context=c, scan_registry=registry))
    conn = make(view.http_client, view)
    try:
        return conn, api, conn.search("discours numérique", max_results=MAX)
    except OperationCancelled as exc:
        return conn, api, exc


class TestIsidoreFlow:
    def test_between_pages_keeps_retained_notices(self, isidore_response):
        def hook_factory(ctx, view):
            def hook(n, request):
                if n == 2:
                    _expire(view.signal)
            return hook

        conn, api, items = _isidore_run(isidore_response, hook_factory)
        assert not isinstance(items, BaseException), items
        assert len(items) == 4 and api.sent == 2
        report = conn.last_status
        assert (report.status, report.reason) == (STATUS_PARTIAL, REASON_DEADLINE)
        assert "parcours Isidore interrompu par l'échéance souple" in report.detail
        assert [r["rank"] for r in report.native_ranks] == [1, 2, 3, 4]
        assert conn.last_scan_status.soft_stopped
        assert conn.last_scan_status.incomplete

    def test_in_flight_keeps_retained_notices(self, isidore_response):
        def hook_factory(ctx, view):
            def hook(n, request):
                if n == 3:
                    _expire(view.signal)
                    _timeout(request)
            return hook

        conn, api, items = _isidore_run(isidore_response, hook_factory)
        assert not isinstance(items, BaseException), items
        assert len(items) == 4 and api.sent == 3
        assert conn.last_status.reason == REASON_DEADLINE

    def test_sub_source_keeps_retained_notices(self, isidore_response):
        """Sous-source (OpenEdition, sans enrichissement) : même contrat."""
        registry = ScanRegistry(replies=2, window=0)

        def hook_factory(ctx, view):
            def hook(n, request):
                if n == 2:
                    _expire(view.signal)
            return hook

        conn, api, items = _isidore_run(
            isidore_response, hook_factory,
            make=lambda http, c: OpenEditionConnector(
                http_client=http, run_context=c, scan_registry=registry))
        assert not isinstance(items, BaseException), items
        assert len(items) == 4 and api.sent == 2
        assert all("Sources: OpenEdition" in it["extra"] for it in items)
        assert (conn.last_status.status, conn.last_status.reason) == \
            (STATUS_PARTIAL, REASON_DEADLINE)

    def test_client_cancellation_is_propagated(self, isidore_response):
        def hook_factory(ctx, view):
            def hook(n, request):
                if n == 2:
                    ctx.cancel_event.set()
            return hook

        _, api, result = _isidore_run(isidore_response, hook_factory)
        assert isinstance(result, OperationCancelled)
        assert result.reason == CANCEL_REASON_CANCELLED

    def test_stop_before_any_notice_is_propagated(self, isidore_response):
        def hook_factory(ctx, view):
            def hook(n, request):
                _expire(view.signal)
                _timeout(request)
            return hook

        _, api, result = _isidore_run(isidore_response, hook_factory)
        assert isinstance(result, OperationCancelled)
        assert result.reason == CANCEL_REASON_DEADLINE

    def test_without_soft_deadline_unchanged(self, isidore_response):
        def hook_factory(ctx, view):
            def hook(n, request):
                if n == 2:
                    _expire(ctx.signal)
            return hook

        _, _, result = _isidore_run(
            isidore_response, hook_factory,
            ctx_factory=lambda client: RunContext(http_client=client))
        assert isinstance(result, OperationCancelled)
        assert result.reason == CANCEL_REASON_DEADLINE


# === Boucles d'enrichissement OAI ===

class _FakeRecord:
    metadata = {}


ENRICHERS = {
    "openedition": (openedition_module, OpenEditionConnector, "map_qdc_to_zotero"),
    "cairn": (cairn_module, CairnConnector, "map_oai_dc_to_zotero"),
    "persee": (persee_module, PerseeConnector, "map_oai_dc_to_zotero"),
    "erudit": (erudit_module, EruditConnector, "map_oai_dc_to_zotero"),
}


def _plain_items(n=5):
    return [{"itemType": "journalArticle", "title": f"Titre {i}",
             "date": "2024", "url": f"https://example.org/{i}",
             "creators": [], "extra": "Sources: Isidore"} for i in range(n)]


def _enrich(key, monkeypatch, ctx, on_record):
    module, cls, mapper = ENRICHERS[key]
    calls = []

    class _Sickle:
        def GetRecord(self, **kwargs):
            calls.append(kwargs["identifier"])
            on_record(len(calls))
            return _FakeRecord()

    monkeypatch.setattr(cls, "_get_sickle", lambda self: _Sickle())
    monkeypatch.setattr(cls, "_extract_oai_id",
                        staticmethod(lambda item: f"oai:test:{item['title']}"))
    monkeypatch.setattr(module, mapper,
                        lambda *a, **k: {"shortTitle": "enrichie par OAI"})
    view = ctx.for_source()
    conn = cls(http_client=view.http_client, run_context=view, enrich_oai=True)
    conn.last_status = SearchReport(source=cls.SOURCE_NAME)
    items = _plain_items()
    try:
        return conn, calls, view, conn._enrich_with_oai(items)
    except OperationCancelled as exc:
        return conn, calls, view, exc


@pytest.mark.parametrize("key", list(ENRICHERS))
class TestOaiEnrichment:
    def test_interrupted_between_records_keeps_every_notice(self, key,
                                                            monkeypatch):
        """Échéance souple pendant le 2e GetRecord : les 2 notices enrichies
        le restent, les 3 autres gardent leurs métadonnées Isidore ; aucune
        requête OAI de plus ; ordre (et donc rangs natifs) conservé."""
        box = {}
        ctx = _soft_ctx(HTTPClient(use_cache=False))

        def on_record(n):
            if n == 2:
                _expire(box["view"].signal)

        # La vue de source est créée dans _enrich : on la récupère par le
        # connecteur une fois construite (même objet signal).
        orig_for_source = ctx.for_source

        def for_source():
            box["view"] = orig_for_source()
            return box["view"]

        ctx.for_source = for_source
        conn, calls, view, out = _enrich(key, monkeypatch, ctx, on_record)
        assert not isinstance(out, BaseException), out
        assert [it["title"] for it in out] == [f"Titre {i}" for i in range(5)]
        assert [it.get("shortTitle") for it in out] == \
            ["enrichie par OAI"] * 2 + [None] * 3
        assert len(calls) == 2
        report = conn.last_status
        assert (report.status, report.reason) == (STATUS_PARTIAL, REASON_DEADLINE)
        assert "après 2 notice(s) traitée(s) sur 5" in report.detail

    def test_client_cancellation_is_propagated(self, key, monkeypatch):
        ctx = _soft_ctx(HTTPClient(use_cache=False))

        def on_record(n):
            if n == 2:
                ctx.cancel_event.set()

        _, calls, _, out = _enrich(key, monkeypatch, ctx, on_record)
        assert isinstance(out, OperationCancelled)
        assert out.reason == CANCEL_REASON_CANCELLED
        assert len(calls) == 2

    def test_without_soft_deadline_unchanged(self, key, monkeypatch):
        ctx = RunContext(http_client=HTTPClient(use_cache=False))

        def on_record(n):
            if n == 2:
                _expire(ctx.signal)

        _, calls, _, out = _enrich(key, monkeypatch, ctx, on_record)
        assert isinstance(out, OperationCancelled)
        assert out.reason == CANCEL_REASON_DEADLINE


# === Compte rendu : troncature antérieure et arrêt déjà posé ===

class TestReportPrecedence:
    def _conn(self):
        ctx = _soft_ctx(HTTPClient(use_cache=False))
        view = ctx.for_source()
        return CoreConnector(http_client=view.http_client, run_context=view,
                             api_key="cle-factice")

    def test_window_detail_is_kept_as_a_warning(self):
        conn = self._conn()
        report = conn._start_report()
        report.window("Isidore ne sert que 200 résultats")
        assert conn.soft_stopped(OperationCancelled(CANCEL_REASON_DEADLINE),
                                 report, 3, "arrêt")
        assert (report.status, report.reason, report.detail) == \
            (STATUS_PARTIAL, REASON_DEADLINE, "arrêt")
        assert report.provider_window
        assert "Isidore ne sert que 200 résultats" in report.warnings

    def test_first_deadline_stop_remains_the_reason(self):
        conn = self._conn()
        report = conn._start_report()
        report.truncated(REASON_DEADLINE, "parcours Isidore interrompu")
        assert conn.soft_stopped(OperationCancelled(CANCEL_REASON_DEADLINE),
                                 report, 3, "enrichissement OAI interrompu")
        assert report.detail == "parcours Isidore interrompu"
        assert "enrichissement OAI interrompu" in report.warnings

    def test_error_keeps_priority(self):
        conn = self._conn()
        report = conn._start_report()
        report.fail("http_error", "HTTP 502")
        assert conn.soft_stopped(OperationCancelled(CANCEL_REASON_DEADLINE),
                                 report, 3, "arrêt")
        assert (report.status, report.reason) == ("error", "http_error")
        assert "arrêt" in report.warnings

    def test_nothing_kept_or_not_a_soft_stop(self):
        conn = self._conn()
        report = conn._start_report()
        deadline = OperationCancelled(CANCEL_REASON_DEADLINE)
        assert not conn.soft_stopped(deadline, report, 0)
        assert not conn.soft_stopped(OperationCancelled(CANCEL_REASON_CANCELLED),
                                     report, 3)
        assert report.status is None
        # Connecteur sans contexte (résolveurs, CLI) : jamais d'arrêt souple.
        bare = CoreConnector(api_key="cle-factice")
        assert not bare.soft_stopped(deadline, None, 3)


# === Moteur : vraie échéance souple, source paginée lente (CORE) ===

class _Mute(BaseAdapter):
    """Service muet : attend le délai de lecture reçu, puis ReadTimeout."""

    def __init__(self):
        super().__init__()
        self.timeouts = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        read = timeout[1] if isinstance(timeout, tuple) else timeout
        self.timeouts.append(read)
        time.sleep(float(read or 0))
        raise requests.exceptions.ReadTimeout("service muet (simulé)",
                                              request=request)

    def close(self):
        pass


class _SlowAfter(BaseAdapter):
    """``pages`` pages servies aussitôt, puis service muet (CORE « lent »)."""

    def __init__(self, build, pages):
        super().__init__()
        self.build = build
        self.pages = pages
        self.sent = 0
        self.mute = _Mute()

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        self.sent += 1
        if self.sent > self.pages:
            return self.mute.send(request, timeout=timeout)
        return _response(request, self.build(self.sent, request))

    def close(self):
        pass


class _FastCore(CoreConnector):
    """Vrai connecteur CORE, clé factice, sans politesse (6 s en réel)."""

    def __init__(self, **kwargs):
        super().__init__(api_key="cle-factice", min_interval=0.0, **kwargs)


def _classes(mapping):
    def get(key):
        return mapping[key]
    return get


class TestHarvestSlowPaginatedSource:
    def test_core_pages_are_published_at_the_deadline(self, monkeypatch,
                                                      tmp_path, core_work,
                                                      hal_solr_response):
        """Avant : CORE ``error/deadline``, 0 notice (3 pages perdues). Après :
        ``partial/deadline``, les 300 notices des 3 pages publiées avec HAL,
        payload brut écrit, moisson non annulée."""
        monkeypatch.setattr(orchestrator, "get_connector_class",
                            _classes({"core": _FastCore, "hal": HALConnector}))
        core = _SlowAfter(_core_page(core_work, size=100), pages=3)
        hal = _Api(lambda n, request: hal_solr_response)
        client = HTTPClient(use_cache=False)
        client.session.mount("https://api.core.ac.uk/", core)
        client.session.mount("https://api.archives-ouvertes.fr/", hal)
        ctx = RunContext(soft_deadline=time.monotonic() + 0.6,
                         soft_deadline_s=0.6, soft_grace_s=0.5,
                         http_client=client, output_dir=tmp_path)
        t0 = time.monotonic()
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["core", "hal"], workers=2,
                          max_per_source=1000), ctx, ranking="off")
        assert time.monotonic() - t0 < 0.6 + 0.5 + 0.6
        by = {s.source: s for s in res.sources}
        src = by["core"]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE), \
            src.describe()
        assert src.item_count == 300 and src.pages == 3
        assert "300 notice(s) conservée(s)" in src.detail
        assert core.sent == 4                     # 3 pages + la page coupée
        assert by["hal"].status == STATUS_COMPLETE
        assert not res.all_failed and res.overall_status == "partial"
        assert len(res.items) == 300 + by["hal"].item_count
        assert not ctx.cancelled()
        raw = json.loads(orchestrator.raw_path(
            tmp_path, "CORE", ctx.run_id).read_text(encoding="utf-8"))
        assert len(raw) == 300

    def test_hal_bibliography_pages_are_published(self, monkeypatch,
                                                  hal_first_doc):
        """HAL en bibliographie sur plusieurs pages (tri par score) : les
        pages reçues sont publiées avec leurs rangs natifs."""
        monkeypatch.setattr(hal_module, "HAL_ROWS_PER_BATCH", PAGE)
        hal = _SlowAfter(_hal_page(hal_first_doc), pages=2)
        client = HTTPClient(use_cache=False)
        client.session.mount("https://api.archives-ouvertes.fr/", hal)
        ctx = RunContext(soft_deadline=time.monotonic() + 0.5,
                         soft_deadline_s=0.5, soft_grace_s=0.5,
                         http_client=client)
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["hal"],
                          max_per_source=MAX), ctx, ranking="off")
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE)
        assert src.item_count == 2 * PAGE and hal.sent == 3
        assert src.native_sort == hal_module.SORT_BIBLIOGRAPHIE
        assert [r["rank"] for r in src.native_ranks] == list(range(1, 2 * PAGE + 1))
        assert len(res.items) == 2 * PAGE

    def test_client_cancellation_still_publishes_nothing(self, monkeypatch,
                                                         tmp_path, core_work):
        """Annulation par le client pendant la pagination de CORE : toute la
        moisson est annulée, aucun payload brut ne reste."""
        monkeypatch.setattr(orchestrator, "get_connector_class",
                            _classes({"core": _FastCore}))
        box = {}

        def hook(n, request):
            if n == 2:
                box["ctx"].cancel_event.set()

        core = _Api(_core_page(core_work, size=100), hook)
        client = HTTPClient(use_cache=False)
        client.session.mount("https://api.core.ac.uk/", core)
        ctx = RunContext(soft_deadline=time.monotonic() + 30,
                         soft_deadline_s=30, http_client=client,
                         output_dir=tmp_path)
        box["ctx"] = ctx
        with pytest.raises(OperationCancelled) as info:
            orchestrator.harvest(
                SearchRequest(query="discours", sources=["core"],
                              max_per_source=1000), ctx, ranking="off")
        assert info.value.reason == CANCEL_REASON_CANCELLED
        assert info.value.result.items == []
        raw_dir = tmp_path / "raw"
        assert not raw_dir.exists() or not any(raw_dir.iterdir())

    def test_enrichment_interrupted_keeps_enriched_notices(
            self, monkeypatch, tmp_path, isidore_response):
        """OpenEdition avec ``enrich_oai`` : échéance pendant le 2e GetRecord ;
        la notice déjà enrichie le reste (avant : reprise sans aucun
        enrichissement par l'orchestrateur)."""
        box = {}

        class _Sickle:
            def GetRecord(self, **kwargs):
                box["n"] = box.get("n", 0) + 1
                if box["n"] == 2:
                    # Le 2e GetRecord se termine après l'échéance souple.
                    while time.monotonic() < box["ctx"].soft_deadline + 0.05:
                        time.sleep(0.01)
                return _FakeRecord()

        monkeypatch.setattr(OpenEditionConnector, "_get_sickle",
                            lambda self: _Sickle())
        monkeypatch.setattr(OpenEditionConnector, "_extract_oai_id",
                            staticmethod(lambda item: "oai:revues.org:lidil/1"))
        monkeypatch.setattr(openedition_module, "map_qdc_to_zotero",
                            lambda *a, **k: {"shortTitle": "enrichie par OAI"})
        isidore = _Api(_isidore_build(isidore_response, per_page=6, total=6))
        client = HTTPClient(use_cache=False)
        client.session.mount("https://api.isidore.science/", isidore)
        ctx = RunContext(soft_deadline=time.monotonic() + 0.5,
                         soft_deadline_s=0.5, soft_grace_s=1.0,
                         http_client=client, output_dir=tmp_path)
        box["ctx"] = ctx
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["openedition"],
                          enrich_oai=True, max_per_source=50), ctx,
            ranking="off")
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE), \
            src.describe()
        assert src.item_count == 6 and box["n"] == 2
        enriched = [it for it in src.items if it.get("shortTitle")]
        assert len(enriched) == 2
        assert any("enrichissement OAI interrompu" in w for w in src.warnings)
        assert isidore.sent == 1

    def test_without_soft_deadline_core_complete(self, monkeypatch, core_work):
        """Sans échéance souple (CLI) : CORE complet, comme avant."""
        monkeypatch.setattr(orchestrator, "get_connector_class",
                            _classes({"core": _FastCore}))
        core = _Api(_core_page(core_work, size=100))
        client = HTTPClient(use_cache=False)
        client.session.mount("https://api.core.ac.uk/", core)
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["core"],
                          max_per_source=300), RunContext(http_client=client),
            ranking="off")
        src = res.sources[0]
        assert src.status == STATUS_COMPLETE and src.item_count == 300
        assert core.sent == 3


# === Serveur MCP (stdio) : notices publiées à l'échéance ===

class TestMcpStdioPublishesKeptPages:
    def test_core_pages_kept_in_the_published_corpus(self, tmp_path,
                                                     monkeypatch, core_work,
                                                     hal_solr_response):
        """``moisson_search`` en stdio, échéance de 0,6 s, CORE lent après 3
        pages : le retour est un succès partiel, CORE ``partial/deadline``
        avec ses 300 notices dans le corpus publié."""
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
        monkeypatch.setenv("MOISSON_CACHE", "0")
        monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "cache" / "http"))
        monkeypatch.setenv("CORE_API_KEY", "cle-factice")
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_MIN_S", 0.1)
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_GRACE_S", 0.5)
        monkeypatch.setattr(orchestrator, "get_connector_class",
                            _classes({"core": _FastCore, "hal": HALConnector}))
        core = _SlowAfter(_core_page(core_work, size=100), pages=3)

        def send(adapter, request, **kwargs):
            host = urlsplit(request.url).hostname
            if host == "api.core.ac.uk":
                return core.send(request, **kwargs)
            if host == "api.archives-ouvertes.fr":
                return _response(request, hal_solr_response)
            raise requests.ConnectionError(f"hôte inattendu : {host}")

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
        res = asyncio.run(mcp_server._tool_moisson_search({
            "query": "discours", "sources": ["hal", "core"],
            "max_per_source": 1000, "cache": False, "deadline_seconds": 0.6}))
        assert not mcp_server._is_tool_error(res)
        blocks, structured = res
        status = structured["by_source_status"]
        assert status["CORE"]["status"] == "partial"
        assert status["CORE"]["reason"] == "deadline"
        assert status["CORE"]["items"] == 300
        assert status["HAL"]["status"] == "complete"
        assert structured["soft_deadline"]["stopped_sources"] == ["CORE"]
        n_hal = status["HAL"]["items"]
        assert structured["total_unique"] == 300 + n_hal
        assert structured["harvest_status"] == "partial"
        assert len(list(tmp_path.glob("*.csl.json"))) == 1
