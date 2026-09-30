# -*- coding: utf-8 -*-
"""
Intégration de la vague C2 : OPT-13 (classement local explicable), OPT-15
(protocole d'évaluation SHS), OPT-09 bis (échéance souple en stdio).

Aucun accès réseau : exécutants de source remplacés, cache désactivé.

- **OPT-15 × OPT-13** : l'adaptateur du protocole (``scripts/eval/rankers.py``)
  est branché sur ``utils.ranking`` par l'API ``rank_records`` (ni crochet, ni
  découverte générique, ni repli sur l'entrelacement) ; après un aller-retour
  moisson → instantané JSON → reconstruction hors réseau, les classeurs
  ``native``, ``local:simple`` et ``local:bm25`` rendent EXACTEMENT l'ordre
  calculé par le moteur (``HarvestResult.ranking``) ou par ``rank_records``
  sur la moisson en mémoire. La référence du gate est donc l'ordre servi par
  défaut (``native``) ;
- **OPT-13 × OPT-09 bis** : en stdio, quand l'échéance souple arrête une
  source, le corpus publié des autres sources est classé ;
  ``structuredContent.top_ranked`` et ``structuredContent.soft_deadline``
  coexistent, et chaque entrée du classement renvoie à une notice du fichier
  de corpus publié (aucune notice de la source arrêtée, invariant 1) ;
- **isolation des tests** : les réglages ``MOISSON_RANKING``,
  ``MOISSON_STDIO_SOFT_DEADLINE``, ``MOISSON_STDIO_SOFT_DEADLINE_GRACE`` et
  ``MOISSON_PROGRESS_HEARTBEAT`` hérités du shell sont retirés par
  ``tests/conftest.py`` : les valeurs par défaut s'appliquent.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

# Import anticipé : urllib3 sonde IPv6 à l'import (bind local, sans connexion) ;
# la passe floue de la déduplication importe requests paresseusement.
import requests  # noqa: E402,F401

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.models import RunContext, SearchRequest, SourceResult  # noqa: E402
from eval import common as C  # noqa: E402
from eval import pool as P  # noqa: E402
from eval import rankers as R  # noqa: E402
from eval import synthetic as T  # noqa: E402
from utils import ranking as RK  # noqa: E402

HAL_SORT = "score desc,docid asc"


# === Univers simulé ===

def _art(title, *, doi=None, label="HAL", abstract="", lines=(), author="Dupont",
         year="2021", tags=()):
    item = {"itemType": "journalArticle", "title": title,
            "creators": [{"creatorType": "author", "firstName": "Anne",
                          "lastName": author}],
            "date": year, "abstractNote": abstract,
            "tags": [{"tag": t} for t in tags]}
    if doi:
        item["DOI"] = doi
    item["extra"] = "\n".join([f"Sources: {label}", *lines])
    return item


def _isidore(n, title, label, **kw):
    return _art(title, label=label, author="Martin",
                lines=(f"Isidore URI: https://isidore.science/document/10670/1.{n:06d}",),
                **kw)


def _universe():
    """Notices servies par source (notices, tri natif, rangs) : HAL et
    CrossRef classés par pertinence (doublons par DOI), Isidore et Cairn
    (même flux, rangs de position), Semantic Scholar par citations. Les
    titres et résumés varient pour que le score lexical départage."""
    hal = [_art(f"Discours numérique et institutions {i}" if i % 2 == 0
                else f"Institutions publiques en ligne {i}",
                doi=f"10.7/h{i}", year=str(2010 + i),
                abstract=("Le discours numérique des institutions. " * (1 + i % 3)),
                tags=("discours numérique",) if i % 3 == 0 else ())
           for i in range(10)]
    crossref = [_art(f"Discours numérique et institutions {i}", doi=f"10.7/h{i}",
                     label="CrossRef", year=str(2010 + i)) for i in range(0, 10, 4)]
    crossref += [_art(f"Chimie organique {i}", doi=f"10.7/c{i}", label="CrossRef",
                      lines=("Cited by: 50000",)) for i in range(3)]
    isidore = [_isidore(i, f"Revues savantes en ligne {i}", "Isidore",
                        abstract="Revues et discours numérique." if i % 2 else "")
               for i in range(6)]
    cairn = [_isidore(i, f"Revues savantes en ligne {i}", "Cairn",
                      abstract="Revues et discours numérique." if i % 2 else "")
             for i in range(0, 6, 2)]
    s2 = [_art(f"Chimie organique {i}", doi=f"10.7/c{i}",
               label="Semantic Scholar", lines=("Cited by: 50000",))
          for i in range(3)]
    return {
        "hal": (hal, HAL_SORT, None),
        "crossref": (crossref, "score:desc", None),
        "isidore": (isidore, "isidore_default", None),
        "cairn": (cairn, "isidore_default", [1, 3, 5]),
        "semantic_scholar": (s2, "citationCount:desc", None),
    }


def _source_result(source_key, name, items, sort, ranks):
    result = SourceResult(source=source_key, display_name=name,
                          items=copy.deepcopy(items), status="complete")
    if sort:
        result.native_sort = sort
        result.native_ranks = [{"rank": r, "score": None, "sort": sort}
                               for r in (ranks or range(1, len(items) + 1))]
        result.bind_ranks()
    return result


def _runner(universe=None):
    data = universe or _universe()

    def stub(name, cls, query, mps, output_dir, run_id, http_client,
             from_date=None, to_date=None, *, run_context=None,
             source_key=None, **kw):
        items, sort, ranks = data[source_key]
        return _source_result(source_key, name, items, sort, ranks)
    return stub


def _request():
    return SearchRequest(query='"discours numérique" institutions',
                         sources=["hal", "crossref", "isidore", "cairn",
                                  "semantic_scholar"],
                         mode="bibliographie", max_per_source=50, cache=False,
                         workers=1, allow_metered=False)


# === OPT-15 × OPT-13 : l'adaptateur du protocole rejoue l'ordre du moteur ===

def test_protocol_adapter_is_wired_to_rank_records():
    """Classeurs découverts par l'API d'OPT-13 (``rank_records`` avec
    ``mode``), versionnés, sans note de repli."""
    rankers, notes = R.resolve_rankers([R.NATIVE, R.LOCAL])
    assert notes == []
    assert [r.name for r in rankers] == ["native", "local:simple", "local:bm25"]
    assert {r.version for r in rankers} == {RK.RANKING_VERSION}
    assert rankers[0].description == "utils.ranking.rank_records(mode='native')"
    assert rankers[1].description == (
        "utils.ranking.rank_records(mode='local', lexical_model='simple')")
    # La référence du gate est l'ordre servi par défaut.
    assert config.RANKING_DEFAULT == RK.RANKING_NATIVE
    assert RK.resolve_ranking_mode(None) == RK.RANKING_NATIVE
    assert RK.DEFAULT_LEXICAL_MODEL == RK.LEXICAL_SIMPLE


@pytest.mark.parametrize("mode", [RK.RANKING_NATIVE, RK.RANKING_LOCAL])
def test_offline_replay_matches_the_engine_order(monkeypatch, mode):
    monkeypatch.setattr(orchestrator, "run_source", _runner())
    request = _request()
    result = orchestrator.harvest(request, RunContext(), ranking=mode)
    assert result.ranking and not result.ranking.get("error")
    assert result.ranking["mode"] == mode
    live_cids = C.candidate_ids(result.items)
    engine_order = [live_cids[e["index"]] for e in result.ranking["entries"]]
    assert sorted(engine_order) == sorted(live_cids)

    # Instantané (format de ``pool.py collect``), relu comme depuis le disque.
    snapshot = P.build_snapshot(
        T.query_entry("D01"), request, result, http_stats={},
        collected_at=f"{datetime.now().year}-01-01T00:00:00",
        environment={"keys": {"keys": False, "removed": []}})
    assert snapshot["groups"] is not None and not snapshot["warnings"]
    assert snapshot["engine_ranking"]["mode"] == mode
    assert snapshot["engine_ranking"]["version"] == RK.RANKING_VERSION
    snapshot = json.loads(json.dumps(snapshot, ensure_ascii=False))
    req2, harvest2 = P.reconstruct(snapshot)
    cids = C.candidate_ids(harvest2.items)
    assert cids == live_cids            # mêmes notices, même ordre de corpus
    assert [cids[i] for i in snapshot["engine_ranking"]["order"]] == engine_order

    rankers = {r.name: r for r in R.resolve_rankers([R.NATIVE, R.LOCAL])[0]}
    replay = {name: r.rank(harvest2.items, req2, harvest2)
              for name, r in rankers.items()}
    expected_name = "native" if mode == RK.RANKING_NATIVE else "local:simple"
    assert replay[expected_name] == engine_order

    # Chaque classeur du protocole = rank_records sur la moisson EN MÉMOIRE
    # (rattachement par identité et clés, année courante), comme le moteur.
    plan = request.query_plan()
    for name, kwargs in (("native", {"mode": RK.RANKING_NATIVE}),
                         ("local:simple", {"mode": RK.RANKING_LOCAL,
                                           "lexical_model": RK.LEXICAL_SIMPLE}),
                         ("local:bm25", {"mode": RK.RANKING_LOCAL,
                                         "lexical_model": RK.LEXICAL_BM25})):
        direct = RK.rank_records(result.items, result.sources, plan=plan, **kwargs)
        assert replay[name] == [live_cids[e["index"]] for e in direct["entries"]], name


def test_replay_never_touches_the_notices(monkeypatch):
    """Invariant 1 : l'évaluation ne modifie ni les notices de l'instantané ni
    celles du résultat reconstruit."""
    monkeypatch.setattr(orchestrator, "run_source", _runner())
    request = _request()
    result = orchestrator.harvest(request, RunContext(), ranking=RK.RANKING_LOCAL)
    snapshot = json.loads(json.dumps(P.build_snapshot(
        T.query_entry("D01"), request, result, http_stats={},
        collected_at="2026-09-29T00:00:00", environment={}), ensure_ascii=False))
    before = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    pool = P.build_pool(snapshot, R.resolve_rankers([R.NATIVE, R.LOCAL])[0], depth=5)
    assert json.dumps(snapshot, sort_keys=True, ensure_ascii=False) == before
    assert all(entry["error"] is None for entry in pool["rankers"].values())
    for cand in pool["candidates"].values():
        blob = json.dumps(cand["item"], ensure_ascii=False)
        assert RK.RANKING_VERSION not in blob and '"rank"' not in blob


# === OPT-13 × OPT-09 bis : corpus partiel classé en stdio ===

def _mute_isidore_runner(universe=None):
    """HAL et CrossRef répondent (rangs natifs) ; Isidore reste muet jusqu'à
    son signal (échéance souple ou annulation)."""
    data = universe or _universe()

    def stub(name, cls, query, mps, output_dir, run_id, http_client,
             run_context=None, source_key=None, **kw):
        if source_key in ("hal", "crossref"):
            items, sort, ranks = data[source_key]
            return _source_result(source_key, name, items, sort, ranks)
        run_context.check()
        while not run_context.signal.is_set():
            time.sleep(0.01)
        run_context.signal.check()
        return []
    return stub


@pytest.fixture
def stdio_out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
    monkeypatch.setenv("MOISSON_CACHE", "0")
    monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "cache" / "http"))
    monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_MIN_S", 0.1)
    monkeypatch.setattr(config, "STDIO_SOFT_DEADLINE_GRACE_S", 0.3)
    return tmp_path


@pytest.mark.parametrize("mode", [None, RK.RANKING_LOCAL])
def test_soft_deadline_partial_corpus_is_ranked(stdio_out, monkeypatch, mode):
    monkeypatch.setattr(orchestrator, "run_source", _mute_isidore_runner())
    # Retour « full » : extrait complet de top_ranked (OPT-17 : en mode
    # digest, les notices classées passent dans top_items).
    args = {"query": '"discours numérique" institutions',
            "sources": ["hal", "crossref", "isidore"], "deadline_seconds": 0.6,
            "inline": "full"}
    if mode is not None:
        args["ranking"] = mode
    res = asyncio.run(mcp_server._tool_moisson_search(args))
    assert not mcp_server._is_tool_error(res)
    _blocks, structured = res
    assert structured["harvest_status"] == "partial"
    assert structured["soft_deadline"]["stopped_sources"] == ["Isidore"]
    assert structured["by_source_status"]["Isidore"]["reason"] == "deadline"

    top = structured["top_ranked"]
    assert top["mode"] == (mode or RK.RANKING_NATIVE)
    assert top["version"] == RK.RANKING_VERSION
    assert top["records"] == structured["total_unique"]
    assert [e["rank"] for e in top["items"]] == sorted(e["rank"] for e in top["items"])

    # OPT-16 : le manifeste (<stem>.manifest.json) s'ajoute aux fichiers.
    corpus_path = next(p for p in stdio_out.glob("corpus_*.json")
                       if not p.name.endswith((".csl.json", ".manifest.json")))
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    assert len(corpus) == structured["total_unique"]
    for entry in top["items"]:
        # Chaque entrée renvoie à une notice du corpus publié…
        assert 0 <= entry["index"] < len(corpus)
        assert RK.record_id(corpus[entry["index"]]) == entry["id"]
        # … jamais à une notice de la source arrêtée.
        assert "Isidore" not in corpus[entry["index"]].get("extra", "")
    # Invariant 1 : ni rang ni score dans les notices publiées.
    blob = corpus_path.read_text(encoding="utf-8")
    assert RK.RANKING_VERSION not in blob and '"rank"' not in blob
    # Les notices classées par pertinence (HAL) passent devant le reste.
    assert top["items"][0]["id"].startswith("doi:10.7/h")


# === Isolation des réglages hérités du shell ===

@pytest.mark.parametrize("var,attr,default", [
    ("MOISSON_RANKING", "RANKING_DEFAULT", "native"),
    ("MOISSON_STDIO_SOFT_DEADLINE", "STDIO_SOFT_DEADLINE_S", 50.0),
    ("MOISSON_STDIO_SOFT_DEADLINE_GRACE", "STDIO_SOFT_DEADLINE_GRACE_S", 2.0),
    ("MOISSON_PROGRESS_HEARTBEAT", "HARVEST_PROGRESS_HEARTBEAT_S", 10.0),
])
def test_shell_tuning_is_not_inherited(var, attr, default):
    """Retirées par ``tests/conftest.py`` avant l'import de ``config`` : un
    shell qui exporte ces variables ne change pas les tests."""
    assert var not in os.environ
    assert getattr(config, attr) == default
