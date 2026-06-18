"""
Déduplication multi-stratégies pour la fusion de corpus multi-sources.

La déduplication procède par priorité décroissante de fiabilité
des identifiants : DOI (le plus fiable), HAL ID, URL canonique,
puis empreinte SHA-256 sur titre + premier auteur + année.

Lorsqu'un doublon est détecté, les champs sont fusionnés selon
la règle : "non vide remporte vide". La provenance multiple
est tracée dans le champ 'extra' au format clé:valeur Zotero.
"""

import hashlib
import logging
from typing import List, Dict, Optional
from collections import defaultdict

from utils.cleaner import normalize_for_fingerprint, extract_year

logger = logging.getLogger(__name__)


# === Priorité de source par champ (fusion cross-corpus FR/EN) ===
# Quand un même identifiant vient de plusieurs sources, on choisit la valeur
# la plus fiable par champ. Les noms sont normalisés (minuscules, espaces →
# underscore) : "Semantic Scholar" → "semantic_scholar", "CrossRef"/"Crossref"
# → "crossref", "CORE" → "core". Si aucune source listée ne fournit de valeur
# non vide pour un champ, on retombe sur la règle « premier non vide gagne ».
SOURCE_FIELD_PRIORITY = {
    "abstractNote":     ["crossref", "openalex", "semantic_scholar", "core",
                         "cairn", "openedition", "hal", "isidore"],
    "DOI":              ["crossref", "openalex", "semantic_scholar", "hal"],
    "publicationTitle": ["crossref", "openalex", "hal", "cairn"],
    "ISSN":             ["crossref", "openalex"],
    "pages":            ["crossref", "openalex"],
    # 'extra' n'est pas arbitré : on concatène les lignes de toutes les sources
    # (et on dédoublonne la ligne 'Sources:' en une seule ligne agrégée).
}


def _normalize_source(name: str) -> str:
    """Normalise un libellé de source pour la comparaison de priorité."""
    return (name or "").strip().lower().replace(" ", "_")


def item_sources(item: Dict) -> set:
    """
    Renvoie l'ensemble (normalisé) des sources d'un item, lues sur la ligne
    ``Sources: A, B`` du champ ``extra``.
    """
    extra = item.get("extra", "") or ""
    for line in extra.split("\n"):
        line = line.strip()
        if line.startswith("Sources:"):
            raw = line[len("Sources:"):]
            return {_normalize_source(s) for s in raw.split(",") if s.strip()}
    return set()


def _pick_by_source_priority(items: List[Dict], field: str,
                             priority: List[str]) -> Optional[str]:
    """
    Choisit la valeur de ``field`` issue de la source la plus prioritaire
    qui en fournit une non vide. Renvoie ``None`` si aucune source listée
    n'a de valeur (l'appelant retombe alors sur le comportement par défaut).
    """
    for src in priority:
        for item in items:
            if src in item_sources(item):
                value = item.get(field)
                if value:
                    return value
    return None


def get_doi(item: Dict) -> Optional[str]:
    """
    Extrait le DOI d'un item Zotero et le normalise.

    Args:
        item: Item Zotero

    Returns:
        DOI normalisé (sans préfixe doi:, en minuscules) ou None
    """
    doi = item.get("DOI", "").strip()
    if not doi:
        return None
    # Normalisation : retrait des préfixes habituels
    doi = doi.replace("https://doi.org/", "")
    doi = doi.replace("http://doi.org/", "")
    doi = doi.replace("doi:", "")
    doi = doi.lower().strip()
    return doi if doi else None


