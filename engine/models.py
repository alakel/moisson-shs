"""
Contrats internes du moteur de moisson (OPT-07, constats C30 et C31).

Ces objets ne sortent jamais dans le JSON Zotero (invariant 1) : ils
décrivent une exécution, pas une notice. Ils circulent entre les adaptateurs
(CLI, serveur MCP), l'orchestrateur et les connecteurs.

Statuts d'une source (``SourceResult.status``) :

- ``complete`` : périmètre demandé exécuté (plafond atteint ou fournisseur
  épuisé) ;
- ``empty`` : exécuté sans résultat ;
- ``partial`` : interrompu ou tronqué (erreur après des pages réussies,
  fenêtre de 200 résultats d'Isidore atteinte avant le plafond, motif
  ``provider_window``, borne de parcours, budget, corps invalide) ;
- ``skipped`` : non exécuté (clé absente, source facturée non autorisée,
  crédits SerpAPI sous le plancher) ;
- ``error`` : échec sans résultat exploitable ;
- ``cancelled`` : annulation ou échéance.

Indicateurs complémentaires : ``limit_reached`` (plafond atteint),
``exhausted`` (fournisseur épuisé), ``provider_window`` (fenêtre du
fournisseur atteinte).

Un connecteur garde sa signature ``search(...) -> List[Dict]`` et publie son
compte rendu dans ``connector.last_status`` (``SearchReport``) ;
l'orchestrateur en déduit le statut (``resolve_source_status``).
"""

from __future__ import annotations

import contextvars
import copy
import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests

from utils.http import (
    CANCEL_REASON_CANCELLED, CANCEL_REASON_DEADLINE, CancelSignal,
    OperationCancelled,
)


# === Statuts ===

STATUS_COMPLETE = "complete"
STATUS_EMPTY = "empty"
STATUS_PARTIAL = "partial"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

STATUSES = (STATUS_COMPLETE, STATUS_EMPTY, STATUS_PARTIAL, STATUS_SKIPPED,
            STATUS_ERROR, STATUS_CANCELLED)

# Source exécutée jusqu'au bout de son périmètre (réponse fiable, même vide).
SUCCESS_STATUSES = frozenset({STATUS_COMPLETE, STATUS_EMPTY})
# Aucun résultat exploitable.
FAILED_STATUSES = frozenset({STATUS_ERROR, STATUS_SKIPPED, STATUS_CANCELLED})

STATUS_LABELS = {
    STATUS_COMPLETE: "complète",
    STATUS_EMPTY: "aucun résultat",
    STATUS_PARTIAL: "partielle",
    STATUS_SKIPPED: "non exécutée",
    STATUS_ERROR: "en échec",
    STATUS_CANCELLED: "annulée",
}


# === Motifs (``SourceResult.reason``) ===

REASON_PROVIDER_WINDOW = "provider_window"      # fenêtre servie par le fournisseur
REASON_SCAN_BOUND = "scan_bound"                # borne ISIDORE_SCAN_MAX_RECORDS
REASON_HTTP_ERROR = "http_error"                # statut HTTP d'échec
REASON_NETWORK_ERROR = "network_error"          # délai, connexion
REASON_INVALID_BODY = "invalid_body"            # corps non JSON ou inattendu
REASON_BUDGET = "budget_exhausted"              # quota ou budget épuisé
REASON_MISSING_KEY = "missing_key"              # clé d'API absente
REASON_METERED = "metered_not_allowed"          # source facturée non autorisée
REASON_CREDITS = "insufficient_credits"         # solde SerpAPI sous le plancher
REASON_CANCELLED = CANCEL_REASON_CANCELLED      # annulation demandée
REASON_DEADLINE = CANCEL_REASON_DEADLINE        # échéance dépassée
REASON_EXCEPTION = "exception"                  # erreur inattendue

# Motifs d'une troncature qui n'est pas une panne : le fournisseur a répondu
# normalement mais ne sert pas davantage (fenêtre de 200 résultats d'Isidore,
# C35 ; borne de parcours). Une source ``partial`` pour l'un de ces motifs a
# bien été exécutée : sans notice, elle ne compte pas comme un échec.
BOUNDED_REASONS = frozenset({REASON_PROVIDER_WINDOW, REASON_SCAN_BOUND})

REASON_LABELS = {
    REASON_PROVIDER_WINDOW: "fenêtre de résultats du fournisseur atteinte",
    REASON_SCAN_BOUND: "borne de parcours atteinte",
    REASON_HTTP_ERROR: "erreur HTTP",
    REASON_NETWORK_ERROR: "erreur réseau",
    REASON_INVALID_BODY: "réponse illisible",
    REASON_BUDGET: "quota ou budget épuisé",
    REASON_MISSING_KEY: "clé d'API absente",
    REASON_METERED: "source facturée non autorisée",
    REASON_CREDITS: "crédits SerpAPI insuffisants",
    REASON_CANCELLED: "annulation demandée",
    REASON_DEADLINE: "échéance dépassée",
    REASON_EXCEPTION: "erreur inattendue",
}


