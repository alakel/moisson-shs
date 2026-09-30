# -*- coding: utf-8 -*-
"""
OPT-15 : collecte des candidats et classeurs (aucun accès réseau).

- garde d'environnement : jamais de clé SerpAPI, aucune clé sans ``--keys``,
  Google Scholar refusé ;
- collecte : moisson simulée (moteur doublé), demande conforme (mode
  bibliographie, sans cache, sources facturées refusées), instantané
  complet et reprise ;
- instantané → reconstruction → ensemble commun de candidats (union des k
  premiers de chaque classeur) ;
- classeurs : entrelacement des ordres natifs (cas calculé à la main),
  adaptateur d'OPT-13 (module réel s'il est présent, module simulé sinon),
  tolérance à l'absence du module, normalisation des sorties, classeur
  externe ;
- identifiants de candidats stables et sans information de source.
"""

from __future__ import annotations

import os
import re
import sys
import types
from pathlib import Path

import pytest

# Import anticipé : urllib3 sonde IPv6 à l'import (bind local, sans connexion) ;
# la passe floue de la déduplication importe requests paresseusement.
import requests  # noqa: E402,F401

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from eval import common as C  # noqa: E402
from eval import pool as P  # noqa: E402
from eval import rankers as R  # noqa: E402
from eval import synthetic as T  # noqa: E402


# === Garde d'environnement ===

def test_prepare_environment_without_keys():
    env = {"OPENALEX_API_KEY": "k1", "CORE_API_KEY": "k2", "SERPAPI_API_KEY": "s",
           "SEMANTIC_SCHOLAR_API_KEY": "k3", "AUTRE": "x"}
    info = P.prepare_environment(keys=False, environ=env)
    assert env == {"AUTRE": "x", "MOISSON_NO_DOTENV": "1"}
    assert set(info["removed"]) == {"OPENALEX_API_KEY", "CORE_API_KEY", "SERPAPI_API_KEY",
                                    "SEMANTIC_SCHOLAR_API_KEY"}


def test_prepare_environment_with_keys_still_removes_serpapi():
    env = {"OPENALEX_API_KEY": "k1", "SERPAPI_API_KEY": "s"}
    P.prepare_environment(keys=True, environ=env)
    assert env == {"OPENALEX_API_KEY": "k1"}
    env["SERPAPI_API_KEY"] = "rechargée par un .env"
    assert P.strip_metered_keys(env) == ["SERPAPI_API_KEY"] and "SERPAPI_API_KEY" not in env


def test_eval_sources_never_scholar():
    for group in ("francophone", "all"):
        sources = P.eval_sources(group)
        assert sources and "google_scholar" not in sources
    assert set(P.eval_sources("francophone")) < set(P.eval_sources("all"))
    for bad in ("scholar", "google_scholar", "international"):
        with pytest.raises(C.EvalError):
            P.eval_sources(bad)


def test_ensure_writable_refuses_the_repository_outside_output_eval(tmp_path):
    with pytest.raises(C.EvalError):
        C.ensure_writable(C.ROOT / "output" / "corpus_test.json")
    with pytest.raises(C.EvalError):
        C.ensure_writable(C.ROOT / "scripts" / "x.json")
    assert C.ensure_writable(tmp_path / "x.json") == (tmp_path / "x.json").resolve()


# === Collecte simulée ===

def test_collect_with_a_doubled_engine(tmp_path, monkeypatch):
    import engine.orchestrator as orchestrator

    seen = []

    def fake_harvest(request, ctx):
        seen.append((request, ctx))
        _req, result = T.sample_harvest(request.query)
        result.request = request
        return result

    monkeypatch.setattr(orchestrator, "harvest", fake_harvest)
    monkeypatch.setenv("SERPAPI_API_KEY", "ne-doit-pas-rester")
    queries = [T.query_entry("D01"), T.query_entry("R01", "reglage")]
    out = P.collect(queries, tmp_path, max_per_source=100, workers=2, pause=0, keys=False)
    assert [e["id"] for e in out] == ["D01", "R01"]
    request, ctx = seen[0]
    assert request.mode == "bibliographie" and request.cache is False
    assert request.allow_metered is False and request.max_per_source == 100
    assert "google_scholar" not in request.sources
    assert ctx.output_dir is None                       # rien dans output/raw
    assert "SERPAPI_API_KEY" not in os.environ
    snap = C.read_json(tmp_path / C.SNAPSHOTS / "D01.json")
    assert snap["format"] == P.SNAPSHOT_FORMAT and snap["groups"] is not None
    assert len(snap["items"]) == 5 and snap["total_before_dedup"] == 6
    assert [s["source"] for s in snap["sources"]] == ["hal", "crossref"]
    assert snap["sources"][0]["ranking"]["ranks"][0]["rank"] == 1
    assert snap["environment"]["keys"]["keys"] is False
    journal = C.read_json(tmp_path / "journal_collecte.json")
    assert [j["id"] for j in journal] == ["D01", "R01"]
    # Reprise : une requête déjà collectée n'est pas refaite.
    assert P.collect(queries, tmp_path, pause=0) == [] and len(seen) == 2


