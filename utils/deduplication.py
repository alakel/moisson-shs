"""
Déduplication et fusion des notices d'un corpus multi-sources.

Principe
--------
L'identité d'une notice est lue par l'extracteur commun
(``utils.identity.strong_keys``) : DOI (déclaré, ou lisible sans ambiguïté
dans l'URL), identifiants natifs (HAL, OAI, Isidore, OpenAlex, Semantic
Scholar, CORE, Google Scholar, PMID, arXiv), URL canonique non générique,
ISBN d'un livre. La déduplication procède en deux étapes.

1. **Clés fortes, union-find.** Deux notices qui partagent une clé forte
   sont reliées, quel que soit le type de la clé : une notice sans DOI mais
   porteuse du même identifiant HAL ou de la même URL canonique qu'une notice
   avec DOI la rejoint (fin de la partition stricte DOI / URI / URL /
   empreinte). Deux clés demandent une concordance supplémentaire :

   - une clé ``url:`` partagée par plus de ``URL_KEY_MAX_SHARE`` titres
     distincts est tenue pour générique et ignorée ; sinon elle ne relie que
     des notices aux titres concordants (égaux, ou similaires au sens de la
     passe floue) : une page de revue non reconnue comme générique ne réunit
     pas deux articles, et la décision ne dépend pas du nombre de copies
     (re-déduplication idempotente) ;
   - une clé ``hal:`` (identifiant sans suffixe de version) ne relie pas deux
     notices dont les années diffèrent de plus de ``HAL_KEY_MAX_YEAR_GAP`` :
     prépublication v1 puis chapitre v2, republication rattachée au même
     dépôt par OpenAlex ; les notices tenues à part portent la mention
     ``version distincte`` dans ``Dedup evidence``.

   **Garde-fou DOI.** Deux DOI différents ne sont jamais fusionnés, même par
   transitivité. Règle de scission d'un composant qui porte plusieurs DOI :

   - les notices de même DOI forment une classe ; deux classes ne fusionnent
     jamais ;
   - les notices sans DOI du composant sont regroupées entre elles par leurs
     clés partagées (sous-composants sans DOI) ;
   - un sous-composant sans DOI relié par ses clés à **une seule** classe DOI
     la rejoint ; relié à **plusieurs**, il reste à part (statut *ambigu*,
     tracé dans ``Dedup evidence``) : rien ne dit à quelle version il
     appartient.

2. **Appariement sans clé commune**, sur les notices fusionnées des groupes
   de l'étape 1 ; paires candidates : une notice et celles qui comptent son
   premier auteur parmi leurs auteurs (même nom normalisé, ou profil de nom
   équivalent), à un an d'écart au plus (exactement les paires que la
   corroboration peut accepter). Deux régimes :

   - *égalité exacte* du titre normalisé (héritière de l'ancienne empreinte
     exacte titre + auteur + année) : seul régime des titres courts (moins de
     ``FUZZY_MIN_SIGNIFICANT_WORDS`` mots significatifs), qui exigent alors la
     même année et le même premier auteur ; seul régime actif quand la passe
     floue est désactivée ;
   - *passe floue conservatrice*, désactivable (paramètre ``fuzzy``, variable
     ``MOISSON_DEDUP_FUZZY=0``) : similarité de titre de
     ``utils.doi_resolver.best_title_ratio`` ≥ ``FUZZY_TITLE_THRESHOLD``
     (0,93) ; un appariement par préfixe (sous-titre absent d'un côté) exige
     en plus la même année, il n'est jamais accepté seul ; deux titres qui ne
     diffèrent que par des mots de paratexte (« X » et « X : introduction »,
     « Erratum : X », « Reply to X »), par des numéros incompatibles
     (« tome 1 » et « tome 2 ») ou par un marqueur de tome ou de partie
     (« X » et « X. Tome 2 », « X : deuxième partie ») ne sont pas appariés.

   Dans les deux régimes :

   - corroboration par l'année (± 1) **et** par le premier auteur, toutes deux
     obligatoires : le premier auteur de l'une figure parmi les auteurs de
     l'autre (règle du résolveur de DOI, qui tolère les listes tronquées ou
     ordonnées différemment), sous le même nom normalisé ou sous un nom
     saisi autrement (:func:`names_equivalent` : ordre nom / prénom inversé,
     nom composé partagé autrement entre les deux champs ou abrégé, avec
     prénom corroboré et sans composé à trait d'union contradictoire ;
     motif ``clé~clé (nom complet)``, régime des titres longs seulement) ;
     une notice sans année ou sans auteur n'est jamais
     appariée par cette étape ;
   - titre non générique (« Introduction », « Éditorial », « Compte rendu »,
     « Avant-propos », « Conclusion », « Préface »…, ou aucun mot
     significatif) ;
   - contraintes dures (:func:`hard_compatible`) : types Zotero compatibles
     (identiques, ou l'un générique : ``document``), de sorte que compte rendu
     et article, prépublication et article, thèse et livre, chapitre et livre
     ne sont jamais appariés d'office ; aucune relation partie/tout (le
     contenant de l'une porte le titre de l'autre, notice d'ensemble sans
     plage de pages) ; même statut de compte rendu (titre de
     recension) ; aucun identifiant contradictoire (DOI, ISBN de livre, arXiv,
     PMID ; deux identifiants HAL distincts seulement pour un titre identique
     d'au moins quatre mots significatifs et la même année, cas des dépôts
     HAL en double).

   Les contraintes dures sont vérifiées pour **chaque paire** d'un
   regroupement : une notice de type générique ne peut pas relier un livre et
   son chapitre par transitivité (agglomération par liens décroissants, dont
   l'ordre ne dépend que du contenu). Les appariements obéissent aussi à la
   règle de scission DOI de l'étape 1. L'étape est itérée jusqu'à stabilité,
   ce qui rend une re-déduplication idempotente.

Fusion indépendante de l'ordre
------------------------------
Les membres d'un groupe sont triés par une clé stable (rang général de la
source, identifiants, titre, contenu) ; chaque champ est choisi par un ordre
total : priorité de source du champ (``SOURCE_FIELD_PRIORITY``), puis rang
général de la source (``GENERAL_SOURCE_ORDER``), puis valeur la plus longue,
puis ordre lexicographique ; pour un contenant (revue, ouvrage), un nom de
dépôt ou de plateforme (« HAL (…) », « Cairn.info », « X eBooks ») passe
après toute vraie valeur. Une valeur est attribuée à la source de la notice
qui la porte : l'origine de **chaque** champ retenu (type Zotero compris) est
tracée dans la ligne ``Dedup evidence`` (``origines``) et relue pour une
notice déjà fusionnée, jamais déduite de la ligne ``Sources`` agrégée ; une
fusion en deux temps (corpus déjà dédupliqués refusionnés) choisit donc
comme une fusion en une fois. Une notice fusionnée sans origine connue n'est
créditée à aucune source prioritaire. Les champs que le type retenu ne
prévoit pas (``publicationTitle`` d'un article réuni à un chapitre) sont
reportés dans ``extra`` (``champ: valeur``, comme ``validate_and_clean``)
et redeviennent candidats si une refusion change le type. Auteurs : liste la
plus complète (plus grand nombre d'auteurs nommés), en écartant les listes
marquées ``Scholar truncation`` quand une autre liste existe. Tags,
collections, ISBN et URL agrégés et dédoublonnés (les URL des membres non
retenues vont dans ``Alternative URLs``). Les avertissements Scholar
(``Scholar snippet``, ``Scholar truncation``) ne sont conservés que si le
champ qu'ils qualifient vient effectivement de Google Scholar.

Ordre de sortie
---------------
Indépendant de l'ordre d'entrée : ordre bibliographique auteur-date (nom
normalisé du premier auteur, notices sans auteur en fin, puis année, titre
normalisé, clés fortes, contenu). Une permutation des entrées produit une
sortie strictement identique.

Ligne ``Dedup evidence``
------------------------
Chaque notice fusionnée porte dans ``extra`` une ligne unique, par exemple ::

    Dedup evidence: 3 notices ; clés doi:10.4000/x, hal:halshs-00367519 ;
    floue titre 0.97 année exacte auteur rabatel ; origines
    title+creators+date+itemType+volume=HAL, abstractNote+DOI=CrossRef

Les champs sont regroupés par origine (forme historique « title=HAL,
creators=HAL » toujours lue).

Aucun champ Zotero arbitraire n'est ajouté.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from unidecode import unidecode

from utils.cleaner import extract_year, normalize_for_fingerprint
from utils.identity import (
    extra_lines, keys_of_kind, normalize_doi, normalize_isbns,
    strong_key_sources,
)
from utils.source_labels import (
    SOURCE_DISPLAY_NAMES, canonical_source_key, display_name,
)

logger = logging.getLogger(__name__)


# === Paramètres ===

# Seuil de similarité de titre de la passe floue (décision §10 du plan :
# 0,93 + année + auteur). Ne jamais l'abaisser (invariant 5).
FUZZY_TITLE_THRESHOLD = 0.93
# Écart d'année toléré par la passe floue (publication en ligne / en numéro).
FUZZY_YEAR_TOLERANCE = 1
# Un titre comptant moins de mots significatifs est exclu de la passe floue.
FUZZY_MIN_SIGNIFICANT_WORDS = 4
# Nombre maximal d'itérations de la passe floue (stabilité).
FUZZY_MAX_ROUNDS = 5
# Au-delà de ce nombre de titres distincts parmi les notices qui partagent une
# même URL canonique, l'URL est tenue pour générique (page de collection, de
# plateforme). On compte les titres et non les notices : plusieurs copies
# d'une même œuvre ne rendent pas une URL générique, et la décision ne change
# pas quand une re-déduplication a déjà réuni ces copies (idempotence).
URL_KEY_MAX_SHARE = 8
# Deux notices qui partagent un identifiant HAL mais dont les années
# diffèrent de plus de cet écart sont deux versions distinctes (prépublication
# puis chapitre, republication) : le lien n'est pas fait.
HAL_KEY_MAX_YEAR_GAP = 1
# Variable d'environnement de désactivation de la passe floue.
FUZZY_ENV = "MOISSON_DEDUP_FUZZY"

# Nombre maximal de clés citées dans la ligne Dedup evidence.
_EVIDENCE_MAX_KEYS = 10
_EVIDENCE_MAX_FUZZY = 5

EVIDENCE_PREFIX = "Dedup evidence:"


# === Priorité de source par champ (fusion cross-corpus FR/EN) ===
# Quand un même travail vient de plusieurs sources, on choisit la valeur la
# plus fiable par champ. Les libellés sont ramenés à la clé de registre
# (``utils.source_labels``). Si aucune source listée ne fournit de valeur non
# vide pour un champ, l'ordre général ci-dessous départage.
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

# Ordre général des sources pour les champs sans priorité dédiée et pour
# départager : métadonnées d'éditeur, puis archives et plateformes
# francophones, puis agrégateurs, Google Scholar en dernier (métadonnées
# reconstruites depuis une ligne de notice, tronquées par nature).
GENERAL_SOURCE_ORDER = [
    "crossref", "hal", "openedition", "cairn", "persee", "erudit",
    "theses_fr", "isidore", "openalex", "semantic_scholar", "core",
    "google_scholar",
]
_GENERAL_RANK = {k: i for i, k in enumerate(GENERAL_SOURCE_ORDER)}
_UNKNOWN_RANK = len(GENERAL_SOURCE_ORDER)

# Champs cités en tête du segment ``origines`` de la ligne Dedup evidence.
# L'origine de **tout** champ arbitré est tracée (type Zotero compris) : sans
# elle, une notice déjà fusionnée ne dirait plus d'où vient sa valeur, et une
# re-fusion (corpus_merge, fetch_by_doi avec add_to) dépendrait de
# l'historique des fusions. Les autres champs suivent, ``itemType`` d'abord,
# puis par ordre alphabétique.
ORIGIN_FIELDS = ("title", "creators", "date", "abstractNote", "DOI",
                 "publicationTitle", "ISSN", "pages", "url")

# Champs de contenant (revue, ouvrage, actes…) : une valeur de dépôt n'y est
# pas une revue (voir :func:`is_repository_venue`), et un contenant égal au
# titre d'une autre notice signale une relation partie/tout.
VENUE_FIELDS = ("publicationTitle", "bookTitle", "proceedingsTitle",
                "encyclopediaTitle", "dictionaryTitle", "websiteTitle",
                "blogTitle", "forumTitle", "programTitle")

# Champs toujours conservés, quel que soit le type retenu.
_ALWAYS_KEPT_FIELDS = {"itemType", "creators", "tags", "collections",
                       "relations", "extra"}

# Champs à traitement dédié lors de la fusion.
_SPECIAL_FIELDS = {"creators", "tags", "extra", "collections", "relations",
                   "itemType", "ISBN"}

# Lignes d'extra reconstruites par la fusion.
_REBUILT_LINES = ("Sources", "Dedup evidence", "Scholar snippet",
                  "Scholar truncation", "Alternative URLs")

_GENERIC_ITEM_TYPES = {"", "document"}


def _general_rank(source_key: str) -> int:
    return _GENERAL_RANK.get(source_key, _UNKNOWN_RANK)


# === Contenants de substitution (dépôts, plateformes) ===

# OpenAlex donne pour « revue » d'un dépôt sans DOI le nom du dépôt ou de la
# plateforme qui l'héberge (« HAL (Le Centre pour la Communication
# Scientifique Directe) », « Dialnet (Universidad de la Rioja) »,
# « Cairn.info », « Peter Lang eBooks »…). Relevé sur les corpus réels de
# output/ (2026-09-28). Ce n'est pas une revue : une vraie valeur, de quelque
# source qu'elle vienne, lui est préférée à la fusion (la valeur n'est jamais
# effacée quand elle est la seule).
_REPOSITORY_VENUE_EXACT = {
    "hal", "cairn", "cairn.info", "cairn info", "openedition",
    "openedition (openedition)", "arxiv", "arxiv.org", "zenodo",
    "semantic scholar", "google scholar", "researchgate", "academia.edu",
    "persee", "erudit", "isidore", "core", "theses.fr",
}
_REPOSITORY_VENUE_PREFIXES = (
    "hal (", "hal-shs", "hal shs", "hal science", "tel (", "dumas (",
    "dialnet", "doaj", "zenodo (", "philpapers", "arxiv (", "oskar-bordeaux",
    "oskar bordeaux", "europe pmc", "pubmed central", "citeseer", "figshare",
    "repec", "research papers in economics", "la referencia", "osti oai",
    "rucforsk", "caltechauthors", "hildok", "aaltodoc", "research explorer (",
    "research portal (", "infoscience", "archive ouverte", "osf preprints",
    "portuguese national funding agency", "researchgate",
)
_REPOSITORY_VENUE_SUBSTRINGS = ("repositor", "open archive", "(rcaap")


def is_repository_venue(value: Any) -> bool:
    """
    Vrai si un contenant (``publicationTitle``, ``bookTitle``…) est le nom
    d'un dépôt ou d'une plateforme et non celui d'une revue ou d'un ouvrage
    (valeur posée par OpenAlex pour les dépôts HAL sans DOI, collections
    « X eBooks » des éditeurs).
    """
    if not isinstance(value, str):
        return False
    v = unidecode(value).strip().lower()
    if not v:
        return False
    if v in _REPOSITORY_VENUE_EXACT:
        return True
    if v.startswith(_REPOSITORY_VENUE_PREFIXES):
        return True
    if any(s in v for s in _REPOSITORY_VENUE_SUBSTRINGS):
        return True
    return v.endswith(" ebooks")


def _allowed_fields(item_type: str) -> Set[str]:
    """Champs Zotero admis pour ``item_type`` (schéma chargé par le loader,
    sinon table embarquée), comme ``validate_and_clean``."""
    try:
        from mappers.zotero_schema_loader import get_allowed_fields
        return set(get_allowed_fields(item_type or "document")) \
            | _ALWAYS_KEPT_FIELDS
    except Exception:  # pragma: no cover - repli de validate_and_clean
        from mappers.zotero_schema import COMMON_FIELDS, ZOTERO_ITEM_FIELDS
        allowed = ZOTERO_ITEM_FIELDS.get(item_type,
                                         ZOTERO_ITEM_FIELDS["document"])
        return set(allowed) | set(COMMON_FIELDS) | _ALWAYS_KEPT_FIELDS


# === Sources ===

def _normalize_source(name: str) -> str:
    """Clé de registre d'un libellé de source (voir ``utils.source_labels``)."""
    return canonical_source_key(name)


