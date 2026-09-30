"""
Classement explicable des notices d'une moisson (OPT-13, constats C25, C29).

Deux classeurs, versionnés ensemble (``RANKING_VERSION``) : toute modification
d'un paramètre ci-dessous change la version, si bien qu'un rang publié se
relit toujours avec les règles qui l'ont produit.

``native`` (ordre par défaut, toujours disponible)
    Fusion de rangs réciproques (RRF, Cormack et al. 2009) des rangs natifs
    publiés par les connecteurs (OPT-12) : ``score = Σ_p w_p / (k + r_p)``,
    ``k = 60``, ``r_p`` le meilleur rang de la notice chez le fournisseur
    ``p``. Le poids dépend de l'ordre RÉELLEMENT appliqué par la source
    pendant l'exécution (``SourceResult.native_sort``), pas de la source
    elle-même :

    - ``relevance`` (poids 1) : ordre de pertinence vérifié en direct le
      2026-09-29 (OPT-12) : HAL en bibliographie (``score desc,docid asc``),
      CrossRef en bibliographie (``score:desc`` sous curseur), OpenAlex
      (``relevance_score:desc``) ;
    - ``presumed`` (poids 0,5) : ordre par défaut présumé de pertinence mais
      NON vérifié : Isidore et ses sous-sources (``isidore_default`` : aucun
      score exposé, documentation de l'API en 403 le 2026-09-29, revue
      OPT-12) ; Semantic Scholar ``/paper/search`` s'il venait à être
      utilisé (``relevance`` : jamais observé en direct, 429 à chacun des
      8 appels du 2026-09-29). Il contribue, avec un poids moindre, et la
      réserve est nommée dans la justification ;
    - ``other`` (poids 0) : ordre sans rapport avec la pertinence (HAL en
      collecte ``docid asc``, CrossRef en collecte ``cursor_default``,
      Semantic Scholar bulk ``paperId`` ou ``citationCount:desc``, tri par
      date) ;
    - ``unranked`` (poids 0) : aucun rang publié (CORE, Google Scholar) : la
      position dans la liste de la source tient lieu d'ordre natif.

    Une notice sans aucun rang pondéré va « en queue », ordonnée par sa
    meilleure position dans un ordre natif NEUTRE (``docid``, ordre d'index,
    ``paperId``, position d'une source sans rang), puis par identifiant. Un
    ordre qui trie selon une dimension séparée (citations, date :
    ``citationCount:desc``, ``publicationDate:desc``…, voir
    ``is_dimension_sort``) n'ordonne jamais la queue : sinon les citations
    ou la récence décideraient du rang. Une notice qui n'a que ce type de
    preuve se place après les notices ordonnées par une position neutre,
    départagée par identifiant. Plusieurs sous-sources d'Isidore (OpenEdition, Cairn,
    Persée, Érudit, Theses.fr) lisent le même flux : elles forment UN
    fournisseur, qui ne compte qu'une fois (invariant 6). Départage
    déterministe : identifiant de la notice (clé forte prioritaire d'
    ``utils.identity``, à défaut empreinte titre/année/auteur), puis, en
    dernier recours, sérialisation canonique de la notice.

``local`` (désactivé par défaut : activation soumise au protocole d'OPT-15)
    Correspondance des concepts du plan de requête (OPT-11 : expressions
    exactes, termes, alternatives, variantes explicites par langue) dans le
    titre, les mots-clés (tags) et le résumé. Troncature finale
    (``numéri*`` : mot commençant par le préfixe) et proximité
    (``"a b"~N`` : tous les mots dans une fenêtre de ``len + N`` mots, ordre
    libre) sont évaluées ; un champ non textuel (auteur, identifiant, date,
    type, revue…), un joker interne ou initial (``*``, ``?``) et la
    recherche approchée (``mot~``) ne le sont pas : ces parties sont écartées
    des concepts et listées dans ``unevaluated`` (jamais déclarées
    « absentes »). Deux pondérations lexicales
    calculées pour chaque notice et comparées (``local.comparison``) :

    - ``simple`` (modèle retenu par défaut, ``DEFAULT_LEXICAL_MODEL``) :
      couverture pondérée des concepts, meilleur champ par concept (titre 3,
      mots-clés 2, résumé 1 ; extrait Scholar 0,5), expression complète d'une
      requête de mots seuls comptée comme demi-concept, pénalité ×0,5 si un
      terme exclu figure dans le titre ou les mots-clés ; score dans [0, 1] ;
    - ``bm25`` : BM25 à champs pondérés (BM25F simplifié : fréquences et
      longueurs pondérées par champ), ``k1 = 1,2``, ``b = 0,75``, IDF
      ``ln(1 + (N - df + 0,5) / (df + 0,5))`` calculé sur les notices de la
      moisson (seule collection disponible : les termes communs à toute la
      moisson y pèsent peu).

    Le score local combine par RRF le rang lexical et le rang natif :
    ``1/(k + rang_lexical) + 1/(k + rang_natif)`` (rangs de compétition :
    ex aequo au même rang ; composante absente si le score est nul). À
    score local égal, la preuve lexicale passe d'abord (score du modèle
    lexical retenu) : une notice sans appariement au rang natif ``r`` et une
    notice appariée au rang lexical ``r`` ont le même score local, et c'est
    la seconde qui passe devant.

    La comparaison des deux modèles (``local.comparison``) porte sur les
    notices appariées par au moins un des deux modèles (corrélation de
    Spearman à rangs moyens pour les ex aequo) ; le bloc des notices
    appariées par aucun n'y entre pas et sa taille est publiée à part
    (``unmatched``).

Dimensions séparées (présentées à part, jamais dans le score principal) :
nombre de fournisseurs indépendants (proxies d'Isidore comptés une fois),
citations (maximum des lignes ``Cited by``), récence (année). Elles ne
départagent, en mode ``local``, que des notices ex aequo sur le score local
ET sur le score lexical (fournisseurs indépendants, puis citations) ; en mode
``native``, jamais (identifiant seul) : une notice très citée hors sujet ne
passe jamais devant une notice centrale.

Justification par notice : rangs natifs retenus (source, fournisseur, classe,
poids), concepts appariés et champs (mode local), concepts absents, réserves
(extrait Scholar au lieu d'un résumé, résumé absent, auteurs tronqués,
identité ambiguë ou floue, rang natif non rattaché, source partielle ou en
échec, terme exclu présent, ordre natif présumé, absence de rang de
pertinence, partie de la requête non évaluée localement).

Compteurs (``counts``) : notices avec au moins un rang de pertinence
vérifié (``with_verified_relevance_rank``), notices classées par un ordre
présumé seul (``presumed_only``), notices en queue (``queue``).

Invariant 1 : le classement ne modifie jamais les notices ni l'ordre des
fichiers de corpus (ordre déterministe d'OPT-06). Il est rendu à part
(``HarvestResult.ranking``, ``structuredContent.top_ranked``, futur manifeste
d'OPT-16).

Rattachement des rangs aux notices finales : par les groupes de la
déduplication s'ils sont fournis (``deduplicate_with_report(...,
with_groups=True)``) ; sinon par identité d'objet (notice non fusionnée,
rendue telle quelle), puis par clés fortes (``utils.identity``) pour les
notices fusionnées, puis par empreinte titre/année. Un rang qui ne peut être
rattaché sans ambiguïté n'est attribué à aucune notice (réserve « rang natif
non rattaché » sur les notices candidates) : jamais un rang attribué à la
mauvaise notice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from unidecode import unidecode

from utils.cleaner import extract_year
from utils.identity import extra_values, strong_key_sources
from utils.source_labels import canonical_source_key, display_name

logger = logging.getLogger("moisson.ranking")


# === Version et modes ===

# Version des règles de classement : à changer avec tout paramètre ci-dessous.
# opt13-2 (revue d'OPT-13) : ordres de citations ou de date exclus de la
# queue ; « relevance » (S2 /paper/search, non observé) rangé en ordre
# présumé ; troncature, proximité et champs non textuels traités ; preuve
# lexicale avant les dimensions séparées en mode local ; Spearman restreint
# aux notices appariées, à rangs moyens.
RANKING_VERSION = "opt13-2"

RANKING_NATIVE = "native"
RANKING_LOCAL = "local"
RANKING_OFF = "off"
RANKING_MODES = (RANKING_NATIVE, RANKING_LOCAL, RANKING_OFF)


# === Paramètres (liés à RANKING_VERSION ; non surchargeables par
# l'environnement : un même numéro de version doit toujours donner le même
# classement) ===

# Constante de la fusion de rangs réciproques (valeur de Cormack et al.).
RRF_K = 60

CLASS_RELEVANCE = "relevance"
CLASS_PRESUMED = "presumed"
CLASS_OTHER = "other"
CLASS_UNRANKED = "unranked"

CLASS_WEIGHTS = {
    CLASS_RELEVANCE: 1.0,
    CLASS_PRESUMED: 0.5,
    CLASS_OTHER: 0.0,
    CLASS_UNRANKED: 0.0,
}

CLASS_LABELS = {
    CLASS_RELEVANCE: "pertinence vérifiée",
    CLASS_PRESUMED: "ordre présumé de pertinence, non vérifié",
    CLASS_OTHER: "ordre sans rapport avec la pertinence",
    CLASS_UNRANKED: "aucun rang publié (position dans la liste)",
}

# Tris natifs (``SourceResult.native_sort``, publiés par les connecteurs,
# OPT-12) qui sont des ordres de pertinence vérifiés en direct.
RELEVANCE_SORTS = frozenset({
    "score desc,docid asc",   # HAL, bibliographie (start/rows)
    "score desc",             # HAL, tri par score seul
    "score:desc",             # CrossRef, bibliographie (sort=score sous curseur)
    "relevance_score:desc",   # OpenAlex (search, défaut de l'API)
})
# Ordres présumés de pertinence, non vérifiés.
PRESUMED_SORTS = frozenset({
    "isidore_default",        # Isidore et ses sous-sources
    # Semantic Scholar /paper/search (non utilisé à ce jour) : ordre jamais
    # observé en direct (429 à chacun des 8 appels du 2026-09-29, OPT-12).
    "relevance",
})
# Ordres qui trient selon une dimension séparée (citations reçues ou
# références, date de publication, de dépôt ou de mise à jour) : jamais
# utilisés pour ordonner la queue (``is_dimension_sort``).
_DIMENSION_SORT_RE = re.compile(
    r"cit(?:ation|ed)|referenc|date|year|publi|issued|created|deposited"
    r"|indexed|recen", re.IGNORECASE)

# Champs lus par le classement local et leur poids.
FIELD_TITLE = "title"
FIELD_TAGS = "tags"
FIELD_ABSTRACT = "abstract"
FIELDS = (FIELD_TITLE, FIELD_TAGS, FIELD_ABSTRACT)
FIELD_WEIGHTS = {FIELD_TITLE: 3.0, FIELD_TAGS: 2.0, FIELD_ABSTRACT: 1.0}
FIELD_LABELS = {FIELD_TITLE: "titre", FIELD_TAGS: "mots-clés",
                FIELD_ABSTRACT: "résumé"}
# Poids du résumé quand c'est un extrait Google Scholar (choisi par Google
# autour des mots cherchés : correspondance biaisée).
SNIPPET_WEIGHT = 0.5
# Poids de l'expression complète implicite d'une requête de mots seuls
# (« discours numérique » cherché aussi comme expression).
IMPLICIT_PHRASE_WEIGHT = 0.5
# Facteur appliqué au score lexical simple quand un terme exclu figure dans
# le titre ou les mots-clés (source qui n'a pas pu appliquer l'exclusion).
EXCLUDED_PENALTY = 0.5
# BM25 (Robertson et Zaragoza 2009), paramètres usuels.
BM25_K1 = 1.2
BM25_B = 0.75

LEXICAL_SIMPLE = "simple"
LEXICAL_BM25 = "bm25"
LEXICAL_MODELS = (LEXICAL_SIMPLE, LEXICAL_BM25)
DEFAULT_LEXICAL_MODEL = LEXICAL_SIMPLE
# Poids des deux composantes du score local (RRF des rangs).
LOCAL_WEIGHTS = {"lexical": 1.0, "native": 1.0}

# Notices exposées dans structuredContent.top_ranked.
TOP_EXPOSED = 20
# Taille des classements comparés (recouvrement simple / BM25).
COMPARISON_TOP = 20
# Fréquence du contrôle d'annulation dans les boucles (notices).
_CHECK_EVERY = 256

# Statuts de source au périmètre exécuté (engine.models.SUCCESS_STATUSES).
_SUCCESS_STATUSES = frozenset({"complete", "empty"})

# Ordre des types de clé pour l'identifiant d'une notice.
_ID_KIND_ORDER = ("doi", "isbn", "hal", "arxiv", "openalex", "s2", "core",
                  "pmid", "isidore", "oai", "scholar", "url")


def ranking_params(lexical_model: str = DEFAULT_LEXICAL_MODEL) -> Dict[str, Any]:
    """Paramètres du classement (consignés avec chaque résultat)."""
    return {
        "rrf_k": RRF_K,
        "class_weights": dict(CLASS_WEIGHTS),
        "relevance_sorts": sorted(RELEVANCE_SORTS),
        "presumed_sorts": sorted(PRESUMED_SORTS),
        "queue_order": "meilleure position dans un ordre natif neutre, puis "
                       "identifiant ; ordres de citations ou de date ignorés "
                       f"(motif {_DIMENSION_SORT_RE.pattern!r})",
        "local_tie_break": "score local, score lexical du modèle retenu, "
                           "fournisseurs indépendants, citations, identifiant",
        "field_weights": dict(FIELD_WEIGHTS),
        "snippet_weight": SNIPPET_WEIGHT,
        "implicit_phrase_weight": IMPLICIT_PHRASE_WEIGHT,
        "excluded_penalty": EXCLUDED_PENALTY,
        "bm25": {"k1": BM25_K1, "b": BM25_B,
                 "idf": "ln(1 + (N - df + 0.5) / (df + 0.5))"},
        "lexical_model": lexical_model,
        "local_weights": dict(LOCAL_WEIGHTS),
        "normalization": "casse, accents (unidecode), élisions et mots "
                         "d'une lettre retirés, pluriel final s/x retiré "
                         "(mots de plus de 3 lettres)",
        "operators": "troncature finale (préfixe), proximité ~N (fenêtre de "
                     "len + N mots, ordre libre) ; champs non textuels, "
                     "jokers internes ou initiaux et recherche approchée non "
                     "évalués (unevaluated)",
    }


# === Mode demandé ===

def resolve_ranking_mode(value: Optional[str] = None) -> str:
    """
    Classeur à appliquer.

    Args:
        value: ``native``, ``local`` ou ``off`` ; None : défaut de
            ``config.RANKING_DEFAULT`` (variable ``MOISSON_RANKING``), ramené
            à ``native`` s'il est invalide (avertissement).

    Raises:
        ValueError: valeur explicite inconnue.
    """
    if value is None:
        import config
        default = str(getattr(config, "RANKING_DEFAULT", RANKING_NATIVE)
                      or RANKING_NATIVE).strip().lower()
        if default not in RANKING_MODES:
            logger.warning(f"MOISSON_RANKING={default!r} inconnu : classement "
                           f"'{RANKING_NATIVE}' appliqué (attendu : "
                           f"{', '.join(RANKING_MODES)})")
            return RANKING_NATIVE
        return default
    mode = str(value).strip().lower()
    if mode not in RANKING_MODES:
        raise ValueError(f"classement {value!r} inconnu (attendu : "
                         f"{', '.join(RANKING_MODES)})")
    return mode


# === Normalisation du texte ===

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS_RAW = """
le la les l de des du d un une et ou en au aux a pour par sur sous dans avec
sans ce ces cet cette se sa son ses leur leurs qui que quoi dont est sont il
elle ils elles on nous vous je tu y ne pas plus entre vers chez comme mais
donc ni car the an of and or in on at to for by with from as is are be this
that these those its it into about
""".split()


def _stem(token: str) -> str:
    """Pluriel final retiré (s, x) sur les mots de plus de 3 lettres."""
    if len(token) > 3 and token[-1] in "sx":
        return token[:-1]
    return token


def _raw_tokens(text: str) -> List[str]:
    """Mots normalisés, pluriel final conservé (préfixe d'une troncature)."""
    return [t for t in _TOKEN_RE.findall(unidecode(text).lower()) if len(t) > 1]


def tokens(text: Any) -> List[str]:
    """
    Mots normalisés d'un texte : minuscules, accents translittérés, élisions
    et mots d'une lettre retirés, pluriel final s/x retiré. Les mots vides ne
    sont PAS retirés (les expressions exactes les contiennent).
    """
    if not text:
        return []
    if not isinstance(text, str):
        text = str(text)
    return [_stem(t) for t in _raw_tokens(text)]


STOPWORDS = frozenset(_stem(w) for w in _STOPWORDS_RAW if len(w) > 1)


# === Concepts du plan de requête ===

EXPR_TERM = "term"        # un mot
EXPR_PREFIX = "prefix"    # troncature finale : mot commençant par le préfixe
EXPR_PHRASE = "phrase"    # suite de mots contigus (expression exacte)
EXPR_NEAR = "near"        # proximité : tous les mots dans une fenêtre
EXPR_ALL = "all"          # tous les mots, dans n'importe quel ordre

# Marque de troncature d'un mot normalisé (« numeri* ») : un mot normalisé
# ne contient jamais « * », la marque est donc sans ambiguïté.
PREFIX_MARK = "*"

# Champs Solr dont la valeur est du texte cherché dans le titre, les
# mots-clés ou le résumé (``title_t``, ``fr_title_s``, ``subTitle_t``,
# ``abstract_t``, ``keyword_s``, ``text``…). Tout autre champ (auteur,
# identifiants, dates, type, langue, revue ``journalTitle_s``…) n'est pas
# évalué localement.
_TEXT_FIELD_RE = re.compile(
    r"^(?:[a-z]{2}_)?(?:sub)?(?:title|abstract|keywords?|subject|text)"
    r"(?:[._][a-z0-9]+)*$", re.IGNORECASE)
# Proximité Solr collée à une expression (« "a b"~5 »).
_PROXIMITY_RE = re.compile(r"^~(\d+)(?:\.\d+)?$")


@dataclass(frozen=True)
class Expression:
    """Forme cherchée d'un concept : mots normalisés et type d'appariement
    (``slop`` : proximité d'une forme ``near``)."""

    tokens: Tuple[str, ...]
    kind: str
    text: str
    slop: int = 0


@dataclass(frozen=True)
class Concept:
    """Concept de la requête : une ou plusieurs formes (alternatives)."""

    label: str
    expressions: Tuple[Expression, ...]
    weight: float = 1.0
    implicit: bool = False


@dataclass(frozen=True)
class ConceptSet:
    """Concepts d'une version de la requête (origine ou variante)."""

    version: str
    query: str
    concepts: Tuple[Concept, ...]
    excluded: Tuple[Expression, ...]
    # Parties de la requête non évaluables localement (champ non textuel,
    # joker interne ou initial, recherche approchée) : ni appariées ni
    # déclarées absentes.
    unevaluated: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "query": self.query,
            "concepts": [{"label": c.label, "weight": c.weight,
                          "implicit": c.implicit,
                          "forms": [{"text": e.text, "kind": e.kind,
                                     **({"slop": e.slop} if e.kind == EXPR_NEAR else {})}
                                    for e in c.expressions]}
                         for c in self.concepts],
            "excluded": [e.text for e in self.excluded],
            "unevaluated": list(self.unevaluated),
        }


