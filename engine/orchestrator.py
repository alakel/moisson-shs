"""
Orchestration d'une moisson, partagée par la CLI et le serveur MCP (OPT-07).

``harvest(request, ctx)`` exécute les connecteurs demandés, collecte un
``SourceResult`` par source (notices et statut), déduplique et renvoie un
``HarvestResult``. L'écriture des fichiers est faite par l'adaptateur
(``engine.outputs.write_outputs``), après l'exécution : une moisson annulée
ne publie rien.

Ordonnancement (OPT-09, topologie plate) : un seul pool de ``workers``
fils sur toutes les sources, sans barrière « natives puis proxies ». L'ordre
de démarrage (``connectors.registry.plan_order``) place d'abord les sources
longues et le premier consommateur du flux Isidore partagé, puis les autres
consommateurs de ce flux, qui relisent les pages déjà téléchargées. La
politesse par hôte de ``utils.http`` borne la charge de chaque hôte quel que
soit le parallélisme, et le flux Isidore (``connectors.isidore_scan``) est sûr
entre fils : une page n'est téléchargée qu'une fois ; une page en échec est
retentée une fois par le flux lui-même après
``config.ISIDORE_DEFERRED_RETRY_S`` secondes (registre de moisson,
``ScanRegistry.for_run``), si bien que tous ses consommateurs, démarrés
ensemble, partagent la même issue. Le résultat publié suit l'ordre des
sources demandées, pas l'ordre d'exécution.

Statuts : chaque connecteur publie son compte rendu (``last_status``) ;
``run_source`` en déduit le statut (``engine.models.resolve_source_status``)
et convertit les exceptions : clé d'API absente → ``skipped`` ; autre
exception → ``error`` ; annulation → ``cancelled``. Une panne n'est donc
jamais présentée comme « aucun article ».

Annulation : le contexte (``RunContext``) porte un signal (événement et
échéance) transmis au client HTTP de l'exécution et aux connecteurs, vérifié
entre les sources, entre les pages, pendant les attentes et après la
déduplication. À l'annulation, les payloads bruts de l'exécution sont
supprimés et ``OperationCancelled`` remonte à l'adaptateur, avec le résultat
partiel dans son attribut ``result`` (jamais publié). En cas d'échec total
(``HarvestResult.all_failed``), les payloads bruts sont aussi supprimés :
aucun corpus ne sera publié.

Échéance souple (OPT-09 bis, ``RunContext.soft_deadline``, posée par le
serveur MCP en stdio) : chaque source reçoit une vue du contexte dont le
signal tombe à l'échéance (``RunContext.for_source``), sans lever celui de
l'exécution. Une source arrêtée est ``error`` (``partial`` avec des notices,
``skipped`` si elle n'a pas démarré), motif ``deadline``
(``_soft_deadline_outcome``) ; les autres sont dédupliquées et publiées par
l'adaptateur. Une source encore occupée après le délai de grâce est
abandonnée (``_run_watched_pool``). Les connecteurs du dépôt rendent les
notices déjà reçues (contrat ``RunContext.soft_stop``, OPT-09 ter) : la
source est ``partial`` ; sans notice, ``error``. Une source à enrichissement
OAI abandonnée après la grâce (``GetRecord`` non interruptible) garde ses
notices Isidore, sans enrichissement (``_enrichment_fallback``). Sans échéance souple (CLI, HTTP), statuts
et publication ne changent pas. Pendant l'attente de sources en cours, une
progression intermédiaire est émise pour tout client qui a demandé la
progression (``config.HARVEST_PROGRESS_HEARTBEAT_S``), transport HTTP
compris ; la CLI, sans progression, garde ses chemins historiques.
"""

from __future__ import annotations

import inspect
import logging
import time
from concurrent.futures import (
    FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait as wait_futures,
)
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from connectors.isidore_scan import ScanRegistry
import config
from connectors.registry import (
    get_connector_class, is_metered_source, plan_order,
)
from engine.models import (
    REASON_DEADLINE, REASON_METERED, REASON_MISSING_KEY,
    SOFT_STOP_DURING_ENRICHMENT, STATUS_CANCELLED, STATUS_ERROR,
    STATUS_PARTIAL, STATUS_SKIPPED, HarvestResult, RunContext, SearchReport,
    SearchRequest, SourceResult, classify_exception, resolve_source_status,
)
from engine.outputs import cleanup_raw, raw_path, write_raw
from utils.http import CancelSignal, HTTPClient, OperationCancelled
from utils.secrets import MissingAPIKey
from utils.source_labels import canonical_source_key, display_name

logger = logging.getLogger("moisson.engine")

# Sources qui acceptent l'enrichissement OAI (option enrich_oai).
ENRICH_OAI_SOURCES = frozenset({"openedition", "cairn", "persee", "erudit"})


# === Ordonnancement ===

def plan_schedule(sources: Sequence[str]) -> List[List[str]]:
    """
    Groupes de sources exécutés l'un après l'autre (chaque groupe dans le
    pool de workers).

    Topologie plate (OPT-09) : un groupe unique, dans l'ordre de démarrage
    de ``connectors.registry.plan_order`` (sources longues et ouvreur du flux
    Isidore d'abord, autres consommateurs du flux ensuite). Jusqu'à OPT-09 :
    deux groupes, sources natives puis sous-sources d'Isidore. Le paramètre
    ``schedule=`` de ``harvest`` permet toujours d'imposer un autre plan.
    """
    ordered = plan_order(list(sources))
    return [ordered] if ordered else []


def pool_size(workers: int, n_jobs: int) -> int:
    """
    Fils du pool d'un groupe : ``workers`` demandé, borné par le nombre de
    sources du groupe et par ``config.HARVEST_MAX_WORKERS``.
    """
    return max(1, min(int(workers or 1), n_jobs, config.HARVEST_MAX_WORKERS))


# === Une source ===

# Options de recherche transmises par ``run_source`` aux seuls connecteurs
# dont ``search`` les accepte (paramètre nommé ou ``**kwargs``) : mode de
# moisson et langues demandées (OPT-12).
SEARCH_OPTIONS = ("mode", "languages")


