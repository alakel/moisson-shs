# -*- coding: utf-8 -*-
"""
OPT-15 : rapport de décision (gate d'activation du classement local).

Cibles du plan appliquées au seul jeu de décision : gain relatif de nDCG@20
>= 10 %, P@20 sans baisse, baisses de rappel listées, zéro fausse fusion et
zéro DOI erroné sur les jeux témoins ; variante évaluée = celle qu'applique
``MOISSON_RANKING=local``, la meilleure du jeu de réglage étant signalée à
titre d'information ; « indéterminé » tant que les jugements manquent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from eval import decide as D  # noqa: E402


# Classement qu'appliquerait MOISSON_RANKING=local dans ces tests (le module
# réel est lu par ``deployed_ranking`` ; voir test_opt15_review.py).
DEPLOYED = {"available": True, "module": "utils.ranking", "variante": "local:simple",
            "lexical_model": "simple", "version": "opt13-t", "empreinte": "0123456789ab"}


def _ranker(ndcg: Optional[float], p: Optional[float], complete: bool = True,
            r: Optional[float] = 1.0, relevant: Optional[List[str]] = None,
            version: str = "opt13-t", fingerprint: Optional[str] = "0123456789ab") -> Dict:
    return {"ndcg_at_k": ndcg, "p_at_k": p, "complet": complete, "r_at_k": r,
            "relevant_top_k": relevant or [], "non_juges": 0 if complete else 3,
            "version": version, "empreinte": fingerprint}


def _decide(metrics: Dict, **kw) -> Dict:
    kw.setdefault("deployed", DEPLOYED)
    return D.decide(metrics, **kw)


def _query(qid: str, subset: str, rankers: Dict[str, Dict], refs=None) -> Dict:
    details = refs or []
    return {"qid": qid, "set": subset, "rankers": rankers,
            "known_refs": {"n": len(details), "acquired": sum(1 for d in details if d.get("cid")),
                           "details": details}}


def _metrics(decision: List[Dict], tuning: Optional[Dict[str, float]] = None,
             false_merges: int = 0, wrong_dois: int = 0) -> Dict:
    tuning = tuning or {"local:simple": 0.6}
    return {
        "campagne": "test", "parameters": {"k": 20},
        "per_query": decision,
        "aggregate": {"reglage": {"rankers": {
            name: {"ndcg_at_k": v, "n_complete": 8, "n_queries": 8}
            for name, v in tuning.items()}}},
        "fusions": {"replay": {"false_merges": [{"id": f"N{i}"} for i in range(false_merges)],
                               "negatives": 66}, "verification": None},
        "dois": {"replay": {"available": True, "wrong_dois": [{"cas": "x"}] * wrong_dois,
                            "negatives": 33, "accepted_ok": 18, "positives": 18},
                 "verification": None},
    }


def _decision_set(n: int, base: float, local: float, p_base: float = 0.5,
                  p_local: float = 0.5, variant: str = "local:simple", **kw) -> List[Dict]:
    return [_query(f"D{i:02d}", "decision",
                   {"native": _ranker(base, p_base), variant: _ranker(local, p_local, **kw)})
            for i in range(1, n + 1)]


def _status(report: Dict, prefix: str) -> str:
    return next(c["statut"] for c in report["criteres"] if c["critere"].startswith(prefix))


def test_activation_when_every_target_is_met():
    report = _decide(_metrics(_decision_set(16, 0.50, 0.56, 0.50, 0.55)))
    assert report["verdict"] == D.VERDICT_ON
    assert report["ndcg"]["gain_relatif"] == pytest.approx(0.12)
    assert "MOISSON_RANKING=local" in report["consequence"]


def test_gain_of_exactly_ten_percent_passes_and_below_fails():
    ok = _decide(_metrics(_decision_set(16, 0.50, 0.55)))
    assert ok["ndcg"]["gain_relatif"] == pytest.approx(0.10) and ok["verdict"] == D.VERDICT_ON
    ko = _decide(_metrics(_decision_set(16, 0.50, 0.549)))
    assert ko["verdict"] == D.VERDICT_OFF
    assert _status(ko, "gain relatif") == D.FAIL
    assert "native" in ko["consequence"]


def test_mean_p_at_20_decrease_blocks():
    report = _decide(_metrics(_decision_set(16, 0.50, 0.70, p_base=0.50, p_local=0.49)))
    assert report["verdict"] == D.VERDICT_OFF
    assert _status(report, "P@20") == D.FAIL


def test_false_merge_or_wrong_doi_blocks():
    assert _decide(_metrics(_decision_set(16, 0.5, 0.7), false_merges=1))["verdict"] == D.VERDICT_OFF
    assert _decide(_metrics(_decision_set(16, 0.5, 0.7), wrong_dois=1))["verdict"] == D.VERDICT_OFF


def test_missing_judgments_make_the_verdict_undetermined():
    report = _decide(_metrics(_decision_set(16, None, None, None, None, complete=False)))
    assert report["verdict"] == D.VERDICT_UNKNOWN
    assert _status(report, "20 premiers") == D.UNKNOWN
    assert "native" in report["consequence"]


def test_too_few_decision_queries_is_undetermined():
    report = _decide(_metrics(_decision_set(15, 0.5, 0.7)))
    assert report["verdict"] == D.VERDICT_UNKNOWN


def test_no_local_ranker_is_undetermined():
    queries = [_query(f"D{i:02d}", "decision", {"native": _ranker(0.5, 0.5)}) for i in range(16)]
    report = _decide(_metrics(queries, tuning={}))
    assert report["verdict"] == D.VERDICT_UNKNOWN and report["variante"] is None


def test_deployed_variant_is_gated_and_tuning_best_is_reported():
    """La variante évaluée est celle qu'applique MOISSON_RANKING=local ; la
    meilleure au réglage (jamais choisie sur la décision) est signalée."""
    decision = [_query(f"D{i:02d}", "decision",
                       {"native": _ranker(0.5, 0.5), "local:simple": _ranker(0.9, 0.9),
                        "local:bm25": _ranker(0.52, 0.5)}) for i in range(16)]
    metrics = _metrics(decision, tuning={"local:simple": 0.60, "local:bm25": 0.65})
    report = _decide(metrics)
    assert report["variante"] == "local:simple" and report["verdict"] == D.VERDICT_ON
    assert report["reglage"]["meilleure"] == "local:bm25"
    info = next(c for c in report["criteres"] if c["critere"] == "meilleure variante au réglage")
    assert info["statut"] == D.INFO and not info["bloquant"] and "local:bm25" in info["detail"]
    forced = _decide(metrics, variant="local:bm25")
    assert forced["variante"] == "local:bm25"
    assert forced["verdict"] == D.VERDICT_OFF          # bm25 : +4 % seulement
    # Sans variante déployée dans les classements : meilleure du réglage.
    assert D.choose_variant(metrics, None, "local:autre")[0] == "local:bm25"
    assert D.choose_variant(metrics)[0] == "local:bm25"


def test_recall_losses_are_listed_and_block_only_on_request():
    refs = [{"titre": "Référence", "cid": "c1", "positions": {"native": 3, "local:simple": 25}}]
    decision = _decision_set(16, 0.5, 0.7)
    decision[0] = _query("D01", "decision", {
        "native": _ranker(0.5, 0.5, r=0.8, relevant=["c1", "c2"]),
        "local:simple": _ranker(0.7, 0.5, r=0.6, relevant=["c2"])}, refs)
    report = _decide(_metrics(decision))
    kinds = sorted(loss["type"] for loss in report["baisses_de_rappel"])
    assert kinds == ["rappel_relatif", "reference_connue"]
    assert report["verdict"] == D.VERDICT_ON
    assert _status(report, "baisses de rappel") == D.INFO
    strict = _decide(_metrics(decision), recall_blocking=True)
    assert strict["verdict"] == D.VERDICT_OFF
    md = D.render_markdown(report)
    assert "Référence" in md and "Verdict" in md


def test_bootstrap_interval_is_reproducible():
    decision = [_query(f"D{i:02d}", "decision",
                       {"native": _ranker(0.4 + 0.01 * i, 0.5),
                        "local:simple": _ranker(0.5 + 0.02 * (i % 3), 0.5)}) for i in range(16)]
    a = _decide(_metrics(decision))["ndcg"]["ic95_bootstrap"]
    b = _decide(_metrics(decision))["ndcg"]["ic95_bootstrap"]
    assert a == b and a[0] <= a[1]
