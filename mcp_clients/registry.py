"""
Registre des clients MCP pris en charge par ``moisson-shs setup``.

Ordre d'affichage : Claude Desktop, Claude Code, Codex, Gemini CLI,
Antigravity (CLI puis IDE), Hermes. Un nouveau client au format JSON
``mcpServers`` (Cursor, LM Studio…) n'est qu'un enregistrement de plus dans
``make_adapters``.
"""

from __future__ import annotations

import argparse
import os
import platform
from pathlib import Path
from typing import Callable, Dict, List, Optional

from mcp_clients.base import ClientAdapter
from mcp_clients.claude_code import ClaudeCodeCliAdapter
from mcp_clients.codex import CodexTomlAdapter
from mcp_clients.hermes import HermesYamlAdapter
from mcp_clients.json_adapter import JsonMcpServersAdapter

# Délai par outil de Gemini CLI, en millisecondes : échéance maximale de
# moisson_search (900 s) plus une marge, comme pour Codex et Hermes.
GEMINI_TIMEOUT_MS = 960000


def claude_desktop_config_path() -> Path:
    """
    ``claude_desktop_config.json`` selon le système :
    - macOS   : ~/Library/Application Support/Claude/
    - Windows : %APPDATA%/Claude/
    - Linux   : $XDG_CONFIG_HOME/Claude/ (défaut ~/.config/Claude/)
    """
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home / ".config"
    return base / "Claude" / "claude_desktop_config.json"


def _gemini_dir() -> Path:
    return Path.home() / ".gemini"


def make_adapters(overrides: Optional[Dict[str, Path]] = None,
                  runner: Optional[Callable] = None,
                  claude_bin_resolver: Optional[Callable[[], Optional[str]]] = None
                  ) -> List[ClientAdapter]:
    """Adaptateurs de tous les clients, chemins éventuellement imposés."""
    o = {k: Path(v) for k, v in (overrides or {}).items()}
    return [
        JsonMcpServersAdapter(
            "claude-desktop", "Claude Desktop", claude_desktop_config_path,
            aliases=("desktop", "claude"), process_names=("Claude",),
            restart_hint="Claude Desktop : quitter complètement (Cmd+Q) et relancer.",
            detect_paths=lambda: (claude_desktop_config_path().parent,),
            path=o.get("claude-desktop")),
        ClaudeCodeCliAdapter(path=o.get("claude-code"), runner=runner,
                             claude_bin_resolver=claude_bin_resolver),
        CodexTomlAdapter(path=o.get("codex")),
        JsonMcpServersAdapter(
            "gemini-cli", "Gemini CLI", lambda: _gemini_dir() / "settings.json",
            aliases=("gemini",), timeouts={"timeout": GEMINI_TIMEOUT_MS},
            restart_hint=("Gemini CLI : relancer ; les serveurs ne se connectent que "
                          "dans un dossier de confiance (`gemini trust`)."),
            detect_commands=("gemini",), path=o.get("gemini-cli")),
        JsonMcpServersAdapter(
            "antigravity", "Antigravity CLI (agy)",
            lambda: _gemini_dir() / "config" / "mcp_config.json",
            aliases=("agy",), restart_hint="Antigravity : relancer agy.",
            detect_commands=("agy",), path=o.get("antigravity")),
        JsonMcpServersAdapter(
            "antigravity-ide", "Antigravity IDE",
            lambda: _gemini_dir() / "antigravity" / "mcp_config.json",
            restart_hint="Antigravity IDE : relancer l'IDE.",
            detect_paths=lambda: (_gemini_dir() / "antigravity",),
            path=o.get("antigravity-ide")),
        HermesYamlAdapter(path=o.get("hermes")),
    ]


def _names() -> Dict[str, str]:
    names = {}
    for adapter in make_adapters():
        names[adapter.id] = adapter.id
        for alias in adapter.aliases:
            names[alias] = adapter.id
    return names


def valid_ids() -> List[str]:
    return [a.id for a in make_adapters()]


def resolve_id(token: str) -> str:
    """Identifiant canonique d'un id ou d'un alias (ArgumentTypeError sinon)."""
    key = token.strip().lower()
    names = _names()
    if key not in names:
        raise argparse.ArgumentTypeError(
            f"client inconnu : « {token.strip()} ». Clients : "
            f"{', '.join(valid_ids())} (ou all).")
    return names[key]


def parse_client_spec(spec: str) -> List[str]:
    """``--client`` : ids ou alias séparés par des virgules, ou ``all``."""
    tokens = [t for t in str(spec or "").split(",") if t.strip()]
    if not tokens:
        raise argparse.ArgumentTypeError("aucun client indiqué")
    if any(t.strip().lower() == "all" for t in tokens):
        return ["all"]
    out: List[str] = []
    for token in tokens:
        cid = resolve_id(token)
        if cid not in out:
            out.append(cid)
    return out


def parse_client_config(spec: str):
    """``--client-config ID=CHEMIN`` -> ``(id, Path)``."""
    cid, sep, path = str(spec or "").partition("=")
    if not sep or not path.strip():
        raise argparse.ArgumentTypeError(
            f"forme attendue ID=CHEMIN, reçu « {spec} »")
    return resolve_id(cid), Path(os.path.expanduser(path.strip()))
