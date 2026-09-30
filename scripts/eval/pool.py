"""
Collecte des candidats du protocole d'évaluation SHS (OPT-15).

Deux étapes séparées, pour que le classement puisse être rejoué sans réseau :

1. ``collect`` (réseau) : pour chaque requête de ``queries_shs.json``, une
   moisson live avec le code actuel (moteur ``engine.orchestrator.harvest``) :
   groupe ``francophone`` ou ``all`` selon la requête, **jamais Google
   Scholar** (source facturée : exclue des groupes, sources facturées
   refusées, clé SerpAPI retirée de l'environnement), **sans clé d'API** par
   défaut (aucun ``.env`` chargé ; OpenAlex et CORE, qui exigent une clé,
   sont alors ``skipped``), mode ``bibliographie``, ``max_per_source`` modéré
   (100), sans cache HTTP, sans écriture dans ``output/raw``. Chaque moisson
   est sauvegardée dans ``output/eval/<date>/snapshots/<id>.json`` : notices
   de chaque source dans l'ordre servi, rangs et scores natifs, statuts,
   plan de requête, notices fusionnées et composition de chaque fusion. Les
   moissons sont espacées (``--pause``) ; une requête déjà collectée n'est pas
   refaite (reprise possible) sauf ``--force``.
2. ``build`` (hors réseau) : pour chaque instantané, applique les classeurs
   comparés (``native`` et variantes ``local`` d'OPT-13 si le module existe,
   voir ``rankers.py``), et écrit ``pools/<id>.json`` : ordre complet de
   chaque classeur et **ensemble commun de candidats** = union des
   ``--depth`` (30) premiers de chaque ordre. À relancer après OPT-13 : les
   identifiants de candidats étant des empreintes du contenu, les jugements
   déjà rendus restent valables.

Commandes annexes : ``resolve-dois`` (réseau, CrossRef, facultatif) résout
les DOI manquants des candidats et écrit ``doi/<id>.doi_report.json`` (rapports
réutilisés par les métriques de précision des DOI) ; ``status`` résume l'état
d'une campagne.

Exemples (depuis la racine du projet) ::

    venv/bin/python scripts/eval/pool.py collect --date 2026-09-29
    venv/bin/python scripts/eval/pool.py build --date 2026-09-29
    venv/bin/python scripts/eval/pool.py status --date 2026-09-29
"""

from __future__ import annotations

import argparse
import copy
import logging
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval import common as C  # noqa: E402
from eval.rankers import Ranker, resolve_rankers  # noqa: E402

logger = logging.getLogger("moisson.eval.pool")

SNAPSHOT_FORMAT = "moisson-shs/eval-snapshot"
POOL_FORMAT = "moisson-shs/eval-pool"
FORMAT_VERSION = 1

DEFAULT_MAX_PER_SOURCE = 100
DEFAULT_PAUSE_S = 8.0
DEFAULT_WORKERS = 4
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_DEPTH = 30