# === Modes (OPT-12 : tri et pagination à la source ; bibliographie = HAL par
# score, CrossRef par score, Semantic Scholar par citations ; collecte =
# ordres déterministes historiques) ===

MODE_BIBLIOGRAPHIE = "bibliographie"
MODE_COLLECTE = "collecte"
MODES = (MODE_BIBLIOGRAPHIE, MODE_COLLECTE)


# Annulation d'une moisson : même classe que l'annulation d'une requête HTTP
# (``utils.http.OperationCancelled``), qui porte le motif et, posé par
# l'orchestrateur, le résultat partiel (jamais publié).
HarvestCancelled = OperationCancelled


# === Classification d'une erreur ===

def classify_exception(exc: BaseException) -> Tuple[str, str]:
    """
    Motif et détail court d'une exception de collecte.

    Le détail ne reprend jamais le message brut d'une exception réseau (il
    porte l'URL, donc parfois une clé d'API) : seulement un type ou un statut.

    Returns:
        ``(motif, détail)``.
    """
    from utils.http_scholarly import BudgetExhausted
    from utils.secrets import MissingAPIKey

    if isinstance(exc, OperationCancelled):
        return exc.reason, str(exc)
    if isinstance(exc, MissingAPIKey):
        return REASON_MISSING_KEY, "clé d'API absente"
    if isinstance(exc, BudgetExhausted):
        return REASON_BUDGET, "quota ou budget épuisé"
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return REASON_HTTP_ERROR, f"HTTP {status}" if status else "erreur HTTP"
    if isinstance(exc, (json.JSONDecodeError,
                        requests.exceptions.JSONDecodeError)):
        # Avant RequestException : depuis requests 2.27, ``Response.json()``
        # lève ``requests.exceptions.JSONDecodeError``, qui hérite À LA FOIS
        # de RequestException et de json.JSONDecodeError. Un corps non JSON
        # (page HTML de maintenance servie en 200) n'est pas une erreur
        # réseau.
        return REASON_INVALID_BODY, f"corps de réponse illisible ({type(exc).__name__})"
    if isinstance(exc, (requests.Timeout, requests.ConnectionError,
                        requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ContentDecodingError)):
        return REASON_NETWORK_ERROR, type(exc).__name__
    if isinstance(exc, requests.RequestException):
        return REASON_NETWORK_ERROR, type(exc).__name__
    if isinstance(exc, ValueError):
        # json.JSONDecodeError hérite de ValueError : corps non JSON.
        return REASON_INVALID_BODY, f"corps de réponse illisible ({type(exc).__name__})"
    return REASON_EXCEPTION, type(exc).__name__


# Statuts d'un refus d'accès : reçus sans clé, ils disent que la source
# exige une clé (source non configurée), non qu'elle est en panne.
KEYLESS_REFUSAL_STATUSES = (401, 403)


def keyless_refusal(error: Any, has_key: bool, env_var: str) -> Optional[str]:
    """
    Détail d'un refus d'accès (HTTP 401 ou 403) reçu par une source
    interrogée **sans clé** (PAT-05) : la source est alors non configurée
    (motif ``missing_key``), non en panne. Constat du 2026-09-30 : Semantic
    Scholar répond 403 sans clé, et ce refus était rendu comme une erreur
    HTTP (dans ``fetch_by_doi``, il empêchait même de conclure « non
    trouvé »).

    Args:
        error: exception ``requests.HTTPError``, statut entier ou détail
            court de la forme « HTTP 403 ».
        has_key: une clé a-t-elle été envoyée ? Avec une clé, un refus est
            une vraie erreur (clé refusée) : ``None``.
        env_var: variable d'environnement de la clé, nommée dans le détail.

    Returns:
        Le détail (« HTTP 403 sans clé : définir … »), ou ``None``.
    """
    if has_key:
        return None
    status = None
    if isinstance(error, requests.HTTPError):
        status = getattr(getattr(error, "response", None), "status_code", None)
    elif isinstance(error, int):
        status = error
    elif isinstance(error, str):
        head = error.strip().split()
        if len(head) >= 2 and head[0] == "HTTP" and head[1].isdigit():
            status = int(head[1])
    if status not in KEYLESS_REFUSAL_STATUSES:
        return None
    return f"HTTP {status} sans clé : définir {env_var}"


# === Rangs et scores natifs (OPT-12) ===

