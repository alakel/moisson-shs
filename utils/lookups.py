"""
Fonctions de recherche ciblée par identifiant ou auteur.

Ces fonctions sont utilisées par les outils MCP fetch_by_doi et
search_by_author. Elles orchestrent plusieurs connecteurs pour
maximiser la couverture et la richesse des métadonnées récupérées.

Tous les accès réseau passent par ``utils.http.HTTPClient`` (client fourni
par l'appelant, sinon client partagé) : même politique de réessai, de
politesse par hôte, de cache et de délais que les connecteurs. Un résolveur
international qui échoue (4xx, réseau, quota) renvoie ``None`` sans
interrompre les autres ; les journaux ne portent ni URL ni clé d'API.

Parallélisme (OPT-10) : les résolveurs de ``fetch_by_doi`` et les sources de
``search_by_author`` visent chacun un hôte distinct ; ils sont interrogés en
parallèle sur un pool borné (``LOOKUP_MAX_WORKERS``), la politesse par hôte
restant celle de ``utils.http``. Le résultat ne dépend pas de l'ordre
d'arrivée des réponses :

- ``fetch_by_doi`` fusionne les notices dans l'ordre de préférence
  CrossRef, HAL, Isidore, OpenAlex, Semantic Scholar, CORE : le résolveur
  prioritaire qui répond fournit la base de la notice, les suivants
  l'enrichissent. Une fois ce résultat prioritaire connu, les retardataires
  disposent d'un délai de grâce (``FETCH_BY_DOI_GRACE``) avant d'être
  abandonnés. Chaque notice doit porter le DOI demandé (normalisé), sinon
  elle est rejetée (statut ``doi_non_concordant``). Une erreur réseau reste
  distinguée d'un DOI inconnu (``lookup_doi``). Un résolveur abandonné
  n'envoie plus rien : ses requêtes attendent une place de résolveur par
  hôte (``FETCH_BY_DOI_HOST_SLOTS``, attente interruptible par l'abandon),
  partent avec pour budget le reste de la durée maximale de l'appel, et
  aucune ne part après l'abandon ou l'échéance.
- ``search_by_author`` concatène les résultats dans l'ordre séquentiel
  historique (HAL, Isidore, OpenAlex, Semantic Scholar) avant déduplication
  et tri.

Recherche d'auteur fiable (OPT-14) : identifiants explicites prioritaires
(idHal, identifiant de personne HAL, ORCID, identifiants d'auteur OpenAlex et
Semantic Scholar) ; en recherche par nom, plusieurs profils examinés par
source et ambiguïté rendue (candidats) au lieu du premier homonyme ;
pagination OpenAlex et Semantic Scholar jusqu'au plafond ; notices Isidore
filtrées sur leurs créateurs ; statut par source et rapport d'ensemble
(``AuthorSearchReport``) porté par la liste renvoyée (``AuthorItems``).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from unidecode import unidecode

from engine.models import (
    FAILED_STATUSES, REASON_HTTP_ERROR, REASON_INVALID_BODY, REASON_LABELS,
    REASON_MISSING_KEY, REASON_NETWORK_ERROR, STATUS_CANCELLED,
    STATUS_COMPLETE, STATUS_EMPTY, STATUS_ERROR, STATUS_LABELS,
    STATUS_PARTIAL, STATUS_SKIPPED, SUCCESS_STATUSES, SearchReport,
    SourceResult, classify_exception, keyless_refusal, resolve_source_status,
)
from utils.http import HTTPClient, get_default_client
from utils.deduplication import deduplicate
from utils.doi_resolver import item_doi, normalize_doi
from connectors.hal import HALConnector
from connectors.isidore import IsidoreConnector
from connectors.isidore_scan import ScanRegistry
from connectors.openedition import OpenEditionConnector
from connectors.cairn import CairnConnector
from mappers.zotero_schema import (
    make_empty_item, validate_and_clean, append_to_extra,
)
from mappers.doc_type_mapping import map_dublin_core_type
from mappers.hal_mapper import map_hal_to_zotero
from mappers.isidore_mapper import map_isidore_to_zotero
from mappers.openalex_mapper import map_openalex_item
from mappers.semantic_scholar_mapper import map_s2_item
from mappers.core_mapper import map_core_item
from utils.cleaner import clean_string
from utils.secrets import get_api_key
from config import (
    OPENALEX_API, S2_GRAPH_API, CORE_SEARCH_WORKS,
    ENV_OPENALEX_KEY, ENV_S2_KEY, ENV_CORE_KEY,
    HAL_SEARCH_URL, HAL_FIELDS, ISIDORE_SEARCH_URL, CONTACT_EMAIL,
    LOOKUP_MAX_WORKERS, FETCH_BY_DOI_GRACE, FETCH_BY_DOI_MAX_SECONDS,
    FETCH_BY_DOI_HOST_SLOTS,
)

logger = logging.getLogger(__name__)

# Champs S2 demandés pour fetch_by_doi / search_by_author (cohérent avec le
# connecteur Semantic Scholar).
_S2_FIELDS = (
    "title,abstract,year,publicationDate,authors,externalIds,citationCount,"
    "influentialCitationCount,publicationTypes,publicationVenue,venue,journal,"
    "openAccessPdf,s2FieldsOfStudy,fieldsOfStudy,url"
)
_OPENALEX_SELECT = (
    "id,doi,title,display_name,publication_year,publication_date,language,"
    "type,authorships,primary_location,locations,abstract_inverted_index,concepts,"
    "cited_by_count,open_access,ids,biblio"
)

# Notices demandées à HAL et à Isidore pour un DOI : quelques-unes suffisent
# (le DOI est vérifié notice par notice), au lieu d'un lot de moisson entier.
_HAL_DOI_ROWS = 5
_ISIDORE_DOI_REPLIES = 10

# Statuts d'un résolveur (fetch_by_doi).
LOOKUP_FOUND = "trouve"
LOOKUP_NOT_FOUND = "non_trouve"
LOOKUP_DOI_MISMATCH = "doi_non_concordant"
LOOKUP_ERROR = "erreur"
LOOKUP_SKIPPED = "ignore"          # clé d'API absente : aucun accès réseau
LOOKUP_ABANDONED = "abandonne"     # délai de grâce ou durée maximale dépassés

# Statut global de fetch_by_doi.
DOI_FOUND = "trouve"
DOI_NOT_FOUND = "non_trouve"
DOI_ERROR = "erreur"
DOI_INVALID = "doi_invalide"


@dataclass
class ResolverOutcome:
    """Issue d'un résolveur de DOI."""
    name: str
    status: str
    item: Optional[Dict] = None
    detail: str = ""
    duration_s: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status,
                "detail": self.detail,
                "duration_s": round(self.duration_s, 3)}


# === Accès réseau commun ===

class _LookupAbandoned(Exception):
    """Résolveur abandonné : sa requête n'est pas envoyée (usage interne)."""


class _HostSlots:
    """
    Places par hôte réservées aux résolveurs de ``fetch_by_doi``, partagées
    par tous les appels du processus, attendues de façon interruptible.

    Une requête de résolveur ne peut plus être retirée une fois entrée dans
    ``utils.http`` (attente d'une place en vol, puis envoi) : elle attend donc
    d'abord ici, où l'abandon de son appel l'interrompt sans envoi. Au plus
    ``size`` requêtes de résolveur par hôte sont ainsi engagées à la fois.
    """

    # Période de vérification de l'abandon pendant l'attente (secondes).
    POLL_S = 0.02

    def __init__(self, size: int):
        self.size = max(1, int(size))
        self._lock = threading.Lock()
        self._slots: Dict[str, threading.BoundedSemaphore] = {}

    def _semaphore(self, host: str) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._slots.get(host)
            if sem is None:
                sem = self._slots[host] = threading.BoundedSemaphore(self.size)
            return sem

    def acquire(self, host: str, abandon: threading.Event,
                deadline: Optional[float]) -> bool:
        """Vrai quand une place est obtenue ; faux si l'appel est abandonné
        ou si l'échéance est atteinte avant."""
        sem = self._semaphore(host)
        while True:
            if abandon.is_set():
                return False
            wait_s = self.POLL_S
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait_s = min(wait_s, remaining)
            if sem.acquire(timeout=wait_s):
                if abandon.is_set():
                    sem.release()
                    return False
                return True

    def release(self, host: str) -> None:
        self._semaphore(host).release()


_RESOLVER_SLOTS = _HostSlots(FETCH_BY_DOI_HOST_SLOTS)


class _GuardedClient:
    """
    Client transmis aux résolveurs d'un appel de ``lookup_doi``.

    Chaque requête attend une place de résolveur sur son hôte
    (``_RESOLVER_SLOTS``), revérifie l'abandon de l'appel, puis part avec
    pour budget (politique HTTP commune) le reste de la durée maximale de
    l'appel : aucune requête ne part après l'abandon ou l'échéance. Les
    autres attributs sont ceux du client enveloppé.
    """

    def __init__(self, client: HTTPClient, abandon: threading.Event,
                 deadline: Optional[float]):
        self._client = client
        self._abandon = abandon
        self._deadline = deadline

    def get(self, url, params=None, timeout=None, headers=None, **kwargs):
        host = urlsplit(url).hostname or ""
        if not _RESOLVER_SLOTS.acquire(host, self._abandon, self._deadline):
            raise _LookupAbandoned()
        try:
            if self._abandon.is_set():
                raise _LookupAbandoned()
            if self._deadline is not None and "budget" not in kwargs:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise _LookupAbandoned()
                kwargs["budget"] = remaining
            return self._client.get(url, params=params, timeout=timeout,
                                    headers=headers, **kwargs)
        finally:
            _RESOLVER_SLOTS.release(host)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _get_json_outcome(
    http_client: Optional[HTTPClient],
    url: str,
    label: str,
    params: Optional[Dict] = None,
    headers: Optional[Dict] = None,
) -> Tuple[Any, str, str]:
    """
    GET JSON via la politique HTTP commune, avec l'issue de l'accès.

    Returns:
        ``(corps, état, détail)`` : ``état`` vaut ``"ok"`` (corps JSON
        décodé), ``LOOKUP_NOT_FOUND`` (HTTP 404 : identifiant inconnu de la
        source, cas nominal) ou ``LOOKUP_ERROR`` (autre statut HTTP, erreur
        réseau, réponse non JSON). ``détail`` est un motif court, sans URL
        ni clé d'API (le message d'une exception requests contient l'URL).
    """
    client = http_client if http_client is not None else get_default_client()
    try:
        response = client.get(url, params=params, headers=headers)
    except _LookupAbandoned:
        return None, LOOKUP_ABANDONED, "abandonné avant l'envoi"
    except requests.HTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 404:
            logger.debug(f"{label} : HTTP 404")
            return None, LOOKUP_NOT_FOUND, "HTTP 404"
        logger.warning(f"{label} : HTTP {status}")
        return None, LOOKUP_ERROR, f"HTTP {status}"
    except Exception as e:  # noqa: BLE001 - résolveur best-effort
        logger.warning(f"{label} : {type(e).__name__}")
        return None, LOOKUP_ERROR, type(e).__name__
    if response.status_code != 200:
        return None, LOOKUP_ERROR, f"HTTP {response.status_code}"
    try:
        return response.json(), "ok", ""
    except ValueError as e:
        logger.warning(f"{label} : réponse non JSON ({type(e).__name__})")
        return None, LOOKUP_ERROR, "réponse non JSON"


def _get_json(
    http_client: Optional[HTTPClient],
    url: str,
    label: str,
    params: Optional[Dict] = None,
    headers: Optional[Dict] = None,
):
    """
    GET JSON via la politique HTTP commune ; ``None`` en cas d'échec.

    Un 404 (identifiant inconnu de la source) est un cas nominal, journalisé
    en DEBUG ; les autres échecs le sont en WARNING, par type et statut
    seulement.

    Args:
        http_client: Client HTTP partagé ; à défaut, le client partagé par
            défaut de ``utils.http``.
        url: URL cible.
        label: Libellé du journal (ex. « OpenAlex échec sur 10.1/x »).
        params: Paramètres de query string.
        headers: En-têtes propres à la requête (clé d'API).

    Returns:
        Le corps JSON décodé, ou ``None``.
    """
    payload, state, _ = _get_json_outcome(http_client, url, label, params,
                                          headers)
    return payload if state == "ok" else None


# === Vérification du DOI porté par une notice ===

_URL_DOI_RE = re.compile(
    r"(?:doi\.org/|/doi/(?:abs/|full/|pdf/|epdf/)?)(10\.\d{4,9}/[^\s?#]+)",
    re.IGNORECASE,
)


def _doi_from_url(url: Optional[str]) -> str:
    """DOI explicitement porté par une URL (``doi.org/…``, ``/doi/…``), ou ""."""
    if not url:
        return ""
    match = _URL_DOI_RE.search(str(url))
    return normalize_doi(match.group(1)) if match else ""


def _carried_doi(item: Dict) -> str:
    """DOI normalisé porté par une notice : champ, ``extra``, puis URL."""
    return item_doi(item) or _doi_from_url(item.get("url"))


def _finalize(name: str, item: Optional[Dict], doi: str) -> ResolverOutcome:
    """Issue d'un résolveur : notice absente, concordante ou non concordante."""
    if not item:
        return ResolverOutcome(name, LOOKUP_NOT_FOUND)
    carried = _carried_doi(item)
    if carried == doi:
        return ResolverOutcome(name, LOOKUP_FOUND, item)
    if not carried:
        return ResolverOutcome(name, LOOKUP_DOI_MISMATCH,
                               detail="notice sans DOI vérifiable")
    return ResolverOutcome(name, LOOKUP_DOI_MISMATCH,
                           detail=f"DOI {carried} au lieu de {doi}")


# === fetch_by_doi ===

def _normalize_doi(doi: str) -> str:
    """Normalise un DOI : retire les préfixes URL et casse."""
    return normalize_doi(doi)


