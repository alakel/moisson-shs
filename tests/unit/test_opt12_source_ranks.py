"""
OPT-12 : rangs et scores natifs par source, tri à la source, transmission du
mode et des langues par ``run_source`` (constats C25, C26).

Sans aucun accès réseau (doublures de client HTTP) :

- Semantic Scholar : bulk trié ``citationCount:desc`` en bibliographie
  (poursuivi par ``token``), sans tri en collecte (ordre ``paperId`` de
  l'API) ; rang natif sans score ; ``languages`` ignoré ;
- CrossRef : ``score`` demandé dans ``select`` et capturé ;
  ``sort=score&order=desc`` avec le curseur en bibliographie (sans tri, le
  curseur sert l'ordre de l'index, vérifié en live) ; ``languages`` ignoré
  (pas de filtre de langue dans l'API) ;
- OpenAlex : ``relevance_score`` demandé et capturé ; filtre ``language:`` ;
- Isidore : UNE requête (``replies=200``) pour la source directe et les cinq
  sous-sources ; rang natif = position dans la fenêtre, sans score ;
- ``run_source`` : mode et langues transmis aux seuls connecteurs qui les
  acceptent ; rangs portés par ``SourceResult`` s'ils sont alignés ;
  ``as_dict`` compact ; jamais de clé de rang dans les notices.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

import config
from connectors import openalex as openalex_module
from connectors import semantic_scholar as s2_module
from connectors.cairn import CairnConnector
from connectors.crossref import CrossrefConnector, SELECT_FIELDS as CR_SELECT
from connectors.erudit import EruditConnector
from connectors.isidore import IsidoreConnector
from connectors.isidore_scan import ScanRegistry
from connectors.openalex import OpenAlexConnector, SELECT_FIELDS as OA_SELECT
from connectors.openedition import OpenEditionConnector
from connectors.persee import PerseeConnector
from connectors.semantic_scholar import SemanticScholarConnector, bulk_sort
from connectors.theses_fr import ThesesFrConnector
from engine import orchestrator
from engine.models import (
    RunContext, SearchReport, SearchRequest, SourceResult, native_score,
)

RANK_KEYS = {"score", "rank", "native_rank", "native_ranks", "relevance_score",
             "native_sort"}


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.from_cache = False
        self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        return self._payload


class Recorder:
    """Client HTTP simplifié : ``handler(params) -> payload`` ; appels notés."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: List[Dict[str, Any]] = []

    def get(self, url, params=None, **kw):
        p = dict(params or {})
        self.calls.append(p)
        return FakeResponse(self.handler(p))

    def close(self):
        pass


def assert_no_rank_keys(items: List[Dict]) -> None:
    for item in items:
        assert not RANK_KEYS & set(item), sorted(RANK_KEYS & set(item))
        extra = item.get("extra", "").lower()
        assert "relevance" not in extra and "rank" not in extra


# === Semantic Scholar ===

def s2_paper(i: int) -> Dict[str, Any]:
    return {"paperId": f"{i:040x}", "title": f"Paper {i}", "year": 2020,
            "authors": [{"name": f"Author {i}"}], "externalIds": {"CorpusId": i},
            "citationCount": 1000 - i}


def s2_handler(total: int = 2500, page: int = 1000):
    def handler(p):
        start = int(p.get("token") or 0)
        batch = [s2_paper(i) for i in range(start, min(start + page, total))]
        nxt = str(start + len(batch)) if start + len(batch) < total else None
        return {"total": total, "token": nxt, "data": batch}
    return handler


def s2_search(fake, **kw):
    c = SemanticScholarConnector(http_client=fake, api_key="CLE-FACTICE")
    items = c.search("digital discourse", **kw)
    return items, c.last_status


