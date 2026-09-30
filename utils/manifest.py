"""
Manifeste par corpus, publication atomique, index dérivé et audit des
réponses natives (OPT-16, constats C15, C32, C33).

Manifeste
---------
Chaque production écrit, à côté de ses fichiers, un manifeste versionné
``<stem>.manifest.json`` (format ``moisson-shs/manifest``, version 1) : il
porte les métadonnées de la production, jamais les notices elles-mêmes
(invariant 1 : aucun champ interne dans le JSON Zotero). Contenu :

- identité (``stem``, ``run_id``) et type de production (``kind`` :
  ``moisson``, ``filtre``, ``fusion``, ``conversion_csl``, ``auteur``,
  ``resolution_doi``, ``export``) ;
- nombre d'items, fichier primaire et fichiers publiés (noms de base, rôle,
  taille, empreinte SHA-256, nombre d'items) ; **aucun chemin absolu** : le
  manifeste est téléchargeable en HTTP (fichier associé) ;
- requête, plan de requête (``query_plan`` avec ``applied``), filtres,
  réglages, sources et statuts (``sources`` : ``by_source_status``), dates de
  début et de fin, versions (paquet, schéma du manifeste, règles de
  déduplication, ``RANKING_VERSION``, règles DOI, plan de requête), rapport
  de déduplication résumé, référence au rapport DOI, échéance souple ;
- ``records`` : une entrée par notice finale, dans l'ordre du fichier de
  corpus, rattachée par sa clé forte principale (``utils.ranking.record_id``
  sur ``utils.identity``) : position, rangs et scores natifs par source
  (``SourceResult.ranking_dict`` rattachés aux notices finales), rang du
  classement ``native`` (et ``local`` si calculé) et justification compacte.

Un ajout à un corpus existant (conversion CSL du même radical, export) met à
jour le manifeste sous verrou (lecture, fusion, remplacement atomique) : deux
ajouts simultanés ne perdent rien ; chaque ajout est consigné dans
``history``. Un corpus antérieur au manifeste qui reçoit ainsi son premier
manifeste garde sa date d'origine (horodatage du nom, à défaut date du
fichier) et le type ``ancien`` (production d'origine inconnue) ; l'ajout
n'apparaît que dans ``history``.

Publication atomique
--------------------
``Publication`` écrit chaque fichier dans un temporaire du même dossier
(``.<nom>.<uuid>.tmp``, nom commençant par un point : jamais listé ni servi,
cf. ``utils.output_files``), le synchronise sur disque, puis, sous le verrou
du dossier (``.moisson.lock``, ``fcntl.flock``) : pose le marqueur
``.<stem>.publishing`` (avant d'écrire le temporaire du manifeste), renomme
les fichiers (``os.replace``), publie le manifeste EN DERNIER, synchronise le
dossier et retire le marqueur. Les temporaires orphelins du moissonneur (motif
exact ``.<nom>.<12 hex>.tmp``, jamais un ``.<x>.tmp`` d'un autre logiciel) de
plus de ``PUBLISH_TEMP_MAX_AGE_S`` secondes sont supprimés, ainsi que les
marqueurs périmés : un manifeste au moins aussi récent que le marqueur a été
publié par cette publication, arrêtée avant de retirer son marqueur.

Le filet anti-chemins (``scrub_paths``) ne s'applique qu'aux champs du
producteur, jamais au manifeste existant relu en mode ajout, et ne remplace
que des préfixes de chemin absolus (un dossier relatif comme ``./output``,
défaut de la CLI, est d'abord résolu).

Règle de complétude (index, ``corpus_list``) :

- ``complete`` : manifeste valide, aucun marqueur, fichiers décrits présents
  avec la taille annoncée ;
- ``incomplete`` : marqueur ``.<stem>.publishing`` présent (publication en
  cours ou interrompue avant le manifeste), ou fichier décrit absent ;
- ``modified`` : fichier décrit présent mais de taille différente de celle
  du manifeste (modifié après publication) ;
- ``legacy`` : ni manifeste valide ni marqueur : corpus antérieur au
  manifeste (ou produit hors du moissonneur) ; ses métadonnées (nombre
  d'items, format détecté, dates des fichiers) sont calculées à la demande.

Un export (rôle ``export``, ``corpus_export``) décrit dans le manifeste du
corpus source n'entre pas dans cette règle : c'est un dérivé, dont la
suppression ou le remplacement ne rend pas le corpus incomplet ni modifié
(l'empreinte du manifeste permet de le vérifier).

Une interruption avant le manifeste laisse donc au pire des fichiers de
données et le marqueur : le corpus est présenté comme incomplet, jamais comme
complet.

Index
-----
``OUTPUT_DIR/.index.json`` (format ``moisson-shs/index``) est un dérivé des
manifestes et des fichiers, jamais une source de vérité : reconstruit
paresseusement, stem par stem, quand la liste des fichiers d'un corpus, leur
taille, leur date de modification ou son marqueur changent ; mis à jour par
chaque publication sous le même verrou, avec remplacement atomique ; un index
illisible est reconstruit. ``corpus_list`` et ``list_resources`` le lisent au
lieu de relire chaque corpus.

Audit des réponses natives (C32)
--------------------------------
``raw/`` garde les notices déjà normalisées par les mappers (pas les réponses
des API). En mode audit (``MOISSON_AUDIT_NATIVE=1``), ``NativeAudit`` capture
au niveau HTTP (crochet de ``utils.http``) les réponses natives d'une
exécution dans ``<sortie>/.native_audit/<run_id>.jsonl`` (dossier de service :
jamais listé, lu ni téléchargé), borné par réponse et par exécution, expurgé
(clés d'API, jetons, adresse ``mailto`` masqués ; aucun en-tête de requête ;
en-têtes de réponse choisis).

Limite : seules les réponses servies par le client HTTP de la moisson sont
capturées. L'enrichissement OAI-PMH (``enrich_oai`` : Cairn, Érudit,
OpenEdition, Persée) passe par Sickle, qui appelle ``requests.get``
directement ; ces réponses ne sont pas capturées, ce que le résumé
``native_audit`` du manifeste signale (``not_captured``).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import platform
import re
import socket
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config
from utils.output_files import (
    INDEX_NAME, LOCK_NAME, NATIVE_AUDIT_DIR, ROLE_CSL, ROLE_DIFF_REPORT,
    ROLE_DOI_REPORT, ROLE_EXPORT, ROLE_MANIFEST, ROLE_STATS, ROLE_ZOTERO,
    corpus_stem_of,
    file_role,
    is_service_path, is_temp_file, manifest_name, publishing_marker_name,
    stem_of_publishing_marker, temp_name,
)

logger = logging.getLogger(__name__)


# === Formats et versions ===

MANIFEST_FORMAT = "moisson-shs/manifest"
MANIFEST_VERSION = 1
# Formats des rapports associés, reconnus par le contenu quand le fichier a
# été renommé en ``.json`` (``zotero_input_error``). Valeurs recopiées de
# ``utils.doi_resolver.REPORT_FORMAT`` et ``utils.corpus_diff.DIFF_FORMAT``
# (import évité : ces modules dépendent de celui-ci) ; un test vérifie
# qu'elles restent égales.
DOI_REPORT_FORMAT = "moisson-shs/doi-report"
DIFF_REPORT_FORMAT = "moisson-shs/diff"
INDEX_FORMAT = "moisson-shs/index"
INDEX_VERSION = 1

# Types de production.
KIND_HARVEST = "moisson"
KIND_FILTER = "filtre"
KIND_MERGE = "fusion"
KIND_CSL = "conversion_csl"
KIND_AUTHOR = "auteur"
KIND_DOI = "resolution_doi"
KIND_EXPORT = "export"
# Corpus antérieur au manifeste, complété par un ajout (conversion CSL ou
# export du même radical) : sa production d'origine est inconnue ; l'ajout
# n'apparaît que dans ``history`` et la date d'origine (horodatage du nom, à
# défaut date du fichier décrit) est conservée dans ``finished_at``.
KIND_LEGACY = "ancien"
KINDS = (KIND_HARVEST, KIND_FILTER, KIND_MERGE, KIND_CSL, KIND_AUTHOR,
         KIND_DOI, KIND_EXPORT, KIND_LEGACY)
KIND_LABELS = {
    KIND_HARVEST: "moisson",
    KIND_FILTER: "filtre",
    KIND_MERGE: "fusion",
    KIND_CSL: "conversion CSL",
    KIND_AUTHOR: "recherche par auteur",
    KIND_DOI: "résolution DOI",
    KIND_EXPORT: "export",
    KIND_LEGACY: "ancien corpus (production d'origine inconnue)",
}

# Statuts d'un corpus dans l'index (règle de complétude du module).
STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUS_MODIFIED = "modified"
STATUS_LEGACY = "legacy"
STATUS_LABELS = {
    STATUS_COMPLETE: "complet",
    STATUS_INCOMPLETE: "incomplet (publication interrompue ou en cours)",
    STATUS_MODIFIED: "modifié depuis sa publication",
    STATUS_LEGACY: "ancien (métadonnées calculées)",
}

# Formats de contenu détectés.
FORMAT_ZOTERO = "zotero"
FORMAT_CSL = "csl"
FORMAT_MANIFEST = "manifest"
FORMAT_EMPTY = "empty"
FORMAT_UNKNOWN = "unknown"
FORMAT_INVALID = "invalid"

# Préfixe des corpus listés (convention historique ``corpus_*.json``).
LIST_PREFIX = "corpus_"

# Rôles des fichiers de données (listables) et des fichiers associés.
DATA_ROLES = (ROLE_ZOTERO, ROLE_CSL, ROLE_STATS)
CORPUS_ROLES = (ROLE_ZOTERO, ROLE_CSL)

# Identifiant des règles de déduplication (OPT-06 : union-find sur les clés
# fortes, passe floue conservatrice) quand le module n'en expose pas.
DEDUP_RULES_ID = "opt06"

# Au-delà de cette taille, le décompte des notices d'un corpus ancien se fait
# en flux (mémoire bornée) plutôt que par json.loads.
_COUNT_STREAM_BYTES = 64 * 1024 * 1024


# === Utilitaires ===

def now_iso() -> str:
    """Horodatage local avec décalage, à la seconde (ISO 8601)."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(
        timespec="seconds")


