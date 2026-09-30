"""
Chargement et cache du schéma Zotero officiel.

Le schéma Zotero officiel (https://api.zotero.org/schema) définit
les itemTypes, leurs champs autorisés et les creatorTypes valides.
Il est mis à jour par Zotero au fil des évolutions du logiciel.

Stratégie :
0. Si $MOISSON_ZOTERO_SCHEMA_FILE désigne un fichier JSON lisible au
   format du schéma officiel, le schéma est lu depuis ce fichier, sans
   réseau ni cache disque (schéma figé : la suite de tests s'en sert pour
   ne dépendre ni du réseau ni de l'état de ~/.cache).
1. Premier appel : lecture du cache disque s'il est valide (< 30 jours),
   sinon téléchargement depuis api.zotero.org et mise en cache dans
   ~/.cache/moisson_shs/zotero_schema.json (ou $MOISSON_SCHEMA_CACHE).
2. En cas d'échec réseau : cache disque même périmé.
3. À défaut, l'échec est mémorisé pendant $MOISSON_SCHEMA_RETRY_AFTER
   secondes (600 par défaut) : pendant cette fenêtre, aucun nouvel accès
   réseau, l'appelant se replie immédiatement sur la table
   ZOTERO_ITEM_FIELDS embarquée dans zotero_schema.py. Sans cette mémoire,
   chaque notice validée relançait un téléchargement (8 s de délai de
   connexion quand l'hôte ne répond pas).

États du chargeur (cf. schema_state()) :
- « non_initialise » : aucun chargement tenté, ou fenêtre d'échec expirée ;
- « charge » : schéma officiel en mémoire pour la durée du processus ;
- « indisponible » : dernier chargement en échec, repli embarqué sans
  accès réseau jusqu'à l'échéance de la fenêtre.

L'initialisation est protégée par un verrou : plusieurs threads qui
valident des notices en même temps ne déclenchent qu'une seule tentative
réseau. Un thread qui attendait le verrou pendant qu'une tentative se
terminait en reprend l'issue au lieu d'en lancer une autre, y compris
quand la mémoire d'échec est désactivée (MOISSON_SCHEMA_RETRY_AFTER=0).
Le verrou est tenu pendant toute la tentative : au pire le délai de
connexion (8 s par défaut) plus, par attente de données, le délai de
lecture plafonné à SCHEMA_READ_TIMEOUT_MAX (15 s) ; requests ne borne pas
la durée totale d'un téléchargement qui arrive au compte-gouttes.

Les champs autorisés et les creatorTypes sont précalculés par
itemType pour le schéma chargé ; ce précalcul est refait si le schéma en
mémoire change (autre objet ou autre version).

Le rafraîchissement explicite (force_refresh=True) contourne les caches
et la fenêtre d'échec ; clear_cache() remet le chargeur à zéro.

Ce module est utilisé par validate_and_clean pour filtrer les champs
incompatibles avec l'itemType déclaré. La version officielle prime
sur la version embarquée car elle reflète les évolutions récentes
du schéma (nouveaux itemTypes comme preprint, dataset, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

import requests

from config import USER_AGENT, HTTP_TIMEOUT
from mappers.zotero_schema import ZOTERO_ITEM_FIELDS, COMMON_FIELDS

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Entier lu dans l'environnement ; valeur absente ou invalide → défaut."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} invalide : valeur par défaut {default}")
        return default
    return max(minimum, value)


# URL du schéma Zotero officiel
ZOTERO_SCHEMA_URL = "https://api.zotero.org/schema"

# Cache local — configurable via env
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "moisson_shs"
CACHE_PATH = Path(
    os.environ.get(
        "MOISSON_SCHEMA_CACHE",
        str(DEFAULT_CACHE_DIR / "zotero_schema.json"),
    )
)

# Durée de validité du cache (30 jours par défaut)
CACHE_MAX_AGE_SECONDS = _env_int("MOISSON_SCHEMA_CACHE_AGE", 30 * 86400)

# Fenêtre pendant laquelle un échec de chargement est mémorisé (secondes).
# 0 désactive la mémoire d'échec : chaque appel successif retente, mais les
# appels concurrents qui attendaient une tentative en cours en partagent
# l'issue (pas de tentatives en file les unes derrière les autres).
SCHEMA_RETRY_AFTER_SECONDS = _env_int("MOISSON_SCHEMA_RETRY_AFTER", 600)

# Délai de lecture maximal du téléchargement du schéma (secondes). Le verrou
# est tenu pendant la tentative et tous les threads de validation l'attendent :
# le délai de lecture général (HTTP_TIMEOUT, 45 s par défaut) est plafonné
# ici. Il s'agit d'un délai par attente de données, pas d'une durée totale.
SCHEMA_READ_TIMEOUT_MAX = 15.0
SCHEMA_HTTP_TIMEOUT = (
    HTTP_TIMEOUT[0],
    min(HTTP_TIMEOUT[1], SCHEMA_READ_TIMEOUT_MAX),
)

# Fichier de schéma figé (lu à chaque chargement, pas à l'import, pour que
# la suite de tests puisse poser la variable avant le premier chargement)
SCHEMA_FILE_ENV = "MOISSON_ZOTERO_SCHEMA_FILE"

# États explicites du chargeur
ETAT_NON_INITIALISE = "non_initialise"
ETAT_CHARGE = "charge"
ETAT_INDISPONIBLE = "indisponible"


# Cache mémoire pour la durée du processus. Les tests peuvent l'affecter
# directement : l'état « charge » se déduit de sa présence.
_MEMORY_CACHE: Optional[Dict] = None

# Échéance (horloge monotone) de la fenêtre d'échec ; None hors échec
_UNAVAILABLE_UNTIL: Optional[float] = None

# Provenance du schéma en mémoire : "fichier", "disque", "reseau",
# "disque_perime" (diagnostic uniquement)
_SCHEMA_ORIGIN: Optional[str] = None

# Sérialise les chargements : une seule tentative réseau à la fois
_LOCK = threading.Lock()

# Nombre de tentatives de chargement terminées (succès ou échec), incrémenté
# sous verrou. Un appel qui constate, une fois le verrou obtenu, que ce
# compteur a changé depuis son arrivée reprend l'issue de cette tentative au
# lieu d'en lancer une nouvelle.
_ATTEMPTS_DONE = 0


class _SchemaIndex:
    """Champs autorisés et creatorTypes précalculés pour un schéma donné."""

    __slots__ = ("schema", "version", "fields", "creator_types", "item_types")

    def __init__(self, schema: Dict) -> None:
        self.schema = schema
        self.version = schema.get("version")
        fields: Dict[str, FrozenSet[str]] = {}
        creators: Dict[str, FrozenSet[str]] = {}
        for it in schema.get("itemTypes") or []:
            if not isinstance(it, dict) or not it.get("itemType"):
                continue
            name = it["itemType"]
            if name in fields:
                # Première occurrence retenue, comme la recherche linéaire
                # historique
                continue
            fields[name] = frozenset(
                f["field"] for f in it.get("fields") or []
                if isinstance(f, dict) and f.get("field")
            ) | COMMON_FIELDS
            creators[name] = frozenset(
                c["creatorType"] for c in it.get("creatorTypes") or []
                if isinstance(c, dict) and c.get("creatorType")
            )
        self.fields = fields
        self.creator_types = creators
        self.item_types = frozenset(fields)

    def matches(self, schema: Dict) -> bool:
        """Le précalcul correspond-il encore au schéma en mémoire ?"""
        return self.schema is schema and self.version == schema.get("version")


# Précalcul du schéma officiel en mémoire (reconstruit s'il change)
_INDEX: Optional[_SchemaIndex] = None

# Table embarquée précalculée (constante pour la durée du processus)
_EMBEDDED_FIELDS: Dict[str, FrozenSet[str]] = {
    item_type: frozenset(fields) | COMMON_FIELDS
    for item_type, fields in ZOTERO_ITEM_FIELDS.items()
}

# creatorTypes acceptés quand le schéma officiel ne dit rien
_DEFAULT_CREATOR_TYPES: FrozenSet[str] = frozenset({
    "author", "editor", "translator", "contributor", "seriesEditor",
    "bookAuthor", "reviewedAuthor",
})


def _clock() -> float:
    """Horloge monotone de la fenêtre d'échec (substituable en test)."""
    return time.monotonic()


