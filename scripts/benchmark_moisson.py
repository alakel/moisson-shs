#!/usr/bin/env python
"""
Banc de mesure du moissonneur SHS (OPT-00) : référence mesurable et
scénarios simulés rejouables.

Principe
--------
Chaque mesure s'exécute dans un processus neuf (état des modules remis à
zéro, pic mémoire propre à la mesure). Les requêtes HTTP sont comptées au
point de passage de tout ``requests`` (``HTTPAdapter.send``, sous le cache
``requests-cache``) ; en mode simulé, un simulateur d'API installé au même
point répond avec la sémantique de pagination de chaque fournisseur (HAL,
Isidore, CrossRef, OpenAlex, Semantic Scholar, CORE, schéma Zotero) et
injecte les pannes du scénario. Aucune socket réelle n'est ouverte en mode
simulé (garde-socket actif). Le mode live n'appelle que des API gratuites,
jamais SerpAPI (refus au point de passage), sans cache HTTP.

Métriques par mesure : durée (médiane, min, max sur ``--repeat``), requêtes
par hôte, pages, reprises (retries), téléchargements redondants, 429,
erreurs, tentatives de téléchargement du schéma Zotero, sommeils cumulés
(politesse, backoff), durées par étape (sources, déduplication, export CSL,
statistiques, retour MCP, validation Zotero), notices reçues / renvoyées par
les sources / uniques, doublons résiduels (titre + année ; vérité terrain en
simulé), pic mémoire, taille du retour MCP en octets.

Usage (depuis la racine du projet, interpréteur du venv)
--------------------------------------------------------
Lister les scénarios ::

    venv/bin/python scripts/benchmark_moisson.py --list

Tous les scénarios simulés, latence nulle, sur l'arbre de travail ::

    venv/bin/python scripts/benchmark_moisson.py --json-out /tmp/bench.json

Comparer un lot à la baseline (mêmes commandes que la baseline ; résumé de
référence versionné dans
``tests/fixtures/bench/baseline_2026-09-29.summary.json`` : copie d'avant
chantier remesurée avec le simulateur actuel ; le résumé du 2026-09-28,
conservé, date des définitions de scénarios d'alors) ::

    venv/bin/python scripts/benchmark_moisson.py --scenario reference --entry both --repeat 5 --latency-ms 0,150 --json-out /tmp/lot_reference.json
    venv/bin/python scripts/benchmark_moisson.py --scenario all --latency-ms 0,150 --json-out /tmp/lot_scenarios.json
    venv/bin/python scripts/benchmark_moisson.py --merge /tmp/lot_reference.json /tmp/lot_scenarios.json --json-out /tmp/lot.json
    venv/bin/python scripts/benchmark_moisson.py --compare tests/fixtures/bench/baseline_2026-09-29.summary.json /tmp/lot.json

Essai rapide après une modification (attentes de politesse instantanées :
compteurs exacts, durées non réalistes). Le résumé versionné contient une
série de référence à ``--sleep-scale 0`` (latence 0) : ``--compare`` apparie
les groupes sur leurs conditions de mesure et ne rapporte jamais une durée
entre séries de ``sleep_scale`` ou de ``workers`` différents ::

    venv/bin/python scripts/benchmark_moisson.py --scenario all --sleep-scale 0 --json-out /tmp/rapide.json
    venv/bin/python scripts/benchmark_moisson.py --compare tests/fixtures/bench/baseline_2026-09-29.summary.json /tmp/rapide.json

Couverture : en simulé, les travaux (vérité terrain) renvoyés par chaque
source et ceux du corpus final sont conservés ; ``--compare`` liste les
travaux perdus et gagnés (« couverture identique » par identifiants).

Mesurer une autre copie du code (ex. copie intacte d'avant le chantier) ::

    venv/bin/python scripts/benchmark_moisson.py --tree /chemin/vers/copie --scenario reference

Exécution réelle (réseau, à froid, sans cache, API gratuites seulement,
jamais SerpAPI) : à réserver aux vérifications, une moisson de référence
télécharge des centaines de pages Isidore de 4,6 Mo ::

    venv/bin/python scripts/benchmark_moisson.py --live --scenario reference --entry mcp --json-out /tmp/live.json

Options utiles : ``--entry mcp|cli|tool|both`` (``tool`` passe par
``call_tool("moisson_search")`` et mesure les valeurs par défaut de l'outil),
``--workers N`` (impose le parallélisme), ``--sleep-scale F`` (facteur des
attentes du code mesuré), ``--tracemalloc`` (pic d'allocation Python,
ralentit la mesure), ``--trace`` (trace requête par requête),
``--live-keys`` (clés de l'environnement en live, jamais SerpAPI),
``--no-parse-records``, ``--summary-only`` (avec ``--merge``),
``--keep-output``, ``--work-dir``, ``--timeout``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bench import scenarios as sc  # noqa: E402
from bench.instrument import BenchError  # noqa: E402


# === Spécification d'une mesure ===

def make_spec(name: str, scenario: Dict[str, Any], entry: str, mode: str,
              tree: Path, latency_ms: float, opts: argparse.Namespace,
              run_dir: Path) -> Dict[str, Any]:
    """Spécification sérialisable d'une mesure (transmise au processus fils)."""
    params = dict(scenario["live"] if mode == "live" else scenario["params"])
    if opts.workers is not None:
        params["workers"] = opts.workers
    spec: Dict[str, Any] = {
        "scenario": name,
        "entry": entry,
        "mode": mode,
        "tree": str(tree),
        "params": params,
        "run_dir": str(run_dir),
        "latency_ms": latency_ms if mode == "sim" else None,
        "sleep_scale": opts.sleep_scale,
        "tracemalloc": opts.tracemalloc,
        "forbid_sockets": mode == "sim",
        "keep_trace": opts.trace,
        "parse_records": not getattr(opts, "no_parse_records", False),
        "live_keys": getattr(opts, "live_keys", False),
        "env": dict(scenario.get("env") or {}),
        "warmup": bool(scenario.get("warmup")),
    }
    if mode == "sim":
        sim = json.loads(json.dumps(scenario["sim"]))
        lat = {"*": latency_ms}
        scen_lat = sim.get("latency_ms")
        if isinstance(scen_lat, dict):
            lat.update(scen_lat)
        elif scen_lat is not None:
            lat["*"] = scen_lat
        sim["latency_ms"] = lat
        spec["sim"] = sim
    return spec