# === Instantané, reconstruction, ensemble de candidats ===

def test_snapshot_groups_and_reconstruction():
    snap = T.sample_snapshot()
    by_title = {it["title"]: i for i, it in enumerate(snap["items"])}
    a = by_title["Le discours numérique en question"]
    assert sorted(map(tuple, snap["groups"][a])) == [(0, 0), (1, 1)]
    request, harvest = P.reconstruct(snap)
    assert request.mode == "bibliographie" and request.allow_metered is False
    assert [s.source for s in harvest.sources] == ["hal", "crossref"]
    assert harvest.sources[0].aligned_ranks()[0]["sort"] == "score desc,docid asc"
    assert harvest.eval_groups == snap["groups"]
    assert harvest.eval_reference_year == 2026


def test_interleave_hand_computed():
    snap = T.sample_snapshot()
    request, harvest = P.reconstruct(snap)
    order = R.native_order(harvest.items, request, harvest)
    titles = [harvest.items[i]["title"] for i in order]
    # Meilleures positions : a 1 (HAL), d 1 (CrossRef), b 2, c 3 (HAL), e 3.
    assert titles == ["Le discours numérique en question", "Digital discourse analysis",
                      "Pratiques numériques des étudiants", "Histoire de la presse",
                      "Une thèse sur les forums"]
    # Sans composition enregistrée : recalcul par la déduplication, même ordre.
    harvest.eval_groups = None
    assert R.native_order(harvest.items, request, harvest) == order


def test_build_pool_union_of_top_k():
    snap = T.sample_snapshot()
    rankers = [T.fixed_ranker("native", ["Histoire de la presse", "Une thèse sur les forums"]),
               T.fixed_ranker("local:simple", ["Digital discourse analysis",
                                               "Histoire de la presse"])]
    pool = T.pool_for(snap, rankers, depth=2)
    t2c = T.title_to_cid(pool)
    assert set(t2c) == {"Histoire de la presse", "Une thèse sur les forums",
                        "Digital discourse analysis"}
    assert pool["pool"] == sorted(pool["pool"])
    assert all(len(v["ranking"]) == 5 and v["error"] is None for v in pool["rankers"].values())
    assert len(pool["all_keys"]) == 5
    assert any("doi:10.1000/a" in keys for keys in pool["all_keys"].values())
    for cand in pool["candidates"].values():
        assert set(cand["display"]) == {"titre", "auteurs", "annee", "type", "support",
                                        "resume", "lien"}


def test_build_pool_records_a_failing_ranker():
    def boom(items, request, harvest):
        raise RuntimeError("panne du classeur")
    snap = T.sample_snapshot()
    rankers = [R.interleave_ranker("native"), R.Ranker("local:x", "local", boom)]
    pool = T.pool_for(snap, rankers, depth=3)
    assert "panne du classeur" in pool["rankers"]["local:x"]["error"]
    assert len(pool["pool"]) == 3


def test_build_writes_pools(tmp_path):
    C.write_json(tmp_path / C.SNAPSHOTS / "D01.json", T.sample_snapshot())
    summary = P.build(tmp_path, [R.interleave_ranker("native")], depth=2)
    assert summary[0]["pool"] == 2
    assert (tmp_path / C.POOLS / "D01.json").exists()
    with pytest.raises(C.EvalError):
        P.build(tmp_path / "vide", [R.interleave_ranker("native")])


# === Adaptateur du classement local (OPT-13) ===

def test_missing_module_falls_back_to_interleave():
    rankers, notes = R.resolve_rankers(["native", "local"], module_name="module_absent_opt15")
    assert [r.name for r in rankers] == ["native"]
    assert rankers[0].version == R.INTERLEAVE_VERSION
    assert any("absent" in n for n in notes) and any("entrelacement" in n for n in notes)


