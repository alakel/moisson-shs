"""
Métriques du protocole d'évaluation SHS (OPT-15).

À partir des ensembles de candidats (``pools/``) et des fiches remplies
(``fiches/``) :

- par requête et par classeur : P@20, nDCG@20, rappel relatif à l'ensemble
  jugé (R@20), rappel sur références connues (acquisition : présentes dans la
  moisson, quel que soit leur rang ; classement : présentes dans les k
  premiers), équilibre des k premiers par langue, type et ancienneté ;
- moyennes par jeu (réglage, décision) ;
- accord inter-juges sur l'échantillon du second jugement : kappa de Cohen
  pondéré (quadratique et linéaire), accord brut, accord sur la pertinence
  binaire, matrice de confusion ;
- précision des fusions : rejeu du jeu témoin
  ``tests/fixtures/dedup_pairs.json`` avec la déduplication actuelle (fausses
  fusions sur les paires négatives, fusions manquées sur les positives),
  bilan des fusions des moissons, et, si la fiche ``verification_fusions`` est
  remplie, précision mesurée sur les fusions réelles ;
- précision des DOI : rejeu du jeu témoin de la résolution
  (``tests/unit/test_opt10_negative_set.py`` : cas négatifs, contrôles
  positifs, abstentions consignées) avec CrossRef simulé, bilan des rapports
  ``doi/*.doi_report.json`` (acceptés, rejetés, ambigus, sans candidat,
  erreurs : couverture et abstentions), et, si la fiche ``verification_doi``
  est remplie, précision mesurée sur les DOI réels.

Définitions (``k`` = 20 par défaut, seuil de pertinence = 2) :

- pertinent : jugement >= seuil (2 = utile, 3 = central) ;
- P@k = (nombre de pertinents parmi les k premiers) / k ; un candidat non
  jugé compte comme non pertinent et est dénombré (``non_juges``) ;
- DCG@k = somme sur i = 1..k de gain(g_i) / log2(i + 1), gain exponentiel
  2^g - 1 (défaut) ou linéaire g ; nDCG@k = DCG@k / IDCG@k, où IDCG@k est
  le DCG des k meilleurs jugements de l'ensemble jugé de la requête ; indéfini
  (None) si aucun candidat n'a un jugement > 0 ;
- R@k = pertinents parmi les k premiers / pertinents de l'ensemble jugé
  (rappel relatif au « pool » : les pertinents hors de l'ensemble commun
  sont inconnus, biais habituel de la mise en commun) ;
- moyennes : macro (chaque requête pèse autant), sur les requêtes où la
  mesure est définie.

Écrit ``rapports/metriques.json`` et ``rapports/metriques.md``.

Exemple ::

    venv/bin/python scripts/eval/metrics.py --date 2026-09-29
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval import common as C  # noqa: E402
from eval.sheets import (  # noqa: E402
    SHEET_A, SHEET_B, SHEET_DOI, SHEET_FUSIONS, load_pools, read_sheet_rows,
)

DEFAULT_K = 20
DEFAULT_THRESHOLD = 2
GAINS = ("exp", "lin")
DEDUP_FIXTURE = _ROOT / "tests" / "fixtures" / "dedup_pairs.json"
DOI_CASES_MODULE = _ROOT / "tests" / "unit" / "test_opt10_negative_set.py"
METRICS_FORMAT = "moisson-shs/eval-metrics"


# === Jugements ===

def parse_grade(value: Any) -> Optional[int]:
    """
    Jugement lu dans une cellule : vide → None ; « 0 » à « 3 » (espaces,
    « 2.0 », « 2,0 » tolérés) → entier ; toute autre valeur lève ValueError
    (jamais ignorée en silence).
    """
    text = str(value if value is not None else "").strip().replace(",", ".")
    if text == "":
        return None
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"jugement illisible : {value!r}") from None
    if number != int(number) or int(number) not in C.SCALE:
        raise ValueError(f"jugement hors échelle (0 à 3) : {value!r}")
    return int(number)


def read_judgments(paths: Sequence[Path]) -> Tuple[Dict[Tuple[str, str], int], List[str]]:
    """
    Jugements des fiches remplies, par (requête, candidat). Renvoie
    ``(jugements, erreurs)`` : une valeur illisible, une ligne ayant plus de
    champs que l'en-tête (séparateur non protégé : colonnes possiblement
    décalées, ligne écartée) ou deux jugements contradictoires du même
    candidat sont des erreurs nommées, avec fichier et ligne (la dernière
    valeur lisible est gardée pour un doublon contradictoire). Un fichier
    absent ou non UTF-8 lève ``EvalError``.
    """
    grades: Dict[Tuple[str, str], int] = {}
    errors: List[str] = []
    for path in paths:
        for n, row in read_sheet_rows(path, errors):
            qid, cid = row.get("requete_id", ""), row.get("candidat_id", "")
            if not qid or not cid:
                continue
            try:
                g = parse_grade(row.get("jugement", ""))
            except ValueError as exc:
                errors.append(f"{Path(path).name} ligne {n} ({qid}/{cid}) : {exc}")
                continue
            if g is None:
                continue
            key = (qid, cid)
            if key in grades and grades[key] != g:
                errors.append(f"{Path(path).name} ligne {n} ({qid}/{cid}) : jugement "
                              f"contradictoire ({grades[key]} puis {g})")
            grades[key] = g
    return grades, errors


# === Mesures de classement ===

def gain(g: int, kind: str = "exp") -> float:
    return float(2 ** g - 1) if kind == "exp" else float(g)


def precision_at_k(ranking: Sequence[str], grades: Dict[str, int], k: int = DEFAULT_K,
                   threshold: int = DEFAULT_THRESHOLD) -> Tuple[float, int]:
    """``(P@k, nombre de non jugés parmi les k premiers)`` ; dénominateur k."""
    top = list(ranking)[:k]
    relevant = sum(1 for cid in top if grades.get(cid, -1) >= threshold)
    unjudged = sum(1 for cid in top if cid not in grades)
    return relevant / k, unjudged


def dcg(ranking_grades: Sequence[Optional[int]], k: int = DEFAULT_K,
        kind: str = "exp") -> float:
    return sum(gain(g, kind) / math.log2(i + 2)
               for i, g in enumerate(list(ranking_grades)[:k]) if g)


def ndcg_at_k(ranking: Sequence[str], grades: Dict[str, int], k: int = DEFAULT_K,
              kind: str = "exp") -> Optional[float]:
    """nDCG@k ; IDCG calculé sur tous les jugements de la requête."""
    ideal = dcg(sorted(grades.values(), reverse=True), k, kind)
    if ideal <= 0:
        return None
    return dcg([grades.get(cid) for cid in list(ranking)[:k]], k, kind) / ideal


def recall_at_k(ranking: Sequence[str], grades: Dict[str, int], k: int = DEFAULT_K,
                threshold: int = DEFAULT_THRESHOLD) -> Optional[float]:
    relevant = {cid for cid, g in grades.items() if g >= threshold}
    if not relevant:
        return None
    return len(relevant & set(list(ranking)[:k])) / len(relevant)


def known_refs(refs: Sequence[Dict[str, Any]], all_keys: Dict[str, Sequence[str]],
               rankings: Dict[str, Sequence[str]], ks: Sequence[int] = (DEFAULT_K, 30)
               ) -> Dict[str, Any]:
    """
    Références connues : trouvées dans la moisson (acquisition, rang
    indifférent) et position dans chaque classement. Une référence à
    identifiants alternatifs (éditions, dépôts) est trouvée si l'un d'eux
    l'est. Toutes les notices qui portent l'une de ses clés lui sont
    associées (``cids`` : un dépôt HAL et la notice DOI de l'éditeur restent
    souvent deux notices non fusionnées) ; sa position dans un classement
    est la meilleure de ces notices. ``cid`` : première notice trouvée.
    """
    key_to_cids: Dict[str, List[str]] = defaultdict(list)
    for cid, keys in all_keys.items():
        for key in keys:
            if cid not in key_to_cids[key]:
                key_to_cids[key].append(cid)
    positions_of = {name: {cid: i + 1 for i, cid in reversed(list(enumerate(ranking)))}
                    for name, ranking in rankings.items()}
    details = []
    for ref in refs:
        keys = ref.get("keys") or C.ref_keys(ref)
        cids: List[str] = []
        for key in keys:
            for cid in key_to_cids.get(key, ()):
                if cid not in cids:
                    cids.append(cid)
        positions = {}
        for name, index in positions_of.items():
            ranks = [index[cid] for cid in cids if cid in index]
            positions[name] = min(ranks) if ranks else None
        details.append({"titre": ref.get("titre"), "keys": keys,
                        "cid": cids[0] if cids else None, "cids": cids,
                        "positions": positions})
    n = len(details)
    out: Dict[str, Any] = {"n": n, "details": details,
                           "acquired": sum(1 for d in details if d["cid"])}
    out["acquisition"] = out["acquired"] / n if n else None
    out["rank_recall"] = {
        name: {str(k): (sum(1 for d in details if d["positions"][name] and d["positions"][name] <= k) / n
                        if n else None) for k in ks}
        for name in rankings}
    return out


def balance(ranking: Sequence[str], candidates: Dict[str, Dict[str, Any]],
            k: int = DEFAULT_K) -> Dict[str, Dict[str, int]]:
    """Répartition des k premiers par langue, type et ancienneté."""
    out: Dict[str, Counter] = {"langue": Counter(), "type": Counter(), "anciennete": Counter()}
    for cid in list(ranking)[:k]:
        bal = (candidates.get(cid) or {}).get("balance") or {}
        for dim in out:
            out[dim][bal.get(dim, "inconnue")] += 1
    return {dim: dict(sorted(c.items())) for dim, c in out.items()}


# === Accord inter-juges ===

def weighted_kappa(pairs: Sequence[Tuple[int, int]], categories: Sequence[int] = (0, 1, 2, 3),
                   weights: str = "quadratic") -> Optional[float]:
    """
    Kappa de Cohen pondéré. Poids d'accord w_ij = 1 - |i - j|^p / (K - 1)^p
    (p = 2 quadratique, p = 1 linéaire, p = ∞ non pondéré) ;
    kappa = (Po - Pe) / (1 - Pe), Po = somme w_ij O_ij / n,
    Pe = somme w_ij (ligne_i × colonne_j) / n². None si moins de deux paires
    ou si Pe = 1 (aucune variabilité).
    """
    if len(pairs) < 2:
        return None
    cats = list(categories)
    index = {c: i for i, c in enumerate(cats)}
    size = len(cats)
    observed = [[0] * size for _ in range(size)]
    for a, b in pairs:
        observed[index[a]][index[b]] += 1
    n = float(len(pairs))
    rows = [sum(r) for r in observed]
    cols = [sum(observed[i][j] for i in range(size)) for j in range(size)]

    def w(i: int, j: int) -> float:
        if weights == "none":
            return 1.0 if i == j else 0.0
        p = 2 if weights == "quadratic" else 1
        return 1.0 - (abs(i - j) ** p) / ((size - 1) ** p)

    po = sum(w(i, j) * observed[i][j] for i in range(size) for j in range(size)) / n
    pe = sum(w(i, j) * rows[i] * cols[j] for i in range(size) for j in range(size)) / (n * n)
    if pe >= 1.0:
        return None
    return (po - pe) / (1.0 - pe)


def agreement(grades_a: Dict[Tuple[str, str], int], grades_b: Dict[Tuple[str, str], int],
              threshold: int = DEFAULT_THRESHOLD) -> Dict[str, Any]:
    common = sorted(set(grades_a) & set(grades_b))
    pairs = [(grades_a[k], grades_b[k]) for k in common]
    confusion = [[0] * 4 for _ in range(4)]
    for a, b in pairs:
        confusion[a][b] += 1
    binary = [(int(a >= threshold), int(b >= threshold)) for a, b in pairs]
    return {
        "n_pairs": len(pairs),
        "kappa_quadratique": weighted_kappa(pairs, weights="quadratic"),
        "kappa_lineaire": weighted_kappa(pairs, weights="linear"),
        "kappa_non_pondere": weighted_kappa(pairs, weights="none"),
        "accord_brut": (sum(1 for a, b in pairs if a == b) / len(pairs)) if pairs else None,
        "accord_binaire": (sum(1 for a, b in binary if a == b) / len(binary)) if binary else None,
        "kappa_binaire": weighted_kappa(binary, categories=(0, 1), weights="none"),
        "confusion": confusion,
        "desaccords_de_2_ou_plus": [
            {"requete_id": k[0], "candidat_id": k[1], "A": grades_a[k], "B": grades_b[k]}
            for k in common if abs(grades_a[k] - grades_b[k]) >= 2],
    }


# === Par requête ===

def query_metrics(pool: Dict[str, Any], grades: Dict[str, int], k: int = DEFAULT_K,
                  threshold: int = DEFAULT_THRESHOLD, gain_kind: str = "exp",
                  query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Mesures d'une requête, pour chaque classeur de l'ensemble de candidats.
    ``query`` : définition courante de la requête (fichier de requêtes), qui
    fait foi pour le jeu et les références connues ; à défaut, la copie
    enregistrée dans l'ensemble de candidats. Sans aucun jugement, P@k et
    R@k valent None (requête non jugée, pas « zéro pertinent »).
    """
    query = query or pool["query"]
    entries = {name: entry for name, entry in pool["rankers"].items() if not entry.get("error")}
    rankings = {name: entry["ranking"] for name, entry in entries.items()}
    pool_cids = set(pool["pool"])
    judged = {cid: g for cid, g in grades.items() if cid in pool_cids}
    per_ranker = {}
    for name, ranking in rankings.items():
        p, unjudged = precision_at_k(ranking, judged, k, threshold)
        per_ranker[name] = {
            "p_at_k": p if judged else None,
            "ndcg_at_k": ndcg_at_k(ranking, judged, k, gain_kind),
            "r_at_k": recall_at_k(ranking, judged, k, threshold),
            "non_juges": unjudged,
            "complet": unjudged == 0,
            "n_ranked": len(ranking),
            "balance": balance(ranking, pool["candidates"], k),
            "relevant_top_k": [cid for cid in ranking[:k] if judged.get(cid, -1) >= threshold],
            # Classement évalué : version déclarée, empreinte du code source
            # (``pool.py build``) et description (mode, pondération lexicale).
            "version": entries[name].get("version"),
            "empreinte": entries[name].get("fingerprint"),
            "description": entries[name].get("description"),
        }
    refs = [dict(ref) for ref in (query.get("references_connues") or [])]
    for ref in refs:
        ref["keys"] = ref.get("keys") or C.ref_keys(ref)
    grade_counts = Counter(judged.values())
    return {
        "qid": pool["qid"],
        "set": query.get("set") or pool.get("set"),
        "query": query.get("query"),
        "n_items": pool.get("n_items"),
        "n_candidates": len(pool_cids),
        "n_judged": len(judged),
        "grades": {str(g): grade_counts.get(g, 0) for g in C.SCALE},
        "n_relevant": sum(1 for g in judged.values() if g >= threshold),
        "rankers": per_ranker,
        "ranker_errors": {name: entry["error"] for name, entry in pool["rankers"].items()
                          if entry.get("error")},
        "known_refs": known_refs(refs, pool.get("all_keys") or {}, rankings, (k, pool.get("depth", 30))),
        "references_note": query.get("references_note"),
        "pool_balance": balance(sorted(pool_cids), pool["candidates"], len(pool_cids)),
        "relevant_balance": balance(sorted(c for c in pool_cids if judged.get(c, -1) >= threshold),
                                    pool["candidates"], len(pool_cids)),
        "candidate_titles": {cid: (cand.get("display") or {}).get("titre", "")
                             for cid, cand in pool["candidates"].items()},
        "sources_status": pool.get("sources_status"),
    }


