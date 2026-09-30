# -*- coding: utf-8 -*-
"""
OPT-15 : fichier de requêtes du protocole d'évaluation
(``scripts/eval/queries_shs.json``) et sa validation.

Au moins 24 requêtes, dont au moins 8 de réglage et 16 de décision ; toutes
les catégories du plan couvertes par le jeu de décision ; jamais Google
Scholar ; variantes anglaises explicites pour les concepts FR/EN ; références
connues à identifiants normalisés et vérification consignée, liste vide
toujours signalée.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from eval import common as C  # noqa: E402

DOC = C.load_queries()
QUERIES = DOC["queries"]


def test_file_is_valid():
    assert C.validate_queries(C.read_json(C.QUERIES_FILE)) == []


def test_counts_and_sets():
    tuning = [q for q in QUERIES if q["set"] == C.SET_TUNING]
    decision = [q for q in QUERIES if q["set"] == C.SET_DECISION]
    assert len(QUERIES) >= 24
    assert len(tuning) >= 8
    assert len(decision) >= 16
    assert len({q["id"] for q in QUERIES}) == len(QUERIES)


def test_every_category_is_covered_by_the_decision_set():
    decision_cats = {c for q in QUERIES if q["set"] == C.SET_DECISION for c in q["categories"]}
    assert set(C.CATEGORIES) <= decision_cats


def test_both_source_groups_and_never_scholar():
    groups = {q["sources"] for q in QUERIES}
    assert groups == {"francophone", "all"}
    text = C.QUERIES_FILE.read_text(encoding="utf-8").lower()
    assert "google_scholar" not in text and "serpapi" not in text


def test_concepts_fr_en_carry_an_explicit_english_variant():
    for q in QUERIES:
        if "concept_fr_en" in q["categories"]:
            assert q["query_variants"]["en"].strip()
            assert q["sources"] == "all", q["id"]


def test_exact_phrases_and_restrictive_periods_are_real():
    phrases = [q for q in QUERIES if "expression_exacte" in q["categories"]]
    assert phrases and all(q["query"].count('"') == 2 for q in phrases)
    periods = [q for q in QUERIES if "periode_restrictive" in q["categories"]]
    assert periods and all(q.get("from_date") or q.get("to_date") for q in periods)


def test_known_references_are_normalized_and_traced():
    n_refs = 0
    for q in QUERIES:
        refs = q["references_connues"]
        if not refs:
            assert q["references_note"].strip(), q["id"]
        for ref in refs:
            n_refs += 1
            assert ref["keys"], ref
            assert all(k.split(":", 1)[0] in ("doi", "hal") for k in ref["keys"])
            assert ref["verification"]
            # Clés déjà normalisées (minuscules pour les DOI, sans version HAL).
            for key in ref["keys"]:
                kind, value = key.split(":", 1)
                if kind == "doi":
                    assert value == value.lower() and value.startswith("10.")
                else:
                    assert not value.endswith(("v1", "v2"))
    assert n_refs >= 30


def test_validation_detects_errors(tmp_path):
    doc = copy.deepcopy(C.read_json(C.QUERIES_FILE))
    q = doc["queries"][0]
    q["sources"] = "scholar"
    q["references_connues"] = []
    q.pop("references_note", None)
    q["categories"].append("inconnue")
    doc["queries"][1]["id"] = doc["queries"][0]["id"]
    doc["queries"][2]["references_connues"] = [{"doi": "pas-un-doi", "titre": "x",
                                               "verification": "test"}]
    errors = "\n".join(C.validate_queries(doc))
    assert "non admis" in errors
    assert "non signalée" in errors
    assert "catégorie inconnue" in errors
    assert "en double" in errors
    assert "identifiant invalide" in errors
    doc_path = tmp_path / "invalide.json"
    doc_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(C.EvalError):
        C.load_queries(doc_path)


def test_ref_keys_alternatives():
    ref = {"doi": "https://doi.org/10.3917/ABC.01", "hal": "halshs-01145718v2",
           "ids": ["doi:10.1/x", "hal:tel-00737637"]}
    assert C.ref_keys(ref) == ["doi:10.3917/abc.01", "hal:halshs-01145718",
                               "doi:10.1/x", "hal:tel-00737637"]


def test_select_queries():
    assert [q["id"] for q in C.select_queries(DOC, ["r01", "D02"])] == ["R01", "D02"]
    assert all(q["set"] == C.SET_TUNING for q in C.select_queries(DOC, subset=C.SET_TUNING))
    with pytest.raises(C.EvalError):
        C.select_queries(DOC, ["Z99"])
