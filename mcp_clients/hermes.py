"""
Hermes Agent (Nous Research) : ``config.yaml`` du dossier Hermes
(``$HERMES_HOME``, sinon ``%LOCALAPPDATA%\\hermes`` sous Windows, sinon
``~/.hermes``), clé ``mcp_servers``.

Écriture par ``ruamel.yaml`` en aller-retour (commentaires, guillemets et
indentation conservés). Si le fichier ne se relit pas à l'identique avant
toute modification, setup refuse d'écrire et donne un extrait à coller.
``hermes mcp add`` n'est pas utilisé : il est interactif (il sonde le
serveur et pose des questions, sans option pour s'en passer).

Hermes ne transmet aux serveurs qu'un environnement réduit plus le bloc
``env`` : toutes les clés y sont déclarées. Délais relevés, jamais abaissés.
"""

from __future__ import annotations

import io
import os
import platform
from pathlib import Path
from typing import Optional

from mcp_clients.common import raise_to
from mcp_clients.file_adapter import FileAdapter
from mcp_clients.snippets import yaml_snippet

CONNECT_TIMEOUT = 120
TOOL_TIMEOUT = 960


class HermesYamlAdapter(FileAdapter):
    """Voir le docstring du module."""

    id = "hermes"
    label = "Hermes Agent (Nous Research)"
    aliases = ("hermes-agent",)
    servers_key = "mcp_servers"
    running_process_names = ("Hermes",)
    detect_commands = ("hermes",)
    restart_hint = ("Hermes : /reload-mcp dans la session ; la passerelle relit "
                    "le fichier en une minute environ.")

    def __init__(self, path: Optional[Path] = None):
        super().__init__(path)
        self._yaml = None

    def default_path(self) -> Optional[Path]:
        home = os.environ.get("HERMES_HOME")
        if home:
            return Path(home) / "config.yaml"
        if platform.system() == "Windows" and os.environ.get("LOCALAPPDATA"):
            return Path(os.environ["LOCALAPPDATA"]) / "hermes" / "config.yaml"
        return Path.home() / ".hermes" / "config.yaml"

    def detect_paths(self):
        return (self.config_path().parent,)

    def writable(self):
        try:
            import ruamel.yaml  # noqa: F401
        except ImportError:
            return False, "bibliothèque ruamel.yaml absente (pip install ruamel.yaml)"
        return True, ""

    def complete(self, entry: dict, previous: dict) -> dict:
        entry["connect_timeout"] = raise_to(previous, "connect_timeout", CONNECT_TIMEOUT)
        entry["timeout"] = raise_to(previous, "timeout", TOOL_TIMEOUT)
        entry["enabled"] = True
        return entry

    def _make_yaml(self, mapping: int = 2, sequence: int = 2, offset: int = 0):
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096
        yaml.indent(mapping=mapping, sequence=sequence, offset=offset)
        return yaml

    def parse(self, text):
        from ruamel.yaml.comments import CommentedMap
        from ruamel.yaml.util import load_yaml_guess_indent
        if text is None or not text.strip():
            self._yaml = self._make_yaml()
            return CommentedMap()
        _data, ind, bsi = load_yaml_guess_indent(text)
        ind = ind or 2
        # Réglages candidats d'indentation ; le premier qui redonne le texte à
        # l'identique est retenu (recette de ruamel en deuxième).
        candidates = [(ind, ind, 0)]
        if bsi is not None:
            candidates += [(bsi, ind, bsi), (ind, ind + bsi, bsi), (2, ind, bsi)]
        for mapping, sequence, offset in candidates:
            self._yaml = self._make_yaml(mapping, sequence, offset)
            doc = self._yaml.load(text)
            if doc is None:
                doc = CommentedMap()
            if not isinstance(doc, dict):
                raise ValueError("document YAML inattendu")
            if self.dump(doc) == text:
                return doc
        raise ValueError("mise en forme non reproductible à l'identique")

    def dump(self, doc) -> str:
        out = io.StringIO()
        (self._yaml or self._make_yaml()).dump(doc, out)
        return out.getvalue()

    def neutral(self, text):
        if text is None or not text.strip():
            return {}
        from ruamel.yaml import YAML
        return YAML(typ="safe", pure=True).load(text) or {}

    def servers(self, doc, create: bool):
        from ruamel.yaml.comments import CommentedMap
        servers = doc.get(self.servers_key)
        if servers is None:
            if not create:
                return None
            servers = doc[self.servers_key] = CommentedMap()
        if not isinstance(servers, dict):
            raise ValueError("« mcp_servers » n'est pas une table")
        return servers

    def snippet(self, entry: dict) -> str:
        return yaml_snippet(entry)