def test_s2_bibliographie_sorts_by_citations_and_keeps_the_sort_with_the_token():
    fake = Recorder(s2_handler())
    items, report = s2_search(fake, max_results=1500, mode="bibliographie")
    assert len(items) == 1500
    assert [c.get("sort") for c in fake.calls] == ["citationCount:desc"] * 2
    assert fake.calls[1]["token"] == "1000"
    assert report.native_sort == "citationCount:desc"
    assert [r["rank"] for r in report.native_ranks] == list(range(1, 1501))
    assert all(r["score"] is None for r in report.native_ranks)
    assert_no_rank_keys(items)


def test_s2_collecte_sends_no_sort_and_names_the_api_order():
    fake = Recorder(s2_handler(total=300))
    items, report = s2_search(fake, max_results=100, mode="collecte")
    assert "sort" not in fake.calls[0]
    assert report.native_sort == "paperId"
    assert len(report.native_ranks) == len(items) == 100


def test_s2_ignores_languages():
    fake = Recorder(s2_handler(total=50))
    _, report = s2_search(fake, max_results=100, mode="bibliographie", languages=["fr"])
    assert not any("lang" in k.lower() for k in fake.calls[0])
    assert report.native_filters == {}


def test_s2_sort_setting_is_validated(monkeypatch):
    assert bulk_sort("publicationDate:desc") == "publicationDate:desc"
    assert bulk_sort("paperId") == "paperId"
    assert bulk_sort("relevance") == "citationCount:desc"
    assert bulk_sort("citationCount:up") == "citationCount:desc"
    monkeypatch.setattr(s2_module, "S2_BIBLIO_SORT", "publicationDate:desc")
    fake = Recorder(s2_handler(total=20))
    _, report = s2_search(fake, max_results=10, mode="bibliographie")
    assert fake.calls[0]["sort"] == "publicationDate:desc"
    assert report.native_sort == "publicationDate:desc"


def test_s2_default_sort_setting():
    assert config.S2_BIBLIO_SORT == "citationCount:desc"


# === CrossRef ===

def crossref_handler(total: int = 3000):
    def handler(p):
        start = int(p["cursor"]) if p["cursor"] != "*" else 0
        rows = int(p["rows"])
        n = max(0, min(rows, total - start))
        items = [{"DOI": f"10.1/cr{start + k}", "title": [f"Titre {start + k}"],
                  "type": "journal-article", "issued": {"date-parts": [[2020]]},
                  "author": [{"family": f"Nom{start + k}", "given": "Jean"}],
                  "score": round(50.0 - (start + k) / 100.0, 3)}
                 for k in range(n)]
        msg = {"items": items, "total-results": total}
        if start + n < total:
            msg["next-cursor"] = str(start + n)
        return {"message": msg}
    return handler


def cr_search(fake, **kw):
    c = CrossrefConnector(http_client=fake)
    items = c.search("discours numérique", **kw)
    return items, c.last_status


def test_crossref_requests_and_captures_the_score():
    assert "score" in CR_SELECT.split(",")
    fake = Recorder(crossref_handler())
    items, report = cr_search(fake, max_results=1200, mode="bibliographie")
    assert len(items) == 1200
    assert all(c["sort"] == "score" and c["order"] == "desc" for c in fake.calls)
    assert fake.calls[1]["cursor"] == "1000"   # tri conservé sous le curseur
    assert report.native_sort == "score:desc"
    assert [r["rank"] for r in report.native_ranks] == list(range(1, 1201))
    assert report.native_ranks[0]["score"] == 50.0
    assert report.native_ranks[-1]["score"] == round(50.0 - 1199 / 100.0, 3)
    assert_no_rank_keys(items)


def test_crossref_collecte_keeps_the_historical_cursor_without_sort():
    fake = Recorder(crossref_handler(total=100))
    items, report = cr_search(fake, max_results=50, mode="collecte")
    assert "sort" not in fake.calls[0] and "order" not in fake.calls[0]
    assert report.native_sort == "cursor_default"
    assert len(report.native_ranks) == 50
    assert report.native_ranks[0]["score"] == 50.0   # score capturé aussi