def _ts_from_iso(value: Any) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def json_bytes(obj: Any) -> bytes:
    """Sérialisation des fichiers publiés (format historique : indentation 2,
    caractères non ASCII conservés)."""
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def manifest_bytes(doc: Dict[str, Any]) -> bytes:
    """
    Sérialisation d'un manifeste : indentation 2 pour l'en-tête (lisible),
    une ligne compacte par entrée de ``records`` (taille divisée par deux
    environ sur un gros corpus). JSON valide dans tous les cas.
    """
    records = doc.get("records")
    if not isinstance(records, list) or not records:
        return json_bytes(doc)
    sentinel = f"__records_{uuid.uuid4().hex}__"
    head = dict(doc)
    head["records"] = sentinel
    text = json.dumps(head, ensure_ascii=False, indent=2)
    body = ",\n".join("    " + json.dumps(r, ensure_ascii=False,
                                          separators=(",", ":"))
                      for r in records)
    return text.replace(json.dumps(sentinel), "[\n" + body + "\n  ]", 1).encode("utf-8")


def _fsync_enabled() -> bool:
    return bool(getattr(config, "PUBLISH_FSYNC", True))


def _unlink_quiet(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _fsync_dir(directory: Path) -> None:
    """Synchronise l'entrée de dossier (renommages durables) ; sans effet là
    où un dossier ne s'ouvre pas (Windows)."""
    if not _fsync_enabled():
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_temp_bytes(directory: Path, final_name: str, data: bytes) -> Path:
    """
    Écrit ``data`` dans un temporaire neuf du dossier (``.<final>.<uuid>.tmp``),
    synchronisé sur disque. Le temporaire est supprimé en cas d'échec.
    """
    directory = Path(directory)
    tmp = directory / temp_name(final_name, uuid.uuid4().hex[:12])
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if _fsync_enabled():
                os.fsync(fh.fileno())
    except BaseException:
        _unlink_quiet(tmp)
        raise
    return tmp


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """
    Remplace ``path`` par ``data`` de façon atomique (temporaire du même
    dossier, fsync, ``os.replace``) : un lecteur voit l'ancien contenu ou le
    nouveau, jamais un fichier tronqué. Sans manifeste (payloads bruts).
    """
    path = Path(path)
    tmp = write_temp_bytes(path.parent, path.name, data)
    try:
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    _fsync_dir(path.parent)
    return path


def file_digest(path: Path, chunk: int = 1 << 20) -> Tuple[int, str]:
    """Taille et empreinte SHA-256 d'un fichier, lu par blocs."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            size += len(block)
            h.update(block)
    return size, h.hexdigest()


@lru_cache(maxsize=1)
def software_versions() -> Dict[str, Any]:
    """
    Versions consignées dans chaque manifeste : paquet, identité du code
    (commit, état de l'arbre, empreinte des modules : PAT-03), schéma du
    manifeste, règles de déduplication, classement, règles DOI, plan de
    requête.
    """
    try:
        from utils.build_info import build_info
        info = build_info()
    except Exception:  # noqa: BLE001 - identité du code indisponible
        info = {}
    versions: Dict[str, Any] = {
        "package": info.get("version") or "inconnue",
        "commit": info.get("commit"),
        "dirty": info.get("dirty"),
        "code_sha256": info.get("code_sha256"),
        "manifest": MANIFEST_VERSION,
        "python": platform.python_version(),
    }
    try:
        from utils import deduplication as dd
        versions["dedup_rules"] = {
            "version": getattr(dd, "RULES_VERSION", DEDUP_RULES_ID),
            "fuzzy_title_threshold": getattr(dd, "FUZZY_TITLE_THRESHOLD", None),
            "fuzzy_year_tolerance": getattr(dd, "FUZZY_YEAR_TOLERANCE", None),
            # Règles optionnelles du sprint patch (drapeaux lus au premier
            # manifeste du processus) : co-diffusion, versions Zenodo,
            # chapitres Google Books.
            "options": (dd.dedup_rules().as_dict()
                        if hasattr(dd, "dedup_rules") else {}),
        }
    except Exception:  # noqa: BLE001
        versions["dedup_rules"] = {"version": DEDUP_RULES_ID}
    try:
        from utils.ranking import RANKING_VERSION
        versions["ranking"] = RANKING_VERSION
    except Exception:  # noqa: BLE001
        versions["ranking"] = None
    try:
        from utils.doi_resolver import RULES_VERSION as DOI_RULES
        versions["doi_rules"] = DOI_RULES
    except Exception:  # noqa: BLE001
        versions["doi_rules"] = None
    try:
        from utils.query_plan import PLAN_VERSION
        versions["query_plan"] = PLAN_VERSION
    except Exception:  # noqa: BLE001
        versions["query_plan"] = None
    return versions


# === Chemins absolus : jamais dans un manifeste ===

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _looks_absolute(value: str) -> bool:
    return (value.startswith("/") and len(value) > 1) or bool(
        _WINDOWS_ABS_RE.match(value)) or value.startswith("\\\\")


def absolute_paths_in(doc: Any) -> List[str]:
    """Chaînes d'un document qui ressemblent à un chemin absolu (contrôle)."""
    found: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if _looks_absolute(value):
                found.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    walk(doc)
    return found


def _absolute_root_forms(root: Any) -> List[str]:
    """
    Formes ABSOLUES d'un dossier racine (``absolute()`` et ``resolve()``,
    sans séparateur final). Un dossier relatif (``Path('./output')``, défaut
    de la CLI) n'est jamais retenu sous sa forme brute (``output``) : ce serait
    un mot quelconque, retiré à tort des noms de fichiers et de la requête.
    """
    try:
        path = Path(root)
    except TypeError:
        return []
    forms: List[str] = []
    for make in (path.absolute, path.resolve):
        try:
            form = str(make()).rstrip("/\\")
        except (OSError, RuntimeError):
            continue
        if _looks_absolute(form) and form not in forms:
            forms.append(form)
    return forms


def scrub_paths(doc: Any, roots: Sequence[Any]) -> Any:
    """
    Retire de toute chaîne les dossiers ``roots`` (formes absolue et résolue) :
    un chemin sous l'un d'eux devient son chemin relatif, le dossier seul une
    chaîne vide. Filet de sécurité : les producteurs n'écrivent que des noms
    de base.

    Seuls des PRÉFIXES DE CHEMIN sont remplacés : la racine absolue doit
    commencer un chemin (précédée d'un début de chaîne ou d'un caractère qui
    n'appartient pas à un chemin) et être suivie d'un séparateur ou d'une fin
    de nom. Une sous-chaîne quelconque (le mot ``output`` d'une requête, le
    préfixe ``corpus`` d'un nom de fichier) n'est jamais touchée.
    """
    needles: List[str] = []
    for root in roots:
        if not root:
            continue
        for form in _absolute_root_forms(root):
            if form not in needles:
                needles.append(form)
    if not needles:
        return doc
    needles.sort(key=len, reverse=True)
    pattern = re.compile(
        r"(?<![\w.~/\\-])(?:" + "|".join(re.escape(n) for n in needles)
        + r")(?:[/\\]+|(?![\w.~-]))")

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            if any(needle in value for needle in needles):
                value = pattern.sub("", value)
            return value
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return clean(doc)


# === Verrou de dossier ===

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:  # pragma: no cover - Windows
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None

_THREAD_LOCKS: Dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class LockTimeout(RuntimeError):
    """
    Verrou de publication non obtenu dans le délai.

    Hérite de ``RuntimeError``, pas de ``TimeoutError`` : depuis Python 3.11,
    ``asyncio.TimeoutError`` EST ``TimeoutError``, et le garde des opérations
    longues (``mcp_server._run_guarded``) prendrait ce refus pour l'échéance
    du serveur (« deadline dépassée », appel compté comme annulé), même en
    stdio où aucune échéance n'existe.
    """


def _thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = _THREAD_LOCKS[key] = threading.Lock()
        return lock


def _try_file_lock(fd: int) -> Optional[bool]:
    """
    ``True`` : verrou pris ; ``False`` : tenu par un autre processus ;
    ``None`` : verrou de fichier non pris en charge par le système de
    fichiers (NFS, SMB, certains FUSE). Jusqu'au 2026-09-30, ce dernier cas
    valait verrou obtenu : mesuré avec deux processus, la moitié des entrées
    d'index et des ajouts de manifeste se perdaient (FIN-18, PAT-10).
    """
    if _fcntl is not None:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except (BlockingIOError, PermissionError):
            return False
        except OSError as exc:  # verrou non pris en charge (système distant)
            logger.debug(f"Verrou de fichier indisponible : {exc}")
            return None
    if _msvcrt is not None:  # pragma: no cover - Windows
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return None


# Repli quand le verrou de fichier n'est pas pris en charge (PAT-10,
# décision DP-9) : un répertoire de verrou, créé par os.mkdir (atomique, y
# compris sur NFS), qui porte le propriétaire (PID, hôte, date). Un
# répertoire dont le propriétaire est mort (même hôte) ou plus ancien que
# DIR_LOCK_STALE_S est tenu pour abandonné et retiré.
DIR_LOCK_SUFFIX = ".d"
_DIR_LOCK_OWNER = "owner.json"
DIR_LOCK_STALE_S = 600.0
_FALLBACK_WARNED: set = set()


def dir_lock_path(directory: Path) -> Path:
    """Répertoire de verrou de repli d'un dossier de publication."""
    return Path(directory) / (LOCK_NAME + DIR_LOCK_SUFFIX)


def _pid_alive(pid: int) -> Optional[bool]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _dir_lock_stale(lock_dir: Path) -> bool:
    try:
        age = time.time() - lock_dir.stat().st_mtime
    except OSError:
        return False
    try:
        owner = json.loads((lock_dir / _DIR_LOCK_OWNER).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        owner = {}
    pid = owner.get("pid") if isinstance(owner, dict) else None
    if isinstance(pid, int) and owner.get("host") == socket.gethostname() \
            and _pid_alive(pid) is False:
        return True
    return age > DIR_LOCK_STALE_S


def _remove_dir_lock(lock_dir: Path) -> None:
    try:
        (lock_dir / _DIR_LOCK_OWNER).unlink()
    except OSError:
        pass
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass


def _try_dir_lock(lock_dir: Path, retry_stale: bool = True) -> bool:
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        if retry_stale and _dir_lock_stale(lock_dir):
            logger.warning("Verrou de publication abandonné retiré (%s)",
                           lock_dir.name)
            _remove_dir_lock(lock_dir)
            return _try_dir_lock(lock_dir, retry_stale=False)
        return False
    try:
        (lock_dir / _DIR_LOCK_OWNER).write_text(json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(),
            "since": now_iso()}), encoding="utf-8")
    except OSError:
        pass
    return True


def _warn_lock_fallback(directory: Path) -> None:
    key = str(directory)
    with _THREAD_LOCKS_GUARD:
        if key in _FALLBACK_WARNED:
            return
        _FALLBACK_WARNED.add(key)
    logger.warning(
        "Verrou de fichier (flock) indisponible pour le dossier %s "
        "(système de fichiers distant ?) : repli sur un répertoire de verrou",
        Path(directory).name)


def _file_unlock(fd: int) -> None:
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        elif _msvcrt is not None:  # pragma: no cover - Windows
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def directory_lock(directory: Path, timeout: Optional[float] = None) -> Iterator[None]:
    """
    Verrou exclusif d'un dossier de publication : verrou de fil (même
    processus) puis ``flock`` sur ``<dossier>/.moisson.lock`` (autres
    processus). Non réentrant.

    Raises:
        LockTimeout: verrou non obtenu dans ``timeout`` secondes (défaut
            ``PUBLISH_LOCK_TIMEOUT_S``).
        OSError: fichier de verrou impossible à créer (dossier en lecture
            seule).
    """
    directory = Path(directory)
    if timeout is None:
        timeout = float(getattr(config, "PUBLISH_LOCK_TIMEOUT_S", 30.0))
    try:
        key = str(directory.resolve())
    except OSError:
        key = str(directory.absolute())
    tlock = _thread_lock(key)
    deadline = time.monotonic() + max(0.0, timeout)
    if not tlock.acquire(timeout=max(0.0, timeout)):
        raise LockTimeout(f"verrou de publication du dossier non obtenu en "
                          f"{timeout:g} s (autre publication en cours)")
    try:
        fd = os.open(str(directory / LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            delay = 0.005
            lock_dir: Optional[Path] = None
            got = _try_file_lock(fd)
            while not got:
                if got is None:
                    # Verrou de fichier non pris en charge : répertoire de
                    # verrou (jamais un verrou réputé obtenu).
                    _warn_lock_fallback(directory)
                    lock_dir = dir_lock_path(directory)
                    got = _try_dir_lock(lock_dir)
                    if got:
                        break
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"verrou de publication du dossier non obtenu en "
                        f"{timeout:g} s (autre publication en cours)")
                time.sleep(delay)
                delay = min(delay * 2, 0.1)
                got = (_try_dir_lock(lock_dir) if lock_dir is not None
                       else _try_file_lock(fd))
            try:
                yield
            finally:
                if lock_dir is not None:
                    _remove_dir_lock(lock_dir)
                else:
                    _file_unlock(fd)
        finally:
            os.close(fd)
    finally:
        tlock.release()


# === Lecture tolérante ===

def is_manifest_document(doc: Any) -> bool:
    """Vrai pour un manifeste du moissonneur (format et version connus)."""
    return (isinstance(doc, dict) and doc.get("format") == MANIFEST_FORMAT
            and isinstance(doc.get("version"), int)
            and doc.get("version") <= MANIFEST_VERSION)


def read_manifest(path: Path) -> Optional[Dict[str, Any]]:
    """Manifeste lu, ou None (absent, illisible, format inconnu)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return doc if is_manifest_document(doc) else None


def manifest_path_for(path: Path) -> Optional[Path]:
    """Manifeste associé à un fichier de corpus (même radical), ou None."""
    path = Path(path)
    stem = corpus_stem_of(path.name)
    if stem is None:
        return None
    return path.parent / manifest_name(stem)


def load_manifest_for(path: Path) -> Optional[Dict[str, Any]]:
    """Manifeste valide du corpus auquel appartient ``path``, ou None."""
    target = manifest_path_for(path)
    return read_manifest(target) if target is not None else None


def manifest_describes(doc: Optional[Dict[str, Any]], path: Path) -> bool:
    """
    Vrai si le manifeste ``doc`` décrit ``path`` tel qu'il est sur disque :
    même taille et, quand elle est consignée, même empreinte SHA-256. Ses
    rangs par position ne s'appliquent qu'à cette condition : une
    republication concurrente de même taille (deux instances du serveur)
    attribuerait sinon des rangs aux mauvaises notices (PAT-10).
    """
    if not doc:
        return False
    path = Path(path)
    described = next((e for e in doc.get("files") or []
                      if isinstance(e, dict) and e.get("name") == path.name),
                     None)
    if described is None:
        return False
    try:
        if described.get("bytes") != path.stat().st_size:
            return False
        if described.get("sha256"):
            return file_digest(path)[1] == described["sha256"]
    except OSError:
        return False
    return True


def detect_items_format(data: Any) -> str:
    """
    Format d'un contenu JSON déjà chargé : ``zotero`` (notices portant
    ``itemType``), ``csl`` (notices CSL-JSON : ``type`` sans ``itemType``),
    ``manifest``, ``empty`` (tableau vide) ou ``unknown``.
    """
    if isinstance(data, dict):
        return FORMAT_MANIFEST if data.get("format") == MANIFEST_FORMAT else FORMAT_UNKNOWN
    if not isinstance(data, list):
        return FORMAT_UNKNOWN
    if not data:
        return FORMAT_EMPTY
    sample = [x for x in data[:50] if isinstance(x, dict)]
    if not sample:
        return FORMAT_UNKNOWN
    zotero = sum(1 for x in sample if "itemType" in x)
    csl = sum(1 for x in sample if "itemType" not in x and "type" in x)
    if zotero == len(sample):
        return FORMAT_ZOTERO
    if csl == len(sample):
        return FORMAT_CSL
    return FORMAT_UNKNOWN


def zotero_input_error(path: Path, data: Any = None, *, tool: str,
                       loaded: bool = False) -> Optional[str]:
    """
    Refus clair d'une entrée qui n'est pas un corpus Zotero JSON interne
    (fichier CSL-JSON, manifeste, rapport DOI ou rapport de comparaison de
    ``corpus_diff``), pour les opérations qui l'exigent. Message sans chemin
    disque. Un rapport renommé ``.json`` est reconnu par son champ
    ``format`` quand le contenu est fourni.

    Args:
        path: fichier fourni.
        data: contenu déjà chargé (si ``loaded``) pour la détection par le
            contenu (CSL-JSON nommé ``.json``).
        tool: nom de l'outil (message).

    Returns:
        Le message de refus, ou None si l'entrée est acceptable.
    """
    path = Path(path)
    name = path.name
    role = file_role(name)
    stem = corpus_stem_of(name)
    fmt = detect_items_format(data) if loaded else None
    content_format = data.get("format") if loaded and isinstance(data, dict) \
        else None
    if role == ROLE_DIFF_REPORT or content_format == DIFF_REPORT_FORMAT:
        return (f"Format refusé : {name} est un rapport de comparaison "
                f"(corpus_diff), pas un corpus ; {tool} exige un corpus Zotero "
                f"JSON interne" + _sibling_hint(path, stem))
    if role in (ROLE_MANIFEST, ROLE_DOI_REPORT) or fmt == FORMAT_MANIFEST \
            or content_format == DOI_REPORT_FORMAT:
        kind = ("un rapport de résolution DOI"
                if role == ROLE_DOI_REPORT or content_format == DOI_REPORT_FORMAT
                else "un manifeste (métadonnées de production)")
        return (f"Format refusé : {name} est {kind}, pas un corpus ; {tool} "
                f"exige un corpus Zotero JSON interne"
                + _sibling_hint(path, stem))
    if role == ROLE_CSL or fmt == FORMAT_CSL:
        return (f"Format refusé : {name} est un corpus CSL-JSON (format "
                f"d'import dans Zotero Desktop) ; {tool} exige le corpus "
                f"Zotero JSON interne" + _sibling_hint(path, stem))
    return None


def _sibling_hint(path: Path, stem: Optional[str]) -> str:
    if not stem:
        return "."
    sibling = path.parent / f"{stem}.json"
    try:
        if sibling.is_file() and sibling.name != path.name:
            return f" : utiliser {sibling.name}."
    except OSError:
        pass
    return (" (fichier .json sans suffixe .csl produit par moisson_search, "
            "search_by_author, corpus_filter ou corpus_merge).")


def count_items(path: Path) -> Tuple[Optional[int], str]:
    """
    Nombre de notices et format d'un fichier JSON de corpus (métadonnées
    calculées à la demande pour un corpus sans manifeste).

    Returns:
        ``(nombre ou None, format)`` ; ``(None, "invalid")`` si illisible.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
        if size > _COUNT_STREAM_BYTES:
            from utils.output_files import iter_json_array
            n = 0
            first = None
            for item in iter_json_array(path):
                if first is None:
                    first = item
                n += 1
            return n, (detect_items_format([first]) if first is not None
                       else FORMAT_EMPTY)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None, FORMAT_INVALID
    fmt = detect_items_format(data)
    return (len(data) if isinstance(data, list) else None), fmt


