"""
Registre central des connecteurs de sources bibliographiques.

Ce module centralise la déclaration des sources moissonnables.
Tout code qui itère sur les sources (main.py, mcp_server.py, tests)
consomme `CONNECTORS` plutôt que de hardcoder des `if/elif`.

Pour ajouter une nouvelle source :
1. Créer `connectors/<nom>.py` héritant de `base.Connector`
2. Créer `mappers/<nom>_mapper.py` si nécessaire
3. Importer la classe ici et l'ajouter dans `CONNECTORS`
4. (Optionnel) Ajouter une entrée `GROUP_PROXY_SOURCES` si la source
   utilise Isidore comme proxy (séquencement après HAL/Isidore natifs)

Aucune autre modification du code n'est requise.
"""

from typing import Dict, Type

from connectors.base import Connector
from connectors.hal import HALConnector
from connectors.isidore import IsidoreConnector
from connectors.openedition import OpenEditionConnector
from connectors.cairn import CairnConnector
from connectors.persee import PerseeConnector
from connectors.theses_fr import ThesesFrConnector
from connectors.erudit import EruditConnector
# Sources internationales (REST natif scholarly)
from connectors.crossref import CrossrefConnector
from connectors.openalex import OpenAlexConnector
from connectors.semantic_scholar import SemanticScholarConnector
from connectors.core import CoreConnector


# === Registre principal ===
# Clé : nom court utilisé en CLI (--sources hal isidore ...) et en MCP
# Valeur : classe Connector à instancier
CONNECTORS: Dict[str, Type[Connector]] = {
    # --- Sources francophones (HAL natif + proxy Isidore) ---
    "hal": HALConnector,
    "isidore": IsidoreConnector,
    "openedition": OpenEditionConnector,
    "cairn": CairnConnector,
    "persee": PerseeConnector,
    "theses_fr": ThesesFrConnector,
    "erudit": EruditConnector,
    # --- Sources internationales (REST natif, métadonnées riches) ---
    "crossref": CrossrefConnector,
    "openalex": OpenAlexConnector,
    "semantic_scholar": SemanticScholarConnector,
    "core": CoreConnector,
}


# === Topologie pour le parallélisme ===
# Les sources "proxy" utilisent Isidore en interne ; il vaut mieux les
# ordonnancer après les sources natives pour ne pas saturer l'API
# Isidore. Le main.py et mcp_server.py exécutent group_native d'abord,
# puis group_proxy.
GROUP_PROXY_SOURCES = {"openedition", "cairn", "persee", "theses_fr", "erudit"}


# === Préréglages de groupes pour --sources / MCP ===
# Les sources internationales sont des API REST natives (pas de proxy Isidore)
# et trois d'entre elles exigent une clé API. Le groupe par défaut reste
# « francophone » pour ne pas exiger de clé sur une moisson de base.
FRANCOPHONE_SOURCES = [
    "hal", "isidore", "openedition", "cairn", "persee", "theses_fr", "erudit",
]
INTERNATIONAL_SOURCES = ["crossref", "openalex", "semantic_scholar", "core"]

SOURCE_GROUPS: Dict[str, list[str]] = {
    "francophone": list(FRANCOPHONE_SOURCES),
    "international": list(INTERNATIONAL_SOURCES),
    "all": list(FRANCOPHONE_SOURCES) + list(INTERNATIONAL_SOURCES),
}


def resolve_sources(tokens: list[str]) -> list[str]:
    """
    Développe une liste de jetons ``--sources`` en noms de sources concrets.

    Un jeton peut être un nom de source enregistré (``hal``, ``openalex``…) ou
    un nom de groupe (``francophone``, ``international``, ``all``). Les doublons
    sont supprimés en préservant l'ordre d'apparition.

    Args:
        tokens: Liste de jetons (noms de sources et/ou de groupes).

    Returns:
        Liste ordonnée et dédupliquée de noms de sources enregistrées.

    Raises:
        ValueError: Si un jeton n'est ni une source enregistrée ni un groupe.
    """
    resolved: list[str] = []
    seen = set()

    def _add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            resolved.append(name)

    for token in tokens:
        key = token.lower().strip()
        if key in SOURCE_GROUPS:
            for src in SOURCE_GROUPS[key]:
                _add(src)
        elif key in CONNECTORS:
            _add(key)
        else:
            available = ", ".join(sorted(CONNECTORS.keys()))
            groups = ", ".join(sorted(SOURCE_GROUPS.keys()))
            raise ValueError(
                f"Source ou groupe inconnu : '{token}'. "
                f"Sources : {available}. Groupes : {groups}."
            )
    return resolved


def list_source_tokens() -> list[str]:
    """Noms de sources + noms de groupes acceptés par ``--sources`` (triés)."""
    return sorted(CONNECTORS.keys()) + sorted(SOURCE_GROUPS.keys())


def get_connector_class(name: str) -> Type[Connector]:
    """
    Récupère la classe Connector enregistrée pour un nom de source.

    Args:
        name: Nom court de la source (insensible à la casse)

    Returns:
        Classe Connector

    Raises:
        ValueError: Si le nom n'est pas enregistré
    """
    key = name.lower()
    if key not in CONNECTORS:
        available = ", ".join(sorted(CONNECTORS.keys()))
        raise ValueError(
            f"Source inconnue : '{name}'. Sources disponibles : {available}"
        )
    return CONNECTORS[key]


def list_sources() -> list[str]:
    """Renvoie la liste triée des noms de sources enregistrées."""
    return sorted(CONNECTORS.keys())


def is_proxy_source(name: str) -> bool:
    """Une source 'proxy' utilise Isidore en interne pour la recherche."""
    return name.lower() in GROUP_PROXY_SOURCES


def split_by_group(sources: list[str]) -> tuple[list[str], list[str]]:
    """
    Sépare une liste de sources en (natives, proxies) pour le parallélisme.

    Args:
        sources: Liste de noms de sources

    Returns:
        (group_native, group_proxy)
    """
    native = [s for s in sources if not is_proxy_source(s)]
    proxy = [s for s in sources if is_proxy_source(s)]
    return native, proxy


# === Métadonnées de présentation par source ===
# Utilisé pour l'aide CLI et le schéma MCP. Description courte par source.
SOURCE_DESCRIPTIONS: Dict[str, str] = {
    "hal": "HAL Archive ouverte — recherche Solr full-text native",
    "isidore": "Isidore Huma-Num — moteur SHS, indexe HAL/OpenEdition/Cairn/Erudit/etc.",
    "openedition": "OpenEdition (journals + books) — proxy Isidore + enrichissement OAI optionnel",
    "cairn": "Cairn.info — proxy Isidore + métadonnées OAI (pas de full-text)",
    "persee": "Persée — périodiques SHS patrimoniaux français, proxy Isidore + OAI optionnel",
    "theses_fr": "Theses.fr — base nationale des thèses françaises (Sudoc/ABES), proxy Isidore",
    "erudit": "Érudit — revues SHS francophones québécoises/canadiennes, proxy Isidore + OAI optionnel",
    "crossref": "CrossRef — métadonnées d'éditeurs (~160M), REST natif, sans clé (polite pool)",
    "openalex": "OpenAlex — graphe scientifique ouvert, concepts + citations, clé gratuite requise",
    "semantic_scholar": "Semantic Scholar — Academic Graph, citations influentes, clé optionnelle",
    "core": "CORE — agrégateur OA (~290M), texte intégral + PDF, clé Bearer requise",
}