def _is_prefix(token: str) -> bool:
    return token.endswith(PREFIX_MARK)


def _token_matches(tok: str, pattern: str) -> bool:
    """Mot de la notice apparié à un mot de la requête. Troncature : le mot
    commence par le préfixe, ou le préfixe est le mot entier dont le pluriel
    final a été retiré (« process* » et le mot « process », normalisé
    « proces »)."""
    if pattern.endswith(PREFIX_MARK):
        stem = pattern[:-1]
        return tok.startswith(stem) or tok == _stem(stem)
    return tok == pattern


def _in_field(present: set, pattern: str) -> bool:
    if not pattern.endswith(PREFIX_MARK):
        return pattern in present
    return any(_token_matches(t, pattern) for t in present)


def _term_tokens(text: str) -> Optional[Tuple[str, ...]]:
    """
    Mots normalisés d'un terme de la requête, troncature finale marquée
    (« numéri* » → ``("numeri*",)``). None si le terme porte un opérateur
    non évaluable localement : joker interne ou initial (« *ique »,
    « num?rique »), recherche approchée (« discours~1 »), troncature sans
    préfixe d'au moins deux lettres (« d* »).
    """
    raw = text.strip()
    body = raw.rstrip("*")
    if "*" in body or "?" in body or "~" in body:
        return None
    if len(body) == len(raw):
        return tuple(tokens(body))
    if not body or not body[-1].isalnum():
        return None
    raw_toks = _raw_tokens(body)
    runs = _TOKEN_RE.findall(unidecode(body).lower())
    if not raw_toks or not runs or runs[-1] != raw_toks[-1]:
        return None
    return tuple(_stem(t) for t in raw_toks[:-1]) + (raw_toks[-1] + PREFIX_MARK,)