def describe_corpus(path: Path) -> Dict[str, Any]:
    """
    Métadonnées d'un fichier de corpus, qu'il ait un manifeste ou non (lecture
    tolérante des anciens corpus) : nom, radical, taille, date de
    modification, format, nombre d'items, statut et type de production.
    """
    path = Path(path)
    st = path.stat()
    doc = load_manifest_for(path)
    info: Dict[str, Any] = {
        "name": path.name,
        "stem": corpus_stem_of(path.name),
        "bytes": st.st_size,
        "modified": _iso_from_ts(st.st_mtime),
        "role": file_role(path.name),
    }
    described = None
    if doc is not None:
        for entry in doc.get("files") or []:
            if isinstance(entry, dict) and entry.get("name") == path.name:
                described = entry
                break
    if described is not None and described.get("bytes") == st.st_size:
        info.update({
            "status": STATUS_COMPLETE,
            "format": described.get("role"),
            "item_count": described.get("items"),
            "kind": doc.get("kind"),
            "run_id": doc.get("run_id"),
            "created": doc.get("finished_at"),
        })
        return info
    n, fmt = count_items(path) if info["role"] in CORPUS_ROLES else (None, FORMAT_UNKNOWN)
    info.update({
        "status": STATUS_MODIFIED if described is not None else STATUS_LEGACY,
        "format": fmt,
        "item_count": n,
        "kind": doc.get("kind") if doc else None,
        "run_id": doc.get("run_id") if doc else None,
        "created": _iso_from_ts(st.st_mtime),
    })
    return info