def get_source_uri(item: Dict) -> Optional[str]:
    """
    Extrait un URI canonique source du champ 'extra'.

    Les HAL ID et identifiants OAI sont stockés dans 'extra'
    sous la forme 'HAL ID: hal-04123456' ou
    'OAI Identifier: oai:revues.org:...'.

    Args:
        item: Item Zotero

    Returns:
        URI canonique normalisé ou None
    """
    extra = item.get("extra", "")
    if not extra:
        return None
    lines = [l.strip() for l in extra.split("\n")]
    # Recherche d'identifiants canoniques connus, par ordre de fiabilité
    # DÉCROISSANTE : on boucle d'abord sur les préfixes (priorité), puis sur les
    # lignes. Ainsi un item portant plusieurs identifiants natifs (ex. déjà
    # fusionné HAL+OpenAlex) renvoie l'URI du type le plus fiable (HAL ID) quel
    # que soit l'ordre des lignes — round-trip déterministe, p. ex. lors d'un
    # corpus_merge suivi d'une re-déduplication. Les sources internationales
    # tracent leur identifiant natif (OpenAlex Work ID, S2 CorpusId, CORE ID).
    for prefix in (
        "HAL ID:", "OAI Identifier:", "Isidore URI:",
        "OpenAlex ID:", "S2 corpus id:", "CORE ID:",
    ):
        for line in lines:
            if line.startswith(prefix):
                value = line[len(prefix):].strip()
                if value:
                    return f"{prefix} {value}"
    return None


def get_url_normalized(item: Dict) -> Optional[str]:
    """
    Renvoie l'URL normalisée d'un item.

    Args:
        item: Item Zotero

    Returns:
        URL en minuscules sans slash final ou None
    """
    url = item.get("url", "").strip()
    if not url:
        return None
    return url.lower().rstrip("/")


def compute_fingerprint(item: Dict) -> str:
    """
    Calcule une empreinte de déduplication basée sur titre,
    premier auteur et année.

    Cette empreinte sert de filet de sécurité quand aucun
    identifiant canonique n'est disponible. Elle ne capture
    pas tous les cas (variations de titre, prénoms abrégés),
    mais résout l'essentiel des doublons exacts.

    Args:
        item: Item Zotero

    Returns:
        Empreinte hexadécimale SHA-256 tronquée
    """
    # Titre normalisé (sans accents, sans ponctuation, minuscules)
    title_norm = normalize_for_fingerprint(item.get("title", ""))

    # Nom du premier auteur
    creators = item.get("creators", [])
    first_author = ""
    if creators:
        first = creators[0]
        last_name = first.get("lastName") or first.get("name", "")
        first_author = normalize_for_fingerprint(last_name)

    # Année
    year = extract_year(item.get("date", "")) or ""

    # Concaténation et hachage
    composite = f"{title_norm}|{first_author}|{year}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()[:16]


def merge_items(items: List[Dict]) -> Dict:
    """
    Fusionne plusieurs items représentant la même ressource.

    Règles :
    - Pour chaque champ, conserve la première valeur non vide rencontrée
    - Les listes (creators, tags) sont conservées telles quelles du
      premier item, car la fusion d'auteurs entre sources est risquée
    - Le champ 'extra' agrège les sources d'origine

    Args:
        items: Liste d'items considérés comme doublons

    Returns:
        Item Zotero fusionné
    """
    if not items:
        return {}
    if len(items) == 1:
        return items[0]

    # Base : le premier item (souvent le plus riche, HAL ou Isidore)
    merged = dict(items[0])

    # Collecte des sources d'origine, dédupliquées SANS tenir compte de la
    # casse : 'CrossRef' (mapper) et 'Crossref' (lookups) désignent la même
    # source. On conserve le premier libellé d'affichage rencontré par clé
    # normalisée pour éviter une ligne 'Sources: CrossRef, Crossref'.
    source_labels: Dict[str, str] = {}
    extras = []
    for item in items:
        extra = item.get("extra", "")
        for line in extra.split("\n"):
            if line.startswith("Sources:"):
                sources_str = line[len("Sources:"):].strip()
                for s in sources_str.split(","):
                    s = s.strip()
                    if s:
                        source_labels.setdefault(_normalize_source(s), s)
            else:
                if line.strip() and line not in extras:
                    extras.append(line)

    # Complétion des champs vides à partir des autres items
    # (règle de base : premier non vide gagne)
    for item in items[1:]:
        for key, value in item.items():
            # Ne pas écraser les listes ni 'extra' (traité séparément)
            if key in ("creators", "tags", "extra", "collections", "relations"):
                continue
            if not merged.get(key) and value:
                merged[key] = value

    # Arbitrage par priorité de source pour les champs sensibles (FR/EN) :
    # la valeur de la source la plus fiable l'emporte si elle existe ; sinon
    # on conserve la valeur déjà fusionnée (premier non vide gagne).
    for field, priority in SOURCE_FIELD_PRIORITY.items():
        chosen = _pick_by_source_priority(items, field, priority)
        if chosen:
            merged[field] = chosen

    # Reconstruction de 'extra' avec sources fusionnées
    final_extras = []
    if source_labels:
        final_extras.append(
            f"Sources: {', '.join(sorted(source_labels.values()))}"
        )
    final_extras.extend(extras)
    merged["extra"] = "\n".join(final_extras)

    return merged