def _make_expression(toks: Sequence[str], kind: str, text: str,
                     slop: int = 0) -> Optional[Expression]:
    toks = tuple(toks)
    if not toks:
        return None
    if len(toks) == 1:
        if _is_prefix(toks[0]):
            kind = EXPR_PREFIX
        elif kind in (EXPR_ALL, EXPR_TERM):
            kind = EXPR_TERM
        elif kind == EXPR_NEAR:
            kind = EXPR_PHRASE
    elif kind == EXPR_TERM:
        kind = EXPR_PHRASE        # mot composé (« socio-économique »)
    return Expression(tokens=toks, kind=kind, text=text,
                      slop=slop if kind == EXPR_NEAR else 0)


def _text_field(name: str) -> bool:
    return bool(_TEXT_FIELD_RE.match(name or ""))


def _raw_label(node) -> str:
    """Partie de requête telle qu'écrite (libellé des parties non évaluées)."""
    from utils.query_plan import And, FieldQuery, Not, Or, Phrase, Range, Term

    if isinstance(node, Term):
        return node.text
    if isinstance(node, Phrase):
        return f"« {node.text} »{node.suffix or ''}"
    if isinstance(node, Range):
        return node.text
    if isinstance(node, FieldQuery):
        return f"{node.name}:{_raw_label(node.value)}"
    if isinstance(node, (Or, And)):
        sep = " OU " if isinstance(node, Or) else " "
        return "(" + sep.join(_raw_label(c) for c in node.children
                              if not isinstance(c, Not)) + ")"
    if isinstance(node, Not):
        return "NON " + _raw_label(node.child)
    return ""


def _node_tokens(node) -> Optional[List[str]]:
    """Mots normalisés d'un sous-arbre positif (conjonction dans une
    alternative) ; None si une partie n'est pas évaluable localement."""
    from utils.query_plan import And, FieldQuery, Not, Or, Phrase, Range, Term

    if isinstance(node, Term):
        toks = _term_tokens(node.text)
        return None if toks is None else list(toks)
    if isinstance(node, Phrase):
        return list(tokens(node.text))
    if isinstance(node, FieldQuery):
        return _node_tokens(node.value) if _text_field(node.name) else None
    if isinstance(node, Range):
        return None
    if isinstance(node, (And, Or)):
        out: List[str] = []
        for child in node.children:
            if isinstance(child, Not):
                continue
            sub = _node_tokens(child)
            if sub is None:
                return None
            out.extend(sub)
        return out
    return []


def _forms(node) -> Tuple[List[Expression], List[str]]:
    """
    Formes positives d'un nœud (une alternative = une forme) et parties non
    évaluables localement. Une alternative dont une branche n'est pas
    évaluable ne l'est pas du tout : la notice pourrait l'apparier par cette
    branche, elle n'est donc ni appariée ni absente.
    """
    from utils.query_plan import And, FieldQuery, Not, Or, Phrase, Range, Term

    if isinstance(node, Term):
        toks = _term_tokens(node.text)
        if toks is None:
            return [], [_raw_label(node)]
        expr = _make_expression(toks, EXPR_TERM, node.text)
        return ([expr] if expr else []), []
    if isinstance(node, Phrase):
        kind, slop = EXPR_PHRASE, 0
        m = _PROXIMITY_RE.match(node.suffix or "")
        if m and int(m.group(1)) > 0:
            kind, slop = EXPR_NEAR, int(m.group(1))
        text = node.text + (node.suffix if kind == EXPR_NEAR else "")
        expr = _make_expression(tokens(node.text), kind, text, slop)
        return ([expr] if expr else []), []
    if isinstance(node, FieldQuery):
        if not _text_field(node.name):
            return [], [_raw_label(node)]
        return _forms(node.value)
    if isinstance(node, Range):
        return [], [_raw_label(node)]
    if isinstance(node, Or):
        out: List[Expression] = []
        for child in node.children:
            if isinstance(child, Not):
                continue
            forms, skipped = _forms(child)
            if skipped:
                return [], [_raw_label(node)]
            for expr in forms:
                if expr not in out:
                    out.append(expr)
        return out, []
    if isinstance(node, And):
        # Conjonction dans une alternative : tous ses mots.
        toks = _node_tokens(node)
        if toks is None:
            return [], [_raw_label(node)]
        expr = _make_expression(toks, EXPR_ALL, " ".join(_node_words(node)))
        return ([expr] if expr else []), []
    return [], []   # exclusion : traitée par l'appelant


def _node_words(node) -> List[str]:
    from utils.query_plan import FieldQuery, Not, Phrase, Term, And, Or

    out: List[str] = []
    if isinstance(node, Term):
        out.append(node.text)
    elif isinstance(node, Phrase):
        out.append(node.text)
    elif isinstance(node, FieldQuery):
        out.extend(_node_words(node.value))
    elif isinstance(node, (And, Or)):
        for child in node.children:
            if not isinstance(child, Not):
                out.extend(_node_words(child))
    return out


def _node_expressions(node) -> List[Expression]:
    """Formes positives d'un nœud (une alternative = une forme) ; aucune si
    une partie n'est pas évaluable localement (voir ``_forms``)."""
    forms, skipped = _forms(node)
    return [] if skipped else forms


def _node_label(node) -> str:
    from utils.query_plan import And, FieldQuery, Or, Phrase, Term

    if isinstance(node, Term):
        return node.text
    if isinstance(node, Phrase):
        suffix = node.suffix if _PROXIMITY_RE.match(node.suffix or "") else ""
        return f"« {node.text} »{suffix}"
    if isinstance(node, FieldQuery):
        return _node_label(node.value)
    if isinstance(node, Or):
        return " OU ".join(_node_label(c) for c in node.children
                           if _node_expressions(c))
    if isinstance(node, And):
        return " ".join(_node_words(node))
    return ""


def _flatten_top(node) -> List[Any]:
    """Éléments de la conjonction de tête (groupes et champs textuels
    dépliés ; un champ non textuel reste un seul élément)."""
    from utils.query_plan import And, FieldQuery

    if isinstance(node, And):
        out: List[Any] = []
        for child in node.children:
            out.extend(_flatten_top(child))
        return out
    if (isinstance(node, FieldQuery) and isinstance(node.value, And)
            and _text_field(node.name)):
        return _flatten_top(node.value)
    return [node]


def _is_stopword_expr(expr: Expression) -> bool:
    return all(t.rstrip(PREFIX_MARK) in STOPWORDS for t in expr.tokens)


def _implicit_phrase(parsed) -> Optional[Expression]:
    """Expression complète d'une requête de mots seuls (troncatures
    comprises), None si un mot n'est pas évaluable localement."""
    from utils.query_plan import Term

    words = _flatten_top(parsed.tree)
    if not words or not all(isinstance(w, Term) for w in words):
        return None
    parts = [_term_tokens(w.text) for w in words]
    if any(p is None for p in parts):
        return None
    toks = tuple(t for p in parts for t in p)
    if not toks:
        return None
    return Expression(tokens=toks, kind=EXPR_PHRASE, text=parsed.text)


def concepts_from_parsed(parsed, version: str = "original") -> ConceptSet:
    """
    Concepts d'une requête analysée (``utils.query_plan.ParsedQuery``).

    Chaque élément de la conjonction de tête est un concept ; une
    alternative (OR) en donne les formes ; une expression exacte se cherche
    mot à mot contiguë, une proximité (« "a b"~N ») dans une fenêtre de
    ``len + N`` mots ; une troncature finale (« numéri* ») par préfixe ; une
    exclusion (NOT) va dans ``excluded``. Un mot vide isolé (« de »,
    « the ») n'est pas un concept, sauf si la requête n'a rien d'autre. Une
    requête de mots seuls d'au moins deux mots pleins est aussi cherchée
    comme expression complète (demi-concept implicite). Les parties non
    évaluables localement (champ non textuel, joker interne ou initial,
    recherche approchée) vont dans ``unevaluated`` : ni concepts, ni
    exclusions.
    """
    from utils.query_plan import Not

    concepts: List[Concept] = []
    stop_only: List[Concept] = []
    excluded: List[Expression] = []
    unevaluated: List[str] = []
    for node in _flatten_top(parsed.tree):
        if isinstance(node, Not):
            forms, skipped = _forms(node.child)
            if skipped:
                label = "NON " + _raw_label(node.child)
                if label not in unevaluated:
                    unevaluated.append(label)
                continue
            for expr in forms:
                if expr not in excluded:
                    excluded.append(expr)
            continue
        forms, skipped = _forms(node)
        if skipped:
            label = _raw_label(node)
            if label not in unevaluated:
                unevaluated.append(label)
            continue
        exprs = tuple(forms)
        if not exprs:
            continue
        concept = Concept(label=_node_label(node) or exprs[0].text,
                          expressions=exprs)
        if all(_is_stopword_expr(e) for e in exprs):
            stop_only.append(concept)
            continue
        if concept not in concepts:
            concepts.append(concept)
    if not concepts:
        concepts = stop_only
    if parsed.simple:
        full = _implicit_phrase(parsed)
        content = [t for t in (full.tokens if full else ())
                   if t.rstrip(PREFIX_MARK) not in STOPWORDS]
        if full is not None and len(content) >= 2:
            concepts.append(Concept(label=f"« {parsed.text} »",
                                    expressions=(full,),
                                    weight=IMPLICIT_PHRASE_WEIGHT,
                                    implicit=True))
    return ConceptSet(version=version, query=parsed.text,
                      concepts=tuple(concepts), excluded=tuple(excluded),
                      unevaluated=tuple(unevaluated))