# === Publication atomique ===

@dataclass
class _Staged:
    final: Path
    temp: Optional[Path]          # None : fichier déjà en place, décrit
    entry: Dict[str, Any]


class Publication:
    """
    Publication atomique d'un corpus (voir le docstring du module).

    Usage ::

        with Publication(dossier, stem, KIND_FILTER) as pub:
            pub.add_json(f"{stem}.json", notices, role="zotero")
            manifest_path = pub.commit({"filters": {...}})

    Les données sont écrites dans des temporaires dès ``add_*`` (hors
    verrou) ; ``commit`` renomme et publie le manifeste en dernier. Une
    exception avant ``commit`` (ou la sortie du bloc sans ``commit``) retire
    les temporaires : rien n'est publié.

    Args:
        directory: dossier de publication (tous les fichiers y sont écrits).
        stem: radical du corpus (nom du manifeste ``<stem>.manifest.json``).
        kind: type de production (``KIND_*``).
        run_id: identifiant d'exécution (défaut : aucun).
        started_at: début de la production (ISO ; défaut : création).
        append: ajout à un corpus existant de même radical : son manifeste
            (s'il est valide) est complété sous verrou au lieu d'être
            remplacé.
        path_roots: dossiers à retirer de toute chaîne du manifeste (filet
            de sécurité contre un chemin absolu).
    """

    def __init__(self, directory: Path, stem: str, kind: str, *,
                 run_id: Optional[str] = None,
                 started_at: Optional[str] = None,
                 append: bool = False,
                 path_roots: Sequence[Any] = ()):
        if not stem or stem.startswith(".") or "/" in stem or "\\" in stem:
            raise ValueError(f"radical de corpus invalide : {stem!r}")
        self.directory = Path(directory)
        self.stem = stem
        self.kind = kind
        self.run_id = run_id
        self.started_at = started_at or now_iso()
        self.append = append
        self.path_roots = list(path_roots)
        self.manifest_path = self.directory / manifest_name(stem)
        self.manifest: Optional[Dict[str, Any]] = None
        self._staged: List[_Staged] = []
        self._done = False

    # --- préparation ---

    def _final(self, final: Any) -> Path:
        final = Path(final)
        if not final.is_absolute() and str(final.parent) in ("", "."):
            final = self.directory / final.name
        try:
            same = final.parent.resolve() == self.directory.resolve()
        except OSError:
            same = final.parent == self.directory
        if not same:
            raise ValueError(f"{final.name} : hors du dossier de publication")
        if final.name.startswith("."):
            raise ValueError(f"{final.name} : nom réservé")
        if final.name == self.manifest_path.name:
            raise ValueError("le manifeste est écrit par commit()")
        if any(s.final.name == final.name for s in self._staged):
            raise ValueError(f"{final.name} : déjà préparé")
        return final

    def add_bytes(self, final: Any, data: bytes, *, role: str,
                  items: Optional[int] = None) -> Path:
        """Prépare un fichier (temporaire synchronisé) ; renvoie son chemin final."""
        if self._done:
            raise RuntimeError("publication déjà terminée")
        final = self._final(final)
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = write_temp_bytes(self.directory, final.name, data)
        entry: Dict[str, Any] = {
            "name": final.name, "role": role, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if items is not None:
            entry["items"] = int(items)
        self._staged.append(_Staged(final=final, temp=tmp, entry=entry))
        return final

    def add_json(self, final: Any, obj: Any, *, role: str,
                 items: Optional[int] = None) -> Path:
        """Prépare un fichier JSON (sérialisation historique, indentation 2)."""
        if items is None and isinstance(obj, list):
            items = len(obj)
        return self.add_bytes(final, json_bytes(obj), role=role, items=items)

    def add_text(self, final: Any, text: str, *, role: str) -> Path:
        """Prépare un fichier texte UTF-8."""
        return self.add_bytes(final, text.encode("utf-8"), role=role)

    def describe_existing(self, final: Any, *, role: str,
                          items: Optional[int] = None) -> Path:
        """
        Décrit dans le manifeste un fichier déjà en place, non réécrit (corpus
        ancien complété par un ajout) : taille et empreinte calculées.
        """
        final = self._final(final)
        size, sha = file_digest(final)
        entry: Dict[str, Any] = {"name": final.name, "role": role,
                                 "bytes": size, "sha256": sha,
                                 "described_after": True}
        if items is not None:
            entry["items"] = int(items)
        self._staged.append(_Staged(final=final, temp=None, entry=entry))
        return final

    @property
    def paths(self) -> List[Path]:
        """Chemins finaux préparés (sans le manifeste)."""
        return [s.final for s in self._staged]

    # --- publication ---

    def _document(self, fields: Dict[str, Any], base: Optional[Dict[str, Any]],
                  finished: str) -> Dict[str, Any]:
        fields = dict(fields or {})
        files = [dict(s.entry) for s in self._staged]
        primary = fields.pop("primary", None)
        if primary is None:
            for entry in files:
                if entry["role"] in CORPUS_ROLES:
                    primary = entry["name"]
                    break
        item_count = fields.pop("item_count", None)
        if item_count is None:
            for entry in files:
                if entry["name"] == primary:
                    item_count = entry.get("items")
        if (base is None and self.append
                and any(s.temp is None for s in self._staged)):
            # Corpus antérieur au manifeste complété par un ajout : manifeste
            # de base synthétique (production d'origine inconnue, date
            # d'origine), l'ajout n'apparaissant que dans ``history``.
            base = self._legacy_base(fields)
        if base is not None:
            doc = dict(base)
            by_name = {f.get("name"): f for f in doc.get("files") or []
                       if isinstance(f, dict)}
            for entry in files:
                by_name[entry["name"]] = entry
            doc["files"] = list(by_name.values())
            event = {"kind": self.kind, "at": finished,
                     "files": [e["name"] for e in files]}
            if self.run_id:
                event["run_id"] = self.run_id
            for key in ("inputs", "notes", "format", "export"):
                if key in fields:
                    event[key] = fields[key]
            doc["history"] = list(doc.get("history") or []) + [event]
            doc["updated_at"] = finished
            if doc.get("primary") is None and primary is not None:
                doc["primary"] = primary
                doc["item_count"] = item_count
            return doc
        doc: Dict[str, Any] = {
            "format": MANIFEST_FORMAT,
            "version": MANIFEST_VERSION,
            "stem": self.stem,
            "run_id": self.run_id,
            "kind": self.kind,
            "item_count": item_count,
            "primary": primary,
            "files": files,
            "started_at": self.started_at,
            "finished_at": finished,
            "versions": software_versions(),
        }
        for key, value in fields.items():
            if key not in doc:
                doc[key] = value
        return doc

    def _legacy_base(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """
        Manifeste de base d'un corpus antérieur au manifeste (ajout de même
        radical sur un corpus sans manifeste valide, fichier existant décrit
        par ``describe_existing``) : type ``KIND_LEGACY``, sans ``run_id`` ;
        ``finished_at`` = date d'origine, d'après l'horodatage du nom
        (``corpus_<requête>_<AAAAMMJJ>_<HHMMSS>``) ou, à défaut, la date de
        modification la plus ancienne des fichiers décrits ; ``origin`` dit
        d'où vient cette date. Les ``records`` et ``notes`` du producteur
        décrivent le corpus lui-même et sont gardés au premier niveau.
        """
        origin_ts = _name_timestamp(self.stem)
        source = "name" if origin_ts is not None else None
        if origin_ts is None:
            mtimes = []
            for staged in self._staged:
                if staged.temp is None:
                    try:
                        mtimes.append(staged.final.stat().st_mtime)
                    except OSError:
                        pass
            if mtimes:
                origin_ts, source = min(mtimes), "mtime"
        origin = _iso_from_ts(origin_ts) if origin_ts is not None else None
        base: Dict[str, Any] = {
            "format": MANIFEST_FORMAT,
            "version": MANIFEST_VERSION,
            "stem": self.stem,
            "run_id": None,
            "kind": KIND_LEGACY,
            "item_count": None,
            "primary": None,
            "files": [],
            "started_at": None,
            "finished_at": origin,
            "origin": {"date": origin, "date_source": source},
            "versions": software_versions(),
        }
        for key in ("records", "notes"):
            if key in fields:
                base[key] = fields[key]
        return base

    def commit(self, fields: Optional[Dict[str, Any]] = None) -> Path:
        """
        Renomme les fichiers préparés puis publie le manifeste (en dernier),
        sous le verrou du dossier ; met à jour l'index du dossier s'il en a
        un (sinon il sera construit à la prochaine lecture).

        Args:
            fields: champs du manifeste (requête, plan, filtres, réglages,
                sources, records…) ; ``primary`` et ``item_count`` y
                remplacent les valeurs déduites des fichiers.

        Returns:
            Le chemin du manifeste publié.
        """
        if self._done:
            raise RuntimeError("publication déjà terminée")
        finished = now_iso()
        roots = [self.directory, *self.path_roots]
        renamed: List[Path] = []
        mtemp: Optional[Path] = None
        marker = self.directory / publishing_marker_name(self.stem)
        marker_written = False
        manifest_published = False
        try:
            with directory_lock(self.directory):
                base = read_manifest(self.manifest_path) if self.append else None
                # Filet anti-chemins sur les seuls champs du producteur :
                # jamais sur le manifeste existant relu (mode ajout), ni sur
                # les noms de fichiers décrits (identité du corpus).
                clean = scrub_paths(dict(fields or {}), roots)
                doc = self._document(clean, base, finished)
                # Marqueur AVANT le temporaire du manifeste : le manifeste
                # publié garde la date de son temporaire (os.replace), il est
                # donc toujours au moins aussi récent que le marqueur ; un
                # marqueur resté après le renommage du manifeste (arrêt brutal
                # avant son retrait) est ainsi reconnu comme périmé.
                marker.write_text(f"{self.run_id or ''} {finished}\n",
                                  encoding="utf-8")
                marker_written = True
                mtemp = write_temp_bytes(self.directory, self.manifest_path.name,
                                         manifest_bytes(doc))
                for staged in self._staged:
                    if staged.temp is not None:
                        os.replace(staged.temp, staged.final)
                        staged.temp = None
                        renamed.append(staged.final)
                # Le manifeste en dernier : marqueur de complétude.
                os.replace(mtemp, self.manifest_path)
                mtemp = None
                manifest_published = True
                _fsync_dir(self.directory)
                _unlink_quiet(marker)
                self.manifest = doc
                self._done = True
                try:
                    _index_after_publication(self, doc)
                except Exception:  # noqa: BLE001 - l'index est un dérivé
                    logger.warning("Index des corpus non mis à jour (il sera "
                                   "reconstruit à la prochaine lecture)",
                                   exc_info=True)
        except LockTimeout as exc:
            # Verrou non obtenu : rien n'a été renommé ni marqué.
            self._drop_temps()
            raise LockTimeout(f"{exc} ; rien n'a été publié ({self.stem})") from None
        except BaseException:
            # Interruption avant le manifeste : les fichiers déjà renommés
            # restent, le marqueur aussi (corpus « incomplet »), jamais
            # présentés comme complets ; les temporaires restants sont retirés.
            # Le marqueur est retiré quand il ne signale plus rien : manifeste
            # déjà publié (publication achevée) ou aucun fichier renommé (rien
            # n'a changé sur disque).
            self._drop_temps()
            if mtemp is not None:
                _unlink_quiet(mtemp)
            if marker_written and (manifest_published or not renamed):
                _unlink_quiet(marker)
            raise
        try:
            cleanup_orphans(self.directory)
        except OSError:
            pass
        logger.info(f"Corpus publié : {self.stem} ({len(self._staged)} "
                    f"fichier(s) et son manifeste)")
        return self.manifest_path

    def _drop_temps(self) -> None:
        for staged in self._staged:
            if staged.temp is not None:
                _unlink_quiet(staged.temp)
                staged.temp = None

    def abort(self) -> None:
        """Abandonne la publication : temporaires retirés, rien de publié."""
        if not self._done:
            self._drop_temps()
            self._done = True

    def __enter__(self) -> "Publication":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._done:
            self.abort()


def _is_stale_marker(directory: Path, stem: str, marker_stat: os.stat_result,
                     now: float, max_age: float, locked: bool) -> bool:
    """
    Marqueur de publication périmé : son manifeste existe et est au moins
    aussi récent que lui. ``commit`` pose le marqueur AVANT d'écrire le
    temporaire du manifeste : un manifeste au moins aussi récent a donc été
    publié par cette publication, arrêtée avant de retirer le marqueur
    (publication achevée). Sans le verrou du dossier (``locked`` faux), le
    marqueur doit en plus être plus ancien que ``max_age`` (aucune
    publication ne garde son marqueur aussi longtemps) ; sous le verrou,
    aucune publication n'est en cours dans le dossier.
    """
    if not locked and now - marker_stat.st_mtime <= max_age:
        return False
    try:
        manifest_stat = (Path(directory) / manifest_name(stem)).stat()
    except OSError:
        return False
    return manifest_stat.st_mtime_ns >= marker_stat.st_mtime_ns


def cleanup_orphans(directory: Path, max_age: Optional[float] = None) -> int:
    """
    Supprime les temporaires d'écriture orphelins du moissonneur de plus de
    ``max_age`` secondes (défaut ``PUBLISH_TEMP_MAX_AGE_S`` ; motif exact de
    ``utils.output_files.is_temp_file`` : un fichier caché ``.<x>.tmp`` d'un
    autre logiciel n'est jamais touché), et les marqueurs de publication
    périmés (``_is_stale_marker`` : manifeste au moins aussi récent,
    publication achevée, marqueur non retiré). Un marqueur sans manifeste au
    moins aussi récent est conservé : il signale un corpus incomplet.

    Returns:
        Nombre de fichiers supprimés.
    """
    directory = Path(directory)
    if max_age is None:
        max_age = float(getattr(config, "PUBLISH_TEMP_MAX_AGE_S", 3600.0))
    now = time.time()
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        name = entry.name
        if not name.startswith("."):
            continue
        try:
            if is_temp_file(name):
                if now - entry.stat(follow_symlinks=False).st_mtime > max_age:
                    os.unlink(entry.path)
                    removed += 1
                continue
            stem = stem_of_publishing_marker(name)
            if stem is not None and _is_stale_marker(
                    directory, stem, entry.stat(follow_symlinks=False), now,
                    max_age, locked=False):
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


# === Notices finales : rangs, scores, justification ===

def _round(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    return value


def harvest_records(result: Any) -> List[Dict[str, Any]]:
    """
    Une entrée par notice finale d'une moisson, dans l'ordre du corpus :
    ``position`` (1..n), ``id`` (clé forte principale), ``native`` (rang et
    score natifs de chaque source qui a rendu la notice) et, si le classement
    a été calculé, ``ranking`` (rang et score retenus, rang ``native`` et
    ``local``, résumé de justification, réserves, concepts appariés).

    Les rangs natifs sont repris des preuves du classement quand il existe ;
    sinon, les notices des sources (``SourceResult.aligned_ranks``) sont
    rattachées aux notices finales (identité, clés fortes, empreinte ;
    ``utils.ranking.map_source_items``). Rien n'est écrit dans les notices.
    """
    from utils.ranking import record_id

    items = list(getattr(result, "items", None) or [])
    ranking = getattr(result, "ranking", None) or {}
    entries = ranking.get("entries") or []
    records: List[Dict[str, Any]] = []
    if entries and not ranking.get("error"):
        by_index = {e.get("index"): e for e in entries if isinstance(e, dict)}
        for i, item in enumerate(items):
            entry = by_index.get(i)
            record: Dict[str, Any] = {
                "position": i + 1,
                "id": entry.get("id") if entry else record_id(item),
            }
            if entry is not None:
                just = entry.get("justification") or {}
                record["native"] = [
                    {"source": n.get("source"), "rank": n.get("rank"),
                     "score": _round(n.get("score"))}
                    for n in just.get("native") or []]
                rk: Dict[str, Any] = {
                    "rank": entry.get("rank"),
                    "score": _round(entry.get("score")),
                    "native_rank": (entry.get("native") or {}).get("rank"),
                }
                if isinstance(entry.get("local"), dict):
                    rk["local_rank"] = entry["local"].get("rank")
                    rk["local_score"] = _round(entry["local"].get("score"))
                if just.get("summary"):
                    rk["summary"] = just["summary"]
                reserves = [r.get("code") for r in just.get("reserves") or []
                            if isinstance(r, dict) and r.get("code")]
                if reserves:
                    rk["reserves"] = reserves
                concepts = [c.get("concept") for c in just.get("concepts") or []
                            if isinstance(c, dict) and c.get("concept")]
                if concepts:
                    rk["concepts"] = concepts
                record["ranking"] = rk
            records.append(record)
        return records

    native = _native_ranks_by_record(items, getattr(result, "sources", None) or [])
    for i, item in enumerate(items):
        record = {"position": i + 1, "id": record_id(item)}
        if native[i]:
            record["native"] = native[i]
        records.append(record)
    return records


def _native_ranks_by_record(items: List[Dict[str, Any]],
                            sources: Sequence[Any]) -> List[List[Dict[str, Any]]]:
    """Rangs natifs rattachés aux notices finales, sans classement."""
    out: List[List[Dict[str, Any]]] = [[] for _ in items]
    if not items or not sources:
        return out
    try:
        from utils.ranking import _source_inputs, map_source_items
        inputs = _source_inputs(sources)
        mapping = map_source_items(items, inputs)
    except Exception:  # noqa: BLE001 - rangs facultatifs, jamais bloquants
        logger.debug("Rattachement des rangs natifs impossible", exc_info=True)
        return out
    best: List[Dict[str, Dict[str, Any]]] = [dict() for _ in items]
    for (si, pos), ri in mapping.assigned.items():
        if not 0 <= ri < len(items):
            continue
        src = inputs[si]
        info = src.ranks[pos] if pos < len(src.ranks) else {}
        rank = int((info or {}).get("rank") or (pos + 1))
        current = best[ri].get(src.source)
        if current is None or rank < current["rank"]:
            best[ri][src.source] = {"source": src.source, "rank": rank,
                                    "score": _round((info or {}).get("score"))}
    for ri, by_source in enumerate(best):
        out[ri] = sorted(by_source.values(), key=lambda e: (e["rank"], e["source"]))
    return out


def simple_records(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Position et clé forte principale de chaque notice (sans rang)."""
    from utils.ranking import record_id
    return [{"position": i + 1, "id": record_id(item)}
            for i, item in enumerate(items)]


def inherited_records(source_doc: Optional[Dict[str, Any]],
                      items: Sequence[Dict[str, Any]], *,
                      by: str = "id") -> List[Dict[str, Any]]:
    """
    Entrées ``records`` d'un corpus dérivé (filtre, résolution DOI,
    conversion) : reprend celles du manifeste source par clé forte (``by="id"``)
    ou par position (``by="position"``, même ordre et même nombre de
    notices), positions recalculées ; à défaut, position et clé seules.
    """
    from utils.ranking import record_id

    source_records = [r for r in (source_doc or {}).get("records") or []
                      if isinstance(r, dict)]
    if by == "position" and len(source_records) == len(items):
        out = []
        for i, (rec, item) in enumerate(zip(source_records, items)):
            new = dict(rec)
            new["position"] = i + 1
            new["id"] = record_id(item)
            if rec.get("id") != new["id"]:
                new["source_id"] = rec.get("id")
            out.append(new)
        return out
    by_id: Dict[str, Dict[str, Any]] = {}
    for rec in source_records:
        rid = rec.get("id")
        if rid and rid not in by_id:
            by_id[rid] = rec
    out = []
    for i, item in enumerate(items):
        rid = record_id(item)
        rec = dict(by_id.get(rid) or {})
        if rec:
            rec["source_position"] = rec.get("position")
        rec["position"] = i + 1
        rec["id"] = rid
        out.append(rec)
    return out


def soft_deadline_summary(result: Any, ctx: Any) -> Optional[Dict[str, Any]]:
    """Échéance souple de l'exécution (OPT-09 bis), ou None sans échéance."""
    at = getattr(ctx, "soft_deadline", None)
    if at is None:
        return None
    unreachable_key = "soft_deadline_unreachable"
    stopped, unreachable = [], []
    for res in getattr(result, "sources", None) or []:
        if getattr(res, "reason", None) == "deadline":
            stopped.append(res.display_name)
            if (getattr(res, "counters", None) or {}).get(unreachable_key):
                unreachable.append(res.display_name)
    seconds = getattr(ctx, "soft_deadline_s", None)
    return {
        "seconds": round(float(seconds), 3) if seconds is not None else None,
        "reached": time.monotonic() >= at,
        "stopped_sources": stopped,
        "unreachable_sources": unreachable,
    }


def harvest_manifest_fields(result: Any, *, settings: Optional[Dict[str, Any]] = None,
                            ctx: Any = None,
                            native_audit: Optional["NativeAudit"] = None,
                            ) -> Dict[str, Any]:
    """
    Champs du manifeste d'une moisson (``HarvestResult``) : requête et plan,
    filtres, réglages, sources et statuts (avec le tri, les filtres natifs et
    le nombre de rangs de chaque source), déduplication résumée, échéance
    souple, classement résumé (sans ses entrées) et ``records``.
    """
    req = getattr(result, "request", None)

    def _req(name: str, default: Any = None) -> Any:
        return getattr(req, name, default) if req is not None else default

    sources: Dict[str, Any] = {}
    for res in getattr(result, "sources", None) or []:
        try:
            status = res.as_dict()
        except Exception:  # noqa: BLE001 - doublure minimale
            status = {"source": getattr(res, "source", None),
                      "status": getattr(res, "status", None)}
        try:
            ranking = res.ranking_dict()
            status["ranked_items"] = len(ranking.get("ranks") or [])
        except Exception:  # noqa: BLE001 - doublure sans rangs
            status["ranked_items"] = 0
        sources[getattr(res, "display_name", None) or status.get("source")] = status
    ranking = getattr(result, "ranking", None)
    ranking_summary = ({k: v for k, v in ranking.items() if k != "entries"}
                       if isinstance(ranking, dict) else None)
    dedup = getattr(result, "dedup_report", None)
    fields: Dict[str, Any] = {
        "query": {
            "text": _req("query"),
            "mode": _req("mode"),
            "sources": list(_req("sources") or []),
            "from_date": _req("from_date"),
            "to_date": _req("to_date"),
            "languages": list(_req("languages") or []),
            "query_variants": dict(_req("query_variants") or {}),
        },
        "query_plan": getattr(result, "query_plan", None),
        "filters": {
            "from_date": _req("from_date"),
            "to_date": _req("to_date"),
            "languages": list(_req("languages") or []),
        },
        "settings": {
            "max_per_source": _req("max_per_source"),
            "workers": _req("workers"),
            "cache": _req("cache"),
            "enrich_oai": _req("enrich_oai"),
            "dedup": _req("dedup"),
            "allow_metered": _req("allow_metered"),
            **(dict(settings) if settings else {}),
        },
        "harvest_status": getattr(result, "overall_status", None),
        "sources": sources,
        "total_before_dedup": getattr(result, "total_before_dedup", None),
        "duplicates_removed": getattr(result, "duplicates_removed", None),
        "duration_s": round(float(getattr(result, "duration_s", 0.0) or 0.0), 3),
        "dedup": ({k: v for k, v in dedup.items() if k != "groups"}
                  if isinstance(dedup, dict) else None),
        "soft_deadline": soft_deadline_summary(result, ctx),
        "ranking": ranking_summary,
        "records": harvest_records(result),
    }
    if native_audit is not None:
        summary = native_audit.summary()
        if _req("enrich_oai"):
            oai = sorted({getattr(r, "source", None)
                          for r in getattr(result, "sources", None) or []}
                         & set(OAI_ENRICH_SOURCES))
            if oai:
                summary["not_captured"] = {
                    "oai_pmh_getrecord": oai,
                    "reason": "enrichissement OAI-PMH (enrich_oai) exécuté par "
                              "Sickle hors du client HTTP de la moisson : "
                              "réponses non capturées",
                }
        fields["native_audit"] = summary
    return fields


# === Index dérivé ===

@dataclass
class CorpusListing:
    """Résultat de ``list_corpora``."""

    entries: List[Dict[str, Any]]
    total: int
    matched: int
    index_state: str
    duration_s: float = 0.0
    swept: int = 0


_NAME_TS_RE = re.compile(r"_(\d{8})_(\d{6})(?:_[0-9a-f]{8})?$")


def _name_timestamp(stem: str) -> Optional[float]:
    match = _NAME_TS_RE.search(stem)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2),
                                 "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def _query_from_name(stem: str) -> Optional[str]:
    """Requête déduite du nom d'un corpus ancien (``corpus_<slug>_<date>``)."""
    body = stem[len(LIST_PREFIX):] if stem.startswith(LIST_PREFIX) else stem
    body = _NAME_TS_RE.sub("", body)
    body = body.replace("_", " ").strip()
    return body or None


def _scan_directory(directory: Path, locked: bool = False
                    ) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]],
                               Dict[str, float], int]:
    """
    Parcours du dossier : fichiers de corpus par radical (rôle, taille, date
    de modification, lien symbolique), marqueurs de publication, et
    suppression des seuls fichiers de service du moissonneur devenus
    inutiles : temporaires orphelins anciens (motif exact de
    ``is_temp_file``) et marqueurs périmés (``_is_stale_marker`` ; ``locked``
    : parcours sous le verrou du dossier). Aucun autre fichier n'est touché.
    """
    groups: Dict[str, Dict[str, Dict[str, Any]]] = {}
    markers: Dict[str, float] = {}
    swept = 0
    max_age = float(getattr(config, "PUBLISH_TEMP_MAX_AGE_S", 3600.0))
    now = time.time()
    base = None
    for entry in os.scandir(directory):
        name = entry.name
        if name.startswith("."):
            if is_temp_file(name):
                try:
                    if now - entry.stat(follow_symlinks=False).st_mtime > max_age:
                        os.unlink(entry.path)
                        swept += 1
                except OSError:
                    pass
                continue
            stem = stem_of_publishing_marker(name)
            if stem is not None:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _is_stale_marker(directory, stem, st, now, max_age, locked):
                    try:
                        os.unlink(entry.path)
                        swept += 1
                        continue
                    except OSError:
                        # Retrait impossible (lecture seule) : le marqueur
                        # ne signale pas pour autant une publication
                        # interrompue.
                        continue
                markers[stem] = st.st_mtime
            continue
        if not name.startswith(LIST_PREFIX):
            continue
        role = file_role(name)
        if role is None:
            continue
        try:
            st = entry.stat()
            if not entry.is_file():
                continue
        except OSError:          # lien cassé
            continue
        info: Dict[str, Any] = {"role": role, "bytes": st.st_size,
                                "mtime_ns": st.st_mtime_ns}
        if entry.is_symlink():
            if base is None:
                base = Path(directory).resolve()
            try:
                target = Path(entry.path).resolve()
            except (OSError, RuntimeError):
                continue
            info["link"] = {
                "service": bool(is_service_path(name)
                                or is_service_path(target, base)),
                "inside": target.is_relative_to(base),
            }
        stem = corpus_stem_of(name)
        groups.setdefault(stem, {})[name] = info
    return groups, markers, swept


