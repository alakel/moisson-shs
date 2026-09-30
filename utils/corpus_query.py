"""
Consultation d'un corpus sans saturer le contexte de l'agent (OPT-17,
constats C05 et C29).

Deux usages, une même lecture des notices :

1. **Digest** des outils producteurs (``inline="digest"``, défaut en stdio).
   Au lieu du corpus Zotero complet (jusqu'à 300 notices, ≈ 391 Ko, de
   l'ordre de 100 000 tokens), le retour donne les ``top`` premières notices
   (40 par défaut) dans l'ordre du classement :

   - classement du moteur (``native`` par défaut, ``local`` s'il est
     activé), pour une moisson ;
   - classement hérité du corpus source (rangs du manifeste), pour un
     corpus dérivé (filtre, résolution DOI, conversion, export) ;
   - à défaut, ordre du corpus (fusion, recherche par auteur).

   Par notice : rang, score (du classement ; absent sans classement),
   titre, trois auteurs (« et al. »), année, support, type, DOI, citations
   si connues, sources (avec leur rang natif), termes de la requête
   appariés et absents (même normalisation que le classement local,
   ``utils.ranking.match_notice``), réserves (justification du classement)
   et identifiant stable (clé forte principale, ``utils.ranking.record_id``,
   utilisable par ``corpus_query(ids=…)``). Le texte compact
   (``DigestEntry.line``, une ligne par notice, notation expliquée par
   ``DIGEST_LEGEND``) porte le digest complet ; les entrées machine
   (``DigestEntry.data``, pour ``structuredContent.top_items``) en sont les
   poignées : rang, score, identifiant (et clé de citation d'un export).
   Quarante notices complètes en double (texte et JSON) ne tiendraient pas
   sous le plafond de 15 Ko. Le plafond en octets du retour est appliqué par
   l'appelant (``mcp_server._fit_digest``) : les dernières notices passent
   d'abord en ligne courte (``DigestEntry.short`` : rang, titre, année,
   premier auteur, identifiant ; au moins ``DIGEST_MIN_FULL`` restent
   complètes), puis sont retirées tant qu'il est dépassé.

2. **corpus_query** : recherche locale classée dans un corpus Zotero
   interne (``query_corpus``), filtres (année, type, langue, source, DOI ou
   résumé présents), projection de champs, pagination stable (ordre total :
   la clé de tri finit toujours par la position dans le fichier, donc ni
   trou ni doublon d'une page à l'autre tant que le fichier ne change pas,
   ce que ``snapshot`` permet de vérifier) et récupération de notices
   complètes par identifiants (au plus ``QUERY_MAX_IDS`` ; seul chemin qui
   renvoie les résumés entiers). Lecture en flux (``iter_json_array``),
   jamais de ``json.loads`` du fichier entier ; les identifiants et les
   rangs sont repris du manifeste quand il décrit le fichier tel qu'il est
   (taille égale), ce qui évite de recalculer les clés fortes et permet de
   s'arrêter dès la dernière notice demandée.

Aucune donnée de classement n'entre dans une notice (invariant 1) : rangs,
scores, termes et réserves sont rendus à côté (``rank``, ``score``…), jamais
dans le JSON Zotero renvoyé.

Constantes surchargeables par variable d'environnement :

| Constante | Variable | Défaut | Rôle |
|---|---|---|---|
| ``DEFAULT_INLINE`` | ``MOISSON_INLINE_DEFAULT`` | ``digest`` | mode de retour des producteurs en stdio (``digest``, ``full``, ``none``) |
| ``DIGEST_DEFAULT_TOP`` | ``MOISSON_DIGEST_TOP`` | 40 | notices du digest |
| ``DIGEST_MAX_TOP`` | ``MOISSON_DIGEST_MAX_TOP`` | 100 | borne de ``top`` |
| ``DIGEST_MAX_BYTES`` | ``MOISSON_DIGEST_MAX_BYTES`` | 15 000 | plafond du retour complet en mode digest (texte, liens et ``structuredContent``) |
| ``QUERY_DEFAULT_LIMIT`` | ``MOISSON_QUERY_LIMIT`` | 20 | notices par page de ``corpus_query`` |
| ``QUERY_MAX_LIMIT`` | ``MOISSON_QUERY_MAX_LIMIT`` | 100 | borne de ``limit`` |
| ``QUERY_MAX_BYTES`` | ``MOISSON_QUERY_MAX_BYTES`` | 100 000 | plafond du retour de ``corpus_query`` en stdio |
| ``QUERY_HTTP_KEEP_SELECTIONS`` | ``MOISSON_QUERY_HTTP_KEEP`` | 10 | sélections HTTP de ``corpus_query`` gardées par corpus source |
| ``RESOURCE_PAGE_DEFAULT`` | ``MOISSON_RESOURCE_PAGE`` | 50 | notices par page de ``corpus://<nom>?offset=&limit=`` |
| ``RESOURCE_PAGE_MAX`` | ``MOISSON_RESOURCE_PAGE_MAX`` | 200 | borne de ``limit`` d'une page de ressource |
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from unidecode import unidecode

from utils.cleaner import extract_year
from utils.identity import extra_values, extract_doi, normalize_doi, strong_keys
from utils.output_files import iter_json_array
from utils.source_labels import canonical_source_key, display_name

logger = logging.getLogger("moisson.corpus_query")


# === Réglages ===

def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Entier d'une variable d'environnement (défaut si absente ou invalide)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} invalide : valeur par défaut {default}")
        return default
    return max(minimum, value)


INLINE_DIGEST = "digest"
INLINE_FULL = "full"
INLINE_NONE = "none"
INLINE_MODES = (INLINE_DIGEST, INLINE_FULL, INLINE_NONE)


def _default_inline() -> str:
    raw = os.environ.get("MOISSON_INLINE_DEFAULT", "").strip().lower()
    if not raw:
        return INLINE_DIGEST
    if raw in INLINE_MODES:
        return raw
    logger.warning(f"MOISSON_INLINE_DEFAULT={raw!r} inconnu : '{INLINE_DIGEST}' "
                   f"appliqué (attendu : {', '.join(INLINE_MODES)})")
    return INLINE_DIGEST


# Mode de retour par défaut des producteurs en stdio (décision du §10 du plan :
# digest, top 40, au plus 15 Ko ; « full » restitue le sprint 05).
DEFAULT_INLINE = _default_inline()
# Notices du digest (argument ``top``) et sa borne (valeur plus grande ramenée).
DIGEST_DEFAULT_TOP = _env_int("MOISSON_DIGEST_TOP", 40)
DIGEST_MAX_TOP = max(DIGEST_DEFAULT_TOP, _env_int("MOISSON_DIGEST_MAX_TOP", 100))
# Plafond en octets UTF-8 du retour complet en mode digest (sérialisation JSON
# de chaque bloc et du structuredContent, comme la mesure du banc d'OPT-00).
DIGEST_MAX_BYTES = _env_int("MOISSON_DIGEST_MAX_BYTES", 15_000, minimum=2_000)
# Longueurs d'affichage d'une notice du digest (caractères).
DIGEST_TITLE_CHARS = 76
DIGEST_VENUE_CHARS = 30
DIGEST_AUTHORS = 3

# corpus_query : pagination, identifiants, plafond du retour.
QUERY_DEFAULT_LIMIT = _env_int("MOISSON_QUERY_LIMIT", 20)
QUERY_MAX_LIMIT = max(QUERY_DEFAULT_LIMIT, _env_int("MOISSON_QUERY_MAX_LIMIT", 100))
QUERY_MAX_IDS = 20
QUERY_MAX_BYTES = _env_int("MOISSON_QUERY_MAX_BYTES", 100_000, minimum=10_000)
# Sélections publiées par corpus_query en HTTP (``<radical>_sel_<uuid>``) :
# nombre gardé par corpus source, les plus récentes ; les plus anciennes
# sont supprimées à chaque nouvelle sélection (un appel en lecture seule
# n'accumule pas de fichiers sans borne dans le répertoire des corpus).
QUERY_HTTP_KEEP_SELECTIONS = _env_int("MOISSON_QUERY_HTTP_KEEP", 10)
# Résumé tronqué dans une page de résultats (entier : seulement par ``ids``).
QUERY_ABSTRACT_CHARS = 300

# Pages de ressource ``corpus://<nom>?offset=&limit=``.
RESOURCE_PAGE_DEFAULT = _env_int("MOISSON_RESOURCE_PAGE", 50)
RESOURCE_PAGE_MAX = max(RESOURCE_PAGE_DEFAULT,
                        _env_int("MOISSON_RESOURCE_PAGE_MAX", 200))


class QueryError(ValueError):
    """Paramètre de consultation refusé (message sans chemin disque)."""


# Message commun aux outils qui exigent un tableau de notices
# (``mcp_server._load_zotero_input``, corpus_query).
NOT_NOTICES_MESSAGE = "Le fichier ne contient pas un tableau d'items Zotero."


class NotNoticesError(ValueError):
    """Élément du tableau qui n'est pas une notice (objet JSON) : nombre,
    chaîne, null… (message ``NOT_NOTICES_MESSAGE``, sans chemin disque)."""


class CorpusChangedError(RuntimeError):
    """Corpus remplacé ou modifié pendant la lecture, à chacune des
    ``QUERY_READ_ATTEMPTS`` tentatives : aucune page cohérente à rendre."""


def resolve_inline(value: Any) -> str:
    """Mode de retour d'un producteur : None ⇒ ``DEFAULT_INLINE``.

    Raises:
        QueryError: valeur inconnue.
    """
    if value is None:
        return DEFAULT_INLINE
    mode = str(value).strip().lower() if isinstance(value, str) else None
    if mode not in INLINE_MODES:
        raise QueryError(f"inline {value!r} inconnu (attendu : "
                         f"{', '.join(INLINE_MODES)})")
    return mode


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value != int(value):
        raise QueryError(f"{name} = {value!r} invalide (entier attendu)")
    return int(value)


def resolve_top(value: Any) -> int:
    """Notices du digest : None ⇒ ``DIGEST_DEFAULT_TOP`` ; ramené à
    ``DIGEST_MAX_TOP`` au plus.

    Raises:
        QueryError: valeur non entière ou inférieure à 1.
    """
    if value is None:
        return DIGEST_DEFAULT_TOP
    top = _positive_int("top", value)
    if top < 1:
        raise QueryError(f"top = {value!r} invalide (entier >= 1 attendu ; "
                         f"inline='none' pour aucune notice)")
    return min(top, DIGEST_MAX_TOP)


# === Lecture d'une notice ===

_TYPE_LABELS = {
    "journalArticle": "article",
    "magazineArticle": "article de magazine",
    "newspaperArticle": "article de presse",
    "book": "livre",
    "bookSection": "chapitre",
    "thesis": "thèse",
    "conferencePaper": "communication",
    "report": "rapport",
    "preprint": "prépublication",
    "webpage": "page web",
    "blogPost": "billet",
    "dataset": "données",
    "document": "document",
    "presentation": "présentation",
    "manuscript": "manuscrit",
    "encyclopediaArticle": "article d'encyclopédie",
    "dictionaryEntry": "entrée de dictionnaire",
    "letter": "lettre",
    "interview": "entretien",
    "videoRecording": "vidéo",
    "audioRecording": "audio",
    "podcast": "podcast",
    "map": "carte",
    "computerProgram": "logiciel",
}

_VENUE_FIELDS = (
    "publicationTitle", "bookTitle", "proceedingsTitle", "encyclopediaTitle",
    "dictionaryTitle", "websiteTitle", "blogTitle", "forumTitle",
    "university", "institution", "repository", "publisher", "conferenceName",
)

_AUTHOR_ROLES = ("author", "contributor")


def type_label(item_type: Optional[str]) -> str:
    """Libellé court (français) d'un type Zotero ; le type brut sinon."""
    if not item_type:
        return "type inconnu"
    return _TYPE_LABELS.get(item_type, item_type)


