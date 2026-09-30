"""
Comparaison de deux corpus Zotero internes (OPT-19 : outil ``corpus_diff``,
veille bibliographique).

Principe
--------
Deux corpus sont comparés : A, la référence, et B, le corpus comparé (en
veille, A est la moisson précédente d'une requête et B la nouvelle). La
comparaison se fait **par identifiants**, selon les règles de la
déduplication (``utils.deduplication``), pour que les deux opérations ne
divergent jamais :

- clés fortes de l'extracteur commun (``utils.identity.strong_keys``) : DOI
  (déclaré dans le champ ou dans ``extra``, ou lisible sans ambiguïté dans
  une URL d'accès), identifiants HAL, OAI, Isidore, OpenAlex, Semantic
  Scholar, CORE, Google Scholar, PMID, arXiv, URL canonique non générique,
  ISBN d'un livre ;
- correspondance par **composantes connexes** (union-find) sur les notices
  des deux corpus réunies, avec les restrictions de l'étape 1 de la
  déduplication : une URL partagée par trop de titres est ignorée et un lien
  d'URL exige des titres concordants ; un identifiant HAL ne relie pas deux
  notices d'années éloignées (versions distinctes d'un dépôt) ; **garde-fou
  DOI** : deux DOI différents ne sont jamais appariés, même par transitivité,
  et une notice sans DOI reliée à deux DOI reste à part (statut ambigu) ;
- étape 2 de la déduplication, **toujours** appliquée comme dans
  ``deduplicate_with_report`` : par défaut, **régime d'égalité exacte**
  seulement (titre normalisé identique ; année à un an près, ou identique
  pour un titre court ; premier auteur ; contraintes dures de type et de
  relation partie/tout), hérité de l'ancienne empreinte exacte ;
  ``fuzzy=True`` y ajoute la **passe floue** (similarité de titre au moins
  égale à 0,93, mêmes corroborations). Aucun seuil n'est exposé : la règle
  est celle de la déduplication, jamais assouplie.

Chaque composante est un **travail** :

- présent dans B seulement : ajout (``added``) ;
- présent dans A seulement : retrait (``removed``) ;
- présent dans les deux : inchangé dans la sélection (``unchanged``). Parmi
  ces travaux, la notice de A et celle de B sont comparées champ par champ :
  ``enriched`` (au moins un champ bibliographique ajouté ou complété : DOI
  ajouté, résumé ajouté, auteurs complétés, date précisée…), ``modified``
  (champs modifiés ou retirés, sans ajout) ou ``identical``.

Les champs de provenance (sources, identifiants natifs, citations, extrait
Google Scholar) sont rapportés à part et ne font pas d'une notice une
notice enrichie : un extrait Scholar n'est pas un résumé, et une source de
plus dit où la notice a été trouvée, pas ce qu'elle décrit.

La comparaison est symétrique : ``diff(B, A)`` échange ajouts et retraits,
garde les mêmes travaux communs et inverse le sens de chaque changement
(ajout et retrait, complément et réduction).

Ajouts : nouvelle publication ou notice nouvellement récupérée
---------------------------------------------------------------
Une notice de B absente de A n'est pas forcément une nouvelle publication.
La date de référence est celle de la moisson A (début de la moisson d'après
son manifeste, ou de la moisson dont A dérive ; à défaut horodatage du nom
``_AAAAMMJJ_HHMMSS``, puis date du fichier). Classement prudent, à la
précision de la date de la notice :

- ``nouvelle_publication`` : publiée après la moisson A (jour, mois ou année
  postérieurs) ;
- ``nouveau_depot`` : publication antérieure, de même période que la
  moisson A (donc peut-être postérieure) ou non datée, mais dépôt HAL
  (ligne ``Submitted Date``) postérieur à la moisson A ; le libellé ne dit
  jamais la publication antérieure sans preuve ;
- ``indeterminee`` : même année (ou même mois) que la moisson A, sans date
  plus précise ;
- ``notice_recuperee`` : publiée avant la moisson A ; sa présence dans B
  tient à la source (indexation tardive, changement de classement, source
  rétablie ou ajoutée, fenêtre de dates) ;
- ``sans_date`` : ni date de publication ni date de dépôt.

Chaque ajout et chaque retrait porte aussi des remarques de contexte : hors
de la fenêtre de dates de l'autre moisson, sources non interrogées, en échec
ou plafonnées de l'autre côté, version distincte d'un dépôt HAL, statut
ambigu.

Contexte des moissons
---------------------
Le manifeste de chaque corpus (``utils.manifest``) donne le type de
production, la requête, le **mode** (``bibliographie`` ou ``collecte``), les
sources et leur statut, le **tri natif** de chaque source, les filtres et le
plafond par source ; pour un corpus dérivé (filtre, résolution DOI,
conversion), le manifeste de son entrée est suivi. Les écarts sont signalés :
en mode bibliographie, chaque source est tronquée selon son propre score, de
sorte qu'un changement de classement chez le fournisseur ressemble à des
ajouts et des retraits sans nouvelle publication ; en mode collecte, une
source plafonnée suit un ordre déterministe (HAL : les plus anciens dépôts
d'abord) et peut omettre les notices récentes. Un corpus sans manifeste a un
mode et un tri inconnus, ce qui est dit aussi.

Rapport
-------
Le rapport complet ``<radical>.diff.json`` (format ``moisson-shs/diff``) est
un fichier associé (``utils.output_files``) : téléchargeable, jamais listé
comme corpus ni lu comme ressource. Il porte les noms de base des corpus,
leurs empreintes SHA-256, leur contexte de moisson, les compteurs, les
avertissements et la liste complète des travaux ajoutés, retirés, enrichis,
modifiés et identiques ; **aucun chemin absolu**. Par défaut, il est écrit à
côté de B sous le radical de B (``corpus_x.json`` → ``corpus_x.diff.json``),
atomiquement, et n'est **jamais remplacé** par une autre comparaison : si ce
nom est pris, le rapport reçoit un nom unique
(``corpus_x_<uuid8>.diff.json``), de sorte que le lien rendu par un appel
sert toujours le rapport de cet appel. Un nom choisi (``report_path``) reste
stable et remplacé à chaque comparaison. Le rapport nomme toujours ses deux
entrées et porte sa date de création.

Le module ne modifie jamais les corpus comparés.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils.cleaner import extract_year
from utils.identity import (
    canonical_url, extra_values, extract_doi, normalize_isbns,
    strong_key_sources,
)
from utils.output_files import (
    DIFF_REPORT_SUFFIX, ROLE_DIFF_REPORT, ROLE_STATS,
    corpus_stem_of, file_role, is_export_file, validate_flat_name,
)


# === Formats, versions, bornes ===

DIFF_FORMAT = "moisson-shs/diff"
DIFF_VERSION = 1
# Version des règles de comparaison (identité, champs comparés, classement
# des ajouts) : à changer quand l'une d'elles change.
# opt19-2 : régime d'égalité exacte de la déduplication appliqué par défaut
# (revue d'OPT-19) ; libellé des nouveaux dépôts HAL.
DIFF_RULES_VERSION = "opt19-2"

# Échantillons du retour MCP (structuredContent) : défaut et plafond. Le
# rapport complet, lui, n'est pas borné.
SAMPLE_DEFAULT = 20
SAMPLE_MAX = 100
# Échantillons repris dans le résumé texte (au plus, par catégorie).
TEXT_SAMPLE_MAX = 8
# Longueur maximale d'un titre ou d'une valeur affichée.
DISPLAY_MAX_CHARS = 200

# Classement des ajouts (ordre d'affichage).
CAT_NEW_PUBLICATION = "nouvelle_publication"
CAT_NEW_DEPOSIT = "nouveau_depot"
CAT_UNDETERMINED = "indeterminee"
CAT_RETRIEVED = "notice_recuperee"
CAT_UNDATED = "sans_date"
ADDED_CATEGORIES = (CAT_NEW_PUBLICATION, CAT_NEW_DEPOSIT, CAT_UNDETERMINED,
                    CAT_RETRIEVED, CAT_UNDATED)
ADDED_LABELS = {
    CAT_NEW_PUBLICATION: "nouvelle publication",
    # Publication antérieure, de même période que la moisson A ou non
    # datée : jamais présentée comme antérieure sans preuve (invariant 6).
    CAT_NEW_DEPOSIT: "nouveau dépôt HAL (publication antérieure ou de date "
                     "imprécise)",
    CAT_UNDETERMINED: "indéterminée (même période que la moisson de "
                      "référence)",
    CAT_RETRIEVED: "notice nouvellement récupérée (publiée avant la "
                   "moisson de référence)",
    CAT_UNDATED: "sans date",
}

# Motifs d'un retrait (premier motif applicable, dans cet ordre).
REM_OUTSIDE_WINDOW = "hors_fenetre"
REM_SOURCES_ABSENT = "sources_non_interrogees"
REM_SOURCES_FAILED = "sources_en_echec"
REM_TRUNCATED = "selection_plafonnee"
REM_ABSENT = "absente"
REMOVED_REASONS = (REM_OUTSIDE_WINDOW, REM_SOURCES_ABSENT, REM_SOURCES_FAILED,
                   REM_TRUNCATED, REM_ABSENT)
REMOVED_LABELS = {
    REM_OUTSIDE_WINDOW: "hors de la fenêtre de dates de la nouvelle moisson",
    REM_SOURCES_ABSENT: "ses sources n'ont pas été interrogées",
    REM_SOURCES_FAILED: "ses sources sont en échec ou incomplètes",
    REM_TRUNCATED: "ses sources ont été plafonnées (sélection)",
    REM_ABSENT: "absente de la nouvelle moisson",
}

# Types de changement d'un champ (de A vers B) et leur inverse (symétrie).
CH_ADDED = "ajout"
CH_REMOVED = "retrait"
CH_EXTENDED = "complément"
CH_REDUCED = "réduction"
CH_MODIFIED = "modification"
INVERSE_CHANGE = {CH_ADDED: CH_REMOVED, CH_REMOVED: CH_ADDED,
                  CH_EXTENDED: CH_REDUCED, CH_REDUCED: CH_EXTENDED,
                  CH_MODIFIED: CH_MODIFIED}
ENRICHING_CHANGES = frozenset({CH_ADDED, CH_EXTENDED})

# Champs bibliographiques comparés (clé du rapport -> libellé).
FIELD_LABELS = {
    "DOI": "DOI",
    "abstract": "résumé",
    "creators": "auteurs",
    "date": "date",
    "title": "titre",
    "itemType": "type",
    "container": "revue ou ouvrage",
    "volume": "volume",
    "issue": "numéro",
    "pages": "pages",
    "publisher": "éditeur",
    "place": "lieu",
    "ISBN": "ISBN",
    "ISSN": "ISSN",
    "language": "langue",
    "url": "URL",
}
# Champs de provenance (rapportés, jamais comptés comme enrichissement).
PROVENANCE_LABELS = {
    "sources": "sources",
    "identifiers": "identifiants",
    "citations": "citations",
    "scholar_snippet": "extrait Google Scholar",
}

# Champs de contenant, dans l'ordre de lecture (même liste que la
# déduplication).
_VENUE_FIELDS = ("publicationTitle", "bookTitle", "proceedingsTitle",
                 "encyclopediaTitle", "dictionaryTitle", "websiteTitle",
                 "blogTitle", "forumTitle", "programTitle")
_SIMPLE_FIELDS = ("volume", "issue", "pages", "publisher", "place", "ISSN",
                  "language")

# Motif d'horodatage des noms de corpus (``corpus_<slug>_AAAAMMJJ_HHMMSS``,
# suivi ou non d'un uuid court), convention des producteurs.
_NAME_TS_RE = re.compile(r"_(\d{8})_(\d{6})(?:_[0-9a-f]{8})?$")
_DATE_RE = re.compile(r"^\s*(\d{4})(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?")
# Suivi des manifestes d'entrée d'un corpus dérivé (filtre de filtre…).
_LINEAGE_MAX_DEPTH = 3


class CorpusDiffInputError(ValueError):
    """
    Entrée refusée par ``corpus_diff`` (fichier qui n'est pas un corpus
    Zotero JSON interne, JSON invalide, sortie refusée). Message sans chemin
    disque, destiné à l'utilisateur.
    """


# === Lecture et contrôle des entrées ===

def load_zotero_corpus(path: Path, *, label: str) -> Tuple[List[Dict], bytes]:
    """
    Lit un corpus Zotero JSON interne et refuse clairement tout le reste.

    Refus (par le nom, avant lecture) : corpus CSL-JSON (``.csl.json``),
    manifeste, rapport DOI, rapport de comparaison, statistiques, export
    BibTeX ou RIS ; (par le contenu) : JSON invalide, contenu qui n'est pas
    un tableau, éléments qui ne sont pas des notices Zotero (``itemType``),
    notices CSL-JSON (``type`` sans ``itemType``), manifeste renommé.

    Args:
        path: fichier fourni.
        label: « corpus_a » ou « corpus_b » (message).

    Returns:
        ``(notices, octets lus)``.

    Raises:
        CorpusDiffInputError: entrée refusée (message sans chemin).
    """
    from utils.corpus_ops import CorpusFormatError, check_zotero_corpus
    from utils.manifest import FORMAT_EMPTY, FORMAT_ZOTERO, detect_items_format

    path = Path(path)
    name = path.name
    tool = "corpus_diff"
    role = file_role(name)
    if role == ROLE_DIFF_REPORT:
        raise CorpusDiffInputError(
            f"Format refusé ({label}) : {name} est un rapport de comparaison, "
            f"pas un corpus ; {tool} compare deux corpus Zotero JSON internes.")
    if role == ROLE_STATS or is_export_file(name):
        raise CorpusDiffInputError(
            f"Format refusé ({label}) : {name} n'est pas un corpus (fichier "
            f"de statistiques ou export) ; {tool} exige un corpus Zotero JSON "
            f"interne (.json).")
    try:
        check_zotero_corpus(path, tool=tool)
    except CorpusFormatError as e:
        raise CorpusDiffInputError(f"{e} ({label})") from None
    try:
        raw = path.read_bytes()
    except OSError:
        raise CorpusDiffInputError(
            f"Lecture impossible ({label}) : {name}.") from None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as e:
        raise CorpusDiffInputError(
            f"JSON invalide ({label}) : {name} ({type(e).__name__}).") from None
    if isinstance(data, dict) and data.get("format") == DIFF_FORMAT:
        raise CorpusDiffInputError(
            f"Format refusé ({label}) : {name} est un rapport de comparaison, "
            f"pas un corpus.")
    try:
        check_zotero_corpus(path, data, tool=tool, loaded=True)
    except CorpusFormatError as e:
        raise CorpusDiffInputError(f"{e} ({label})") from None
    if not isinstance(data, list):
        raise CorpusDiffInputError(
            f"Format refusé ({label}) : {name} n'est pas un corpus Zotero JSON "
            f"interne (tableau de notices attendu).")
    fmt = detect_items_format(data)
    if fmt not in (FORMAT_ZOTERO, FORMAT_EMPTY) or not all(
            isinstance(x, dict) and "itemType" in x for x in data):
        raise CorpusDiffInputError(
            f"Format refusé ({label}) : {name} n'est pas un corpus Zotero JSON "
            f"interne (notices sans itemType).")
    return data, raw


def check_report_output(path: Path) -> None:
    """
    Contrôle du nom d'un rapport demandé (``output_path``) : suffixe
    ``.diff.json`` exact (casse indifférente), radical non vide, nom plat
    (pas de point initial, ni séparateur).

    Raises:
        CorpusDiffInputError: nom refusé.
    """
    name = Path(path).name
    try:
        validate_flat_name(name)
    except ValueError as e:
        raise CorpusDiffInputError(f"Nom de rapport refusé : {e}.") from None
    lower = name.lower()
    if not lower.endswith(DIFF_REPORT_SUFFIX) or len(name) <= len(DIFF_REPORT_SUFFIX):
        raise CorpusDiffInputError(
            f"Nom de rapport refusé : {name} (suffixe {DIFF_REPORT_SUFFIX} "
            f"attendu, par exemple veille{DIFF_REPORT_SUFFIX}).")


def _report_stem(path_b: Path) -> str:
    path_b = Path(path_b)
    return corpus_stem_of(path_b.name) or path_b.stem


def default_report_path(path_b: Path, directory: Optional[Path] = None) -> Path:
    """
    Premier nom essayé pour le rapport par défaut : ``<radical de B>.diff.json``,
    dans ``directory`` (défaut : le dossier de B). S'il existe déjà, le
    rapport prend un nom unique (``unique_report_path``) : voir
    ``diff_corpus_files``.
    """
    path_b = Path(path_b)
    return Path(directory or path_b.parent) / f"{_report_stem(path_b)}{DIFF_REPORT_SUFFIX}"


def unique_report_path(path_b: Path, directory: Optional[Path] = None) -> Path:
    """
    Nom unique d'un rapport : ``<radical de B>_<uuid8>.diff.json`` (même
    convention que les exports de ``corpus_export``, OPT-16 C33).
    """
    path_b = Path(path_b)
    return (Path(directory or path_b.parent)
            / f"{_report_stem(path_b)}_{uuid.uuid4().hex[:8]}{DIFF_REPORT_SUFFIX}")


def _publish_new(target: Path, data: bytes) -> bool:
    """
    Publie ``data`` sous ``target`` sans jamais remplacer un fichier existant.

    Temporaire du même dossier (synchronisé), puis lien physique vers le nom
    final : opération atomique qui échoue si le nom existe déjà, y compris
    face à une comparaison concurrente. Sur un système de fichiers sans liens
    physiques, repli : contrôle d'existence puis remplacement atomique (un
    lecteur ne voit jamais de fichier tronqué ; la fenêtre de concurrence se
    réduit à l'intervalle entre les deux appels).

    Returns:
        True si le rapport est publié, False si ``target`` existe déjà.

    Raises:
        OSError: écriture impossible.
    """
    from utils.manifest import _fsync_dir, _unlink_quiet, write_temp_bytes

    target = Path(target)
    if target.exists():
        return False
    tmp = write_temp_bytes(target.parent, target.name, data)
    try:
        try:
            os.link(tmp, target)
        except FileExistsError:
            return False
        except OSError:
            if target.exists():
                return False
            os.replace(tmp, target)
    finally:
        _unlink_quiet(tmp)
    _fsync_dir(target.parent)
    return True


# === Normalisation et lecture des champs ===

def _fold(value: Any) -> str:
    """Forme de comparaison d'un texte : NFKC, casse, blancs réduits."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _norm_text(value: Any) -> str:
    """Titre ou résumé comparé sans casse, ponctuation ni blancs multiples."""
    text = re.sub(r"[^\w\s]", " ", _fold(value))
    return re.sub(r"\s+", " ", text).strip()