def _language_list(languages: Any) -> Optional[List[str]]:
    """
    Codes de langue ISO 639-1 demandés, normalisés (minuscules, sans
    doublon, ordre conservé) ; ``"fr,en"`` et ``["fr", "en"]`` sont acceptés.
    None si aucun code exploitable : le connecteur ne filtre pas.
    """
    if not languages:
        return None
    if isinstance(languages, str):
        languages = languages.replace(";", ",").replace(" ", ",").split(",")
    codes: List[str] = []
    for value in languages:
        code = str(value or "").strip().lower()
        if len(code) == 2 and code.isalpha() and code.isascii() and code not in codes:
            codes.append(code)
    return codes or None


def _search_options(search: Callable, **options: Any) -> Dict[str, Any]:
    """
    Options (``mode``, ``languages``) acceptées par la méthode ``search``
    d'un connecteur : paramètre de ce nom ou ``**kwargs``. Une option absente
    (None) n'est jamais transmise ; une signature illisible ne reçoit rien
    (comportement historique).
    """
    wanted = {k: v for k, v in options.items()
              if v is not None and k in SEARCH_OPTIONS}
    if not wanted:
        return {}
    try:
        params = inspect.signature(search).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return wanted
    return {k: v for k, v in wanted.items()
            if k in params and params[k].kind in (params[k].POSITIONAL_OR_KEYWORD,
                                                  params[k].KEYWORD_ONLY)}


def _collect_ranking(result: SourceResult, report: Any, name: str) -> None:
    """
    Rangs et scores natifs du compte rendu (OPT-12) portés par le résultat,
    seulement s'ils sont alignés sur ses notices (une entrée par notice) :
    sinon ils sont écartés (journal), jamais attribués à la mauvaise notice.
    """
    result.native_sort = getattr(report, "native_sort", None)
    result.native_filters = dict(getattr(report, "native_filters", None) or {})
    ranks = list(getattr(report, "native_ranks", None) or [])
    if ranks and len(ranks) != len(result.items):
        logger.warning(
            f"[{name}] {len(ranks)} rang(s) natif(s) pour {len(result.items)} "
            f"notice(s) : rangs écartés (non alignés)")
        ranks = []
    result.native_ranks = [dict(r) for r in ranks]
    # Référence des notices classées : un filtre appliqué ensuite à
    # ``result.items`` (filtre local de langue) ne désaligne pas les rangs
    # servis par ``aligned_ranks`` / ``ranking_dict``.
    result.bind_ranks()


def run_source(
    name: str,
    connector_cls,
    query: str,
    max_per_source: int,
    output_dir,
    timestamp: str,
    http_client,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    *,
    run_context: Optional[RunContext] = None,
    source_key: Optional[str] = None,
    mode: Optional[str] = None,
    languages: Optional[Sequence[str]] = None,
    **kwargs,
) -> SourceResult:
    """
    Exécute une source et renvoie son résultat (notices et statut).

    Signature positionnelle historique de ``main.run_source`` conservée
    (nom d'affichage, classe, requête, plafond, répertoire de sortie,
    horodatage ou run_id, client HTTP). Les erreurs sont isolées : une
    exception devient un statut (``skipped`` pour une clé absente, ``error``
    sinon), jamais une liste vide muette. Le payload brut de la source est
    écrit dans ``<output_dir>/raw/`` (si ``output_dir``) quand la source a
    été exécutée.

    Mode et langues (OPT-12) : transmis à ``connector.search`` seulement si
    elle les accepte (paramètre nommé ou ``**kwargs``, cf.
    ``_search_options``) ; un connecteur qui les ignore garde son
    comportement, et le filtre de langue est alors fait localement par
    l'orchestrateur. Ils peuvent aussi arriver parmi les options du
    connecteur (``kwargs``) : ils en sont retirés, le constructeur ne les
    reçoit jamais.

    Rangs natifs (OPT-12) : le compte rendu du connecteur (``last_status``)
    porte une entrée par notice renvoyée (rang, score natif, tri) ; elles
    passent dans ``SourceResult.native_ranks`` si elles sont alignées sur les
    notices, jamais dans les notices elles-mêmes (invariant 1).

    Args:
        name: Libellé d'affichage (« HAL », « Persée »…), nom des journaux et
            du fichier brut.
        connector_cls: Classe du connecteur.
        query, max_per_source, from_date, to_date: paramètres de recherche.
        output_dir: Répertoire des payloads bruts (None : aucun).
        timestamp: Identifiant d'exécution (suffixe du fichier brut).
        http_client: Client HTTP de l'exécution.
        run_context: Contexte (annulation, registre Isidore).
        source_key: Clé de registre (déduite du libellé si absente).
        mode: ``bibliographie`` ou ``collecte`` (None : défaut du
            connecteur, comportement historique ``collecte``).
        languages: codes ISO 639-1 demandés (None : aucun filtre).
        **kwargs: Options du connecteur (``enrich_oai``, ``scan_registry``…).

    Returns:
        ``SourceResult``.
    """
    key = source_key or canonical_source_key(name)
    # Mode et langues reçus parmi les options du connecteur : ce sont des
    # options de recherche, pas de construction.
    if mode is None:
        mode = kwargs.pop("mode", None)
    else:
        kwargs.pop("mode", None)
    if languages is None:
        languages = kwargs.pop("languages", None)
    else:
        kwargs.pop("languages", None)
    logger.info(f"=== Démarrage source: {name} ===")
    start = time.monotonic()
    connector = None
    result = SourceResult(source=key, display_name=name)
    try:
        if run_context is not None:
            run_context.check()
        # Signature uniforme des connecteurs (http_client, run_context,
        # **kwargs) : les kwargs non pertinents sont ignorés par la cible.
        connector = connector_cls(http_client=http_client,
                                  run_context=run_context, **kwargs)
        # from_date/to_date propagés aux connecteurs qui les supportent.
        search_kwargs = {}
        if from_date:
            search_kwargs["from_date"] = from_date
        if to_date:
            search_kwargs["to_date"] = to_date
        search_kwargs.update(_search_options(
            connector.search, mode=mode, languages=_language_list(languages)))
        items = connector.search(query, max_results=max_per_source,
                                 **search_kwargs)
        items = list(items or [])
        report = getattr(connector, "last_status", None)
        status, reason, detail = resolve_source_status(report, len(items))
        result.items = items
        result.status = status
        result.reason = reason
        result.detail = detail
        if report is not None:
            result.limit_reached = report.limit_reached
            result.exhausted = report.exhausted
            result.provider_window = report.provider_window
            result.pages = report.pages
            result.counters = dict(report.counters)
            result.warnings = list(report.warnings)
            n_unmapped = int(report.counters.get("mapping_errors") or 0)
            if n_unmapped:
                result.warnings.append(
                    f"{n_unmapped} notice(s) reçue(s) non lue(s) "
                    f"(échec du mappage)")
            _collect_ranking(result, report, name)
        if output_dir is not None:
            write_raw(output_dir, name, timestamp, items)
    except OperationCancelled as exc:
        result.items = []
        result.status = STATUS_CANCELLED
        result.reason = exc.reason
        result.detail = str(exc)
        # Arrêt par l'échéance souple (revue OPT-09 bis) : pages et
        # compteurs déjà publiés par le connecteur, pour dire ce qui avait
        # été reçu et n'est pas conservé. Annulation de la moisson (client,
        # échéance dure) : rien ne change, rien n'est publié.
        soft_stop = getattr(run_context, "soft_stop", None)
        if callable(soft_stop) and soft_stop(exc):
            _note_received_before_stop(result, connector)
        logger.warning(f"=== Source {name} annulée ({exc.reason}) ===")
    except MissingAPIKey as exc:
        result.status = STATUS_SKIPPED
        result.reason = REASON_MISSING_KEY
        result.detail = str(exc)
        logger.warning(f"=== Source {name} ignorée : {exc} ===")
    except Exception as exc:  # noqa: BLE001 - une source n'arrête pas les autres
        reason, detail = classify_exception(exc)
        result.status = STATUS_ERROR
        result.reason = reason
        result.detail = detail
        logger.error(f"=== Échec source {name}: {exc} ===", exc_info=True)
    finally:
        if connector is not None:
            try:
                connector.close()
            except Exception:  # noqa: BLE001 - fermeture best-effort
                logger.debug(f"[{name}] fermeture du connecteur", exc_info=True)
        result.duration_s = time.monotonic() - start
    level = logging.INFO if result.succeeded else logging.WARNING
    logger.log(
        level,
        f"=== {name} terminé : {result.item_count} items en "
        f"{result.duration_s:.1f}s — {result.describe()} ===",
    )
    return result


