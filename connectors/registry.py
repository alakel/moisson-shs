"""
Registre central des connecteurs de sources bibliographiques.

Ce module centralise la déclaration des sources moissonnables.
Tout code qui itère sur les sources (main.py, mcp_server.py, tests)
consomme `CONNECTORS` plutôt que de hardcoder des `if/elif`.

Pour ajouter une nouvelle source :
1. Créer `connectors/<nom>.py` héritant de `base.Connector`
2. Créer `mappers/<nom>_mapper.py` si nécessaire
3. Importer la classe ici et l'ajouter dans `CONNECTORS`
4. (Optionnel) Ajouter une entrée `GROUP_PROXY_SOURCES` et
   `SHARED_FLOW_SOURCES` si la source utilise Isidore comme proxy (elle lit
   alors le flux Isidore partagé de la moisson), et une entrée
   `START_PRIORITY` si elle est lente (politesse espacée, nombreuses pages)
5. (Optionnel) Ajouter une entrée `METERED_SOURCES` si chaque requête est
   facturée : la source reste alors hors des groupes `international` et `all`

Aucune autre modification du code n'est requise.
"""

from typing import Dict, Optional, Type

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
# Source facturée à la requête (passerelle SerpAPI vers Google Scholar)
from connectors.google_scholar import GoogleScholarConnector


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
    # --- Source facturée à la requête (hors groupes, cf. METERED_SOURCES) ---
    "google_scholar": GoogleScholarConnector,
}


# === Sources facturées ===
# Toutes les sources ci-dessus sont gratuites sauf celles listées ici, dont
# chaque page de résultats consomme un crédit du compte de l'utilisateur.
# Conséquence de conception : elles sont **exclues des groupes** `international`
# et `all`, pour qu'aucune moisson de routine ne consomme un quota mensuel à
# l'insu de l'appelant. Elles ne s'activent que nommées explicitement
# (`--sources google_scholar`) ou par le groupe dédié `scholar`.
METERED_SOURCES = {"google_scholar"}


# === Sous-sources d'Isidore ===
# Les sources "proxy" passent par Isidore : elles lisent le flux de pages
# Isidore partagé de la moisson (connectors/isidore_scan.py) et n'en gardent
# que leurs notices. Jusqu'à OPT-09, elles étaient exécutées après toutes les
# sources natives (barrière « natives puis proxies ») ; l'ordonnancement suit
# désormais la topologie plate ci-dessous (plan_order). `split_by_group` reste
# exporté pour compatibilité.
GROUP_PROXY_SOURCES = {"openedition", "cairn", "persee", "theses_fr", "erudit"}


# === Topologie d'exécution (OPT-09) ===
# Un seul pool de workers sur toutes les sources, sans barrière. Ce qui
# gouverne l'exécution :
# - la politesse par hôte et par compte de utils/http.py (intervalle entre
#   deux départs, requêtes en vol), qui vaut quel que soit le parallélisme :
#   deux sources sur un même hôte ne dépassent jamais son débit ;
# - la seule dépendance réelle : les consommateurs d'un flux partagé (source
#   `isidore` et ses sous-sources) lisent les MÊMES pages, téléchargées une
#   fois par le premier qui les demande. Le premier consommateur ouvre le flux
#   dès le départ ; les suivants sont placés en fin de file, où ils ne
#   bloquent aucun worker dont une source indépendante aurait besoin (ils se
#   contentent de relire le flux, le plus souvent déjà téléchargé) ;
# - l'ordre de démarrage : les sources les plus longues d'abord (politesse
#   espacée ou pages nombreuses), pour que la durée totale soit celle de la
#   plus longue et non la somme des dernières.

# Flux partagé lu par chaque source (clé : source ; valeur : nom du flux).
SHARED_FLOW_SOURCES: Dict[str, str] = {
    "isidore": "isidore",
    "openedition": "isidore",
    "cairn": "isidore",
    "persee": "isidore",
    "theses_fr": "isidore",
    "erudit": "isidore",
}

