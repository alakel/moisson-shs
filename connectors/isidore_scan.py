"""
Parcours Isidore mutualisé : un seul flux de pages par requête et par exécution.

Contexte (constats C09, C10 et C35 du plan d'optimisation) :

- la source ``isidore`` et les cinq sous-sources qui passent par Isidore
  (OpenEdition, Cairn, Persée, Érudit, Theses.fr) paginaient chacune la même
  requête ``resource/search`` pour n'en garder que leurs notices : six
  paginations identiques par moisson francophone ;
- l'API ne sert que les ``ISIDORE_RESULT_WINDOW`` (200) premiers résultats
  d'une requête : toute page au-delà répète la dernière fenêtre. Faute de fin
  détectable, chaque sous-source retéléchargeait la même page jusqu'à la
  borne ``ceil(max_results × 100 / 100) + 10`` pages (210 pages de 4,6 Mo pour
  200 notices demandées ; dans la moisson live, les 200 notices « Cairn »
  n'étaient que 4 notices distinctes).

Vérifications live du 2026-09-28 (cf. notes OPT-08) : ``resource/search`` ne
propose aucun filtre fiable par agrégateur. Ses filtres documentés sont
``uri, author, authorId, date, language, type, discipline, subject, source,
scope`` ; ``source`` désigne une source au sens d'Isidore (une revue, un
carnet : des milliers par agrégateur) et n'a rien renvoyé pour la revue de la
première notice de la requête de référence ; ``collection`` et
``organization`` ne filtrent que ``source/search`` (sur ``resource/search``,
ils renvoient des notices sans rapport) ; ``publisher`` (non documenté) est
ignoré (même total, mêmes notices que sans filtre) ; ``date`` donne des totaux
incohérents ; la syntaxe documentée de ``q`` (ET, OU, SAUF, PROCHE,
guillemets) n'a pas de champ « domaine » (``ET url:cairn.info`` agit comme des
mots du plein texte : 3 notices sur 5 hors Cairn). Aucun point d'accès SPARQL
n'est documenté par l'API (``isidore.science/sparql`` : 404) ; il ne serait de
toute façon pas le même modèle de recherche (pas le classement par pertinence
de ``resource/search``). Toutes les sous-sources consomment donc le **même
flux non filtré**, et la limite de la fenêtre du fournisseur est annoncée
(statut et journal) quand une sous-source n'atteint pas son plafond à cause
d'elle.

Fonctionnement :

- ``IsidoreScan`` : flux de pages d'une requête (paramètres Isidore envoyés,
  taille de page). Les pages sont téléchargées paresseusement, **une seule
  fois** même si plusieurs threads les demandent en même temps ; chaque
  consommateur relit le flux depuis le début. Les notices sont mappées une
  fois vers Zotero à l'arrivée (la notice brute, ~45 Ko, n'est pas conservée) ;
  chaque consommateur reçoit une copie profonde. Fin du flux : page vide ou
  partielle, total annoncé atteint, bloc ``page`` d'Isidore sans ``@next`` ou
  dont ``@current`` diffère de la page demandée, page sans aucune notice
  nouvelle (identifiant Isidore déjà vu), position atteignant la fenêtre
  ``ISIDORE_RESULT_WINDOW``, borne ``ISIDORE_SCAN_MAX_RECORDS`` (y compris
  quand la page qui la franchit est la dernière : des notices ont été
  écartées), ou erreur (réseau, corps non JSON : l'erreur est conservée dans
  le statut) ;
- erreur d'une page : partagée par les consommateurs déjà engagés dans le
  flux (pas de 6 × 3 tentatives pendant une panne franche) ; un consommateur
  arrivé **après** l'échec reprend la page qui a échoué si l'erreur date d'au
  moins ``ScanRegistry.error_retry_after_s`` secondes (pages déjà acquises
  conservées ; au plus une reprise par consommateur). Une panne passagère
  ne rend donc plus muets les consommateurs lancés plus tard, ni un appel
  ultérieur sur un client de longue durée ;
- reprise différée (OPT-09, flux d'une moisson : ``ScanRegistry.for_run``) :
  en topologie plate, la source directe et les sous-sources démarrent
  ensemble et sont toutes engagées avant un échec ; le flux attend alors
  ``deferred_retry_after`` secondes et retente la page une seule fois (une
  tentative réseau, sans cache), les consommateurs attendant l'issue et la
  partageant : même résultat pour tous, quel que soit l'instant de leur
  démarrage ; pas de reprise différée sur un refus définitif (4xx hors 429)
  ni après un délai dépassé (le service ne répond plus : les tentatives de
  la politique commune ont déjà attendu chacune un délai complet) ; la
  tentative différée a son propre budget
  (``config.ISIDORE_DEFERRED_RETRY_BUDGET_S``) et, sous une échéance
  d'exécution, n'est faite que si elle peut finir 1 s avant elle (son échec
  reste une erreur de page, jamais l'annulation de la moisson) ;
- ``ScanRegistry`` : flux d'une exécution. Par défaut, un registre est
  attaché à l'instance ``HTTPClient`` partagée par la moisson
  (``registry_for``, dictionnaire à clés faibles) : chaque moisson crée son
  client, donc deux moissons ne partagent jamais leurs flux. Un contexte
  d'exécution peut aussi passer explicitement son registre (paramètre
  ``scan_registry=`` des connecteurs) ;
- filtre par sous-source sur le **domaine analysé** des URL (URL primaire et
  ``ore.aggregates``) : l'hôte doit être le domaine ou l'un de ses
  sous-domaines (``cairn.info`` ne correspond ni à ``notcairn.info`` ni à un
  paramètre de requête contenant ``cairn.info``) ;
- ``IsidoreScanStatus`` : statut lisible par l'orchestrateur (pages, notices
  parcourues et retenues, fin de fenêtre, borne, erreur et son motif,
  sous-source incomplète) ;
- annulation (OPT-07) : ``iter_records`` et ``consume`` acceptent un signal
  d'annulation (``utils.http.CancelSignal``), vérifié avant le
  téléchargement de chaque page et pendant l'attente de la page qu'un autre
  consommateur télécharge : aucune nouvelle page ne part après la détection.
  Un téléchargement interrompu libère le flux sans le clore (la page n'est
  ni acquise ni en erreur) ;
- taille de page et rangs (OPT-12) : ``config.ISIDORE_REPLIES_PER_PAGE``
  vaut 200, la fenêtre entière, servie en UNE requête (vérifié en live les
  2026-09-28 et 2026-09-29 : 200 notices, 9,3 Mo, sans bloc ``page``) ; le
  rang natif d'une notice est sa position dans la réponse brute de l'API,
  doublons compris (``ScanRecord.native_rank`` : un doublon servi par l'API
  laisse un trou, comme pour HAL), que ``consume`` renvoie dans
  ``IsidoreScanStatus.ranks``, aligné sur les items retenus ; Isidore
  n'expose aucun score, et son ordre par défaut n'est pas documenté comme un
  classement par pertinence (non vérifié : rang de position seulement).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
import threading
import time
import weakref
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

import config
from engine.models import REASON_INVALID_BODY, classify_exception
from mappers.isidore_mapper import map_isidore_to_zotero
from utils.http import (
    CANCEL_WAIT_SLICE, RETRYABLE_STATUS, CancelSignal, OperationCancelled,
)

logger = logging.getLogger(__name__)


# Raisons de fin d'un flux (attribut ``end_reason``).
END_EXHAUSTED = "exhausted"   # page vide ou partielle, total annoncé atteint
END_WINDOW = "window"         # fenêtre de résultats du fournisseur atteinte
END_REPEAT = "repeat"         # page sans notice nouvelle (fenêtre répétée)
END_BOUND = "bound"           # borne ISIDORE_SCAN_MAX_RECORDS atteinte
END_ERROR = "error"           # échec réseau ou corps illisible

_WINDOW_REASONS = frozenset({END_WINDOW, END_REPEAT})


class IsidoreScanError(RuntimeError):
    """
    Échec d'une page du parcours Isidore (corps non JSON, structure
    inattendue). Le message commence par « page N : ».

    Attributes:
        retried: une nouvelle tentative sans cache a déjà été faite.
    """

    def __init__(self, message: str, retried: bool = False):
        super().__init__(message)
        self.retried = retried


# === Lecture d'une notice Isidore ===

def extract_replies(data: Any) -> List[Any]:
    """
    Liste des notices d'une réponse ``resource/search``.

    ``replies.content.reply`` est une liste, ou un objet unique quand la page
    ne compte qu'un résultat (particularité de l'API).
    """
    replies_block = data.get("response", {}).get("replies", {})
    if not isinstance(replies_block, dict):
        return []
    content = replies_block.get("content", {})
    if isinstance(content, dict):
        replies = content.get("reply", [])
    else:
        replies = content
    if isinstance(replies, dict):
        return [replies]
    if isinstance(replies, list):
        return replies
    return []


def record_urls(record: Dict) -> List[str]:
    """URL primaire (``isidore.url``) puis URL de ``ore.aggregates``."""
    iso = record.get("isidore", {}) if isinstance(record, dict) else {}
    if not isinstance(iso, dict):
        return []
    urls: List[str] = []
    primary = iso.get("url", "")
    if isinstance(primary, str) and primary:
        urls.append(primary)
    ore = iso.get("ore", {})
    aggregates = ore.get("aggregates", []) if isinstance(ore, dict) else []
    if isinstance(aggregates, dict):
        aggregates = [aggregates]
    if not isinstance(aggregates, list):
        aggregates = []
    for agg in aggregates:
        url = agg.get("$", "") if isinstance(agg, dict) else str(agg)
        if url:
            urls.append(url)
    return urls


def normalize_domain(marker: str) -> str:
    """
    Domaine de référence d'une sous-source (``"cairn.info"``).

    Accepte un domaine nu ou une URL (``https://www.cairn.info/``) ; renvoie le
    nom d'hôte en minuscules, sans point initial ni final.
    """
    text = (marker or "").strip().lower()
    if "/" in text or ":" in text:
        host = url_host(text)
        if host:
            text = host
    return text.strip(".").removeprefix("*.").strip(".")


def url_host(url: str) -> Optional[str]:
    """
    Nom d'hôte analysé d'une URL (minuscules, sans port ni point final).

    Une URL sans schéma (``www.cairn.info/revue…``) est lue comme
    ``//www.cairn.info/revue…``. Renvoie None si aucun hôte n'est lisible.
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    if "://" not in text and not text.startswith("//"):
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.rstrip(".").lower() or None


def host_matches_domain(host: Optional[str], domain: str) -> bool:
    """Vrai si ``host`` est ``domain`` ou l'un de ses sous-domaines."""
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def urls_match_domain(urls: Iterable[str], domain: str) -> bool:
    """Vrai si l'hôte d'une des URL appartient au domaine ``domain``."""
    domain = normalize_domain(domain)
    return any(host_matches_domain(url_host(u), domain) for u in urls)


def record_matches_domain(record: Dict, marker: str) -> bool:
    """Vrai si l'URL primaire ou un agrégat d'une notice relève du domaine."""
    return urls_match_domain(record_urls(record), marker)


_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")


def extract_year(record: Dict) -> Optional[int]:
    """
    Année (int) de ``isidore.date`` pour le filtrage temporel.

    Priorité ``@year``, ``year``, puis ``normalizedDate``, ``@startDate`` ;
    une date texte est lue par expression régulière. None si indétectable.
    """
    iso = record.get("isidore", {}) if isinstance(record, dict) else {}
    if not isinstance(iso, dict):
        return None
    date_field = iso.get("date")
    if not date_field:
        return None
    if isinstance(date_field, str):
        m = _YEAR_RE.search(date_field)
        return int(m.group(1)) if m else None
    if isinstance(date_field, dict):
        for key in ("@year", "year"):
            v = date_field.get(key)
            if v and str(v).isdigit():
                return int(v)
        for key in ("normalizedDate", "@startDate"):
            v = date_field.get(key)
            if v and len(str(v)) >= 4:
                try:
                    return int(str(v)[:4])
                except ValueError:
                    pass
    return None


def extract_source_name(record: Dict) -> str:
    """
    Nom de la source originelle (``isidore.source.name``), français de
    préférence, sinon première valeur disponible ; "" si absent.
    """
    iso = record.get("isidore", {}) if isinstance(record, dict) else {}
    if not isinstance(iso, dict):
        return ""
    source = iso.get("source")
    if not source:
        return ""
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        names = source.get("name", [])
        if isinstance(names, list):
            for n in names:
                if isinstance(n, dict) and n.get("@xml:lang") == "fr" and n.get("$"):
                    return n["$"]
            for n in names:
                if isinstance(n, dict) and n.get("$"):
                    return n["$"]
                elif isinstance(n, str) and n:
                    return n
        elif isinstance(names, str):
            return names
        elif isinstance(names, dict) and names.get("$"):
            return names["$"]
    if isinstance(source, list) and source:
        return extract_source_name({"isidore": {"source": source[0]}})
    return ""


def record_key(record: Any) -> str:
    """
    Identifiant d'une notice pour détecter les répétitions : ``@uri`` Isidore,
    sinon empreinte du contenu (notice sans identifiant, cas marginal).
    """
    if isinstance(record, dict):
        uri = record.get("@uri")
        if isinstance(uri, str) and uri:
            return uri
    try:
        blob = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(record)
    return "sha1:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()


class ScanRecord:
    """
    Notice d'un flux, réduite à ce que les consommateurs utilisent : URL
    (filtre de domaine), année (dates), nom de source (filtre nominal) et
    item Zotero mappé (modèle jamais remis tel quel : copie profonde).

    ``position`` : indice parmi les notices distinctes du flux (0..n-1) ;
    ``native_rank`` : position 1..n de la notice dans la réponse brute de
    l'API, doublons compris (décalage de la page + indice dans la réponse),
    c'est-à-dire le rang natif publié (revue OPT-12 : un doublon servi par
    l'API laisse un trou, comme pour HAL, au lieu de décaler les rangs
    suivants).
    """

    __slots__ = ("position", "native_rank", "page", "key", "urls", "year",
                 "source_name", "item", "map_error")

    def __init__(self, record: Dict, page: int, native_rank: int = 0):
        self.position = -1
        self.native_rank = int(native_rank)
        self.page = page
        self.key = record_key(record)
        self.urls = tuple(record_urls(record))
        self.year = extract_year(record)
        self.source_name = extract_source_name(record)
        self.item: Optional[Dict] = None
        self.map_error: Optional[str] = None
        try:
            self.item = map_isidore_to_zotero(record)
        except Exception as exc:  # noqa: BLE001 - notice isolée, on continue
            self.map_error = f"{type(exc).__name__}: {exc}"

    def matches_domain(self, domain: str) -> bool:
        """Filtre de sous-source sur le domaine analysé des URL."""
        return any(host_matches_domain(url_host(u), domain) for u in self.urls)

    def copy_item(self) -> Dict:
        """Copie profonde de l'item Zotero (chaque consommateur l'annote)."""
        return copy.deepcopy(self.item)


# === Flux de pages ===

def _response_description(response: Any) -> str:
    status = getattr(response, "status_code", "?")
    headers = getattr(response, "headers", None) or {}
    try:
        ctype = headers.get("Content-Type")
    except Exception:  # noqa: BLE001 - objet de réponse inhabituel
        ctype = None
    content = getattr(response, "content", b"") or b""
    if isinstance(content, str):
        content = content.encode("utf-8", "replace")
    head = content[:120].decode("utf-8", "replace").replace("\n", " ")
    return (f"statut {status}, Content-Type {ctype!r}, {len(content)} octets, "
            f"début {head!r}")


def _evict_cached(http: Any, response: Any) -> None:
    """Retire du cache HTTP une réponse illisible (best-effort)."""
    if not getattr(response, "from_cache", False):
        return
    session = getattr(http, "session", None)
    cache = getattr(session, "cache", None)
    request = getattr(response, "request", None)
    if cache is None or request is None:
        return
    try:
        cache.delete(requests=[request])
    except Exception:  # noqa: BLE001 - API de requests-cache variable
        logger.debug("[Isidore] Réponse illisible non retirée du cache", exc_info=True)


def _is_timeout(exc: BaseException) -> bool:
    """
    Vrai si l'échec est un délai dépassé : connexion ou lecture
    (``requests.Timeout``, budget de requête épuisé compris), ou délai de
    lecture survenu pendant le corps (``ConnectionError`` enveloppant un
    ``TimeoutError`` de urllib3).
    """
    try:
        from urllib3.exceptions import TimeoutError as _Urllib3Timeout
    except ImportError:  # pragma: no cover - urllib3 vient avec requests
        _Urllib3Timeout = ()
    current: Optional[BaseException] = exc
    for _ in range(5):
        if current is None:
            return False
        if isinstance(current, requests.Timeout):
            return True
        if _Urllib3Timeout and isinstance(current, _Urllib3Timeout):
            return True
        args = getattr(current, "args", ())
        wrapped = args[0] if args and isinstance(args[0], BaseException) else None
        current = wrapped or current.__cause__ or current.__context__
    return False


class _PageOutcome:
    __slots__ = ("records", "raw_count", "total", "page_block", "error",
                 "retried", "duplicates", "error_reason", "error_detail",
                 "retryable", "timed_out")

    def __init__(self):
        self.records: List[ScanRecord] = []
        self.raw_count = 0
        self.total: Optional[int] = None
        self.page_block: Optional[Dict] = None
        self.error: Optional[str] = None
        self.retried = False
        self.duplicates = 0
        # Motif (engine.models.REASON_*) et détail court de l'erreur, sans URL.
        self.error_reason: Optional[str] = None
        self.error_detail: Optional[str] = None
        # Faux pour un refus définitif (4xx hors 429) : une reprise différée
        # n'y changerait rien.
        self.retryable = True
        # Vrai si l'échec final est un délai dépassé (connexion ou lecture) :
        # pas de reprise différée (le service ne répond plus).
        self.timed_out = False


class IsidoreScan:
    """
    Flux de pages Isidore d'une requête, partagé par ses consommateurs.

    Args:
        query: requête plein texte (paramètre ``q``)
        extra_params: paramètres Isidore supplémentaires envoyés tels quels
            (aucun aujourd'hui : pas de filtre fiable, cf. docstring du module)
        replies: taille de page ; le registre passe
            ``config.ISIDORE_REPLIES_PER_PAGE`` (200 depuis OPT-12 : une
            requête pour toute la fenêtre) ; 100 n'est que la valeur par
            défaut de la classe construite directement
        window: fenêtre de résultats servie (``ISIDORE_RESULT_WINDOW`` ; 0 :
            pas de règle de fenêtre)
        max_records: borne de notices distinctes (``ISIDORE_SCAN_MAX_RECORDS``)
        error_retry_after: âge minimal (secondes) d'une erreur de page pour
            qu'un consommateur arrivé après elle reprenne la page
            (``ScanRegistry.error_retry_after_s``)
        deferred_retry_after: reprise différée d'une page en échec (OPT-09) :
            délai (secondes) après lequel le flux retente UNE fois la page,
            les consommateurs attendant l'issue et la partageant ; ``None``
            ou négatif : aucune reprise différée (défaut : un flux créé hors
            d'une moisson, par un registre implicite, garde la seule règle
            de reprise par un consommateur ultérieur)
        deferred_retry_budget: budget (secondes) de la tentative différée,
            connexion et attente des octets comprises
            (``config.ISIDORE_DEFERRED_RETRY_BUDGET_S`` par défaut)
    """

    # Marge (secondes) laissée avant l'échéance de l'exécution par la reprise
    # différée : les autres sources publient, la moisson n'est pas annulée.
    deferred_deadline_margin_s = 1.0

    def __init__(self, query: str, extra_params: Tuple[Tuple[str, str], ...] = (),
                 replies: int = 100, window: int = 200, max_records: int = 3000,
                 error_retry_after: float = 5.0,
                 deferred_retry_after: Optional[float] = None,
                 deferred_retry_budget: Optional[float] = None):
        self.query = query
        self.extra_params = tuple(extra_params)
        self.replies = max(1, int(replies))
        self.window = max(0, int(window))
        self.max_records = max(1, int(max_records))
        self.error_retry_after = max(0.0, float(error_retry_after))
        self.deferred_retry_after: Optional[float] = (
            None if deferred_retry_after is None or deferred_retry_after < 0
            else float(deferred_retry_after))
        self.deferred_retry_budget = max(0.1, float(
            config.ISIDORE_DEFERRED_RETRY_BUDGET_S if deferred_retry_budget is None
            else deferred_retry_budget))
        # Garde-fou sur les pages : une page normale apporte `replies` notices
        # nouvelles ; une réponse pathologique (une notice nouvelle par page)
        # ne peut pas prolonger le parcours au-delà de ce nombre de pages.
        self.max_pages = max(1, math.ceil(self.max_records / self.replies))
        self.created_at = time.monotonic()
        self.last_used = self.created_at

        self._cond = threading.Condition()
        self._records: List[ScanRecord] = []
        self._seen: set = set()
        self._fetching = False
        self._next_page = 1

        self.finished = False
        self.end_reason: Optional[str] = None
        self.error: Optional[str] = None
        # Motif et détail court de la dernière erreur (statut des sources).
        self.error_reason: Optional[str] = None
        self.error_detail: Optional[str] = None
        # Horodatage (monotone) et rang de la dernière erreur de page : un
        # consommateur qui a commencé sa lecture avant elle la partage, un
        # consommateur arrivé après peut reprendre la page (cf. _may_resume).
        self.error_at: Optional[float] = None
        self._errors = 0
        self.error_resumes = 0
        # Reprises différées (OPT-09) : pages déjà retentées par le flux (au
        # plus une fois chacune), reprises tentées et reprises réussies.
        self._deferred_pages: set = set()
        self.deferred_retries = 0
        self.deferred_recovered = 0
        self.total_available: Optional[int] = None
        self.pages_downloaded = 0
        self.records_received = 0
        self.duplicates = 0
        self.retried_pages = 0
        self.mapping_errors = 0
        self.truncated = 0

    # --- état ---

    @property
    def window_reached(self) -> bool:
        return self.end_reason in _WINDOW_REASONS

    def snapshot(self) -> Dict[str, Any]:
        """État du flux (lecture sous verrou)."""
        with self._cond:
            return {
                "query": self.query,
                "params": dict(self.extra_params),
                "replies": self.replies,
                "window": self.window,
                "max_records": self.max_records,
                "pages_downloaded": self.pages_downloaded,
                "records": len(self._records),
                "records_received": self.records_received,
                "duplicates": self.duplicates,
                "retried_pages": self.retried_pages,
                "mapping_errors": self.mapping_errors,
                "truncated": self.truncated,
                "total_available": self.total_available,
                "finished": self.finished,
                "end_reason": self.end_reason,
                "error": self.error,
                "error_reason": self.error_reason,
                "error_detail": self.error_detail,
                "error_resumes": self.error_resumes,
                "deferred_retries": self.deferred_retries,
                "deferred_recovered": self.deferred_recovered,
            }

    # --- lecture ---

    def iter_records(self, http: Any, consumer: str = "Isidore",
                     counters: Optional[Dict[str, int]] = None,
                     cancel: Any = None) -> Iterator[ScanRecord]:
        """
        Parcourt le flux depuis le début, en téléchargeant les pages manquantes.

        Args:
            http: client HTTP du consommateur (utilisé s'il doit télécharger
                la page suivante ; le flux ne conserve aucune référence au
                client, pour que le registre attaché au client puisse être
                libéré avec lui)
            consumer: nom du consommateur (journal)
            counters: dict facultatif incrémenté de ``downloads`` (pages
                téléchargées par ce consommateur)
            cancel: signal d'annulation (``CancelSignal`` ou objet à
                ``is_set()``) : vérifié avant chaque page à télécharger et
                pendant l'attente d'une page en cours ; lève
                ``OperationCancelled``.
        """
        cancel = CancelSignal.coerce(cancel)
        with self._cond:
            # Erreurs déjà survenues quand ce consommateur commence sa lecture.
            errors_at_start = self._errors
        index = 0
        while self._ensure(index, http, consumer, counters, errors_at_start,
                           cancel):
            yield self._records[index]
            index += 1

    def _may_resume(self, errors_at_start: int, now: float) -> bool:
        """
        Vrai si un consommateur peut reprendre la page en erreur (sous verrou).

        Conditions : le flux s'est arrêté sur une erreur, cette erreur est
        antérieure au début de la lecture du consommateur (aucune erreur
        nouvelle depuis : un consommateur déjà engagé la partage, et un
        consommateur dont la reprise vient d'échouer ne recommence pas), et
        elle date d'au moins ``error_retry_after`` secondes (pendant une panne
        franche, les consommateurs qui se suivent de près ne multiplient pas
        les tentatives).
        """
        return (self.end_reason == END_ERROR
                and self._errors == errors_at_start
                and self.error_at is not None
                and now - self.error_at >= self.error_retry_after)

    def _resume(self, consumer: str, now: float) -> None:
        """Rouvre le flux sur la page en erreur (sous verrou)."""
        self.error_resumes += 1
        logger.warning(
            f"[Isidore] Reprise du parcours après erreur (q={self.query[:50]}, "
            f"page {self._next_page}, erreur survenue il y a "
            f"{now - self.error_at:.0f} s, demandée par {consumer}) : {self.error}"
        )
        self.finished = False
        self.end_reason = None
        self.error = None
        self.error_reason = None
        self.error_detail = None

    def _ensure(self, index: int, http: Any, consumer: str,
                counters: Optional[Dict[str, int]], errors_at_start: int = 0,
                cancel: Optional[CancelSignal] = None) -> bool:
        """Vrai quand la notice ``index`` est disponible, faux en fin de flux."""
        while True:
            with self._cond:
                while True:
                    now = time.monotonic()
                    self.last_used = now
                    if index < len(self._records):
                        return True
                    if self.finished:
                        if not self._may_resume(errors_at_start, now):
                            return False
                        # Aucun téléchargement en cours (flux terminé) : ce
                        # consommateur reprend la page qui a échoué.
                        if cancel is not None:
                            cancel.check()
                        self._resume(consumer, now)
                    if not self._fetching:
                        # Annulation vérifiée avant toute nouvelle page.
                        if cancel is not None:
                            cancel.check()
                        self._fetching = True
                        page = self._next_page
                        break
                    # Un autre consommateur télécharge la page : on l'attend
                    # plutôt que de la redemander (une requête par page) ;
                    # attente interruptible par l'annulation.
                    if cancel is None:
                        self._cond.wait()
                    else:
                        self._cond.wait(CANCEL_WAIT_SLICE)
                        cancel.check()
            try:
                outcome = self._fetch_page(http, page, consumer)
                if outcome.error is not None and self._defer_retry(
                        page, outcome, consumer, cancel):
                    # Reprise différée (OPT-09) : ce consommateur garde le
                    # téléchargement (les autres continuent d'attendre la
                    # page), patiente, puis retente la page une seule fois ;
                    # seule l'issue de cette reprise est appliquée au flux.
                    self._pause(self.deferred_retry_after, cancel)
                    budget = self._deferred_budget(cancel)
                    if budget is None:
                        logger.info(
                            f"[Isidore] Page {page} : reprise différée "
                            f"abandonnée, échéance de l'exécution trop proche")
                    else:
                        outcome = self._fetch_page(http, page, consumer,
                                                   deferred=True, budget=budget)
                    if outcome.error is None:
                        with self._cond:
                            self.deferred_recovered += 1
                        logger.warning(
                            f"[Isidore] Page {page} obtenue à la reprise "
                            f"différée (q={self.query[:50]}, demandée par "
                            f"{consumer})")
            except BaseException:
                # Interruption (annulation, KeyboardInterrupt…) : libère le
                # téléchargement sans clore le flux (la page n'est ni acquise
                # ni en erreur), pour ne pas bloquer les autres consommateurs,
                # puis propage.
                with self._cond:
                    self._fetching = False
                    self._cond.notify_all()
                raise
            with self._cond:
                try:
                    self._apply(page, outcome, consumer)
                    if counters is not None and outcome.error is None:
                        counters["downloads"] = counters.get("downloads", 0) + 1
                finally:
                    self._fetching = False
                    self._cond.notify_all()

    # --- téléchargement ---

    def _params(self, page: int) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "q": self.query,
            "replies": self.replies,
            "page": page,
            "output": "json",
        }
        for k, v in self.extra_params:
            params[k] = v
        return params

    @staticmethod
    def _get(http: Any, params: Dict[str, Any], fresh: bool,
             single_attempt: bool = False, budget: Optional[float] = None) -> Any:
        if not fresh:
            return http.get(config.ISIDORE_SEARCH_URL, params=params)
        if single_attempt:
            kwargs: Dict[str, Any] = {"use_cache": False, "max_attempts": 1}
            if budget is not None:
                kwargs["budget"] = budget
            try:
                return http.get(config.ISIDORE_SEARCH_URL, params=params, **kwargs)
            except TypeError:
                pass  # client simplifié sans option de tentatives ni de budget
        try:
            return http.get(config.ISIDORE_SEARCH_URL, params=params, use_cache=False)
        except TypeError:
            # Client simplifié (tests, doublures) sans option de cache.
            return http.get(config.ISIDORE_SEARCH_URL, params=params)

    def _fetch_page(self, http: Any, page: int, consumer: str,
                    deferred: bool = False,
                    budget: Optional[float] = None) -> _PageOutcome:
        """
        Télécharge une page et convertit un échec en issue d'erreur (motif,
        détail court, caractère définitif, délai dépassé). Une interruption
        (``BaseException`` : annulation…) est propagée telle quelle.
        """
        try:
            return self._download(http, page, consumer, deferred=deferred,
                                  budget=budget)
        except IsidoreScanError as exc:
            outcome = _PageOutcome()
            outcome.error = str(exc)
            outcome.retried = exc.retried
            outcome.error_reason = REASON_INVALID_BODY
            outcome.error_detail = f"page {page} : réponse illisible (corps non JSON ou structure inattendue)"
            return outcome
        except Exception as exc:  # noqa: BLE001 - erreur réseau après la politique HTTP
            outcome = _PageOutcome()
            outcome.error = f"page {page} : {type(exc).__name__}: {exc}"
            reason, detail = classify_exception(exc)
            outcome.error_reason = reason
            outcome.error_detail = f"page {page} : {detail}"
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if (isinstance(status, int) and 400 <= status < 500
                    and status not in RETRYABLE_STATUS):
                outcome.retryable = False
            outcome.timed_out = _is_timeout(exc)
            return outcome

    def _defer_retry(self, page: int, outcome: _PageOutcome, consumer: str,
                     cancel: Optional[CancelSignal] = None) -> bool:
        """
        Vrai si le flux doit retenter la page en échec après
        ``deferred_retry_after`` secondes (OPT-09) : reprise différée activée,
        erreur non définitive et autre qu'un délai dépassé, page pas encore
        retentée, et assez de temps avant l'échéance de l'exécution pour
        l'attente, la tentative entière (``deferred_retry_budget``) et la
        marge ``deferred_deadline_margin_s`` (sinon la reprise annulerait
        toute la moisson au lieu de laisser publier les autres sources).
        Réserve alors la reprise (au plus une par page) et l'annonce au
        journal.

        Après un délai dépassé, pas de reprise : les tentatives de la
        politique commune ont déjà attendu chacune un délai complet (service
        muet ou injoignable) ; une de plus ne ferait qu'allonger la panne du
        délai de reprise et d'un délai réseau, pour la même issue.
        """
        if self.deferred_retry_after is None or not outcome.retryable:
            return False
        if outcome.timed_out:
            logger.info(
                f"[Isidore] Page {page} en échec sur délai dépassé : pas de "
                f"reprise différée (le service ne répond plus)")
            return False
        remaining = cancel.remaining() if cancel is not None else None
        needed = (self.deferred_retry_after + self.deferred_retry_budget
                  + self.deferred_deadline_margin_s)
        if remaining is not None and remaining < needed:
            logger.info(
                f"[Isidore] Page {page} en échec : pas de reprise différée, "
                f"échéance de l'exécution trop proche ({remaining:.1f} s "
                f"< {needed:.1f} s)")
            return False
        with self._cond:
            if page in self._deferred_pages:
                return False
            self._deferred_pages.add(page)
            self.deferred_retries += 1
        logger.warning(
            f"[Isidore] Page {page} en échec (q={self.query[:50]}, demandée par "
            f"{consumer}) : {outcome.error} ; nouvelle tentative unique dans "
            f"{self.deferred_retry_after:g} s, partagée par les consommateurs "
            f"du flux"
        )
        return True

    def _deferred_budget(self, cancel: Optional[CancelSignal]) -> Optional[float]:
        """
        Budget de la tentative différée : ``deferred_retry_budget``, ramené
        pour finir ``deferred_deadline_margin_s`` avant l'échéance de
        l'exécution ; ``None`` s'il ne reste plus de temps.
        """
        remaining = cancel.remaining() if cancel is not None else None
        if remaining is None:
            return self.deferred_retry_budget
        budget = min(self.deferred_retry_budget,
                     remaining - self.deferred_deadline_margin_s)
        return budget if budget > 0 else None

    @staticmethod
    def _pause(seconds: float, cancel: Optional[CancelSignal]) -> None:
        """
        Attente de la reprise différée, interruptible par ``cancel``
        (``OperationCancelled``). Faite de tranches de ``time.sleep`` : le
        banc de mesure la compte et la met à l'échelle comme les autres
        attentes.
        """
        if cancel is not None:
            cancel.check()
            if seconds > 0 and cancel.wait(seconds):
                cancel.check()
            return
        left = max(0.0, float(seconds))
        while left > 0:
            step = min(left, CANCEL_WAIT_SLICE)
            t0 = time.monotonic()
            time.sleep(step)
            left -= max(step, time.monotonic() - t0)

    def _download(self, http: Any, page: int, consumer: str,
                  deferred: bool = False,
                  budget: Optional[float] = None) -> _PageOutcome:
        """
        Télécharge et décode une page (hors verrou, un seul fil à la fois).

        ``deferred`` : reprise différée d'une page en échec (OPT-09) : une
        seule tentative réseau, sans cache (ni lecture ni écriture), bornée
        par ``budget`` (secondes), et pas de seconde tentative sur un corps
        illisible.
        """
        logger.info(
            f"[Isidore] Page {page} (q={self.query[:50]}, flux partagé, "
            f"demandée par {consumer}, déjà {len(self._records)} notices distinctes"
            f"{', reprise différée' if deferred else ''})"
        )
        params = self._params(page)
        outcome = _PageOutcome()
        response = self._get(http, params, fresh=deferred, single_attempt=deferred,
                             budget=budget if deferred else None)
        try:
            data = response.json()
        except ValueError:
            if deferred:
                raise IsidoreScanError(
                    f"page {page} : corps de réponse non JSON à la reprise "
                    f"différée ({_response_description(response)})", retried=True,
                ) from None
            first = _response_description(response)
            _evict_cached(http, response)
            logger.warning(
                f"[Isidore] Page {page} : corps de réponse non JSON ({first}) ; "
                f"nouvelle tentative sans cache"
            )
            outcome.retried = True
            response = self._get(http, params, fresh=True)
            try:
                data = response.json()
            except ValueError:
                raise IsidoreScanError(
                    f"page {page} : corps de réponse non JSON à deux reprises "
                    f"({_response_description(response)})", retried=True,
                ) from None
        if not isinstance(data, dict) or not isinstance(data.get("response", {}), dict):
            raise IsidoreScanError(
                f"page {page} : structure de réponse inattendue "
                f"({type(data).__name__})", retried=outcome.retried,
            )
        replies_block = data.get("response", {}).get("replies", {})
        if isinstance(replies_block, dict):
            meta = replies_block.get("meta", {})
            if isinstance(meta, dict):
                items_str = meta.get("@items") or meta.get("items")
                try:
                    outcome.total = int(items_str) if items_str is not None else None
                except (TypeError, ValueError):
                    outcome.total = None
            block = replies_block.get("page")
            if isinstance(block, dict):
                outcome.page_block = block
        raw = extract_replies(data)
        outcome.raw_count = len(raw)
        # Seul le fil qui télécharge modifie `_seen` (sous verrou dans
        # `_apply`) : la lecture ici, hors verrou, ne peut pas être concurrente
        # d'une écriture. Seules les notices nouvelles sont mappées.
        batch: set = set()
        # Rang natif : position dans la réponse brute (pages pleines de
        # `replies` notices : une page n'est demandée qu'après une page
        # pleine), doublons compris.
        offset = (page - 1) * self.replies
        for index, record in enumerate(raw):
            key = record_key(record)
            if key in self._seen or key in batch:
                outcome.duplicates += 1
                continue
            batch.add(key)
            outcome.records.append(ScanRecord(record, page, offset + index + 1))
        return outcome

    def _apply(self, page: int, outcome: _PageOutcome, consumer: str) -> None:
        """Intègre une page au flux et décide de la fin (sous verrou)."""
        if outcome.retried:
            self.retried_pages += 1
        if outcome.error is not None:
            # Les pages acquises restent ; `_next_page` désigne toujours la
            # page qui a échoué, qu'un consommateur ultérieur peut reprendre.
            self.error = outcome.error
            self.error_reason = outcome.error_reason
            self.error_detail = outcome.error_detail
            self.error_at = time.monotonic()
            self._errors += 1
            self.finished = True
            self.end_reason = END_ERROR
            logger.error(
                f"[Isidore] Parcours interrompu sur erreur (q={self.query[:50]}, "
                f"demandée par {consumer}) : {self.error} ; un consommateur "
                f"ultérieur reprendra la page {page} au-delà de "
                f"{self.error_retry_after:g} s"
            )
            return
        self.pages_downloaded += 1
        self.records_received += outcome.raw_count
        self.duplicates += outcome.duplicates
        if self.total_available is None and outcome.total is not None:
            self.total_available = outcome.total
            logger.info(f"[Isidore] {outcome.total} résultats disponibles au total")
        new = 0
        truncated = 0
        for rec in outcome.records:
            if rec.key in self._seen:
                self.duplicates += 1
                continue
            if len(self._records) >= self.max_records:
                # Borne stricte : aucune notice au-delà de max_records.
                truncated += 1
                continue
            self._seen.add(rec.key)
            rec.position = len(self._records)
            self._records.append(rec)
            new += 1
            if rec.map_error:
                self.mapping_errors += 1
                logger.warning(f"[Isidore] Échec mapping {rec.key}: {rec.map_error}")
        self.truncated += truncated
        self._next_page = page + 1
        reason = self._end_reason_after(page, outcome, new, truncated)
        if reason is not None:
            self.finished = True
            self.end_reason = reason
            logger.info(
                f"[Isidore] Fin du parcours (q={self.query[:50]}) : "
                f"{_REASON_LABELS[reason]} ; {len(self._records)} notices "
                f"distinctes en {self.pages_downloaded} page(s)"
            )

    def _end_reason_after(self, page: int, outcome: _PageOutcome,
                          new: int, truncated: int = 0) -> Optional[str]:
        if outcome.raw_count == 0:
            return END_EXHAUSTED
        if truncated:
            # Des notices distinctes de cette page ont été écartées par la
            # borne : la fin est due à la borne, même si la page est partielle
            # ou atteint le total annoncé (sinon « épuisé » masquerait la
            # troncature et la sous-source ne serait pas signalée incomplète).
            return END_BOUND
        if new == 0:
            # Page entièrement déjà vue : l'API répète la dernière fenêtre.
            return END_REPEAT
        block = outcome.page_block or {}
        position = page * self.replies
        total = self.total_available
        try:
            current = int(block["@current"]) if "@current" in block else None
        except (TypeError, ValueError):
            current = None
        if current is not None and current != page:
            # Isidore ramène une page hors fenêtre à la dernière page servie.
            return END_WINDOW
        if outcome.raw_count < self.replies:
            # Page partielle : fin des résultats, sauf si Isidore en annonce
            # davantage (taille de page supérieure à la fenêtre servie).
            served = (page - 1) * self.replies + outcome.raw_count
            return END_EXHAUSTED if (total is None or served >= total) else END_WINDOW
        if total is not None and position >= total:
            return END_EXHAUSTED
        if len(self._records) >= self.max_records:
            return END_BOUND
        if "@current" in block and "@next" not in block:
            # Dernière page servie (live : page 10 de replies=20 → @previous,
            # @current, pas de @next) : inutile de demander la suivante.
            return END_WINDOW if (total is None or total > position) else END_EXHAUSTED
        if self.window and position >= self.window:
            return END_WINDOW
        if self.pages_downloaded >= self.max_pages:
            return END_BOUND
        return None


