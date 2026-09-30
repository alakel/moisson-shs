"""
Export des notices Zotero internes au format BibTeX ou BibLaTeX (OPT-18).

Tâche #20 du sprint 03, jamais livrée jusqu'ici. Deux dialectes :

- ``bibtex`` (défaut) : BibTeX classique (``@article``, ``@incollection``,
  ``@phdthesis``, ``@techreport``, ``@misc``…), ``year`` + ``month`` en macro.
  C'est la forme la plus sûre pour l'import dans Zotero Desktop
  (Fichier > Importer) ; biber la lit aussi (types et champs hérités).
- ``biblatex`` : types et champs natifs de BibLaTeX (``@thesis`` avec
  ``type``, ``@report``, ``@online``, ``@inbook``, ``@dataset``…),
  ``date`` ISO 8601, ``journaltitle``, ``location``, ``langid``, ``eprint``.

Règles communes :

- **Clés de citation stables et déterministes** : ``nom`` du premier auteur
  (particules en minuscules retirées) + ``année`` (``nd`` sans date) + premier
  mot significatif du titre, en ASCII minuscule (``foucault1975surveiller``).
  Collision : suffixes ``a``, ``b``, ``c``… attribués dans un ordre fixé par
  le contenu des notices (DOI, titre, date, auteur, URL), jamais par leur
  ordre d'arrivée : permuter le corpus ne change aucune clé.
- **Échappement LaTeX** de ``& % $ # _ { } ~ ^ \\`` ; accolades littérales
  écrites ``\\{ \\}`` quand elles sont équilibrées, ``\\textbraceleft{}`` /
  ``\\textbraceright{}`` sinon (BibTeX compte les accolades même précédées
  d'une contre-oblique). Accents conservés en UTF-8 par défaut, ou convertis
  en commandes LaTeX (``{\\'e}``, ``{\\c{c}}``, ``{\\oe}``…) avec
  ``ascii=True``. URL et DOI sont écrits tels quels (champs verbatim de
  biber), seuls ``{ } \\`` et les blancs y sont percent-encodés.
- **Auteurs** ``Nom, Prénom and …`` ; auteur institutionnel (champ ``name``)
  entre accolades (``{Organisation mondiale de la santé}``).
- **Majuscules protégées** dans les titres (``{Foucault}``, ``{HAL}``) pour
  que les styles qui passent les titres en minuscules ne les altèrent pas ;
  Zotero retire ces accolades à l'import.
- Champ ``annote`` alimenté par une ligne ``Annote:`` de ``extra``.
- **Aucune métadonnée de moisson** (invariant 1) : ni ligne ``Sources``, ni
  identifiant natif, ni ``Cited by``, ni rang ou score (ces derniers vivent
  dans le manifeste, jamais dans les notices). Le champ ``extra`` n'est
  exporté que sur demande explicite (``include_provenance=True``), dans
  ``note``.
- **Prudence bibliographique** (invariant 6) : un extrait Google Scholar
  placé dans ``abstractNote`` n'est pas exporté comme résumé ; une notice
  marquée ``Scholar truncation`` (auteurs et/ou revue coupés par Google
  Scholar) porte **par défaut** un avertissement dans ``note`` (``N1`` en
  RIS), et ``and others`` clôt sa liste d'auteurs quand la troncature de
  cette liste est établie (voir ``scholar_truncation``). Ce sont des mises
  en garde de fiabilité, pas de la provenance de moisson.
- Les champs migrés dans ``extra`` par ``validate_and_clean`` (types Zotero
  sans ce champ : ``publicationTitle: …``, ``DOI: …``, ``pages: …``) sont
  relus : ce sont des données bibliographiques, pas de la provenance.

Le module porte aussi le **service d'export de fichier** partagé par l'outil
MCP ``corpus_export`` et la sous-commande ``moisson-shs export`` : lecture et
contrôle du corpus d'entrée (refus d'un CSL-JSON, d'un fichier associé),
nom de sortie unique, publication atomique tracée dans un manifeste
(``publish_export``, ``utils.manifest.Publication``). Le format RIS est rendu par
``mappers.ris_exporter``, qui réutilise les aides communes définies ici.

Références : BibTeX (Patashnik, « BibTeXing », 1988), BibLaTeX
(manuel, §2.1 types, §2.2 champs), biber (champs verbatim ``url``, ``doi``).
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote

from unidecode import unidecode

from utils.identity import extra_lines, extra_values, normalize_doi


# =====================================================================
# Constantes
# =====================================================================

DIALECT_BIBTEX = "bibtex"
DIALECT_BIBLATEX = "biblatex"
DIALECTS = (DIALECT_BIBTEX, DIALECT_BIBLATEX)

# Formats du service d'export → extension du fichier produit.
EXPORT_FORMATS: Dict[str, str] = {
    "bibtex": ".bib",
    "biblatex": ".bib",
    "ris": ".ris",
}

# Types MIME annoncés pour les fichiers produits (lien de ressource,
# contenu inliné). Pas de type enregistré à l'IANA pour l'un ni l'autre :
# ce sont les types d'usage courant (Zotero, gestionnaires de références).
EXPORT_MIME: Dict[str, str] = {
    "bibtex": "application/x-bibtex",
    "biblatex": "application/x-bibtex",
    "ris": "application/x-research-info-systems",
}

EXPORT_LABELS: Dict[str, str] = {
    "bibtex": "BibTeX",
    "biblatex": "BibLaTeX",
    "ris": "RIS",
}

# itemType Zotero → type d'entrée BibTeX classique.
BIBTEX_TYPES: Dict[str, str] = {
    "journalArticle": "article",
    "magazineArticle": "article",
    "newspaperArticle": "article",
    "book": "book",
    "bookSection": "incollection",
    "encyclopediaArticle": "incollection",
    "dictionaryEntry": "incollection",
    "conferencePaper": "inproceedings",
    "thesis": "phdthesis",            # ou mastersthesis (voir _is_masters)
    "report": "techreport",
    "manuscript": "unpublished",
    # Tout le reste (preprint, webpage, blogPost, document, dataset,
    # presentation, letter, computerProgram, enregistrements…) → misc.
}

# itemType Zotero → type d'entrée BibLaTeX.
BIBLATEX_TYPES: Dict[str, str] = {
    "journalArticle": "article",
    "magazineArticle": "article",
    "newspaperArticle": "article",
    "book": "book",
    "bookSection": "incollection",    # ou inbook (auteur du livre connu)
    "encyclopediaArticle": "inreference",
    "dictionaryEntry": "inreference",
    "conferencePaper": "inproceedings",
    "thesis": "thesis",
    "report": "report",
    "manuscript": "unpublished",
    "preprint": "online",
    "webpage": "online",
    "blogPost": "online",
    "dataset": "dataset",
    "computerProgram": "software",
    "patent": "patent",
}

# Rôles Zotero → champ de personnes. Les rôles « de type auteur » secondaires
# ne deviennent auteurs qu'en l'absence de tout créateur ``author``.
_AUTHOR_LIKE_ROLES = frozenset({
    "contributor", "presenter", "interviewee", "cartographer", "inventor",
    "wordsBy", "director", "artist", "performer", "composer", "programmer",
    "podcaster", "sponsor", "castMember", "guest", "counsel",
    "attorneyAgent", "recipient",
})

# Champs Zotero relus dans ``extra`` (ligne ``<champ>: valeur``) quand
# ``validate_and_clean`` les y a migrés faute de place dans le type.
MIGRATED_FIELDS = frozenset({
    "publicationTitle", "bookTitle", "proceedingsTitle", "conferenceName",
    "encyclopediaTitle", "dictionaryTitle", "websiteTitle", "blogTitle",
    "volume", "issue", "pages", "numPages", "publisher", "place", "ISBN",
    "ISSN", "DOI", "language", "university", "institution", "thesisType",
    "reportType", "reportNumber", "edition", "series", "seriesTitle",
    "seriesNumber", "repository", "archiveID", "journalAbbreviation",
    "shortTitle", "accessDate",
})

# Clé de la ligne d'``extra`` qui alimente ``annote`` (casse indifférente).
ANNOTE_KEY = "annote"

# Ligne d'``extra`` posée par le mapper Google Scholar quand Google a coupé
# les auteurs et/ou la revue par des points de suspension (retirés par le
# mapper) ; la fusion ne la garde que si l'un de ces champs vient de Scholar.
SCHOLAR_TRUNCATION_KEY = "Scholar truncation"

# Avertissement écrit dans l'export (``note`` en BibTeX et BibLaTeX, ``N1`` en
# RIS) pour une notice marquée ``Scholar truncation``, même sans
# ``include_provenance`` : c'est une mise en garde de fiabilité bibliographique
# (invariant 6), pas une métadonnée de moisson (invariant 1). Texte fixe
# (sortie déterministe), jamais recopié d'``extra``.
SCHOLAR_TRUNCATION_WARNING = (
    "Avertissement : auteurs et/ou revue tronqués par Google Scholar, "
    "à vérifier contre la source primaire avant citation.")

_MONTH_MACROS = ("jan", "feb", "mar", "apr", "may", "jun",
                 "jul", "aug", "sep", "oct", "nov", "dec")

# Code de langue (ISO 639-1/2 ou nom) → langue babel/polyglossia (``langid``).
_LANGID: Dict[str, str] = {
    "fr": "french", "fre": "french", "fra": "french", "french": "french",
    "francais": "french",
    "en": "english", "eng": "english", "english": "english",
    "anglais": "english",
    "de": "ngerman", "ger": "ngerman", "deu": "ngerman", "german": "ngerman",
    "allemand": "ngerman",
    "es": "spanish", "spa": "spanish", "spanish": "spanish",
    "espagnol": "spanish",
    "it": "italian", "ita": "italian", "italian": "italian",
    "italien": "italian",
    "pt": "portuguese", "por": "portuguese", "portuguese": "portuguese",
    "portugais": "portuguese",
    "nl": "dutch", "dut": "dutch", "nld": "dutch", "dutch": "dutch",
    "ca": "catalan", "cat": "catalan", "catalan": "catalan",
    "da": "danish", "dan": "danish", "danish": "danish",
    "sv": "swedish", "swe": "swedish", "swedish": "swedish",
    "no": "norsk", "nb": "norsk", "nor": "norsk", "nob": "norsk",
    "nn": "nynorsk",
    "fi": "finnish", "fin": "finnish", "finnish": "finnish",
    "pl": "polish", "pol": "polish", "polish": "polish",
    "ru": "russian", "rus": "russian", "russian": "russian",
    "cs": "czech", "cze": "czech", "ces": "czech", "czech": "czech",
    "el": "greek", "gre": "greek", "ell": "greek", "greek": "greek",
    "tr": "turkish", "tur": "turkish", "turkish": "turkish",
    "ro": "romanian", "rum": "romanian", "ron": "romanian",
    "hu": "magyar", "hun": "magyar",
    "la": "latin", "lat": "latin", "latin": "latin",
}

# Mots vides ignorés pour le mot du titre et le nom institutionnel des clés.
_KEY_STOPWORDS = frozenset("""
a an the of on in and or for to with from by at as is are
le la les l un une des de du d et ou en au aux sur pour par dans avec sans
sous entre vers chez ce ces cette cet son sa ses leur leurs qu que qui
el los las lo del y con por para una unos unas
der die das dem den des ein eine einer und im zu von mit fur uber
il gli i uno della delle dei di e per
""".split())

# Longueur maximale du mot de titre dans une clé.
_KEY_WORD_MAX = 24


# =====================================================================
# Lecture des champs (commune BibTeX / RIS)
# =====================================================================

def _clean_str(value: Any) -> str:
    """Valeur scalaire → chaîne débarrassée des blancs de bordure."""
    if value is None or isinstance(value, (list, dict)):
        return ""
    return str(value).strip()


def field_value(item: Dict, name: str) -> str:
    """
    Valeur d'un champ Zotero : le champ lui-même, ou à défaut la ligne
    ``<name>: …`` d'``extra`` si ``name`` fait partie des champs que
    ``validate_and_clean`` migre (``MIGRATED_FIELDS``).
    """
    value = _clean_str(item.get(name))
    if value or name not in MIGRATED_FIELDS:
        return value
    for v in extra_values(item, name):
        v = _clean_str(v)
        if v:
            return v
    return ""


_DOI_PREFIX_STRIP_RE = re.compile(
    r"^(?:(?:https?://)?(?:dx\.|www\.)?doi\.org/|doi\s*:\s*)", re.IGNORECASE)


def export_doi(item: Dict) -> str:
    """
    DOI déclaré d'une notice, casse d'origine conservée, sans préfixe
    (``https://doi.org/``, ``doi:``) : champ ``DOI``, sinon première ligne
    ``DOI:`` valide d'``extra`` (jamais ``DOI source``). Chaîne vide sinon.
    """
    candidates = [item.get("DOI")] + extra_values(item, "DOI")
    for raw in candidates:
        raw = _clean_str(raw)
        if not raw or not normalize_doi(raw):
            continue
        value = _DOI_PREFIX_STRIP_RE.sub("", raw).strip().rstrip(".,;")
        if "%" in value:
            value = unquote(value)
        return value
    return ""


def annote_text(item: Dict) -> str:
    """Texte des lignes ``Annote:`` d'``extra`` (casse de la clé
    indifférente), jointes par une espace."""
    parts = [v for k, v in extra_lines(item)
             if k.lower() == ANNOTE_KEY and v]
    return " ".join(parts)


def exportable_abstract(item: Dict) -> Tuple[str, bool]:
    """
    Résumé exportable et indicateur « extrait Scholar écarté ».

    Un extrait Google Scholar (ligne ``Scholar snippet`` et résumé venu de
    Scholar) n'est pas un résumé d'éditeur : il n'est pas exporté
    (invariant 6).

    Returns:
        ``(résumé, écarté)`` ; ``("", True)`` pour un extrait écarté.
    """
    abstract = _clean_str(item.get("abstractNote"))
    if not abstract:
        return "", False
    from utils.deduplication import abstract_is_scholar_snippet
    if abstract_is_scholar_snippet(item):
        return "", True
    return abstract, False


def scholar_truncation(item: Dict) -> Tuple[bool, bool]:
    """
    Troncature Google Scholar d'une notice : ``(avertir, auteurs_incomplets)``.

    - ``avertir`` : ligne ``Scholar truncation`` présente dans ``extra`` ;
      l'export écrit alors ``SCHOLAR_TRUNCATION_WARNING`` par défaut.
    - ``auteurs_incomplets`` : la troncature de la **liste d'auteurs** est
      établie, soit des auteurs venus de Scholar
      (``utils.deduplication.authors_truncated``) dans une notice sans
      aucune revue ni ouvrage (``_container``) : la marque, qui couvre
      « auteurs et/ou revue », ne peut alors porter que sur les auteurs.
      Quand une revue est présente, la marque ne dit pas lequel des deux
      champs a été coupé (sur les notices Scholar de ``output/raw``, 1 263
      des 3 904 notices marquées n'ont qu'un ou deux auteurs visibles, cas
      où Scholar n'a pas lieu de couper la liste) : ``and others``
      affirmerait des auteurs manquants sans preuve, l'avertissement seul
      est écrit.
    """
    if not any(k == SCHOLAR_TRUNCATION_KEY for k, _ in extra_lines(item)):
        return False, False
    from utils.deduplication import authors_truncated
    return True, bool(authors_truncated(item) and not _container(item))


def provenance_lines(item: Dict) -> List[str]:
    """Lignes d'``extra`` exportées sur demande explicite (provenance de
    moisson), hors lignes ``Annote:`` et ``Scholar truncation``, rendues à
    part (annotation, avertissement de troncature)."""
    extra = item.get("extra") or ""
    if not isinstance(extra, str):
        return []
    out = []
    for line in extra.split("\n"):
        line = line.strip()
        if not line:
            continue
        key = line.split(":", 1)[0].strip() if ":" in line else ""
        if key.lower() == ANNOTE_KEY or key == SCHOLAR_TRUNCATION_KEY:
            continue
        out.append(line)
    return out


def item_tags(item: Dict) -> List[str]:
    """Mots-clés (tags Zotero) dédoublonnés, dans l'ordre."""
    seen = set()
    out = []
    for t in item.get("tags") or []:
        tag = _clean_str(t.get("tag") if isinstance(t, dict) else t)
        tag = re.sub(r"\s+", " ", tag)
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