def native_score(value: Any) -> Optional[float]:
    """
    Score natif d'une notice (``score`` Solr ou CrossRef, ``relevance_score``
    d'OpenAlex) : nombre fini, sinon None (absent, texte, NaN, booléen).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if score == score and score not in (float("inf"), float("-inf")) else None


# === Compte rendu d'un connecteur ===

@dataclass
class SearchReport:
    """
    Compte rendu de collecte publié par un connecteur (``last_status``).

    Le connecteur le remplit au fil de la pagination ; ``status`` n'est posé
    que pour un arrêt anormal (``partial``, ``error``, ``skipped``) : sinon
    l'orchestrateur conclut ``complete`` ou ``empty`` selon les notices.

    Rangs et scores natifs (OPT-12, constats C25 et C26) : ``native_ranks``
    compte une entrée par notice RENVOYÉE, dans l'ordre des notices
    (``{"rank", "score", "sort"}`` : position 1..n dans l'ordre natif du
    fournisseur, score natif ou None, tri appliqué), ``native_sort`` le tri
    demandé au fournisseur et ``native_filters`` les filtres appliqués par le
    fournisseur lui-même (par exemple ``{"languages": ["fr"]}``). Ces
    métadonnées de moisson ne vont jamais dans les notices Zotero
    (invariant 1) : elles suivent ``SourceResult`` jusqu'au futur manifeste.
    """

    source: str = ""
    pages: int = 0
    records: int = 0
    limit_reached: bool = False
    exhausted: bool = False
    provider_window: bool = False
    status: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    counters: Dict[str, Any] = field(default_factory=dict)
    native_sort: Optional[str] = None
    native_ranks: List[Dict[str, Any]] = field(default_factory=list)
    native_filters: Dict[str, Any] = field(default_factory=dict)

    def sorted_by(self, sort: Optional[str]) -> None:
        """Tri natif demandé au fournisseur (``score desc,docid asc``…)."""
        self.native_sort = sort

    def rank(self, position: int, score: Any = None) -> None:
        """
        Rang natif de la notice qui vient d'être retenue (à appeler une fois
        par notice ajoutée, dans l'ordre : la liste reste alignée sur les
        notices renvoyées).

        Args:
            position: position 1..n dans l'ordre natif du fournisseur (les
                documents écartés, mappage en échec ou doublon, laissent un
                trou : le rang reste celui du fournisseur).
            score: score natif (None si le fournisseur n'en donne pas).
        """
        self.native_ranks.append({"rank": int(position),
                                  "score": native_score(score),
                                  "sort": self.native_sort})

    def keep_ranks(self, n_items: int) -> None:
        """Aligne les rangs sur les ``n_items`` notices renvoyées (troncature)."""
        if len(self.native_ranks) > n_items:
            del self.native_ranks[max(0, int(n_items)):]

    def filtered(self, name: str, value: Any) -> None:
        """Filtre appliqué nativement par le fournisseur (langues…)."""
        self.native_filters[name] = value

    def page(self, records: int = 0) -> None:
        """Une page reçue et décodée."""
        self.pages += 1
        self.records += int(records or 0)

    def limit(self) -> None:
        """Plafond demandé atteint."""
        self.limit_reached = True

    def done(self) -> None:
        """Fin normale de la pagination (fournisseur épuisé)."""
        self.exhausted = True

    def window(self, detail: Optional[str] = None,
               reason: str = REASON_PROVIDER_WINDOW) -> None:
        """Fenêtre ou borne du fournisseur atteinte avant le plafond."""
        self.provider_window = reason == REASON_PROVIDER_WINDOW
        self._set(STATUS_PARTIAL, reason, detail)

    def truncated(self, reason: str, detail: Optional[str] = None) -> None:
        """Collecte tronquée (budget…) : partielle."""
        self._set(STATUS_PARTIAL, reason, detail)

    def fail(self, reason: str, detail: Optional[str] = None) -> None:
        """Échec : ``error``, ramené à ``partial`` s'il y a eu des pages."""
        self._set(STATUS_ERROR, reason, detail)

    def fail_with(self, exc: BaseException) -> None:
        """Échec décrit par une exception (motif et détail sans URL)."""
        reason, detail = classify_exception(exc)
        self.fail(reason, detail)

    def skip(self, reason: str, detail: Optional[str] = None) -> None:
        """Source non exécutée."""
        self._set(STATUS_SKIPPED, reason, detail)

    def warn(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def _set(self, status: str, reason: Optional[str],
             detail: Optional[str]) -> None:
        # Un échec prime sur une troncature, un saut sur tout le reste.
        order = {None: 0, STATUS_PARTIAL: 1, STATUS_ERROR: 2, STATUS_SKIPPED: 3}
        if order.get(status, 0) >= order.get(self.status, 0):
            self.status = status
            self.reason = reason
            self.detail = detail


def resolve_source_status(report: Optional[SearchReport],
                          n_items: int) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Statut final d'une source d'après son compte rendu et ses notices.

    Sans compte rendu (connecteur tiers, doublure de test) : ``complete``
    s'il y a des notices, ``empty`` sinon.

    Des notices reçues dont AUCUNE n'a pu être lue (compteur
    ``mapping_errors`` du compte rendu, aucune notice retenue) ne donnent
    jamais « aucun résultat » : c'est une panne interne (schéma de l'API
    changé, par exemple), rapportée comme un corps illisible (``partial``
    après des pages, ``error`` sinon ; une erreur ou un saut déjà posés
    priment).

    Returns:
        ``(statut, motif, détail)``.
    """
    mapping_errors = int((report.counters or {}).get("mapping_errors") or 0) \
        if report is not None else 0
    if (mapping_errors and not n_items
            and (report.status is None or report.status == STATUS_PARTIAL)):
        status = STATUS_PARTIAL if report.pages else STATUS_ERROR
        return (status, REASON_INVALID_BODY,
                f"{mapping_errors} notice(s) reçue(s), aucune n'a pu être lue "
                f"(échec du mappage)")
    if report is None or report.status is None:
        return (STATUS_COMPLETE if n_items else STATUS_EMPTY), None, None
    if report.status == STATUS_ERROR:
        status = STATUS_PARTIAL if (n_items or report.pages) else STATUS_ERROR
        return status, report.reason, report.detail
    return report.status, report.reason, report.detail


# === Demande ===

@dataclass
class SearchRequest:
    """
    Ce qui est demandé à une moisson.

    Attributes:
        query: requête telle que saisie.
        sources: clés de registre, groupes déjà résolus (ordre conservé).
        from_date, to_date: bornes (YYYY ou YYYY-MM-DD).
        mode: ``bibliographie`` (défaut MCP) ou ``collecte`` (défaut CLI) ;
            transmis aux connecteurs qui l'acceptent (OPT-12).
        max_per_source: plafond par source.
        enrich_oai: enrichissement OAI des sous-sources qui le permettent.
        dedup: déduplication finale.
        cache: option à trois états (None : héritée de ``MOISSON_CACHE``).
        workers: taille du pool unique de sources (topologie plate,
            OPT-09), ramenée à ``config.HARVEST_MAX_WORKERS`` au plus.
        allow_metered: autorise les sources facturées nommées ; faux : elles
            sont ``skipped`` (motif ``metered_not_allowed``).
        query_variants: variantes explicites par langue (OPT-11), ex.
            ``{"en": "digital discourse"}`` : une source dont le fonds est
            majoritairement de cette langue reçoit la variante ; sans
            variante, la requête d'origine (aucune traduction implicite).
        languages: codes ISO 639-1 demandés (OPT-11) : filtre natif là où
            la source le permet, local sinon, signalé dans le plan.
        plan: plan de requête (``utils.query_plan.QueryPlan``) ; calculé à
            la demande par ``query_plan()`` s'il est absent.
    """

    query: str
    sources: List[str]
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    mode: str = MODE_BIBLIOGRAPHIE
    max_per_source: int = 1000
    enrich_oai: bool = False
    dedup: bool = True
    cache: Optional[bool] = None
    workers: int = 1
    allow_metered: bool = True
    query_variants: Dict[str, str] = field(default_factory=dict)
    languages: List[str] = field(default_factory=list)
    plan: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(
                f"Mode inconnu : {self.mode!r} (attendu : {', '.join(MODES)})"
            )
        self.sources = list(self.sources or [])
        try:
            self.workers = max(1, int(self.workers or 1))
        except (TypeError, ValueError):
            raise ValueError(f"workers = {self.workers!r} invalide") from None
        # Borne du parallélisme (OPT-09) : ramené sans erreur, pour ne pas
        # refuser une valeur acceptée jusqu'ici.
        import config
        self.workers = min(self.workers, config.HARVEST_MAX_WORKERS)
        self.max_per_source = int(self.max_per_source)
        # Variantes et langues (OPT-11) : normalisées, ou refusées
        # (``QueryPlanError``, sous-classe de ValueError).
        from utils.query_plan import normalize_languages, normalize_variants
        self.query_variants = normalize_variants(self.query_variants)
        self.languages = normalize_languages(self.languages)

    def query_plan(self):
        """
        Plan de requête par source (``utils.query_plan.QueryPlan``), calculé
        une fois sans accès réseau et conservé dans ``plan``.

        Raises:
            utils.query_plan.QueryPlanError: requête vide ou sans terme,
                variante invalide, borne de date invalide ou plage vide.
        """
        if self.plan is None:
            from utils.query_plan import plan_for_request
            self.plan = plan_for_request(self)
        return self.plan


# === Contexte d'exécution ===

def new_run_id(timestamp: Optional[str] = None, unique: bool = True) -> str:
    """Identifiant d'exécution : horodatage à la seconde (+ uuid court)."""
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{uuid.uuid4().hex[:8]}" if unique else ts