def _short(text: Any, limit: int) -> str:
    """Texte sur une ligne, tronqué au dernier mot entier (« … »)."""
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    cut = value[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-–—") + "…"


_SUBTITLE_RE = re.compile(r"\s[:—–-]\s|[.?!]\s")


def short_title(title: str, limit: int) -> str:
    """
    Titre borné à ``limit`` caractères : coupé de préférence avant le
    sous-titre (« : », « — », « . »…) s'il reste au moins 25 caractères,
    sinon au dernier mot entier ; « … » marque la coupe.
    """
    value = " ".join(str(title or "").split())
    if len(value) <= limit:
        return value
    cut = None
    for m in _SUBTITLE_RE.finditer(value):
        if 25 <= m.start() <= limit - 1:
            cut = m.start()
    if cut is not None:
        return value[:cut].rstrip(" ,;:.-–—") + "…"
    return _short(value, limit)


def notice_title(item: Dict[str, Any]) -> str:
    return " ".join(str(item.get("title") or "").split()) or "(sans titre)"


def notice_year(item: Dict[str, Any]) -> Optional[int]:
    year = extract_year(item.get("date") or "")
    return int(year) if year else None


def notice_venue(item: Dict[str, Any]) -> Optional[str]:
    """Support de la notice : revue, ouvrage, actes, site, université,
    éditeur… ; à défaut, la ligne ``Original Source`` d'``extra``."""
    for name in _VENUE_FIELDS:
        value = " ".join(str(item.get(name) or "").split())
        if value:
            return value
    for value in extra_values(item, "Original Source"):
        value = " ".join(value.split())
        if value:
            return value
    return None


def _initials(first: str) -> str:
    parts = [p for p in re.split(r"([\s\-]+)", first.strip()) if p]
    out = []
    for p in parts:
        if p.strip() == "":
            out.append(" ")
        elif p.strip("-") == "":
            out.append("-")
        else:
            out.append(p[0].upper() + ".")
    return "".join(out).replace(" ", "")


def _creator_name(c: Dict[str, Any]) -> str:
    last = " ".join(str(c.get("lastName") or "").split())
    if last:
        first = str(c.get("firstName") or "").strip()
        return f"{last} {_initials(first)}" if first else last
    return _short(c.get("name") or "", 40)


def notice_authors(item: Dict[str, Any], limit: int = DIGEST_AUTHORS
                   ) -> Tuple[List[str], bool]:
    """
    Premiers auteurs (« Nom P. ») et indicateur « et al. » : auteurs et
    contributeurs ; à défaut, les personnes du premier autre rôle (éditeurs
    scientifiques…).
    """
    creators = [c for c in item.get("creators") or [] if isinstance(c, dict)]
    chosen = [c for c in creators
              if (c.get("creatorType") or "author") in _AUTHOR_ROLES]
    if not chosen and creators:
        role = creators[0].get("creatorType")
        chosen = [c for c in creators if c.get("creatorType") == role]
    names = [n for n in (_creator_name(c) for c in chosen) if n]
    return names[:limit], len(names) > limit


def notice_sources(item: Dict[str, Any]) -> List[str]:
    """Sources de la notice (ligne ``Sources:`` d'``extra``), libellés
    d'affichage, dans l'ordre de la ligne et sans doublon."""
    out: List[str] = []
    for value in extra_values(item, "Sources"):
        for label in value.split(","):
            name = display_name(label.strip())
            if name and name not in out:
                out.append(name)
        break
    return out


def notice_id(item: Dict[str, Any]) -> str:
    """Identifiant stable : clé forte principale (``utils.ranking.record_id``)."""
    from utils.ranking import record_id
    return record_id(item)


def notice_citations(item: Dict[str, Any]) -> Optional[int]:
    from utils.ranking import citation_count
    return citation_count(item)


# Réserves propres à la notice (texte du digest) ; les réserves de la moisson
# (ordre présumé, rang absent, fenêtre du fournisseur, requête en partie non
# évaluée) sont dites une fois dans l'en-tête ou par le rang natif affiché.
RESERVE_TEXT = {
    "scholar_snippet": "extrait Scholar, pas un résumé",
    "no_abstract": "sans résumé",
    "authors_truncated": "auteurs ou revue tronqués (Scholar)",
    "identity_ambiguous": "identité ambiguë",
    "identity_fuzzy": "identité par appariement flou",
    "rank_unmapped": "rang natif non rattaché",
    "source_partial": "source partielle",
    "excluded_term": "terme exclu présent",
}
# Code de réserve donné à une « source partielle » quand la seule troncature
# des sources de la notice est une limite du fournisseur (fenêtre de 200
# résultats d'Isidore) : ce n'est pas une perte.
RESERVE_PROVIDER_WINDOW = "provider_window"


def notice_reserves(item: Dict[str, Any]) -> List[str]:
    """Réserves propres à la notice, lues sur la notice elle-même (codes du
    classement, ``utils.ranking.RESERVE_*``)."""
    from utils.deduplication import (
        abstract_is_scholar_snippet, authors_truncated, parse_evidence,
    )
    codes: List[str] = []
    if abstract_is_scholar_snippet(item):
        codes.append("scholar_snippet")
    elif not str(item.get("abstractNote") or "").strip():
        codes.append("no_abstract")
    if authors_truncated(item):
        codes.append("authors_truncated")
    ev = parse_evidence(item)
    if ev and ev.get("ambiguous"):
        codes.append("identity_ambiguous")
    if ev and ev.get("fuzzy"):
        codes.append("identity_fuzzy")
    return codes


# === Termes de la requête ===

class TermMatcher:
    """Appariement des notices aux concepts d'une requête (même
    normalisation que le classement local : ``utils.ranking``)."""

    def __init__(self, query: Optional[str],
                 query_variants: Optional[Dict[str, str]] = None):
        self.concept_sets = []
        self.query = query
        if not query or not str(query).strip():
            return
        from utils.ranking import build_concepts
        try:
            self.concept_sets = build_concepts(query=str(query),
                                               query_variants=query_variants)
        except ValueError as exc:     # QueryPlanError
            raise QueryError(f"requête invalide : {exc}") from None

    def __bool__(self) -> bool:
        return bool(self.concept_sets)

    def match(self, item: Dict[str, Any]) -> Dict[str, Any]:
        from utils.ranking import match_notice
        return match_notice(self.concept_sets, item)


def _term_text(matched: Sequence[Dict[str, Any]]) -> List[str]:
    return [f"{m['concept']} ({', '.join(m['fields'])})" for m in matched]


# === Digest des producteurs ===

@dataclass
class DigestSpec:
    """
    Ce que le producteur sait de son corpus pour le digest.

    Attributes:
        items: notices Zotero, dans l'ordre du fichier de corpus.
        records: entrées ``records`` du manifeste, même ordre (rangs natifs,
            classement hérité) ; facultatif.
        ranking: classement du moteur (``HarvestResult.ranking``) ;
            prioritaire sur ``records``.
        query: requête (termes appariés) et ses variantes explicites.
        corpus_name: argument ``corpus`` de ``corpus_query`` affiché dans
            l'en-tête (fichier Zotero interne interrogeable) ; recalculé
            depuis ``corpus_path`` par l'appelant quand celui-ci est fourni
            (``mcp_server._query_reference`` : URI ``corpus://<nom>`` pour un
            fichier du répertoire des corpus, chemin absolu sinon ; un nom
            nu ne se résout pas depuis un répertoire courant quelconque).
        corpus_path: fichier Zotero interne que ``corpus_query`` interroge.
        source_status: statut par source de la moisson
            (``by_source_status``) : distingue une limite du fournisseur
            d'une perte.
        keys: clé de citation de chaque notice (export BibTeX).
        order_label: libellé de l'ordre quand il n'y a pas de classement
            (« année décroissante »…).
    """

    items: Sequence[Dict[str, Any]]
    records: Optional[Sequence[Dict[str, Any]]] = None
    ranking: Optional[Dict[str, Any]] = None
    query: Optional[str] = None
    query_variants: Optional[Dict[str, str]] = None
    corpus_name: Optional[str] = None
    source_status: Optional[Dict[str, Dict[str, Any]]] = None
    keys: Optional[Sequence[str]] = None
    order_label: Optional[str] = None
    corpus_path: Optional[Path] = None


@dataclass
class DigestEntry:
    """
    Une notice du digest : ligne complète, ligne courte (rang, titre, année,
    premier auteur, identifiant ; utilisée pour les dernières notices quand
    le plafond d'octets l'exige) et entrée machine.
    """

    line: str
    data: Dict[str, Any]
    short: str = ""


# Première ligne de tout digest (repérée par ``_fit_digest`` de mcp_server)
# et ligne qui signale un digest raccourci par le plafond d'octets.
DIGEST_HEADER = "Notices en tête"
DIGEST_TRUNCATED_PREFIX = "(Digest arrêté"


# Notices du digest toujours rendues en ligne complète avant qu'une notice ne
# soit omise (au-delà, les dernières passent en ligne courte).
DIGEST_MIN_FULL = 10


def truncation_note(count: int, wanted: int, full: Optional[int] = None) -> str:
    """Ligne d'en-tête d'un digest raccourci par le plafond d'octets :
    notices en ligne courte et notices omises."""
    full = count if full is None else min(full, count)
    parts = []
    if full < count:
        parts.append(f"notices {full + 1} à {count} en ligne courte")
    if count < wanted:
        parts.append(f"{count} notice(s) sur {wanted} demandées")
    return (f"{DIGEST_TRUNCATED_PREFIX} au plafond d'octets du retour : "
            + ", ".join(parts) + " ; détail et suite par corpus_query.)")


def digest_header(label: str, shown: int, total: int,
                  corpus: Optional[str] = None) -> List[str]:
    """
    Lignes d'en-tête d'un digest de ``shown`` notices (celles effectivement
    rendues, après le plafond d'octets) : ordre, nombre, légende, et appel de
    ``corpus_query`` pour les notices complètes et la suite, qui reprend à
    ``offset=shown`` (ordre du digest = tri par défaut de ``corpus_query`` :
    rang du manifeste, à défaut position ; ni trou ni doublon).
    """
    lines = [
        f"{DIGEST_HEADER} ({label}) : {shown} sur {total} ; "
        "métadonnées seulement, texte intégral non lu.",
        DIGEST_LEGEND,
    ]
    if corpus:
        # Argument cité en chaîne JSON : un chemin Windows (barres obliques
        # inverses) ou un nom portant un guillemet se recopie tel quel.
        lines.append(
            f"Notices complètes : corpus_query(corpus="
            f"{json.dumps(corpus, ensure_ascii=False)}, "
            f"ids=[…]) ; suivantes : offset={shown}.")
    return lines


_HEADER_COUNT_RE = re.compile(r"^(" + re.escape(DIGEST_HEADER)
                              + r" \(.*\) : )\d+( sur \d+ ;)")
_HEADER_OFFSET_RE = re.compile(r"(suivantes : offset=)\d+(\.)$")


def retarget_header(lines: Sequence[str], shown: int) -> List[str]:
    """En-tête d'un digest déjà rendu, ramené à ``shown`` notices (nombre
    affiché et ``offset`` de la suite) : sert quand des lignes de notices
    sont retirées après coup du texte (``mcp_server._fit_digest`` sans objet
    ``Digest``)."""
    out = []
    for line in lines:
        line = _HEADER_COUNT_RE.sub(lambda m: f"{m.group(1)}{shown}{m.group(2)}",
                                    line)
        if line.startswith("Notices complètes : corpus_query("):
            line = _HEADER_OFFSET_RE.sub(
                lambda m: f"{m.group(1)}{shown}{m.group(2)}", line)
        out.append(line)
    return out


@dataclass
class Digest:
    """
    Digest ordonné : en-tête, notices et description de l'ordre. Texte :
    lignes d'en-tête, puis une ligne par notice, rien après (le plafond
    d'octets raccourcit puis retire les dernières lignes ; l'en-tête suit le
    nombre de notices rendues).
    """

    header: List[str]
    entries: List[DigestEntry]
    order: Dict[str, Any]
    total: int
    top: int
    corpus_name: Optional[str] = None

    def text(self, count: Optional[int] = None, full: Optional[int] = None) -> str:
        count = len(self.entries) if count is None else min(count, len(self.entries))
        full = count if full is None else min(full, count)
        lines = (digest_header(self.order.get("label") or "", count, self.total,
                               self.corpus_name)
                 if count < len(self.entries) else list(self.header))
        wanted = min(self.top, self.total)
        if full < count or count < min(len(self.entries), wanted):
            lines.append(truncation_note(count, wanted, full))
        for i, e in enumerate(self.entries[:count]):
            lines.append(e.line if i < full or not e.short else e.short)
        return "\n".join(lines)


@dataclass
class _Ranked:
    index: int
    rank: int
    score: Optional[float] = None
    native: List[Dict[str, Any]] = field(default_factory=list)
    reserves: List[str] = field(default_factory=list)


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 5)
    return value