def test_crossref_ignores_languages():
    fake = Recorder(crossref_handler(total=10))
    _, report = cr_search(fake, max_results=10, from_date="2018",
                          mode="bibliographie", languages=["fr"])
    assert "language" not in fake.calls[0].get("filter", "")
    assert report.native_filters == {}


# === OpenAlex ===

def openalex_fake(monkeypatch, total: int = 450):
    calls: List[Dict[str, Any]] = []

    def get_json(url, params=None, **kw):
        p = dict(params or {})
        calls.append(p)
        start = int(p["cursor"]) if p["cursor"] != "*" else 0
        per_page = int(p["per-page"])
        n = max(0, min(per_page, total - start))
        results = [{"id": f"https://openalex.org/W{start + k}",
                    "display_name": f"Work {start + k}", "title": f"Work {start + k}",
                    "publication_year": 2021, "type": "article", "language": "fr",
                    "authorships": [], "relevance_score": 900.0 - (start + k)}
                   for k in range(n)]
        nxt = str(start + n) if start + n < total else None
        return {"meta": {"count": total, "next_cursor": nxt}, "results": results}
    monkeypatch.setattr(openalex_module, "get_json", get_json)
    return calls


class _Http:
    session = object()

    def close(self):
        pass


def oa_search(**kw):
    c = OpenAlexConnector(http_client=_Http(), api_key="CLE-FACTICE")
    items = c.search("digital discourse", **kw)
    return items, c.last_status


def test_openalex_requests_and_captures_the_relevance_score(monkeypatch):
    calls = openalex_fake(monkeypatch)
    assert "relevance_score" in OA_SELECT.split(",")
    items, report = oa_search(max_results=300, mode="bibliographie")
    assert len(items) == 300 and len(calls) == 2
    assert report.native_sort == "relevance_score:desc"
    assert [r["rank"] for r in report.native_ranks] == list(range(1, 301))
    assert [r["score"] for r in report.native_ranks[:3]] == [900.0, 899.0, 898.0]
    assert "sort" not in calls[0]   # ordre de pertinence par défaut de l'API
    assert_no_rank_keys(items)


def test_openalex_native_language_filter(monkeypatch):
    calls = openalex_fake(monkeypatch, total=5)
    _, report = oa_search(max_results=10, from_date="2018", languages=["fr", "EN"])
    assert calls[0]["filter"] == "from_publication_date:2018-01-01,language:fr|en"
    assert report.native_filters == {"languages": ["fr", "en"]}


def test_openalex_language_filter_not_duplicated(monkeypatch):
    calls = openalex_fake(monkeypatch, total=5)
    _, report = oa_search(max_results=10, extra_filters="language:de", languages=["fr"])
    assert calls[0]["filter"] == "language:de"
    assert report.native_filters == {}


# === Isidore : une requête, rang = position dans la fenêtre ===

DOMAINS = ["journals.openedition.org", "www.cairn.info", "www.persee.fr",
           "id.erudit.org", "www.theses.fr", "hal.science", "www.example.org"]


def isidore_record(i: int) -> Dict[str, Any]:
    host = DOMAINS[i % len(DOMAINS)]
    url = f"https://{host}/article/w{i}"
    return {"@uri": f"10670/1.w{i}", "isidore": {
        "title": [{"@xml:lang": "fr", "$": f"Titre {i}"}], "url": url,
        "enrichedCreators": {"creator": {"@origin": "Dupont, Jean",
                                         "firstname": "Jean", "lastname": "Dupont"}},
        "date": {"@startDate": "2020-01-01", "normalizedDate": "2020-01-01", "year": "2020"},
        "types": {"type": ["article"]}, "ore": {"aggregates": [{"$": url}]},
        "language": "fr"}}


