"""
Revue d'OPT-09 ter : ce que disent le détail d'une source arrêtée par
l'échéance souple et la ligne d'échéance du retour MCP.

Constats corrigés :

- le détail « K notice(s) conservée(s) » était compté AVANT le filtre local
  de langue (CORE, Isidore et ses sous-sources) : une source « partielle »
  sans notice publiée annonçait « 300 notice(s) conservée(s) ». Il compte
  désormais les notices publiées et nomme celles que le filtre écarte ;
- une source arrêtée pendant l'enrichissement OAI (étape facultative, après
  une recherche Isidore terminée) était dite « collecte interrompue » : le
  détail dit de nouveau « pendant l'enrichissement OAI » (compteur
  ``SOFT_STOP_DURING_ENRICHMENT`` posé par le connecteur) ;
- la ligne d'échéance du retour MCP rangeait une source ``partial/deadline``
  parmi les sources sans notice (« le corpus des sources qui ont répondu à
  temps est publié. Pour les attendre… ») : elle nomme maintenant les
  notices déjà reçues et publiées des sources arrêtées, et conseille de les
  « compléter » ;
- la description de ``deadline_seconds``, le commentaire de ``config.py`` et
  la FAQ affirmaient sans réserve que les notices déjà enrichies le restent :
  faux pour une source abandonnée après le délai de grâce.

Aucun accès réseau ni socket : services en mémoire (outils du fichier de
tests d'OPT-09 ter), OAI (sickle) doublé.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

import config
import connectors.openedition as openedition_module
import mcp_server
from connectors.hal import HALConnector
from connectors.isidore_scan import ScanRegistry
from connectors.openedition import OpenEditionConnector
from engine import orchestrator
from engine.models import (
    REASON_DEADLINE, SOFT_STOP_DURING_ENRICHMENT, STATUS_PARTIAL, RunContext,
    SearchReport, SearchRequest, SourceResult,
)
from mcp import types
from utils.http import HTTPClient

from tests.unit.test_opt09ter_soft_stop import (  # noqa: F401 (fixture)
    ENRICHERS, _Api, _FakeRecord, _FastCore, _SlowAfter, _classes, _client,
    _core_page, _enrich, _expire, _isidore_build, _no_politeness, _response,
    _soft_ctx,
)

ROOT = Path(__file__).resolve().parents[2]


# === Outils ===

def _core_mixed_page(work, size=100):
    """Pages CORE dont une notice sur deux est en français."""
    base = _core_page(work, size=size)

    def build(n, request):
        body = base(n, request)
        for i, w in enumerate(body["results"]):
            if i % 2 == 0:
                w["language"] = {"code": "fr", "name": "French"}
        return body
    return build


def _harvest_core(monkeypatch, build, languages, pages=3, tmp_path=None):
    """CORE sert ``pages`` pages aussitôt puis se tait ; échéance de 0,6 s."""
    monkeypatch.setattr(orchestrator, "get_connector_class",
                        _classes({"core": _FastCore}))
    core = _SlowAfter(build, pages=pages)
    client = HTTPClient(use_cache=False)
    client.session.mount("https://api.core.ac.uk/", core)
    ctx = RunContext(soft_deadline=time.monotonic() + 0.6, soft_deadline_s=0.6,
                     soft_grace_s=0.5, http_client=client, output_dir=tmp_path)
    res = orchestrator.harvest(
        SearchRequest(query="discours", sources=["core"], languages=languages,
                      max_per_source=1000), ctx, ranking="off")
    return res, ctx


def _items_with_languages(*codes):
    return [{"itemType": "journalArticle", "title": f"Titre {i}",
             "language": code, "extra": "Sources: Test"}
            for i, code in enumerate(codes)]


def _job(**extra):
    job = {"key": "core", "name": "CORE", "started_at": 0.0}
    job.update(extra)
    return job


def _reached_ctx():
    """Échéance souple déjà passée (atteinte), exécution non annulée."""
    return RunContext(soft_deadline=time.monotonic() - 0.1, soft_deadline_s=0.6)


def _partial(items, counters=None):
    return SourceResult(source="core", display_name="CORE", items=list(items),
                        status=STATUS_PARTIAL, reason=REASON_DEADLINE,
                        detail="collecte interrompue par l'échéance souple",
                        counters=dict(counters or {}))


# === Constat 1 : détail compté après le filtre local de langue ===

class TestDetailCountsPublishedNotices:
    def test_core_all_filtered_out_says_zero_kept(self, monkeypatch, core_work):
        """Avant : « 300 notice(s) conservée(s) » pour 0 notice publiée.
        Après : 0 conservée sur 300 reçues, filtre local de langue nommé."""
        res, _ = _harvest_core(monkeypatch, _core_page(core_work, size=100),
                               ["fr"])
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE)
        assert src.item_count == 0
        assert "300 notice(s) conservée(s)" not in src.detail
        assert ("collecte interrompue, 0 notice(s) conservée(s) sur 300 "
                "reçue(s), filtre local de langue") in src.detail
        assert any("filtre local de langue (fr) : 300 notice(s) écartée(s)"
                   in w for w in src.warnings)
        # Aucune notice publiée : échec total, comme avant.
        assert res.all_failed

    def test_core_half_kept_counts_the_published_half(self, monkeypatch,
                                                      core_work, tmp_path):
        res, ctx = _harvest_core(monkeypatch, _core_mixed_page(core_work),
                                 ["fr"], tmp_path=tmp_path)
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE)
        assert src.item_count == 150 and len(res.items) == 150
        assert ("150 notice(s) conservée(s) sur 300 reçue(s), filtre local "
                "de langue") in src.detail
        assert src.counters["language_filter_removed"] == 150
        # Payload brut : notices de la source avant le filtre (inchangé).
        raw = json.loads(orchestrator.raw_path(
            tmp_path, "CORE", ctx.run_id).read_text(encoding="utf-8"))
        assert len(raw) == 300

    def test_without_language_filter_text_unchanged(self, monkeypatch,
                                                    core_work):
        res, _ = _harvest_core(monkeypatch, _core_page(core_work, size=100),
                               None)
        src = res.sources[0]
        assert src.item_count == 300
        assert src.detail.endswith("collecte interrompue, 300 notice(s) "
                                   "conservée(s)")
        assert "reçue(s)" not in src.detail

    def test_outcome_counts_after_the_local_filter(self):
        """``_soft_deadline_outcome`` seul : notices intactes (le filtre est
        appliqué ensuite), compte anticipé ; notices sans langue conservées
        comme le fait le filtre."""
        items = _items_with_languages("en", "fr", None, "en")
        res = orchestrator._soft_deadline_outcome(
            _partial(items), _job(local_languages=["fr"]), _reached_ctx())
        assert res.item_count == 4                  # rien n'est retiré ici
        assert res.detail == ("échéance souple de 0.6 s atteinte : collecte "
                              "interrompue, 2 notice(s) conservée(s) sur 4 "
                              "reçue(s), filtre local de langue")
        kept = orchestrator._local_language_filter(
            res, _job(local_languages=["fr"]))
        assert kept.item_count == 2

    def test_cancelled_with_items_counts_after_the_local_filter(self):
        """Exécutant qui rend des notices avec l'annulation : même compte."""
        result = SourceResult(
            source="core", display_name="CORE",
            items=_items_with_languages("en", "fr", "en"),
            status="cancelled", reason=REASON_DEADLINE, duration_s=1.0)
        ctx = _reached_ctx()
        res = orchestrator._soft_deadline_outcome(
            result, _job(local_languages=["fr"],
                         started_at=ctx.soft_deadline - 1), ctx)
        assert res.status == STATUS_PARTIAL
        assert res.detail.endswith("1 notice(s) conservée(s) sur 3 reçue(s), "
                                   "filtre local de langue")

    def test_abandoned_enrichment_fallback_counts_after_the_filter(
            self, monkeypatch, tmp_path, isidore_response):
        """Reprise sans enrichissement d'une source abandonnée (GetRecord
        bloqué) avec un filtre local de langue qui écarte tout : le détail
        compte 0 notice publiée sur 6 reçues."""
        box = {}

        class _Sickle:
            def GetRecord(self, **kwargs):
                box["n"] = box.get("n", 0) + 1
                if box["n"] == 2:
                    ctx = box["ctx"]
                    limit = ctx.soft_deadline + ctx.soft_grace() + 0.2
                    while time.monotonic() < limit:
                        time.sleep(0.01)
                return _FakeRecord()

        _patch_openedition(monkeypatch, _Sickle)
        isidore = _Api(_isidore_build(isidore_response, per_page=6, total=6))
        client = HTTPClient(use_cache=False)
        client.session.mount("https://api.isidore.science/", isidore)
        ctx = RunContext(soft_deadline=time.monotonic() + 0.3,
                         soft_deadline_s=0.3, soft_grace_s=0.2,
                         http_client=client, output_dir=tmp_path)
        box["ctx"] = ctx
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["openedition"],
                          enrich_oai=True, languages=["en"],
                          max_per_source=50), ctx, ranking="off")
        src = res.sources[0]
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE), \
            src.describe()
        assert src.item_count == 0
        assert ("notices Isidore reprises sans enrichissement, 0 notice(s) "
                "conservée(s) sur 6 reçue(s), filtre local de langue") \
            in src.detail
        assert any("toujours occupée" in w for w in src.warnings)
        time.sleep(0.4)                 # fin du fil abandonné