def _coerce_result(key: str, name: str, value: Any) -> SourceResult:
    """
    Résultat d'un exécutant de source : ``SourceResult``, ou liste de notices
    (exécutant hérité, doublure de test), convertie en statut
    ``complete`` / ``empty``.
    """
    if isinstance(value, SourceResult):
        return value
    items = list(value or [])
    status, reason, detail = resolve_source_status(None, len(items))
    return SourceResult(source=key, display_name=name, items=items,
                        status=status, reason=reason, detail=detail)


# === Exécution d'un groupe ===

def _source_jobs(names: Sequence[str], request: SearchRequest,
                 ctx: RunContext) -> List[Dict[str, Any]]:
    """
    Tâches des sources d'un groupe. Plan de requête (OPT-11,
    ``request.query_plan()``) : requête propre à chaque source (``query``),
    options de recherche transmises à ``run_source`` (``mode`` ; ``languages``
    là où le filtre de langue est natif), options du connecteur (``lr`` de
    Google Scholar) et langues du filtre local (``local_languages``).
    """
    plan = request.query_plan()
    jobs = []
    for key in names:
        kwargs: Dict[str, Any] = {"scan_registry": ctx.scan_registry}
        if key in ENRICH_OAI_SOURCES:
            kwargs["enrich_oai"] = request.enrich_oai
        entry = plan.for_source(key)
        search: Dict[str, Any] = {"mode": request.mode}
        query = request.query
        local_languages: List[str] = []
        if entry is not None:
            query = entry.query_sent
            kwargs.update(entry.connector_kwargs)
            search.update(entry.search_kwargs)
            local_languages = list(entry.local_languages)
        jobs.append({"key": key, "name": display_name(key),
                     "cls": get_connector_class(key), "kwargs": kwargs,
                     "query": query, "search": search,
                     "local_languages": local_languages})
    return jobs


def _call_runner(job: Dict[str, Any], request: SearchRequest,
                 ctx: RunContext) -> SourceResult:
    """
    Appelle ``run_source`` (résolu à l'appel : remplaçable par les tests)
    avec la requête et les options de la source (plan de requête, OPT-11),
    puis applique le filtre local de langue quand la source n'a pas de
    filtre natif : une notice de langue connue et non demandée est écartée,
    une notice sans langue lisible est conservée ; les deux nombres vont dans
    les compteurs de la source et un avertissement les nomme. Les rangs
    natifs (OPT-12) suivent les notices conservées (``keep_items``).
    """
    from utils.query_plan import supported_kwargs

    search = supported_kwargs(run_source, job.get("search") or {})
    # Échéance souple (OPT-09 bis) : la source reçoit une vue du contexte
    # dont le signal (et celui du client HTTP) tombe à l'échéance, sans
    # toucher au signal de l'exécution ; sans échéance souple, le contexte
    # lui-même.
    src_ctx = ctx.for_source()
    job["started_at"] = time.monotonic()
    try:
        value = run_source(
            job["name"], job["cls"], job.get("query", request.query),
            request.max_per_source,
            ctx.output_dir, ctx.run_id, src_ctx.http_client,
            from_date=request.from_date, to_date=request.to_date,
            run_context=src_ctx, source_key=job["key"], **search, **job["kwargs"],
        )
    except OperationCancelled as exc:
        value = SourceResult(source=job["key"], display_name=job["name"],
                             status=STATUS_CANCELLED, reason=exc.reason,
                             detail=str(exc),
                             duration_s=time.monotonic() - job["started_at"])
    except Exception as exc:  # noqa: BLE001 - exécutant de remplacement
        reason, detail = classify_exception(exc)
        logger.error(f"Source {job['name']} en erreur : {exc}")
        return SourceResult(source=job["key"], display_name=job["name"],
                            status=STATUS_ERROR, reason=reason, detail=detail)
    result = _soft_deadline_outcome(
        _coerce_result(job["key"], job["name"], value), job, ctx)
    # Arrêtée pendant l'enrichissement OAI (facultatif) : ses notices
    # Isidore, déjà obtenues, sont reprises sans enrichissement (revue
    # OPT-09 bis) plutôt que perdues.
    result = _enrichment_fallback(result, job, request, ctx)
    return _local_language_filter(result, job)


