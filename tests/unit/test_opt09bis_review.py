"""
OPT-09 bis, revue : échéance souple en stdio, constats corrigés.

- une source à enrichissement OAI (``enrich_oai``) arrêtée par l'échéance
  pendant l'enrichissement, ou abandonnée après elle, garde ses notices
  Isidore sans enrichissement (``partial``, motif ``deadline``) au lieu de
  les perdre ; la reprise relit le flux Isidore en mémoire, sans requête ;
- une source paginée arrêtée (vrai connecteur CORE) rend les notices des
  pages reçues (depuis OPT-09 ter, les connecteurs suivent le contrat
  ``RunContext.soft_stop``) ; un connecteur qui suit ce contrat est publié
  ``partial`` avec ses notices ;
- le retour MCP distingue l'échéance atteinte d'une échéance intenable
  (source arrêtée plus tôt, prochain créneau de requête au-delà) :
  ``soft_deadline.reached`` suit l'horloge ;
- ``deadline_seconds`` au-delà de 900 est ramené à 900 par le serveur, y
  compris à travers le répartiteur du SDK (plus de ``maximum`` dans le
  schéma).

Aucun accès réseau ni socket : transport ``requests`` en mémoire, OAI
(sickle) doublé, ou exécutant de source remplacé ; transport MCP du SDK en
mémoire.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import anyio
import pytest
import requests
from requests.adapters import BaseAdapter

import config
import connectors.openedition as openedition_module
import mcp_server
from connectors.base import Connector
from connectors.core import CoreConnector
from engine import orchestrator
from engine.models import (
    REASON_DEADLINE, STATUS_COMPLETE, STATUS_ERROR, STATUS_PARTIAL,
    RunContext, SearchRequest, SourceResult,
)
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


class _CountingJsonAdapter(BaseAdapter):
    """Service qui répond aussitôt un JSON fixe et compte les requêtes."""

    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.sent = 0

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        self.sent += 1
        return _response(request, self.payload)

    def close(self):
        pass


class _MuteAdapter(BaseAdapter):
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


class _CorePages(BaseAdapter):
    """API CORE en mémoire : pages de 100 notices distinctes."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        from urllib.parse import parse_qs, urlsplit
        self.sent.append(time.monotonic())
        offset = int(parse_qs(urlsplit(request.url).query).get("offset", ["0"])[0])
        results = [{"title": f"Titre {offset + i}", "yearPublished": 2020,
                    "documentType": "research",
                    "authors": [{"name": f"Auteur{offset + i} Nom"}],
                    "doi": f"10.7777/opt09bis.{offset + i}"} for i in range(100)]
        return _response(request, {"results": results, "totalHits": 5000})

    def close(self):
        pass


def _items(label, n=2):
    return [{"itemType": "journalArticle", "title": f"{label} titre {i}",
             "date": "2024", "DOI": f"10.9999/{label.lower()}.{i}",
             "creators": [{"creatorType": "author", "lastName": f"{label}{i}"}],
             "extra": f"Sources: {label}"} for i in range(n)]


def _client(**adapters):
    client = HTTPClient(use_cache=False)
    for prefix, adapter in adapters.items():
        client.session.mount(prefix, adapter)
    return client


def _soft_ctx(seconds, grace=0.3, **kwargs):
    return RunContext(soft_deadline=time.monotonic() + seconds,
                      soft_deadline_s=seconds, soft_grace_s=grace, **kwargs)


def _classes(mapping):
    def get(key):
        return mapping[key]
    return get


@pytest.fixture(autouse=True)
def _fresh_politeness():
    reset_http_policy()
    yield
    reset_http_policy()


# === Isidore réel : notices OpenEdition distinctes ===

N_RECORDS = 6
_TITLE = ("Marie-Anne Paveau, L’analyse du discours numérique. Dictionnaire "
          "des formes et des pratiques")


