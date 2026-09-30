"""
Extracteur commun des identifiants d'une notice Zotero.

Toute décision d'identité du projet (déduplication, fusion de corpus,
statistiques, résolution des DOI manquants, comparaison de corpus) doit lire
les identifiants d'une notice de la même façon. Ce module est ce point unique.

API publique stable (importée par d'autres modules) :

- :func:`extract_doi` : DOI **déclaré** de la notice, normalisé, lu dans le
  champ ``DOI`` ou, à défaut, dans une ligne ``DOI: ...`` du champ ``extra``.
  ``validate_and_clean`` (``mappers/zotero_schema.py``) y migre le DOI des
  types Zotero sans champ ``DOI`` (``book``, ``thesis``, ``presentation``…)
  sous la forme exacte ``DOI: <valeur>`` ; les mappers internationaux et le
  résolveur y écrivent la même ligne. La ligne ``DOI source: ...`` du
  résolveur (motif de l'appariement) n'est **jamais** lue comme un DOI ;
- :func:`strong_keys` : clés fortes typées de la notice, triées et sans
  doublon (``doi:``, ``hal:``, ``oai:``, ``isidore:``, ``openalex:``, ``s2:``,
  ``core:``, ``scholar:``, ``url:``, ``isbn:``, ``pmid:``, ``arxiv:``).

Une clé forte désigne un enregistrement précis : deux notices qui en
partagent une décrivent le même enregistrement. Les clés sont lues dans les
lignes d'``extra`` posées par les mappers (``HAL ID``, ``OAI Identifier``,
``Isidore URI``, ``OpenAlex ID``, ``S2 corpus id``, ``CORE ID``,
``Scholar cluster ID``, ``Scholar result ID``, ``PMID``, ``arXiv``), dans
les champs ``DOI``, ``url`` et ``ISBN``, et dérivées des URL d'accès (champ
``url`` et ligne ``Alternative URLs``, que le mapper Isidore renseigne et que
la fusion complète avec les URL des notices fusionnées non retenues) quand
l'URL porte l'identifiant sans ambiguïté :

- DOI d'une URL ``doi.org`` (et des chemins éditeurs reconnus par le mapper
  Scholar) ; DOI Persée ``10.3406/<collection>.<année>.<numéro>`` d'une URL
  ``persee.fr/doc/<collection>_<issn>_<année>_num_<vol>_<no>_<numéro>``
  (motif strict : les sous-documents ``..._t1_0162_0000_2`` des rubriques de
  comptes rendus ne sont pas dérivés, ils n'ont pas de DOI propre) ;
- identifiant HAL d'une URL de portail HAL du CCSD (``hal.science``,
  ``*.hal.science``, ``hal.<établissement>.fr``, ``*.archives-ouvertes.fr``,
  ``*.ccsd.cnrs.fr`` : ``dumas``, ``archivesic``…), sans suffixe de version ;
- identifiant arXiv d'une URL ``arxiv.org/abs|pdf/...`` ou d'un DOI arXiv
  (``10.48550/arxiv.<id>``). La ligne ``arXiv`` d'une notice qui porte le DOI
  d'une autre version (Semantic Scholar y rattache la prépublication de
  l'article publié) est un renvoi vers une version, pas une clé de la notice.

Une notice n'a qu'une clé ``doi:`` : le DOI déclaré, ou à défaut le DOI lu
dans une URL d'accès (:func:`identity_doi`).

URL canonique (clé ``url:``) : normalisation prudente seulement (schéma
ignoré, ``www.`` retiré, casse, slash final, fragment, paramètres de suivi
``utm_*`` et assimilés, paramètres restants triés). Les URL génériques ne
sont **pas** des clés fortes : page d'accueil d'un domaine, page d'index,
URL de recherche, ``doi.org`` sans suffixe, pages Google (Scholar, Books :
un identifiant de livre y est partagé par tous ses chapitres) et portion de
document (fragment ``#page=``, typique des chapitres pointant dans le PDF
d'un ouvrage collectif).

L'ISBN n'est une clé forte que pour le type ``book`` : les chapitres d'un
même ouvrage portent tous l'ISBN de l'ouvrage.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit


# === DOI ===

_DOI_PREFIX_RE = re.compile(
    r"^(?:https?://)?(?:dx\.|www\.)?doi\.org/", re.IGNORECASE)
_DOI_SCHEME_RE = re.compile(r"^doi\s*:\s*", re.IGNORECASE)
_DOI_VALID_RE = re.compile(r"^10\.\d+(?:\.\d+)*/\S+$")
_DOI_IN_TEXT_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>&?#]+", re.IGNORECASE)


def normalize_doi(value: Optional[str]) -> Optional[str]:
    """
    Normalise un DOI : préfixes ``https://doi.org/``, ``dx.doi.org``,
    ``doi:`` retirés, décodage des ``%xx``, minuscules, ponctuation finale
    retirée. Renvoie ``None`` si la valeur n'a pas la forme ``10.<n>/<...>``.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    v = unquote(v)
    v = _DOI_PREFIX_RE.sub("", v)
    v = _DOI_SCHEME_RE.sub("", v)
    v = v.strip().rstrip(".,;").lower()
    if not _DOI_VALID_RE.match(v):
        return None
    return v


