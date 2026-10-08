"""
Interface commune des adaptateurs de clients MCP.

Un adaptateur sait où le client garde sa configuration, s'il est installé
(``detect``), lire l'entrée ``moisson-shs`` (``read_entry``), composer
l'entrée à déclarer (``build_entry`` : entrée précédente conservée, champs
propres au client ajoutés), l'installer, la retirer et en donner un extrait
à coller. Il ne produit jamais de texte contenant une valeur secrète :
``Result.detail`` et ``Result.snippet`` sont masqués.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional


class ConfigUnreadable(RuntimeError):
    """Fichier de configuration illisible (droits, format, bibliothèque)."""


@dataclass
class Detection:
    """Présence d'un client sur la machine."""

    found: bool
    how: str = ""                      # « fichier », « dossier », « CLI »
    path: Optional[Path] = None
    declared: bool = False             # entrée moisson-shs présente
    writable: bool = True              # setup peut écrire (sinon extrait)
    reason: str = ""                   # pourquoi non inscriptible / illisible
    env: Optional[dict] = None         # bloc env déclaré (jamais affiché)


@dataclass
class Result:
    """Issue d'une installation ou d'un retrait (sans aucune valeur secrète)."""

    client_id: str
    action: str        # added, updated, removed, dry-run, snippet, absent,
    #                    skipped, error
    ok: bool = True
    path: Optional[Path] = None
    backup: Optional[Path] = None
    detail: str = ""
    snippet: str = ""
    others: List[str] = field(default_factory=list)
    secured: bool = False              # droits restreints à 0600


class ClientAdapter:
    """Classe de base ; voir le docstring du module."""

    id: str = ""
    label: str = ""
    aliases: tuple = ()
    running_process_names: tuple = ()
    restart_hint: str = ""
    detect_commands: tuple = ()

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else None

    # --- emplacement et détection ---

    def default_path(self) -> Optional[Path]:
        raise NotImplementedError

    def config_path(self) -> Optional[Path]:
        return self._path or self.default_path()

    def detect_paths(self) -> Iterable[Path]:
        """Chemins dont la présence signale le client installé."""
        return ()

    def writable(self):
        """``(bool, motif)`` : setup peut-il écrire chez ce client ?"""
        return True, ""

    def detect(self) -> Detection:
        path = self.config_path()
        how = ""
        if path is not None and path.is_file():
            how = "fichier"
        elif any(Path(p).exists() for p in self.detect_paths()):
            how = "dossier"
        elif any(shutil.which(cmd) for cmd in self.detect_commands):
            how = "CLI"
        declared, env, reason = False, None, ""
        try:
            entry = self.read_entry()
            declared = entry is not None
            env = entry.get("env") if isinstance(entry, dict) else None
        except ConfigUnreadable as exc:
            reason = str(exc)
        writable, why = self.writable()
        return Detection(found=bool(how), how=how, path=path, declared=declared,
                         writable=writable, reason=why or reason,
                         env=env if isinstance(env, dict) else None)

    # --- lecture ---

    def read_entry(self) -> Optional[dict]:
        raise NotImplementedError

    def other_server_names(self) -> List[str]:
        return []

    def secret_values(self) -> List[str]:
        """Valeurs secrètes présentes dans la configuration (pour masquer)."""
        return []

    # --- écriture ---

    def build_entry(self, base: dict, previous: Optional[dict]) -> dict:
        """Entrée à déclarer : champs inconnus de l'entrée précédente
        conservés, champs de ``base`` (command, args, env) imposés, champs
        propres au client ajoutés (``complete``)."""
        entry = dict(previous) if isinstance(previous, dict) else {}
        entry.update(base)
        return self.complete(entry, previous if isinstance(previous, dict) else {})

    def complete(self, entry: dict, previous: dict) -> dict:
        return entry

    def install(self, entry: dict, dry_run: bool = False) -> Result:
        raise NotImplementedError

    def uninstall(self, dry_run: bool = False) -> Result:
        raise NotImplementedError

    def snippet(self, entry: dict) -> str:
        raise NotImplementedError
