"""
Rapport de décision du protocole d'évaluation SHS (OPT-15) : le classement
local d'OPT-13 doit-il devenir le classement par défaut ?

Méthode :

1. la variante locale comparée est celle qu'appliquerait
   ``MOISSON_RANKING=local`` : ``local:<DEFAULT_LEXICAL_MODEL>`` du module
   ``utils.ranking`` (le moteur ne transmet aucune pondération lexicale et
   aucune variable ne la choisit). Le jeu de **réglage** classe les
   variantes disponibles (meilleur nDCG@20 moyen) à titre d'information :
   si une autre pondération l'emporte, le rapport le signale (réglage à
   rendre configurable par OPT-13, puis nouvelle évaluation). Sans variante
   déployée dans les classements, la meilleure du réglage est comparée ;
   ``--variante`` impose une variante. Dans ces deux cas, une variante qui
   n'est pas celle qu'applique ``MOISSON_RANKING=local`` ne peut conduire
   qu'à « indéterminé » ou « ne pas activer » ;
2. les cibles du plan (§5 OPT-15) sont appliquées sur le jeu de
   **décision** seul, contre la référence ``native`` (ordre par défaut quand
   le classement local est désactivé) :

   - gain relatif de nDCG@20 moyen >= 10 % ;
   - P@20 moyen sans baisse ;
   - toute baisse de rappel listée (références connues sorties des 20
     premiers, rappel relatif R@20 en baisse) ; bloquante seulement avec
     ``--rappel-bloquant`` ;
   - zéro fausse fusion sur les paires négatives du jeu témoin
     (``tests/fixtures/dedup_pairs.json``) et zéro DOI erroné sur le jeu
     témoin de la résolution (rejoués par ``metrics.py``) ;

3. conditions de validité : au moins 16 requêtes de décision, toutes
   jugées sur les 20 premiers de chaque ordre comparé ; une seule version
   du classement (version déclarée et empreinte du code, consignées par
   ``pool.py build``) dans tous les ensembles de décision, identique à celle
   du module actuel ; sinon le verdict est « indéterminé ». Le verdict
   nomme la variante, la pondération et la version validées.

Verdicts : « activer le classement local par défaut », « ne pas activer le
classement local par défaut » ou « indéterminé ». Le script ne modifie
aucune configuration : l'activation (``MOISSON_RANKING``, OPT-13) reste une
décision humaine, prise sur ce rapport. Un intervalle de confiance
(bootstrap apparié sur les requêtes, graine fixe) accompagne le gain, à
titre d'information.

Écrit ``rapports/decision.md`` et ``rapports/decision.json``.

Exemple ::

    venv/bin/python scripts/eval/decide.py --date 2026-09-29
"""

from __future__ import annotations

import argparse
import importlib
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval import common as C  # noqa: E402
from eval.rankers import LOCAL_MODULE, module_fingerprint  # noqa: E402

BASELINE = "native"
NDCG_REL_GAIN_MIN = 0.10
MIN_DECISION_QUERIES = C.MIN_DECISION
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260929

def _signed_pct(value: float) -> str:
    return f"{100 * value:+.1f} %"


VERDICT_ON = "activer le classement local par défaut"
VERDICT_OFF = "ne pas activer le classement local par défaut"
VERDICT_UNKNOWN = "indéterminé"

PASS, FAIL, UNKNOWN, INFO = "satisfait", "non satisfait", "indéterminé", "information"


def _local_names(metrics: Dict[str, Any]) -> List[str]:
    return sorted({n for q in metrics["per_query"] for n in q["rankers"] if n.startswith("local")})


def deployed_ranking(module_name: str = LOCAL_MODULE) -> Dict[str, Any]:
    """
    Classement qu'appliquerait ``MOISSON_RANKING=local`` : le moteur
    (``engine.orchestrator.harvest``) appelle ``rank_harvest`` sans
    pondération lexicale, qui retombe sur ``DEFAULT_LEXICAL_MODEL``.
    Renvoie la variante correspondante (nom du classeur dans les ensembles
    de candidats), la pondération, la version et l'empreinte du code actuel.
    """
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - module absent ou en cours d'écriture
        return {"available": False, "module": module_name,
                "error": f"{type(exc).__name__}: {exc}"}
    model = getattr(module, "DEFAULT_LEXICAL_MODEL", None)
    return {"available": True, "module": module_name,
            "variante": f"local:{model}" if model else "local",
            "lexical_model": model,
            "version": getattr(module, "RANKING_VERSION", None),
            "empreinte": module_fingerprint(module)}