def lookup_doi(
    doi: str,
    http_client: Optional[HTTPClient] = None,
    *,
    max_workers: Optional[int] = None,
    grace: Optional[float] = None,
    max_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Récupère une notice par DOI, avec l'issue de chaque résolveur.

    Les résolveurs (CrossRef, HAL, Isidore, OpenAlex, Semantic Scholar,
    CORE, par ordre de préférence) partent en parallèle sur un pool borné.
    Les notices trouvées, toutes vérifiées (DOI demandé), sont fusionnées par
    ``deduplicate``, dont le choix de chaque champ suit les priorités de
    source (``SOURCE_FIELD_PRIORITY`` puis ``GENERAL_SOURCE_ORDER``, qui
    classe CrossRef, HAL et Isidore dans l'ordre de préférence ci-dessus) et
    ne dépend pas de l'ordre d'arrivée : la notice est identique à celle d'une
    exécution séquentielle où tous les résolveurs répondent. Dès que le
    résultat prioritaire est connu (tous les résolveurs de rang supérieur ont
    répondu et l'un a trouvé la notice), les autres disposent de ``grace``
    secondes, puis sont abandonnés. Un résolveur abandonné n'envoie plus
    aucune requête (``_GuardedClient``) ; une requête déjà partie se termine
    en arrière-plan, bornée par le reste de ``max_seconds``, et son résultat
    est ignoré.

    Args:
        doi: DOI (``10.x/y``, ``doi:10.x/y`` ou ``https://doi.org/10.x/y``).
        http_client: Client HTTP partagé optionnel (sinon client propre à
            l'appel, fermé quand tous les résolveurs ont fini).
        max_workers: Taille du pool (défaut ``LOOKUP_MAX_WORKERS``).
        grace: Délai de grâce des retardataires (défaut
            ``FETCH_BY_DOI_GRACE``).
        max_seconds: Durée maximale totale (défaut
            ``FETCH_BY_DOI_MAX_SECONDS``).

    Returns:
        ``{"doi", "status", "item", "resolvers", "duration_s"}``. ``status``
        vaut ``trouve``, ``non_trouve`` (toutes les sources interrogées ont
        répondu sans notice concordante), ``erreur`` (aucune notice, et au
        moins une source en erreur ou abandonnée : l'absence n'est pas
        établie) ou ``doi_invalide``. ``resolvers`` donne, dans l'ordre de
        préférence, ``name``, ``status``, ``detail`` et ``duration_s``.
    """
    started = time.monotonic()
    norm = _normalize_doi(doi)
    if not norm:
        return {"doi": str(doi or ""), "status": DOI_INVALID, "item": None,
                "resolvers": [], "duration_s": 0.0}

    # Table construite à l'appel : les résolveurs restent remplaçables.
    resolvers: List[Tuple[str, Callable]] = [
        ("CrossRef", _lookup_crossref),
        ("HAL", _lookup_hal),
        ("Isidore", _lookup_isidore),
        ("OpenAlex", _lookup_openalex),
        ("Semantic Scholar", _lookup_s2),
        ("CORE", _lookup_core),
    ]
    grace = FETCH_BY_DOI_GRACE if grace is None else max(0.0, float(grace))
    limit = FETCH_BY_DOI_MAX_SECONDS if max_seconds is None else float(max_seconds)
    workers = LOOKUP_MAX_WORKERS if max_workers is None else int(max_workers)
    workers = max(1, min(workers, len(resolvers)))

    own_http = http_client is None
    client = HTTPClient() if own_http else http_client

    outcomes: List[Optional[ResolverOutcome]] = [None] * len(resolvers)
    deadline = started + limit if limit > 0 else None
    # Abandon de l'appel : plus aucune requête de résolveur ne part.
    abandon = threading.Event()
    guarded = _GuardedClient(client, abandon, deadline)
    pool = ThreadPoolExecutor(max_workers=workers,
                              thread_name_prefix="fetch-by-doi")
    futures = {
        pool.submit(_run_resolver, name, fn, norm, guarded): i
        for i, (name, fn) in enumerate(resolvers)
    }
    pending = set(futures)
    winner_at: Optional[float] = None
    abandon_reason = ""
    try:
        while pending:
            cutoff = deadline
            if winner_at is not None:
                cutoff = (winner_at + grace if cutoff is None
                          else min(cutoff, winner_at + grace))
            timeout = None if cutoff is None else cutoff - time.monotonic()
            if timeout is not None and timeout <= 0:
                abandon_reason = (
                    f"abandonné : résultat prioritaire obtenu, délai de "
                    f"grâce de {grace:g} s écoulé"
                    if winner_at is not None and (
                        deadline is None or winner_at + grace <= deadline)
                    else f"abandonné : durée maximale de {limit:g} s atteinte"
                )
                break
            done, pending = wait(pending, timeout=timeout,
                                 return_when=FIRST_COMPLETED)
            for fut in done:
                outcomes[futures[fut]] = fut.result()
            if winner_at is None and _winner_known(outcomes):
                winner_at = time.monotonic()
    finally:
        abandon.set()
        for fut in pending:
            fut.cancel()
            i = futures[fut]
            outcomes[i] = ResolverOutcome(
                resolvers[i][0], LOOKUP_ABANDONED,
                detail=abandon_reason or "abandonné",
                duration_s=time.monotonic() - started,
            )
        pool.shutdown(wait=False, cancel_futures=True)
        if own_http:
            _close_when_done(list(pending), client)

    items = [o.item for o in outcomes if o and o.status == LOOKUP_FOUND]
    merged = None
    if items:
        merged_list = deduplicate(items)
        merged = merged_list[0] if merged_list else None
    if merged:
        status = DOI_FOUND
    elif any(o.status in (LOOKUP_ERROR, LOOKUP_ABANDONED) for o in outcomes):
        status = DOI_ERROR
    else:
        status = DOI_NOT_FOUND
    result = {
        "doi": norm,
        "status": status,
        "item": merged,
        "resolvers": [o.as_dict() for o in outcomes],
        "duration_s": round(time.monotonic() - started, 3),
    }
    logger.info(
        f"[fetch_by_doi] {norm} : {status} en {result['duration_s']} s ("
        + ", ".join(f"{o.name}={o.status}" for o in outcomes) + ")"
    )
    return result


def fetch_by_doi(doi: str, http_client: HTTPClient = None) -> Optional[Dict]:
    """
    Récupère un item Zotero complet à partir d'un DOI.

    Stratégie : résolveurs en parallèle (CrossRef, HAL, Isidore, puis les
    sources internationales), notices vérifiées sur le DOI demandé, fusion
    dans l'ordre de préférence par ``deduplicate`` (voir ``lookup_doi``, qui
    renvoie aussi l'issue de chaque résolveur).

    Args:
        doi: DOI canonique (ex : 10.4000/lidil.6033)
        http_client: Client HTTP partagé optionnel

    Returns:
        Item Zotero unique fusionné ou None si rien trouvé
    """
    return lookup_doi(doi, http_client)["item"]


def _run_resolver(name: str, fn: Callable, doi: str,
                  client: HTTPClient) -> ResolverOutcome:
    """Exécute un résolveur ; ne lève jamais ; revérifie le DOI trouvé."""
    t0 = time.monotonic()
    try:
        outcome = fn(doi, client)
    except Exception as e:  # noqa: BLE001 - un résolveur n'arrête pas les autres
        logger.warning(f"{name} échec sur {doi} : {type(e).__name__}")
        outcome = ResolverOutcome(name, LOOKUP_ERROR, detail=type(e).__name__)
    if not isinstance(outcome, ResolverOutcome):
        outcome = _finalize(name, outcome, doi)
    elif outcome.status == LOOKUP_FOUND:
        outcome = _finalize(name, outcome.item, doi)
    outcome.name = name
    outcome.duration_s = time.monotonic() - t0
    return outcome


def _winner_known(outcomes: List[Optional[ResolverOutcome]]) -> bool:
    """Vrai si tous les résolveurs de rang supérieur au premier résolveur
    ayant trouvé la notice ont répondu (le résultat prioritaire est fixé)."""
    for outcome in outcomes:
        if outcome is None:
            return False
        if outcome.status == LOOKUP_FOUND:
            return True
    return False


def _close_when_done(pending: list, client: HTTPClient) -> None:
    """Ferme ``client`` quand les résolveurs abandonnés auront fini."""
    if not pending:
        client.close()
        return
    remaining = [len(pending)]
    lock = threading.Lock()

    def _done(_fut):
        with lock:
            remaining[0] -= 1
            last = remaining[0] == 0
        if last:
            client.close()

    for fut in pending:
        fut.add_done_callback(_done)


# --- Résolveurs (une issue par source) ---

def _map_crossref_work(data: Dict) -> Dict:
    """Enregistrement CrossRef (``message``) → item Zotero."""
    # Mapping Crossref → Zotero
    cr_type = data.get("type", "")
    item_type_map = {
        "journal-article": "journalArticle",
        "book-chapter": "bookSection",
        "book": "book",
        "proceedings-article": "conferencePaper",
        "report": "report",
        "posted-content": "preprint",
        "dissertation": "thesis",
    }
    item = make_empty_item(item_type_map.get(cr_type, "document"))

    # Titre
    titles = data.get("title", [])
    if isinstance(titles, list) and titles:
        item["title"] = clean_string(titles[0])

    # Auteurs
    for author in data.get("author", []):
        family = author.get("family", "").strip()
        given = author.get("given", "").strip()
        if family or given:
            item["creators"].append({
                "creatorType": "author",
                "firstName": given,
                "lastName": family,
            })

    # Date
    issued = data.get("issued", {}).get("date-parts", [])
    if issued and isinstance(issued[0], list):
        parts = issued[0]
        if len(parts) >= 3:
            item["date"] = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
        elif len(parts) >= 2:
            item["date"] = f"{parts[0]:04d}-{parts[1]:02d}"
        elif parts:
            item["date"] = str(parts[0])

    # Container
    ct = data.get("container-title", [])
    if isinstance(ct, list) and ct:
        if item["itemType"] == "journalArticle":
            item["publicationTitle"] = clean_string(ct[0])
        elif item["itemType"] == "bookSection":
            item["bookTitle"] = clean_string(ct[0])

    # Volume / Issue / Pages
    if data.get("volume"):
        item["volume"] = str(data["volume"])
    if data.get("issue"):
        item["issue"] = str(data["issue"])
    if data.get("page"):
        item["pages"] = str(data["page"])

    # ISSN / ISBN
    issn_list = data.get("ISSN", [])
    if isinstance(issn_list, list) and issn_list:
        item["ISSN"] = issn_list[0]
    isbn_list = data.get("ISBN", [])
    if isinstance(isbn_list, list) and isbn_list:
        item["ISBN"] = isbn_list[0]

    # Publisher
    if data.get("publisher"):
        item["publisher"] = clean_string(data["publisher"])

    # Abstract
    if data.get("abstract"):
        item["abstractNote"] = clean_string(data["abstract"])

    # Language
    if data.get("language"):
        item["language"] = data["language"]

    # DOI (celui de l'enregistrement renvoyé, normalisé : il est comparé au
    # DOI demandé, jamais recopié depuis la demande) et URL
    item["DOI"] = normalize_doi(data.get("DOI") or "")
    if data.get("URL"):
        item["url"] = data["URL"]

    # Source (libellé canonique aligné sur mappers/crossref_mapper.SOURCE_LABEL)
    append_to_extra(item, "Sources", "CrossRef")
    # Sujets : catégories de la revue, tracées dans extra comme le fait
    # mappers/crossref_mapper, jamais en mots-clés (PAT-04).
    subjects = [str(x) for x in (data.get("subject") or []) if x]
    if subjects:
        append_to_extra(item, "CrossRef subjects", ", ".join(subjects))
    return validate_and_clean(item)


def _lookup_crossref(doi: str,
                     http_client: Optional[HTTPClient] = None) -> ResolverOutcome:
    """
    Crossref REST API : métadonnées canoniques pour un DOI.

    Cf. https://api.crossref.org/works/{doi}. Le paramètre ``mailto`` place
    la requête dans le « polite pool » : sans lui, CrossRef la range dans le
    pool public (en-têtes relevés le 2026-09-28 : ``x-api-pool:
    public-single``, 5 requêtes/s, une seule simultanée).
    """
    payload, state, detail = _get_json_outcome(
        http_client, f"https://api.crossref.org/works/{doi}",
        f"Crossref échec sur {doi}", params={"mailto": CONTACT_EMAIL},
    )
    if state != "ok":
        return ResolverOutcome("CrossRef", state, detail=detail)
    data = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ResolverOutcome("CrossRef", LOOKUP_ERROR,
                               detail="réponse inattendue")
    try:
        item = _map_crossref_work(data)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Crossref échec sur {doi} : {type(e).__name__}")
        return ResolverOutcome("CrossRef", LOOKUP_ERROR,
                               detail=f"conversion : {type(e).__name__}")
    return _finalize("CrossRef", item, doi)


def _lookup_hal(doi: str,
                http_client: Optional[HTTPClient] = None) -> ResolverOutcome:
    """
    HAL Solr, filtre ``doiId_s`` (même requête et même ordre ``docid asc``
    que le connecteur), limité à quelques notices ; la première notice qui
    porte le DOI demandé est retenue.
    """
    params = {
        "q": f'doiId_s:"{doi}"',
        "fl": ",".join(HAL_FIELDS),
        "wt": "json",
        "rows": _HAL_DOI_ROWS,
        "sort": "docid asc",
    }
    payload, state, detail = _get_json_outcome(
        http_client, HAL_SEARCH_URL, f"HAL échec sur {doi}", params=params,
    )
    if state != "ok":
        return ResolverOutcome("HAL", state, detail=detail)
    block = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return ResolverOutcome("HAL", LOOKUP_ERROR, detail="réponse inattendue")
    return _first_concordant("HAL", block.get("docs") or [],
                             map_hal_to_zotero, doi)


def _isidore_replies(payload: Any) -> Optional[list]:
    """Notices d'une réponse Isidore (``reply`` isolé ou liste) ; ``None`` si
    la réponse n'a pas la forme attendue."""
    if not isinstance(payload, dict):
        return None
    replies_block = (payload.get("response") or {}).get("replies")
    if not isinstance(replies_block, dict):
        return None
    content = replies_block.get("content", {})
    replies = content.get("reply", []) if isinstance(content, dict) else content
    if isinstance(replies, dict):
        return [replies]
    return replies if isinstance(replies, list) else []


def _lookup_isidore(doi: str,
                    http_client: Optional[HTTPClient] = None) -> ResolverOutcome:
    """
    Isidore : recherche plein texte du DOI, puis vérification notice par
    notice (DOI du champ, de ``extra`` ou explicitement porté par l'URL ;
    une simple sous-chaîne de l'URL ne suffit pas).
    """
    params = {"q": doi, "replies": _ISIDORE_DOI_REPLIES, "page": 1,
              "output": "json"}
    payload, state, detail = _get_json_outcome(
        http_client, ISIDORE_SEARCH_URL, f"Isidore échec sur {doi}",
        params=params,
    )
    if state != "ok":
        return ResolverOutcome("Isidore", state, detail=detail)
    replies = _isidore_replies(payload)
    if replies is None:
        return ResolverOutcome("Isidore", LOOKUP_ERROR,
                               detail="réponse inattendue")
    return _first_concordant("Isidore", replies, map_isidore_to_zotero, doi)


def _first_concordant(name: str, records: list, mapper: Callable,
                      doi: str) -> ResolverOutcome:
    """Première notice concordante parmi ``records`` ; sinon non trouvé ou
    DOI non concordant (motif de la première notice lue)."""
    first_mismatch: Optional[ResolverOutcome] = None
    for record in records:
        try:
            item = mapper(record)
        except Exception as e:  # noqa: BLE001 - notice isolée
            logger.warning(f"{name} : échec de conversion ({type(e).__name__})")
            continue
        outcome = _finalize(name, item, doi)
        if outcome.status == LOOKUP_FOUND:
            return outcome
        if first_mismatch is None and outcome.status == LOOKUP_DOI_MISMATCH:
            first_mismatch = outcome
    if first_mismatch is not None:
        return first_mismatch
    return ResolverOutcome(name, LOOKUP_NOT_FOUND)


def _lookup_openalex(doi: str,
                     http_client: Optional[HTTPClient] = None) -> ResolverOutcome:
    """OpenAlex : Work par DOI (compte de citations, concepts, OA). Best-effort."""
    api_key = get_api_key(ENV_OPENALEX_KEY, required=False)
    if not api_key:
        # OpenAlex exige une clé depuis fév. 2026 : on s'abstient sans clé.
        return ResolverOutcome("OpenAlex", LOOKUP_SKIPPED,
                               detail=f"clé {ENV_OPENALEX_KEY} absente")
    payload, state, detail = _get_json_outcome(
        http_client, f"{OPENALEX_API}/doi:{doi}",
        f"OpenAlex échec sur {doi}",
        params={"api_key": api_key, "select": _OPENALEX_SELECT},
    )
    if state != "ok":
        return ResolverOutcome("OpenAlex", state, detail=detail)
    if not payload:
        return ResolverOutcome("OpenAlex", LOOKUP_NOT_FOUND)
    try:
        item = map_openalex_item(payload)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"OpenAlex échec sur {doi} : {type(e).__name__}")
        return ResolverOutcome("OpenAlex", LOOKUP_ERROR,
                               detail=f"conversion : {type(e).__name__}")
    return _finalize("OpenAlex", item, doi)


def _lookup_s2(doi: str,
               http_client: Optional[HTTPClient] = None) -> ResolverOutcome:
    """Semantic Scholar : paper par DOI (citations influentes). Best-effort."""
    api_key = get_api_key(ENV_S2_KEY, required=False)
    headers = {"x-api-key": api_key} if api_key else None
    payload, state, detail = _get_json_outcome(
        http_client, f"{S2_GRAPH_API}/paper/DOI:{doi}",
        f"Semantic Scholar échec sur {doi}",
        params={"fields": _S2_FIELDS}, headers=headers,
    )
    if state == LOOKUP_ERROR:
        # 401 ou 403 sans clé : résolveur non interrogé (clé requise), comme
        # CORE sans clé ; il ne rend plus le statut global « erreur » (PAT-05).
        refusal = keyless_refusal(detail, bool(api_key), ENV_S2_KEY)
        if refusal:
            return ResolverOutcome("Semantic Scholar", LOOKUP_SKIPPED,
                                   detail=refusal)
    if state != "ok":
        return ResolverOutcome("Semantic Scholar", state, detail=detail)
    if not payload:
        return ResolverOutcome("Semantic Scholar", LOOKUP_NOT_FOUND)
    try:
        item = map_s2_item(payload)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Semantic Scholar échec sur {doi} : {type(e).__name__}")
        return ResolverOutcome("Semantic Scholar", LOOKUP_ERROR,
                               detail=f"conversion : {type(e).__name__}")
    return _finalize("Semantic Scholar", item, doi)


def _lookup_core(doi: str,
                 http_client: Optional[HTTPClient] = None) -> ResolverOutcome:
    """CORE : recherche par DOI (texte intégral / PDF OA). Best-effort."""
    api_key = get_api_key(ENV_CORE_KEY, required=False)
    if not api_key:
        return ResolverOutcome("CORE", LOOKUP_SKIPPED,
                               detail=f"clé {ENV_CORE_KEY} absente")
    payload, state, detail = _get_json_outcome(
        http_client, CORE_SEARCH_WORKS, f"CORE échec sur {doi}",
        params={"q": f'doi:"{doi}"', "limit": 1},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if state != "ok":
        return ResolverOutcome("CORE", state, detail=detail)
    try:
        results = (payload or {}).get("results") or []
        return _first_concordant("CORE", results, map_core_item, doi)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"CORE échec sur {doi} : {type(e).__name__}")
        return ResolverOutcome("CORE", LOOKUP_ERROR,
                               detail=f"conversion : {type(e).__name__}")


# --- Formes historiques (item concordant ou None) ---

def _fetch_from_crossref(
    doi: str, http_client: Optional[HTTPClient] = None
) -> Optional[Dict]:
    """Crossref : item concordant ou None (voir ``_lookup_crossref``)."""
    return _lookup_crossref(doi, http_client).item


def _fetch_from_hal(doi: str, http_client: HTTPClient = None) -> Optional[Dict]:
    """HAL : item concordant ou None (voir ``_lookup_hal``)."""
    return _lookup_hal(doi, http_client).item


def _fetch_from_isidore(doi: str,
                        http_client: HTTPClient = None) -> Optional[Dict]:
    """Isidore : item concordant ou None (voir ``_lookup_isidore``)."""
    return _lookup_isidore(doi, http_client).item


def _fetch_from_openalex(
    doi: str, http_client: Optional[HTTPClient] = None
) -> Optional[Dict]:
    """OpenAlex : item concordant ou None (voir ``_lookup_openalex``)."""
    return _lookup_openalex(doi, http_client).item


def _fetch_from_s2(
    doi: str, http_client: Optional[HTTPClient] = None
) -> Optional[Dict]:
    """Semantic Scholar : item concordant ou None (voir ``_lookup_s2``)."""
    return _lookup_s2(doi, http_client).item


def _fetch_from_core(
    doi: str, http_client: Optional[HTTPClient] = None
) -> Optional[Dict]:
    """CORE : item concordant ou None (voir ``_lookup_core``)."""
    return _lookup_core(doi, http_client).item


# === search_by_author (OPT-14 : recherche d'auteur fiable) ===
#
# Principes (constat C28 du plan d'optimisation) :
#
# - **identifiants explicites prioritaires** : avec un identifiant propre à
#   une source (idHal ou identifiant de personne HAL, ORCID, identifiant
#   d'auteur OpenAlex ou Semantic Scholar), cette source n'est jamais
#   interrogée par le nom ;
# - **recherche par nom** : plusieurs candidats examinés par source (nom
#   compatible, affiliation, identifiants, nombre de travaux, période,
#   co-auteurs principaux) ; au moins deux candidats plausibles ayant chacun
#   au moins ``AUTHOR_AMBIGUITY_MIN_WORKS`` travaux font une **ambiguïté** :
#   la source n'est pas moissonnée, ses candidats sont rendus, et un second
#   appel avec l'identité choisie est demandé (jamais le premier candidat en
#   silence) ; un seul candidat plausible est retenu et nommé, sauf en mode
#   identifiant (second appel) où un profil choisi sur le nom et relié à
#   aucun identifiant fourni est mis de côté et proposé à confirmer ;
# - **pagination** OpenAlex (curseur) et Semantic Scholar (décalage) jusqu'au
#   plafond ``max_per_source`` ou à épuisement, plafond signalé ;
# - **Isidore** (plein texte sur le nom) : une notice n'est conservée que si
#   ses créateurs portent le nom cherché (compteur ``rejected_not_author``) ;
# - **statut par source** (``AuthorSourceResult``, sous-classe du
#   ``SourceResult`` du moteur) et rapport d'ensemble
#   (``AuthorSearchReport``) ; aucune promesse de production complète quand
#   une source est plafonnée, indisponible ou ambiguë, quand un profil de
#   nom compatible est écarté, ou quand des notices reposent sur une
#   identité non établie (nom seul, dépôts HAL sans auteur identifié).


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Entier lu dans l'environnement, borné à ``[lo, hi]``."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


# Seuil d'ambiguïté : au moins deux candidats plausibles ayant chacun au
# moins ce nombre de travaux. MOISSON_AUTHOR_AMBIGUITY_MIN_WORKS, borné à
# [1, 5] : un seuil plus bas signale davantage d'ambiguïtés (plus prudent) ;
# au-delà de 5, la règle du plan serait assouplie, ce qui est refusé.
AUTHOR_AMBIGUITY_MIN_WORKS = _env_int(
    "MOISSON_AUTHOR_AMBIGUITY_MIN_WORKS", 5, 1, 5)
# Profils examinés par source lors d'une recherche par nom (une requête) :
# MOISSON_AUTHOR_CANDIDATES, borné à [2, 25].
AUTHOR_CANDIDATES_EXAMINED = _env_int("MOISSON_AUTHOR_CANDIDATES", 10, 2, 25)
# Candidats d'une source ambiguë pour lesquels exemples de titres, période et
# co-auteurs principaux sont demandés (une requête par candidat, les plus
# productifs d'abord) : MOISSON_AUTHOR_CANDIDATE_DETAILS, borné à [0, 10].
AUTHOR_CANDIDATE_DETAILS = _env_int(
    "MOISSON_AUTHOR_CANDIDATE_DETAILS", 3, 0, 10)
# Exemples de titres par candidat détaillé.
AUTHOR_CANDIDATE_SAMPLE = 3
# Tailles de page : OpenAlex /works (maximum de l'API : 200) et Semantic
# Scholar /author/{id}/papers (maximum de l'API : 1000).
# MOISSON_AUTHOR_OPENALEX_PAGE_SIZE et MOISSON_AUTHOR_S2_PAGE_SIZE.
AUTHOR_OPENALEX_PAGE_SIZE = _env_int(
    "MOISSON_AUTHOR_OPENALEX_PAGE_SIZE", 200, 1, 200)
AUTHOR_S2_PAGE_SIZE = _env_int("MOISSON_AUTHOR_S2_PAGE_SIZE", 500, 1, 1000)

# Motifs propres à la recherche d'auteur (``SourceResult.reason``).
REASON_AMBIGUOUS_AUTHOR = "ambiguous_author"      # homonymes : choisir
REASON_AWAITING_IDENTITY = "awaiting_identity"    # notices écartées
REASON_NO_AUTHOR_CANDIDATE = "no_author_candidate"  # aucun nom compatible
REASON_NO_IDENTITY = "no_identity"                # ni nom ni identifiant
# Mode identifiant : profil choisi sur le nom, relié à aucun identifiant
# fourni ; ses notices ne sont pas retenues (profil proposé à confirmer).
REASON_UNCONFIRMED_PROFILE = "unconfirmed_profile"

AUTHOR_REASON_LABELS = {
    **REASON_LABELS,
    REASON_AMBIGUOUS_AUTHOR: "homonymes, identité à choisir",
    REASON_AWAITING_IDENTITY: "notices non retenues, homonymie à lever",
    REASON_NO_AUTHOR_CANDIDATE: "aucun profil d'auteur au nom compatible",
    REASON_NO_IDENTITY: "ni nom ni identifiant propre à cette source",
    REASON_UNCONFIRMED_PROFILE: ("profil choisi sur le nom, relié à aucun "
                                 "identifiant fourni : à confirmer"),
}
# Sources dont les notices ont été mises de côté (requête exécutée).
_HELD_REASONS = frozenset({REASON_AWAITING_IDENTITY, REASON_UNCONFIRMED_PROFILE})

# Méthode d'interrogation d'une source.
METHOD_IDHAL = "idhal"
METHOD_HAL_PERSON = "hal_person_id"
METHOD_ORCID = "orcid"
METHOD_OPENALEX_ID = "openalex_author_id"
METHOD_S2_ID = "s2_author_id"
METHOD_NAME = "name"                 # candidats examinés sur le nom
METHOD_NAME_TEXT = "name_fulltext"   # plein texte sur le nom (Isidore)

# Fondement de l'identité des notices retenues.
BASIS_IDENTIFIER = "identifiant"         # identifiant explicite (ORCID…)
BASIS_ORCID_MATCH = "candidat_orcid"     # profil désigné par l'ORCID fourni
BASIS_SINGLE = "candidat_unique"         # seul candidat plausible productif
BASIS_MAIN = "candidat_principal"        # plus gros de petits profils
BASIS_NAME_ONLY = "nom_seul"             # aucun contrôle d'identité
# Identité établie par un identifiant ; profil choisi sur le nom.
VERIFIED_BASES = frozenset({BASIS_IDENTIFIER, BASIS_ORCID_MATCH})
NAME_CHOICE_BASES = frozenset({BASIS_SINGLE, BASIS_MAIN})

_BASIS_LABELS = {
    BASIS_IDENTIFIER: "identifiant fourni",
    BASIS_ORCID_MATCH: "profil portant l'ORCID fourni",
    BASIS_SINGLE: "seul profil plausible d'au moins {n} travaux",
    BASIS_MAIN: "profil le plus productif (aucun n'atteint {n} travaux)",
    BASIS_NAME_ONLY: "nom seul, identité non vérifiée",
}


# --- Identifiants ---

# ORCID lu dans les réponses des fournisseurs (URL complète, forme nue).
_ORCID_RE = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.IGNORECASE)
# ORCID saisi : forme nue ou URL orcid.org, rien d'autre autour (un chiffre
# en trop n'est jamais tronqué en silence en un autre ORCID).
_ORCID_INPUT_RE = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?orcid\.org/)?(\d{4}-\d{4}-\d{4}-\d{3}[\dX])/?$",
    re.IGNORECASE)
_OPENALEX_AUTHOR_RE = re.compile(
    r"^(?:https?://)?(?:api\.)?(?:openalex\.org/)?(?:authors/)?(A\d+)$",
    re.IGNORECASE)
_S2_AUTHOR_RE = re.compile(r"(?:^|/)(\d+)/?$")
_ID_SPLIT_RE = re.compile(r"[\s,;|]+")


def _split_ids(value: Any) -> List[str]:
    """Liste d'identifiants : liste, ou chaîne séparée par , ; | ou espaces."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        parts = [str(value)]
    out: List[str] = []
    for part in parts:
        out.extend(p for p in _ID_SPLIT_RE.split(part.strip()) if p)
    return out


def orcid_checksum_ok(orcid: str) -> bool:
    """
    Clé de contrôle d'un ORCID (ISO 7064 11,2, dernier caractère, ``X``
    valant 10) : une faute de frappe sur un chiffre est détectée.
    """
    digits = str(orcid or "").replace("-", "").upper()
    if len(digits) != 16 or not digits[:15].isdigit():
        return False
    total = 0
    for char in digits[:15]:
        total = (total + int(char)) * 2
    result = (12 - total % 11) % 11
    return digits[15] == ("X" if result == 10 else str(result))


def normalize_orcid(value: Any, check_digit: bool = True) -> str:
    """
    ORCID normalisé ``0000-0000-0000-000X`` : forme nue ou URL
    ``https://orcid.org/…`` ; rien d'autre autour (un chiffre en trop est
    refusé, jamais tronqué en un autre ORCID).

    Args:
        check_digit: vérifier aussi la clé de contrôle (ISO 7064 11,2).

    Raises:
        ValueError: valeur qui n'a pas la forme d'un ORCID, ou dont la clé
            de contrôle est fausse (``check_digit``).
    """
    match = _ORCID_INPUT_RE.match(str(value or "").strip())
    if not match:
        raise ValueError(f"ORCID invalide : {value!r} (attendu 0000-0000-0000-000X)")
    orcid = match.group(1).upper()
    if check_digit and not orcid_checksum_ok(orcid):
        raise ValueError(f"ORCID invalide : {value!r} (clé de contrôle "
                         f"incorrecte, faute de frappe probable)")
    return orcid


def normalize_openalex_author_ids(value: Any) -> List[str]:
    """
    Identifiants d'auteur OpenAlex normalisés (``A5023888391``), URL
    ``https://openalex.org/A…`` tolérée ; plusieurs identifiants (profils
    fragmentés d'une même personne) séparés par des virgules.

    Raises:
        ValueError: identifiant qui n'a pas la forme ``A`` suivi de chiffres.
    """
    ids: List[str] = []
    for raw in _split_ids(value):
        match = _OPENALEX_AUTHOR_RE.match(raw.strip())
        if not match:
            raise ValueError(
                f"identifiant d'auteur OpenAlex invalide : {raw!r} "
                f"(attendu A suivi de chiffres)")
        ident = match.group(1).upper()
        if ident not in ids:
            ids.append(ident)
    return ids


def normalize_s2_author_ids(value: Any) -> List[str]:
    """
    Identifiants d'auteur Semantic Scholar (chiffres ; URL de profil
    tolérée), plusieurs séparés par des virgules.

    Raises:
        ValueError: identifiant non numérique.
    """
    ids: List[str] = []
    for raw in _split_ids(value):
        match = _S2_AUTHOR_RE.search(raw.strip())
        if not match:
            raise ValueError(
                f"identifiant d'auteur Semantic Scholar invalide : {raw!r} "
                f"(attendu des chiffres)")
        if match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def normalize_hal_person_id(value: Any) -> str:
    """
    Identifiant de personne HAL (``authIdPerson_i``, chiffres).

    Raises:
        ValueError: identifiant non numérique ou nul.
    """
    text = str(value if value is not None else "").strip()
    if not text.isdigit() or int(text) == 0:
        raise ValueError(
            f"identifiant de personne HAL invalide : {value!r} (attendu des chiffres)")
    return str(int(text))


def normalize_author_identifiers(
    orcid: Any = None, openalex_author_id: Any = None,
    s2_author_id: Any = None, hal_person_id: Any = None,
    check_orcid: bool = True,
) -> Dict[str, Any]:
    """
    Valide et normalise les identifiants explicites d'un auteur.

    Args:
        check_orcid: refuser aussi un ORCID dont la clé de contrôle est
            fausse (l'outil MCP le fait ; ``search_by_author`` le signale
            dans son rapport sans lever).

    Returns:
        ``{"orcid", "openalex_author_id", "s2_author_id", "hal_person_id"}``
        (``None`` ou liste vide pour un identifiant absent).

    Raises:
        ValueError: identifiant mal formé (message en français, sans URL).
    """
    return {
        "orcid": (normalize_orcid(orcid, check_digit=check_orcid)
                  if orcid else None),
        "openalex_author_id": (normalize_openalex_author_ids(openalex_author_id)
                               if openalex_author_id else []),
        "s2_author_id": (normalize_s2_author_ids(s2_author_id)
                         if s2_author_id else []),
        "hal_person_id": (normalize_hal_person_id(hal_person_id)
                          if hal_person_id not in (None, "") else None),
    }


# --- Comparaison de noms ---
#
# Règle stricte et explicable, commune au filtre des créateurs Isidore et à
# la plausibilité des profils d'auteur : accents, casse, tirets et points
# neutralisés ; formes « Nom, Prénom » et « Prénom Nom » ; nom de famille
# égal (particules ignorées) ; prénoms égaux, ou compatibles par leurs
# initiales (« J.-P. » pour « Jean-Pierre », « O. » pour « Olivier »), jeton
# à jeton sur toute la longueur. Un prénom de plus d'un côté, complet ou
# abrégé, ne correspond pas (« Jean » / « Jean-Pierre », « Jean » /
# « Jean-P. », « Dominique » / « Dominique M. ») ; un créateur sans prénom ne
# correspond pas (identité invérifiable) ; aucune inversion prénom/nom n'est
# supposée (« Martin Olivier » n'est pas « Olivier Martin ») : une saisie
# peut-être inversée est seulement signalée (``inverted_name_form``).

_NAME_PARTICLES = frozenset({
    "de", "del", "della", "der", "den", "di", "da", "do", "dos", "das",
    "van", "von", "vander", "ter", "ten", "le", "la", "les", "du", "d", "l",
    "el", "al", "bin", "ibn", "abu",
})
_NAME_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _name_tokens(text: Any) -> Tuple[str, ...]:
    """Jetons normalisés d'un nom (sans accents, minuscules, sans ponctuation)."""
    return tuple(_NAME_NON_ALNUM.sub(" ", unidecode(str(text or "")).lower()).split())