def extra_lines(item: Dict) -> List[Tuple[str, str]]:
    """
    Lignes ``clé: valeur`` du champ ``extra``, dans l'ordre, clés et valeurs
    débarrassées des espaces de bordure. Les lignes sans ``:`` sont ignorées.
    """
    out: List[Tuple[str, str]] = []
    extra = item.get("extra") or ""
    if not isinstance(extra, str):
        return out
    for line in extra.split("\n"):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out.append((key.strip(), value.strip()))
    return out


def extra_values(item: Dict, key: str) -> List[str]:
    """Valeurs non vides des lignes ``<key>: ...`` d'``extra`` (clé exacte)."""
    return [v for k, v in extra_lines(item) if k == key and v]


def extract_doi(item: Dict) -> Optional[str]:
    """
    DOI déclaré d'une notice, normalisé.

    Ordre de lecture : champ ``DOI`` ; à défaut, première ligne ``DOI: ...``
    valide du champ ``extra`` (DOI migré par ``validate_and_clean`` pour les
    types sans champ DOI, ou tracé par un mapper). La ligne ``DOI source``
    n'est jamais lue. Un DOI seulement lisible dans l'URL n'est pas un DOI
    déclaré (voir :func:`identity_doi`).

    Returns:
        DOI normalisé (minuscules, sans préfixe) ou ``None``.
    """
    doi = normalize_doi(item.get("DOI"))
    if doi:
        return doi
    for value in extra_values(item, "DOI"):
        doi = normalize_doi(value)
        if doi:
            return doi
    return None


def doi_in_extra_only(item: Dict) -> bool:
    """Vrai si le DOI déclaré n'est lisible que dans ``extra``."""
    return not normalize_doi(item.get("DOI")) and extract_doi(item) is not None


# === URL ===

# Paramètres de suivi retirés de l'URL canonique (aucune incidence sur le
# document désigné).
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "yclid", "msclkid", "igshid", "mc_cid",
    "mc_eid", "_ga", "_gl", "_hsenc", "_hsmkt", "ref_src",
}

# Hôtes dont les URL ne désignent pas une notice : moteur, page de livre
# partagée par tous les chapitres.
_EXCLUDED_HOST_RE = re.compile(
    r"(^|\.)(scholar\.google\.[a-z.]+|books\.google\.[a-z.]+|google\.[a-z.]+"
    r"|scholar\.archive\.org)$")