# Priorité de démarrage (plus petit = plus tôt ; ex æquo : ordre demandé).
# CORE espace ses pages de 6 s (REQUEST_DELAY_CORE) ; Semantic Scholar
# (1,05 s) et SerpAPI (1 s, pages de 20) sont lents aussi ; HAL (une page
# lourde) et l'ouvreur du flux Isidore (jusqu'à deux pages de 4,6 Mo, 0,3 s
# entre elles) ensuite ; CrossRef et OpenAlex servent 1000 et 200 notices par
# requête depuis OPT-09. Défaut : PRIORITY_DEFAULT.
PRIORITY_DEFAULT = 3
PRIORITY_FLOW_OPENER = 2
START_PRIORITY: Dict[str, int] = {
    "core": 0,
    "semantic_scholar": 1,
    "google_scholar": 1,
    "hal": 2,
    "crossref": 3,
    "openalex": 3,
}


# === Préréglages de groupes pour --sources / MCP ===
# Les sources internationales sont des API REST natives (pas de proxy Isidore).
# OpenAlex et CORE exigent une clé API, Semantic Scholar la rend facultative
# (pool partagé plus lent sans clé), CrossRef n'en a pas. Le groupe par défaut
# reste « francophone » pour ne pas exiger de clé sur une moisson de base.
FRANCOPHONE_SOURCES = [
    "hal", "isidore", "openedition", "cairn", "persee", "theses_fr", "erudit",
]
INTERNATIONAL_SOURCES = ["crossref", "openalex", "semantic_scholar", "core"]

# Google Scholar est absent de `international` et de `all` : voir
# METERED_SOURCES. Le combiner explicitement quand on le veut, par exemple
# `--sources international google_scholar`.
METERED_GROUP_SOURCES = ["google_scholar"]

SOURCE_GROUPS: Dict[str, list[str]] = {
    "francophone": list(FRANCOPHONE_SOURCES),
    "international": list(INTERNATIONAL_SOURCES),
    "all": list(FRANCOPHONE_SOURCES) + list(INTERNATIONAL_SOURCES),
    "scholar": list(METERED_GROUP_SOURCES),
}


def is_metered_source(name: str) -> bool:
    """Une source 'facturée' consomme un crédit par page de résultats."""
    return name.lower() in METERED_SOURCES


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


def shared_flow_of(name: str) -> Optional[str]:
    """Nom du flux partagé que lit la source (``"isidore"``), ou None."""
    return SHARED_FLOW_SOURCES.get(name.lower())


def plan_order(sources: list[str]) -> list[str]:
    """
    Ordre de démarrage des sources dans le pool unique (topologie plate,
    OPT-09) : sources indépendantes et premier consommateur de chaque flux
    partagé, par priorité de démarrage (``START_PRIORITY``, ordre demandé en
    cas d'égalité), puis les autres consommateurs des flux partagés, dans
    l'ordre demandé. Aucune source n'attend la fin d'une autre : c'est
    seulement l'ordre dans lequel les workers libres les prennent.

    Args:
        sources: noms de sources (groupes déjà résolus), sans doublon.

    Returns:
        Les mêmes noms, réordonnés.
    """
    leaders: list[tuple[int, int, str]] = []
    followers: list[str] = []
    opened = set()
    for index, name in enumerate(sources):
        flow = shared_flow_of(name)
        if flow is not None:
            if flow in opened:
                followers.append(name)
                continue
            opened.add(flow)
            priority = min(START_PRIORITY.get(name.lower(), PRIORITY_FLOW_OPENER),
                           PRIORITY_FLOW_OPENER)
        else:
            priority = START_PRIORITY.get(name.lower(), PRIORITY_DEFAULT)
        leaders.append((priority, index, name))
    leaders.sort(key=lambda t: (t[0], t[1]))
    return [name for _, _, name in leaders] + followers


def split_by_group(sources: list[str]) -> tuple[list[str], list[str]]:
    """
    Sépare une liste de sources en (natives, proxies).

    Topologie historique (barrière « natives puis proxies ») : n'est plus
    utilisée par l'orchestrateur depuis OPT-09 (voir ``plan_order``) ;
    conservée pour compatibilité.

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
    "google_scholar": (
        "Google Scholar via SerpAPI — couverture maximale (actes, chapitres, "
        "littérature grise) et compteur de citations, mais sans DOI ni résumé ; "
        "clé requise et FACTURÉE À LA REQUÊTE, d'où son exclusion des groupes"
    ),
}
