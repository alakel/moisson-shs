# -*- coding: utf-8 -*-
"""
OPT-15 : non-régression des constats de revue (aucun accès réseau).

- le gate ne peut conclure « activer » que pour la variante qu'applique
  réellement ``MOISSON_RANKING=local`` (pondération lexicale par défaut du
  module, que le moteur ne surcharge pas), dans une seule version du code,
  identique au module actuel ; le verdict nomme ce qu'il valide ;
- relancer ``sheets.py generate`` (ou ``fusions``, ``dois``) ne perd aucun
  jugement ni verdict saisi : report d'office A sur A et B sur B, copie de
  sauvegarde, refus d'écraser sans ``--force`` ;
- une fiche malformée (champ en trop, encodage non UTF-8, fichier absent)
  est signalée nommément, jamais par une trace Python ;
- une référence connue présente sous plusieurs notices est comptée à la
  meilleure position de ses notices ;
- l'acquisition des références connues ne dépend d'aucune variante.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

# Import anticipé : urllib3 sonde IPv6 à l'import (bind local, sans connexion) ;
# la passe floue de la déduplication importe requests paresseusement.
import requests  # noqa: E402,F401

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from eval import common as C  # noqa: E402
from eval import decide as D  # noqa: E402
from eval import metrics as M  # noqa: E402
from eval import rankers as R  # noqa: E402
from eval import sheets as S  # noqa: E402
from eval import synthetic as T  # noqa: E402

TITLES = ["Le discours numérique en question", "Pratiques numériques des étudiants",
          "Histoire de la presse", "Digital discourse analysis", "Une thèse sur les forums"]

DEPLOYED = {"available": True, "module": "utils.ranking", "variante": "local:simple",
            "lexical_model": "simple", "version": "opt13-t", "empreinte": "0123456789ab"}


# === Aides : métriques synthétiques ===

def _rk(ndcg: Optional[float], p: Optional[float], version: str = "opt13-t",
        fingerprint: Optional[str] = "0123456789ab") -> Dict:
    return {"ndcg_at_k": ndcg, "p_at_k": p, "r_at_k": 0.5, "non_juges": 0, "complet": True,
            "relevant_top_k": [], "version": version, "empreinte": fingerprint}


def _q(qid: str, subset: str, rankers: Dict[str, Dict], refs: Optional[List[Dict]] = None) -> Dict:
    details = refs or []
    return {"qid": qid, "set": subset, "rankers": rankers,
            "known_refs": {"n": len(details), "acquired": sum(1 for d in details if d.get("cid")),
                           "details": details, "rank_recall": {}}}


def _metrics(per_query: List[Dict], tuning: Dict[str, float]) -> Dict:
    return {"campagne": "test", "parameters": {"k": 20}, "per_query": per_query,
            "aggregate": {"reglage": {"rankers": {
                n: {"ndcg_at_k": v, "n_complete": 8, "n_queries": 8} for n, v in tuning.items()}}},
            "fusions": {"replay": {"false_merges": [], "negatives": 66}},
            "dois": {"replay": {"available": True, "wrong_dois": [], "negatives": 33,
                                "accepted_ok": 18, "positives": 18}}}


def _bm25_wins_tuning_simple_fails_decision() -> Dict:
    """Cas du relecteur : bm25 meilleur au réglage et +20 % en décision ;
    simple (la variante déployée) en baisse de nDCG et de P@20."""
    rows = [_q(f"R{i:02d}", "reglage", {"native": _rk(.5, .5), "local:simple": _rk(.48, .5),
                                        "local:bm25": _rk(.6, .6)}) for i in range(1, 9)]
    rows += [_q(f"D{i:02d}", "decision", {"native": _rk(.5, .5), "local:simple": _rk(.47, .45),
                                          "local:bm25": _rk(.6, .6)}) for i in range(1, 17)]
    return _metrics(rows, {"native": .5, "local:simple": .48, "local:bm25": .6})


def _status(report: Dict, prefix: str) -> str:
    return next(c["statut"] for c in report["criteres"] if c["critere"].startswith(prefix))


# === Constats 1 et 4 : le gate porte sur la variante déployée ===

def test_bm25_winning_tuning_never_activates_while_deployed_simple_fails():
    report = D.decide(_bm25_wins_tuning_simple_fails_decision(), deployed=DEPLOYED)
    assert report["variante"] == "local:simple"
    assert report["verdict"] == D.VERDICT_OFF
    assert "MOISSON_RANKING=native" in report["consequence"]
    assert report["reglage"]["meilleure"] == "local:bm25"
    assert "local:bm25" in D.render_markdown(report)


def test_forced_variant_not_applied_by_moisson_ranking_cannot_activate():
    report = D.decide(_bm25_wins_tuning_simple_fails_decision(), variant="local:bm25",
                      deployed=DEPLOYED)
    assert report["variante"] == "local:bm25"
    assert _status(report, "variante évaluée") == D.UNKNOWN
    assert report["verdict"] == D.VERDICT_UNKNOWN
    assert "ne l'applique pas" in report["consequence"]
    assert "classement_valide" not in report


def test_without_deployed_variant_in_rankings_verdict_cannot_be_on():
    metrics = _bm25_wins_tuning_simple_fails_decision()
    other = dict(DEPLOYED, variante="local:autre", lexical_model="autre")
    report = D.decide(metrics, deployed=other)
    assert report["variante"] == "local:bm25"            # repli : meilleure du réglage
    assert report["verdict"] == D.VERDICT_UNKNOWN
    missing = D.decide(metrics, deployed={"available": False, "error": "ImportError: x"})
    assert missing["verdict"] == D.VERDICT_UNKNOWN


def test_on_verdict_names_the_validated_ranking():
    rows = [_q(f"D{i:02d}", "decision", {"native": _rk(.5, .5), "local:simple": _rk(.6, .55)})
            for i in range(1, 17)]
    report = D.decide(_metrics(rows, {"local:simple": .6}), deployed=DEPLOYED)
    assert report["verdict"] == D.VERDICT_ON
    assert report["classement_valide"] == {"variante": "local:simple", "lexical_model": "simple",
                                           "version": "opt13-t", "empreinte": "0123456789ab"}
    for needle in ("MOISSON_RANKING=local", "local:simple", "simple", "opt13-t", "0123456789ab"):
        assert needle in report["consequence"]


def test_deployed_ranking_is_read_from_the_ranking_module():
    import utils.ranking as ranking

    dep = D.deployed_ranking()
    assert dep["available"] is True
    assert dep["variante"] == f"local:{ranking.DEFAULT_LEXICAL_MODEL}"
    assert dep["lexical_model"] == ranking.DEFAULT_LEXICAL_MODEL
    assert dep["version"] == ranking.RANKING_VERSION
    assert re.fullmatch(r"[0-9a-f]{12}", dep["empreinte"])
    assert dep["empreinte"] == R.module_fingerprint(ranking)
    assert D.deployed_ranking("module_de_classement_absent")["available"] is False


def test_engine_does_not_override_the_lexical_model():
    """Hypothèse de ``deployed_ranking`` : le moteur appelle ``rank_harvest``
    sans pondération lexicale (défaut du module). Si le moteur en transmet
    une un jour, ``deployed_ranking`` doit être revu."""
    import ast

    import engine.orchestrator as orchestrator
    import utils.ranking as ranking

    tree = ast.parse(Path(orchestrator.__file__).read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "id", getattr(node.func, "attr", None)) == "rank_harvest"]
    assert calls, "appel de rank_harvest introuvable dans engine/orchestrator.py"
    for call in calls:
        assert "lexical_model" not in {kw.arg for kw in call.keywords}
        assert not any(isinstance(kw.value, ast.Dict) for kw in call.keywords if kw.arg is None)
    default = inspect.signature(ranking.rank_harvest).parameters["lexical_model"].default
    assert default == ranking.DEFAULT_LEXICAL_MODEL


# === Constat 5 : version du classement consignée et vérifiée ===

def _decision_rows(**overrides) -> List[Dict]:
    rows = []
    for i in range(1, 17):
        simple = _rk(.6, .55, **overrides) if i == 1 else _rk(.6, .55)
        rows.append(_q(f"D{i:02d}", "decision", {"native": _rk(.5, .5), "local:simple": simple}))
    return rows


def test_mixed_versions_make_the_verdict_undetermined():
    report = D.decide(_metrics(_decision_rows(version="opt13-0"), {"local:simple": .6}),
                      deployed=DEPLOYED)
    assert _status(report, "une seule version") == D.UNKNOWN
    assert report["verdict"] == D.VERDICT_UNKNOWN
    assert "mélangées" in next(c["detail"] for c in report["criteres"]
                               if c["critere"].startswith("une seule version"))


def test_missing_or_changed_code_fingerprint_makes_the_verdict_undetermined():
    rows = [_q(f"D{i:02d}", "decision", {"native": _rk(.5, .5, fingerprint=None),
                                         "local:simple": _rk(.6, .55, fingerprint=None)})
            for i in range(1, 17)]
    assert D.decide(_metrics(rows, {"local:simple": .6}),
                    deployed=DEPLOYED)["verdict"] == D.VERDICT_UNKNOWN
    changed = dict(DEPLOYED, empreinte="ffffffffffff")
    ok_rows = [_q(f"D{i:02d}", "decision", {"native": _rk(.5, .5), "local:simple": _rk(.6, .55)})
               for i in range(1, 17)]
    report = D.decide(_metrics(ok_rows, {"local:simple": .6}), deployed=changed)
    assert report["verdict"] == D.VERDICT_UNKNOWN
    assert "a changé" in next(c["detail"] for c in report["criteres"]
                              if c["critere"].startswith("une seule version"))


def test_metrics_copy_version_fingerprint_and_description(tmp_path):
    snap = T.sample_snapshot("D01", "decision")
    ranker = T.fixed_ranker("local:simple", TITLES)
    ranker.fingerprint = "abcdefabcdef"
    pool = T.pool_for(snap, [ranker], depth=5)
    assert pool["rankers"]["local:simple"]["fingerprint"] == "abcdefabcdef"
    q = M.query_metrics(pool, {}, k=3)
    r = q["rankers"]["local:simple"]
    assert (r["version"], r["empreinte"]) == ("test", "abcdefabcdef")
    agg = M.aggregate([q], "decision")["rankers"]["local:simple"]
    assert agg["versions"] == ["test (abcdefabcdef)"]
    md = M.render_markdown({"campagne": "x", "parameters": {"k": 3, "threshold": 2, "gain": "exp"},
                            "n_judgments_a": 0, "n_judgments_b": 0, "judgment_errors": [],
                            "per_query": [q], "aggregate": {s: M.aggregate([q], s) for s in C.SETS},
                            "agreement": None,
                            "fusions": {"replay": None, "harvests": {}, "verification": None},
                            "dois": {"replay": None, "reports": {"files": 0}, "verification": None}})
    assert "test (abcdefabcdef)" in md


def test_ranking_module_rankers_carry_the_source_fingerprint():
    import utils.ranking as ranking

    native, local, message = R.discover_ranking_module()
    assert message is None and native is not None and local
    expected = R.module_fingerprint(ranking)
    assert native.fingerprint == expected and all(r.fingerprint == expected for r in local)


# === Constat 6 : référence connue sous plusieurs notices ===

def test_known_reference_uses_the_best_ranked_of_its_notices():
    ref = {"titre": "Ouvrage X", "doi": "10.1234/x", "ids": ["hal:halshs-00000001"]}
    all_keys = {"cDOI": ["doi:10.1234/x"], "cHAL": ["hal:halshs-00000001"]}
    filler = [f"f{i}" for i in range(60)]
    rankings = {"native": ["cHAL"] + filler[:30] + ["cDOI"] + filler[30:],
                "local:simple": filler[:40] + ["cHAL", "cDOI"] + filler[40:]}
    kr = M.known_refs([ref], all_keys, rankings)
    d = kr["details"][0]
    assert d["cids"] == ["cDOI", "cHAL"] and d["cid"] == "cDOI"
    assert d["positions"] == {"native": 1, "local:simple": 41}
    assert kr["rank_recall"]["native"]["20"] == pytest.approx(1.0)
    q = {"qid": "D99", "known_refs": kr,
         "rankers": {"native": {"r_at_k": None}, "local:simple": {"r_at_k": None}}}
    losses = D.recall_losses([q], "local:simple", 20)
    assert losses == [{"requete": "D99", "type": "reference_connue", "titre": "Ouvrage X",
                       "rang_reference": 1, "rang_variante": 41}]


def test_one_key_shared_by_two_notices_counts_both():
    ref = {"titre": "Y", "doi": "10.1/y"}
    kr = M.known_refs([ref], {"a": ["doi:10.1/y"], "b": ["doi:10.1/y"]},
                      {"native": ["z", "b", "a"]})
    assert kr["details"][0]["cids"] == ["a", "b"] and kr["details"][0]["positions"]["native"] == 2


# === Constat 8 : acquisition indépendante de la variante ===

def test_acquisition_and_comparable_queries_without_a_chosen_variant():
    refs = [{"titre": "A", "cid": "c1", "positions": {}}, {"titre": "B", "cid": None, "positions": {}}]
    rows = [_q(f"D{i:02d}", "decision", {"native": _rk(None, None)}, refs) for i in range(1, 17)]
    report = D.decide(_metrics(rows, {}), deployed=DEPLOYED)
    assert report["variante"] is None and report["verdict"] == D.VERDICT_UNKNOWN
    assert report["acquisition"] == {"references": 32, "acquises": 16, "requetes": 16}
    detail = next(c["detail"] for c in report["criteres"] if "comparables" in c["critere"])
    assert "variante non choisie" in detail and "16 requête(s)" in detail
    md = D.render_markdown(report)
    assert "16 référence(s) connue(s) moissonnée(s) sur 32" in md


# === Constat 7 : référence D09 ===

def test_d09_reference_keeps_only_the_identifier_with_identical_title():
    doc = C.load_queries()
    d09 = next(q for q in doc["queries"] if q["id"] == "D09")
    first = d09["references_connues"][0]
    assert C.ref_keys(first) == ["hal:halshs-01145718"]
    assert "10.3917/ina.cardo.2015.01.0009" in first["note"]      # écart consigné
    all_keys = [k for ref in d09["references_connues"] for k in C.ref_keys(ref)]
    assert "doi:10.3917/ina.cardo.2015.01.0009" not in all_keys


# === Constat 3 : fiches malformées signalées nommément ===

def test_extra_field_is_a_named_error_not_a_crash(tmp_path):
    sheet = tmp_path / "a.csv"
    sheet.write_text("requete_id;candidat_id;jugement;remarque\nD01;c1;2;bien; mais daté\n"
                     "D01;c2;3;\n", encoding="utf-8")
    grades, errors = M.read_judgments([sheet])
    assert grades == {("D01", "c2"): 3}
    assert len(errors) == 1 and "a.csv ligne 2" in errors[0] and "champ" in errors[0]
    with pytest.raises(C.EvalError, match="ligne 2"):
        S.read_sheet(sheet)
    ver = tmp_path / "v.csv"
    ver.write_text("fusion_id;verdict;remarque\nF001;1;a;b\nF002;0;\n", encoding="utf-8")
    out = M.verification_precision(ver, "fusion_id")
    assert (out["yes"], out["no"]) == (0, 1) and len(out["errors"]) == 1


def test_non_utf8_sheet_asks_for_csv_utf8(tmp_path):
    sheet = tmp_path / "b.csv"
    sheet.write_bytes("requete_id;candidat_id;jugement;remarque\nD01;c1;2;thèse\n".encode("cp1252"))
    with pytest.raises(C.EvalError) as exc:
        M.read_judgments([sheet])
    assert "b.csv ligne 2" in str(exc.value) and "CSV UTF-8" in str(exc.value)


def test_metrics_cli_reports_a_bad_sheet_without_traceback(tmp_path, capsys):
    out_dir = tmp_path / "2026-09-29"
    snap = T.sample_snapshot("D01", "decision")
    C.write_json(out_dir / C.POOLS / "D01.json", T.pool_for(snap, [T.fixed_ranker("native", TITLES)]))
    bad = tmp_path / "bad.csv"
    bad.write_bytes("requete_id;candidat_id;jugement\nD01;c1;é\n".encode("cp1252"))
    code = M.main(["--root", str(tmp_path), "--date", "2026-09-29", "--juge-a", str(bad),
                   "--sans-rejeu"])
    assert code == 2 and "CSV UTF-8" in capsys.readouterr().err


# === Constat 2 : aucun jugement perdu à la régénération des fiches ===

def _campaign(tmp_path, fraction: float = 1.0):
    out_dir = tmp_path / "2026-09-29"
    snap = T.sample_snapshot("D01", "decision")
    pool = T.pool_for(snap, [T.fixed_ranker("native", TITLES)], depth=5)
    C.write_json(out_dir / C.POOLS / "D01.json", pool)
    S.generate(out_dir, [pool], fraction=fraction)
    return out_dir, pool


def _fill(path: Path, value: str, only: Optional[int] = None) -> None:
    rows = S.read_sheet(path)
    for n, row in enumerate(rows):
        if only is None or n < only:
            row["jugement"] = value
    C.write_text(path, S.rows_to_csv(rows, S.COLUMNS), bom=True)


def _grades(path: Path) -> Dict:
    return M.read_judgments([path])[0]


def test_regenerating_keeps_spreadsheet_judgments_of_both_sheets(tmp_path):
    out_dir, pool = _campaign(tmp_path)
    a, b = out_dir / C.SHEETS / "jugement_A.csv", out_dir / C.SHEETS / "jugement_B_echantillon.csv"
    _fill(a, "2")
    _fill(b, "1", only=3)
    before_a, before_b = _grades(a), _grades(b)
    S.generate(out_dir, [pool], fraction=1.0)
    assert _grades(a) == before_a and len(before_a) == 5
    assert _grades(b) == before_b and len(before_b) == 3        # jamais A sur B
    backups = sorted(p.name for p in (out_dir / C.SHEETS / S.BACKUPS).glob("*.csv"))
    assert any(n.startswith("jugement_A_") for n in backups)
    assert any(n.startswith("jugement_B_echantillon_") for n in backups)
    html = (out_dir / C.SHEETS / "jugement_B_echantillon.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r'id="sheet-data">(.*?)</script>', html, re.S).group(1))
    assert sum(1 for r in data["rows"] if r["jugement"]) == 3


def test_a_judgments_are_never_carried_onto_b(tmp_path):
    out_dir, pool = _campaign(tmp_path)
    _fill(out_dir / C.SHEETS / "jugement_A.csv", "3")
    S.generate(out_dir, [pool], fraction=1.0)
    assert _grades(out_dir / C.SHEETS / "jugement_B_echantillon.csv") == {}


def test_html_exports_are_carried_into_the_regenerated_sheets(tmp_path):
    out_dir, pool = _campaign(tmp_path)
    rows = S.read_sheet(out_dir / C.SHEETS / "jugement_A.csv")
    rows[0]["jugement"] = "3"
    C.write_text(out_dir / C.SHEETS / "jugement_A_rempli.csv", S.rows_to_csv(rows, S.COLUMNS),
                 bom=True)
    S.generate(out_dir, [pool], fraction=1.0)
    assert _grades(out_dir / C.SHEETS / "jugement_A.csv") == {("D01", rows[0]["candidat_id"]): 3}


def test_regeneration_refuses_to_drop_judgments_without_force(tmp_path):
    out_dir, pool = _campaign(tmp_path)
    a = out_dir / C.SHEETS / "jugement_A.csv"
    _fill(a, "2")
    raw = a.read_bytes()
    with pytest.raises(C.EvalError, match="--force"):
        S.generate(out_dir, [], fraction=1.0)              # comme --ids sans cette requête
    assert a.read_bytes() == raw                           # rien n'a été écrit
    S.generate(out_dir, [], fraction=1.0, force=True)
    assert _grades(a) == {}
    saved = sorted((out_dir / C.SHEETS / S.BACKUPS).glob("jugement_A_*.csv"))
    assert saved and len(_grades(saved[-1])) == 5


def test_second_sheet_keeps_already_judged_candidates_outside_the_new_sample():
    snap = T.sample_snapshot("D01", "decision")
    pool = T.pool_for(snap, [T.fixed_ranker("native", TITLES)], depth=5)
    sample = set(S.second_sample(pool["pool"], S.DEFAULT_SEED, "D01", 0.2))
    outside = next(c for c in sorted(pool["pool"]) if c not in sample)
    rows = S.judgment_rows([pool], salt="B", fraction=0.2,
                           prefill={("D01", outside): {"jugement": "2", "remarque": ""},
                                    ("D01", "absent"): {"jugement": "1", "remarque": ""}})
    cids = {r["candidat_id"] for r in rows}
    assert cids == sample | {outside}
    assert next(r for r in rows if r["candidat_id"] == outside)["jugement"] == "2"


def test_missing_prefill_file_is_reported_without_traceback(tmp_path, capsys):
    out_dir, _pool = _campaign(tmp_path)
    code = S.main(["--root", str(tmp_path), "--date", "2026-09-29", "generate", "--prefill",
                   str(out_dir / C.SHEETS / "jugement_A_rempli.csv")])
    assert code == 2 and "introuvable" in capsys.readouterr().err


def test_verification_verdicts_are_carried_by_row_content(tmp_path):
    out_dir = tmp_path / "2026-09-29"
    C.write_json(out_dir / C.SNAPSHOTS / "D01.json", T.sample_snapshot())
    path = S.write_fusions(out_dir, n_strong=5, seed=1, n_fuzzy=5)
    rows = S.read_sheet(path)
    rows[0]["verdict"], rows[0]["remarque"] = "1", "vu"
    C.write_text(path, S.rows_to_csv(rows, S.FUSION_COLUMNS), bom=True)
    S.write_fusions(out_dir, n_strong=5, seed=1, n_fuzzy=5)
    again = S.read_sheet(path)
    assert (again[0]["verdict"], again[0]["remarque"]) == ("1", "vu")
    assert list((out_dir / C.SHEETS / S.BACKUPS).glob("verification_fusions_*.csv"))
    # La fusion disparaît des moissons : refus sans --force.
    (out_dir / C.SNAPSHOTS / "D01.json").unlink()
    with pytest.raises(C.EvalError, match="verdict"):
        S.write_fusions(out_dir, n_strong=5, seed=1, n_fuzzy=5)
    assert S.read_sheet(path)[0]["verdict"] == "1"
    # DOI : appariement sur (requête, candidat, DOI proposé).
    report = {"eval": {"qid": "D01", "cids": ["c1"]}, "entries": [
        {"index": 0, "status": "accepte", "doi": "10.1/ok", "title": "T", "year": "2020"}]}
    C.write_json(out_dir / C.DOI_DIR / "D01.doi_report.json", report)
    dpath = S.write_dois(out_dir)
    drows = S.read_sheet(dpath)
    drows[0]["verdict"] = "0"
    C.write_text(dpath, S.rows_to_csv(drows, S.DOI_COLUMNS), bom=True)
    S.write_dois(out_dir)
    assert S.read_sheet(dpath)[0]["verdict"] == "0"
