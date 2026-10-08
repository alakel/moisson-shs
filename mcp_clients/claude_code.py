"""
Claude Code : portée utilisateur, par la CLI ``claude``.

``~/.claude.json`` est lu seulement : Claude Code le réécrit en continu, la
déclaration passe donc par ``claude mcp remove`` puis ``claude mcp add-json
moisson-shs <json> --scope user`` ; si l'ajout échoue, l'entrée précédente
est restaurée. Jamais en portée projet (``.mcp.json``, versionné : les clés
y seraient en clair). Limite : la CLI reçoit l'entrée en argument, les clés
sont visibles dans la liste des processus le temps de l'appel.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from mcp_clients.base import ClientAdapter, ConfigUnreadable, Result
from mcp_clients.common import SERVER_KEY, collect_secret_values, scrub
from mcp_clients.snippets import json_snippet, masked_entry


def claude_code_config_path() -> Path:
    """Fichier d'état de Claude Code (serveurs MCP de portée utilisateur)."""
    return Path.home() / ".claude.json"


def find_claude_cli() -> Optional[str]:
    """Chemin de la CLI ``claude``, ou None."""
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.is_file() else None


class ClaudeCodeCliAdapter(ClientAdapter):
    """Voir le docstring du module."""

    id = "claude-code"
    label = "Claude Code"
    aliases = ("code",)
    detect_commands = ("claude",)
    restart_hint = "Claude Code : relancer les sessions ouvertes (vérifier avec /mcp)."

    def __init__(self, path: Optional[Path] = None, runner: Optional[Callable] = None,
                 claude_bin_resolver: Optional[Callable[[], Optional[str]]] = None):
        super().__init__(path)
        self._runner = runner
        self._resolver = claude_bin_resolver or find_claude_cli

    def default_path(self) -> Optional[Path]:
        return claude_code_config_path()

    def writable(self):
        if self._resolver():
            return True, ""
        return False, "CLI `claude` introuvable"

    def detect(self):
        det = super().detect()
        if not det.found and self._resolver():
            det.found, det.how = True, "CLI"
        return det

    def _servers(self) -> dict:
        path = self.config_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigUnreadable(f"{path} illisible : {type(exc).__name__}") from exc
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        return servers if isinstance(servers, dict) else {}

    def read_entry(self) -> Optional[dict]:
        entry = self._servers().get(SERVER_KEY)
        return entry if isinstance(entry, dict) else None

    def other_server_names(self) -> List[str]:
        try:
            return [name for name in self._servers() if name != SERVER_KEY]
        except ConfigUnreadable:
            return []

    def secret_values(self) -> List[str]:
        try:
            return collect_secret_values({"mcpServers": self._servers()})
        except ConfigUnreadable:
            return []

    def complete(self, entry: dict, previous: dict) -> dict:
        return {"type": "stdio", **{k: v for k, v in entry.items() if k != "type"}}

    def _run(self, cmd: list):
        return (self._runner or subprocess.run)(cmd, capture_output=True, text=True,
                                                timeout=60)

    def install(self, entry: dict, dry_run: bool = False) -> Result:
        path = self.config_path()
        try:
            previous = self.read_entry()
        except ConfigUnreadable as exc:
            return Result(self.id, "error", ok=False, path=path, detail=str(exc))
        others = self.other_server_names()
        if dry_run:
            return Result(self.id, "dry-run", path=path, others=others)
        claude = self._resolver()
        if not claude:
            return Result(self.id, "error", ok=False, path=path,
                          detail="CLI `claude` introuvable : installer Claude Code, ou "
                                 "viser un autre client",
                          snippet=self.snippet(entry))
        known = (list((entry.get("env") or {}).values())
                 + list(((previous or {}).get("env") or {}).values()))
        scope = ["--scope", "user"]
        if previous is not None:
            done = self._run([claude, "mcp", "remove", SERVER_KEY] + scope)
            if done.returncode != 0:
                return Result(self.id, "error", ok=False, path=path,
                              detail=f"échec du retrait de l'entrée existante (code "
                                     f"{done.returncode}) : {scrub(done.stderr, known)}")
        done = self._run([claude, "mcp", "add-json", SERVER_KEY,
                          json.dumps(entry, ensure_ascii=False)] + scope)
        if done.returncode != 0:
            detail = (f"échec de l'ajout (code {done.returncode}) : "
                      f"{scrub(done.stderr, known)}")
            if previous is not None:
                back = self._run([claude, "mcp", "add-json", SERVER_KEY,
                                  json.dumps(previous, ensure_ascii=False)] + scope)
                detail += ("\n   Entrée précédente restaurée." if back.returncode == 0
                           else "\n   ⚠️ Restauration de l'entrée précédente en échec : "
                                "relancer la commande.")
            return Result(self.id, "error", ok=False, path=path, detail=detail)
        return Result(self.id, "updated" if previous else "added", path=path,
                      others=others)

    def uninstall(self, dry_run: bool = False) -> Result:
        path = self.config_path()
        try:
            entry = self.read_entry()
        except ConfigUnreadable as exc:
            return Result(self.id, "error", ok=False, path=path, detail=str(exc))
        if entry is None:
            return Result(self.id, "absent", path=path)
        if dry_run:
            return Result(self.id, "dry-run", path=path, others=self.other_server_names())
        claude = self._resolver()
        if not claude:
            return Result(self.id, "error", ok=False, path=path,
                          detail="CLI `claude` introuvable")
        done = self._run([claude, "mcp", "remove", SERVER_KEY, "--scope", "user"])
        if done.returncode != 0:
            known = list((entry.get("env") or {}).values())
            return Result(self.id, "error", ok=False, path=path,
                          detail=f"échec du retrait (code {done.returncode}) : "
                                 f"{scrub(done.stderr, known)}")
        return Result(self.id, "removed", path=path)

    def snippet(self, entry: dict) -> str:
        shown = json.dumps(masked_entry(entry), ensure_ascii=False)
        return (json_snippet("mcpServers", entry) + "\n\nou :\n"
                f"claude mcp add-json {SERVER_KEY} '{shown}' --scope user")