def tuning_best(metrics: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Meilleure variante locale sur le jeu de réglage (nDCG@k moyen)."""
    names = _local_names(metrics)
    if not names:
        return None, "aucun classement local dans les ensembles de candidats"
    tuning = metrics["aggregate"].get(C.SET_TUNING, {}).get("rankers", {})
    scored = [(tuning.get(n, {}).get("ndcg_at_k"), n) for n in names]
    judged = [(v, n) for v, n in scored if v is not None]
    if not judged:
        return None, "jeu de réglage non jugé"
    complete = [(v, n) for v, n in judged
                if tuning[n]["n_complete"] == tuning[n]["n_queries"]]
    pool = complete or judged
    best = max(pool, key=lambda vn: (vn[0], -names.index(vn[1])))
    note = "" if complete else " (réglage incomplètement jugé)"
    return best[1], (f"meilleur nDCG@k moyen sur le jeu de réglage{note} : "
                     + ", ".join(f"{n} {C.fmt(v)}" for v, n in judged))


def choose_variant(metrics: Dict[str, Any], forced: Optional[str] = None,
                   deployed: Optional[str] = None) -> Tuple[Optional[str], str]:
    """
    Variante locale comparée : imposée (``forced``), sinon la variante
    déployée (``deployed``) si les classements la contiennent, sinon la
    meilleure sur le jeu de réglage.
    """
    names = _local_names(metrics)
    if forced:
        if forced not in names:
            return None, (f"variante {forced!r} absente des classements "
                          f"(disponibles : {', '.join(names) or 'aucune'})")
        return forced, "imposée (--variante)"
    if not names:
        return None, "aucun classement local dans les ensembles de candidats"
    if deployed and deployed in names:
        return deployed, "variante qu'applique MOISSON_RANKING=local"
    if len(names) == 1:
        return names[0], "seule variante disponible"
    best, why = tuning_best(metrics)
    if best is None:
        return None, f"{why} : la variante ne peut pas être choisie"
    return best, why


def _bootstrap_gain(pairs: Sequence[Tuple[float, float]], samples: int = BOOTSTRAP_SAMPLES,
                    seed: int = BOOTSTRAP_SEED) -> Optional[Tuple[float, float]]:
    """Intervalle à 95 % du gain relatif des moyennes (bootstrap apparié)."""
    if len(pairs) < 2:
        return None
    rng = random.Random(seed)
    gains = []
    for _ in range(samples):
        draw = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        base = sum(b for b, _v in draw) / len(draw)
        var = sum(v for _b, v in draw) / len(draw)
        if base > 0:
            gains.append((var - base) / base)
    if not gains:
        return None
    gains.sort()
    return gains[int(0.025 * len(gains))], gains[min(len(gains) - 1, int(0.975 * len(gains)))]


def recall_losses(queries: Sequence[Dict[str, Any]], variant: str, k: int,
                  baseline: str = BASELINE) -> List[Dict[str, Any]]:
    """Baisses de rappel, requête par requête : références connues sorties
    des k premiers, rappel relatif R@k en baisse."""
    losses = []
    for q in queries:
        for d in q["known_refs"]["details"]:
            b = d["positions"].get(baseline)
            v = d["positions"].get(variant)
            if b is not None and b <= k and (v is None or v > k):
                losses.append({"requete": q["qid"], "type": "reference_connue",
                               "titre": d["titre"], "rang_reference": b, "rang_variante": v})
        rb = (q["rankers"].get(baseline) or {}).get("r_at_k")
        rv = (q["rankers"].get(variant) or {}).get("r_at_k")
        if rb is not None and rv is not None and rv < rb - 1e-12:
            lost = sorted(set(q["rankers"][baseline]["relevant_top_k"])
                          - set(q["rankers"][variant]["relevant_top_k"]))
            titles = q.get("candidate_titles") or {}
            losses.append({"requete": q["qid"], "type": "rappel_relatif",
                           "r_reference": rb, "r_variante": rv, "pertinents_perdus": lost,
                           "titres_perdus": [titles.get(c, "") for c in lost]})
    return losses


def _version_of(ranker_metrics: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    return ranker_metrics.get("version"), ranker_metrics.get("empreinte")


def version_check(queries: Sequence[Dict[str, Any]], names: Sequence[str],
                  deployed: Optional[Dict[str, Any]]) -> Tuple[str, str, Dict[str, Any]]:
    """
    Version du classement évaluée, classeur par classeur, sur les requêtes
    données : une seule version (déclarée et empreinte du code) par
    classeur, identique à celle du module actuel. Renvoie
    ``(statut, détail, versions)`` ; tout écart rend le verdict indéterminé.
    """
    versions: Dict[str, List[Tuple[Optional[str], Optional[str]]]] = {}
    for name in names:
        seen = sorted({_version_of(q["rankers"][name]) for q in queries if name in q["rankers"]},
                      key=lambda vf: (str(vf[0]), str(vf[1])))
        versions[name] = seen
    labels = {n: ", ".join(f"{v or 'version inconnue'} ({f or 'empreinte non consignée'})"
                           for v, f in vs) or "—" for n, vs in versions.items()}
    report = {n: [{"version": v, "empreinte": f} for v, f in vs] for n, vs in versions.items()}
    problems = []
    for name, seen in versions.items():
        if len(seen) > 1:
            problems.append(f"{name} : versions mélangées d'un ensemble à l'autre ({labels[name]}) ; "
                            f"relancer pool.py build pour toutes les requêtes")
        elif seen and (seen[0][0] is None or seen[0][1] is None):
            problems.append(f"{name} : {labels[name]} ; relancer pool.py build")
    if not problems and deployed and deployed.get("available"):
        current = (deployed.get("version"), deployed.get("empreinte"))
        for name, seen in versions.items():
            if seen and seen[0] != current:
                problems.append(
                    f"{name} évalué en {labels[name]}, module actuel en {current[0]} ({current[1]}) : "
                    f"le code du classement a changé ; relancer pool.py build puis sheets.py "
                    f"generate et compléter les jugements")
    elif not problems:
        problems.append("module de classement actuel introuvable : version déployée inconnue"
                        + (f" ({deployed.get('error')})" if deployed and deployed.get("error") else ""))
    if problems:
        return UNKNOWN, " ; ".join(problems), report
    return PASS, " ; ".join(f"{n} {labels[n]}" for n in names), report


def decide(metrics: Dict[str, Any], variant: Optional[str] = None,
           baseline: str = BASELINE, recall_blocking: bool = False,
           min_queries: int = MIN_DECISION_QUERIES,
           gain_min: float = NDCG_REL_GAIN_MIN,
           deployed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Applique les cibles du plan et rend le verdict (voir le module).
    ``deployed`` : classement qu'appliquerait ``MOISSON_RANKING=local``
    (défaut : ``deployed_ranking()``, lu dans le module actuel).
    """
    k = metrics["parameters"]["k"]
    deployed = deployed if deployed is not None else deployed_ranking()
    deployed_name = deployed.get("variante") if deployed.get("available") else None
    chosen, why = choose_variant(metrics, variant, deployed_name)
    best, best_why = tuning_best(metrics)
    criteria: List[Dict[str, Any]] = []
    decision_q = [q for q in metrics["per_query"] if q["set"] == C.SET_DECISION]
    report: Dict[str, Any] = {
        "campagne": metrics.get("campagne"), "k": k, "reference": baseline,
        "variante": chosen, "choix_variante": why, "criteres": criteria,
        "variante_deployee": deployed,
        "reglage": {"meilleure": best, "detail": best_why},
        "requetes_decision": [q["qid"] for q in decision_q],
    }

    def add(name: str, status: str, detail: str, blocking: bool = True, **extra: Any) -> None:
        criteria.append({"critere": name, "statut": status, "detail": detail,
                         "bloquant": blocking, **extra})

    # --- Validité ---
    usable = [q for q in decision_q if chosen and baseline in q["rankers"]
              and chosen in q["rankers"]]
    incomplete = [q["qid"] for q in usable
                  if not (q["rankers"][baseline]["complet"] and q["rankers"][chosen]["complet"])]
    if chosen is None:
        add("classement local disponible", UNKNOWN, why)
    deployed_label = (f"{deployed_name} (pondération lexicale {deployed.get('lexical_model')})"
                      if deployed_name else "inconnue")
    if chosen is not None and chosen == deployed_name:
        add("variante évaluée = variante qu'applique MOISSON_RANKING=local", PASS, deployed_label)
    elif chosen is not None:
        add("variante évaluée = variante qu'applique MOISSON_RANKING=local", UNKNOWN,
            f"variante évaluée {chosen} ; MOISSON_RANKING=local applique {deployed_label}"
            + ("" if deployed_name else f" ({deployed.get('error', 'module introuvable')})")
            + " : activable seulement après avoir rendu cette pondération configurable "
              "(OPT-13), reconstruit les ensembles et réévalué")
    if best is not None and chosen is not None and best != chosen:
        add("meilleure variante au réglage", INFO,
            f"{best} l'emporte sur le jeu de réglage ({best_why}) ; la variante évaluée pour "
            f"l'activation reste {chosen}. Pour activer {best}, OPT-13 doit la rendre "
            f"sélectionnable (ou en faire la pondération par défaut), puis reconstruire et "
            f"réévaluer", blocking=False)
    with_baseline = sum(1 for q in decision_q if baseline in q["rankers"])
    if chosen is None:
        add(f"au moins {min_queries} requêtes de décision comparables", UNKNOWN,
            f"variante non choisie ; {with_baseline} requête(s) de décision classée(s) par "
            f"{baseline}")
    elif len(usable) < min_queries:
        add(f"au moins {min_queries} requêtes de décision comparables", UNKNOWN,
            f"{len(usable)} requête(s) avec les deux classements")
    else:
        add(f"au moins {min_queries} requêtes de décision comparables", PASS,
            f"{len(usable)} requêtes")
    if incomplete:
        add(f"{k} premiers de chaque ordre entièrement jugés", UNKNOWN,
            f"jugements manquants pour {len(incomplete)} requête(s) : {', '.join(incomplete)}",
            requetes=incomplete)
    elif usable:
        add(f"{k} premiers de chaque ordre entièrement jugés", PASS, "complet")
    if usable:
        status, detail, versions = version_check(usable, [baseline, chosen], deployed)
        report["versions"] = versions
        add("une seule version du classement, identique au module actuel", status, detail)

    # --- nDCG ---
    pairs = [(q["rankers"][baseline]["ndcg_at_k"], q["rankers"][chosen]["ndcg_at_k"])
             for q in usable]
    defined = [(b, v) for b, v in pairs if b is not None and v is not None]
    mean_b = C.mean(b for b, _v in defined)
    mean_v = C.mean(v for _b, v in defined)
    gain = ((mean_v - mean_b) / mean_b) if (mean_b and mean_v is not None) else None
    ci = _bootstrap_gain(defined)
    report["ndcg"] = {"reference": mean_b, "variante": mean_v, "gain_relatif": gain,
                      "ic95_bootstrap": ci, "requetes_definies": len(defined),
                      "ameliorees": sum(1 for b, v in defined if v > b + 1e-12),
                      "degradees": sum(1 for b, v in defined if v < b - 1e-12)}
    if gain is None or incomplete or not usable:
        add(f"gain relatif de nDCG@{k} >= {100 * gain_min:.0f} %", UNKNOWN,
            "non calculable (jugements absents ou incomplets)")
    else:
        add(f"gain relatif de nDCG@{k} >= {100 * gain_min:.0f} %",
            PASS if gain >= gain_min - 1e-12 else FAIL,
            f"{C.fmt(mean_b)} → {C.fmt(mean_v)} : {_signed_pct(gain)}"
            + (f" (IC 95 % bootstrap {_signed_pct(ci[0])} à {_signed_pct(ci[1])})" if ci else ""))

    # --- P@k ---
    p_b = C.mean(q["rankers"][baseline]["p_at_k"] for q in usable)
    p_v = C.mean(q["rankers"][chosen]["p_at_k"] for q in usable)
    report["p_at_k"] = {"reference": p_b, "variante": p_v}
    if p_b is None or incomplete or not usable:
        add(f"P@{k} moyen sans baisse", UNKNOWN, "non calculable (jugements absents ou incomplets)")
    else:
        add(f"P@{k} moyen sans baisse", PASS if p_v >= p_b - 1e-12 else FAIL,
            f"{C.fmt(p_b)} → {C.fmt(p_v)}")

    # --- Rappel ---
    losses = recall_losses(usable, chosen, k, baseline) if chosen else []
    report["baisses_de_rappel"] = losses
    # Acquisition : indépendante de tout classement, mesurée sur toutes les
    # requêtes de décision (une référence présente dans la moisson l'est
    # quelle que soit la variante).
    acq = [q["known_refs"] for q in decision_q if q["known_refs"]["n"]]
    report["acquisition"] = {"references": sum(r["n"] for r in acq),
                             "acquises": sum(r["acquired"] for r in acq),
                             "requetes": len(acq)}
    if not usable or incomplete:
        add("baisses de rappel listées", UNKNOWN if recall_blocking else INFO,
            f"{len(losses)} baisse(s) repérée(s) sur les données disponibles",
            blocking=recall_blocking)
    else:
        status = (FAIL if losses else PASS) if recall_blocking else INFO
        add("baisses de rappel listées", status,
            f"{len(losses)} baisse(s) : à examiner une à une (liste ci-dessous)" if losses
            else "aucune baisse", blocking=recall_blocking)

    # --- Fusions et DOI (jeux témoins) ---
    rep = (metrics.get("fusions") or {}).get("replay")
    if not rep:
        add("zéro fausse fusion (paires négatives du jeu témoin)", UNKNOWN, "jeu témoin non rejoué")
    else:
        n_false = len(rep["false_merges"])
        add("zéro fausse fusion (paires négatives du jeu témoin)", PASS if n_false == 0 else FAIL,
            f"{n_false} fausse(s) fusion(s) sur {rep['negatives']} paires négatives")
    ver = (metrics.get("fusions") or {}).get("verification")
    if ver and ver.get("judged"):
        add("fusions réelles vérifiées", INFO,
            f"précision {C.pct(ver['precision'])} sur {ver['judged']} ; erronées : "
            f"{', '.join(ver['erroneous']) or 'aucune'}", blocking=False)
    drep = (metrics.get("dois") or {}).get("replay")
    if not drep or not drep.get("available"):
        add("zéro DOI erroné (jeu témoin de la résolution)", UNKNOWN,
            (drep or {}).get("error", "jeu témoin non rejoué"))
    else:
        n_wrong = len(drep["wrong_dois"])
        add("zéro DOI erroné (jeu témoin de la résolution)", PASS if n_wrong == 0 else FAIL,
            f"{n_wrong} DOI erroné(s) ; {drep['negatives']} cas négatifs, "
            f"{drep['accepted_ok']}/{drep['positives']} contrôles positifs acceptés")
    dver = (metrics.get("dois") or {}).get("verification")
    if dver and dver.get("judged"):
        add("DOI réels vérifiés", INFO,
            f"précision {C.pct(dver['precision'])} sur {dver['judged']} ; erronés : "
            f"{', '.join(dver['erroneous']) or 'aucun'}", blocking=False)

    # --- Verdict ---
    blocking = [c for c in criteria if c["bloquant"]]
    if any(c["statut"] == FAIL for c in blocking):
        verdict = VERDICT_OFF
    elif any(c["statut"] == UNKNOWN for c in blocking):
        verdict = VERDICT_UNKNOWN
    else:
        verdict = VERDICT_ON
    report["verdict"] = verdict
    if verdict == VERDICT_ON:
        validated = {"variante": chosen, "lexical_model": deployed.get("lexical_model"),
                     "version": deployed.get("version"), "empreinte": deployed.get("empreinte")}
        report["classement_valide"] = validated
        report["consequence"] = (
            f"Le classement local peut être activé par défaut : MOISSON_RANKING=local applique "
            f"la variante évaluée ({chosen}, pondération lexicale {validated['lexical_model']}, "
            f"version {validated['version']}, empreinte du code {validated['empreinte']}). "
            f"Décision humaine ; ce script ne modifie aucune configuration. Toute modification "
            f"ultérieure du classement demande une nouvelle évaluation.")
    else:
        report["consequence"] = "Le classement local reste désactivé par défaut (MOISSON_RANKING=native)."
        if (verdict == VERDICT_UNKNOWN and chosen is not None and chosen != deployed_name
                and all(c["statut"] != UNKNOWN for c in blocking
                        if not c["critere"].startswith("variante évaluée"))):
            report["consequence"] += (
                f" La variante {chosen} satisfait les cibles, mais MOISSON_RANKING=local ne "
                f"l'applique pas : la rendre configurable (OPT-13), reconstruire et réévaluer.")
    return report


def render_markdown(r: Dict[str, Any]) -> str:
    k = r["k"]
    dep = r.get("variante_deployee") or {}
    dep_text = (f"`{dep.get('variante')}` (pondération lexicale {dep.get('lexical_model')}, "
                f"version {dep.get('version')}, empreinte du code {dep.get('empreinte')})"
                if dep.get("available") else
                f"inconnue ({dep.get('error', 'module de classement introuvable')})")
    tuning = r.get("reglage") or {}
    lines = [f"# Décision : classement local par défaut — campagne {r['campagne']}", "",
             f"**Verdict : {r['verdict']}.** {r['consequence']}", "",
             f"Référence : `{r['reference']}` ; variante comparée : `{r['variante'] or '—'}` "
             f"({r['choix_variante']}).", "",
             f"Classement qu'appliquerait `MOISSON_RANKING=local` : {dep_text}.", "",
             f"Réglage : {tuning.get('detail', '—')}"
             + (f" ; meilleure : `{tuning['meilleure']}`" if tuning.get("meilleure") else "")
             + ".", "",
             f"Requêtes de décision : {len(r['requetes_decision'])} "
             f"({', '.join(r['requetes_decision'])}).", "",
             "| Critère | Statut | Bloquant | Détail |", "|---|---|---|---|"]
    for c in r["criteres"]:
        lines.append(f"| {c['critere']} | {c['statut']} | {'oui' if c['bloquant'] else 'non'} | "
                     f"{c['detail']} |")
    lines.append("")
    nd = r.get("ndcg") or {}
    if nd.get("requetes_definies"):
        lines += [f"nDCG@{k} : {nd['ameliorees']} requête(s) améliorée(s), {nd['degradees']} "
                  f"dégradée(s) sur {nd['requetes_definies']}.", ""]
    losses = r.get("baisses_de_rappel") or []
    lines += ["## Baisses de rappel", ""]
    if not losses:
        lines.append("Aucune.")
    for loss in losses:
        if loss["type"] == "reference_connue":
            lines.append(f"- {loss['requete']} : référence connue « {loss['titre']} » au rang "
                         f"{loss['rang_reference']} en référence, "
                         f"{loss['rang_variante'] or 'hors classement'} avec la variante")
        else:
            lost = [f"« {t} » ({c})" if t else c for c, t in
                    zip(loss["pertinents_perdus"], loss.get("titres_perdus") or
                        [""] * len(loss["pertinents_perdus"]))]
            lines.append(f"- {loss['requete']} : R@{k} {C.fmt(loss['r_reference'])} → "
                         f"{C.fmt(loss['r_variante'])} ; pertinents sortis des {k} premiers : "
                         f"{', '.join(lost) or '—'}")
    acq = r.get("acquisition") or {}
    lines += ["", "## Limites", "",
              f"- Acquisition : {acq.get('acquises', 0)} référence(s) connue(s) moissonnée(s) sur "
              f"{acq.get('references', 0)} ({acq.get('requetes', 0)} requêtes du jeu de décision, "
              "quel que soit le classement) ; un reclassement ne répare pas une "
              "référence jamais récupérée : acquisition et classement sont mesurés séparément.",
              "- Rappel relatif calculé sur l'ensemble commun jugé (union des premiers de chaque "
              "ordre) : les pertinents hors de cet ensemble sont inconnus.",
              "- Résultats sur échantillon : ils ne garantissent pas l'absence d'erreur en "
              "production ; le score reste un indicateur de classement, pas une probabilité de "
              "vérité.", ""]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rapport de décision (OPT-15).")
    parser.add_argument("--date", default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--variante", default=None,
                        help="variante locale imposée (local:bm25…) ; si ce n'est pas celle "
                             "qu'applique MOISSON_RANKING=local, le verdict ne peut pas être "
                             "« activer »")
    parser.add_argument("--reference", default=BASELINE)
    parser.add_argument("--rappel-bloquant", action="store_true",
                        help="toute baisse de rappel bloque l'activation")
    parser.add_argument("--min-requetes", type=int, default=MIN_DECISION_QUERIES)
    args = parser.parse_args(argv)
    try:
        out_dir = C.ensure_writable(C.run_dir(args.date, Path(args.root) if args.root else None))
        path = out_dir / C.REPORTS / "metriques.json"
        if not path.exists():
            raise C.EvalError(f"{path} absent : lancer d'abord metrics.py")
        metrics = C.read_json(path)
        report = decide(metrics, args.variante, args.reference, args.rappel_bloquant,
                        args.min_requetes)
        C.write_json(out_dir / C.REPORTS / "decision.json", report)
        C.write_text(out_dir / C.REPORTS / "decision.md", render_markdown(report))
    except C.EvalError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    print(f"verdict : {report['verdict']} (variante {report['variante'] or '—'}) — "
          f"{out_dir / C.REPORTS / 'decision.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
