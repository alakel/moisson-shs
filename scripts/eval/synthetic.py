"""
Données synthétiques du protocole d'évaluation (OPT-15) : moissons
construites sans réseau, pour les tests et pour essayer la chaîne complète
(collecte simulée, classements, fiches, métriques) sans appel aux API.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from eval import common as C  # noqa: F401 - chemins du projet
except ImportError:  # pragma: no cover
    import common as C  # type: ignore # noqa: F401

# Tri natif publié par source (valeurs réelles d'OPT-12).
NATIVE_SORTS = {"hal": "score desc,docid asc", "crossref": "score:desc",
                "isidore": "isidore_default", "semantic_scholar": "citationCount:desc"}
LABELS = {"hal": "HAL", "crossref": "CrossRef", "isidore": "Isidore",
          "semantic_scholar": "Semantic Scholar", "openedition": "OpenEdition"}


def item(title: str, *, doi: Optional[str] = None, year: str = "2020",
         last: str = "Dupont", first: str = "Jean", itype: str = "journalArticle",
         url: Optional[str] = None, abstract: str = "", source: str = "HAL",
         extra: Sequence[str] = (), language: str = "fr",
         publication: str = "Revue test") -> Dict[str, Any]:
    """Notice Zotero minimale, avec provenance dans ``extra``."""
    lines = [f"Sources: {source}"] + list(extra)
    out: Dict[str, Any] = {
        "itemType": itype, "title": title,
        "creators": [{"creatorType": "author", "firstName": first, "lastName": last}],
        "abstractNote": abstract, "date": year, "language": language,
        "url": url or "", "tags": [], "collections": [], "relations": {},
        "extra": "\n".join(lines),
    }
    if itype == "journalArticle":
        out["publicationTitle"] = publication
    if doi:
        out["DOI"] = doi
    return out


def harvest_result(query: str, per_source: Dict[str, List[Dict[str, Any]]],
                   mode: str = "bibliographie", query_variants: Optional[Dict[str, str]] = None,
                   statuses: Optional[Dict[str, str]] = None):
    """
    ``(SearchRequest, HarvestResult)`` d'une moisson simulée : les notices de
    chaque source dans l'ordre donné (rang natif = position), fusionnées par
    la déduplication du moteur.
    """
    from engine.models import HarvestResult, SearchRequest, SourceResult
    from utils.deduplication import deduplicate_with_report

    request = SearchRequest(query=query, sources=list(per_source), mode=mode,
                            max_per_source=100, cache=False, allow_metered=False,
                            query_variants=query_variants or {})
    sources = []
    all_items: List[Dict[str, Any]] = []
    for key, items in per_source.items():
        sort = NATIVE_SORTS.get(key)
        res = SourceResult(
            source=key, display_name=LABELS.get(key, key), items=list(items),
            status=(statuses or {}).get(key, "complete" if items else "empty"),
            native_sort=sort,
            native_ranks=[{"rank": i + 1, "score": None, "sort": sort}
                          for i in range(len(items))] if sort else [])
        res.bind_ranks()
        sources.append(res)
        all_items.extend(items)
    merged, report = deduplicate_with_report(copy.deepcopy(all_items))
    result = HarvestResult(run_id="20260929_000000_test", request=request, sources=sources,
                           items=merged, total_before_dedup=len(all_items),
                           dedup_report=report, duration_s=0.1,
                           query_plan={"query": query})
    return request, result


def sample_harvest(query: str = "discours numérique"):
    """
    Moisson type : HAL [a, b, c], CrossRef [d, a (même DOI), e]. Après
    fusion : a (HAL 1 + CrossRef 2), b, c, d, e.
    """
    a = item("Le discours numérique en question", doi="10.1000/a", year="2019",
             abstract="Une analyse du discours numérique natif. " * 5)
    a_cr = item("Le discours numérique en question", doi="10.1000/a", year="2019",
                source="CrossRef", extra=["DOI: 10.1000/a"])
    b = item("Pratiques numériques des étudiants", year="2021", last="Martin",
             url="https://hal.science/hal-01000001", extra=["HAL ID: hal-01000001"])
    c = item("Histoire de la presse", year="1998", last="Durand", itype="book",
             url="https://hal.science/hal-01000002", extra=["HAL ID: hal-01000002"])
    d = item("Digital discourse analysis", doi="10.1000/d", year="2022", last="Smith",
             first="Ann", source="CrossRef", language="en")
    e = item("Une thèse sur les forums", year="2015", last="Petit", itype="thesis",
             source="CrossRef", url="https://www.semanticscholar.org/paper/xyz",
             extra=["S2 Paper ID: xyz"])
    return harvest_result(query, {"hal": [a, b, c], "crossref": [d, a_cr, e]})


def query_entry(qid: str = "D01", subset: str = "decision",
                refs: Optional[List[Dict[str, Any]]] = None, query: str = "discours numérique"
                ) -> Dict[str, Any]:
    return {"id": qid, "set": subset, "query": query, "sources": "francophone",
            "categories": ["theme_large"],
            "besoin": "Besoin d'information de test : travaux sur le discours numérique, "
                      "au sens de l'analyse du discours des productions en ligne.",
            "references_connues": refs if refs is not None else [
                {"doi": "10.1000/a", "titre": "Le discours numérique en question",
                 "verification": "test"}]}


def sample_snapshot(qid: str = "D01", subset: str = "decision",
                    refs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Instantané complet (format de ``pool.py collect``) de la moisson type."""
    from eval.pool import build_snapshot

    request, result = sample_harvest()
    return build_snapshot(query_entry(qid, subset, refs), request, result,
                          http_stats={}, collected_at="2026-09-29T02:00:00",
                          environment={"keys": {"keys": False, "removed": []}})


def fixed_ranker(name: str, order_titles: Sequence[str]):
    """Classeur externe qui range les notices dans l'ordre des titres donnés."""
    from eval.rankers import Ranker

    def run(items, request, harvest):
        pos = {t: i for i, t in enumerate(order_titles)}
        return sorted(range(len(items)), key=lambda i: pos.get(items[i]["title"], 10 ** 6))

    return Ranker(name=name, kind="externe", func=run, version="test")


def pool_for(snapshot: Dict[str, Any], rankers: Sequence[Any], depth: int = 30
             ) -> Dict[str, Any]:
    from eval.pool import build_pool
    return build_pool(snapshot, rankers, depth)


def title_to_cid(pool: Dict[str, Any]) -> Dict[str, str]:
    return {cand["item"]["title"]: cid for cid, cand in pool["candidates"].items()}


def grades_by_title(pool: Dict[str, Any], grades: Dict[str, int]) -> Dict[Tuple[str, str], int]:
    t2c = title_to_cid(pool)
    return {(pool["qid"], t2c[t]): g for t, g in grades.items()}