def _source_labels(item: Dict) -> List[str]:
    """Libellés bruts de la ligne ``Sources:`` d'un item, dans l'ordre."""
    for key, value in extra_lines(item):
        if key == "Sources":
            return [s.strip() for s in value.split(",") if s.strip()]
    return []


def item_sources(item: Dict) -> set:
    """
    Ensemble des clés de source d'un item (``"hal"``, ``"crossref"``,
    ``"semantic_scholar"``, ``"google_scholar"``…), lues sur la ligne
    ``Sources: A, B`` du champ ``extra`` et ramenées au registre.
    """
    return {_normalize_source(s) for s in _source_labels(item) if s}


def _pick_by_source_priority(items: List[Dict], field: str,
                             priority: List[str]) -> Optional[str]:
    """
    Valeur de ``field`` issue de la source la plus prioritaire qui en fournit
    une non vide, l'origine étant celle de la valeur (voir :func:`merge_items`)
    et non la ligne ``Sources`` agrégée. ``None`` si aucune source listée n'a
    de valeur. Conservée pour compatibilité ; la fusion utilise
    :func:`_choose_value`.
    """
    infos = [_MemberInfo(it) for it in items]
    best = None
    for info in infos:
        value = info.item.get(field)
        if not _non_empty(value):
            continue
        origin = info.origin(field)
        if len(origin) != 1:
            continue
        src = next(iter(origin))
        if src not in priority:
            continue
        rank = (priority.index(src), _value_tiebreak(value))
        if best is None or rank < best[0]:
            best = (rank, value)
    return best[1] if best else None


# === Accès compatibles (API historique) ===

def get_doi(item: Dict) -> Optional[str]:
    """
    DOI déclaré d'un item, normalisé (champ ``DOI`` ou ligne ``DOI:`` de
    ``extra``). Délègue à ``utils.identity.extract_doi``.
    """
    from utils.identity import extract_doi
    return extract_doi(item)


def get_source_uri(item: Dict) -> Optional[str]:
    """
    URI canonique source lu dans ``extra`` (API historique).

    Les identifiants natifs sont stockés dans ``extra`` sous la forme
    ``HAL ID: hal-04123456``, ``OAI Identifier: oai:revues.org:...``, etc. On
    boucle d'abord sur les préfixes (ordre de fiabilité décroissante), puis
    sur les lignes : un item portant plusieurs identifiants natifs renvoie
    celui du type le plus fiable quel que soit l'ordre des lignes.
    """
    extra = item.get("extra", "")
    if not extra:
        return None
    lines = [l.strip() for l in extra.split("\n")]
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
    """URL en minuscules sans slash final (API historique ; voir
    ``utils.identity.canonical_url`` pour la forme canonique)."""
    url = item.get("url", "").strip()
    if not url:
        return None
    return url.lower().rstrip("/")


def compute_fingerprint(item: Dict) -> str:
    """
    Empreinte exacte titre + premier auteur + année (SHA-256 tronqué).

    N'intervient plus dans la déduplication (remplacée par la passe floue) ;
    conservée comme identifiant synthétique stable d'une notice sans clé
    forte (rapport de fusion de corpus).
    """
    title_norm = normalize_for_fingerprint(item.get("title", ""))
    creators = item.get("creators", [])
    first_author = ""
    if creators:
        first = creators[0]
        last_name = first.get("lastName") or first.get("name", "")
        first_author = normalize_for_fingerprint(last_name)
    year = extract_year(item.get("date", "")) or ""
    composite = f"{title_norm}|{first_author}|{year}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()[:16]


# === Ligne Dedup evidence ===

def parse_evidence(item: Dict) -> Optional[Dict[str, Any]]:
    """
    Lit la ligne ``Dedup evidence`` d'un item.

    Returns:
        ``{"count": int, "keys": [..], "fuzzy": [..], "origins": {champ:
        [clés de source]}, "ambiguous": str}`` ou ``None`` sans ligne.
    """
    raw = None
    for key, value in extra_lines(item):
        if key == "Dedup evidence":
            raw = value
            break
    if raw is None:
        return None
    ev: Dict[str, Any] = {"count": 1, "keys": [], "fuzzy": [], "origins": {},
                          "ambiguous": ""}
    for seg in (s.strip() for s in raw.split(" ; ")):
        if not seg:
            continue
        m = re.match(r"^(\d+) notices?$", seg)
        if m:
            ev["count"] = int(m.group(1))
        elif seg.startswith("clés "):
            ev["keys"] = [k.strip() for k in seg[5:].split(", ") if k.strip()]
        elif seg.startswith("floue "):
            ev["fuzzy"] = [f.strip() for f in seg[6:].split(" + ") if f.strip()]
        elif seg.startswith("origines "):
            # « title+creators=HAL, abstractNote=CrossRef » ; forme historique
            # « title=HAL, creators=HAL » acceptée.
            for part in seg[9:].split(", "):
                if "=" in part:
                    fields, labels = part.split("=", 1)
                    keys = [canonical_source_key(l) for l in labels.split("|")
                            if l.strip()]
                    for field in fields.split("+"):
                        if field.strip():
                            ev["origins"][field.strip()] = list(keys)
        elif seg.startswith("ambigu "):
            ev["ambiguous"] = seg[7:]
    return ev


def format_evidence(ev: Dict[str, Any]) -> str:
    """Compose la ligne ``Dedup evidence`` (inverse de :func:`parse_evidence`)."""
    segs: List[str] = []
    count = int(ev.get("count") or 1)
    if count > 1:
        segs.append(f"{count} notices")
    keys = list(ev.get("keys") or [])
    if keys:
        shown = keys[:_EVIDENCE_MAX_KEYS]
        segs.append("clés " + ", ".join(shown))
    fuzzy = list(ev.get("fuzzy") or [])
    if fuzzy:
        segs.append("floue " + " + ".join(fuzzy[:_EVIDENCE_MAX_FUZZY]))
    origins = ev.get("origins") or {}
    if origins:
        # Champs regroupés par origine, dans l'ordre de leur premier champ.
        by_origin: Dict[Tuple[str, ...], List[str]] = {}
        for field in sorted(origins, key=_origin_field_order):
            srcs = tuple(k for k in (origins[field] or ()) if k)
            if srcs:
                by_origin.setdefault(srcs, []).append(field)
        parts = ["+".join(fields) + "="
                 + "|".join(display_name(k) for k in srcs)
                 for srcs, fields in by_origin.items()]
        if parts:
            segs.append("origines " + ", ".join(parts))
    if ev.get("ambiguous"):
        segs.append("ambigu " + ev["ambiguous"])
    return f"{EVIDENCE_PREFIX} " + " ; ".join(segs)


def _origin_field_order(field: str) -> Tuple[int, int, str]:
    if field in ORIGIN_FIELDS:
        return (0, ORIGIN_FIELDS.index(field), field)
    if field == "itemType":
        return (1, 0, field)
    return (2, 0, field)


# === Informations par membre ===

def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _value_tiebreak(value: Any) -> Tuple[int, str]:
    """Départage déterministe : valeur la plus longue, puis lexicographique."""
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str)
    return (-len(text), text)


def _canonical_dump(item: Dict) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)