# Segments de chemin et paramètres caractéristiques d'une page de recherche.
_SEARCH_SEGMENTS = {
    "search", "recherche", "results", "resultats", "find", "advanced-search",
    "searchresults", "search.php", "search.html", "search.aspx",
}
_SEARCH_PARAMS = {"q", "query", "search", "lookfor", "keyword", "keywords",
                  "searchterm", "s", "term", "text"}
_INDEX_PAGES = {"", "/", "/index.html", "/index.htm", "/index.php",
                "/default.aspx", "/home", "/accueil", "/en", "/fr"}


def _split_url(url: Optional[str]):
    """``urlsplit`` tolérant : ``None`` si l'URL n'est pas http(s) exploitable."""
    if not url or not isinstance(url, str):
        return None
    u = html.unescape(url.strip())
    if not u:
        return None
    if u.lower().startswith("www."):
        u = "http://" + u
    if not re.match(r"^https?://", u, re.IGNORECASE):
        return None
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    return parts, host


def _bare_host(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def is_generic_url(url: Optional[str]) -> bool:
    """
    Vrai si l'URL ne peut pas servir de clé forte : absente ou invalide, page
    d'accueil ou d'index, page de recherche, ``doi.org`` sans DOI, hôte
    Google (Scholar, Books), portion de document (``#page=``).
    """
    return canonical_url(url) is None


def canonical_url(url: Optional[str]) -> Optional[str]:
    """
    Forme canonique prudente d'une URL, ou ``None`` si l'URL est générique.

    Schéma ignoré (http et https confondus), ``www.`` retiré, casse
    neutralisée (l'ancienne normalisation d'URL du projet mettait déjà toute
    l'URL en minuscules), ``;jsessionid`` retiré, slash final retiré,
    fragment retiré, paramètres de suivi retirés, paramètres restants triés.
    """
    split = _split_url(url)
    if split is None:
        return None
    parts, host = split
    host = _bare_host(host)
    if _EXCLUDED_HOST_RE.search(host):
        return None

    fragment = (parts.fragment or "").lower()
    if "page=" in fragment:
        # Portion d'un document (chapitre dans le PDF d'un collectif) : l'URL
        # désigne le contenant, pas la notice.
        return None

    path = unquote(parts.path or "")
    path = re.sub(r";jsessionid=[^/?#]*", "", path, flags=re.IGNORECASE)
    path = re.sub(r"/{2,}", "/", path).lower().rstrip("/")

    try:
        params = parse_qsl(parts.query or "", keep_blank_values=False)
    except ValueError:
        params = []
    kept = []
    for key, value in params:
        k = key.lower()
        if k.startswith("utm_") or k in _TRACKING_PARAMS:
            continue
        kept.append((k, value.lower()))
    kept.sort()

    if host.endswith("doi.org"):
        doi = normalize_doi(path.lstrip("/"))
        if not doi:
            return None
        return f"doi.org/{doi}"

    if path in _INDEX_PAGES and not kept:
        return None
    segments = [s for s in path.split("/") if s]
    if any(s in _SEARCH_SEGMENTS for s in segments):
        return None
    if any(k in _SEARCH_PARAMS for k, _ in kept):
        return None

    query = ("?" + urlencode(kept)) if kept else ""
    return f"{host}{path}{query}"


# --- Identifiants dérivés de l'URL ---

# Identifiant de dépôt HAL : préfixe de portail (``hal``, ``halshs``,
# ``tel``, ``dumas``…), numéro à huit chiffres, suffixe de version ``vN``
# facultatif. Le séparateur est un tiret, ou un souligné dans la forme
# historique des portails anciens du CCSD (ArchiveSIC : ``sic_01002311``,
# que HAL déclare tel quel dans ``HAL ID``).
_HAL_ID_RE = re.compile(
    r"^([a-z][a-z0-9]*(?:-[a-z0-9]+)*[-_]\d{8})(?:v\d+)?$")
_PERSEE_DOC_RE = re.compile(
    r"^/doc/([a-z0-9]+)_(\d{4}-\d{3}[\dx])_(\d{4})_num_([^_/]+)_([^_/]+)_(\d+)$")


def _is_hal_host(host: str) -> bool:
    """
    Vrai pour un portail HAL du CCSD : ``hal.science`` et ses sous-domaines
    (``shs.hal.science``, ``theses.hal.science``…), ``*.archives-ouvertes.fr``
    (``halshs``, ``tel``, ``hal-univ-paris3``…), ``*.ccsd.cnrs.fr`` (portails
    historiques : ``dumas``, ``archivesic``, ``hal-emse``…) et
    ``hal.<établissement>`` / ``hal-<collection>.<domaine>``.

    Relevé sur les corpus réels de output/ (2026-09-28) : ``dumas.ccsd.cnrs.fr``
    (29 URL) et ``archivesic.ccsd.cnrs.fr`` (6 URL) n'étaient pas reconnus, de
    sorte qu'une notice Isidore d'un mémoire DUMAS ne portait pas la clé
    ``hal:`` de la notice HAL du même dépôt. L'hôte seul ne suffit jamais :
    :func:`hal_id_from_url` exige en plus un premier segment de chemin au
    format d'un identifiant de dépôt.
    """
    host = _bare_host(host)
    labels = host.split(".")
    return (
        host == "hal.science" or host.endswith(".hal.science")
        or host == "archives-ouvertes.fr"
        or host.endswith(".archives-ouvertes.fr")
        or host.endswith(".ccsd.cnrs.fr")
        or labels[0] == "hal" or labels[0].startswith("hal-")
    )


def normalize_hal_id(value: Optional[str]) -> Optional[str]:
    """Identifiant HAL normalisé (minuscules, sans suffixe de version ``vN``)."""
    if not value:
        return None
    v = str(value).strip().lower()
    m = _HAL_ID_RE.match(v)
    return m.group(1) if m else None


def hal_id_from_url(url: Optional[str]) -> Optional[str]:
    """
    Identifiant HAL lu dans une URL de portail HAL (premier segment du
    chemin, version retirée) : ``https://shs.hal.science/halshs-00367519v1``
    -> ``halshs-00367519``. ``None`` hors portail HAL, pour une portion de
    document (``#page=``) ou une page de recherche.
    """
    split = _split_url(url)
    if split is None:
        return None
    parts, host = split
    if not _is_hal_host(host):
        return None
    if "page=" in (parts.fragment or "").lower():
        return None
    segments = [s for s in unquote(parts.path or "").split("/") if s]
    if not segments:
        return None
    return normalize_hal_id(segments[0])


def doi_from_identifier_query(url: Optional[str]) -> Optional[str]:
    """
    DOI porté par les paramètres ``identifierName=doi`` et
    ``identifierValue=10.…`` d'une URL Taylor & Francis (téléchargement de
    chapitre servi par Google Scholar), ou ``None``. Constat du 2026-09-30 :
    40 notices Scholar sans DOI portaient ainsi le leur (PAT-07).
    """
    split = _split_url(url)
    if split is None:
        return None
    parts, host = split
    if not _bare_host(host).endswith("taylorfrancis.com"):
        return None
    params = {k.lower(): v for k, v in parse_qsl(parts.query or "")}
    if (params.get("identifiername") or "").strip().lower() != "doi":
        return None
    return normalize_doi(params.get("identifiervalue") or "")


def doi_from_url(url: Optional[str]) -> Optional[str]:
    """
    DOI lisible sans ambiguïté dans une URL : hôte ``doi.org``, chemins
    éditeurs reconnus (``/doi/``, ``/article/10.``, ``/chapter/10.``),
    paramètres ``identifierName=doi&identifierValue=…`` de Taylor & Francis,
    ou DOI Persée dérivé d'une URL ``persee.fr/doc/...`` au motif strict.
    """
    split = _split_url(url)
    if split is None:
        return None
    parts, host = split
    host = _bare_host(host)
    path = unquote(parts.path or "")
    low = path.lower()

    from_query = doi_from_identifier_query(url)
    if from_query:
        return from_query

    if host == "persee.fr":
        m = _PERSEE_DOC_RE.match(low.rstrip("/"))
        if m:
            return f"10.3406/{m.group(1)}.{m.group(3)}.{m.group(6)}"
        return None

    if host.endswith("doi.org"):
        return normalize_doi(path.strip("/"))

    eligible = (
        "/doi/" in low
        or re.search(r"/(article|chapter|referenceworkentry)/10\.", low)
    )
    if not eligible:
        return None
    m = _DOI_IN_TEXT_RE.search(path)
    if not m:
        return None
    doi = m.group(0).rstrip(").,;:/")
    for ext in (".pdf", ".full", ".epub", ".xml"):
        if doi.lower().endswith(ext):
            doi = doi[: -len(ext)]
    for seg in ("/html", "/full", "/abstract", "/pdf", "/epub", "/meta",
                "/text"):
        if doi.lower().endswith(seg):
            doi = doi[: -len(seg)]
    m2 = re.match(r"^(10\.\d{4,9}/.+)/\d+$", doi)
    if m2:
        doi = m2.group(1)
    return normalize_doi(doi.rstrip("/"))


_ARXIV_ID_RE = re.compile(
    r"^((?:\d{4}\.\d{4,5})|(?:[a-z-]+(?:\.[a-z]{2})?/\d{7}))(?:v\d+)?$")


def normalize_arxiv_id(value: Optional[str]) -> Optional[str]:
    """Identifiant arXiv sans préfixe ni version (``2402.07043``,
    ``cs/0112017``), ou ``None``."""
    if not value:
        return None
    v = str(value).strip().lower()
    v = re.sub(r"^(arxiv:|https?://(www\.)?arxiv\.org/(abs|pdf)/)", "", v)
    v = re.sub(r"\.pdf$", "", v)
    m = _ARXIV_ID_RE.match(v)
    return m.group(1) if m else None


def arxiv_from_url(url: Optional[str]) -> Optional[str]:
    """Identifiant arXiv d'une URL ``arxiv.org/abs/...`` ou ``/pdf/...``."""
    split = _split_url(url)
    if split is None:
        return None
    parts, host = split
    if _bare_host(host) != "arxiv.org":
        return None
    m = re.match(r"^/(abs|pdf)/(.+?)/?$", unquote(parts.path or ""))
    return normalize_arxiv_id(m.group(2)) if m else None


def arxiv_from_doi(doi: Optional[str]) -> Optional[str]:
    """Identifiant arXiv d'un DOI arXiv (``10.48550/arxiv.<id>``)."""
    d = normalize_doi(doi)
    if d and d.startswith("10.48550/arxiv."):
        return normalize_arxiv_id(d[len("10.48550/arxiv."):])
    return None


def item_urls(item: Dict) -> List[str]:
    """
    URL d'accès d'une notice : champ ``url`` puis URL de la ligne
    ``Alternative URLs`` (posée par le mapper Isidore, et par la fusion pour
    conserver les URL des notices fusionnées non retenues).
    """
    return _urls_from(item, extra_values(item, "Alternative URLs"))


def _urls_from(item: Dict, alternative_lines: List[str]) -> List[str]:
    """URL d'accès : champ ``url`` puis URL des lignes ``Alternative URLs``
    déjà extraites (voir :func:`item_urls`)."""
    out: List[str] = []
    url = item.get("url")
    if isinstance(url, str) and url.strip():
        out.append(url.strip())
    for value in alternative_lines:
        for u in value.split("|"):
            u = u.strip()
            if u and u not in out:
                out.append(u)
    return out


def identity_doi(item: Dict) -> Optional[str]:
    """
    DOI servant aux décisions d'identité : DOI déclaré (:func:`extract_doi`)
    ou, à défaut, premier DOI lisible sans ambiguïté dans les URL d'accès de
    la notice (:func:`item_urls`, :func:`doi_from_url`).
    """
    doi = extract_doi(item)
    if doi:
        return doi
    for url in item_urls(item):
        doi = doi_from_url(url)
        if doi:
            return doi
    return None


# === ISBN ===

def _isbn10_to_13(isbn10: str) -> Optional[str]:
    core = "978" + isbn10[:9]
    total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def normalize_isbns(value: Optional[str]) -> List[str]:
    """ISBN d'un champ (plusieurs possibles), ramenés à ISBN-13 sans tirets."""
    if not value:
        return []
    out: List[str] = []
    for token in re.split(r"[;,\s]+(?=[\dXx])", str(value)):
        digits = re.sub(r"[^0-9Xx]", "", token).upper()
        if len(digits) == 13 and digits.isdigit():
            isbn = digits
        elif len(digits) == 10 and digits[:9].isdigit():
            isbn = _isbn10_to_13(digits)
        else:
            continue
        if isbn and isbn not in out:
            out.append(isbn)
    return out


# === Clés fortes ===

# Lignes d'extra porteuses d'un identifiant natif -> type de clé.
_EXTRA_ID_LINES: Tuple[Tuple[str, str], ...] = (
    ("HAL ID", "hal"),
    ("OAI Identifier", "oai"),
    ("Isidore URI", "isidore"),
    ("OpenAlex ID", "openalex"),
    ("S2 corpus id", "s2"),
    ("CORE ID", "core"),
    ("Scholar cluster ID", "scholar"),
    ("Scholar result ID", "scholar"),
    ("PMID", "pmid"),
    ("arXiv", "arxiv"),
)

def _native_value(kind: str, raw_key: str, value: str) -> Optional[str]:
    v = value.strip()
    if not v:
        return None
    if kind == "hal":
        return normalize_hal_id(v) or v.lower()
    if kind == "oai":
        v = v.lower()
        return v[4:] if v.startswith("oai:") else v
    if kind == "isidore":
        v = re.sub(r"^https?://(www\.)?isidore\.science/(resource|document)/",
                   "", v, flags=re.IGNORECASE)
        return v.lower().rstrip("/") or None
    if kind == "openalex":
        v = re.sub(r"^https?://(www\.)?openalex\.org/", "", v,
                   flags=re.IGNORECASE)
        return v.lower() or None
    if kind == "scholar":
        sub = "cluster" if raw_key.startswith("Scholar cluster") else "result"
        return f"{sub}/{v}"
    if kind == "arxiv":
        v = re.sub(r"^(arxiv:|https?://arxiv\.org/abs/)", "", v,
                   flags=re.IGNORECASE)
        return re.sub(r"v\d+$", "", v.lower()) or None
    return v.lower()


def strong_key_sources(item: Dict) -> List[Tuple[str, bool]]:
    """
    Clés fortes de la notice avec leur origine : ``(clé, dérivée_de_l_url)``.

    Une clé déclarée (champ ``DOI``, ligne d'``extra``, ISBN) prime : si la
    même clé est à la fois déclarée et dérivée de l'URL, elle est marquée
    déclarée. Liste triée par clé, sans doublon.
    """
    declared: Dict[str, bool] = {}

    def add(key: Optional[str], derived: bool) -> None:
        if not key:
            return
        if key in declared:
            declared[key] = declared[key] and derived
        else:
            declared[key] = derived

    # Lignes d'extra lues une seule fois (même résultat que extra_values,
    # clé par clé ; une notice fusionnée peut porter des centaines de lignes).
    values: Dict[str, List[str]] = {}
    for key, value in extra_lines(item):
        if value:
            values.setdefault(key, []).append(value)

    def extra_of(key: str) -> List[str]:
        return values.get(key, [])

    # DOI : le DOI déclaré (champ, sinon ligne ``DOI:`` d'extra) ; à défaut,
    # le DOI lisible dans une URL d'accès. Une notice n'a qu'une clé DOI : un
    # DOI d'URL différent du DOI déclaré n'est pas retenu.
    declared_doi = normalize_doi(item.get("DOI"))
    if not declared_doi:
        declared_doi = next((d for d in map(normalize_doi, extra_of("DOI"))
                             if d), None)
    urls = _urls_from(item, extra_of("Alternative URLs"))
    if declared_doi:
        add(f"doi:{declared_doi}", False)
    else:
        url_doi = next((d for d in map(_cached_doi_from_url, urls) if d),
                       None)
        if url_doi:
            add(f"doi:{url_doi}", True)

    for raw_key, kind in _EXTRA_ID_LINES:
        if kind == "arxiv":
            continue
        for value in extra_of(raw_key):
            native = _native_value(kind, raw_key, value)
            if native:
                add(f"{kind}:{native}", False)

    # arXiv : la ligne ``arXiv`` d'une notice qui porte le DOI d'une autre
    # version (Semantic Scholar rattache la prépublication à l'article publié)
    # est un renvoi vers une version, pas l'identifiant de la notice : elle
    # n'est une clé que sans DOI déclaré ou avec un DOI arXiv. Clés dérivées :
    # DOI arXiv (10.48550/arxiv.<id>) et URL arxiv.org.
    doi_arxiv = arxiv_from_doi(declared_doi)
    if not declared_doi or doi_arxiv:
        for value in extra_of("arXiv"):
            native = normalize_arxiv_id(value)
            if native:
                add(f"arxiv:{native}", False)
    if doi_arxiv:
        add(f"arxiv:{doi_arxiv}", False)

    if (item.get("itemType") or "") == "book":
        for isbn in normalize_isbns(item.get("ISBN")):
            add(f"isbn:{isbn}", False)

    for url in urls:
        canon, hal, arx = _url_keys(url)
        # Une URL doi.org est représentée par la clé doi: (et soumise, comme
        # elle, au garde-fou DOI de la déduplication).
        if canon and not canon.startswith("doi.org/"):
            add(f"url:{canon}", True)
        if hal:
            add(f"hal:{hal}", True)
        if arx and (not declared_doi or doi_arxiv):
            add(f"arxiv:{arx}", True)

    return sorted(declared.items())


@lru_cache(maxsize=65536)
def _url_keys(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Clés dérivées d'une URL (URL canonique, HAL, arXiv) ; fonctions pures,
    mises en cache : une notice fusionnée cite souvent des dizaines d'URL,
    relues à chaque fusion."""
    return canonical_url(url), hal_id_from_url(url), arxiv_from_url(url)


@lru_cache(maxsize=65536)
def _cached_doi_from_url(url: str) -> Optional[str]:
    return doi_from_url(url)


def strong_keys(item: Dict) -> List[str]:
    """
    Clés fortes typées d'une notice, triées et sans doublon.

    Formats : ``doi:<doi>``, ``hal:<id sans version>``, ``oai:<identifiant
    sans préfixe oai:>``, ``isidore:<handle>``, ``openalex:<w…>``,
    ``s2:<corpus id>``, ``core:<id>``, ``scholar:cluster/<id>``,
    ``scholar:result/<id>``, ``url:<url canonique>``, ``isbn:<isbn-13>``
    (type ``book`` seulement), ``pmid:<n>``, ``arxiv:<id sans version>``.
    """
    return [key for key, _ in strong_key_sources(item)]


def keys_of_kind(keys: Iterable[str], kind: str) -> List[str]:
    """Clés d'un type donné (``"doi"``, ``"hal"``…), valeurs seules."""
    prefix = kind + ":"
    return [k[len(prefix):] for k in keys if k.startswith(prefix)]
