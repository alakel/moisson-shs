"""
OPT-12 : classement de HAL à la source (constat C25).

Sans aucun accès réseau : doublure sémantique de l'API Solr de HAL
(``FakeSolr``) dont le score est décorrélé du ``docid``, ce qui rend visible
la différence entre les deux modes :

- ``bibliographie`` : ``sort=score desc,docid asc``, pagination
  ``start``/``rows`` bornée à ``HAL_RANKED_MAX_RESULTS`` ; quand
  ``numFound > max_results``, les notices retenues sont celles de meilleur
  score ; un document revenu sur une page suivante (réplicas aux scores
  différents) est écarté ;
- ``collecte`` (et sans mode : comportement historique) : ``cursorMark`` +
  ``docid asc``, ordre déterministe ;
- dans les deux modes : ``rows = min(1000, reste à collecter)`` à chaque lot
  (revue OPT-12 : le dernier lot aussi), ``score`` demandé
  dans ``fl``, rang et score natifs dans le compte rendu (alignés sur les
  notices), jamais dans les notices Zotero (invariant 1) ; filtre de langue
  natif ``language_s``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

import config
from connectors import hal as hal_module
from connectors.hal import HALConnector, SORT_BIBLIOGRAPHIE, SORT_COLLECTE


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.from_cache = False
        self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        return self._payload


def solr_score(docid: int) -> float:
    """Score pseudo-aléatoire, décorrélé du docid (reproductible)."""
    return round(((docid * 2654435761) % 1000003) / 1000.0, 4)


def hal_doc(docid: int, lang: str = "fr") -> Dict[str, Any]:
    return {"docid": docid, "halId_s": f"hal-{docid:05d}",
            "title_s": [f"Titre HAL {docid}"], "producedDate_s": "2020",
            "docType_s": "ART", "authFullName_s": [f"Auteur{docid} Jean"],
            "uri_s": f"https://hal.science/hal-{docid:05d}", "language_s": [lang]}


class FakeSolr:
    """
    Doublure de ``/search/`` : tri (première clause ``score`` ou ``docid``),
    ``start``/``rows``, ``cursorMark`` (exige ``docid`` dans le tri, comme
    Solr), ``fq`` de langue, ``fl`` (score renvoyé seulement s'il est
    demandé). ``replica_shift`` : décale d'un rang les pages suivantes, comme
    deux réplicas aux scores légèrement différents (un document revient).
    """

    def __init__(self, n: int = 5000, langs=("fr", "en", "fr"),
                 replica_shift: bool = False):
        self.docs = [hal_doc(i, langs[i % len(langs)]) for i in range(1, n + 1)]
        self.replica_shift = replica_shift
        self.calls: List[Dict[str, Any]] = []

    def get(self, url, params=None, **kw):
        p = dict(params or {})
        self.calls.append(p)
        docs = self.docs
        fqs = p.get("fq") or []
        fqs = [fqs] if isinstance(fqs, str) else list(fqs)
        for fq in fqs:
            if fq.startswith("language_s:"):
                wanted = fq.split("(", 1)[1].rstrip(")").split(" OR ")
                docs = [d for d in docs if d["language_s"][0] in wanted]
        sort = p.get("sort", "score desc")
        primary = sort.split(",")[0].strip()
        if primary.startswith("score"):
            ordered = sorted(docs, key=lambda d: (-solr_score(d["docid"]), d["docid"]))
        else:
            ordered = sorted(docs, key=lambda d: d["docid"])
        rows = int(p["rows"])
        cursor = p.get("cursorMark")
        if cursor is not None:
            assert "docid" in sort, "cursorMark exige un tri sur la clé unique"
            start = 0 if cursor == "*" else int(cursor[1:])
        else:
            start = int(p.get("start", 0))
            if self.replica_shift and start > 0:
                start -= 1   # le dernier document de la page précédente revient
        page = ordered[start:start + rows]
        want_score = "score" in (p.get("fl") or "").split(",")
        out = [dict(d, score=solr_score(d["docid"])) if want_score else dict(d)
               for d in page]
        payload: Dict[str, Any] = {"response": {"numFound": len(ordered), "docs": out}}
        if cursor is not None:
            payload["nextCursorMark"] = f"c{start + len(page)}" if page else cursor
        return FakeResponse(payload)

    def close(self):
        pass


def best_by_score(fake: FakeSolr, k: int) -> List[int]:
    return [d["docid"] for d in sorted(fake.docs, key=lambda d: (-solr_score(d["docid"]),
                                                                  d["docid"]))[:k]]


def docids(items: List[Dict]) -> List[int]:
    return [int(it["url"].rsplit("-", 1)[1]) for it in items]


def search(fake, **kw):
    c = HALConnector(http_client=fake)
    items = c.search("discours numérique", **kw)
    return items, c.last_status


# === Bibliographie : meilleurs scores ===

def test_bibliographie_keeps_the_best_scores_when_numfound_exceeds_the_cap():
    fake = FakeSolr(5000)
    items, report = search(fake, max_results=200, mode="bibliographie")
    assert docids(items) == best_by_score(fake, 200)
    # Une requête : rows = min(1000, 200), tri par score, start 0, score dans fl.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["rows"] == 200 and call["start"] == 0
    assert call["sort"] == SORT_BIBLIOGRAPHIE == "score desc,docid asc"
    assert "cursorMark" not in call
    assert "score" in call["fl"].split(",")
    # Rang et score natifs, alignés sur les notices.
    assert report.native_sort == SORT_BIBLIOGRAPHIE
    assert [r["rank"] for r in report.native_ranks] == list(range(1, 201))
    scores = [r["score"] for r in report.native_ranks]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == solr_score(docids(items)[0])
    assert report.limit_reached and report.status is None


def test_collecte_is_unchanged_and_deterministic():
    fake = FakeSolr(5000)
    first, report = search(fake, max_results=200, mode="collecte")
    again, _ = search(FakeSolr(5000), max_results=200, mode="collecte")
    assert docids(first) == docids(again) == list(range(1, 201))   # docid asc
    call = fake.calls[0]
    assert call["sort"] == SORT_COLLECTE == "docid asc"
    assert call["cursorMark"] == "*" and "start" not in call
    assert call["rows"] == 200   # et non plus 1000 pour en garder 200
    assert report.native_sort == "docid asc"
    assert [r["rank"] for r in report.native_ranks] == list(range(1, 201))
    # Score renvoyé sous cursorMark (vérifié en live) : enregistré, sans tri.
    assert all(r["score"] is not None for r in report.native_ranks)


def test_without_mode_the_historical_cursor_is_used():
    fake = FakeSolr(300)
    items, report = search(fake, max_results=50)
    assert fake.calls[0]["sort"] == "docid asc" and fake.calls[0]["cursorMark"] == "*"
    assert docids(items) == list(range(1, 51))
    assert report.native_sort == "docid asc"


def test_the_two_modes_differ_only_by_the_sort_on_a_truncated_result():
    fake = FakeSolr(1200)
    biblio, _ = search(fake, max_results=100, mode="bibliographie")
    collecte, _ = search(fake, max_results=100, mode="collecte")
    assert set(docids(biblio)) != set(docids(collecte))
    # Sans troncature, les deux modes renvoient le même ensemble.
    small = FakeSolr(80)
    a, _ = search(small, max_results=100, mode="bibliographie")
    b, _ = search(small, max_results=100, mode="collecte")
    assert sorted(docids(a)) == sorted(docids(b)) == list(range(1, 81))


@pytest.mark.parametrize("cap,expected_rows", [(1, 1), (200, 200), (1000, 1000),
                                               (1500, 1000)])
@pytest.mark.parametrize("mode", ["bibliographie", "collecte"])
def test_rows_is_min_1000_max(cap, expected_rows, mode):
    fake = FakeSolr(5000)
    items, report = search(fake, max_results=cap, mode=mode)
    assert len(items) == cap
    assert fake.calls[0]["rows"] == expected_rows
    assert len(fake.calls) == (2 if cap > 1000 else 1)
    assert len(report.native_ranks) == cap


def test_rows_setting_is_honoured(monkeypatch):
    monkeypatch.setattr(hal_module, "HAL_ROWS_PER_BATCH", 300)
    fake = FakeSolr(1000)
    items, report = search(fake, max_results=700, mode="bibliographie")
    # Dernier lot ramené au reste à collecter (revue OPT-12).
    assert [c["rows"] for c in fake.calls] == [300, 300, 100]
    assert [c["start"] for c in fake.calls] == [0, 300, 600]
    assert docids(items) == best_by_score(fake, 700)
    assert [r["rank"] for r in report.native_ranks] == list(range(1, 701))


def test_bibliographie_multi_page_ranks_follow_start():
    fake = FakeSolr(2500)
    items, report = search(fake, max_results=2200, mode="bibliographie")
    assert [c["start"] for c in fake.calls] == [0, 1000, 2000]
    # Dernier lot ramené au reste à collecter (revue OPT-12).
    assert [c["rows"] for c in fake.calls] == [1000, 1000, 200]
    assert docids(items) == best_by_score(fake, 2200)
    assert [r["rank"] for r in report.native_ranks] == list(range(1, 2201))


def test_bibliographie_exhausts_without_an_extra_request():
    fake = FakeSolr(150)
    items, report = search(fake, max_results=1000, mode="bibliographie")
    assert len(items) == 150 and len(fake.calls) == 1
    assert report.exhausted and not report.limit_reached and report.status is None


def test_ranked_pagination_is_bounded(monkeypatch):
    monkeypatch.setattr(hal_module, "HAL_RANKED_MAX_RESULTS", 250)
    fake = FakeSolr(5000)
    items, report = search(fake, max_results=1000, mode="bibliographie")
    assert len(items) == 250 and docids(items) == best_by_score(fake, 250)
    # Une requête : rows ramené à la place restante (start + rows <= borne).
    assert [(c["start"], c["rows"]) for c in fake.calls] == [(0, 250)]
    assert report.status == "partial" and report.reason == "scan_bound"
    assert "5000" in report.detail and "collecte" in report.detail


def test_default_ranked_bound_is_10000():
    assert config.HAL_RANKED_MAX_RESULTS == 10000
    assert config.HAL_ROWS_PER_BATCH == 1000


def test_document_repeated_by_a_replica_is_dropped_and_ranks_stay_aligned(monkeypatch):
    monkeypatch.setattr(hal_module, "HAL_ROWS_PER_BATCH", 100)
    fake = FakeSolr(1000, replica_shift=True)
    items, report = search(fake, max_results=250, mode="bibliographie")
    ids = docids(items)
    assert len(ids) == len(set(ids)) == 250
    assert report.counters["duplicates"] >= 1
    assert len(report.native_ranks) == len(items)
    ranks = [r["rank"] for r in report.native_ranks]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)


def test_mapping_error_leaves_a_gap_in_native_ranks(monkeypatch):
    real = hal_module.map_hal_to_zotero

    def flaky(doc):
        if doc["docid"] == best_by_score(FakeSolr(400), 3)[1]:
            raise KeyError("title_s")
        return real(doc)
    monkeypatch.setattr(hal_module, "map_hal_to_zotero", flaky)
    items, report = search(FakeSolr(400), max_results=5, mode="bibliographie")
    assert len(items) == 5
    assert [r["rank"] for r in report.native_ranks] == [1, 3, 4, 5, 6]
    assert report.counters["mapping_errors"] == 1


def test_zero_cap_makes_no_request():
    fake = FakeSolr(10)
    items, report = search(fake, max_results=0, mode="bibliographie")
    assert items == [] and fake.calls == [] and report.limit_reached


# === Filtre de langue natif ===

def test_language_filter_is_native_and_reported():
    fake = FakeSolr(600)
    items, report = search(fake, max_results=1000, mode="bibliographie",
                           languages=["FR", "fr", "xx1", "en"])
    assert fake.calls[0]["fq"] == "language_s:(fr OR en)"
    assert report.native_filters == {"languages": ["fr", "en"]}
    assert len(items) == 600
    only_fr, rep_fr = search(FakeSolr(600), max_results=1000, languages=["fr"])
    assert len(only_fr) == 400 and rep_fr.native_filters == {"languages": ["fr"]}


def test_language_and_date_filters_are_two_fq():
    fake = FakeSolr(30)
    search(fake, max_results=10, mode="collecte", from_date="2018", languages=["fr"])
    fq = fake.calls[0]["fq"]
    assert isinstance(fq, list) and len(fq) == 2
    assert fq[0].startswith("producedDate_tdate:[2018-01-01")
    assert fq[1] == "language_s:(fr)"


def test_without_languages_no_language_filter():
    fake = FakeSolr(30)
    _, report = search(fake, max_results=10, from_date="2018")
    assert isinstance(fake.calls[0]["fq"], str)   # forme historique
    assert report.native_filters == {}


# === Invariant 1 ===

@pytest.mark.parametrize("mode", ["bibliographie", "collecte"])
def test_no_rank_or_score_in_zotero_items(mode):
    items, report = search(FakeSolr(300), max_results=50, mode=mode)
    assert report.native_ranks
    for item in items:
        assert not {"score", "rank", "native_rank", "native_ranks", "relevance_score"} & set(item)
        assert "score" not in item.get("extra", "").lower()
        assert "rank" not in item.get("extra", "").lower()