def _local_language_filter(result: SourceResult,
                           job: Dict[str, Any]) -> SourceResult:
    """
    Filtre local de langue d'une source sans filtre natif (OPT-11) : une
    notice de langue connue et non demandée est écartée, une notice sans
    langue lisible est conservée ; les deux nombres vont dans les compteurs
    de la source et un avertissement les nomme.
    """
    from utils.query_plan import apply_local_language_filter

    languages = job.get("local_languages") or []
    if languages and result.items:
        kept, stats = apply_local_language_filter(result.items, languages)
        # keep_items : les rangs natifs (OPT-12) des notices écartées sont
        # retirés avec elles, la liste reste alignée sur les notices.
        result.keep_items(kept)
        result.counters["language_filter_removed"] = stats["removed"]
        result.counters["language_filter_unknown_kept"] = stats["unknown_kept"]
        if stats["removed"] or stats["unknown_kept"]:
            result.warnings.append(
                f"filtre local de langue ({', '.join(languages)}) : "
                f"{stats['removed']} notice(s) écartée(s), "
                f"{stats['unknown_kept']} sans langue lisible conservée(s)")
    return result


# === Échéance souple (OPT-09 bis) ===

def _seconds_text(value: Optional[float]) -> str:
    """Durée lisible (« 50 », « 12.5 »)."""
    return format(float(value or 0.0), "g")


# Compteur posé sur une source arrêtée AVANT l'échéance souple parce que son
# prochain créneau de requête tombait au-delà (échéance « intenable ») : le
# retour MCP distingue ces sources de celles arrêtées à l'échéance
# (``mcp_server._note_soft_deadline``).
SOFT_DEADLINE_UNREACHABLE = "soft_deadline_unreachable"


def _soft_deadline_phrase(ctx: RunContext, unreachable: bool) -> str:
    """« échéance souple de 50 s atteinte » ou « … intenable (…) »."""
    seconds = _seconds_text(ctx.soft_deadline_s)
    if unreachable:
        return (f"échéance souple de {seconds} s intenable (prochain créneau "
                f"de requête au-delà)")
    return f"échéance souple de {seconds} s atteinte"


def _kept_count(result: SourceResult, job: Dict[str, Any]) -> str:
    """
    « K notice(s) conservée(s) » d'une source arrêtée par l'échéance souple,
    compté comme le corpus publié (revue OPT-09 ter) : le filtre local de
    langue (``_local_language_filter``), appliqué APRÈS le statut d'échéance,
    est anticipé ici sans toucher aux notices. S'il en écartera, la phrase
    le dit (« … sur N reçue(s), filtre local de langue ») ; son
    avertissement en donnera le détail.
    """
    received = result.item_count
    kept = received
    languages = job.get("local_languages") or []
    if languages and result.items:
        from utils.query_plan import apply_local_language_filter
        kept = apply_local_language_filter(result.items, languages)[1]["kept"]
    text = f"{kept} notice(s) conservée(s)"
    if kept < received:
        text += f" sur {received} reçue(s), filtre local de langue"
    return text


def _soft_deadline_outcome(result: SourceResult, job: Dict[str, Any],
                           ctx: RunContext) -> SourceResult:
    """
    Statut d'une source arrêtée par l'échéance souple de l'exécution.

    Une source rendue ``cancelled`` pour le motif ``deadline`` alors que le
    signal de l'EXÉCUTION n'est pas levé (ni annulation par le client, ni
    échéance dure) a été arrêtée par l'échéance souple, la seule autre
    échéance qu'elle voie (``RunContext.source_signal``) : ce n'est pas une
    annulation de la moisson. Y compris avant l'échéance, quand son prochain
    créneau de requête tombait au-delà (politesse par hôte, ``Retry-After``
    reçu par une autre source du même hôte) : l'échéance était intenable
    (compteur ``SOFT_DEADLINE_UNREACHABLE``).
    Elle devient ``partial`` si elle rend des notices, ``error`` sinon,
    ``skipped`` si elle a démarré après l'échéance (jamais exécutée) ; le
    motif reste ``deadline`` et le détail le dit. Tout autre résultat est
    rendu tel quel.

    Les connecteurs du dépôt suivent le contrat de ``RunContext.soft_stop``
    (OPT-09 ter) : ils rendent les notices déjà reçues avec un compte rendu
    ``partial`` de motif ``deadline``, dont le détail est complété ici
    (revue OPT-09 ter) : nature de l'arrêt (« collecte interrompue », ou
    « pendant l'enrichissement OAI » si le connecteur l'a marqué,
    ``SOFT_STOP_DURING_ENRICHMENT``) et nombre de notices publiées, compté
    après le filtre local de langue appliqué ensuite (``_kept_count``).
    Arrêtée avant toute notice, une source propage l'arrêt et reste
    ``error`` ; le détail nomme alors les pages reçues et non conservées
    (``_note_received_before_stop``, valable aussi pour un connecteur tiers
    qui ne suit pas le contrat). Une source à enrichissement OAI abandonnée
    après la grâce garde ses notices Isidore (``_enrichment_fallback``).
    """
    if (ctx.soft_deadline is not None and result.status == STATUS_PARTIAL
            and result.reason == REASON_DEADLINE and not ctx.signal.is_set()):
        # Collecte interrompue rendue par le connecteur (RunContext.soft_stop).
        job["soft_stopped"] = True
        unreachable = not ctx.soft_deadline_passed()
        if unreachable:
            result.counters[SOFT_DEADLINE_UNREACHABLE] = 1
        if result.detail:
            result.warnings.append(result.detail)
        phrase = _soft_deadline_phrase(ctx, unreachable)
        if result.counters.get(SOFT_STOP_DURING_ENRICHMENT):
            # Arrêt pendant l'enrichissement OAI (étape facultative) : la
            # recherche Isidore était terminée, la collecte n'a pas été
            # interrompue (revue OPT-09 ter) ; l'avertissement du connecteur
            # dit combien de notices ont été traitées.
            result.detail = (f"{phrase} pendant l'enrichissement OAI, "
                             f"recherche Isidore terminée : "
                             f"{_kept_count(result, job)}")
        else:
            result.detail = (f"{phrase} : collecte interrompue, "
                             f"{_kept_count(result, job)}")
        logger.warning(f"=== Source {job['name']} : {result.describe()} ===")
        return result
    if (ctx.soft_deadline is None or result.status != STATUS_CANCELLED
            or result.reason != REASON_DEADLINE or ctx.signal.is_set()):
        return result
    seconds = _seconds_text(ctx.soft_deadline_s)
    started = job.get("started_at")
    if started is not None and started >= ctx.soft_deadline:
        result.status = STATUS_SKIPPED
        result.detail = (f"échéance souple de {seconds} s atteinte avant le "
                         f"démarrage de la source")
    else:
        job["soft_stopped"] = True
        result.status = STATUS_PARTIAL if result.items else STATUS_ERROR
        unreachable = not ctx.soft_deadline_passed()
        if unreachable:
            result.counters[SOFT_DEADLINE_UNREACHABLE] = 1
        result.detail = (f"{_soft_deadline_phrase(ctx, unreachable)} : source "
                         f"arrêtée après {result.duration_s:.1f} s")
        if result.items:
            result.detail += f", {_kept_count(result, job)}"
        elif result.pages:
            result.detail += (f", {result.pages} page(s) déjà reçue(s) non "
                              f"conservée(s)")
    result.reason = REASON_DEADLINE
    logger.warning(f"=== Source {job['name']} : {result.describe()} ===")
    return result