def source_overlap_matrix(items: List[Dict]) -> Dict[str, int]:
    """
    Compte, sur un corpus dédupliqué, les recouvrements par paire de sources.

    Un item fusionné dont la ligne ``Sources:`` liste plusieurs sources atteste
    qu'au moins deux sources couvraient la même ressource. On compte chaque
    paire (non ordonnée) une fois par item partagé. Utile pour mesurer la
    valeur ajoutée réelle d'une source internationale par rapport aux sources
    francophones.

    Returns:
        Dict ``"sourceA + sourceB" -> nombre d'items partagés`` (clés triées).
    """
    from collections import Counter
    from itertools import combinations

    pairs: Counter = Counter()
    for item in items:
        srcs = sorted(item_sources(item))
        for a, b in combinations(srcs, 2):
            pairs[f"{a} + {b}"] += 1
    return dict(sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0])))


def deduplicate(items: List[Dict]) -> List[Dict]:
    """
    Déduplique une liste d'items Zotero issue de sources multiples.

    Procède par groupement successif sur trois clés :
    DOI, URI canonique, empreinte. Chaque groupe est fusionné.

    Args:
        items: Liste d'items à dédupliquer

    Returns:
        Liste dédupliquée et fusionnée
    """
    if not items:
        return []

    # Étape 1 : groupement par DOI
    by_doi = defaultdict(list)
    no_doi = []
    for item in items:
        doi = get_doi(item)
        if doi:
            by_doi[doi].append(item)
        else:
            no_doi.append(item)

    # Fusion des groupes DOI
    deduplicated = [merge_items(group) for group in by_doi.values()]

    # Étape 2 : groupement des items sans DOI par URI canonique
    by_uri = defaultdict(list)
    no_uri = []
    for item in no_doi:
        uri = get_source_uri(item)
        if uri:
            by_uri[uri].append(item)
        else:
            no_uri.append(item)

    deduplicated.extend(merge_items(group) for group in by_uri.values())

    # Étape 3 : groupement des items restants par URL puis par empreinte
    by_url = defaultdict(list)
    no_url = []
    for item in no_uri:
        url = get_url_normalized(item)
        if url:
            by_url[url].append(item)
        else:
            no_url.append(item)

    deduplicated.extend(merge_items(group) for group in by_url.values())

    # Étape 4 : derniers items sans aucun identifiant → empreinte
    by_fp = defaultdict(list)
    for item in no_url:
        fp = compute_fingerprint(item)
        by_fp[fp].append(item)

    deduplicated.extend(merge_items(group) for group in by_fp.values())

    initial = len(items)
    final = len(deduplicated)
    logger.info(
        f"Déduplication : {initial} items → {final} items uniques "
        f"({initial - final} doublons fusionnés)"
    )

    return deduplicated