# === Échéance souple (OPT-09 bis) ===
#
# Échéance de COLLECTE d'une moisson lancée par le serveur MCP en stdio : à
# l'échéance, les sources encore en cours reçoivent le signal d'arrêt (leur
# signal propre, ``RunContext.source_signal``), sont marquées ``error`` (ou
# ``partial`` si elles rendent des notices) avec le motif ``deadline``, et la
# moisson publie le corpus des autres sources. Un connecteur ne rend ses
# notices déjà reçues que s'il suit le contrat de ``RunContext.soft_stop`` :
# c'est le cas des connecteurs paginés du dépôt, de la lecture du flux
# Isidore et des boucles d'enrichissement OAI (OPT-09 ter,
# ``connectors.base.Connector.soft_stopped``) ; un connecteur qui ne le suit
# pas perd ses notices (les sources à enrichissement OAI abandonnées gardent
# leurs notices Isidore, cf. ``engine.orchestrator``). Rien à voir avec l'échéance
# DURE (``RunContext.deadline``, transport HTTP) : celle-ci annule toute
# l'exécution et rien n'est publié.
#
# L'adaptateur qui l'active (``mcp_server._tool_moisson_search``) ne construit
# pas lui-même le contexte d'exécution : il ouvre une portée
# (``soft_deadline_scope``) autour de l'appel, dans le fil de travail, et tout
# ``RunContext`` créé dans cette portée prend l'échéance. Hors portée (CLI,
# HTTP, tests), aucune échéance souple : comportement historique.