# Clés des sources gratuites : retirées sauf --keys. La clé SerpAPI (seule
# source facturée) est retirée dans tous les cas.
FREE_KEY_VARS = ("OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "CORE_API_KEY")
ALWAYS_REMOVED_VARS = ("SERPAPI_API_KEY",)


# === Garde d'environnement ===

def prepare_environment(keys: bool = False, environ=None) -> Dict[str, Any]:
    """
    Garde d'environnement de la collecte, à appeler AVANT d'importer le
    moteur : sans ``keys``, aucun ``.env`` n'est chargé
    (``MOISSON_NO_DOTENV=1``) et les clés des sources gratuites sont
    retirées ; la clé SerpAPI est toujours retirée.
    """
    env = os.environ if environ is None else environ
    removed = []
    if not keys:
        env["MOISSON_NO_DOTENV"] = "1"
        for var in FREE_KEY_VARS:
            if env.pop(var, None) is not None:
                removed.append(var)
    for var in ALWAYS_REMOVED_VARS:
        if env.pop(var, None) is not None:
            removed.append(var)
    return {"keys": bool(keys), "removed": removed}


def strip_metered_keys(environ=None) -> List[str]:
    """Retire la clé SerpAPI (à rappeler après l'import des connecteurs,
    qui peut charger un ``.env`` quand ``--keys`` est demandé)."""
    env = os.environ if environ is None else environ
    return [v for v in ALWAYS_REMOVED_VARS if env.pop(v, None) is not None]


def eval_sources(group: str) -> List[str]:
    """Sources d'un groupe admis, jamais Google Scholar (``EvalError`` sinon)."""
    if group not in C.ALLOWED_SOURCE_GROUPS:
        raise C.EvalError(f"groupe de sources non admis : {group!r}")
    from connectors.registry import is_metered_source, resolve_sources
    sources = resolve_sources([group])
    bad = [s for s in sources if s in C.FORBIDDEN_SOURCES or is_metered_source(s)]
    if bad:
        raise C.EvalError(f"source facturée dans le groupe {group!r} : {', '.join(bad)}")
    return sources


def code_version() -> Optional[str]:
    """Révision git courante (lecture seule), pour la traçabilité."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(_ROOT),
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# === Instantané d'une moisson ===

def build_request(q: Dict[str, Any], max_per_source: int, workers: int):
    from engine.models import MODE_BIBLIOGRAPHIE, SearchRequest
    return SearchRequest(
        query=q["query"], sources=eval_sources(q["sources"]),
        from_date=q.get("from_date"), to_date=q.get("to_date"),
        mode=MODE_BIBLIOGRAPHIE, max_per_source=max_per_source,
        enrich_oai=False, dedup=True, cache=False, workers=workers,
        allow_metered=False, query_variants=q.get("query_variants") or {},
        languages=q.get("languages") or [],
    )


def request_dict(request) -> Dict[str, Any]:
    return {
        "query": request.query, "sources": list(request.sources),
        "from_date": request.from_date, "to_date": request.to_date,
        "mode": request.mode, "max_per_source": request.max_per_source,
        "workers": request.workers, "cache": False, "allow_metered": False,
        "query_variants": dict(request.query_variants),
        "languages": list(request.languages),
    }


def _public_query(q: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(q)
    for ref in out.get("references_connues") or []:
        ref.pop("keys", None)
    return out


def build_snapshot(q: Dict[str, Any], request, result, *,
                   http_stats: Optional[Dict] = None,
                   collected_at: Optional[str] = None,
                   environment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Instantané sérialisable d'un ``HarvestResult`` : notices de chaque source
    dans l'ordre servi, rangs natifs alignés, statuts, notices fusionnées et
    composition de chaque fusion (``groups`` : pour chaque notice fusionnée,
    ``[indice de source, position dans la source]`` de ses membres). La
    composition est recalculée par la déduplication du moteur sur les mêmes
    notices ; si le résultat diffère de celui de la moisson (ce qui ne
    devrait pas arriver : fusion déterministe), les notices de la moisson
    sont gardées et ``groups`` vaut None (avertissement consigné).
    """
    from utils.deduplication import deduplicate_with_report

    sources = []
    flat: List[Tuple[int, int]] = []
    all_items: List[Dict] = []
    for s, res in enumerate(result.sources):
        sources.append({
            "source": res.source,
            "display_name": res.display_name,
            "status": res.as_dict(),
            "ranking": {"sort": res.native_sort,
                        "filters": dict(res.native_filters),
                        "ranks": res.aligned_ranks()},
            "items": list(res.items),
        })
        for p, it in enumerate(res.items):
            flat.append((s, p))
            all_items.append(it)
    warnings = []
    records, report = deduplicate_with_report(copy.deepcopy(all_items), with_groups=True)
    raw_groups = report.pop("groups", [])
    same = Counter(C.canonical_dump(r) for r in records) == Counter(
        C.canonical_dump(r) for r in result.items)
    if same:
        items = records
        groups = [[list(flat[g]) for g in members] for members in raw_groups]
    else:
        items = list(result.items)
        groups = None
        warnings.append("déduplication recalculée différente de celle de la moisson : "
                        "composition des fusions indisponible")
    dedup_report = dict(result.dedup_report or report)
    dedup_report.pop("groups", None)
    # Classement calculé par le moteur pendant la moisson (OPT-13, si
    # présent) : ordre seulement, pour contrôler qu'un rejeu hors réseau
    # donne le même ordre ; jamais utilisé par les métriques.
    engine_ranking = getattr(result, "ranking", None)
    if isinstance(engine_ranking, dict):
        engine_ranking = {"version": engine_ranking.get("version"),
                          "mode": engine_ranking.get("mode"),
                          "error": engine_ranking.get("error"),
                          "order": [e.get("index") for e in engine_ranking.get("entries") or []]}
    else:
        engine_ranking = None
    return {
        "format": SNAPSHOT_FORMAT,
        "version": FORMAT_VERSION,
        "query": _public_query(q),
        "request": request_dict(request),
        "collected_at": collected_at or datetime.now().isoformat(timespec="seconds"),
        "run_id": result.run_id,
        "duration_s": round(result.duration_s, 3),
        "overall_status": result.overall_status,
        "query_plan": result.query_plan,
        "sources": sources,
        "total_before_dedup": result.total_before_dedup,
        "dedup_report": dedup_report,
        "items": items,
        "groups": groups,
        "engine_ranking": engine_ranking,
        "http": http_stats or {},
        "environment": environment or {},
        "warnings": warnings,
    }


def reconstruct(snapshot: Dict[str, Any]):
    """``(SearchRequest, HarvestResult)`` reconstitués depuis un instantané ;
    ``harvest.eval_groups`` porte la composition des fusions."""
    from engine.models import HarvestResult, SearchRequest, SourceResult

    r = snapshot["request"]
    request = SearchRequest(
        query=r["query"], sources=list(r["sources"]), from_date=r.get("from_date"),
        to_date=r.get("to_date"), mode=r.get("mode", "bibliographie"),
        max_per_source=r.get("max_per_source", DEFAULT_MAX_PER_SOURCE),
        cache=False, workers=r.get("workers") or 1, allow_metered=False,
        query_variants=r.get("query_variants") or {}, languages=r.get("languages") or [],
    )
    sources = []
    for s in snapshot.get("sources") or []:
        st = s.get("status") or {}
        ranking = s.get("ranking") or {}
        res = SourceResult(
            source=s["source"], display_name=s.get("display_name") or s["source"],
            items=list(s.get("items") or []), status=st.get("status", "complete"),
            reason=st.get("reason"), detail=st.get("detail"),
            limit_reached=bool(st.get("limit_reached")), exhausted=bool(st.get("exhausted")),
            provider_window=bool(st.get("provider_window")), pages=int(st.get("pages") or 0),
            counters=dict(st.get("counters") or {}), warnings=list(st.get("warnings") or []),
            duration_s=float(st.get("duration_s") or 0.0),
            native_sort=ranking.get("sort"), native_ranks=list(ranking.get("ranks") or []),
            native_filters=dict(ranking.get("filters") or {}),
        )
        res.bind_ranks()
        sources.append(res)
    harvest = HarvestResult(
        run_id=snapshot.get("run_id") or "", request=request, sources=sources,
        items=list(snapshot.get("items") or []),
        total_before_dedup=int(snapshot.get("total_before_dedup") or 0),
        dedup_report=snapshot.get("dedup_report"),
        duration_s=float(snapshot.get("duration_s") or 0.0),
        query_plan=snapshot.get("query_plan"),
    )
    harvest.eval_groups = snapshot.get("groups")
    # Année de référence (récence, départage d'OPT-13) : celle de la
    # collecte, pour qu'un classement rejoué plus tard soit identique.
    collected = str(snapshot.get("collected_at") or "")
    harvest.eval_reference_year = int(collected[:4]) if collected[:4].isdigit() else None
    return request, harvest


# === Collecte (réseau) ===

def _source_summary(snapshot: Dict[str, Any]) -> str:
    bits = []
    for s in snapshot.get("sources") or []:
        st = s.get("status") or {}
        bits.append(f"{s.get('display_name')}={st.get('items', 0)}"
                    + ("" if st.get("status") in ("complete", "empty")
                       else f"[{st.get('status')}:{st.get('reason')}]"))
    return ", ".join(bits)


def collect(queries: Sequence[Dict[str, Any]], out_dir: Path, *,
            max_per_source: int = DEFAULT_MAX_PER_SOURCE,
            workers: int = DEFAULT_WORKERS, pause: float = DEFAULT_PAUSE_S,
            keys: bool = False, force: bool = False,
            timeout: float = DEFAULT_TIMEOUT_S) -> List[Dict[str, Any]]:
    """Moissons live des requêtes (voir le docstring du module)."""
    env_info = prepare_environment(keys=keys)
    from engine.models import RunContext, new_run_id
    from engine.orchestrator import harvest
    from utils.http import HTTPClient, OperationCancelled, get_http_stats, reset_http_stats
    env_info["removed"] += strip_metered_keys()
    environment = {"keys": env_info, "python": sys.version.split()[0],
                   "code": code_version(), "cache": False}

    journal_path = out_dir / "journal_collecte.json"
    journal = C.read_json(journal_path) if journal_path.exists() else []
    network_used = False
    out = []
    for q in queries:
        path = out_dir / C.SNAPSHOTS / f"{q['id']}.json"
        if path.exists() and not force:
            print(f"[{q['id']}] déjà collectée : {path.name} (--force pour refaire)")
            continue
        if network_used and pause > 0:
            time.sleep(pause)
        request = build_request(q, max_per_source, workers)
        client = HTTPClient(use_cache=False)
        ctx = RunContext.with_timeout(timeout, run_id=new_run_id(), output_dir=None,
                                      http_client=client,
                                      budgets={"max_per_source": max_per_source})
        reset_http_stats()
        started = datetime.now().isoformat(timespec="seconds")
        t0 = time.monotonic()
        entry: Dict[str, Any] = {"id": q["id"], "started": started}
        try:
            network_used = True
            result = harvest(request, ctx)
        except OperationCancelled as exc:
            entry.update(status="annulee", reason=getattr(exc, "reason", str(exc)),
                         duration_s=round(time.monotonic() - t0, 1))
            journal.append(entry)
            C.write_json(journal_path, journal)
            print(f"[{q['id']}] ANNULÉE ({entry['reason']}) : rien d'enregistré")
            continue
        finally:
            client.close()
        snapshot = build_snapshot(q, request, result, http_stats=get_http_stats(),
                                  collected_at=started, environment=environment)
        C.write_json(path, snapshot)
        n_requests = sum(int(v.get("network") or 0) for v in snapshot["http"].values())
        entry.update(status=snapshot["overall_status"], items=len(snapshot["items"]),
                     before_dedup=snapshot["total_before_dedup"],
                     duration_s=round(time.monotonic() - t0, 1),
                     network_requests=n_requests,
                     hosts={h: int(v.get("network") or 0) for h, v in snapshot["http"].items()},
                     sources=_source_summary(snapshot))
        journal.append(entry)
        C.write_json(journal_path, journal)
        out.append(entry)
        print(f"[{q['id']}] {entry['status']} : {entry['items']} notices uniques "
              f"({entry['before_dedup']} avant fusion) en {entry['duration_s']} s, "
              f"{n_requests} requêtes réseau — {entry['sources']}")
    return out


# === Construction de l'ensemble de candidats (hors réseau) ===

def build_pool(snapshot: Dict[str, Any], rankers: Sequence[Ranker],
               depth: int = DEFAULT_DEPTH, abstract_chars: int = 700) -> Dict[str, Any]:
    """
    Ensemble commun de candidats d'une requête : union des ``depth``
    premiers de chaque classeur ; ordre complet de chaque classeur conservé
    (métriques) ; notices, champs d'affichage et catégories d'équilibre des
    candidats ; clés fortes de TOUTES les notices (rappel d'acquisition sur
    références connues).
    """
    from utils.identity import strong_keys

    request, harvest = reconstruct(snapshot)
    items = harvest.items
    cids = C.candidate_ids(items)
    rankings: Dict[str, Dict[str, Any]] = {}
    for ranker in rankers:
        entry = {"kind": ranker.kind, "version": ranker.version,
                 "fingerprint": getattr(ranker, "fingerprint", None),
                 "description": ranker.description, "ranking": [], "error": None}
        try:
            entry["ranking"] = ranker.rank(items, request, harvest)
        except Exception as exc:  # noqa: BLE001 - un classeur en échec est consigné
            entry["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[{snapshot['query']['id']}] classeur {ranker.name} en échec : {exc}")
        rankings[ranker.name] = entry
    pool = sorted({cid for entry in rankings.values() for cid in entry["ranking"][:depth]})
    index = {cid: i for i, cid in enumerate(cids)}
    candidates = {}
    for cid in pool:
        item = items[index[cid]]
        candidates[cid] = {
            "item": item,
            "display": C.display_fields(item, abstract_chars),
            "balance": C.balance_fields(item),
            "keys": strong_keys(item),
        }
    q = snapshot["query"]
    return {
        "format": POOL_FORMAT,
        "version": FORMAT_VERSION,
        "qid": q["id"],
        "set": q.get("set"),
        "query": q,
        "depth": depth,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_collected_at": snapshot.get("collected_at"),
        "n_items": len(items),
        "sources_status": {s["display_name"]: {k: (s.get("status") or {}).get(k)
                                               for k in ("status", "reason", "items")}
                           for s in snapshot.get("sources") or []},
        "rankers": rankings,
        "pool": pool,
        "candidates": candidates,
        "all_keys": {cid: strong_keys(items[i]) for i, cid in enumerate(cids)},
    }


def build(out_dir: Path, rankers: Sequence[Ranker], depth: int = DEFAULT_DEPTH,
          ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    snap_dir = out_dir / C.SNAPSHOTS
    paths = sorted(snap_dir.glob("*.json"))
    if ids:
        wanted = {i.upper() for i in ids}
        paths = [p for p in paths if p.stem.upper() in wanted]
    if not paths:
        raise C.EvalError(f"aucun instantané dans {snap_dir} (lancer d'abord « collect »)")
    summary = []
    for path in paths:
        snapshot = C.read_json(path)
        pool = build_pool(snapshot, rankers, depth)
        C.write_json(out_dir / C.POOLS / f"{pool['qid']}.json", pool)
        line = {"id": pool["qid"], "items": pool["n_items"], "pool": len(pool["pool"]),
                "rankers": {k: (len(v["ranking"]) if not v["error"] else v["error"])
                            for k, v in pool["rankers"].items()}}
        summary.append(line)
        print(f"[{pool['qid']}] {pool['n_items']} notices, {len(pool['pool'])} candidats "
              f"(union des {depth} premiers) — "
              + ", ".join(f"{k}: {v}" for k, v in line["rankers"].items()))
    return summary


# === DOI des candidats (réseau, facultatif) ===

def resolve_pool_dois(out_dir: Path, limit: int = 200, workers: int = 3,
                      ids: Optional[Sequence[str]] = None,
                      per_query: Optional[int] = 8) -> Dict[str, Any]:
    """
    Résout par CrossRef les DOI manquants des candidats (règles du projet,
    seuils inchangés) et écrit un rapport par requête
    (``doi/<id>.doi_report.json``, format ``build_report_document`` plus un
    bloc ``eval`` qui rattache chaque entrée à son candidat). Au plus
    ``per_query`` candidats par requête (pris dans l'ordre des identifiants,
    sans rapport avec un classement) et ``limit`` pour toute la campagne :
    un échantillon, pas une résolution exhaustive.
    """
    prepare_environment(keys=False)
    from utils.doi_resolver import build_report_document, resolve_dois
    from utils.http import HTTPClient
    from utils.identity import extract_doi

    remaining = int(limit)
    totals = Counter()
    for path in sorted((out_dir / C.POOLS).glob("*.json")):
        if ids and path.stem.upper() not in {i.upper() for i in ids}:
            continue
        if remaining <= 0:
            break
        pool = C.read_json(path)
        cids = [cid for cid in pool["pool"] if not extract_doi(pool["candidates"][cid]["item"])]
        cids = cids[:min(remaining, per_query or remaining)]
        if not cids:
            continue
        items = [copy.deepcopy(pool["candidates"][cid]["item"]) for cid in cids]
        client = HTTPClient(use_cache=False)
        try:
            _out, report = resolve_dois(items, workers=workers, http_client=client)
        finally:
            client.close()
        doc = build_report_document(report, source_corpus=f"eval-{pool['qid']}")
        doc["eval"] = {"qid": pool["qid"], "cids": cids}
        C.write_json(out_dir / C.DOI_DIR / f"{pool['qid']}.doi_report.json", doc)
        remaining -= len(cids)
        counts = doc["counts"]
        for key in ("accepte", "rejete", "ambigu", "sans_candidat", "erreur", "interroges"):
            totals[key] += int(counts.get(key) or 0)
        print(f"[{pool['qid']}] {len(cids)} candidats sans DOI : "
              + ", ".join(f"{k}={counts.get(k, 0)}" for k in
                          ("accepte", "rejete", "ambigu", "sans_candidat", "erreur")))
    return dict(totals)


# === État ===

def status(out_dir: Path, doc: Dict[str, Any]) -> None:
    print(f"Campagne : {out_dir}")
    for q in doc["queries"]:
        snap = out_dir / C.SNAPSHOTS / f"{q['id']}.json"
        pool = out_dir / C.POOLS / f"{q['id']}.json"
        line = f"  {q['id']} [{q['set']}] {q['query'][:48]:<48} "
        line += "instantané " + ("oui" if snap.exists() else "non")
        if pool.exists():
            p = C.read_json(pool)
            line += f", {len(p['pool'])} candidats (" + ", ".join(
                f"{k}{'!' if v['error'] else ''}" for k, v in p["rankers"].items()) + ")"
        print(line)


# === Ligne de commande ===

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collecte des candidats du protocole d'évaluation SHS (OPT-15).")
    parser.add_argument("--date", default=None, help="campagne AAAA-MM-JJ (défaut : aujourd'hui)")
    parser.add_argument("--root", default=None,
                        help="répertoire racine des campagnes (défaut : output/eval)")
    parser.add_argument("--queries", default=None, help="fichier de requêtes")
    parser.add_argument("--ids", default=None, help="identifiants (R01,D02…)")
    parser.add_argument("--set", dest="subset", choices=C.SETS, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="moissons live (réseau)")
    p_collect.add_argument("--max-per-source", type=int, default=DEFAULT_MAX_PER_SOURCE)
    p_collect.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p_collect.add_argument("--pause", type=float, default=DEFAULT_PAUSE_S,
                           help="secondes entre deux moissons")
    p_collect.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                           help="échéance d'une moisson (secondes)")
    p_collect.add_argument("--keys", action="store_true",
                           help="utiliser les clés des sources gratuites (.env) ; "
                                "la clé SerpAPI reste retirée")
    p_collect.add_argument("--force", action="store_true", help="refaire les requêtes déjà collectées")

    p_build = sub.add_parser("build", help="classements et ensemble de candidats (hors réseau)")
    p_build.add_argument("--rankers", default="native,local",
                         help="classeurs (native, local, local:<variante>)")
    p_build.add_argument("--ranker", action="append", default=[],
                         help="classeur externe nom=paquet.module:fonction (répétable)")
    p_build.add_argument("--depth", type=int, default=DEFAULT_DEPTH)

    p_doi = sub.add_parser("resolve-dois", help="DOI manquants des candidats (réseau, CrossRef)")
    p_doi.add_argument("--limit", type=int, default=200, help="plafond pour la campagne")
    p_doi.add_argument("--per-query", type=int, default=8, help="plafond par requête")
    p_doi.add_argument("--workers", type=int, default=3)

    sub.add_parser("status", help="état de la campagne")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        # Statuts des sources résumés par la ligne de chaque requête : les
        # avertissements du moteur (fenêtre Isidore…) restent dans le
        # journal et l'instantané, pas dans la console.
        for name in ("moisson", "connectors", "utils", "mappers"):
            logging.getLogger(name).setLevel(logging.ERROR)
    ids = C.parse_id_list(args.ids)
    try:
        out_dir = C.ensure_writable(C.run_dir(args.date, Path(args.root) if args.root else None))
        doc = C.load_queries(Path(args.queries) if args.queries else None)
        if args.command == "collect":
            queries = C.select_queries(doc, ids, args.subset)
            collect(queries, out_dir, max_per_source=args.max_per_source,
                    workers=args.workers, pause=args.pause, keys=args.keys,
                    force=args.force, timeout=args.timeout)
        elif args.command == "build":
            names = C.parse_id_list(args.rankers) or ["native"]
            rankers, notes = resolve_rankers(names, args.ranker)
            for note in notes:
                print(f"note : {note}")
            print("classeurs : " + ", ".join(r.name for r in rankers))
            selected = [q["id"] for q in C.select_queries(doc, ids, args.subset)]
            build(out_dir, rankers, args.depth, selected)
        elif args.command == "resolve-dois":
            selected = [q["id"] for q in C.select_queries(doc, ids, args.subset)]
            totals = resolve_pool_dois(out_dir, args.limit, args.workers, selected,
                                       args.per_query)
            print("total : " + ", ".join(f"{k}={v}" for k, v in totals.items()))
        elif args.command == "status":
            status(out_dir, doc)
    except C.EvalError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