# === Constat 3 : arrêt pendant l'enrichissement OAI, nature gardée ===

def _patch_openedition(monkeypatch, sickle_cls, title="enrichie par OAI"):
    monkeypatch.setattr(OpenEditionConnector, "_get_sickle",
                        lambda self: sickle_cls())
    monkeypatch.setattr(OpenEditionConnector, "_extract_oai_id",
                        staticmethod(lambda item: "oai:revues.org:lidil/1"))
    monkeypatch.setattr(openedition_module, "map_qdc_to_zotero",
                        lambda *a, **k: {"shortTitle": title})


@pytest.mark.parametrize("key", list(ENRICHERS))
def test_enrichment_stop_is_marked_by_the_connector(key, monkeypatch):
    """Les quatre boucles d'enrichissement marquent leur arrêt souple."""
    box = {}
    ctx = _soft_ctx(HTTPClient(use_cache=False))
    orig = ctx.for_source

    def for_source():
        box["view"] = orig()
        return box["view"]

    ctx.for_source = for_source

    def on_record(n):
        if n == 2:
            _expire(box["view"].signal)

    conn, calls, _, out = _enrich(key, monkeypatch, ctx, on_record)
    assert not isinstance(out, BaseException), out
    report = conn.last_status
    assert (report.status, report.reason) == (STATUS_PARTIAL, REASON_DEADLINE)
    assert report.counters.get(SOFT_STOP_DURING_ENRICHMENT) == 1