def _looks_like_schema(data: Any) -> bool:
    """Structure minimale attendue d'un schéma Zotero."""
    return isinstance(data, dict) and isinstance(data.get("itemTypes"), list)


def _is_cache_valid(path: Path) -> bool:
    """Le cache est-il présent et frais ?"""
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_MAX_AGE_SECONDS


def _load_from_cache(path: Path) -> Optional[Dict]:
    """Charge le schéma depuis le cache disque, None si échec."""
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"Cache schéma corrompu : {e}")
        return None
    if not _looks_like_schema(schema):
        logger.warning(f"Cache schéma de structure inattendue : {path}")
        return None
    return schema


def _save_to_cache(path: Path, schema: Dict) -> None:
    """Persiste le schéma téléchargé."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"Schéma Zotero caché : {path}")
    except OSError as e:
        logger.warning(f"Impossible d'écrire le cache schéma : {e}")


def _schema_file_from_env() -> Optional[Path]:
    """Chemin désigné par $MOISSON_ZOTERO_SCHEMA_FILE, None si absent."""
    raw = os.environ.get(SCHEMA_FILE_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def _load_from_schema_file(path: Path) -> Optional[Dict]:
    """Lit le schéma figé ; None (avec avertissement) s'il est inutilisable."""
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(
            f"{SCHEMA_FILE_ENV} illisible ({path}) : {e} ; "
            f"stratégie ordinaire (cache disque, réseau)"
        )
        return None
    if not _looks_like_schema(schema):
        logger.warning(
            f"{SCHEMA_FILE_ENV} ({path}) n'a pas la structure d'un schéma "
            f"Zotero ; stratégie ordinaire (cache disque, réseau)"
        )
        return None
    return schema