def _isidore_payload(isidore_response, n=N_RECORDS):
    """Réponse Isidore de ``n`` notices OpenEdition distinctes (URL, DOI,
    titre et identifiant propres), tirées de la notice de la fixture."""
    base = json.dumps(
        isidore_response["response"]["replies"]["content"]["reply"][0],
        ensure_ascii=False)
    replies = []
    for i in range(n):
        text = (base.replace("lidil.6033", f"lidil.{7000 + i}")
                    .replace("lidil/6033", f"lidil/{7000 + i}")
                    .replace("lidil.5895", f"lidil.{8000 + i}")
                    .replace(_TITLE, f"Titre opt09bis {i}"))
        rec = json.loads(text)
        rec["@uri"] = f"20.500.13089/opt09bis{i}"
        replies.append(rec)
    return {"response": {"replies": {
        "meta": {"@items": str(n), "@pageItems": str(n)},
        "content": {"reply": replies}}}}


class _FakeRecord:
    metadata = {}


def _patch_oai(monkeypatch, get_record):
    """OAI d'OpenEdition doublé : identifiant fixe, GetRecord fourni."""
    class _Sickle:
        def GetRecord(self, **kwargs):
            return get_record()

    monkeypatch.setattr(openedition_module.OpenEditionConnector, "_get_sickle",
                        lambda self: _Sickle())
    monkeypatch.setattr(openedition_module.OpenEditionConnector,
                        "_extract_oai_id",
                        staticmethod(lambda item: "oai:revues.org:lidil/1"))
    monkeypatch.setattr(openedition_module, "map_qdc_to_zotero",
                        lambda *a, **k: {})


# === Enrichissement OAI interrompu : notices Isidore conservées ===