def _short(value: Any, limit: int = DISPLAY_MAX_CHARS) -> Optional[str]:
    """Valeur affichée : une ligne, tronquée à ``limit`` caractères."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def field_value(item: Dict, name: str) -> Optional[str]:
    """
    Valeur d'un champ Zotero : champ lui-même, ou ligne ``<champ>: valeur``
    d'``extra`` (champs que ``validate_and_clean`` y reporte quand le type
    ne les prévoit pas).
    """
    value = item.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    for extra in extra_values(item, name):
        return extra
    return None


def parse_date(value: Any) -> Optional[Tuple[int, ...]]:
    """
    Date d'une notice en tuple ``(année[, mois[, jour]])`` à sa précision :
    ``2020`` → ``(2020,)``, ``2020-05-15`` → ``(2020, 5, 15)`` ; à défaut,
    année lue n'importe où (``15/05/2020`` → ``(2020,)``). None si aucune.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    m = _DATE_RE.match(text)
    if m:
        parts = [int(m.group(1))]
        if m.group(2):
            month = int(m.group(2))
            if 1 <= month <= 12:
                parts.append(month)
                if m.group(3):
                    day = int(m.group(3))
                    if 1 <= day <= 31:
                        parts.append(day)
        return tuple(parts)
    year = extract_year(text)
    return (int(year),) if year else None


def _format_date(parts: Optional[Tuple[int, ...]]) -> Optional[str]:
    if not parts:
        return None
    return "-".join([f"{parts[0]:04d}"] + [f"{p:02d}" for p in parts[1:]])


def compare_dates(value: Tuple[int, ...], reference: Tuple[int, ...]) -> str:
    """
    ``"after"``, ``"same"`` ou ``"before"`` : comparaison à la précision
    commune (une année seule égale à celle de la référence est ``"same"``,
    donc indéterminée).
    """
    n = min(len(value), len(reference))
    a, b = tuple(value[:n]), tuple(reference[:n])
    if a > b:
        return "after"
    if a < b:
        return "before"
    return "same"


