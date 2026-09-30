# -*- coding: utf-8 -*-
"""
OPT-10 : search_by_author interroge HAL, Isidore, OpenAlex et Semantic
Scholar en parallèle (pool borné) et fusionne dans l'ordre séquentiel
historique, quel que soit l'ordre d'arrivée. Les sources sont simulées au
niveau des connecteurs et des fonctions de recherche d'auteur par source :
la sélection des candidats et le contrôle d'identité d'OPT-14, logés dans ces
fonctions, sont testés dans ``test_opt14_author.py``. Aucun réseau.
"""

import copy
import time

import pytest

import utils.lookups as lookups


def _item(title, doi=None, year="2020", source="HAL", abstract=""):
    item = {"itemType": "journalArticle", "title": title, "date": year,
            "creators": [{"creatorType": "author", "firstName": "Anne",
                          "lastName": "Dupont"}],
            "extra": f"Sources: {source}", "tags": [], "collections": [],
            "relations": {}, "abstractNote": abstract}
    if doi:
        item["DOI"] = doi
    return item


# Un même travail (10.1/a) vu par HAL et par OpenAlex, avec des titres
# différents : la notice fusionnée dépend de l'ordre de fusion (HAL d'abord).
HAL_ITEMS = [_item("Travail A (HAL)", "10.1/a", "2020", "HAL"),
             _item("Travail D", "10.1/d", "2018", "HAL")]
ISIDORE_ITEMS = [_item("Travail B", None, "2019", "Isidore")]
OPENALEX_ITEMS = [_item("Travail A (OpenAlex)", "10.1/a", "2020", "OpenAlex",
                        abstract="Résumé OpenAlex")]
S2_ITEMS = [_item("Travail C", "10.1/c", "2021", "Semantic Scholar")]


def _install(monkeypatch, latency, calls=None, fail=()):
    """Remplace les quatre sources par des doublures à latence réelle."""
    calls = calls if calls is not None else []

    def _make_connector(label, items):
        class _Fake:
            def __init__(self, http_client=None, **kwargs):
                # **kwargs : signature uniforme des connecteurs (scan_registry
                # passé au connecteur Isidore).
                self.http = http_client

            def search(self, query, max_results=1000, **kw):
                calls.append((label, query, max_results))
                time.sleep(latency.get(label, 0.0))
                if label in fail:
                    raise RuntimeError("panne simulée")
                return copy.deepcopy(items)
        return _Fake

    def _openalex(name, max_per_source, orcid=None, http_client=None):
        calls.append(("OpenAlex", name, max_per_source))
        time.sleep(latency.get("OpenAlex", 0.0))
        return copy.deepcopy(OPENALEX_ITEMS)

    def _s2(name, max_per_source, http_client=None):
        calls.append(("S2", name, max_per_source))
        time.sleep(latency.get("S2", 0.0))
        return copy.deepcopy(S2_ITEMS)

    monkeypatch.setattr(lookups, "HALConnector",
                        _make_connector("HAL", HAL_ITEMS))
    monkeypatch.setattr(lookups, "IsidoreConnector",
                        _make_connector("Isidore", ISIDORE_ITEMS))
    monkeypatch.setattr(lookups, "_search_openalex_author", _openalex)
    monkeypatch.setattr(lookups, "_search_s2_author", _s2)
    return calls


class _Client:
    def close(self):
        pass


def test_sources_are_queried_in_parallel(monkeypatch):
    _install(monkeypatch, {"HAL": 0.25, "Isidore": 0.25, "OpenAlex": 0.25,
                           "S2": 0.25})
    t0 = time.monotonic()
    items = lookups.search_by_author("Anne Dupont", http_client=_Client())
    elapsed = time.monotonic() - t0
    assert len(items) == 4
    # Somme : 1,0 s ; maximum : 0,25 s.
    assert elapsed < 0.6, elapsed


@pytest.mark.parametrize("latency", [
    {"HAL": 0.3, "Isidore": 0.2, "OpenAlex": 0.1, "S2": 0.0},  # ordre inversé
    {"HAL": 0.0, "Isidore": 0.1, "OpenAlex": 0.2, "S2": 0.3},
    {"HAL": 0.15, "Isidore": 0.0, "OpenAlex": 0.3, "S2": 0.05},
])
def test_merge_order_is_the_sequential_order(monkeypatch, latency):
    _install(monkeypatch, {})
    sequential = lookups.search_by_author("Anne Dupont", http_client=_Client(),
                                          max_workers=1)
    _install(monkeypatch, latency)
    parallel = lookups.search_by_author("Anne Dupont", http_client=_Client())
    assert parallel == sequential
    # HAL est fusionné en premier : sa valeur de titre gagne ; OpenAlex
    # complète le résumé ; tri par année décroissante inchangé.
    merged_a = next(it for it in parallel if it.get("DOI") == "10.1/a")
    assert merged_a["title"] == "Travail A (HAL)"
    assert merged_a["abstractNote"] == "Résumé OpenAlex"
    assert [it["date"] for it in parallel] == ["2021", "2020", "2019", "2018"]


def test_selection_logic_is_unchanged(monkeypatch):
    """
    Requêtes et plafonds transmis aux fonctions de recherche par source,
    inchangés par le parallélisme (ces fonctions, qui portent la sélection
    des candidats d'OPT-14, sont doublées ici).
    """
    calls = _install(monkeypatch, {})
    lookups.search_by_author("Anne Dupont", idhal="anne-dupont",
                             max_per_source=50, http_client=_Client())
    assert sorted(calls) == sorted([
        ("HAL", 'authIdHal_s:"anne-dupont"', 50),
        ("Isidore", '"Anne Dupont"', 50),
        ("OpenAlex", "Anne Dupont", 50),
        ("S2", "Anne Dupont", 50),
    ])


def test_one_failing_source_does_not_stop_the_others(monkeypatch):
    _install(monkeypatch, {}, fail=("HAL",))
    items = lookups.search_by_author("Anne Dupont", http_client=_Client())
    titles = {it["title"] for it in items}
    assert titles == {"Travail B", "Travail A (OpenAlex)", "Travail C"}


def test_no_name_orcid_queries_hal_and_openalex(monkeypatch):
    """
    Sans nom, un ORCID interroge HAL et OpenAlex (par identifiant), jamais
    Isidore ni Semantic Scholar (recherche sur le nom). Vérification finale
    du lot C : l'argument orcid promet « HAL et OpenAlex par identifiant »,
    et l'outil MCP accepte un nom vide accompagné d'un identifiant.
    """
    calls = _install(monkeypatch, {})
    lookups.search_by_author("", orcid="0000-0002-1234-5678",
                             http_client=_Client())
    assert sorted(c[0] for c in calls) == ["HAL", "OpenAlex"]
    assert ("OpenAlex", "", 200) in calls


def test_mcp_tool_passes_the_shared_client(monkeypatch, tmp_path):
    import asyncio
    import mcp_server
    seen = {}

    def fake(name, idhal=None, orcid=None, max_per_source=200,
             http_client=None, **kw):
        seen["client"] = http_client
        return []

    monkeypatch.setattr(mcp_server, "search_by_author", fake)
    out = asyncio.run(mcp_server._tool_search_by_author(
        {"name": "Anne Dupont", "save_corpus": False}))
    assert isinstance(out, str) and "Items uniques : 0" in out
    from utils.http import get_default_client
    assert seen["client"] is get_default_client()