_REASON_LABELS = {
    END_EXHAUSTED: "résultats épuisés",
    END_WINDOW: "fenêtre de résultats du fournisseur atteinte",
    END_REPEAT: "page répétée (fenêtre de résultats du fournisseur atteinte)",
    END_BOUND: "borne ISIDORE_SCAN_MAX_RECORDS atteinte",
    END_ERROR: "erreur",
}


# === Registre des flux d'une exécution ===

class ScanRegistry:
    """
    Flux Isidore d'une exécution, indexés par (requête, paramètres, taille
    de page). Sûr entre threads.

    Un registre attaché à un client HTTP de longue durée (client partagé par
    défaut des lookups) ne grossit pas sans fin : au-delà de ``max_flows``
    flux, les moins récemment utilisés sont oubliés, et un flux plus vieux
    que ``max_age_s`` est recréé à la demande suivante (résultats frais).
    Une moisson dure bien moins que ``max_age_s`` et ouvre un flux par
    requête (un seul en pratique).

    Un flux arrêté sur une erreur n'est pas gardé en l'état : dès que
    l'erreur date d'au moins ``error_retry_after_s`` secondes, le consommateur
    suivant reprend la page qui a échoué (cf. ``IsidoreScan._may_resume``).
    Cinq secondes : au-delà des reprises de la politique HTTP (backoff 1 s
    puis 2 s), assez court pour qu'une panne passagère ne prive pas les
    consommateurs lancés ensuite (appel ultérieur sur un client de longue
    durée, source démarrée tard) ; pendant une panne franche, les
    consommateurs qui se suivent de près partagent l'erreur.

    Registre d'une moisson (``for_run``, topologie plate d'OPT-09) : la
    source directe et les sous-sources démarrent ensemble, donc toutes
    « engagées avant l'échec » ; la règle précédente seule leur ferait
    partager une erreur passagère pour toute l'exécution. Le flux y fait donc
    lui-même une reprise différée : il attend ``deferred_retry_after``
    secondes (``config.ISIDORE_DEFERRED_RETRY_S``) puis retente UNE fois la
    page en échec ; tous les consommateurs attendent et partagent l'issue,
    quel que soit l'instant de leur démarrage (nombre de workers, durée des
    autres sources). Coût borné pendant une panne franche à réponse rapide
    (5xx, connexion refusée) : une tentative de plus par page, le délai et au
    plus ``config.ISIDORE_DEFERRED_RETRY_BUDGET_S`` ; aucun surcoût pour un
    service muet (délai dépassé : pas de reprise différée).

    Args:
        replies, window, max_records: surcharges des constantes de config
            (défaut : valeurs courantes de ``config`` à la création du flux)
        deferred_retry_after: délai de la reprise différée des flux créés
            (``None`` : aucune, défaut d'un registre implicite)
    """

    max_flows = 32
    max_age_s = 3600.0
    error_retry_after_s = 5.0

    def __init__(self, replies: Optional[int] = None, window: Optional[int] = None,
                 max_records: Optional[int] = None,
                 deferred_retry_after: Optional[float] = None):
        self._lock = threading.Lock()
        self._flows: Dict[tuple, IsidoreScan] = {}
        self.replies = replies
        self.window = window
        self.max_records = max_records
        self.deferred_retry_after = deferred_retry_after

    @classmethod
    def for_run(cls) -> "ScanRegistry":
        """
        Registre d'une moisson : reprise différée activée selon
        ``config.ISIDORE_DEFERRED_RETRY_S`` (négatif : désactivée).
        """
        delay = config.ISIDORE_DEFERRED_RETRY_S
        return cls(deferred_retry_after=delay if delay >= 0 else None)

    def get_scan(self, query: str,
                 extra_params: Iterable[Tuple[str, Any]] = ()) -> Tuple[IsidoreScan, bool]:
        """
        Flux de la requête (créé au besoin).

        Returns:
            (flux, partagé) : ``partagé`` vaut True si le flux existait déjà.
        """
        params = tuple(sorted((str(k), str(v)) for k, v in extra_params))
        replies = self.replies if self.replies is not None else config.ISIDORE_REPLIES_PER_PAGE
        window = self.window if self.window is not None else config.ISIDORE_RESULT_WINDOW
        max_records = (self.max_records if self.max_records is not None
                       else config.ISIDORE_SCAN_MAX_RECORDS)
        key = (query, params, int(replies), int(window), int(max_records))
        now = time.monotonic()
        with self._lock:
            scan = self._flows.get(key)
            if scan is not None and now - scan.created_at <= self.max_age_s:
                scan.last_used = now
                return scan, True
            scan = IsidoreScan(query, params, replies=replies, window=window,
                               max_records=max_records,
                               error_retry_after=self.error_retry_after_s,
                               deferred_retry_after=self.deferred_retry_after)
            self._flows[key] = scan
            self._prune()
            return scan, False

    def _prune(self) -> None:
        """
        Oublie les flux les moins récemment utilisés au-delà de ``max_flows``
        (terminés ou non : un consommateur en cours garde sa propre
        référence au flux et le termine normalement).
        """
        excess = len(self._flows) - self.max_flows
        if excess <= 0:
            return
        oldest = sorted(self._flows.items(), key=lambda kv: kv[1].last_used)
        for k, _scan in oldest[:excess]:
            del self._flows[k]

    def flows(self) -> List[IsidoreScan]:
        """Flux connus (copie de la liste)."""
        with self._lock:
            return list(self._flows.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._flows)