class FakeIsidore:
    """Fenêtre de 200 résultats, pages de ``replies`` (au-delà : répétition)."""

    def __init__(self, n: int = 5000):
        self.records = [isidore_record(i) for i in range(n)]
        self.calls: List[Dict[str, Any]] = []

    def get(self, url, params=None, **kw):
        p = dict(params or {})
        self.calls.append(p)
        replies = min(int(p["replies"]), 200)
        page = int(p["page"])
        offset = min((page - 1) * replies, 200 - replies)
        chunk = self.records[offset:offset + replies]
        return FakeResponse({"response": {"replies": {
            "meta": {"@items": str(len(self.records)), "@pageItems": str(len(chunk))},
            "content": {"reply": chunk}}}})

    def close(self):
        pass


def test_isidore_family_makes_one_request_with_replies_200():
    assert config.ISIDORE_REPLIES_PER_PAGE == 200
    fake = FakeIsidore()
    registry = ScanRegistry()
    classes = [IsidoreConnector, OpenEditionConnector, CairnConnector,
               PerseeConnector, EruditConnector, ThesesFrConnector]
    reports = {}
    for cls in classes:
        c = cls(http_client=fake, scan_registry=registry)
        items = c.search("discours numérique", max_results=200)
        reports[cls.__name__] = (items, c.last_status)
    assert len(fake.calls) == 1 and fake.calls[0]["replies"] == 200
    # Rang natif = position dans la fenêtre, identique pour tous les
    # consommateurs du flux ; aucun score.
    items, report = reports["IsidoreConnector"]
    assert [r["rank"] for r in report.native_ranks] == list(range(1, 201))
    items, report = reports["CairnConnector"]
    assert len(items) == len(report.native_ranks) > 0
    assert [r["rank"] for r in report.native_ranks] == [i + 1 for i in range(200)
                                                        if i % 7 == 1]
    assert all(r["score"] is None and r["sort"] == "isidore_default"
               for r in report.native_ranks)
    for items, _ in reports.values():
        assert_no_rank_keys(items)


def test_isidore_ranks_follow_date_filter_and_cap():
    fake = FakeIsidore(50)
    for i, rec in enumerate(fake.records):
        rec["isidore"]["date"]["year"] = "2010" if i % 2 else "2020"
    c = IsidoreConnector(http_client=fake, scan_registry=ScanRegistry())
    items = c.search("q", max_results=5, from_date="2018")
    assert len(items) == 5
    assert [r["rank"] for r in c.last_status.native_ranks] == [1, 3, 5, 7, 9]


def test_scan_status_as_dict_stays_compact():
    fake = FakeIsidore(30)
    c = IsidoreConnector(http_client=fake, scan_registry=ScanRegistry())
    c.search("q", max_results=10)
    assert c.last_scan_status.ranks == list(range(1, 11))
    assert "ranks" not in c.last_scan_status.as_dict()


# === SearchReport / SourceResult ===

def test_native_score_accepts_only_finite_numbers():
    assert native_score(3) == 3.0 and native_score("2.5") == 2.5
    for bad in (None, True, "abc", float("nan"), float("inf"), [1]):
        assert native_score(bad) is None


def test_report_ranks_carry_the_sort_and_are_trimmed():
    r = SearchReport()
    r.sorted_by("score desc,docid asc")
    for pos in (1, 2, 4):
        r.rank(pos, pos * 1.5)
    r.keep_ranks(2)
    assert r.native_ranks == [{"rank": 1, "score": 1.5, "sort": "score desc,docid asc"},
                              {"rank": 2, "score": 3.0, "sort": "score desc,docid asc"}]
    r.filtered("languages", ["fr"])
    assert r.native_filters == {"languages": ["fr"]}


def test_source_result_as_dict_is_compact_and_ranking_dict_complete():
    res = SourceResult(source="hal", display_name="HAL", items=[{"title": "a"}],
                       native_sort="score desc,docid asc",
                       native_ranks=[{"rank": 1, "score": 7.3, "sort": "score desc,docid asc"}],
                       native_filters={"languages": ["fr"]})
    d = res.as_dict()
    assert d["native_sort"] == "score desc,docid asc"
    assert d["native_filters"] == {"languages": ["fr"]}
    assert "native_ranks" not in d
    rk = res.ranking_dict()
    assert rk == {"source": "hal", "sort": "score desc,docid asc",
                  "filters": {"languages": ["fr"]},
                  "ranks": [{"rank": 1, "score": 7.3, "sort": "score desc,docid asc"}]}