def aggregate(per_query: Sequence[Dict[str, Any]], subset: Optional[str]) -> Dict[str, Any]:
    """Moyennes macro par classeur sur un jeu (None : toutes les requêtes)."""
    rows = [q for q in per_query if subset is None or q["set"] == subset]
    names = sorted({name for q in rows for name in q["rankers"]})
    out: Dict[str, Any] = {"n_queries": len(rows), "rankers": {}}
    for name in names:
        present = [q for q in rows if name in q["rankers"]]
        vals = [q["rankers"][name] for q in present]
        bal: Dict[str, Counter] = defaultdict(Counter)
        for v in vals:
            for dim, counts in v["balance"].items():
                bal[dim].update(counts)
        acq = [q["known_refs"]["acquisition"] for q in present]
        out["rankers"][name] = {
            "n_queries": len(present),
            "n_complete": sum(1 for v in vals if v["complet"]),
            "p_at_k": C.mean(v["p_at_k"] for v in vals),
            "ndcg_at_k": C.mean(v["ndcg_at_k"] for v in vals),
            "r_at_k": C.mean(v["r_at_k"] for v in vals),
            "ndcg_defined": sum(1 for v in vals if v["ndcg_at_k"] is not None),
            "known_ref_acquisition": C.mean(acq),
            "known_ref_rank_recall": {
                kk: C.mean(q["known_refs"]["rank_recall"].get(name, {}).get(kk) for q in present)
                for kk in sorted({kk for q in present
                                  for kk in q["known_refs"]["rank_recall"].get(name, {})}, key=int)},
            "balance": {dim: dict(sorted(c.items())) for dim, c in bal.items()},
            # Versions du classement rencontrées sur ce jeu : plus d'une
            # signale des ensembles construits avec des codes différents.
            "versions": sorted({version_label(v) for v in vals}),
            "description": sorted({v.get("description") or "" for v in vals}),
        }
    return out


