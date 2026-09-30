# -*- coding: utf-8 -*-
"""
OPT-15 : métriques du protocole d'évaluation, sur des cas calculés à la main
(aucun accès réseau).

P@k, nDCG@k (gains exponentiel et linéaire), rappel relatif, références
connues (acquisition et rang), kappa de Cohen pondéré (quadratique,
linéaire, non pondéré), lecture des fiches remplies (BOM, séparateurs,
erreurs nommées), rejeu des jeux témoins (fusions, DOI) et calcul complet
sur une campagne synthétique.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

# Import anticipé : urllib3 sonde IPv6 à l'import (bind local, sans connexion) ;
# la passe floue de la déduplication importe requests paresseusement.
import requests  # noqa: E402,F401

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from eval import common as C  # noqa: E402
from eval import metrics as M  # noqa: E402
from eval import sheets as S  # noqa: E402
from eval import synthetic as T  # noqa: E402
from eval.rankers import resolve_rankers  # noqa: E402

LOG2_3 = math.log2(3)   # 1,584962…
LOG2_5 = math.log2(5)   # 2,321928…


# === P@k ===

def test_precision_at_k_hand_computed():
    grades = {"a": 3, "b": 0, "c": 2, "d": 1}
    # 2 pertinents (>= 2) parmi 4 : 0,5 ; un non jugé (e) compte comme non
    # pertinent et est dénombré.
    assert M.precision_at_k(["a", "b", "c", "d"], grades, k=4) == (0.5, 0)
    assert M.precision_at_k(["a", "e", "c", "d"], grades, k=4) == (0.5, 1)
    # Dénominateur k même si le classement est plus court : 1 / 4.
    assert M.precision_at_k(["a"], grades, k=4) == (0.25, 0)
    # Seuil 1 : a, c, d.
    assert M.precision_at_k(["a", "b", "c", "d"], grades, k=4, threshold=1) == (0.75, 0)


# === nDCG@k ===

def test_ndcg_exponential_gain_hand_computed():
    grades = {"a": 1, "b": 3, "c": 0}
    # DCG = (2^1 - 1)/log2(2) + (2^3 - 1)/log2(3) + 0 = 1 + 7/1,58496 = 5,41650
    # IDCG = 7/log2(2) + 1/log2(3) = 7 + 0,63093 = 7,63093 ; nDCG = 0,70981
    expected = (1 + 7 / LOG2_3) / (7 + 1 / LOG2_3)
    assert abs(expected - 0.70981) < 1e-4
    assert M.ndcg_at_k(["a", "b", "c"], grades, k=3, kind="exp") == pytest.approx(expected)
    # Ordre idéal : 1.
    assert M.ndcg_at_k(["b", "a", "c"], grades, k=3) == pytest.approx(1.0)


def test_ndcg_linear_gain_hand_computed():
    grades = {"a": 1, "b": 3, "c": 0}
    # DCG = 1 + 3/1,58496 = 2,89279 ; IDCG = 3 + 1/1,58496 = 3,63093 ; 0,79671
    expected = (1 + 3 / LOG2_3) / (3 + 1 / LOG2_3)
    assert abs(expected - 0.79671) < 1e-4
    assert M.ndcg_at_k(["a", "b", "c"], grades, k=3, kind="lin") == pytest.approx(expected)


def test_ndcg_ideal_uses_all_judgments_of_the_query_and_cuts_at_k():
    # Pool jugé : x=3 hors du classement ; classement [a, b] avec a=2, b=0.
    grades = {"a": 2, "b": 0, "x": 3, "y": 2}
    # k = 2 : DCG = 3/1 = 3 ; IDCG = 7/1 + 3/log2(3) = 8,89279 ; 0,33735
    expected = 3 / (7 + 3 / LOG2_3)
    assert M.ndcg_at_k(["a", "b"], grades, k=2) == pytest.approx(expected)
    # Un candidat non jugé au rang 1 ne rapporte rien.
    assert M.ndcg_at_k(["z", "a"], grades, k=2) == pytest.approx((3 / LOG2_3) / (7 + 3 / LOG2_3))


def test_ndcg_undefined_without_any_positive_judgment():
    assert M.ndcg_at_k(["a", "b"], {"a": 0, "b": 0}, k=2) is None
    assert M.ndcg_at_k(["a", "b"], {}, k=2) is None


def test_dcg_position_discount():
    # Rang 4 : log2(5).
    assert M.dcg([0, 0, 0, 2], k=4) == pytest.approx(3 / LOG2_5)
    assert M.dcg([0, 0, 0, 2], k=3) == 0


# === Rappel ===

def test_recall_at_k_relative_to_pool():
    grades = {"a": 3, "b": 0, "c": 2, "e": 2}
    assert M.recall_at_k(["a", "b"], grades, k=2) == pytest.approx(1 / 3)
    assert M.recall_at_k(["a", "c", "e"], grades, k=3) == pytest.approx(1.0)
    assert M.recall_at_k(["a"], {"a": 1}, k=1) is None


def test_known_refs_acquisition_and_rank_with_alternative_ids():
    refs = [
        {"titre": "Livre", "ids": ["doi:10.1/ed1", "doi:10.1/ed2"]},   # 2e édition présente
        {"titre": "Thèse", "hal": "tel-00737637"},                      # présente, rang 25
        {"titre": "Absente", "doi": "10.1/absent"},
    ]
    all_keys = {"c1": ["doi:10.1/ed2"], "c2": ["hal:tel-00737637", "url:x"], "c3": []}
    rankings = {"native": ["c1", "c3"] + [f"z{i}" for i in range(22)] + ["c2"],
                "local": ["c2", "c1"]}
    out = M.known_refs(refs, all_keys, rankings, ks=(20, 30))
    assert out["n"] == 3 and out["acquired"] == 2
    assert out["acquisition"] == pytest.approx(2 / 3)
    assert out["details"][0]["positions"] == {"native": 1, "local": 2}
    assert out["details"][1]["positions"] == {"native": 25, "local": 1}
    assert out["details"][2]["cid"] is None
    assert out["rank_recall"]["native"] == {"20": pytest.approx(1 / 3), "30": pytest.approx(2 / 3)}
    assert out["rank_recall"]["local"]["20"] == pytest.approx(2 / 3)
    empty = M.known_refs([], all_keys, rankings)
    assert empty["acquisition"] is None


# === Kappa pondéré ===

PAIRS = [(0, 0), (1, 1), (2, 2), (3, 3), (0, 1), (2, 3)]


def test_weighted_kappa_quadratic_hand_computed():
    # Po = (4 + 8/9 + 8/9) / 6 = 52/54 ; Pe = (232/9) / 36 = 232/324 ;
    # kappa = (312 - 232) / (324 - 232) = 80/92 = 20/23.
    assert M.weighted_kappa(PAIRS, weights="quadratic") == pytest.approx(20 / 23)


def test_weighted_kappa_linear_hand_computed():
    # Po = (4 + 2/3 + 2/3) / 6 = 8/9 ; Pe = (62/3) / 36 = 62/108 ;
    # kappa = (96 - 62) / (108 - 62) = 34/46 = 17/23.
    assert M.weighted_kappa(PAIRS, weights="linear") == pytest.approx(17 / 23)


def test_unweighted_kappa_hand_computed():
    # Po = 4/6 ; Pe = (2·1 + 1·2 + 2·1 + 1·2) / 36 = 8/36 ; kappa = 16/28 = 4/7.
    assert M.weighted_kappa(PAIRS, weights="none") == pytest.approx(4 / 7)


def test_kappa_edge_cases():
    assert M.weighted_kappa([(2, 2)]) is None                 # une seule paire
    assert M.weighted_kappa([(2, 2), (2, 2)]) is None         # aucune variabilité
    assert M.weighted_kappa([(0, 0), (3, 3)]) == pytest.approx(1.0)


def test_agreement_summary():
    a = {("Q", "x"): 3, ("Q", "y"): 0, ("Q", "z"): 2, ("Q", "w"): 1}
    b = {("Q", "x"): 3, ("Q", "y"): 2, ("Q", "z"): 2, ("R", "v"): 1}
    ag = M.agreement(a, b)
    assert ag["n_pairs"] == 3
    assert ag["accord_brut"] == pytest.approx(2 / 3)
    assert ag["accord_binaire"] == pytest.approx(2 / 3)
    assert ag["confusion"][0][2] == 1
    assert ag["desaccords_de_2_ou_plus"] == [{"requete_id": "Q", "candidat_id": "y", "A": 0, "B": 2}]


# === Lecture des fiches ===

def _write(path: Path, text: str, bom: bool = True) -> Path:
    path.write_bytes(("﻿" if bom else "").encode("utf-8") + text.encode("utf-8"))
    return path


def test_parse_grade():
    assert M.parse_grade(" 2 ") == 2
    assert M.parse_grade("3.0") == 3
    assert M.parse_grade("1,0") == 1
    assert M.parse_grade("") is None
    for bad in ("5", "-1", "2.5", "oui"):
        with pytest.raises(ValueError):
            M.parse_grade(bad)


def test_read_judgments_semicolon_bom_and_errors(tmp_path):
    csv_text = ("requete_id;candidat_id;titre;jugement;remarque\r\n"
                "D01;c1;Titre; 3 ;\r\n"
                "D01;c2;\"Titre; avec point-virgule\";0;ok\r\n"
                "D01;c3;Titre;;\r\n"
                "D01;c4;Titre;7;\r\n"
                "D02;c1;Titre;2,0;\r\n")
    grades, errors = M.read_judgments([_write(tmp_path / "a.csv", csv_text)])
    assert grades == {("D01", "c1"): 3, ("D01", "c2"): 0, ("D02", "c1"): 2}
    assert len(errors) == 1 and "c4" in errors[0] and "hors échelle" in errors[0]


def test_read_judgments_comma_separator_and_contradiction(tmp_path):
    first = _write(tmp_path / "a.csv", "requete_id,candidat_id,jugement\nD01,c1,2\n", bom=False)
    second = _write(tmp_path / "b.csv", "Requete_ID,Candidat_ID,Jugement\nD01,c1,1\n")
    grades, errors = M.read_judgments([first, second])
    assert grades == {("D01", "c1"): 1}
    assert len(errors) == 1 and "contradictoire" in errors[0]


def test_verification_precision(tmp_path):
    sheet = _write(tmp_path / "v.csv", "fusion_id;verdict\nF001;1\nF002;0\nF003;?\nF004;\nF005;oui\nF006;peut-être\n")
    out = M.verification_precision(sheet, "fusion_id")
    assert (out["yes"], out["no"], out["uncertain"]) == (2, 1, 1)
    assert out["precision"] == pytest.approx(2 / 3)
    assert out["erroneous"] == ["F002"] and len(out["errors"]) == 1
    assert M.verification_precision(tmp_path / "absent.csv", "fusion_id") is None


# === Jeux témoins (rejeu hors réseau) ===

def test_fusion_replay_on_the_dedup_fixture_has_no_false_merge():
    rep = M.fusion_replay()
    assert rep["negatives"] >= 66 and rep["positives"] >= 54
    assert rep["false_merges"] == []
    assert rep["unexpected_missed"] == []


def test_doi_replay_on_the_negative_set_has_no_wrong_doi():
    rep = M.doi_replay()
    assert rep["available"] is True
    assert rep["negatives"] >= 31
    assert rep["wrong_dois"] == []
    assert rep["accepted_ok"] == rep["positives"] > 0


def test_doi_replay_reports_a_missing_case_module(tmp_path):
    rep = M.doi_replay(tmp_path / "absent.py")
    assert rep["available"] is False and rep["error"]


# === Calcul complet sur une campagne synthétique ===

def _campaign(tmp_path, grades_a, grades_b=None):
    snap = T.sample_snapshot("D01", "decision")
    rankers = [T.fixed_ranker("native", ["Histoire de la presse", "Le discours numérique en question",
                                         "Pratiques numériques des étudiants"]),
               T.fixed_ranker("local:simple", ["Le discours numérique en question",
                                               "Digital discourse analysis",
                                               "Histoire de la presse"])]
    pool = T.pool_for(snap, rankers, depth=3)
    out_dir = tmp_path / "2026-09-29"
    C.write_json(out_dir / C.POOLS / "D01.json", pool)
    t2c = T.title_to_cid(pool)
    rows = S.judgment_rows([pool], salt="A")
    for row in rows:
        title = next(t for t, c in t2c.items() if c == row["candidat_id"])
        row["jugement"] = str(grades_a.get(title, ""))
    C.write_text(out_dir / C.SHEETS / "jugement_A_rempli.csv", S.rows_to_csv(rows, S.COLUMNS), bom=True)
    paths_b = []
    if grades_b:
        rows_b = S.judgment_rows([pool], salt="B", fraction=1.0)
        for row in rows_b:
            title = next(t for t, c in t2c.items() if c == row["candidat_id"])
            row["jugement"] = str(grades_b.get(title, ""))
        paths_b = [C.write_text(out_dir / C.SHEETS / "b.csv", S.rows_to_csv(rows_b, S.COLUMNS), bom=True)]
    return out_dir, pool, paths_b


def test_compute_end_to_end(tmp_path):
    grades = {"Le discours numérique en question": 3, "Digital discourse analysis": 2,
              "Histoire de la presse": 0, "Pratiques numériques des étudiants": 1}
    out_dir, pool, paths_b = _campaign(tmp_path, grades, grades)
    m = M.compute(out_dir, M.default_sheet(out_dir, "jugement_A"), paths_b, k=3,
                  replay=False)
    q = m["per_query"][0]
    assert q["n_candidates"] == 4 and q["n_judged"] == 4 and q["n_relevant"] == 2
    nat, loc = q["rankers"]["native"], q["rankers"]["local:simple"]
    # native : [presse 0, discours 3, pratiques 1] ; P@3 = 1/3.
    assert nat["p_at_k"] == pytest.approx(1 / 3) and nat["complet"]
    # DCG = 0 + 7/log2(3) + 1/log2(4) ; IDCG = 7 + 3/log2(3) + 1/2.
    ideal = 7 + 3 / LOG2_3 + 1 / 2
    assert nat["ndcg_at_k"] == pytest.approx((7 / LOG2_3 + 1 / 2) / ideal)
    # local : [discours 3, digital 2, presse 0] ; P@3 = 2/3.
    assert loc["p_at_k"] == pytest.approx(2 / 3)
    assert loc["ndcg_at_k"] == pytest.approx((7 + 3 / LOG2_3) / ideal)
    # Référence connue (DOI 10.1000/a) : acquise, rang 2 (native), 1 (local).
    kr = q["known_refs"]
    assert kr["acquired"] == 1 and kr["details"][0]["positions"] == {"native": 2, "local:simple": 1}
    agg = m["aggregate"]["decision"]["rankers"]
    assert agg["local:simple"]["p_at_k"] == pytest.approx(2 / 3)
    assert m["agreement"]["n_pairs"] == 4 and m["agreement"]["kappa_quadratique"] == pytest.approx(1.0)
    md = M.render_markdown(m)
    assert "Jeu « decision »" in md and "local:simple" in md


def test_compute_without_judgments_reports_none(tmp_path):
    out_dir, _pool, _ = _campaign(tmp_path, {})
    m = M.compute(out_dir, M.default_sheet(out_dir, "jugement_A"), k=3, replay=False)
    r = m["per_query"][0]["rankers"]["native"]
    assert r["p_at_k"] is None and r["ndcg_at_k"] is None and not r["complet"]
    assert m["agreement"] is None


def test_current_query_definition_prevails_for_references(tmp_path):
    out_dir, pool, _ = _campaign(tmp_path, {"Le discours numérique en question": 3})
    current = T.query_entry("D01", "decision", refs=[
        {"titre": "Absente", "doi": "10.9/absent", "verification": "test"}])
    m = M.compute(out_dir, M.default_sheet(out_dir, "jugement_A"), k=3, replay=False,
                  queries={"D01": current})
    kr = m["per_query"][0]["known_refs"]
    assert kr["n"] == 1 and kr["acquired"] == 0


def test_balance_counts():
    snap = T.sample_snapshot()
    rankers, _notes = resolve_rankers(["entrelacement"])
    pool = T.pool_for(snap, rankers, depth=5)
    bal = M.balance(pool["rankers"]["entrelacement"]["ranking"], pool["candidates"], k=5)
    assert sum(bal["langue"].values()) == 5
    assert bal["langue"] == {"en": 1, "fr": 4}
    assert bal["type"] == {"article": 3, "livre": 1, "these": 1}
    assert bal["anciennete"] == {"2010-2019": 2, "2020 et après": 2, "avant 2000": 1}