_ISO_DATE_RE = re.compile(r"^\s*(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?:[T\s].*)?$")
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b")


def parse_date(value: Any) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Date Zotero → ``(année, mois, jour)`` (entiers ou ``None``).

    Formes ISO ``AAAA``, ``AAAA-MM``, ``AAAA-MM-JJ`` (heure ignorée) ; sinon,
    première année plausible trouvée dans le texte (``[s.d.]`` → rien).
    """
    text = _clean_str(value)
    if not text:
        return None, None, None
    m = _ISO_DATE_RE.match(text)
    if m:
        year = int(m.group(1))
        month = int(m.group(2)) if m.group(2) else None
        day = int(m.group(3)) if m.group(3) else None
        if month is not None and not 1 <= month <= 12:
            month, day = None, None
        if day is not None and not 1 <= day <= 31:
            day = None
        return year, month, day
    m = _YEAR_RE.search(text)
    if m:
        return int(m.group(1)), None, None
    return None, None, None


_PAGE_TOKEN = r"(?:[A-Za-z]{0,4}\d+[A-Za-z]?|[ivxlcdmIVXLCDM]+)"
_PAGE_RANGE_RE = re.compile(
    r"^\s*(?:pp?\.?\s*)?(" + _PAGE_TOKEN + r")\s*(?:-{1,3}|–|—)\s*("
    + _PAGE_TOKEN + r")\s*$")
_PAGE_SINGLE_RE = re.compile(
    r"^\s*(?:pp?\.?\s*)?([A-Za-z]{0,4}\d+[A-Za-z]?)\s*$")


def split_pages(value: Any) -> Tuple[str, str]:
    """
    Pages → ``(début, fin)``. ``"12-34"``, ``"p. 12 - 34"``, ``"e12–e34"``
    → ``("12", "34")`` ; page unique → ``("12", "")`` ; forme non reconnue
    (``"250 p."``, ``"publication électronique"``) → ``(texte, "")``.
    """
    text = _clean_str(value)
    if not text:
        return "", ""
    m = _PAGE_RANGE_RE.match(text)
    if m:
        start, end = m.group(1), m.group(2)
        return start, ("" if end == start else end)
    m = _PAGE_SINGLE_RE.match(text)
    if m:
        return m.group(1), ""
    return re.sub(r"\s+", " ", text), ""


def creators_by_role(item: Dict) -> Dict[str, List[Dict]]:
    """
    Créateurs regroupés par champ de personnes : ``author``, ``editor``,
    ``bookauthor``, ``translator`` ; les rôles secondaires de type auteur
    (contributeur, présentateur…) ne deviennent auteurs qu'en l'absence de
    tout ``author``.
    """
    roles: Dict[str, List[Dict]] = defaultdict(list)
    secondary: List[Dict] = []
    for c in item.get("creators") or []:
        if not isinstance(c, dict):
            continue
        ctype = c.get("creatorType") or "author"
        if ctype == "author":
            roles["author"].append(c)
        elif ctype == "editor":
            roles["editor"].append(c)
        elif ctype == "bookAuthor":
            roles["bookauthor"].append(c)
        elif ctype == "translator":
            roles["translator"].append(c)
        elif ctype in _AUTHOR_LIKE_ROLES:
            secondary.append(c)
    if not roles.get("author") and secondary:
        roles["author"] = secondary
    return dict(roles)


def creator_parts(c: Dict) -> Tuple[str, str, bool]:
    """``(nom, prénom, institutionnel)`` d'un créateur Zotero ; un créateur à
    champ unique (``name``) est institutionnel."""
    last = re.sub(r"\s+", " ", _clean_str(c.get("lastName")))
    first = re.sub(r"\s+", " ", _clean_str(c.get("firstName")))
    name = re.sub(r"\s+", " ", _clean_str(c.get("name")))
    if last:
        return last, first, False
    if name:
        return name, "", True
    if first:
        return first, "", True
    return "", "", False


# =====================================================================
# Échappement LaTeX
# =====================================================================

# Caractères spéciaux ASCII (dans les deux modes).
_ASCII_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

# Accents combinants → commande LaTeX (mode ASCII).
_COMBINING = {
    "\u0300": "`", "\u0301": "'", "\u0302": "^", "\u0303": "~",
    "\u0308": '"', "\u0304": "=", "\u0307": ".", "\u030a": "r",
    "\u030b": "H", "\u030c": "v", "\u0306": "u", "\u0327": "c",
    "\u0328": "k", "\u0323": "d", "\u0331": "b",
}

# Lettres et signes sans décomposition utile (mode ASCII).
_SPECIAL_CHARS = {
    "œ": r"{\oe}", "Œ": r"{\OE}", "æ": r"{\ae}", "Æ": r"{\AE}",
    "ß": r"{\ss}", "ø": r"{\o}", "Ø": r"{\O}", "å": r"{\aa}", "Å": r"{\AA}",
    "ł": r"{\l}", "Ł": r"{\L}", "ı": r"{\i}", "ȷ": r"{\j}",
    "ð": r"{\dh}", "Ð": r"{\DH}", "þ": r"{\th}", "Þ": r"{\TH}",
    "đ": r"{\dj}", "Đ": r"{\DJ}",
    "«": r"{\guillemotleft}", "»": r"{\guillemotright}",
    "‹": r"{\guilsinglleft}", "›": r"{\guilsinglright}",
    "–": "--", "—": "---", "‐": "-", "‑": "-", "−": "-",
    "‘": "`", "’": "'", "‚": ",", "“": "``", "”": "''", "„": ",,",
    "…": r"{\ldots}", "\u00a0": "~", "\u202f": r"{\,}", "\u2009": r"{\,}",
    "€": r"{\texteuro}", "£": r"{\pounds}", "§": r"{\S}", "¶": r"{\P}",
    "©": r"{\textcopyright}", "®": r"{\textregistered}",
    "°": r"{\textdegree}", "×": r"{\texttimes}", "·": r"{\textperiodcentered}",
    "¿": r"{?`}", "¡": r"{!`}", "•": r"{\textbullet}",
    "ª": r"{\textordfeminine}", "º": r"{\textordmasculine}",
    "´": "'", "¨": '"', "¸": ",",
}

_WS_RE = re.compile(r"[ \t\r\n\f\v\u2028\u2029]+")
# Caractères de contrôle (C0, DEL, C1), et caractères de mise en forme
# invisibles sans valeur textuelle (espace sans chasse, gluon de mots, BOM,
# trait d'union conditionnel, marques et plongements bidirectionnels) :
# absents du texte affiché, ils font échouer pdfLaTeX (caractère Unicode non
# défini) et Zotero les importerait tels quels. Les liants ZWJ/ZWNJ,
# significatifs dans certaines écritures, sont conservés.
_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u00ad\u200b\u200e\u200f"
    r"\u202a-\u202e\u2060\u2066-\u2069\ufeff]")

# Contrôles C1 (U+0080 à U+009F) : jamais du texte légitime. Presque
# toujours des octets Windows-1252 lus comme du Latin-1 (U+0092 pour
# l'apostrophe de « l'âge », U+0091 et U+0092 autour de « ciencia » dans
# des résumés réels) : rendus par le caractère voulu. Exception : U+0098 et
# U+009C encadrent, dans les données de catalogue, un article à ignorer au
# classement (marqueurs NSB/NSE : U+0098 « The » U+009C « European
# Union ») ; quand U+0098 est présent, les deux sont retirés. U+0088 et
# U+0098 (accents isolés) et les positions non définies en Windows-1252
# (U+0081, U+008D, U+008F, U+0090, U+009D) sont retirés.
_CP1252_C1 = {
    "\x80": "€", "\x82": "‚", "\x83": "ƒ", "\x84": "„", "\x85": "…",
    "\x86": "†", "\x87": "‡", "\x89": "‰", "\x8a": "Š", "\x8b": "‹",
    "\x8c": "Œ", "\x8e": "Ž", "\x91": "‘", "\x92": "’", "\x93": "“",
    "\x94": "”", "\x95": "•", "\x96": "–", "\x97": "—", "\x99": "™",
    "\x9a": "š", "\x9b": "›", "\x9c": "œ", "\x9e": "ž", "\x9f": "Ÿ",
}
_C1_RE = re.compile(r"[\x80-\x9f]")
_NSB, _NSE = "\x98", "\x9c"


def _repair_c1(s: str) -> str:
    """Contrôles C1 → caractère Windows-1252 voulu, ou retrait (voir
    ``_CP1252_C1``)."""
    if not _C1_RE.search(s):
        return s
    if _NSB in s:
        s = s.replace(_NSB, "").replace(_NSE, "")
    return _C1_RE.sub(lambda m: _CP1252_C1.get(m.group(), ""), s)


def normalize_space(text: Any) -> str:
    """Blancs ASCII (et séparateurs de ligne Unicode) réduits à une espace,
    contrôles C1 réparés (``_repair_c1``, avant tout retrait des blancs de
    bordure : Python tient U+0085 pour un blanc), caractères de contrôle et
    de mise en forme invisibles retirés ; espaces insécables conservées."""
    if text is None or isinstance(text, (list, dict)):
        return ""
    s = _CONTROL_RE.sub("", _repair_c1(str(text)))
    return _WS_RE.sub(" ", s).strip()


def strip_controls(text: Any) -> str:
    """Valeur verbatim (URL, DOI) débarrassée des caractères de contrôle et
    invisibles, C1 compris, **sans** réparation : rien n'est deviné dans un
    identifiant."""
    return _CONTROL_RE.sub("", _clean_str(text))


def _braces_balanced(text: str) -> bool:
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _accent_to_latex(ch: str) -> Optional[str]:
    """Lettre accentuée → commande LaTeX (``{\\'e}``, ``{\\c{c}}``,
    accents multiples imbriqués) ; ``None`` si non représentable."""
    decomposed = unicodedata.normalize("NFD", ch)
    base, marks = decomposed[0], decomposed[1:]
    if not marks or not base.isascii() or not base.isalpha():
        return None
    if any(m not in _COMBINING for m in marks):
        return None
    if base == "i":
        base = r"\i"
    elif base == "j":
        base = r"\j"
    if len(marks) == 1:
        cmd = _COMBINING[marks[0]]
        if cmd.isalpha():
            # Accent désigné par une lettre (\\c, \\v…) : argument entre
            # accolades, sans quoi « \\cc » serait lu comme une commande.
            return "{\\" + cmd + "{" + base + "}}"
        return "{\\" + cmd + base + "}"
    inner = base
    for mark in marks:
        inner = "\\" + _COMBINING[mark] + "{" + inner + "}"
    return "{" + inner + "}"


def _non_ascii_to_latex(ch: str) -> str:
    """Caractère non ASCII → LaTeX ASCII (table, accents, puis translittération
    ``unidecode`` en dernier recours, elle-même échappée)."""
    if ch in _SPECIAL_CHARS:
        return _SPECIAL_CHARS[ch]
    accented = _accent_to_latex(ch)
    if accented is not None:
        return accented
    # Formes de compatibilité (ligatures « ﬀ », exposants, pleine chasse) :
    # décomposition NFKC caractère par caractère, jamais sur tout le texte
    # (NFKC changerait l'espace insécable en espace ordinaire).
    compat = unicodedata.normalize("NFKC", ch)
    if compat != ch:
        return "".join(
            _ASCII_SPECIALS.get(c, c) if ord(c) < 128 and c not in "{}"
            else ("" if c in "{}" else _non_ascii_to_latex(c))
            for c in compat)
    fallback = unidecode(ch)
    return "".join(_ASCII_SPECIALS.get(c, c) for c in fallback
                   if c not in "{}")


def latex_escape(text: Any, *, ascii: bool = False,
                 braces_balanced: Optional[bool] = None) -> str:
    """
    Échappe un texte pour une valeur de champ BibTeX.

    - ``& % $ # _ ~ ^ \\`` → ``\\& \\% \\$ \\# \\_ \\textasciitilde{}
      \\textasciicircum{} \\textbackslash{}`` ;
    - ``{ }`` → ``\\{ \\}`` si les accolades du texte sont équilibrées,
      sinon ``\\textbraceleft{} \\textbraceright{}`` : BibTeX compte les
      accolades même précédées d'une contre-oblique, une accolade seule
      rendrait le fichier illisible ;
    - ``ascii=False`` (défaut) : caractères non ASCII conservés (UTF-8,
      forme NFC) ; ``ascii=True`` : commandes LaTeX (``{\\'e}``, ``{\\oe}``,
      ``--``, ``{\\guillemotleft}``…), translittération en dernier recours
      (perte pour les écritures non latines : garder l'UTF-8 dans ce cas).

    Blancs réduits à une espace ; caractères de contrôle retirés.
    ``braces_balanced`` impose le mode d'écriture des accolades (utile quand
    un texte est échappé mot à mot) ; par défaut, calculé sur ``text``.
    """
    s = normalize_space(text)
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    balanced = (_braces_balanced(s) if braces_balanced is None
                else braces_balanced)
    out = []
    for ch in s:
        if ch in _ASCII_SPECIALS:
            out.append(_ASCII_SPECIALS[ch])
        elif ch == "{":
            out.append(r"\{" if balanced else r"\textbraceleft{}")
        elif ch == "}":
            out.append(r"\}" if balanced else r"\textbraceright{}")
        elif ascii and ord(ch) > 127:
            out.append(_non_ascii_to_latex(ch))
        else:
            out.append(ch)
    return "".join(out)


def _needs_case_protection(word: str, first: bool) -> bool:
    """Vrai si un mot de titre porte une majuscule qu'un style BibTeX
    pourrait abaisser : toute majuscule hors du premier mot ; dans le
    premier mot, une majuscule au-delà de la première lettre (sigle)."""
    letters = [c for c in word if c.isalpha()]
    upper = [i for i, c in enumerate(letters) if c.isupper()]
    if not upper:
        return False
    if first:
        return any(i > 0 for i in upper)
    return True


def protect_title(text: Any, *, ascii: bool = False) -> str:
    """Titre échappé, mots porteurs de majuscules entre accolades
    (``Les archives de {Foucault}``, ``{HAL} et la science ouverte``)."""
    s = normalize_space(text)
    if not s:
        return ""
    # L'équilibre des accolades littérales se juge sur le titre entier (un
    # mot isolé peut n'en porter qu'une) : il décide de leur écriture.
    whole_balanced = _braces_balanced(s)
    words = s.split(" ")
    out = []
    for i, word in enumerate(words):
        esc = latex_escape(word, ascii=ascii, braces_balanced=whole_balanced)
        if not esc:
            continue
        if _needs_case_protection(word, first=(i == 0)):
            # Pour BibTeX, un groupe de niveau 0 ouvert par une contre-oblique
            # est un « caractère spécial » dont change.case$ convertit les
            # lettres hors nom de commande ({\#MeToo} → {\#metoo}). Doublées,
            # les accolades extérieures forment un groupe protégé ordinaire.
            esc = ("{{" + esc + "}}") if esc.startswith("\\") \
                else ("{" + esc + "}")
        out.append(esc)
    return " ".join(out)


_URL_UNSAFE = {"{": "%7B", "}": "%7D", "\\": "%5C", " ": "%20",
               "\t": "%09", "\n": "%0A", "\r": "%0D", '"': "%22"}


def verbatim_value(text: Any) -> str:
    """Valeur verbatim (URL, DOI) : écrite telle quelle, seuls les caractères
    qui casseraient le fichier (accolades, contre-oblique, blancs) sont
    percent-encodés."""
    return "".join(_URL_UNSAFE.get(ch, ch) for ch in strip_controls(text))


# =====================================================================
# Noms de personnes
# =====================================================================

_AND_RE = re.compile(r"(?i)(?:^|\s)and(?:\s|$)")


def _bibtex_name(c: Dict, *, ascii: bool) -> str:
    """Créateur → nom BibTeX (``Nom, Prénom`` ; institutionnel entre
    accolades ; parties contenant une virgule ou « and » protégées)."""
    last, first, institutional = creator_parts(c)
    if not last:
        return ""
    if institutional:
        return "{" + latex_escape(last, ascii=ascii) + "}"
    last_e = latex_escape(last, ascii=ascii)
    if "," in last or _AND_RE.search(last) or (not first and " " in last):
        last_e = "{" + last_e + "}"
    if not first:
        return last_e
    first_e = latex_escape(first, ascii=ascii)
    if "," in first or _AND_RE.search(first):
        first_e = "{" + first_e + "}"
    return f"{last_e}, {first_e}"


def bibtex_names(creators: Iterable[Dict], *, ascii: bool = False) -> str:
    """Liste de créateurs → ``Nom, Prénom and Nom, Prénom``."""
    names = [n for n in (_bibtex_name(c, ascii=ascii) for c in creators) if n]
    return " and ".join(names)


# =====================================================================
# Clés de citation
# =====================================================================

def _ascii_alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unidecode(text or "").lower())


def _ascii_words(text: str) -> List[str]:
    return re.sub(r"[^a-z0-9]+", " ", unidecode(text or "").lower()).split()


def _key_creators(item: Dict) -> List[Dict]:
    roles = creators_by_role(item)
    return roles.get("author") or roles.get("editor") or roles.get(
        "bookauthor") or []


def _key_name(item: Dict) -> str:
    creators = _key_creators(item)
    if not creators:
        return "anon"
    last, _first, institutional = creator_parts(creators[0])
    if not last:
        return "anon"
    if institutional:
        words = [w for w in _ascii_words(last) if w not in _KEY_STOPWORDS]
        return (words[0] if words else _ascii_alnum(last)) or "anon"
    tokens = last.split(" ")
    # Particules en minuscules (« de », « van ») : partie « von » de BibTeX,
    # hors clé (de Certeau → certeau), sauf si rien d'autre ne reste.
    while len(tokens) > 1 and tokens[0][:1].islower():
        tokens.pop(0)
    return _ascii_alnum("".join(tokens)) or "anon"


def _key_title_word(item: Dict) -> str:
    words = _ascii_words(_clean_str(item.get("title")))
    for w in words:
        if w not in _KEY_STOPWORDS:
            return w[:_KEY_WORD_MAX]
    return words[0][:_KEY_WORD_MAX] if words else ""


def citation_key_base(item: Dict) -> str:
    """Clé avant désambiguïsation : ``nom`` + ``année`` (``nd``) + mot du
    titre, ASCII minuscule (``foucault1975surveiller``)."""
    year = parse_date(item.get("date"))[0]
    return f"{_key_name(item)}{year if year else 'nd'}{_key_title_word(item)}"


def _alpha_suffix(n: int) -> str:
    """0 → a, 25 → z, 26 → aa, 27 → ab… (base 26 bijective)."""
    out = ""
    n += 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("a") + r) + out
    return out


def _order_token(item: Dict) -> Tuple[str, ...]:
    """Ordre de désambiguïsation fixé par le contenu (jamais par l'ordre
    d'arrivée) : DOI, titre, date, premier auteur, URL, puis la notice
    sérialisée (départage complet)."""
    creators = _key_creators(item)
    first = " ".join(creator_parts(creators[0])[:2]) if creators else ""
    try:
        canonical = json.dumps(item, sort_keys=True, ensure_ascii=False,
                               default=str)
    except (TypeError, ValueError):
        canonical = repr(item)
    return (
        (normalize_doi(export_doi(item)) or ""),
        " ".join(_ascii_words(_clean_str(item.get("title")))),
        _clean_str(item.get("date")),
        first.lower(),
        _clean_str(item.get("url")),
        canonical,
    )


def make_citation_keys(items: Sequence[Dict]) -> List[str]:
    """
    Clés de citation uniques, dans l'ordre des notices.

    Une clé de base portée par une seule notice reste nue ; les notices qui
    partagent une clé de base reçoivent toutes un suffixe ``a``, ``b``,
    ``c``… (convention auteur-année) dans l'ordre de ``_order_token``, en
    sautant tout suffixe qui coïnciderait avec une autre clé. Le résultat ne
    dépend que de l'ensemble des notices, pas de leur ordre.
    """
    bases = [citation_key_base(it) for it in items]
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, base in enumerate(bases):
        groups[base].append(i)
    keys: List[str] = [""] * len(items)
    taken = set()
    for base, idx in groups.items():
        if len(idx) == 1:
            keys[idx[0]] = base
            taken.add(base)
    for base in sorted(b for b, idx in groups.items() if len(idx) > 1):
        members = sorted(groups[base], key=lambda i: _order_token(items[i]))
        n = 0
        for i in members:
            while True:
                candidate = base + _alpha_suffix(n)
                n += 1
                if candidate not in taken:
                    break
            taken.add(candidate)
            keys[i] = candidate
    return keys


# =====================================================================
# Rendu BibTeX / BibLaTeX
# =====================================================================

_MASTERS_RE = re.compile(
    r"(?i)master|m[ée]moire|ma[iî]trise|\bmsc\b|\bm\.?\s?a\b|\bm2\b|"
    r"\bdea\b|\bdess\b|licence|bachelor")
_DOCTORAL_RE = re.compile(r"(?i)doctor|doctorat|ph\.?\s?d|th[èe]se")


def _is_masters(thesis_type: str) -> bool:
    return bool(thesis_type) and bool(_MASTERS_RE.search(thesis_type))


def entry_type(item: Dict, dialect: str = DIALECT_BIBTEX) -> str:
    """Type d'entrée BibTeX ou BibLaTeX d'une notice Zotero."""
    item_type = item.get("itemType") or "document"
    if dialect == DIALECT_BIBLATEX:
        if item_type == "bookSection" and creators_by_role(item).get(
                "bookauthor"):
            return "inbook"
        return BIBLATEX_TYPES.get(item_type, "misc")
    if item_type == "thesis":
        return ("mastersthesis" if _is_masters(field_value(item, "thesisType"))
                else "phdthesis")
    return BIBTEX_TYPES.get(item_type, "misc")


def _langid(language: str) -> str:
    code = language.strip().lower()
    code = unidecode(code)
    if code in _LANGID:
        return _LANGID[code]
    head = re.split(r"[-_\s]", code)[0]
    return _LANGID.get(head, "")


def _container(item: Dict) -> str:
    """Titre du conteneur (revue, ouvrage, actes, site…), selon le type."""
    for name in ("publicationTitle", "bookTitle", "proceedingsTitle",
                 "encyclopediaTitle", "dictionaryTitle", "websiteTitle",
                 "blogTitle"):
        value = field_value(item, name)
        if value:
            return value
    return ""


@dataclass
class _Fields:
    """Champs d'une entrée, dans l'ordre d'insertion, sans doublon."""

    pairs: List[Tuple[str, str]] = field(default_factory=list)

    def add(self, name: str, rendered: str) -> None:
        if not rendered:
            return
        if any(n == name for n, _ in self.pairs):
            return
        self.pairs.append((name, rendered))


def _braced(value: str) -> str:
    return "{" + value + "}" if value else ""


def _thesis_type_biblatex(thesis_type: str) -> str:
    if not thesis_type:
        return "phdthesis"
    if _is_masters(thesis_type):
        return "mathesis"
    if _DOCTORAL_RE.search(thesis_type) and not re.search(
            r"(?i)habilitation", thesis_type):
        return "phdthesis"
    return ""


def bibtex_entry_fields(
    item: Dict,
    *,
    dialect: str = DIALECT_BIBTEX,
    ascii: bool = False,
    include_provenance: bool = False,
    stats: Optional[Dict[str, int]] = None,
) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Type et champs d'une entrée (valeurs déjà rendues : ``{…}`` ou macro de
    mois), dans un ordre fixe.

    Args:
        item: notice Zotero interne.
        dialect: ``bibtex`` ou ``biblatex``.
        ascii: accents en commandes LaTeX plutôt qu'en UTF-8.
        include_provenance: exporter les lignes d'``extra`` (provenance de
            moisson) dans ``note`` ; faux par défaut (invariant 1).
        stats: compteurs incrémentés (``snippet_abstracts_skipped``,
            ``annotes``, ``scholar_truncation_warnings``,
            ``incomplete_author_lists``).

    Une notice marquée ``Scholar truncation`` reçoit toujours
    ``SCHOLAR_TRUNCATION_WARNING`` dans ``note`` (en tête, avant la
    provenance éventuelle), et ``and others`` en fin d'``author`` quand la
    troncature de la liste d'auteurs est établie (``scholar_truncation``).
    """
    if dialect not in DIALECTS:
        raise ValueError(f"dialecte inconnu : {dialect!r}")
    biblatex = dialect == DIALECT_BIBLATEX
    etype = entry_type(item, dialect)
    item_type = item.get("itemType") or "document"
    esc = lambda v: latex_escape(v, ascii=ascii)  # noqa: E731
    f = _Fields()
    truncation_warning, authors_incomplete = scholar_truncation(item)

    # --- Personnes ---
    roles = creators_by_role(item)
    authors = bibtex_names(roles.get("author", []), ascii=ascii)
    if authors and authors_incomplete:
        # Convention BibTeX et BibLaTeX d'une liste incomplète (« et al. »).
        authors += " and others"
        if stats is not None:
            stats["incomplete_author_lists"] = stats.get(
                "incomplete_author_lists", 0) + 1
    f.add("author", _braced(authors))
    f.add("editor", _braced(bibtex_names(roles.get("editor", []), ascii=ascii)))
    if biblatex:
        f.add("bookauthor", _braced(
            bibtex_names(roles.get("bookauthor", []), ascii=ascii)))
    f.add("translator", _braced(
        bibtex_names(roles.get("translator", []), ascii=ascii)))

    # --- Titres et conteneur ---
    f.add("title", _braced(protect_title(item.get("title"), ascii=ascii)))
    f.add("shorttitle", _braced(
        protect_title(field_value(item, "shortTitle"), ascii=ascii)))

    container = _container(item)
    conference = field_value(item, "conferenceName")
    if etype == "article":
        f.add("journaltitle" if biblatex else "journal",
              _braced(esc(container)))
    elif etype in ("incollection", "inbook", "inproceedings", "inreference"):
        booktitle = container or (conference if etype == "inproceedings"
                                  else "")
        f.add("booktitle", _braced(protect_title(booktitle, ascii=ascii)))
        if biblatex and etype == "inproceedings" and conference and \
                conference != booktitle:
            f.add("eventtitle", _braced(protect_title(conference, ascii=ascii)))
    elif container and etype in ("misc", "unpublished"):
        f.add("howpublished", _braced(esc(container)))
    elif container and biblatex and etype in ("online", "dataset",
                                              "software", "patent"):
        f.add("organization", _braced(esc(container)))

    series = field_value(item, "series") or field_value(item, "seriesTitle")
    f.add("series", _braced(esc(series)))
    f.add("edition", _braced(esc(field_value(item, "edition"))))
    f.add("volume", _braced(esc(field_value(item, "volume"))))

    # « number » : numéro de revue, de rapport ou de collection selon le type.
    if item_type == "report":
        number = field_value(item, "reportNumber")
    elif etype == "article":
        number = field_value(item, "issue")
    else:
        number = field_value(item, "issue") or field_value(item, "seriesNumber")
    f.add("number", _braced(esc(number)))

    start, end = split_pages(field_value(item, "pages"))
    pages = f"{esc(start)}--{esc(end)}" if start and end else esc(start)
    f.add("pages", _braced(pages))
    if biblatex:
        f.add("pagetotal", _braced(esc(field_value(item, "numPages"))))

    # --- Date ---
    year, month, day = parse_date(item.get("date"))
    if biblatex:
        if year:
            iso = f"{year:04d}"
            if month:
                iso += f"-{month:02d}"
                if day:
                    iso += f"-{day:02d}"
            f.add("date", _braced(iso))
    else:
        if year:
            f.add("year", _braced(str(year)))
        if month:
            f.add("month", _MONTH_MACROS[month - 1])

    # --- Éditeur, institution, lieu ---
    publisher = field_value(item, "publisher")
    place = field_value(item, "place")
    university = field_value(item, "university")
    institution = field_value(item, "institution")
    if item_type == "thesis":
        f.add("institution" if biblatex else "school",
              _braced(esc(university or institution or publisher)))
    elif item_type == "report":
        f.add("institution", _braced(esc(institution or publisher)))
    else:
        f.add("publisher", _braced(esc(publisher)))
    f.add("location" if biblatex else "address", _braced(esc(place)))

    # --- Type (thèse, rapport) ---
    if item_type == "thesis":
        thesis_type = field_value(item, "thesisType")
        if biblatex:
            key = _thesis_type_biblatex(thesis_type)
            f.add("type", _braced(key or esc(thesis_type)))
        else:
            f.add("type", _braced(esc(thesis_type)))
    elif item_type == "report":
        report_type = field_value(item, "reportType")
        if biblatex:
            f.add("type", _braced(esc(report_type) or "techreport"))
        else:
            f.add("type", _braced(esc(report_type)))

    # --- Prépublication (BibLaTeX) : dépôt et identifiant ---
    if biblatex and item_type == "preprint":
        repository = field_value(item, "repository")
        archive_id = field_value(item, "archiveID")
        if repository and archive_id:
            f.add("eprinttype", _braced(esc(repository)))
            f.add("eprint", _braced(verbatim_value(archive_id)))
        elif repository:
            f.add("organization", _braced(esc(repository)))
    elif not biblatex and item_type == "preprint":
        f.add("howpublished", _braced(esc(field_value(item, "repository"))))

    # --- Identifiants ---
    f.add("doi", _braced(verbatim_value(export_doi(item))))
    f.add("isbn", _braced(esc(field_value(item, "ISBN"))))
    f.add("issn", _braced(esc(field_value(item, "ISSN"))))
    f.add("url", _braced(verbatim_value(item.get("url"))))
    access = parse_date(field_value(item, "accessDate"))
    if access[0] and access[1] and access[2]:
        f.add("urldate", "{%04d-%02d-%02d}" % access)

    # --- Langue ---
    language = field_value(item, "language")
    if language and language.lower() not in ("und", "zxx", "mul"):
        if biblatex:
            f.add("langid", _braced(_langid(language)))
        else:
            f.add("language", _braced(esc(language)))

    # --- Mots-clés, résumé, annotation, provenance ---
    tags = []
    for tag in item_tags(item):
        t = esc(tag)
        tags.append("{" + t + "}" if "," in tag else t)
    f.add("keywords", _braced(", ".join(tags)))

    abstract, skipped = exportable_abstract(item)
    if skipped and stats is not None:
        stats["snippet_abstracts_skipped"] = stats.get(
            "snippet_abstracts_skipped", 0) + 1
    f.add("abstract", _braced(esc(abstract)))
    annote = annote_text(item)
    if annote and stats is not None:
        stats["annotes"] = stats.get("annotes", 0) + 1
    f.add("annote", _braced(esc(annote)))
    notes = []
    if truncation_warning:
        # Mise en garde de fiabilité (invariant 6), écrite par défaut.
        notes.append(SCHOLAR_TRUNCATION_WARNING)
        if stats is not None:
            stats["scholar_truncation_warnings"] = stats.get(
                "scholar_truncation_warnings", 0) + 1
    if include_provenance:
        notes.extend(provenance_lines(item))
    f.add("note", _braced(esc(" ; ".join(notes))))

    return etype, f.pairs


def format_entry(etype: str, key: str, pairs: Sequence[Tuple[str, str]]) -> str:
    """Entrée BibTeX : ``@type{clé,`` puis un champ par ligne."""
    lines = [f"@{etype}{{{key},"]
    for i, (name, value) in enumerate(pairs):
        sep = "," if i < len(pairs) - 1 else ""
        lines.append(f"  {name} = {value}{sep}")
    lines.append("}")
    return "\n".join(lines)


def export_bibtex(
    items: Sequence[Dict],
    *,
    dialect: str = DIALECT_BIBTEX,
    ascii: bool = False,
    include_provenance: bool = False,
    stats: Optional[Dict[str, int]] = None,
    header: bool = True,
) -> str:
    """
    Corpus Zotero interne → texte BibTeX (ou BibLaTeX), déterministe : les
    mêmes notices donnent le même texte, octet pour octet.

    Args:
        items: notices Zotero internes (dans l'ordre de sortie souhaité).
        dialect: ``bibtex`` (défaut) ou ``biblatex``.
        ascii: accents en commandes LaTeX (défaut : UTF-8 conservé).
        include_provenance: exporter la provenance de moisson (``extra``)
            dans ``note`` ; faux par défaut (invariant 1).
        stats: dictionnaire complété (``entries``, ``disambiguated_keys``,
            ``snippet_abstracts_skipped``, ``annotes``,
            ``scholar_truncation_warnings``, ``incomplete_author_lists``).
        header: commentaire d'en-tête (hors entrée, ignoré par les
            analyseurs).
    """
    if dialect not in DIALECTS:
        raise ValueError(f"dialecte inconnu : {dialect!r}")
    stats = stats if stats is not None else {}
    keys = make_citation_keys(items)
    bases = [citation_key_base(it) for it in items]
    stats["entries"] = len(items)
    stats["disambiguated_keys"] = sum(1 for k, b in zip(keys, bases) if k != b)
    blocks = []
    if header:
        label = "BibLaTeX" if dialect == DIALECT_BIBLATEX else "BibTeX"
        text = (
            f"% Export {label} produit par moisson-shs : {len(items)} "
            f"entrée(s), encodage "
            f"{'ASCII (commandes LaTeX)' if ascii else 'UTF-8'}.\n"
            "% Clés de citation stables : nom + année + premier mot du titre."
        )
        # En mode ASCII, le fichier entier reste ASCII, commentaire compris.
        blocks.append(unidecode(text) if ascii else text)
    for item, key in zip(items, keys):
        etype, pairs = bibtex_entry_fields(
            item, dialect=dialect, ascii=ascii,
            include_provenance=include_provenance, stats=stats)
        blocks.append(format_entry(etype, key, pairs))
    return "\n\n".join(blocks) + "\n"


# =====================================================================
# Service d'export de fichier (BibTeX, BibLaTeX, RIS)
# =====================================================================

class ExportInputError(ValueError):
    """Entrée ou sortie refusée par le service d'export (message destiné à
    l'utilisateur, sans chemin disque)."""


@dataclass
class ExportResult:
    """Résultat d'un export de fichier."""

    path: Path
    format: str
    text: str
    items: List[Dict]
    stats: Dict[str, int]
    # Manifeste qui trace l'export (OPT-16) : celui du corpus source quand
    # l'export est écrit dans son dossier, sinon celui de l'export lui-même.
    manifest_path: Optional[Path] = None

    @property
    def entries(self) -> int:
        return len(self.items)

    @property
    def size_bytes(self) -> int:
        return len(self.text.encode("utf-8"))


# Suffixes réservés aux fichiers du moissonneur : un export écrit sous l'un
# d'eux serait pris pour un corpus, des statistiques ou un fichier associé.
_RESERVED_OUTPUT_SUFFIXES = (".json", "_stats.txt")


def _looks_like_csl(items: List[Any]) -> bool:
    sample = [it for it in items[:50] if isinstance(it, dict)]
    if not sample:
        return False
    if any("itemType" in it for it in sample):
        return False
    return any("type" in it and ("issued" in it or "author" in it
                                 or "container-title" in it or "id" in it)
               for it in sample)


def load_zotero_items(path: Path) -> List[Dict]:
    """
    Lit un corpus **Zotero interne** et refuse clairement toute autre entrée.

    Refusés : CSL-JSON (``.csl.json`` ou contenu CSL : ``type`` sans
    ``itemType``), fichier associé (``.manifest.json``, ``.doi_report.json``),
    statistiques, fichier de service, JSON illisible, contenu qui n'est pas
    un tableau de notices.

    Raises:
        ExportInputError: entrée refusée (message sans chemin disque).
    """
    return _read_zotero_input(path)[0]


def _read_zotero_input(path: Path) -> Tuple[List[Dict], bytes]:
    """``load_zotero_items`` avec les octets lus (empreinte du manifeste).

    La détection par le contenu (manifeste ou CSL-JSON nommé ``.json``) est
    celle des autres outils (``utils.manifest.zotero_input_error``, OPT-16),
    complétée par un repérage plus large des notices CSL.
    """
    from utils.manifest import zotero_input_error
    from utils.output_files import corpus_kind, is_associated_file

    path = Path(path)
    name = path.name
    lower = name.lower()
    if name.startswith("."):
        raise ExportInputError(f"Fichier de service refusé : {name}")
    if is_associated_file(name):
        raise ExportInputError(
            f"{name} est un fichier associé (manifeste ou rapport), pas un "
            "corpus : fournir le corpus Zotero interne <radical>.json.")
    kind = corpus_kind(name)
    if kind == "csl":
        raise ExportInputError(
            f"{name} est un corpus CSL-JSON : l'export BibTeX/RIS part du "
            "corpus Zotero interne (même radical, sans « .csl »), plus riche "
            "(types, identifiants, champs migrés).")
    if kind == "stats" or lower.endswith(".txt"):
        raise ExportInputError(
            f"{name} n'est pas un corpus JSON (fichier de statistiques ?).")
    if not path.is_file():
        raise ExportInputError(f"Fichier introuvable : {name}")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ExportInputError(f"Erreur de parsing JSON ({name}) : {e}") from None
    refusal = zotero_input_error(path, data, tool="corpus_export", loaded=True)
    if refusal:
        raise ExportInputError(refusal)
    if not isinstance(data, list):
        raise ExportInputError(
            f"{name} ne contient pas un tableau de notices Zotero.")
    if _looks_like_csl(data):
        raise ExportInputError(
            f"{name} contient des notices CSL-JSON (champ « type », sans "
            "« itemType ») : fournir le corpus Zotero interne.")
    bad = sum(1 for it in data if not isinstance(it, dict))
    if bad:
        raise ExportInputError(
            f"{name} contient {bad} élément(s) qui ne sont pas des notices.")
    return data, raw


def check_export_output(path: Path, fmt: str, *, strict_suffix: bool) -> None:
    """
    Contrôle le nom du fichier de sortie.

    Toujours refusé : nom de fichier de service (point initial) ou suffixe
    réservé du moissonneur (``.json``, ``_stats.txt``) : l'export serait pris
    pour un corpus ou des statistiques. Extension d'un autre format d'export
    (``.ris`` pour du BibTeX, ``.bib`` pour du RIS) refusée aussi.
    ``strict_suffix`` (mode HTTP) : l'extension doit être exactement celle
    du format (``.bib`` ou ``.ris``), seule servie par le téléchargement.

    Raises:
        ExportInputError: nom refusé.
    """
    name = Path(path).name
    lower = name.lower()
    expected = EXPORT_FORMATS[fmt]
    if not name or name.startswith("."):
        raise ExportInputError(f"Nom de sortie réservé : {name!r}")
    if lower.endswith(_RESERVED_OUTPUT_SUFFIXES):
        raise ExportInputError(
            f"Nom de sortie refusé ({name}) : les suffixes .json et _stats.txt "
            f"sont réservés aux corpus ; utiliser l'extension {expected}.")
    others = {s for s in EXPORT_FORMATS.values() if s != expected}
    if any(lower.endswith(s) for s in others):
        raise ExportInputError(
            f"Extension incohérente avec le format {EXPORT_LABELS[fmt]} "
            f"({name}) : utiliser {expected}.")
    if strict_suffix and not lower.endswith(expected):
        raise ExportInputError(
            f"Extension attendue pour le format {EXPORT_LABELS[fmt]} : "
            f"{expected} ({name}).")


def default_export_path(corpus_path: Path, fmt: str,
                        directory: Optional[Path] = None) -> Path:
    """
    Nom de sortie unique : ``<radical>_<uuid8><ext>`` dans ``directory``
    (défaut : dossier du corpus). Jamais d'écrasement d'un export antérieur.
    """
    corpus_path = Path(corpus_path)
    directory = Path(directory) if directory is not None else corpus_path.parent
    stem = corpus_path.name
    if stem.lower().endswith(".json"):
        stem = stem[: -len(".json")]
    ext = EXPORT_FORMATS[fmt]
    for _ in range(100):
        candidate = directory / f"{stem}_{uuid.uuid4().hex[:8]}{ext}"
        if not candidate.exists():
            return candidate
    raise ExportInputError("Impossible de trouver un nom de sortie libre.")


def render_export(
    items: Sequence[Dict],
    fmt: str,
    *,
    ascii: bool = False,
    include_provenance: bool = False,
    stats: Optional[Dict[str, int]] = None,
) -> str:
    """Texte d'export d'un corpus dans le format demandé (``bibtex``,
    ``biblatex``, ``ris``)."""
    if fmt not in EXPORT_FORMATS:
        raise ExportInputError(
            f"Format inconnu : {fmt!r} (attendu : "
            f"{', '.join(EXPORT_FORMATS)}).")
    if fmt == "ris":
        from mappers.ris_exporter import export_ris
        return export_ris(items, include_provenance=include_provenance,
                          stats=stats)
    return export_bibtex(items, dialect=fmt, ascii=ascii,
                         include_provenance=include_provenance, stats=stats)


def export_manifest_stem(output: Path) -> str:
    """
    Radical du manifeste propre d'un export écrit hors du dossier du corpus
    source.

    Le radical de l'export (``refs.bib`` → ``refs``) sert, sauf s'il porte
    déjà un corpus dans ce dossier : fichier de données ou associé
    (``<radical>.json``, ``.csl.json``, ``_stats.txt``, ``.doi_report.json``)
    ou manifeste qui n'est pas celui d'un export (manifeste illisible
    compris). Publier sous ce radical ferait décrire ce corpus par l'export
    (primaire, nombre de notices, type). Le nom complet de l'export sert
    alors de radical (``corpus_b.bib`` → ``corpus_b.bib.manifest.json``) :
    aucun fichier de données ne le porte, l'index ne le liste donc jamais.

    Raises:
        ExportInputError: les deux radicaux sont pris (rien n'est écrit).
    """
    from utils.manifest import KIND_EXPORT, read_manifest
    from utils.output_files import (
        CSL_SUFFIX, DOI_REPORT_SUFFIX, STATS_SUFFIX, ZOTERO_SUFFIX,
        manifest_name,
    )

    output = Path(output)
    directory = output.parent
    for stem in (output.stem, output.name):
        if not stem or stem.startswith("."):
            continue
        taken = any((directory / f"{stem}{suffix}").exists() for suffix in (
            ZOTERO_SUFFIX, CSL_SUFFIX, STATS_SUFFIX, DOI_REPORT_SUFFIX))
        manifest = directory / manifest_name(stem)
        if not taken and manifest.exists():
            doc = read_manifest(manifest)
            taken = doc is None or doc.get("kind") != KIND_EXPORT
        if not taken:
            return stem
    raise ExportInputError(
        f"Impossible de tracer l'export {output.name} sans toucher au "
        "manifeste d'un corpus existant du même dossier : choisir un autre "
        "nom de sortie.")


def publish_export(
    corpus_path: Path,
    output: Path,
    text: str,
    fmt: str,
    items: Sequence[Dict],
    raw: bytes,
    stats: Dict[str, int],
    *,
    ascii: bool = False,
    include_provenance: bool = False,
    path_roots: Sequence[Any] = (),
) -> Path:
    """
    Publie un export de façon atomique et le trace dans un manifeste
    (``utils.manifest.Publication``, OPT-16) ; renvoie le chemin du manifeste.

    - Export écrit dans le dossier du corpus source (cas par défaut, et
      toujours en HTTP) : **ajout** au manifeste du corpus source (fichier de
      rôle ``export``, événement ``export`` dans ``history``), sous le verrou
      du dossier ; un corpus ancien sans manifeste en reçoit un qui décrit
      aussi son fichier Zotero (comme ``corpus_to_csl``).
    - Ailleurs (``output_path`` d'un autre dossier, stdio ou CLI ; corpus
      d'un sous-dossier en HTTP) : manifeste propre
      ``<radical de l'export>.manifest.json``, complété s'il existe et décrit
      déjà un export ; jamais celui d'un corpus de même radical
      (``export_manifest_stem``).

    Un export n'est jamais un corpus : ni listé, ni regroupé par l'index, ni
    compté dans la règle de complétude du corpus source. Le manifeste ne
    porte que des noms de base (aucun chemin disque).
    """
    import hashlib

    from utils.manifest import (
        KIND_EXPORT, Publication, load_manifest_for, simple_records,
    )
    from utils.output_files import (
        ROLE_EXPORT, ROLE_ZOTERO, corpus_stem_of, manifest_name,
    )

    corpus_path = Path(corpus_path)
    output = Path(output)
    source_stem = corpus_stem_of(corpus_path.name) or corpus_path.stem
    try:
        same_dir = output.parent.resolve() == corpus_path.parent.resolve()
    except OSError:
        same_dir = output.parent == corpus_path.parent
    stem = source_stem if same_dir else export_manifest_stem(output)
    source_doc = load_manifest_for(corpus_path)
    n = len(items)
    inputs = [{
        "name": corpus_path.name,
        "items": n,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "manifest": manifest_name(source_stem) if source_doc is not None else None,
    }]
    export_info = {
        "format": fmt,
        "file": output.name,
        "entries": n,
        "ascii": bool(ascii),
        "include_provenance": bool(include_provenance),
        "disambiguated_keys": int(stats.get("disambiguated_keys", 0)),
        "annotes": int(stats.get("annotes", 0)),
        "snippet_abstracts_skipped": int(stats.get("snippet_abstracts_skipped", 0)),
        "scholar_truncation_warnings": int(
            stats.get("scholar_truncation_warnings", 0)),
    }
    if fmt != "ris":
        export_info["incomplete_author_lists"] = int(
            stats.get("incomplete_author_lists", 0))
    roots = [corpus_path.parent, *path_roots]
    with Publication(output.parent, stem, KIND_EXPORT, append=True,
                     path_roots=roots) as pub:
        pub.add_bytes(output, text.encode("utf-8"), role=ROLE_EXPORT, items=n)
        fields: Dict[str, Any] = {"inputs": inputs, "format": fmt,
                                  "export": export_info}
        if same_dir and source_doc is None:
            pub.describe_existing(corpus_path, role=ROLE_ZOTERO, items=n)
            fields.update({"primary": corpus_path.name, "item_count": n,
                           "records": simple_records(items),
                           "notes": ["corpus antérieur au manifeste : fichier "
                                     "Zotero décrit lors de l'export"]})
        elif not same_dir:
            fields.update({"primary": output.name, "item_count": n})
        return pub.commit(fields)


def export_corpus_file(
    corpus_path: Path,
    fmt: str = "bibtex",
    output_path: Optional[Path] = None,
    *,
    output_dir: Optional[Path] = None,
    ascii: bool = False,
    include_provenance: bool = False,
    strict_suffix: bool = False,
    path_roots: Sequence[Any] = (),
) -> ExportResult:
    """
    Exporte un corpus Zotero interne dans un fichier BibTeX, BibLaTeX ou RIS.

    Args:
        corpus_path: corpus Zotero interne (``<radical>.json``), déjà résolu
            (et confiné en HTTP) par l'appelant.
        fmt: ``bibtex``, ``biblatex`` ou ``ris``.
        output_path: fichier de sortie (déjà résolu par l'appelant) ; absent :
            nom unique ``<radical>_<uuid8>.<ext>`` dans ``output_dir``.
        output_dir: dossier du nom par défaut (défaut : dossier du corpus).
        ascii: BibTeX/BibLaTeX : accents en commandes LaTeX.
        include_provenance: exporter la provenance de moisson (``extra``).
        strict_suffix: exiger l'extension exacte du format (mode HTTP).
        path_roots: dossiers à retirer de toute chaîne du manifeste (filet
            de sécurité, ``utils.manifest.scrub_paths``).

    Écriture atomique et manifeste : ``publish_export`` (OPT-16).

    Raises:
        ExportInputError: format, entrée ou nom de sortie refusés.
    """
    if fmt not in EXPORT_FORMATS:
        raise ExportInputError(
            f"Format inconnu : {fmt!r} (attendu : "
            f"{', '.join(EXPORT_FORMATS)}).")
    corpus_path = Path(corpus_path)
    items, raw = _read_zotero_input(corpus_path)
    if output_path is None:
        output = default_export_path(corpus_path, fmt, output_dir)
    else:
        output = Path(output_path)
    check_export_output(output, fmt, strict_suffix=strict_suffix)
    try:
        if output.resolve() == corpus_path.resolve():
            raise ExportInputError("La sortie ne peut pas remplacer le corpus.")
    except OSError:
        pass
    if not output.parent.is_dir():
        raise ExportInputError(
            f"Dossier de sortie introuvable pour {output.name}.")
    if output.is_dir():
        raise ExportInputError(f"La sortie {output.name} est un dossier.")
    stats: Dict[str, int] = {}
    text = render_export(items, fmt, ascii=ascii,
                         include_provenance=include_provenance, stats=stats)
    manifest_path = publish_export(
        corpus_path, output, text, fmt, items, raw, stats, ascii=ascii,
        include_provenance=include_provenance, path_roots=path_roots)
    return ExportResult(path=output, format=fmt, text=text, items=items,
                        stats=stats, manifest_path=manifest_path)


__all__ = [
    "BIBLATEX_TYPES", "BIBTEX_TYPES", "DIALECTS", "DIALECT_BIBLATEX",
    "DIALECT_BIBTEX", "EXPORT_FORMATS", "EXPORT_LABELS", "EXPORT_MIME",
    "ExportInputError", "ExportResult", "SCHOLAR_TRUNCATION_KEY",
    "SCHOLAR_TRUNCATION_WARNING", "annote_text", "bibtex_entry_fields",
    "bibtex_names", "check_export_output", "citation_key_base",
    "creator_parts", "item_tags",
    "creators_by_role", "default_export_path", "entry_type", "export_bibtex",
    "export_corpus_file", "export_doi", "export_manifest_stem",
    "exportable_abstract", "field_value",
    "latex_escape", "load_zotero_items", "make_citation_keys",
    "normalize_space", "parse_date", "protect_title", "provenance_lines",
    "publish_export", "render_export", "scholar_truncation", "split_pages",
    "strip_controls", "verbatim_value",
]