def _name_splits(full: Any) -> List[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    """
    Découpages ``(prénoms, nom)`` possibles d'un nom complet : coupure sur la
    virgule pour « Nom, Prénom », sinon toutes les coupures de « Prénom Nom »
    (noms composés, particules : « Jean-Pierre Olivier de Sardan »).
    """
    text = str(full or "").strip().strip(",").strip()
    if text.count(",") == 1:
        last, _, first = text.partition(",")
        family, given = _name_tokens(last), _name_tokens(first)
        if family:
            return [(given, family)]
    tokens = _name_tokens(text)
    if not tokens:
        return []
    if len(tokens) == 1:
        return [((), tokens)]
    return [(tokens[:k], tokens[k:]) for k in range(1, len(tokens))]


def _family_core(tokens: Tuple[str, ...]) -> Tuple[str, ...]:
    core = tuple(t for t in tokens if t not in _NAME_PARTICLES)
    return core or tokens


def _given_compatible(query: Tuple[str, ...], cand: Tuple[str, ...]) -> bool:
    """Prénoms compatibles (voir la règle ci-dessus)."""
    if not query:          # nom cherché sans prénom : le nom de famille suffit
        return True
    if not cand:           # créateur sans prénom : identité invérifiable
        return False
    if len(query) != len(cand):
        # Un prénom de plus d'un côté (complet ou initiale) : autre personne
        # possible (« Jean » / « Jean-P. » comme « Jean » / « Jean-Pierre »).
        return False
    for a, b in zip(query, cand):
        if a == b:
            continue
        if len(a) == 1 and b.startswith(a):
            continue
        if len(b) == 1 and a.startswith(b):
            continue
        return False
    return True


def names_match(query: str, first: Any = "", last: Any = "",
                full: Any = "") -> bool:
    """
    Vrai si le nom d'une personne (``first``/``last`` structurés, ou nom
    complet ``full``) correspond au nom cherché ``query``.
    """
    query_splits = _name_splits(query)
    if not query_splits:
        return False
    if first or last:
        cand_splits = [(_name_tokens(first), _name_tokens(last))]
        # Nom complet rangé dans un seul champ (prénom vide) : relu.
        if not _name_tokens(first) and len(_name_tokens(last)) > 1:
            cand_splits += _name_splits(last)
        elif not _name_tokens(last) and len(_name_tokens(first)) > 1:
            cand_splits += _name_splits(first)
    else:
        cand_splits = _name_splits(full)
    for q_given, q_family in query_splits:
        for c_given, c_family in cand_splits:
            if (q_family and c_family
                    and _family_core(q_family) == _family_core(c_family)
                    and _given_compatible(q_given, c_given)):
                return True
    return False


def creators_contain(item: Dict, name: str) -> bool:
    """Vrai si l'un des créateurs de la notice (tous rôles) porte ``name``."""
    for creator in item.get("creators") or []:
        if not isinstance(creator, dict):
            continue
        if names_match(name, creator.get("firstName") or "",
                       creator.get("lastName") or "",
                       creator.get("name") or ""):
            return True
    return False


def _inverted_forms(name: str) -> List[str]:
    """
    Formes « Prénom Nom » d'un nom peut-être saisi « NOM Prénom » (sans
    virgule) : « Cardon Dominique » → « Dominique Cardon ». Servent
    seulement à signaler une saisie peut-être inversée, jamais à accepter
    une notice ou un profil (aucune inversion n'est supposée).
    """
    text = str(name or "").strip()
    # Nom en capitales (« CARDON ») rendu en casse normale : la forme
    # proposée doit être directement réutilisable (HAL compare le nom
    # complet exact, casse comprise).
    words = ["-".join(p.capitalize() for p in w.split("-"))
             if w.isupper() and len(w) > 1 else w for w in text.split()]
    if "," in text or len(words) < 2:
        return []
    return [" ".join(words[k:] + words[:k]) for k in range(1, len(words))]


def inverted_name_form(name: str, first: Any = "", last: Any = "",
                       full: Any = "") -> Optional[str]:
    """
    Forme « Prénom Nom » du nom cherché qui correspond à la personne, quand
    le nom cherché lui-même ne lui correspond pas (saisie peut-être
    inversée), sinon ``None``.
    """
    if names_match(name, first, last, full):
        return None
    for form in _inverted_forms(name):
        if names_match(form, first, last, full):
            return form
    return None


def _creators_inverted_form(item: Dict, name: str) -> Optional[str]:
    """Forme inversée du nom portée par l'un des créateurs de la notice."""
    for creator in item.get("creators") or []:
        if not isinstance(creator, dict):
            continue
        form = inverted_name_form(name, creator.get("firstName") or "",
                                  creator.get("lastName") or "",
                                  creator.get("name") or "")
        if form:
            return form
    return None


# --- Résultats ---

@dataclass
class AuthorSourceResult(SourceResult):
    """
    Notices et statut d'une source pour une recherche d'auteur.

    Statuts et motifs du moteur (``engine.models``), plus :

    Attributes:
        method: interrogation (identifiant, ``name``, ``name_fulltext``).
        query: requête ou filtre envoyé (jamais de clé d'API).
        identity: profil retenu (identifiant, nom, affiliation, travaux).
        identity_verified: identité établie par un identifiant explicite.
        identity_basis: fondement de l'identité (``BASIS_*``).
        candidates: profils examinés (ambiguïté : ceux entre lesquels
            choisir ; sinon le profil retenu, ``selected``, et les autres).
        available: total annoncé par le fournisseur quand il est connu.
        excluded: profils (ou personnes HAL) de nom compatible écartés au
            profit du profil retenu : fragments de la même personne ou
            homonymes, travaux possiblement manquants.
        unseparated: mode identifiant, notices retenues sur le nom seul
            (Isidore, HAL sans contrôle) : non séparées d'éventuels
            homonymes.
        name_hint: forme « Prénom Nom » à laquelle correspondent des
            profils ou notices refusés quand rien ne correspond au nom
            saisi (saisie peut-être inversée « NOM Prénom »).
    """

    method: Optional[str] = None
    query: Optional[str] = None
    identity: Optional[Dict[str, Any]] = None
    identity_verified: bool = False
    identity_basis: Optional[str] = None
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    available: Optional[int] = None
    excluded: List[Dict[str, Any]] = field(default_factory=list)
    unseparated: bool = False
    name_hint: Optional[str] = None

    @property
    def ambiguous(self) -> bool:
        return self.reason == REASON_AMBIGUOUS_AUTHOR

    @property
    def held(self) -> bool:
        """Requête exécutée, notices mises de côté (identité à établir)."""
        return self.reason in _HELD_REASONS

    @property
    def rejected_not_author(self) -> int:
        return int(self.counters.get("rejected_not_author", 0) or 0)

    @property
    def unlinked_included(self) -> int:
        """HAL : dépôts sans auteur HAL identifié inclus avec le profil."""
        return int(self.counters.get("unlinked_included", 0) or 0)

    @property
    def identity_established(self) -> bool:
        """
        Identité des notices établie : identifiant (ou ORCID porté par le
        profil), ou seul profil de nom compatible, sans autre profil
        compatible écarté ni dépôt d'auteur non identifié inclus. Faux pour
        le nom seul (Isidore) et pour un fondement inconnu.
        """
        if self.identity_basis in VERIFIED_BASES:
            return True
        return (self.identity_basis in NAME_CHOICE_BASES
                and not self.excluded and not self.unlinked_included)

    def set_status(self, status: str, reason: Optional[str] = None,
                   detail: Optional[str] = None) -> None:
        self.status, self.reason, self.detail = status, reason, detail

    def warn(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)

    def describe(self) -> str:
        """Statut, motif et plafond (« liste incomplète » s'il est atteint)."""
        bits = []
        if self.limit_reached:
            cap = "plafond atteint"
            if self.available and self.available > self.item_count:
                cap += f" ({self.available} disponibles)"
            bits.append(cap + ", liste incomplète")
        elif self.exhausted and self.status in SUCCESS_STATUSES:
            bits.append("résultats épuisés")
        if self.reason:
            label = AUTHOR_REASON_LABELS.get(self.reason, self.reason)
            if self.detail and self.detail not in label:
                label = f"{label} : {self.detail}"
            bits.append(label)
        # Notices mises de côté : la requête a bien été exécutée.
        text = ("mise de côté" if self.held
                else STATUS_LABELS.get(self.status, self.status))
        return f"{text} ({' ; '.join(bits)})" if bits else text

    def line(self) -> str:
        """Ligne du résumé : notices, statut, identité, rejets."""
        extras = []
        if self.identity_basis == BASIS_IDENTIFIER:
            arg = (self.identity or {}).get("argument") or {}
            extras.append("par identifiant (" + ", ".join(
                f"{k}={v}" for k, v in arg.items()) + ")")
        elif self.identity and not self.ambiguous:
            basis = _BASIS_LABELS.get(self.identity_basis or "", "")
            verb = {REASON_AWAITING_IDENTITY: "profil identifié",
                    REASON_UNCONFIRMED_PROFILE: "profil trouvé par le nom",
                    }.get(self.reason or "", "profil retenu")
            extras.append(f"{verb} : " + candidate_label(self.identity)
                          + (f", {basis.format(n=AUTHOR_AMBIGUITY_MIN_WORKS)}"
                             if basis else ""))
        if self.excluded:
            extras.append(f"{len(self.excluded)} autre(s) profil(s) de nom "
                          f"compatible non inclus")
        if self.unlinked_included and self.item_count:
            extras.append(f"{self.unlinked_included} dépôt(s) sans auteur HAL "
                          f"identifié inclus (identité non vérifiée)")
        if self.rejected_not_author:
            extras.append(f"{self.rejected_not_author} notice(s) écartée(s), "
                          f"nom absent des créateurs")
        if self.name_hint:
            extras.append(f"nom peut-être inversé : "
                          f"{int(self.counters.get('inverted_name_matches') or 0)}"
                          f" correspondance(s) à « {self.name_hint} »")
        if self.identity_basis == BASIS_NAME_ONLY and self.item_count:
            extras.append("non séparée des homonymes (nom seul)"
                          if self.unseparated else
                          "identité non vérifiée (nom seul)")
        text = f"{self.display_name} : {self.item_count} — {self.describe()}"
        return text + (" ; " + " ; ".join(extras) if extras else "")

    def reason_text(self) -> str:
        """Motif lisible et détail (« erreur HTTP : HTTP 503 »)."""
        label = AUTHOR_REASON_LABELS.get(self.reason or "", self.reason or "")
        if self.detail and self.detail not in label:
            label = f"{label} : {self.detail}" if label else self.detail
        return label or STATUS_LABELS.get(self.status, self.status)

    def as_dict(self) -> Dict[str, Any]:
        data = super().as_dict()
        data.update({
            "method": self.method,
            "query": self.query,
            "identity": self.identity,
            "identity_verified": self.identity_verified,
            "identity_basis": self.identity_basis,
            "available": self.available,
            "rejected_not_author": self.rejected_not_author,
            "candidates": [dict(c) for c in self.candidates],
            "excluded_profiles": [dict(c) for c in self.excluded],
            "identity_established": self.identity_established,
            "unseparated": self.unseparated,
            "name_hint": self.name_hint,
        })
        return data


def candidate_label(cand: Dict[str, Any]) -> str:
    """« Nom (source id, affiliation, N travaux, période, ORCID) »."""
    bits = [f"{cand.get('source', '')} {cand.get('id', '')}".strip()]
    if cand.get("idhal"):
        bits.append(f"idHal {cand['idhal']}")
    if cand.get("affiliation"):
        bits.append(str(cand["affiliation"]))
    if cand.get("works_count") is not None:
        bits.append(f"{cand['works_count']} travaux")
    if cand.get("period"):
        bits.append(str(cand["period"]))
    if cand.get("orcid"):
        bits.append(f"ORCID {cand['orcid']}")
    return f"{cand.get('name') or '?'} ({', '.join(b for b in bits if b)})"


def _argument_text(argument: Optional[Dict[str, Any]]) -> str:
    """Argument à repasser à l'outil : ``s2_author_id="87578754"``."""
    return ", ".join(f'{k}="{v}"' for k, v in (argument or {}).items())


def _profiles_text(profiles: List[Dict[str, Any]], limit: int = 5) -> str:
    """« id : N travaux, … » des premiers profils d'une liste."""
    parts = []
    for cand in profiles[:limit]:
        works = cand.get("works_count")
        parts.append(f"{cand.get('id')}" + (f" : {works} travaux"
                                            if works is not None else ""))
    return ", ".join(parts) + (" …" if len(profiles) > limit else "")


class AuthorItems(list):
    """
    Notices d'une recherche d'auteur (liste ordinaire, compatible avec les
    appelants historiques) portant le rapport de la recherche (``report``).
    """

    report: Optional["AuthorSearchReport"] = None

    def __init__(self, items=(), report: Optional["AuthorSearchReport"] = None):
        super().__init__(items)
        self.report = report


@dataclass
class AuthorSearchReport:
    """Rapport d'une recherche d'auteur (hors JSON Zotero, invariant 1)."""

    name: str
    identifiers: Dict[str, Any]
    sources: List[AuthorSourceResult]
    total_before_dedup: int = 0
    total_unique: int = 0
    duration_s: float = 0.0
    # Réserves propres à l'appel (ORCID à clé de contrôle fausse…) : en
    # tête des réserves, elles interdisent toute promesse de complétude.
    notes: List[str] = field(default_factory=list)

    @property
    def identifier_mode(self) -> bool:
        """Au moins un identifiant explicite fourni."""
        return any(bool(v) for v in self.identifiers.values())

    @property
    def ambiguous_sources(self) -> List[AuthorSourceResult]:
        return [r for r in self.sources if r.ambiguous]

    @property
    def ambiguous(self) -> bool:
        """Recherche par nom seul dont une source a établi une homonymie :
        rien n'est retenu, l'identité doit être choisie."""
        return bool(self.ambiguous_sources) and not self.identifier_mode

    @property
    def candidates(self) -> List[Dict[str, Any]]:
        """Candidats des sources ambiguës, entre lesquels choisir."""
        out: List[Dict[str, Any]] = []
        for r in self.ambiguous_sources:
            out.extend(dict(c) for c in r.candidates)
        return out

    @property
    def profiles_to_confirm(self) -> List[Dict[str, Any]]:
        """
        Profils choisis sur le nom par des sources non ambiguës dont les
        notices ont été mises de côté (homonymie établie ailleurs, ou mode
        identifiant sans lien avec les identifiants fournis) : à ajouter au
        second appel, par leur argument, s'il s'agit de la personne choisie.
        """
        out: List[Dict[str, Any]] = []
        for r in self.sources:
            if (r.held and r.identity
                    and r.identity_basis in NAME_CHOICE_BASES):
                entry = dict(r.identity, source_status=r.reason)
                if r.excluded:
                    entry["other_compatible_profiles"] = [
                        dict(c) for c in r.excluded]
                out.append(entry)
        return out

    @property
    def all_failed(self) -> bool:
        """Échec total : aucune notice, aucune source exécutée sans panne, et
        aucune homonymie à lever (une ambiguïté, des notices mises de côté
        pour identité à établir, ne sont pas des pannes)."""
        if (self.total_unique or not self.sources or self.ambiguous_sources
                or any(r.held for r in self.sources)):
            return False
        return not any(r.executed for r in self.sources)

    @property
    def overall_status(self) -> str:
        """``error``, ``ambiguous``, ``partial`` (au moins une source
        incomplète ou non exécutée) ou ``ok``."""
        if self.all_failed:
            return "error"
        if self.ambiguous:
            return "ambiguous"
        return "partial" if any(not r.succeeded for r in self.sources) else "ok"

    @property
    def production_complete(self) -> bool:
        """
        Toutes les sources parcourues jusqu'au bout, sans plafond, sans
        panne, sans ambiguïté ni source ignorée ou mise de côté, sans
        réserve propre à l'appel, sans profil de nom compatible écarté (un
        auteur fragmenté perdrait des travaux) ni nom peut-être inversé, et
        identité établie pour toutes les notices retenues (identifiant, ou
        seul profil compatible sans dépôt d'auteur non identifié) : les
        notices retenues sur le nom seul (Isidore) l'interdisent toujours.
        """
        if self.overall_status != "ok" or self.notes:
            return False
        for r in self.sources:
            if r.limit_reached or r.provider_window or r.excluded or r.name_hint:
                return False
            if r.item_count and not r.identity_established:
                return False
        return True

    def status_lines(self, indent: str = "  - ") -> List[str]:
        return [indent + r.line() for r in self.sources]

    def confirm_lines(self, indent: str = "  ") -> List[str]:
        """Profils à confirmer (voir ``profiles_to_confirm``), avec
        l'argument à ajouter et les autres profils compatibles."""
        lines: List[str] = []
        for r in self.sources:
            if not (r.held and r.identity
                    and r.identity_basis in NAME_CHOICE_BASES):
                continue
            arg = _argument_text(r.identity.get("argument"))
            lines.append(f"{indent}- {r.display_name} : "
                         f"{candidate_label(r.identity)}"
                         + (f" ; argument : {arg}" if arg else ""))
            if r.excluded:
                lines.append(f"{indent}    autres profils de nom compatible : "
                             + _profiles_text(r.excluded))
        return lines

    def candidate_lines(self, indent: str = "  ") -> List[str]:
        """Candidats des sources ambiguës, avec l'argument à repasser."""
        lines: List[str] = []
        for r in self.ambiguous_sources:
            lines.append(f"{indent}{r.display_name} ({r.detail}) :")
            for cand in r.candidates:
                arg = _argument_text(cand.get("argument"))
                lines.append(f"{indent}  - {candidate_label(cand)}"
                             + (f" ; argument : {arg}" if arg else ""))
                if cand.get("sample_titles"):
                    lines.append(f"{indent}      ex. : " + " ; ".join(
                        str(t)[:90] for t in cand["sample_titles"]))
                if cand.get("top_coauthors"):
                    lines.append(f"{indent}      co-auteurs : "
                                 + ", ".join(cand["top_coauthors"]))
        return lines

    def caveats(self) -> List[str]:
        """Raisons pour lesquelles la production peut être incomplète ou
        mêlée d'homonymes (réserves de l'appel, puis une phrase par source
        et par motif)."""
        out: List[str] = list(self.notes)
        hints = [r for r in self.sources if r.name_hint]
        if hints:
            form = hints[0].name_hint
            where = ", ".join(
                f"{r.display_name} ({int(r.counters.get('inverted_name_matches') or 0)})"
                for r in hints)
            out.append(f"Nom peut-être saisi « NOM Prénom » : rien ne "
                       f"correspond à « {self.name} », mais des profils ou "
                       f"notices correspondent à « {form} » ({where}) ; "
                       f"relancer avec name=\"{form}\" (aucune inversion "
                       f"n'est supposée)")
        for r in self.sources:
            if r.ambiguous:
                orcid_note = next(("aucun profil examiné ne porte l'ORCID "
                                   "fourni" if "aucun profil" in w else
                                   "plusieurs profils portent l'ORCID fourni"
                                   for w in r.warnings if "ORCID" in w), None)
                out.append(f"{r.display_name} : homonymes ({r.detail}), "
                           f"identité à choisir"
                           + (f" ; {orcid_note}" if orcid_note else ""))
            elif r.reason == REASON_UNCONFIRMED_PROFILE:
                arg = _argument_text((r.identity or {}).get("argument"))
                label = candidate_label(r.identity) if r.identity else "?"
                out.append(f"{r.display_name} : profil {label} choisi sur le "
                           f"nom, relié à aucun identifiant fourni ({r.detail})"
                           f" ; ajouter " + (arg or "son identifiant")
                           + " s'il s'agit de la même personne")
            elif r.reason == REASON_AWAITING_IDENTITY:
                out.append(f"{r.display_name} : {r.detail}")
            elif r.limit_reached:
                # Isidore : le plafond porte sur les notices reçues, avant le
                # filtre des créateurs.
                received = int(r.counters.get("received") or 0)
                count = received or r.item_count
                total = (f" sur {r.available}"
                         if r.available and r.available > count else "")
                kept = (f", {r.item_count} retenues" if received else "")
                out.append(f"{r.display_name} : plafond de {count} notices"
                           f"{' reçues' if received else ''} atteint{total}"
                           f"{kept}, d'autres travaux peuvent exister")
            elif r.status == STATUS_PARTIAL:
                out.append(f"{r.display_name} : partielle ({r.reason_text()})")
            elif r.status in (STATUS_ERROR, STATUS_CANCELLED):
                # Isidore couvre OpenEdition, Cairn, Persée, Érudit et
                # Theses.fr dans la recherche d'auteur (PAT-16).
                covered = (" ; OpenEdition, Cairn, Persée, Érudit et "
                           "Theses.fr, lus par Isidore, manquent aussi"
                           if r.source == "isidore" else "")
                out.append(f"{r.display_name} : indisponible "
                           f"({r.reason_text()}){covered}")
            elif r.status == STATUS_SKIPPED:
                out.append(f"{r.display_name} : non interrogée ({r.reason_text()})")
            if r.excluded and not r.held:
                if r.source == "hal":
                    out.append(
                        f"{r.display_name} : {len(r.excluded)} autre(s) "
                        f"personne(s) HAL de même nom exclue(s) "
                        f"({_profiles_text(r.excluded)}) : doublons du "
                        f"profil retenu (dépôts manquants) ou homonymes")
                else:
                    out.append(
                        f"{r.display_name} : profil retenu sur le nom ; "
                        f"{len(r.excluded)} autre(s) profil(s) de nom "
                        f"compatible non inclus ({_profiles_text(r.excluded)})"
                        f" : fragments du même auteur (travaux manquants) ou "
                        f"homonymes ; passer leurs identifiants pour les "
                        f"inclure")
            if r.item_count and r.unlinked_included:
                out.append(f"{r.display_name} : {r.unlinked_included} dépôt(s) "
                           f"sans auteur HAL identifié inclus (identité non "
                           f"vérifiée, homonymes possibles)")
            if r.identity_basis == BASIS_NAME_ONLY and r.item_count:
                out.append(
                    f"{r.display_name} : notices retenues sur le nom seul, "
                    f"non séparées d'éventuels homonymes malgré les "
                    f"identifiants fournis (aucun identifiant d'auteur "
                    f"exploitable dans cette source) : à vérifier"
                    if r.unseparated else
                    f"{r.display_name} : identité non vérifiée "
                    f"(notices retenues sur le nom seul)")
            if (r.source == "hal" and r.method == METHOD_ORCID
                    and r.status == STATUS_EMPTY):
                # Vérifié le 2026-09-29 : l'ORCID est rarement renseigné dans
                # les dépôts HAL (0 dépôt pour un auteur qui en compte 226).
                out.append("HAL : aucun dépôt ne porte cet ORCID (souvent non "
                           "renseigné dans HAL) ; préciser idhal ou "
                           "hal_person_id si l'auteur dépose dans HAL")
        return out

    def as_dict(self) -> Dict[str, Any]:
        """Champs de ``structuredContent`` (statuts, candidats, réserves)."""
        return {
            "harvest_status": self.overall_status,
            "production_complete": self.production_complete,
            "identifier_mode": self.identifier_mode,
            "identifiers": dict(self.identifiers),
            "by_source_status": {r.display_name: r.as_dict()
                                 for r in self.sources},
            "candidates": self.candidates,
            "profiles_to_confirm": self.profiles_to_confirm,
            "caveats": self.caveats(),
            "total_before_dedup": self.total_before_dedup,
            "total_unique": self.total_unique,
            "duration_seconds": round(self.duration_s, 1),
        }


# --- Outils communs ---

def _check_cancel(client: Any) -> None:
    """Annulation vérifiée entre deux pages (signal porté par le client)."""
    signal = getattr(client, "cancel_signal", None)
    if signal is not None:
        signal.check()


def _outcome_failure(state: str, detail: str) -> Tuple[str, str]:
    """Motif et détail d'un accès en échec (``_get_json_outcome``)."""
    if state == LOOKUP_NOT_FOUND:
        return REASON_HTTP_ERROR, "HTTP 404 (identifiant inconnu de la source)"
    if detail.startswith("HTTP"):
        return REASON_HTTP_ERROR, detail
    if detail == "réponse non JSON":
        return REASON_INVALID_BODY, detail
    return REASON_NETWORK_ERROR, detail or "erreur réseau"


def _apply_report(res: AuthorSourceResult,
                  report: Optional[SearchReport]) -> None:
    """Statut final d'une source d'après son compte rendu de collecte."""
    status, reason, detail = resolve_source_status(report, res.item_count)
    res.set_status(status, reason, detail)
    if report is not None:
        res.pages = report.pages
        res.limit_reached = report.limit_reached
        res.exhausted = report.exhausted
        res.provider_window = report.provider_window
        for key, value in (report.counters or {}).items():
            # Compteurs numériques seulement : un total inconnu (None) du flux
            # Isidore faisait lever TypeError, et la panne réseau devenait
            # « erreur inattendue » (PAT-05, retour d'expérience B5).
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            res.counters[key] = res.counters.get(key, 0) + value
        for message in report.warnings:
            res.warn(message)
    # Plafond atteint pile au total annoncé : fournisseur épuisé (un total
    # inférieur aux notices reçues est incohérent : le plafond reste signalé).
    if (res.limit_reached and res.available is not None
            and res.available == res.item_count):
        res.limit_reached = False
        res.exhausted = True


def _years_span(years: List[Any]) -> Optional[str]:
    values = sorted({int(y) for y in years if str(y).isdigit()})
    if not values:
        return None
    return str(values[0]) if values[0] == values[-1] else f"{values[0]}–{values[-1]}"


def _works(cand: Dict[str, Any]) -> int:
    try:
        return int(cand.get("works_count") or 0)
    except (TypeError, ValueError):
        return 0


def _candidate(source: str, ident: str, name: str, works: Any, *,
               plausible: bool, argument: Dict[str, str],
               **extra: Any) -> Dict[str, Any]:
    cand = {"source": source, "id": ident, "name": name,
            "works_count": works if isinstance(works, int) else None,
            "plausible": bool(plausible), "argument": dict(argument)}
    cand.update({k: v for k, v in extra.items() if v not in (None, "", [])})
    return cand


def _top_coauthors(names: List[str], limit: int = 3) -> List[str]:
    counts = Counter(n for n in names if n)
    return [n for n, _ in counts.most_common(limit)]


def _decide(res: AuthorSourceResult, cands: List[Dict[str, Any]],
            details: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
            orcid: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Choix d'un profil parmi les candidats examinés sur le nom.

    - un profil (plausible ou non) porte l'ORCID fourni, et lui seul : retenu ;
    - au moins deux profils plausibles d'au moins
      ``AUTHOR_AMBIGUITY_MIN_WORKS`` travaux : ambiguïté (source ``skipped``,
      motif ``ambiguous_author``, candidats détaillés), aucun retenu ;
    - un seul profil plausible productif : retenu ; sinon le plus productif
      des profils plausibles ; aucun profil plausible : ``empty``.

    Returns:
        Le candidat retenu, ou ``None`` (ambiguïté ou aucun candidat).
    """
    plausible = sorted((c for c in cands if c.get("plausible")),
                       key=_works, reverse=True)
    if orcid:
        matches = [c for c in cands if c.get("orcid") == orcid]
        if len(matches) == 1:
            chosen = matches[0]
            res.identity_basis = BASIS_ORCID_MATCH
            res.identity_verified = True
            return _retain(res, chosen, plausible)
        res.warn(f"aucun profil examiné ne porte l'ORCID {orcid} "
                 f"(choix sur le nom)" if not matches else
                 f"plusieurs profils portent l'ORCID {orcid} (choix sur le nom)")
    big = [c for c in plausible if _works(c) >= AUTHOR_AMBIGUITY_MIN_WORKS]
    if len(big) >= 2:
        if details is not None and AUTHOR_CANDIDATE_DETAILS:
            details(big[:AUTHOR_CANDIDATE_DETAILS])
        res.set_status(
            STATUS_SKIPPED, REASON_AMBIGUOUS_AUTHOR,
            f"{len(big)} profils homonymes d'au moins "
            f"{AUTHOR_AMBIGUITY_MIN_WORKS} travaux")
        res.candidates = plausible[:AUTHOR_CANDIDATES_EXAMINED]
        return None
    if not plausible:
        inverted = [c for c in cands if c.get("inverted_name")]
        detail = f"{len(cands)} profil(s) examiné(s)"
        if inverted:
            # Saisie peut-être « NOM Prénom » : signalée, jamais supposée.
            res.name_hint = inverted[0]["inverted_name"]
            res.counters["inverted_name_matches"] = len(inverted)
            detail += (f", dont {len(inverted)} au nom « {res.name_hint} » "
                       f"(nom peut-être saisi inversé)")
        res.set_status(STATUS_EMPTY, REASON_NO_AUTHOR_CANDIDATE, detail)
        return None
    res.identity_basis = BASIS_SINGLE if big else BASIS_MAIN
    return _retain(res, big[0] if big else plausible[0], plausible)


def _retain(res: AuthorSourceResult, chosen: Dict[str, Any],
            plausible: List[Dict[str, Any]],
            warn_others: bool = True) -> Dict[str, Any]:
    """Profil retenu : nommé, les autres profils plausibles signalés et
    consignés (``excluded`` : travaux possiblement manquants)."""
    chosen = dict(chosen, selected=True)
    others = [dict(c, selected=False) for c in plausible
              if c.get("id") != chosen.get("id")]
    res.identity = {k: v for k, v in chosen.items()
                    if k not in ("plausible", "selected")}
    res.candidates = [chosen] + others[:AUTHOR_CANDIDATES_EXAMINED - 1]
    res.excluded = [{k: c[k] for k in ("source", "id", "name", "works_count",
                                       "argument") if c.get(k) is not None}
                    for c in others]
    if others and warn_others:
        res.warn(f"{len(others)} autre(s) profil(s) de nom compatible non "
                 f"inclus : " + ", ".join(str(c.get("id")) for c in others[:5])
                 + (" …" if len(others) > 5 else ""))
    return chosen


def _coerce_source_result(value: Any, key: str,
                          display: str) -> AuthorSourceResult:
    """Résultat d'une source ; une liste (ou ``None``) rendue par une
    fonction remplacée (doublure, extension) devient ``complete``/``empty``."""
    if isinstance(value, AuthorSourceResult):
        return value
    res = AuthorSourceResult(source=key, display_name=display)
    res.items = list(value or [])
    res.set_status(STATUS_COMPLETE if res.items else STATUS_EMPTY)
    return res


def _guarded(key: str, display: str,
             fn: Callable[[], Any]) -> Callable[[], AuthorSourceResult]:
    """Une source qui lève devient ``error`` (motif classé, sans URL) ;
    l'annulation (``OperationCancelled``) remonte."""
    def run() -> AuthorSourceResult:
        try:
            return _coerce_source_result(fn(), key, display)
        except Exception as e:  # noqa: BLE001 - une source n'arrête pas les autres
            logger.warning(f"[search_by_author] {display} : {type(e).__name__}")
            res = AuthorSourceResult(source=key, display_name=display)
            reason, detail = classify_exception(e)
            res.set_status(STATUS_ERROR, reason, detail)
            return res
    return run


# --- HAL ---

_HAL_PERSON_FACET = "authFullNamePersonIDIDHal_fs"
_FACET_SEP = "_FacetSep_"


def _solr_phrase(text: str) -> str:
    """Contenu d'une phrase Solr entre guillemets (antislash et guillemet
    échappés)."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _build_hal_author_query(
    name: str, idhal: Optional[str], orcid: Optional[str],
    hal_person_id: Optional[str] = None,
) -> str:
    """
    Requête Solr HAL la plus précise possible : idHal, identifiant de
    personne HAL, ORCID (champ ``authORCIDIdExt_s``, vérifié en ligne le
    2026-09-29 ; l'ancien ``authOrcidId_s`` n'existe pas et ne trouvait rien),
    sinon nom complet exact (``authFullName_s``).
    """
    if idhal:
        return f'authIdHal_s:"{_solr_phrase(idhal)}"'
    if hal_person_id:
        return f"authIdPerson_i:{hal_person_id}"
    if orcid:
        return f'authORCIDIdExt_s:"{_solr_phrase(orcid)}"'
    if name:
        return f'authFullName_s:"{_solr_phrase(name)}"'
    return ""


def _hal_candidates(client: Any, name: str):
    """
    Auteurs HAL identifiés sous le nom exact ``name`` (facette
    ``authFullNamePersonIDIDHal_fs`` : « Nom_FacetSep_personne_FacetSep_idHal »).

    Returns:
        ``(candidats, total, non_rattachés, état, détail)`` : ``total`` est
        le nombre de dépôts sous ce nom, ``non_rattachés`` ceux dont l'auteur
        n'est rattaché à aucune personne HAL (identité inconnue).
    """
    params = {
        "q": _build_hal_author_query(name, None, None),
        "rows": 0, "wt": "json", "facet": "true",
        "facet.field": _HAL_PERSON_FACET,
        "facet.prefix": f"{name}{_FACET_SEP}",
        "facet.mincount": 1, "facet.limit": 100,
    }
    payload, state, detail = _get_json_outcome(
        client, HAL_SEARCH_URL, "HAL candidats auteur", params=params)
    if state != "ok":
        return None, 0, 0, state, detail
    try:
        total = int((payload.get("response") or {}).get("numFound") or 0)
        values = ((payload.get("facet_counts") or {}).get("facet_fields")
                  or {}).get(_HAL_PERSON_FACET) or []
    except AttributeError:
        return None, 0, 0, LOOKUP_ERROR, "réponse non JSON"
    by_person: Dict[str, Dict[str, Any]] = {}
    unlinked = 0
    for value, count in zip(values[::2], values[1::2]):
        parts = str(value).split(_FACET_SEP)
        if len(parts) < 2:
            continue
        person = parts[1].strip()
        idhal = parts[2].strip() if len(parts) > 2 else ""
        count = int(count or 0)
        if not person.isdigit() or int(person) == 0:
            unlinked += count
            continue
        if person in by_person:
            by_person[person]["works_count"] += count
            continue
        by_person[person] = _candidate(
            "HAL", person, parts[0], count, plausible=True,
            argument={"idhal": idhal} if idhal else {"hal_person_id": person},
            idhal=idhal or None)
    cands = sorted(by_person.values(), key=_works, reverse=True)
    return cands, total, unlinked, "ok", ""


def _hal_candidate_details(cands: List[Dict[str, Any]], client: Any,
                           name: str) -> None:
    """Exemples de titres, période et co-auteurs de candidats HAL (une
    requête chacun ; un échec laisse le candidat sans détail)."""
    for cand in cands:
        params = {
            "q": f"authIdPerson_i:{cand['id']}", "rows": AUTHOR_CANDIDATE_SAMPLE,
            "wt": "json", "fl": "title_s,producedDateY_i,authFullName_s",
            "sort": "producedDateY_i desc", "stats": "true",
            "stats.field": "producedDateY_i",
        }
        payload, state, _ = _get_json_outcome(
            client, HAL_SEARCH_URL, "HAL détail candidat", params=params)
        if state != "ok":
            continue
        response = payload.get("response") or {}
        docs = response.get("docs") or []
        cand["sample_titles"] = [
            f"{(d.get('title_s') or ['?'])[0]} ({d.get('producedDateY_i', '?')})"
            for d in docs]
        stats = (((payload.get("stats") or {}).get("stats_fields") or {})
                 .get("producedDateY_i") or {})
        span = _years_span([int(stats[k]) for k in ("min", "max")
                            if isinstance(stats.get(k), (int, float))])
        if span:
            cand["period"] = span
        if isinstance(response.get("numFound"), int):
            cand["works_all_forms"] = response["numFound"]
        cand["top_coauthors"] = _top_coauthors([
            n for d in docs for n in (d.get("authFullName_s") or [])
            if not names_match(name, full=n)])


# --- OpenAlex ---

_OPENALEX_AUTHORS_URL = "https://api.openalex.org/authors"
_OPENALEX_AUTHOR_SELECT = (
    "id,display_name,display_name_alternatives,orcid,works_count,"
    "cited_by_count,last_known_institutions,counts_by_year"
)


def _openalex_short_id(value: Any) -> str:
    match = re.search(r"(A\d+)$", str(value or ""), re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _orcid_tail(value: Any) -> Optional[str]:
    match = _ORCID_RE.search(str(value or ""))
    return match.group(1).upper() if match else None


def _openalex_candidates(name: str, client: Any, base: Dict[str, str]):
    """Profils OpenAlex d'un nom (``/authors?search=``) : ``(candidats,
    état, détail)``."""
    payload, state, detail = _get_json_outcome(
        client, _OPENALEX_AUTHORS_URL, "OpenAlex candidats auteur",
        params={**base, "search": name, "per-page": AUTHOR_CANDIDATES_EXAMINED,
                "select": _OPENALEX_AUTHOR_SELECT})
    if state != "ok":
        return None, state, detail
    cands = []
    for author in (payload or {}).get("results") or []:
        ident = _openalex_short_id(author.get("id"))
        if not ident:
            continue
        names = [author.get("display_name")] + list(
            author.get("display_name_alternatives") or [])
        insts = [i.get("display_name") for i in
                 author.get("last_known_institutions") or []
                 if isinstance(i, dict) and i.get("display_name")]
        years = [c.get("year") for c in author.get("counts_by_year") or []
                 if isinstance(c, dict) and (c.get("works_count") or 0) > 0]
        plausible = any(names_match(name, full=n) for n in names if n)
        inverted = None if plausible else next(
            (f for n in names if n
             for f in [inverted_name_form(name, full=n)] if f), None)
        cands.append(_candidate(
            "OpenAlex", ident, author.get("display_name") or "",
            author.get("works_count"),
            plausible=plausible, inverted_name=inverted,
            argument={"openalex_author_id": ident},
            affiliation="; ".join(insts[:2]) or None,
            orcid=_orcid_tail(author.get("orcid")),
            cited_by_count=author.get("cited_by_count"),
            period=_years_span(years)))
    return cands, "ok", ""


def _openalex_candidate_details(cands: List[Dict[str, Any]], client: Any,
                                base: Dict[str, str]) -> None:
    """Travaux les plus cités et co-auteurs de candidats OpenAlex."""
    for cand in cands:
        payload, state, _ = _get_json_outcome(
            client, OPENALEX_API, "OpenAlex détail candidat",
            params={**base, "filter": f"author.id:{cand['id']}",
                    "per-page": AUTHOR_CANDIDATE_SAMPLE,
                    "sort": "cited_by_count:desc",
                    "select": "title,publication_year,authorships"})
        if state != "ok":
            continue
        works = (payload or {}).get("results") or []
        cand["sample_titles"] = [
            f"{w.get('title') or '?'} ({w.get('publication_year') or '?'})"
            for w in works]
        cand["top_coauthors"] = _top_coauthors([
            (a.get("author") or {}).get("display_name")
            for w in works for a in w.get("authorships") or []
            if _openalex_short_id((a.get("author") or {}).get("id")) != cand["id"]])


def _search_openalex_author(
    name: str, max_per_source: int, orcid: Optional[str] = None,
    http_client: Optional[HTTPClient] = None, *,
    author_ids: Any = None,
) -> AuthorSourceResult:
    """
    Production d'un auteur sur OpenAlex.

    Identité : identifiant(s) d'auteur OpenAlex fournis (filtre
    ``author.id``), sinon ORCID (filtre ``author.orcid``), sinon profils
    examinés sur le nom (``/authors``, voir ``_decide``). Pagination par
    curseur jusqu'au plafond ou à épuisement, tri par date décroissante.
    Désactivé proprement sans clé (statut ``skipped``).
    """
    res = AuthorSourceResult(source="openalex", display_name="OpenAlex")
    api_key = get_api_key(ENV_OPENALEX_KEY, required=False)
    if not api_key:
        res.set_status(STATUS_SKIPPED, REASON_MISSING_KEY,
                       f"{ENV_OPENALEX_KEY} absente")
        return res
    base = {"api_key": api_key}
    ids = normalize_openalex_author_ids(author_ids) if author_ids else []
    if ids:
        res.method, res.identity_basis = METHOD_OPENALEX_ID, BASIS_IDENTIFIER
        res.identity_verified = True
        res.identity = {"source": "OpenAlex",
                        "argument": {"openalex_author_id": ",".join(ids)}}
        author_filter = "author.id:" + "|".join(ids)
    elif orcid:
        res.method, res.identity_basis = METHOD_ORCID, BASIS_IDENTIFIER
        res.identity_verified = True
        res.identity = {"source": "OpenAlex", "argument": {"orcid": orcid}}
        author_filter = f"author.orcid:{orcid}"
    elif name:
        res.method = METHOD_NAME
        cands, state, detail = _openalex_candidates(name, http_client, base)
        if cands is None:
            res.set_status(STATUS_ERROR, *_outcome_failure(state, detail))
            return res
        chosen = _decide(res, cands, lambda big: _openalex_candidate_details(
            big, http_client, base))
        if chosen is None:
            return res
        res.available = chosen.get("works_count")
        author_filter = f"author.id:{chosen['id']}"
    else:
        res.set_status(STATUS_SKIPPED, REASON_NO_IDENTITY)
        return res
    res.query = author_filter
    _paginate_openalex_works(res, http_client, base, author_filter,
                             max_per_source)
    return res


def _paginate_openalex_works(res: AuthorSourceResult, client: Any,
                             base: Dict[str, str], author_filter: str,
                             max_per_source: int) -> None:
    """Travaux d'un filtre d'auteur, page après page (curseur)."""
    report = SearchReport(source=res.display_name)
    max_per_source = max(1, int(max_per_source))
    per_page = max(1, min(AUTHOR_OPENALEX_PAGE_SIZE, max_per_source))
    cursor = "*"
    while True:
        _check_cancel(client)
        payload, state, detail = _get_json_outcome(
            client, OPENALEX_API, "OpenAlex travaux de l'auteur",
            params={**base, "filter": author_filter, "per-page": per_page,
                    "cursor": cursor, "select": _OPENALEX_SELECT,
                    "sort": "publication_date:desc"})
        if state != "ok":
            report.fail(*_outcome_failure(state, detail))
            break
        meta = (payload or {}).get("meta") or {}
        if isinstance(meta.get("count"), int):
            res.available = meta["count"]
        works = (payload or {}).get("results") or []
        if not works:
            report.done()
            break
        report.page(len(works))
        taken = 0
        for work in works:
            if res.item_count >= max_per_source:
                break
            taken += 1
            try:
                res.items.append(map_openalex_item(work))
            except Exception as e:  # noqa: BLE001 - notice illisible
                report.bump("mapping_errors")
                logger.warning(f"[search_by_author] OpenAlex : notice "
                               f"illisible ({type(e).__name__})")
        cursor = meta.get("next_cursor")
        if res.item_count >= max_per_source and (cursor or taken < len(works)):
            report.limit()
            break
        if not cursor:
            report.done()
            break
    _apply_report(res, report)


# --- Semantic Scholar ---

_S2_AUTHOR_FIELDS = "name,affiliations,paperCount,citationCount,hIndex,externalIds,url"


def _s2_candidates(name: str, client: Any, headers: Optional[Dict[str, str]]):
    """Profils Semantic Scholar d'un nom (``/author/search``) :
    ``(candidats, état, détail)``."""
    payload, state, detail = _get_json_outcome(
        client, f"{S2_GRAPH_API}/author/search", "Semantic Scholar candidats auteur",
        params={"query": name, "fields": _S2_AUTHOR_FIELDS,
                "limit": AUTHOR_CANDIDATES_EXAMINED},
        headers=headers)
    if state != "ok":
        return None, state, detail
    cands = []
    for author in (payload or {}).get("data") or []:
        ident = str(author.get("authorId") or "").strip()
        if not ident:
            continue
        ext = author.get("externalIds") or {}
        orcid = ext.get("ORCID")
        if isinstance(orcid, list):
            orcid = orcid[0] if orcid else None
        affiliations = [a for a in author.get("affiliations") or [] if a]
        full = author.get("name") or ""
        plausible = names_match(name, full=full)
        cands.append(_candidate(
            "Semantic Scholar", ident, full,
            author.get("paperCount"),
            plausible=plausible,
            inverted_name=None if plausible else inverted_name_form(
                name, full=full),
            argument={"s2_author_id": ident},
            affiliation="; ".join(affiliations[:2]) or None,
            orcid=_orcid_tail(orcid) if orcid else None,
            cited_by_count=author.get("citationCount"),
            h_index=author.get("hIndex")))
    return cands, "ok", ""


def _s2_candidate_details(cands: List[Dict[str, Any]], client: Any,
                          headers: Optional[Dict[str, str]]) -> None:
    """Dernières publications et co-auteurs de candidats Semantic Scholar."""
    for cand in cands:
        payload, state, _ = _get_json_outcome(
            client, f"{S2_GRAPH_API}/author/{cand['id']}/papers",
            "Semantic Scholar détail candidat",
            params={"fields": "title,year,authors",
                    "limit": AUTHOR_CANDIDATE_SAMPLE},
            headers=headers)
        if state != "ok":
            continue
        papers = (payload or {}).get("data") or []
        cand["sample_titles"] = [
            f"{p.get('title') or '?'} ({p.get('year') or '?'})" for p in papers]
        span = _years_span([p.get("year") for p in papers])
        if span:
            cand["period"] = f"{span} (dernières publications)"
        cand["top_coauthors"] = _top_coauthors([
            a.get("name") for p in papers for a in p.get("authors") or []
            if str(a.get("authorId") or "") != cand["id"]])


def _search_s2_author(
    name: str, max_per_source: int,
    http_client: Optional[HTTPClient] = None, *,
    author_ids: Any = None, orcid: Optional[str] = None,
) -> AuthorSourceResult:
    """
    Production d'un auteur sur Semantic Scholar (sans clé : pool partagé).

    Identité : identifiant(s) d'auteur fournis, sinon profils examinés sur le
    nom (``/author/search``) ; un ORCID fourni désigne le profil qui le porte
    (``externalIds``). Pagination par décalage (``offset`` / ``next``) de
    ``/author/{id}/papers`` jusqu'au plafond ou à épuisement.
    """
    res = AuthorSourceResult(source="semantic_scholar",
                             display_name="Semantic Scholar")
    api_key = get_api_key(ENV_S2_KEY, required=False)
    headers = {"x-api-key": api_key} if api_key else None
    ids = normalize_s2_author_ids(author_ids) if author_ids else []
    if ids:
        res.method, res.identity_basis = METHOD_S2_ID, BASIS_IDENTIFIER
        res.identity_verified = True
        res.identity = {"source": "Semantic Scholar",
                        "argument": {"s2_author_id": ",".join(ids)}}
    elif name:
        res.method = METHOD_NAME
        cands, state, detail = _s2_candidates(name, http_client, headers)
        if cands is None:
            refusal = keyless_refusal(detail, bool(api_key), ENV_S2_KEY)
            if refusal:
                res.set_status(STATUS_SKIPPED, REASON_MISSING_KEY, refusal)
            else:
                res.set_status(STATUS_ERROR, *_outcome_failure(state, detail))
            return res
        chosen = _decide(res, cands, lambda big: _s2_candidate_details(
            big, http_client, headers), orcid=orcid)
        if chosen is None:
            return res
        res.available = chosen.get("works_count")
        ids = [chosen["id"]]
    else:
        res.set_status(STATUS_SKIPPED, REASON_NO_IDENTITY)
        return res
    res.query = "author:" + ",".join(ids)
    _paginate_s2_papers(res, http_client, headers, ids, max_per_source)
    return res


def _paginate_s2_papers(res: AuthorSourceResult, client: Any,
                        headers: Optional[Dict[str, str]], ids: List[str],
                        max_per_source: int) -> None:
    """Publications d'un ou plusieurs profils, page après page (décalage)."""
    report = SearchReport(source=res.display_name)
    max_per_source = max(1, int(max_per_source))
    all_exhausted = True
    for index, author_id in enumerate(ids):
        if res.item_count >= max_per_source:
            all_exhausted = False
            break
        offset = 0
        while True:
            _check_cancel(client)
            limit = max(1, min(AUTHOR_S2_PAGE_SIZE,
                               max_per_source - res.item_count))
            payload, state, detail = _get_json_outcome(
                client, f"{S2_GRAPH_API}/author/{author_id}/papers",
                "Semantic Scholar publications de l'auteur",
                params={"fields": _S2_FIELDS, "offset": offset, "limit": limit},
                headers=headers)
            if state != "ok":
                refusal = (keyless_refusal(detail, bool(headers), ENV_S2_KEY)
                           if report.pages == 0 else None)
                if refusal:
                    report.skip(REASON_MISSING_KEY, refusal)
                else:
                    report.fail(*_outcome_failure(state, detail))
                all_exhausted = False
                break
            papers = (payload or {}).get("data") or []
            if not papers:
                break
            report.page(len(papers))
            for paper in papers:
                if res.item_count >= max_per_source:
                    break
                try:
                    res.items.append(map_s2_item(paper))
                except Exception as e:  # noqa: BLE001 - notice illisible
                    report.bump("mapping_errors")
                    logger.warning(f"[search_by_author] S2 : notice illisible "
                                   f"({type(e).__name__})")
            nxt = (payload or {}).get("next")
            if nxt is None:
                break
            if res.item_count >= max_per_source:
                all_exhausted = False
                break
            offset = int(nxt)
    if all_exhausted:
        report.done()
    elif report.status is None:
        report.limit()
    _apply_report(res, report)