def _note_received_before_stop(result: SourceResult, connector: Any) -> None:
    """
    Source arrêtée par une annulation ou une échéance : pages, compteurs et
    avertissements que le connecteur avait déjà publiés (``last_status``)
    sont recopiés dans le résultat, et un avertissement dit que les
    enregistrements reçus ne sont pas conservés (les connecteurs propagent
    l'arrêt, ``OperationCancelled``, sans rendre leur collecte en cours).
    """
    report = getattr(connector, "last_status", None)
    if not isinstance(report, SearchReport):
        return
    result.pages = report.pages
    result.counters = dict(report.counters)
    result.warnings = list(report.warnings)
    if report.pages:
        result.warnings.append(
            f"arrêt après {report.pages} page(s) reçue(s) ({report.records} "
            f"enregistrement(s) lu(s)) : les notices déjà reçues ne sont pas "
            f"conservées (le connecteur ne rend pas une collecte interrompue)")


def _offline_client(ctx: RunContext) -> Any:
    """
    Vue du client HTTP de l'exécution dont le signal est déjà levé : aucune
    requête ne part (``policy_get`` vérifie le signal avant tout accès, cache
    compris) et toute attente s'interrompt ; seules les données déjà en
    mémoire de l'exécution (flux Isidore du registre) restent lisibles.
    None si le client n'offre pas de vue (doublure).
    """
    with_cancel = getattr(ctx.http_client, "with_cancel", None)
    if not callable(with_cancel):
        return None
    try:
        return with_cancel(CancelSignal(ctx.cancel_event, time.monotonic()))
    except Exception:  # noqa: BLE001 - doublure sans vue possible
        return None


def _enrichment_fallback(result: SourceResult, job: Dict[str, Any],
                         request: SearchRequest,
                         ctx: RunContext) -> SourceResult:
    """
    Source à enrichissement OAI (``enrich_oai``) arrêtée par l'échéance
    souple, ou abandonnée après elle : ses notices Isidore, obtenues avant
    l'enrichissement (étape facultative, une requête OAI par notice), sont
    reprises SANS enrichissement plutôt que perdues.

    La source est relancée sans enrichissement, hors contexte, sur un client
    dont le signal est levé (``_offline_client``) : elle ne relit que le flux
    Isidore déjà téléchargé du registre de l'exécution, sans aucune requête.
    Si le flux n'était pas complet (arrêt pendant la recherche Isidore), la
    relance échoue aussitôt et le résultat reste celui de l'arrêt. Réussie,
    elle donne ``partial``, motif ``deadline``, avec un avertissement ; le
    payload brut est écrit pour ces notices.
    """
    if (not job.get("soft_stopped") or result.status != STATUS_ERROR
            or result.reason != REASON_DEADLINE or ctx.soft_deadline is None
            or ctx.signal.is_set() or job.get("key") not in ENRICH_OAI_SOURCES
            or not (job.get("kwargs") or {}).get("enrich_oai")):
        return result
    client = _offline_client(ctx)
    if client is None:
        return result
    from utils.query_plan import supported_kwargs

    kwargs = dict(job["kwargs"])
    kwargs["enrich_oai"] = False
    search = supported_kwargs(run_source, job.get("search") or {})
    try:
        value = run_source(
            job["name"], job["cls"], job.get("query", request.query),
            request.max_per_source, None, ctx.run_id, client,
            from_date=request.from_date, to_date=request.to_date,
            run_context=None, source_key=job["key"], **search, **kwargs,
        )
    except (OperationCancelled, Exception):  # noqa: BLE001 - relance facultative
        logger.debug(f"[{job['name']}] relance sans enrichissement",
                     exc_info=True)
        return result
    kept = _coerce_result(job["key"], job["name"], value)
    if kept.status in (STATUS_CANCELLED, STATUS_ERROR, STATUS_SKIPPED) \
            or not kept.items:
        return result
    unreachable = bool(result.counters.get(SOFT_DEADLINE_UNREACHABLE))
    warnings = list(kept.warnings)
    if kept.status == STATUS_PARTIAL:
        # Limite de la recherche Isidore elle-même (fenêtre de 200…).
        warnings.append(kept.describe())
    warnings.append(
        "enrichissement OAI interrompu par l'échéance souple : métadonnées "
        "Isidore conservées sans enrichissement (deadline_seconds plus grand "
        "pour l'enrichissement)")
    if job.get("abandoned"):
        warnings.append(
            f"source toujours occupée après {_seconds_text(ctx.soft_grace())} "
            f"s de grâce : son résultat tardif est ignoré")
    kept.warnings = warnings
    kept.status = STATUS_PARTIAL
    kept.reason = REASON_DEADLINE
    # Compte des notices publiées, après le filtre local de langue appliqué
    # ensuite par l'appelant (revue OPT-09 ter, ``_kept_count``).
    kept.detail = (f"{_soft_deadline_phrase(ctx, unreachable)} pendant "
                   f"l'enrichissement OAI : notices Isidore reprises sans "
                   f"enrichissement, {_kept_count(kept, job)}")
    if unreachable:
        kept.counters[SOFT_DEADLINE_UNREACHABLE] = 1
    begun = job.get("started_at")
    kept.duration_s = time.monotonic() - begun if begun is not None else 0.0
    if ctx.output_dir is not None:
        write_raw(ctx.output_dir, job["name"], ctx.run_id, kept.items)
    logger.warning(f"=== Source {job['name']} : {kept.describe()} ===")
    return kept


