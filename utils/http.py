"""
Client HTTP commun : une seule politique de réessai, de politesse et de cache.

Ce module est le point de passage unique des accès réseau sortants du
moissonneur : ``HTTPClient`` (connecteurs HAL, Isidore, CrossRef, Semantic
Scholar), ``utils.http_scholarly.get_json`` (OpenAlex, CORE, SerpAPI, CrossRef
pour la résolution de DOI) et ``utils.lookups`` (fetch_by_doi,
search_by_author) appliquent tous la même politique, implémentée par
``policy_get`` :

- **4xx définitif** (400, 401, 403, 404, 405, 410, 422…) : une seule tentative,
  l'erreur HTTP est levée immédiatement (un nouvel essai ne changerait rien) ;
  seule exception, un changement d'identifiant et non un réessai (sprint
  « clés ») : un 401 ou un 403 reçu avec la clé d'API du client est suivi
  d'UNE requête avec la clé de réserve du `.env` quand ``utils.secrets`` le
  permet (``MOISSON_KEY_FALLBACK``, réserve différente) ; le refus est
  mémorisé et les requêtes suivantes partent directement avec la réserve ;
- **429 et transitoires** (500, 502, 503, 504, délai dépassé, erreur de
  connexion, connexion rompue en cours de lecture du corps) : au plus
  ``HTTP_MAX_RETRIES`` tentatives, ``Retry-After`` respecté (secondes ou date
  HTTP) tant qu'il ne dépasse pas ``HTTP_RETRY_AFTER_MAX`` (au-delà, abandon
  plutôt qu'un réessai plus tôt que demandé) et appliqué à tous les threads
  qui visent le même hôte avec le même compte, backoff exponentiel sinon,
  plafond cumulé des attentes ``HTTP_MAX_TOTAL_WAIT`` ; aucune attente après
  la dernière tentative ;
- **délais** : connexion et lecture distincts (tuple ``HTTP_TIMEOUT`` ; un
  délai scalaire fourni par un appelant ne vaut que pour la lecture, la
  connexion reste bornée par ``HTTP_CONNECT_TIMEOUT``), plus un budget global
  par requête logique (``HTTP_REQUEST_BUDGET``, tentatives, attentes de
  politesse, de place en vol et de réessai comprises) ;
- **politesse par hôte et par compte** : intervalle minimal entre deux
  départs de requête réseau vers un même hôte pour un même compte (la clé de
  compte est une empreinte de la clé d'API, jamais la clé elle-même, ni un
  jeton de pagination ; le compte SerpAPI est partagé avec
  MyWebIntelligence), réservé sous verrou pour que plusieurs threads ne
  doublent pas le débit, et plafond de requêtes en vol par hôte
  (``HTTP_MAX_IN_FLIGHT_PER_HOST``, ou plafond propre à l'hôte :
  ``HOST_MAX_IN_FLIGHT``) ; un hôte peut distinguer des familles de requêtes
  à politesse propre (CrossRef : listes et travail unique, OPT-09), et les
  limites annoncées par le serveur (en-têtes ``x-rate-limit-limit``,
  ``x-rate-limit-interval``, ``x-concurrency-limit``) durcissent la
  politesse configurée, sans jamais la relâcher ;
- **sommeil uniquement sur accès réseau réel** : une réponse que le cache
  requests-cache peut servir est lue sans aucune temporisation ;
- **fusion des requêtes identiques simultanées** (single-flight) : deux
  threads qui demandent la même URL avec les mêmes paramètres et le même
  compte au même moment ne produisent qu'un accès ;
- **compteurs par hôte** : requêtes logiques, accès réseau (une
  revalidation conditionnelle 304 en est un), hits et misses de cache,
  réessais, 429, erreurs, requêtes fusionnées, temps d'attente cumulé
  (``get_http_stats`` / ``reset_http_stats``).

Cache HTTP : option à trois états. ``use_cache=None`` (option absente) hérite
de la variable d'environnement ``MOISSON_CACHE`` (1/true/yes/on) ;
``True``/``False`` l'imposent. Le cache est persistant sur disque (SQLite via
requests-cache), TTL ``MOISSON_CACHE_TTL`` (défaut 86400 s = 24 h), répertoire
``MOISSON_CACHE_DIR``. Seules les réponses de succès et les redirections
stables sont mises en cache. Une requête peut refuser le cache
(``use_cache=False`` par requête, ex. solde SerpAPI toujours frais) : elle est
alors envoyée avec ``Cache-Control: no-store``, ni lue ni écrite en cache.
requests-cache ne retirant jamais une réponse expirée, ``prune_sqlite_cache``
(``HTTPClient.prune_cache``) borne le fichier : expirées, puis plus anciennes
au-delà d'un plafond, compactage éventuel (entretien du serveur MCP).

Annulation coopérative (OPT-07) : ``policy_get`` accepte un signal
d'annulation (``cancel``, cf. ``CancelSignal`` : événement et échéance
monotone). Il est vérifié avant chaque tentative, pendant l'attente d'une
place en vol, du créneau de politesse et du délai entre deux tentatives
(attentes interruptibles), et les délais réseau sont bornés par l'échéance.
Une opération annulée lève ``OperationCancelled`` ; une requête déjà partie
se termine dans la limite de ses délais (une connexion ouverte ne peut pas
être interrompue de l'extérieur). ``HTTPClient.cancel_signal`` porte le signal
d'une exécution ; ``HTTPClient.with_cancel`` en fait une vue (même session)
pour un client partagé.
"""

import copy
import email.utils
import hashlib
import logging
import math
import os
import re
import threading
import time
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from config import (
    HTTP_TIMEOUT,
    HTTP_CONNECT_TIMEOUT,
    HTTP_MAX_RETRIES,
    HTTP_BACKOFF_FACTOR,
    HTTP_RETRY_AFTER_MAX,
    HTTP_MAX_TOTAL_WAIT,
    HTTP_REQUEST_BUDGET,
    HTTP_MAX_IN_FLIGHT_PER_HOST,
    HTTP_CACHE_TTL,
    USER_AGENT,
    REQUEST_DELAY,
    REQUEST_DELAY_S2,
    REQUEST_DELAY_CORE,
    REQUEST_DELAY_SERPAPI,
    REQUEST_DELAY_CROSSREF,
    REQUEST_DELAY_CROSSREF_LIST,
    CROSSREF_MAX_IN_FLIGHT,
)
from utils.secrets import substitute_credential

# Configuration du logger pour tracer les échecs réseau
logger = logging.getLogger(__name__)


# === Classification des statuts ===

# Codes HTTP qui justifient un nouvel essai (throttling et pannes transitoires).
# Tout autre code >= 400 est définitif : une seule tentative.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Intervalle minimal (secondes) entre deux départs de requête vers un hôte,
# pour un même compte. Les hôtes à débit contraint reprennent les constantes
# déjà définies dans config.py ; les autres suivent REQUEST_DELAY.
HOST_MIN_INTERVALS: Dict[str, float] = {
    "api.semanticscholar.org": REQUEST_DELAY_S2,
    "api.core.ac.uk": REQUEST_DELAY_CORE,
    "serpapi.com": REQUEST_DELAY_SERPAPI,
    "api.crossref.org": REQUEST_DELAY_CROSSREF,
}


def host_min_interval(host: str) -> float:
    """Intervalle de politesse par défaut pour ``host`` (secondes)."""
    return HOST_MIN_INTERVALS.get(host, REQUEST_DELAY)


# --- Familles de requêtes d'un même hôte (OPT-09) ---
#
# CrossRef compte les requêtes de liste (/works?query…, filtres) et celles
# d'un travail unique (/works/{doi}) dans deux pools distincts, aux limites
# différentes (cf. config.py, REQUEST_DELAY_CROSSREF_LIST). Chaque famille a
# donc son propre intervalle et sa propre barrière de politesse ; le plafond
# de requêtes en vol reste commun à l'hôte.

CROSSREF_HOST = "api.crossref.org"
REQUEST_CLASS_LIST = "list"
REQUEST_CLASS_SINGLE = "single"

# Intervalle minimal par (hôte, famille) ; à défaut, celui de l'hôte.
REQUEST_CLASS_INTERVALS: Dict[Tuple[str, str], float] = {
    (CROSSREF_HOST, REQUEST_CLASS_LIST): REQUEST_DELAY_CROSSREF_LIST,
    (CROSSREF_HOST, REQUEST_CLASS_SINGLE): REQUEST_DELAY_CROSSREF,
}