def _stem_signature(files: Dict[str, Dict[str, Any]], marker: bool) -> Dict[str, Any]:
    return {"files": {n: [f["bytes"], f["mtime_ns"]] for n, f in sorted(files.items())},
            "marker": bool(marker)}


def _describe_stem(directory: Path, stem: str, files: Dict[str, Dict[str, Any]],
                   marker: bool, previous: Optional[Dict[str, Any]]
                   ) -> Optional[Dict[str, Any]]:
    """Entrée d'index d'un corpus (radical), ou None s'il n'a aucun fichier
    de données."""
    if not any(f["role"] in DATA_ROLES for f in files.values()):
        return None
    doc = None
    mname = manifest_name(stem)
    if mname in files:
        doc = read_manifest(Path(directory) / mname)
    prev_files = (previous or {}).get("files") or {}
    described: Dict[str, Dict[str, Any]] = {}
    if doc is not None:
        described = {f.get("name"): f for f in doc.get("files") or []
                     if isinstance(f, dict) and f.get("name")}
    out_files: Dict[str, Dict[str, Any]] = {}
    status = STATUS_COMPLETE if doc is not None else STATUS_LEGACY
    notes: List[str] = []
    for name, info in sorted(files.items()):
        if info["role"] == ROLE_MANIFEST:
            continue
        entry: Dict[str, Any] = {"role": info["role"], "bytes": info["bytes"],
                                 "mtime_ns": info["mtime_ns"]}
        if "link" in info:
            entry["link"] = info["link"]
        desc = described.get(name)
        if desc is not None and desc.get("bytes") == info["bytes"]:
            entry["items"] = desc.get("items")
            role = desc.get("role") or info["role"]
            entry["format"] = role if role in CORPUS_ROLES else None
        elif (info.get("link") or {}).get("service"):
            # Lien vers un fichier de service : jamais lu (ni listé).
            entry["items"] = None
            entry["format"] = FORMAT_UNKNOWN
        elif info["role"] in CORPUS_ROLES:
            prev = prev_files.get(name) or {}
            if (prev.get("bytes") == info["bytes"]
                    and prev.get("mtime_ns") == info["mtime_ns"]
                    and "items" in prev):
                entry["items"] = prev.get("items")
                entry["format"] = prev.get("format")
            else:
                n, fmt = count_items(Path(directory) / name)
                entry["items"] = n
                entry["format"] = fmt
        out_files[name] = entry
    if doc is not None:
        for name, desc in described.items():
            # Un export est un dérivé (hors règle de complétude) : il n'est
            # d'ailleurs jamais regroupé sous le radical par le parcours.
            if desc.get("role") in (ROLE_MANIFEST, ROLE_EXPORT):
                continue
            current = files.get(name)
            if current is None:
                status = STATUS_INCOMPLETE
                notes.append(f"{name} absent")
            elif (desc.get("bytes") is not None
                    and current["bytes"] != desc.get("bytes")
                    and status == STATUS_COMPLETE):
                status = STATUS_MODIFIED
                notes.append(f"{name} modifié")
    elif mname in files:
        notes.append("fiche de publication illisible")
    if marker:
        status = STATUS_INCOMPLETE
        notes.append("publication interrompue ou en cours")
    mtimes = [f["mtime_ns"] / 1e9 for f in files.values()]
    if doc is not None:
        created = doc.get("finished_at") or doc.get("updated_at")
        created_ts = _ts_from_iso(created) or (min(mtimes) if mtimes else 0.0)
        updated_ts = _ts_from_iso(doc.get("updated_at")) or created_ts
        query = doc.get("query")
        if isinstance(query, dict):
            query = query.get("text")
        primary = doc.get("primary")
        item_count = doc.get("item_count")
        kind = doc.get("kind")
        run_id = doc.get("run_id")
    else:
        created_ts = _name_timestamp(stem) or (min(mtimes) if mtimes else 0.0)
        updated_ts = max(mtimes) if mtimes else created_ts
        created = _iso_from_ts(created_ts)
        query = None
        primary = None
        for role in CORPUS_ROLES:
            for name, entry in out_files.items():
                if entry["role"] == role:
                    primary = name
                    break
            if primary:
                break
        item_count = out_files.get(primary, {}).get("items") if primary else None
        kind = None
        run_id = None
    query = query if isinstance(query, str) and query.strip() else None
    # Requête déduite du nom : corpus sans manifeste, ou ancien corpus
    # complété par un ajout (production d'origine inconnue, sans requête
    # consignée) ; jamais pour une fusion ou une recherche par auteur, dont
    # le nom ne porte pas de requête.
    from_name = (_query_from_name(stem)
                 if query is None and kind in (None, KIND_LEGACY) else None)
    entry = {
        "stem": stem,
        "status": status,
        "kind": kind,
        "run_id": run_id,
        "query": query,
        "query_from_name": from_name,
        "created": created,
        "created_ts": created_ts,
        "updated_ts": updated_ts,
        "item_count": item_count,
        "primary": primary,
        "manifest": mname if doc is not None else None,
        "files": out_files,
        "notes": notes,
        "sig": _stem_signature(files, marker),
    }
    return entry