# --- Orchestration ---

def _run_in_pool(tasks: List[Tuple[str, Callable[[], Any]]],
                 max_workers: Optional[int] = None) -> List[Any]:
    """
    Exécute des tâches indépendantes sur un pool borné et renvoie leurs
    résultats **dans l'ordre des tâches** (jamais dans l'ordre d'arrivée).
    Une tâche qui lève renvoie ``None`` (journalisé).
    """
    if not tasks:
        return []
    workers = LOOKUP_MAX_WORKERS if max_workers is None else int(max_workers)
    workers = max(1, min(workers, len(tasks)))

    def _safe(label: str, fn: Callable[[], Any]):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - une source n'arrête pas les autres
            logger.warning(f"{label} author search échec : {e}")
            return None

    if workers == 1:
        return [_safe(label, fn) for label, fn in tasks]
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="author-search") as pool:
        futures = [pool.submit(_safe, label, fn) for label, fn in tasks]
        return [fut.result() for fut in futures]


def _set_aside(res: AuthorSourceResult) -> int:
    """Notices d'une source retirées du résultat (comptées)."""
    n = res.item_count
    res.counters["discarded"] = res.counters.get("discarded", 0) + n
    res.items = []
    res.limit_reached = False
    return n


def _discard(res: AuthorSourceResult, why: str) -> None:
    """Notices d'une source retirées du résultat (homonymie à lever)."""
    n = _set_aside(res)
    res.set_status(STATUS_SKIPPED, REASON_AWAITING_IDENTITY,
                   f"{n} notice(s) obtenue(s) par le nom, non retenues : {why}")