def build_concepts(plan=None, query: Optional[str] = None,
                   query_variants: Optional[Dict[str, str]] = None
                   ) -> List[ConceptSet]:
    """
    Concepts de chaque version de la requête : d'après le plan de requête
    (``utils.query_plan.QueryPlan``, analyse déjà faite) ou, à défaut,
    d'après la requête et ses variantes explicites.
    """
    if plan is not None and getattr(plan, "parsed", None):
        items = list(plan.parsed.items())
    else:
        from utils.query_plan import normalize_variants, parse_query

        if not query:
            return []
        items = [("original", parse_query(query))]
        for lang, text in normalize_variants(query_variants or {}).items():
            items.append((lang, parse_query(text, label=f"variante '{lang}'")))
    return [concepts_from_parsed(parsed, version=name) for name, parsed in items]


# === Champs d'une notice ===

class _Fields:
    """Mots normalisés d'une notice, par champ (calculés une fois)."""

    __slots__ = ("seqs", "sets", "counts", "lengths", "abstract_weight")

    def __init__(self, item: Dict[str, Any], snippet: bool):
        title = tokens(item.get("title"))
        tag_seq: List[str] = []
        for tag in item.get("tags") or []:
            value = tag.get("tag") if isinstance(tag, dict) else tag
            toks = tokens(value)
            if toks:
                if tag_seq:
                    tag_seq.append("|")   # pas d'expression à cheval sur deux mots-clés
                tag_seq.extend(toks)
        abstract = tokens(item.get("abstractNote"))
        self.seqs = {FIELD_TITLE: title, FIELD_TAGS: tag_seq,
                     FIELD_ABSTRACT: abstract}
        self.sets = {f: set(s) for f, s in self.seqs.items()}
        self.counts: Optional[Dict[str, Counter]] = None
        self.lengths = {f: sum(1 for t in s if t != "|")
                        for f, s in self.seqs.items()}
        self.abstract_weight = SNIPPET_WEIGHT if snippet else FIELD_WEIGHTS[FIELD_ABSTRACT]

    def weight(self, field: str) -> float:
        return self.abstract_weight if field == FIELD_ABSTRACT else FIELD_WEIGHTS[field]

    def field_counts(self) -> Dict[str, Counter]:
        if self.counts is None:
            self.counts = {f: Counter(s) for f, s in self.seqs.items()}
        return self.counts

    def matches(self, expr: Expression, field: str) -> bool:
        present = self.sets[field]
        if expr.kind == EXPR_TERM:
            return expr.tokens[0] in present
        if not all(_in_field(present, t) for t in expr.tokens):
            return False
        if expr.kind in (EXPR_ALL, EXPR_PREFIX):
            return True
        if expr.kind == EXPR_NEAR:
            return _within_window(self.seqs[field], expr.tokens,
                                  len(expr.tokens) + expr.slop)
        return _contains_sequence(self.seqs[field], expr.tokens)


def _contains_sequence(seq: List[str], sub: Tuple[str, ...]) -> bool:
    n = len(sub)
    if not any(_is_prefix(t) for t in sub):
        if n == 1:
            return sub[0] in seq
        first = sub[0]
        for i in range(len(seq) - n + 1):
            if seq[i] == first and tuple(seq[i:i + n]) == sub:
                return True
        return False
    for i in range(len(seq) - n + 1):
        if all(_token_matches(seq[i + k], sub[k]) for k in range(n)):
            return True
    return False


def _within_window(seq: List[str], sub: Tuple[str, ...], width: int) -> bool:
    """
    Proximité : tous les mots de ``sub`` (avec leurs répétitions) dans une
    fenêtre de ``width`` mots consécutifs, dans n'importe quel ordre, sans
    franchir la frontière de deux mots-clés (« | »). Approximation de la
    proximité Solr (``"a b"~N``) : ordre libre, sans coût de permutation.
    """
    need = Counter(sub)
    for i, tok in enumerate(seq):
        if tok not in need:
            continue
        window: Counter = Counter()
        for t in seq[i:i + width]:
            if t == "|":
                break
            window[t] += 1
        if all(window[t] >= c for t, c in need.items()):
            return True
    return False


# === Modèles lexicaux ===