# Compteur du compte rendu (``SearchReport.counters``, valeur 1) posé par un
# connecteur dont l'arrêt souple est survenu PENDANT l'enrichissement OAI,
# étape facultative qui suit une recherche Isidore terminée (revue OPT-09 ter,
# ``Connector.soft_stopped(…, during_enrichment=True)``) : l'orchestrateur dit
# alors « pendant l'enrichissement OAI » et non « collecte interrompue ».
# Absent si un arrêt antérieur (parcours Isidore) reste le motif.
SOFT_STOP_DURING_ENRICHMENT = "soft_stop_during_enrichment"

_SOFT_DEADLINE_SCOPE: "contextvars.ContextVar[Optional[Tuple[float, float]]]" = (
    contextvars.ContextVar("moisson_soft_deadline", default=None))


@contextmanager
def soft_deadline_scope(at: Optional[float],
                        seconds: Optional[float] = None) -> Iterator[None]:
    """
    Portée dans laquelle tout ``RunContext`` créé reçoit une échéance souple.

    Args:
        at: échéance (horloge ``time.monotonic()``) ; None : aucune (la
            portée est alors neutre).
        seconds: durée annoncée dans les messages (défaut : ``at`` moins
            l'instant d'ouverture de la portée).

    La portée est propre au fil (et à la tâche asyncio) qui l'ouvre : deux
    moissons simultanées dans deux fils ne se voient pas.
    """
    value = None
    if at is not None:
        if seconds is None:
            seconds = max(0.0, at - time.monotonic())
        value = (float(at), float(seconds))
    token = _SOFT_DEADLINE_SCOPE.set(value)
    try:
        yield
    finally:
        _SOFT_DEADLINE_SCOPE.reset(token)


def current_soft_deadline() -> Optional[Tuple[float, float]]:
    """Échéance souple de la portée courante : ``(échéance, durée)`` ou None."""
    return _SOFT_DEADLINE_SCOPE.get()