# === run_source : transmission du mode et des langues, collecte des rangs ===

class _Spy:
    """Connecteur espion ; ``accepts`` : paramètres nommés de ``search``."""

    seen: List[Dict[str, Any]] = []
    init_kwargs: List[Dict[str, Any]] = []

    def __init__(self, http_client=None, run_context=None, **kwargs):
        type(self).init_kwargs.append(dict(kwargs))
        self.last_status = SearchReport(source="spy")

    def close(self):
        pass


class SpyWithKwargs(_Spy):
    def search(self, query, max_results=10, **kwargs):
        type(self).seen.append(dict(kwargs))
        self.last_status.sorted_by("s")
        self.last_status.rank(1, 2.0)
        self.last_status.rank(2)
        return [{"title": "a", "itemType": "journalArticle"},
                {"title": "b", "itemType": "journalArticle"}]


class SpyModeOnly(_Spy):
    def search(self, query, max_results=10, from_date=None, *, mode=None):
        type(self).seen.append({"mode": mode})
        return []


class SpyNoOption(_Spy):
    def search(self, query, max_results=10, from_date=None, to_date=None):
        type(self).seen.append({})
        return [{"title": "a"}]


class SpyMisaligned(_Spy):
    def search(self, query, max_results=10, **kwargs):
        self.last_status.rank(1)
        return [{"title": "a"}, {"title": "b"}]


@pytest.fixture(autouse=True)
def _reset_spies():
    _Spy.seen = []
    _Spy.init_kwargs = []
    yield


def _run(cls, **kw):
    return orchestrator.run_source("Spy", cls, "q", 10, None, "ts", object(),
                                   source_key="spy", **kw)


def test_run_source_passes_mode_and_languages_to_kwargs_connectors():
    res = _run(SpyWithKwargs, mode="bibliographie", languages=["FR", "en", "fr", "xyz"])
    assert _Spy.seen == [{"mode": "bibliographie", "languages": ["fr", "en"]}]
    assert res.native_sort == "s"
    assert res.native_ranks == [{"rank": 1, "score": 2.0, "sort": "s"},
                                {"rank": 2, "score": None, "sort": "s"}]
    assert_no_rank_keys(res.items)


def test_run_source_passes_only_the_accepted_options():
    _run(SpyModeOnly, mode="collecte", languages=["fr"])
    assert _Spy.seen == [{"mode": "collecte"}]
    _Spy.seen = []
    res = _run(SpyNoOption, mode="bibliographie", languages=["fr"])
    assert _Spy.seen == [{}]
    assert res.status == "complete" and res.native_ranks == []


def test_run_source_accepts_mode_and_languages_among_connector_options():
    _run(SpyWithKwargs, **{"mode": "bibliographie", "languages": "fr,en",
                           "enrich_oai": True})
    assert _Spy.seen == [{"mode": "bibliographie", "languages": ["fr", "en"]}]
    # Le constructeur ne reçoit jamais les options de recherche.
    assert _Spy.init_kwargs == [{"enrich_oai": True}]


def test_run_source_without_options_passes_nothing():
    _run(SpyWithKwargs)
    assert _Spy.seen == [{}]


def test_misaligned_ranks_are_discarded(caplog):
    res = _run(SpyMisaligned)
    assert res.item_count == 2 and res.native_ranks == []
    assert any("non alignés" in r.getMessage() for r in caplog.records)


# === Moisson de bout en bout (mode transmis par l'orchestrateur) ===