_REGISTRIES: "weakref.WeakKeyDictionary[Any, ScanRegistry]" = weakref.WeakKeyDictionary()
_REGISTRIES_LOCK = threading.Lock()


def registry_for(http_client: Any, explicit: Optional[ScanRegistry] = None) -> ScanRegistry:
    """
    Registre de flux d'une exécution.

    Args:
        http_client: client HTTP partagé par la moisson ; le registre lui est
            attaché (clé faible) et disparaît avec lui.
        explicit: registre fourni par un contexte d'exécution (prioritaire).

    Un client qui n'accepte pas de référence faible reçoit un registre
    privé (aucun partage, comportement sûr).
    """
    if explicit is not None:
        return explicit
    with _REGISTRIES_LOCK:
        try:
            registry = _REGISTRIES.get(http_client)
            if registry is None:
                registry = ScanRegistry()
                _REGISTRIES[http_client] = registry
            return registry
        except TypeError:
            logger.debug("[Isidore] Client HTTP sans référence faible : flux non partagé")
            return ScanRegistry()


# === Statut d'un consommateur ===

@dataclass
class IsidoreScanStatus:
    """
    Statut du parcours Isidore d'un consommateur (source directe ou
    sous-source), lisible par l'orchestrateur (``connector.last_scan_status``).
    """

    source: str
    query: str
    cap: int
    retained: int = 0
    scanned: int = 0
    pages_read: int = 0
    pages_downloaded: int = 0
    flow_pages_downloaded: int = 0
    flow_records: int = 0
    total_available: Optional[int] = None
    shared_flow: bool = False
    cap_reached: bool = False
    window_reached: bool = False
    bound_reached: bool = False
    exhausted: bool = False
    end_reason: Optional[str] = None
    error: Optional[str] = None
    # Motif (engine.models.REASON_*) et détail court de l'erreur, sans URL.
    error_reason: Optional[str] = None
    error_detail: Optional[str] = None
    mapping_errors: int = 0
    # Parcours arrêté par l'échéance souple de la moisson (OPT-09 ter,
    # ``RunContext.soft_stop``) après des notices retenues, rendues.
    soft_stopped: bool = False
    # Rangs natifs (OPT-12) : position 1..n dans la réponse de l'API (la
    # fenêtre servie par Isidore, dans son ordre, doublons compris) de chaque
    # notice retenue, alignée sur les items renvoyés. Isidore n'expose aucun
    # score (vérifié le 2026-09-29).
    ranks: List[int] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        """Plafond non atteint à cause de la fenêtre, de la borne, d'une erreur
        ou de l'échéance souple."""
        return (not self.cap_reached
                and (self.window_reached or self.bound_reached
                     or self.error is not None or self.soft_stopped))

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Statut lisible et compact : les rangs (une entrée par notice) sont
        # portés par le compte rendu de collecte (SearchReport.native_ranks).
        d.pop("ranks", None)
        d["incomplete"] = self.incomplete
        return d

    def describe(self) -> str:
        """Phrase de journal expliquant pourquoi le plafond n'est pas atteint."""
        base = (f"[{self.source}] {self.retained} notice(s) retenue(s) sur "
                f"{self.scanned} parcourue(s) dans Isidore (plafond {self.cap} "
                f"non atteint)")
        if self.soft_stopped:
            return (f"{base} : parcours interrompu par l'échéance souple "
                    f"({self.pages_read} page(s) lue(s)) — sous-source incomplète")
        if self.error is not None:
            return f"{base} : parcours interrompu par une erreur ({self.error}) — sous-source incomplète"
        if self.window_reached:
            announced = (f" sur {self.total_available} annoncés"
                         if self.total_available else "")
            return (f"{base} : fenêtre de résultats du fournisseur atteinte "
                    f"(Isidore n'a servi que {self.flow_records} résultats "
                    f"distincts{announced}) — sous-source incomplète")
        if self.bound_reached:
            return (f"{base} : borne ISIDORE_SCAN_MAX_RECORDS atteinte "
                    f"({self.flow_records} notices) — sous-source incomplète")
        return base


