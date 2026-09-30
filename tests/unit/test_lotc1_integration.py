"""
Lot C1, intégration : OPT-11 (plan de requête) × OPT-12 (classement à la
source) × OPT-14 (recherche d'auteur).

Sans aucun accès réseau (client HTTP de substitution aiguillé par hôte) :

- mappeur Isidore : la langue est lue dans ``dc:language``, le champ que
  portent les notices de l'API (sondes du 2026-09-29) ; une liste garde toutes
  ses langues. Sans cette lecture, le filtre local de langue d'OPT-11 ne
  voyait aucune langue et conservait toutes les notices d'Isidore et de ses
  sous-sources ;
- moisson de bout en bout avec les vrais connecteurs HAL, Isidore,
  OpenEdition, CrossRef et OpenAlex : requête propre à chaque source (plan
  d'OPT-11), mode et langues transmis par ``run_source`` (OPT-12), filtre
  natif (HAL, OpenAlex) ou local (famille Isidore), aucun filtre pour
  CrossRef, rangs natifs alignés sur les notices après le filtre local
  (``keep_items``), aucune clé de rang dans les notices (invariant 1) ;
- serveur MCP : neuf outils, arguments historiques conservés, ajouts
  d'OPT-11 et d'OPT-14 présents dans les schémas.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List
from urllib.parse import urlparse

import pytest

from connectors import openalex as openalex_module
from engine import orchestrator
from engine.models import RunContext, SearchRequest, SourceResult
from mappers.isidore_mapper import _extract_language, map_isidore_to_zotero
from utils.query_plan import apply_local_language_filter, item_languages

RANK_KEYS = {"score", "rank", "native_rank", "native_ranks", "relevance_score",
             "native_sort"}


# === Mappeur Isidore : dc:language ===

@pytest.mark.parametrize("iso,expected", [
    ({"dc:language": "fr"}, "fr"),
    ({"dc:language": "fre"}, "fre"),
    ({"dc:language": ["fr", "en"]}, "fr, en"),
    ({"dc:language": ["ENG", "FRA"]}, "ENG, FRA"),
    ({"dc:language": ["fr", "", "fr", None, "en"]}, "fr, en"),
    ({"dc:language": {"$": "fr"}}, "fr"),
    ({"dc:language": [{"$": "en"}, {"$": "fr"}]}, "en, fr"),
    ({"language": "fr"}, "fr"),                    # forme historique
    ({"lang": "en"}, "en"),
    ({"dc:language": "en", "language": "fr"}, "en"),   # champ de l'API d'abord
    ({"dc:language": "", "language": "fr"}, "fr"),
    ({}, ""),
    ({"dc:language": []}, ""),
])
def test_isidore_language_is_read_from_dc_language(iso, expected):
    assert _extract_language(iso) == expected


def test_bilingual_isidore_notice_is_not_dropped_by_the_local_filter():
    """Une notice ``["ENG", "FRA"]`` reste lisible comme française."""
    item = map_isidore_to_zotero({"@uri": "x", "isidore": {
        "title": "Titre", "dc:language": ["ENG", "FRA"]}})
    assert item["language"] == "ENG, FRA"
    assert item_languages(item["language"]) == ["en", "fr"]
    kept, stats = apply_local_language_filter([item], ["fr"])
    assert kept == [item] and stats["removed"] == 0


def test_real_isidore_fixture_carries_its_language(isidore_first_item):
    """La notice réelle capturée porte ``dc:language`` : elle est lue."""
    assert isidore_first_item["isidore"].get("dc:language") == "fr"
    assert "language" not in isidore_first_item["isidore"]
    assert map_isidore_to_zotero(isidore_first_item)["language"] == "fr"


def test_missing_language_leaves_the_field_absent():
    item = map_isidore_to_zotero({"@uri": "x", "isidore": {"title": "T"}})
    assert not item.get("language")


# === Moisson de bout en bout : plan × classement × filtre de langue ===

class FakeResponse:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.from_cache = False
        self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        return self._payload


ISIDORE_HOSTS = ["journals.openedition.org", "www.example.org"]


def isidore_record(i: int) -> Dict[str, Any]:
    """Notice Isidore au format de l'API (langue dans ``dc:language``)."""
    url = f"https://{ISIDORE_HOSTS[i % 2]}/article/w{i}"
    iso = {
        "title": [{"@xml:lang": "fr", "$": f"Discours numérique {i}"}],
        "url": url,
        "enrichedCreators": {"creator": {"firstname": "Jean",
                                         "lastname": f"Dupont{i}"}},
        "date": {"normalizedDate": "2020-01-01", "year": "2020"},
        "types": {"type": ["article"]},
        "ore": {"aggregates": [{"$": url}]},
    }
    # Positions 0, 3, 6… : anglais ; 1, 4, 7… : français ; 2, 5, 8… : sans
    # langue, sauf 5 : bilingue.
    if i % 3 == 0:
        iso["dc:language"] = "en"
    elif i % 3 == 1:
        iso["dc:language"] = "fre"
    elif i == 5:
        iso["dc:language"] = ["ENG", "FRA"]
    return {"@uri": f"10670/1.w{i}", "isidore": iso}


