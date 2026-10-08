"""
Écriture sûre des fichiers de configuration des clients MCP.

- ``atomic_write`` : fichier temporaire dans le même dossier, ``fsync``,
  ``os.replace`` ; droits conservés (fichier neuf en 0600, dossier créé en
  0700) ; un lien symbolique est suivi (la cible est écrite) ; refus si le
  fichier a changé depuis sa lecture (empreinte), car plusieurs clients
  réécrivent leur propre fichier pendant qu'ils tournent.
- ``backup_file`` : copie ``<fichier>.moisson-bak-AAAAmmjj-HHMMSS`` en 0600
  avant chaque écriture ; ``prune_backups`` n'en garde que les plus
  récentes et ne touche jamais une sauvegarde d'un autre outil.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

BACKUP_TAG = ".moisson-bak-"
BACKUP_KEEP = 5


class ConcurrentModification(RuntimeError):
    """Le fichier a changé entre sa lecture et l'écriture."""


def fingerprint(data: Optional[bytes]) -> Optional[str]:
    """Empreinte SHA-256 d'un contenu (None : fichier absent)."""
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def read_text(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """``(texte, empreinte)`` ; ``(None, None)`` si le fichier est absent."""
    data = _read_bytes(path)
    if data is None:
        return None, None
    return data.decode("utf-8"), fingerprint(data)


def is_world_readable(path: Path) -> bool:
    """Le fichier est-il lisible par le groupe ou les autres comptes ?"""
    try:
        return bool(stat.S_IMODE(os.stat(path).st_mode) & 0o077)
    except OSError:
        return False


def atomic_write(path: Path, text: str, expected_fp: Optional[str],
                 private: bool = False) -> bool:
    """
    Écrit ``text`` dans ``path`` de façon atomique.

    Args:
        expected_fp: empreinte lue avant modification (None : le fichier
            n'existait pas) ; écriture refusée si le fichier a changé.
        private: le contenu porte des secrets : un fichier lisible par
            d'autres comptes passe en 0600.

    Returns:
        True si les droits ont été restreints à 0600.

    Raises:
        ConcurrentModification: le fichier a changé depuis sa lecture.
    """
    target = Path(os.path.realpath(path))
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if fingerprint(_read_bytes(target)) != expected_fp:
        raise ConcurrentModification(str(path))
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    else:
        mode = 0o600
    tightened = False
    if private and mode & 0o077:
        mode, tightened = 0o600, True
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tightened


def backup_file(path: Path, now: Optional[datetime] = None) -> Optional[Path]:
    """Copie horodatée (0600) du fichier avant écriture ; None s'il est
    absent. Deux sauvegardes dans la même seconde : suffixe ``-2``, ``-3``…"""
    path = Path(path)
    if not path.is_file():
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = path.with_name(f"{path.name}{BACKUP_TAG}{stamp}")
    backup, n = base, 1
    while backup.exists():
        n += 1
        backup = base.with_name(f"{base.name}-{n}")
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    return backup


def prune_backups(path: Path, keep: int = BACKUP_KEEP) -> List[Path]:
    """Ne garde que les ``keep`` sauvegardes ``moisson-bak`` les plus
    récentes ; les autres sauvegardes du dossier ne sont jamais touchées."""
    path = Path(path)
    pattern = re.compile(re.escape(path.name) + re.escape(BACKUP_TAG)
                         + r"(\d{8}-\d{6})(?:-(\d+))?$")
    found = []
    for candidate in path.parent.glob(f"{path.name}{BACKUP_TAG}*"):
        match = pattern.match(candidate.name)
        if match:
            found.append(((match.group(1), int(match.group(2) or 1)), candidate))
    found.sort()
    removed = []
    for _key, candidate in found[:max(0, len(found) - keep)]:
        try:
            candidate.unlink()
            removed.append(candidate)
        except OSError:
            pass
    return removed