def run_in_subprocess(spec: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """Lance la mesure dans un processus neuf et renvoie ses métriques."""
    from bench.runner import build_env
    run_dir = Path(spec["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    spec_path = run_dir / "spec.json"
    out_path = run_dir / "result.json"
    log_path = run_dir / "run.log"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    env = build_env(spec, run_dir)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_worker",
           str(spec_path), str(out_path)]
    t0 = time.perf_counter()
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(cmd, env=env, cwd=str(run_dir), stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    wall = time.perf_counter() - t0
    if out_path.is_file():
        result = json.loads(out_path.read_text(encoding="utf-8"))
    else:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
        result = {"scenario": spec["scenario"], "entry": spec["entry"],
                  "mode": spec["mode"], "latency_ms": spec.get("latency_ms"),
                  "bench_error": f"processus de mesure sans résultat (code {rc})",
                  "log_tail": tail}
    result["process_wall_s"] = round(wall, 3)
    result["log"] = str(log_path)
    return result


def worker_main(spec_path: str, out_path: str) -> int:
    """Processus fils : exécute une mesure et écrit ses métriques en JSON."""
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    from bench.runner import execute_run
    try:
        result = execute_run(spec)
    except BenchError as e:
        result = {"scenario": spec.get("scenario"), "entry": spec.get("entry"),
                  "mode": spec.get("mode"), "latency_ms": spec.get("latency_ms"),
                  "bench_error": str(e)}
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False, default=str),
                              encoding="utf-8")
    return 0


# === Agrégation ===

def _median(values: List[float]) -> Optional[float]:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(vals), 4) if vals else None


def _host(run: Dict, host: str, key: str, default=0):
    return (((run.get("network") or {}).get("hosts") or {}).get(host) or {}).get(key, default)


def _coverage_digest(run: Dict[str, Any]) -> Optional[tuple]:
    cov = run.get("coverage")
    if not cov:
        return None
    final = (cov.get("final") or {}).get("digest")
    return (final,) + tuple(sorted((k, (v or {}).get("digest"))
                                   for k, v in (cov.get("sources") or {}).items()))