def version_label(ranker_metrics: Dict[str, Any]) -> str:
    """Version d'un classeur telle que consignée : « version (empreinte) »."""
    version = ranker_metrics.get("version") or "version inconnue"
    fingerprint = ranker_metrics.get("empreinte")
    return f"{version} ({fingerprint})" if fingerprint else f"{version} (empreinte non consignée)"


# === Fusions et DOI ===

def fusion_replay(fixture: Path = DEDUP_FIXTURE) -> Dict[str, Any]:
    """Rejeu du jeu témoin de la déduplication avec le code actuel."""
    import copy
    from utils.deduplication import deduplicate

    data = C.read_json(fixture)
    false_merges, missed = [], []
    for pair in data.get("negatives") or []:
        if len(deduplicate([copy.deepcopy(pair["a"]), copy.deepcopy(pair["b"])])) != 2:
            false_merges.append({"id": pair["id"], "trap": pair.get("trap"), "note": pair.get("note")})
    positives = data.get("positives") or []
    for pair in positives:
        if len(deduplicate([copy.deepcopy(pair["a"]), copy.deepcopy(pair["b"])])) != 1:
            missed.append({"id": pair["id"], "expected_missed": bool(pair.get("expected_missed")),
                           "note": pair.get("note")})
    n_neg = len(data.get("negatives") or [])
    return {"fixture": str(fixture.relative_to(_ROOT)) if _ROOT in fixture.parents else str(fixture),
            "negatives": n_neg, "false_merges": false_merges,
            "positives": len(positives), "merged_positives": len(positives) - len(missed),
            "missed": missed,
            "unexpected_missed": [m["id"] for m in missed if not m["expected_missed"]]}