class TestEnrichmentStopDetail:
    def test_marker_only_when_the_enrichment_stop_is_the_reason(self):
        """Arrêt antérieur (parcours Isidore) resté motif, ou erreur : pas de
        marque ; troncature de fenêtre remplacée : marque posée."""
        from connectors.core import CoreConnector
        from utils.http import CANCEL_REASON_DEADLINE, OperationCancelled
        view = _soft_ctx(HTTPClient(use_cache=False)).for_source()
        conn = CoreConnector(http_client=view.http_client, run_context=view,
                             api_key="cle-factice")
        stop = OperationCancelled(CANCEL_REASON_DEADLINE)

        first = SearchReport()
        first.truncated(REASON_DEADLINE, "parcours Isidore interrompu")
        assert conn.soft_stopped(stop, first, 3, "enrichissement OAI "
                                 "interrompu", during_enrichment=True)
        assert SOFT_STOP_DURING_ENRICHMENT not in first.counters

        failed = SearchReport()
        failed.fail("http_error", "HTTP 502")
        assert conn.soft_stopped(stop, failed, 3, "x", during_enrichment=True)
        assert SOFT_STOP_DURING_ENRICHMENT not in failed.counters

        window = SearchReport()
        window.window("Isidore ne sert que 200 résultats")
        assert conn.soft_stopped(stop, window, 3, "x", during_enrichment=True)
        assert window.counters[SOFT_STOP_DURING_ENRICHMENT] == 1
        assert window.provider_window

        paginated = SearchReport()
        assert conn.soft_stopped(stop, paginated, 3)
        assert SOFT_STOP_DURING_ENRICHMENT not in paginated.counters

    def test_scan_stop_then_enrichment_is_a_collection_stop(
            self, monkeypatch, isidore_response):
        """Sous-source arrêtée dans le parcours Isidore (2 pages) : aucune
        requête OAI, pas de marque d'enrichissement, « collecte
        interrompue » dans le détail final."""
        calls = []

        class _Sickle:
            def GetRecord(self, **kwargs):
                calls.append(kwargs)
                return _FakeRecord()

        _patch_openedition(monkeypatch, _Sickle, title="enrichie")
        api = _Api(_isidore_build(isidore_response))
        client = _client("https://api.isidore.science/", api)
        ctx = _soft_ctx(client)
        view = ctx.for_source()

        def hook(n, request):
            if n == 2:
                _expire(view.signal)

        api.hook = hook
        conn = OpenEditionConnector(
            http_client=view.http_client, run_context=view,
            scan_registry=ScanRegistry(replies=2, window=0), enrich_oai=True)
        items = conn.search("discours numérique", max_results=30)
        assert len(items) == 4 and not calls
        report = conn.last_status
        assert "parcours Isidore" in report.detail
        assert SOFT_STOP_DURING_ENRICHMENT not in report.counters
        result = _partial(items, report.counters)
        result.detail = report.detail
        out = orchestrator._soft_deadline_outcome(
            result, _job(key="openedition", name="OpenEdition"), _reached_ctx())
        assert "collecte interrompue, 4 notice(s) conservée(s)" in out.detail

    def test_outcome_says_during_enrichment(self):
        res = orchestrator._soft_deadline_outcome(
            _partial(_items_with_languages("fr", "fr", "fr"),
                     {SOFT_STOP_DURING_ENRICHMENT: 1}),
            _job(key="openedition", name="OpenEdition"), _reached_ctx())
        assert res.detail == ("échéance souple de 0.6 s atteinte pendant "
                              "l'enrichissement OAI, recherche Isidore "
                              "terminée : 3 notice(s) conservée(s)")
        assert "collecte interrompue" not in res.detail
        # Le détail du connecteur passe aux avertissements.
        assert "collecte interrompue par l'échéance souple" in res.warnings

    def test_harvest_openedition_stopped_during_enrichment(
            self, monkeypatch, tmp_path, isidore_response):
        """Avant : « collecte interrompue, 6 notice(s) conservée(s) ». Après :
        « pendant l'enrichissement OAI, recherche Isidore terminée »."""
        box = {}

        class _Sickle:
            def GetRecord(self, **kwargs):
                box["n"] = box.get("n", 0) + 1
                if box["n"] == 2:
                    while time.monotonic() < box["ctx"].soft_deadline + 0.05:
                        time.sleep(0.01)
                return _FakeRecord()

        _patch_openedition(monkeypatch, _Sickle)
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
        assert (src.status, src.reason) == (STATUS_PARTIAL, REASON_DEADLINE)
        assert src.detail == ("échéance souple de 0.5 s atteinte pendant "
                              "l'enrichissement OAI, recherche Isidore "
                              "terminée : 6 notice(s) conservée(s)")
        assert src.counters[SOFT_STOP_DURING_ENRICHMENT] == 1
        assert any("après 2 notice(s) traitée(s) sur 6" in w
                   for w in src.warnings)
        assert len([it for it in src.items if it.get("shortTitle")]) == 2


