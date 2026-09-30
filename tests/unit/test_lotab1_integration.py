# -*- coding: utf-8 -*-
"""
Intégration des lots A et B1 (OPT-06, OPT-08, OPT-10). Aucun accès réseau.

Couvre les raccords entre tâches :

- OPT-08 × OPT-06 : une notice retenue à la fois par la source Isidore et par
  une sous-source (Cairn, Persée) du flux Isidore partagé fusionne en une
  seule notice ; la ligne ``Sources`` est agrégée, la preuve de fusion
  (``Dedup evidence``) cite la clé Isidore ; la sortie ne dépend pas de
  l'ordre des consommateurs et une re-déduplication est idempotente ; les
  statistiques par source comptent chaque source une fois par notice ;
- OPT-10 × OPT-06 : le résolveur de DOI, la déduplication et les
  statistiques lisent le DOI d'une notice de la même façon (extracteur commun
  ``utils.identity.extract_doi``, sans repli local) ;
- OPT-10 × OPT-08 : ``search_by_author`` reçoit désormais le client HTTP
  partagé de longue durée du serveur MCP ; le flux Isidore d'un appel (et une
  erreur Isidore) n'est pas mémorisé pour l'appel suivant.
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from test_opt08_isidore_scan import FakeIsidore, make_record, universe  # noqa: E402

import utils.lookups as lookups  # noqa: E402
from connectors.cairn import CairnConnector  # noqa: E402
from connectors.isidore import IsidoreConnector  # noqa: E402
from connectors.persee import PerseeConnector  # noqa: E402
from utils import doi_resolver  # noqa: E402
from utils.deduplication import (  # noqa: E402
    deduplicate, deduplicate_with_report, get_doi, item_sources, parse_evidence,
)
from utils.identity import extract_doi  # noqa: E402


# === OPT-08 × OPT-06 : sous-sources Isidore et déduplication ===

def _harvest(n=60, iso_cap=50, cap=50):
    """Source Isidore et deux sous-sources sur un même flux (un client)."""
    fake = FakeIsidore(universe(n))
    iso = IsidoreConnector(http_client=fake).search("q", max_results=iso_cap)
    cairn = CairnConnector(http_client=fake).search("q", max_results=cap)
    persee = PerseeConnector(http_client=fake, enrich_oai=False).search(
        "q", max_results=cap)
    return fake, iso, cairn, persee


def _uri(item):
    for line in (item.get("extra") or "").split("\n"):
        if line.startswith("Isidore URI:"):
            return line.split(":", 1)[1].strip()
    return None


def test_proxy_and_direct_isidore_notices_merge_with_aggregated_sources():
    fake, iso, cairn, persee = _harvest()
    # Flux partagé : une seule page téléchargée pour les trois consommateurs.
    assert fake.calls == [1]
    assert cairn and persee
    # Les sous-sources gardent Isidore comme source intermédiaire.
    for it in cairn:
        assert item_sources(it) == {"cairn", "isidore"}

    iso_uris = {_uri(it) for it in iso}
    items, report = deduplicate_with_report(iso + cairn + persee)

    by_uri = {}
    for it in items:
        by_uri.setdefault(_uri(it), []).append(it)
    # Aucune notice en double après fusion.
    assert all(len(v) == 1 for v in by_uri.values())
    assert len(items) == len(iso_uris | {_uri(i) for i in cairn + persee})

    for proxy_items, key, label in ((cairn, "cairn", "Cairn"),
                                    (persee, "persee", "Persée")):
        for p in proxy_items:
            merged = by_uri[_uri(p)][0]
            assert item_sources(merged) == {key, "isidore"}
            sources_line = [ln for ln in merged["extra"].split("\n")
                            if ln.startswith("Sources:")]
            assert len(sources_line) == 1
            ev = parse_evidence(merged)
            if _uri(p) in iso_uris:
                # Notice vue par Isidore direct et par la sous-source : ligne
                # Sources agrégée par la fusion (libellés triés).
                assert sources_line == [
                    f"Sources: {', '.join(sorted([label, 'Isidore']))}"]
                assert ev is not None and ev["count"] == 2
                assert any(k.startswith("isidore:") for k in ev["keys"])
                # Aucune ligne dupliquée par la fusion.
                assert merged["extra"].count("Isidore URI:") == 1
                assert merged["extra"].count("Full Text Available:") <= 1
            else:
                # Au-delà du plafond de la source directe : notice seule,
                # sans preuve de fusion (comportement historique).
                assert ev is None or ev["count"] == 1

    assert report["strong_merges"] == len(
        {_uri(p) for p in cairn + persee} & iso_uris)
    assert report["fuzzy_merges"] == 0 and report["ambiguous"] == 0


def test_proxy_merge_is_order_independent_and_idempotent():
    _, iso, cairn, persee = _harvest()
    reference = deduplicate(copy.deepcopy(iso + cairn + persee))
    for order in ((persee, cairn, iso), (cairn, iso, persee),
                  (list(reversed(iso)), persee, list(reversed(cairn)))):
        batch = [it for group in order for it in copy.deepcopy(group)]
        assert deduplicate(batch) == reference
    assert deduplicate(copy.deepcopy(reference)) == reference


def test_stats_count_each_source_once_per_merged_notice():
    from main import compute_stats
    _, iso, cairn, persee = _harvest()
    items = deduplicate(iso + cairn + persee)
    stats = compute_stats(items)
    assert stats["by_source_key"]["cairn"] == len(cairn)
    assert stats["by_source_key"]["persee"] == len(persee)
    # Isidore : notices directes + notices de sous-source hors plafond direct
    # (Isidore reste cité comme source intermédiaire).
    assert stats["by_source_key"]["isidore"] == len(items)
    assert stats["source_overlap"]["cairn + isidore"] == len(cairn)
    assert stats["merged_items"] == sum(
        1 for it in items if (parse_evidence(it) or {}).get("count", 1) > 1)


def test_proxy_notice_with_doi_merges_with_a_crossref_notice():
    """Une notice Cairn du flux Isidore (DOI lisible dans l'URL doi.org)
    rejoint la notice CrossRef de même DOI ; le résumé CrossRef est
    prioritaire."""
    rec = make_record(1, host="www.cairn.info", year=2020)
    rec["isidore"]["url"] = "https://doi.org/10.3917/test.020.0001"
    rec["isidore"]["ore"]["aggregates"] = [
        {"$": "https://doi.org/10.3917/test.020.0001"},
        {"$": "https://www.cairn.info/revue-test-2020-1-page-1.htm"}]
    fake = FakeIsidore([rec])
    cairn = CairnConnector(http_client=fake).search("q", max_results=10)
    assert len(cairn) == 1
    crossref = {
        "itemType": "journalArticle", "title": "Titre de la notice 1",
        "creators": [{"creatorType": "author", "firstName": "Jean",
                      "lastName": "Dupont"}],
        "date": "2020", "DOI": "10.3917/test.020.0001",
        "abstractNote": "Résumé CrossRef", "extra": "Sources: CrossRef",
        "tags": [], "collections": [], "relations": {},
    }
    merged = deduplicate(cairn + [crossref])
    assert len(merged) == 1
    assert item_sources(merged[0]) == {"cairn", "isidore", "crossref"}
    assert merged[0]["abstractNote"] == "Résumé CrossRef"
    assert get_doi(merged[0]) == "10.3917/test.020.0001"


# === OPT-10 × OPT-06 : une seule lecture du DOI ===

_DOI_CASES = [
    {"itemType": "journalArticle", "title": "a", "DOI": "https://doi.org/10.1/A"},
    {"itemType": "book", "title": "b", "extra": "Sources: HAL\nDOI: 10.2/B"},
    {"itemType": "journalArticle", "title": "c",
     "extra": "DOI source: appariement titre 0.95 10.3/faux"},
    {"itemType": "journalArticle", "title": "d",
     "url": "https://doi.org/10.4/url-seulement"},
    {"itemType": "journalArticle", "title": "e", "DOI": "pas un doi"},
    {"itemType": "thesis", "title": "f", "extra": "DOI: doi:10.5/F."},
]


@pytest.mark.parametrize("item", _DOI_CASES, ids=lambda i: i["title"])
def test_resolver_dedup_and_stats_read_the_same_doi(item):
    expected = extract_doi(item)
    assert doi_resolver.item_doi(item) == (expected or "")
    assert get_doi(item) == expected


def test_resolver_skip_count_matches_stats_with_doi(monkeypatch):
    """Les notices que le résolveur tient pour pourvues d'un DOI sont
    exactement celles que les statistiques comptent « avec DOI »."""
    from main import compute_stats
    items = copy.deepcopy(_DOI_CASES)
    for it in items:
        it.setdefault("creators", [])
        it.setdefault("date", "2020")
    stats = compute_stats(items)
    assert stats["with_doi"] == sum(
        1 for it in items if doi_resolver.item_doi(it)) == 3
    assert not hasattr(doi_resolver, "_local_extract_doi")


# === OPT-10 × OPT-08 : client partagé de longue durée ===

def _only_isidore(monkeypatch):
    class _NoHal:
        def __init__(self, http_client=None, **kwargs):
            pass

        def search(self, query, max_results=1000, **kwargs):
            return []

    monkeypatch.setattr(lookups, "HALConnector", _NoHal)
    monkeypatch.setattr(lookups, "_search_openalex_author",
                        lambda *a, **k: None)
    monkeypatch.setattr(lookups, "_search_s2_author", lambda *a, **k: None)


def test_search_by_author_does_not_reuse_a_flow_across_calls(monkeypatch):
    _only_isidore(monkeypatch)
    shared = FakeIsidore([make_record(i, host="hal.science") for i in range(5)])
    first = lookups.search_by_author("Jean Dupont", http_client=shared)
    second = lookups.search_by_author("Jean Dupont", http_client=shared)
    assert len(first) == len(second) == 5
    # Chaque appel relit Isidore (résultats frais, comme avant le chantier,
    # où chaque appel créait son propre client).
    assert shared.calls == [1, 1]


def test_search_by_author_does_not_memoize_an_isidore_error(monkeypatch):
    _only_isidore(monkeypatch)
    shared = FakeIsidore([make_record(i, host="hal.science") for i in range(5)],
                         faults={1: ["error"]})
    first = lookups.search_by_author("Jean Dupont", http_client=shared)
    assert first == []
    # Isidore rétabli : l'appel suivant, sur le même client, le réinterroge.
    second = lookups.search_by_author("Jean Dupont", http_client=shared)
    assert len(second) == 5
    assert shared.calls == [1, 1]
