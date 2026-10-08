"""
Écrivain générique des fichiers JSON à clé ``mcpServers``.

Sert Claude Desktop, Gemini CLI, Antigravity (CLI et IDE) ; un autre client
de même forme (Cursor, LM Studio…) n'y ajoute qu'un enregistrement dans le
registre. Un fichier JSON avec commentaires (JSONC) n'est jamais réécrit :
le relire avec ``json`` perdrait les commentaires, setup donne alors un
extrait à coller.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable, Optional

from mcp_clients.common import raise_to
from mcp_clients.file_adapter import FileAdapter
from mcp_clients.snippets import json_snippet

_INDENT = re.compile(r'^([ \t]+)"', re.MULTILINE)


class JsonMcpServersAdapter(FileAdapter):
    """Fichier JSON ``{"mcpServers": {...}}`` ; voir le docstring du module."""

    def __init__(self, client_id: str, label: str,
                 path_resolver: Callable[[], Path], *, aliases: tuple = (),
                 servers_key: str = "mcpServers", timeouts: Optional[dict] = None,
                 process_names: tuple = (), restart_hint: str = "",
                 detect_paths: Callable[[], Iterable[Path]] = lambda: (),
                 detect_commands: tuple = (), path: Optional[Path] = None):
        super().__init__(path)
        self.id = client_id
        self.label = label
        self.aliases = aliases
        self.servers_key = servers_key
        self.running_process_names = process_names
        self.restart_hint = restart_hint
        self.detect_commands = detect_commands
        self._resolver = path_resolver
        self._detect_paths = detect_paths
        self._timeouts = dict(timeouts or {})
        self._indent = 2

    def default_path(self) -> Optional[Path]:
        return self._resolver()

    def detect_paths(self):
        return self._detect_paths()

    def complete(self, entry: dict, previous: dict) -> dict:
        for key, minimum in self._timeouts.items():
            entry[key] = raise_to(previous, key, minimum)
        return entry

    def parse(self, text):
        if text is None or not text.strip():
            return {}
        match = _INDENT.search(text)
        if match:
            ws = match.group(1)
            self._indent = "\t" if "\t" in ws else len(ws)
        doc = json.loads(text)
        if not isinstance(doc, dict):
            raise ValueError("document JSON inattendu")
        return doc

    def dump(self, doc) -> str:
        return json.dumps(doc, indent=self._indent, ensure_ascii=False) + "\n"

    def neutral(self, text):
        if text is None or not text.strip():
            return {}
        return json.loads(text)

    def servers(self, doc, create: bool):
        servers = doc.get(self.servers_key)
        if servers is None:
            if not create:
                return None
            servers = doc[self.servers_key] = {}
        if not isinstance(servers, dict):
            raise ValueError(f"« {self.servers_key} » n'est pas un objet")
        return servers

    def snippet(self, entry: dict) -> str:
        return json_snippet(self.servers_key, entry)
