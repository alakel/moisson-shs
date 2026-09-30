"""
Exécution d'une mesure : environnement, import de la copie mesurée, points
d'entrée CLI et MCP, collecte des métriques.

Une mesure (« run ») s'exécute dans un processus neuf lancé par
``scripts/benchmark_moisson.py`` : l'état des modules (cache mémoire du schéma
Zotero, sessions, constantes lues à l'import comme ``REQUEST_DELAY``) repart
de zéro à chaque répétition, et le pic mémoire (``ru_maxrss``) est celui de la
seule mesure. ``execute_run`` peut aussi être appelé dans le processus courant
(tests unitaires) ; il ne restaure alors pas les modules importés.

Points d'entrée mesurés :

- ``cli`` : ``main.main()`` avec ``sys.argv`` construit depuis les paramètres
  du scénario, sortie dans un répertoire temporaire (jamais ``output/`` du
  projet) ;
- ``mcp`` : ``mcp_server._run_moisson_sync`` appelé par noms de paramètres
  après lecture de sa signature (``inspect.signature``). Un paramètre
  obligatoire inconnu ou un paramètre attendu disparu produit une erreur
  explicite qui cite la signature courante ;
- ``tool`` : ``mcp_server.call_tool("moisson_search", arguments)`` (contrat
  public de l'outil, conservé par le plan) : mesure les VALEURS PAR DÉFAUT de
  l'outil (workers, cache, max_per_source) quand le scénario ne les fixe pas.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import json
import os
import platform
import resource
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any, Dict, List, Optional

from bench import analysis
from bench.instrument import (
    BenchError, CacheCounter, NetworkRecorder, SleepRecorder, SocketGuard,
    StageRecorder, install_network_layer,
)

# Variables des clés API des sources internationales.
KEY_VARS = (
    "OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "CORE_API_KEY",
    "SERPAPI_API_KEY",
)

# Clés factices posées en mode simulé (le simulateur les exige comme l'API
# réelle : OpenAlex refuse une requête sans api_key, CORE sans Bearer).
FAKE_KEYS = {
    "OPENALEX_API_KEY": "bench-fake-openalex",
    "SEMANTIC_SCHOLAR_API_KEY": "bench-fake-s2",
    "CORE_API_KEY": "bench-fake-core",
}

# Hôtes autorisés en mode live (API gratuites). Tout autre hôte est refusé,
# SerpAPI en premier lieu (source facturée : jamais appelée par le banc).
LIVE_ALLOWED_HOSTS = {
    "api.archives-ouvertes.fr", "api.isidore.science", "api.crossref.org",
    "api.openalex.org", "api.semanticscholar.org", "api.core.ac.uk",
    "api.zotero.org", "metadata.openedition.org", "oai.cairn.info",
    "oai.persee.fr", "oai.erudit.org",
}
FORBIDDEN_HOSTS = {"serpapi.com"}

# Modules dont on vérifie qu'ils proviennent de la copie mesurée.
_CHECKED_MODULES = ("config", "main", "connectors", "utils", "mappers")

# Fonctions chronométrées (module, attribut, étape, nature). Une fonction
# absente est signalée dans « missing » sans faire échouer la mesure.
STAGE_TARGETS = (
    ("main", "run_source", "sources", "run_source"),
    # Moteur (OPT-07) : deduplicate_with_report (notices et rapport) ; copie
    # plus ancienne : deduplicate.
    ("utils.deduplication", ("deduplicate_with_report", "deduplicate"),
     "deduplication", "dedup"),
    ("mappers.csl_exporter", "export_corpus_to_csl", "export_csl", "plain"),
    ("main", "compute_stats", "statistiques", "plain"),
    ("main", "write_stats_file", "fichier_stats", "plain"),
    ("mcp_server", "_build_corpus_tool_result", "retour_mcp", "plain"),
    ("mappers.zotero_schema", "validate_and_clean", "validation_zotero", "plain"),
    ("mappers.zotero_schema_loader", "load_zotero_schema", "chargement_schema", "plain"),
)

# Paramètres de _run_moisson_sync attendus (nom canonique -> alias acceptés).
_MCP_PARAM_ALIASES = {
    "query": ("query",),
    "max_per_source": ("max_per_source", "max_results"),
    "sources": ("sources",),
    "enrich_oai": ("enrich_oai",),
    "do_dedup": ("do_dedup", "dedup"),
    "from_date": ("from_date",),
    "to_date": ("to_date",),
    "cache": ("cache", "use_cache"),
    "workers": ("workers",),
    "progress": ("progress",),
    "cancel_event": ("cancel_event",),
    # OPT-12 : mode de moisson (transmis s'il figure dans le scénario et dans
    # la signature ; sinon défaut du point d'entrée).
    "mode": ("mode",),
}
_MCP_REQUIRED = ("query", "max_per_source", "sources")


# === Environnement ===

def build_env(spec: Dict[str, Any], run_dir: Path,
              base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Environnement du processus de mesure.

    - ``MOISSON_*`` hérités retirés (délais, timeouts, cache hérités du shell
      fausseraient la comparaison), puis posés explicitement ;
    - ``MOISSON_NO_DOTENV=1`` et clés API vidées : une variable définie, même
      vide, n'est jamais écrasée par ``python-dotenv`` (``override=False``),
      ce qui neutralise aussi le ``.env`` d'une copie qui ignorerait
      ``MOISSON_NO_DOTENV`` (copie de référence) ;
    - mode simulé : clés factices ; mode live : aucune clé, sauf
      ``live_keys`` (clés réelles de l'environnement, jamais SerpAPI) ;
    - cache HTTP, cache du schéma Zotero et sortie dans ``run_dir`` : le cache
      personnel ``~/.cache/moisson_shs`` n'est ni lu ni pollué ;
    - ``PYTHONDONTWRITEBYTECODE=1`` : aucun ``__pycache__`` écrit dans la
      copie mesurée (la copie de référence reste intacte).
    """
    base = dict(os.environ if base_env is None else base_env)
    env = {k: v for k, v in base.items() if not k.startswith("MOISSON_")}
    env.update({
        "MOISSON_NO_DOTENV": "1",
        "MOISSON_OUTPUT_DIR": str(run_dir / "output"),
        "MOISSON_CACHE": "0",
        "MOISSON_CACHE_DIR": str(run_dir / "cache" / "http"),
        "MOISSON_SCHEMA_CACHE": str(run_dir / "cache" / "zotero_schema.json"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    })
    for k in KEY_VARS:
        env[k] = ""
    if spec.get("mode") == "sim":
        env.update(FAKE_KEYS)
    elif spec.get("live_keys"):
        for k in KEY_VARS:
            if base.get(k):
                env[k] = base[k]
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    env["SERPAPI_API_KEY"] = ""  # jamais de requête facturée
    return env


def apply_env(env: Dict[str, str]) -> None:
    """Applique ``env`` au processus courant (avant tout import du code mesuré)."""
    for k in list(os.environ):
        if k.startswith("MOISSON_") and k not in env:
            del os.environ[k]
    for k, v in env.items():
        os.environ[k] = v


# === Import de la copie mesurée ===

def import_tree(tree: Path, entry: str) -> Dict[str, Any]:
    """
    Importe la copie ``tree`` du moissonneur et vérifie sa provenance.

    La copie est placée en tête de ``sys.path`` ; l'installation « editable »
    du venv (chercheur de ``sys.meta_path`` placé après ``PathFinder``) ne peut
    donc pas la masquer, mais on le VÉRIFIE par ``module.__file__`` plutôt que
    de le supposer.

    Raises:
        BenchError: module importé depuis une autre copie, ou copie invalide.
    """
    tree = Path(tree).resolve()
    if not (tree / "main.py").is_file() or not (tree / "connectors").is_dir():
        raise BenchError(f"--tree {tree} : ce n'est pas une copie du moissonneur "
                         f"(main.py ou connectors/ absent)")
    already = [m for m in ("main", "config", "mcp_server") if m in sys.modules]
    if already:
        origin = Path(getattr(sys.modules[already[0]], "__file__", "") or "").resolve()
        if tree not in origin.parents:
            raise BenchError(
                f"le module {already[0]} est déjà importé depuis {origin} : "
                f"impossible de mesurer {tree} dans ce processus"
            )
    if str(tree) not in sys.path:
        sys.path.insert(0, str(tree))
    import importlib
    mods = {}
    names = ["config", "main", "connectors.registry"]
    if entry in ("mcp", "tool"):
        names.append("mcp_server")
    for name in names:
        try:
            mods[name] = importlib.import_module(name)
        except Exception as e:
            raise BenchError(f"import de {name} depuis {tree} impossible : "
                             f"{type(e).__name__}: {e}") from e
    check = list(_CHECKED_MODULES) + (["mcp_server"] if "mcp_server" in mods else [])
    files = {}
    for name in check:
        mod = sys.modules.get(name)
        f = getattr(mod, "__file__", None)
        if not f:
            raise BenchError(f"module {name} sans __file__ : provenance invérifiable")
        path = Path(f).resolve()
        if tree not in path.parents:
            raise BenchError(
                f"le module {name} provient de {path}, pas de la copie mesurée "
                f"{tree} (installation editable du venv ?)"
            )
        files[name] = str(path)
    return {"tree": str(tree), "modules": mods, "files": files}


# Dossier du banc lui-même (scripts/bench) : jamais instrumenté, même quand la
# copie mesurée est la racine de ce dépôt (qui le contient).
_BENCH_DIR = Path(__file__).resolve().parent


def _is_environment_file(path: Path) -> bool:
    """Vrai pour un module de l'interpréteur ou d'un environnement virtuel."""
    if "site-packages" in path.parts or "dist-packages" in path.parts:
        return True
    for prefix in {sys.prefix, sys.base_prefix, sys.exec_prefix}:
        try:
            if Path(prefix).resolve() in path.parents:
                return True
        except (OSError, ValueError):
            continue
    return False


def tree_modules(tree: Path) -> List[Any]:
    """
    Modules chargés depuis la copie mesurée, hors banc et hors environnement.

    Mesurer l'arbre de travail (défaut) donne une racine qui contient aussi
    ``scripts/bench`` et, souvent, le ``venv`` : les instrumenter faussait la
    mesure (bibliothèques tierces comptées comme code mesuré) et, pire, le
    remplacement de ``ORIGINAL_SLEEP`` dans ``bench.instrument`` rendait
    ``SleepRecorder.sleep`` récursif dès que ``--sleep-scale`` > 0 (sources
    arrêtées en silence sur RecursionError).
    """
    tree = Path(tree).resolve()
    out = []
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            path = Path(f).resolve()
            if tree not in path.parents:
                continue
            if _BENCH_DIR == path.parent or _BENCH_DIR in path.parents:
                continue
            if _is_environment_file(path):
                continue
            out.append(mod)
        except (OSError, ValueError):
            continue
    return out


# === Points d'entrée ===

def build_cli_argv(params: Dict[str, Any], out_dir: Path) -> List[str]:
    argv = ["main.py", params["query"], "--max", str(params["max_per_source"]),
            "--output", str(out_dir), "--sources", *params["sources"]]
    if params.get("from_date"):
        argv += ["--from-date", str(params["from_date"])]
    if params.get("to_date"):
        argv += ["--to-date", str(params["to_date"])]
    if params.get("workers") is not None:
        argv += ["--workers", str(params["workers"])]
    if params.get("cache"):
        argv += ["--cache"]
    if params.get("enrich_oai"):
        argv += ["--enrich-oai"]
    if params.get("mode"):
        argv += ["--mode", str(params["mode"])]
    return argv


# Codes de sortie de la CLI qui sont des résultats mesurables, pas des erreurs
# du banc : 0 (succès), None, et 3 (moteur OPT-07 : aucune source exécutée
# jusqu'au bout, aucun corpus écrit — scénarios de panne totale).
_CLI_MEASURED_CODES = (0, None, 3)


def run_cli(main_mod, params: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    """Moisson CLI via ``main.main()`` (sys.argv patché, stdout capturée)."""
    if not callable(getattr(main_mod, "main", None)):
        raise BenchError("main.main introuvable : point d'entrée CLI modifié ?")
    argv = build_cli_argv(params, out_dir)
    saved = sys.argv
    buf = io.StringIO()
    rc = None
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(buf):
            rc = main_mod.main()
    except SystemExit as e:
        rc = e.code
        if rc not in _CLI_MEASURED_CODES:
            raise BenchError(
                f"main.main() a quitté avec le code {rc} pour les arguments "
                f"{argv[1:]} : {buf.getvalue()[-800:]}"
            ) from e
    finally:
        sys.argv = saved
    return {"return_code": rc, "argv": argv[1:], "stdout_tail": buf.getvalue()[-1500:]}


def bind_mcp_kwargs(fn, values: Dict[str, Any]) -> Dict[str, Any]:
    """
    Construit les arguments nommés de ``_run_moisson_sync`` d'après sa signature.

    Les valeurs None ne sont pas transmises (valeur par défaut de la fonction).

    Raises:
        BenchError: paramètre obligatoire inconnu du banc, ou paramètre
            essentiel (requête, plafond, sources) absent de la signature.
    """
    sig = inspect.signature(fn)
    params = sig.parameters
    kwargs = {}
    consumed = set()
    for pname, p in params.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        canon = next((c for c, al in _MCP_PARAM_ALIASES.items() if pname in al), None)
        if canon is not None and values.get(canon) is not None:
            kwargs[pname] = values[canon]
            consumed.add(canon)
        elif p.default is inspect.Parameter.empty:
            raise BenchError(
                f"mcp_server._run_moisson_sync exige le paramètre « {pname} » "
                f"inconnu du banc. Signature actuelle : {sig}. Adapter "
                f"bench.runner._MCP_PARAM_ALIASES ou utiliser --entry tool."
            )
    missing = [c for c in _MCP_REQUIRED if c not in consumed]
    if missing:
        raise BenchError(
            f"mcp_server._run_moisson_sync n'accepte plus {missing}. "
            f"Signature actuelle : {sig}. Utiliser --entry tool (contrat public "
            f"de l'outil moisson_search)."
        )
    return kwargs


def run_mcp_sync(mcp_mod, params: Dict[str, Any]) -> Any:
    fn = getattr(mcp_mod, "_run_moisson_sync", None)
    if fn is None:
        raise BenchError("mcp_server._run_moisson_sync introuvable (refonte du "
                         "moteur ?) : utiliser --entry tool")
    values = {
        "query": params["query"],
        "max_per_source": params["max_per_source"],
        "sources": list(params["sources"]),
        "enrich_oai": bool(params.get("enrich_oai", False)),
        "do_dedup": True,
        "from_date": params.get("from_date"),
        "to_date": params.get("to_date"),
        "cache": params.get("cache"),
        "workers": params.get("workers"),
        "progress": None,
        "cancel_event": None,
        "mode": params.get("mode"),
    }
    kwargs = bind_mcp_kwargs(fn, values)
    return fn(**kwargs)


def run_mcp_tool(mcp_mod, params: Dict[str, Any]) -> Any:
    fn = getattr(mcp_mod, "call_tool", None)
    if fn is None or not inspect.iscoroutinefunction(fn):
        raise BenchError("mcp_server.call_tool introuvable ou non asynchrone")
    args = {"query": params["query"], "max_per_source": params["max_per_source"],
            "sources": list(params["sources"])}
    for k in ("from_date", "to_date", "workers", "cache", "enrich_oai", "mode"):
        if params.get(k) is not None:
            args[k] = params[k]
    for k, v in (params.get("tool_args") or {}).items():
        args[k] = v
    return asyncio.run(fn("moisson_search", args))


# === Mémoire ===

def _maxrss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS : octets ; Linux : kilo-octets.
    return int(rss if platform.system() == "Darwin" else rss * 1024)


# === Mesure ===

def execute_run(spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Exécute une mesure et renvoie ses métriques (dict sérialisable).

    ``spec`` : dict produit par ``benchmark_moisson.make_spec`` (scénario,
    entrée, mode, copie, paramètres, configuration du simulateur).
    """
    run_dir = Path(spec["run_dir"])
    out_dir = run_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "cache").mkdir(parents=True, exist_ok=True)
    mode = spec["mode"]
    entry = spec["entry"]
    tree = Path(spec["tree"]).resolve()
    params = spec["params"]

    if spec.get("apply_env", True):
        apply_env(build_env(spec, run_dir))

    guard_sock = None
    if spec.get("forbid_sockets"):
        guard_sock = SocketGuard()
        guard_sock.install()

    t_import = time.perf_counter()
    info = import_tree(tree, entry)
    import_s = time.perf_counter() - t_import
    mods = info["modules"]
    modules = tree_modules(tree)

    # --- réseau : compteurs + simulateur (ou envoi réel filtré) ---
    # Décompte des notices reçues : fourni par le simulateur en mode simulé ;
    # en live, il exige de décoder chaque réponse une seconde fois (~0,03 s
    # par page Isidore réelle de 4,6 Mo), temps compté comme surcoût du banc
    # et retranché de la durée corrigée. Désactivable (--no-parse-records).
    net = NetworkRecorder(parse_records=(mode == "live" and spec.get("parse_records", True)),
                          keep_trace=bool(spec.get("keep_trace")))
    sim = None
    if mode == "sim":
        from bench.simulator import ApiSimulator
        sim = ApiSimulator(spec["sim"])
        restore_net = install_network_layer(net, inner_send=sim.send)
    else:
        def live_guard(host, request):
            import requests
            if host in FORBIDDEN_HOSTS or host.endswith(".serpapi.com"):
                raise requests.ConnectionError(
                    f"hôte interdit par le banc (source facturée) : {host}")
            if host not in LIVE_ALLOWED_HOSTS:
                raise requests.ConnectionError(
                    f"hôte non autorisé en mode live : {host}")
        restore_net = install_network_layer(net, guard=live_guard)

    # --- cache HTTP (succès / échecs), au-dessus des compteurs réseau ---
    cache_counter = CacheCounter()
    cache_counter.install()

    # --- sommeils et étapes ---
    sleeps = SleepRecorder(scale=float(spec.get("sleep_scale", 1.0)))
    sleeps.install(modules)
    stages = StageRecorder()
    net.current_source = stages.current_source
    # Appel logique courant : distingue une reprise (même appel) d'un nouvel
    # appel indépendant sur la même URL (cf. bench.instrument).
    net.current_call = stages.current_call
    import importlib
    for module_name, _attr, _stage, _kind in STAGE_TARGETS:
        # Modules importés paresseusement par le code (chargeur de schéma) :
        # importés ici pour pouvoir les instrumenter. mcp_server n'est pas
        # importé pour une mesure CLI (le retour MCP n'y existe pas).
        if module_name in sys.modules or (module_name == "mcp_server" and entry == "cli"):
            continue
        try:
            importlib.import_module(module_name)
        except Exception:
            pass
    modules = tree_modules(tree)
    sleeps.install(modules)  # références « from time import sleep » des modules tardifs
    for module_name, attr, stage, kind in STAGE_TARGETS:
        if module_name == "mcp_server" and entry == "cli":
            continue
        stages.wrap_everywhere(modules, module_name, attr, stage, kind)
    registry = mods["connectors.registry"]
    classes = list(dict.fromkeys(getattr(registry, "CONNECTORS", {}).values()))
    iso_mod = sys.modules.get("connectors.isidore")
    if iso_mod is not None and hasattr(iso_mod, "IsidoreConnector"):
        classes.append(iso_mod.IsidoreConnector)
    stages.wrap_connector_classes(list(dict.fromkeys(classes)))

    analysis_overhead = {"s": 0.0}
    dedup_input = {}
    # Couverture par source (vérité terrain) : travaux renvoyés par chaque
    # source, pour comparer des IDENTIFIANTS entre deux mesures (§7.1 :
    # « couverture identique », jamais un arrêt anticipé caché).
    source_works: Dict[str, set] = {}
    if mode == "sim":
        def on_dedup_input(items):
            t = time.perf_counter()
            try:
                dedup_input.update(analysis.gt_summary(items or []))
            finally:
                analysis_overhead["s"] += time.perf_counter() - t
        stages.hooks["dedup_input"] = on_dedup_input

        def on_source_items(name, items):
            t = time.perf_counter()
            try:
                ids = analysis.work_ids(items)
                source_works.setdefault(str(name), set()).update(ids)
            except Exception:  # forme de retour inattendue : pas de couverture
                pass
            finally:
                analysis_overhead["s"] += time.perf_counter() - t
        stages.hooks["source_items"] = on_source_items

    def run_entry():
        if entry == "cli":
            return None, run_cli(mods["main"], params, out_dir)
        if entry == "mcp":
            return run_mcp_sync(mods["mcp_server"], params), {}
        if entry == "tool":
            return run_mcp_tool(mods["mcp_server"], params), {}
        raise BenchError(f"point d'entrée inconnu : {entry}")

    warmup = None
    if spec.get("warmup"):
        # Moisson d'amorçage, non mesurée : remplit le cache HTTP (même
        # répertoire MOISSON_CACHE_DIR) avant la moisson mesurée « à chaud ».
        t_w = time.perf_counter()
        warmup = {}
        try:
            run_entry()
        except BenchError:
            raise
        except Exception as e:
            warmup["error"] = f"{type(e).__name__}: {e}"
        warmup["duration_s"] = round(time.perf_counter() - t_w, 4)
        warmup["network_requests"] = net.snapshot()["total"]["requests"]
        warmup["sleep_s"] = sleeps.snapshot()["total_s"]
        warmup["cache"] = cache_counter.snapshot()
        # Créneaux de politesse de l'amorçage oubliés (OPT-09) : sans cela, la
        # première requête mesurée vers un hôte lent (CORE, 6 s) attendrait
        # l'intervalle ouvert par la dernière requête de l'amorçage, et
        # l'attente dépendrait de l'ordre des sources, pas du code mesuré.
        http_mod = sys.modules.get("utils.http")
        reset_policy = getattr(http_mod, "reset_http_policy", None)
        if callable(reset_policy):
            reset_policy()
        net.reset()
        sleeps.reset()
        stages.reset()
        cache_counter.reset()
        if sim is not None:
            sim.reset_counters()
        dedup_input.clear()
        source_works.clear()
        analysis_overhead["s"] = 0.0

    if spec.get("tracemalloc"):
        tracemalloc.start()
    rss_before = _maxrss_bytes()

    error = None
    result = None
    entry_info: Dict[str, Any] = {}
    t0 = time.perf_counter()
    try:
        result, entry_info = run_entry()
    except BenchError:
        raise
    except Exception as e:  # la moisson a échoué : on mesure quand même
        import traceback
        error = f"{type(e).__name__}: {e}"
        entry_info = {"traceback": traceback.format_exc()[-3000:]}
    duration = time.perf_counter() - t0

    tm_peak = None
    if spec.get("tracemalloc"):
        tm_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    rss_after = _maxrss_bytes()
    restore_net()

    # --- analyse du corpus produit ---
    structured = analysis.structured_of(result)
    corpus_path = analysis.find_corpus_file(out_dir, structured)
    corpus = {}
    gt_out = {}
    coverage = None
    if mode == "sim":
        coverage = {
            "sources": {name: analysis.ids_record(ids)
                        for name, ids in sorted(source_works.items())},
            "final": None,
        }
    if corpus_path is not None:
        try:
            items = json.loads(corpus_path.read_text(encoding="utf-8"))
            corpus = analysis.corpus_summary(items)
            corpus["file"] = corpus_path.name
            corpus["file_bytes"] = corpus_path.stat().st_size
            if mode == "sim":
                gt_out = analysis.gt_summary(items)
                coverage["final"] = analysis.ids_record(analysis.work_ids(items))
        except (OSError, ValueError) as e:
            corpus = {"error": f"{type(e).__name__}: {e}"}

    net_snap = net.snapshot()
    stage_snap = stages.snapshot()
    sim_snap = sim.snapshot() if sim is not None else None
    sim_overhead = (sim_snap or {}).get("handler_time_s", 0.0)
    overhead = (net_snap["instrument_overhead_s"] + sim_overhead
                + analysis_overhead["s"])

    raw_items = sum((s.get("items") or 0) for s in stage_snap["sources"])
    dedup_stage = stage_snap["stages"].get("deduplication", {})
    if sim_snap is not None:
        records_received = sim_snap.get("records_served")
    elif net.parse_records:
        records_received = net_snap["total"]["records"]
    else:
        records_received = None
    items_summary = {
        "records_received": records_received,
        "returned_by_sources": raw_items if stage_snap["sources"] else None,
        "dedup_in": dedup_stage.get("items_in"),
        "unique": dedup_stage.get("items_out", corpus.get("items")),
        "final_corpus": corpus.get("items"),
    }

    return {
        "scenario": spec["scenario"],
        "entry": entry,
        "mode": mode,
        "latency_ms": spec.get("latency_ms"),
        "params": params,
        "tree": info["tree"],
        "module_files": info["files"],
        "error": error,
        "entry_info": entry_info,
        "warmup": warmup,
        "import_s": round(import_s, 3),
        "duration_s": round(duration, 4),
        "overhead_s": round(overhead, 4),
        "duration_adjusted_s": round(max(duration - overhead, 0.0), 4),
        "network": net_snap,
        "trace": net.trace if spec.get("keep_trace") else None,
        "sleeps": sleeps.snapshot(),
        "http_cache": cache_counter.snapshot(),
        "stages": stage_snap,
        "items": items_summary,
        "corpus": corpus,
        "ground_truth": {"dedup_input": dedup_input, "output": gt_out} if mode == "sim" else None,
        "coverage": coverage,
        "mcp_return": analysis.measure_tool_result(result) if result is not None else None,
        "memory": {
            "maxrss_before_bytes": rss_before,
            "maxrss_after_bytes": rss_after,
            "maxrss_delta_bytes": max(rss_after - rss_before, 0),
            "tracemalloc_peak_bytes": tm_peak,
        },
        "simulator": sim_snap,
        "real_socket_attempts": (guard_sock.attempts if guard_sock else None),
        "python": sys.version.split()[0],
    }
