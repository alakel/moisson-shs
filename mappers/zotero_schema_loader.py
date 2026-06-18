"""
Chargement et cache du schéma Zotero officiel.

Le schéma Zotero officiel (https://api.zotero.org/schema) définit
les itemTypes, leurs champs autorisés et les creatorTypes valides.
Il est mis à jour par Zotero au fil des évolutions du logiciel.

Stratégie :
1. Premier appel : téléchargement depuis api.zotero.org, mise en cache
   sur disque dans ~/.cache/moisson_shs/zotero_schema.json (ou
   $MOISSON_SCHEMA_CACHE).
2. Appels suivants : lecture du cache si valide (< 30 jours).
3. En cas d'échec réseau ou de cache absent : fallback sur la table
   ZOTERO_ITEM_FIELDS embarquée dans zotero_schema.py.

Ce module est utilisé par validate_and_clean pour filtrer les champs
incompatibles avec l'itemType déclaré. La version officielle prime
sur la version embarquée car elle reflète les évolutions récentes
du schéma (nouveaux itemTypes comme preprint, dataset, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Set, Optional

import requests

from config import USER_AGENT, HTTP_TIMEOUT
from mappers.zotero_schema import ZOTERO_ITEM_FIELDS, COMMON_FIELDS

logger = logging.getLogger(__name__)

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
CACHE_MAX_AGE_SECONDS = int(
    os.environ.get("MOISSON_SCHEMA_CACHE_AGE", str(30 * 86400))
)


# Cache mémoire pour la durée du processus
_MEMORY_CACHE: Optional[Dict] = None


def _is_cache_valid(path: Path) -> bool:
    """Le cache est-il présent et frais ?"""
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_MAX_AGE_SECONDS


def _load_from_cache(path: Path) -> Optional[Dict]:
    """Charge le schéma depuis le cache disque, None si échec."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Cache schéma corrompu : {e}")
        return None


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


def _fetch_from_network() -> Optional[Dict]:
    """Télécharge le schéma officiel depuis api.zotero.org."""
    try:
        r = requests.get(
            ZOTERO_SCHEMA_URL,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        schema = r.json()
        if not isinstance(schema, dict) or "itemTypes" not in schema:
            logger.warning(
                "Schéma Zotero téléchargé mais structure inattendue"
            )
            return None
        return schema
    except (requests.RequestException, json.JSONDecodeError) as e:
        logger.warning(f"Échec téléchargement schéma Zotero : {e}")
        return None


def load_zotero_schema(force_refresh: bool = False) -> Optional[Dict]:
    """
    Charge le schéma Zotero officiel.

    Stratégie de résolution :
    1. Cache mémoire (si déjà chargé dans cette session)
    2. Cache disque (si frais)
    3. Téléchargement réseau
    4. None si tout échoue (l'appelant fera fallback sur schéma embarqué)

    Args:
        force_refresh: Si True, ignore les caches et redownload

    Returns:
        Dict du schéma Zotero ou None
    """
    global _MEMORY_CACHE

    if not force_refresh and _MEMORY_CACHE is not None:
        return _MEMORY_CACHE

    # Cache disque
    if not force_refresh and _is_cache_valid(CACHE_PATH):
        schema = _load_from_cache(CACHE_PATH)
        if schema:
            logger.debug(f"Schéma Zotero chargé depuis cache : {CACHE_PATH}")
            _MEMORY_CACHE = schema
            return schema

    # Réseau
    logger.info("Téléchargement du schéma Zotero officiel...")
    schema = _fetch_from_network()
    if schema:
        _save_to_cache(CACHE_PATH, schema)
        _MEMORY_CACHE = schema
        return schema

    # Fallback : cache même si périmé
    if CACHE_PATH.exists():
        logger.warning(
            f"Réseau indisponible, utilisation cache périmé : {CACHE_PATH}"
        )
        schema = _load_from_cache(CACHE_PATH)
        if schema:
            _MEMORY_CACHE = schema
            return schema

    return None


def get_allowed_fields(item_type: str) -> Set[str]:
    """
    Renvoie l'ensemble des champs autorisés pour un itemType donné.

    Priorité : schéma officiel chargé > table embarquée > fallback document.

    Args:
        item_type: Nom du type Zotero (ex : "journalArticle")

    Returns:
        Set de noms de champs valides (incluant COMMON_FIELDS)
    """
    schema = load_zotero_schema()
    if schema:
        for it in schema.get("itemTypes", []):
            if it.get("itemType") == item_type:
                fields = {f["field"] for f in it.get("fields", [])}
                return fields | COMMON_FIELDS

    # Fallback sur table embarquée
    embedded = ZOTERO_ITEM_FIELDS.get(item_type, ZOTERO_ITEM_FIELDS["document"])
    return embedded | COMMON_FIELDS


def get_allowed_creator_types(item_type: str) -> Set[str]:
    """
    Renvoie les creatorTypes valides pour un itemType.

    Args:
        item_type: Nom du type Zotero

    Returns:
        Set de creatorTypes (vide si non détectable)
    """
    schema = load_zotero_schema()
    if schema:
        for it in schema.get("itemTypes", []):
            if it.get("itemType") == item_type:
                return {c["creatorType"] for c in it.get("creatorTypes", [])}
    # Fallback : on accepte les creatorTypes les plus courants
    return {
        "author", "editor", "translator", "contributor", "seriesEditor",
        "bookAuthor", "reviewedAuthor",
    }


def is_valid_item_type(item_type: str) -> bool:
    """Test si un itemType est reconnu par Zotero (officiel ou embarqué)."""
    schema = load_zotero_schema()
    if schema:
        valid = {it["itemType"] for it in schema.get("itemTypes", [])}
        return item_type in valid
    return item_type in ZOTERO_ITEM_FIELDS


def clear_cache() -> None:
    """Supprime le cache disque et mémoire (utile pour tests)."""
    global _MEMORY_CACHE
    _MEMORY_CACHE = None
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()
