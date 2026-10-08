"""
Patron commun aux clients dont setup écrit lui-même le fichier (JSON, TOML,
YAML).

Déroulé d'une installation :
1. lecture du fichier et de son empreinte ;
2. analyse ; en cas d'échec (bibliothèque absente, JSON avec commentaires,
   structure inattendue, mise en forme non reproductible), extrait à coller,
   fichier intact ;
3. insertion de l'entrée ;
4. garde sémantique : relu par un analyseur de référence, le document privé
   de notre entrée doit être identique avant et après, et notre entrée
   valoir exactement l'entrée voulue ; sinon extrait à coller ;
5. sauvegarde horodatée, écriture atomique (refusée si le fichier a changé
   entre-temps), relecture de contrôle, rétention des sauvegardes.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, List, Optional

from mcp_clients import fileops
from mcp_clients.base import ClientAdapter, ConfigUnreadable, Result
from mcp_clients.common import SERVER_KEY, collect_secret_values, has_secret_values


class GuardError(ValueError):
    """La modification toucherait autre chose que notre entrée."""


class FileAdapter(ClientAdapter):
    """Adaptateur d'un fichier de configuration ; voir le docstring du module."""

    servers_key = "mcpServers"

    # --- à fournir par chaque format ---

    def parse(self, text: Optional[str]) -> Any:
        """Document modifiable (texte None ou vide : document neuf)."""
        raise NotImplementedError

    def dump(self, doc: Any) -> str:
        raise NotImplementedError

    def neutral(self, text: Optional[str]) -> dict:
        """Lecture de référence en types Python simples ({} si vide)."""
        raise NotImplementedError

    def servers(self, doc: Any, create: bool) -> Any:
        """Table des serveurs du document (None si absente et non créée)."""
        raise NotImplementedError

    def set_entry(self, servers: Any, entry: dict) -> None:
        servers[SERVER_KEY] = copy.deepcopy(entry)

    def remove_entry(self, servers: Any) -> None:
        del servers[SERVER_KEY]

    # --- lecture ---

    def _read(self):
        path = self.config_path()
        try:
            return fileops.read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigUnreadable(f"{type(exc).__name__} à la lecture") from exc

    def _neutral_or_raise(self, text: Optional[str]) -> dict:
        try:
            doc = self.neutral(text)
        except Exception as exc:  # noqa: BLE001 - tout format illisible
            raise ConfigUnreadable(f"format illisible ({type(exc).__name__})") from exc
        if not isinstance(doc, dict):
            raise ConfigUnreadable("document inattendu (pas une table)")
        return doc

    def _servers_of(self, neutral: dict) -> dict:
        servers = neutral.get(self.servers_key)
        return servers if isinstance(servers, dict) else {}

    def read_entry(self) -> Optional[dict]:
        text, _fp = self._read()
        if text is None:
            return None
        entry = self._servers_of(self._neutral_or_raise(text)).get(SERVER_KEY)
        return entry if isinstance(entry, dict) else None

    def other_server_names(self) -> List[str]:
        try:
            text, _fp = self._read()
            if text is None:
                return []
            servers = self._servers_of(self._neutral_or_raise(text))
        except ConfigUnreadable:
            return []
        return [name for name in servers if name != SERVER_KEY]

    def secret_values(self) -> List[str]:
        try:
            text, _fp = self._read()
            return collect_secret_values(self._neutral_or_raise(text)) if text else []
        except ConfigUnreadable:
            return []

    # --- garde sémantique ---

    def _without_ours(self, neutral: dict) -> dict:
        doc = copy.deepcopy(neutral)
        servers = doc.get(self.servers_key)
        if isinstance(servers, dict):
            servers.pop(SERVER_KEY, None)
            if not servers:
                doc.pop(self.servers_key, None)
        return doc

    def _guard(self, before: Optional[str], after: str,
               expected: Optional[dict]) -> List[str]:
        old = self.neutral(before) if before and before.strip() else {}
        new = self.neutral(after)
        if self._without_ours(old) != self._without_ours(new):
            raise GuardError("le reste du fichier serait modifié")
        got = self._servers_of(new).get(SERVER_KEY)
        if got != expected:
            raise GuardError("l'entrée relue diffère de l'entrée voulue")
        return [name for name in self._servers_of(new) if name != SERVER_KEY]

    # --- écriture ---

    def _snippet_result(self, entry: dict, why: str) -> Result:
        return Result(self.id, "snippet", ok=True, path=self.config_path(),
                      detail=why, snippet=self.snippet(entry))

    def _write(self, path: Path, text: str, fp: Optional[str], private: bool,
               dry_run: bool):
        if dry_run:
            return None, False
        backup = fileops.backup_file(path)
        secured = fileops.atomic_write(path, text, expected_fp=fp, private=private)
        fileops.prune_backups(path)
        return backup, secured

    def install(self, entry: dict, dry_run: bool = False) -> Result:
        path = self.config_path()
        ok, why = self.writable()
        if not ok:
            return self._snippet_result(entry, why)
        try:
            text, fp = self._read()
        except ConfigUnreadable as exc:
            return Result(self.id, "error", ok=False, path=path, detail=str(exc))
        try:
            doc = self.parse(text)
            servers = self.servers(doc, create=True)
            existed = SERVER_KEY in servers
            self.set_entry(servers, entry)
            new_text = self.dump(doc)
            others = self._guard(text, new_text, entry)
        except Exception as exc:  # noqa: BLE001 - repli : extrait à coller
            reason = str(exc) if isinstance(exc, GuardError) else type(exc).__name__
            return self._snippet_result(entry, f"écriture automatique impossible ({reason})")
        if dry_run:
            return Result(self.id, "dry-run", path=path, others=others)
        try:
            backup, secured = self._write(path, new_text, fp,
                                          has_secret_values(entry), dry_run)
        except fileops.ConcurrentModification:
            return Result(self.id, "error", ok=False, path=path,
                          detail="fichier modifié pendant l'écriture (client ouvert ?) : "
                                 "rien n'a été écrit, relancer")
        except OSError as exc:
            return Result(self.id, "error", ok=False, path=path,
                          detail=f"écriture impossible ({type(exc).__name__})")
        try:
            check = self.read_entry()
        except ConfigUnreadable:
            check = None
        if check != entry:
            return Result(self.id, "error", ok=False, path=path, backup=backup,
                          detail="relecture de contrôle différente : restaurer la sauvegarde")
        return Result(self.id, "updated" if existed else "added", path=path,
                      backup=backup, others=others, secured=secured)

    def uninstall(self, dry_run: bool = False) -> Result:
        path = self.config_path()
        try:
            text, fp = self._read()
        except ConfigUnreadable as exc:
            return Result(self.id, "error", ok=False, path=path, detail=str(exc))
        if text is None:
            return Result(self.id, "absent", path=path)
        ok, why = self.writable()
        try:
            if not ok:
                raise GuardError(why)
            doc = self.parse(text)
            servers = self.servers(doc, create=False)
            if servers is None or SERVER_KEY not in servers:
                return Result(self.id, "absent", path=path)
            self.remove_entry(servers)
            new_text = self.dump(doc)
            others = self._guard(text, new_text, None)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) if isinstance(exc, GuardError) else type(exc).__name__
            return Result(self.id, "error", ok=False, path=path,
                          detail=f"retrait automatique impossible ({reason}) : retirer "
                                 f"l'entrée {SERVER_KEY} à la main")
        if dry_run:
            return Result(self.id, "dry-run", path=path, others=others)
        try:
            backup, _secured = self._write(path, new_text, fp, False, dry_run)
        except fileops.ConcurrentModification:
            return Result(self.id, "error", ok=False, path=path,
                          detail="fichier modifié pendant l'écriture : relancer")
        return Result(self.id, "removed", path=path, backup=backup, others=others)
