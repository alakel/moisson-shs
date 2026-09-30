"""
Classification des fichiers de `OUTPUT_DIR` et espace de noms `corpus://`.

Point unique de vérité sur la convention de nommage des fichiers de sortie,
partagé par `list_resources`, `read_resource`, `corpus_list` et `/download`
(auparavant, chacun appliquait son propre `glob` ou sa propre liste
d'extensions, et rien n'écartait les fichiers de service) :

| Catégorie        | Motif                                        | Exposition                  |
|------------------|----------------------------------------------|-----------------------------|
| corpus Zotero    | `<stem>.json`                                | listé, lisible, téléchargeable |
| corpus CSL       | `<stem>.csl.json`                            | listé, lisible, téléchargeable |
| statistiques     | `<stem>_stats.txt`                           | lisible, téléchargeable     |
| fichier associé  | `<stem>.manifest.json`, `<stem>.doi_report.json`, `<stem>.diff.json` | téléchargeable seulement |
| export bibliographique | `<nom>.bib`, `<nom>.ris` (`corpus_export`) | téléchargeable seulement |
| fichier de service | tout composant commençant par un point (`.oauth/`, `.audit/`, `.index.json`…) | jamais |

Un export (BibTeX, BibLaTeX ou RIS, OPT-18) n'est ni un corpus (jamais listé
par `corpus_list` ni `list_resources`, jamais regroupé par l'index : il
n'appartient à aucun radical), ni un fichier associé (en stdio, son lien
`corpus://` reste lisible par `read_resource`), ni une ressource en HTTP
(hors `PUBLISHABLE_SUFFIXES`) : seul `/download` le sert, avec son type MIME.
Il est tracé dans le manifeste du corpus source (rôle `export`).

Fichiers de service de la publication (OPT-16), tous commençant par un point,
donc jamais listés, lus comme ressource ni téléchargés :

| Fichier                          | Rôle                                                   |
|----------------------------------|--------------------------------------------------------|
| `.<nom final>.<uuid>.tmp`        | temporaire d'écriture, renommé atomiquement en `<nom final>` ; orphelin (interruption) supprimé passé `MOISSON_PUBLISH_TEMP_MAX_AGE`, sur le motif exact (jeton de 12 chiffres hexadécimaux, nom final publié par le moissonneur) : un `.<x>.tmp` d'un autre logiciel n'est jamais touché |
| `.<stem>.publishing`             | marqueur de publication en cours : posé avant le premier renommage, retiré après celui du manifeste ; resté seul, il signale une publication interrompue (corpus incomplet) |
| `.moisson.lock`                  | verrou (``fcntl.flock``) des renommages, des manifestes et de l'index |
| `.index.json`                    | index dérivé des corpus de `OUTPUT_DIR` (`utils.manifest`) |
| `.native_audit/<run_id>.jsonl`   | réponses natives des fournisseurs, mode audit (`MOISSON_AUDIT_NATIVE`), bornées et expurgées ; distinct de `raw/`, qui garde les notices déjà normalisées |

Les tests de suffixe sont insensibles à la casse : sur un système de fichiers
insensible à la casse (macOS), `corpus_x.MANIFEST.json` désigne le même
fichier que `corpus_x.manifest.json` et ne doit pas échapper à l'exclusion.

L'espace `corpus://<nom>` est **plat** : un nom de fichier nu, sans séparateur
ni composant `.`/`..`. Le SDK MCP (pydantic `AnyUrl`) ne protège pas contre la
traversée : il conserve `corpus://../x` tel quel (`..` y est l'hôte opaque de
l'URL), `corpus:///etc/passwd` (hôte vide, chemin absolu) et les encodages
`%2e%2e` de l'hôte, et il percent-encode les noms non ASCII
(`corpus_é.json` → `corpus_%C3%A9.json`). D'où `corpus_name_from_uri`, qui
décode une seule fois puis valide le nom obtenu.

Pages d'un corpus (OPT-17) : `corpus://<nom>?offset=<n>&limit=<n>`
(`parse_corpus_uri`, `corpus_page_uri`). Le nom subit la même validation ;
seuls ces deux paramètres, en entiers décimaux bornés, sont acceptés
(`corpus_name_from_uri` continue de refuser toute URI à paramètres).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, unquote


# === Convention de nommage (fixée pour tout le chantier d'optimisation) ===

CORPUS_URI_PREFIX = "corpus://"

ZOTERO_SUFFIX = ".json"
CSL_SUFFIX = ".csl.json"
STATS_SUFFIX = "_stats.txt"
MANIFEST_SUFFIX = ".manifest.json"
DOI_REPORT_SUFFIX = ".doi_report.json"
# Rapport de comparaison de deux corpus (`corpus_diff`, OPT-19), écrit par
# défaut sous le radical du corpus comparé (`<stem>.diff.json`).
DIFF_REPORT_SUFFIX = ".diff.json"

# Fichiers associés à un corpus (manifeste, rapport de résolution DOI,
# rapport de comparaison) : métadonnées de moisson, jamais présentées comme
# un corpus.
ASSOCIATED_SUFFIXES = (MANIFEST_SUFFIX, DOI_REPORT_SUFFIX, DIFF_REPORT_SUFFIX)

# Exports bibliographiques (`corpus_export`, OPT-18) : téléchargeables
# seulement, avec leur type MIME (`/download`).
BIBTEX_SUFFIX = ".bib"
RIS_SUFFIX = ".ris"
EXPORT_SUFFIXES = (BIBTEX_SUFFIX, RIS_SUFFIX)
EXPORT_MEDIA_TYPES = {
    BIBTEX_SUFFIX: "application/x-bibtex; charset=utf-8",
    RIS_SUFFIX: "application/x-research-info-systems; charset=utf-8",
}

# Liste blanche des fichiers publiables (lecture de ressource, /download).
# Ordre important pour les tests `endswith` : `.csl.json` avant `.json`.
PUBLISHABLE_SUFFIXES = (CSL_SUFFIX, ZOTERO_SUFFIX, STATS_SUFFIX)

# Fichiers de service connus (informatif : la règle générale est « tout
# composant commençant par un point »).
INDEX_NAME = ".index.json"
LOCK_NAME = ".moisson.lock"
NATIVE_AUDIT_DIR = ".native_audit"
SERVICE_DIRS = (".oauth", ".audit", NATIVE_AUDIT_DIR, ".instances")

# Publication atomique (OPT-16) : temporaires et marqueurs, fichiers de
# service du dossier de publication (nom commençant par un point).
TEMP_SUFFIX = ".tmp"
PUBLISHING_SUFFIX = ".publishing"
# Motif exact d'un temporaire du moissonneur (jeton : 12 chiffres
# hexadécimaux, ``uuid4().hex[:12]`` dans ``utils.manifest``) et suffixes des
# noms finaux qu'il publie (``.json`` couvre ``.csl.json``, le manifeste et le
# rapport DOI).
TEMP_TOKEN_LEN = 12
_TEMP_NAME_RE = re.compile(
    r"^\.(?P<final>.+)\.[0-9a-f]{%d}%s$" % (TEMP_TOKEN_LEN, re.escape(TEMP_SUFFIX)))
_TEMP_FINAL_SUFFIXES = (ZOTERO_SUFFIX, STATS_SUFFIX, *EXPORT_SUFFIXES)

# Rôles des fichiers d'un corpus (manifeste, index).
ROLE_ZOTERO = "zotero"
ROLE_CSL = "csl"
ROLE_STATS = "stats"
ROLE_MANIFEST = "manifest"
ROLE_DOI_REPORT = "doi_report"
ROLE_DIFF_REPORT = "diff_report"  # rapport de corpus_diff (fichier associé)
ROLE_EXPORT = "export"          # export décrit dans le manifeste du corpus source

# Suffixe → rôle, du plus long au plus court (`.csl.json` et les fichiers
# associés avant `.json`).
_ROLE_SUFFIXES = (
    (MANIFEST_SUFFIX, ROLE_MANIFEST),
    (DOI_REPORT_SUFFIX, ROLE_DOI_REPORT),
    (DIFF_REPORT_SUFFIX, ROLE_DIFF_REPORT),
    (CSL_SUFFIX, ROLE_CSL),
    (STATS_SUFFIX, ROLE_STATS),
    (ZOTERO_SUFFIX, ROLE_ZOTERO),
)

# Caractères interdits dans un nom plat : séparateurs, NUL et caractères de
# contrôle (un nom légitime produit par le moissonneur n'en contient jamais).
_FORBIDDEN_NAME_RE = re.compile(r"[/\\\x00-\x1f\x7f]")


# === Classification ===

def is_service_path(path: str | Path, base: str | Path | None = None) -> bool:
    """
    Vrai si le chemin désigne un fichier de service : un de ses composants
    commence par un point (`.oauth/`, `.audit/`, `.index.json`, `.env`…).

    Args:
        path: Nom nu ou chemin.
        base: Racine de référence (`OUTPUT_DIR`, résolue par l'appelant). Si
            `path` est sous `base`, seuls les composants relatifs à `base`
            comptent (un `OUTPUT_DIR` situé sous un dossier caché reste
            utilisable) ; hors de `base`, seul le nom de fichier compte.

    Les composants `.` et `..` sont eux aussi traités comme « de service »
    (refus prudent : ils n'apparaissent jamais dans un nom légitime).
    """
    p = Path(path)
    if base is not None:
        try:
            parts = p.relative_to(Path(base)).parts
        except ValueError:
            parts = (p.name,)
    else:
        parts = p.parts
    return any(part.startswith(".") for part in parts if part not in ("/", ""))


def is_associated_file(path: str | Path) -> bool:
    """Vrai pour un fichier associé (`<stem>.manifest.json`,
    `<stem>.doi_report.json`, `<stem>.diff.json`), quelle que soit la
    casse."""
    return Path(path).name.lower().endswith(ASSOCIATED_SUFFIXES)


def is_export_file(path: str | Path) -> bool:
    """Vrai pour un export bibliographique (`<nom>.bib`, `<nom>.ris`), quelle
    que soit la casse ; jamais un fichier de service (point initial)."""
    name = Path(path).name
    if not name or name.startswith("."):
        return False
    lower = name.lower()
    return any(lower.endswith(s) and len(name) > len(s) for s in EXPORT_SUFFIXES)


def export_media_type(path: str | Path) -> str | None:
    """Type MIME servi par `/download` pour un export, None sinon."""
    if not is_export_file(path):
        return None
    lower = Path(path).name.lower()
    for suffix, media in EXPORT_MEDIA_TYPES.items():
        if lower.endswith(suffix):
            return media
    return None


def corpus_kind(path: str | Path) -> str | None:
    """
    Nature d'un fichier de sortie d'après son nom.

    Returns:
        ``"csl"`` (`.csl.json`), ``"stats"`` (`_stats.txt`), ``"zotero"``
        (`.json`), ou ``None`` pour tout le reste : fichier de service (nom
        commençant par un point), fichier associé, extension hors liste
        blanche.
    """
    name = Path(path).name
    if not name or name.startswith("."):
        return None
    if is_associated_file(name):
        return None
    lower = name.lower()
    if lower.endswith(CSL_SUFFIX):
        return "csl"
    if lower.endswith(STATS_SUFFIX):
        return "stats"
    if lower.endswith(ZOTERO_SUFFIX):
        return "zotero"
    return None


def is_corpus_file(path: str | Path) -> bool:
    """Vrai pour un corpus (Zotero interne ou CSL-JSON) : ce qui est listé par
    `corpus_list` et `list_resources`."""
    return corpus_kind(path) in ("zotero", "csl")


def is_publishable_file(path: str | Path) -> bool:
    """Vrai pour un fichier lisible comme ressource : corpus ou statistiques
    (liste blanche, hors fichiers associés et de service)."""
    return corpus_kind(path) is not None


def is_downloadable_file(path: str | Path) -> bool:
    """
    Vrai pour un fichier servi par `/download` : fichier publiable, fichier
    associé **ou** export bibliographique (`.bib`, `.ris`) ; jamais un
    fichier de service.

    Les fichiers associés restent téléchargeables : ils décrivent une moisson
    (plan de requête, statuts, rapport DOI) avec la même sensibilité que le
    corpus lui-même, déjà téléchargeable, et sont le seul accès à cette
    traçabilité pour un client distant sans accès disque. Contrepartie : leurs
    producteurs n'y écrivent jamais de chemin disque (contrat HTTP).
    """
    if is_service_path(Path(path).name):
        return False
    return (is_publishable_file(path) or is_associated_file(path)
            or is_export_file(path))


# === Rôles et radicaux (OPT-16) ===

def file_role(path: str | Path) -> str | None:
    """
    Rôle d'un fichier de corpus d'après son nom : ``"zotero"``, ``"csl"``,
    ``"stats"``, ``"manifest"``, ``"doi_report"``, ``"diff_report"`` ; None
    pour un fichier de service (nom commençant par un point) ou hors
    convention.
    """
    name = Path(path).name
    if not name or name.startswith("."):
        return None
    lower = name.lower()
    for suffix, role in _ROLE_SUFFIXES:
        if lower.endswith(suffix) and len(name) > len(suffix):
            return role
    return None


def corpus_stem_of(path: str | Path) -> str | None:
    """
    Radical ``<stem>`` d'un fichier de corpus ou associé
    (``corpus_x.csl.json`` → ``corpus_x``) ; None hors convention.
    """
    name = Path(path).name
    if not name or name.startswith("."):
        return None
    lower = name.lower()
    for suffix, _role in _ROLE_SUFFIXES:
        if lower.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return None


def manifest_name(stem: str) -> str:
    """Nom du manifeste d'un corpus : ``<stem>.manifest.json``."""
    return f"{stem}{MANIFEST_SUFFIX}"


def temp_name(final_name: str, token: str) -> str:
    """Nom du temporaire d'écriture de ``final_name`` (même dossier, nom
    commençant par un point, donc jamais listé ni servi)."""
    return f".{final_name}.{token}{TEMP_SUFFIX}"


def is_temp_file(path: str | Path) -> bool:
    """
    Vrai pour un temporaire d'écriture du moissonneur, et pour lui seul :
    motif exact de ``temp_name`` avec le jeton de ``utils.manifest``
    (``.<nom>.<12 chiffres hexadécimaux>.tmp``) ET nom final d'un fichier que
    le moissonneur publie (corpus, fichier associé, statistiques, export,
    index). Un fichier caché ``.<x>.tmp`` d'un autre logiciel (sauvegarde,
    fichier d'échange) n'y répond pas : il n'est jamais supprimé par le
    nettoyage des orphelins, même dans un dossier de l'utilisateur (stdio).
    """
    match = _TEMP_NAME_RE.match(Path(path).name)
    if match is None:
        return False
    final = match.group("final")
    return final == INDEX_NAME or final.lower().endswith(_TEMP_FINAL_SUFFIXES)


def publishing_marker_name(stem: str) -> str:
    """Marqueur de publication en cours d'un corpus : ``.<stem>.publishing``."""
    return f".{stem}{PUBLISHING_SUFFIX}"


def stem_of_publishing_marker(path: str | Path) -> str | None:
    """Radical désigné par un marqueur de publication, None sinon."""
    name = Path(path).name
    if (name.startswith(".") and name.endswith(PUBLISHING_SUFFIX)
            and len(name) > 1 + len(PUBLISHING_SUFFIX)):
        return name[1:-len(PUBLISHING_SUFFIX)]
    return None


# === Espace de noms corpus:// ===

def validate_flat_name(name: str) -> str:
    """
    Valide un nom de fichier « plat » de l'espace `corpus://`.

    Refuse : nom vide, séparateur (`/`, `\\`), NUL ou caractère de contrôle,
    nom commençant par un point (fichiers de service, `.`, `..`).

    Returns:
        Le nom inchangé s'il est valide.

    Raises:
        ValueError: nom refusé (message sans chemin disque).
    """
    if not name:
        raise ValueError("nom de fichier vide")
    if _FORBIDDEN_NAME_RE.search(name):
        raise ValueError("séparateur ou caractère de contrôle interdit")
    if name.startswith("."):
        raise ValueError("nom réservé (fichier de service ou traversée)")
    return name


def corpus_uri(name: str) -> str:
    """
    URI `corpus://<nom>` d'un fichier, nom percent-encodé.

    Les noms produits par le moissonneur (slugs ASCII) sont inchangés ; un nom
    contenant une espace, qui faisait échouer la validation `AnyUrl` du SDK,
    ou un caractère non ASCII, que le SDK encodait sans que `read_resource` le
    décode, fait désormais l'aller-retour.
    """
    return CORPUS_URI_PREFIX + quote(name, safe="")


def corpus_name_from_uri(uri) -> str:
    """
    Extrait et valide le nom de fichier d'une URI `corpus://<nom>`.

    Accepte une ``str`` ou un ``AnyUrl`` pydantic (forme transmise par le SDK).
    Le nom est décodé **une seule fois** (`%2e%2e` → `..`, puis refusé) et
    doit être plat (`validate_flat_name`). Les paramètres (`?…`) et fragments
    (`#…`) ne sont pas pris en charge : un nom légitime les porterait encodés.

    Raises:
        ValueError: URI hors schéma, nom encodé invalide ou nom refusé.
    """
    text = str(uri)
    if not text.startswith(CORPUS_URI_PREFIX):
        raise ValueError("schéma attendu : corpus://<nom>")
    raw = text[len(CORPUS_URI_PREFIX):]
    if "?" in raw or "#" in raw:
        raise ValueError("paramètres ou fragment d'URI non pris en charge")
    try:
        name = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        raise ValueError("encodage de nom invalide") from None
    return validate_flat_name(name)


# === Pages d'un corpus : corpus://<nom>?offset=<n>&limit=<n> (OPT-17) ===

# Bornes des paramètres de page acceptés dans l'URI (la taille de page utile
# est ensuite ramenée par l'appelant, ``utils.corpus_query.resolve_page``).
PAGE_PARAMS = ("offset", "limit")
PAGE_OFFSET_MAX = 10_000_000
PAGE_LIMIT_MAX = 10_000
_PAGE_INT_RE = re.compile(r"^[0-9]{1,8}$")


def parse_corpus_uri(uri) -> tuple:
    """
    Nom plat et page demandée d'une URI ``corpus://<nom>`` ou
    ``corpus://<nom>?offset=<n>&limit=<n>``.

    Extension prudente de ``corpus_name_from_uri`` : le nom subit exactement
    la même validation (décodé une fois, plat, sans point initial) ; seuls
    les paramètres ``offset`` et ``limit`` sont acceptés, chacun au plus une
    fois, en entier décimal non signé (chiffres ASCII seuls, sans encodage
    ni signe), ``offset`` ≤ ``PAGE_OFFSET_MAX`` et 1 ≤ ``limit`` ≤
    ``PAGE_LIMIT_MAX``. Tout autre paramètre, un fragment (``#``), une valeur
    vide, un paramètre répété ou une URI terminée par ``?`` sans paramètre
    sont refusés.

    Returns:
        ``(nom, page)`` : ``page`` vaut None sans paramètre, sinon un
        dictionnaire ``{"offset": int | None, "limit": int | None}``.

    Raises:
        ValueError: URI hors schéma, nom refusé ou paramètre refusé.
    """
    text = str(uri)
    if not text.startswith(CORPUS_URI_PREFIX):
        raise ValueError("schéma attendu : corpus://<nom>")
    raw = text[len(CORPUS_URI_PREFIX):]
    if "#" in raw:
        raise ValueError("fragment d'URI non pris en charge")
    base, sep, query = raw.partition("?")
    name = corpus_name_from_uri(CORPUS_URI_PREFIX + base)
    if not sep:
        return name, None
    if not query:
        raise ValueError("paramètres d'URI vides")
    page: dict = {"offset": None, "limit": None}
    seen = set()
    for part in query.split("&"):
        key, eq, value = part.partition("=")
        if key not in PAGE_PARAMS:
            raise ValueError("paramètre d'URI non pris en charge (seuls offset "
                             "et limit sont acceptés)")
        if key in seen:
            raise ValueError(f"paramètre {key} répété")
        seen.add(key)
        if not eq or not _PAGE_INT_RE.match(value):
            raise ValueError(f"{key} : entier décimal attendu")
        number = int(value)
        if key == "offset" and number > PAGE_OFFSET_MAX:
            raise ValueError(f"offset au plus {PAGE_OFFSET_MAX}")
        if key == "limit" and not 1 <= number <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit entre 1 et {PAGE_LIMIT_MAX}")
        page[key] = number
    return name, page


def corpus_page_uri(name: str, offset: int, limit: int) -> str:
    """URI d'une page : ``corpus://<nom>?offset=<n>&limit=<n>``."""
    return f"{corpus_uri(name)}?offset={int(offset)}&limit={int(limit)}"


# === Lecture bornée d'un tableau JSON ===

_WS_RE = re.compile(r"[ \t\n\r]*")
# Caractères qui peuvent suivre un élément complet d'un tableau JSON.
_FOLLOWERS = " \t\n\r,]"


class _JsonArrayStream:
    """Décodeur incrémental d'un tableau JSON lu par blocs (mémoire bornée
    par la taille d'un bloc et de la plus grosse notice)."""

    def __init__(self, fh, chunk_chars: int, max_item_chars: int):
        self.fh = fh
        self.chunk = chunk_chars
        self.max_item = max_item_chars
        self.decoder = json.JSONDecoder()
        self.buf = ""
        self.pos = 0
        self.eof = False

    def _read_more(self, size: int) -> bool:
        if self.eof:
            return False
        data = self.fh.read(size)
        if not data:
            self.eof = True
            return False
        # Compactage : on ne garde que la partie non consommée du tampon.
        self.buf = self.buf[self.pos:] + data
        self.pos = 0
        return True

    def _peek(self) -> str | None:
        """Prochain caractère non blanc (sans le consommer), None en fin."""
        while True:
            self.pos = _WS_RE.match(self.buf, self.pos).end()
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._read_more(self.chunk):
                return None

    def _value(self):
        size = self.chunk
        while True:
            try:
                obj, end = self.decoder.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                # Valeur probablement coupée par la fin du bloc : lire
                # davantage (taille doublée pour éviter un coût quadratique),
                # dans la limite d'une notice de `max_item` caractères.
                if len(self.buf) - self.pos > self.max_item:
                    raise ValueError("notice trop volumineuse ou JSON invalide")
                if self._read_more(size):
                    size *= 2
                    continue
                raise ValueError("JSON invalide ou tronqué") from None
            follower = self.buf[end] if end < len(self.buf) else ""
            if (not follower or follower not in _FOLLOWERS) and not self.eof \
                    and len(self.buf) - self.pos <= self.max_item \
                    and self._read_more(size):
                # Un nombre coupé par la fin du bloc (`-45000` | `000.5e3`)
                # se décode sans erreur mais trop tôt : tant que la valeur
                # n'est pas suivie d'un blanc, d'une virgule ou d'un crochet,
                # on relit avec la suite.
                continue
            self.pos = end
            return obj

    def items(self) -> Iterator:
        if self._peek() != "[":
            raise ValueError("le contenu n'est pas un tableau JSON")
        self.pos += 1
        if self._peek() == "]":
            return
        while True:
            if self._peek() is None:
                raise ValueError("JSON invalide ou tronqué")
            yield self._value()
            c = self._peek()
            if c == ",":
                self.pos += 1
                continue
            if c == "]":
                return
            raise ValueError("JSON invalide : ',' ou ']' attendu")


def iter_json_array(
    path: str | Path,
    *,
    chunk_chars: int = 1 << 16,
    max_item_chars: int = 32 * 1024 * 1024,
) -> Iterator:
    """
    Itère sur les éléments d'un fichier contenant un tableau JSON **sans le
    charger en entier** : lecture par blocs de `chunk_chars` caractères,
    décodage élément par élément (décodeur C de la bibliothèque standard).

    Args:
        path: Fichier UTF-8 contenant un tableau JSON.
        chunk_chars: Taille d'un bloc de lecture.
        max_item_chars: Taille maximale d'un élément (garde-fou mémoire contre
            un fichier invalide ou une notice aberrante).

    Raises:
        ValueError: contenu qui n'est pas un tableau JSON valide.
        OSError, UnicodeDecodeError: erreurs de lecture.
    """
    with open(path, encoding="utf-8") as fh:
        yield from _JsonArrayStream(fh, chunk_chars, max_item_chars).items()