# Plafond de requêtes en vol propre à un hôte ; à défaut,
# HTTP_MAX_IN_FLIGHT_PER_HOST.
HOST_MAX_IN_FLIGHT: Dict[str, int] = {
    CROSSREF_HOST: CROSSREF_MAX_IN_FLIGHT,
}

# Travail unique CrossRef : /works/{doi} (préfixe /v1 facultatif). Toute autre
# route de l'hôte (/works, /journals/{issn}/works, /members…) renvoie une
# liste et suit la limite, plus stricte, des listes.
_CROSSREF_SINGLE_PATH = re.compile(r"^(?:/v1)?/works/[^/]")


def request_class(host: str, url: str) -> str:
    """
    Famille d'une requête pour la politesse (``"list"``, ``"single"``), ou
    ``""`` pour un hôte qui ne distingue pas ses requêtes.
    """
    if host != CROSSREF_HOST:
        return ""
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        return REQUEST_CLASS_LIST
    if _CROSSREF_SINGLE_PATH.match(path):
        return REQUEST_CLASS_SINGLE
    return REQUEST_CLASS_LIST


def request_min_interval(host: str, url: str) -> float:
    """Intervalle de politesse (secondes) d'une requête vers ``url``."""
    cls = request_class(host, url)
    if cls:
        interval = REQUEST_CLASS_INTERVALS.get((host, cls))
        if interval is not None:
            return interval
    return host_min_interval(host)


def host_max_in_flight(host: str) -> int:
    """Plafond configuré de requêtes simultanées vers ``host``."""
    return max(1, int(HOST_MAX_IN_FLIGHT.get(host, HTTP_MAX_IN_FLIGHT_PER_HOST)))


# --- Limites annoncées par le serveur (en-têtes x-rate-limit-*) ---
#
# CrossRef annonce sa limite dans chaque réponse : ``x-rate-limit-limit``
# (requêtes) par ``x-rate-limit-interval`` (« 1s »), et ``x-concurrency-limit``
# (requêtes simultanées). La politique les lit à chaque accès réseau et ne
# s'en sert que pour DURCIR la politesse configurée : intervalle effectif =
# max(intervalle configuré, intervalle / limite annoncés × marge), requêtes en
# vol = min(plafond configuré, concurrence annoncée). Une annonce plus
# généreuse que la configuration ne la relâche jamais. Seuls les hôtes dont
# les en-têtes ont été vérifiés en live sont lus (CrossRef, le 2026-09-28) :
# un autre fournisseur pourrait donner à des en-têtes homonymes un autre sens
# (quota journalier, par exemple).

ANNOUNCED_RATE_MARGIN = 1.02
HOSTS_ANNOUNCING_LIMITS = frozenset({CROSSREF_HOST})
_INTERVAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)
_INTERVAL_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_rate_interval(value) -> Optional[float]:
    """Durée d'un en-tête ``x-rate-limit-interval`` (« 1s », « 500ms »…), en s."""
    if value is None:
        return None
    m = _INTERVAL_RE.match(str(value))
    if not m:
        return None
    seconds = float(m.group(1)) * _INTERVAL_UNITS[(m.group(2) or "s").lower()]
    return seconds if seconds > 0 else None


def announced_limits(headers) -> Tuple[Optional[float], Optional[int]]:
    """
    Limites annoncées par une réponse : ``(intervalle minimal en s,
    concurrence)``, chacun None s'il est absent ou illisible.
    """
    if not headers:
        return None, None
    try:
        get = headers.get
    except AttributeError:
        return None, None
    interval = None
    try:
        limit = float(get("x-rate-limit-limit"))
    except (TypeError, ValueError):
        limit = None
    period = parse_rate_interval(get("x-rate-limit-interval"))
    if limit and limit > 0 and period:
        interval = period / limit * ANNOUNCED_RATE_MARGIN
    concurrency = None
    try:
        raw = get("x-concurrency-limit")
        if raw is not None:
            concurrency = int(float(raw))
    except (TypeError, ValueError):
        concurrency = None
    if concurrency is not None and concurrency < 1:
        concurrency = None
    return interval, concurrency


# === Annulation coopérative (OPT-07) ===

# Tranche d'une attente interruptible (secondes) : délai maximal entre une
# annulation et la fin d'une attente de politesse ou de réessai en cours.
CANCEL_WAIT_SLICE = 0.1

# Motifs d'annulation (attribut ``OperationCancelled.reason``).
CANCEL_REASON_CANCELLED = "cancelled"
CANCEL_REASON_DEADLINE = "deadline"


class OperationCancelled(BaseException):
    """
    Annulation coopérative d'une opération (demande du client ou échéance).

    Hérite de ``BaseException``, comme ``asyncio.CancelledError`` : les
    ``except Exception`` des connecteurs, mappeurs et résolveurs, qui
    convertissent une panne en liste vide ou partielle, ne l'avalent pas ;
    elle remonte jusqu'à l'orchestrateur, qui n'écrit alors aucun corpus. Les
    blocs ``finally`` (fermeture des sessions, libération des places) sont
    exécutés normalement.

    Attributes:
        reason: ``"cancelled"`` (demande) ou ``"deadline"`` (échéance).
        result: résultat partiel éventuel, posé par l'orchestrateur (sources
            déjà terminées et leurs statuts), jamais publié.
    """

    def __init__(self, reason: str = CANCEL_REASON_CANCELLED,
                 message: Optional[str] = None):
        self.reason = reason or CANCEL_REASON_CANCELLED
        self.result = None
        super().__init__(message or (
            "échéance dépassée" if self.reason == CANCEL_REASON_DEADLINE
            else "annulation demandée"
        ))