def _fake_opt13(monkeypatch, calls):
    mod = types.ModuleType("faux_classement_opt15")
    mod.RANKING_VERSION = "faux-1"
    mod.RANKING_NATIVE, mod.RANKING_LOCAL = "native", "local"
    mod.LEXICAL_MODELS = ("simple", "bm25")

    def rank_records(records, sources, *, mode="native", plan=None, query=None,
                     query_variants=None, lexical_model="simple", groups=None,
                     check=None, reference_year=None):
        calls.append({"mode": mode, "model": lexical_model, "groups": groups,
                      "year": reference_year, "n_sources": len(sources),
                      "plan": plan is not None})
        order = list(range(len(records)))
        if mode == "local":
            order.reverse() if lexical_model == "simple" else order.sort(
                key=lambda i: records[i]["title"])
        return {"version": "faux-1", "entries": [{"index": i, "id": "x", "rank": r + 1}
                                                 for r, i in enumerate(order)]}

    mod.rank_records = rank_records
    monkeypatch.setitem(sys.modules, "faux_classement_opt15", mod)
    return mod


def test_opt13_api_adapter(monkeypatch):
    calls = []
    _fake_opt13(monkeypatch, calls)
    rankers, notes = R.resolve_rankers(["native", "local"], module_name="faux_classement_opt15")
    assert notes == []
    assert [r.name for r in rankers] == ["native", "local:simple", "local:bm25"]
    assert all(r.version == "faux-1" for r in rankers)
    snap = T.sample_snapshot()
    request, harvest = P.reconstruct(snap)
    cids = C.candidate_ids(harvest.items)
    assert rankers[0].rank(harvest.items, request, harvest) == cids
    assert rankers[1].rank(harvest.items, request, harvest) == list(reversed(cids))
    by_title = sorted(range(5), key=lambda i: harvest.items[i]["title"])
    assert rankers[2].rank(harvest.items, request, harvest) == [cids[i] for i in by_title]
    # Groupes passés en indices de la concaténation des sources, année de la
    # collecte, sources et plan transmis.
    a = next(i for i, it in enumerate(harvest.items)
             if it["title"] == "Le discours numérique en question")
    assert sorted(calls[0]["groups"][a]) == [0, 4]      # HAL 0, CrossRef 3 + 1
    assert calls[0]["year"] == 2026 and calls[0]["n_sources"] == 2 and calls[0]["plan"]
    assert [c["mode"] for c in calls] == ["native", "local", "local"]


def test_adapter_never_mutates_protocol_items(monkeypatch):
    mod = types.ModuleType("classement_mutant_opt15")

    def rank_items(items, request=None, harvest=None):
        for it in items:
            it["score_local"] = 1.0          # interdit : invariant 1
        return list(range(len(items)))

    mod.rank_items = rank_items
    monkeypatch.setitem(sys.modules, "classement_mutant_opt15", mod)
    rankers, _ = R.resolve_rankers(["local"], module_name="classement_mutant_opt15")
    snap = T.sample_snapshot()
    request, harvest = P.reconstruct(snap)
    rankers[0].rank(harvest.items, request, harvest)
    assert all("score_local" not in it for it in harvest.items)


def test_eval_rankers_hook_has_priority(monkeypatch):
    mod = types.ModuleType("classement_crochet_opt15")
    mod.eval_rankers = lambda: {"native": lambda items, request, harvest: [0, 1],
                                "bm25": lambda items, request, harvest: [1, 0]}
    mod.rank_records = lambda *a, **k: pytest.fail("crochet ignoré")
    monkeypatch.setitem(sys.modules, "classement_crochet_opt15", mod)
    rankers, notes = R.resolve_rankers(["native", "local"], module_name="classement_crochet_opt15")
    assert [r.name for r in rankers] == ["native", "local:bm25"] and not notes


def test_generic_discovery_with_variants_and_unknown_required_parameter(monkeypatch):
    mod = types.ModuleType("classement_generique_opt15")
    mod.METHODS = ["lexical", "bm25"]

    def rank(corpus_items, query=None, method="lexical"):
        order = list(range(len(corpus_items)))
        return order if method == "lexical" else order[::-1]

    mod.rank = rank
    monkeypatch.setitem(sys.modules, "classement_generique_opt15", mod)
    rankers, _ = R.resolve_rankers(["local"], module_name="classement_generique_opt15")
    assert [r.name for r in rankers] == ["local:lexical", "local:bm25"]
    items = [{"itemType": "book", "title": t} for t in ("a", "b", "c")]
    cids = C.candidate_ids(items)
    assert rankers[1].rank(items, None, None) == cids[::-1]

    def needs_mode(records, sources, *, mode):
        return []
    with pytest.raises(R.AdapterError):
        R.call_flexible(needs_mode, items, None, None)


