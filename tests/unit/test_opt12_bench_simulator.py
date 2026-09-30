"""
OPT-12 : sémantique de tri et de langue du simulateur du banc, alignée sur
les vérifications live du 2026-09-29.

- HAL : ``sort=score desc,docid asc`` classe par score (première clause) et
  reste accepté sous ``cursorMark`` ; ``fq=language_s:(…)`` filtre ;
- CrossRef : sous ``cursor`` sans ``sort``, ordre de l'index (scores non
  triés) ; ``sort=score&order=desc`` trie et se poursuit par curseur ; tri
  par date refusé sous curseur ; filtre ``language`` refusé ;
- OpenAlex : ``filter=language:fr|en`` filtre ;
- Isidore : ``replies=200`` sert la fenêtre entière en une page, sans bloc
  ``page`` ;
- Semantic Scholar : bulk sans tri par ``paperId``, ``citationCount:desc``
  poursuivi par ``token``.

Aucune socket réelle (garde du module de tests du banc).
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bench.instrument import NetworkRecorder, install_network_layer  # noqa: E402
from bench.simulator import ApiSimulator  # noqa: E402

HAL = "https://api.archives-ouvertes.fr/search/"
CR = "https://api.crossref.org/works"
OA = "https://api.openalex.org/works"
ISI = "https://api.isidore.science/resource/search"
S2 = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"


@pytest.fixture(autouse=True)
def no_real_socket(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("connexion réelle tentée")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def session():
    sim = ApiSimulator({
        "universe": {"seed": "opt12", "size": 4000, "english_rate": 0.4,
                     "subsource_weights": {"openedition": 0.3, "cairn": 0.1,
                                           "hal": 0.4, "autre": 0.2}},
        "providers": {"hal": {"target_total": 900},
                      "crossref": {"target_total": 1500},
                      "openalex": {"target_total": 1500},
                      "semantic_scholar": {"target_total": 1500}},
    })
    restore = install_network_layer(NetworkRecorder(parse_records=False),
                                    inner_send=sim.send)
    try:
        yield requests.Session()
    finally:
        restore()


def _docs(r):
    return r.json()["response"]["docs"]


def test_hal_score_sort_with_tie_breaker_ranks_by_score(session):
    params = {"q": "x", "fl": "docid,score", "rows": 50, "start": 0}
    by_score = _docs(session.get(HAL, params={**params, "sort": "score desc,docid asc"}))
    scores = [d["score"] for d in by_score]
    assert scores == sorted(scores, reverse=True)
    by_docid = _docs(session.get(HAL, params={**params, "sort": "docid asc"}))
    assert [int(d["docid"]) for d in by_docid] == sorted(int(d["docid"]) for d in by_docid)
    assert [d["docid"] for d in by_score] != [d["docid"] for d in by_docid]
    # Accepté sous cursorMark (la clé unique figure dans le tri), comme en live.
    r = session.get(HAL, params={"q": "x", "fl": "docid,score", "rows": 10,
                                 "cursorMark": "*", "sort": "score desc,docid asc"})
    assert r.status_code == 200 and r.json()["nextCursorMark"] != "*"


def test_hal_language_filter(session):
    base = {"q": "x", "fl": "docid,language_s", "rows": 2000, "start": 0,
            "sort": "docid asc"}
    all_docs = session.get(HAL, params=base).json()["response"]
    fr = session.get(HAL, params={**base, "fq": "language_s:(fr)"}).json()["response"]
    both = session.get(HAL, params={**base, "fq": ["language_s:(fr OR en)"]}).json()["response"]
    assert 0 < fr["numFound"] < all_docs["numFound"] == both["numFound"]
    assert {d["language_s"][0] for d in fr["docs"]} == {"fr"}


def test_crossref_cursor_without_sort_is_index_order(session):
    base = {"query.bibliographic": "x", "rows": 40, "cursor": "*", "select": "DOI,score"}
    unsorted = session.get(CR, params=base).json()["message"]["items"]
    scores = [it["score"] for it in unsorted]
    assert scores != sorted(scores, reverse=True)
    ranked = session.get(CR, params={**base, "sort": "score", "order": "desc"}).json()["message"]
    top = [it["score"] for it in ranked["items"]]
    assert top == sorted(top, reverse=True) and top[0] > max(scores) - 1e-9
    nxt = session.get(CR, params={**base, "sort": "score", "order": "desc",
                                  "cursor": ranked["next-cursor"]}).json()["message"]["items"]
    assert nxt[0]["score"] <= top[-1]


def test_crossref_refuses_date_sort_under_cursor_and_language_filter(session):
    r = session.get(CR, params={"query.bibliographic": "x", "rows": 5, "cursor": "*",
                                "sort": "published", "order": "desc"})
    assert r.status_code == 400
    assert r.json()["message"][0]["type"] == "sort-criteria-incompatible-with-cursor"
    r = session.get(CR, params={"query.bibliographic": "x", "rows": 5, "cursor": "*",
                                "filter": "from-pub-date:2018-01-01,language:fr"})
    assert r.status_code == 400
    assert r.json()["message"][0]["type"] == "filter-not-available"


def test_openalex_language_filter(session):
    base = {"search": "x", "per-page": 200, "cursor": "*", "api_key": "k",
            "select": "id,language,relevance_score"}
    total = session.get(OA, params=base).json()["meta"]["count"]
    fr = session.get(OA, params={**base, "filter": "language:fr"}).json()
    assert 0 < fr["meta"]["count"] < total
    assert {r["language"] for r in fr["results"]} == {"fr"}
    both = session.get(OA, params={**base, "filter": "language:fr|en"}).json()
    assert both["meta"]["count"] == total


def test_isidore_replies_200_is_one_page_without_page_block(session):
    data = session.get(ISI, params={"q": "x", "replies": 200, "page": 1,
                                    "output": "json"}).json()["response"]["replies"]
    assert data["meta"]["@pageItems"] == "200"
    assert len(data["content"]["reply"]) == 200
    assert "page" not in data


def test_s2_bulk_orders(session):
    base = {"query": "x", "fields": "title,citationCount"}
    plain = session.get(S2, params=base).json()
    ids = [d["paperId"] for d in plain["data"]]
    assert ids == sorted(ids)
    ranked = session.get(S2, params={**base, "sort": "citationCount:desc"}).json()
    cites = [d["citationCount"] for d in ranked["data"]]
    assert cites == sorted(cites, reverse=True)
    more = session.get(S2, params={**base, "sort": "citationCount:desc",
                                   "token": ranked["token"]}).json()
    assert more["data"][0]["citationCount"] <= cites[-1]
