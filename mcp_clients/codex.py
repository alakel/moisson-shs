"""
OpenAI Codex : ``~/.codex/config.toml`` (ou ``$CODEX_HOME/config.toml``),
fichier commun à l'application ChatGPT (côté Codex), au CLI et à l'IDE.

Écriture par ``tomlkit``, qui conserve commentaires et mise en forme ; la
sous-table ``env`` est mise à jour en place. Lecture de référence par
``tomllib`` (``tomli`` ou ``tomlkit`` sous Python 3.10), pour que
``--status`` marche même sans ``tomlkit``.

Codex ne transmet au serveur qu'une liste blanche de variables plus le bloc
``env`` : toutes les clés y sont donc déclarées. Ses délais par défaut
(10 s au démarrage, 60 s par outil) sont trop courts pour un venv froid sur
Google Drive et pour une moisson : ils sont relevés, jamais abaissés.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from mcp_clients.common import SERVER_KEY, raise_to
from mcp_clients.file_adapter import FileAdapter
from mcp_clients.snippets import toml_snippet

STARTUP_TIMEOUT_SEC = 60
# Échéance maximale de moisson_search (900 s) plus une marge : le serveur
# borne lui-même la durée, le client ne doit jamais couper avant lui.
TOOL_TIMEOUT_SEC = 960


def _toml_reader():
    try:
        import tomllib
        return tomllib.loads
    except ImportError:  # pragma: no cover - Python 3.10
        try:
            import tomli
            return tomli.loads
        except ImportError:
            import tomlkit
            return lambda text: tomlkit.parse(text).unwrap()


class CodexTomlAdapter(FileAdapter):
    """Voir le docstring du module."""

    id = "codex"
    label = "OpenAI Codex (application ChatGPT, CLI, IDE)"
    aliases = ("chatgpt",)
    servers_key = "mcp_servers"
    running_process_names = ("Codex", "ChatGPT")
    detect_commands = ("codex",)
    restart_hint = ("Codex : relancer l'application ChatGPT/Codex ou les sessions "
                    "CLI (vérifier avec /mcp).")

    def default_path(self) -> Optional[Path]:
        home = os.environ.get("CODEX_HOME")
        base = Path(home) if home else Path.home() / ".codex"
        return base / "config.toml"

    def detect_paths(self):
        return (self.config_path().parent,)

    def writable(self):
        try:
            import tomlkit  # noqa: F401
        except ImportError:
            return False, "bibliothèque tomlkit absente (pip install tomlkit)"
        return True, ""

    def complete(self, entry: dict, previous: dict) -> dict:
        # Ancienne clé en millisecondes : laissée telle quelle, sans doublon.
        if "startup_timeout_ms" not in previous:
            entry["startup_timeout_sec"] = raise_to(previous, "startup_timeout_sec",
                                                    STARTUP_TIMEOUT_SEC)
        entry["tool_timeout_sec"] = raise_to(previous, "tool_timeout_sec",
                                             TOOL_TIMEOUT_SEC)
        entry["enabled"] = True
        return entry

    def parse(self, text):
        import tomlkit
        return tomlkit.parse(text or "")

    def dump(self, doc) -> str:
        import tomlkit
        return tomlkit.dumps(doc)

    def neutral(self, text):
        if text is None or not text.strip():
            return {}
        return _toml_reader()(text)

    def servers(self, doc, create: bool):
        import tomlkit
        from tomlkit.items import InlineTable, Table
        if "mcp_servers" not in doc:
            if not create:
                return None
            doc["mcp_servers"] = tomlkit.table(is_super_table=True)
        servers = doc["mcp_servers"]
        if not isinstance(servers, (Table, InlineTable)):
            raise ValueError("« mcp_servers » n'est pas une table")
        return servers

    def _env_table(self, env: dict):
        import tomlkit
        table = tomlkit.table()
        for key, value in env.items():
            table[key] = value
        table.add(tomlkit.nl())
        return table

    def set_entry(self, servers, entry: dict) -> None:
        import tomlkit
        from tomlkit.items import InlineTable, Table
        current = servers.get(SERVER_KEY) if SERVER_KEY in servers else None
        if isinstance(current, (Table, InlineTable)):
            for key in [k for k in current.keys() if k not in entry]:
                del current[key]
            for key, value in entry.items():
                if key == "env" and isinstance(value, dict):
                    env = current.get("env")
                    if isinstance(env, (Table, InlineTable)):
                        for name in [k for k in env.keys() if k not in value]:
                            del env[name]
                        for name, val in value.items():
                            env[name] = val
                    else:
                        current["env"] = self._env_table(value)
                else:
                    current[key] = value
            return
        table = tomlkit.table()
        for key, value in entry.items():
            if key != "env":
                table[key] = value
        if isinstance(entry.get("env"), dict):
            table["env"] = self._env_table(entry["env"])
        servers[SERVER_KEY] = table

    def snippet(self, entry: dict) -> str:
        return toml_snippet(entry)