class CancelSignal:
    """
    Signal d'annulation d'une exécution : événement et échéance monotone.

    Args:
        event: objet exposant ``is_set()`` (``threading.Event`` ou doublure) ;
            un ``threading.Event`` neuf si absent.
        deadline: échéance (``time.monotonic()``) ; ``None`` : aucune.

    ``wait`` est une attente interruptible faite de tranches de
    ``time.sleep`` (au plus ``CANCEL_WAIT_SLICE`` secondes chacune) plutôt que
    d'un ``Event.wait`` : les attentes restent comptées et mises à l'échelle
    par le banc de mesure, qui remplace ``time.sleep``.
    """

    __slots__ = ("event", "deadline")

    def __init__(self, event: Any = None, deadline: Optional[float] = None):
        self.event = event if event is not None else threading.Event()
        self.deadline = deadline

    @classmethod
    def coerce(cls, obj: Any) -> Optional["CancelSignal"]:
        """``None``, un ``CancelSignal`` ou un objet à ``is_set()`` → signal."""
        if obj is None or isinstance(obj, CancelSignal):
            return obj
        return cls(obj)

    def set(self) -> None:
        """Demande l'annulation (sans effet sur un objet sans ``set``)."""
        setter = getattr(self.event, "set", None)
        if callable(setter):
            setter()

    def deadline_passed(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def reason(self) -> Optional[str]:
        """Motif de l'annulation en cours, ou ``None``."""
        if self.event is not None and self.event.is_set():
            return CANCEL_REASON_CANCELLED
        if self.deadline_passed():
            return CANCEL_REASON_DEADLINE
        return None

    def is_set(self) -> bool:
        return self.reason() is not None

    def remaining(self) -> Optional[float]:
        """Secondes avant l'échéance (négatif si dépassée), ``None`` sans échéance."""
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()

    def check(self) -> None:
        """Lève ``OperationCancelled`` si l'annulation est demandée."""
        reason = self.reason()
        if reason is not None:
            raise OperationCancelled(reason)

    def wait(self, seconds: float) -> bool:
        """
        Attend ``seconds`` secondes au plus, en s'interrompant dès l'annulation
        ou l'échéance.

        Returns:
            True si l'annulation est demandée (ou l'échéance atteinte).
        """
        left = max(0.0, float(seconds))
        while left > 0:
            if self.is_set():
                return True
            step = min(left, CANCEL_WAIT_SLICE)
            rem = self.remaining()
            if rem is not None:
                if rem <= 0:
                    return True
                # Plancher de 10 ms : borne le nombre de tranches quand
                # time.sleep est rendu instantané (banc à l'échelle 0).
                step = min(step, max(rem, 0.01))
            t0 = time.monotonic()
            time.sleep(step)
            # Décompte au plus grand du demandé et du réellement écoulé : le
            # dépassement de chaque tranche (granularité de l'horloge) ne
            # s'additionne pas sur une longue attente, et un time.sleep rendu
            # instantané (banc à l'échelle 0) termine en seconds/tranche tours.
            left -= max(step, time.monotonic() - t0)
        return self.is_set()


def _cancelled_error(cancel: "CancelSignal") -> OperationCancelled:
    return OperationCancelled(cancel.reason() or CANCEL_REASON_CANCELLED)


# === Option cache à trois états ===

_TRUE_VALUES = {"1", "true", "yes", "on"}


def cache_enabled_from_env() -> bool:
    """Valeur héritée de ``MOISSON_CACHE`` (1/true/yes/on → activé)."""
    return os.environ.get("MOISSON_CACHE", "0").strip().lower() in _TRUE_VALUES


def resolve_cache_option(value: Optional[bool]) -> bool:
    """
    Résout l'option cache à trois états.

    Args:
        value: ``None`` (option absente) → hérite de ``MOISSON_CACHE`` ;
            ``True`` / ``False`` → valeur explicite, prioritaire sur
            l'environnement.
    """
    if value is None:
        return cache_enabled_from_env()
    return bool(value)


# === Secrets : empreinte de compte et masquage ===

# Paramètres de query string et en-têtes qui identifient un compte (clé
# d'API OpenAlex et SerpAPI en paramètre, x-api-key Semantic Scholar et
# Bearer CORE en en-tête). Ils fixent la clé de politesse et de fusion.
# Les jetons de pagination n'en font surtout pas partie (``token`` de la
# recherche bulk Semantic Scholar, qui change à chaque page) : chaque page
# aurait sinon sa propre barrière de politesse, donc aucune attente entre
# pages, et le registre des barrières grossirait d'une entrée par page.
_ACCOUNT_PARAMS = {"api_key", "apikey", "access_token"}
_ACCOUNT_HEADERS = {"x-api-key", "authorization"}

# Paramètres masqués dans les messages et journaux : liste plus large que
# celle des comptes, un masquage de trop ne coûtant rien.
_SECRET_IN_TEXT = re.compile(
    r"(?i)(?<![a-z0-9_])(api_key|apikey|key|token|access_token)=([^&\s'\"]+)"
)


def _pairs(mapping) -> list:
    """Paires (clé, valeur) d'un dict ou d'une liste de tuples ; [] sinon."""
    if not mapping:
        return []
    if isinstance(mapping, Mapping):
        return list(mapping.items())
    try:
        return [(k, v) for k, v in mapping]
    except (TypeError, ValueError):
        return []


def account_fingerprint(params=None, headers=None) -> str:
    """
    Empreinte courte du compte utilisé par une requête ("" si anonyme).

    Calculée sur les clés de compte présentes dans les paramètres
    (``api_key``…) et les en-têtes (``x-api-key``, ``Authorization``), à
    l'exclusion des jetons de pagination. Sert de clé de politesse et de
    fusion : la clé elle-même n'est jamais conservée ni journalisée.
    """
    secrets = []
    for k, v in _pairs(params):
        if str(k).lower() in _ACCOUNT_PARAMS and v:
            secrets.append(f"p:{str(k).lower()}={v}")
    for k, v in _pairs(headers):
        if str(k).lower() in _ACCOUNT_HEADERS and v:
            secrets.append(f"h:{str(k).lower()}={v}")
    if not secrets:
        return ""
    digest = hashlib.sha256("\n".join(sorted(secrets)).encode("utf-8"))
    return digest.hexdigest()[:12]


# Refus d'une clé d'API : statuts qui déclenchent le repli sur la clé de
# réserve du .env (sprint « clés », décision D2 : ni 429 ni quota épuisé).
KEY_REFUSAL_STATUSES = (401, 403)


def _swap_credentials(params, headers, *, on_refusal: bool,
                      status: Optional[int] = None):
    """
    Identifiants de compte (``_ACCOUNT_PARAMS``, ``_ACCOUNT_HEADERS``) qui
    portent la clé du client, remplacés par la clé de réserve du `.env`
    (``utils.secrets.substitute_credential``) : après un refus de cette
    requête (``on_refusal``), ou pour une clé déjà refusée. Les objets de
    l'appelant ne sont jamais modifiés.

    Returns:
        ``(params, headers, variables remplacées)``.
    """
    swapped: List[str] = []

    def _rewrite(pairs, names):
        out, changed = [], False
        for k, v in pairs:
            if str(k).lower() in names and v:
                sub = substitute_credential(str(v), on_refusal=on_refusal,
                                            status=status)
                if sub is not None:
                    swapped.append(sub[0])
                    v, changed = sub[1], True
            out.append((k, v))
        return out, changed

    new_params, new_headers = params, headers
    pairs, changed = _rewrite(_pairs(params), _ACCOUNT_PARAMS)
    if changed:
        new_params = dict(pairs) if isinstance(params, Mapping) else pairs
    pairs, changed = _rewrite(_pairs(headers), _ACCOUNT_HEADERS)
    if changed:
        new_headers = dict(pairs)
    return new_params, new_headers, swapped


def redact_secrets(text: str) -> str:
    """Masque la valeur des paramètres secrets (``api_key=…``) d'un texte."""
    if not text:
        return text
    return _SECRET_IN_TEXT.sub(lambda m: f"{m.group(1)}=***", str(text))


def _sanitize_exception(exc: BaseException) -> BaseException:
    """
    Renvoie une exception de même type au message expurgé des secrets.

    Les messages de requests contiennent l'URL complète, donc la clé d'API
    passée en paramètre (OpenAlex, SerpAPI). L'exception d'origine est rendue
    telle quelle si son message ne contient aucun secret ou si elle ne peut
    pas être reconstruite.
    """
    message = str(exc)
    cleaned = redact_secrets(message)
    if cleaned == message:
        return exc
    try:
        kwargs = {}
        if isinstance(exc, requests.RequestException):
            kwargs = {"request": getattr(exc, "request", None),
                      "response": getattr(exc, "response", None)}
        new = type(exc)(cleaned, **kwargs)
        new.__suppress_context__ = True
        return new
    except Exception:  # noqa: BLE001 - reconstruction best-effort
        return exc


def _http_error(response, url: str) -> requests.HTTPError:
    """HTTPError portant la réponse, au message expurgé des secrets."""
    status = getattr(response, "status_code", "?")
    reason = getattr(response, "reason", "") or ""
    target = redact_secrets(getattr(response, "url", None) or url)
    label = f"HTTP {status}{' ' + reason if isinstance(reason, str) and reason else ''}"
    return requests.HTTPError(f"{label} sur {target}", response=response)


# === Retry-After et délais ===

def parse_retry_after(value) -> Optional[float]:
    """
    Délai demandé par un en-tête ``Retry-After`` (secondes, >= 0).

    Accepte un nombre de secondes ou une date HTTP (RFC 7231). Renvoie
    ``None`` si l'en-tête est absent ou illisible.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        if math.isnan(seconds):
            return None
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - time.time())


def _normalize_timeout(timeout) -> Tuple[Optional[float], Optional[float]]:
    """
    Délais ``(connexion, lecture)``.

    ``None`` → ``HTTP_TIMEOUT`` ; un tuple est conservé ; un scalaire ne vaut
    que pour la lecture, la connexion restant bornée par
    ``HTTP_CONNECT_TIMEOUT`` (un hôte mort échoue vite, même quand
    l'appelant accorde une longue lecture).
    """
    if timeout is None:
        return tuple(HTTP_TIMEOUT)
    if isinstance(timeout, (tuple, list)):
        return tuple(timeout)
    value = float(timeout)
    return (min(HTTP_CONNECT_TIMEOUT, value), value)


def _bound_timeout(timeout: tuple, remaining: Optional[float]) -> tuple:
    """Borne chaque délai par le budget restant de la requête logique."""
    if remaining is None:
        return timeout
    return tuple(
        t if (t is not None and t <= remaining) else max(0.1, remaining)
        for t in timeout
    )


# === Compteurs observables par hôte ===

_STAT_KEYS = (
    "logical",        # requêtes logiques reçues (appels à policy_get)
    "network",        # accès réseau réels (chaque tentative compte)
    "cache_hits",     # réponses servies par requests-cache
    "cache_misses",   # requêtes cache actif non servies par le cache
    "retries",        # nouvelles tentatives
    "http_429",       # réponses 429 reçues
    "errors",         # requêtes logiques terminées en exception
    "coalesced",      # requêtes servies par une requête identique en cours
    "wait_s",         # temps d'attente cumulé (politesse + réessais)
    "retry_wait_s",   # dont attentes entre tentatives
)

_stats_lock = threading.Lock()
_stats: Dict[str, Dict[str, float]] = {}


def _bump(host: str, key: str, value: float = 1) -> None:
    with _stats_lock:
        entry = _stats.get(host)
        if entry is None:
            entry = _stats[host] = dict.fromkeys(_STAT_KEYS, 0)
        entry[key] += value


def get_http_stats(host: Optional[str] = None) -> Dict:
    """
    Instantané des compteurs réseau.

    Args:
        host: Nom d'hôte (ex. ``api.openalex.org``). ``None`` → tous les
            hôtes, sous la forme ``{hôte: {compteur: valeur}}``.

    Returns:
        Une copie (modifiable sans effet sur les compteurs). Clés :
        ``logical``, ``network``, ``cache_hits``, ``cache_misses``,
        ``retries``, ``http_429``, ``errors``, ``coalesced``, ``wait_s``,
        ``retry_wait_s``.
    """
    with _stats_lock:
        if host is not None:
            return dict(_stats.get(host) or dict.fromkeys(_STAT_KEYS, 0))
        return {h: dict(v) for h, v in _stats.items()}


def reset_http_stats() -> None:
    """Remet tous les compteurs réseau à zéro."""
    with _stats_lock:
        _stats.clear()


# === Politesse par hôte et par compte ===

class _HostGate:
    """
    Créneaux d'accès à un hôte pour un compte donné.

    ``reserve`` attribue sous verrou le prochain départ autorisé
    (``max(maintenant, dernier départ + intervalle)``) puis rend la main :
    l'attente se fait hors verrou, et deux threads obtiennent deux créneaux
    distincts au lieu de partir ensemble. ``commit``, appelé au départ
    effectif, recale le dernier départ sur l'heure réelle si aucun créneau
    plus récent n'a été réservé entre-temps : l'intervalle court ainsi depuis
    le départ réel (un fil retardé par l'ordonnanceur ne rapproche pas le
    suivant), et un sommeil raccourci par une instrumentation (banc à
    l'échelle 0) ne fait pas dériver les créneaux dans le futur.

    ``defer`` interdit tout départ avant une échéance : un ``Retry-After``
    reçu par un fil vaut pour tous les fils qui visent cet hôte avec ce
    compte. ``cancel`` rend le dernier créneau réservé quand le départ est
    abandonné (budget de la requête dépassé), pour qu'il ne retarde pas les
    requêtes suivantes.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_start: Optional[float] = None
        self._prev_start: Optional[float] = None
        self._not_before = -math.inf
        self._seq = 0
        # Intervalle annoncé par le serveur (en-têtes x-rate-limit-*, OPT-09) :
        # plancher supplémentaire, jamais un relâchement.
        self.announced_interval = 0.0

    def announce(self, interval: Optional[float]) -> None:
        """Dernier intervalle annoncé par le serveur (None : inchangé)."""
        if interval is None:
            return
        with self._lock:
            self.announced_interval = max(0.0, float(interval))

    def reserve(self, interval: float) -> Tuple[float, int]:
        """
        Réserve un créneau.

        L'intervalle appliqué est le plus grand de ``interval`` (politesse
        configurée) et de l'intervalle annoncé par le serveur.

        Returns:
            ``(attente, jeton)`` : l'attente (s) à observer avant l'envoi, et
            le jeton à passer à ``commit`` au départ effectif.
        """
        with self._lock:
            now = time.monotonic()
            interval = max(max(0.0, interval), self.announced_interval)
            if self._last_start is None:
                start = now
            else:
                start = max(now, self._last_start + interval)
            start = max(start, self._not_before)
            self._prev_start = self._last_start
            self._last_start = start
            self._seq += 1
            return start - now, self._seq

    def commit(self, token: int) -> None:
        """Enregistre le départ effectif du créneau ``token`` s'il est le dernier."""
        with self._lock:
            if token == self._seq:
                self._last_start = time.monotonic()

    def cancel(self, token: int) -> None:
        """Rend le créneau ``token`` s'il est le dernier réservé (départ abandonné)."""
        with self._lock:
            if token == self._seq:
                self._last_start = self._prev_start

    def defer(self, until: float) -> None:
        """Aucun départ avant ``until`` (horloge monotone), quel que soit le fil."""
        with self._lock:
            if until > self._not_before:
                self._not_before = until


class _HostSlots:
    """
    Places de requêtes en vol vers un hôte (interface d'un sémaphore :
    ``acquire(timeout=…)`` / ``release()``), dont le plafond peut être abaissé
    par la concurrence annoncée par le serveur (``x-concurrency-limit``,
    OPT-09) : plafond effectif = min(plafond configuré, concurrence annoncée).
    Un abaissement ne coupe aucune requête en cours : les nouvelles attendent
    que le nombre de requêtes en vol repasse sous le plafond.
    """

    def __init__(self, limit: int):
        self._cond = threading.Condition()
        self.configured = max(1, int(limit))
        self.announced: Optional[int] = None
        self.in_use = 0

    @property
    def limit(self) -> int:
        if self.announced is None:
            return self.configured
        return max(1, min(self.configured, self.announced))

    def announce(self, concurrency: Optional[int]) -> None:
        """Dernière concurrence annoncée par le serveur (None : inchangée)."""
        if concurrency is None:
            return
        with self._cond:
            self.announced = max(1, int(concurrency))
            self._cond.notify_all()

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        with self._cond:
            if not blocking:
                timeout = 0.0
            end = None if timeout is None else time.monotonic() + max(0.0, timeout)
            while self.in_use >= self.limit:
                if end is None:
                    self._cond.wait()
                    continue
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            self.in_use += 1
            return True

    def release(self) -> None:
        with self._cond:
            if self.in_use <= 0:
                raise ValueError("_HostSlots.release() sans acquire()")
            self.in_use -= 1
            self._cond.notify()


_registry_lock = threading.Lock()
_gates: Dict[Tuple[str, str, str], _HostGate] = {}
_inflight: Dict[str, _HostSlots] = {}


def _gate(host: str, account: str, cls: str = "") -> _HostGate:
    """Barrière de politesse d'un hôte, d'un compte et d'une famille de requêtes."""
    with _registry_lock:
        gate = _gates.get((host, account, cls))
        if gate is None:
            gate = _gates[(host, account, cls)] = _HostGate()
        return gate


def _inflight_semaphore(host: str) -> _HostSlots:
    with _registry_lock:
        sem = _inflight.get(host)
        if sem is None:
            sem = _inflight[host] = _HostSlots(host_max_in_flight(host))
        return sem


def _observe_limits(host: str, account: str, cls: str, response) -> None:
    """
    Applique les limites annoncées par une réponse réseau (en-têtes
    x-rate-limit-*, x-concurrency-limit) à la barrière de sa famille et aux
    places en vol de l'hôte. Sans en-tête, rien ne change.
    """
    if host not in HOSTS_ANNOUNCING_LIMITS:
        return
    interval, concurrency = announced_limits(getattr(response, "headers", None))
    if interval is not None:
        gate = _gate(host, account, cls)
        if abs(gate.announced_interval - interval) > 1e-9:
            logger.debug(
                f"Limite annoncée par {host} ({cls or 'toutes requêtes'}) : "
                f"un départ toutes les {interval:.3f} s au plus"
            )
        gate.announce(interval)
    if concurrency is not None:
        _inflight_semaphore(host).announce(concurrency)


def reset_http_policy() -> None:
    """
    Oublie les créneaux de politesse et les plafonds en vol (tests, bancs).

    À n'appeler que lorsqu'aucune requête n'est en cours.
    """
    with _registry_lock:
        _gates.clear()
        _inflight.clear()


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class _BudgetSpent(Exception):
    """Budget de la requête logique épuisé avant l'envoi (usage interne)."""


def _polite_sleep(host: str, account: str, interval: float,
                  deadline: Optional[float] = None,
                  cancel: Optional[CancelSignal] = None,
                  cls: str = "") -> float:
    """
    Attend le créneau de politesse de ``(host, account, cls)`` (``cls`` :
    famille de requêtes, cf. ``request_class``).

    Avec ``deadline`` (horloge monotone), un créneau qui tomberait à ou après
    l'échéance est rendu sans attendre et ``_BudgetSpent`` est levée : une
    tentative ne part jamais budget épuisé. Avec ``cancel``, l'attente est
    interruptible : une annulation (ou l'échéance du signal, atteinte ou
    antérieure au créneau) rend le créneau et lève ``OperationCancelled``.
    """
    gate = _gate(host, account, cls)
    wait, token = gate.reserve(interval)
    now = time.monotonic()
    if deadline is not None and now + wait >= deadline:
        gate.cancel(token)
        raise _BudgetSpent()
    if cancel is not None:
        if cancel.is_set() or (cancel.deadline is not None
                               and now + wait >= cancel.deadline):
            gate.cancel(token)
            raise OperationCancelled(cancel.reason() or CANCEL_REASON_DEADLINE)
    if wait > 0:
        if cancel is None:
            time.sleep(wait)
        elif cancel.wait(wait):
            gate.cancel(token)
            raise _cancelled_error(cancel)
        _bump(host, "wait_s", wait)
    gate.commit(token)
    return wait


def polite_wait(url: str, min_interval: Optional[float] = None,
                cancel: Any = None) -> float:
    """
    Applique la politesse par hôte avant un accès réseau fait hors de ce module.

    Pour les bibliothèques qui gèrent leur propre transport (sickle pour les
    GetRecord OAI) : réserve le créneau de l'hôte, attend si nécessaire et
    compte l'accès. Renvoie l'attente observée (secondes). Avec ``cancel``
    (signal d'annulation), l'attente est interruptible et une annulation lève
    ``OperationCancelled`` sans compter d'accès.
    """
    host = _host_of(url)
    interval = (request_min_interval(host, url) if min_interval is None
                else min_interval)
    wait = _polite_sleep(host, "", interval, cancel=CancelSignal.coerce(cancel),
                         cls=request_class(host, url))
    _bump(host, "logical")
    _bump(host, "network")
    return wait


# === Fusion des requêtes identiques simultanées (single-flight) ===

class _Flight:
    __slots__ = ("done", "result", "error")

    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.error: Optional[BaseException] = None


class _SingleFlight:
    """Une seule exécution par clé en cours ; les suivants attendent son résultat."""

    def __init__(self):
        self._lock = threading.Lock()
        self._calls: Dict[Any, _Flight] = {}

    def do(self, key, fn: Callable, host: str,
           cancel: Optional[CancelSignal] = None):
        if key is None:
            return fn()
        with self._lock:
            flight = self._calls.get(key)
            leader = flight is None
            if leader:
                flight = self._calls[key] = _Flight()
        if not leader:
            if cancel is None:
                flight.done.wait()
            else:
                # Attente interruptible : l'annulation de CETTE requête ne
                # dépend pas de la fin de la requête identique en cours.
                while not flight.done.wait(CANCEL_WAIT_SLICE):
                    cancel.check()
            if isinstance(flight.error, OperationCancelled) and not (
                    cancel is not None and cancel.is_set()):
                # La requête menée a été annulée par SON exécution, pas par
                # celle-ci : on la refait (en meneur si personne ne l'a déjà
                # relancée).
                return self.do(key, fn, host, cancel)
            _bump(host, "coalesced")
            if flight.error is not None:
                raise flight.error
            return flight.result
        try:
            flight.result = fn()
            return flight.result
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self._lock:
                self._calls.pop(key, None)
            flight.done.set()


_single_flight = _SingleFlight()


def _flight_key(url: str, params, account: str, cache_readable: bool):
    """
    Clé de fusion : URL préparée (paramètres encodés) + compte + lecture du
    cache permise. Une requête qui refuse le cache (``--no-cache``, solde
    SerpAPI) ne reçoit jamais la réponse d'une requête servie par le cache.
    """
    try:
        prepared = requests.Request("GET", url, params=params).prepare().url
    except Exception:  # noqa: BLE001 - URL non préparable : pas de fusion
        return None
    return (prepared, account, bool(cache_readable))


# === Cache ===

def _is_cached_session(session) -> bool:
    """La session est-elle une CachedSession requests-cache ?"""
    try:
        from requests_cache.session import CacheMixin
    except ImportError:
        return False
    return isinstance(session, CacheMixin)


def _cacheable_response(response) -> bool:
    """
    Filtre du cache HTTP (``filter_fn`` de requests-cache) : une réponse 200
    ou 203 dont le corps n'est pas du JSON n'est jamais mise en cache.

    Toutes les API interrogées par ce client répondent en JSON (les parcours
    OAI passent par le transport de sickle, hors de ce cache). Une page
    d'erreur HTML servie avec un statut 200, ou un corps vide, mise en
    cache, serait resservie sans aucune requête pendant toute la durée de
    vie du cache (6 h côté MCP, où il est actif par défaut) : la source
    resterait « corps illisible » d'un appel à l'autre. requests-cache
    applique aussi ce filtre à une entrée déjà stockée, qu'il supprime.
    Seul le premier octet significatif est examiné (coût borné) : un JSON
    tronqué qui commence par « { » n'est pas détecté. Les autres codes
    admis (redirections) ne sont pas concernés.
    """
    if getattr(response, "status_code", None) not in (200, 203):
        return True
    try:
        head = (response.content or b"")[:256].lstrip()[:1]
    except Exception:  # noqa: BLE001 - corps illisible : pas de mise en cache
        return False
    return head in (b"{", b"[")


def _probe_cache(session, url: str, params, headers):
    """
    Réponse servie par le cache sans accès réseau, ou ``None``.

    ``only_if_cached=True`` ne sort jamais sur le réseau : en l'absence
    d'entrée utilisable (absente ou expirée), requests-cache renvoie une
    réponse synthétique 504 (jamais mise en cache, allowable_codes l'exclut).
    Une entrée écartée par ``_cacheable_response`` (corps non JSON stocké
    avant l'ajout du filtre) n'est pas servie : requests-cache vient de la
    supprimer, la requête part sur le réseau.
    """
    kwargs = {"params": params, "only_if_cached": True}
    if headers:
        kwargs["headers"] = headers
    try:
        response = session.get(url, **kwargs)
    except Exception:  # noqa: BLE001 - sonde best-effort
        return None
    if (getattr(response, "from_cache", False) and response.status_code != 504
            and _cacheable_response(response)):
        return response
    return None


# === Politique commune ===

# Erreurs de transport transitoires, réessayées avec backoff. Outre le délai
# dépassé et la connexion refusée ou coupée, une connexion rompue pendant la
# lecture du corps (« Connection broken: IncompleteRead ») lève
# ChunkedEncodingError, et un corps compressé tronqué ContentDecodingError :
# ces deux classes héritent directement de RequestException, pas de
# ConnectionError. Elles touchent surtout les gros lots (Solr HAL à 1000
# lignes). Les autres RequestException (URL invalide, trop de redirections…)
# sont définitives.
_TRANSIENT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)


# === Audit des réponses natives (OPT-16, C32) ===
#
# Crochet de capture : un enregistreur (``utils.manifest.NativeAudit``) posé
# sur la session d'une exécution (``attach_native_audit``) reçoit chaque
# réponse obtenue par ``policy_get``, servie par le cache comme par le
# réseau, avant sa classification. Sans enregistreur (défaut), aucun coût ;
# une capture en échec ne touche jamais la requête.

NATIVE_AUDIT_ATTR = "moisson_native_audit"


def attach_native_audit(client_or_session: Any, recorder: Any) -> None:
    """
    Pose (ou retire, ``recorder=None``) l'enregistreur d'audit des réponses
    natives sur la session d'un ``HTTPClient`` (ou sur une session requests) :
    ses vues (``with_cancel``) et ``http_scholarly`` partagent cette session.
    """
    session = getattr(client_or_session, "session", client_or_session)
    try:
        setattr(session, NATIVE_AUDIT_ATTR, recorder)
    except (AttributeError, TypeError):  # doublure sans attribut modifiable
        pass


def _capture_native(session, response, url: str, params) -> None:
    """Transmet la réponse à l'enregistreur de la session, s'il existe."""
    recorder = getattr(session, NATIVE_AUDIT_ATTR, None)
    if recorder is None:
        return
    try:
        recorder.capture(response, url=url, params=params)
    except Exception:  # noqa: BLE001 - l'audit ne perturbe jamais la requête
        logger.debug("Audit des réponses natives : capture impossible",
                     exc_info=True)


def policy_get(
    session,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout=None,
    *,
    max_attempts: Optional[int] = None,
    backoff_factor: Optional[float] = None,
    min_interval: Optional[float] = None,
    use_cache: bool = True,
    response_hook: Optional[Callable[[Any], None]] = None,
    budget: Optional[float] = None,
    cancel: Any = None,
) -> requests.Response:
    """
    GET selon la politique commune (voir le docstring du module).

    Args:
        session: Session requests (éventuellement CachedSession) qui émet
            la requête.
        url: URL cible (sans secret : les clés passent par ``params`` ou
            ``headers``).
        params: Paramètres de query string.
        headers: En-têtes propres à cette requête (fusionnés par requests
            avec ceux de la session, sans la muter).
        timeout: ``None`` (``HTTP_TIMEOUT``), tuple ``(connexion, lecture)``
            ou scalaire (lecture seule, connexion bornée).
        max_attempts: Tentatives maximales sur erreur transitoire
            (défaut ``HTTP_MAX_RETRIES``).
        backoff_factor: Base du backoff exponentiel (défaut
            ``HTTP_BACKOFF_FACTOR``) : attente ``base × 2^(tentative-1)``.
        min_interval: Intervalle de politesse pour cette requête (défaut :
            celui de l'hôte, cf. ``HOST_MIN_INTERVALS``).
        use_cache: ``False`` interdit lecture et écriture en cache pour
            cette requête (``Cache-Control: no-store``).
        response_hook: Fonction appelée sur chaque réponse reçue avant la
            classification ; peut lever pour interrompre (ex.
            ``BudgetExhausted`` de ``http_scholarly``).
        budget: Budget global (secondes) de la requête logique (défaut
            ``HTTP_REQUEST_BUDGET`` ; 0 ou négatif : sans budget).
        cancel: Signal d'annulation (``CancelSignal`` ou objet à
            ``is_set()``) : vérifié avant chaque tentative et pendant les
            attentes (place en vol, politesse, réessai), qui deviennent
            interruptibles ; son échéance borne les délais réseau.

    Returns:
        La réponse (statut < 400).

    Raises:
        requests.HTTPError: 4xx définitif (dès la première tentative), ou
            429/5xx persistant après les tentatives permises. L'erreur porte
            la réponse (``exc.response.status_code``).
        requests.RequestException: erreur réseau persistante (délai,
            connexion) ou définitive (URL invalide…).
        OperationCancelled: annulation ou échéance du signal ``cancel``.
    """
    cancel = CancelSignal.coerce(cancel)
    host = _host_of(url)
    if cancel is not None:
        cancel.check()
    # Clé du client déjà refusée : la clé de réserve part directement.
    params, headers, _ = _swap_credentials(params, headers, on_refusal=False)

    def _send(params, headers):
        account = account_fingerprint(params, headers)
        _bump(host, "logical")
        key = _flight_key(url, params, account,
                          use_cache and _is_cached_session(session))
        return _single_flight.do(
            key,
            lambda: _execute(
                session, url, host, account, params, headers, timeout,
                max_attempts, backoff_factor, min_interval, use_cache,
                response_hook, budget, cancel,
            ),
            host,
            cancel,
        )

    try:
        return _send(params, headers)
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status not in KEY_REFUSAL_STATUSES:
            raise
        retry_params, retry_headers, swapped = _swap_credentials(
            params, headers, on_refusal=True, status=status)
        if not swapped:
            raise
        logger.warning(
            "%s : clé du client refusée (HTTP %s), clé de réserve du .env "
            "utilisée (%s)", host, status, ", ".join(sorted(set(swapped))))
        return _send(retry_params, retry_headers)


def _acquire_slot(semaphore, remaining: Optional[float],
                  cancel: Optional[CancelSignal]) -> bool:
    """
    Place en vol, attendue au plus ``remaining`` secondes ; attente
    interruptible par ``cancel`` (``OperationCancelled``).

    Returns:
        False si le budget est épuisé avant d'obtenir la place.
    """
    if cancel is None:
        if remaining is None:
            semaphore.acquire()
            return True
        return semaphore.acquire(timeout=remaining)
    end = None if remaining is None else time.monotonic() + remaining
    while True:
        cancel.check()
        step = CANCEL_WAIT_SLICE
        if end is not None:
            left = end - time.monotonic()
            if left <= 0:
                return False
            step = min(step, left)
        if semaphore.acquire(timeout=step):
            return True


def _execute(session, url, host, account, params, headers, timeout,
             max_attempts, backoff_factor, min_interval, use_cache,
             response_hook, budget, cancel=None) -> requests.Response:
    """Exécution d'une requête logique (appelée une fois par clé en cours)."""
    attempts_max = max(1, int(max_attempts or HTTP_MAX_RETRIES))
    base = HTTP_BACKOFF_FACTOR if backoff_factor is None else backoff_factor
    # Famille de la requête (liste / travail unique pour CrossRef, OPT-09) :
    # intervalle et barrière de politesse propres.
    cls = request_class(host, url)
    interval = (request_min_interval(host, url) if min_interval is None
                else min_interval)
    budget = HTTP_REQUEST_BUDGET if budget is None else budget
    safe_url = redact_secrets(url)
    cached_session = _is_cached_session(session)
    base_timeout = _normalize_timeout(timeout)

    request_headers = dict(headers) if headers else None
    if cached_session and not use_cache:
        request_headers = dict(request_headers or {})
        request_headers["Cache-Control"] = "no-store"

    # 1) Cache : une réponse disponible est servie sans aucune temporisation.
    probe_missed = False
    if cached_session and use_cache:
        cached = _probe_cache(session, url, params, request_headers)
        if cached is not None:
            _bump(host, "cache_hits")
            _capture_native(session, cached, url, params)
            return cached
        _bump(host, "cache_misses")
        probe_missed = True

    started = time.monotonic()
    deadline = started + budget if budget and budget > 0 else None
    retry_waited = 0.0
    attempt = 0

    def _fail(exc: BaseException) -> BaseException:
        _bump(host, "errors")
        return exc

    def _budget_error() -> requests.Timeout:
        return requests.Timeout(
            f"Budget de requête épuisé ({budget:g} s) sur {safe_url}"
        )

    def _may_retry(wait: float) -> bool:
        if attempt >= attempts_max:
            return False
        if retry_waited + wait > HTTP_MAX_TOTAL_WAIT:
            return False
        if deadline is not None and time.monotonic() + wait >= deadline:
            return False
        return True

    def _sleep_before_retry(wait: float) -> None:
        nonlocal retry_waited
        if wait > 0:
            if cancel is None:
                time.sleep(wait)
            elif cancel.wait(wait):
                # Annulation pendant l'attente : aucune nouvelle tentative.
                raise _cancelled_error(cancel)
            _bump(host, "wait_s", wait)
            _bump(host, "retry_wait_s", wait)
        retry_waited += wait
        _bump(host, "retries")

    def _cancel_remaining() -> Optional[float]:
        """Reste avant l'échéance du signal d'annulation (None : aucune)."""
        return None if cancel is None else cancel.remaining()

    while True:
        attempt += 1
        if cancel is not None:
            cancel.check()
        remaining = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _fail(_budget_error())

        get_kwargs = {"params": params}
        # On ne transmet `headers` que s'il est fourni : préserve la compat
        # avec toute session ne déclarant pas ce paramètre.
        if request_headers:
            get_kwargs["headers"] = request_headers

        semaphore = _inflight_semaphore(host)
        try:
            # Attente d'une place en vol, puis du créneau de politesse, toutes
            # deux bornées par le budget : aucune tentative ne part budget
            # épuisé, et le délai transmis est recalculé sur le reste réel.
            # Avec un signal d'annulation, ces attentes sont interruptibles.
            if not _acquire_slot(semaphore, remaining, cancel):
                raise _BudgetSpent()
            try:
                _polite_sleep(host, account, interval, deadline, cancel, cls)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _BudgetSpent()
                # L'échéance de l'exécution borne aussi le délai réseau.
                cancel_left = _cancel_remaining()
                if cancel_left is not None:
                    if cancel_left <= 0:
                        raise _cancelled_error(cancel)
                    remaining = (cancel_left if remaining is None
                                 else min(remaining, cancel_left))
                get_kwargs["timeout"] = _bound_timeout(base_timeout, remaining)
                response = session.get(url, **get_kwargs)
            finally:
                semaphore.release()
        except _BudgetSpent:
            raise _fail(_budget_error()) from None
        except requests.RequestException as exc:
            if cancel is not None and cancel.is_set():
                # Délai coupé par l'échéance de l'exécution, ou annulation
                # survenue pendant l'envoi : c'est une annulation, pas une
                # panne du service.
                raise _cancelled_error(cancel) from None
            if not isinstance(exc, _TRANSIENT_ERRORS):
                # Définitive (URL invalide, trop de redirections…) : un essai.
                raise _fail(_sanitize_exception(exc))
            # Transitoire : délai dépassé, connexion refusée ou coupée (y
            # compris en cours de lecture du corps).
            wait = base * (2 ** (attempt - 1))
            if not _may_retry(wait):
                logger.warning(
                    f"Échec définitif sur {safe_url}: {type(exc).__name__} "
                    f"(tentative {attempt}/{attempts_max})"
                )
                raise _fail(_sanitize_exception(exc))
            logger.warning(
                f"Exception réseau sur {safe_url}: {type(exc).__name__} "
                f"(tentative {attempt}/{attempts_max}), attente {wait:.1f}s"
            )
            _sleep_before_retry(wait)
            continue

        if (getattr(response, "from_cache", False)
                and getattr(response, "revalidated", False) is not True):
            # Servie par le cache sans accès réseau : entrée écrite entre la
            # sonde et l'envoi (course rare). La requête logique change de
            # colonne, de « miss » à « hit », pour n'être comptée qu'une fois.
            _bump(host, "cache_hits")
            if probe_missed:
                _bump(host, "cache_misses", -1)
                probe_missed = False
        else:
            # Accès réseau réel, y compris une revalidation conditionnelle
            # d'une entrée expirée (304) : requests-cache rend alors l'entrée
            # en cache (from_cache vrai, revalidated vrai), mais le serveur a
            # bien été interrogé.
            _bump(host, "network")
            # Limites annoncées par le serveur (x-rate-limit-*,
            # x-concurrency-limit) : durcissent la politesse suivante.
            _observe_limits(host, account, cls, response)

        status = getattr(response, "status_code", 0)
        if status == 429:
            _bump(host, "http_429")

        _capture_native(session, response, url, params)

        if response_hook is not None:
            try:
                response_hook(response)
            except Exception:
                _bump(host, "errors")
                raise

        if status < 400:
            return response

        if status in RETRYABLE_STATUS:
            response_headers = getattr(response, "headers", None) or {}
            retry_after = parse_retry_after(response_headers.get("Retry-After"))
            if retry_after is not None and retry_after > HTTP_RETRY_AFTER_MAX:
                logger.warning(
                    f"Statut {status} sur {safe_url} : Retry-After "
                    f"{retry_after:.0f}s au-delà du plafond "
                    f"({HTTP_RETRY_AFTER_MAX:.0f}s), abandon"
                )
                raise _fail(_http_error(response, url))
            if retry_after is not None:
                # Consigne du serveur au client entier : les autres fils qui
                # visent cet hôte avec ce compte attendent aussi l'échéance
                # au lieu de récolter de nouveaux 429.
                _gate(host, account, cls).defer(time.monotonic() + retry_after)
            wait = (retry_after if retry_after is not None
                    else base * (2 ** (attempt - 1)))
            if not _may_retry(wait):
                # Plus de tentative : l'erreur HTTP réelle (avec son statut)
                # est levée sans dormir.
                logger.warning(
                    f"Échec définitif sur {safe_url}: HTTP {status} "
                    f"(tentative {attempt}/{attempts_max})"
                )
                raise _fail(_http_error(response, url))
            logger.warning(
                f"Statut {status} sur {safe_url} "
                f"(tentative {attempt}/{attempts_max}), attente {wait:.1f}s"
            )
            _sleep_before_retry(wait)
            continue

        # 4xx définitif : un nouvel essai ne changerait rien.
        log = logger.debug if status in (404, 410) else logger.warning
        log(f"Statut {status} définitif sur {safe_url} : pas de nouvel essai")
        raise _fail(_http_error(response, url))


# === Entretien du cache disque ===

# Compactage (VACUUM) seulement si l'espace libre atteint cette part du fichier
# (au moins 1 Mio) et si les réponses conservées pèsent au plus
# _VACUUM_MAX_LIVE_BYTES : la copie reste brève (bien moins que les 5 s
# d'attente de verrou de sqlite3), une autre moisson qui lit ou écrit le même
# fichier ne fait qu'attendre.
_VACUUM_MIN_FREE_RATIO = 0.5
_VACUUM_MIN_FREE_BYTES = 1024 * 1024
_VACUUM_MAX_LIVE_BYTES = 64 * 1024 * 1024


def prune_sqlite_cache(cache, max_bytes: Optional[int] = None,
                       vacuum: bool = True) -> Dict[str, Any]:
    """
    Entretien d'un cache requests-cache (moteur SQLite) : borne sa taille.

    requests-cache ne retire jamais une réponse expirée (il ne la remplace que
    si la même requête revient) : sans entretien, chaque requête distincte
    reste dans le fichier pour toujours. Étapes :

    1. retrait des réponses expirées (``expires <= maintenant``, en SQL) et
       des redirections orphelines ;
    2. si ``max_bytes`` est positif et que les réponses restantes le
       dépassent, retrait des plus anciennes (échéance la plus proche, donc
       écrites le plus tôt à durée de vie égale) jusqu'à revenir sous le
       plafond ;
    3. compactage (``VACUUM``) sur une connexion dédiée quand l'espace libre
       domine le fichier et que le reste est petit (voir
       ``_VACUUM_MIN_FREE_RATIO``, ``_VACUUM_MAX_LIVE_BYTES``). Le ``vacuum``
       de requests-cache s'exécute dans une transaction et échoue sans bruit
       (« cannot VACUUM from within a transaction ») : il n'est pas utilisé.
       Sans compactage, les pages libérées sont réutilisées par les écritures
       suivantes : le fichier ne grossit plus, sans rétrécir.

    Un compactage impossible (verrou tenu par une autre connexion) est
    journalisé et ignoré. Les autres erreurs sont propagées à l'appelant.

    Returns:
        ``{"expired_removed", "size_removed", "live_bytes", "bytes_before",
        "bytes_after", "vacuumed"}`` (``None`` pour une valeur inconnue avec
        un autre moteur que SQLite).
    """
    responses = getattr(cache, "responses", None)
    table = getattr(responses, "table_name", None)
    db_path = getattr(responses, "db_path", None)
    if responses is None or table is None or db_path is None:
        # Autre moteur : purge générique des réponses expirées.
        cache.delete(expired=True)
        return {"expired_removed": None, "size_removed": 0, "live_bytes": None,
                "bytes_before": None, "bytes_after": None, "vacuumed": False}

    import sqlite3

    bytes_before = responses.size()
    rows_before = len(responses)
    cache.delete(expired=True, vacuum=False)
    expired_removed = rows_before - len(responses)

    with responses.connection() as con:
        rows = con.execute(
            f"SELECT key, length(value) FROM {table} ORDER BY expires, rowid"
        ).fetchall()
    live = sum(int(length or 0) for _, length in rows)
    victims: List[str] = []
    if max_bytes is not None and max_bytes > 0 and live > max_bytes:
        for key, length in rows:
            if live <= max_bytes:
                break
            victims.append(key)
            live -= int(length or 0)
        cache.delete(*victims, vacuum=False)

    vacuumed = False
    if vacuum:
        with responses.connection() as con:
            page_size = con.execute("PRAGMA page_size").fetchone()[0]
            free_pages = con.execute("PRAGMA freelist_count").fetchone()[0]
        free = int(page_size) * int(free_pages)
        size_now = responses.size()
        if (free >= _VACUUM_MIN_FREE_BYTES
                and free >= _VACUUM_MIN_FREE_RATIO * size_now
                and live <= _VACUUM_MAX_LIVE_BYTES):
            try:
                con = sqlite3.connect(str(db_path), timeout=1.0,
                                      isolation_level=None)
                try:
                    con.execute("VACUUM")
                finally:
                    con.close()
                vacuumed = True
            except sqlite3.Error as exc:
                logger.info(
                    f"Cache HTTP : compactage reporté ({type(exc).__name__}: "
                    f"{exc})"
                )

    return {
        "expired_removed": expired_removed,
        "size_removed": len(victims),
        "live_bytes": live,
        "bytes_before": bytes_before,
        "bytes_after": responses.size(),
        "vacuumed": vacuumed,
    }


# === Client ===

class HTTPClient:
    """
    Session requests partagée, soumise à la politique commune.

    Les codes 429 et 5xx transitoires déclenchent de nouveaux essais bornés ;
    les autres codes 4xx sont levés dès la première tentative, car ils
    traduisent une erreur de requête qu'un nouvel essai ne résoudrait pas.
    L'usage d'une session partagée réutilise les connexions TCP.
    """

    # Codes HTTP qui justifient un nouvel essai
    RETRYABLE_STATUS = RETRYABLE_STATUS

    def __init__(self, use_cache: Optional[bool] = None,
                 cache_ttl: Optional[int] = None,
                 cache_dir: Optional[str] = None):
        """
        Initialise la session avec les en-têtes par défaut.

        Args:
            use_cache: Option à trois états. ``None`` (défaut) hérite de
                ``MOISSON_CACHE`` ; ``True`` / ``False`` l'imposent.
            cache_ttl: TTL du cache en secondes. Si None, ``HTTP_CACHE_TTL``
                (``MOISSON_CACHE_TTL``, défaut 86400 = 24h).
            cache_dir: Répertoire de cache. Si None, lit
                MOISSON_CACHE_DIR ou utilise ~/.cache/moisson_shs/http.
        """
        use_cache = resolve_cache_option(use_cache)
        if cache_ttl is None:
            cache_ttl = HTTP_CACHE_TTL
        if cache_dir is None:
            cache_dir = os.environ.get(
                "MOISSON_CACHE_DIR",
                str(Path.home() / ".cache" / "moisson_shs" / "http"),
            )

        self.cache_enabled = use_cache
        self.cache_ttl = cache_ttl
        # Signal d'annulation de l'exécution qui utilise ce client (OPT-07) :
        # transmis à chaque requête (policy_get). None : aucune annulation.
        self.cancel_signal: Optional[CancelSignal] = None
        # Vue d'un autre client (with_cancel) : ne ferme pas la session.
        self._is_view = False
        if use_cache:
            # Lazy import : requests-cache est optionnel
            try:
                import requests_cache
                cache_path = Path(cache_dir)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.session = requests_cache.CachedSession(
                    cache_name=str(cache_path),
                    backend="sqlite",
                    expire_after=timedelta(seconds=cache_ttl),
                    # Ne cache que les codes succès et redirections stables
                    allowable_codes=[200, 203, 300, 301, 308],
                    # Jamais un corps non JSON servi en 200 (page d'erreur
                    # HTML passagère) : cf. _cacheable_response
                    filter_fn=_cacheable_response,
                    # Sépare le cache par URL+params, ignore les en-têtes
                    # volatils (date, etc.)
                    match_headers=False,
                )
                logger.info(
                    f"Cache HTTP activé : {cache_path} (TTL {cache_ttl}s)"
                )
            except ImportError:
                logger.warning(
                    "requests-cache non installé, cache HTTP désactivé. "
                    "Installer avec : pip install requests-cache"
                )
                self.cache_enabled = False
                self.session = requests.Session()
            except Exception as exc:  # noqa: BLE001 - cache optionnel
                # Répertoire non inscriptible, base SQLite illisible… Le cache
                # étant actif par défaut côté MCP (OPT-09), son indisponibilité
                # ne doit pas faire échouer la moisson : elle se fait sans.
                logger.warning(
                    f"Cache HTTP indisponible ({type(exc).__name__}: {exc}) : "
                    f"moisson sans cache"
                )
                self.cache_enabled = False
                self.session = requests.Session()
        else:
            self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": USER_AGENT,
            # Accept-Encoding gzip réduit le volume transféré,
            # particulièrement utile pour les gros lots Solr
            "Accept-Encoding": "gzip, deflate",
        })
        # Augmente le pool de connexions pour les requêtes concurrentes
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout=None,
        headers: Optional[Dict[str, str]] = None,
        *,
        min_interval: Optional[float] = None,
        use_cache: bool = True,
        max_attempts: Optional[int] = None,
        response_hook: Optional[Callable[[Any], None]] = None,
        budget: Optional[float] = None,
        cancel_event: Any = None,
    ) -> requests.Response:
        """
        Effectue un GET selon la politique commune (cf. ``policy_get``).

        Args:
            url: URL cible
            params: Paramètres de query string
            timeout: ``None`` (``HTTP_TIMEOUT``), tuple ``(connexion,
                lecture)`` ou scalaire (lecture ; connexion bornée par
                ``HTTP_CONNECT_TIMEOUT``)
            headers: En-têtes supplémentaires propres à cette requête
                (ex : Authorization Bearer, x-api-key). Fusionnés par
                requests avec les en-têtes par défaut de la session, sans
                muter celle-ci — sûr quand la session est partagée entre
                plusieurs sources.
            min_interval: Intervalle de politesse propre à cette requête
                (défaut : celui de l'hôte).
            use_cache: ``False`` : ni lecture ni écriture en cache.
            max_attempts: Tentatives maximales sur erreur transitoire.
            response_hook: Voir ``policy_get``.
            budget: Budget global de la requête logique (secondes).
            cancel_event: Signal d'annulation propre à cette requête (défaut :
                ``self.cancel_signal``, celui de l'exécution).

        Returns:
            L'objet Response de requests (statut < 400)

        Raises:
            requests.HTTPError: 4xx définitif dès la première tentative ;
                429/5xx persistant après les tentatives permises.
            requests.RequestException: erreur réseau persistante.
            OperationCancelled: annulation ou échéance de l'exécution.
        """
        cancel = (cancel_event if cancel_event is not None
                  else getattr(self, "cancel_signal", None))
        return policy_get(
            self.session, url, params=params, headers=headers,
            timeout=timeout, max_attempts=max_attempts,
            min_interval=min_interval, use_cache=use_cache,
            response_hook=response_hook, budget=budget, cancel=cancel,
        )

    def with_cancel(self, cancel: Any) -> "HTTPClient":
        """
        Vue de ce client portant son propre signal d'annulation.

        La vue partage la session (connexions, cache) : elle sert à annuler
        les requêtes d'UN appel fait sur un client partagé de longue durée
        (client par défaut des lookups) sans toucher aux autres appels. Sa
        fermeture ne ferme pas la session partagée.
        """
        view = copy.copy(self)
        view.cancel_signal = CancelSignal.coerce(cancel)
        view._is_view = True
        return view

    def prune_cache(self, max_bytes: Optional[int] = None,
                    vacuum: bool = True) -> Optional[Dict[str, Any]]:
        """
        Entretien du cache disque de ce client (``prune_sqlite_cache``) :
        réponses expirées retirées, plafond ``max_bytes`` (octets, ``None``
        ou 0 : aucun), compactage éventuel. ``None`` si le cache est inactif.
        """
        if not self.cache_enabled or not _is_cached_session(self.session):
            return None
        return prune_sqlite_cache(self.session.cache, max_bytes=max_bytes,
                                  vacuum=vacuum)

    @staticmethod
    def _was_cached(response) -> bool:
        """Renvoie True si la réponse provient du cache local."""
        return getattr(response, "from_cache", False)

    def close(self):
        """Ferme la session HTTP et libère les connexions (sauf pour une vue)."""
        if getattr(self, "_is_view", False):
            return
        self.session.close()

    def __enter__(self):
        """Support du context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Fermeture automatique en sortie de bloc with."""
        self.close()


# === Client partagé par défaut ===

_default_client: Optional[HTTPClient] = None
_default_lock = threading.Lock()


def get_default_client() -> HTTPClient:
    """
    Client partagé pour les appels qui n'en reçoivent pas (lookups, DOI).

    Créé à la première utilisation, avec l'option cache héritée de
    l'environnement à ce moment-là (``MOISSON_CACHE``).
    """
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = HTTPClient()
        return _default_client


def close_default_client() -> None:
    """Ferme et oublie le client partagé (il sera recréé à la demande)."""
    global _default_client
    with _default_lock:
        if _default_client is not None:
            _default_client.close()
        _default_client = None