def summarize(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Résumé d'un groupe de répétitions (même scénario, entrée, latence).

    Définition des reprises : les mesures prises avant la revue d'OPT-00
    (compteurs réseau sans ``repeats_after_failure``) comptaient comme
    reprise toute requête « même URL que la précédente du fil, après un
    échec », y compris des appels indépendants (validations Zotero, proxies
    Isidore successifs). Leur valeur n'est PAS présentée comme ``retries``
    mais sous ``retries_v1`` (définition initiale), pour qu'aucune
    comparaison ne mette en regard deux définitions différentes.
    """
    ok = [r for r in runs if not r.get("bench_error")]
    if not ok:
        return {"runs": len(runs), "errors": [r.get("bench_error") for r in runs]}
    first = ok[0]
    durations = [r["duration_s"] for r in ok]
    net = first.get("network") or {}
    hosts = net.get("hosts") or {}
    total = net.get("total") or {}
    v1 = "repeats_after_failure" not in total
    variable = sorted({
        key for key in ("requests", "retries", "duplicate_fetches", "records")
        if len({(r.get("network") or {}).get("total", {}).get(key) for r in ok}) > 1
    })
    if len({_coverage_digest(r) for r in ok}) > 1:
        variable.append("coverage")
    caveats = []
    for r in ok:
        for c in r.get("caveats") or []:
            if c not in caveats:
                caveats.append(c)
    corrections = next((r["corrections"] for r in ok if r.get("corrections")), None)
    ret = first.get("mcp_return") or {}
    items = first.get("items") or {}
    corpus = first.get("corpus") or {}
    gt = ((first.get("ground_truth") or {}).get("output")) or {}
    stages = {}
    for st in (first.get("stages") or {}).get("stages", {}):
        stages[st] = _median([((r.get("stages") or {}).get("stages", {}).get(st) or {})
                              .get("total_s") for r in ok])
    n_items = ret.get("item_count") or corpus.get("items")
    return {
        "runs": len(runs),
        "failed_runs": len(runs) - len(ok),
        "harvest_errors": sorted({r["error"] for r in ok if r.get("error")}),
        "duration_s": {"median": _median(durations), "min": round(min(durations), 4),
                       "max": round(max(durations), 4)},
        "duration_adjusted_s": _median([r.get("duration_adjusted_s") for r in ok]),
        "requests_total": net.get("total", {}).get("requests"),
        "requests_by_host": {h: v["requests"] for h, v in hosts.items()},
        "pages_by_host": {h: v["ok"] for h, v in hosts.items()},
        "isidore_pages": _host(first, "api.isidore.science", "ok"),
        "isidore_duplicate_fetches": _host(first, "api.isidore.science", "duplicate_fetches"),
        "isidore_requests_by_source": {
            src: hosts_.get("api.isidore.science")
            for src, hosts_ in (net.get("by_source") or {}).items()
            if hosts_.get("api.isidore.science")
        },
        "non_json_200": total.get("non_json_200"),
        "body_errors": total.get("body_errors"),
        "http_cache": first.get("http_cache"),
        "warmup": first.get("warmup"),
        "counter_definition": 1 if v1 else 2,
        "retries": None if v1 else total.get("retries"),
        "retries_v1": total.get("retries") if v1 else None,
        "repeats_after_failure": total.get("repeats_after_failure"),
        "duplicate_fetches": net.get("total", {}).get("duplicate_fetches"),
        "status_429": net.get("total", {}).get("status_429"),
        "errors_http": net.get("total", {}).get("errors_http"),
        "exceptions": net.get("total", {}).get("exceptions"),
        "schema_attempts": _host(first, "api.zotero.org", "requests"),
        "sleep_s": _median([(r.get("sleeps") or {}).get("total_s") for r in ok]),
        "sleep_by_module": (first.get("sleeps") or {}).get("by_module"),
        "items": items,
        "residual_title_year": corpus.get("residual_title_year"),
        "residual_title_year_pct": corpus.get("residual_title_year_pct"),
        "gt_residual_duplicates": gt.get("residual_duplicates"),
        "gt_false_merges": gt.get("false_merges"),
        "gt_works_out": gt.get("works"),
        "gt_works_in": ((first.get("ground_truth") or {}).get("dedup_input") or {}).get("works"),
        "mcp_return_bytes": ret.get("total_bytes"),
        "mcp_embedded_bytes": ret.get("embedded_bytes"),
        "mcp_bytes_per_item": (round(ret["embedded_bytes"] / n_items, 1)
                               if ret.get("embedded_bytes") and n_items else None),
        "mcp_inline_json": ret.get("inline_json"),
        "maxrss_mb": _median([(r.get("memory") or {}).get("maxrss_after_bytes", 0) / 1e6
                              for r in ok]),
        "maxrss_delta_mb": _median([(r.get("memory") or {}).get("maxrss_delta_bytes", 0) / 1e6
                                    for r in ok]),
        "stages_s": stages,
        "sources": (first.get("stages") or {}).get("sources"),
        # Statut par source (moteur OPT-07 ; absent pour une copie plus
        # ancienne) : complete, empty, partial, skipped, error, cancelled.
        "source_statuses": _source_statuses(first),
        "mcp_is_error": ret.get("is_error"),
        "connector_calls": (first.get("stages") or {}).get("connector_calls"),
        "missing_instrumentation": (first.get("stages") or {}).get("missing"),
        "variable_counters": variable,
        "real_socket_attempts": sum(len(r.get("real_socket_attempts") or []) for r in ok),
        "coverage": first.get("coverage"),
        "caveats": caveats,
        "corrections": corrections,
    }


def _source_statuses(run: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """``{source: "statut" ou "statut/motif"}`` d'une mesure, ou None."""
    out = {}
    for x in (run.get("stages") or {}).get("sources") or []:
        if x.get("status") is None:
            continue
        label = x["status"] + (f"/{x['reason']}" if x.get("reason") else "")
        out[x["source"]] = label
    return out or None


def _fmt_bytes(n) -> str:
    if n is None:
        return "-"
    if n >= 1e6:
        return f"{n / 1e6:.2f} Mo"
    if n >= 1e3:
        return f"{n / 1e3:.1f} Ko"
    return f"{n} o"


def format_table(groups: List[Dict[str, Any]]) -> str:
    """Tableau texte lisible des groupes de mesures."""
    head = (f"{'scénario':<28} {'entrée':<6} {'lat.':>5} {'n':>2} "
            f"{'durée méd. [min-max] s':>26} {'req.':>6} {'Isid.p':>6} "
            f"{'redond':>6} {'repr.':>5} {'429':>4} {'err':>4} {'schéma':>6} "
            f"{'brut→uniq':>11} {'dbl.rés':>7} {'retour MCP':>11} {'RSS Mo':>7}")
    lines = [head, "-" * len(head)]
    for g in groups:
        s = g["summary"]
        lat = "live" if g["mode"] == "live" else f"{g['latency_ms']:g}"
        if "duration_s" not in s:
            lines.append(f"{g['scenario']:<28} {g['entry']:<6} {lat:>5} "
                         f"ÉCHEC : {'; '.join(str(e) for e in s.get('errors', []))[:120]}")
            continue
        d = s["duration_s"]
        dur = f"{d['median']:.2f} [{d['min']:.2f}-{d['max']:.2f}]"
        it = s["items"] or {}
        raw = it.get("returned_by_sources")
        uniq = it.get("final_corpus")
        bu = f"{raw if raw is not None else '-'}→{uniq if uniq is not None else '-'}"
        res = s.get("gt_residual_duplicates")
        res = s.get("residual_title_year") if res is None else res
        if s.get("retries") is not None:
            rep = str(s["retries"])
        elif s.get("retries_v1") is not None:
            rep = f"{s['retries_v1']}*"  # définition initiale (cf. légende)
        else:
            rep = "0"
        if s.get("caveats"):
            dur += " !"
        lines.append(
            f"{g['scenario']:<28} {g['entry']:<6} {lat:>5} {s['runs']:>2} "
            f"{dur:>26} {s['requests_total'] or 0:>6} {s['isidore_pages']:>6} "
            f"{s['duplicate_fetches'] or 0:>6} {rep:>5} "
            f"{s['status_429'] or 0:>4} {(s['errors_http'] or 0) + (s['exceptions'] or 0):>4} "
            f"{s['schema_attempts']:>6} {bu:>11} {res if res is not None else '-':>7} "
            f"{_fmt_bytes(s['mcp_return_bytes']):>11} {s['maxrss_mb'] or 0:>7.0f}"
        )
    lines.append("")
    lines.append("Légende : req. = requêtes réseau (tous hôtes) ; Isid.p = pages Isidore "
                 "téléchargées ; redond = téléchargements redondants (URL déjà servie) ; "
                 "repr. = reprises (retry : même URL, même fil, même appel logique, après "
                 "un échec ; « N* » = définition initiale, même URL après échec sans "
                 "distinction d'appel, mesures antérieures à la revue) ; err = erreurs "
                 "HTTP + exceptions ; schéma = tentatives de téléchargement du schéma "
                 "Zotero ; dbl.rés = doublons résiduels (vérité terrain en simulé, "
                 "titre+année en live) ; « ! » = mesure assortie de réserves (détail "
                 "ci-dessous).")
    for g in groups:
        s = g["summary"]
        if "duration_s" not in s:
            continue
        lines.append("")
        lat = "live" if g["mode"] == "live" else f"latence {g['latency_ms']:g} ms"
        cond = _describe_conditions(g)
        lines.append(f"== {g['scenario']} / {g['entry']} / {lat}"
                     + (f" / {cond}" if cond else ""))
        for c in s.get("caveats") or []:
            lines.append(f"   RÉSERVE : {c}")
        hosts = ", ".join(f"{h}={n} (pages {s['pages_by_host'].get(h, 0)})"
                          for h, n in s["requests_by_host"].items())
        lines.append(f"   requêtes par hôte : {hosts or '-'}")
        srcs = ", ".join(f"{x['source']}={x['items']} ({x['duration_s']:.1f} s)"
                         for x in (s.get("sources") or []))
        lines.append(f"   sources : {srcs or '-'}")
        if s.get("source_statuses"):
            st_line = ", ".join(f"{k}={v}" for k, v in s["source_statuses"].items())
            lines.append(f"   statuts par source : {st_line}"
                         + (" ; retour MCP en erreur (isError)"
                            if s.get("mcp_is_error") else ""))
        if s.get("isidore_requests_by_source"):
            iso = ", ".join(f"{k}={v}" for k, v in s["isidore_requests_by_source"].items())
            lines.append(f"   requêtes Isidore par source : {iso}")
        if s.get("non_json_200"):
            lines.append(f"   réponses 200 au corps non JSON : {s['non_json_200']}")
        if s.get("body_errors"):
            lines.append(f"   erreurs de lecture du corps (transfert interrompu) : "
                         f"{s['body_errors']}")
        if s.get("repeats_after_failure"):
            lines.append(f"   appels répétés après échec (même URL, autre appel logique, "
                         f"hors reprises) : {s['repeats_after_failure']}")
        cov = s.get("coverage") or {}
        if cov:
            fin = (cov.get("final") or {}).get("count")
            per = ", ".join(f"{k}={(v or {}).get('count')}"
                            for k, v in (cov.get("sources") or {}).items())
            lines.append(f"   couverture (travaux, vérité terrain) : corpus final {fin} ; "
                         f"par source : {per or '-'}")
        hc = s.get("http_cache") or {}
        if hc.get("hits") or hc.get("misses"):
            lines.append(f"   cache HTTP : {hc['hits']} succès, {hc['misses']} échecs")
        if s.get("warmup"):
            w = s["warmup"]
            lines.append(f"   amorçage (non mesuré) : {w.get('duration_s')} s, "
                         f"{w.get('network_requests')} requêtes, sommeils {w.get('sleep_s')} s")
        st = ", ".join(f"{k}={v:.2f} s" for k, v in (s.get("stages_s") or {}).items()
                       if v is not None and k != "sources")
        lines.append(f"   étapes : {st or '-'} ; sommeils cumulés {s['sleep_s']} s ; "
                     f"durée corrigée des surcoûts du banc {s['duration_adjusted_s']} s")
        it = s["items"] or {}
        lines.append(
            f"   notices : reçues {it.get('records_received')}, renvoyées par les sources "
            f"{it.get('returned_by_sources')}, uniques {it.get('final_corpus')} ; "
            f"doublons titre+année {s['residual_title_year']} "
            f"({s['residual_title_year_pct']} %) ; vérité terrain : doublons "
            f"{s['gt_residual_duplicates']}, fusions erronées {s['gt_false_merges']}")
        if s.get("mcp_return_bytes") is not None:
            lines.append(
                f"   retour MCP : {_fmt_bytes(s['mcp_return_bytes'])} (JSON inline "
                f"{_fmt_bytes(s['mcp_embedded_bytes'])}, {s['mcp_bytes_per_item']} o/notice, "
                f"inline={s['mcp_inline_json']})")
        if s.get("harvest_errors"):
            lines.append(f"   erreurs de moisson : {s['harvest_errors']}")
        if s.get("missing_instrumentation"):
            lines.append(f"   non instrumenté (fonction absente) : {s['missing_instrumentation']}")
        if s.get("variable_counters"):
            lines.append(f"   compteurs variables d'une répétition à l'autre : {s['variable_counters']}")
        if s.get("real_socket_attempts"):
            lines.append(f"   ATTENTION : {s['real_socket_attempts']} tentative(s) de socket réelle")
    return "\n".join(lines)


# === Conditions de mesure ===

# Options qui changent les durées (et, pour workers, parfois les compteurs)
# sans apparaître dans la clé scénario / entrée / mode / latence.
CONDITION_KEYS = ("sleep_scale", "workers")


def group_conditions(g: Dict[str, Any], meta: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """
    Conditions de mesure d'un groupe (``sleep_scale``, ``workers`` imposé).

    Lues sur le groupe (fichiers produits depuis la revue d'OPT-00), à défaut
    dans ``meta.options`` d'un fichier d'une seule exécution. None : inconnues
    (ancien fichier fusionné), traitées comme incomparables pour les durées.
    """
    if "sleep_scale" in g:
        return {k: g.get(k) for k in CONDITION_KEYS}
    opts = (meta or {}).get("options")
    if isinstance(opts, dict) and "sleep_scale" in opts:
        return {k: opts.get(k) for k in CONDITION_KEYS}
    return None


def _describe_conditions(g: Dict[str, Any], cond: Optional[Dict] = None) -> str:
    cond = cond if cond is not None else {k: g.get(k) for k in CONDITION_KEYS}
    parts = []
    if cond.get("sleep_scale") is not None:
        parts.append(f"sleep_scale {cond['sleep_scale']:g}")
    if cond.get("workers") is not None:
        parts.append(f"workers {cond['workers']}")
    return ", ".join(parts)


# Métriques de --compare : (libellé, extraction, nature). « durée » : dépend
# des conditions de mesure, jamais rapportée entre séries incompatibles ;
# « compteur » : déterministe en simulé, repris (et signalé « * ») d'une autre
# série de référence du même scénario quand le groupe apparié ne le porte pas
# (ex. reprises mesurées avec la définition initiale).
_COMPARE_METRICS = [
    ("durée médiane (s)", lambda s: (s.get("duration_s") or {}).get("median"), "durée"),
    ("requêtes", lambda s: s.get("requests_total"), "compteur"),
    ("pages Isidore", lambda s: s.get("isidore_pages"), "compteur"),
    ("reprises", lambda s: s.get("retries"), "compteur"),
    ("appels répétés après échec", lambda s: s.get("repeats_after_failure"), "compteur"),
    ("erreurs de lecture du corps", lambda s: s.get("body_errors"), "compteur"),
    ("tentatives schéma", lambda s: s.get("schema_attempts"), "compteur"),
    ("notices uniques", lambda s: (s.get("items") or {}).get("final_corpus"), "compteur"),
    ("doublons résiduels (VT)", lambda s: s.get("gt_residual_duplicates"), "compteur"),
    ("fusions erronées (VT)", lambda s: s.get("gt_false_merges"), "compteur"),
    ("retour MCP (o)", lambda s: s.get("mcp_return_bytes"), "compteur"),
    ("RSS (Mo)", lambda s: s.get("maxrss_mb"), "durée"),
]


def _coverage_lines(cov_b: Dict[str, Any], cov_n: Dict[str, Any], note: str = "") -> List[str]:
    """Travaux perdus et gagnés (vérité terrain), corpus final puis par source."""
    from bench import analysis

    def diff(rb, rn):
        a = set(analysis.decode_ids((rb or {}).get("ids")))
        b = set(analysis.decode_ids((rn or {}).get("ids")))
        return a - b, b - a, len(a), len(b)

    def fmt(lost, gained, na, nb):
        if not lost and not gained:
            return f"{na} → {nb}  identique"
        ex = ", ".join(str(x) for x in sorted(lost)[:5])
        return (f"{na} → {nb}  perdus {len(lost)}" + (f" (ex. {ex})" if lost else "")
                + f", gagnés {len(gained)}")

    out = []
    if cov_b.get("final") is not None or cov_n.get("final") is not None:
        out.append(f"   {'couverture finale (travaux)':<30} "
                   f"{fmt(*diff(cov_b.get('final'), cov_n.get('final')))}{note}")
    sb, sn = cov_b.get("sources") or {}, cov_n.get("sources") or {}
    names = sorted(set(sb) | set(sn))
    changed = [(n, diff(sb.get(n), sn.get(n))) for n in names]
    changed = [(n, d) for n, d in changed if d[0] or d[1]]
    if names and not changed:
        out.append(f"   {'couverture par source':<30} identique ({len(names)} sources){note}")
    for n, d in changed:
        out.append(f"   {'couverture ' + n:<30} {fmt(*d)}{note}")
    return out


def compare(base_path: Path, new_path: Path) -> str:
    """
    Compare deux fichiers de résultats (mêmes scénarios, entrées, latences).

    Les groupes sont appariés sur (scénario, entrée, mode, latence), puis, parmi
    les candidats, sur les conditions de mesure (``sleep_scale``, ``workers``).
    Une durée n'est jamais rapportée entre séries de conditions différentes ou
    inconnues ; aucun rapport n'est calculé pour un groupe assorti de réserves
    (``caveats``), affichées en tête du groupe. La couverture (travaux perdus
    et gagnés) est comparée par identifiants quand les deux côtés la portent.
    """
    base = json.loads(base_path.read_text(encoding="utf-8"))
    new = json.loads(new_path.read_text(encoding="utf-8"))
    base_meta, new_meta = base.get("meta") or {}, new.get("meta") or {}

    def key(g):
        return (g["scenario"], g["entry"], g["mode"], g.get("latency_ms"))

    by_key: Dict[tuple, List[Dict]] = {}
    by_scenario: Dict[tuple, List[Dict]] = {}
    for g in base.get("groups", []):
        g = dict(g, _cond=group_conditions(g, base_meta))
        by_key.setdefault(key(g), []).append(g)
        by_scenario.setdefault(key(g)[:3], []).append(g)

    out = [f"Comparaison {base_path.name} → {new_path.name}", ""]
    warnings = 0
    for g in new.get("groups", []):
        s_new = g["summary"]
        cond_n = group_conditions(g, new_meta)
        lat = "live" if g["mode"] == "live" else f"{g.get('latency_ms')} ms"
        out.append(f"== {g['scenario']} / {g['entry']} / {lat}"
                   + (f" / {_describe_conditions(g, cond_n)}" if cond_n else ""))
        cands = by_key.get(key(g), [])
        exact = [b for b in cands if cond_n is not None and b["_cond"] == cond_n]
        b = exact[0] if exact else (cands[0] if cands else None)
        if b is None:
            out.append("   (absent de la référence)")
            continue
        s_base = b["summary"]
        same_cond = bool(exact)
        workers_differ = (cond_n is None or b["_cond"] is None
                          or cond_n.get("workers") != b["_cond"].get("workers"))
        caveats = [("référence", c) for c in s_base.get("caveats") or []]
        caveats += [("nouvelle mesure", c) for c in s_new.get("caveats") or []]
        if not same_cond:
            warnings += 1
            out.append(
                "   ATTENTION : conditions de mesure différentes ou inconnues "
                f"(référence : {_describe_conditions(b, b['_cond']) if b['_cond'] else 'inconnues'} ; "
                f"nouvelle : {_describe_conditions(g, cond_n) if cond_n else 'inconnues'}) : "
                "durées non comparables" + (", compteurs indicatifs" if workers_differ else ""))
        for side, c in caveats:
            warnings += 1
            out.append(f"   RÉSERVE ({side}) : {c}")
        if caveats:
            out.append("   (aucun rapport calculé pour ce groupe : mesure assortie de réserves)")
        # Autres séries de référence du même scénario, pour les compteurs
        # absents du groupe apparié (latence identique d'abord).
        others = [x for x in cands if x is not b] + [
            x for x in by_scenario.get(key(g)[:3], [])
            if x not in cands]
        footnotes = []
        for label, fn, nature in _COMPARE_METRICS:
            vb, vn = fn(s_base), fn(s_new)
            mark = ""
            if vb is None and nature == "compteur":
                alt = next((x for x in others if fn(x["summary"]) is not None), None)
                if alt is not None:
                    vb = fn(alt["summary"])
                    mark = "*"
                    desc = (f"latence {alt.get('latency_ms')} ms"
                            + (f", {_describe_conditions(alt, alt['_cond'])}"
                               if alt["_cond"] else ""))
                    if desc not in footnotes:
                        footnotes.append(desc)
            if vb is None and vn is None:
                continue
            comparable = not caveats and (
                (nature == "durée" and same_cond)
                or (nature == "compteur" and not workers_differ))
            ratio = ""
            if (comparable and isinstance(vb, (int, float))
                    and isinstance(vn, (int, float)) and vb):
                ratio = f"  (x{vn / vb:.2f})"
            elif nature == "durée" and not same_cond:
                ratio = "  (non comparable)"
            out.append(f"   {label:<30} {str(vb) + mark:>12} → {vn!s:<12}{ratio}")
        # Couverture par identifiants (vérité terrain, scénarios simulés).
        cov_n = s_new.get("coverage")
        cov_b, note = s_base.get("coverage"), ""
        if cov_n and not cov_b:
            alt = next((x for x in others if x["summary"].get("coverage")), None)
            if alt is not None:
                cov_b, note = alt["summary"]["coverage"], " *"
                desc = (f"latence {alt.get('latency_ms')} ms"
                        + (f", {_describe_conditions(alt, alt['_cond'])}"
                           if alt["_cond"] else ""))
                if desc not in footnotes:
                    footnotes.append(desc)
        st_b, st_n = s_base.get("source_statuses"), s_new.get("source_statuses")
        if st_b or st_n:
            names = list(dict.fromkeys(list(st_b or {}) + list(st_n or {})))
            out.append("   statuts par source : " + " ; ".join(
                f"{n} {(st_b or {}).get(n, '—')} → {(st_n or {}).get(n, '—')}"
                for n in names))
        if cov_n and cov_b:
            out.extend(_coverage_lines(cov_b, cov_n, note))
        elif cov_b and not cov_n:
            out.append("   couverture : absente de la nouvelle mesure")
        for desc in footnotes:
            out.append(f"   * valeur de référence prise dans la série {desc} "
                       "(compteur déterministe en simulé)")
    if warnings:
        out.insert(1, f"ATTENTION : {warnings} avertissement(s) (conditions de mesure "
                      "différentes ou réserves) : lire les groupes signalés.")
    return "\n".join(out)


# === Métadonnées ===

def tree_digest(tree: Path) -> str:
    """Empreinte des sources Python de la copie mesurée (hors tests et venv)."""
    h = hashlib.sha256()
    for p in sorted(tree.rglob("*.py")):
        rel = p.relative_to(tree).as_posix()
        if rel.startswith(("venv/", "tests/", ".", "scripts/")) or "__pycache__" in rel:
            continue
        h.update(rel.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def git_head(tree: Path) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(tree), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        dirty = subprocess.run(["git", "-C", str(tree), "status", "--porcelain",
                                "--untracked-files=no"], capture_output=True, text=True,
                               timeout=10).stdout.strip()
        return out.stdout.strip() + ("+modifications" if dirty else "")
    except Exception:
        return None


# === Programme principal ===

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Banc de mesure du moissonneur SHS (OPT-00).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--scenario", action="append", default=None,
                   help="Scénario(s) à exécuter (répétable ou liste séparée par des "
                        "virgules ; 'all' = tous). Défaut : tous en simulé, "
                        "'reference' en live.")
    p.add_argument("--repeat", type=int, default=1,
                   help="Répétitions par scénario (médiane, min, max des durées).")
    p.add_argument("--latency-ms", default="0",
                   help="Latence simulée par requête, en ms ; liste séparée par des "
                        "virgules pour plusieurs séries (ex. 0,150).")
    p.add_argument("--tree", type=Path, default=PROJECT_ROOT,
                   help="Copie du code à mesurer (défaut : la racine de ce dépôt).")
    p.add_argument("--live", action="store_true",
                   help="Exécution réelle (réseau, sans cache, API gratuites seulement).")
    p.add_argument("--live-keys", action="store_true",
                   help="En live, conserver les clés API de l'environnement "
                        "(jamais SerpAPI). Défaut : aucune clé.")
    p.add_argument("--entry", choices=["scenario", "mcp", "cli", "tool", "both"],
                   default="scenario",
                   help="Point d'entrée mesuré (défaut : ceux du scénario ; both = mcp+cli).")
    p.add_argument("--workers", type=int, default=None,
                   help="Impose le nombre de workers (défaut : valeur du point d'entrée).")
    p.add_argument("--sleep-scale", type=float, default=1.0,
                   help="Facteur appliqué aux time.sleep du code mesuré (0 = instantané, "
                        "durées non réalistes).")
    p.add_argument("--tracemalloc", action="store_true",
                   help="Mesure le pic d'allocation Python (ralentit l'exécution).")
    p.add_argument("--no-parse-records", action="store_true",
                   help="En live, ne pas décoder les réponses pour compter les notices "
                        "reçues (surcoût ~0,03 s par page Isidore de 4,6 Mo, retranché "
                        "de la durée corrigée).")
    p.add_argument("--trace", action="store_true",
                   help="Conserve la trace requête par requête dans le JSON.")
    p.add_argument("--json-out", type=Path, default=None,
                   help="Fichier JSON des résultats bruts et résumés.")
    p.add_argument("--work-dir", type=Path, default=None,
                   help="Répertoire des mesures (défaut : temporaire).")
    p.add_argument("--keep-output", action="store_true",
                   help="Conserver les corpus et journaux des mesures.")
    p.add_argument("--timeout", type=float, default=3600.0,
                   help="Délai maximal d'une mesure, en secondes.")
    p.add_argument("--list", action="store_true", help="Lister les scénarios et quitter.")
    p.add_argument("--compare", nargs=2, type=Path, metavar=("REFERENCE", "NOUVEAU"),
                   help="Comparer deux fichiers --json-out et quitter.")
    p.add_argument("--merge", nargs="+", type=Path, metavar="FICHIER",
                   help="Fusionner des fichiers --json-out dans celui de --json-out "
                        "(groupes concaténés, métadonnées conservées) et quitter.")
    p.add_argument("--summary-only", action="store_true",
                   help="Avec --merge : n'écrire que les résumés (sans les mesures "
                        "brutes ni chemin absolu dans les métadonnées), fichier "
                        "compact comparable par --compare, destiné au dépôt.")
    return p.parse_args(argv)


def neutral_meta(obj: Any) -> Any:
    """
    Copie de métadonnées sans chemin absolu : chaque chaîne qui est un chemin
    absolu est réduite à son nom de base (``/…/baseline_tree`` →
    ``baseline_tree``). Pour tout fichier destiné au dépôt (``--summary-only``) :
    ni répertoire de travail, ni compte local, ni arborescence du poste.
    """
    if isinstance(obj, dict):
        return {k: neutral_meta(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [neutral_meta(v) for v in obj]
    if isinstance(obj, str) and os.path.isabs(obj) and len(Path(obj).parts) > 1:
        return Path(obj).name
    return obj


def merge_results(paths: List[Path], out: Path, summary_only: bool = False) -> Dict[str, Any]:
    """
    Fusionne plusieurs fichiers de résultats en un seul.

    Les conditions de mesure (``sleep_scale``, ``workers``) de chaque fichier
    d'une seule exécution sont reportées sur ses groupes, pour que
    ``--compare`` les voie encore après fusion. Avec ``summary_only``, les
    mesures brutes sont retirées et les métadonnées rendues neutres
    (``neutral_meta``).
    """
    metas, groups, tables = [], [], []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = dict(data.get("meta") or {})
        meta["source_file"] = path.name
        metas.append(meta)
        for g in data.get("groups") or []:
            # Résumé recalculé depuis les mesures brutes quand elles sont
            # présentes : cohérent avec la version courante de l'agrégation.
            if g.get("runs"):
                g["summary"] = summarize(g["runs"])
            cond = group_conditions(g, data.get("meta"))
            if cond is not None:
                for k, v in cond.items():
                    g.setdefault(k, v)
            if summary_only:
                g = {k: v for k, v in g.items() if k != "runs"}
            groups.append(g)
        if data.get("table"):
            tables.append(data["table"])
    merged_meta = {"merged": metas, "date": datetime.now().isoformat(timespec="seconds")}
    if summary_only:
        merged_meta = neutral_meta(merged_meta)
    merged = {"meta": merged_meta, "groups": groups, "table": format_table(groups)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    return merged


def select_scenarios(opts: argparse.Namespace) -> List[str]:
    raw = []
    for s in opts.scenario or []:
        raw.extend(x.strip() for x in s.split(",") if x.strip())
    if not raw:
        return ["reference"] if opts.live else sc.names()
    if "all" in raw:
        return sc.live_names() if opts.live else sc.names()
    for name in raw:
        sc.get(name)  # lève une erreur claire si inconnu
    return raw


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--_worker"]:
        return worker_main(argv[1], argv[2])
    opts = parse_args(argv)
    if opts.list:
        for name in sc.names() + ["smoke"]:
            s = sc.get(name)
            live = " [live possible]" if s.get("live") else ""
            print(f"{name:<28} {s['description']}{live}")
        return 0
    if opts.compare:
        print(compare(*opts.compare))
        return 0
    if opts.merge:
        if not opts.json_out:
            print("--merge exige --json-out (fichier fusionné)", file=sys.stderr)
            return 2
        merged = merge_results(opts.merge, opts.json_out, opts.summary_only)
        print(merged["table"])
        return 0

    tree = opts.tree.resolve()
    if not (tree / "main.py").is_file():
        print(f"--tree {tree} : main.py introuvable", file=sys.stderr)
        return 2
    names = select_scenarios(opts)
    mode = "live" if opts.live else "sim"
    latencies = [float(x) for x in str(opts.latency_ms).split(",") if x.strip()]
    if mode == "live":
        latencies = [0.0]
    work = opts.work_dir or Path(tempfile.mkdtemp(prefix="moisson_bench_"))
    work.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    groups = []
    for name in names:
        scenario = sc.get(name)
        if mode == "live" and not scenario.get("live"):
            print(f"[banc] {name} : pas de variante live, ignoré", file=sys.stderr)
            continue
        if opts.entry == "scenario":
            entries = scenario.get("entries") or ["mcp"]
        elif opts.entry == "both":
            entries = ["mcp", "cli"]
        else:
            entries = [opts.entry]
        for lat in latencies:
            for entry in entries:
                runs = []
                for i in range(opts.repeat):
                    run_dir = work / f"{stamp}_{name}_{entry}_{mode}_{lat:g}_{i}"
                    spec = make_spec(name, scenario, entry, mode, tree, lat, opts, run_dir)
                    print(f"[banc] {name} / {entry} / {mode} / latence {lat:g} ms / "
                          f"répétition {i + 1}/{opts.repeat}...", file=sys.stderr, flush=True)
                    res = run_in_subprocess(spec, opts.timeout)
                    if res.get("bench_error"):
                        print(f"[banc]   ERREUR : {res['bench_error']}", file=sys.stderr)
                    else:
                        print(f"[banc]   {res['duration_s']:.2f} s, "
                              f"{res['network']['total']['requests']} requêtes",
                              file=sys.stderr, flush=True)
                    runs.append(res)
                    if not opts.keep_output and not res.get("bench_error"):
                        shutil.rmtree(run_dir, ignore_errors=True)
                groups.append({"scenario": name, "entry": entry, "mode": mode,
                               "latency_ms": lat if mode == "sim" else None,
                               "sleep_scale": opts.sleep_scale,
                               "workers": opts.workers,
                               "summary": summarize(runs), "runs": runs})

    report = format_table(groups)
    print(report)
    if opts.json_out:
        meta = {
            "date": datetime.now().isoformat(timespec="seconds"),
            "tree": str(tree),
            "tree_digest": tree_digest(tree),
            "git": git_head(tree),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "mode": mode,
            "options": {k: (str(v) if isinstance(v, Path) else v)
                        for k, v in vars(opts).items()},
        }
        opts.json_out.parent.mkdir(parents=True, exist_ok=True)
        opts.json_out.write_text(json.dumps({"meta": meta, "groups": groups, "table": report},
                                            ensure_ascii=False, indent=1, default=str),
                                 encoding="utf-8")
        print(f"\n[banc] résultats : {opts.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