def _simple_score(cs: ConceptSet, fields: _Fields
                  ) -> Tuple[float, List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    """
    Pondération lexicale simple : ``Σ w_c · poids du meilleur champ apparié
    / Σ w_c · poids du titre``, ×``EXCLUDED_PENALTY`` si un terme exclu
    figure dans le titre ou les mots-clés.

    Returns:
        ``(score, concepts appariés, concepts absents, termes exclus présents)``.
    """
    num = 0.0
    den = 0.0
    matched: List[Dict[str, Any]] = []
    missing: List[str] = []
    top = FIELD_WEIGHTS[FIELD_TITLE]
    for concept in cs.concepts:
        den += concept.weight * top
        hit_fields = [f for f in FIELDS
                      if any(fields.matches(e, f) for e in concept.expressions)]
        if hit_fields:
            best = max(fields.weight(f) for f in hit_fields)
            num += concept.weight * best
            matched.append({"concept": concept.label,
                            "fields": [FIELD_LABELS[f] for f in hit_fields],
                            "implicit": concept.implicit})
        elif not concept.implicit:
            missing.append(concept.label)
    score = num / den if den else 0.0
    excluded_hits: List[Dict[str, Any]] = []
    penalize = False
    for expr in cs.excluded:
        hit_fields = [f for f in FIELDS if fields.matches(expr, f)]
        if hit_fields:
            excluded_hits.append({"term": expr.text,
                                  "fields": [FIELD_LABELS[f] for f in hit_fields]})
            if FIELD_TITLE in hit_fields or FIELD_TAGS in hit_fields:
                penalize = True
    if penalize:
        score *= EXCLUDED_PENALTY
    return score, matched, missing, excluded_hits


def _bm25_terms(cs: ConceptSet) -> List[str]:
    terms = sorted({t for c in cs.concepts if not c.implicit
                    for e in c.expressions for t in e.tokens
                    if t.rstrip(PREFIX_MARK) not in STOPWORDS})
    if not terms:
        terms = sorted({t for c in cs.concepts for e in c.expressions
                        for t in e.tokens})
    return terms


def _term_count(counter: Counter, term: str) -> int:
    """Occurrences d'un terme (troncature : tous les mots qu'elle apparie)."""
    if not term.endswith(PREFIX_MARK):
        return counter.get(term, 0)
    return sum(c for tok, c in counter.items() if _token_matches(tok, term))


def _bm25_scores(concept_sets: List[ConceptSet], fields: List[_Fields],
                 check: Optional[Callable[[], None]] = None
                 ) -> Tuple[List[float], List[str]]:
    """
    BM25 à champs pondérés, meilleure version de la requête par notice.

    Returns:
        ``(scores, version retenue)`` par notice (même ordre que ``fields``).
    """
    n = len(fields)
    if not n or not concept_sets:
        return [0.0] * n, ["original"] * n
    per_version = [(cs.version, _bm25_terms(cs)) for cs in concept_sets]
    all_terms = sorted({t for _v, terms in per_version for t in terms})
    tf: List[Dict[str, float]] = []
    lengths: List[float] = []
    for i, f in enumerate(fields):
        if check is not None and i % _CHECK_EVERY == 0:
            check()
        counts = f.field_counts()
        weights = {name: f.weight(name) for name in FIELDS}
        tf.append({t: math.fsum(weights[name] * _term_count(counts[name], t)
                                for name in FIELDS)
                   for t in all_terms})
        lengths.append(math.fsum(weights[name] * f.lengths[name] for name in FIELDS))
    avgdl = math.fsum(lengths) / n if n else 0.0
    df = {t: sum(1 for d in tf if d[t] > 0) for t in all_terms}
    idf = {t: math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in all_terms}
    scores: List[float] = []
    versions: List[str] = []
    for i in range(n):
        norm = BM25_K1 * (1.0 - BM25_B + BM25_B * (lengths[i] / avgdl if avgdl else 0.0))
        best, best_version = 0.0, per_version[0][0]
        for version, terms in per_version:
            value = math.fsum(idf[t] * tf[i][t] * (BM25_K1 + 1.0) / (tf[i][t] + norm)
                              for t in terms if tf[i][t] > 0)
            if value > best:
                best, best_version = value, version
        scores.append(best)
        versions.append(best_version)
    return scores, versions


# === Appariement lexical exposé (consultation locale, OPT-17) ===
#
# Même normalisation et mêmes formes que le classement local (casse, accents,
# élisions, pluriels, expressions exactes, troncature, proximité), exposées
# pour le digest des outils producteurs et l'outil ``corpus_query``
# (``utils.corpus_query``) : aucune règle n'est modifiée, d'où une
# ``RANKING_VERSION`` inchangée.

def match_notice(concept_sets: Sequence[ConceptSet], item: Dict[str, Any],
                 snippet: Optional[bool] = None) -> Dict[str, Any]:
    """
    Appariement d'une notice aux concepts d'une requête : pondération
    lexicale ``simple`` (titre 3, mots-clés 2, résumé 1 ; extrait Scholar
    0,5), meilleure version de la requête.

    Args:
        concept_sets: concepts de chaque version de la requête
            (``build_concepts``).
        item: notice Zotero.
        snippet: le résumé est un extrait Google Scholar ; None : détecté
            (``utils.deduplication.abstract_is_scholar_snippet``).

    Returns:
        ``score`` (0 à 1), ``matched`` (concepts appariés et leurs champs),
        ``missing`` (concepts absents), ``excluded`` (termes exclus présents),
        ``version`` et ``unevaluated`` (parties non évaluées localement).
    """
    if snippet is None:
        from utils.deduplication import abstract_is_scholar_snippet
        snippet = abstract_is_scholar_snippet(item)
    fields = _Fields(item, bool(snippet))
    best = None
    for cs in concept_sets:
        score, matched, missing, excluded = _simple_score(cs, fields)
        if best is None or score > best[0]:
            best = (score, matched, missing, excluded, cs)
    if best is None:
        return {"score": 0.0, "matched": [], "missing": [], "excluded": [],
                "version": None, "unevaluated": []}
    return {"score": best[0], "matched": best[1], "missing": best[2],
            "excluded": best[3], "version": best[4].version,
            "unevaluated": list(best[4].unevaluated)}


def citation_count(item: Dict[str, Any]) -> Optional[int]:
    """Nombre de citations connu d'une notice (maximum des lignes ``Cited
    by`` d'``extra``), None si inconnu : dimension séparée, jamais dans un
    score de classement."""
    return _citations(item)


# === Identité d'une notice ===

def record_id(item: Dict[str, Any], keys: Optional[Sequence[str]] = None) -> str:
    """
    Identifiant stable d'une notice : clé forte prioritaire
    (``doi:`` > ``isbn:`` > ``hal:`` > ``arxiv:`` > identifiants natifs >
    ``url:``), à défaut ``fp:`` + empreinte (titre normalisé, année, nom du
    premier auteur).
    """
    if keys is None:
        keys = [k for k, _derived in strong_key_sources(item)]
    best: Optional[Tuple[int, str]] = None
    for key in keys:
        kind = key.split(":", 1)[0]
        order = _ID_KIND_ORDER.index(kind) if kind in _ID_KIND_ORDER else len(_ID_KIND_ORDER)
        candidate = (order, key)
        if best is None or candidate < best:
            best = candidate
    if best is not None:
        return best[1]
    return "fp:" + _fingerprint(item)


def _first_author(item: Dict[str, Any]) -> str:
    for c in item.get("creators") or []:
        if isinstance(c, dict):
            name = c.get("lastName") or c.get("name") or ""
            toks = tokens(name)
            if toks:
                return toks[-1] if not c.get("lastName") else " ".join(toks)
    return ""


def _fingerprint(item: Dict[str, Any]) -> str:
    raw = "|".join([" ".join(tokens(item.get("title"))),
                    extract_year(item.get("date") or "") or "",
                    _first_author(item)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _canonical(item: Dict[str, Any]) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)


# === Entrées : résultats par source ===

@dataclass
class _SourceInput:
    source: str
    label: str
    provider: str
    items: List[Dict[str, Any]]
    ranks: List[Optional[Dict[str, Any]]]
    sort: Optional[str]
    rank_class: str
    status: Optional[str]
    describe: str


@lru_cache(maxsize=None)
def provider_of(source_key: str) -> str:
    """Fournisseur d'une source : Isidore pour ses sous-sources, la source
    elle-même sinon (``connectors.capabilities``, champ ``proxy_of``)."""
    from connectors.capabilities import get_capabilities

    caps = get_capabilities(source_key)
    return (caps.proxy_of if caps is not None and caps.proxy_of else source_key)


def classify_sort(sort: Optional[str], has_ranks: bool = True) -> str:
    """Classe d'un ordre natif (``CLASS_*``) d'après le tri appliqué."""
    if not has_ranks:
        return CLASS_UNRANKED
    value = (sort or "").strip()
    if value in RELEVANCE_SORTS:
        return CLASS_RELEVANCE
    if value in PRESUMED_SORTS:
        return CLASS_PRESUMED
    if not value:
        return CLASS_UNRANKED
    return CLASS_OTHER


def is_dimension_sort(sort: Optional[str]) -> bool:
    """
    Ordre natif qui trie selon une dimension séparée : citations
    (``citationCount:desc`` de Semantic Scholar, ``cited_by_count``,
    ``is-referenced-by-count``) ou date (``publicationDate:desc``,
    ``published``, ``updated``…). Il n'ordonne jamais la queue du
    classement natif : sinon les citations ou la récence décideraient du
    rang.
    """
    return bool(sort) and bool(_DIMENSION_SORT_RE.search(str(sort)))


def _short_status(status: Optional[str], reason: Optional[str]) -> str:
    """Statut et motif d'une source, en clair et sans détail (réserve
    compacte : « partielle : fenêtre de résultats du fournisseur atteinte »)."""
    from engine.models import REASON_LABELS, STATUS_LABELS

    text = STATUS_LABELS.get(status or "", status or "")
    if reason:
        text += " : " + REASON_LABELS.get(reason, reason)
    return text


def _source_inputs(sources: Sequence[Any]) -> List[_SourceInput]:
    """
    Entrées normalisées : ``SourceResult`` (moteur) ou dictionnaire
    ``{"source", "items", "ranks"?, "sort"?, "status"?}`` (évaluation,
    corpus relus).
    """
    out: List[_SourceInput] = []
    for src in sources or []:
        if isinstance(src, dict):
            key = canonical_source_key(src.get("source") or "")
            items = list(src.get("items") or [])
            ranks = list(src.get("ranks") or [])
            sort = src.get("sort")
            status = src.get("status")
            label = src.get("label") or display_name(key)
            describe = src.get("describe") or (status or "")
        else:
            key = canonical_source_key(getattr(src, "source", "") or "")
            items = list(getattr(src, "items", None) or [])
            aligned = getattr(src, "aligned_ranks", None)
            if callable(aligned):
                ranks = list(aligned() or [])
            else:
                ranks = list(getattr(src, "native_ranks", None) or [])
            sort = getattr(src, "native_sort", None)
            status = getattr(src, "status", None)
            label = getattr(src, "display_name", None) or display_name(key)
            describe = _short_status(status, getattr(src, "reason", None))
        if len(ranks) != len(items):
            ranks = []
        has_ranks = bool(ranks)
        rank_class = classify_sort(sort, has_ranks)
        if not has_ranks:
            # Aucun rang publié : la position dans la liste tient lieu
            # d'ordre natif (classe sans poids).
            ranks = [{"rank": i + 1, "score": None, "sort": None}
                     for i in range(len(items))]
        out.append(_SourceInput(source=key, label=label, provider=provider_of(key),
                                items=items, ranks=ranks, sort=sort,
                                rank_class=rank_class, status=status,
                                describe=describe))
    return out


# === Rattachement des notices de source aux notices finales ===

@dataclass
class _Mapping:
    # (indice de source, position) -> indice de la notice finale
    assigned: Dict[Tuple[int, int], int]
    # indice de notice finale -> nombre de rangs non rattachés qui la visaient
    uncertain: Dict[int, int]
    stats: Dict[str, Any]


def map_source_items(records: Sequence[Dict[str, Any]],
                     inputs: Sequence[_SourceInput],
                     groups: Optional[Sequence[Sequence[int]]] = None,
                     record_keys: Optional[List[List[Tuple[str, bool]]]] = None,
                     check: Optional[Callable[[], None]] = None) -> _Mapping:
    """
    Rattache chaque notice de source à sa notice finale.

    Ordre des preuves : groupes de la déduplication (indices dans la
    concaténation des notices des sources, dans l'ordre de ``inputs``) ;
    sinon identité d'objet (notice rendue telle quelle par la
    déduplication) ; puis clés fortes partagées avec une notice fusionnée
    (déclarées : poids 2, dérivées d'une URL : poids 1 ; à égalité, la
    notice qui a le moins de clés en plus) ; puis empreinte titre/année
    unique. Sinon, rang non rattaché (les notices candidates en portent la
    réserve).
    """
    assigned: Dict[Tuple[int, int], int] = {}
    uncertain: Dict[int, int] = {}
    stats = {"method": "groups" if groups is not None else "identity+keys",
             "source_items": sum(len(s.items) for s in inputs),
             "identity": 0, "groups": 0, "keys": 0, "fingerprint": 0,
             "title": 0, "ambiguous": 0, "unmapped": 0}
    if groups is not None:
        flat: List[Tuple[int, int]] = [(si, pos) for si, s in enumerate(inputs)
                                       for pos in range(len(s.items))]
        for ri, members in enumerate(groups):
            for m in members:
                if 0 <= m < len(flat):
                    assigned[flat[m]] = ri
                    stats["groups"] += 1
        stats["unmapped"] = stats["source_items"] - stats["groups"]
        return _Mapping(assigned, uncertain, stats)

    by_object = {id(rec): ri for ri, rec in enumerate(records)}
    source_ids = set()
    residual_items: List[Tuple[int, int]] = []
    for si, s in enumerate(inputs):
        for pos, item in enumerate(s.items):
            source_ids.add(id(item))
            ri = by_object.get(id(item))
            if ri is not None:
                assigned[(si, pos)] = ri
                stats["identity"] += 1
            else:
                residual_items.append((si, pos))
    if not residual_items:
        return _Mapping(assigned, uncertain, stats)

    # Notices finales qui ne sont pas des notices de source (fusions, copies
    # annotées) : seules candidates des notices restantes.
    residual_records = [ri for ri, rec in enumerate(records)
                        if id(rec) not in source_ids]
    key_index: Dict[str, List[int]] = {}
    keys_of: Dict[int, Dict[str, bool]] = {}
    fp_index: Dict[str, List[int]] = {}
    for n, ri in enumerate(residual_records):
        if check is not None and n % _CHECK_EVERY == 0:
            check()
        pairs = record_keys[ri] if record_keys is not None else strong_key_sources(records[ri])
        keys_of[ri] = dict(pairs)
        for key, _derived in pairs:
            key_index.setdefault(key, []).append(ri)
        fp_index.setdefault(_fingerprint(records[ri]), []).append(ri)

    for n, (si, pos) in enumerate(residual_items):
        if check is not None and n % _CHECK_EVERY == 0:
            check()
        item = inputs[si].items[pos]
        pairs = strong_key_sources(item)
        own = {k for k, _d in pairs}
        scores: Dict[int, float] = {}
        for key, derived in pairs:
            for ri in key_index.get(key, ()):
                scores[ri] = scores.get(ri, 0.0) + (1.0 if derived else 2.0)
        if scores:
            ranked = sorted(scores.items(),
                            key=lambda kv: (-kv[1], len(set(keys_of[kv[0]]) - own), kv[0]))
            best_ri, best_score = ranked[0]
            best_extra = len(set(keys_of[best_ri]) - own)
            ties = [ri for ri, sc in ranked
                    if sc == best_score and len(set(keys_of[ri]) - own) == best_extra]
            if len(ties) == 1:
                assigned[(si, pos)] = best_ri
                stats["keys"] += 1
                continue
            stats["ambiguous"] += 1
            for ri in ties:
                uncertain[ri] = uncertain.get(ri, 0) + 1
            continue
        candidates = fp_index.get(_fingerprint(item), [])
        if len(candidates) == 1:
            assigned[(si, pos)] = candidates[0]
            stats["fingerprint"] += 1
            continue
        if not candidates:
            candidates = _title_candidates(item, residual_records, records)
            if len(candidates) == 1:
                assigned[(si, pos)] = candidates[0]
                stats["title"] += 1
                continue
        if candidates:
            stats["ambiguous"] += 1
            for ri in candidates:
                uncertain[ri] = uncertain.get(ri, 0) + 1
        else:
            stats["unmapped"] += 1
    return _Mapping(assigned, uncertain, stats)


def _title_candidates(item: Dict[str, Any], residual_records: Sequence[int],
                      records: Sequence[Dict[str, Any]]) -> List[int]:
    """
    Dernier recours du rattachement (notice fusionnée par la passe floue,
    sans clé ni empreinte commune) : notices fusionnées dont la ligne
    ``Sources`` contient les sources de la notice, d'année compatible
    (± ``FUZZY_YEAR_TOLERANCE``) et de titre au moins aussi proche que le
    seuil de la passe floue (``best_title_ratio`` ≥ ``FUZZY_TITLE_THRESHOLD``,
    jamais abaissé). Rend les candidates de meilleure similarité (une seule :
    rattachement ; plusieurs : ambiguïté).
    """
    from utils.deduplication import (
        FUZZY_TITLE_THRESHOLD, FUZZY_YEAR_TOLERANCE, item_sources,
    )
    from utils.doi_resolver import best_title_ratio

    title = str(item.get("title") or "")
    if not title.strip():
        return []
    own_sources = item_sources(item)
    year = extract_year(item.get("date") or "")
    scored: List[Tuple[float, int]] = []
    for ri in residual_records:
        rec = records[ri]
        if own_sources and not own_sources <= item_sources(rec):
            continue
        rec_year = extract_year(rec.get("date") or "")
        if year and rec_year and abs(int(year) - int(rec_year)) > FUZZY_YEAR_TOLERANCE:
            continue
        ratio, _prefix = best_title_ratio(title, str(rec.get("title") or ""))
        if ratio >= FUZZY_TITLE_THRESHOLD:
            scored.append((ratio, ri))
    if not scored:
        return []
    best = max(r for r, _ri in scored)
    return sorted(ri for r, ri in scored if r == best)


# === Réserves ===

RESERVE_SCHOLAR_SNIPPET = "scholar_snippet"
RESERVE_NO_ABSTRACT = "no_abstract"
RESERVE_AUTHORS_TRUNCATED = "authors_truncated"
RESERVE_IDENTITY_AMBIGUOUS = "identity_ambiguous"
RESERVE_IDENTITY_FUZZY = "identity_fuzzy"
RESERVE_RANK_UNMAPPED = "rank_unmapped"
RESERVE_SOURCE_PARTIAL = "source_partial"
RESERVE_EXCLUDED_TERM = "excluded_term"
RESERVE_PRESUMED_ORDER = "presumed_order"
RESERVE_NO_RELEVANCE_RANK = "no_relevance_rank"
RESERVE_UNEVALUATED = "query_part_unevaluated"

RESERVE_LABELS = {
    RESERVE_SCHOLAR_SNIPPET: "extrait Google Scholar au lieu d'un résumé",
    RESERVE_NO_ABSTRACT: "résumé absent",
    RESERVE_AUTHORS_TRUNCATED: "auteurs ou revue tronqués par Google Scholar",
    RESERVE_IDENTITY_AMBIGUOUS: "identité ambiguë (notice tenue à part)",
    RESERVE_IDENTITY_FUZZY: "identité établie par appariement flou (sans "
                            "identifiant commun)",
    RESERVE_RANK_UNMAPPED: "rang natif non rattaché avec certitude "
                           "(identité incertaine)",
    RESERVE_SOURCE_PARTIAL: "source incomplète ou partielle",
    RESERVE_EXCLUDED_TERM: "terme exclu présent",
    RESERVE_PRESUMED_ORDER: "rang d'un ordre présumé de pertinence, non vérifié",
    RESERVE_NO_RELEVANCE_RANK: "aucun rang de pertinence natif (placée en "
                               "queue)",
    RESERVE_UNEVALUATED: "partie de la requête non évaluée localement (ni "
                         "appariée ni déclarée absente)",
}


def _queue_detail(evidence: Any) -> str:
    """Ordre de queue d'une notice sans rang pondéré, en clair."""
    evidence = list(evidence)
    if not evidence:
        return "aucun rang rattaché ; départage par identifiant"
    neutral = sorted({f"{e['label']} {e['sort'] or 'position'}"
                      for e in evidence if not e["dimension"]})
    ignored = sorted({f"{e['label']} {e['sort']}"
                      for e in evidence if e["dimension"]})
    parts = []
    if neutral:
        parts.append("ordre natif : " + ", ".join(neutral))
    if ignored:
        parts.append("ordre de citations ou de date non utilisé : "
                     + ", ".join(ignored))
    if not neutral:
        parts.append("départage par identifiant")
    return " ; ".join(parts)


def _reserve(code: str, detail: Optional[str] = None) -> Dict[str, str]:
    label = RESERVE_LABELS[code]
    return {"code": code, "label": f"{label} : {detail}" if detail else label}


def _citations(item: Dict[str, Any]) -> Optional[int]:
    best: Optional[int] = None
    for value in extra_values(item, "Cited by"):
        digits = re.sub(r"[^\d]", "", value or "")
        if digits:
            n = int(digits)
            best = n if best is None else max(best, n)
    return best


# === Classement ===

def _competition_ranks(values: Dict[int, float]) -> Dict[int, int]:
    """Rangs de compétition (ex aequo au même rang) des valeurs positives."""
    ordered = sorted(((i, v) for i, v in values.items() if v > 0),
                     key=lambda kv: (-kv[1], kv[0]))
    ranks: Dict[int, int] = {}
    previous = None
    rank = 0
    for position, (i, v) in enumerate(ordered, 1):
        if v != previous:
            rank, previous = position, v
        ranks[i] = rank
    return ranks


def _sort_indices(indices: List[int], key: Callable[[int], Tuple],
                  records: Sequence[Dict[str, Any]]) -> List[int]:
    """Tri total et indépendant de l'ordre d'entrée : clé, puis sérialisation
    canonique pour les seuls ex aequo complets."""
    ordered = sorted(indices, key=lambda i: (key(i), i))
    out: List[int] = []
    run: List[int] = []
    run_key = None
    for i in ordered:
        k = key(i)
        if run and k != run_key:
            out.extend(_break_ties(run, records))
            run = []
        run.append(i)
        run_key = k
    out.extend(_break_ties(run, records))
    return out


def _break_ties(run: List[int], records: Sequence[Dict[str, Any]]) -> List[int]:
    if len(run) < 2:
        return run
    return sorted(run, key=lambda i: _canonical(records[i]))


def _average_ranks(values: Dict[int, float], indices: Sequence[int]
                   ) -> Dict[int, float]:
    """Rangs moyens des valeurs décroissantes sur ``indices`` (ex aequo :
    moyenne de leurs positions ; valeur absente : 0)."""
    ordered = sorted(indices, key=lambda i: (-values.get(i, 0.0), i))
    ranks: Dict[int, float] = {}
    pos = 0
    while pos < len(ordered):
        value = values.get(ordered[pos], 0.0)
        end = pos
        while end + 1 < len(ordered) and values.get(ordered[end + 1], 0.0) == value:
            end += 1
        mean = (pos + end) / 2.0 + 1.0
        for k in range(pos, end + 1):
            ranks[ordered[k]] = mean
        pos = end + 1
    return ranks


def _spearman(a: Dict[int, float], b: Dict[int, float]
              ) -> Tuple[Optional[float], int]:
    """
    Corrélation de Spearman des scores de deux modèles lexicaux, à rangs
    moyens, calculée sur les seules notices appariées par au moins un des
    deux (score > 0) : le bloc des notices appariées par aucun, identique
    par construction dans les deux classements, n'y entre pas.

    Returns:
        ``(corrélation ou None, nombre de notices comparées)``.
    """
    union = sorted(i for i in set(a) | set(b)
                   if a.get(i, 0.0) > 0 or b.get(i, 0.0) > 0)
    m = len(union)
    if m < 2:
        return None, m
    ra = _average_ranks(a, union)
    rb = _average_ranks(b, union)
    da = [ra[i] for i in union]
    db = [rb[i] for i in union]
    ma = math.fsum(da) / m
    mb = math.fsum(db) / m
    cov = math.fsum((x - ma) * (y - mb) for x, y in zip(da, db))
    va = math.fsum((x - ma) ** 2 for x in da)
    vb = math.fsum((y - mb) ** 2 for y in db)
    if not va or not vb:
        return None, m
    return round(cov / math.sqrt(va * vb), 4), m


def rank_records(
    records: Sequence[Dict[str, Any]],
    sources: Sequence[Any],
    *,
    mode: str = RANKING_NATIVE,
    plan=None,
    query: Optional[str] = None,
    query_variants: Optional[Dict[str, str]] = None,
    lexical_model: str = DEFAULT_LEXICAL_MODEL,
    groups: Optional[Sequence[Sequence[int]]] = None,
    check: Optional[Callable[[], None]] = None,
    reference_year: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Classe les notices finales d'une moisson, sans les modifier.

    Args:
        records: notices finales (dédupliquées), dans l'ordre des fichiers
            de corpus ; ``index`` des entrées renvoie à cet ordre.
        sources: résultats par source (``SourceResult`` ou dictionnaires,
            voir ``_source_inputs``).
        mode: ``native`` ou ``local``.
        plan: plan de requête (concepts du mode local) ; à défaut ``query``
            et ``query_variants``.
        lexical_model: pondération du score local (``simple`` ou ``bm25`` ;
            les deux sont calculées et comparées).
        groups: groupes de la déduplication (``with_groups=True``), indices
            dans la concaténation des notices des sources.
        check: rappel d'annulation (appelé périodiquement ; peut lever).
        reference_year: année de référence de la récence (défaut : année
            courante ; consignée dans les paramètres).

    Returns:
        Dictionnaire sérialisable : ``version``, ``mode``, ``params``,
        ``sources``, ``notes``, ``local``, ``counts``, ``mapping``,
        ``duration_s`` et ``entries`` (toutes les notices, par rang
        croissant : ``index``, ``id``, ``rank``, ``score``, ``native``,
        ``local``, ``components``, ``dimensions``, ``justification``).
    """
    if mode not in (RANKING_NATIVE, RANKING_LOCAL):
        raise ValueError(f"classement {mode!r} non calculable (native ou local)")
    if lexical_model not in LEXICAL_MODELS:
        raise ValueError(f"pondération lexicale {lexical_model!r} inconnue "
                         f"(attendu : {', '.join(LEXICAL_MODELS)})")
    t0 = time.perf_counter()
    records = list(records or [])
    n = len(records)
    year_ref = int(reference_year or datetime.now().year)
    inputs = _source_inputs(sources)

    # Clés fortes des notices finales (identifiants, rattachement).
    record_keys: List[List[Tuple[str, bool]]] = []
    for i, rec in enumerate(records):
        if check is not None and i % _CHECK_EVERY == 0:
            check()
        record_keys.append(strong_key_sources(rec))
    ids = [record_id(rec, [k for k, _d in record_keys[i]])
           for i, rec in enumerate(records)]
    mapping = map_source_items(records, inputs, groups=groups,
                               record_keys=record_keys, check=check)
    t_map = time.perf_counter()

    # --- Preuves natives par notice ---
    # evidence[ri][source] = meilleure entrée de cette source pour la notice
    evidence: List[Dict[str, Dict[str, Any]]] = [dict() for _ in range(n)]
    for (si, pos), ri in mapping.assigned.items():
        if not 0 <= ri < n:
            continue
        s = inputs[si]
        rank_info = s.ranks[pos] or {}
        rank = int(rank_info.get("rank") or (pos + 1))
        entry = {"source": s.source, "label": s.label, "provider": s.provider,
                 "rank": rank, "score": rank_info.get("score"),
                 "sort": s.sort, "class": s.rank_class,
                 "weight": CLASS_WEIGHTS[s.rank_class],
                 "dimension": is_dimension_sort(s.sort)}
        current = evidence[ri].get(s.source)
        if current is None or rank < current["rank"]:
            evidence[ri][s.source] = entry

    native_scores: Dict[int, float] = {}
    contributions: List[List[Dict[str, Any]]] = []
    queue_position: Dict[int, int] = {}
    for ri in range(n):
        by_provider: Dict[str, Dict[str, Any]] = {}
        for entry in sorted(evidence[ri].values(),
                            key=lambda e: (e["provider"], e["rank"], e["source"])):
            best = by_provider.get(entry["provider"])
            if (best is None or entry["weight"] > best["weight"]
                    or (entry["weight"] == best["weight"]
                        and entry["rank"] < best["rank"])):
                by_provider[entry["provider"]] = entry
        contrib = []
        for provider in sorted(by_provider):
            entry = by_provider[provider]
            if entry["weight"] > 0:
                contrib.append({**entry,
                                "contribution": entry["weight"] / (RRF_K + entry["rank"])})
        contributions.append(contrib)
        score = math.fsum(c["contribution"] for c in contrib)
        native_scores[ri] = score
        # Position de queue : ordres neutres seulement (jamais un ordre de
        # citations ou de date, qui ferait décider une dimension séparée).
        neutral = [e["rank"] for e in evidence[ri].values() if not e["dimension"]]
        if neutral:
            queue_position[ri] = min(neutral)

    def native_key(ri: int) -> Tuple:
        score = native_scores[ri]
        if score > 0:
            return (0, -score, 0, ids[ri])
        return (1, 0.0, queue_position.get(ri, 10 ** 9), ids[ri])

    native_order = _sort_indices(list(range(n)), native_key, records)
    native_rank = {ri: pos for pos, ri in enumerate(native_order, 1)}
    native_competition = _competition_ranks(native_scores)
    t_native = time.perf_counter()

    # --- Réserves et dimensions (communes aux deux modes) ---
    from utils.deduplication import (
        abstract_is_scholar_snippet, authors_truncated, item_sources,
        parse_evidence,
    )

    status_of = {s.source: s for s in inputs}
    snippets: List[bool] = []
    reserves: List[List[Dict[str, str]]] = []
    dimensions: List[Dict[str, Any]] = []
    for ri, rec in enumerate(records):
        if check is not None and ri % _CHECK_EVERY == 0:
            check()
        snippet = abstract_is_scholar_snippet(rec)
        snippets.append(snippet)
        res: List[Dict[str, str]] = []
        if snippet:
            res.append(_reserve(RESERVE_SCHOLAR_SNIPPET))
        elif not str(rec.get("abstractNote") or "").strip():
            res.append(_reserve(RESERVE_NO_ABSTRACT))
        if authors_truncated(rec):
            res.append(_reserve(RESERVE_AUTHORS_TRUNCATED))
        ev = parse_evidence(rec)
        if ev and ev.get("ambiguous"):
            res.append(_reserve(RESERVE_IDENTITY_AMBIGUOUS, ev["ambiguous"]))
        if ev and ev.get("fuzzy"):
            res.append(_reserve(RESERVE_IDENTITY_FUZZY))
        if mapping.uncertain.get(ri):
            res.append(_reserve(RESERVE_RANK_UNMAPPED,
                                f"{mapping.uncertain[ri]} rang(s)"))
        source_keys = set(evidence[ri]) | set(item_sources(rec))
        partial = sorted(
            f"{status_of[k].label} ({status_of[k].describe})"
            for k in source_keys
            if k in status_of and status_of[k].status
            and status_of[k].status not in _SUCCESS_STATUSES)
        if partial:
            res.append(_reserve(RESERVE_SOURCE_PARTIAL, ", ".join(partial)))
        if any(c["class"] == CLASS_PRESUMED for c in contributions[ri]):
            res.append(_reserve(RESERVE_PRESUMED_ORDER, ", ".join(sorted(
                c["label"] for c in contributions[ri]
                if c["class"] == CLASS_PRESUMED))))
        if native_scores[ri] <= 0:
            res.append(_reserve(RESERVE_NO_RELEVANCE_RANK,
                                _queue_detail(evidence[ri].values())))
        reserves.append(res)
        providers = sorted({provider_of(k) for k in source_keys if k})
        year = extract_year(rec.get("date") or "")
        dimensions.append({
            "sources": {"independent": len(providers),
                        "providers": [display_name(p) for p in providers],
                        "sources": sorted(display_name(k) for k in source_keys if k)},
            "citations": _citations(rec),
            "year": int(year) if year else None,
            "age_years": (year_ref - int(year)) if year else None,
        })

    # --- Classement local ---
    local_info: Optional[Dict[str, Any]] = None
    local_scores: Dict[int, float] = {}
    local_rank: Dict[int, int] = {}
    lexical_detail: List[Dict[str, Any]] = [dict() for _ in range(n)]
    t_local_start = time.perf_counter()
    if mode == RANKING_LOCAL:
        concept_sets = build_concepts(plan=plan, query=query,
                                      query_variants=query_variants)
        fields = []
        for ri, rec in enumerate(records):
            if check is not None and ri % _CHECK_EVERY == 0:
                check()
            fields.append(_Fields(rec, snippets[ri]))
        unevaluated_of = {cs.version: cs.unevaluated for cs in concept_sets}
        simple_scores: Dict[int, float] = {}
        for ri in range(n):
            if check is not None and ri % _CHECK_EVERY == 0:
                check()
            best = None
            for cs in concept_sets:
                score, matched, missing, excluded = _simple_score(cs, fields[ri])
                if best is None or score > best[0]:
                    best = (score, matched, missing, excluded, cs.version)
            if best is None:
                best = (0.0, [], [], [], "original")
            simple_scores[ri] = best[0]
            lexical_detail[ri] = {"simple": best[0], "matched": best[1],
                                  "missing": best[2], "excluded": best[3],
                                  "version": best[4]}
            if best[3]:
                reserves[ri].append(_reserve(RESERVE_EXCLUDED_TERM, ", ".join(
                    f"{h['term']} ({', '.join(h['fields'])})" for h in best[3])))
            unevaluated = unevaluated_of.get(best[4]) or ()
            lexical_detail[ri]["unevaluated"] = list(unevaluated)
            if unevaluated:
                reserves[ri].append(_reserve(RESERVE_UNEVALUATED,
                                             ", ".join(unevaluated)))
        bm25_list, bm25_versions = _bm25_scores(concept_sets, fields, check)
        bm25_scores = {ri: bm25_list[ri] for ri in range(n)}
        for ri in range(n):
            lexical_detail[ri]["bm25"] = bm25_list[ri]
            lexical_detail[ri]["bm25_version"] = bm25_versions[ri]
        simple_rank = _competition_ranks(simple_scores)
        bm25_rank = _competition_ranks(bm25_scores)
        chosen = simple_rank if lexical_model == LEXICAL_SIMPLE else bm25_rank
        chosen_scores = (simple_scores if lexical_model == LEXICAL_SIMPLE
                         else bm25_scores)
        for ri in range(n):
            lex = chosen.get(ri)
            nat = native_competition.get(ri)
            local_scores[ri] = math.fsum([
                LOCAL_WEIGHTS["lexical"] / (RRF_K + lex) if lex else 0.0,
                LOCAL_WEIGHTS["native"] / (RRF_K + nat) if nat else 0.0,
            ])
            lexical_detail[ri]["simple_rank"] = simple_rank.get(ri)
            lexical_detail[ri]["bm25_rank"] = bm25_rank.get(ri)

        def local_key(ri: int) -> Tuple:
            # À score local égal, la preuve lexicale d'abord : une notice
            # sans appariement au rang natif r ne passe jamais devant une
            # notice appariée au rang lexical r par ses seules dimensions
            # séparées (fournisseurs indépendants, citations).
            cites = dimensions[ri]["citations"]
            return (-local_scores[ri], -chosen_scores[ri],
                    -dimensions[ri]["sources"]["independent"],
                    -(cites if cites is not None else -1), ids[ri])

        local_order = _sort_indices(list(range(n)), local_key, records)
        local_rank = {ri: pos for pos, ri in enumerate(local_order, 1)}
        top_simple = {ri for ri, _r in sorted(simple_rank.items(),
                                              key=lambda kv: (kv[1], ids[kv[0]]))[:COMPARISON_TOP]}
        top_bm25 = {ri for ri, _r in sorted(bm25_rank.items(),
                                            key=lambda kv: (kv[1], ids[kv[0]]))[:COMPARISON_TOP]}
        compared = max(len(top_simple), len(top_bm25))
        spearman, spearman_records = _spearman(simple_scores, bm25_scores)
        local_info = {
            "lexical_model": lexical_model,
            "concepts": [cs.as_dict() for cs in concept_sets],
            "comparison": {
                "top": COMPARISON_TOP,
                # Part commune des deux têtes de classement lexical (None si
                # aucune notice n'apparie la requête).
                "top_overlap": (round(len(top_simple & top_bm25) / compared, 4)
                                if compared else None),
                # Spearman à rangs moyens, sur les notices appariées par au
                # moins un modèle (``spearman_records``) ; le bloc des
                # notices appariées par aucun (``unmatched``) n'y entre pas.
                "spearman": spearman,
                "spearman_records": spearman_records,
                "spearman_method": "rangs moyens (ex aequo), notices "
                                   "appariées par au moins un des deux modèles",
                "unmatched": n - spearman_records,
                "matched_simple": len(simple_rank),
                "matched_bm25": len(bm25_rank),
            },
            "unevaluated": sorted({u for cs in concept_sets for u in cs.unevaluated}),
        }
        order = local_order
    else:
        order = native_order
    t_local = time.perf_counter()

    # --- Entrées ---
    entries: List[Dict[str, Any]] = []
    for position, ri in enumerate(order, 1):
        native_ev = sorted(evidence[ri].values(),
                           key=lambda e: (-e["weight"], e["rank"], e["source"]))
        justification: Dict[str, Any] = {
            "native": [{k: e[k] for k in ("source", "provider", "rank", "score",
                                           "sort", "class", "weight")}
                       for e in native_ev],
            "reserves": reserves[ri],
        }
        components: Dict[str, Any] = {
            "native_rrf": native_scores[ri],
            "native_contributions": [
                {"provider": c["provider"], "source": c["source"],
                 "label": c["label"],
                 "rank": c["rank"], "weight": c["weight"],
                 "value": c["contribution"]} for c in contributions[ri]],
        }
        entry: Dict[str, Any] = {
            "index": ri,
            "id": ids[ri],
            "rank": position,
            "native": {"rank": native_rank[ri], "score": native_scores[ri]},
            "components": components,
            "dimensions": dimensions[ri],
        }
        if mode == RANKING_LOCAL:
            detail = lexical_detail[ri]
            entry["score"] = local_scores[ri]
            entry["local"] = {"rank": local_rank[ri], "score": local_scores[ri]}
            components.update({
                "lexical_model": lexical_model,
                "lexical_simple": detail.get("simple", 0.0),
                "lexical_simple_rank": detail.get("simple_rank"),
                "bm25": detail.get("bm25", 0.0),
                "bm25_rank": detail.get("bm25_rank"),
                "native_competition_rank": native_competition.get(ri),
                "query_version": detail.get("version"),
            })
            justification["concepts"] = detail.get("matched", [])
            justification["missing"] = detail.get("missing", [])
            justification["unevaluated"] = detail.get("unevaluated", [])
        else:
            entry["score"] = native_scores[ri]
        justification["summary"] = _summary(entry, justification, mode)
        entry["justification"] = justification
        entries.append(entry)

    # Pertinence vérifiée (au moins une contribution de poids plein) et
    # ordre présumé seul (Isidore, S2 /paper/search) comptés à part.
    verified = sum(1 for ri in range(n)
                   if any(c["class"] == CLASS_RELEVANCE for c in contributions[ri]))
    presumed_only = sum(1 for ri in range(n) if contributions[ri] and all(
        c["class"] == CLASS_PRESUMED for c in contributions[ri]))
    notes = []
    for s in inputs:
        if s.status and s.status not in _SUCCESS_STATUSES:
            notes.append(f"{s.label} : {s.describe}")
    presumed_inputs = [s for s in inputs if s.rank_class == CLASS_PRESUMED and s.items]
    if presumed_inputs:
        note = (", ".join(sorted({s.label for s in presumed_inputs}))
                + " : ordre présumé de pertinence, non vérifié (poids "
                f"{CLASS_WEIGHTS[CLASS_PRESUMED]})")
        if any(s.provider == "isidore" for s in presumed_inputs):
            note += " ; Isidore et ses sous-sources comptées comme un seul fournisseur"
        notes.append(note)
    if any(s.items and s.rank_class == CLASS_OTHER and is_dimension_sort(s.sort)
           for s in inputs):
        notes.append("Ordres de citations ou de date ("
                     + ", ".join(sorted({f"{s.label} {s.sort}" for s in inputs
                                         if s.items and s.rank_class == CLASS_OTHER
                                         and is_dimension_sort(s.sort)}))
                     + ") : sans poids et sans effet sur l'ordre de la queue")
    if local_info and local_info.get("unevaluated"):
        notes.append("Parties de la requête non évaluées localement (ni "
                     "appariées ni déclarées absentes) : "
                     + ", ".join(local_info["unevaluated"]))
    t_end = time.perf_counter()
    return {
        "version": RANKING_VERSION,
        "mode": mode,
        "params": {**ranking_params(lexical_model), "reference_year": year_ref},
        "sources": [{
            "source": s.source, "label": s.label, "provider": s.provider,
            "sort": s.sort, "class": s.rank_class,
            "class_label": CLASS_LABELS[s.rank_class],
            "weight": CLASS_WEIGHTS[s.rank_class], "items": len(s.items),
            "status": s.status,
        } for s in inputs],
        "notes": notes,
        "local": local_info,
        "counts": {"records": n, "with_verified_relevance_rank": verified,
                   "presumed_only": presumed_only,
                   "queue": n - verified - presumed_only},
        "mapping": mapping.stats,
        "duration_s": {
            "mapping": round(t_map - t0, 4),
            "native": round(t_native - t_map, 4),
            "local": round(t_local - t_local_start, 4) if mode == RANKING_LOCAL else 0.0,
            "total": round(t_end - t0, 4),
        },
        "entries": entries,
    }


def _summary(entry: Dict[str, Any], justification: Dict[str, Any],
             mode: str) -> str:
    """Phrase de justification (français, compacte)."""
    parts: List[str] = []
    contrib = entry["components"]["native_contributions"]
    if contrib:
        parts.append("rangs natifs : " + ", ".join(
            f"{c['label']} {c['rank']}"
            + (" (présumé)" if c["weight"] < 1 else "")
            for c in contrib))
    else:
        parts.append("aucun rang de pertinence natif")
    if mode == RANKING_LOCAL:
        concepts = justification.get("concepts") or []
        if concepts:
            parts.append("concepts : " + ", ".join(
                f"{c['concept']} ({', '.join(c['fields'])})" for c in concepts))
        missing = justification.get("missing") or []
        if missing:
            parts.append("absents : " + ", ".join(missing))
    dims = entry["dimensions"]
    if dims["sources"]["independent"] > 1:
        parts.append(f"{dims['sources']['independent']} fournisseurs indépendants")
    reserves = justification.get("reserves") or []
    if reserves:
        parts.append("réserves : " + " ; ".join(r["label"] for r in reserves))
    return " ; ".join(parts)


# === Point d'entrée du moteur ===

def rank_harvest(sources: Sequence[Any], items: Sequence[Dict[str, Any]], *,
                 mode: str, plan=None, groups=None,
                 check: Optional[Callable[[], None]] = None,
                 elapsed_before: Optional[float] = None,
                 lexical_model: str = DEFAULT_LEXICAL_MODEL,
                 ) -> Optional[Dict[str, Any]]:
    """
    Étape de classement de ``engine.orchestrator.harvest`` (après la
    déduplication). ``off`` : rien (None). Une erreur interne du classement
    ne fait jamais échouer la moisson : elle est journalisée et rendue dans
    ``error`` ; l'annulation (``check``) remonte.

    Args:
        elapsed_before: durée de la moisson avant le classement (acquisition
            et déduplication), consignée à part (``acquisition_s``).
    """
    from utils.http import OperationCancelled

    if mode == RANKING_OFF:
        return None
    try:
        result = rank_records(items, sources, mode=mode, plan=plan,
                              groups=groups, check=check,
                              lexical_model=lexical_model)
    except OperationCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - le classement n'empêche pas la publication
        logger.error(f"Classement '{mode}' en échec : {exc}", exc_info=True)
        return {"version": RANKING_VERSION, "mode": mode,
                "error": f"{type(exc).__name__}: {exc}", "entries": []}
    if elapsed_before is not None:
        result["acquisition_s"] = round(float(elapsed_before), 4)
    durations = result["duration_s"]
    counts = result["counts"]
    logger.info(
        f"Classement '{mode}' ({RANKING_VERSION}) : {counts['records']} "
        f"notices en {durations['total']:.3f} s ; "
        f"{counts['with_verified_relevance_rank']} avec rang de pertinence "
        f"vérifié, {counts['presumed_only']} avec ordre présumé seul, "
        f"{counts['queue']} en queue")
    return result


def top_ranked_payload(ranking: Optional[Dict[str, Any]],
                       limit: int = TOP_EXPOSED) -> Optional[Dict[str, Any]]:
    """
    Extrait compact pour ``structuredContent.top_ranked`` : version, mode et,
    pour les ``limit`` premières notices, rang, score, identifiant et
    position dans le corpus (aucun chemin disque, aucune notice).
    """
    if not ranking:
        return None
    payload: Dict[str, Any] = {
        "version": ranking.get("version"),
        "mode": ranking.get("mode"),
        "records": (ranking.get("counts") or {}).get("records", 0),
        "limit": int(limit),
        "items": [{"rank": e["rank"], "score": round(float(e["score"]), 6),
                   "id": e["id"], "index": e["index"]}
                  for e in (ranking.get("entries") or [])[:max(0, int(limit))]],
    }
    if ranking.get("error"):
        payload["error"] = ranking["error"]
    durations = ranking.get("duration_s")
    if durations:
        payload["duration_s"] = durations.get("total")
    return payload


def ranking_text_lines(ranking: Optional[Dict[str, Any]],
                       items: Sequence[Dict[str, Any]], limit: int = 5,
                       indent: str = "  ") -> List[str]:
    """Premières notices du classement, lisibles (CLI)."""
    if not ranking or not ranking.get("entries"):
        if ranking and ranking.get("error"):
            return [f"Classement indisponible : {ranking['error']}"]
        return []
    label = "local" if ranking.get("mode") == RANKING_LOCAL else "natif"
    lines = [f"Classement {label} ({ranking.get('version')}) : "
             f"{min(limit, len(ranking['entries']))} premières notices "
             f"(l'ordre du fichier de corpus est inchangé)"]
    for e in ranking["entries"][:limit]:
        item = items[e["index"]] if 0 <= e["index"] < len(items) else {}
        title = str(item.get("title") or "(sans titre)")
        if len(title) > 90:
            title = title[:87] + "…"
        year = e["dimensions"].get("year")
        lines.append(f"{indent}{e['rank']}. {title}"
                     + (f" ({year})" if year else ""))
        lines.append(f"{indent}   {e['justification']['summary']}")
    return lines
