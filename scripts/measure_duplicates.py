#!/usr/bin/env python
"""
Mesure des doublons sur les corpus réels (OPT-06), avant et après la
refonte de la déduplication.

Le script parcourt, en **lecture seule**, les corpus Zotero internes d'un
dossier de sortie (``corpus_*.json``, hors ``.csl.json`` et fichiers
associés ``.manifest.json`` / ``.doi_report.json``), re-déduplique chaque
corpus avec une ou plusieurs versions du code et compare :

- notices en entrée et en sortie ;
- doublons résiduels à titre normalisé et année identiques (mesure du
  constat C18 : chaque groupe de n notices compte n - 1), avec la même
  normalisation que le banc (``scripts/bench/analysis.py``), indépendante
  du code mesuré ;
- paires DOI / sans-DOI du même titre et de la même année, et part de ces
  paires fusionnées ;
- durée de la déduplication (plus gros corpus, maximum, total) ;
- en option, déterminisme (permutations de l'entrée) et idempotence
  (re-déduplication de la sortie) ;
- en option (``--diff-out``), liste **exhaustive** des groupes qui changent
  entre la version « avant » et la version « après » (fusions nouvelles ou
  agrandies, scissions), pour l'examen manuel de chaque nouvelle fusion.

Chaque version du code tourne dans un processus fils (``--worker``) dont le
``sys.path`` pointe sur l'arbre mesuré, ce qui permet de mesurer une copie
d'avant chantier sans conflit de modules. Les notices sont suivies par une
ligne d'``extra`` temporaire (``Mesure-ID: n``), conservée par toutes les
versions de la fusion (lignes d'``extra`` agrégées) ; elle n'intervient dans
aucune décision d'identité.

Usage (depuis la racine du projet) ::

    venv/bin/python scripts/measure_duplicates.py --baseline-tree /chemin/copie_avant --json-out /tmp/mesure.json --samples-out /tmp/echantillons.json --diff-out /tmp/groupes_changes.json --check-determinism

``--output-dir`` mesure un autre dossier de corpus (par exemple le dossier de
sortie d'une moisson live conservée).

Aucune écriture dans le dossier mesuré ; les sorties vont où ``--json-out``
et ``--samples-out`` l'indiquent.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from bench.analysis import item_doi, item_year, normalize_title  # noqa: E402

MARK = "Mesure-ID"
_ASSOCIATED = (".csl.json", ".manifest.json", ".doi_report.json")


# === Corpus ===

def list_corpora(output_dir: Path) -> List[Path]:
    """Corpus Zotero internes du dossier (lecture seule), triés par nom."""
    out = []
    for path in sorted(output_dir.glob("corpus_*.json")):
        name = path.name.lower()
        if name.endswith(_ASSOCIATED) or not path.is_file():
            continue
        out.append(path)
    return out


def load_items(path: Path) -> List[Dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def tag_items(items: List[Dict]) -> List[Dict]:
    """Copie des notices avec une ligne de suivi ``Mesure-ID: n``."""
    tagged = []
    for i, it in enumerate(items):
        c = dict(it)
        extra = c.get("extra") or ""
        c["extra"] = (extra + "\n" if extra else "") + f"{MARK}: {i}"
        tagged.append(c)
    return tagged


def members_of(record: Dict) -> List[int]:
    out = []
    for line in str(record.get("extra") or "").split("\n"):
        if line.startswith(MARK + ":"):
            try:
                out.append(int(line.split(":", 1)[1]))
            except ValueError:
                pass
    return sorted(set(out))


def strip_marks(record: Dict) -> Dict:
    c = dict(record)
    lines = [l for l in str(c.get("extra") or "").split("\n")
             if not l.startswith(MARK + ":")]
    c["extra"] = "\n".join(lines)
    return c


def title_year_groups(items: List[Dict]) -> List[List[int]]:
    """Groupes (>= 2) de notices de même titre normalisé et même année."""
    groups: Dict[tuple, List[int]] = defaultdict(list)
    for i, it in enumerate(items):
        t = normalize_title(it.get("title", ""))
        if t:
            groups[(t, item_year(it))].append(i)
    return [g for g in groups.values() if len(g) > 1]


def describe(item: Dict) -> Dict[str, Any]:
    """Description compacte d'une notice pour l'examen manuel."""
    sources = ""
    for line in str(item.get("extra") or "").split("\n"):
        if line.startswith("Sources:"):
            sources = line[len("Sources:"):].strip()
            break
    evidence = ""
    for line in str(item.get("extra") or "").split("\n"):
        if line.startswith("Dedup evidence:"):
            evidence = line[len("Dedup evidence:"):].strip()
    creators = item.get("creators") or []
    return {
        "title": (item.get("title") or "")[:160],
        "date": item.get("date", ""),
        "type": item.get("itemType", ""),
        "doi": item_doi(item),
        "sources": sources,
        "authors": [c.get("lastName") or c.get("name") or "" for c in creators][:4],
        # Nom et prénom tels que saisis (examen des corroborations d'auteur).
        "authors_full": [", ".join(p for p in (c.get("lastName") or c.get("name")
                                               or "", c.get("firstName") or "")
                                   if p)
                         for c in creators][:4],
        "n_authors": len(creators),
        "url": (item.get("url") or "")[:140],
        "venue": (item.get("publicationTitle") or item.get("bookTitle")
                  or item.get("proceedingsTitle") or "")[:80],
        "evidence": evidence[:300],
    }


# === Processus fils : une version du code ===

def worker(tree: Path, output_dir: Path, check: bool, permutations: int) -> Dict:
    sys.path.insert(0, str(tree))
    from utils.deduplication import deduplicate  # noqa: E402

    results = []
    determinism = {"checked": 0, "permutation_failures": 0,
                   "idempotence_failures": 0, "examples": []}
    for path in list_corpora(output_dir):
        items = load_items(path)
        tagged = tag_items(items)
        t0 = time.perf_counter()
        out = deduplicate([dict(x) for x in tagged])
        dt = time.perf_counter() - t0
        membership = [members_of(r) for r in out]
        clean = [strip_marks(r) for r in out]
        residual_groups = title_year_groups(clean)
        results.append({
            "file": path.name,
            "bytes": path.stat().st_size,
            "n_in": len(items),
            "n_out": len(out),
            "seconds": round(dt, 4),
            "groups": [m for m in membership if len(m) > 1],
            "residual": [[membership[i] for i in g] for g in residual_groups],
            "residual_records": [[describe(clean[i]) for i in g]
                                 for g in residual_groups],
            "merged_records": {",".join(map(str, membership[i])):
                               describe(clean[i])
                               for i in range(len(out))
                               if len(membership[i]) > 1},
        })
        if check:
            determinism["checked"] += 1
            ref = json.dumps(deduplicate([dict(x) for x in items]),
                             ensure_ascii=False, sort_keys=True)
            for seed in range(permutations):
                shuffled = [dict(x) for x in items]
                random.Random(seed).shuffle(shuffled)
                got = json.dumps(deduplicate(shuffled), ensure_ascii=False,
                                 sort_keys=True)
                if got != ref:
                    determinism["permutation_failures"] += 1
                    if len(determinism["examples"]) < 5:
                        determinism["examples"].append(
                            {"file": path.name, "kind": "permutation",
                             "seed": seed})
                    break
            once = json.loads(ref)
            twice = json.dumps(deduplicate([dict(x) for x in once]),
                               ensure_ascii=False, sort_keys=True)
            if twice != ref:
                determinism["idempotence_failures"] += 1
                if len(determinism["examples"]) < 5:
                    determinism["examples"].append(
                        {"file": path.name, "kind": "idempotence"})
    return {"tree": str(tree), "corpora": results,
            "determinism": determinism if check else None}


def run_worker(tree: Path, output_dir: Path, fuzzy: Optional[bool],
               check: bool, permutations: int) -> Dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree)
    env.setdefault("MOISSON_NO_DOTENV", "1")
    if fuzzy is False:
        env["MOISSON_DEDUP_FUZZY"] = "0"
    else:
        env.pop("MOISSON_DEDUP_FUZZY", None)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
           "--tree", str(tree), "--output-dir", str(output_dir),
           "--permutations", str(permutations)]
    if check:
        cmd.append("--check-determinism")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          cwd=str(tree))
    if proc.returncode != 0:
        raise RuntimeError(f"échec de la mesure sur {tree} :\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout)


# === Synthèse ===

def summarize(label: str, run: Dict, inputs: Dict[str, List[Dict]]) -> Dict:
    corpora = run["corpora"]
    n_in = sum(c["n_in"] for c in corpora)
    n_out = sum(c["n_out"] for c in corpora)
    residual = sum(sum(len(g) - 1 for g in c["residual"]) for c in corpora)
    residual_groups = sum(len(c["residual"]) for c in corpora)

    # Paires DOI / sans-DOI du même titre et de la même année, mesurées sur
    # les notices d'entrée (corpus stockés) ; fusionnées si les deux notices
    # finissent dans la même notice de sortie.
    pairs = merged_pairs = 0
    residual_mixed_groups = 0
    for c in corpora:
        items = inputs[c["file"]]
        owner: Dict[int, int] = {}
        for gi, g in enumerate(c["groups"]):
            for m in g:
                owner[m] = gi
        for g in title_year_groups(items):
            with_doi = [i for i in g if item_doi(items[i])]
            without = [i for i in g if not item_doi(items[i])]
            for a in with_doi:
                for b in without:
                    pairs += 1
                    if a in owner and owner.get(a) == owner.get(b):
                        merged_pairs += 1
        for recs in c["residual_records"]:
            dois = [bool(r["doi"]) for r in recs]
            if any(dois) and not all(dois):
                residual_mixed_groups += 1

    biggest = max(corpora, key=lambda c: c["bytes"]) if corpora else None
    slowest = max(corpora, key=lambda c: c["seconds"]) if corpora else None
    return {
        "label": label,
        "corpora": len(corpora),
        "items_in": n_in,
        "items_out": n_out,
        "merged_away": n_in - n_out,
        "residual_title_year": residual,
        "residual_title_year_groups": residual_groups,
        "residual_pct": round(100.0 * residual / max(n_out, 1), 2),
        "doi_nodoi_pairs_in_input": pairs,
        "doi_nodoi_pairs_merged": merged_pairs,
        "residual_groups_mixing_doi_and_no_doi": residual_mixed_groups,
        "biggest_corpus": ({"file": biggest["file"], "bytes": biggest["bytes"],
                            "items": biggest["n_in"],
                            "seconds": biggest["seconds"]} if biggest else None),
        "slowest_corpus": ({"file": slowest["file"], "items": slowest["n_in"],
                            "seconds": slowest["seconds"]} if slowest else None),
        "total_seconds": round(sum(c["seconds"] for c in corpora), 3),
        "determinism": run.get("determinism"),
    }


def build_samples(run: Dict, inputs: Dict[str, List[Dict]], n: int,
                  seed: int) -> Dict[str, List]:
    """Échantillons à graine fixe : fusions nouvelles et résiduels restants."""
    merges = []
    residuals = []
    for c in run["corpora"]:
        items = inputs[c["file"]]
        for g in c["groups"]:
            merges.append({
                "file": c["file"],
                "members": [describe(items[i]) for i in g],
                "merged": c["merged_records"].get(",".join(map(str, g))),
            })
        for recs in c["residual_records"]:
            residuals.append({"file": c["file"], "records": recs})
    rng = random.Random(seed)
    rng.shuffle(merges)
    rng.shuffle(residuals)
    return {"merges_total": len(merges), "residuals_total": len(residuals),
            "merges": merges[:n], "residuals": residuals[:n]}


def group_diff(before: Dict, after: Dict, inputs: Dict[str, List[Dict]]) -> Dict:
    """
    Groupes qui changent entre deux versions du code, corpus par corpus, pour
    un examen **exhaustif** (et non un échantillon) : ``new`` liste les
    regroupements de la version « après » absents de la version « avant »
    (fusions nouvelles ou agrandies), avec leurs notices d'entrée, la notice
    fusionnée et les groupes d'avant qu'ils réunissent ; ``lost`` liste les
    regroupements d'avant disparus (scissions). Un groupe agrandi figure dans
    les deux listes.
    """
    before_by_file = {c["file"]: c for c in before["corpora"]}
    new: List[Dict] = []
    lost: List[Dict] = []
    for c in after["corpora"]:
        b = before_by_file.get(c["file"])
        if b is None:
            continue
        items = inputs[c["file"]]
        g_after = {tuple(g) for g in c["groups"]}
        g_before = {tuple(g) for g in b["groups"]}
        for g in sorted(g_after - g_before):
            members = set(g)
            new.append({
                "file": c["file"],
                "members": [describe(items[i]) for i in g],
                "merged": c["merged_records"].get(",".join(map(str, g))),
                "previous_groups": [list(pg) for pg in sorted(g_before)
                                    if members & set(pg)],
            })
        for g in sorted(g_before - g_after):
            lost.append({
                "file": c["file"],
                "members": [describe(items[i]) for i in g],
                "merged_before": b["merged_records"].get(",".join(map(str, g))),
            })
    return {"new_total": len(new), "lost_total": len(lost),
            "new": new, "lost": lost}


def format_table(summaries: List[Dict]) -> str:
    rows = [
        ("Corpus", "corpora"),
        ("Notices en entrée", "items_in"),
        ("Notices en sortie", "items_out"),
        ("Notices fusionnées", "merged_away"),
        ("Doublons résiduels titre+année", "residual_title_year"),
        ("  en % de la sortie", "residual_pct"),
        ("Paires DOI / sans-DOI (entrée)", "doi_nodoi_pairs_in_input"),
        ("  dont fusionnées", "doi_nodoi_pairs_merged"),
        ("Groupes résiduels DOI + sans-DOI", "residual_groups_mixing_doi_and_no_doi"),
        ("Durée totale (s)", "total_seconds"),
    ]
    width = max(len(r[0]) for r in rows) + 2
    head = "".ljust(width) + "".join(s["label"].rjust(16) for s in summaries)
    lines = [head]
    for title, key in rows:
        lines.append(title.ljust(width)
                     + "".join(str(s[key]).rjust(16) for s in summaries))
    for s in summaries:
        b = s["biggest_corpus"]
        if b:
            lines.append(f"[{s['label']}] plus gros corpus : {b['file']} "
                         f"({b['bytes']} octets, {b['items']} notices) en "
                         f"{b['seconds']} s ; plus lent : "
                         f"{s['slowest_corpus']['file']} en "
                         f"{s['slowest_corpus']['seconds']} s")
        d = s.get("determinism")
        if d:
            lines.append(f"[{s['label']}] déterminisme : {d['checked']} corpus, "
                         f"{d['permutation_failures']} écart(s) de permutation, "
                         f"{d['idempotence_failures']} écart(s) d'idempotence")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "output")
    ap.add_argument("--baseline-tree", type=Path, default=None,
                    help="copie du code d'avant chantier (mesure « avant »)")
    ap.add_argument("--tree", type=Path, default=PROJECT_ROOT,
                    help="arbre mesuré « après » (défaut : ce projet)")
    ap.add_argument("--no-fuzzy-run", action="store_true",
                    help="ne pas mesurer la variante sans passe floue")
    ap.add_argument("--check-determinism", action="store_true")
    ap.add_argument("--permutations", type=int, default=3)
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument("--samples-out", type=Path, default=None)
    ap.add_argument("--diff-out", type=Path, default=None,
                    help="groupes qui changent entre « avant » et « après » "
                         "(examen exhaustif ; exige --baseline-tree)")
    ap.add_argument("--samples", type=int, default=60)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    output_dir = args.output_dir.resolve()
    if args.worker:
        res = worker(args.tree.resolve(), output_dir, args.check_determinism,
                     args.permutations)
        sys.stdout.write(json.dumps(res, ensure_ascii=False))
        return 0

    corpora = list_corpora(output_dir)
    inputs = {p.name: load_items(p) for p in corpora}
    print(f"{len(corpora)} corpus, {sum(len(v) for v in inputs.values())} "
          f"notices dans {output_dir.name}/", file=sys.stderr)

    # « Stocké » : les corpus tels qu'ils sont sur disque (sans re-déduplication).
    stored = {"label": "stocké", "corpora": len(corpora),
              "items_in": sum(len(v) for v in inputs.values())}
    stored_res = sum(sum(len(g) - 1 for g in title_year_groups(v))
                     for v in inputs.values())
    stored["residual_title_year"] = stored_res
    stored["residual_pct"] = round(100.0 * stored_res / max(stored["items_in"], 1), 2)

    runs = []
    if args.baseline_tree:
        runs.append(("avant", run_worker(args.baseline_tree.resolve(),
                                         output_dir, None, False, 0)))
    runs.append(("après", run_worker(args.tree.resolve(), output_dir, None,
                                     args.check_determinism,
                                     args.permutations)))
    if not args.no_fuzzy_run:
        runs.append(("après sans floue",
                     run_worker(args.tree.resolve(), output_dir, False,
                                False, 0)))

    summaries = [summarize(label, run, inputs) for label, run in runs]
    print(f"Stocké : {stored['items_in']} notices, {stored_res} doublons "
          f"résiduels titre+année ({stored['residual_pct']} %)")
    print(format_table(summaries))

    if args.json_out:
        args.json_out.write_text(json.dumps(
            {"output_dir": output_dir.name, "stored": stored,
             "summaries": summaries}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    if args.samples_out:
        after = dict(runs)["après"]
        args.samples_out.write_text(json.dumps(
            build_samples(after, inputs, args.samples, args.seed),
            ensure_ascii=False, indent=1), encoding="utf-8")
    if args.diff_out:
        by_label = dict(runs)
        if "avant" not in by_label:
            print("--diff-out exige --baseline-tree", file=sys.stderr)
            return 2
        diff = group_diff(by_label["avant"], by_label["après"], inputs)
        print(f"Groupes nouveaux ou agrandis : {diff['new_total']} ; "
              f"groupes disparus ou scindés : {diff['lost_total']}")
        args.diff_out.write_text(json.dumps(diff, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