def outside_window(parts: Optional[Tuple[int, ...]], from_date: Any,
                   to_date: Any) -> bool:
    """
    Vrai si la date de la notice est **certainement** hors de la fenêtre
    ``[from_date, to_date]`` (comparaison à la précision commune ; une date
    absente ou imprécise n'est jamais hors fenêtre).
    """
    if not parts:
        return False
    low = parse_date(from_date)
    high = parse_date(to_date)
    if low and compare_dates(parts, low) == "before":
        return True
    if high and compare_dates(parts, high) == "after":
        return True
    return False


def _creator_keys(item: Dict) -> List[str]:
    """Noms des auteurs nommés (nom de famille, ou nom unique), normalisés."""
    out = []
    for c in item.get("creators") or []:
        if not isinstance(c, dict):
            continue
        name = c.get("lastName") or c.get("name") or ""
        key = _norm_text(name)
        if key:
            out.append(key)
    return out


def _abstract_state(item: Dict) -> Tuple[str, str]:
    """``(état, texte normalisé)`` du résumé : ``none``, ``snippet`` (extrait
    Google Scholar, qui n'est pas un résumé) ou ``abstract``."""
    from utils.deduplication import abstract_is_scholar_snippet

    text = item.get("abstractNote")
    if not isinstance(text, str) or not text.strip():
        return "none", ""
    if abstract_is_scholar_snippet(item):
        return "snippet", _norm_text(text)
    return "abstract", _norm_text(text)


def _container(item: Dict) -> Optional[str]:
    for name in _VENUE_FIELDS:
        value = field_value(item, name)
        if value:
            return value
    return None


def _item_sources(item: Dict) -> List[str]:
    """Clés de registre des sources de la notice (ligne ``Sources``)."""
    from utils.deduplication import item_sources
    return sorted(s for s in item_sources(item) if s)


def _source_names(keys) -> List[str]:
    """Libellés d'affichage de clés de source (``hal`` → ``HAL``)."""
    from utils.source_labels import display_name
    return [display_name(k) for k in sorted(keys)]


def _citations(item: Dict) -> Optional[int]:
    best = None
    for value in extra_values(item, "Cited by"):
        digits = re.sub(r"[^\d]", "", value or "")
        if digits:
            best = max(best or 0, int(digits))
    return best


def _deposit_date(item: Dict) -> Optional[Tuple[int, ...]]:
    """Date de dépôt HAL (ligne ``Submitted Date`` du mapper HAL)."""
    for value in extra_values(item, "Submitted Date"):
        parts = parse_date(value)
        if parts:
            return parts
    return None


# === Comparaison de deux notices d'un même travail ===

def _change(field: str, kind: str, a: Any = None, b: Any = None,
            note: Optional[str] = None, provenance: bool = False) -> Dict[str, Any]:
    labels = PROVENANCE_LABELS if provenance else FIELD_LABELS
    out: Dict[str, Any] = {"field": field, "label": labels.get(field, field),
                           "change": kind}
    if a is not None:
        out["a"] = a
    if b is not None:
        out["b"] = b
    if note:
        out["note"] = note
    return out


def _presence_change(field: str, va: Any, vb: Any, *, same: bool,
                     display_a: Any = None, display_b: Any = None
                     ) -> Optional[Dict[str, Any]]:
    """Ajout, retrait ou modification d'un champ simple (None si égal)."""
    if not va and not vb:
        return None
    da = display_a if display_a is not None else _short(va)
    db = display_b if display_b is not None else _short(vb)
    if not va:
        return _change(field, CH_ADDED, b=db)
    if not vb:
        return _change(field, CH_REMOVED, a=da)
    if same:
        return None
    return _change(field, CH_MODIFIED, a=da, b=db)


def _set_change(field: str, sa: set, sb: set, display) -> Optional[Dict[str, Any]]:
    """Changement d'un ensemble (ISBN) : ajout, retrait, complément,
    réduction ou modification."""
    if sa == sb:
        return None
    if not sa:
        return _change(field, CH_ADDED, b=display(sb))
    if not sb:
        return _change(field, CH_REMOVED, a=display(sa))
    if sa < sb:
        return _change(field, CH_EXTENDED, a=display(sa), b=display(sb))
    if sb < sa:
        return _change(field, CH_REDUCED, a=display(sa), b=display(sb))
    return _change(field, CH_MODIFIED, a=display(sa), b=display(sb))


def compare_records(a: Dict, b: Dict) -> Tuple[List[Dict[str, Any]],
                                                List[Dict[str, Any]]]:
    """
    Changements de la notice ``a`` (corpus A) à la notice ``b`` (corpus B)
    d'un même travail.

    Returns:
        ``(bibliographiques, provenance)`` : listes de changements
        ``{"field", "label", "change", "a", "b", "note"}`` ; ``change`` vaut
        ``ajout``, ``retrait``, ``complément``, ``réduction`` ou
        ``modification``. Les valeurs affichées sont tronquées ; un résumé
        n'est jamais recopié (état et longueur seulement).
    """
    biblio: List[Dict[str, Any]] = []

    def add(change: Optional[Dict[str, Any]]) -> None:
        if change is not None:
            biblio.append(change)

    # DOI déclaré (champ ou extra) : deux DOI différents ne sont jamais dans
    # le même travail (garde-fou), d'où ajout ou retrait en pratique.
    doi_a, doi_b = extract_doi(a), extract_doi(b)
    change = _presence_change("DOI", doi_a, doi_b, same=doi_a == doi_b)
    if change is not None and change["change"] == CH_ADDED \
            and extra_values(b, "DOI source"):
        change["note"] = ("DOI résolu par appariement bibliographique "
                          "(ligne DOI source)")
    add(change)

    # Résumé : un extrait Google Scholar n'est pas un résumé (invariant 6).
    state_a, text_a = _abstract_state(a)
    state_b, text_b = _abstract_state(b)
    real_a, real_b = state_a == "abstract", state_b == "abstract"
    if real_b and not real_a:
        add(_change("abstract", CH_ADDED, b=f"{len(b['abstractNote'])} car."))
    elif real_a and not real_b:
        add(_change("abstract", CH_REMOVED, a=f"{len(a['abstractNote'])} car."))
    elif real_a and real_b and text_a != text_b:
        add(_change("abstract", CH_MODIFIED,
                    a=f"{len(a['abstractNote'])} car.",
                    b=f"{len(b['abstractNote'])} car."))

    # Auteurs : liste complétée (inclusion stricte), réduite ou modifiée.
    ca, cb = _creator_keys(a), _creator_keys(b)
    if ca != cb:
        da, db = f"{len(ca)} auteur(s)", f"{len(cb)} auteur(s)"
        if not ca:
            add(_change("creators", CH_ADDED, b=db))
        elif not cb:
            add(_change("creators", CH_REMOVED, a=da))
        elif Counter(ca) - Counter(cb) == Counter() and len(cb) > len(ca):
            add(_change("creators", CH_EXTENDED, a=da, b=db))
        elif Counter(cb) - Counter(ca) == Counter() and len(ca) > len(cb):
            add(_change("creators", CH_REDUCED, a=da, b=db))
        else:
            add(_change("creators", CH_MODIFIED, a=da, b=db))

    # Date : précisée (même début, plus précise), réduite ou modifiée.
    pa, pb = parse_date(a.get("date")), parse_date(b.get("date"))
    if pa != pb:
        da, db = _format_date(pa), _format_date(pb)
        if not pa:
            add(_change("date", CH_ADDED, b=db))
        elif not pb:
            add(_change("date", CH_REMOVED, a=da))
        elif len(pa) < len(pb) and pb[:len(pa)] == pa:
            add(_change("date", CH_EXTENDED, a=da, b=db))
        elif len(pb) < len(pa) and pa[:len(pb)] == pb:
            add(_change("date", CH_REDUCED, a=da, b=db))
        else:
            add(_change("date", CH_MODIFIED, a=da, b=db))

    ta, tb = a.get("title"), b.get("title")
    add(_presence_change("title", ta if _norm_text(ta) else None,
                         tb if _norm_text(tb) else None,
                         same=_norm_text(ta) == _norm_text(tb)))
    ia, ib = a.get("itemType") or None, b.get("itemType") or None
    add(_presence_change("itemType", ia, ib, same=ia == ib))
    va, vb = _container(a), _container(b)
    add(_presence_change("container", va, vb, same=_fold(va) == _fold(vb)))
    for name in _SIMPLE_FIELDS:
        va, vb = field_value(a, name), field_value(b, name)
        add(_presence_change(name, va, vb, same=_fold(va) == _fold(vb)))
    add(_set_change("ISBN", set(normalize_isbns(field_value(a, "ISBN"))),
                    set(normalize_isbns(field_value(b, "ISBN"))),
                    lambda s: ", ".join(sorted(s))))
    ua, ub = field_value(a, "url"), field_value(b, "url")
    add(_presence_change("url", ua, ub,
                         same=(canonical_url(ua) or _fold(ua))
                         == (canonical_url(ub) or _fold(ub))))

    return biblio, _provenance_changes(a, b, state_a, state_b)


def _provenance_changes(a: Dict, b: Dict, state_a: str, state_b: str
                        ) -> List[Dict[str, Any]]:
    """Changements de provenance (sources, identifiants natifs, citations,
    extrait Google Scholar) : rapportés, jamais un enrichissement."""
    out: List[Dict[str, Any]] = []
    sa, sb = set(_item_sources(a)), set(_item_sources(b))
    change = _set_change("sources", sa, sb,
                         lambda s: ", ".join(_source_names(s)))
    if change is not None:
        change["label"] = PROVENANCE_LABELS["sources"]
        out.append(change)
    ka = {k for k, _ in strong_key_sources(a) if not k.startswith("doi:")}
    kb = {k for k, _ in strong_key_sources(b) if not k.startswith("doi:")}
    if ka != kb:
        change = _set_change("identifiers", ka, kb, lambda s: len(s))
        change["label"] = PROVENANCE_LABELS["identifiers"]
        added, removed = sorted(kb - ka), sorted(ka - kb)
        if added:
            change["added_keys"] = added[:20]
        if removed:
            change["removed_keys"] = removed[:20]
        out.append(change)
    ca, cb = _citations(a), _citations(b)
    if ca != cb:
        kind = (CH_ADDED if ca is None else CH_REMOVED if cb is None
                else CH_MODIFIED)
        out.append(_change("citations", kind, a=ca, b=cb, provenance=True))
    sn_a, sn_b = state_a == "snippet", state_b == "snippet"
    if sn_a != sn_b:
        out.append(_change("scholar_snippet", CH_ADDED if sn_b else CH_REMOVED,
                           provenance=True,
                           note="extrait de la page de résultats Google "
                                "Scholar, pas un résumé"))
    return out