class RoutingClient:
    """Client HTTP de substitution, aiguillé par hôte ; appels notés."""

    cancel_signal = None
    session = object()

    def __init__(self, n_isidore: int = 30, n_hal: int = 40):
        self.calls: List[tuple] = []
        self.isidore = [isidore_record(i) for i in range(n_isidore)]
        self.n_hal = n_hal

    def get(self, url, params=None, **kw):
        p = dict(params or {})
        host = urlparse(url).netloc
        self.calls.append((host, p))
        if host == "api.archives-ouvertes.fr":
            return FakeResponse(self._hal(p))
        if host == "api.isidore.science":
            return FakeResponse(self._isidore(p))
        if host == "api.crossref.org":
            return FakeResponse(self._crossref(p))
        raise AssertionError(f"hôte inattendu : {url}")

    def close(self):
        pass

    def calls_to(self, host: str) -> List[Dict[str, Any]]:
        return [p for h, p in self.calls if h == host]

    def _hal(self, p):
        ids = list(range(1, self.n_hal + 1))
        if str(p.get("sort", "")).startswith("score"):
            ids = ids[::-1]
        start = int(p.get("start") or 0)
        if "cursorMark" in p and p["cursorMark"] != "*":
            start = int(p["cursorMark"])
        page = ids[start:start + int(p["rows"])]
        docs = [{"docid": i, "halId_s": f"hal-{i:05d}",
                 "title_s": [f"Titre HAL {i}"], "producedDate_s": "2020",
                 "docType_s": "ART", "authFullName_s": [f"Jean Martin{i}"],
                 "uri_s": f"https://hal.science/hal-{i:05d}",
                 "language_s": ["fr"], "score": float(i)} for i in page]
        payload = {"response": {"numFound": self.n_hal, "docs": docs}}
        if "cursorMark" in p:
            payload["nextCursorMark"] = (str(start + len(page)) if page
                                         else p["cursorMark"])
        return payload

    def _isidore(self, p):
        replies = min(int(p["replies"]), 200)
        chunk = self.isidore[:replies]
        return {"response": {"replies": {
            "meta": {"@items": str(len(self.isidore)),
                     "@pageItems": str(len(chunk))},
            "content": {"reply": chunk}}}}

    def _crossref(self, p):
        start = int(p["cursor"]) if p["cursor"] != "*" else 0
        total = 12
        n = max(0, min(int(p["rows"]), total - start))
        items = [{"DOI": f"10.1/cr{start + k}",
                  "title": [f"Digital discourse {start + k}"],
                  "type": "journal-article",
                  "issued": {"date-parts": [[2021]]},
                  "author": [{"family": f"Smith{start + k}", "given": "Ann"}],
                  "score": 30.0 - (start + k)} for k in range(n)]
        msg = {"items": items, "total-results": total}
        if start + n < total:
            msg["next-cursor"] = str(start + n)
        return {"message": msg}