def test_real_ranking_module_if_present():
    """Adaptateur contre ``utils.ranking`` réel (OPT-13) quand il existe."""
    rankers, notes = R.resolve_rankers(["native", "local"])
    try:
        import utils.ranking  # noqa: F401
    except ImportError:
        assert [r.name for r in rankers] == ["native"] and notes
        return
    assert rankers[0].name == "native" and rankers[0].version != R.INTERLEAVE_VERSION
    assert any(r.name.startswith("local") for r in rankers)
    snap = T.sample_snapshot()
    request, harvest = P.reconstruct(snap)
    cids = C.candidate_ids(harvest.items)
    for ranker in rankers:
        order = ranker.rank(harvest.items, request, harvest)
        assert sorted(order) == sorted(cids), ranker.name


# === Normalisation des sorties ===

class _Entry:
    def __init__(self, index):
        self.index = index


class _Result:
    def __init__(self, entries):
        self.entries = entries


@pytest.mark.parametrize("make", [
    lambda items, cids: [2, 0, 1],
    lambda items, cids: [cids[2], cids[0], cids[1]],
    lambda items, cids: [items[2], items[0], items[1]],
    lambda items, cids: [(items[2], 0.9), (items[0], 0.5), (1, 0.1)],
    lambda items, cids: {"entries": [{"index": 2}, {"index": 0}, {"index": 1}]},
    lambda items, cids: _Result([_Entry(2), _Entry(0), _Entry(1)]),
    lambda items, cids: [0.2, 0.1, 0.9],                       # scores alignés
    lambda items, cids: [dict(items[2]), dict(items[0]), dict(items[1])],   # copies
    lambda items, cids: [2, 0, 0, 1],                          # doublon ignoré
], ids=["indices", "cids", "notices", "couples", "dict", "objet", "scores", "copies", "doublon"])
def test_normalize_ranking_shapes(make):
    items = [{"itemType": "book", "title": t} for t in ("a", "b", "c")]
    cids = C.candidate_ids(items)
    assert R.normalize_ranking(make(items, cids), items, cids) == [cids[2], cids[0], cids[1]]


@pytest.mark.parametrize("bad", [None, "texte", {"autre": 1}, [7], ["inconnu"], [True],
                                 [{"titre": "sans lien"}]])
def test_normalize_ranking_rejects_unknown_outputs(bad):
    items = [{"itemType": "book", "title": t} for t in ("a", "b")]
    with pytest.raises(R.AdapterError):
        R.normalize_ranking(bad, items, C.candidate_ids(items))


def test_external_ranker_spec(monkeypatch):
    mod = types.ModuleType("classeur_externe_opt15")
    mod.inverse = lambda items, request, harvest: list(range(len(items)))[::-1]
    monkeypatch.setitem(sys.modules, "classeur_externe_opt15", mod)
    ranker = R.load_external("test=classeur_externe_opt15:inverse")
    items = [{"itemType": "book", "title": t} for t in ("a", "b")]
    assert ranker.rank(items, None, None) == C.candidate_ids(items)[::-1]
    with pytest.raises(R.AdapterError):
        R.load_external("sans_forme")


# === Identifiants de candidats ===

def test_candidate_ids_are_stable_and_order_independent():
    snap = T.sample_snapshot()
    items = snap["items"]
    ids = C.candidate_ids(items)
    assert len(set(ids)) == len(ids)
    rev = C.candidate_ids(list(reversed(items)))
    assert rev == list(reversed(ids))
    assert all(re.fullmatch(r"c[0-9a-f]{10}(-\d+)?", cid) for cid in ids)


def test_candidate_ids_collisions_are_resolved_by_content():
    a = T.item("Même titre", year="2020")
    b = dict(a, abstractNote="autre contenu")
    ids = C.candidate_ids([a, b])
    assert ids[0] != ids[1] and ids[1].split("-")[0] == ids[0].split("-")[0]
    assert C.candidate_ids([b, a]) == [ids[1], ids[0]]