@dataclass
class RunContext:
    """
    Contexte d'une exécution, partagé par l'orchestrateur et les connecteurs.

    Attributes:
        run_id: identifiant (suffixe des fichiers de l'exécution).
        deadline: échéance ``time.monotonic()`` ; None : aucune.
        cancel_event: événement d'annulation (``threading.Event`` ou objet à
            ``is_set()``) ; un événement neuf si absent.
        budgets: limites appliquées (informatif : plafond serveur, échéance).
        progress: rappel ``(fait, total, message)`` après chaque source ; il
            peut lever ``OperationCancelled``.
        http_client: client HTTP de l'exécution (reçoit ``cancel_signal``).
        scan_registry: registre des flux Isidore de l'exécution.
        output_dir: répertoire des payloads bruts ``raw/`` (None : aucun).
        counters: compteurs libres de l'exécution.
        soft_deadline: échéance souple de la collecte (OPT-09 bis, horloge
            ``time.monotonic()``) ; None : aucune. Absente à la construction,
            elle est prise de la portée ``soft_deadline_scope`` si une portée
            est ouverte. Elle ne touche pas ``signal`` (le signal de
            l'exécution) : seules les sources la voient
            (``source_signal``, ``for_source``).
        soft_deadline_s: durée annoncée de l'échéance souple (messages).
        soft_grace_s: délai (secondes) laissé après l'échéance souple aux
            sources pour rendre la main avant d'être abandonnées ; None :
            ``config.STDIO_SOFT_DEADLINE_GRACE_S``.
    """

    run_id: str = field(default_factory=new_run_id)
    deadline: Optional[float] = None
    cancel_event: Any = None
    budgets: Dict[str, Any] = field(default_factory=dict)
    progress: Optional[Callable[[float, Optional[float], str], None]] = None
    http_client: Any = None
    scan_registry: Any = None
    output_dir: Optional[Path] = None
    counters: Dict[str, Any] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    soft_deadline: Optional[float] = None
    soft_deadline_s: Optional[float] = None
    soft_grace_s: Optional[float] = None

    def __post_init__(self):
        if self.cancel_event is None:
            self.cancel_event = threading.Event()
        self.signal = CancelSignal(self.cancel_event, self.deadline)
        self._lock = threading.Lock()
        if self.soft_deadline is None:
            scoped = current_soft_deadline()
            if scoped is not None:
                self.soft_deadline, seconds = scoped
                if self.soft_deadline_s is None:
                    self.soft_deadline_s = seconds
        if self.soft_deadline is not None:
            if self.soft_deadline_s is None:
                self.soft_deadline_s = max(0.0, self.soft_deadline - self.started)
            self.budgets.setdefault("soft_deadline_s",
                                    round(float(self.soft_deadline_s), 1))

    @classmethod
    def with_timeout(cls, seconds: Optional[float], **kwargs) -> "RunContext":
        """Contexte dont l'échéance tombe dans ``seconds`` secondes."""
        deadline = (time.monotonic() + float(seconds)
                    if seconds is not None and seconds > 0 else None)
        return cls(deadline=deadline, **kwargs)

    # --- échéance souple (OPT-09 bis) ---

    @property
    def soft_deadline_active(self) -> bool:
        return self.soft_deadline is not None

    def soft_deadline_passed(self) -> bool:
        """Vrai si l'échéance souple existe et est atteinte."""
        return (self.soft_deadline is not None
                and time.monotonic() >= self.soft_deadline)

    def soft_grace(self) -> float:
        """Délai de grâce après l'échéance souple (secondes, >= 0)."""
        if self.soft_grace_s is not None:
            return max(0.0, float(self.soft_grace_s))
        import config
        return max(0.0, float(getattr(config, "STDIO_SOFT_DEADLINE_GRACE_S", 2.0)))

    def source_signal(self) -> CancelSignal:
        """
        Signal d'annulation d'UNE source : l'événement de l'exécution
        (annulation par le client, propagée telle quelle, motif
        ``cancelled``) et la plus proche des deux échéances, dure et souple
        (motif ``deadline``). L'échéance souple arrête ainsi la source (ses
        requêtes en vol ont leur délai réseau borné par elle, ses attentes
        s'interrompent) sans lever le signal de l'exécution : les autres
        sources déjà terminées sont publiées.
        """
        deadlines = [d for d in (self.deadline, self.soft_deadline) if d is not None]
        return CancelSignal(self.cancel_event, min(deadlines) if deadlines else None)

    def for_source(self) -> "RunContext":
        """
        Vue du contexte propre à une source : signal ``source_signal()`` et
        client HTTP portant ce signal (``HTTPClient.with_cancel`` : même
        session, même cache). Sans échéance souple, le contexte lui-même
        (comportement historique). La vue partage tout le reste (registre
        Isidore, compteurs, identifiant, répertoire des payloads bruts).
        """
        if self.soft_deadline is None:
            return self
        view = copy.copy(self)
        view.signal = self.source_signal()
        # Signal de l'exécution, pour distinguer l'échéance souple d'une
        # annulation de la moisson (``soft_stop``).
        view._run_signal = self.signal
        with_cancel = getattr(self.http_client, "with_cancel", None)
        if callable(with_cancel):
            try:
                view.http_client = with_cancel(view.signal)
            except Exception:  # noqa: BLE001 - doublure sans vue possible
                view.http_client = self.http_client
        return view

    def soft_stop(self, exc: BaseException) -> bool:
        """
        Vrai si ``exc`` arrête la source par la SEULE échéance souple
        (revue OPT-09 bis) : ``OperationCancelled`` de motif ``deadline``,
        échéance souple active, signal de l'exécution non levé (ni
        annulation par le client, ni échéance dure). Vaut aussi pour un arrêt
        anticipé (prochain créneau de requête au-delà de l'échéance).

        Contrat offert aux connecteurs : sur un tel arrêt entre deux pages
        (ou pendant une requête, ou entre deux enregistrements OAI), un
        connecteur peut rendre les notices déjà reçues avec
        ``SearchReport.truncated(REASON_DEADLINE, …)`` au lieu de propager ;
        l'orchestrateur publie alors la source ``partial`` (motif
        ``deadline``). Il ne demande alors plus aucune page. Sans notice
        reçue, il propage : l'orchestrateur garde le statut ``error``
        (``skipped`` si la source n'a pas démarré), motif ``deadline``.
        Toute autre annulation doit être propagée telle quelle (la moisson
        est annulée, rien n'est publié). Mise en œuvre commune :
        ``connectors.base.Connector.soft_stopped`` (OPT-09 ter) ; un arrêt
        pendant l'enrichissement OAI y est marqué
        (``SOFT_STOP_DURING_ENRICHMENT``) pour que le détail de la source dise
        la nature de l'arrêt.
        """
        if (self.soft_deadline is None
                or not isinstance(exc, OperationCancelled)
                or getattr(exc, "reason", None) != CANCEL_REASON_DEADLINE):
            return False
        run_signal = getattr(self, "_run_signal", None) or self.signal
        return not run_signal.is_set()

    def cancelled(self) -> bool:
        return self.signal.is_set()

    def cancel_reason(self) -> Optional[str]:
        return self.signal.reason()

    def cancel(self) -> None:
        self.signal.set()

    def check(self) -> None:
        """Lève ``OperationCancelled`` si l'annulation est demandée."""
        self.signal.check()

    def remaining(self) -> Optional[float]:
        return self.signal.remaining()

    def report(self, done: float, total: Optional[float], message: str) -> None:
        if self.progress is not None:
            self.progress(done, total, message)

    def bump(self, key: str, n: float = 1) -> None:
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + n

    def elapsed(self) -> float:
        return time.monotonic() - self.started