class TestEnrichmentFallback:
    def test_stopped_during_oai_enrichment_keeps_isidore_notices(
            self, monkeypatch, tmp_path, isidore_response):
        """Avant la revue : OpenEdition error/deadline, 0 notice, échec total
        (``all_failed``). Après : partial/deadline, les 6 notices publiées,
        aucune requête Isidore de plus (depuis OPT-09 ter, rendues par la
        boucle d'enrichissement elle-même : celles déjà enrichies le
        restent)."""
        def get_record():
            time.sleep(0.15)            # un GetRecord OAI (politesse + réseau)
            return _FakeRecord()

        _patch_oai(monkeypatch, get_record)
        isidore = _CountingJsonAdapter(_isidore_payload(isidore_response))
        client = _client(**{"https://api.isidore.science/": isidore})
        ctx = _soft_ctx(0.6, grace=1.0, http_client=client, output_dir=tmp_path)
        t0 = time.monotonic()
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["openedition"], workers=2,
                          enrich_oai=True, max_per_source=50), ctx,
            ranking="off")
        assert time.monotonic() - t0 < 0.6 + 1.0
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE), \
            src.describe()
        assert src.item_count == N_RECORDS
        assert f"{N_RECORDS} notice(s) conservée(s)" in src.detail
        # Nature de l'arrêt gardée (revue OPT-09 ter) : la collecte Isidore
        # était complète, seul l'enrichissement a été interrompu.
        assert "pendant l'enrichissement OAI" in src.detail
        assert "collecte interrompue" not in src.detail
        assert any("enrichissement OAI interrompu" in w for w in src.warnings)
        assert not res.all_failed and res.overall_status == "partial"
        assert len(res.items) == N_RECORDS
        assert isidore.sent == 1          # la reprise relit le flux en mémoire
        assert not ctx.cancelled()
        # Payload brut : les notices publiées.
        raw = json.loads(orchestrator.raw_path(
            tmp_path, "OpenEdition", ctx.run_id).read_text(encoding="utf-8"))
        assert len(raw) == N_RECORDS

    def test_without_deadline_enrichment_completes(self, monkeypatch,
                                                   isidore_response):
        """Sans échéance souple : comportement historique (complète,
        enrichie), la reprise n'intervient pas."""
        _patch_oai(monkeypatch, lambda: _FakeRecord())
        isidore = _CountingJsonAdapter(_isidore_payload(isidore_response, 3))
        ctx = RunContext(http_client=_client(
            **{"https://api.isidore.science/": isidore}))
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["openedition"],
                          enrich_oai=True, max_per_source=50), ctx,
            ranking="off")
        src = res.sources[0]
        assert src.reason != REASON_DEADLINE
        assert src.item_count == 3 and not src.warnings

    def test_isidore_flow_incomplete_no_fallback_no_request(
            self, monkeypatch, isidore_response):
        """Arrêt pendant la recherche Isidore (service muet) : rien à
        reprendre ; la reprise échoue aussitôt, sans requête de plus."""
        _patch_oai(monkeypatch, lambda: _FakeRecord())
        mute = _MuteAdapter()
        ctx = _soft_ctx(0.6, grace=0.3, http_client=_client(
            **{"https://api.isidore.science/": mute}))
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["openedition"],
                          enrich_oai=True, max_per_source=50), ctx,
            ranking="off")
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_ERROR, REASON_DEADLINE)
        assert src.item_count == 0 and res.all_failed
        assert len(mute.timeouts) == 1

    def test_client_cancellation_is_not_rescued(self, monkeypatch,
                                                isidore_response):
        """Annulation par le client pendant l'enrichissement : toute la
        moisson est annulée, aucune reprise."""
        ctx_box = {}

        def get_record():
            ctx_box["ctx"].cancel()     # le client annule
            return _FakeRecord()

        _patch_oai(monkeypatch, get_record)
        isidore = _CountingJsonAdapter(_isidore_payload(isidore_response))
        ctx = _soft_ctx(30, http_client=_client(
            **{"https://api.isidore.science/": isidore}))
        ctx_box["ctx"] = ctx
        with pytest.raises(OperationCancelled) as info:
            orchestrator.harvest(
                SearchRequest(query="discours", sources=["openedition"],
                              enrich_oai=True, max_per_source=50), ctx,
                ranking="off")
        assert info.value.reason == CANCEL_REASON_CANCELLED
        assert info.value.result.items == []

    def test_abandoned_during_enrichment_keeps_isidore_notices(
            self, monkeypatch, tmp_path, isidore_response):
        """GetRecord bloqué (reprise sickle non interruptible) : la source est
        abandonnée après la grâce, mais ses notices Isidore sont publiées ;
        le payload brut, réécrit après la fin tardive du fil, contient les
        notices publiées."""
        late_done = threading.Event()

        def get_record():
            try:
                time.sleep(1.5)         # non interruptible
                return _FakeRecord()
            finally:
                late_done.set()

        _patch_oai(monkeypatch, get_record)
        isidore = _CountingJsonAdapter(_isidore_payload(isidore_response))
        ctx = _soft_ctx(0.4, grace=0.3, output_dir=tmp_path, http_client=_client(
            **{"https://api.isidore.science/": isidore}))
        t0 = time.monotonic()
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["openedition"], workers=2,
                          enrich_oai=True, max_per_source=50), ctx,
            ranking="off")
        assert time.monotonic() - t0 < 0.4 + 0.3 + 0.6
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE), \
            src.describe()
        assert src.item_count == N_RECORDS and len(res.items) == N_RECORDS
        assert any("toujours occupée" in w for w in src.warnings)
        assert isidore.sent == 1
        raw_file = orchestrator.raw_path(tmp_path, "OpenEdition", ctx.run_id)
        assert len(json.loads(raw_file.read_text(encoding="utf-8"))) == N_RECORDS
        # Fin tardive du fil abandonné : le payload brut reste celui publié.
        assert late_done.wait(3.0)
        time.sleep(0.3)
        assert len(json.loads(raw_file.read_text(encoding="utf-8"))) == N_RECORDS


# === Source paginée : pages perdues nommées ; contrat soft_stop ===

class _FastCore(CoreConnector):
    """Vrai connecteur CORE, politesse ramenée à 0,5 s (6 s en réel)."""

    def __init__(self, **kwargs):
        super().__init__(api_key="cle-factice", min_interval=0.5, **kwargs)