def _dump_key(value: Any) -> str:
    """Clé de contenu d'une valeur quelconque (tag, collection, relation)."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class _MemberInfo:
    """Lecture des sources, origines et avertissements d'un membre."""

    __slots__ = ("item", "sources", "labels", "evidence", "count", "rank",
                 "keys", "truncated", "snippet", "extra")

    def __init__(self, item: Dict):
        self.item = item
        self.labels = _source_labels(item)
        seen: List[str] = []
        for label in self.labels:
            key = _normalize_source(label)
            if key and key not in seen:
                seen.append(key)
        self.sources = seen
        self.evidence = parse_evidence(item)
        self.count = self.evidence["count"] if self.evidence else 1
        self.rank = min((_general_rank(s) for s in seen), default=_UNKNOWN_RANK)
        self.keys = None  # calculées à la demande (tri, preuve)
        self.extra = extra_lines(item)
        lines = {k for k, _ in self.extra}
        self.truncated = "Scholar truncation" in lines
        self.snippet = "Scholar snippet" in lines

    def origin(self, field: str) -> Tuple[str, ...]:
        """
        Sources pouvant avoir fourni la valeur de ``field`` : origine tracée
        par une fusion antérieure, sinon la source unique de la notice ; pour
        une notice fusionnée sans origine tracée, l'ensemble de ses sources
        (origine ambiguë, jamais créditée à une source prioritaire).
        """
        if self.evidence and self.evidence["origins"].get(field):
            return tuple(self.evidence["origins"][field])
        if len(self.sources) == 1:
            return (self.sources[0],)
        if not self.sources:
            return ("",)
        return tuple(self.sources)

    def value(self, field: str) -> Any:
        """
        Valeur de ``field`` portée par le membre. Pour une notice déjà
        fusionnée dont le type ne prévoyait pas ce champ, la valeur retenue
        par la fusion antérieure a été reportée dans ``extra`` (``champ:
        valeur``, forme de ``validate_and_clean``) avec son origine : elle
        redevient candidate, de sorte qu'une fusion en deux temps choisit
        comme une fusion en une fois. Plusieurs lignes du même champ :
        aucune n'est reprise (origine incertaine).
        """
        value = self.item.get(field)
        if _non_empty(value):
            return value
        if self.evidence and field in self.evidence["origins"] \
                and field not in _SPECIAL_FIELDS:
            parked = {v for k, v in self.extra if k == field and v}
            if len(parked) == 1:
                return next(iter(parked))
        return None

    def parked_fields(self) -> List[str]:
        """Champs d'origine tracée dont la valeur n'est que dans ``extra``."""
        if not self.evidence:
            return []
        return [f for f in self.evidence["origins"]
                if f not in _SPECIAL_FIELDS
                and not _non_empty(self.item.get(f))
                and self.value(f) is not None]

    def strong_keys(self) -> List[str]:
        if self.keys is None:
            self.keys = [k for k, _ in strong_key_sources(self.item)]
        return self.keys

    def sort_key(self) -> Tuple:
        from utils.doi_resolver import normalize_title
        return (self.rank, tuple(self.strong_keys()),
                normalize_title(self.item.get("title", "") or ""),
                _canonical_dump(self.item))


def _origin_rank(origin: Tuple[str, ...], priority: Optional[List[str]]):
    """
    Rang d'une origine pour un champ : (rang dans la priorité du champ, rang
    général). Une origine ambiguë (plusieurs sources possibles) n'est
    créditée à aucune source listée et prend le rang général le moins bon.
    """
    if len(origin) == 1:
        src = origin[0]
        prio = (priority.index(src) if priority and src in priority
                else len(priority or ()))
        return (prio, _general_rank(src))
    return (len(priority or ()), max(_general_rank(s) for s in origin))


def _choose_value(infos: List[_MemberInfo], field: str):
    """
    Valeur retenue pour ``field`` et son origine (ordre total, stable). Pour
    un champ de contenant, un nom de dépôt (:func:`is_repository_venue`)
    passe après toute vraie valeur, quelle qu'en soit la source.
    """
    priority = SOURCE_FIELD_PRIORITY.get(field)
    venue = field in VENUE_FIELDS
    best = None
    for info in infos:
        value = info.value(field)
        if not _non_empty(value):
            continue
        origin = info.origin(field)
        rank = (venue and is_repository_venue(value),
                _origin_rank(origin, priority), _value_tiebreak(value))
        if best is None or rank < best[0]:
            best = (rank, value, origin)
    if best is None:
        return None, None
    return best[1], best[2]


def _named_authors(creators: List[Dict]) -> int:
    """Nombre d'auteurs nommés (à défaut, de contributeurs nommés)."""
    def named(c: Dict) -> bool:
        return bool((c.get("lastName") or c.get("name") or "").strip())
    authors = [c for c in creators or []
               if (c.get("creatorType") or "author") == "author" and named(c)]
    if authors:
        return len(authors)
    return sum(1 for c in creators or [] if named(c))


def _creators_truncated(info: _MemberInfo) -> bool:
    """Vrai si la liste d'auteurs du membre peut être tronquée par Scholar."""
    if not info.truncated:
        return False
    origin = info.origin("creators")
    return "google_scholar" in origin or origin == ("",)


def _choose_creators(infos: List[_MemberInfo]):
    candidates = [i for i in infos if _non_empty(i.item.get("creators"))]
    if not candidates:
        return [], None
    complete = [i for i in candidates if not _creators_truncated(i)]
    pool = complete or candidates
    best = None
    for info in pool:
        creators = info.item.get("creators") or []
        origin = info.origin("creators")
        rank = (-_named_authors(creators), _origin_rank(origin, None),
                _value_tiebreak(creators))
        if best is None or rank < best[0]:
            best = (rank, creators, origin)
    return list(best[1]), best[2]


def _choose_item_type(infos: List[_MemberInfo]):
    """Type Zotero et son origine : celui de la meilleure source, un type
    précis primant sur ``document``."""
    best = None
    for info in infos:
        itype = info.item.get("itemType") or ""
        if not itype:
            continue
        origin = info.origin("itemType")
        rank = (itype in _GENERIC_ITEM_TYPES, _origin_rank(origin, None),
                itype)
        if best is None or rank < best[0]:
            best = (rank, itype, origin)
    return (best[1], best[2]) if best else ("document", None)


def _merge_sources_line(infos: List[_MemberInfo]) -> Optional[str]:
    labels_by_key: Dict[str, Set[str]] = defaultdict(set)
    for info in infos:
        for label in info.labels:
            key = _normalize_source(label)
            if key:
                labels_by_key[key].add(label)
    if not labels_by_key:
        return None
    chosen = []
    for key, labels in labels_by_key.items():
        preferred = SOURCE_DISPLAY_NAMES.get(key)
        chosen.append(preferred if preferred in labels else min(labels))
    return "Sources: " + ", ".join(sorted(chosen))


def _url_list(value: str) -> List[str]:
    return [u.strip() for u in value.split("|") if u.strip()]


def merge_items(items: List[Dict],
                evidence: Optional[Dict[str, Any]] = None) -> Dict:
    """
    Fusionne plusieurs notices représentant le même travail.

    Le résultat ne dépend pas de l'ordre de ``items`` (voir l'en-tête du
    module pour les règles par champ). Une liste d'un seul item est renvoyée
    telle quelle.

    Args:
        items: Notices considérées comme doublons.
        evidence: Éléments de preuve fournis par :func:`deduplicate`
            (``keys``, ``fuzzy``, ``ambiguous``) ; à défaut, les clés fortes
            partagées par au moins deux membres.

    Returns:
        Notice Zotero fusionnée.
    """
    if not items:
        return {}
    if len(items) == 1 and not (evidence and evidence.get("ambiguous")):
        return items[0]
    if len(items) == 1:
        return _annotate_single(items[0], evidence)

    infos = sorted((_MemberInfo(it) for it in items), key=_MemberInfo.sort_key)
    merged: Dict[str, Any] = {}
    origins: Dict[str, Tuple[str, ...]] = {}

    # Ordre des champs : celui du premier membre trié, puis les champs
    # nouveaux dans l'ordre des membres (déterministe) ; enfin les champs
    # qu'une fusion antérieure avait reportés dans extra.
    fields: List[str] = []
    for info in infos:
        for key in info.item.keys():
            if key not in fields:
                fields.append(key)
    for info in infos:
        for key in info.parked_fields():
            if key not in fields:
                fields.append(key)

    for field in fields:
        if field in _SPECIAL_FIELDS:
            continue
        value, origin = _choose_value(infos, field)
        if value is None:
            # Conserver la présence du champ (valeur vide du premier membre).
            for info in infos:
                if field in info.item:
                    merged[field] = info.item[field]
                    break
            continue
        merged[field] = value
        # Origine de chaque champ arbitré (C21), pas seulement des champs
        # cités en tête de la ligne.
        origins[field] = origin

    merged["itemType"], type_origin = _choose_item_type(infos)
    if type_origin:
        origins["itemType"] = type_origin

    def present(field: str) -> bool:
        return any(field in info.item for info in infos)

    creators, c_origin = _choose_creators(infos)
    if creators or present("creators"):
        merged["creators"] = creators
    if creators:
        origins["creators"] = c_origin

    # Agrégats (tags, collections, relations, ISBN) : union dédoublonnée,
    # triée sur une clé de contenu, représentant choisi par le plus petit
    # contenu ; le résultat ne dépend ni de l'ordre des membres ni de
    # l'historique des fusions (fusion en deux temps = fusion en une fois).
    tag_by_norm: Dict[str, Any] = {}
    for info in infos:
        for tag in info.item.get("tags") or []:
            text = (tag.get("tag") if isinstance(tag, dict) else str(tag)) or ""
            norm = text.strip().lower()
            if not norm:
                continue
            if norm not in tag_by_norm or \
                    _dump_key(tag) < _dump_key(tag_by_norm[norm]):
                tag_by_norm[norm] = tag
    tags = [tag_by_norm[n] for n in sorted(tag_by_norm)]
    if tags or present("tags"):
        merged["tags"] = tags

    collections = sorted({_dump_key(c): c for info in infos
                          for c in info.item.get("collections") or []}.items())
    if collections or present("collections"):
        merged["collections"] = [c for _k, c in collections]

    relations: Dict[str, Any] = {}
    for info in infos:
        rel = info.item.get("relations")
        if isinstance(rel, dict):
            for k, v in rel.items():
                if k not in relations or _dump_key(v) < _dump_key(relations[k]):
                    relations[k] = v
    if relations or present("relations"):
        merged["relations"] = {k: relations[k] for k in sorted(relations)}

    # ISBN : union des ISBN distincts (identifiants agrégés).
    isbn_by_key: Dict[str, str] = {}
    for info in infos:
        raw = info.item.get("ISBN")
        if not _non_empty(raw):
            continue
        for token in re.split(r"[;,]|\s+(?=[\dXx-]{10,})", str(raw)):
            token = token.strip()
            if not token:
                continue
            norms = normalize_isbns(token)
            key = norms[0] if norms else token.lower()
            if key not in isbn_by_key or token < isbn_by_key[key]:
                isbn_by_key[key] = token
    isbn_values = [isbn_by_key[k] for k in sorted(isbn_by_key)]
    if isbn_values:
        merged["ISBN"] = " ".join(isbn_values)
    elif any("ISBN" in i.item for i in infos):
        merged["ISBN"] = ""

    parked = _park_disallowed_fields(merged)
    merged["extra"] = _merge_extra(infos, merged, origins, evidence, parked)
    return merged


def _park_disallowed_fields(merged: Dict[str, Any]) -> List[str]:
    """
    Retire de la notice fusionnée les champs que son type ne prévoit pas
    (union de membres de types différents : ``publicationTitle`` d'un
    article sur un chapitre retenu comme ``bookSection``, que l'export CSL
    lirait comme contenant) et renvoie leurs lignes ``champ: valeur`` pour
    ``extra``, comme ``validate_and_clean`` : aucune information perdue,
    aucun champ Zotero invalide.
    """
    allowed = _allowed_fields(merged.get("itemType") or "document")
    lines: List[str] = []
    for field in list(merged):
        if field in allowed:
            continue
        value = merged.pop(field)
        if isinstance(value, str) and value.strip():
            lines.append(f"{field}: {value.strip()}")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"{field}: {value}")
        elif isinstance(value, list) and value:
            lines.append(f"{field}: {', '.join(str(v) for v in value)}")
    return lines