def _openalex_fake(monkeypatch):
    calls: List[Dict[str, Any]] = []

    def get_json(url, params=None, **kw):
        p = dict(params or {})
        calls.append(p)
        results = [{"id": f"https://openalex.org/W{k}",
                    "display_name": f"Digital discourse work {k}",
                    "title": f"Digital discourse work {k}",
                    "publication_year": 2022, "type": "article",
                    "language": "fr", "authorships": [],
                    "relevance_score": 500.0 - k} for k in range(8)]
        return {"meta": {"count": 8, "next_cursor": None}, "results": results}

    monkeypatch.setattr(openalex_module, "get_json", get_json)
    monkeypatch.setenv("OPENALEX_API_KEY", "CLE-FACTICE")
    return calls


SOURCES = ["hal", "isidore", "openedition", "crossref", "openalex"]
QUERY = '"discours numérique" NOT politique'
VARIANT_EN = '"digital discourse" NOT politics'


def _harvest(monkeypatch, mode: str = "bibliographie", languages=("fr",)):
    oa_calls = _openalex_fake(monkeypatch)
    fake = RoutingClient()
    request = SearchRequest(query=QUERY, sources=list(SOURCES), mode=mode,
                            max_per_source=20, dedup=False,
                            query_variants={"en": VARIANT_EN},
                            languages=list(languages))
    result = orchestrator.harvest(request, RunContext(http_client=fake))
    return result, fake, oa_calls


def _by_source(result) -> Dict[str, SourceResult]:
    return {r.source: r for r in result.sources}


def test_each_source_receives_its_own_query_mode_and_language_filter(monkeypatch):
    result, fake, oa_calls = _harvest(monkeypatch)
    res = _by_source(result)
    assert all(r.status in ("complete", "partial") for r in res.values()), \
        {k: (r.status, r.reason, r.detail) for k, r in res.items()}

    # HAL : syntaxe Solr, tri par score (bibliographie), filtre natif.
    hal = fake.calls_to("api.archives-ouvertes.fr")
    assert hal[0]["q"] == QUERY
    assert hal[0]["sort"].startswith("score desc")
    fq = hal[0].get("fq")
    fq = fq if isinstance(fq, list) else [fq]
    assert any("language_s:(fr)" in str(f) for f in fq)
    assert res["hal"].native_filters.get("languages") == ["fr"]

    # Isidore et OpenEdition : UNE requête partagée, syntaxe ET/OU/SAUF, sans
    # filtre natif de langue.
    isi = fake.calls_to("api.isidore.science")
    assert len(isi) == 1 and isi[0]["replies"] == 200
    assert isi[0]["q"] == '"discours numérique" SAUF politique'
    assert not any("lang" in k for k in isi[0])

    # CrossRef : variante anglaise en sac de mots, exclusion retirée, tri par
    # score, aucun filtre de langue.
    cr = fake.calls_to("api.crossref.org")
    assert cr[0]["query.bibliographic"] == "digital discourse"
    assert cr[0]["sort"] == "score" and cr[0]["order"] == "desc"
    assert "language" not in str(cr[0].get("filter", ""))
    assert res["crossref"].native_filters == {}

    # OpenAlex : variante anglaise avec opérateurs, filtre natif de langue.
    assert oa_calls[0]["search"] == VARIANT_EN
    assert "language:fr" in oa_calls[0]["filter"]
    assert res["openalex"].native_filters.get("languages") == ["fr"]