def _read_index(directory: Path) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads((Path(directory) / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if (not isinstance(doc, dict) or doc.get("format") != INDEX_FORMAT
            or doc.get("version") != INDEX_VERSION
            or not isinstance(doc.get("corpora"), dict)):
        return None
    return doc


def _write_index(directory: Path, corpora: Dict[str, Dict[str, Any]],
                 ignored: Optional[Dict[str, Any]] = None) -> None:
    """
    Écrit l'index (remplacement atomique). ``ignored`` : signatures des
    radicaux ``corpus_*`` sans fichier de données (manifeste ou rapport DOI
    resté seul, export), mémorisées pour ne pas les réexaminer ni réécrire
    l'index à chaque lecture tant qu'ils ne changent pas.
    """
    doc = {"format": INDEX_FORMAT, "version": INDEX_VERSION,
           "updated_at": now_iso(), "corpora": corpora,
           "ignored": ignored or {}}
    data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = write_temp_bytes(Path(directory), INDEX_NAME, data)
    try:
        os.replace(tmp, Path(directory) / INDEX_NAME)
    except BaseException:
        _unlink_quiet(tmp)
        raise


def _refresh(directory: Path, rebuild: bool, write: bool
             ) -> Tuple[Dict[str, Dict[str, Any]], str, int]:
    """Index à jour (corpus par radical), état (``hot``, ``refreshed``,
    ``rebuilt``) et nombre de temporaires supprimés."""
    current = None if rebuild else _read_index(directory)
    state = "hot"
    if current is None:
        state = "rebuilt"
        previous: Dict[str, Dict[str, Any]] = {}
        previous_ignored: Dict[str, Any] = {}
    else:
        previous = current["corpora"]
        previous_ignored = current.get("ignored")
        if not isinstance(previous_ignored, dict):
            previous_ignored = {}
    # ``write`` : parcours sous le verrou du dossier (voir load_index).
    groups, markers, swept = _scan_directory(directory, locked=write)
    corpora: Dict[str, Dict[str, Any]] = {}
    ignored: Dict[str, Any] = {}
    changed = current is None
    for stem, files in groups.items():
        marker = stem in markers
        old = previous.get(stem)
        sig = _stem_signature(files, marker)
        if old is not None and old.get("sig") == sig:
            corpora[stem] = old
            continue
        if old is None and previous_ignored.get(stem) == sig:
            # Radical sans fichier de données, inchangé : ni réexaminé, ni
            # cause d'une réécriture de l'index.
            ignored[stem] = sig
            continue
        entry = _describe_stem(directory, stem, files, marker, old)
        changed = True
        if entry is not None:
            corpora[stem] = entry
        else:
            ignored[stem] = sig
    if set(previous) - set(corpora) or set(previous_ignored) - set(ignored):
        changed = True
    if changed and state == "hot":
        state = "refreshed"
    if changed and write:
        try:
            _write_index(directory, corpora, ignored)
        except OSError as exc:
            logger.warning(f"Index des corpus non écrit ({type(exc).__name__}) : "
                           f"lecture poursuivie en mémoire")
    return corpora, state, swept


def load_index(directory: Path, *, rebuild: bool = False) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """
    Index des corpus du dossier, mis à jour paresseusement sous verrou
    (reconstruction complète si ``rebuild`` ou si l'index est absent ou
    illisible). Dossier non inscriptible : index calculé en mémoire.

    Returns:
        ``(corpus par radical, état)`` ; état ``hot`` (index à jour),
        ``refreshed`` (entrées mises à jour), ``rebuilt``, ou ``memory``.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return {}, "memory"
    try:
        with directory_lock(directory):
            corpora, state, _swept = _refresh(directory, rebuild, write=True)
            return corpora, state
    except (OSError, LockTimeout) as exc:
        logger.debug(f"Index sans verrou ({type(exc).__name__}) : calcul en mémoire")
        corpora, _state, _swept = _refresh(directory, rebuild, write=False)
        return corpora, "memory"


def _index_after_publication(pub: Publication, doc: Dict[str, Any]) -> None:
    """
    Mise à jour de l'index du dossier après une publication (appelée sous le
    verrou du dossier) : seule l'entrée du radical publié est recalculée ; un
    index absent n'est pas créé ici (il le sera paresseusement à la
    prochaine lecture).
    """
    directory = pub.directory
    if not pub.stem.startswith(LIST_PREFIX):
        return
    current = _read_index(directory)
    if current is None:
        # Pas encore d'index (ou index illisible) : il sera construit à la
        # prochaine lecture (paresseusement), pas au milieu d'une production.
        return
    corpora = dict(current["corpora"])
    files: Dict[str, Dict[str, Any]] = {}
    for name in _stem_candidates(directory, pub.stem):
        path = directory / name
        try:
            st = path.stat()
        except OSError:
            continue
        role = file_role(name)
        if role is None or not path.is_file():
            continue
        info: Dict[str, Any] = {"role": role, "bytes": st.st_size,
                                "mtime_ns": st.st_mtime_ns}
        if path.is_symlink():
            base = directory.resolve()
            target = path.resolve()
            info["link"] = {"service": bool(is_service_path(target, base)),
                            "inside": target.is_relative_to(base)}
        files[name] = info
    marker = (directory / publishing_marker_name(pub.stem)).exists()
    entry = _describe_stem(directory, pub.stem, files, marker,
                           corpora.get(pub.stem))
    ignored = current.get("ignored")
    ignored = dict(ignored) if isinstance(ignored, dict) else {}
    if entry is None:
        corpora.pop(pub.stem, None)
        ignored[pub.stem] = _stem_signature(files, marker)
    else:
        corpora[pub.stem] = entry
        ignored.pop(pub.stem, None)
    _write_index(directory, corpora, ignored)


def _stem_candidates(directory: Path, stem: str) -> List[str]:
    """Noms des fichiers possibles d'un radical (convention de nommage)."""
    from utils.output_files import (
        CSL_SUFFIX, DOI_REPORT_SUFFIX, MANIFEST_SUFFIX, STATS_SUFFIX,
        ZOTERO_SUFFIX,
    )
    return [stem + s for s in (ZOTERO_SUFFIX, CSL_SUFFIX, STATS_SUFFIX,
                               MANIFEST_SUFFIX, DOI_REPORT_SUFFIX)]