# === Résultats ===

@dataclass
class SourceResult:
    """
    Notices et statut d'une source pour une exécution.

    ``native_ranks`` (OPT-12) : une entrée ``{"rank", "score", "sort"}`` par
    notice de ``items``, dans le même ordre (vide si le connecteur n'en publie
    pas, ou si la liste n'était pas alignée sur ses notices) ;
    ``native_sort`` : tri demandé au fournisseur ; ``native_filters`` :
    filtres appliqués par le fournisseur (langues…). Jamais dans les notices
    Zotero (invariant 1) ; ``as_dict`` n'en expose que le tri et les filtres
    (retour MCP compact), la liste complète est servie par
    ``ranking_dict`` (manifeste).
    """

    source: str
    display_name: str
    items: List[Dict] = field(default_factory=list)
    status: str = STATUS_COMPLETE
    reason: Optional[str] = None
    detail: Optional[str] = None
    limit_reached: bool = False
    exhausted: bool = False
    provider_window: bool = False
    pages: int = 0
    counters: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    native_sort: Optional[str] = None
    native_ranks: List[Dict[str, Any]] = field(default_factory=list)
    native_filters: Dict[str, Any] = field(default_factory=dict)
    # Notices auxquelles ``native_ranks`` était aligné à la collecte (mêmes
    # objets, même ordre) : permet de réaligner les rangs par identité quand
    # des notices sont retirées ensuite (filtre local de langue). Références
    # seulement, jamais sérialisées.
    _ranked_items: List[Dict] = field(default_factory=list, repr=False, compare=False)

    def bind_ranks(self) -> None:
        """Mémorise les notices auxquelles ``native_ranks`` est aligné."""
        self._ranked_items = (list(self.items)
                              if len(self.native_ranks) == len(self.items) else [])

    def aligned_ranks(self) -> List[Dict[str, Any]]:
        """
        Rangs natifs alignés sur ``items`` (une entrée par notice, même
        ordre). Si des notices ont été retirées après la collecte sans que
        les rangs le soient (filtre appliqué directement à ``items``), les
        rangs sont réalignés par identité des notices ; si une notice n'a pas
        de rang connu (notice remplacée ou ajoutée), aucun rang n'est rendu :
        jamais un rang attribué à la mauvaise notice.

        Quand les notices classées sont mémorisées (``bind_ranks``), le
        réalignement se fait toujours par identité, même à longueur égale
        (revue OPT-12) : une liste réordonnée ou dont une notice a été
        remplacée ne reçoit pas les rangs par position. Le raccourci par
        position ne vaut que sans liaison (contrat de la classe : rangs
        alignés sur ``items``) ou pour exactement les mêmes objets, dans le
        même ordre.
        """
        ranked = self._ranked_items
        if not ranked:
            if len(self.native_ranks) == len(self.items):
                return [dict(r) for r in self.native_ranks]
            return []
        if len(ranked) != len(self.native_ranks):
            return []
        if len(ranked) == len(self.items) and all(
                a is b for a, b in zip(ranked, self.items)):
            return [dict(r) for r in self.native_ranks]
        by_identity = {id(item): rank for item, rank in zip(ranked, self.native_ranks)}
        out = []
        for item in self.items:
            rank = by_identity.get(id(item))
            if rank is None:
                return []
            out.append(dict(rank))
        return out

    def keep_items(self, kept: List[Dict]) -> None:
        """
        Remplace les notices par un sous-ensemble ordonné de celles-ci
        (filtre local), en retirant les rangs natifs des notices écartées.
        """
        ranks = self.aligned_ranks() if self.native_ranks else []
        by_identity = {id(item): rank for item, rank in zip(self.items, ranks)}
        self.items = list(kept)
        self.native_ranks = [by_identity[id(item)] for item in self.items
                             if id(item) in by_identity]
        if len(self.native_ranks) != len(self.items):
            self.native_ranks = []
        self.bind_ranks()

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def succeeded(self) -> bool:
        """Périmètre exécuté (réponse fiable, même vide)."""
        return self.status in SUCCESS_STATUSES

    @property
    def failed(self) -> bool:
        return self.status in FAILED_STATUSES

    @property
    def executed(self) -> bool:
        """
        Le fournisseur a répondu sans panne : périmètre exécuté (``complete``,
        ``empty``), ou tronqué par une limite du fournisseur (``partial`` de
        motif ``provider_window`` ou ``scan_bound``, voir
        ``BOUNDED_REASONS``). Faux pour ``error``, ``skipped``, ``cancelled``
        et pour un ``partial`` né d'une panne (HTTP, réseau, corps
        illisible, budget).
        """
        if self.status in SUCCESS_STATUSES:
            return True
        return self.status == STATUS_PARTIAL and self.reason in BOUNDED_REASONS

    def describe(self) -> str:
        """Phrase courte : statut, motif et indicateurs."""
        bits = []
        if self.limit_reached:
            bits.append("plafond atteint")
        elif self.exhausted and self.status in SUCCESS_STATUSES:
            bits.append("résultats épuisés")
        if self.reason:
            label = REASON_LABELS.get(self.reason, self.reason)
            if self.detail and self.detail not in label:
                label = f"{label} : {self.detail}"
            bits.append(label)
        text = STATUS_LABELS.get(self.status, self.status)
        return f"{text} ({' ; '.join(bits)})" if bits else text

    def as_dict(self) -> Dict[str, Any]:
        """Statut sérialisable (``structuredContent.by_source_status``)."""
        return {
            "source": self.source,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "items": self.item_count,
            "pages": self.pages,
            "limit_reached": self.limit_reached,
            "exhausted": self.exhausted,
            "provider_window": self.provider_window,
            "warnings": list(self.warnings),
            "counters": dict(self.counters),
            "duration_s": round(self.duration_s, 3),
            # OPT-12 : tri et filtres natifs seulement (les rangs, un par
            # notice, sont servis par ranking_dict).
            "native_sort": self.native_sort,
            "native_filters": dict(self.native_filters),
        }

    def ranking_dict(self) -> Dict[str, Any]:
        """
        Rangs et scores natifs de la source (destinés au manifeste du corpus,
        jamais au JSON Zotero) : tri, filtres natifs et une entrée par notice
        de ``items``, dans le même ordre.
        """
        return {
            "source": self.source,
            "sort": self.native_sort,
            "filters": dict(self.native_filters),
            "ranks": self.aligned_ranks(),
        }