class TestPaginatedSource:
    def test_real_core_keeps_the_pages_it_received(self, monkeypatch):
        """CORE arrêté rend les notices des pages reçues (OPT-09 ter : contrat
        ``RunContext.soft_stop`` appliqué par le connecteur), aucune page de
        plus ; avant, il les perdait (``error``, pages nommées)."""
        pages = _CorePages()
        monkeypatch.setattr(orchestrator, "get_connector_class",
                            _classes({"core": _FastCore}))
        ctx = _soft_ctx(1.3, grace=0.5, http_client=_client(
            **{"https://api.core.ac.uk/": pages}))
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=["core"], mode="collecte",
                          max_per_source=1000, workers=1), ctx, ranking="off")
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE), \
            src.describe()
        assert src.pages == len(pages.sent) >= 2
        assert src.item_count == 100 * src.pages
        assert f"{src.item_count} notice(s) conservée(s)" in src.detail
        assert any("collecte interrompue par l'échéance souple" in w
                   for w in src.warnings)
        assert len(res.items) == src.item_count
        assert not ctx.cancelled()

    def test_connector_following_the_contract_is_partial(self, monkeypatch):
        """Un connecteur qui rend sa collecte sur ``RunContext.soft_stop`` est
        publié ``partial`` (motif deadline) avec ses notices ; l'arrêt
        anticipé (créneau au-delà) est marqué intenable."""
        class _Paged(Connector):
            SOURCE_NAME = "Paginé"
            URL = "https://pages.opt09bis.test/page"

            def search(self, query, max_results=1000, **kwargs):
                report = self._start_report()
                items = []
                try:
                    for page in range(1, 20):
                        self.check_cancelled()
                        self.http.get(self.URL, params={"p": page},
                                      min_interval=0.3, max_attempts=1)
                        report.page(5)
                        items.extend(_items(f"P{page}x", 5))
                except OperationCancelled as exc:
                    if self.run_context is None or \
                            not self.run_context.soft_stop(exc):
                        raise
                    report.truncated(REASON_DEADLINE, "collecte interrompue")
                return items

        monkeypatch.setattr(orchestrator, "get_connector_class", _classes({
            "crossref": _Paged}))
        ctx = _soft_ctx(1.0, grace=0.5, http_client=_client(
            **{"https://pages.opt09bis.test/": _CountingJsonAdapter({})}))
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=["crossref"], workers=1), ctx,
            ranking="off")
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE), \
            src.describe()
        assert src.item_count >= 10 and src.item_count % 5 == 0
        assert "notice(s) conservée(s)" in src.detail
        assert src.counters.get(orchestrator.SOFT_DEADLINE_UNREACHABLE) == 1
        assert not res.all_failed and len(res.items) == src.item_count


class TestSoftStopContract:
    def test_soft_stop(self):
        ctx = _soft_ctx(30)
        view = ctx.for_source()
        assert view.soft_stop(OperationCancelled(CANCEL_REASON_DEADLINE))
        assert not view.soft_stop(OperationCancelled(CANCEL_REASON_CANCELLED))
        assert not view.soft_stop(ValueError("x"))
        ctx.cancel()                    # annulation par le client
        assert not view.soft_stop(OperationCancelled(CANCEL_REASON_DEADLINE))

    def test_no_soft_deadline_no_soft_stop(self):
        ctx = RunContext()
        assert ctx.for_source() is ctx
        assert not ctx.soft_stop(OperationCancelled(CANCEL_REASON_DEADLINE))

    def test_hard_deadline_is_not_a_soft_stop(self):
        ctx = RunContext(deadline=time.monotonic() - 1,
                         soft_deadline=time.monotonic() + 30)
        assert not ctx.for_source().soft_stop(
            OperationCancelled(CANCEL_REASON_DEADLINE))


# === Retour MCP : atteinte ou intenable ===

@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
    monkeypatch.setenv("MOISSON_CACHE", "0")
    monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "cache" / "http"))
    monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_MIN_S", 0.1)
    monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_GRACE_S", 0.3)
    return tmp_path


def _runner(name, cls, query, mps, output_dir, run_id, http_client,
            run_context=None, source_key=None, **kw):
    """HAL répond ; CrossRef est arrêtée aussitôt, faute de créneau avant
    l'échéance (comme la politesse de ``utils.http``) ; Isidore est muette
    jusqu'à son signal."""
    if source_key == "hal":
        return SourceResult(source=source_key, display_name=name,
                            items=_items(name), status=STATUS_COMPLETE)
    if source_key == "crossref":
        raise OperationCancelled(CANCEL_REASON_DEADLINE)
    while not run_context.signal.is_set():
        time.sleep(0.01)
    run_context.signal.check()
    return []


