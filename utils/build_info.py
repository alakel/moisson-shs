"""
Identité du code en cours d'exécution (PAT-03) : version, commit, état de
l'arbre de travail et empreinte des modules.

Un numéro de version seul ne distingue pas deux états du code : 0.3.0 est
resté inchangé de juin à septembre 2026, alors que le moissonneur passait de
sept à douze sources. Chaque manifeste porte donc aussi le commit, l'état de
l'arbre et l'empreinte des modules empaquetés, calculés une fois par
processus (le journal de démarrage et ``moisson-shs version`` les montrent
aussi).

Ordre de résolution du commit :

1. ``MOISSON_BUILD_COMMIT`` : image Docker, dont le contexte de construction
   exclut ``.git`` (argument de construction recopié dans l'environnement) ;
2. lecture directe de ``.git`` à la racine du projet (``HEAD``, référence
   libre, ``packed-refs``, fichier ``.git`` de worktree), sans sous-processus :
   le ``PATH`` d'un serveur lancé par Claude Desktop ne garantit pas ``git`` ;
3. sinon ``None`` (roue installée hors dépôt) : l'empreinte ``code_sha256``
   reste disponible et suffit à identifier le code.

``dirty`` (arbre modifié depuis le commit) n'est calculé que si ``.git`` et
l'exécutable ``git`` sont présents : ``git --no-optional-locks status
--porcelain --untracked-files=no``, deux secondes au plus ; sinon ``None``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from _version import __version__

# Variable d'environnement qui fixe le commit (image Docker, CI).
BUILD_COMMIT_ENV = "MOISSON_BUILD_COMMIT"

# Racine du code : le dossier qui contient les modules de premier niveau
# (dépôt en installation éditable, site-packages pour une roue).
CODE_ROOT = Path(__file__).resolve().parents[1]

# Modules empaquetés : doit rester identique à ``py-modules`` et aux
# paquets de ``pyproject.toml`` (vérifié par les tests).
PACKAGED_MODULES = (
    "cli", "main", "mcp_server", "config", "setup_claude_desktop", "auth",
    "remote_auth", "oauth_provider", "_version",
)
PACKAGED_PACKAGES = ("connectors", "mappers", "utils", "engine")

# Délai maximal du calcul de l'état de l'arbre (secondes).
DIRTY_TIMEOUT_S = 2.0

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _valid_sha(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    return value if _SHA_RE.match(value) else None


# === Empreinte du code ===

def code_files(root: Path = CODE_ROOT) -> Iterator[Path]:
    """Fichiers Python empaquetés, dans un ordre stable (chemin relatif)."""
    found = []
    for name in PACKAGED_MODULES:
        path = root / f"{name}.py"
        if path.is_file():
            found.append(path)
    for package in PACKAGED_PACKAGES:
        base = root / package
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" not in path.parts:
                found.append(path)
    yield from sorted(found, key=lambda p: p.relative_to(root).as_posix())


def code_sha256(root: Path = CODE_ROOT) -> Optional[str]:
    """Empreinte SHA-256 des modules empaquetés (chemin relatif et contenu
    de chaque fichier) ; ``None`` si aucun fichier n'est lisible."""
    h = hashlib.sha256()
    count = 0
    for path in code_files(root):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        h.update(path.relative_to(root).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
        count += 1
    return h.hexdigest() if count else None


# === Commit ===

def _git_dir(root: Path) -> Optional[Path]:
    """Dossier git de ``root`` : ``.git`` lui-même, ou la cible d'un fichier
    ``.git`` de worktree (``gitdir: …``)."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            text = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text[len("gitdir:"):].strip())
            if not target.is_absolute():
                target = (root / target).resolve()
            return target if target.is_dir() else None
    return None


def _common_dir(git_dir: Path) -> Path:
    """Dossier commun d'un worktree (références et ``packed-refs``)."""
    try:
        text = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    common = Path(text)
    if not common.is_absolute():
        common = (git_dir / common).resolve()
    return common if common.is_dir() else git_dir


def _resolve_ref(ref: str, dirs) -> Optional[str]:
    for base in dirs:
        try:
            sha = _valid_sha((base / ref).read_text(encoding="utf-8"))
        except OSError:
            sha = None
        if sha:
            return sha
    for base in dirs:
        try:
            lines = (base / "packed-refs").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref:
                sha = _valid_sha(parts[0])
                if sha:
                    return sha
    return None


def git_commit(root: Path = CODE_ROOT) -> Optional[str]:
    """Commit de ``HEAD`` lu directement dans ``.git`` (sans ``git``)."""
    git_dir = _git_dir(root)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return _valid_sha(head)          # HEAD détaché
    ref = head[len("ref:"):].strip()
    dirs = [git_dir]
    common = _common_dir(git_dir)
    if common != git_dir:
        dirs.append(common)
    return _resolve_ref(ref, dirs)


def git_dirty(root: Path = CODE_ROOT,
              timeout: float = DIRTY_TIMEOUT_S) -> Optional[bool]:
    """Vrai si l'arbre suivi diffère du commit ; ``None`` si on ne peut pas
    le savoir (pas de ``.git``, pas de ``git``, délai dépassé)."""
    if _git_dir(root) is None:
        return None
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run(
            [git, "--no-optional-locks", "-C", str(root), "status",
             "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


# === Synthèse ===

@lru_cache(maxsize=1)
def build_info() -> Dict[str, Any]:
    """Identité du code, calculée une fois par processus : ``version``,
    ``commit``, ``commit_source`` (``env``, ``git`` ou ``None``), ``dirty``,
    ``code_sha256``."""
    commit = _valid_sha(os.environ.get(BUILD_COMMIT_ENV))
    source = "env" if commit else None
    dirty: Optional[bool] = None
    if commit is None:
        commit = git_commit()
        if commit:
            source = "git"
            dirty = git_dirty()
    return {
        "version": __version__,
        "commit": commit,
        "commit_source": source,
        "dirty": dirty,
        "code_sha256": code_sha256(),
    }


def describe(info: Optional[Dict[str, Any]] = None) -> str:
    """Ligne lisible : ``0.4.0.dev0 (commit e61c2b9, arbre modifié)``."""
    info = info or build_info()
    parts = []
    if info.get("commit"):
        parts.append(f"commit {info['commit'][:7]}")
    else:
        parts.append("commit inconnu")
    if info.get("dirty") is True:
        parts.append("arbre modifié")
    if info.get("code_sha256"):
        parts.append(f"code {info['code_sha256'][:12]}")
    return f"{info['version']} ({', '.join(parts)})"
