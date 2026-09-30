"""
Plan de requête par source (OPT-11, constat C27).

À partir de la requête de l'utilisateur (mots, expressions exactes entre
guillemets droits ou français, opérateurs AND/OR/NOT ou ET/OU/SAUF,
parenthèses, préfixes ``+`` et ``-``, ``champ:valeur``), de variantes
explicites par langue (``query_variants``, ex. ``{"fr": "…", "en": "…"}``),
des langues demandées, des bornes de dates et du mode, le plan donne pour
chaque source :

- la requête envoyée, traduite dans la syntaxe de la source si elle honore
  expressions exactes et opérateurs (registre ``connectors.capabilities``,
  vérifié en direct), ou la variante « sac de mots » dérivée pour une source
  qui ne les honore pas (CrossRef) : mots des parties positives, sans
  guillemets ni opérateurs, termes exclus retirés ;
- l'origine de cette requête (requête d'origine ou variante ``fr`` / ``en``) ;
- les filtres natifs (appliqués par la source) et locaux (appliqués sur les
  notices reçues) ;
- les limites de traduction (avertissements) et des notes.

Aucune traduction ni expansion cachée : une source majoritairement
anglophone ne reçoit la variante ``en`` que si l'utilisateur l'a fournie ;
sinon elle reçoit la requête d'origine et le plan le signale. Le groupe
``all`` ne rend pas une requête française multilingue par lui-même. Tout ce
qui est envoyé figure dans le plan (``query_sent``), qui est renvoyé avec la
moisson (``structuredContent.query_plan``) et consultable sans exécution
(``plan_only`` / ``--plan-only``).

Règle d'identité : une requête faite de mots seuls (ni guillemets, ni
opérateur, ni parenthèse, ni préfixe, ni champ) est envoyée telle quelle, aux
espaces près, à toutes les sources : comportement antérieur inchangé, mêmes
clés de cache, même flux Isidore.

Lecture (revue d'OPT-11) : précédence NOT > AND (écrit ou juxtaposition) >
OR ; une exclusion écrite au niveau d'une alternative, hors parenthèses,
porte sur toute l'alternative (Lucene, Google) ; une branche d'alternative
sans terme positif est ignorée ; ces lectures sont signalées avec la lecture
retenue. La syntaxe Solr de HAL (plages ``champ:[a TO b]``, ``"…"~N``,
``^N``, échappements ``\\``) est conservée pour HAL et adaptée ailleurs
(``_adapt_tree``), avec avertissement.

Ce module ne fait aucun accès réseau.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from connectors.capabilities import (
    CORPUS_EN, CORPUS_MULTI, FILTER_LOCAL, FILTER_NATIVE, LANG_VIA_LR,
    LANG_VIA_SEARCH, SYNTAX_BAG, SYNTAX_LABELS, SYNTAX_SOLR,
    SourceCapabilities, get_capabilities,
)

PLAN_VERSION = 1

# Imbrication maximale d'une requête (parenthèses, exclusions, champs) :
# largement au-delà d'un usage bibliographique, bien en deçà de la pile.
MAX_NESTING = 64


class QueryPlanError(ValueError):
    """Requête, variante, langue ou borne de date invalide."""


# === Codes de langue (ISO 639-1) ===

ISO_639_1 = frozenset("""
aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co
cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl
gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg
ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk
ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps
pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta
te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za
zh zu
""".split())

# Codes ISO 639-2 (B et T) et noms courants -> ISO 639-1, pour lire la langue
# des notices (``language`` : « fr », « fre », « French », « français »,
# « fr-FR », URI lexvo…).
_LANG_ALIASES = {
    "fra": "fr", "fre": "fr", "french": "fr", "francais": "fr",
    "eng": "en", "english": "en", "anglais": "en",
    "spa": "es", "spanish": "es", "espagnol": "es", "espanol": "es",
    "deu": "de", "ger": "de", "german": "de", "allemand": "de",
    "deutsch": "de",
    "ita": "it", "italian": "it", "italien": "it", "italiano": "it",
    "por": "pt", "portuguese": "pt", "portugais": "pt", "portugues": "pt",
    "nld": "nl", "dut": "nl", "dutch": "nl", "neerlandais": "nl",
    "ara": "ar", "arabic": "ar", "arabe": "ar",
    "rus": "ru", "russian": "ru", "russe": "ru",
    "zho": "zh", "chi": "zh", "chinese": "zh", "chinois": "zh",
    "jpn": "ja", "japanese": "ja", "japonais": "ja",
    "pol": "pl", "polish": "pl", "polonais": "pl",
    "ces": "cs", "cze": "cs", "czech": "cs", "tcheque": "cs",
    "ron": "ro", "rum": "ro", "romanian": "ro", "roumain": "ro",
    "ell": "el", "gre": "el", "greek": "el", "grec": "el",
    "tur": "tr", "turkish": "tr", "turc": "tr",
    "cat": "ca", "catalan": "ca",
    "lat": "la", "latin": "la",
    "swe": "sv", "swedish": "sv", "suedois": "sv",
    "dan": "da", "danish": "da", "danois": "da",
    "nor": "no", "norwegian": "no", "norvegien": "no",
    "fin": "fi", "finnish": "fi", "finnois": "fi",
    "hun": "hu", "hungarian": "hu", "hongrois": "hu",
    "heb": "he", "hebrew": "he", "hebreu": "he",
    "kor": "ko", "korean": "ko", "coreen": "ko",
    "ukr": "uk", "ukrainian": "uk", "ukrainien": "uk",
    "bul": "bg", "bulgarian": "bg", "bulgare": "bg",
    "hrv": "hr", "croatian": "hr", "croate": "hr",
    "srp": "sr", "serbian": "sr", "serbe": "sr",
    "slv": "sl", "slovenian": "sl", "slovene": "sl",
    "slk": "sk", "slo": "sk", "slovak": "sk", "slovaque": "sk",
    "eus": "eu", "baq": "eu", "basque": "eu",
    "glg": "gl", "galician": "gl", "galicien": "gl",
    "oci": "oc", "occitan": "oc",
    "bre": "br", "breton": "br",
    "cym": "cy", "wel": "cy", "welsh": "cy", "gallois": "cy",
    "gle": "ga", "irish": "ga", "irlandais": "ga",
    "isl": "is", "ice": "is", "icelandic": "is", "islandais": "is",
    "est": "et", "estonian": "et", "estonien": "et",
    "lav": "lv", "latvian": "lv", "letton": "lv",
    "lit": "lt", "lithuanian": "lt", "lituanien": "lt",
    "fas": "fa", "per": "fa", "persian": "fa", "persan": "fa",
    "hin": "hi", "hindi": "hi",
    "ind": "id", "indonesian": "id", "indonesien": "id",
    "vie": "vi", "vietnamese": "vi", "vietnamien": "vi",
    "tha": "th", "thai": "th",
}

_LANG_SPLIT_RE = re.compile(r"[,;|/\s]+")


def _fold(text: str) -> str:
    """Minuscules sans diacritiques (comparaison de noms de langue)."""
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def normalize_languages(value: Any) -> List[str]:
    """
    Liste de codes ISO 639-1 (minuscules, sans doublon, ordre conservé).

    Accepte une liste, ou une chaîne « fr,en » / « fr en ». None : liste vide.

    Raises:
        QueryPlanError: code absent de la norme ISO 639-1.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in _LANG_SPLIT_RE.split(value) if v]
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise QueryPlanError(
            f"languages doit être une liste de codes ISO 639-1 (reçu : "
            f"{type(value).__name__})")
    out: List[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise QueryPlanError(f"Code de langue invalide : {raw!r}")
        code = raw.strip().lower()
        if code not in ISO_639_1:
            raise QueryPlanError(
                f"Code de langue invalide : {raw!r} (attendu : code ISO 639-1 "
                f"à deux lettres, ex. 'fr', 'en')")
        if code not in out:
            out.append(code)
    return out


def normalize_variants(value: Any) -> Dict[str, str]:
    """
    Variantes de requête par langue : ``{code ISO 639-1: requête}``.

    Raises:
        QueryPlanError: clé invalide, valeur non textuelle ou vide.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise QueryPlanError(
            "query_variants doit être un objet {code de langue: requête}, "
            "ex. {\"en\": \"digital discourse\"}")
    out: Dict[str, str] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key).strip().lower()
        if key not in ISO_639_1:
            raise QueryPlanError(
                f"Variante de requête : langue {raw_key!r} invalide (attendu : "
                f"code ISO 639-1, ex. 'fr', 'en')")
        if not isinstance(raw_val, str) or not raw_val.strip():
            raise QueryPlanError(f"Variante de requête '{key}' vide")
        out[key] = raw_val.strip()
    return out


def normalize_item_language(value: Any) -> Optional[str]:
    """
    Langue d'une notice ramenée à un code ISO 639-1, ou None si inconnue.

    Lit « fr », « fre », « fra », « fr-FR », « French », « français », les URI
    lexvo (« http://lexvo.org/id/iso639-3/fra ») ; une liste ou une chaîne à
    plusieurs langues donne la première reconnue.
    """
    return next(iter(item_languages(value)), None)


def notice_languages(item: Dict[str, Any]) -> List[str]:
    """
    Langues reconnues d'une notice : champ ``language`` et lignes
    ``language:`` d'``extra`` (un type Zotero sans champ de langue, comme
    ``computerProgram``, y reporte la valeur). Liste vide : langue inconnue.
    """
    values: List[Any] = [item.get("language")]
    extra = item.get("extra")
    if isinstance(extra, str):
        values.extend(line.split(":", 1)[1] for line in extra.split("\n")
                      if line.split(":", 1)[0].strip().lower() == "language"
                      and ":" in line)
    return item_languages(values)


def item_languages(value: Any) -> List[str]:
    """Toutes les langues reconnues d'une valeur ``language`` de notice."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        found: List[str] = []
        for v in value:
            for code in item_languages(v):
                if code not in found:
                    found.append(code)
        return found
    if isinstance(value, dict):
        return item_languages(value.get("code") or value.get("$")
                              or value.get("name"))
    text = str(value).strip()
    if not text:
        return []
    if "/" in text and text.startswith("http"):
        text = text.rstrip("/").rsplit("/", 1)[-1]
    found = []
    for part in _LANG_SPLIT_RE.split(text):
        token = _fold(part.strip())
        if not token:
            continue
        base = re.split(r"[-_]", token, maxsplit=1)[0]
        code = base if base in ISO_639_1 else _LANG_ALIASES.get(base)
        if code and code not in found:
            found.append(code)
    return found


def apply_local_language_filter(
    items: Sequence[Dict], languages: Sequence[str],
) -> Tuple[List[Dict], Dict[str, int]]:
    """
    Filtre local de langue sur les notices d'une source.

    Règle de prudence : une notice dont la langue est connue et absente de
    ``languages`` est écartée ; une notice sans langue lisible est conservée
    (et comptée), jamais écartée en silence.

    Returns:
        ``(notices conservées, compteurs)`` ; compteurs ``kept``,
        ``removed``, ``unknown_kept``.
    """
    wanted = set(languages or ())
    if not wanted:
        return list(items), {"kept": len(items), "removed": 0,
                             "unknown_kept": 0}
    kept: List[Dict] = []
    removed = unknown = 0
    for item in items:
        langs = notice_languages(item)
        if not langs:
            unknown += 1
            kept.append(item)
        elif wanted.intersection(langs):
            kept.append(item)
        else:
            removed += 1
    return kept, {"kept": len(kept), "removed": removed,
                  "unknown_kept": unknown}


# === Dates ===

_YEAR_RE = re.compile(r"^\d{4}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_date(value: Any, name: str) -> Optional[str]:
    """
    Borne de date : ``AAAA`` ou ``AAAA-MM-JJ`` (date réelle), ou None.

    Raises:
        QueryPlanError: format ou date invalide.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _YEAR_RE.match(text):
        return text
    if _DAY_RE.match(text):
        try:
            date.fromisoformat(text)
        except ValueError:
            raise QueryPlanError(f"{name} : date inexistante ({text!r})") from None
        return text
    raise QueryPlanError(
        f"{name} : format de date invalide ({text!r} ; attendu AAAA ou "
        f"AAAA-MM-JJ)")


def check_date_range(from_date: Optional[str], to_date: Optional[str]) -> None:
    """
    Raises:
        QueryPlanError: borne de début postérieure à la borne de fin.
    """
    if not from_date or not to_date:
        return
    lo = from_date if len(from_date) == 10 else f"{from_date}-01-01"
    hi = to_date if len(to_date) == 10 else f"{to_date}-12-31"
    if lo > hi:
        raise QueryPlanError(
            f"Plage de dates vide : from_date {from_date} postérieure à "
            f"to_date {to_date}")


# === Analyse de la requête ===

@dataclass(frozen=True)
class Term:
    text: str


@dataclass(frozen=True)
class Phrase:
    text: str
    # Suffixe Solr collé au guillemet fermant : proximité « ~N » ou
    # pondération « ^N » (« "discours numérique"~5 ») ; vide sinon.
    suffix: str = ""


@dataclass(frozen=True)
class Range:
    """Plage Solr écrite après un champ (« [2020 TO 2024] », « {a TO *] »),
    conservée telle quelle : jamais découpée en mots."""
    text: str


@dataclass(frozen=True)
class FieldQuery:
    name: str
    value: Any   # Term, Phrase, Range ou groupe


@dataclass(frozen=True)
class Not:
    child: Any


@dataclass(frozen=True)
class And:
    children: Tuple[Any, ...]
    explicit: bool = False   # au moins un ET écrit (AND, ET, &&, +)


@dataclass(frozen=True)
class Or:
    children: Tuple[Any, ...]


_QUOTE_PAIRS = {'"': '"', "«": "»", "“": "”", "„": "“"}
_QUOTE_CHARS = frozenset('"«»“”„')
_OPERATORS = {
    "AND": "AND", "ET": "AND", "&&": "AND",
    "OR": "OR", "OU": "OR", "||": "OR", "|": "OR",
    "NOT": "NOT", "SAUF": "NOT",
}
_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):(.*)$", re.S)
# Champ suivi d'une plage Solr (« producedDateY_i:[2020 TO 2024] »).
_FIELD_RANGE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*):([\[{])")
# Proximité « ~N » ou pondération « ^N » collée à un guillemet fermant ou à
# une parenthèse fermante.
_SUFFIX_RE = re.compile(r"[~^]\d+(?:\.\d+)?(?=[\s()]|$)")
_PUNCT_ONLY_RE = re.compile(r"^[^\w*?]+$", re.U)
# Caractères que l'analyseur Lucene/Solr permet d'échapper par « \ ».
_SOLR_SPECIAL = frozenset('+-&|!(){}[]^"~*?:\\/ ')
_ESCAPED_RE = re.compile(r'\\([+\-&|!(){}\[\]^"~*?:\\/ ])')
_PHRASE_TRIM = " \t   "


@dataclass
class ParsedQuery:
    """Requête analysée : arbre, texte normalisé, indicateurs."""

    text: str                 # requête normalisée (espaces)
    tree: Any                 # nœud racine
    simple: bool              # mots seuls : envoyée telle quelle
    warnings: List[str] = field(default_factory=list)
    # Lecture retenue quand la précédence de OR peut surprendre (PAT-19) :
    # OR mêlé sans parenthèses à des termes juxtaposés, à AND ou à une
    # exclusion ; None sinon.
    ambiguity: Optional[str] = None

    @property
    def has_phrase(self) -> bool:
        return _any_node(self.tree, Phrase)

    @property
    def has_or(self) -> bool:
        return _any_node(self.tree, Or)

    @property
    def has_not(self) -> bool:
        return _any_node(self.tree, Not)

    @property
    def has_field(self) -> bool:
        return _any_node(self.tree, FieldQuery)

    def phrases(self) -> List[str]:
        return [n.text for n in _walk(self.tree) if isinstance(n, Phrase)]

    def fields(self) -> List[str]:
        return [n.name for n in _walk(self.tree) if isinstance(n, FieldQuery)]

    def excluded_words(self) -> List[str]:
        return _dedupe(w for n in _walk(self.tree) if isinstance(n, Not)
                       for w in _words(n.child))

    def positive_words(self) -> List[str]:
        return _dedupe(_positive_words(self.tree))

    def bag_of_words(self) -> str:
        """Variante sac de mots : mots des parties positives, dans l'ordre."""
        if self.simple:
            return self.text
        return " ".join(self.positive_words())

    def summary(self) -> Dict[str, Any]:
        return {
            "normalized": self.text,
            "simple": self.simple,
            "phrases": self.phrases(),
            "operators": sorted({
                name for name, cls in (("AND", And), ("OR", Or), ("NOT", Not))
                if (_any_node(self.tree, cls) and
                    (cls is not And or _any_explicit_and(self.tree)))
            }),
            "fields": self.fields(),
            "excluded": self.excluded_words(),
            "bag_of_words": self.bag_of_words(),
            "warnings": list(self.warnings),
        }


def _walk(node) -> Iterable[Any]:
    if node is None:
        return
    yield node
    if isinstance(node, (And, Or)):
        for child in node.children:
            yield from _walk(child)
    elif isinstance(node, Not):
        yield from _walk(node.child)
    elif isinstance(node, FieldQuery):
        yield from _walk(node.value)


def _any_node(node, cls) -> bool:
    return any(isinstance(n, cls) for n in _walk(node))


def _any_explicit_and(node) -> bool:
    return any(isinstance(n, And) and n.explicit for n in _walk(node))


def _words(node) -> List[str]:
    """Mots d'un sous-arbre (phrases découpées, champs ramenés à la valeur,
    échappements Solr retirés ; une plage ne donne aucun mot)."""
    out: List[str] = []
    for n in _walk(node):
        if isinstance(n, Term):
            out.append(_unescape(_clean_word(n.text)))
        elif isinstance(n, Phrase):
            out.extend(_unescape(_clean_word(w)) for w in n.text.split())
    return [w for w in out if w]


def _has_positive(node) -> bool:
    """Le sous-arbre contient un terme, une expression ou une plage hors de
    toute exclusion (une branche qui n'en a pas ne restreint rien)."""
    if node is None or isinstance(node, Not):
        return False
    if isinstance(node, (And, Or)):
        return any(_has_positive(c) for c in node.children)
    if isinstance(node, FieldQuery):
        return _has_positive(node.value)
    return isinstance(node, (Term, Phrase, Range))


def _and_node(items: List[Any], explicit: bool):
    return items[0] if len(items) == 1 else And(tuple(items), explicit)


def _positive_words(node) -> List[str]:
    if node is None or isinstance(node, Not):
        return []
    if isinstance(node, (And, Or)):
        out: List[str] = []
        for child in node.children:
            out.extend(_positive_words(child))
        return out
    if isinstance(node, FieldQuery):
        return _positive_words(node.value)
    return _words(node)


def _dedupe(words: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for w in words:
        key = w.casefold()
        if w and key not in seen:
            seen.add(key)
            out.append(w)
    return out


def _clean_word(word: str) -> str:
    """Retire la ponctuation de syntaxe en bord de mot (« Europe: »)."""
    return word.strip().strip(":;,")


def _unescape(word: str) -> str:
    """Retire les échappements Solr écrits par l'utilisateur (« C\\+\\+ » →
    « C++ ») : ils n'ont de sens que pour HAL."""
    return _ESCAPED_RE.sub(r"\1", word)


def _range_end(text: str, start: int) -> Optional[int]:
    """Indice du crochet ou de l'accolade qui ferme une plage, ou None."""
    for k in range(start + 1, len(text)):
        if text[k] in "]}":
            return k
    return None


def _suffix_token(text: str, i: int, tokens: List[Tuple[str, str]]) -> int:
    """Jeton SUFFIX (« ~5 », « ^2 ») collé en position ``i`` ; nouvel indice."""
    m = _SUFFIX_RE.match(text, i)
    if m:
        tokens.append(("SUFFIX", m.group()))
        return m.end()
    return i


def _tokenize(text: str, warnings: List[str]) -> List[Tuple[str, str]]:
    """Jetons : LP, RP, PHRASE, WORD, OP, PLUS, MINUS, FIELD, RANGE, SUFFIX."""
    tokens: List[Tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            tokens.append(("LP", c))
            i += 1
            continue
        if c == ")":
            tokens.append(("RP", c))
            i = _suffix_token(text, i + 1, tokens)
            continue
        if c in _QUOTE_PAIRS:
            closer = _QUOTE_PAIRS[c]
            j = text.find(closer, i + 1)
            if c == "„" and j < 0:
                j = text.find("”", i + 1)
            if j < 0:
                warnings.append(
                    f"guillemet {c} non fermé : la fin de la requête est lue "
                    f"comme une expression exacte")
                j = n
            tokens.append(("PHRASE", text[i + 1:j].strip(_PHRASE_TRIM)))
            # Proximité ou pondération Solr collée au guillemet fermant
            # (« "discours numérique"~5 ») : rattachée à l'expression.
            i = _suffix_token(text, j + 1, tokens) if j < n else j + 1
            continue
        if c in _QUOTE_CHARS:            # guillemet fermant orphelin
            warnings.append(f"guillemet {c} sans ouverture : ignoré")
            i += 1
            continue
        prev = text[i - 1] if i > 0 else " "
        if (c in "+-" and i + 1 < n and not text[i + 1].isspace()
                and (prev.isspace() or prev == "(")):
            tokens.append(("PLUS" if c == "+" else "MINUS", c))
            i += 1
            continue
        # Plage Solr après un champ : un seul jeton, jamais découpée en mots
        # (« producedDateY_i:[2020 TO 2024] »).
        m = _FIELD_RANGE_RE.match(text, i)
        if m and len(m.group(1)) >= 2:
            end = _range_end(text, m.start(2))
            if end is not None:
                tokens.append(("FIELD", m.group(1)))
                tokens.append(("RANGE", text[m.start(2):end + 1]))
                i = end + 1
                continue
            warnings.append(f"plage « {m.group(1)}:{m.group(2)}… » non "
                            f"fermée : lue comme des mots")
        j = i
        while (j < n and not text[j].isspace() and text[j] not in "()"
               and text[j] not in _QUOTE_CHARS):
            # « \ » échappe le caractère suivant (syntaxe Solr : « C\+\+ »,
            # « 10.3917\/rfsic ») : il reste dans le mot.
            if text[j] == "\\" and j + 1 < n and not text[j + 1].isspace():
                j += 2
                continue
            j += 1
        word = text[i:j]
        i = j
        if word in _OPERATORS:
            tokens.append(("OP", _OPERATORS[word]))
            continue
        m = _FIELD_RE.match(word)
        if m and len(m.group(1)) >= 2 and not m.group(2).startswith("/"):
            name, rest = m.group(1), m.group(2)
            if rest:
                tokens.append(("FIELD", name))
                tokens.append(("WORD", rest))
                continue
            if i < n and (text[i] in _QUOTE_PAIRS or text[i] == "("):
                tokens.append(("FIELD", name))
                continue
        tokens.append(("WORD", word))
    return tokens


class _Parser:
    """
    Descente récursive : OR < AND (implicite ou écrit) < NOT < primaire.

    Exception à cette précédence, alignée sur Lucene (lecture de HAL avant
    OPT-11) et sur Google : une exclusion écrite au niveau d'une alternative,
    hors parenthèses (« twitter OR facebook NOT publicité »), porte sur toute
    l'alternative (« (twitter OR facebook) NOT publicité »). Une branche
    d'alternative sans terme positif (« a OR NOT b », « a OR (-b -c) ») ne
    restreint rien : elle est ignorée. Les lectures ambiguës (OR mêlé sans
    parenthèses à une juxtaposition, à un AND ou à une exclusion) sont
    signalées (``ambiguous``, ``hoisted``) : ``parse_query`` en tire un
    avertissement qui montre la lecture retenue.
    """

    def __init__(self, tokens: List[Tuple[str, str]], warnings: List[str]):
        self.tokens = tokens
        self.pos = 0
        self.warnings = warnings
        self.ambiguous = False    # OR mêlé à un ET au même niveau
        self.hoisted = False      # exclusion rattachée à toute l'alternative
        self.depth = 0            # imbrication (parenthèses, exclusions…)

    def _enter(self) -> None:
        """Borne l'imbrication : au-delà, erreur claire plutôt qu'un
        dépassement de pile (analyse et rendu sont récursifs)."""
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise QueryPlanError(
                f"requête trop imbriquée (plus de {MAX_NESTING} niveaux de "
                f"parenthèses, d'exclusions ou de champs)")

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> Optional[Tuple[str, str]]:
        tok = self.peek()
        if tok is not None:
            self.pos += 1
        return tok

    def parse(self):
        nodes = []
        while self.peek() is not None:
            node = self.parse_or()
            if node is not None:
                nodes.append(node)
            tok = self.peek()
            if tok is not None and tok[0] == "RP":
                self.take()
                self.warnings.append("parenthèse fermante sans ouverture : "
                                     "ignorée")
        return _simplify(And(tuple(nodes))) if nodes else None

    def parse_or(self):
        # Branches de l'alternative : éléments juxtaposés (ou reliés par AND)
        # de chaque branche, au niveau courant (un groupe entre parenthèses
        # est un seul élément).
        branches: List[Tuple[List[Any], bool]] = []
        items, explicit = self._and_items()
        if items:
            branches.append((items, explicit))
        while self.peek() == ("OP", "OR"):
            self.take()
            items, explicit = self._and_items()
            if not items or not branches:
                self.warnings.append("opérateur OR sans terme de part et "
                                     "d'autre : ignoré")
            if items:
                branches.append((items, explicit))
        if not branches:
            return None
        if len(branches) == 1:
            return _and_node(*branches[0])
        alternatives: List[Any] = []
        hoisted: List[Any] = []
        mixed = dropped = False
        for items, explicit in branches:
            positives = [n for n in items if _has_positive(n)]
            negatives = [n for n in items if not _has_positive(n)]
            if not positives:
                # « a OR NOT b », « a OR (-b -c) », « a OR champ:(-b) » : une
                # branche sans terme positif ne restreint rien (et n'a pas de
                # forme valide chez Isidore).
                dropped = True
                continue
            mixed = mixed or len(positives) > 1
            hoisted.extend(negatives)
            alternatives.append(_and_node(positives, explicit))
        if dropped:
            self.warnings.append(
                "exclusion placée dans une alternative (OR NOT) : sans "
                "effet restrictif, ignorée")
        if not alternatives:
            return None
        if len(alternatives) == 1:
            node = alternatives[0]
        else:
            node = Or(tuple(alternatives))
            self.ambiguous = self.ambiguous or mixed
            self.hoisted = self.hoisted or bool(hoisted)
        # Exclusions écrites au niveau de l'alternative : sur toute
        # l'alternative (convention Lucene et Google).
        return And((node, *hoisted)) if hoisted else node

    def _and_items(self) -> Tuple[List[Any], bool]:
        """Éléments d'une branche (juxtaposés ou reliés par AND) et présence
        d'un ET écrit."""
        items = []
        explicit = False
        while True:
            tok = self.peek()
            if tok is None or tok[0] == "RP" or tok == ("OP", "OR"):
                break
            if tok == ("OP", "AND"):
                self.take()
                if not items:
                    self.warnings.append("opérateur AND sans terme à gauche : "
                                         "ignoré")
                else:
                    explicit = True
                nxt = self.peek()
                if nxt is None or nxt[0] == "RP" or nxt == ("OP", "OR"):
                    self.warnings.append("opérateur AND sans terme à droite : "
                                         "ignoré")
                continue
            node = self.parse_unary()
            if node is not None:
                items.append(node)
        return items, explicit

    def parse_unary(self):
        tok = self.peek()
        if tok is None:
            return None
        self._enter()
        try:
            if tok in (("OP", "NOT"),) or tok[0] == "MINUS":
                self.take()
                operand = self.parse_unary()
                if operand is None:
                    self.warnings.append("exclusion (NOT, SAUF, -) sans "
                                         "terme : ignorée")
                    return None
                return Not(operand)
            if tok[0] == "PLUS":
                self.take()
                return self.parse_unary()
            return self.parse_primary()
        finally:
            self.depth -= 1

    def parse_primary(self):
        tok = self.take()
        if tok is None:
            return None
        kind, value = tok
        if kind == "LP":
            node = self.parse_or()
            if self.peek() is not None and self.peek()[0] == "RP":
                self.take()
                suffix = self._suffix()
                if suffix:
                    self.warnings.append(
                        f"« {suffix} » après une parenthèse (pondération ou "
                        f"proximité d'un groupe) : non pris en charge, ignoré")
            else:
                self.warnings.append("parenthèse ouvrante non fermée : "
                                     "fermée en fin de requête")
            return node
        if kind == "RP":
            self.warnings.append("parenthèse fermante sans ouverture : ignorée")
            return None
        if kind == "PHRASE":
            suffix = self._suffix()
            if not value.strip():
                self.warnings.append("expression entre guillemets vide : "
                                     "ignorée")
                return None
            return Phrase(" ".join(value.split()), suffix)
        if kind == "RANGE":
            return Range(value)
        if kind == "SUFFIX":             # jamais émis seul ; par prudence
            self.warnings.append(f"« {value} » isolé : ignoré")
            return None
        if kind == "FIELD":
            self._enter()
            try:
                inner = self.parse_primary()
            finally:
                self.depth -= 1
            if inner is None:
                self.warnings.append(f"champ « {value}: » sans valeur : "
                                     f"ignoré")
                return None
            return FieldQuery(value, inner)
        if kind == "WORD":
            word = value
            if _PUNCT_ONLY_RE.match(word):
                return None      # ponctuation isolée (« : », « - »…)
            return Term(word)
        # OP ou préfixe à une place inattendue (« OR » en tête…)
        self.warnings.append(f"opérateur {value} mal placé : ignoré")
        return None

    def _suffix(self) -> str:
        """Suffixe « ~N » / « ^N » qui suit le jeton courant, consommé."""
        tok = self.peek()
        if tok is not None and tok[0] == "SUFFIX":
            self.take()
            return tok[1]
        return ""


def _simplify(node):
    """
    Aplatit les ET/OU imbriqués de même nature, déballe les singletons, et
    sort l'exclusion d'un champ : ``champ:(-x)`` devient ``NOT champ:x``
    (« title_t:NOT x » n'est pas une requête Solr valide), ``champ:(-x -y)``
    devient ``NOT champ:x NOT champ:y``.
    """
    if isinstance(node, (And, Or)):
        children = []
        explicit = getattr(node, "explicit", False)
        for child in node.children:
            child = _simplify(child)
            if child is None:
                continue
            if type(child) is type(node):
                children.extend(child.children)
                if isinstance(child, And):
                    explicit = explicit or child.explicit
            else:
                children.append(child)
        if not children:
            return None
        if len(children) == 1:
            return children[0]
        return And(tuple(children), explicit) if isinstance(node, And) \
            else Or(tuple(children))
    if isinstance(node, Not):
        child = _simplify(node.child)
        return Not(child) if child is not None else None
    if isinstance(node, FieldQuery):
        value = _simplify(node.value)
        if value is None:
            return None
        if isinstance(value, FieldQuery):
            return value          # champ imbriqué : le plus intérieur l'emporte
        if isinstance(value, Not):
            return Not(_simplify(FieldQuery(node.name, value.child)))
        if isinstance(value, And) and all(isinstance(c, Not)
                                          for c in value.children):
            return And(tuple(Not(_simplify(FieldQuery(node.name, c.child)))
                             for c in value.children), value.explicit)
        return FieldQuery(node.name, value)
    return node


def _display(tree) -> str:
    """Lecture retenue d'un arbre, parenthèses explicites (avertissements)."""
    caps = get_capabilities("hal")      # AND/OR/NOT, champs, parenthèses
    return " ".join(_Renderer(caps, "display").render(tree).split())


def parse_query(text: Any, *, label: str = "requête") -> ParsedQuery:
    """
    Analyse une requête.

    Raises:
        QueryPlanError: requête vide, sans terme (opérateurs seuls) ou sans
            terme positif (exclusion seule).
    """
    if not isinstance(text, str):
        raise QueryPlanError(f"{label} : chaîne attendue")
    normalized = " ".join(text.split())
    if not normalized:
        raise QueryPlanError(f"{label} vide")
    warnings: List[str] = []
    tokens = _tokenize(normalized, warnings)
    parser = _Parser(tokens, warnings)
    tree = parser.parse()
    if tree is None:
        raise QueryPlanError(
            f"{label} sans terme de recherche (seulement des opérateurs ou de "
            f"la ponctuation) : {normalized!r}")
    if not _has_positive(tree):
        raise QueryPlanError(
            f"{label} sans terme positif : une exclusion seule ne définit pas "
            f"de recherche ({normalized!r})")
    # Précédence : la lecture retenue est montrée quand elle peut surprendre
    # (les moteurs et les bases ne s'accordent pas sur la priorité de OR).
    if parser.hoisted or parser.ambiguous:
        what, why = [], []
        if parser.ambiguous:
            what.append("des termes juxtaposés ou reliés par AND")
            why.append("AND prioritaire sur OR")
        if parser.hoisted:
            what.append("une exclusion")
            why.append("exclusion appliquée à toute l'alternative, comme "
                       "Lucene et Google")
        warnings.append(
            f"précédence : OR mêlé sans parenthèses à {' et à '.join(what)} : "
            f"lu « {_display(tree)} » ({' ; '.join(why)}) ; les moteurs ne "
            f"s'accordent pas sur ces priorités : mettre des parenthèses pour "
            f"fixer la lecture")
    # Requête entière entre « » (thème cité tel quel, par exemple recopié
    # d'un prompt) : c'est une expression exacte, bien plus restrictive
    # qu'une recherche par mots (HAL : 179 notices contre 2 754).
    if (normalized.startswith("«") and normalized.endswith("»")
            and len(tokens) == 1 and tokens[0][0] == "PHRASE"
            and len(tokens[0][1].split()) > 1):
        warnings.append(
            "requête entière entre guillemets « » : lue comme une expression "
            "exacte (beaucoup moins de notices qu'une recherche par mots) ; "
            "retirer les guillemets pour chercher les mots séparément")
    return ParsedQuery(
        text=normalized, tree=tree,
        simple=all(kind == "WORD" for kind, _ in tokens),
        warnings=warnings,
        ambiguity=(_display(tree) if (parser.hoisted or parser.ambiguous)
                   else None),
    )


# === Rendu par source ===

def _escape_term(word: str, syntax: str) -> str:
    word = _clean_word(word)
    if syntax == "solr":
        return _solr_escape(word)
    # Hors Solr, un échappement « \x » écrit pour HAL n'a pas de sens.
    return _unescape(word)


def _solr_escape(word: str) -> str:
    """
    Échappement Solr d'un mot : « / » ouvre une expression régulière dans
    l'analyseur Lucene ; un « : » resté dans un mot (pas un nom de champ
    valide) serait lu comme une recherche par champ. Un échappement déjà
    écrit par l'utilisateur (« \\/ », « \\+ », « \\: »…, documenté pour HAL)
    est conservé tel quel ; seul un antislash isolé est doublé.
    """
    out: List[str] = []
    i, n = 0, len(word)
    while i < n:
        c = word[i]
        if c == "\\":
            if i + 1 < n and word[i + 1] in _SOLR_SPECIAL:
                out.append(word[i:i + 2])
                i += 2
                continue
            out.append("\\\\")
        elif c in "/:":
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _strip_not(node, excluded: List[str]):
    """Retire les exclusions (source sans NOT) ; mots exclus dans ``excluded``."""
    if isinstance(node, Not):
        excluded.extend(_words(node.child))
        return None
    if isinstance(node, (And, Or)):
        kids = [c for c in (_strip_not(c, excluded) for c in node.children)
                if c is not None]
        if not kids:
            return None
        if len(kids) == 1:
            return kids[0]
        return And(tuple(kids), node.explicit) if isinstance(node, And) \
            else Or(tuple(kids))
    if isinstance(node, FieldQuery):
        value = _strip_not(node.value, excluded)
        return FieldQuery(node.name, value) if value is not None else None
    return node


class _Renderer:
    """Rendu d'un arbre dans la syntaxe d'une source booléenne."""

    def __init__(self, caps: SourceCapabilities, syntax: str):
        self.caps = caps
        self.syntax = syntax
        self.warnings: List[str] = []
        self.notes: List[str] = []

    def render(self, node) -> str:
        if isinstance(node, Term):
            text = _escape_term(node.text, self.syntax)
            if text in _OPERATORS and self.caps.phrase:
                # Valeur de champ égale à un opérateur (« title_t:OR ») : nue,
                # elle serait lue comme l'opérateur.
                return f'"{text}"'
            return text
        if isinstance(node, Range):
            return node.text
        if isinstance(node, Phrase):
            if self.caps.phrase:
                # Le suffixe (~N, ^N) n'arrive ici que pour Solr : ailleurs,
                # _adapt_tree l'a déjà traduit ou retiré.
                return '"' + node.text.replace('"', "") + '"' + node.suffix
            self.warnings.append(
                f"expression exacte « {node.text} » non prise en charge : "
                f"mots envoyés séparément")
            return " ".join(_escape_term(w, self.syntax)
                            for w in node.text.split())
        if isinstance(node, FieldQuery):
            if self.caps.field_passthrough or node.name in self.caps.native_fields:
                return f"{node.name}:{self._atom(node.value)}"
            self.warnings.append(
                f"recherche par champ « {node.name}: » non prise en charge : "
                f"valeur cherchée sans restriction de champ")
            return self.render(node.value)
        if isinstance(node, Not):
            return self._negation(node.child)
        if isinstance(node, Or):
            if not self.caps.parentheses and any(
                    isinstance(c, (And, Or)) for c in node.children):
                self._no_parentheses_note()
            return f" {self.caps.or_op} ".join(self._atom(c)
                                               for c in node.children)
        if isinstance(node, And):
            positives = [c for c in node.children if not isinstance(c, Not)]
            negatives = [c for c in node.children if isinstance(c, Not)]
            op = self.caps.and_op
            joiner = f" {op} " if (node.explicit and op) else " "
            parts = [self._atom(c) for c in positives]
            text = joiner.join(parts)
            for neg in negatives:
                rendered = self._negation(neg.child)
                text = f"{text} {rendered}" if text else rendered
            return text
        raise TypeError(f"nœud inconnu : {node!r}")

    def _negation(self, child) -> str:
        op = self.caps.not_op
        atom = self._atom(child)
        if self.caps.not_style == "prefix":
            return f"{op}{atom}"
        return f"{op} {atom}"

    def _atom(self, node) -> str:
        text = self.render(node)
        if isinstance(node, (And, Or)):
            if not self.caps.parentheses:
                self._no_parentheses_note()
            return f"({text})"
        return text

    def _no_parentheses_note(self) -> None:
        note = (f"regroupement par parenthèses non documenté pour "
                f"{self.caps.label} : transmis tel quel")
        if note not in self.notes:
            self.notes.append(note)


def _adapt_tree(node, caps: SourceCapabilities, syntax: str,
                warnings: List[str], notes: List[str]):
    """
    Adapte l'arbre à une source AVANT le rendu, pour que les parenthèses, la
    disjonction de tête (CORE) et la règle « OR NOT » voient la structure
    réellement envoyée :

    - champ non pris en charge : remplacé par sa valeur (groupe compris),
      cherchée sans restriction de champ ; une plage (« champ:[a TO b] ») ne peut pas
      l'être : la clause est retirée ;
    - proximité « "…"~N » hors Solr : mots cherchés ensemble (ensemble plus
      large, rien n'est perdu) ; pondération « ^N » hors Solr : retirée ;
    - branche d'alternative devenue sans terme positif : retirée.

    Returns:
        Le nœud adapté, ou None s'il ne reste rien.
    """
    if node is None:
        return None
    if isinstance(node, FieldQuery):
        if caps.field_passthrough or node.name in caps.native_fields:
            value = _adapt_tree(node.value, caps, syntax, warnings, notes)
            return FieldQuery(node.name, value) if value is not None else None
        if isinstance(node.value, Range):
            warnings.append(
                f"plage « {node.name}:{node.value.text} » non prise en charge "
                f"par {caps.label} : critère retiré de la requête envoyée")
            return None
        warnings.append(
            f"recherche par champ « {node.name}: » non prise en charge : "
            f"valeur cherchée sans restriction de champ")
        return _adapt_tree(node.value, caps, syntax, warnings, notes)
    if isinstance(node, Phrase):
        if not node.suffix or syntax == SYNTAX_SOLR:
            return node
        if node.suffix.startswith("~"):
            warnings.append(
                f"proximité « \"{node.text}\"{node.suffix} » non prise en "
                f"charge par {caps.label} : mots cherchés ensemble, sans "
                f"contrainte de distance")
            words = [Term(w) for w in node.text.split()]
            return words[0] if len(words) == 1 else And(tuple(words))
        notes.append(f"pondération « {node.suffix} » ignorée par {caps.label} "
                     f"(elle ne joue que sur le classement)")
        return Phrase(node.text)
    if isinstance(node, Not):
        child = _adapt_tree(node.child, caps, syntax, warnings, notes)
        return Not(child) if child is not None else None
    if isinstance(node, (And, Or)):
        kids = [k for k in (_adapt_tree(c, caps, syntax, warnings, notes)
                            for c in node.children) if k is not None]
        if isinstance(node, Or):
            positive = [k for k in kids if _has_positive(k)]
            if len(positive) < len(kids):
                warnings.append(
                    "exclusion placée dans une alternative (OR NOT) : sans "
                    "effet restrictif, ignorée")
            kids = positive
        if not kids:
            return None
        if len(kids) == 1:
            return kids[0]
        return And(tuple(kids), node.explicit) if isinstance(node, And) \
            else Or(tuple(kids))
    return node


def _ranges(node) -> List[str]:
    """Plages « champ:[a TO b] » d'un arbre, telles qu'écrites."""
    return [f"{n.name}:{n.value.text}" for n in _walk(node)
            if isinstance(n, FieldQuery) and isinstance(n.value, Range)]


def _nothing_searchable(caps: SourceCapabilities, parsed: ParsedQuery):
    ranges = ", ".join(f"« {r} »" for r in _ranges(parsed.tree))
    detail = (f" (plage {ranges} non prise en charge par cette source)"
              if ranges else "")
    return QueryPlanError(
        f"{caps.label} : la requête ne garde aucun terme cherchable{detail} ; "
        f"ajouter des mots, utiliser from_date/to_date pour les dates, ou ne "
        f"pas interroger cette source")


def render_for_source(parsed: ParsedQuery, caps: SourceCapabilities,
                      syntax: Optional[str] = None
                      ) -> Tuple[str, str, List[str], List[str]]:
    """
    Requête envoyée à une source.

    Returns:
        ``(requête, traduction, avertissements, notes)`` ; traduction :
        ``unchanged`` (mots seuls, envoyés tels quels), ``translated``
        (syntaxe de la source) ou ``bag_of_words`` (variante dérivée).

    Raises:
        QueryPlanError: plus aucun terme cherchable pour cette source (plage
            seule sur une source qui ne la prend pas en charge).
    """
    syntax = syntax or caps.syntax
    warnings: List[str] = []
    notes: List[str] = []
    if parsed.simple:
        return parsed.text, "unchanged", warnings, notes
    if syntax == SYNTAX_BAG:
        if not parsed.positive_words():
            raise _nothing_searchable(caps, parsed)
        ranges = _ranges(parsed.tree)
        if parsed.has_phrase:
            warnings.append(
                "expression exacte non prise en charge : mots envoyés sans "
                "guillemets (" + ", ".join(f"« {p} »" for p in parsed.phrases())
                + ")")
        if parsed.has_or:
            warnings.append(
                "opérateur OR non pris en charge : tous les mots sont envoyés "
                "ensemble et servent au classement par pertinence")
        positives = {w.casefold() for w in parsed.positive_words()}
        excluded = [w for w in parsed.excluded_words()
                    if w.casefold() not in positives]
        if parsed.has_not:
            what = ", ".join(f"« {w} »" for w in excluded) or "termes exclus"
            warnings.append(
                f"exclusion non prise en charge : {what} retiré(s) de la "
                f"requête envoyée ; les notices qui les contiennent ne sont "
                f"pas écartées")
        range_fields = {r.split(":", 1)[0] for r in ranges}
        fields = [f for f in _dedupe(parsed.fields()) if f not in range_fields]
        if fields:
            warnings.append(
                "recherche par champ non prise en charge : valeurs cherchées "
                "sans restriction de champ (" + ", ".join(fields) + ")")
        if ranges:
            warnings.append(
                "plage non prise en charge : critère retiré de la requête "
                "envoyée (" + ", ".join(f"« {r} »" for r in ranges) + ")")
        return parsed.bag_of_words(), "bag_of_words", warnings, notes
    # Adaptation à la source d'abord (champs, plages, proximité), puis
    # retrait des exclusions si la source n'en a pas : le rendu, les
    # parenthèses et la disjonction de tête portent sur l'arbre réellement
    # envoyé.
    tree = _adapt_tree(parsed.tree, caps, syntax, warnings, notes)
    if caps.not_op is None and _any_node(tree, Not):
        excluded: List[str] = []
        tree = _strip_not(tree, excluded)
        warnings.append(
            "exclusion non documentée pour " + caps.label + " : "
            + (", ".join(f"« {w} »" for w in _dedupe(excluded)) or "termes")
            + " retiré(s) de la requête envoyée ; les notices qui les "
            "contiennent ne sont pas écartées")
    tree = _simplify(tree)
    if tree is None or not _has_positive(tree):
        raise _nothing_searchable(caps, parsed)
    renderer = _Renderer(caps, syntax)
    text = renderer.render(tree)
    if caps.wrap_top_level_or and isinstance(tree, Or):
        text = f"({text})"
    warnings.extend(renderer.warnings)
    notes.extend(renderer.notes)
    return (" ".join(text.split()), "translated", _dedupe_keep(warnings),
            _dedupe_keep(notes))


def _dedupe_keep(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


# === Heuristique de langue de la requête (avertissement seulement) ===

_FRENCH_WORDS = frozenset("""
le la les des du de d l un une et ou pour dans sur par avec sans entre
aux au en est sont ce cette ces qui que quoi dont où leur leurs nous vous
société politique numérique éducation histoire santé recherche
""".split())
_FRENCH_CHARS = frozenset("éèêëàâäùûüçôöîïœÉÈÊÀÂÙÛÇÔÎŒ")


def looks_french(text: str) -> bool:
    """
    Indice (non décisif) qu'une requête est en français : diacritiques
    propres au français, élision (« l' », « d' ») ou mot-outil français.
    Sert seulement à formuler l'avertissement du plan ; rien n'en dépend.
    """
    if any(c in _FRENCH_CHARS for c in text):
        return True
    words = re.findall(r"[\w']+", text.lower())
    for w in words:
        if re.match(r"^(l|d|qu|j|n|s|c)'", w):
            return True
        if w in _FRENCH_WORDS:
            return True
    return False


# === Plan ===

@dataclass
class SourcePlan:
    """Ce qui est envoyé à une source et ce qui est filtré, où."""

    source: str
    label: str
    query_sent: str
    query_origin: str               # "original" ou "variant:<lang>"
    syntax: str
    translation: str                # unchanged | translated | bag_of_words
    status: str                     # statut des capacités
    verified_on: str
    native_filters: Dict[str, str] = field(default_factory=dict)
    local_filters: Dict[str, str] = field(default_factory=dict)
    ordering: str = ""
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    # Exécution : kwargs de run_source (``languages`` natif, OPT-12) et du
    # constructeur du connecteur (``lr`` de Google Scholar), langues du
    # filtre local.
    search_kwargs: Dict[str, Any] = field(default_factory=dict)
    connector_kwargs: Dict[str, Any] = field(default_factory=dict)
    local_languages: List[str] = field(default_factory=list)
    shared_flow: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "query_sent": self.query_sent,
            "query_origin": self.query_origin,
            "syntax": self.syntax,
            "translation": self.translation,
            "capabilities_status": self.status,
            "verified_on": self.verified_on,
            "native_filters": dict(self.native_filters),
            "local_filters": dict(self.local_filters),
            "ordering": self.ordering,
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "shared_flow": self.shared_flow,
        }


_FILTER_NAMES = {"dates": "dates", "languages": "langues"}


@dataclass
class QueryPlan:
    """Plan d'une moisson : une entrée par source, dans l'ordre demandé."""

    original_query: str
    query_variants: Dict[str, str]
    languages: List[str]
    mode: str
    from_date: Optional[str]
    to_date: Optional[str]
    sources: List[SourcePlan]
    parsed: Dict[str, ParsedQuery]           # "original" et variantes
    warnings: List[str] = field(default_factory=list)
    hal_fulltext: bool = False               # PAT-22

    def for_source(self, key: str) -> Optional[SourcePlan]:
        for entry in self.sources:
            if entry.source == key:
                return entry
        return None

    def as_dict(self, execution: Optional[Dict[str, Dict[str, Any]]] = None
                ) -> Dict[str, Any]:
        """
        Plan sérialisable (``structuredContent.query_plan``).

        Args:
            execution: par source, ce qui a été appliqué à l'exécution
                (compteurs du filtre local de langue…).
        """
        sources = []
        for entry in self.sources:
            data = entry.as_dict()
            if execution and entry.source in execution:
                data["applied"] = execution[entry.source]
            sources.append(data)
        return {
            "version": PLAN_VERSION,
            "original_query": self.original_query,
            "query_variants": dict(self.query_variants),
            "languages": list(self.languages),
            "mode": self.mode,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "automatic_translation": False,
            "derived": {name: p.summary() for name, p in self.parsed.items()},
            "sources": sources,
            "warnings": list(self.warnings),
            "ambiguities": self.ambiguities,
            "hal_fulltext": self.hal_fulltext,
        }

    @property
    def ambiguities(self) -> List[Dict[str, str]]:
        """Requêtes (d'origine ou variantes) dont la lecture dépend de la
        précédence de OR : ``{"query": nom, "reading": lecture retenue}``."""
        return [{"query": name, "reading": p.ambiguity}
                for name, p in self.parsed.items() if p.ambiguity]

    def ambiguity_lines(self) -> List[str]:
        """Avertissements à placer en tête du retour (PAT-19)."""
        lines = []
        for a in self.ambiguities:
            which = ("" if a["query"] == "original"
                     else f" (variante {a['query']})")
            lines.append(f"Attention : requête{which} ambiguë, lue "
                         f"« {a['reading']} » ; ajouter des parenthèses pour "
                         f"fixer la lecture.")
        return lines

    @property
    def trivial(self) -> bool:
        """Requête transmise telle quelle partout, sans avertissement ni
        variante ni filtre de langue."""
        return (all(e.translation == "unchanged" for e in self.sources)
                and not self.warnings and not self.query_variants
                and not self.languages
                and not any(e.warnings for e in self.sources))

    def summary_lines(self, indent: str = "  ") -> List[str]:
        """
        Résumé court (retour MCP) : une ligne quand le plan est trivial,
        sinon ``text_lines`` ; le détail reste dans ``as_dict``.
        """
        if self.trivial:
            return ["Plan de requête : requête transmise telle quelle à "
                    "toutes les sources (détail : "
                    "structuredContent.query_plan)."]
        return self.text_lines(indent)

    def text_lines(self, indent: str = "  ") -> List[str]:
        """Résumé lisible (retour MCP, CLI) : sources de même envoi groupées."""
        lines = [f"Plan de requête (mode {self.mode} ; aucune traduction "
                 f"automatique) :",
                 f"{indent}Requête d'origine : {self.original_query}"]
        if self.query_variants:
            lines.append(f"{indent}Variantes : " + " ; ".join(
                f"{k} = {v}" for k, v in self.query_variants.items()))
        if self.languages:
            lines.append(f"{indent}Langues : {', '.join(self.languages)}")
        groups: Dict[Tuple, List[SourcePlan]] = {}
        for entry in self.sources:
            key = (entry.query_sent, entry.syntax, entry.translation,
                   tuple(sorted(entry.native_filters)),
                   tuple(sorted(entry.local_filters)))
            groups.setdefault(key, []).append(entry)
        for (query_sent, syntax, translation, native, local), entries in \
                groups.items():
            names = ", ".join(e.label for e in entries)
            how = SYNTAX_LABELS.get(syntax, syntax)
            if translation == "unchanged":
                how += ", requête transmise telle quelle"
            elif translation == "bag_of_words":
                how += ", variante dérivée"
            bits = [f"« {query_sent} »"]
            if native:
                bits.append("filtres natifs : " + ", ".join(
                    _FILTER_NAMES.get(n, n) for n in native))
            if local:
                bits.append("filtres locaux : " + ", ".join(
                    _FILTER_NAMES.get(n, n) for n in local))
            lines.append(f"{indent}- {names} ({how}) : " + " ; ".join(bits))
        seen: Dict[str, List[str]] = {}
        for entry in self.sources:
            for w in entry.warnings:
                seen.setdefault(w, []).append(entry.label)
        if seen or self.warnings:
            lines.append(f"{indent}Avertissements :")
            for w in self.warnings:
                lines.append(f"{indent}  - {w}")
            for w, labels in seen.items():
                lines.append(f"{indent}  - {', '.join(labels)} : {w}")
        return lines


def _date_granularity_warning(caps: SourceCapabilities,
                              from_date: Optional[str],
                              to_date: Optional[str]) -> Optional[str]:
    full = [d for d in (from_date, to_date) if d and len(d) == 10]
    if caps.date_granularity == "year" and full:
        return ("bornes de dates ramenées à l'année ("
                + ", ".join(f"{d} → {d[:4]}" for d in full) + ")")
    return None


# Texte intégral de HAL (PAT-22) : le champ par défaut (``text``) couvre les
# métadonnées (titre, résumé, mots-clés) ; ``fulltext_t`` indexe le texte des
# dépôts avec fichier. Sondes du 2026-09-30 (rows=0) : « "modèle actantiel" »
# rend 7 notices (toutes par le résumé), fulltext_t 224, dont 221 hors
# métadonnées, l'union 228 ; texte intégral indexé pour 1 704 036 des
# 4 679 411 documents (36 %).
HAL_FULLTEXT_FIELD = "fulltext_t"
HAL_FULLTEXT_WARNING = (
    "texte intégral (hal_fulltext) : HAL cherche aussi dans le texte des "
    "dépôts avec fichier (fulltext_t, 36 % des dépôts) ; ensemble bien plus "
    "large et plus bruité (sonde du 2026-09-30 : « \"modèle actantiel\" », 7 "
    "notices par les métadonnées, 228 avec le texte intégral)")


def hal_fulltext_query(sent: str) -> str:
    """Requête HAL étendue au texte intégral (PAT-22) : métadonnées (champ
    par défaut) OU texte intégral des dépôts avec fichier."""
    return f"({sent}) OR {HAL_FULLTEXT_FIELD}:({sent})"


def build_query_plan(
    query: str,
    sources: Sequence[str],
    *,
    query_variants: Optional[Dict[str, str]] = None,
    languages: Optional[Sequence[str]] = None,
    mode: str = "bibliographie",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    hal_fulltext: bool = False,
) -> QueryPlan:
    """
    Construit le plan de requête d'une moisson (sources déjà résolues).

    ``hal_fulltext`` (PAT-22) : la requête envoyée à HAL cherche aussi dans
    le texte intégral des dépôts (``(q) OR fulltext_t:(q)``), avec un
    avertissement ; sans effet sur les autres sources.

    Raises:
        QueryPlanError: requête ou variante vide ou sans terme, code de
            langue invalide, borne de date invalide ou plage vide.
    """
    variants = normalize_variants(query_variants)
    langs = normalize_languages(languages)
    from_date = validate_date(from_date, "from_date")
    to_date = validate_date(to_date, "to_date")
    check_date_range(from_date, to_date)

    parsed: Dict[str, ParsedQuery] = {"original": parse_query(query)}
    for lang, text in variants.items():
        parsed[lang] = parse_query(text, label=f"variante '{lang}'")
    original = parsed["original"]

    plan_warnings: List[str] = []
    for w in original.warnings:
        plan_warnings.append(f"requête d'origine : {w}")
    for lang in variants:
        for w in parsed[lang].warnings:
            plan_warnings.append(f"variante '{lang}' : {w}")

    entries: List[SourcePlan] = []
    used_variants = set()
    english_without_variant = False
    for key in sources:
        caps = get_capabilities(key)
        if caps is None:
            # Source sans description : requête d'origine telle quelle.
            entries.append(SourcePlan(
                source=key, label=key, query_sent=original.text,
                query_origin="original", syntax="unknown",
                translation="unchanged", status="inconnu", verified_on="",
                warnings=["capacités de requête inconnues : requête "
                          "d'origine transmise telle quelle"]))
            continue
        lang = caps.corpus_language
        if lang != CORPUS_MULTI and lang in variants:
            base, origin = parsed[lang], f"variant:{lang}"
            used_variants.add(lang)
        else:
            base, origin = original, "original"
        syntax = caps.syntax_for(mode)
        sent, translation, warnings, notes = render_for_source(base, caps,
                                                               syntax)
        if hal_fulltext and key == "hal":
            sent = hal_fulltext_query(sent)
            warnings.append(HAL_FULLTEXT_WARNING)
        if lang == CORPUS_EN and origin == "original":
            english_without_variant = True
            kind = ("requête française" if looks_french(base.text)
                    else "requête d'origine")
            warnings.insert(0, f"{kind} envoyée à une source majoritairement "
                               f"anglophone (aucune variante 'en' fournie)")
        if caps.apostrophe_restricts and "'" in base.text.replace("’", "'"):
            notes.append(f"apostrophe : {caps.apostrophe}")
        if caps.accents == "significant" and any(
                ord(c) > 127 and c.isalpha() for c in base.text):
            notes.append(
                "accents significatifs pour " + caps.label + " : une notice "
                "écrite sans accent n'est pas retrouvée par un mot accentué "
                "(et inversement)")

        entry = SourcePlan(
            source=key, label=caps.label, query_sent=sent,
            query_origin=origin, syntax=syntax, translation=translation,
            status=caps.status, verified_on=caps.verified_on,
            ordering=caps.ordering.get(mode, ""), warnings=warnings,
            notes=notes, shared_flow=caps.proxy_of or (
                "isidore" if key == "isidore" else None),
        )
        # Dates
        if from_date or to_date:
            span = f"{from_date or '…'} à {to_date or '…'}"
            if caps.dates == FILTER_NATIVE:
                entry.native_filters["dates"] = f"{span} — {caps.dates_detail}"
            else:
                entry.local_filters["dates"] = f"{span} — {caps.dates_detail}"
            gran = _date_granularity_warning(caps, from_date, to_date)
            if gran:
                entry.warnings.append(gran)
        # Langues
        if langs:
            label = ", ".join(langs)
            if caps.languages == FILTER_NATIVE:
                entry.native_filters["languages"] = (
                    f"{label} — {caps.languages_detail}")
                if caps.language_transport == LANG_VIA_SEARCH:
                    entry.search_kwargs["languages"] = list(langs)
                elif caps.language_transport == LANG_VIA_LR:
                    entry.connector_kwargs["lr"] = "|".join(
                        f"lang_{code}" for code in langs)
            elif caps.languages == FILTER_LOCAL:
                entry.local_filters["languages"] = (
                    f"{label} — {caps.languages_detail} ; notices sans "
                    f"langue conservées ; appliqué après le plafond par "
                    f"source")
                entry.local_languages = list(langs)
            else:
                entry.warnings.append(
                    f"filtre de langue impossible : {caps.languages_detail} ; "
                    f"notices non filtrées")
        entries.append(entry)

    for lang in variants:
        if lang not in used_variants:
            plan_warnings.append(
                f"variante '{lang}' non utilisée : aucune source sélectionnée "
                f"n'a ce fonds majoritaire (fr pour les sources francophones, "
                f"en pour CrossRef, OpenAlex, Semantic Scholar et CORE)")
    if hal_fulltext and "hal" not in sources:
        plan_warnings.append("hal_fulltext sans effet : HAL n'est pas "
                             "interrogé")
    if english_without_variant:
        plan_warnings.append(
            "aucune variante 'en' : les sources internationales reçoivent la "
            "requête d'origine, sans traduction automatique (fournir "
            "query_variants={\"en\": \"…\"} pour les interroger en anglais)")
    return QueryPlan(
        original_query=original.text, query_variants=variants,
        languages=langs, mode=mode, from_date=from_date, to_date=to_date,
        sources=entries, parsed=parsed, warnings=plan_warnings,
        hal_fulltext=bool(hal_fulltext),
    )


def plan_for_request(request) -> QueryPlan:
    """Plan d'une ``engine.models.SearchRequest``."""
    return build_query_plan(
        request.query, request.sources,
        query_variants=request.query_variants, languages=request.languages,
        mode=request.mode, from_date=request.from_date,
        to_date=request.to_date,
        hal_fulltext=bool(getattr(request, "hal_fulltext", False)),
    )


def supported_kwargs(func, candidates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sous-ensemble de ``candidates`` que ``func`` accepte : paramètre nommé,
    ou ``**kwargs`` (``engine.orchestrator.run_source`` retire ``mode`` et
    ``languages`` de ses ``**kwargs`` : le constructeur du connecteur ne les
    reçoit jamais). Une valeur None n'est jamais transmise ; sans signature
    lisible : aucun (comportement historique).
    """
    wanted = {k: v for k, v in (candidates or {}).items() if v is not None}
    if not wanted:
        return {}
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return wanted
    named = {name for name, p in params.items()
             if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    return {k: v for k, v in wanted.items() if k in named}
