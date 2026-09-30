"""
Confinement des chemins de fichiers (anti path-traversal) — mode HTTP distant.

À distance, un client peut passer des chemins arbitraires (`corpus_path`,
`output_path`, `corpus_paths`, `add_to_corpus`…). Sans garde, un
`../../etc/passwd`, un chemin absolu hors zone ou un symlink pointant dehors
permettrait de lire/écrire n'importe où. Ce module **confine** tout chemin
utilisateur sous `OUTPUT_DIR` (la racine de la sandbox).

La garde est **HTTP-only** : en stdio (process local de confiance), les chemins
restent libres (cf. `mcp_server._safe_path`). Ici on fournit la primitive ;
c'est l'appelant qui décide de l'activer selon le transport.

Les **fichiers de service** de `OUTPUT_DIR` (`.oauth/oauth_store.json`,
`.audit/*.jsonl`, `.index.json`…) sont hors zone eux aussi : `OUTPUT_DIR` est
prévisible en déploiement (`/data/output`), et un chemin absolu vers le magasin
OAuth permettait à tout porteur de jeton de l'écraser par l'`output_path` de
`corpus_to_csl` ou `corpus_merge` (serveur qui ne redémarre plus, journal
d'audit effacé) ou de le lire comme corpus d'entrée. Cette primitive ne sert
qu'aux chemins fournis par le client ; le serveur écrit ses propres fichiers
de service directement, sans passer par elle.
"""

from __future__ import annotations

import re
from pathlib import Path

from utils.output_files import is_service_path


class PathSecurityError(Exception):
    """Chemin refusé : hors de la sandbox `OUTPUT_DIR` (ou introuvable)."""


# Charset autorisé pour les suffixes de sortie (corpus_filter `output_suffix`).
# Interdit tout séparateur de chemin et tout point → pas de traversal possible.
_SAFE_SUFFIX_RE = re.compile(r"[A-Za-z0-9_-]+")


def resolve_in_output_dir(
    user_path: str | Path,
    output_dir: str | Path,
    *,
    must_exist: bool = False,
) -> Path:
    """
    Résout `user_path` en le **confinant** strictement sous `output_dir`.

    Stratégie :
    - Un chemin **relatif** ou un **nom nu** est rebasé sous `output_dir` en ne
      gardant que son `name` (basename) — l'espace `OUTPUT_DIR` est plat.
    - Un chemin **absolu** est gardé tel quel puis vérifié.
    - Dans tous les cas, on **résout les symlinks** (`resolve()`) PUIS on exige
      que le chemin résolu soit relatif à `output_dir` résolu — un symlink
      pointant dehors est donc rejeté.
    - Le chemin résolu ne doit pas être un **fichier de service** : aucun de
      ses composants relatifs à `output_dir` ne commence par un point
      (`utils.output_files.is_service_path`). Un symlink interne vers
      `.oauth/` est donc rejeté aussi. Le refus précède le test d'existence
      (pas d'oracle sur la présence d'un fichier de service).

    Args:
        user_path: Chemin fourni par le client (relatif, nu ou absolu).
        output_dir: Racine de la sandbox (`OUTPUT_DIR`).
        must_exist: Si True, exige que la cible existe (rejet sinon).

    Returns:
        Le `Path` résolu, garanti sous `output_dir`.

    Raises:
        PathSecurityError: chemin hors sandbox, ou introuvable si `must_exist`.
    """
    base = Path(output_dir).resolve()
    p = Path(user_path)

    # Rejet EXPLICITE de toute composante de traversal (« .. ») — relatif comme
    # absolu. Plus strict que le simple flattening : un `../../etc/passwd` est
    # refusé, pas silencieusement ramené à un basename.
    if ".." in p.parts:
        raise PathSecurityError(f"Traversal interdit (« .. ») : {user_path}")

    # Noms nus / relatifs → basename sous la racine ; absolus → tels quels.
    candidate = (base / p.name) if not p.is_absolute() else p
    candidate = candidate.resolve()  # suit les symlinks des segments existants

    if not candidate.is_relative_to(base):  # Python 3.9+
        raise PathSecurityError(f"Chemin hors de la zone autorisée : {user_path}")
    # Fichiers de service (`.oauth/`, `.audit/`, `.index.json`…) : ni lus ni
    # écrits par les outils. Seuls les composants relatifs à la racine
    # comptent : un OUTPUT_DIR situé sous un dossier caché reste utilisable.
    if is_service_path(candidate, base):
        raise PathSecurityError(f"Fichier de service inaccessible : {user_path}")
    if must_exist and not candidate.exists():
        raise PathSecurityError(f"Fichier introuvable dans la zone : {user_path}")
    return candidate


def validate_suffix(suffix: str) -> str:
    """
    Valide un suffixe de nom de fichier (charset strict, anti-traversal).

    Args:
        suffix: Suffixe utilisateur (ex. ``_filtered``).

    Returns:
        Le suffixe inchangé s'il est valide.

    Raises:
        PathSecurityError: si le suffixe contient un caractère interdit
            (séparateur de chemin, point, espace…).
    """
    if not suffix or not _SAFE_SUFFIX_RE.fullmatch(suffix):
        raise PathSecurityError(
            f"Suffixe invalide (attendu [A-Za-z0-9_-]+) : {suffix!r}"
        )
    return suffix