def _records_aligned(records: Optional[Sequence[Dict[str, Any]]],
                     n: int) -> Optional[List[Dict[str, Any]]]:
    """Entrées du manifeste alignées sur les notices (même nombre, positions
    1..n dans l'ordre), None sinon : jamais un rang attribué à la mauvaise
    notice."""
    if not records or len(records) != n:
        return None
    out = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            return None
        pos = rec.get("position")
        if pos is not None and pos != i + 1:
            return None
        out.append(rec)
    return out


def _native_from_ranking(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for n in (entry.get("justification") or {}).get("native") or []:
        if not isinstance(n, dict):
            continue
        out.append({"source": display_name(n.get("source")),
                    "rank": n.get("rank"),
                    "class": n.get("class")})
    return out


def _native_from_record(rec: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for n in (rec or {}).get("native") or []:
        if isinstance(n, dict):
            out.append({"source": display_name(n.get("source")),
                        "rank": n.get("rank")})
    return out


def _order(spec: DigestSpec) -> Tuple[List[_Ranked], Dict[str, Any]]:
    """Ordre du digest et sa description (``order``)."""
    n = len(spec.items)
    ranking = spec.ranking if isinstance(spec.ranking, dict) else None
    records = _records_aligned(spec.records, n)
    entries = (ranking or {}).get("entries") or []
    if ranking and entries and not ranking.get("error"):
        seen = set()
        order: List[_Ranked] = []
        for e in entries:
            idx = e.get("index") if isinstance(e, dict) else None
            if not isinstance(idx, int) or not 0 <= idx < n or idx in seen:
                continue
            seen.add(idx)
            codes = [r.get("code") for r in
                     (e.get("justification") or {}).get("reserves") or []
                     if isinstance(r, dict) and r.get("code")]
            order.append(_Ranked(index=idx, rank=int(e.get("rank") or len(order) + 1),
                                 score=e.get("score"),
                                 native=_native_from_ranking(e),
                                 reserves=codes))
        for idx in range(n):
            if idx not in seen:
                order.append(_Ranked(index=idx, rank=len(order) + 1,
                                     native=_native_from_record(
                                         records[idx] if records else None)))
        mode = ranking.get("mode")
        label = ("classement local (expérimental)" if mode == "local"
                 else "classement natif")
        return order, {"basis": "ranking", "mode": mode,
                       "version": ranking.get("version"),
                       "label": f"{label} {ranking.get('version') or ''}".strip()}
    if records and any(isinstance(r.get("ranking"), dict)
                       and r["ranking"].get("rank") is not None for r in records):
        ranked = []
        rest = []
        for i, rec in enumerate(records):
            rk = rec.get("ranking") if isinstance(rec.get("ranking"), dict) else None
            if rk and isinstance(rk.get("rank"), (int, float)):
                ranked.append((rk["rank"], i))
            else:
                rest.append(i)
        order = []
        for _r, i in sorted(ranked):
            rk = records[i]["ranking"]
            order.append(_Ranked(index=i, rank=len(order) + 1,
                                 score=rk.get("score"),
                                 native=_native_from_record(records[i]),
                                 reserves=[c for c in rk.get("reserves") or []
                                           if isinstance(c, str)]))
        for i in rest:
            order.append(_Ranked(index=i, rank=len(order) + 1,
                                 native=_native_from_record(records[i])))
        return order, {"basis": "inherited",
                       "label": "classement hérité du corpus source"}
    order = [_Ranked(index=i, rank=i + 1,
                     native=_native_from_record(records[i] if records else None))
             for i in range(n)]
    label = "ordre du corpus" + (f" ({spec.order_label})" if spec.order_label
                                 else " (aucun classement disponible)")
    return order, {"basis": "corpus_order", "label": label}


def _lossy_sources(source_status: Optional[Dict[str, Dict[str, Any]]]) -> set:
    """Clés des sources qui ont réellement perdu des notices (panne,
    échéance, annulation, non exécutées), hors limite du fournisseur."""
    from engine.models import BOUNDED_REASONS, SUCCESS_STATUSES
    out = set()
    for name, info in (source_status or {}).items():
        if not isinstance(info, dict):
            continue
        status = info.get("status")
        if status in SUCCESS_STATUSES:
            continue
        if status == "partial" and info.get("reason") in BOUNDED_REASONS:
            continue
        out.add(canonical_source_key(info.get("source") or name))
    return out


def _basis_text(native: Sequence[Dict[str, Any]], sources: Sequence[str]) -> str:
    """Sources et rangs natifs : « HAL #3, Isidore #37 (présumé), Cairn »."""
    parts: List[str] = []
    named = set()
    for n in native:
        name = n.get("source") or "?"
        cls = n.get("class")
        if n.get("rank") and cls in (None, "relevance", "presumed"):
            text = f"{name} #{n['rank']}"
            if cls == "presumed":
                text += "*"
        else:
            text = name
        if name not in named:
            parts.append(text)
            named.add(name)
    for name in sources:
        if name not in named:
            parts.append(name)
            named.add(name)
    return ", ".join(parts)


def build_digest(spec: DigestSpec, top: int) -> Digest:
    """
    Digest des ``top`` premières notices (ordre du classement). Le plafond
    en octets est appliqué par l'appelant (``Digest.text(count)`` et
    ``entries[:count]``).
    """
    items = list(spec.items or [])
    n = len(items)
    order, order_info = _order(spec)
    try:
        matcher = (TermMatcher(spec.query, spec.query_variants)
                   if spec.query else None)
    except QueryError:
        matcher = None          # requête non analysable : pas de termes
    lossy = _lossy_sources(spec.source_status)
    keys = list(spec.keys) if spec.keys and len(spec.keys) == n else None
    records = _records_aligned(spec.records, n)
    selected = order[:max(0, int(top))]
    entries: List[DigestEntry] = []
    for r in selected:
        item = items[r.index] if isinstance(items[r.index], dict) else {}
        rec = records[r.index] if records else None
        rid = (rec or {}).get("id") or notice_id(item)
        authors, et_al = notice_authors(item)
        year = notice_year(item)
        venue = notice_venue(item)
        doi = extract_doi(item)
        sources = notice_sources(item)
        cites = notice_citations(item)
        codes = list(dict.fromkeys(notice_reserves(item) + [
            c for c in r.reserves if c not in ("scholar_snippet", "no_abstract",
                                               "authors_truncated",
                                               "identity_ambiguous",
                                               "identity_fuzzy")]))
        if "source_partial" in codes:
            item_keys = {canonical_source_key(s) for s in sources} | {
                canonical_source_key(x.get("source")) for x in r.native}
            if spec.source_status is not None and not (item_keys & lossy):
                codes = [RESERVE_PROVIDER_WINDOW if c == "source_partial" else c
                         for c in codes]
        terms: List[Dict[str, Any]] = []
        missing: List[str] = []
        if matcher:
            m = matcher.match(item)
            terms = m["matched"]
            missing = m["missing"]
            if m["excluded"] and "excluded_term" not in codes:
                codes.append("excluded_term")
        full: Dict[str, Any] = {
            "rank": r.rank, "id": rid,
            "title": short_title(notice_title(item), DIGEST_TITLE_CHARS),
            "authors": authors, "et_al": et_al, "year": year,
            "venue": _short(venue, DIGEST_VENUE_CHARS) if venue else None,
            "type": item.get("itemType") or None, "doi": doi,
            "citations": cites, "sources": _basis_text(r.native, sources),
            "missing": missing, "reserves": codes,
            "key": keys[r.index] if keys else None,
        }
        # Entrée machine (``structuredContent.top_items``) : poignées de la
        # notice (rang, score du classement, identifiant pour
        # ``corpus_query(ids=…)``, clé de citation d'un export). Le texte
        # porte le digest complet : 40 notices complètes en double ne
        # tiendraient pas sous le plafond d'octets (écart consigné, OPT-17).
        data: Dict[str, Any] = {"rank": r.rank}
        if r.score is not None:
            data["score"] = _round(float(r.score))
        data["id"] = rid
        if keys:
            data["key"] = keys[r.index]
        entries.append(DigestEntry(line=_digest_line(full, terms), data=data,
                                   short=_digest_short_line(full)))
    corpus = spec.corpus_name
    shown = min(len(selected), n)
    header = digest_header(order_info["label"], shown, n, corpus)
    return Digest(header=header, entries=entries, order=order_info, total=n,
                  top=int(top), corpus_name=corpus)


_FIELD_LETTERS = {"titre": "t", "mots-clés": "m", "résumé": "r"}


def _terms_compact(matched: Sequence[Dict[str, Any]],
                   missing: Sequence[str]) -> str:
    """Termes appariés et absents, notation compacte de la légende du
    digest : « +discours(t,r) +numérique(r) −médias »."""
    parts = []
    for m in matched:
        letters = ",".join(_FIELD_LETTERS.get(f, f) for f in m.get("fields") or [])
        parts.append(f"+{m['concept']}" + (f"({letters})" if letters else ""))
    parts.extend(f"−{label}" for label in missing)
    return " ".join(parts)


# Légende de la notation compacte (en-tête du digest et des pages).
DIGEST_LEGEND = ("Légende : #n rang natif chez la source (* ordre présumé) ; "
                 "+terme apparié (t titre, m mots-clés, r résumé), −terme "
                 "absent ; ⚠ réserve.")


def _digest_short_line(data: Dict[str, Any]) -> str:
    """Ligne courte d'une notice : « rang. titre (année) — premier auteur
    et al. · identifiant »."""
    head = f"{data['rank']}. {short_title(data.get('title') or '(sans titre)', 60)}"
    if data.get("year"):
        head += f" ({data['year']})"
    authors = data.get("authors") or []
    parts = [head]
    if authors:
        parts.append(authors[0] + (" et al." if len(authors) > 1
                                   or data.get("et_al") else ""))
    parts.append(data["id"])
    return " ".join((" — ".join(parts[:-1]) + " · " + parts[-1]).split("\n"))


def _digest_line(data: Dict[str, Any], terms: Sequence[Dict[str, Any]], *,
                 show_title: bool = True, show_type: bool = True) -> str:
    """
    Ligne compacte d'une notice du digest (ou d'une page de
    ``corpus_query``) : « rang. titre (année) — auteurs — support · type ·
    sources et rangs natifs · identifiant · termes · citations · réserves ·
    clé ».

    ``show_title`` et ``show_type`` faux (page de ``corpus_query`` dont
    ``fields`` exclut ces champs) : titre et type omis, sans libellé de
    remplacement (« (sans titre) », « type inconnu » affirmeraient à tort
    qu'une notice titrée et typée n'a ni titre ni type).
    """
    head = f"{data['rank']}."
    if show_title:
        head += f" {data.get('title') or '(sans titre)'}"
    if data.get("year"):
        head += f" ({data['year']})"
    parts = [head]
    if data.get("authors"):
        parts.append(", ".join(data["authors"])
                     + (" et al." if data.get("et_al") else ""))
    tail = []
    if data.get("venue"):
        tail.append(data["venue"])
    if show_type:
        tail.append(type_label(data.get("type")))
    if data.get("sources"):
        tail.append(data["sources"])
    tail.append(data["id"])
    terms_text = _terms_compact(terms, data.get("missing") or [])
    if terms_text:
        tail.append(terms_text)
    if data.get("citations") is not None:
        tail.append(f"cité {data['citations']} fois")
    shown = [RESERVE_TEXT[c] for c in data.get("reserves") or [] if c in RESERVE_TEXT]
    if shown:
        tail.append("⚠ " + ", ".join(shown))
    if data.get("key"):
        tail.append(f"clé {data['key']}")
    parts.append(" · ".join(tail))
    return " ".join(" — ".join(parts).split("\n"))


# === corpus_query ===

FILTER_KEYS = ("year_min", "year_max", "item_types", "languages", "sources",
               "has_doi", "has_abstract")
SORTS = ("relevance", "rank", "position", "year_desc", "year_asc", "title")
# Champs projetables d'une page de résultats (les notices complètes passent
# par ``ids``).
FIELDS = ("id", "rank", "position", "score", "title", "authors", "creators",
          "year", "date", "venue", "type", "doi", "url", "language", "sources",
          "citations", "tags", "abstract", "terms", "missing", "reserves")
DEFAULT_FIELDS = ("id", "rank", "position", "score", "title", "authors",
                  "year", "venue", "type", "doi", "sources", "terms")


def _string_list(name: str, value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(v, str) for v in value):
        raise QueryError(f"{name} : liste de chaînes attendue")
    return [v.strip() for v in value if v.strip()]


def _year_bound(name: str, value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    year = _positive_int(name, value)
    if not 0 < year < 10000:
        raise QueryError(f"{name} = {value!r} invalide (année attendue)")
    return year


def _bool(name: str, value: Any) -> Optional[bool]:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise QueryError(f"{name} = {value!r} invalide (booléen attendu)")
    return value


@dataclass
class Filters:
    """Filtres locaux de ``corpus_query``."""

    year_min: Optional[int] = None
    year_max: Optional[int] = None
    item_types: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    has_doi: Optional[bool] = None
    has_abstract: Optional[bool] = None

    @classmethod
    def parse(cls, value: Any) -> "Filters":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise QueryError("filters : objet attendu (" + ", ".join(FILTER_KEYS) + ")")
        unknown = sorted(set(value) - set(FILTER_KEYS))
        if unknown:
            raise QueryError(f"filtre(s) inconnu(s) : {', '.join(unknown)} "
                             f"(acceptés : {', '.join(FILTER_KEYS)})")
        f = cls(
            year_min=_year_bound("year_min", value.get("year_min")),
            year_max=_year_bound("year_max", value.get("year_max")),
            item_types=_string_list("item_types", value.get("item_types")),
            languages=[x.lower() for x in _string_list("languages",
                                                        value.get("languages"))],
            sources=_string_list("sources", value.get("sources")),
            has_doi=_bool("has_doi", value.get("has_doi")),
            has_abstract=_bool("has_abstract", value.get("has_abstract")),
        )
        if f.year_min and f.year_max and f.year_min > f.year_max:
            raise QueryError("year_min postérieur à year_max")
        return f

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key in FILTER_KEYS:
            value = getattr(self, key)
            if value not in (None, []):
                out[key] = value
        return out

    def __bool__(self) -> bool:
        return bool(self.as_dict())

    def accepts(self, item: Dict[str, Any]) -> bool:
        if self.year_min or self.year_max:
            year = notice_year(item)
            if year is None:
                return False
            if self.year_min and year < self.year_min:
                return False
            if self.year_max and year > self.year_max:
                return False
        if self.item_types and item.get("itemType") not in self.item_types:
            return False
        if self.languages:
            from utils.query_plan import item_languages
            langs = item_languages(item.get("language"))
            if not any(lang in self.languages for lang in langs):
                return False
        if self.sources:
            wanted = {canonical_source_key(s) for s in self.sources}
            have = {canonical_source_key(s) for s in notice_sources(item)}
            if not wanted & have:
                return False
        if self.has_doi is not None and bool(extract_doi(item)) != self.has_doi:
            return False
        if self.has_abstract is not None and \
                bool(str(item.get("abstractNote") or "").strip()) != self.has_abstract:
            return False
        return True


def normalize_id(value: Any) -> str:
    """
    Forme de comparaison d'un identifiant demandé : clé forte typée
    (``doi:…``, ``hal:…``, ``url:…``…) ; un DOI nu (« 10.x/y ») ou une URL
    doi.org devient ``doi:<doi normalisé>``.

    Raises:
        QueryError: identifiant vide ou non textuel.
    """
    if not isinstance(value, str) or not value.strip():
        raise QueryError(f"identifiant {value!r} invalide (chaîne attendue)")
    text = value.strip()
    # DOI nu ou URL doi.org avant tout découpage sur « : » : un DOI peut en
    # contenir (DOI SICI de Wiley : « 10.1002/(SICI)…49:8<693::AID-…> »).
    if text.startswith("10.") or re.match(r"(?i)^https?://(dx\.)?doi\.org/", text):
        doi = normalize_doi(text)
        if doi:
            return f"doi:{doi}"
    kind, sep, rest = text.partition(":")
    if sep and kind.lower() == "doi":
        doi = normalize_doi(rest)
        return f"doi:{doi}" if doi else text
    doi = normalize_doi(text) if not sep or kind.lower() in ("http", "https") else None
    if doi and (text.startswith("10.") or "doi.org" in text.lower()):
        return f"doi:{doi}"
    if sep and kind.lower() in ("hal", "fp", "isbn", "arxiv", "openalex", "s2",
                                "core", "pmid", "isidore", "oai", "scholar"):
        return f"{kind.lower()}:{rest}"
    return text


def item_ids(item: Dict[str, Any], record_id: Optional[str] = None) -> set:
    """Identifiants sous lesquels une notice peut être demandée : son
    identifiant stable et toutes ses clés fortes."""
    ids = set(strong_keys(item))
    ids.add(record_id or notice_id(item))
    return ids


@dataclass
class ManifestView:
    """Ce que le manifeste apprend d'un fichier de corpus tel qu'il est
    (taille égale à celle qu'il décrit) : identifiants et classement par
    position, nombre de notices."""

    ids: Optional[List[str]] = None
    ranks: Optional[List[Optional[int]]] = None
    scores: Optional[List[Optional[float]]] = None
    item_count: Optional[int] = None
    ranking_label: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "ManifestView":
        from utils.manifest import load_manifest_for
        try:
            doc = load_manifest_for(path)
            size = path.stat().st_size
        except OSError:
            return cls()
        if not doc:
            return cls()
        described = None
        for entry in doc.get("files") or []:
            if isinstance(entry, dict) and entry.get("name") == path.name:
                described = entry
                break
        if described is None or described.get("bytes") != size:
            return cls()        # fichier modifié ou non décrit : rien de sûr
        count = described.get("items")
        records = doc.get("records") or []
        view = cls(item_count=count if isinstance(count, int) else None)
        if isinstance(count, int) and len(records) == count and all(
                isinstance(r, dict) and r.get("position") == i + 1
                for i, r in enumerate(records)):
            view.ids = [r.get("id") for r in records]
            ranks = [((r.get("ranking") or {}).get("rank")
                      if isinstance(r.get("ranking"), dict) else None)
                     for r in records]
            if any(x is not None for x in ranks):
                view.ranks = ranks
                view.scores = [((r.get("ranking") or {}).get("score")
                                if isinstance(r.get("ranking"), dict) else None)
                               for r in records]
                ranking = doc.get("ranking") or {}
                view.ranking_label = (
                    f"classement {'local' if ranking.get('mode') == 'local' else 'natif'} "
                    f"{ranking.get('version') or ''}".strip()
                    if ranking else "classement hérité du corpus source")
        return view


def _snapshot(path: Path) -> Dict[str, Any]:
    st = path.stat()
    return {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns}


# Lectures d'un même corpus par corpus_query : une première, puis une seule
# relecture si le fichier a changé entre-temps (remplacement atomique par
# corpus_filter, appels d'outils en parallèle).
QUERY_READ_ATTEMPTS = 2


def _stat_key(path: Path) -> Tuple[int, int, int]:
    """Identité d'un état du fichier (taille, ``mtime_ns``, inode) : un
    remplacement atomique change l'inode, une réécriture en place la date
    de modification. ``ctime_ns`` n'y entre pas : un client de
    synchronisation qui pose un attribut étendu le change sans toucher au
    contenu, d'où des relectures inutiles."""
    st = path.stat()
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _project(item: Dict[str, Any], fields: Sequence[str], *, rid: str,
             rank: Optional[int], position: int, score: Optional[float],
             match: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Champs demandés d'une notice (résumé tronqué), hors notice Zotero."""
    out: Dict[str, Any] = {}
    for name in fields:
        if name == "id":
            out["id"] = rid
        elif name == "rank":
            if rank is not None:
                out["rank"] = rank
        elif name == "position":
            out["position"] = position
        elif name == "score":
            if score is not None:
                out["score"] = _round(float(score))
        elif name == "title":
            out["title"] = notice_title(item)
        elif name == "authors":
            authors, et_al = notice_authors(item)
            if authors:
                out["authors"] = authors
            if et_al:
                out["et_al"] = True
        elif name == "creators":
            out["creators"] = item.get("creators") or []
        elif name == "year":
            year = notice_year(item)
            if year:
                out["year"] = year
        elif name == "date":
            if item.get("date"):
                out["date"] = item["date"]
        elif name == "venue":
            venue = notice_venue(item)
            if venue:
                out["venue"] = venue
        elif name == "type":
            out["type"] = item.get("itemType")
        elif name == "doi":
            doi = extract_doi(item)
            if doi:
                out["doi"] = doi
        elif name == "url":
            if item.get("url"):
                out["url"] = item["url"]
        elif name == "language":
            if item.get("language"):
                out["language"] = item["language"]
        elif name == "sources":
            sources = notice_sources(item)
            if sources:
                out["sources"] = sources
        elif name == "citations":
            cites = notice_citations(item)
            if cites is not None:
                out["citations"] = cites
        elif name == "tags":
            tags = [t.get("tag") if isinstance(t, dict) else t
                    for t in item.get("tags") or []]
            tags = [t for t in tags if t]
            if tags:
                out["tags"] = tags
        elif name == "abstract":
            abstract = " ".join(str(item.get("abstractNote") or "").split())
            if abstract:
                out["abstract"] = _short(abstract, QUERY_ABSTRACT_CHARS)
                if len(abstract) > QUERY_ABSTRACT_CHARS:
                    out["abstract_truncated"] = True
        elif name == "terms":
            if match and match.get("matched"):
                out["terms"] = _term_text(match["matched"])
        elif name == "missing":
            if match and match.get("missing"):
                out["missing"] = list(match["missing"])
        elif name == "reserves":
            codes = notice_reserves(item)
            if match and match.get("excluded"):
                codes.append("excluded_term")
            if codes:
                out["reserves"] = codes
    return out


def _title_key(item: Dict[str, Any]) -> str:
    return " ".join(unidecode(notice_title(item)).lower().split())


@dataclass
class QueryResult:
    """Résultat de ``query_corpus`` (sérialisable par ``as_dict``)."""

    corpus: str
    mode: str                          # "search" ou "ids"
    total_items: int
    matched: int = 0
    offset: int = 0
    limit: int = 0
    sort: Optional[str] = None
    order_label: Optional[str] = None
    filters: Dict[str, Any] = field(default_factory=dict)
    query: Optional[str] = None
    fields: List[str] = field(default_factory=list)
    items: List[Dict[str, Any]] = field(default_factory=list)
    positions: List[int] = field(default_factory=list)   # 0-based, ordre rendu
    # Notices complètes de la page (``with_notices``), même ordre que
    # ``items`` ; jamais sérialisées par ``as_dict``.
    notices: List[Dict[str, Any]] = field(default_factory=list)
    requested_ids: List[str] = field(default_factory=list)
    missing_ids: List[str] = field(default_factory=list)
    snapshot: Dict[str, Any] = field(default_factory=dict)
    unevaluated: List[str] = field(default_factory=list)
    # Appariement de chaque notice rendue (texte compact), non sérialisé.
    matches: List[Optional[Dict[str, Any]]] = field(default_factory=list)

    @property
    def next_offset(self) -> Optional[int]:
        if self.mode != "search":
            return None
        end = self.offset + len(self.items)
        return end if end < self.matched else None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "corpus": self.corpus,
            "mode": self.mode,
            "total_items": self.total_items,
            "snapshot": self.snapshot,
        }
        if self.mode == "search":
            out.update({
                "query": self.query,
                "filters": self.filters,
                "sort": self.sort,
                "order": self.order_label,
                "fields": self.fields,
                "matched": self.matched,
                "offset": self.offset,
                "limit": self.limit,
                "returned": len(self.items),
                "next_offset": self.next_offset,
                "items": self.items,
            })
            if self.unevaluated:
                out["query_unevaluated"] = self.unevaluated
        else:
            out.update({
                "requested_ids": self.requested_ids,
                "found": len(self.items),
                "missing_ids": self.missing_ids,
                "items": self.items,
            })
        return out


def _resolve_limit(value: Any) -> int:
    if value is None:
        return QUERY_DEFAULT_LIMIT
    limit = _positive_int("limit", value)
    if limit < 1:
        raise QueryError(f"limit = {value!r} invalide (entier >= 1 attendu)")
    return min(limit, QUERY_MAX_LIMIT)


def _resolve_offset(value: Any) -> int:
    if value is None:
        return 0
    offset = _positive_int("offset", value)
    if offset < 0:
        raise QueryError(f"offset = {value!r} invalide (entier >= 0 attendu)")
    return offset


def _resolve_fields(value: Any) -> List[str]:
    if value is None:
        return list(DEFAULT_FIELDS)
    names = _string_list("fields", value)
    if not names:
        return list(DEFAULT_FIELDS)
    unknown = [n for n in names if n not in FIELDS]
    if unknown:
        raise QueryError(f"champ(s) inconnu(s) : {', '.join(unknown)} "
                         f"(acceptés : {', '.join(FIELDS)} ; notice complète "
                         f"par ids)")
    return list(dict.fromkeys(["id", *names]))


def _resolve_ids(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise QueryError("ids : liste d'identifiants attendue")
    if len(value) > QUERY_MAX_IDS:
        raise QueryError(f"ids : {len(value)} identifiants, au plus "
                         f"{QUERY_MAX_IDS} par appel (notices complètes)")
    out: List[str] = []
    for v in value:
        norm = normalize_id(v)
        if norm not in out:
            out.append(norm)
    return out


def query_corpus(path: Path, *, query: Optional[str] = None,
                 filters: Any = None, fields: Any = None,
                 sort: Optional[str] = None, offset: Any = None,
                 limit: Any = None, ids: Any = None,
                 check: Optional[Callable[[], None]] = None,
                 with_notices: bool = False) -> QueryResult:
    """
    Recherche locale classée, filtres et pagination, ou notices complètes
    par identifiants (``ids``), dans un corpus Zotero interne lu en flux.

    Cohérence : l'état du fichier (taille, dates, inode) est relevé avant la
    première lecture et revérifié après la dernière. S'il a changé (fichier
    remplacé entre les deux passes), la requête est relancée une fois sur le
    nouvel état ; s'il change encore, ``CorpusChangedError`` : jamais une
    page triée sur un contenu et projetée sur un autre.

    Args:
        path: corpus Zotero interne (déjà résolu et confiné par l'appelant).
        query: requête (mêmes formes que ``moisson_search`` : expressions
            exactes, alternatives, exclusions, troncature) ; score local
            ``simple`` du classement (titre 3, mots-clés 2, résumé 1). Une
            exclusion ne retire pas la notice : elle divise son score par
            deux si le terme exclu figure dans le titre ou les mots-clés
            (règle du classement), et la réserve ``excluded_term`` la
            signale.
        filters: ``Filters.parse``.
        fields: champs projetés (``FIELDS``).
        sort: ``relevance`` (défaut avec une requête), ``rank`` (défaut
            sinon : classement du manifeste, à défaut position),
            ``position``, ``year_desc``, ``year_asc``, ``title``.
        offset, limit: pagination (ordre total, départage par position).
        ids: au plus ``QUERY_MAX_IDS`` identifiants : notices complètes, dans
            l'ordre demandé ; les autres paramètres sont alors ignorés.
        check: rappel d'annulation, appelé périodiquement.
        with_notices: garder aussi les notices complètes de la page
            (``QueryResult.notices``), lues pendant la même passe que leur
            projection (sélection publiée en HTTP, sans troisième lecture).

    Raises:
        QueryError: paramètre refusé ; NotNoticesError : élément qui n'est
        pas un objet JSON ; ValueError : contenu qui n'est pas un tableau
        JSON ; CorpusChangedError : fichier modifié à chaque tentative.
    """
    path = Path(path)
    if query is not None and not isinstance(query, str):
        raise QueryError("query : chaîne attendue")
    if sort is not None and not isinstance(sort, str):
        raise QueryError("sort : chaîne attendue")
    wanted_ids = _resolve_ids(ids)
    params = None
    if not wanted_ids:
        flt = Filters.parse(filters)
        proj = _resolve_fields(fields)
        off = _resolve_offset(offset)
        lim = _resolve_limit(limit)
        matcher = TermMatcher(query) if query and str(query).strip() else None
        if sort is None:
            sort = "relevance" if matcher else "rank"
        if sort not in SORTS:
            raise QueryError(f"tri {sort!r} inconnu (attendu : {', '.join(SORTS)})")
        if sort == "relevance" and not matcher:
            raise QueryError("tri 'relevance' sans requête (query) : utiliser "
                             "'rank' ou 'position'")
        params = (flt, proj, off, lim, matcher, sort, query)

    for attempt in range(QUERY_READ_ATTEMPTS):
        before = _stat_key(path)
        view = ManifestView.load(path)
        snapshot = _snapshot(path)
        if wanted_ids:
            result = _fetch_ids(path, wanted_ids, view, snapshot, check)
        else:
            result = _search(path, view, snapshot, check, with_notices, *params)
        if _stat_key(path) == before:
            return result
        logger.info(f"corpus_query : {path.name} modifié pendant la lecture "
                    f"(tentative {attempt + 1}/{QUERY_READ_ATTEMPTS})")
    raise CorpusChangedError(
        f"{path.name} a changé pendant la lecture, à chacune des "
        f"{QUERY_READ_ATTEMPTS} tentatives")


def _search(path: Path, view: ManifestView, snapshot: Dict[str, Any],
            check: Optional[Callable[[], None]], with_notices: bool,
            flt: Filters, proj: List[str], off: int, lim: int,
            matcher: Optional[TermMatcher], sort: str,
            query: Optional[str]) -> QueryResult:
    """Une lecture de ``query_corpus`` en mode recherche (deux passes)."""
    # Passe 1 : clé de tri de chaque notice retenue (mémoire : quelques
    # nombres par notice ; les champs ne sont projetés que pour la page).
    keys: List[Tuple] = []
    total = 0
    for pos, item in enumerate(iter_json_array(path)):
        total += 1
        if check is not None and pos % 256 == 0:
            check()
        if not isinstance(item, dict):
            raise NotNoticesError(NOT_NOTICES_MESSAGE)
        if not flt.accepts(item):
            continue
        m = matcher.match(item) if matcher else None
        if matcher and m["score"] <= 0:
            continue
        rank = view.ranks[pos] if view.ranks and pos < len(view.ranks) else None
        rank_key = rank if rank is not None else 10 ** 9 + pos
        if sort == "relevance":
            key = (-m["score"], rank_key, pos)
        elif sort == "rank":
            key = (rank_key, pos)
        elif sort == "position":
            key = (pos,)
        elif sort in ("year_desc", "year_asc"):
            year = notice_year(item)
            missing = 1 if year is None else 0
            y = year or 0
            key = (missing, -y if sort == "year_desc" else y, rank_key, pos)
        else:
            key = (_title_key(item), pos)
        keys.append(key)
    keys.sort()
    page = keys[off:off + lim]
    page_pos = [k[-1] for k in page]
    wanted = set(page_pos)
    rank_label = view.ranking_label or "position dans le fichier"
    order_label = {
        "relevance": "pertinence locale (titre, mots-clés, résumé), puis "
                     + rank_label,
        "rank": rank_label if view.ranks else "position dans le fichier "
                                              "(aucun classement au manifeste)",
        "position": "position dans le fichier",
        "year_desc": "année décroissante",
        "year_asc": "année croissante",
        "title": "titre",
    }[sort]
    result = QueryResult(
        corpus=path.name, mode="search", total_items=total, matched=len(keys),
        offset=off, limit=lim, sort=sort, order_label=order_label,
        filters=flt.as_dict(), query=query if matcher else None, fields=proj,
        snapshot=snapshot)
    if matcher:
        result.unevaluated = sorted({u for cs in matcher.concept_sets
                                     for u in cs.unevaluated})
    if not page_pos:
        return result
    # Passe 2 : projection des seules notices de la page (arrêt à la
    # dernière position utile).
    last = max(page_pos)
    found: Dict[int, Dict[str, Any]] = {}
    whole: Dict[int, Dict[str, Any]] = {}
    matched_by_pos: Dict[int, Optional[Dict[str, Any]]] = {}
    for pos, item in enumerate(iter_json_array(path)):
        if pos > last:
            break
        if pos not in wanted or not isinstance(item, dict):
            continue
        rid = view.ids[pos] if view.ids and pos < len(view.ids) and view.ids[pos] \
            else notice_id(item)
        rank = view.ranks[pos] if view.ranks and pos < len(view.ranks) else None
        m = matcher.match(item) if matcher else None
        score = m["score"] if m is not None else (
            view.scores[pos] if view.scores and pos < len(view.scores) else None)
        found[pos] = _project(item, proj, rid=rid, rank=rank, position=pos + 1,
                              score=score, match=m)
        matched_by_pos[pos] = m
        if with_notices:
            whole[pos] = item
    result.positions = [p for p in page_pos if p in found]
    result.items = [found[p] for p in result.positions]
    result.matches = [matched_by_pos.get(p) for p in result.positions]
    if with_notices:
        result.notices = [whole[p] for p in result.positions]
    return result


def _fetch_ids(path: Path, wanted: List[str], view: ManifestView,
               snapshot: Dict[str, Any],
               check: Optional[Callable[[], None]]) -> QueryResult:
    """Notices complètes par identifiants (ordre demandé)."""
    result = QueryResult(corpus=path.name, mode="ids", total_items=0,
                         requested_ids=list(wanted), snapshot=snapshot)
    remaining = set(wanted)
    found: Dict[str, Tuple[int, Dict[str, Any], str]] = {}
    # Positions connues par le manifeste (identifiant principal de chaque
    # notice) : si tous les identifiants demandés y figurent, lecture arrêtée
    # à la dernière notice utile ; sinon, parcours complet par clés fortes.
    stop_at = None
    if view.ids is not None:
        first: Dict[str, int] = {}
        for i, rid in enumerate(view.ids):
            if rid in remaining and rid not in first:
                first[rid] = i
        if first and set(first) == remaining:
            stop_at = max(first.values())
    total = 0
    for pos, item in enumerate(iter_json_array(path)):
        total += 1
        if check is not None and pos % 256 == 0:
            check()
        if stop_at is not None and pos > stop_at:
            total = None
            break
        if not isinstance(item, dict):
            raise NotNoticesError(NOT_NOTICES_MESSAGE)
        if not remaining:
            continue
        rid = view.ids[pos] if view.ids and pos < len(view.ids) and view.ids[pos] \
            else None
        if rid is not None and rid in remaining and stop_at is not None:
            hits = {rid}
        else:
            hits = item_ids(item, rid) & remaining
        for h in hits:
            found[h] = (pos, item, rid or notice_id(item))
            remaining.discard(h)
    result.total_items = total if total is not None else (view.item_count or 0)
    for wid in wanted:
        if wid not in found:
            result.missing_ids.append(wid)
            continue
        pos, item, rid = found[wid]
        rank = view.ranks[pos] if view.ranks and pos < len(view.ranks) else None
        entry: Dict[str, Any] = {"id": rid, "requested": wid, "position": pos + 1}
        if rank is not None:
            entry["rank"] = rank
        entry["notice"] = item
        result.items.append(entry)
        result.positions.append(pos)
    return result


def read_positions(path: Path, positions: Sequence[int]) -> List[Dict[str, Any]]:
    """Notices complètes aux positions données (0-based), dans l'ordre
    donné ; lecture en flux arrêtée à la dernière position utile."""
    wanted = set(positions)
    if not wanted:
        return []
    last = max(wanted)
    found: Dict[int, Dict[str, Any]] = {}
    for pos, item in enumerate(iter_json_array(path)):
        if pos > last:
            break
        if pos in wanted:
            found[pos] = item
    return [found[p] for p in positions if p in found]


def query_lines(result: QueryResult) -> List[str]:
    """Texte compact d'une page de ``corpus_query`` (une ligne par notice)."""
    if result.mode == "ids":
        lines = [f"corpus_query : {len(result.items)} notice(s) complète(s) sur "
                 f"{len(result.requested_ids)} demandée(s) dans {result.corpus}."]
        if result.missing_ids:
            lines.append("Identifiants introuvables : " + ", ".join(result.missing_ids))
        return lines
    end = result.offset + len(result.items)
    lines = [
        f"corpus_query : {result.matched} notice(s) sur {result.total_items} "
        f"retenue(s) dans {result.corpus}"
        + (f" pour « {result.query} »" if result.query else "")
        + (f", filtres {json.dumps(result.filters, ensure_ascii=False)}"
           if result.filters else "")
        + f" ; tri : {result.order_label}.",
        (f"Notices {result.offset + 1} à {end} :" if result.items
         else "Aucune notice sur cette page."),
    ]
    if result.items:
        lines.insert(1, DIGEST_LEGEND)
    for i, data in enumerate(result.items):
        entry = dict(data)
        m = result.matches[i] if i < len(result.matches) else None
        view = {"rank": result.offset + i + 1,
                "title": _short(entry.get("title") or "", DIGEST_TITLE_CHARS)
                if "title" in entry else None,
                "year": entry.get("year"),
                "authors": entry.get("authors"),
                "et_al": entry.get("et_al"),
                "venue": _short(entry["venue"], DIGEST_VENUE_CHARS)
                if entry.get("venue") else None,
                "type": entry.get("type"),
                "sources": ", ".join(entry["sources"]) if entry.get("sources") else None,
                "id": entry["id"],
                "citations": entry.get("citations"),
                "missing": (m or {}).get("missing") or None,
                "reserves": entry.get("reserves")}
        lines.append(_digest_line({k: v for k, v in view.items() if v is not None},
                                  (m or {}).get("matched") or [],
                                  show_title="title" in result.fields,
                                  show_type="type" in result.fields))
    if result.next_offset is not None:
        lines.append(f"Suite : offset={result.next_offset} (même requête, mêmes "
                     f"filtres, même tri).")
    if result.unevaluated:
        lines.append("Parties de la requête non évaluées localement : "
                     + ", ".join(result.unevaluated))
    return lines


# === Pages de ressource corpus://<nom>?offset=&limit= ===

def resolve_page(offset: Optional[int], limit: Optional[int]) -> Tuple[int, int]:
    """Page d'une ressource : ``offset`` (0 par défaut) et ``limit``
    (``RESOURCE_PAGE_DEFAULT`` par défaut, au plus ``RESOURCE_PAGE_MAX``)."""
    off = 0 if offset is None else int(offset)
    lim = RESOURCE_PAGE_DEFAULT if limit is None else int(limit)
    return max(0, off), max(1, min(lim, RESOURCE_PAGE_MAX))


def iter_page(path: Path, offset: int, limit: int, *,
              item_bytes: Callable[[Any], int], max_bytes: int,
              known_total: Optional[int] = None
              ) -> Tuple[List[Any], Optional[int], bool]:
    """
    Notices ``offset`` à ``offset + limit`` d'un tableau JSON, lues en flux,
    sous un plafond souple d'octets (au moins une notice si la page en a).

    Returns:
        ``(notices, total ou None, plafond atteint)`` ; le total est compté
        jusqu'au bout du fichier sauf si ``known_total`` est fourni (lecture
        arrêtée après la page).
    """
    page: List[Any] = []
    size = 0
    capped = False
    total = 0
    for pos, item in enumerate(iter_json_array(path)):
        total = pos + 1
        if pos < offset:
            continue
        if len(page) < limit and not capped:
            cost = item_bytes(item)
            if page and size + cost > max_bytes:
                capped = True
            else:
                page.append(item)
                size += cost
                continue
        if known_total is not None:
            return page, known_total, capped
    return page, total, capped

