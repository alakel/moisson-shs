"""
Registre des capacités de requête par source (OPT-11, constat C27).

Chaque source est décrite telle qu'elle se comporte réellement : prise en
charge de l'expression exacte, des opérateurs booléens et des parenthèses,
recherche par champ, ordre des résultats, filtres de dates, de langue et de
type, pagination, clé, limites, sensibilité aux accents et aux apostrophes.
``utils.query_plan`` s'en sert pour traduire la requête de l'utilisateur dans
la syntaxe de chaque source, ou pour en dériver une variante « sac de mots »
affichée, et pour décider quels filtres sont natifs et lesquels restent
locaux.

Statut de chaque description (``status``) :

- ``vérifié`` : sondé en direct le ``verified_on`` (petites pages, requêtes
  espacées ; numéros cités dans ``evidence`` : journal daté des sondes,
  annexe de ``docs/API_SPECS.md``, voir ``PROBE_LOG``) ;
- ``vérifié via Isidore`` : sous-source qui passe par le flux Isidore ; elle
  hérite de ses capacités de requête ;
- ``documenté (non vérifié en live)`` : décrit d'après la documentation
  officielle seulement (CORE, clé absente ; Google Scholar, facturé, jamais
  appelé pour une vérification).

Sondes du 2026-09-29 (00 h 04 à 00 h 09, heure de Paris, 65 requêtes) :
l'affirmation « les sources internationales ignorent les opérateurs » est
fausse en bloc. OpenAlex honore guillemets, AND/OR/NOT et parenthèses ;
Semantic Scholar (point d'accès ``bulk``) honore guillemets, ``+``, ``|``,
``-`` et parenthèses, mais lit « AND » et « NOT » comme des mots vides (une
exclusion écrite « NOT » y renvoie donc l'inverse de ce qui est demandé) ;
seul CrossRef traite la requête en sac de mots (même total et mêmes premières
notices avec ou sans guillemets, AND ou NOT). Côté francophone, Isidore
n'honore que les opérateurs français ET, OU, SAUF : AND, OR et NOT y sont lus
comme des mots.

Ce module ne dépend d'aucun connecteur : il se lit sans réseau ni clé, et le
registre des connecteurs n'a pas à l'importer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# Date des sondes live qui fondent les descriptions « vérifié ».
VERIFIED_ON = "2026-09-29"
# Date de lecture de la documentation pour les descriptions « documenté ».
DOCUMENTED_ON = "2026-09-29"

STATUS_VERIFIED = "vérifié"
STATUS_VERIFIED_VIA_ISIDORE = "vérifié via Isidore"
STATUS_DOCUMENTED = "documenté (non vérifié en live)"

# Syntaxes de rendu connues de utils.query_plan.
SYNTAX_SOLR = "solr"            # HAL : Lucene/Solr (AND, OR, NOT, champ:valeur)
SYNTAX_ISIDORE = "isidore"      # Isidore : ET, OU, SAUF
SYNTAX_OPENALEX = "openalex"    # OpenAlex : AND, OR, NOT dans ``search``
SYNTAX_S2_BULK = "s2_bulk"      # Semantic Scholar bulk : + | - ( ) "…"
SYNTAX_CORE = "core"            # CORE : AND, OR, ( ), champ:valeur, "…"
SYNTAX_SCHOLAR = "scholar"      # Google Scholar : "…", OR, -terme
SYNTAX_BAG = "bag"              # sac de mots (aucun opérateur, aucune phrase)

SYNTAX_LABELS = {
    SYNTAX_SOLR: "syntaxe Solr",
    SYNTAX_ISIDORE: "syntaxe Isidore ET/OU/SAUF",
    SYNTAX_OPENALEX: "syntaxe OpenAlex",
    SYNTAX_S2_BULK: "syntaxe Semantic Scholar + | -",
    SYNTAX_CORE: "syntaxe CORE",
    SYNTAX_SCHOLAR: "syntaxe Google Scholar",
    SYNTAX_BAG: "sac de mots",
}

# Mode des filtres (dates, langues).
FILTER_NATIVE = "native"   # appliqué par la source
FILTER_LOCAL = "local"     # appliqué sur les notices reçues
FILTER_NONE = "none"       # impossible (ni filtre natif ni donnée locale)

# Transport du filtre de langue natif.
LANG_VIA_SEARCH = "search_kwarg"   # kwarg ``languages`` de search() (OPT-12)
LANG_VIA_LR = "constructor_lr"     # paramètre ``lr`` du connecteur Scholar

# Langue majoritaire du fonds interrogé : décide quelle variante reçoit la
# source (``query_variants``). « multi » : aucune variante imposée.
CORPUS_FR = "fr"
CORPUS_EN = "en"
CORPUS_MULTI = "multi"

# Journal des sondes (heure, requête, statut HTTP, total, première notice),
# versionné avec le dépôt : les numéros cités dans ``evidence`` y renvoient.
PROBE_LOG = ("docs/API_SPECS.md, annexe « Journal des sondes du 2026-09-29 » "
             "(sondes n° 1 à 65)")


@dataclass(frozen=True)
class SourceCapabilities:
    """
    Capacités de requête d'une source.

    Attributes:
        key, label: clé de registre et libellé.
        endpoint: point d'accès interrogé par le connecteur.
        syntax: rendu de la requête (``SYNTAX_*``).
        phrase: l'expression exacte entre guillemets est honorée.
        and_op, or_op, not_op: opérateurs explicites (None : non pris en
            charge ; pour ``and_op``, la juxtaposition vaut ET).
        not_style: ``infix`` (``a NOT b``) ou ``prefix`` (``a -b``).
        parentheses: regroupement par parenthèses honoré.
        implicit_operator: combinaison de deux termes juxtaposés
            (``AND`` vérifié, ou ``pertinence`` : tous les mots servent au
            classement sans être exigés).
        field_search: description de la recherche par champ.
        native_fields: champs transmis tels quels (``nom:valeur``) ; un
            champ absent de cette liste est ramené à la recherche sans
            restriction de champ, avec
            avertissement. ``field_passthrough`` : tout champ est transmis.
        relevance_ranking: ordre par défaut = pertinence.
        sorts: tris disponibles.
        ordering: ordre des notices retenues selon le mode de moisson.
        dates: ``native`` ou ``local`` ; ``dates_detail`` ;
            ``date_granularity`` (``day`` ou ``year``).
        languages: ``native``, ``local`` ou ``none`` ; ``languages_detail`` ;
            ``language_transport`` (filtre natif) ; ``language_in_records``.
        types: filtre de type (description ; vide : aucun).
        pagination, key_required (``none``, ``optional``, ``required``),
            limits.
        accents: ``folded`` (sans effet), ``significant`` ou ``unknown``.
        apostrophe: comportement observé des élisions (« l'école ») ;
            ``apostrophe_restricts`` : l'élision réduit les résultats
            (signalé dans le plan quand la requête en contient une).
        corpus_language: langue majoritaire du fonds (``fr``, ``en``,
            ``multi``).
        proxy_of: source dont le flux est lu (sous-sources d'Isidore).
        syntax_by_mode: rendu propre à un mode (point d'accès différent).
        wrap_top_level_or: entourer de parenthèses une disjonction de tête
            (le connecteur CORE ajoute ``AND yearPublished…`` à la requête).
        status, verified_on: statut de la description et date.
        evidence: preuves (numéros de sondes, réponses clés).
        docs: documentation officielle consultée.
    """

    key: str
    label: str
    endpoint: str
    syntax: str
    phrase: bool
    and_op: Optional[str]
    or_op: Optional[str]
    not_op: Optional[str]
    not_style: str = "infix"
    parentheses: bool = False
    implicit_operator: str = "AND"
    field_search: str = ""
    native_fields: FrozenSet[str] = frozenset()
    field_passthrough: bool = False
    relevance_ranking: bool = True
    sorts: str = ""
    ordering: Dict[str, str] = field(default_factory=dict)
    dates: str = FILTER_NATIVE
    dates_detail: str = ""
    date_granularity: str = "day"
    languages: str = FILTER_NONE
    languages_detail: str = ""
    language_transport: Optional[str] = None
    language_in_records: bool = False
    types: str = ""
    pagination: str = ""
    key_required: str = "none"
    limits: str = ""
    accents: str = "unknown"
    apostrophe: str = ""
    apostrophe_restricts: bool = False
    corpus_language: str = CORPUS_FR
    proxy_of: Optional[str] = None
    syntax_by_mode: Dict[str, str] = field(default_factory=dict)
    wrap_top_level_or: bool = False
    status: str = STATUS_VERIFIED
    verified_on: str = VERIFIED_ON
    evidence: Tuple[str, ...] = ()
    docs: Tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        """Description fondée sur des sondes live (directes ou via Isidore)."""
        return self.status in (STATUS_VERIFIED, STATUS_VERIFIED_VIA_ISIDORE)

    @property
    def supports_booleans(self) -> bool:
        return bool(self.or_op or self.not_op)

    def syntax_for(self, mode: Optional[str]) -> str:
        """Rendu de la requête pour un mode de moisson."""
        return self.syntax_by_mode.get(mode or "", self.syntax)

    def as_dict(self) -> Dict[str, Any]:
        """Description sérialisable (tableau des capacités, plan)."""
        data = asdict(self)
        data["native_fields"] = sorted(self.native_fields)
        data["evidence"] = list(self.evidence)
        data["docs"] = list(self.docs)
        data["verified"] = self.verified
        return data

    def summary(self) -> Dict[str, Any]:
        """Résumé compact (entrée du plan de requête)."""
        return {
            "syntax": self.syntax,
            "phrase": self.phrase,
            "booleans": {"and": self.and_op, "or": self.or_op,
                         "not": self.not_op},
            "parentheses": self.parentheses,
            "relevance_ranking": self.relevance_ranking,
            "dates": self.dates,
            "languages": self.languages,
            "accents": self.accents,
            "status": self.status,
            "verified_on": self.verified_on,
        }


# === Sources francophones ===

HAL = SourceCapabilities(
    key="hal",
    label="HAL",
    endpoint="https://api.archives-ouvertes.fr/search/",
    syntax=SYNTAX_SOLR,
    phrase=True,
    and_op="AND",
    or_op="OR",
    not_op="NOT",
    not_style="infix",
    parentheses=True,
    implicit_operator="AND",
    field_search="champ:valeur Solr (title_t, abstract_t, authFullName_t, "
                 "keyword_t, fulltext_t pour le texte intégral…) ; champ par "
                 "défaut « text » : métadonnées (titre, résumé, mots-clés), "
                 "pas le texte intégral ; « title_t:\"discours numérique\" » "
                 ": 45 notices",
    field_passthrough=True,
    relevance_ranking=True,
    sorts="score desc par défaut (sans paramètre sort) ; tout champ triable "
          "(producedDateY_i desc vérifié) ; docid asc imposé par cursorMark",
    ordering={
        "bibliographie": "pertinence (score desc, pagination start/rows : "
                         "OPT-12)",
        "collecte": "ordre déterministe (docid asc, cursorMark)",
    },
    dates=FILTER_NATIVE,
    dates_detail="fq producedDate_tdate:[… TO …] (jour)",
    date_granularity="day",
    languages=FILTER_NATIVE,
    languages_detail="fq language_s (codes ISO 639-1)",
    language_transport=LANG_VIA_SEARCH,
    language_in_records=True,
    types="fq docType_s (ART, COMM, OUV, COUV, THESE…)",
    pagination="cursorMark (docid asc) ou start/rows ; start=10000 servi "
               "(2,15 s) ; rows ≤ 10 000 (documentation)",
    key_required="none",
    limits="aucun quota publié",
    accents="folded",
    apostrophe="l'élision compte comme un mot : « l'école » 696 079 notices "
               "contre 1 024 777 pour « école »",
    apostrophe_restricts=True,
    corpus_language=CORPUS_FR,
    evidence=(
        "n° 1 « discours numérique » : 2 754 ; n° 3 avec AND : 2 754 "
        "(opérateur implicite ET) ; ordre par score décroissant",
        "n° 2 expression exacte : 179 (sous-ensemble cohérent)",
        "n° 4 OR : 182 388 ; n° 5 NOT : 60 056 ; n° 6 « (discours OR récit) "
        "AND numérique NOT politique » : 2 644",
        "n° 7 title_t:\"discours numérique\" : 45",
        "n° 8 fq année ≥ 2018 + language_s:fr + docType_s:ART, tri année "
        "décroissante : 460, 3/3 notices conformes",
        "n° 9 et 10 « écriture » = « ecriture » : 26 885",
        "n° 11 « l'école » : 696 079 ; n° 12 « école » : 1 024 777",
        "n° 13 start=10000 : notice servie",
        "sondes du 2026-09-30 (PAT-22) : « modèle actantiel » 9 ; "
        "« \"modèle actantiel\" » 7, identique à text:\"…\" et à "
        "title_t OR abstract_t OR keyword_t (les 7 par abstract_t) ; "
        "fulltext_t:\"modèle actantiel\" 224, dont 221 hors text ; union "
        "228 ; fulltext_t:* 1 704 036 documents sur 4 679 411",
    ),
    docs=("https://api.archives-ouvertes.fr/docs/search",),
)

ISIDORE = SourceCapabilities(
    key="isidore",
    label="Isidore",
    endpoint="https://api.isidore.science/resource/search",
    syntax=SYNTAX_ISIDORE,
    phrase=True,
    and_op="ET",
    or_op="OU",
    not_op="SAUF",
    not_style="infix",
    parentheses=True,
    implicit_operator="AND",
    field_search="aucune (la documentation n'en décrit pas ; « url:cairn.info » "
                 "est lu comme des mots, OPT-08)",
    relevance_ranking=True,
    sorts="relevance par défaut ; date_desc accepté mais non fiable (premières "
          "notices de 2008 pour une requête qui en compte de 2026)",
    ordering={
        "bibliographie": "pertinence (tri par défaut d'Isidore)",
        "collecte": "pertinence (tri par défaut d'Isidore ; fenêtre de 200)",
    },
    dates=FILTER_LOCAL,
    dates_detail="filtre local sur l'année des notices (le filtre date "
                 "d'Isidore donne des totaux incohérents, OPT-08) ; notices "
                 "sans année écartées",
    date_granularity="year",
    languages=FILTER_LOCAL,
    languages_detail="filtre local sur la langue des notices ; le filtre "
                     "natif language (URI lexvo) est sous-inclusif : 7 276 "
                     "notices sur 266 508 ; langue lue dans le champ "
                     "dc:language des notices (38 sur 48 notices sondées le "
                     "2026-09-29 le portent ; les autres sont conservées)",
    language_in_records=True,
    types="type=URI de l'ontologie Isidore (ex. …ontology#article) : "
          "documenté, non isolé (3/3 articles en combinaison)",
    pagination="page/replies ; fenêtre de 200 résultats (C35) ; une requête "
               "replies=200 (OPT-08)",
    key_required="none",
    limits="200 premiers résultats servis par requête ; environ 46 Ko par "
           "notice",
    accents="folded",
    apostrophe="élision presque neutre : « l'école » 1 059 649 contre "
               "1 124 625 pour « école »",
    corpus_language=CORPUS_FR,
    evidence=(
        "n° 14 « discours numérique » : 266 508 ; n° 16 avec ET : 266 508 "
        "(opérateur implicite ET)",
        "n° 15 expression exacte : 1 276",
        "n° 18 OU : 2 110 979 ; n° 20 SAUF : 338 142 ; n° 22 « (discours OU "
        "récit) ET numérique » : 418 753",
        "n° 17 AND : 251 777 (≠ ET, notices anglophones en tête) ; n° 19 OR : "
        "128 289 (< requête de base) ; n° 21 NOT : 33 649, premières notices "
        "contenant « discours numériques » : opérateurs anglais lus comme "
        "des mots",
        "n° 62 language=…/fra seul : 7 276 ; n° 23 language + type + tri "
        "date : 2 402, 2/3 notices en anglais ; n° 63 sort=date_desc : "
        "premières notices de mai 2008",
        "n° 24 et 25 « écriture » = « ecriture » : 872 163",
        "n° 26 « l'école » : 1 059 649 ; n° 27 « école » : 1 124 625",
    ),
    docs=("https://isidore.science/api",),
)


def _isidore_subsource(key: str, label: str) -> SourceCapabilities:
    """Sous-source lisant le flux Isidore : capacités de requête d'Isidore."""
    return replace(
        ISIDORE,
        key=key,
        label=label,
        proxy_of="isidore",
        status=STATUS_VERIFIED_VIA_ISIDORE,
        limits=("notices d'Isidore dont une URL relève du domaine de "
                f"{label} ; fenêtre de 200 résultats d'Isidore (C35)"),
        evidence=("requête transmise à Isidore (flux partagé de la moisson) : "
                  "voir Isidore",),
    )


# === Sources internationales ===

CROSSREF = SourceCapabilities(
    key="crossref",
    label="CrossRef",
    endpoint="https://api.crossref.org/works (query.bibliographic)",
    syntax=SYNTAX_BAG,
    phrase=False,
    and_op=None,
    or_op=None,
    not_op=None,
    parentheses=False,
    implicit_operator="pertinence",
    field_search="query.author, query.container-title… (documentés) ; "
                 "query.title accepté (28 885 notices, titres contenant les "
                 "deux mots) mais absent de la documentation",
    relevance_ranking=True,
    sorts="sans curseur : pertinence (score) par défaut ; sous cursor sans "
          "sort : ordre de l'index, pas la pertinence (OPT-12) ; "
          "sort=score&order=desc compatible avec cursor ; sort=published "
          "(vérifié sans curseur) refusé sous cursor (400, OPT-12)",
    ordering={
        "bibliographie": "pertinence (sort=score&order=desc sous cursor, "
                         "OPT-12)",
        "collecte": "ordre de l'index sous cursor, sans tri (déterministe)",
    },
    dates=FILTER_NATIVE,
    dates_detail="filter=from-pub-date / until-pub-date (jour)",
    date_granularity="day",
    languages=FILTER_NONE,
    languages_detail="aucun filtre de langue (HTTP 400 filter-not-available) "
                     "et langue absente des notices (select ne l'accepte "
                     "pas)",
    language_in_records=False,
    types="filter=type:journal-article, book-chapter…",
    pagination="rows ≤ 1000 ; cursor=* (offset plafonné à 10 000)",
    key_required="none",
    limits="pool poli (mailto) ; notices « Title Pending » datées dans le "
           "futur en tête d'un tri par date",
    accents="significant",
    apostrophe="élision neutre : « l'école » = « école » (13 710)",
    corpus_language=CORPUS_EN,
    evidence=(
        "n° 28 à 31 : « discours numérique », avec guillemets, avec AND, avec "
        "NOT : 43 931 chacune, mêmes premières notices (guillemets et "
        "opérateurs sans effet ; NOT n'exclut rien)",
        "n° 32 query.title : 28 885",
        "n° 33 from-pub-date 2018 + type:journal-article + sort=published "
        "desc : 10 627, dates décroissantes",
        "n° 34 filter=language:fr : HTTP 400 filter-not-available",
        "n° 35 « écriture » : 5 744 ; n° 36 « ecriture » : 466",
        "n° 37 « l'école » = n° 38 « école » : 13 710",
    ),
    docs=("https://api.crossref.org/swagger-ui/index.html",),
)

OPENALEX = SourceCapabilities(
    key="openalex",
    label="OpenAlex",
    endpoint="https://api.openalex.org/works (search)",
    syntax=SYNTAX_OPENALEX,
    phrase=True,
    and_op="AND",
    or_op="OR",
    not_op="NOT",
    not_style="infix",
    parentheses=True,
    implicit_operator="AND",
    field_search="filter=title.search, abstract.search, "
                 "title_and_abstract.search (hors du paramètre search) ; "
                 "title.search:\"discours numérique\" : 68 notices",
    relevance_ranking=True,
    sorts="relevance_score:desc par défaut avec search, conservé sous "
          "cursor (OPT-12) ; publication_date, cited_by_count… "
          "(publication_date:desc vérifié)",
    ordering={
        "bibliographie": "pertinence (relevance_score)",
        "collecte": "pertinence (relevance_score, curseur)",
    },
    dates=FILTER_NATIVE,
    dates_detail="filter=publication_year / from_publication_date (année)",
    date_granularity="year",
    languages=FILTER_NATIVE,
    languages_detail="filter=language (codes ISO 639-1)",
    language_transport=LANG_VIA_SEARCH,
    language_in_records=True,
    types="filter=type:article, book…",
    pagination="per-page ≤ 200 ; cursor=*",
    key_required="required",
    limits="0,001 $ par appel ; sans clé, plafond de 0,1 $ par jour "
           "(X-RateLimit-Limit-USD) ; le connecteur exige la clé",
    accents="significant",
    apostrophe="élision acceptée (« l'école » : 301 138, titres pertinents)",
    corpus_language=CORPUS_EN,
    evidence=(
        "n° 39 « discours numérique » : 45 699 ; n° 43 avec AND : 45 699 "
        "(opérateur implicite ET)",
        "n° 40 expression exacte : 696",
        "n° 41 OR : 738 066 ; n° 42 NOT : 510 262 ; n° 49 « (discours OR "
        "récit) AND numérique » : 51 284",
        "n° 45 filter=title.search:\"discours numérique\" : 68",
        "n° 44 année > 2017 + language:fr + type:article, tri date "
        "décroissante : 15 728, 3/3 notices conformes",
        "n° 46 « écriture » : 112 865 ; n° 47 « ecriture » : 15 804",
        "n° 48 « l'école » : 301 138",
        "coût 0,001 $ par appel (X-RateLimit-Cost-USD), sans clé",
    ),
    docs=("https://docs.openalex.org/how-to-use-the-api/get-lists-of-entities/"
          "search-entities",),
)

SEMANTIC_SCHOLAR = SourceCapabilities(
    key="semantic_scholar",
    label="Semantic Scholar",
    endpoint="https://api.semanticscholar.org/graph/v1/paper/search/bulk",
    syntax=SYNTAX_S2_BULK,
    phrase=True,
    and_op="+",
    or_op="|",
    not_op="-",
    not_style="prefix",
    parentheses=True,
    implicit_operator="AND",
    field_search="aucune dans la requête (filtres venue, fieldsOfStudy ; "
                 "/paper/search/match pour un titre)",
    relevance_ranking=False,
    sorts="bulk : sans tri, ordre sans rapport avec la pertinence ; "
          "sort=citationCount:desc, publicationDate, paperId (citationCount "
          "vérifié) ; /paper/search : pertinence, sans syntaxe de requête",
    ordering={
        "bibliographie": "citations décroissantes (bulk, "
                         "sort=citationCount:desc, OPT-12) : pas un "
                         "classement par pertinence",
        "collecte": "paperId croissant (bulk sans tri, déterministe, sans "
                    "rapport avec la requête)",
    },
    dates=FILTER_NATIVE,
    dates_detail="year=AAAA-AAAA (année)",
    date_granularity="year",
    languages=FILTER_NONE,
    languages_detail="aucun filtre de langue ni champ de langue dans les "
                     "notices",
    language_in_records=False,
    types="publicationTypes=JournalArticle, Review, Book…",
    pagination="bulk : jusqu'à 1000 notices par appel, jeton token ; "
               "/paper/search : offset + limit ≤ 1000",
    key_required="optional",
    limits="pool partagé sans clé : HTTP 429 dès la 10e requête espacée de "
           "3,3 à 5 s",
    accents="folded",
    apostrophe="non concluant : « l'école » 61 396 notices, ordre du bulk non "
               "pertinent, comparaison avec « école » refusée (429)",
    corpus_language=CORPUS_EN,
    evidence=(
        "n° 50 « discours numérique » (bulk) : 1 800 ; n° 54 avec « AND » : "
        "1 800 ; n° 65 avec « NOT » : 1 800, mêmes notices (AND et NOT lus "
        "comme des mots vides)",
        "n° 51 expression exacte : 121",
        "n° 52 « discours | numérique » : 201 507 ; n° 53 « discours "
        "-numérique » : 91 173 ; n° 55 « (discours | récit) + numérique » : "
        "2 417",
        "n° 56 sort=citationCount:desc + year=2018- + "
        "publicationTypes=JournalArticle : 271, citations 21 ≥ 11 ≥ 10",
        "n° 57 /paper/search : 202 042 (appariement lâche), ordre pertinent",
        "n° 58 et 59 « écriture » = « ecriture » : 23 355",
        "n° 64 « l'école » : 61 396 ; n° 60 et 61 : HTTP 429",
    ),
    docs=("https://api.semanticscholar.org/api-docs/graph",),
)

CORE = SourceCapabilities(
    key="core",
    label="CORE",
    endpoint="https://api.core.ac.uk/v3/search/works",
    syntax=SYNTAX_CORE,
    phrase=True,
    and_op="AND",
    or_op="OR",
    not_op=None,
    parentheses=True,
    implicit_operator="AND",
    field_search="champ:valeur (title, authors, abstract, fullText, "
                 "yearPublished, documentType…) ; bornes >, <, >=, <= ; "
                 "_exists_:champ",
    native_fields=frozenset({"title", "authors", "abstract", "fullText",
                             "yearPublished", "documentType", "doi",
                             "publisher", "language", "_exists_"}),
    relevance_ranking=True,
    sorts="non documenté",
    ordering={
        "bibliographie": "ordre de l'API (tri non documenté)",
        "collecte": "ordre de l'API (tri non documenté)",
    },
    dates=FILTER_NATIVE,
    dates_detail="bornes yearPublished dans la requête (année)",
    date_granularity="year",
    languages=FILTER_LOCAL,
    languages_detail="filtre local sur language.code des notices (aucun "
                     "filtre de langue documenté)",
    language_in_records=True,
    types="documentType:… dans la requête (documenté)",
    pagination="offset + limit, 10 000 résultats au plus ; scroll au-delà",
    key_required="required",
    limits="jetons par jour selon le compte (1 000 à 5 000 et plus), 10 à 25 "
           "requêtes par minute",
    accents="unknown",
    apostrophe="non documenté",
    corpus_language=CORPUS_EN,
    wrap_top_level_or=True,
    status=STATUS_DOCUMENTED,
    verified_on=DOCUMENTED_ON,
    evidence=(
        "documentation v3 : AND (AND, +, espace), OR, (…), champ:valeur, "
        "bornes, _exists_, expression exacte entre guillemets ; NOT absent "
        "du tableau des opérateurs ; 10 000 résultats au plus",
    ),
    docs=("https://api.core.ac.uk/docs/v3",),
)

GOOGLE_SCHOLAR = SourceCapabilities(
    key="google_scholar",
    label="Google Scholar",
    endpoint="https://serpapi.com/search.json?engine=google_scholar",
    syntax=SYNTAX_SCHOLAR,
    phrase=True,
    and_op=None,
    or_op="OR",
    not_op="-",
    not_style="prefix",
    parentheses=False,
    implicit_operator="AND",
    field_search="author:, source: (SerpAPI) ; allintitle: (aide Google "
                 "Scholar)",
    native_fields=frozenset({"author", "source", "intitle", "allintitle"}),
    relevance_ranking=True,
    sorts="pertinence par défaut ; scisbd = articles de l'année triés par "
          "date",
    ordering={
        "bibliographie": "pertinence",
        "collecte": "pertinence (pages de 20)",
    },
    dates=FILTER_NATIVE,
    dates_detail="as_ylo / as_yhi (année)",
    date_granularity="year",
    languages=FILTER_NATIVE,
    languages_detail="lr=lang_fr|lang_en (paramètre lr du connecteur)",
    language_transport=LANG_VIA_LR,
    language_in_records=False,
    types="aucun (as_sdt : brevets, jurisprudence ; as_rr : synthèses)",
    pagination="start + num ≤ 20 ; SERPAPI_MAX_PAGES",
    key_required="required",
    limits="facturé à la requête (1 crédit par page de 20) ; hors des "
           "groupes, nommé explicitement",
    accents="unknown",
    apostrophe="non documenté",
    corpus_language=CORPUS_MULTI,
    status=STATUS_DOCUMENTED,
    verified_on=DOCUMENTED_ON,
    evidence=(
        "documentation SerpAPI : q avec author: et source:, as_ylo, as_yhi, "
        "lr, hl, scisbd, start, num (1 à 20), as_sdt, as_rr",
        "aide Google Scholar : titre entre guillemets, author:\"…\", "
        "allintitle: ; OR et « - » relèvent de la syntaxe Google générale, "
        "non décrite par ces pages",
    ),
    docs=("https://serpapi.com/google-scholar-api",
          "https://scholar.google.com/intl/en/scholar/help.html"),
)


CAPABILITIES: Dict[str, SourceCapabilities] = {
    "hal": HAL,
    "isidore": ISIDORE,
    "openedition": _isidore_subsource("openedition", "OpenEdition"),
    "cairn": _isidore_subsource("cairn", "Cairn"),
    "persee": _isidore_subsource("persee", "Persée"),
    "theses_fr": _isidore_subsource("theses_fr", "Theses.fr"),
    "erudit": _isidore_subsource("erudit", "Érudit"),
    "crossref": CROSSREF,
    "openalex": OPENALEX,
    "semantic_scholar": SEMANTIC_SCHOLAR,
    "core": CORE,
    "google_scholar": GOOGLE_SCHOLAR,
}


def get_capabilities(key: str) -> Optional[SourceCapabilities]:
    """Capacités d'une source (clé de registre), ou None si inconnue."""
    return CAPABILITIES.get((key or "").lower())


def capabilities_table() -> List[Dict[str, Any]]:
    """Une ligne par source (ordre du registre), pour la documentation."""
    return [caps.as_dict() for caps in CAPABILITIES.values()]


def _yes(value: Any) -> str:
    return "oui" if value else "non"


_FILTER_FR = {FILTER_NATIVE: "natif", FILTER_LOCAL: "local",
              FILTER_NONE: "impossible"}
_ACCENTS_FR = {"folded": "sans effet", "significant": "significatifs",
               "unknown": "non vérifié"}


def _cell(text: str) -> str:
    """Cellule de tableau Markdown (« | » échappé)."""
    return str(text).replace("|", "\\|")


def capabilities_markdown() -> str:
    """
    Tableau Markdown des capacités (section de ``docs/API_SPECS.md``).

    Le tableau de la documentation est engendré par cette fonction ; un test
    vérifie qu'il n'a pas divergé du registre.
    """
    head = ("| Source | Expression exacte | Booléens | Parenthèses | Champs | "
            "Tri par pertinence | Dates | Langues | Accents | Statut |")
    sep = "|" + "---|" * 10
    rows = [head, sep]
    for caps in CAPABILITIES.values():
        if caps.proxy_of:
            continue
        ops = [op for op in (caps.and_op, caps.or_op, caps.not_op) if op]
        booleans = " ".join(f"`{op}`" for op in ops) if ops else "non"
        fields = "oui" if (caps.native_fields or caps.field_passthrough) else (
            "par filtre" if "filter=" in caps.field_search else "non")
        rows.append(
            f"| {caps.label} | {_yes(caps.phrase)} | {_cell(booleans)} | "
            f"{_yes(caps.parentheses)} | {fields} | "
            f"{_yes(caps.relevance_ranking)} | "
            f"{_FILTER_FR.get(caps.dates, caps.dates)} | "
            f"{_FILTER_FR.get(caps.languages, caps.languages)} | "
            f"{_ACCENTS_FR.get(caps.accents, caps.accents)} | "
            f"{caps.status} ({caps.verified_on}) |"
        )
    return "\n".join(rows)