def test_local_language_filter_reads_dc_language_and_keeps_ranks_aligned(monkeypatch):
    result, _, _ = _harvest(monkeypatch)
    isidore = _by_source(result)["isidore"]
    # 20 premières notices de la fenêtre : 7 en anglais (0, 3, …, 18)
    # écartées ; 7 françaises, la bilingue (5) et 5 sans langue (2, 8, 11,
    # 14, 17) conservées.
    assert isidore.counters["language_filter_removed"] == 7
    assert isidore.counters["language_filter_unknown_kept"] == 5
    assert isidore.item_count == 13
    expected = [i + 1 for i in range(20) if i % 3 != 0]
    # Champ brut aligné (keep_items), rangs servis au manifeste identiques.
    assert [r["rank"] for r in isidore.native_ranks] == expected
    assert [r["rank"] for r in isidore.ranking_dict()["ranks"]] == expected
    assert len(isidore.native_ranks) == isidore.item_count
    bilingual = [it for it in isidore.items if it.get("language") == "ENG, FRA"]
    assert len(bilingual) == 1

    # Sous-source OpenEdition : même flux, même filtre.
    oe = _by_source(result)["openedition"]
    assert oe.counters["language_filter_removed"] > 0
    assert len(oe.native_ranks) == oe.item_count > 0
    assert all(item_languages(it.get("language")) in ([], ["fr"], ["en", "fr"])
               for it in oe.items)

    # Le plan sérialisé rend compte de ce qui a été appliqué.
    applied = {e["source"]: e["applied"] for e in result.query_plan["sources"]
               if "applied" in e}
    assert applied["isidore"]["language_filter"] == {
        "languages": ["fr"], "kept": 13, "removed": 7, "unknown_kept": 5}
    assert set(applied) == {"isidore", "openedition"}


def test_native_ranks_present_for_ranking_sources_and_never_in_notices(monkeypatch):
    result, _, _ = _harvest(monkeypatch)
    res = _by_source(result)
    assert res["hal"].native_sort == "score desc,docid asc"
    assert [r["score"] for r in res["hal"].native_ranks[:2]] == [40.0, 39.0]
    assert res["crossref"].native_sort == "score:desc"
    assert res["crossref"].native_ranks[0]["score"] == 30.0
    assert res["openalex"].native_sort == "relevance_score:desc"
    assert res["openalex"].native_ranks[0]["score"] == 500.0
    for r in res.values():
        assert len(r.native_ranks) == r.item_count
        assert "native_ranks" not in r.as_dict()
    for item in result.items:
        assert not RANK_KEYS & set(item), sorted(RANK_KEYS & set(item))
        extra = item.get("extra", "").lower()
        assert "relevance" not in extra and "native" not in extra


def test_collecte_mode_keeps_the_historical_orders(monkeypatch):
    result, fake, _ = _harvest(monkeypatch, mode="collecte", languages=())
    hal = fake.calls_to("api.archives-ouvertes.fr")
    assert hal[0]["sort"] == "docid asc" and "cursorMark" in hal[0]
    cr = fake.calls_to("api.crossref.org")
    assert "sort" not in cr[0]
    res = _by_source(result)
    # Sans langue demandée : aucun filtre local, aucun compteur.
    assert "language_filter_removed" not in res["isidore"].counters
    assert res["isidore"].item_count == 20
    assert not any("applied" in e for e in result.query_plan["sources"])


# === Serveur MCP : outils et arguments ===

HISTORICAL_ARGS = {
    "moisson_search": {"query", "sources", "max_per_source", "enrich_oai",
                       "dedup", "from_date", "to_date", "cache", "workers"},
    "search_by_author": {"name", "idhal", "orcid", "max_per_source",
                         "save_corpus"},
}


def test_nine_tools_with_historical_and_new_arguments():
    import mcp_server

    tools = asyncio.run(mcp_server.list_tools())
    names = [t.name for t in tools]
    # Neuf outils historiques toujours présents, sans doublon ; le lot D en
    # ajoute (corpus_export, OPT-18…) : présence vérifiée, pas un nombre figé.
    assert {"moisson_search", "corpus_analyze", "corpus_list", "corpus_filter",
            "corpus_resolve_dois", "corpus_to_csl", "fetch_by_doi",
            "search_by_author", "corpus_merge"} <= set(names)
    assert "corpus_export" in names
    assert len(names) == len(set(names))
    props = {t.name: set(t.inputSchema.get("properties", {})) for t in tools}
    for tool, args in HISTORICAL_ARGS.items():
        assert args <= props[tool], args - props[tool]
    assert {"mode", "query_variants", "languages", "plan_only"} <= props["moisson_search"]
    assert {"openalex_author_id", "s2_author_id", "hal_person_id"} <= \
        props["search_by_author"]