# === Consommation ===

def consume(http: Any, query: str, max_results: int, *, source_label: str = "Isidore",
            url_marker: Optional[str] = None, source_filter: Optional[str] = None,
            from_date: Optional[str] = None, to_date: Optional[str] = None,
            scan_registry: Optional[ScanRegistry] = None,
            cancel: Any = None,
            soft_stop: Optional[Callable[[BaseException], bool]] = None,
            ) -> Tuple[List[Dict], IsidoreScanStatus]:
    """
    Lit le flux partagé de ``query`` et retient les notices d'un consommateur.

    Filtres, dans l'ordre historique : domaine d'URL (``url_marker``,
    prioritaire) ou nom de source (``source_filter``, sous-chaîne insensible
    à la casse), puis années (``from_date`` / ``to_date``, notices sans année
    rejetées). Arrêt au plafond ``max_results`` ; sinon à la fin du flux.
    ``cancel`` (signal d'annulation) interrompt le parcours avant toute
    nouvelle page (``OperationCancelled``).

    ``soft_stop`` (OPT-09 ter) : prédicat ``Connector.soft_stop_applies``.
    Si l'interruption vient de la SEULE échéance souple de la moisson et que
    des notices sont déjà retenues, le parcours s'arrête sans nouvelle page
    et les rend (``status.soft_stopped``) ; sinon l'annulation est propagée
    (aucune notice retenue, client, échéance dure, prédicat absent).

    Returns:
        (items Zotero copiés, statut du consommateur ; ``status.ranks`` donne
        la position de chaque item dans le flux, alignée sur la liste)
    """
    from_year = int(from_date[:4]) if from_date and len(from_date) >= 4 else None
    to_year = int(to_date[:4]) if to_date and len(to_date) >= 4 else None
    domain = normalize_domain(url_marker) if url_marker else None
    wanted_source = source_filter.lower() if source_filter else None

    items: List[Dict] = []
    if max_results <= 0:
        # Plafond nul : aucune requête, aucun flux ouvert.
        status = IsidoreScanStatus(source=source_label, query=query, cap=max_results)
        status.cap_reached = True
        return items, status
    registry = registry_for(http, scan_registry)
    scan, shared = registry.get_scan(query)
    status = IsidoreScanStatus(source=source_label, query=query,
                               cap=max_results, shared_flow=shared)
    counters: Dict[str, int] = {}
    if shared:
        logger.info(
            f"[{source_label}] Parcours Isidore partagé : relecture du flux "
            f"déjà ouvert pour « {query[:50]} »"
        )
    try:
        for rec in scan.iter_records(http, consumer=source_label, counters=counters,
                                     cancel=cancel):
            status.scanned += 1
            status.pages_read = max(status.pages_read, rec.page)
            if domain is not None:
                if not rec.matches_domain(domain):
                    continue
            elif wanted_source is not None:
                if not rec.source_name or wanted_source not in rec.source_name.lower():
                    continue
            if from_year or to_year:
                if rec.year is None:
                    continue
                if from_year and rec.year < from_year:
                    continue
                if to_year and rec.year > to_year:
                    continue
            if rec.item is None:
                status.mapping_errors += 1
                continue
            items.append(rec.copy_item())
            # Rang natif : position de la notice dans la réponse de l'API
            # (fenêtre servie par Isidore, doublons compris), la même pour tous
            # les consommateurs.
            status.ranks.append(rec.native_rank or rec.position + 1)
            if len(items) >= max_results:
                status.cap_reached = True
                logger.info(f"[{source_label}] Plafond {max_results} atteint, arrêt")
                break
    except OperationCancelled as exc:
        # Échéance souple (OPT-09 ter, RunContext.soft_stop) : les notices déjà
        # retenues sont rendues, aucune page de plus ; toute autre annulation
        # (ou un arrêt sans notice retenue) est propagée.
        if not items or soft_stop is None or not soft_stop(exc):
            raise
        status.soft_stopped = True
        logger.warning(
            f"[{source_label}] Parcours Isidore interrompu par l'échéance "
            f"souple : {len(items)} notice(s) retenue(s) conservée(s)")

    snap = scan.snapshot()
    status.retained = len(items)
    status.pages_downloaded = counters.get("downloads", 0)
    status.flow_pages_downloaded = snap["pages_downloaded"]
    status.flow_records = snap["records"]
    status.total_available = snap["total_available"]
    if not status.cap_reached:
        reason = snap["end_reason"]
        status.end_reason = reason
        status.window_reached = reason in _WINDOW_REASONS
        status.bound_reached = reason == END_BOUND
        status.exhausted = reason == END_EXHAUSTED
        status.error = snap["error"]
        status.error_reason = snap["error_reason"]
        status.error_detail = snap["error_detail"]
        if status.incomplete:
            log = logger.error if status.error is not None else logger.warning
            log(status.describe())
    return items, status


def annotate_proxy_source(items: List[Dict], label: str) -> None:
    """
    Place ``label`` en tête de la ligne ``Sources:`` de chaque item (Isidore
    reste cité comme source intermédiaire : compatibilité de la
    déduplication et des statistiques par source).
    """
    for item in items:
        extra = item.get("extra", "")
        new_lines = []
        for line in extra.split("\n"):
            if line.startswith("Sources:"):
                sources = [s.strip() for s in line[len("Sources:"):].split(",")]
                if label not in sources:
                    sources.insert(0, label)
                new_lines.append("Sources: " + ", ".join(sources))
            else:
                new_lines.append(line)
        item["extra"] = "\n".join(new_lines)