def _fetch_from_network() -> Optional[Dict]:
    """Télécharge le schéma officiel depuis api.zotero.org."""
    try:
        r = requests.get(
            ZOTERO_SCHEMA_URL,
            timeout=SCHEMA_HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        schema = r.json()
        if not _looks_like_schema(schema):
            logger.warning(
                "Schéma Zotero téléchargé mais structure inattendue"
            )
            return None
        return schema
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Échec téléchargement schéma Zotero : {e}")
        return None


def _failure_window_active() -> bool:
    """Un échec récent interdit-il encore toute nouvelle tentative ?"""
    until = _UNAVAILABLE_UNTIL
    return until is not None and _clock() < until


def _set_loaded(schema: Dict, origin: str) -> Dict:
    """Passe à l'état « charge » (verrou tenu par l'appelant)."""
    global _MEMORY_CACHE, _UNAVAILABLE_UNTIL, _SCHEMA_ORIGIN
    _MEMORY_CACHE = schema
    _UNAVAILABLE_UNTIL = None
    _SCHEMA_ORIGIN = origin
    return schema


def _load_locked(force_refresh: bool) -> Optional[Dict]:
    """Résolution complète du schéma (verrou tenu par l'appelant)."""
    global _UNAVAILABLE_UNTIL

    # 0. Schéma figé désigné par l'environnement : ni réseau ni cache disque
    schema_file = _schema_file_from_env()
    if schema_file is not None:
        schema = _load_from_schema_file(schema_file)
        if schema is not None:
            logger.debug(f"Schéma Zotero chargé depuis {schema_file}")
            return _set_loaded(schema, "fichier")

    # 1. Cache disque frais
    if not force_refresh and _is_cache_valid(CACHE_PATH):
        schema = _load_from_cache(CACHE_PATH)
        if schema:
            logger.debug(f"Schéma Zotero chargé depuis cache : {CACHE_PATH}")
            return _set_loaded(schema, "disque")

    # 2. Réseau
    logger.info("Téléchargement du schéma Zotero officiel...")
    schema = _fetch_from_network()
    if schema:
        _save_to_cache(CACHE_PATH, schema)
        return _set_loaded(schema, "reseau")

    # 3. Fallback : cache même si périmé
    if CACHE_PATH.exists():
        logger.warning(
            f"Réseau indisponible, utilisation cache périmé : {CACHE_PATH}"
        )
        schema = _load_from_cache(CACHE_PATH)
        if schema:
            return _set_loaded(schema, "disque_perime")

    # 4. Échec. Un rafraîchissement explicite qui échoue laisse en place le
    # schéma déjà chargé (comportement historique : None pour cet appel,
    # l'ancien schéma continue de servir les appels ordinaires).
    if force_refresh and _MEMORY_CACHE is not None:
        return None
    _UNAVAILABLE_UNTIL = _clock() + SCHEMA_RETRY_AFTER_SECONDS
    logger.warning(
        f"Schéma Zotero indisponible : table embarquée utilisée, "
        f"nouvelle tentative dans {SCHEMA_RETRY_AFTER_SECONDS} s au plus tôt"
    )
    return None


def load_zotero_schema(force_refresh: bool = False) -> Optional[Dict]:
    """
    Charge le schéma Zotero officiel.

    Stratégie de résolution :
    1. Cache mémoire (si déjà chargé dans cette session)
    2. Fenêtre d'échec active → None immédiat, sans réseau
    3. Fichier figé $MOISSON_ZOTERO_SCHEMA_FILE (si lisible)
    4. Cache disque (si frais)
    5. Téléchargement réseau
    6. Cache disque périmé
    7. None si tout échoue (l'appelant fera fallback sur schéma embarqué) ;
       l'échec est mémorisé pendant SCHEMA_RETRY_AFTER_SECONDS

    Args:
        force_refresh: Si True, ignore les caches et la fenêtre d'échec
            et redownload (ou relit le fichier figé s'il est désigné)

    Returns:
        Dict du schéma Zotero ou None
    """
    global _ATTEMPTS_DONE

    # Génération lue avant toute vérification : une tentative qui se termine
    # après ce point vaut aussi pour cet appel
    seen = _ATTEMPTS_DONE

    # Chemin rapide sans verrou : lecture de références, atomique en CPython
    if not force_refresh:
        schema = _MEMORY_CACHE
        if schema is not None:
            return schema
        if _failure_window_active():
            return None

    with _LOCK:
        if not force_refresh:
            # Un autre thread a pu conclure pendant l'attente du verrou
            if _MEMORY_CACHE is not None:
                return _MEMORY_CACHE
            if _failure_window_active() or _ATTEMPTS_DONE != seen:
                # Échec conclu pendant l'attente (fenêtre active, ou
                # désactivée mais tentative terminée entre-temps)
                return None
        try:
            return _load_locked(force_refresh)
        finally:
            _ATTEMPTS_DONE += 1


def _index_for(schema: Dict) -> _SchemaIndex:
    """Précalcul associé au schéma, reconstruit si le schéma a changé."""
    global _INDEX
    index = _INDEX
    if index is None or not index.matches(schema):
        # Construction éventuellement concurrente : résultat identique,
        # le dernier écrit gagne, sans incidence
        index = _SchemaIndex(schema)
        _INDEX = index
    return index


def get_allowed_fields(item_type: str) -> FrozenSet[str]:
    """
    Renvoie l'ensemble des champs autorisés pour un itemType donné.

    Priorité : schéma officiel chargé > table embarquée > fallback document.
    L'ensemble renvoyé est précalculé et partagé : il est immuable.

    Args:
        item_type: Nom du type Zotero (ex : "journalArticle")

    Returns:
        frozenset de noms de champs valides (incluant COMMON_FIELDS)
    """
    schema = load_zotero_schema()
    if schema:
        fields = _index_for(schema).fields.get(item_type)
        if fields is not None:
            return fields

    # Fallback sur table embarquée
    return _EMBEDDED_FIELDS.get(item_type, _EMBEDDED_FIELDS["document"])


def get_allowed_creator_types(item_type: str) -> FrozenSet[str]:
    """
    Renvoie les creatorTypes valides pour un itemType.

    Args:
        item_type: Nom du type Zotero

    Returns:
        frozenset de creatorTypes (vide si le schéma n'en déclare aucun)
    """
    schema = load_zotero_schema()
    if schema:
        creators = _index_for(schema).creator_types.get(item_type)
        if creators is not None:
            return creators
    # Fallback : on accepte les creatorTypes les plus courants
    return _DEFAULT_CREATOR_TYPES


def is_valid_item_type(item_type: str) -> bool:
    """Test si un itemType est reconnu par Zotero (officiel ou embarqué)."""
    schema = load_zotero_schema()
    if schema:
        return item_type in _index_for(schema).item_types
    return item_type in ZOTERO_ITEM_FIELDS


def schema_state() -> str:
    """
    État courant du chargeur.

    Returns:
        ETAT_CHARGE, ETAT_INDISPONIBLE (fenêtre d'échec active) ou
        ETAT_NON_INITIALISE (rien tenté, ou fenêtre expirée : le prochain
        appel retentera)
    """
    if _MEMORY_CACHE is not None:
        return ETAT_CHARGE
    if _failure_window_active():
        return ETAT_INDISPONIBLE
    return ETAT_NON_INITIALISE


def schema_status() -> Dict[str, Any]:
    """Diagnostic : état, provenance, version, délai avant nouvelle tentative."""
    state = schema_state()
    schema = _MEMORY_CACHE
    retry_in = None
    if state == ETAT_INDISPONIBLE and _UNAVAILABLE_UNTIL is not None:
        retry_in = max(0.0, round(_UNAVAILABLE_UNTIL - _clock(), 1))
    return {
        "etat": state,
        "origine": _SCHEMA_ORIGIN if schema is not None else None,
        "version": schema.get("version") if isinstance(schema, dict) else None,
        "nouvelle_tentative_dans_s": retry_in,
    }


def reset_state() -> None:
    """
    Remet le chargeur à l'état « non_initialise » sans toucher au disque :
    oublie le schéma en mémoire, la fenêtre d'échec et le précalcul.
    """
    global _MEMORY_CACHE, _UNAVAILABLE_UNTIL, _SCHEMA_ORIGIN, _INDEX
    with _LOCK:
        _MEMORY_CACHE = None
        _UNAVAILABLE_UNTIL = None
        _SCHEMA_ORIGIN = None
        _INDEX = None


def clear_cache() -> None:
    """Supprime le cache disque et mémoire (utile pour tests)."""
    reset_state()
    try:
        CACHE_PATH.unlink()
    except FileNotFoundError:
        pass