def _load_doi_cases(path: Path):
    spec = importlib.util.spec_from_file_location("opt15_doi_cases", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def doi_replay(cases_module: Path = DOI_CASES_MODULE) -> Dict[str, Any]:
    """
    Rejeu du jeu témoin de la résolution de DOI (CrossRef simulé, aucun
    accès réseau) : un DOI écrit sur un cas négatif, ou un DOI différent du
    DOI attendu sur un contrôle positif, est un DOI erroné.
    """
    from unittest.mock import patch
    import utils.doi_resolver as dr

    try:
        module = _load_doi_cases(cases_module)
    except Exception as exc:  # noqa: BLE001 - jeu indisponible : signalé
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    def run(item, candidates):
        def fake(url, params=None, **kwargs):
            return {"message": {"items": candidates}}
        with patch.object(dr, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            out, rep = dr.resolve_dois([item])
        return out[0], rep["entries"][0]

    wrong, statuses = [], Counter()
    negatives = list(getattr(module, "NEGATIVE_CASES", []))
    for name, item, candidates in negatives:
        out, entry = run(item, candidates)
        statuses[entry["status"]] += 1
        if out.get("DOI") or "DOI:" in (out.get("extra") or ""):
            wrong.append({"cas": name, "doi": out.get("DOI"), "type": "négatif"})
    positives = list(getattr(module, "POSITIVE_CASES", [])) + list(
        getattr(module, "POSITIVE_WITH_DISTRACTOR", []))
    accepted_ok, abstained = 0, []
    for name, item, candidates, expected in positives:
        out, entry = run(item, candidates)
        if out.get("DOI") == expected:
            accepted_ok += 1
        elif out.get("DOI"):
            wrong.append({"cas": name, "doi": out.get("DOI"), "attendu": expected,
                          "type": "positif"})
        else:
            abstained.append(name)
    known = list(getattr(module, "KNOWN_ABSTENTIONS", []))
    return {"available": True, "rules_version": dr.RULES_VERSION,
            "module": str(cases_module.relative_to(_ROOT)) if _ROOT in cases_module.parents
            else str(cases_module),
            "negatives": len(negatives), "negative_statuses": dict(statuses),
            "positives": len(positives), "accepted_ok": accepted_ok,
            "abstained_positives": abstained, "known_abstentions": len(known),
            "wrong_dois": wrong}


def doi_reports_summary(out_dir: Path) -> Dict[str, Any]:
    """Bilan des rapports de résolution des candidats (``doi/``)."""
    totals = Counter()
    ambiguous = []
    files = sorted((out_dir / C.DOI_DIR).glob("*.doi_report.json"))
    for path in files:
        doc = C.read_json(path)
        for key, value in (doc.get("counts") or {}).items():
            if isinstance(value, int):
                totals[key] += value
        for entry in doc.get("entries") or []:
            if entry.get("status") == "ambigu":
                ambiguous.append({"requete_id": (doc.get("eval") or {}).get("qid"),
                                  "titre": entry.get("title"), "motif": entry.get("reason")})
    attempted = totals.get("interroges", 0)
    return {"files": len(files), "counts": dict(totals),
            "enrichment_pct_of_attempted": (100 * totals.get("accepte", 0) / attempted)
            if attempted else None,
            "abstentions": totals.get("rejete", 0) + totals.get("ambigu", 0),
            "ambiguous": ambiguous[:50]}


def verification_precision(path: Optional[Path], id_col: str) -> Optional[Dict[str, Any]]:
    """Précision d'une fiche de vérification remplie (verdict 1 / 0 / ?)."""
    if path is None or not Path(path).exists():
        return None
    yes, no, unsure, errors, refused = 0, 0, 0, [], []
    for n, row in read_sheet_rows(path, errors):
        verdict = row.get("verdict", "").strip().lower()
        if verdict == "":
            continue
        if verdict in ("1", "oui", "o", "yes"):
            yes += 1
        elif verdict in ("0", "non", "n", "no"):
            no += 1
            refused.append(row.get(id_col, f"ligne {n}"))
        elif verdict in ("?", "incertain"):
            unsure += 1
        else:
            errors.append(f"ligne {n} : verdict illisible {verdict!r}")
    judged = yes + no
    return {"file": Path(path).name, "judged": judged, "yes": yes, "no": no,
            "uncertain": unsure, "precision": (yes / judged) if judged else None,
            "erroneous": refused, "errors": errors}


def dedup_summary(out_dir: Path) -> Dict[str, Any]:
    totals = Counter()
    for path in sorted((out_dir / C.SNAPSHOTS).glob("*.json")):
        rep = C.read_json(path).get("dedup_report") or {}
        for key in ("input", "output", "merged_groups", "strong_merges", "fuzzy_merges",
                    "doi_split_components", "ambiguous", "version_splits"):
            totals[key] += int(rep.get(key) or 0)
    return dict(totals)


# === Calcul complet ===

def default_sheet(out_dir: Path, name: str) -> List[Path]:
    """Fiche remplie par défaut : export HTML (``*_rempli.csv``) s'il existe,
    sinon le CSV d'origine rempli dans un tableur."""
    sheets = out_dir / C.SHEETS
    filled = sheets / f"{name}_rempli.csv"
    if filled.exists():
        return [filled]
    plain = sheets / f"{name}.csv"
    return [plain] if plain.exists() else []


def compute(out_dir: Path, judge_a: Sequence[Path], judge_b: Sequence[Path] = (),
            k: int = DEFAULT_K, threshold: int = DEFAULT_THRESHOLD, gain_kind: str = "exp",
            fusion_sheet: Optional[Path] = None, doi_sheet: Optional[Path] = None,
            order: Optional[Sequence[str]] = None,
            replay: bool = True,
            queries: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    pools = load_pools(out_dir, order)
    grades_a, errors_a = read_judgments(judge_a)
    grades_b, errors_b = read_judgments(judge_b) if judge_b else ({}, [])
    per_query = []
    for pool in pools:
        g = {cid: v for (qid, cid), v in grades_a.items() if qid == pool["qid"]}
        per_query.append(query_metrics(pool, g, k, threshold, gain_kind,
                                       (queries or {}).get(pool["qid"])))
    return {
        "format": METRICS_FORMAT,
        "version": 1,
        "campagne": out_dir.name,
        "parameters": {"k": k, "threshold": threshold, "gain": gain_kind,
                       "judge_a": [str(p.name) for p in judge_a],
                       "judge_b": [str(p.name) for p in judge_b]},
        "judgment_errors": errors_a + errors_b,
        "n_judgments_a": len(grades_a),
        "n_judgments_b": len(grades_b),
        "per_query": per_query,
        "aggregate": {s: aggregate(per_query, s) for s in C.SETS},
        "agreement": agreement(grades_a, grades_b, threshold) if grades_b else None,
        "fusions": {
            "replay": fusion_replay() if replay else None,
            "harvests": dedup_summary(out_dir),
            "verification": verification_precision(fusion_sheet, "fusion_id"),
        },
        "dois": {
            "replay": doi_replay() if replay else None,
            "reports": doi_reports_summary(out_dir),
            "verification": verification_precision(doi_sheet, "verification_id"),
        },
    }


# === Rapport lisible ===

def render_markdown(m: Dict[str, Any]) -> str:
    k = m["parameters"]["k"]
    lines = [f"# Métriques d'évaluation — campagne {m['campagne']}", "",
             f"Paramètres : k = {k}, seuil de pertinence = {m['parameters']['threshold']}, "
             f"gain {m['parameters']['gain']} ; jugements A : {m['n_judgments_a']}, "
             f"B : {m['n_judgments_b']}.", ""]
    if m["judgment_errors"]:
        lines += ["## Erreurs de lecture des fiches", ""]
        lines += [f"- {e}" for e in m["judgment_errors"][:50]] + [""]
    for subset in C.SETS:
        agg = m["aggregate"][subset]
        lines += [f"## Jeu « {subset} » ({agg['n_queries']} requêtes)", "",
                  f"| Classeur | complètes | P@{k} | nDCG@{k} | R@{k} | acquisition réf. | réf. dans top {k} |",
                  "|---|---|---|---|---|---|---|"]
        for name, a in agg["rankers"].items():
            rr = a["known_ref_rank_recall"].get(str(k))
            lines.append(f"| {name} | {a['n_complete']}/{a['n_queries']} | {C.fmt(a['p_at_k'])} | "
                         f"{C.fmt(a['ndcg_at_k'])} | {C.fmt(a['r_at_k'])} | "
                         f"{C.pct(a['known_ref_acquisition'])} | {C.pct(rr)} |")
        lines.append("")
        for name, a in agg["rankers"].items():
            lines.append(f"Version évaluée ({name}) : {', '.join(a.get('versions') or ['—'])}"
                         + (" — **versions mélangées**" if len(a.get("versions") or []) > 1 else ""))
        lines.append("")
        for name, a in agg["rankers"].items():
            lines.append(f"Équilibre des {k} premiers ({name}) : " + " ; ".join(
                f"{dim} " + ", ".join(f"{c} {n}" for c, n in counts.items())
                for dim, counts in a["balance"].items()))
        lines.append("")
    lines += ["## Par requête", "",
              f"| Requête | jeu | notices | candidats | jugés | pertinents | "
              + " | ".join(f"P@{k} / nDCG@{k} {n}" for n in _ranker_names(m)) + " | réf. acquises |",
              "|---|---|---|---|---|---|" + "---|" * len(_ranker_names(m)) + "---|"]
    for q in m["per_query"]:
        cells = []
        for name in _ranker_names(m):
            r = q["rankers"].get(name)
            cells.append("–" if r is None else f"{C.fmt(r['p_at_k'], 2)} / {C.fmt(r['ndcg_at_k'], 2)}"
                         + ("" if r["complet"] else f" ({r['non_juges']} non jugés)"))
        kr = q["known_refs"]
        refs = f"{kr['acquired']}/{kr['n']}" if kr["n"] else "– (non établies)"
        lines.append(f"| {q['qid']} | {q['set']} | {q['n_items']} | {q['n_candidates']} | "
                     f"{q['n_judged']} | {q['n_relevant']} | " + " | ".join(cells) + f" | {refs} |")
    lines.append("")
    missing = [(q["qid"], d) for q in m["per_query"] for d in q["known_refs"]["details"] if not d["cid"]]
    if missing:
        lines += ["### Références connues non acquises (jamais moissonnées : un reclassement "
                  "ne peut pas les faire remonter)", ""]
        lines += [f"- {qid} : {d['titre']} ({', '.join(d['keys'])})" for qid, d in missing] + [""]
    ag = m.get("agreement")
    lines += ["## Accord inter-juges", ""]
    if ag:
        lines += [f"- paires jugées deux fois : {ag['n_pairs']}",
                  f"- kappa pondéré quadratique : {C.fmt(ag['kappa_quadratique'])} ; linéaire : "
                  f"{C.fmt(ag['kappa_lineaire'])} ; non pondéré : {C.fmt(ag['kappa_non_pondere'])}",
                  f"- accord brut : {C.pct(ag['accord_brut'])} ; accord sur la pertinence "
                  f"(seuil) : {C.pct(ag['accord_binaire'])} (kappa {C.fmt(ag['kappa_binaire'])})",
                  f"- désaccords d'au moins deux degrés : {len(ag['desaccords_de_2_ou_plus'])}", ""]
    else:
        lines += ["Second jugement non rendu : accord non mesuré.", ""]
    fus = m["fusions"]
    lines += ["## Fusions", ""]
    if fus["replay"]:
        r = fus["replay"]
        lines += [f"- jeu témoin ({r['fixture']}) : {len(r['false_merges'])} fausse(s) fusion(s) "
                  f"sur {r['negatives']} paires négatives ; {r['merged_positives']}/{r['positives']} "
                  f"positives fusionnées (manquées : {', '.join(x['id'] for x in r['missed']) or 'aucune'} ;"
                  f" hors manques documentés : {', '.join(r['unexpected_missed']) or 'aucune'})"]
        lines += [f"  - fausse fusion {x['id']} ({x['trap']}) : {x['note']}" for x in r["false_merges"]]
    h = fus["harvests"]
    if h:
        lines.append(f"- moissons de la campagne : {h.get('input', 0)} notices → {h.get('output', 0)} "
                     f"({h.get('merged_groups', 0)} fusions, dont passe floue {h.get('fuzzy_merges', 0)} ; "
                     f"ambiguës tenues à part {h.get('ambiguous', 0)} ; versions séparées "
                     f"{h.get('version_splits', 0)})")
    v = fus["verification"]
    lines.append(f"- vérification manuelle : précision {C.pct(v['precision'])} sur {v['judged']} "
                 f"fusions jugées ({v['uncertain']} incertaines)" if v and v["judged"] else
                 "- vérification manuelle des fusions réelles : fiche non remplie")
    lines.append("")
    dois = m["dois"]
    lines += ["## DOI", ""]
    rp = dois["replay"]
    if rp and rp.get("available"):
        lines.append(f"- jeu témoin ({rp['module']}, règles {rp['rules_version']}) : "
                     f"{len(rp['wrong_dois'])} DOI erroné(s) ; {rp['negatives']} cas négatifs "
                     f"(statuts {rp['negative_statuses']}) ; {rp['accepted_ok']}/{rp['positives']} "
                     f"contrôles positifs acceptés ; {rp['known_abstentions']} abstentions consignées")
        lines += [f"  - DOI erroné : {w}" for w in rp["wrong_dois"]]
    elif rp:
        lines.append(f"- jeu témoin indisponible : {rp.get('error')}")
    rep = dois["reports"]
    if rep["files"]:
        c = rep["counts"]
        lines.append(f"- résolution des candidats ({rep['files']} rapports) : {c.get('interroges', 0)} "
                     f"interrogés, {c.get('accepte', 0)} acceptés, {c.get('rejete', 0)} rejetés, "
                     f"{c.get('ambigu', 0)} ambigus, {c.get('sans_candidat', 0)} sans candidat, "
                     f"{c.get('erreur', 0)} erreurs ; enrichissement {C.fmt(rep['enrichment_pct_of_attempted'], 1)} % "
                     f"des interrogés (couverture, publiée à part de la précision)")
    else:
        lines.append("- résolution des candidats : aucun rapport (pool.py resolve-dois)")
    v = dois["verification"]
    lines.append(f"- vérification manuelle : précision {C.pct(v['precision'])} sur {v['judged']} DOI "
                 f"jugés ({v['uncertain']} incertains)" if v and v["judged"] else
                 "- vérification manuelle des DOI : fiche non remplie")
    lines.append("")
    return "\n".join(lines)


def _ranker_names(m: Dict[str, Any]) -> List[str]:
    names = sorted({n for q in m["per_query"] for n in q["rankers"]})
    return sorted(names, key=lambda n: (n != "native", n))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Métriques du protocole d'évaluation (OPT-15).")
    parser.add_argument("--date", default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--juge-a", action="append", default=[], help="fiche A remplie (répétable)")
    parser.add_argument("--juge-b", action="append", default=[], help="fiche B remplie (répétable)")
    parser.add_argument("--fusions", default=None, help="fiche de vérification des fusions remplie")
    parser.add_argument("--dois", default=None, help="fiche de vérification des DOI remplie")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--seuil", type=int, default=DEFAULT_THRESHOLD, choices=(1, 2, 3))
    parser.add_argument("--gain", choices=GAINS, default="exp")
    parser.add_argument("--sans-rejeu", action="store_true",
                        help="ne pas rejouer les jeux témoins (fusions, DOI)")
    args = parser.parse_args(argv)
    try:
        out_dir = C.ensure_writable(C.run_dir(args.date, Path(args.root) if args.root else None))
        judge_a = [Path(p) for p in args.juge_a] or default_sheet(out_dir, SHEET_A)
        judge_b = [Path(p) for p in args.juge_b] or default_sheet(out_dir, SHEET_B)
        fusion_sheet = Path(args.fusions) if args.fusions else next(
            iter(default_sheet(out_dir, SHEET_FUSIONS)), None)
        doi_sheet = Path(args.dois) if args.dois else next(iter(default_sheet(out_dir, SHEET_DOI)), None)
        doc = C.load_queries(strict=False)
        metrics = compute(out_dir, judge_a, judge_b, args.k, args.seuil, args.gain,
                          fusion_sheet, doi_sheet, [q["id"] for q in doc["queries"]],
                          replay=not args.sans_rejeu,
                          queries={q["id"]: q for q in doc["queries"]})
        C.write_json(out_dir / C.REPORTS / "metriques.json", metrics)
        C.write_text(out_dir / C.REPORTS / "metriques.md", render_markdown(metrics))
    except C.EvalError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    print(f"métriques écrites dans {out_dir / C.REPORTS} "
          f"({metrics['n_judgments_a']} jugements A, {metrics['n_judgments_b']} jugements B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