class FakeHAL:
    """HAL minimal : 300 documents, score décroissant avec le docid inversé."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def get(self, url, params=None, **kw):
        p = dict(params or {})
        self.calls.append(p)
        ids = list(range(1, 301))
        if p["sort"].startswith("score"):
            ids = ids[::-1]
        start = 0 if "cursorMark" in p else int(p["start"])
        if "cursorMark" in p and p["cursorMark"] != "*":
            start = int(p["cursorMark"])
        page = ids[start:start + int(p["rows"])]
        docs = [{"docid": i, "halId_s": f"hal-{i:05d}", "title_s": [f"T {i}"],
                 "producedDate_s": "2020", "docType_s": "ART",
                 "authFullName_s": ["A Jean"], "uri_s": f"https://hal.science/hal-{i:05d}",
                 "score": float(i)} for i in page]
        payload = {"response": {"numFound": 300, "docs": docs}}
        if "cursorMark" in p:
            payload["nextCursorMark"] = str(start + len(page)) if page else p["cursorMark"]
        return FakeResponse(payload)

    def close(self):
        pass

    cancel_signal = None


@pytest.mark.parametrize("mode,sort,first", [("bibliographie", "score desc,docid asc", 300),
                                             ("collecte", "docid asc", 1)])
def test_harvest_transmits_the_mode_and_keeps_native_ranks(mode, sort, first):
    fake = FakeHAL()
    request = SearchRequest(query="q", sources=["hal"], mode=mode, max_per_source=20,
                            dedup=False)
    result = orchestrator.harvest(request, RunContext(http_client=fake))
    res = result.sources[0]
    assert fake.calls[0]["sort"] == sort and fake.calls[0]["rows"] == 20
    assert res.native_sort == sort
    assert [r["rank"] for r in res.native_ranks] == list(range(1, 21))
    assert res.native_ranks[0]["score"] == float(first)
    assert res.as_dict()["native_sort"] == sort
    assert_no_rank_keys(result.items)


# === Rangs et filtre appliqué après la collecte (filtre local de langue) ===

def _ranked_result(n: int = 5) -> SourceResult:
    items = [{"title": f"t{i}"} for i in range(n)]
    res = SourceResult(source="isidore", display_name="Isidore", items=items,
                       native_sort="isidore_default",
                       native_ranks=[{"rank": i + 1, "score": None,
                                      "sort": "isidore_default"} for i in range(n)])
    res.bind_ranks()
    return res


def test_aligned_ranks_survive_a_filter_applied_to_items():
    res = _ranked_result()
    res.items = [res.items[0], res.items[3]]        # filtre appliqué à items seul
    assert [r["rank"] for r in res.aligned_ranks()] == [1, 4]
    assert [r["rank"] for r in res.ranking_dict()["ranks"]] == [1, 4]


def test_keep_items_filters_ranks_with_items():
    res = _ranked_result()
    res.keep_items([res.items[1], res.items[2], res.items[4]])
    assert [r["rank"] for r in res.native_ranks] == [2, 3, 5]
    assert len(res.native_ranks) == res.item_count == 3


def test_unknown_item_gives_no_rank_rather_than_a_wrong_one():
    res = _ranked_result()
    res.items = [res.items[0], {"title": "copie"}]
    assert res.aligned_ranks() == [] and res.ranking_dict()["ranks"] == []
    res2 = _ranked_result()
    res2.keep_items([res2.items[0], {"title": "nouvelle"}])
    assert res2.native_ranks == []


def test_harvest_local_language_filter_keeps_ranks_aligned():
    fake = FakeIsidore(400)
    for i, rec in enumerate(fake.records):
        rec["isidore"]["language"] = "en" if i % 3 == 0 else "fr"
    request = SearchRequest(query="q", sources=["isidore"], max_per_source=200,
                            dedup=False, languages=["fr"])
    result = orchestrator.harvest(request, RunContext(http_client=fake))
    res = result.sources[0]
    ranks = res.ranking_dict()["ranks"]
    assert res.item_count > 0 and len(ranks) == res.item_count
    # Rangs des notices conservées : positions des notices en français.
    expected = [i + 1 for i in range(200) if i % 3 != 0][:res.item_count]
    assert [r["rank"] for r in ranks] == expected