def invert_change(change: Dict[str, Any]) -> Dict[str, Any]:
    """Changement vu de B vers A (symétrie) : sens inversé, valeurs
    échangées."""
    out = dict(change)
    out["change"] = INVERSE_CHANGE.get(change.get("change"), change.get("change"))
    out.pop("a", None)
    out.pop("b", None)
    if "b" in change:
        out["a"] = change["b"]
    if "a" in change:
        out["b"] = change["a"]
    if "added_keys" in change or "removed_keys" in change:
        out.pop("added_keys", None)
        out.pop("removed_keys", None)
        if change.get("removed_keys"):
            out["added_keys"] = change["removed_keys"]
        if change.get("added_keys"):
            out["removed_keys"] = change["added_keys"]
    out.pop("note", None)
    return out


# === Contexte de moisson (manifestes) ===

def _local_date(dt: datetime) -> Tuple[int, int, int]:
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.year, dt.month, dt.day


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _name_datetime(stem: str) -> Optional[datetime]:
    m = _NAME_TS_RE.search(stem or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _query_from_name(stem: str) -> Optional[str]:
    body = stem[len("corpus_"):] if stem.startswith("corpus_") else stem
    body = _NAME_TS_RE.sub("", body).replace("_", " ").strip()
    return body or None


def _source_entries(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Statut compact de chaque source d'un manifeste de moisson."""
    out = []
    sources = doc.get("sources")
    if not isinstance(sources, dict):
        return out
    for label, entry in sorted(sources.items()):
        if not isinstance(entry, dict):
            continue
        out.append({
            "source": entry.get("source") or label,
            "label": label,
            "status": entry.get("status"),
            "reason": entry.get("reason"),
            "items": entry.get("items"),
            "limit_reached": bool(entry.get("limit_reached")),
            "provider_window": bool(entry.get("provider_window")),
            "native_sort": entry.get("native_sort"),
        })
    return out


def source_state(entry: Dict[str, Any]) -> str:
    """
    État d'une source pour l'interprétation d'un diff : ``failed`` (en
    échec, non exécutée, annulée, ou partielle sur panne), ``truncated``
    (plafond atteint, fenêtre ou borne du fournisseur) ou ``ok``.
    """
    from engine.models import BOUNDED_REASONS, FAILED_STATUSES, STATUS_PARTIAL

    status = entry.get("status")
    reason = entry.get("reason")
    if status in FAILED_STATUSES:
        return "failed"
    if status == STATUS_PARTIAL and reason not in BOUNDED_REASONS \
            and not entry.get("provider_window"):
        return "failed"
    if entry.get("limit_reached") or entry.get("provider_window") \
            or (status == STATUS_PARTIAL and reason in BOUNDED_REASONS):
        return "truncated"
    return "ok"


def _describe_source(entry: Dict[str, Any]) -> str:
    from engine.models import REASON_LABELS, STATUS_LABELS

    text = STATUS_LABELS.get(entry.get("status") or "", entry.get("status") or "?")
    bits = []
    if entry.get("limit_reached"):
        bits.append("plafond atteint")
    if entry.get("reason"):
        bits.append(REASON_LABELS.get(entry["reason"], entry["reason"]))
    return f"{entry.get('label')} {text}" + (f" ({' ; '.join(bits)})" if bits else "")


def corpus_context(path: Path, items: Sequence[Dict], raw: bytes
                   ) -> Dict[str, Any]:
    """
    Contexte d'un corpus comparé, sans chemin disque : nom de base, notices,
    empreinte, manifeste, type de production, requête, mode, sources et
    statuts (tri natif compris), filtres, plafond par source, date de la
    moisson de référence et sa provenance (``manifeste``, ``nom``,
    ``fichier``). Pour un corpus dérivé (filtre, résolution DOI, conversion),
    le manifeste de son entrée est suivi (``inherited_from``) ; pour une
    fusion, les contextes de ses entrées sont réunis (PAT-18,
    :func:`_merged_context`). Aucune requête n'est tirée du nom d'une fusion
    ni d'une recherche par auteur.
    """
    from utils.manifest import (
        KIND_AUTHOR, KIND_HARVEST, KIND_LABELS, KIND_MERGE, harvest_ancestor,
        load_manifest_for, merge_contexts,
    )

    path = Path(path)
    stem = corpus_stem_of(path.name) or path.stem
    doc = load_manifest_for(path)
    ctx: Dict[str, Any] = {
        "name": path.name,
        "items": len(items),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "manifest": f"{stem}.manifest.json" if doc is not None else None,
        "kind": doc.get("kind") if doc else None,
        "kind_label": KIND_LABELS.get(doc.get("kind")) if doc else None,
    }
    named_by_query = not (
        (doc or {}).get("kind") in (KIND_MERGE, KIND_AUTHOR)
        or stem.startswith(("corpus_merged_", "corpus_author_")))
    if doc is not None and doc.get("kind") == KIND_MERGE:
        contexts = merge_contexts(doc)
        if any(contexts):
            ctx.update(_merged_context(contexts))
            if ctx.get("harvest_date") is None:
                ctx.update(_reference_date(
                    path, stem, [(doc, "manifeste (date de la fusion)")]))
            return ctx
    # Manifeste porteur du contexte de moisson : le sien, ou celui de son
    # entrée pour un corpus dérivé (au plus _LINEAGE_MAX_DEPTH niveaux).
    harvest_doc, inherited = harvest_ancestor(path.parent, doc,
                                              max_depth=_LINEAGE_MAX_DEPTH)
    base = harvest_doc or {}
    harvest_doc_sources = bool(base.get("sources"))
    query = base.get("query")
    if not isinstance(query, dict):
        query = {"text": query} if isinstance(query, str) else {}
    settings = base.get("settings") or {}
    filters = base.get("filters") or {}
    ranking = base.get("ranking") or {}
    text = query.get("text") if isinstance(query.get("text"), str) else None
    ctx.update({
        "inherited_from": inherited,
        "query": text,
        "query_from_name": (_query_from_name(stem)
                            if not text and named_by_query else None),
        "mode": query.get("mode"),
        "requested_sources": list(query.get("sources") or []),
        "filters": {
            "from_date": filters.get("from_date", query.get("from_date")),
            "to_date": filters.get("to_date", query.get("to_date")),
            "languages": list(filters.get("languages")
                              or query.get("languages") or []),
        },
        "max_per_source": settings.get("max_per_source"),
        "ranking_mode": ranking.get("mode") if isinstance(ranking, dict) else None,
        "sources": _source_entries(base) if harvest_doc_sources else [],
        "harvest": harvest_doc_sources and base.get("kind") == KIND_HARVEST,
    })
    # Date de référence : début de la moisson (ou de la production), puis
    # horodatage du nom, puis date du fichier.
    candidates = []
    if harvest_doc is not None:
        candidates.append((harvest_doc, "manifeste" + (f" ({inherited})"
                                                       if inherited else "")))
    if doc is not None and doc is not harvest_doc:
        candidates.append((doc, "manifeste"))
    ctx.update(_reference_date(path, stem, candidates))
    return ctx


def _reference_date(path: Path, stem: str,
                    candidates: Sequence[Tuple[Dict[str, Any], str]]
                    ) -> Dict[str, Any]:
    """Date de référence d'un corpus : début (ou fin) de la première
    production de ``candidates`` qui en porte une, puis horodatage du nom,
    puis date du fichier."""
    when, basis = None, None
    for source_doc, label in candidates:
        for key in ("started_at", "finished_at"):
            when = _parse_iso(source_doc.get(key))
            if when is not None:
                basis = label
                break
        if when is not None:
            break
    if when is None:
        when = _name_datetime(stem)
        basis = "nom" if when is not None else None
    if when is None:
        try:
            when = datetime.fromtimestamp(path.stat().st_mtime)
            basis = "fichier"
        except OSError:
            when = None
    return {
        "harvest_date": when.isoformat(timespec="seconds") if when else None,
        "harvest_day": list(_local_date(when)) if when else None,
        "harvest_date_basis": basis,
    }


# État d'une source, du meilleur au pire, pour réunir les entrées d'une fusion.
_STATE_ORDER = {"ok": 0, "truncated": 1, "failed": 2}


def _merged_context(contexts: Sequence[Optional[Dict[str, Any]]]
                    ) -> Dict[str, Any]:
    """
    Contexte réuni des entrées d'une fusion (PAT-18). Mode commun, sinon
    « mixte » ; requête commune, sinon aucune (``input_queries`` les
    nomme) ; union des sources, chacune dans son meilleur état (une source
    complète dans une entrée n'est pas en échec pour la fusion), notices
    additionnées ; fenêtre de dates englobante et langues réunies, connues
    seulement si chaque entrée les donne ; plafond et classement communs,
    sinon inconnus ou « mixte » ; date de référence : début de l'entrée la
    plus ancienne. ``merged_inputs`` : nombre d'entrées, ``merged_unknown`` :
    entrées sans contexte (corpus sans manifeste).
    """
    known = [c for c in contexts if isinstance(c, dict)]
    unknown = len(contexts) - len(known)
    queries = [c.get("query") if isinstance(c.get("query"), dict) else {}
               for c in known]

    def _common(values: List[Any]) -> Any:
        if unknown or not values or any(v in (None, "", []) for v in values):
            return None
        return values[0] if all(v == values[0] for v in values) else None

    modes = [q.get("mode") for q in queries]
    distinct_modes = sorted({m for m in modes if m})
    mode = _common(modes) or ("mixte" if distinct_modes else None)
    texts = [q.get("text") if isinstance(q.get("text"), str) else None
             for q in queries]
    input_queries: List[str] = []
    for text in texts:
        if text and _fold(text) not in {_fold(t) for t in input_queries}:
            input_queries.append(text)
    query = (input_queries[0] if len(input_queries) == 1 and all(texts)
             and not unknown else None)
    requested: List[str] = []
    for q in queries:
        for name in q.get("sources") or []:
            if name not in requested:
                requested.append(name)
    froms = [q.get("from_date") for q in queries]
    tos = [q.get("to_date") for q in queries]
    langs = [list(q.get("languages") or []) for q in queries]
    complete = bool(known) and not unknown
    filters = {
        "from_date": (min(str(v) for v in froms)
                      if complete and all(froms) else None),
        "to_date": (max(str(v) for v in tos) if complete and all(tos) else None),
        "languages": (sorted({lang for ls in langs for lang in ls})
                      if complete and all(langs) else []),
    }
    caps = [(c.get("settings") or {}).get("max_per_source") for c in known]
    rankings = [(c.get("ranking") or {}).get("mode") for c in known]
    distinct_rankings = sorted({r for r in rankings if r})
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for c in known:
        sources = c.get("sources") if isinstance(c.get("sources"), dict) else {}
        for label, entry in sources.items():
            if not isinstance(entry, dict):
                continue
            e = dict(entry, label=label)
            e.setdefault("source", label)
            by_source.setdefault(e["source"], []).append(e)
    merged_sources = []
    for key, entries in by_source.items():
        best = min(entries, key=lambda e: _STATE_ORDER.get(source_state(e), 2))
        sorts = {e.get("native_sort") for e in entries}
        merged_sources.append({
            "source": key,
            "label": best["label"],
            "status": best.get("status"),
            "reason": best.get("reason"),
            "items": sum(int(e.get("items") or 0) for e in entries),
            "limit_reached": bool(best.get("limit_reached")),
            "provider_window": bool(best.get("provider_window")),
            "native_sort": sorts.pop() if len(sorts) == 1 else "mixte",
            "inputs": len(entries),
        })
    merged_sources.sort(key=lambda e: e["label"])
    oldest, oldest_ctx = None, None
    for c in known:
        when = _parse_iso(c.get("started_at"))
        if when is None:
            continue
        try:
            earlier = oldest is None or when < oldest
        except TypeError:          # dates naïve et avec décalage
            earlier = when.replace(tzinfo=None) < oldest.replace(tzinfo=None)
        if earlier:
            oldest, oldest_ctx = when, c
    out: Dict[str, Any] = {
        "inherited_from": None,
        "query": query,
        "query_from_name": None,
        "mode": mode,
        "requested_sources": requested,
        "filters": filters,
        "max_per_source": _common(caps),
        "ranking_mode": _common(rankings) or ("mixte" if distinct_rankings
                                              else None),
        "sources": merged_sources,
        "harvest": False,
        "merged_inputs": len(contexts),
        "merged_unknown": unknown,
        "input_modes": distinct_modes,
        "input_queries": input_queries,
    }
    if oldest is not None:
        out.update({
            "harvest_date": oldest.isoformat(timespec="seconds"),
            "harvest_day": list(_local_date(oldest)),
            "harvest_date_basis": "manifeste (entrée la plus ancienne de la "
                                  "fusion)",
        })
    return out


def _queried_sources(ctx: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {e["source"]: e for e in ctx.get("sources") or [] if e.get("source")}


# === Correspondance des notices ===

def match_works(items_a: Sequence[Dict], items_b: Sequence[Dict], *,
                fuzzy: bool = False) -> Dict[str, Any]:
    """
    Travaux des deux corpus : composantes connexes des notices réunies (A
    puis B), selon les deux étapes de la déduplication, exactement comme
    ``deduplicate_with_report`` : étape 1, clés fortes et garde-fou DOI ;
    étape 2, **toujours** appliquée, en régime d'égalité exacte (titre
    normalisé identique, année, premier auteur, contraintes dures) quand
    ``fuzzy`` est faux, avec en plus la similarité de titre de la passe
    floue quand ``fuzzy`` est vrai. Réutilise directement
    ``utils.deduplication`` pour que diff et déduplication ne divergent pas.

    Returns:
        ``{"groups": [(indices_A, indices_B), …], "ambiguous": {groupe: DOI},
        "versions": {(côté, indice): [clés HAL]}}`` ; les groupes sont triés,
        les indices sont ceux de chaque corpus.
    """
    from utils.deduplication import _fuzzy_rounds, _strong_groups

    na = len(items_a)
    combined = list(items_a) + list(items_b)
    if not combined:
        return {"groups": [], "ambiguous": {}, "versions": {}}
    groups, ambiguous, dois, _n_split, withheld = _strong_groups(combined)
    # Étape 2 de la déduplication, même appel que deduplicate_with_report :
    # régime exact seul sans passe floue, similarité 0,93 avec.
    groups, _motifs, ambiguous, _n = _fuzzy_rounds(
        combined, groups, ambiguous, dois, approximate=bool(fuzzy))

    def split(members) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
        return (tuple(sorted(i for i in members if i < na)),
                tuple(sorted(i - na for i in members if i >= na)))

    owner = {i: gi for gi, members in enumerate(groups) for i in members}
    versions: Dict[Tuple[str, int], List[str]] = {}
    for x, y, key in withheld:
        if owner.get(x) == owner.get(y):
            continue
        for i in (x, y):
            side, idx = ("a", i) if i < na else ("b", i - na)
            versions.setdefault((side, idx), [])
            if key not in versions[(side, idx)]:
                versions[(side, idx)].append(key)
    out_groups = [split(members) for members in groups]
    amb = {split(members): list(linked) for members, linked in ambiguous.items()}
    return {"groups": out_groups, "ambiguous": amb, "versions": versions}


# === Classement des ajouts et des retraits ===

def classify_added(item: Dict, reference_day: Optional[Sequence[int]]
                   ) -> Tuple[str, str]:
    """
    Catégorie d'un ajout (voir l'en-tête du module) et motif lisible.

    Args:
        item: notice de B.
        reference_day: ``(année, mois, jour)`` de la moisson A.
    """
    pub = parse_date(item.get("date"))
    dep = _deposit_date(item)
    if reference_day is None:
        return (CAT_UNDATED if not pub and not dep else CAT_UNDETERMINED,
                "date de la moisson de référence inconnue")
    ref = tuple(reference_day)
    ref_text = _format_date(ref)
    pub_cmp = compare_dates(pub, ref) if pub else None
    dep_cmp = compare_dates(dep, ref) if dep else None
    if pub_cmp == "after":
        return (CAT_NEW_PUBLICATION,
                f"publiée {_format_date(pub)}, après la moisson de référence "
                f"({ref_text})")
    if dep_cmp == "after":
        if pub is None:
            pub_text = " ; sans date de publication"
        elif pub_cmp == "same":
            pub_text = (f" ; publication {_format_date(pub)}, même période que "
                        f"la moisson de référence (antérieure ou non : "
                        f"précision insuffisante)")
        else:
            pub_text = f" ; publication {_format_date(pub)}"
        return (CAT_NEW_DEPOSIT,
                f"déposée dans HAL le {_format_date(dep)}, après la moisson de "
                f"référence ({ref_text})" + pub_text)
    if pub_cmp == "same" or (pub is None and dep_cmp == "same"):
        shown = _format_date(pub or dep)
        return (CAT_UNDETERMINED,
                f"date {shown} : même période que la moisson de référence "
                f"({ref_text}), précision insuffisante")
    if pub_cmp == "before" or dep_cmp == "before":
        shown = _format_date(pub) if pub else f"dépôt {_format_date(dep)}"
        return (CAT_RETRIEVED,
                f"publiée {shown}, avant la moisson de référence ({ref_text})")
    return CAT_UNDATED, "ni date de publication ni date de dépôt"


def _context_notes(item: Dict, other: Dict[str, Any], side_label: str
                   ) -> Tuple[List[str], Optional[str]]:
    """
    Remarques d'une notice présente d'un seul côté, vues depuis l'autre
    corpus (``other`` : son contexte) : hors fenêtre, sources non
    interrogées, en échec ou plafonnées. Renvoie aussi le motif principal
    (codes ``REM_*``).
    """
    notes: List[str] = []
    reason = None
    filters = other.get("filters") or {}
    if outside_window(parse_date(item.get("date")), filters.get("from_date"),
                      filters.get("to_date")):
        notes.append(f"hors de la fenêtre de dates de {side_label} "
                     f"({filters.get('from_date') or '…'} → "
                     f"{filters.get('to_date') or '…'})")
        reason = REM_OUTSIDE_WINDOW
    queried = _queried_sources(other)
    sources = _item_sources(item)
    if queried and sources:
        present = [queried[s] for s in sources if s in queried]
        if not present:
            notes.append(f"sources de la notice non interrogées dans "
                         f"{side_label} ({', '.join(_source_names(sources))})")
            reason = reason or REM_SOURCES_ABSENT
        else:
            states = {source_state(e) for e in present}
            if states == {"failed"}:
                notes.append(f"sources en échec ou incomplètes dans "
                             f"{side_label} : "
                             + ", ".join(_describe_source(e) for e in present))
                reason = reason or REM_SOURCES_FAILED
            elif "ok" not in states:
                notes.append(f"sources plafonnées dans {side_label} : "
                             + ", ".join(_describe_source(e) for e in present))
                reason = reason or REM_TRUNCATED
    return notes, reason


# === Comparaison ===

@dataclass
class DiffResult:
    """Résultat de ``diff_corpus_files`` : rapport complet (document JSON
    sans chemin disque) et fichier écrit (ou None)."""

    report: Dict[str, Any]
    report_path: Optional[Path]


def _work_entry(item: Dict, position: int) -> Dict[str, Any]:
    from utils.ranking import record_id

    keys = [k for k, _ in strong_key_sources(item)]
    creators = _creator_keys(item)
    first = None
    for c in item.get("creators") or []:
        if isinstance(c, dict) and (c.get("lastName") or c.get("name")):
            first = c.get("lastName") or c.get("name")
            break
    return {
        "id": record_id(item, keys),
        "position": position + 1,
        "title": _short(item.get("title")),
        "date": _format_date(parse_date(item.get("date"))),
        "first_author": _short(first, 80),
        "authors": len(creators),
        "itemType": item.get("itemType"),
        "sources": _source_names(_item_sources(item)),
        "keys": keys[:20],
    }


def diff_corpora(items_a: Sequence[Dict], items_b: Sequence[Dict], *,
                 context_a: Optional[Dict[str, Any]] = None,
                 context_b: Optional[Dict[str, Any]] = None,
                 fuzzy: bool = False) -> Dict[str, Any]:
    """
    Compare deux listes de notices Zotero (A, référence ; B, comparé).

    Returns:
        Rapport complet (format ``moisson-shs/diff``) : ``counts``,
        ``warnings``, ``added``, ``removed``, ``enriched``, ``modified``,
        ``identical``, contexte des deux corpus. Aucun chemin disque.
    """
    from utils.ranking import record_id

    context_a = dict(context_a or {"name": "A", "items": len(items_a)})
    context_b = dict(context_b or {"name": "B", "items": len(items_b)})
    matched = match_works(items_a, items_b, fuzzy=fuzzy)
    versions = matched["versions"]
    ambiguous = matched["ambiguous"]
    ref_day = context_a.get("harvest_day")

    added, removed, enriched, modified, identical = [], [], [], [], []
    multiple = 0
    dup_a = dup_b = 0
    enriched_fields: Counter = Counter()
    for group in matched["groups"]:
        ga, gb = group
        if len(ga) > 1:
            dup_a += 1
        if len(gb) > 1:
            dup_b += 1
        notes: List[str] = []
        if group in ambiguous:
            notes.append("ambiguë : clés partagées avec des notices de DOI "
                         "distincts (" + ", ".join(f"doi:{d}" for d in
                                                   ambiguous[group][:4]) + ")")
        if ga and gb:
            ia, ib = ga[0], gb[0]
            biblio, prov = compare_records(items_a[ia], items_b[ib])
            entry: Dict[str, Any] = {
                "id_a": record_id(items_a[ia]),
                "id_b": record_id(items_b[ib]),
                "position_a": ia + 1,
                "position_b": ib + 1,
                "title": _short(items_b[ib].get("title")
                                or items_a[ia].get("title")),
                "date": _format_date(parse_date(items_b[ib].get("date"))),
            }
            if len(ga) > 1 or len(gb) > 1:
                multiple += 1
                notes.append(f"correspondance multiple : {len(ga)} notice(s) "
                             f"dans A, {len(gb)} dans B")
                entry["positions_a"] = [i + 1 for i in ga]
                entry["positions_b"] = [i + 1 for i in gb]
            if notes:
                entry["notes"] = notes
            if biblio:
                entry["changes"] = biblio
            if prov:
                entry["provenance"] = prov
            gained = [c["field"] for c in biblio if c["change"] in ENRICHING_CHANGES]
            if gained:
                entry["enriched_fields"] = gained
                enriched_fields.update(gained)
                enriched.append(entry)
            elif biblio:
                modified.append(entry)
            else:
                identical.append(entry)
            continue
        if gb:
            ib = gb[0]
            entry = _work_entry(items_b[ib], ib)
            category, basis = classify_added(items_b[ib], ref_day)
            entry["category"] = category
            entry["basis"] = basis
            extra_notes, _reason = _context_notes(items_b[ib], context_a, "A")
            notes.extend(extra_notes)
            held = versions.get(("b", ib))
            if held:
                notes.append("version distincte d'un dépôt présent dans A "
                             "(même identifiant HAL, années éloignées) : "
                             + ", ".join(held[:4]))
            if len(gb) > 1:
                notes.append(f"{len(gb)} notices de B pour ce travail")
                entry["positions"] = [i + 1 for i in gb]
            if notes:
                entry["notes"] = notes
            added.append(entry)
        else:
            ia = ga[0]
            entry = _work_entry(items_a[ia], ia)
            extra_notes, reason = _context_notes(items_a[ia], context_b, "B")
            entry["reason"] = reason or REM_ABSENT
            notes.extend(extra_notes)
            held = versions.get(("a", ia))
            if held:
                notes.append("version distincte d'un dépôt présent dans B "
                             "(même identifiant HAL, années éloignées) : "
                             + ", ".join(held[:4]))
            if len(ga) > 1:
                notes.append(f"{len(ga)} notices de A pour ce travail")
                entry["positions"] = [i + 1 for i in ga]
            if notes:
                entry["notes"] = notes
            removed.append(entry)

    order = {c: i for i, c in enumerate(ADDED_CATEGORIES)}

    def _date_key(entry: Dict[str, Any]) -> Tuple:
        parts = parse_date(entry.get("date")) or ()
        return tuple(-p for p in parts) + (0,) * (3 - len(parts))

    added.sort(key=lambda e: (order.get(e["category"], 9), _date_key(e),
                              e["position"]))
    removed.sort(key=lambda e: e["position"])
    for bucket in (enriched, modified, identical):
        bucket.sort(key=lambda e: e["position_b"])

    by_category = Counter(e["category"] for e in added)
    by_reason = Counter(e["reason"] for e in removed)
    without_a = sum(1 for it in items_a if not strong_key_sources(it))
    without_b = sum(1 for it in items_b if not strong_key_sources(it))
    counts = {
        "records_a": len(items_a),
        "records_b": len(items_b),
        "works_a": sum(1 for ga, _ in matched["groups"] if ga),
        "works_b": sum(1 for _, gb in matched["groups"] if gb),
        "added": len(added),
        "removed": len(removed),
        "unchanged": len(enriched) + len(modified) + len(identical),
        "enriched": len(enriched),
        "modified": len(modified),
        "identical": len(identical),
        "added_by_category": {c: by_category.get(c, 0) for c in ADDED_CATEGORIES},
        "removed_by_reason": {r: by_reason.get(r, 0) for r in REMOVED_REASONS},
        "enriched_fields": dict(sorted(enriched_fields.items(),
                                       key=lambda kv: (-kv[1], kv[0]))),
        "multiple_matches": multiple,
        "internal_duplicates_a": dup_a,
        "internal_duplicates_b": dup_b,
        "ambiguous": sum(1 for g in ambiguous),
        "without_keys_a": without_a,
        "without_keys_b": without_b,
    }
    report = {
        "format": DIFF_FORMAT,
        "version": DIFF_VERSION,
        "rules": {
            "version": DIFF_RULES_VERSION,
            "identity": "clés fortes (utils.identity.strong_keys), composantes "
                        "connexes et garde-fou DOI de la déduplication ; "
                        "sans clé commune, titre normalisé identique, année "
                        "et premier auteur (régime exact de la "
                        "déduplication)"
                        + (", puis similarité de titre 0,93 (passe floue)"
                           if fuzzy else ""),
            "fuzzy": bool(fuzzy),
            "reference_date": context_a.get("harvest_date"),
            "reference_date_basis": context_a.get("harvest_date_basis"),
        },
        "corpus_a": context_a,
        "corpus_b": context_b,
        "counts": counts,
        "warnings": diff_warnings(context_a, context_b, counts,
                                  fuzzy=fuzzy),
        "added": added,
        "removed": removed,
        "enriched": enriched,
        "modified": modified,
        "identical": [{"id_a": e["id_a"], "id_b": e["id_b"],
                       "position_a": e["position_a"],
                       "position_b": e["position_b"],
                       **({"provenance": e["provenance"]}
                          if e.get("provenance") else {}),
                       **({"notes": e["notes"]} if e.get("notes") else {})}
                      for e in identical],
    }
    return report


# === Avertissements ===

def _warning(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def _query_slug(text: str) -> str:
    """Forme d'une requête dans un nom de corpus (``main.slugify``) :
    translittération ASCII, séparateurs ``_``, minuscules, 60 caractères."""
    from unidecode import unidecode

    return re.sub(r"[^a-zA-Z0-9]+", "_", unidecode(text or "")).strip("_").lower()[:60]


def queries_match(ctx_a: Dict[str, Any], ctx_b: Dict[str, Any]) -> bool:
    """
    Vrai si les deux corpus ont la même requête : texte comparé sans casse ni
    blancs multiples ; si l'une n'est connue que par le nom du corpus
    (corpus sans manifeste), comparaison sous la forme du nom (sans accents,
    tronquée à 60 caractères).

    Les deux formes de nom sont normalisées pareillement : ``main.slugify``
    retire les séparateurs d'extrémité AVANT de tronquer, de sorte que le nom
    d'une requête longue peut finir par ``_`` (60e caractère), séparateur que
    la requête relue dans le nom (``_query_from_name``) a perdu. Le ``_``
    final est donc retiré des deux côtés après la troncature.
    """
    qa, qb = ctx_a.get("query"), ctx_b.get("query")
    if qa and qb:
        return _fold(qa) == _fold(qb)
    na = qa or ctx_a.get("query_from_name") or ""
    nb = qb or ctx_b.get("query_from_name") or ""
    sa, sb = _query_slug(na).rstrip("_"), _query_slug(nb).rstrip("_")
    return bool(sa and sb) and sa == sb


def _merged_warning(side: str, ctx: Dict[str, Any]) -> str:
    """Avertissement ``corpus_fusionne`` d'un côté fusionné (PAT-18)."""
    n = ctx.get("merged_inputs") or 0
    bits = []
    modes = ctx.get("input_modes") or []
    if len(modes) > 1:
        bits.append("modes " + ", ".join(modes))
    queries = ctx.get("input_queries") or []
    if len(queries) > 1:
        bits.append("requêtes " + ", ".join(f"« {_short(q, 60)} »"
                                            for q in queries[:4])
                    + (" …" if len(queries) > 4 else ""))
    if ctx.get("merged_unknown"):
        bits.append(f"{ctx['merged_unknown']} entrée(s) sans contexte")
    when = (ctx.get("harvest_date") or "")[:10]
    return (f"{side} ({ctx.get('name')}) est une fusion de {n} corpus"
            + (f" ({' ; '.join(bits)})" if bits else "")
            + " : ses notices viennent de productions distinctes ; sources, "
              "fenêtres de dates et plafonds de ses entrées sont réunis, et "
              "la date de référence est celle de l'entrée la plus ancienne"
            + (f" ({when})" if when else "") + ".")


def diff_warnings(ctx_a: Dict[str, Any], ctx_b: Dict[str, Any],
                  counts: Dict[str, Any], *, fuzzy: bool = False
                  ) -> List[Dict[str, str]]:
    """
    Avertissements d'interprétation : mode et tri de chaque moisson, écarts
    de requête, de sources, de filtres ou de plafond, sources incomplètes,
    corpus dérivés ou sans manifeste, notices sans clé forte.
    """
    out: List[Dict[str, str]] = []
    if ctx_a.get("sha256") and ctx_a.get("sha256") == ctx_b.get("sha256"):
        out.append(_warning("meme_contenu",
                            "Les deux fichiers ont le même contenu : aucune "
                            "différence n'est attendue."))

    modes = {"A": ctx_a.get("mode"), "B": ctx_b.get("mode")}
    for side, ctx in (("A", ctx_a), ("B", ctx_b)):
        if not ctx.get("manifest"):
            out.append(_warning(
                "mode_inconnu",
                f"{side} ({ctx.get('name')}) n'a pas de manifeste : mode, tri "
                f"et statut des sources inconnus. S'il s'agit d'une moisson en "
                f"mode bibliographie, ses ajouts et retraits peuvent venir d'un "
                f"changement de classement."))
        elif not ctx.get("sources"):
            out.append(_warning(
                "contexte_absent",
                f"{side} ({ctx.get('name')}) est un corpus "
                f"{ctx.get('kind_label') or ctx.get('kind') or 'dérivé'} sans "
                f"contexte de moisson retrouvé : mode et tri inconnus."))
        elif ctx.get("merged_inputs"):
            out.append(_warning("corpus_fusionne", _merged_warning(side, ctx)))
        elif ctx.get("inherited_from"):
            out.append(_warning(
                "corpus_derive",
                f"{side} ({ctx.get('name')}) est un corpus "
                f"{ctx.get('kind_label') or ctx.get('kind')} : contexte de "
                f"moisson repris de {ctx['inherited_from']}. Ses notices ne "
                f"sont qu'une partie de la moisson d'origine."))
    known = {s: m for s, m in modes.items() if m}
    input_modes = {"A": ctx_a.get("input_modes") or [],
                   "B": ctx_b.get("input_modes") or []}
    if len(set(known.values())) > 1:
        out.append(_warning(
            "modes_differents",
            f"Modes différents (A : {modes['A']}, B : {modes['B']}) : la "
            f"sélection des notices n'obéit pas aux mêmes règles ; une partie "
            f"des ajouts et des retraits en découle."))
    # Une fusion dont une entrée est en mode bibliographie en hérite les
    # réserves (PAT-18).
    biblio = [s for s in ("A", "B") if known.get(s) == "bibliographie"
              or "bibliographie" in input_modes[s]]
    if biblio:
        truncated = []
        for side in biblio:
            ctx = ctx_a if side == "A" else ctx_b
            capped = [e for e in ctx.get("sources") or []
                      if source_state(e) == "truncated"]
            if capped:
                truncated.append(f"{side} : " + ", ".join(
                    f"{e['label']} (tri {e.get('native_sort') or 'par défaut'})"
                    for e in capped))
        message = (
            f"Mode bibliographie ({' et '.join(biblio)}) : chaque source rend "
            f"ses meilleures notices selon son propre score, plafonnées par "
            f"source. Un changement de classement chez le fournisseur "
            f"(nouveaux documents, réindexation) fait entrer ou sortir des "
            f"notices sans nouvelle publication : les ajouts ne sont pas tous "
            f"des nouveautés, ni les retraits des suppressions.")
        if truncated:
            message += " Sources plafonnées : " + " ; ".join(truncated) + "."
        out.append(_warning("mode_bibliographie", message))
    for side, ctx in (("A", ctx_a), ("B", ctx_b)):
        if ctx.get("mode") != "collecte":
            continue
        capped = [e for e in ctx.get("sources") or [] if e.get("limit_reached")]
        if capped:
            out.append(_warning(
                "collecte_plafonnee",
                f"Mode collecte, sources plafonnées dans {side} : "
                + ", ".join(f"{e['label']} (ordre {e.get('native_sort') or 'du fournisseur'})"
                            for e in capped)
                + ". Le plafond suit un ordre déterministe (HAL : les plus "
                  "anciens dépôts d'abord) : des notices récentes peuvent "
                  "manquer."))
    qa = ctx_a.get("query") or ctx_a.get("query_from_name")
    qb = ctx_b.get("query") or ctx_b.get("query_from_name")
    if qa and qb and not queries_match(ctx_a, ctx_b):
        out.append(_warning(
            "requetes_differentes",
            f"Requêtes différentes (A : « {_short(qa, 120)} », B : "
            f"« {_short(qb, 120)} ») : la comparaison ne mesure pas une "
            f"évolution de la même veille."))

    sa, sb = set(_queried_sources(ctx_a)), set(_queried_sources(ctx_b))
    if sa and sb and sa != sb:
        bits = []
        if sa - sb:
            bits.append("seulement dans A : " + ", ".join(sorted(sa - sb)))
        if sb - sa:
            bits.append("seulement dans B : " + ", ".join(sorted(sb - sa)))
        out.append(_warning(
            "sources_differentes",
            "Sources différentes (" + " ; ".join(bits) + ") : les notices de "
            "ces sources apparaissent comme ajouts ou retraits."))
    common = sa & sb
    sorts = []
    qa_src, qb_src = _queried_sources(ctx_a), _queried_sources(ctx_b)
    for key in sorted(common):
        s1, s2 = qa_src[key].get("native_sort"), qb_src[key].get("native_sort")
        if s1 != s2:
            sorts.append(f"{qa_src[key].get('label')} : {s1 or 'par défaut'} → "
                         f"{s2 or 'par défaut'}")
    if sorts:
        out.append(_warning(
            "tris_differents",
            "Tris natifs différents (" + " ; ".join(sorts) + ") : une source "
            "plafonnée ne retient pas les mêmes notices."))

    # Filtres comparés seulement quand les deux contextes sont connus : un
    # corpus sans manifeste n'a pas de filtres « vides », ils sont inconnus.
    fa, fb = ctx_a.get("filters") or {}, ctx_b.get("filters") or {}
    diffs = []
    both_known = bool(ctx_a.get("sources")) and bool(ctx_b.get("sources"))
    for key, label in (("from_date", "date de début"), ("to_date", "date de fin"),
                       ("languages", "langues")):
        va, vb = fa.get(key) or None, fb.get(key) or None
        if va != vb and both_known:
            diffs.append(f"{label} : {va or '—'} → {vb or '—'}")
    if diffs:
        out.append(_warning(
            "filtres_differents",
            "Filtres différents (" + " ; ".join(diffs) + ") : les notices hors "
            "de la fenêtre de l'autre moisson sont des ajouts ou des retraits "
            "attendus (signalés notice par notice)."))
    ma, mb = ctx_a.get("max_per_source"), ctx_b.get("max_per_source")
    if ma and mb and ma != mb:
        out.append(_warning(
            "plafonds_differents",
            f"Plafonds par source différents (A : {ma}, B : {mb}) : une source "
            f"plafonnée ne rend pas le même nombre de notices."))

    for side, ctx in (("A", ctx_a), ("B", ctx_b)):
        failed = [e for e in ctx.get("sources") or [] if source_state(e) == "failed"]
        if failed:
            out.append(_warning(
                "sources_incompletes",
                f"Sources en échec ou incomplètes dans {side} : "
                + ", ".join(_describe_source(e) for e in failed)
                + ". Leurs notices manquent de ce côté : "
                + ("des retraits" if side == "B" else "des ajouts")
                + " en découlent sans changement réel."))

    da, db = ctx_a.get("harvest_date"), ctx_b.get("harvest_date")
    pa, pb = _parse_iso(da), _parse_iso(db)
    if pa and pb:
        try:
            later = pa > pb
        except TypeError:          # dates naïve et avec décalage
            later = pa.replace(tzinfo=None) > pb.replace(tzinfo=None)
        if later:
            out.append(_warning(
                "ordre_chronologique",
                f"A ({da}) est postérieur à B ({db}) : les « ajouts » sont les "
                f"notices de B absentes de A ; pour une veille, A doit être le "
                f"corpus le plus ancien."))

    wa, wb = counts.get("without_keys_a", 0), counts.get("without_keys_b", 0)
    if (wa or wb) and not fuzzy:
        out.append(_warning(
            "sans_cle",
            f"Notices sans identifiant (ni DOI, ni identifiant natif, ni URL "
            f"exploitable) : {wa} dans A, {wb} dans B. Elles ne sont "
            f"appariées que par titre identique une fois normalisé, année et "
            f"premier auteur, sinon elles comptent en ajouts ou retraits ; "
            f"fuzzy=true les compare aussi par similarité de titre (0,93), "
            f"avec les mêmes corroborations."))
    if counts.get("ambiguous"):
        out.append(_warning(
            "ambigus",
            f"{counts['ambiguous']} travail(aux) laissé(s) à part : clés "
            f"partagées avec des notices de DOI distincts (garde-fou DOI)."))
    return out


# === Fichiers ===

# Noms uniques essayés après le nom par défaut (collision d'uuid : jamais en
# pratique).
_UNIQUE_NAME_ATTEMPTS = 100


def diff_corpus_files(path_a: Path, path_b: Path, *, fuzzy: bool = False,
                      report_path: Optional[Path] = None,
                      report_dir: Optional[Path] = None, write: bool = True,
                      path_roots: Sequence[Any] = ()) -> DiffResult:
    """
    Compare deux fichiers de corpus Zotero internes et écrit le rapport.

    Nom du rapport :

    - ``report_path`` fourni (``output_path`` de l'outil) : nom stable
      choisi par l'utilisateur, remplacé atomiquement à chaque comparaison ;
    - sinon, un rapport n'est **jamais remplacé** par une autre comparaison :
      ``<radical de B>.diff.json`` s'il est libre (première comparaison de
      B), à défaut ``<radical de B>_<uuid8>.diff.json``. Le nom rendu par un
      appel désigne donc toujours le rapport de cet appel, même quand deux
      comparaisons du même B se suivent ou se croisent (``_publish_new``).

    Args:
        path_a: corpus de référence (A).
        path_b: corpus comparé (B).
        fuzzy: ajoute la passe floue à l'étape 2 de la déduplication (voir
            le module).
        report_path: rapport à écrire, remplacé s'il existe.
        report_dir: dossier du rapport par défaut (défaut : celui de B).
        write: écrire le rapport (sinon ``report_path`` du résultat est None).
        path_roots: dossiers à retirer des chaînes du rapport (filet : le
            rapport ne contient que des noms de base).

    Raises:
        CorpusDiffInputError: entrée refusée (aucun fichier écrit).
        OSError: écriture du rapport impossible.
    """
    from utils.manifest import atomic_write_bytes, now_iso, scrub_paths

    path_a, path_b = Path(path_a), Path(path_b)
    items_a, raw_a = load_zotero_corpus(path_a, label="corpus_a")
    items_b, raw_b = load_zotero_corpus(path_b, label="corpus_b")
    ctx_a = corpus_context(path_a, items_a, raw_a)
    ctx_b = corpus_context(path_b, items_b, raw_b)
    report = diff_corpora(items_a, items_b, context_a=ctx_a, context_b=ctx_b,
                          fuzzy=fuzzy)
    report["created_at"] = now_iso()
    if not write:
        return DiffResult(report=report, report_path=None)

    def encode(target: Path) -> Tuple[Dict[str, Any], bytes]:
        check_report_output(target)
        doc = dict(report, report=target.name)
        roots = list(path_roots) + [path_a.parent, path_b.parent, target.parent]
        doc = scrub_paths(doc, roots)
        return doc, json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")

    if report_path:
        target = Path(report_path)
        doc, data = encode(target)
        atomic_write_bytes(target, data)
        return DiffResult(report=doc, report_path=target)
    candidates = [default_report_path(path_b, report_dir)] + [
        unique_report_path(path_b, report_dir)
        for _ in range(_UNIQUE_NAME_ATTEMPTS)]
    for target in candidates:
        doc, data = encode(target)
        if _publish_new(target, data):
            return DiffResult(report=doc, report_path=target)
    raise FileExistsError("aucun nom de rapport libre")


# === Restitution ===

# Champs bibliographiques retirés des échantillons quand ``titles`` est faux
# (serveur distant : identifiants seuls, aucune donnée bibliographique
# inline ; la liste complète est dans le rapport téléchargeable).
_BIBLIOGRAPHIC_SAMPLE_KEYS = ("title",)


def _sample(entry: Dict[str, Any], keys: Sequence[str], titles: bool
            ) -> Dict[str, Any]:
    out = {k: entry.get(k) for k in keys
           if titles or k not in _BIBLIOGRAPHIC_SAMPLE_KEYS}
    if entry.get("notes"):
        out["notes"] = entry["notes"]
    return out


def _sample_added(entry: Dict[str, Any], titles: bool = True) -> Dict[str, Any]:
    return _sample(entry, ("id", "title", "date", "category", "basis",
                           "sources", "position"), titles)


def _sample_removed(entry: Dict[str, Any], titles: bool = True) -> Dict[str, Any]:
    return _sample(entry, ("id", "title", "date", "reason", "sources",
                           "position"), titles)


def _sample_common(entry: Dict[str, Any], titles: bool = True) -> Dict[str, Any]:
    out = _sample(entry, ("id_a", "id_b", "title", "date", "position_b"),
                  titles)
    out["changes"] = [{"field": c["field"], "change": c["change"]}
                      for c in entry.get("changes") or []]
    # Ordre stable des clés (notes en dernier).
    if "notes" in out:
        out["notes"] = out.pop("notes")
    return out


def structured_summary(report: Dict[str, Any], sample: int = SAMPLE_DEFAULT,
                       *, titles: bool = True) -> Dict[str, Any]:
    """
    ``structuredContent`` du retour MCP : contexte compact des deux corpus,
    compteurs, avertissements et échantillons bornés (``sample`` par
    catégorie, au plus ``SAMPLE_MAX``) d'identifiants et de titres. Jamais
    de notice complète ni de chemin disque.

    Args:
        titles: faux pour le serveur distant : échantillons réduits aux
            identifiants, dates, sources, catégories ou motifs et noms de
            champs modifiés (aucun titre) ; le rapport complet se
            télécharge.
    """
    n = max(0, min(int(sample), SAMPLE_MAX))

    def ctx(c: Dict[str, Any]) -> Dict[str, Any]:
        return {k: c.get(k) for k in (
            "name", "items", "manifest", "kind", "query", "query_from_name",
            "mode", "harvest_date", "harvest_date_basis", "inherited_from",
            "max_per_source", "filters")} | {
            k: c[k] for k in ("merged_inputs", "input_modes", "input_queries")
            if c.get(k)} | {
            "sources": [{k: e.get(k) for k in (
                "label", "status", "reason", "items", "limit_reached",
                "provider_window", "native_sort")} for e in c.get("sources") or []]}

    # ``report`` et ``created_at`` : nom et date de création du rapport
    # écrit (même valeur que dans le fichier), pour que le client relie le
    # lien rendu à ce rapport précis.
    return {
        "corpus_a": ctx(report["corpus_a"]),
        "corpus_b": ctx(report["corpus_b"]),
        "counts": report["counts"],
        "warnings": report["warnings"],
        "identity": report["rules"],
        "sample_size": n,
        "sample_titles": bool(titles),
        "samples": {
            "added": [_sample_added(e, titles) for e in report["added"][:n]],
            "removed": [_sample_removed(e, titles)
                        for e in report["removed"][:n]],
            "enriched": [_sample_common(e, titles)
                         for e in report["enriched"][:n]],
            "modified": [_sample_common(e, titles)
                         for e in report["modified"][:n]],
        },
        "report": report.get("report"),
        "created_at": report.get("created_at"),
    }


def _context_line(side: str, ctx: Dict[str, Any]) -> str:
    bits = [f"{ctx.get('items', 0)} notice(s)"]
    kind = ctx.get("kind_label") or ctx.get("kind")
    if ctx.get("merged_inputs"):
        kind = f"fusion de {ctx['merged_inputs']} corpus"
    if kind:
        bits.append(kind)
    if ctx.get("harvest_date"):
        bits.append(f"date de référence {ctx['harvest_date'][:10]}"
                    + (f" ({ctx['harvest_date_basis']})"
                       if ctx.get("harvest_date_basis") not in (None, "manifeste")
                       else ""))
    bits.append(f"mode {ctx['mode']}" if ctx.get("mode") else "mode inconnu")
    query = ctx.get("query") or ctx.get("query_from_name")
    if query:
        bits.append(f"requête « {_short(query, 80)} »"
                    + (" (d'après le nom)" if not ctx.get("query") else ""))
    elif len(ctx.get("input_queries") or []) > 1:
        bits.append(f"{len(ctx['input_queries'])} requêtes")
    return f"{side} : {ctx.get('name')} — " + " · ".join(bits)


def _change_text(changes: Sequence[Dict[str, Any]]) -> str:
    return ", ".join(f"{c['label']} ({c['change']})" for c in changes)


def summary_lines(report: Dict[str, Any], sample: int = SAMPLE_DEFAULT,
                  *, titles: bool = True) -> List[str]:
    """
    Résumé texte (français) de la comparaison, sans chemin disque.

    Args:
        sample: notices citées par catégorie (au plus ``TEXT_SAMPLE_MAX``).
        titles: faux pour le serveur distant : aucune notice citée (ni titre
            ni identifiant dans le texte), compteurs et avertissements
            seulement ; les identifiants sont dans ``structuredContent``.
    """
    counts = report["counts"]
    cats = counts["added_by_category"]
    reasons = counts["removed_by_reason"]
    n = max(0, min(int(sample), TEXT_SAMPLE_MAX)) if titles else 0
    lines = [
        "=== Comparaison de corpus (corpus_diff) ===",
        _context_line("A (référence)", report["corpus_a"]),
        _context_line("B (comparé)", report["corpus_b"]),
        "",
        f"Présents dans B seulement (ajouts) : {counts['added']}",
        f"  - nouvelles publications (datées après la moisson de référence) : "
        f"{cats[CAT_NEW_PUBLICATION]}",
        f"  - nouveaux dépôts HAL (publication antérieure ou de date "
        f"imprécise) : {cats[CAT_NEW_DEPOSIT]}",
        f"  - indéterminées (même période que la moisson de référence) : "
        f"{cats[CAT_UNDETERMINED]}",
        f"  - notices nouvellement récupérées (publiées avant) : "
        f"{cats[CAT_RETRIEVED]}",
        f"  - sans date : {cats[CAT_UNDATED]}",
        f"Présents dans A seulement (retraits) : {counts['removed']}",
    ]
    for code in REMOVED_REASONS:
        if reasons.get(code):
            lines.append(f"  - {REMOVED_LABELS[code]} : {reasons[code]}")
    fields = counts.get("enriched_fields") or {}
    lines.append(
        f"Communs aux deux (inchangés dans la sélection) : {counts['unchanged']}"
        f", dont enrichis {counts['enriched']}"
        + (" (" + ", ".join(f"{FIELD_LABELS.get(f, f)} {c}"
                            for f, c in fields.items()) + ")" if fields else "")
        + f", modifiés sans ajout {counts['modified']}, identiques "
          f"{counts['identical']}")
    rules = report["rules"]
    lines.append(
        "Identité : clés fortes partagées (DOI, identifiants HAL, OAI, "
        "Isidore, OpenAlex, S2, CORE, Scholar, URL canonique, ISBN de livre, "
        "PMID, arXiv), garde-fou DOI ; sans clé commune, titre normalisé "
        "identique, année et premier auteur (régime exact de la "
        "déduplication) ; "
        + ("passe floue de la déduplication active (fuzzy=true, similarité "
           "de titre 0,93)." if rules.get("fuzzy")
           else "passe floue désactivée (fuzzy=false)."))
    if report["warnings"]:
        lines.append("")
        lines.append("Avertissements :")
        lines.extend(f"  - {w['message']}" for w in report["warnings"])
    if n and report["added"]:
        lines.append("")
        lines.append(f"Ajouts (nouvelles publications d'abord, {min(n, len(report['added']))} "
                     f"sur {len(report['added'])}) :")
        for e in report["added"][:n]:
            lines.append(f"  - [{e.get('date') or 's. d.'}] {e.get('title') or '(sans titre)'}"
                         f" — {ADDED_LABELS.get(e['category'], e['category'])}"
                         f" ({e['id']})")
    if n and report["removed"]:
        lines.append("")
        lines.append(f"Retraits ({min(n, len(report['removed']))} sur "
                     f"{len(report['removed'])}) :")
        for e in report["removed"][:n]:
            lines.append(f"  - [{e.get('date') or 's. d.'}] {e.get('title') or '(sans titre)'}"
                         f" — {REMOVED_LABELS.get(e['reason'], e['reason'])} ({e['id']})")
    if n and report["enriched"]:
        lines.append("")
        lines.append(f"Notices enrichies ({min(n, len(report['enriched']))} sur "
                     f"{len(report['enriched'])}) :")
        for e in report["enriched"][:n]:
            lines.append(f"  - {e.get('title') or '(sans titre)'} : "
                         f"{_change_text(e.get('changes') or [])}")
    return lines