def _abandoned_result(job: Dict[str, Any], ctx: RunContext,
                      started: bool) -> SourceResult:
    """
    Résultat d'une source encore occupée (``started``) ou jamais démarrée à
    la fin du délai de grâce qui suit l'échéance souple : elle n'est plus
    attendue, son éventuel résultat tardif est ignoré.
    """
    seconds = _seconds_text(ctx.soft_deadline_s)
    if started:
        job["soft_stopped"] = True
        job["abandoned"] = True
        begun = job.get("started_at")
        status = STATUS_ERROR
        detail = (f"échéance souple de {seconds} s atteinte : source toujours "
                  f"occupée après {_seconds_text(ctx.soft_grace())} s de grâce, "
                  f"abandonnée (résultat tardif ignoré)")
        duration = time.monotonic() - begun if begun is not None else 0.0
    else:
        status = STATUS_SKIPPED
        detail = (f"échéance souple de {seconds} s atteinte avant le démarrage "
                  f"de la source")
        duration = 0.0
    result = SourceResult(source=job["key"], display_name=job["name"],
                          status=status, reason=REASON_DEADLINE, detail=detail,
                          duration_s=duration)
    logger.warning(f"=== Source {job['name']} : {result.describe()} ===")
    return result


def _discard_late_raw(ctx: RunContext, job: Dict[str, Any],
                      keep: Optional[List[Dict]] = None) -> Callable:
    """
    Rappel de fin d'une source abandonnée : supprime le payload brut qu'elle
    aurait écrit après coup (le corpus publié ne la contient pas). Si des
    notices de repli ont été publiées pour elle (``keep``, notices Isidore
    sans enrichissement), le payload brut est réécrit avec elles, sauf si
    l'exécution a été annulée entre-temps (rien n'est alors publié).
    """
    def _discard(_future) -> None:
        if ctx.output_dir is None:
            return
        if keep is not None and not ctx.signal.is_set():
            try:
                write_raw(ctx.output_dir, job["name"], ctx.run_id, keep)
            except OSError:
                logger.debug(f"[{job['name']}] payload brut de repli",
                             exc_info=True)
            return
        try:
            raw_path(ctx.output_dir, job["name"], ctx.run_id).unlink()
            logger.info(f"[{job['name']}] payload brut tardif supprimé "
                        f"(source abandonnée à l'échéance)")
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug(f"[{job['name']}] payload brut tardif", exc_info=True)
    return _discard