class TestReachedOrUnreachable:
    def test_unreachable_before_the_deadline_is_not_reached(self, out,
                                                            monkeypatch):
        monkeypatch.setattr("engine.orchestrator.run_source", _runner)
        t0 = time.monotonic()
        res = asyncio.run(mcp_server._tool_moisson_search({
            "query": "q", "sources": ["hal", "crossref"],
            "deadline_seconds": 2.0}))
        assert time.monotonic() - t0 < 1.5          # bien avant l'échéance
        blocks, structured = res
        assert structured["soft_deadline"] == {
            "seconds": 2.0, "reached": False, "stopped_sources": ["CrossRef"],
            "unreachable_sources": ["CrossRef"]}
        second = blocks[0].text.split("\n")[1]
        assert second.startswith(
            "Échéance souple de 2 s intenable pour 1 source(s) (motif "
            "« deadline » : CrossRef) : arrêtée(s) avant l'échéance")
        assert "atteinte" not in second
        assert "deadline_seconds plus grand" in second
        detail = structured["by_source_status"]["CrossRef"]["detail"]
        assert "intenable" in detail

    def test_mixed_reached_and_unreachable(self, out, monkeypatch):
        monkeypatch.setattr("engine.orchestrator.run_source", _runner)
        res = asyncio.run(mcp_server._tool_moisson_search({
            "query": "q", "sources": ["hal", "isidore", "crossref"],
            "deadline_seconds": 0.5}))
        blocks, structured = res
        sd = structured["soft_deadline"]
        assert sd["reached"] is True
        assert sd["stopped_sources"] == ["Isidore", "CrossRef"]
        assert sd["unreachable_sources"] == ["CrossRef"]
        second = blocks[0].text.split("\n")[1]
        assert second.startswith(
            "Échéance souple de 0.5 s atteinte : 2 source(s) arrêtée(s) ou non "
            "démarrée(s) (motif « deadline » : Isidore, CrossRef), dont 1 "
            "avant l'échéance")

    def test_without_clock_reached_comes_from_the_sources(self):
        from mcp import types
        key = orchestrator.SOFT_DEADLINE_UNREACHABLE
        structured = {"by_source_status": {
            "CORE": {"reason": "deadline", "counters": {key: 1}}}}
        out = mcp_server._note_soft_deadline(
            ([types.TextContent(type="text", text="Moisson.\nsuite")],
             structured), 8.0)
        assert structured["soft_deadline"]["reached"] is False
        assert "intenable" in out[0][0].text.split("\n")[1]


# === deadline_seconds à travers le répartiteur du SDK ===

async def _sdk_call(name, args):
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(
            mcp_server.server) as client:
        return await client.call_tool(name, args)


class TestSchemaClampThroughSdk:
    @pytest.mark.parametrize("value,expected", [(1000, 900.0), (5, 10.0),
                                                (900, 900.0)])
    def test_value_is_clamped_not_refused(self, out, monkeypatch, value,
                                          expected):
        monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_MIN_S", 10)
        seen = {}

        def runner(name, cls, query, mps, output_dir, run_id, http_client,
                   run_context=None, source_key=None, **kw):
            seen["soft_s"] = run_context.soft_deadline_s
            return _items(name)

        monkeypatch.setattr("engine.orchestrator.run_source", runner)
        res = anyio.run(_sdk_call, "moisson_search", {
            "query": "q", "sources": ["hal"], "deadline_seconds": value})
        assert res.isError is False, res.content[0].text
        assert seen["soft_s"] == expected
        sc = res.structuredContent
        assert sc["settings"]["soft_deadline_seconds"] == expected
        assert sc["soft_deadline"]["reached"] is False

    def test_negative_is_still_refused(self, out, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("aucune moisson attendue")
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", boom)
        res = anyio.run(_sdk_call, "moisson_search", {
            "query": "q", "sources": ["hal"], "deadline_seconds": -1})
        assert res.isError is True