def _merge_extra(infos: List[_MemberInfo], merged: Dict[str, Any],
                 origins: Dict[str, Tuple[str, ...]],
                 evidence: Optional[Dict[str, Any]],
                 parked: Optional[List[str]] = None) -> str:
    lines: List[str] = []
    sources_line = _merge_sources_line(infos)
    if sources_line:
        lines.append(sources_line)

    seen: Set[str] = set()
    seen_dois: Set[str] = set()
    snippet_line = truncation_line = None
    alt_urls: List[str] = []
    def add_line(raw: str, info: Optional[_MemberInfo]) -> None:
        nonlocal snippet_line, truncation_line
        line = raw.strip()
        if not line:
            return
        key = line.split(":", 1)[0].strip() if ":" in line else ""
        if (key and key != "DOI" and key not in _REBUILT_LINES
                and isinstance(merged.get(key), str)
                and merged[key].strip() == line.split(":", 1)[1].strip()):
            # Valeur reportée dans extra par une fusion antérieure (ou par
            # validate_and_clean), redevenue champ avec la même valeur.
            return
        if key == "Scholar snippet":
            if info is not None and "google_scholar" in \
                    info.origin("abstractNote"):
                snippet_line = snippet_line or line
            return
        if key == "Scholar truncation":
            truncation_line = truncation_line or line
            return
        if key == "Alternative URLs":
            for u in _url_list(line.split(":", 1)[1]):
                if u not in alt_urls:
                    alt_urls.append(u)
            return
        if key in _REBUILT_LINES:
            return
        if key == "DOI":
            norm = normalize_doi(line.split(":", 1)[1])
            if norm:
                if norm in seen_dois:
                    return
                seen_dois.add(norm)
        if line not in seen:
            seen.add(line)
            lines.append(line)

    for info in infos:
        for raw in (info.item.get("extra") or "").split("\n"):
            add_line(raw, info)
        url = info.item.get("url")
        if _non_empty(url) and url.strip() not in alt_urls:
            alt_urls.append(url.strip())
    # Champs que le type retenu ne prévoit pas, après les lignes des membres
    # (même traitement : dédoublonnage, DOI normalisés).
    for raw in parked or []:
        add_line(raw, None)

    chosen_url = (merged.get("url") or "").strip()
    alt_urls = [u for u in alt_urls if u != chosen_url]
    if alt_urls:
        lines.append("Alternative URLs: " + " | ".join(alt_urls))

    # Avertissements Scholar : seulement si le champ qualifié vient de Scholar.
    abstract_origin = origins.get("abstractNote") or ()
    if snippet_line and "google_scholar" in abstract_origin:
        lines.append(snippet_line)
    creators_origin = origins.get("creators") or ()
    venue_origin = origins.get("publicationTitle") or ()
    if truncation_line and ("google_scholar" in creators_origin
                            or "google_scholar" in venue_origin):
        lines.append(truncation_line)

    ev = _combine_evidence(infos, origins, evidence)
    lines.append(format_evidence(ev))
    return "\n".join(lines)


def _combine_evidence(infos: List[_MemberInfo],
                      origins: Dict[str, Tuple[str, ...]],
                      evidence: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Preuve de la fusion : membres, clés partagées, appariements flous."""
    count = sum(i.count for i in infos)
    keys: List[str] = []
    fuzzy: List[str] = []
    for info in infos:
        if info.evidence:
            keys.extend(info.evidence["keys"])
            fuzzy.extend(info.evidence["fuzzy"])
    if evidence and evidence.get("keys") is not None:
        keys.extend(evidence["keys"])
    else:
        counts: Dict[str, int] = defaultdict(int)
        for info in infos:
            for k in set(info.strong_keys()):
                counts[k] += 1
        keys.extend(k for k, n in counts.items() if n >= 2)
    if evidence:
        fuzzy.extend(evidence.get("fuzzy") or [])
    ambiguous = (evidence or {}).get("ambiguous", "")
    return {
        "count": count,
        "keys": sorted(set(keys), key=_key_order),
        "fuzzy": sorted(set(fuzzy)),
        "origins": {f: list(o) for f, o in origins.items() if o and o != ("",)},
        "ambiguous": ambiguous,
    }


def _key_order(key: str) -> Tuple[int, str]:
    kinds = ["doi", "hal", "isbn", "arxiv", "pmid", "oai", "isidore",
             "openalex", "s2", "core", "scholar", "url"]
    kind = key.split(":", 1)[0]
    return (kinds.index(kind) if kind in kinds else len(kinds), key)


def _annotate_single(item: Dict, evidence: Dict[str, Any]) -> Dict:
    """Pose (ou remplace) la ligne Dedup evidence d'une notice non fusionnée
    laissée à part pour ambiguïté."""
    info = _MemberInfo(item)
    ev = info.evidence or {"count": 1, "keys": [], "fuzzy": [], "origins": {},
                           "ambiguous": ""}
    ev = dict(ev)
    ev["ambiguous"] = evidence.get("ambiguous", "")
    out = dict(item)
    lines = [l for l in (item.get("extra") or "").split("\n")
             if l.strip() and not l.strip().startswith(EVIDENCE_PREFIX)]
    lines.append(format_evidence(ev))
    out["extra"] = "\n".join(lines)
    return out


# === Provenance lue par les statistiques ===

def abstract_is_scholar_snippet(item: Dict) -> bool:
    """
    Vrai si ``abstractNote`` contient l'extrait Google Scholar et non un
    résumé : ligne ``Scholar snippet`` présente et résumé venu de Scholar
    (origine tracée, ou notice issue de Scholar seul ; une notice fusionnée
    sans origine tracée est comptée comme extrait, par prudence).
    """
    if not _non_empty(item.get("abstractNote")):
        return False
    info = _MemberInfo(item)
    if not info.snippet:
        return False
    return "google_scholar" in info.origin("abstractNote") or \
        info.origin("abstractNote") == ("",)


def authors_truncated(item: Dict) -> bool:
    """
    Vrai si la liste d'auteurs peut être tronquée par Google Scholar (ligne
    ``Scholar truncation`` et auteurs venus de Scholar). La marque du mapper
    couvre « auteurs et/ou revue » : le compte est un majorant.
    """
    return _creators_truncated(_MemberInfo(item))


def source_overlap_matrix(items: List[Dict]) -> Dict[str, int]:
    """
    Compte, sur un corpus dédupliqué, les recouvrements par paire de sources.

    Un item fusionné dont la ligne ``Sources:`` liste plusieurs sources atteste
    qu'au moins deux sources couvraient la même ressource. On compte chaque
    paire (non ordonnée, clés de registre) une fois par item partagé.

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


# === Passe floue : caractéristiques ===

_STOPWORDS = {
    "a", "au", "aux", "avec", "ce", "ces", "cette", "chez", "comme", "d",
    "dans", "de", "des", "du", "en", "entre", "est", "et", "il", "ils", "l",
    "la", "le", "les", "leur", "leurs", "mais", "ne", "ni", "ou", "par",
    "pas", "pour", "qu", "que", "qui", "s", "sa", "se", "ses", "son", "sont",
    "sous", "sur", "un", "une", "vers", "y", "an", "and", "are", "as", "at",
    "be", "by", "for", "from", "in", "is", "it", "its", "of", "on", "or",
    "the", "their", "this", "to", "with", "o", "el", "los", "las", "del",
    "y", "e", "di", "da", "der", "die", "das", "und", "im",
}

# Vocabulaire des titres génériques : exclu du décompte des mots
# significatifs, de sorte que « Introduction : le numérique » reste générique.
_GENERIC_WORDS = {
    "introduction", "introductions", "conclusion", "conclusions", "preface",
    "postface", "avant", "propos", "editorial", "edito", "presentation",
    "compte", "comptes", "rendu", "rendus", "recension", "recensions",
    "foreword", "afterword", "epilogue", "prologue", "remerciements",
    "acknowledgements", "acknowledgments", "sommaire", "contents", "index",
    "bibliographie", "bibliography", "references", "annexe", "annexes",
    "erratum", "errata", "corrigendum", "review", "reviews", "book",
    "note", "notes", "lecture", "lectures", "resume", "resumes", "abstract",
    "abstracts", "chronique", "chroniques", "entretien", "interview",
    "hommage", "memoriam", "liminaire", "ouverture", "generale", "general",
    "dossier", "varia", "discussion", "debat", "numero", "issue", "volume",
    "special", "guise", "glossaire", "glossary", "table", "matieres",
}

_GENERIC_TITLES = {
    "introduction", "editorial", "edito", "compte rendu", "comptes rendus",
    "avant propos", "conclusion", "conclusions", "preface", "postface",
    "foreword", "afterword", "presentation", "remerciements",
    "acknowledgements", "acknowledgments", "sommaire", "table des matieres",
    "contents", "bibliographie", "bibliography", "references", "index",
    "notes de lecture", "note de lecture", "book review", "book reviews",
    "review", "reviews", "abstract", "resume", "resumes", "erratum",
    "corrigendum", "discussion", "entretien", "interview", "in memoriam",
    "hommage", "liminaire", "ouverture", "epilogue", "prologue", "annexe",
    "annexes", "chronique", "varia",
}

_REVIEW_RE = re.compile(
    r"^(compte[s]? rendu[s]?|c r |recension|notes? de lecture|note critique"
    r"|review of|book review|reviewed work|rezension|resena)\b")
_REVIEW_EDITORS_RE = re.compile(
    r"\b(dir|dirs|eds?|ed|coord|sous la dir)\b.*\b(1[5-9]\d{2}|20\d{2})\b")

# Mots de paratexte : deux titres qui ne diffèrent que par ces mots désignent
# un ouvrage et l'une de ses parties (« X » et « X : introduction »).
_PARATEXT_WORDS = {
    "introduction", "introductions", "conclusion", "conclusions", "preface",
    "postface", "avant", "propos", "presentation", "epilogue", "prologue",
    "foreword", "afterword", "annexe", "annexes", "appendix", "appendice",
    "bibliographie", "bibliography", "references", "index", "sommaire",
    "remerciements", "acknowledgements", "acknowledgments", "glossaire",
    "glossary", "table", "matieres", "contents", "resume", "resumes",
    "abstract", "abstracts", "generale", "general", "liminaire", "ouverture",
    "postscript", "erratum", "errata", "corrigendum", "chronologie",
    # Pièces qui répondent à un texte ou le corrigent : œuvres distinctes.
    "correction", "corrections", "retraction", "retracted", "addendum",
    "reply", "rejoinder", "comment", "comments", "commentary", "response",
}

# Chiffres romains de deux lettres ou plus (« i », « v », « x » sont trop
# ambigus pour être lus comme des numéros).
_ROMAN_RE = re.compile(r"^(?=[ivxlc]{2,}$)c{0,3}(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")

_PARTICLES = {"de", "du", "des", "d", "van", "von", "der", "den", "da", "di",
              "le", "la", "del", "della", "dos", "das", "ten", "ter", "mc"}


@lru_cache(maxsize=131072)
def _norm_title_cached(title: str) -> str:
    from utils.doi_resolver import normalize_title
    return normalize_title(title)


def _norm_title(title: str) -> str:
    return _norm_title_cached(title) if isinstance(title, str) and title \
        else ""


def _significant_words(norm_title: str) -> int:
    return sum(1 for w in norm_title.split()
               if len(w) > 1 and w not in _STOPWORDS
               and w not in _GENERIC_WORDS and not w.isdigit())


def _title_class(norm: str) -> str:
    """``"blocked"`` (vide, générique, aucun mot significatif), ``"short"``
    (moins de ``FUZZY_MIN_SIGNIFICANT_WORDS`` mots significatifs) ou
    ``"ok"``."""
    if not norm or norm in _GENERIC_TITLES:
        return "blocked"
    n = _significant_words(norm)
    if n == 0:
        return "blocked"
    return "short" if n < FUZZY_MIN_SIGNIFICANT_WORDS else "ok"


def is_generic_title(title: str) -> bool:
    """
    Vrai si le titre est exclu de l'appariement approché : titre générique
    (« Introduction », « Éditorial », « Compte rendu »…) ou de moins de
    ``FUZZY_MIN_SIGNIFICANT_WORDS`` mots significatifs. Un titre court non
    générique reste apparié par égalité exacte (même année, même auteur).
    """
    return _title_class(_norm_title(title)) != "ok"


def _is_review_title(norm_title: str) -> bool:
    return bool(_REVIEW_RE.match(norm_title)
                or _REVIEW_EDITORS_RE.search(norm_title))