def _heartbeat_interval() -> float:
    """Intervalle des notifications intermédiaires (0 : aucune)."""
    try:
        return max(0.0, float(getattr(config, "HARVEST_PROGRESS_HEARTBEAT_S", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _run_watched_pool(jobs: List[Dict[str, Any]], size: int,
                      request: SearchRequest, ctx: RunContext,
                      on_wait: Optional[Callable[[List[str], int], None]],
                      heartbeat: float) -> Iterator[SourceResult]:
    """
    Pool de sources surveillé : rend chaque résultat dès qu'il est connu,
    comme ``as_completed``, et en plus :

    - pendant l'attente, appelle ``on_wait(sources en cours, sources en file)``
      quand aucune source ne s'est terminée depuis ``heartbeat`` secondes
      (progression intermédiaire), jamais après l'échéance souple ;
    - avec une échéance souple (OPT-09 bis) : les sources la voient par leur
      propre signal (``RunContext.for_source``) et rendent la main d'elles-
      mêmes ; à l'échéance plus le délai de grâce (``ctx.soft_grace()``), une
      source encore occupée est abandonnée (``error``, motif ``deadline``,
      résultat tardif et payload brut ignorés), une source jamais démarrée
      est ``skipped`` ; le pool est alors fermé sans attendre ses fils.

    Sans abandon, la fermeture attend la fin réelle des fils (aucun fil
    fantôme), comme le pool historique, y compris sur annulation.
    """
    soft = ctx.soft_deadline
    limit = soft + ctx.soft_grace() if soft is not None else None
    pool = ThreadPoolExecutor(max_workers=size,
                              thread_name_prefix="moisson-source")
    futures = {pool.submit(_call_runner, job, request, ctx): job for job in jobs}
    rank = {fut: i for i, fut in enumerate(futures)}

    def _ordered(futs) -> list:
        return sorted(futs, key=rank.__getitem__)

    pending = set(futures)
    abandoned = False
    last_event = time.monotonic()
    try:
        while pending:
            now = time.monotonic()
            if limit is not None and now >= limit:
                abandoned = True
                for fut in _ordered(pending):
                    job = futures[fut]
                    if fut.done():
                        yield fut.result()
                    elif fut.cancel():
                        yield _abandoned_result(job, ctx, started=False)
                    else:
                        # Source à enrichissement OAI : ses notices Isidore
                        # sont reprises sans enrichissement (relecture du
                        # flux en mémoire, aucune requête) plutôt que
                        # perdues avec le résultat tardif.
                        res = _enrichment_fallback(
                            _abandoned_result(job, ctx, started=True),
                            job, request, ctx)
                        # Payload brut : notices de la source avant le filtre
                        # local de langue, comme pour une source terminée.
                        keep = (list(res.items) if res.status == STATUS_PARTIAL
                                else None)
                        fut.add_done_callback(
                            _discard_late_raw(ctx, job, keep=keep))
                        yield _local_language_filter(res, job)
                pending = set()
                break
            beat = heartbeat > 0 and on_wait is not None and (
                soft is None or now < soft)
            timeout = None if limit is None else limit - now
            if beat:
                next_beat = last_event + heartbeat - now
                timeout = next_beat if timeout is None else min(timeout, next_beat)
            done, pending = wait_futures(
                pending, timeout=None if timeout is None else max(0.0, timeout),
                return_when=FIRST_COMPLETED)
            if done:
                for fut in _ordered(done):
                    yield fut.result()
                last_event = time.monotonic()
                continue
            now = time.monotonic()
            if (beat and pending and now - last_event >= heartbeat
                    and (soft is None or now < soft)):
                running = [futures[f]["name"] for f in _ordered(pending)
                           if f.running()]
                on_wait(running, len(pending) - len(running))
                last_event = time.monotonic()
    finally:
        if abandoned:
            # Les sources abandonnées finissent seules (leurs requêtes sont
            # bornées par l'échéance) ; la moisson n'attend plus.
            pool.shutdown(wait=False, cancel_futures=True)
        else:
            for fut in futures:
                fut.cancel()
            pool.shutdown(wait=True)


def _run_group(names: Sequence[str], request: SearchRequest,
               ctx: RunContext,
               on_wait: Optional[Callable[[List[str], int], None]] = None,
               ) -> Iterator[SourceResult]:
    """
    Exécute un groupe de sources, en série ou dans un pool borné
    (``pool_size``), et rend chaque résultat dès qu'il est connu. Les sources
    sont soumises dans l'ordre du plan : un fil libre prend la suivante.
    L'annulation est vérifiée avant chaque source ; en parallèle, les sources
    en cours la voient entre deux pages et le groupe attend leur fin réelle
    (aucun fil fantôme).

    Pool surveillé (``_run_watched_pool``, OPT-09 bis), même avec un seul
    fil : avec une échéance souple (``ctx.soft_deadline``), ou avec
    ``on_wait`` (progression intermédiaire pendant l'attente, si
    ``config.HARVEST_PROGRESS_HEARTBEAT_S`` > 0). Sinon, chemins historiques.
    """
    jobs = _source_jobs(names, request, ctx)
    size = pool_size(request.workers, len(jobs))
    heartbeat = _heartbeat_interval() if on_wait is not None else 0.0
    if ctx.soft_deadline is not None or heartbeat > 0:
        yield from _run_watched_pool(jobs, size, request, ctx, on_wait,
                                     heartbeat)
        return
    if size > 1:
        with ThreadPoolExecutor(max_workers=size,
                                thread_name_prefix="moisson-source") as pool:
            futures = {pool.submit(_call_runner, job, request, ctx): job
                       for job in jobs}
            try:
                for fut in as_completed(futures):
                    yield fut.result()
            finally:
                for fut in futures:
                    fut.cancel()
    else:
        for job in jobs:
            ctx.check()   # annulation AVANT de démarrer la source suivante
            yield _call_runner(job, request, ctx)


# === Moisson ===

def _metered_refusals(request: SearchRequest) -> Dict[str, SourceResult]:
    """Sources facturées non autorisées par la demande : non exécutées."""
    refused = {}
    if request.allow_metered:
        return refused
    for key in request.sources:
        if is_metered_source(key):
            refused[key] = SourceResult(
                source=key, display_name=display_name(key),
                status=STATUS_SKIPPED, reason=REASON_METERED,
                detail="source facturée à la requête, non autorisée ici",
            )
    return refused


def deduplicate_items(items: List[Dict]):
    """
    Déduplication finale : ``deduplicate_with_report`` si disponible
    (notices et rapport), sinon ``deduplicate`` (rapport absent).
    """
    from utils import deduplication

    with_report = getattr(deduplication, "deduplicate_with_report", None)
    if callable(with_report):
        out, report = with_report(items)
        return out, report
    return deduplication.deduplicate(items), None


def harvest(request: SearchRequest, ctx: Optional[RunContext] = None, *,
            schedule: Optional[Callable[[Sequence[str]], List[List[str]]]] = None,
            ranking: Optional[str] = None,
            ) -> HarvestResult:
    """
    Exécute une moisson et renvoie son résultat, sans rien publier.

    Args:
        request: ce qui est demandé (sources déjà résolues).
        ctx: contexte d'exécution ; un contexte neuf si absent. Son client
            HTTP (créé et fermé ici s'il manque) reçoit le signal
            d'annulation ; un registre de flux Isidore propre à l'exécution
            (``ScanRegistry.for_run`` : reprise différée d'une page en échec)
            est créé s'il manque.
        schedule: ordonnancement de remplacement (défaut ``plan_schedule``).

    Returns:
        ``HarvestResult`` : un ``SourceResult`` par source demandée (dans
        l'ordre demandé), notices dédupliquées (si ``request.dedup``) et
        rapport de déduplication. En cas d'échec total, les payloads bruts
        de l'exécution sont supprimés (rien ne sera publié).

    Plan de requête (OPT-11) : calculé avant tout accès réseau
    (``request.query_plan()``) ; chaque source reçoit sa requête et ses
    filtres ; le plan sérialisé, complété de ce qui a été appliqué (filtre
    local de langue), est porté par ``HarvestResult.query_plan``.

    Classement (OPT-13, ``utils.ranking``) : après la déduplication, le
    classement ``native`` (fusion des rangs natifs des sources qui classent
    par pertinence) est toujours calculé ; ``local`` (concepts du plan de
    requête, BM25 et pondération simple, combinés au classement natif)
    seulement si demandé ; ``off`` : aucun. ``ranking`` : ``native``,
    ``local`` ou ``off`` ; None : ``request.ranking`` s'il existe, sinon
    ``config.RANKING_DEFAULT`` (``MOISSON_RANKING``). Le résultat
    (``HarvestResult.ranking`` : rang, score, composantes, justification,
    version) ne modifie ni les notices (invariant 1) ni leur ordre.

    Raises:
        OperationCancelled: annulation ou échéance ; ``exc.result`` porte le
            résultat partiel (jamais publié) ; les payloads bruts de
            l'exécution sont supprimés.
        utils.query_plan.QueryPlanError: requête vide ou sans terme,
            variante ou langue invalide, bornes de dates invalides (levée
            avant tout accès réseau).
        ValueError: classement demandé inconnu (levée avant tout accès
            réseau).
    """
    from utils.ranking import rank_harvest, resolve_ranking_mode

    # Plan de requête et classement d'abord : une demande invalide échoue
    # sans réseau.
    ranking_mode = resolve_ranking_mode(
        ranking if ranking is not None else getattr(request, "ranking", None))
    plan = request.query_plan()
    for line in plan.text_lines():
        logger.info(line)

    def _plan_dict(sources: List[SourceResult]) -> Dict[str, Any]:
        # Ce qui a été appliqué à l'exécution (filtre local de langue).
        execution: Dict[str, Dict[str, Any]] = {}
        for res in sources:
            entry = plan.for_source(res.source)
            if entry is not None and entry.local_languages and \
                    "language_filter_removed" in res.counters:
                execution[res.source] = {"language_filter": {
                    "languages": list(entry.local_languages),
                    "kept": res.item_count,
                    "removed": res.counters.get("language_filter_removed", 0),
                    "unknown_kept": res.counters.get(
                        "language_filter_unknown_kept", 0),
                }}
        return plan.as_dict(execution=execution)

    ctx = ctx or RunContext()
    started = time.monotonic()
    own_client = ctx.http_client is None
    if own_client:
        ctx.http_client = HTTPClient(use_cache=request.cache)
    try:
        # Le signal d'annulation accompagne chaque requête de l'exécution.
        ctx.http_client.cancel_signal = ctx.signal
    except AttributeError:  # client sans attribut modifiable (doublure)
        pass
    if ctx.scan_registry is None:
        ctx.scan_registry = ScanRegistry.for_run()

    refused = _metered_refusals(request)
    runnable = [key for key in request.sources if key not in refused]
    groups = (schedule or plan_schedule)(runnable)
    total = len(request.sources)
    results: Dict[str, SourceResult] = dict(refused)
    done = len(refused)

    def _ordered() -> List[SourceResult]:
        # Ordre des sources demandées (indépendant de l'ordre d'exécution) ;
        # une source hors demande (plan imposé) vient ensuite.
        order = list(request.sources)
        order += [k for g in groups for k in g if k not in order]
        return [results[k] for k in order if k in results]

    def _heartbeat(running_names: List[str], queued: int) -> None:
        # Progression pendant l'attente de sources encore en cours (OPT-09
        # bis) : valeur strictement croissante (spécification MCP), toujours
        # sous la valeur entière suivante (fin de la prochaine source).
        elapsed = time.monotonic() - started
        message = (f"En cours : {', '.join(running_names) or 'aucune'}"
                   + (f" ; {queued} en attente" if queued else "")
                   + f" ({elapsed:.0f} s écoulées")
        left = (ctx.soft_deadline - time.monotonic()
                if ctx.soft_deadline is not None else None)
        if left is not None:
            message += f", échéance dans {max(0.0, left):.0f} s"
        ctx.report(done + elapsed / (elapsed + 60.0), float(total),
                   message + ")")

    try:
        ctx.check()
        for group in groups:
            ctx.check()
            running = _run_group(
                group, request, ctx,
                on_wait=_heartbeat if ctx.progress is not None else None)
            try:
                for res in running:
                    results[res.source] = res
                    done += 1
                    if res.status == STATUS_CANCELLED:
                        # Source arrêtée par l'annulation, ou par une échéance
                        # qu'elle ne pouvait plus tenir (créneau de politesse
                        # au-delà) : l'exécution entière est annulée, les
                        # sources encore en cours sont arrêtées.
                        ctx.cancel()
                        raise OperationCancelled(res.reason or "cancelled")
                    ctx.report(float(done), float(total),
                               f"Source {res.display_name} terminée "
                               f"({done}/{total})")
            finally:
                # Fermeture explicite : en cas d'annulation, les sources en
                # cours du groupe sont attendues (fin réelle des fils) avant
                # le nettoyage des payloads bruts.
                running.close()
        ctx.check()

        ordered = _ordered()
        all_items: List[Dict] = []
        for res in ordered:
            all_items.extend(res.items)
        total_before = len(all_items)
        logger.info(f"Total avant déduplication : {total_before} items")

        dedup_report = None
        if request.dedup:
            all_items, dedup_report = deduplicate_items(all_items)
        # Classement (OPT-13) : rangs natifs rattachés aux notices finales
        # (groupes de la déduplication s'ils sont fournis, sinon identité et
        # clés fortes) ; rien n'est écrit dans les notices, leur ordre reste
        # celui de la déduplication. Durée consignée à part de l'acquisition.
        ranking_result = rank_harvest(
            ordered, all_items, mode=ranking_mode, plan=plan,
            groups=(dedup_report or {}).get("groups"), check=ctx.check,
            elapsed_before=time.monotonic() - started)
        # Dernier contrôle après la déduplication et le classement (plus
        # d'une seconde sur un gros corpus) : une annulation ou une échéance
        # survenue pendant ces phases empêche toute publication.
        ctx.check()
    except OperationCancelled as exc:
        removed = cleanup_raw(ctx.output_dir, ctx.run_id)
        partial = _ordered()
        for key in runnable:
            if key not in results:
                partial.append(SourceResult(
                    source=key, display_name=display_name(key),
                    status=STATUS_CANCELLED, reason=exc.reason,
                    detail="non démarrée"))
        exc.result = HarvestResult(
            run_id=ctx.run_id, request=request, sources=partial, items=[],
            total_before_dedup=sum(r.item_count for r in partial),
            duration_s=time.monotonic() - started,
            query_plan=_plan_dict(partial),
        )
        logger.warning(
            f"Moisson {ctx.run_id} annulée ({exc.reason}) après "
            f"{time.monotonic() - started:.2f} s ; {removed} payload(s) brut(s) "
            f"supprimé(s), aucun corpus publié"
        )
        raise
    finally:
        if own_client:
            ctx.http_client.close()

    result = HarvestResult(
        run_id=ctx.run_id, request=request, sources=ordered, items=all_items,
        total_before_dedup=total_before, dedup_report=dedup_report,
        duration_s=time.monotonic() - started,
        query_plan=_plan_dict(ordered),
        ranking=ranking_result,
    )
    if result.all_failed:
        # Échec total : aucun corpus ne sera publié ; les payloads bruts de
        # l'exécution (listes vides des sources en échec) ne doivent pas
        # rester orphelins dans raw/.
        removed = cleanup_raw(ctx.output_dir, ctx.run_id)
        if removed:
            logger.info(f"Échec total : {removed} payload(s) brut(s) supprimé(s)")
    return result