# === Constats 2 et 5 : ligne d'échéance du retour MCP ===

def _note(by_status, seconds=50.0, tool_error=False):
    structured = {"by_source_status": by_status, "settings": {}}
    if tool_error:
        err = mcp_server.ToolError("Moisson en échec.\nsuite",
                                   structured=structured)
        out = mcp_server._note_soft_deadline(err, seconds, at=0.0)
        return str(out).split("\n")[1], structured
    blocks = [types.TextContent(type="text", text="Moisson terminée.\nsuite")]
    out = mcp_server._note_soft_deadline((blocks, structured), seconds, at=0.0)
    return out[0][0].text.split("\n")[1], structured


def _st(status, items, **extra):
    info = {"status": status, "reason": "deadline", "items": items,
            "counters": {}}
    info.update(extra)
    return info


class TestMcpDeadlineLine:
    def test_partial_source_notices_are_said_published(self):
        line, structured = _note({"HAL": {"status": "complete", "items": 5,
                                          "reason": None},
                                  "CORE": _st("partial", 900)})
        assert line.startswith("Échéance souple de 50 s atteinte : 1 source(s) "
                               "arrêtée(s) ou non démarrée(s) (motif "
                               "« deadline » : CORE)")
        assert ("le corpus des sources qui ont répondu à temps est publié, "
                "avec les notices déjà reçues des sources arrêtées (CORE : "
                "900 ; « partielle(s) »).") in line
        assert line.endswith(" Pour les compléter : deadline_seconds plus "
                             "grand, si le client accepte des appels plus "
                             "longs.")
        assert "Pour les attendre" not in line
        # structuredContent inchangé (le détail est dans by_source_status).
        assert structured["soft_deadline"] == {
            "seconds": 50.0, "reached": True, "stopped_sources": ["CORE"],
            "unreachable_sources": []}

    def test_sources_without_notices_text_unchanged(self):
        line, _ = _note({"HAL": {"status": "complete", "items": 5},
                         "Isidore": _st("error", 0),
                         "Cairn": _st("skipped", 0)})
        assert line.endswith(
            " ; le corpus des sources qui ont répondu à temps est publié. "
            "Pour les attendre : deadline_seconds plus grand, si le client "
            "accepte des appels plus longs.")
        assert "notices déjà reçues" not in line

    def test_mixed_sources_are_distinguished(self):
        line, _ = _note({"Isidore": _st("error", 0),
                         "CORE": _st("partial", 300),
                         "HAL": _st("partial", 12)})
        assert ("avec les notices déjà reçues des sources arrêtées (CORE : "
                "300, HAL : 12 ; « partielle(s) ») ; sans notice : Isidore.") \
            in line
        assert " Pour attendre ou compléter ces sources : deadline_seconds " \
               "plus grand" in line

    def test_partial_without_published_notice_counts_as_empty(self):
        """Source partielle dont le filtre local de langue a tout écarté."""
        line, _ = _note({"HAL": {"status": "complete", "items": 5},
                         "CORE": _st("partial", 0)})
        assert "notices déjà reçues" not in line
        assert "Pour les attendre" in line

    def test_unreachable_partial_source(self):
        key = orchestrator.SOFT_DEADLINE_UNREACHABLE
        structured = {"by_source_status": {
            "CORE": _st("partial", 900, counters={key: 1})}}
        blocks = [types.TextContent(type="text", text="Moisson.\nsuite")]
        out = mcp_server._note_soft_deadline((blocks, structured), 50.0)
        line = out[0][0].text.split("\n")[1]
        assert line.startswith("Échéance souple de 50 s intenable pour 1 "
                               "source(s) (motif « deadline » : CORE)")
        assert "(CORE : 900 ; « partielle(s) »)" in line
        assert "Pour les compléter" in line

    def test_tool_error_text_unchanged(self):
        line, _ = _note({"Isidore": _st("error", 0)}, seconds=0.4,
                        tool_error=True)
        assert line == (
            "Échéance souple de 0.4 s atteinte : 1 source(s) arrêtée(s) ou non "
            "démarrée(s) (motif « deadline » : Isidore) ; aucune autre source "
            "n'a abouti, aucun corpus n'a été écrit. Pour les attendre : "
            "deadline_seconds plus grand, si le client accepte des appels "
            "plus longs.")

    def test_stdio_moisson_search_names_the_kept_notices(
            self, tmp_path, monkeypatch, core_work, hal_solr_response):
        """Bout en bout (stdio, échéance de 0,6 s, CORE lent après 3 pages) :
        la ligne d'échéance dit que les 300 notices de CORE sont publiées."""
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
                return _response(request, copy.deepcopy(hal_solr_response))
            raise requests.ConnectionError(f"hôte inattendu : {host}")

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
        res = asyncio.run(mcp_server._tool_moisson_search({
            "query": "discours", "sources": ["hal", "core"],
            "max_per_source": 1000, "cache": False, "deadline_seconds": 0.6}))
        assert not mcp_server._is_tool_error(res)
        blocks, structured = res
        assert structured["by_source_status"]["CORE"]["items"] == 300
        second = blocks[0].text.split("\n")[1]
        assert "(CORE : 300 ; « partielle(s) »)" in second
        assert "Pour les compléter" in second
        assert "Pour les attendre" not in second


# === Constat 4 : réserve sur les notices déjà enrichies ===

class TestEnrichedNoticesReserve:
    RESERVE = "délai de grâce"

    def test_deadline_seconds_description(self):
        text = mcp_server._soft_deadline_schema()["description"]
        assert "celles déjà enrichies le restant, sauf si" in text
        assert self.RESERVE in text and "sans aucun enrichissement" in text

    def test_config_comment(self):
        source = (ROOT / "config.py").read_text(encoding="utf-8")
        block = source[source.index("celles déjà enrichies le"):][:400]
        assert self.RESERVE in block and "sans aucun enrichissement" in block

    def test_faq(self):
        faq = (ROOT / "docs" / "FAQ.md").read_text(encoding="utf-8")
        block = faq[faq.index("Ce qu'une source arrêtée garde"):][:1200]
        assert "enrichies le restent" in block
        assert self.RESERVE in block and "sans aucun enrichissement" in block