@dataclass
class HarvestResult:
    """
    Résultat consolidé d'une moisson.

    ``query_plan`` (OPT-11) : plan de requête sérialisé (requête envoyée à
    chaque source, filtres natifs et locaux, avertissements), complété de ce
    qui a été appliqué à l'exécution (``applied`` : filtre local de langue) ;
    destiné à ``structuredContent.query_plan``, jamais aux notices.

    ``ranking`` (OPT-13, ``utils.ranking.rank_records``) : classement des
    notices de ``items`` (None si ``off``) : ``version``, ``mode``
    (``native`` ou ``local``), paramètres, classe d'ordre de chaque source,
    durées (``duration_s`` du classement, ``acquisition_s`` avant lui) et
    ``entries`` par rang croissant (``index`` dans ``items``, ``id``,
    ``rank``, ``score``, composantes, dimensions séparées, justification).
    Jamais dans les notices (invariant 1) ; ``items`` garde l'ordre
    déterministe de la déduplication. ``structuredContent.top_ranked`` en
    donne un extrait ; le manifeste (OPT-16) et le digest (OPT-17) le
    consommeront.
    """

    run_id: str
    request: SearchRequest
    sources: List[SourceResult]
    items: List[Dict]
    total_before_dedup: int
    dedup_report: Optional[Dict[str, Any]] = None
    duration_s: float = 0.0
    query_plan: Optional[Dict[str, Any]] = None
    ranking: Optional[Dict[str, Any]] = None

    @property
    def by_source(self) -> Dict[str, int]:
        """Notices renvoyées par source (clé historique ``by_source``)."""
        return {r.display_name: r.item_count for r in self.sources}

    @property
    def by_source_status(self) -> Dict[str, Dict[str, Any]]:
        return {r.display_name: r.as_dict() for r in self.sources}

    @property
    def duplicates_removed(self) -> int:
        return self.total_before_dedup - len(self.items)

    @property
    def all_failed(self) -> bool:
        """
        Échec total (erreur de l'outil) : aucune notice, et aucune source
        n'a répondu sans panne (toutes ``error``, ``skipped``, ``cancelled``,
        ou ``partial`` né d'une panne).

        N'est PAS un échec total une moisson où une source est ``empty``
        (l'absence est établie) ou ``partial`` pour une limite du
        fournisseur (``provider_window``, ``scan_bound``) : une sous-source
        d'Isidore absente de la fenêtre de 200 résultats (C35) a été
        moissonnée normalement ; le corpus vide est publié et la limite est
        nommée dans le statut de la source.
        """
        if self.items:
            return False
        if not self.sources:
            return False
        return not any(r.executed for r in self.sources)

    @property
    def not_complete(self) -> List[SourceResult]:
        return [r for r in self.sources if not r.succeeded]

    @property
    def overall_status(self) -> str:
        """``ok`` (tout exécuté), ``partial`` (au moins une source incomplète)
        ou ``error`` (rien d'exploitable)."""
        if self.all_failed:
            return "error"
        return "partial" if self.not_complete else "ok"

    def status_lines(self, indent: str = "  - ") -> List[str]:
        """Une ligne par source : nom, notices, statut et motif."""
        return [f"{indent}{r.display_name} : {r.item_count} — {r.describe()}"
                for r in self.sources]