def _hold_unconfirmed(res: AuthorSourceResult) -> None:
    """
    Mode identifiant : profil choisi sur le nom, relié à aucun identifiant
    fourni (ni identifiant propre à la source, ni ORCID porté par le
    profil). Rien ne dit que c'est la personne désignée par les
    identifiants (homonymie possiblement établie par un appel précédent) :
    ses notices sont mises de côté et le profil est proposé à confirmer.
    """
    n = _set_aside(res)
    orcid_note = ""
    if any("ORCID" in w for w in res.warnings):
        orcid_note = (" ; plusieurs profils portent l'ORCID fourni"
                      if any("plusieurs profils" in w for w in res.warnings)
                      else " ; aucun profil examiné ne porte l'ORCID fourni")
    res.set_status(STATUS_SKIPPED, REASON_UNCONFIRMED_PROFILE,
                   f"{n} notice(s) non retenue(s){orcid_note}")


def _skipped_source(key: str, display: str, detail: str) -> AuthorSourceResult:
    """Source non interrogée faute de nom (inscrite au rapport)."""
    res = AuthorSourceResult(source=key, display_name=display)
    res.set_status(STATUS_SKIPPED, REASON_NO_IDENTITY, detail)
    return res


def search_by_author(
    name: str,
    idhal: str = None,
    orcid: str = None,
    max_per_source: int = 200,
    http_client: HTTPClient = None,
    *,
    max_workers: Optional[int] = None,
    cancel: Any = None,
    openalex_author_id: Any = None,
    s2_author_id: Any = None,
    hal_person_id: Any = None,
) -> AuthorItems:
    """
    Recherche la production d'un auteur sur HAL, Isidore, OpenAlex et
    Semantic Scholar, avec contrôle d'identité (OPT-14).

    Stratégie par source :
    - HAL : idHal, identifiant de personne HAL ou ORCID
      (``authORCIDIdExt_s``) s'ils sont fournis ; sinon nom exact
      (``authFullName_s``) après examen des auteurs HAL identifiés sous ce
      nom (homonymes : ambiguïté ; un seul auteur productif : les dépôts des
      autres personnes HAL homonymes sont exclus) ;
    - Isidore : plein texte sur le nom entre guillemets (couvre OpenEdition,
      Cairn, Persée, Érudit), notices conservées seulement si leurs
      créateurs portent le nom (compteur ``rejected_not_author``) ; identité
      jamais vérifiable (homonymes indiscernables) ;
    - OpenAlex (clé requise) : identifiant(s) d'auteur, sinon ORCID, sinon
      profils examinés sur le nom ; pagination par curseur ;
    - Semantic Scholar : identifiant(s) d'auteur, sinon profils examinés sur
      le nom (l'ORCID fourni désigne le profil qui le porte) ; pagination
      par décalage.

    Homonymie : en recherche par nom seul (aucun identifiant), une ambiguïté
    établie par une source (au moins deux profils plausibles d'au moins
    ``AUTHOR_AMBIGUITY_MIN_WORKS`` travaux) fait écarter toutes les notices :
    le rapport rend les candidats des sources ambiguës et les profils choisis
    sur le nom par les autres (``profiles_to_confirm``), et un second appel
    avec l'identité choisie est attendu. Avec au moins un identifiant
    (second appel), seules sont retenues les sources dont l'identité est
    établie par un identifiant (fourni, ou ORCID fourni porté par le
    profil) : une source revenue au nom et dont le profil n'est relié à
    aucun identifiant fourni est mise de côté (motif
    ``unconfirmed_profile``, profil proposé à confirmer) ; les sources sans
    identifiant d'auteur (Isidore, HAL sans liste d'auteurs) sont écartées
    si une ambiguïté est établie dans l'appel, sinon retenues et signalées
    comme non séparées des homonymes. Aucune promesse de complétude dans ces
    cas (``production_complete``).

    Sans nom, seules les sources dotées de leur propre identifiant sont
    interrogées (HAL : idhal, hal_person_id ou orcid ; OpenAlex :
    openalex_author_id ou orcid ; Semantic Scholar : s2_author_id) ; les
    autres figurent au rapport (``skipped``, ``no_identity``). Une saisie
    peut-être inversée (« NOM Prénom ») est
    signalée, jamais supposée.

    Les sources sont interrogées en parallèle (pool borné) ; leurs résultats
    sont concaténés dans l'ordre séquentiel historique (HAL, Isidore,
    OpenAlex, S2) avant déduplication et tri, donc indépendamment de l'ordre
    d'arrivée.

    Args:
        name: Nom complet (ex : "Patrick Charaudeau").
        idhal: Identifiant HAL de l'auteur (ex : "patrick-charaudeau").
        orcid: ORCID (ex : "0000-0002-1825-0097") : HAL et OpenAlex par
            identifiant, profil Semantic Scholar qui le porte. N'identifie
            une source que si ses notices ou profils le portent (fiable pour
            OpenAlex, rarement renseigné dans HAL et Semantic Scholar). Une
            clé de contrôle fausse est signalée dans le rapport (l'outil MCP
            la refuse).
        max_per_source: Plafond par source (atteint : signalé, liste
            incomplète).
        http_client: Client HTTP partagé.
        max_workers: Taille du pool (défaut ``LOOKUP_MAX_WORKERS``).
        cancel: Signal d'annulation de l'appel (OPT-07) : porté par une vue
            du client (``HTTPClient.with_cancel``, même session), il atteint
            chaque requête et chaque connecteur (vérification entre deux
            pages) ; une annulation lève ``utils.http.OperationCancelled``.
        openalex_author_id: Identifiant(s) d'auteur OpenAlex (``A…``,
            plusieurs séparés par des virgules pour des profils fragmentés).
        s2_author_id: Identifiant(s) d'auteur Semantic Scholar.
        hal_person_id: Identifiant de personne HAL (``authIdPerson_i``), pour
            un auteur HAL sans idHal.

    Returns:
        ``AuthorItems`` : liste d'items Zotero dédupliquée, triée par année
        décroissante, qui porte le rapport (``.report``,
        ``AuthorSearchReport`` : statut et candidats par source).

    Raises:
        ValueError: identifiant mal formé (``normalize_author_identifiers``),
            ou ni nom ni identifiant.
    """
    started = time.monotonic()
    name = (name or "").strip()
    idhal = (idhal or "").strip() or None
    ids = normalize_author_identifiers(
        orcid=orcid, openalex_author_id=openalex_author_id,
        s2_author_id=s2_author_id, hal_person_id=hal_person_id,
        check_orcid=False)
    orcid = ids["orcid"]
    oa_ids, s2_ids = ids["openalex_author_id"], ids["s2_author_id"]
    hal_person = ids["hal_person_id"]
    max_per_source = max(1, int(max_per_source))
    if not (name or idhal or orcid or oa_ids or s2_ids or hal_person):
        raise ValueError("nom de l'auteur vide et aucun identifiant fourni")
    notes: List[str] = []
    if orcid and not orcid_checksum_ok(orcid):
        # Signalé sans lever (appelants historiques) ; l'outil MCP refuse.
        notes.append(f"ORCID {orcid} : clé de contrôle incorrecte (faute de "
                     f"frappe probable) ; les sources interrogées par cet "
                     f"ORCID ne trouveront pas l'auteur")

    own_http = http_client is None
    if own_http:
        http_client = HTTPClient()
    owner = http_client
    if cancel is not None and hasattr(http_client, "with_cancel"):
        http_client = http_client.with_cancel(cancel)

    def _hal() -> AuthorSourceResult:
        res = AuthorSourceResult(source="hal", display_name="HAL")
        hal = HALConnector(http_client=http_client)
        query = _build_hal_author_query(name, idhal, orcid, hal_person)
        if idhal or hal_person or orcid:
            res.method = (METHOD_IDHAL if idhal else
                          METHOD_HAL_PERSON if hal_person else METHOD_ORCID)
            res.identity_basis, res.identity_verified = BASIS_IDENTIFIER, True
            argument = ({"idhal": idhal} if idhal else
                        {"hal_person_id": hal_person} if hal_person else
                        {"orcid": orcid})
            res.identity = {"source": "HAL", "argument": argument}
        else:
            res.method = METHOD_NAME
            # Auteurs HAL identifiés sous ce nom, par le client du connecteur
            # (même session, même politique) ; indisponibles : nom seul.
            client = getattr(hal, "http", None)
            found = (_hal_candidates(client, name) if client is not None
                     else (None, 0, 0, LOOKUP_ERROR, "client absent"))
            cands, total, unlinked, _state, _detail = found
            if cands is None:
                res.identity_basis = BASIS_NAME_ONLY
                res.warn("homonymie non vérifiée sur HAL (liste des auteurs "
                         "HAL indisponible)")
            else:
                big = [c for c in cands
                       if _works(c) >= AUTHOR_AMBIGUITY_MIN_WORKS]
                if len(big) >= 2:
                    if AUTHOR_CANDIDATE_DETAILS:
                        _hal_candidate_details(
                            big[:AUTHOR_CANDIDATE_DETAILS], client, name)
                    res.set_status(
                        STATUS_SKIPPED, REASON_AMBIGUOUS_AUTHOR,
                        f"{len(big)} auteurs HAL homonymes d'au moins "
                        f"{AUTHOR_AMBIGUITY_MIN_WORKS} dépôts")
                    res.candidates = cands[:AUTHOR_CANDIDATES_EXAMINED]
                    res.query = query
                    return res
                if big:
                    chosen = big[0]
                    others = [c for c in cands if c["id"] != chosen["id"]]
                    res.identity_basis = BASIS_SINGLE
                    _retain(res, chosen, cands, warn_others=False)
                    # Dépôts sous ce nom, moins ceux des autres personnes HAL
                    # homonymes, plus ceux de la personne retenue sous
                    # d'autres formes de son nom.
                    by_name = query
                    if others:
                        by_name = (f"({query} AND NOT authIdPerson_i:("
                                   + " OR ".join(c["id"] for c in others) + "))")
                        res.warn(
                            f"{sum(_works(c) for c in others)} dépôt(s) "
                            f"d'autres auteurs HAL homonymes exclus : "
                            + ", ".join(c["id"] for c in others[:5])
                            + (" …" if len(others) > 5 else ""))
                    query = f"{by_name} OR authIdPerson_i:{chosen['id']}"
                    if unlinked:
                        # Dépôts sous ce nom sans personne HAL : identité
                        # inconnue (homonymes possibles), inclus et signalés
                        # (réserve, pas de promesse de complétude).
                        res.counters["unlinked_included"] = unlinked
                else:
                    res.identity_basis = BASIS_NAME_ONLY
                    res.candidates = [dict(c, selected=False) for c in cands]
                    res.available = total
                if unlinked:
                    res.warn(f"{unlinked} dépôt(s) sans auteur HAL identifié "
                             f"inclus (identité non vérifiée)")
        res.query = query
        res.items = list(hal.search(query, max_results=max_per_source) or [])
        _apply_report(res, getattr(hal, "last_status", None))
        logger.info(f"[search_by_author] HAL : {res.item_count} items")
        return res

    def _isidore() -> AuthorSourceResult:
        # Recherche plein texte sur le nom (entre guillemets pour l'exactitude),
        # puis filtre sur les créateurs : une notice qui cite l'auteur (compte
        # rendu, séminaire) sans qu'il en soit l'auteur est écartée.
        # Registre de flux propre à l'appel : le client reçu peut être le
        # client partagé de longue durée du serveur MCP, auquel le registre
        # par défaut serait attaché ; un flux Isidore (et une erreur Isidore)
        # y resterait mémorisé d'un appel à l'autre, jusqu'à une heure.
        res = AuthorSourceResult(source="isidore", display_name="Isidore",
                                 method=METHOD_NAME_TEXT,
                                 identity_basis=BASIS_NAME_ONLY)
        iso = IsidoreConnector(http_client=http_client,
                               scan_registry=ScanRegistry())
        res.query = f'"{name}"'
        received = list(iso.search(res.query, max_results=max_per_source) or [])
        res.items = [it for it in received if creators_contain(it, name)]
        _apply_report(res, getattr(iso, "last_status", None))
        res.counters["received"] = len(received)
        res.counters["rejected_not_author"] = len(received) - res.item_count
        if not res.items and received:
            # Rien ne correspond au nom saisi : saisie peut-être inversée
            # (« NOM Prénom ») si des créateurs portent la forme inversée.
            forms = [f for f in (_creators_inverted_form(it, name)
                                 for it in received) if f]
            if forms:
                res.name_hint = forms[0]
                res.counters["inverted_name_matches"] = len(forms)
        logger.info(f"[search_by_author] Isidore : {res.item_count} items "
                    f"({res.rejected_not_author} sans l'auteur parmi les "
                    f"créateurs)")
        return res

    def _openalex():
        # Arguments nouveaux transmis seulement s'ils servent (signature
        # historique des doublures : nom, plafond, orcid, client).
        extra = {"author_ids": oa_ids} if oa_ids else {}
        return _search_openalex_author(name, max_per_source, orcid,
                                       http_client=http_client, **extra)

    def _s2():
        extra: Dict[str, Any] = {}
        if s2_ids:
            extra["author_ids"] = s2_ids
        if orcid:
            extra["orcid"] = orcid
        return _search_s2_author(name, max_per_source,
                                 http_client=http_client, **extra)

    # Sources dans l'ordre séquentiel historique ; sans nom, une source
    # dépourvue de son propre identifiant n'est pas interrogée mais figure
    # au rapport (``skipped``, ``no_identity``) : jamais une source absente
    # du rapport ni une production présentée comme complète sans elle.
    # Sans nom, OpenAlex est interrogé sur openalex_author_id ou sur l'ORCID
    # (filtre author.orcid, fiable : c'est ce que promet l'argument orcid) ;
    # Semantic Scholar seulement sur s2_author_id (aucune recherche de profil
    # par ORCID sans nom).
    plan: List[Tuple[str, Any]] = [
        ("HAL", _guarded("hal", "HAL", _hal)
         if (idhal or hal_person or orcid or name) else _skipped_source(
             "hal", "HAL", "sans nom, HAL n'est interrogé que sur idhal, "
             "hal_person_id ou orcid")),
        ("Isidore", _guarded("isidore", "Isidore", _isidore)
         if name else _skipped_source(
             "isidore", "Isidore", "nom requis (recherche plein texte sur "
             "le nom)")),
        ("OpenAlex", _guarded("openalex", "OpenAlex", _openalex)
         if (name or oa_ids or orcid) else _skipped_source(
             "openalex", "OpenAlex", "sans nom, OpenAlex n'est interrogé que "
             "sur openalex_author_id ou orcid")),
        ("Semantic Scholar", _guarded(
            "semantic_scholar", "Semantic Scholar", _s2)
         if (name or s2_ids) else _skipped_source(
             "semantic_scholar", "Semantic Scholar", "sans nom, Semantic "
             "Scholar n'est interrogé que sur s2_author_id")),
    ]
    tasks = [(label, job) for label, job in plan if callable(job)]

    try:
        ran = iter(_run_in_pool(tasks, max_workers))
    finally:
        if own_http:
            owner.close()
    results = [r for r in (next(ran) if callable(job) else job
                           for _, job in plan) if r is not None]

    identifiers = {"idhal": idhal, "orcid": orcid,
                   "openalex_author_id": oa_ids, "s2_author_id": s2_ids,
                   "hal_person_id": hal_person}
    identifier_mode = any(bool(v) for v in identifiers.values())
    ambiguous = [r for r in results if r.ambiguous]
    why = ("homonymes signalés par "
           + ", ".join(r.display_name for r in ambiguous)) if ambiguous else ""
    for res in results:
        if res.ambiguous:
            continue
        if not identifier_mode:
            # Nom seul : aucune notice retenue tant que l'identité n'est pas
            # choisie (jamais le premier candidat en silence).
            if ambiguous and res.items:
                _discard(res, why)
        elif res.identity_basis in NAME_CHOICE_BASES:
            # Mode identifiant (second appel) : profil choisi sur le nom,
            # relié à aucun identifiant fourni ; l'homonymie a pu être
            # établie par l'appel précédent, invisible ici : mis de côté et
            # proposé à confirmer, jamais retenu en silence.
            _hold_unconfirmed(res)
        elif res.identity_basis == BASIS_NAME_ONLY and res.items:
            if ambiguous:
                # Source sans contrôle d'identité : ses notices mêleraient
                # les homonymes établis dans cet appel.
                _discard(res, why)
            else:
                # Aucun identifiant d'auteur dans cette source (Isidore) :
                # retenue, mais signalée comme non séparée des homonymes.
                res.unseparated = True

    all_items: List[Dict] = []
    for res in results:
        all_items.extend(res.items)

    # Déduplication
    deduplicated = deduplicate(all_items)

    # Tri par année décroissante (les plus récents en premier)
    def _year_key(item):
        date = item.get("date") or ""
        m = re.search(r"\b(\d{4})\b", date)
        return -int(m.group(1)) if m else 0
    deduplicated.sort(key=_year_key)

    report = AuthorSearchReport(
        name=name, identifiers=identifiers, sources=results,
        total_before_dedup=len(all_items), total_unique=len(deduplicated),
        duration_s=time.monotonic() - started, notes=notes)
    logger.info(
        f"[search_by_author] {name}: {len(all_items)} bruts → "
        f"{len(deduplicated)} uniques ({report.overall_status})"
    )
    return AuthorItems(deduplicated, report)