@lru_cache(maxsize=131072)
def _author_key_cached(name: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", unidecode(name).lower()).split()
    while len(words) > 1 and words[0] in _PARTICLES:
        words = words[1:]
    return "".join(words)


def _author_key(name: str) -> str:
    """Nom normalisé (translittération, minuscules, signes retirés,
    particules initiales retirées) ; mis en cache : les mêmes noms reviennent
    à chaque notice fusionnée et à chaque tour de la passe floue."""
    if isinstance(name, str):
        return _author_key_cached(name)
    return _author_key_cached(unidecode(name or ""))


def _author_creators(item: Dict) -> List[Dict]:
    """Auteurs d'une notice (à défaut, tous ses contributeurs)."""
    creators = item.get("creators") or []
    authors = [c for c in creators
               if (c.get("creatorType") or "author") == "author"]
    return authors or creators


def first_author_key(item: Dict) -> str:
    """Nom normalisé du premier auteur (particules initiales retirées)."""
    for c in _author_creators(item):
        key = _author_key(c.get("lastName") or c.get("name") or "")
        if key:
            return key
    return ""


def author_keys(item: Dict) -> Set[str]:
    """Noms normalisés de tous les auteurs (à défaut, des contributeurs)."""
    out = set()
    for c in _author_creators(item):
        key = _author_key(c.get("lastName") or c.get("name") or "")
        if key:
            out.add(key)
    return out


# --- Corroboration par l'auteur : jetons du nom complet (OPT-06 bis) ---

# Particules de trois lettres ou plus, écartées des jetons de nom ; les plus
# courtes (« de », « da », « di », « le », « d »…) le sont par la longueur.
_NAME_PARTICLES = _PARTICLES | {
    "des", "dos", "das", "del", "della", "delle", "degli", "dei", "der",
    "den", "van", "von", "vom", "zur", "ten", "ter", "les", "ibn", "bin",
}
# Longueur minimale d'un jeton de nom significatif : les initiales (« J »,
# « SM ») ne corroborent pas un prénom.
NAME_TOKEN_MIN_LETTERS = 3
# Au-delà de ce nombre de jetons (nom et prénom réunis), un nom n'a pas de
# profil : saisie anormale (liste d'auteurs dans un seul champ), et borne de
# l'index des paires candidates (sous-ensembles des jetons).
NAME_MAX_TOKENS = 6


_NO_PARTS = (frozenset(), frozenset())


@lru_cache(maxsize=131072)
def _name_parts_cached(value: str) -> Tuple[frozenset, frozenset]:
    tokens: Set[str] = set()
    compounds = set()
    for word in unidecode(value).lower().split():
        parts = [w for w in re.sub(r"[^a-z0-9]+", " ", word).split()
                 if len(w) >= NAME_TOKEN_MIN_LETTERS and w.isalpha()
                 and w not in _NAME_PARTICLES]
        tokens.update(parts)
        if len(parts) >= 2:
            compounds.add(frozenset(parts))
    return frozenset(tokens), frozenset(compounds)


def _name_parts(value: Any) -> Tuple[frozenset, frozenset]:
    """``(jetons, composés)`` d'un nom ou d'un prénom. Jetons significatifs :
    translittération, minuscules, découpage sur tout signe non
    alphanumérique (tirets compris), particules et jetons de moins de
    ``NAME_TOKEN_MIN_LETTERS`` lettres écartés. Composés : jetons d'un même
    mot sans espace (« Jean-Paul », « Kerbrat-Orecchioni »), quand il en
    compte au moins deux."""
    if not isinstance(value, str) or not value:
        return _NO_PARTS
    return _name_parts_cached(value)


def name_profile(creator: Optional[Dict], key: Optional[str] = None):
    """
    Profil de nom d'un auteur saisi en deux champs : ``(clé, jetons du nom,
    jetons du prénom, composés)``, la clé étant celle de
    :func:`first_author_key` (``key`` la fournit si elle est déjà calculée)
    et les composés les ensembles de jetons liés par un trait d'union (ou
    tout autre signe sans espace) dans l'un ou l'autre champ. ``None`` pour
    un auteur saisi en un seul champ (``name`` : nom et prénom
    indiscernables), sans jeton significatif de nom ou de prénom (prénom
    réduit à des initiales), ou de plus de ``NAME_MAX_TOKENS`` jetons.
    """
    if not isinstance(creator, dict):
        return None
    last = creator.get("lastName")
    surname, bound_last = _name_parts(last)
    if not surname:
        return None
    given, bound_first = _name_parts(creator.get("firstName"))
    if not given or len(surname | given) > NAME_MAX_TOKENS:
        return None
    return (key if key is not None else _author_key(last), surname, given,
            bound_last | bound_first)


def names_equivalent(p, q) -> bool:
    """
    Vrai si deux profils de nom (:func:`name_profile`) désignent la même
    personne saisie autrement : ordre nom / prénom inversé, nom composé
    partagé autrement entre les deux champs, ou nom composé abrégé.

    Cas réels : « CHOPPIN DE JANVRY, Thibaud » / « DE JANVRY, Thibaud
    Choppin » ; « Marques dos Santos, Clecio » / « Santos, Clecio Marques
    dos ». Règle (jetons de nom et de prénom, particules et jetons de moins
    de trois lettres écartés) :

    - les jetons de l'un sont **tous** dans l'autre (un nom ne fait que
      compléter ou redistribuer l'autre ; deux jetons propres de part et
      d'autre, « Clecio » / « Maria », signalent deux personnes) ;
    - au moins deux jetons communs ;
    - un jeton de nom commun **et** un jeton de prénom commun (le prénom est
      corroboré comme prénom : « Martin, Paul » / « Paul, Jacques Martin »
      n'est pas accepté), ou bien inversion exacte des deux champs
      (« Dupont, Jean » / « Jean, Dupont ») ;
    - aucun jeton qui change de champ n'est lié, des deux côtés, par un
      trait d'union à des jetons différents : « Martin-Paul, Jean » /
      « Martin, Jean-Paul » (Jean Martin-Paul et Jean-Paul Martin) ne sont
      pas équivalents, car une source redistribue les mots aux espaces, pas
      au milieu d'un composé. Le composé présent d'un seul côté reste
      accepté (« Kerbrat-Orecchioni, Catherine » / « Orecchioni, Catherine
      Kerbrat » : trait d'union perdu par l'autre source).

    Membres d'une même famille (« Marques dos Santos, Clecio » / « Santos,
    Maria Marques dos »), cousins partageant deux noms, prénom de l'un pris
    pour nom de l'autre sans prénom commun, composés à trait d'union
    contradictoires, prénom en initiales : jamais équivalents.

    Limite (documentée) : sans trait d'union, « Martin Paul, Jean » /
    « Martin, Jean Paul » a exactement la structure du cas réel Janvry
    (un jeton du nom de l'un est dans le prénom de l'autre, nom et prénom
    corroborés par ailleurs) et reste équivalent ; aucune règle sur les
    seuls jetons ne sépare les deux. La fusion exige de toute façon un
    titre à 0,93 au moins, l'année à ± 1 et des types compatibles.
    """
    if p is None or q is None:
        return False
    _ka, sa, ga, ca = p
    _kb, sb, gb, cb = q
    fa, fb = sa | ga, sb | gb
    if not (fa <= fb or fb <= fa):
        return False
    if len(fa & fb) < 2:
        return False
    if not ((sa & sb and ga & gb) or (sa == gb and sb == ga)):
        return False
    if ca and cb:
        # Jetons qui changent de champ : liés à des partenaires différents
        # des deux côtés, ils désignent deux composés distincts.
        for token in (sa & gb) | (ga & sb):
            left = [c for c in ca if token in c]
            right = [c for c in cb if token in c]
            if left and right and set(left) != set(right):
                return False
    return True


def _authors_digest(item: Dict):
    """
    Un seul passage sur les auteurs : ``(clé du premier auteur, clés de
    tous les auteurs, profil du premier auteur, profils de tous les
    auteurs)``, mêmes définitions que :func:`first_author_key`,
    :func:`author_keys` et :func:`name_profile`.
    """
    first_key, first_profile = "", None
    keys: Set[str] = set()
    profiles = set()
    for c in _author_creators(item):
        key = _author_key(c.get("lastName") or c.get("name") or "")
        if not key:
            continue
        keys.add(key)
        prof = name_profile(c, key) if c.get("lastName") else None
        if prof is not None:
            profiles.add(prof)
        if not first_key:
            first_key, first_profile = key, prof
    return first_key, keys, first_profile, frozenset(profiles)


def author_profiles(item: Dict) -> frozenset:
    """Profils de nom (:func:`name_profile`) de tous les auteurs saisis en
    deux champs."""
    return _authors_digest(item)[3]


def _types_compatible(t1: str, t2: str) -> bool:
    if t1 == t2:
        return True
    return t1 in _GENERIC_ITEM_TYPES or t2 in _GENERIC_ITEM_TYPES


def _containers(record: Dict) -> Set[str]:
    """Titres normalisés des contenants d'une notice (revue, ouvrage, actes,
    y compris reportés dans ``extra`` par ``validate_and_clean`` ou par une
    fusion), noms de dépôt exclus."""
    values = [record.get(f) for f in VENUE_FIELDS]
    values.extend(v for k, v in extra_lines(record) if k in VENUE_FIELDS)
    out = set()
    for v in values:
        if isinstance(v, str) and v.strip() and not is_repository_venue(v):
            norm = _norm_title(v)
            if norm:
                out.add(norm)
    return out


_PAGE_RANGE_RE = re.compile(r"\d+\s*[-–—]\s*\d+")


def _has_page_range(record: Dict) -> bool:
    """Vrai si la notice porte une plage de pages (champ ``pages`` ou ligne
    ``pages:`` reportée dans ``extra``) : c'est une pièce d'un ensemble."""
    values = [record.get("pages")]
    values.extend(v for k, v in extra_lines(record) if k == "pages")
    return any(isinstance(v, str) and _PAGE_RANGE_RE.search(v) for v in values)


class _Features:
    """Caractéristiques d'un groupe pour la passe floue."""

    __slots__ = ("title", "year", "author", "authors", "itype", "tclass",
                 "review", "doi", "hal", "isbn", "arxiv", "pmid", "raw_title",
                 "containers", "page_range", "chars", "author_profile",
                 "profiles")

    def __init__(self, record: Dict):
        self.raw_title = record.get("title", "") or ""
        self.title = _norm_title(self.raw_title)
        y = extract_year(record.get("date", "") or "")
        self.year = int(y) if y else None
        # Premier auteur et tous les auteurs : clés normalisées et profils de
        # nom (jetons du nom et du prénom, voir names_equivalent).
        (self.author, self.authors, self.author_profile,
         self.profiles) = _authors_digest(record)
        self.itype = record.get("itemType", "") or ""
        self.tclass = _title_class(self.title)
        self.review = _is_review_title(self.title)
        keys = [k for k, _ in strong_key_sources(record)]
        # La clé doi: est le DOI d'identité (identity_doi), déjà calculé.
        self.doi = next(iter(keys_of_kind(keys, "doi")), None)
        self.hal = set(keys_of_kind(keys, "hal"))
        self.isbn = set(keys_of_kind(keys, "isbn"))
        self.arxiv = set(keys_of_kind(keys, "arxiv"))
        self.pmid = set(keys_of_kind(keys, "pmid"))
        self.containers = _containers(record)
        self.page_range = _has_page_range(record)
        self.chars = None  # profil de caractères du titre, à la demande

    def order_key(self) -> Tuple:
        """Clé totale (sur les caractéristiques comparées) qui fixe l'ordre
        canonique d'une paire."""
        return (self.title, self.raw_title, self.year or 0, self.author,
                tuple(sorted(self.authors)), self.itype, self.doi or "",
                tuple(sorted(self.hal)))


def _conflict(a: Set[str], b: Set[str]) -> bool:
    return bool(a) and bool(b) and not (a & b)


# Types qui désignent une pièce d'un ensemble : une notice de ce type n'est
# jamais le « tout » d'une relation partie/tout.
_PART_ITEM_TYPES = {
    "bookSection", "journalArticle", "conferencePaper", "encyclopediaArticle",
    "dictionaryEntry", "magazineArticle", "newspaperArticle", "blogPost",
    "forumPost",
}


def _part_of(a: "_Features", b: "_Features") -> bool:
    """
    Vrai si ``a`` est une partie de ``b`` : le contenant de ``a`` (ouvrage,
    revue) porte le titre de ``b``, qui n'est ni lui-même une pièce
    (chapitre, article…) ni contenu dans un ensemble du même titre. Cas
    réel : chapitre « Dialogisme et polyphonie » (contenant « Dialogisme et
    polyphonie ») et notice de l'ouvrage collectif du même titre, typée
    ``document`` par Semantic Scholar. Une notice qui porte une plage de
    pages est une pièce, pas l'ouvrage : deux dépôts HAL d'un même chapitre,
    dont l'un répète le titre du chapitre comme titre d'ouvrage, et la
    notice Semantic Scholar de ce chapitre (pages 135-150) restent
    fusionnables (cas réel Rabatel, « Diversité des points de vue et
    mobilité empathique »).
    """
    return bool(b.title) and b.itype not in _PART_ITEM_TYPES \
        and not b.page_range \
        and b.title in a.containers and b.title not in b.containers


def _paratext_only_difference(ta: str, tb: str) -> bool:
    """Vrai si les mots propres à l'un des titres ne sont que du paratexte
    (et des mots vides) : l'un désigne une partie de l'autre."""
    wa, wb = set(ta.split()), set(tb.split())
    extra = (wa - wb) | (wb - wa)
    extra = {w for w in extra if w not in _STOPWORDS and not w.isdigit()}
    return bool(extra) and extra <= _PARATEXT_WORDS


def _numbers(title: str) -> Set[str]:
    return {w for w in title.split() if w.isdigit() or _ROMAN_RE.match(w)}


def _discordant_numbers(ta: str, tb: str) -> bool:
    """Vrai si les deux titres portent des numéros incompatibles (aucun des
    deux ensembles ne contient l'autre : « tome 1 » / « tome 2 », « partie
    II » / « partie III ») : deux volumes ou deux parties, pas deux graphies
    du même titre. Un titre tronqué garde un sous-ensemble des numéros."""
    na, nb = _numbers(ta), _numbers(tb)
    return bool(na) and bool(nb) and not (na <= nb or nb <= na)


# Marqueurs de tome ou de partie. « Forts » : leur seule présence dans la
# partie propre à l'un des titres désigne un autre volume. Les autres sont
# aussi des mots ordinaires (« le volume sonore », « part-time ») : ils ne
# comptent qu'accolés à un numéro ou à un ordinal.
_PART_MARKERS_STRONG = {"tome", "tomes", "fascicule", "fascicules"}
_PART_MARKERS = {
    "t", "vol", "vols", "volume", "volumes", "partie", "parties", "part",
    "parts", "livre", "livres", "book", "books", "chapitre", "chapter",
    "cahier", "cahiers", "episode", "saison", "season", "section", "fasc",
    "no", "n", "numero",
}
_ORDINAL_WORDS = {
    "premier", "premiere", "deuxieme", "second", "seconde", "troisieme",
    "quatrieme", "cinquieme", "sixieme", "septieme", "huitieme", "neuvieme",
    "dixieme", "dernier", "derniere", "first", "third", "fourth", "fifth",
    "sixth", "seventh", "eighth", "ninth", "tenth", "last",
}
_ORDINAL_RE = re.compile(r"^\d+(er|re|ere|e|eme|nd|rd|th|st)$")
_SEQUEL_WORDS = {"suite", "et", "fin", "la", "le", "de"}
# Numéros de tome ou de partie en chiffres romains (2 à 39, lettres i, v, x
# seulement : « ci » de « celle-ci », « li », « cl » ne sont pas des tomes).
_PART_ROMAN_RE = re.compile(r"^(?=[ivx]{2,}$)x{0,3}(ix|iv|v?i{0,3})$")


def _is_number_word(word: str) -> bool:
    return word.isdigit() or bool(_PART_ROMAN_RE.match(word)) \
        or word in {"i", "v", "x"}


def _is_ordinal(word: str) -> bool:
    return word in _ORDINAL_WORDS or bool(_ORDINAL_RE.match(word))


def _part_marker_difference(ta: str, tb: str) -> bool:
    """
    Vrai si la partie propre à l'un des deux titres normalisés annonce un
    autre tome ou une autre partie : « X » / « X. Tome 2 », « X, volume II »,
    « X : deuxième partie », « X : suite et fin », « X II ». Le cas courant
    du premier volume catalogué sans numéro n'est donc pas fusionné avec le
    deuxième. Un simple chiffre arabe final n'est pas un marqueur (appel de
    note recopié dans le titre : « … Robert Antelme 1 », même article
    Érudit), les numéros discordants relevant de :func:`_discordant_numbers`.
    Une étiquette en tête de titre (« Chapitre 2. X », « Première partie :
    X ») numérote la pièce elle-même et n'est pas un marqueur ; « Tome 2 »
    l'est partout.
    """
    wa, wb = ta.split(), tb.split()
    sa, sb = set(wa), set(wb)
    for words, other in ((wa, sb), (wb, sa)):
        extra_idx = [k for k, w in enumerate(words) if w not in other]
        if not extra_idx:
            continue
        extra = [words[k] for k in extra_idx]
        if any(w in _PART_MARKERS_STRONG for w in extra):
            return True
        if extra_idx[0] == 0 and extra_idx[-1] < 3 \
                and extra_idx == list(range(len(extra_idx))):
            continue  # étiquette de tête (« chapitre 2 », « 2e partie »)
        if "suite" in extra and set(extra) <= _SEQUEL_WORDS:
            return True
        for k in extra_idx:
            w = words[k]
            if _PART_ROMAN_RE.match(w):
                return True
            neighbours = words[max(k - 1, 0):k] + words[k + 1:k + 2]
            if w in _PART_MARKERS and any(
                    _is_number_word(n) or _is_ordinal(n) for n in neighbours):
                return True
            if _is_ordinal(w) and any(n in _PART_MARKERS
                                      or n in _PART_MARKERS_STRONG
                                      for n in neighbours):
                return True
    return False


def _char_profile(feats: "_Features") -> Dict[str, int]:
    if feats.chars is None:
        profile: Dict[str, int] = defaultdict(int)
        for ch in feats.title:
            profile[ch] += 1
        feats.chars = dict(profile)
    return feats.chars


def _common_chars(pa: Dict[str, int], pb: Dict[str, int]) -> int:
    if len(pb) < len(pa):
        pa, pb = pb, pa
    return sum(min(n, pb.get(ch, 0)) for ch, n in pa.items())


def _title_ratio_upper_bound(a: "_Features", b: "_Features") -> float:
    """
    Majorant exact de ``best_title_ratio`` : le rapport de SequenceMatcher
    (2M / T) ne peut dépasser 2 × (caractères communs, multiplicités
    comprises) / T, ni pour la comparaison entière ni pour la comparaison
    du titre court au préfixe du long (même longueur ; le préfixe du long
    n'a pas plus de caractères communs que le long entier). Sert de filtre :
    une paire sous le seuil n'appelle pas SequenceMatcher.
    """
    la, lb = len(a.title), len(b.title)
    if not la or not lb:
        return 0.0
    common = _common_chars(_char_profile(a), _char_profile(b))
    bound = 2.0 * common / (la + lb)
    short = min(la, lb)
    if short != max(la, lb):
        bound = max(bound, min(common, short) / short)
    return bound


def hard_compatible(a: _Features, b: _Features) -> bool:
    """
    Contraintes dures entre deux groupes, indépendantes de la similarité des
    titres : types compatibles, aucune relation partie/tout (le contenant de
    l'une porte le titre de l'autre : chapitre et ouvrage), même statut de
    compte rendu, aucun identifiant contradictoire (DOI, ISBN, arXiv, PMID ;
    identifiants HAL distincts seulement pour un titre identique d'au moins
    quatre mots significatifs et la même année). Vérifiées pour chaque paire
    d'un regroupement flou : une notice de type générique ne peut pas relier
    un livre et un chapitre.
    """
    if a.doi and b.doi and a.doi != b.doi:
        return False
    if (_conflict(a.isbn, b.isbn) or _conflict(a.arxiv, b.arxiv)
            or _conflict(a.pmid, b.pmid)):
        return False
    if not _types_compatible(a.itype, b.itype):
        return False
    if _part_of(a, b) or _part_of(b, a):
        return False
    if a.review != b.review:
        return False
    if _conflict(a.hal, b.hal):
        same_year = a.year is not None and a.year == b.year
        if not (a.title == b.title and same_year
                and a.tclass == "ok" and b.tclass == "ok"):
            return False
    return True


def _tolerant_author(a: _Features, b: _Features) -> Optional[str]:
    """
    Corroboration par le premier auteur saisi autrement : le premier auteur
    de l'une et l'un des auteurs de l'autre ont des profils de nom
    équivalents (:func:`names_equivalent`). Renvoie le motif
    ``clé~clé (nom complet)`` (le plus petit s'il y en a plusieurs, pour ne
    pas dépendre de l'ordre d'itération) ou ``None``.
    """
    found = []
    for first, others in ((a.author_profile, b.profiles),
                          (b.author_profile, a.profiles)):
        if first is None:
            continue
        for other in others:
            if names_equivalent(first, other):
                found.append(f"{first[0]}~{other[0]}")
        if found:
            break
    return f"{min(found)} (nom complet)" if found else None


def _tolerant_candidate(first, others) -> bool:
    """Vrai si ``first`` (profil d'un premier auteur) est équivalent à l'un
    des profils ``others`` (filtre des paires candidates)."""
    return first is not None and any(names_equivalent(first, o)
                                     for o in others)


def _match(a: _Features, b: _Features, approximate: bool = True):
    """
    Appariement de deux groupes sans clé forte commune.

    Returns:
        ``None`` ou ``(exact, ratio, par_préfixe, écart_d_année, auteur)``.
    """
    from utils.doi_resolver import best_title_ratio

    # Ordre canonique de la paire : SequenceMatcher n'est pas strictement
    # symétrique et le motif nomme un auteur ; la décision et le motif ne
    # doivent pas dépendre de l'ordre des arguments (ni donc des entrées).
    if b.order_key() < a.order_key():
        a, b = b, a

    if a.tclass == "blocked" or b.tclass == "blocked":
        return None
    # Corroboration par le premier auteur, au sens du résolveur de DOI : le
    # premier auteur de l'une figure parmi les auteurs de l'autre (listes
    # tronquées ou ordonnées différemment d'une source à l'autre) ; à défaut,
    # il y figure sous un nom saisi autrement (inversion nom / prénom, nom
    # composé : :func:`names_equivalent`).
    if a.author and a.author in b.authors:
        matched = a.author
    elif b.author and b.author in a.authors:
        matched = b.author
    else:
        matched = _tolerant_author(a, b)
        if matched is None:
            return None
    if a.year is None or b.year is None:
        return None
    gap = abs(a.year - b.year)
    if gap > FUZZY_YEAR_TOLERANCE:
        return None
    exact = a.title == b.title
    if exact and (a.tclass == "short" or b.tclass == "short"):
        # Titre court (moins de quatre mots significatifs) : l'égalité du
        # titre prouve peu (« Dialogisme et polyphonie » désigne un chapitre
        # et l'ouvrage collectif) ; même année et même premier auteur exigés,
        # comme l'empreinte exacte historique. Le nom doit être saisi à
        # l'identique : la corroboration par les jetons du nom complet ne
        # s'applique pas à ce régime.
        if gap != 0 or not a.author or a.author != b.author:
            return None
    if not exact and (not approximate or a.tclass != "ok"
                      or b.tclass != "ok"):
        return None
    if not hard_compatible(a, b):
        return None
    if exact:
        return (True, 1.0, False, gap, matched)
    la, lb = len(a.title), len(b.title)
    # Filtres bon marché d'abord. Borne supérieure du rapport de
    # SequenceMatcher (2M / (la + lb)) : inutile de comparer si elle est sous
    # le seuil, sauf appariement par préfixe possible (même début de titre).
    bound = 2.0 * min(la, lb) / max(la + lb, 1)
    if bound < FUZZY_TITLE_THRESHOLD:
        short, long_ = (a.title, b.title) if la <= lb else (b.title, a.title)
        if not long_.startswith(short[:12]):
            return None
    if _title_ratio_upper_bound(a, b) < FUZZY_TITLE_THRESHOLD:
        return None
    if _paratext_only_difference(a.title, b.title):
        return None
    if _discordant_numbers(a.title, b.title):
        return None
    if _part_marker_difference(a.title, b.title):
        return None
    ratio, by_prefix = best_title_ratio(a.raw_title, b.raw_title)
    if ratio < FUZZY_TITLE_THRESHOLD:
        return None
    if by_prefix and gap != 0:
        return None
    return (False, ratio, by_prefix, gap, matched)


def _motif(match) -> str:
    exact, ratio, by_prefix, gap, matched = match
    if exact:
        motif = "titre identique"
    else:
        motif = f"titre {ratio:.2f}" + (" préfixe" if by_prefix else "")
    motif += " année exacte" if gap == 0 else f" année ±{gap}"
    return motif + f" auteur {matched}"


def fuzzy_match(a: _Features, b: _Features,
                approximate: bool = True) -> Optional[str]:
    """
    Appariement de deux groupes sans clé forte commune ; renvoie le motif
    (``titre 0.95 année exacte auteur x``) ou ``None``.

    Deux régimes : égalité exacte du titre normalisé (seul régime des titres
    courts, qui exige en plus la même année ; seul régime actif quand la
    passe floue est désactivée, comme l'empreinte exacte historique) ; et
    similarité approchée ``best_title_ratio`` ≥ ``FUZZY_TITLE_THRESHOLD``
    (``approximate=True``, titres d'au moins
    ``FUZZY_MIN_SIGNIFICANT_WORDS`` mots significatifs, qui ne diffèrent pas
    seulement par des mots de paratexte). Les deux régimes exigent la
    corroboration par l'année et par le premier auteur, et les contraintes
    dures de :func:`hard_compatible`. Le résultat ne dépend pas de l'ordre
    des arguments.
    """
    m = _match(a, b, approximate)
    return _motif(m) if m else None


# === Déduplication ===

def fuzzy_enabled(fuzzy: Optional[bool] = None) -> bool:
    """Passe floue active ? Paramètre explicite, sinon ``MOISSON_DEDUP_FUZZY``
    (``0``, ``false``, ``no``, ``off`` la désactivent), sinon active."""
    if fuzzy is not None:
        return bool(fuzzy)
    raw = os.environ.get(FUZZY_ENV, "").strip().lower()
    return raw not in ("0", "false", "no", "off", "non")


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def _components(n: int, edges: Iterable[Tuple[int, int]]) -> List[List[int]]:
    uf = _UnionFind(n)
    for a, b in edges:
        uf.union(a, b)
    comps: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        comps[uf.find(i)].append(i)
    return sorted(comps.values())


def _split_by_doi(nodes: List[int], dois: Dict[int, Optional[str]],
                  adjacency: Dict[int, Set[int]]):
    """
    Scinde un composant selon les DOI (règle de l'en-tête du module).

    Les nœuds de même DOI forment une classe ; les nœuds sans DOI sont
    regroupés par leurs liens mutuels ; un sous-composant sans DOI rejoint
    la classe DOI à laquelle il est relié si elle est unique, et reste à part
    s'il est relié à plusieurs DOI. Les liens de ``adjacency`` peuvent sortir
    du composant (ambiguïté héritée d'une étape précédente) : ils comptent
    pour décider de l'ambiguïté.

    Returns:
        ``(groupes, scindé)`` : ``groupes`` est une liste de
        ``(membres, dois_reliés_si_ambigu)`` (liste vide sauf pour un
        sous-composant sans DOI relié à plusieurs DOI) ; ``scindé`` indique
        que le composant portait plusieurs DOI.
    """
    by_doi: Dict[str, List[int]] = defaultdict(list)
    no_doi: List[int] = []
    for n in nodes:
        if dois.get(n):
            by_doi[dois[n]].append(n)
        else:
            no_doi.append(n)

    index = {n: i for i, n in enumerate(no_doi)}
    edges = [(index[a], index[b]) for a in no_doi
             for b in adjacency.get(a, ()) if b in index and a < b]
    groups = {d: list(members) for d, members in by_doi.items()}
    out = []
    for sub in _components(len(no_doi), edges):
        members = [no_doi[i] for i in sub]
        linked = sorted({dois[b] for a in members for b in adjacency.get(a, ())
                         if dois.get(b)})
        if len(linked) == 1 and linked[0] in groups:
            groups[linked[0]].extend(members)
        elif len(linked) >= 2:
            out.append((sorted(members), linked))
        else:
            out.append((sorted(members), []))
    out.extend((sorted(m), []) for m in groups.values())
    return out, len(by_doi) > 1


def _strong_groups(items: List[Dict]):
    """
    Étape 1 : groupes par clés fortes avec garde-fou DOI.

    Toute clé forte partagée relie les notices, avec trois restrictions :
    une clé ``url:`` partagée par plus de ``URL_KEY_MAX_SHARE`` titres
    distincts est ignorée, et elle ne relie que des notices aux titres
    concordants ; une clé ``hal:`` ne relie pas deux notices dont les années
    diffèrent de plus de ``HAL_KEY_MAX_YEAR_GAP`` (versions distinctes d'un
    même dépôt, liens tracés dans ``retenus``).

    Returns:
        ``(groupes, ambiguïtés, dois, composants_scindés, retenus)`` ;
        ``groupes`` est une liste de listes d'indices ; ``ambiguïtés``
        associe le groupe aux DOI entre lesquels il est resté à part ;
        ``retenus`` liste les paires ``(i, j, clé)`` non reliées.
    """
    n = len(items)
    dois: Dict[int, Optional[str]] = {}
    by_key: Dict[str, List[int]] = defaultdict(list)
    for i, it in enumerate(items):
        dois[i] = None
        for key, _derived in strong_key_sources(it):
            if key.startswith("doi:"):
                # La clé doi: est le DOI d'identité (identity_doi) : elle
                # porte la classe DOI, pas un lien ordinaire.
                dois[i] = key[4:]
                continue
            by_key[key].append(i)
    doi_members: Dict[str, List[int]] = defaultdict(list)
    for i, d in dois.items():
        if d:
            doi_members[d].append(i)

    titles: Dict[int, str] = {}
    years: Dict[int, Optional[int]] = {}

    def title_of(i: int) -> str:
        if i not in titles:
            titles[i] = _norm_title(items[i].get("title", "") or "")
        return titles[i]

    def year_of(i: int) -> Optional[int]:
        if i not in years:
            y = extract_year(items[i].get("date", "") or "")
            years[i] = int(y) if y else None
        return years[i]

    adjacency: Dict[int, Set[int]] = defaultdict(set)
    edges: List[Tuple[int, int]] = []
    withheld: List[Tuple[int, int, str]] = []

    def link_all(members: List[int]) -> None:
        for a in members:
            adjacency[a].update(m for m in members if m != a)
        edges.extend((members[0], m) for m in members[1:])

    def link_pairs(pairs: Iterable[Tuple[int, int]]) -> None:
        for a, b in pairs:
            adjacency[a].add(b)
            adjacency[b].add(a)
            edges.append((a, b))

    for key, members in by_key.items():
        if len(members) < 2:
            continue
        if key.startswith("url:"):
            # URL générique : trop de titres distincts la partagent.
            if len({title_of(m) for m in members}) > URL_KEY_MAX_SHARE:
                continue
            # Une URL de page de revue non reconnue comme générique ne doit
            # pas réunir deux articles : le lien exige des titres concordants.
            link_pairs((a, b) for x, a in enumerate(members)
                       for b in members[x + 1:]
                       if _titles_corroborate(items[a], items[b],
                                              title_of(a), title_of(b)))
        elif key.startswith("hal:"):
            # Même dépôt HAL (version retirée), années éloignées : deux
            # versions distinctes (prépublication puis chapitre,
            # republication), jamais réunies par la seule clé HAL.
            pairs = []
            for x, a in enumerate(members):
                for b in members[x + 1:]:
                    ya, yb = year_of(a), year_of(b)
                    if ya is not None and yb is not None \
                            and abs(ya - yb) > HAL_KEY_MAX_YEAR_GAP:
                        withheld.append((a, b, key))
                    else:
                        pairs.append((a, b))
            link_pairs(pairs)
        else:
            link_all(members)
    for members in doi_members.values():
        link_all(members)

    groups: List[List[int]] = []
    ambiguous: Dict[Tuple[int, ...], List[str]] = {}
    n_split = 0
    for comp in _components(n, edges):
        parts, split = _split_by_doi(comp, dois, adjacency)
        n_split += int(split)
        for members, linked in parts:
            if linked:
                ambiguous[tuple(members)] = linked
            groups.append(members)
    groups.sort()
    return groups, ambiguous, dois, n_split, withheld


def _titles_corroborate(a: Dict, b: Dict, ta: str, tb: str) -> bool:
    """Titres concordants pour un lien reposant sur une URL : égaux une fois
    normalisés, ou similaires au sens de la passe floue (préfixe admis)."""
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    from utils.doi_resolver import best_title_ratio
    ratio, _by_prefix = best_title_ratio(a.get("title", "") or "",
                                         b.get("title", "") or "")
    return ratio >= FUZZY_TITLE_THRESHOLD


def _group_doi(members: List[int], dois: Dict[int, Optional[str]]):
    found = {dois[m] for m in members if dois.get(m)}
    return next(iter(found)) if len(found) == 1 else None


def deduplicate_with_report(items: List[Dict],
                            fuzzy: Optional[bool] = None,
                            with_groups: bool = False):
    """
    Déduplique et renvoie ``(notices, rapport)``.

    Le rapport compte : ``input``, ``output``, ``merged_groups`` (notices de
    sortie issues d'une fusion), ``strong_merges`` (notices absorbées par les
    clés fortes), ``fuzzy_merges`` (groupes absorbés par la passe floue),
    ``doi_split_components`` (composants scindés pour DOI contradictoires),
    ``ambiguous`` (notices laissées à part entre plusieurs DOI),
    ``version_splits`` (notices de sortie tenues à part d'une autre version
    du même dépôt HAL), ``fuzzy`` (passe active ou non). Avec
    ``with_groups=True``, ``groups`` donne pour chaque notice de sortie (même
    ordre) les indices des notices d'entrée qu'elle réunit (liste vide pour
    une entrée vide).
    """
    items = list(items or [])
    report = {"input": len(items), "output": 0, "merged_groups": 0,
              "strong_merges": 0, "fuzzy_merges": 0,
              "doi_split_components": 0, "ambiguous": 0,
              "version_splits": 0, "fuzzy": fuzzy_enabled(fuzzy)}
    if not items:
        if with_groups:
            report["groups"] = []
        return [], report

    groups, ambiguous, dois, n_split, withheld = _strong_groups(items)
    report["strong_merges"] = len(items) - len(groups)
    report["doi_split_components"] = n_split

    # Étape 2 : appariement sans clé commune. Passe floue désactivée :
    # égalité exacte seule (titre normalisé, année, premier auteur), comme
    # l'empreinte exacte historique.
    groups, motifs, ambiguous, n_fuzzy = _fuzzy_rounds(
        items, groups, ambiguous, dois, approximate=report["fuzzy"])
    report["fuzzy_merges"] = n_fuzzy

    # Versions distinctes d'un même dépôt HAL restées dans des notices
    # différentes : statut tracé de part et d'autre.
    owner = {i: gi for gi, members in enumerate(groups) for i in members}
    version_keys: Dict[int, Set[str]] = defaultdict(set)
    for a, b, key in withheld:
        if owner[a] != owner[b]:
            version_keys[owner[a]].add(key)
            version_keys[owner[b]].add(key)

    out: List[Tuple[Dict, List[int]]] = []
    for gi, members in enumerate(groups):
        key = tuple(members)
        member_items = [items[i] for i in members]
        evidence = None
        linked = ambiguous.get(key)
        held = sorted(version_keys.get(gi, ()), key=_key_order)
        if len(members) > 1 or linked or held:
            evidence = {"keys": _shared_keys(member_items),
                        "fuzzy": motifs.get(key, [])}
            notes = []
            if linked:
                notes.append(
                    "non fusionné, clés partagées avec des DOI distincts : "
                    + ", ".join(f"doi:{d}" for d in linked[:4]))
                report["ambiguous"] += len(members)
            if held:
                notes.append(
                    "version distincte non fusionnée (années éloignées), "
                    "clé partagée : " + ", ".join(held[:4]))
                report["version_splits"] += 1
            if notes:
                evidence["ambiguous"] = " + ".join(notes)
        if len(members) > 1:
            report["merged_groups"] += 1
        out.append((merge_items(member_items, evidence), members))

    out.sort(key=lambda pair: _output_sort_key(pair[0]))
    records = [record for record, _members in out]
    report["output"] = len(records)
    if with_groups:
        report["groups"] = [list(members) for _record, members in out]
    logger.info(
        f"Déduplication : {len(items)} items → {len(records)} items uniques "
        f"({len(items) - len(records)} doublons fusionnés ; "
        f"floue {report['fuzzy_merges']} ; ambigus {report['ambiguous']})"
    )
    return records, report


def _shared_keys(member_items: List[Dict]) -> List[str]:
    counts: Dict[str, int] = defaultdict(int)
    for it in member_items:
        for key in {k for k, _ in strong_key_sources(it)}:
            counts[key] += 1
    return sorted((k for k, c in counts.items() if c >= 2), key=_key_order)


def _fuzzy_edges(feats: List["_Features"], approximate: bool = True,
                 ids: Optional[List[Tuple[int, ...]]] = None,
                 cache: Optional[Dict[Tuple, Any]] = None):
    """
    Appariements sans clé commune entre groupes.

    Paires candidates : une notice et les notices qui comptent son premier
    auteur parmi leurs auteurs (même nom normalisé, ou profil de nom
    équivalent au sens de :func:`names_equivalent`), à au plus
    ``FUZZY_YEAR_TOLERANCE`` an d'écart. C'est exactement l'ensemble des
    paires que la corroboration de :func:`_match` peut accepter (le premier
    auteur de l'une figure parmi les auteurs de l'autre) : un co-auteur de
    nom fréquent ne multiplie plus les comparaisons. ``ids`` (membres de
    chaque groupe) et ``cache`` permettent de ne pas recomparer, d'un tour à
    l'autre, deux groupes inchangés (le résultat ne dépend que de leurs
    notices).

    Returns:
        ``{(ga, gb): match}`` (voir :func:`_match`).
    """
    from bisect import bisect_left, bisect_right

    eligible = [gi for gi, f in enumerate(feats)
                if f.authors and f.year is not None and f.tclass != "blocked"]
    holders: Dict[str, List[int]] = defaultdict(list)
    for gi in eligible:
        for name in feats[gi].authors:
            holders[name].append(gi)
    holder_years: Dict[str, List[int]] = {}
    for name, members in holders.items():
        members.sort(key=lambda g: (feats[g].year, g))
        holder_years[name] = [feats[g].year for g in members]

    edges: Dict[Tuple[int, int], Tuple] = {}
    seen: Set[Tuple[int, int]] = set()
    use_cache = cache is not None and ids is not None
    for ga in eligible:
        fa = feats[ga]
        members = holders.get(fa.author) if fa.author else None
        if not members or len(members) < 2:
            continue
        years = holder_years[fa.author]
        lo = bisect_left(years, fa.year - FUZZY_YEAR_TOLERANCE)
        hi = bisect_right(years, fa.year + FUZZY_YEAR_TOLERANCE)
        for gb in members[lo:hi]:
            if gb == ga:
                continue
            pair = (ga, gb) if ga < gb else (gb, ga)
            if pair in seen:
                continue
            seen.add(pair)
            if use_cache:
                ia, ib = ids[pair[0]], ids[pair[1]]
                ckey = (ia, ib) if ia < ib else (ib, ia)
                if ckey in cache:
                    m = cache[ckey]
                else:
                    m = _match(feats[pair[0]], feats[pair[1]], approximate)
                    cache[ckey] = m
            else:
                m = _match(feats[pair[0]], feats[pair[1]], approximate)
            if m:
                edges[pair] = m

    # Premier auteur saisi autrement (names_equivalent). L'équivalence exige
    # que les jetons (nom et prénom réunis) de l'un soient tous dans l'autre,
    # au nombre d'au moins deux : un groupe est indexé sous chaque
    # sous-ensemble d'au moins deux jetons de chacun de ses auteurs, et
    # l'index est interrogé avec chaque sous-ensemble d'au moins deux jetons
    # du premier auteur (les deux inclusions sont couvertes : jetons du
    # premier auteur contenus dans ceux d'un auteur de l'autre, ou
    # l'inverse). Seules les paires qui passent le filtre d'équivalence sont
    # comparées. Les paires déjà vues par la boucle précédente (même nom
    # normalisé) ne sont pas recomparées : _match y a déjà appliqué les deux
    # corroborations.
    subset_holders: Dict[frozenset, Set[int]] = defaultdict(set)
    exact_holders: Dict[frozenset, Set[int]] = defaultdict(set)
    for gi in eligible:
        for prof in feats[gi].profiles:
            full = prof[1] | prof[2]
            exact_holders[full].add(gi)
            for sub in _token_subsets(full):
                subset_holders[sub].add(gi)

    def by_year(index: Dict[frozenset, Set[int]]):
        out = {}
        for key, members in index.items():
            ordered = sorted(members, key=lambda g: (feats[g].year, g))
            out[key] = (ordered, [feats[g].year for g in ordered])
        return out

    subset_sorted, exact_sorted = by_year(subset_holders), by_year(exact_holders)
    for ga in eligible:
        fa = feats[ga]
        prof = fa.author_profile
        if prof is None:
            continue
        full = prof[1] | prof[2]
        # Auteurs dont les jetons contiennent ceux du premier auteur, puis
        # auteurs dont les jetons sont contenus dans les siens ; fenêtre
        # d'années par bissection.
        lists = [subset_sorted.get(full)]
        lists.extend(exact_sorted.get(sub) for sub in _token_subsets(full)
                     if sub != full)
        for entry in lists:
            if not entry:
                continue
            members, years = entry
            lo = bisect_left(years, fa.year - FUZZY_YEAR_TOLERANCE)
            hi = bisect_right(years, fa.year + FUZZY_YEAR_TOLERANCE)
            for gb in members[lo:hi]:
                if gb == ga:
                    continue
                pair = (ga, gb) if ga < gb else (gb, ga)
                if pair in seen:
                    continue
                if not _tolerant_candidate(prof, feats[gb].profiles):
                    continue
                seen.add(pair)
                if use_cache:
                    ia, ib = ids[pair[0]], ids[pair[1]]
                    ckey = (ia, ib) if ia < ib else (ib, ia)
                    if ckey in cache:
                        m = cache[ckey]
                    else:
                        m = _match(feats[pair[0]], feats[pair[1]], approximate)
                        cache[ckey] = m
                else:
                    m = _match(feats[pair[0]], feats[pair[1]], approximate)
                if m:
                    edges[pair] = m
    return edges


@lru_cache(maxsize=65536)
def _token_subsets(tokens: frozenset) -> Tuple[frozenset, ...]:
    """Sous-ensembles d'au moins deux jetons d'un nom (au plus
    ``NAME_MAX_TOKENS`` jetons : 57 sous-ensembles au pire)."""
    from itertools import combinations
    items = sorted(tokens)
    return tuple(frozenset(c) for k in range(2, len(items) + 1)
                 for c in combinations(items, k))


def _constrained_merge(parts: List[int], edges: Dict[Tuple[int, int], Tuple],
                       feats: List["_Features"], sort_keys: List[Tuple]):
    """
    Regroupe les groupes ``parts`` reliés par ``edges`` sans jamais réunir
    deux groupes incompatibles (:func:`hard_compatible`), même par
    transitivité : agglomération par liens décroissants (titre identique,
    puis similarité, puis clés de contenu), chaque union n'étant faite que si
    toutes les paires du regroupement obtenu restent compatibles. L'ordre des
    liens ne dépend que du contenu des notices.

    Returns:
        Liste de sous-listes de ``parts``.
    """
    part_set = set(parts)
    local = [(pair, m) for pair, m in edges.items()
             if pair[0] in part_set and pair[1] in part_set]
    clusters = {g: [g] for g in parts}
    owner = {g: g for g in parts}
    # Chemin rapide : toutes les paires compatibles.
    if all(hard_compatible(feats[x], feats[y])
           for i, x in enumerate(parts) for y in parts[i + 1:]):
        return [sorted(parts)]

    def edge_order(item):
        (ga, gb), (exact, ratio, by_prefix, gap, _author) = item
        ka, kb = sorted((sort_keys[ga], sort_keys[gb]))
        return (not exact, -ratio, by_prefix, gap, ka, kb)

    for (ga, gb), _m in sorted(local, key=edge_order):
        ca, cb = owner[ga], owner[gb]
        if ca == cb:
            continue
        if all(hard_compatible(feats[x], feats[y])
               for x in clusters[ca] for y in clusters[cb]):
            keep, drop = min(ca, cb), max(ca, cb)
            clusters[keep].extend(clusters.pop(drop))
            for g in clusters[keep]:
                owner[g] = keep
    return [sorted(c) for c in clusters.values()]


def _fuzzy_rounds(items: List[Dict], groups: List[List[int]],
                  ambiguous: Dict[Tuple[int, ...], List[str]],
                  dois: Dict[int, Optional[str]], approximate: bool = True):
    """
    Étape 2 : appariement sans clé commune, itéré jusqu'à stabilité.

    Chaque tour compare les notices fusionnées des groupes courants, applique
    la règle de scission DOI aux appariements obtenus (un groupe resté ambigu
    à l'étape 1 garde ses liens vers les DOI qui l'encadrent), puis, dans
    chaque regroupement proposé, les contraintes dures paire à paire
    (:func:`_constrained_merge`).
    """
    motifs: Dict[Tuple[int, ...], List[str]] = {}
    total = 0
    # Caches d'un tour à l'autre, indexés par les membres d'un groupe : la
    # notice fusionnée (donc ses caractéristiques et l'appariement de deux
    # groupes) ne dépend que de ces membres.
    group_cache: Dict[Tuple[int, ...], Tuple[Dict, "_Features"]] = {}
    match_cache: Dict[Tuple, Any] = {}
    dump_cache: Dict[Tuple[int, ...], str] = {}
    for _round in range(FUZZY_MAX_ROUNDS):
        ids = [tuple(g) for g in groups]
        for gid in ids:
            if gid not in group_cache:
                record = merge_items([items[i] for i in gid])
                group_cache[gid] = (record, _Features(record))
        records = [group_cache[gid][0] for gid in ids]
        feats = [group_cache[gid][1] for gid in ids]
        edges = _fuzzy_edges(feats, approximate, ids, match_cache)
        if not edges:
            break
        involved = {g for pair in edges for g in pair}
        sort_keys: List[Optional[Tuple]] = [None] * len(ids)
        for gi in involved:
            gid = ids[gi]
            if gid not in dump_cache:
                dump_cache[gid] = _canonical_dump(records[gi])
            sort_keys[gi] = feats[gi].order_key() + (dump_cache[gid],)
        g_doi = {gi: _group_doi(g, dois) for gi, g in enumerate(groups)}
        doi_group = {d: gi for gi, d in g_doi.items() if d}

        adjacency: Dict[int, Set[int]] = defaultdict(set)
        for a, b in edges:
            adjacency[a].add(b)
            adjacency[b].add(a)
        for gi, g in enumerate(groups):
            for d in ambiguous.get(tuple(g), []):
                if d in doi_group:
                    adjacency[gi].add(doi_group[d])

        new_groups: List[List[int]] = []
        new_amb: Dict[Tuple[int, ...], List[str]] = {}
        new_motifs: Dict[Tuple[int, ...], List[str]] = {}
        for comp in _components(len(groups), edges.keys()):
            proposed, _split = _split_by_doi(comp, g_doi, adjacency)
            for parts, linked in proposed:
                for sub in _constrained_merge(parts, edges, feats, sort_keys):
                    members = sorted(i for gi in sub for i in groups[gi])
                    key = tuple(members)
                    sub_set = set(sub)
                    found: List[str] = []
                    for gi in sub:
                        found.extend(motifs.get(tuple(groups[gi]), []))
                    found.extend(_motif(m) for (a, b), m in edges.items()
                                 if a in sub_set and b in sub_set)
                    if found:
                        new_motifs[key] = sorted(set(found))
                    if linked:
                        new_amb[key] = linked
                    new_groups.append(members)
        new_groups.sort()
        merged_now = len(groups) - len(new_groups)
        total += merged_now
        groups, motifs, ambiguous = new_groups, new_motifs, new_amb
        if merged_now == 0:
            break
    return groups, motifs, ambiguous, total


def _output_sort_key(item: Dict) -> Tuple:
    author = first_author_key(item)
    year = extract_year(item.get("date", "") or "") or "9999"
    return (0 if author else 1, author, year, _norm_title(item.get("title", "")),
            tuple(k for k, _ in strong_key_sources(item)),
            _canonical_dump(item))


def deduplicate(items: List[Dict], fuzzy: Optional[bool] = None) -> List[Dict]:
    """
    Déduplique une liste d'items Zotero issue de sources multiples.

    Clés fortes (union-find avec garde-fou DOI) puis passe floue
    conservatrice (désactivable : ``fuzzy=False`` ou
    ``MOISSON_DEDUP_FUZZY=0``). Sortie en ordre bibliographique
    auteur-date, indépendante de l'ordre d'entrée.

    Args:
        items: Liste d'items à dédupliquer.
        fuzzy: Passe floue (``None`` : variable d'environnement, active par
            défaut).

    Returns:
        Liste dédupliquée et fusionnée.
    """
    result, _report = deduplicate_with_report(items, fuzzy=fuzzy)
    return result