def _fold(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def parse_since(value: Any) -> Optional[float]:
    """
    Borne ``since`` de ``corpus_list`` : ``AAAA``, ``AAAA-MM``, ``AAAA-MM-JJ``
    ou date-heure ISO (heure locale si sans décalage) ; None si absente.

    Raises:
        ValueError: valeur illisible.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt, pattern in (("%Y", r"^\d{4}$"), ("%Y-%m", r"^\d{4}-\d{2}$")):
        if re.match(pattern, text):
            return datetime.strptime(text, fmt).timestamp()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"date {text!r} illisible (AAAA, AAAA-MM, AAAA-MM-JJ "
                         f"ou date-heure ISO attendue)") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _matches(entry: Dict[str, Any], pattern: str) -> bool:
    folded = _fold(pattern)
    haystacks = [_fold(entry.get("stem")), _fold(entry.get("query")),
                 _fold(entry.get("query_from_name"))]
    haystacks += [_fold(n) for n in (entry.get("files") or {})]
    if any(ch in pattern for ch in "*?["):
        return any(fnmatch.fnmatchcase(h, folded) for h in haystacks if h)
    return any(folded in h for h in haystacks if h)


def list_corpora(directory: Path, *, limit: Optional[int] = None,
                 pattern: Optional[str] = None, since: Any = None,
                 rebuild: bool = False,
                 accept: Optional[Callable[[Dict[str, Any]], bool]] = None,
                 ) -> CorpusListing:
    """
    Corpus du dossier (index dérivé), du plus récent au plus ancien.

    Args:
        limit: nombre maximal d'entrées rendues (None : toutes).
        pattern: motif glob (``*``, ``?``) ou sous-chaîne, insensible à la
            casse et aux accents, appliqué au nom du corpus, à ses fichiers
            et à sa requête.
        since: date minimale de création (voir ``parse_since``).
        rebuild: reconstruction complète de l'index.
        accept: filtre supplémentaire sur l'entrée (transport).

    Raises:
        ValueError: ``since`` illisible.
    """
    t0 = time.perf_counter()
    since_ts = parse_since(since)
    corpora, state = load_index(directory, rebuild=rebuild)
    entries = list(corpora.values())
    if accept is not None:
        entries = [e for e in entries if accept(e)]
    total = len(entries)
    if pattern:
        entries = [e for e in entries if _matches(e, pattern)]
    if since_ts is not None:
        entries = [e for e in entries if (e.get("created_ts") or 0) >= since_ts]
    # Création (manifeste : fin de production, à la seconde), puis date de
    # modification la plus récente des fichiers (départage à la
    # nanoseconde), puis nom.
    entries.sort(key=lambda e: (e.get("created_ts") or 0, e.get("updated_ts") or 0,
                                e.get("stem")), reverse=True)
    matched = len(entries)
    if limit is not None:
        entries = entries[:max(0, int(limit))]
    return CorpusListing(entries=entries, total=total, matched=matched,
                         index_state=state,
                         duration_s=time.perf_counter() - t0)


# === Audit des réponses natives (C32) ===

# Sources dont l'enrichissement OAI-PMH (``enrich_oai``) passe par Sickle,
# qui appelle directement ``requests.get`` hors de la session de la moisson :
# ces réponses échappent au crochet d'audit (limite signalée dans le résumé
# ``native_audit`` du manifeste, clé ``not_captured``).
OAI_ENRICH_SOURCES = ("cairn", "erudit", "openedition", "persee")

# Paramètres dont la valeur est masquée (clés, jetons, adresse de contact).
AUDIT_SECRET_PARAMS = frozenset({
    "api_key", "apikey", "key", "token", "access_token", "mailto", "email",
    "password", "secret", "client_secret", "auth", "authorization",
})
# En-têtes de réponse conservés (aucun en-tête de requête n'est écrit).
AUDIT_RESPONSE_HEADERS = (
    "content-type", "content-length", "content-encoding", "date", "etag",
    "last-modified", "retry-after", "x-rate-limit-limit",
    "x-rate-limit-interval", "x-concurrency-limit", "x-ratelimit-limit",
    "x-ratelimit-remaining", "x-ratelimit-reset",
)


def _known_secret_values(params: Any = None) -> List[str]:
    """Valeurs secrètes à masquer dans une réponse : clés d'API de
    l'environnement et valeurs des paramètres sensibles de la requête."""
    values = []
    for attr in ("ENV_OPENALEX_KEY", "ENV_S2_KEY", "ENV_CORE_KEY", "ENV_SERPAPI_KEY"):
        env_name = getattr(config, attr, None)
        value = os.environ.get(env_name or "", "") if env_name else ""
        if value and len(value) >= 6:
            values.append(value)
    pairs = params.items() if isinstance(params, dict) else (params or [])
    try:
        for k, v in pairs:
            if str(k).lower() in AUDIT_SECRET_PARAMS and v and len(str(v)) >= 6:
                values.append(str(v))
    except (TypeError, ValueError):
        pass
    return sorted(set(values), key=len, reverse=True)


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(URL illisible)"
    if parts.query:
        query = urlencode([(k, "***" if k.lower() in AUDIT_SECRET_PARAMS else v)
                           for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        parts = parts._replace(query=query)
    netloc = parts.netloc.rsplit("@", 1)[-1]      # identifiants d'URL retirés
    return urlunsplit(parts._replace(netloc=netloc))


def _redact_text(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, "***")
    from utils.http import redact_secrets
    text = redact_secrets(text)
    return re.sub(r"(?i)((?:mailto|email)=)[^&\s'\"]+", r"\1***", text)


class NativeAudit:
    """
    Enregistreur des réponses natives d'une exécution (mode audit) : une
    ligne JSON par réponse dans ``<dossier>/<run_id>.jsonl``, corps borné à
    ``max_response_bytes`` octets, total borné à ``max_run_bytes`` octets
    (au-delà, une ligne signale l'arrêt de la capture). Sûr entre fils.
    """

    def __init__(self, directory: Path, run_id: str, *,
                 max_response_bytes: Optional[int] = None,
                 max_run_bytes: Optional[int] = None):
        safe_run = re.sub(r"[^A-Za-z0-9_.-]", "_", str(run_id)) or "run"
        self.directory = Path(directory)
        self.path = self.directory / f"{safe_run}.jsonl"
        self.max_response_bytes = int(max_response_bytes if max_response_bytes is not None
                                      else config.AUDIT_NATIVE_MAX_RESPONSE_BYTES)
        self.max_run_bytes = int(max_run_bytes if max_run_bytes is not None
                                 else config.AUDIT_NATIVE_MAX_RUN_BYTES)
        self._lock = threading.Lock()
        self.written = 0
        self.responses = 0
        self.dropped = 0
        self.truncated_bodies = 0
        self._capped = False

    def _body(self, response: Any) -> Tuple[bytes, int, str]:
        content = getattr(response, "content", None)
        if not isinstance(content, (bytes, bytearray)):
            text = getattr(response, "text", None)
            if not isinstance(text, str):
                try:
                    text = json.dumps(response.json(), ensure_ascii=False)
                except Exception:  # noqa: BLE001 - doublure sans corps
                    text = ""
            content = text.encode("utf-8")
        content = bytes(content)
        return content, len(content), hashlib.sha256(content).hexdigest()

    def capture(self, response: Any, *, url: str, params: Any = None) -> None:
        """Consigne une réponse (appelé par ``utils.http`` ; jamais bloquant)."""
        with self._lock:
            if self._capped:
                self.dropped += 1
                return
        secrets = _known_secret_values(params)
        content, size, digest = self._body(response)
        body = content[:self.max_response_bytes].decode("utf-8", errors="replace")
        truncated = size > self.max_response_bytes
        full_url = getattr(response, "url", None)
        if not isinstance(full_url, str) or not full_url:
            full_url = url + (("&" if "?" in url else "?") + urlencode(params)
                              if isinstance(params, dict) and params else "")
        headers = getattr(response, "headers", None) or {}
        kept_headers = {}
        for name in AUDIT_RESPONSE_HEADERS:
            try:
                value = headers.get(name)
            except Exception:  # noqa: BLE001
                value = None
            if value is not None:
                kept_headers[name] = _redact_text(str(value), secrets)
        record = {
            "ts": now_iso(),
            "method": "GET",
            "url": _redact_text(_redact_url(full_url), secrets),
            "status": getattr(response, "status_code", None),
            "from_cache": bool(getattr(response, "from_cache", False)),
            "headers": kept_headers,
            "body_bytes": size,
            "body_sha256": digest,
            "body_truncated": truncated,
            "body": _redact_text(body, secrets),
        }
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            if self._capped:
                self.dropped += 1
                return
            if self.written + len(line) > self.max_run_bytes:
                self._capped = True
                self.dropped += 1
                note = (json.dumps({"ts": now_iso(), "capture_stopped": True,
                                    "reason": f"plafond de {self.max_run_bytes} "
                                              f"octets par exécution atteint"},
                                   ensure_ascii=False) + "\n").encode("utf-8")
                self._append(note)
                return
            self._append(line)
            self.written += len(line)
            self.responses += 1
            if truncated:
                self.truncated_bodies += 1

    def _append(self, data: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as fh:
            fh.write(data)

    def summary(self) -> Dict[str, Any]:
        """Résumé pour le manifeste (chemin relatif au dossier de sortie)."""
        return {
            "file": f"{NATIVE_AUDIT_DIR}/{self.path.name}",
            "responses": self.responses,
            "bytes": self.written,
            "truncated_bodies": self.truncated_bodies,
            "dropped": self.dropped,
            "max_response_bytes": self.max_response_bytes,
            "max_run_bytes": self.max_run_bytes,
        }


def native_audit_for_run(output_dir: Path, run_id: str) -> Optional[NativeAudit]:
    """
    Enregistreur d'audit d'une exécution si ``MOISSON_AUDIT_NATIVE=1``
    (``config.AUDIT_NATIVE``), None sinon. Les plus anciens fichiers
    d'exécution au-delà de ``AUDIT_NATIVE_KEEP_RUNS`` sont supprimés.
    """
    if not getattr(config, "AUDIT_NATIVE", False) or output_dir is None:
        return None
    directory = Path(output_dir) / NATIVE_AUDIT_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        keep = int(getattr(config, "AUDIT_NATIVE_KEEP_RUNS", 50))
        if keep > 0:
            runs = sorted((p for p in directory.glob("*.jsonl")),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for old in runs[keep - 1:]:
                _unlink_quiet(old)
    except OSError as exc:
        logger.warning(f"Audit des réponses natives indisponible ({exc})")
        return None
    return NativeAudit(directory, run_id)
