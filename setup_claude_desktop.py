"""
Configuration du serveur MCP moisson-shs dans les clients MCP (setup
universel) : Claude Desktop, Claude Code, OpenAI Codex (application
ChatGPT, CLI, IDE), Gemini CLI, Antigravity (CLI et IDE), Hermes Agent.

Usage :
    moisson-shs setup                        # menu des clients détectés (terminal)
    moisson-shs setup --client codex,gemini  # clients nommés (ids ou alias)
    moisson-shs setup --client all           # tous les clients détectés
    moisson-shs setup --status               # état de tous les clients
    moisson-shs setup --uninstall
    moisson-shs setup --dry-run

Sans ``--client`` : en terminal, menu des clients détectés (déjà cochés) ;
hors terminal, Claude Desktop seul (comportement historique).

Pour chaque client, un adaptateur (paquet ``mcp_clients``) sait où est sa
configuration et comment l'écrire : fichier JSON ``mcpServers`` (Claude
Desktop, Gemini, Antigravity), CLI ``claude`` (Claude Code), TOML (Codex,
``tomlkit``), YAML (Hermes, ``ruamel.yaml``). Chaque écriture est précédée
d'une sauvegarde horodatée, faite de façon atomique, et contrôlée : si le
reste du fichier devait changer, setup donne un extrait à coller au lieu
d'écrire. Aucune valeur secrète d'un autre serveur n'est jamais affichée.

Clés d'API (sprint « clés ») : la déclaration du client porte toujours les
quatre clés, renseignées ou vides ; elles sont saisies une seule fois et
partagées par les clients ; une clé vide chez un client est complétée par
la valeur commune aux autres clients (si elle est la même partout). Le
`.env` du projet reste la réserve du serveur (``utils.secrets``) et n'est
jamais copié chez un client.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from mcp_clients import menu, procs, registry
from mcp_clients.base import ConfigUnreadable, Result
from mcp_clients.claude_code import claude_code_config_path as get_claude_code_config_path
from mcp_clients.claude_code import find_claude_cli
from mcp_clients.common import (
    API_KEY_ENV_VARS, SERVER_KEY, api_keys_filled, complete_api_key_env,
    merge_env, pool_api_keys, resolve_api_keys,
)
from mcp_clients.common import redact_env as _redact_env
from mcp_clients.common import scrub as _scrub
from mcp_clients.fileops import is_world_readable

__all__ = [
    "SERVER_KEY", "API_KEY_ENV_VARS", "complete_api_key_env", "find_claude_cli",
    "get_claude_code_config_path", "_redact_env", "_scrub",
]

# Exécutant des commandes des clients (CLI claude) ; None : subprocess.run.
# Remplaçable par les tests.
_CLI_RUNNER: Optional[Callable] = None


# === Détection des chemins ===

def get_claude_config_path() -> Path:
    """
    Chemin du `claude_desktop_config.json` selon l'OS :
    - macOS  : ~/Library/Application Support/Claude/claude_desktop_config.json
    - Windows: %APPDATA%/Claude/claude_desktop_config.json
    - Linux  : ~/.config/Claude/claude_desktop_config.json
    """
    return registry.claude_desktop_config_path()


def get_python_executable() -> str:
    """
    Renvoie le chemin absolu de l'interpréteur Python à utiliser.

    IMPORTANT : on conserve `sys.executable` tel quel SANS résoudre les
    symlinks. Quand un venv est actif, `sys.executable` pointe vers
    `<venv>/bin/python3`, qui est un symlink vers le Python système.
    Résoudre ce symlink (`.resolve()`) ferait perdre le contexte venv :
    le Python résolu utiliserait son `site-packages` global et non
    celui du venv où les dépendances (`requests`, `sickle`, `mcp`...)
    sont installées.

    On utilise donc `Path(exe).absolute()` qui rend le chemin absolu
    SANS résoudre les symlinks, préservant ainsi le venv.

    Returns:
        Chemin absolu vers le binaire python (venv préservé)
    """
    exe = sys.executable
    try:
        return str(Path(exe).absolute())
    except OSError:
        return exe


def get_mcp_server_path() -> Path:
    """
    Localise le fichier `mcp_server.py` du moissonneur.

    Cherche d'abord à côté de ce fichier (cas développement / source),
    sinon dans le package installé.

    Returns:
        Chemin absolu de mcp_server.py
    """
    # Cas 1 : exécution depuis la source — mcp_server.py est à côté
    here = Path(__file__).resolve().parent
    candidate = here / "mcp_server.py"
    if candidate.exists():
        return candidate

    # Cas 2 : installation pip — chercher dans le package
    try:
        import mcp_server  # type: ignore
        return Path(mcp_server.__file__).resolve()
    except ImportError:
        pass

    raise FileNotFoundError(
        "Impossible de localiser mcp_server.py. "
        "Vérifier que moisson-shs est correctement installé."
    )


# === Manipulation de la config ===

def load_existing_config(path: Path) -> dict:
    """Charge la config existante ou renvoie un dict vide si absente."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"claude_desktop_config.json présent mais illisible : {e}\n"
            f"Sauvegardez le fichier et corrigez-le manuellement avant "
            f"de relancer 'moisson-shs setup'."
        ) from e


def build_server_entry(
    python_exe: str,
    server_path: Path,
    output_dir: Optional[Path] = None,
    cache_enabled: bool = False,
    extra_env: Optional[dict] = None,
) -> dict:
    """
    Construit la section JSON pour le serveur moisson-shs.

    Args:
        python_exe: Chemin absolu vers l'interpréteur Python (avec mcp installé)
        server_path: Chemin absolu vers mcp_server.py
        output_dir: Répertoire de sortie pour les corpus
        cache_enabled: Active le cache HTTP par défaut sur le serveur
        extra_env: Variables d'environnement supplémentaires
    """
    env = {}
    if output_dir:
        env["MOISSON_OUTPUT_DIR"] = str(output_dir.resolve())
    if cache_enabled:
        env["MOISSON_CACHE"] = "1"
    if extra_env:
        env.update(extra_env)

    entry: dict = {
        "command": python_exe,
        "args": [str(server_path)],
    }
    if env:
        entry["env"] = env
    return entry


def build_remote_client_entry(url: str, token: str,
                              name: str = "moisson-shs-remote") -> dict:
    """
    Construit la config CLIENT pour un serveur MCP **distant** (HTTP).

    Contrairement à `build_server_entry` (serveur local : `command`/`args`/`env`
    avec chemins disque), l'entrée distante ne contient QUE l'`url` et l'en-tête
    `Authorization: Bearer` — **aucun chemin disque local du serveur**. Forme
    valide pour `.mcp.json` / Claude Desktop / agents.

    Args:
        url: URL HTTPS du endpoint MCP (ex. https://moisson.example.org/mcp).
        token: jeton Bearer.
        name: nom de l'entrée dans `mcpServers`.

    Returns:
        Dict `{"mcpServers": {name: {"url": …, "headers": {"Authorization": …}}}}`.
    """
    return {
        "mcpServers": {
            name: {
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }




# === Clés API ===

# Libellé + URL d'obtention pour la saisie interactive.
API_KEY_PROMPTS = {
    "OPENALEX_API_KEY": (
        "OpenAlex — requis pour --sources openalex (clé gratuite)",
        "https://openalex.org/settings/api",
    ),
    "SEMANTIC_SCHOLAR_API_KEY": (
        "Semantic Scholar — optionnel (sans clé : pool partagé throttlé)",
        "https://www.semanticscholar.org/product/api",
    ),
    "CORE_API_KEY": (
        "CORE — requis pour --sources core (clé Bearer, sur inscription)",
        "https://core.ac.uk/services/api",
    ),
    "SERPAPI_API_KEY": (
        "SerpAPI — requis pour --sources google_scholar "
        "(FACTURÉ À LA REQUÊTE : 1 crédit par page de 20 résultats)",
        "https://serpapi.com/manage-api-key",
    ),
}




def missing_keys_notice(env: dict, where: str) -> str:
    """Avertissement listant les clés inscrites vides ("" si aucune)."""
    empty = [v for v in API_KEY_ENV_VARS if not str(env.get(v) or "").strip()]
    if not empty:
        return ""
    return "\n".join([
        f"⚠️ Clés vides (inscrites sans valeur) : {', '.join(empty)}.",
        f"   À renseigner dans {where}, ou relancer `moisson-shs setup` dans "
        "un terminal (saisie masquée).",
        "   Sans clé, la source concernée n'est pas interrogée ; les autres "
        "sources restent disponibles.",
        "   Le .env du projet sert de réserve côté serveur pour OpenAlex, "
        "Semantic Scholar et CORE (MOISSON_KEY_FALLBACK), jamais pour SerpAPI "
        "par défaut (source facturée).",
    ])


def collect_api_key_env(args) -> dict:
    """
    Construit le dict de clés API à injecter dans le bloc `env` du serveur.

    Priorité : valeur de flag explicite (--openalex-key…), sinon valeur
    présente dans l'environnement du shell courant. Les clés absentes ne sont
    pas injectées (on n'écrit jamais de valeur vide).
    """
    flag_map = {
        "OPENALEX_API_KEY": getattr(args, "openalex_key", None),
        "SEMANTIC_SCHOLAR_API_KEY": getattr(args, "semantic_scholar_key", None),
        "CORE_API_KEY": getattr(args, "core_key", None),
        "SERPAPI_API_KEY": getattr(args, "serpapi_key", None),
    }
    env = {}
    for var, flag_value in flag_map.items():
        value = flag_value or os.environ.get(var)
        if value and value.strip():
            env[var] = value.strip()
    return env


def prompt_missing_api_keys(
    existing: dict,
    no_prompt: bool = False,
    interactive: Optional[bool] = None,
    prompt_func: Optional[Callable[[str], str]] = None,
) -> dict:
    """
    Demande interactivement (saisie masquée) les clés API encore absentes.

    Ne demande QUE les clés non déjà fournies (ni par flag, ni par
    l'environnement — c.-à-d. absentes de ``existing``), et uniquement si le
    terminal est interactif et que ``--no-prompt`` n'a pas été passé. Une saisie
    vide saute la clé : elle ne sera pas écrite, et ``install`` conservera la
    valeur éventuellement déjà présente dans la config (Entrée = conserver).

    Returns:
        Dict des clés saisies (non vides), à fusionner par l'appelant. Jamais
        bloquant hors terminal interactif (renvoie ``{}``), donc sûr en CI.
    """
    if interactive is None:
        try:
            interactive = sys.stdin.isatty()
        except Exception:
            interactive = False
    if no_prompt or not interactive:
        return {}

    ask = prompt_func or getpass.getpass
    missing = [v for v in API_KEY_ENV_VARS if v not in existing]
    if not missing:
        return {}

    print(
        "\nClés API des sources internationales "
        "(saisie masquée ; Entrée vide = passer / conserver l'existante) :"
    )
    collected: dict = {}
    for var in missing:
        label, url = API_KEY_PROMPTS.get(var, (var, ""))
        try:
            value = ask(f"  {var} — {label}\n    {url}\n    > ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        value = (value or "").strip()
        if value:
            collected[var] = value
    return collected




def key_report(env: Optional[dict]) -> list:
    """
    État des quatre clés d'une déclaration de client, sans aucune valeur :
    déclaration (renseignée, vide, absente), réserve du `.env` du projet
    (présente, absente) et repli permis (``MOISSON_KEY_FALLBACK`` de la
    déclaration, sinon de l'environnement, sinon défaut).
    """
    env = env if isinstance(env, dict) else {}
    try:
        from utils import secrets
    except Exception:  # noqa: BLE001 - paquet incomplet : état partiel
        secrets = None
    policy = env.get("MOISSON_KEY_FALLBACK")
    width = max(len(v) for v in API_KEY_ENV_VARS)
    lines = ["   Clés d'API (déclaration du client · réserve du .env · "
             "repli permis) :"]
    for var in API_KEY_ENV_VARS:
        if var not in env:
            declared = "absente"
        elif str(env[var]).strip():
            declared = "renseignée"
        else:
            declared = "vide"
        if secrets is None:
            reserve, allowed = "inconnue", "inconnu"
        else:
            reserve = "présente" if secrets.reserve_value(var) else "absente"
            allowed = "oui" if secrets.fallback_allowed(var, policy) else "non"
            if allowed == "non" and var == "SERPAPI_API_KEY":
                allowed = "non (source facturée)"
        lines.append(f"     {var.ljust(width)}  {declared} · {reserve} · {allowed}")
    return lines



# === Registre des clients ===

def build_registry(overrides: Optional[Dict[str, Path]] = None,
                   runner: Optional[Callable] = None,
                   claude_bin: Optional[str] = None) -> list:
    """Adaptateurs de tous les clients. La CLI ``claude`` est résolue à
    l'appel par ``find_claude_cli`` de ce module (remplaçable par les tests)."""
    resolver = (lambda: claude_bin) if claude_bin else (lambda: find_claude_cli())
    return registry.make_adapters(overrides, runner=runner or _CLI_RUNNER,
                                  claude_bin_resolver=resolver)


def _adapter(client_id: str, path: Optional[Path] = None,
             runner: Optional[Callable] = None, claude_bin: Optional[str] = None):
    overrides = {client_id: Path(path)} if path else {}
    adapters = build_registry(overrides, runner=runner, claude_bin=claude_bin)
    return {a.id: a for a in adapters}[client_id]


def _safe_entry(adapter) -> Optional[dict]:
    try:
        return adapter.read_entry()
    except ConfigUnreadable:
        return None


def _entry_for(adapter, python_exe: str, server_path: Path,
               output_dir: Optional[Path], cache_enabled: bool,
               provided: Optional[dict], pooled: Optional[dict] = None,
               conflicts=()) -> dict:
    """Entrée à déclarer chez ``adapter`` : variables déjà déclarées
    conservées, réglages fournis appliqués, quatre clés (fournie, sinon déjà
    déclarée chez ce client, sinon commune aux autres clients, sinon vide),
    champs propres au client ajoutés."""
    previous = _safe_entry(adapter)
    previous_env = previous.get("env") if isinstance(previous, dict) else None
    managed = {}
    if output_dir:
        managed["MOISSON_OUTPUT_DIR"] = str(Path(output_dir).resolve())
    if cache_enabled:
        managed["MOISSON_CACHE"] = "1"
    keys = resolve_api_keys(provided, previous_env, pooled, conflicts)
    env = merge_env(previous_env, managed, keys)
    base = build_server_entry(python_exe, server_path, extra_env=env)
    return adapter.build_entry(base, previous)


# === Rendu des résultats (jamais de valeur secrète) ===

_DONE = {"added": "ajoutée", "updated": "mise à jour"}


def _render_install(adapter, result: Result, entry: dict, secrets=()) -> str:
    where = result.path or adapter.config_path()
    shown = json.dumps({SERVER_KEY: _redact_env(entry)}, indent=2,
                       ensure_ascii=False)
    lines: List[str]
    if result.action == "dry-run":
        lines = [f"--- DRY RUN ({adapter.label}) ---",
                 f"Aucune modification. Entrée qui serait déclarée dans {where} "
                 f"(clés API masquées) :", "", shown]
        if result.others:
            lines += ["", f"Autres serveurs MCP préservés : {', '.join(result.others)}"]
    elif result.action in _DONE:
        lines = [f"✅ Configuration moisson-shs {_DONE[result.action]} dans "
                 f"{adapter.label} :", f"     {where}", "",
                 "Section installée (clés API masquées à l'affichage) :", shown]
        if result.backup:
            lines += ["", f"💾 Sauvegarde précédente : {result.backup}"]
        if result.secured:
            lines += ["🔒 Droits du fichier restreints à 0600 (il porte des clés d'API)."]
    elif result.action == "snippet":
        lines = [f"⚠️ {adapter.label} : {result.detail}.",
                 f"   À coller dans {where} (clés remplacées par des marqueurs "
                 f"<NOM_DE_LA_CLÉ>) :", "", result.snippet]
    elif result.action == "skipped":
        lines = [f"⏭️ {adapter.label} : {result.detail}."]
    else:
        lines = [f"❌ {adapter.label} : {result.detail}"]
        if result.snippet:
            lines += ["", "Extrait à coller (clés masquées) :", result.snippet]
    if result.action in _DONE or result.action == "dry-run":
        notice = missing_keys_notice(entry.get("env") or {}, str(where))
        if notice:
            lines += ["", notice]
    return _scrub("\n".join(lines), secrets)


def _render_uninstall(adapter, result: Result, secrets=()) -> str:
    where = result.path or adapter.config_path()
    if result.action == "removed":
        lines = [f"✅ Section '{SERVER_KEY}' supprimée de {adapter.label} ({where})."]
        if result.backup:
            lines.append(f"💾 Sauvegarde : {result.backup}")
    elif result.action == "dry-run":
        lines = [f"--- DRY RUN ({adapter.label}) ---",
                 f"L'entrée '{SERVER_KEY}' serait retirée de {where}."]
        if result.others:
            lines.append(f"Autres serveurs conservés : {', '.join(result.others)}")
    elif result.action == "absent":
        lines = [f"⚠️ Aucune entrée '{SERVER_KEY}' dans {adapter.label} ({where})."]
    elif result.action == "skipped":
        lines = [f"⏭️ {adapter.label} : {result.detail}."]
    else:
        lines = [f"❌ {adapter.label} : {result.detail}"]
    return _scrub("\n".join(lines), secrets)


_TIMEOUT_FIELDS = (("startup_timeout_sec", "démarrage", "s"),
                   ("startup_timeout_ms", "démarrage", "ms"),
                   ("tool_timeout_sec", "outil", "s"),
                   ("connect_timeout", "connexion", "s"),
                   ("timeout", "outil", "s"))


def _render_status(adapter) -> str:
    path = adapter.config_path()
    try:
        entry = adapter.read_entry()
    except ConfigUnreadable as exc:
        return f"❌ {adapter.label} : configuration illisible ({exc}) : {path}"
    if entry is None:
        if adapter.id != "claude-code" and not Path(path).exists():
            return f"❌ Aucune configuration {adapter.label} trouvée à {path}."
        return (f"⚠️ Aucune entrée '{SERVER_KEY}' dans {adapter.label} ({path}) "
                f"(lancez 'moisson-shs setup --client {adapter.id}').")
    command = entry.get("command")
    args = entry.get("args") or []
    lines = [f"✅ Serveur moisson-shs installé dans {adapter.label} : {path}",
             f"   command : {command}"
             + ("" if command and Path(str(command)).exists() else "  (⚠️ introuvable)"),
             f"   args    : {args}"
             + ("" if all(Path(str(a)).exists() for a in args if str(a).endswith(".py"))
                else "  (⚠️ fichier absent)")]
    if entry.get("env"):
        lines.append(f"   env     : {_redact_env(entry)['env']}")
    delays = []
    for key, label, unit in _TIMEOUT_FIELDS:
        if key in entry:
            value = entry[key]
            if adapter.id == "gemini-cli" and key == "timeout":
                delays.append(f"{label} {value} ms")
            else:
                delays.append(f"{label} {value} {unit}")
    if delays:
        lines.append(f"   délais  : {' · '.join(delays)}")
    lines += key_report(entry.get("env"))
    others = adapter.other_server_names()
    if others:
        lines.append(f"   Autres serveurs MCP configurés : {', '.join(others)}")
    if adapter.id != "claude-code" and is_world_readable(path):
        lines.append("   ⚠️ Fichier lisible par d'autres comptes : il porte des clés "
                     "(relancer setup le passe en 0600).")
    return _scrub("\n".join(lines), adapter.secret_values())


# === Fonctions historiques (Claude Desktop, Claude Code) ===

def install(
    config_path: Path,
    python_exe: str,
    server_path: Path,
    output_dir: Optional[Path],
    cache_enabled: bool,
    dry_run: bool = False,
    extra_env: Optional[dict] = None,
) -> str:
    """Insère ou met à jour l'entrée moisson-shs dans Claude Desktop."""
    adapter = _adapter("claude-desktop", config_path)
    entry = _entry_for(adapter, python_exe, server_path, output_dir,
                       cache_enabled, extra_env)
    result = adapter.install(entry, dry_run=dry_run)
    return _render_install(adapter, result, entry, adapter.secret_values())


def uninstall(config_path: Path, dry_run: bool = False) -> str:
    """Supprime l'entrée moisson-shs de Claude Desktop."""
    adapter = _adapter("claude-desktop", config_path)
    return _render_uninstall(adapter, adapter.uninstall(dry_run=dry_run),
                             adapter.secret_values())


def status(config_path: Path) -> str:
    """État de l'installation dans Claude Desktop."""
    return _render_status(_adapter("claude-desktop", config_path))


def read_claude_code_entry(config_path: Path) -> Optional[dict]:
    """Entrée ``moisson-shs`` de portée utilisateur de Claude Code."""
    try:
        return _adapter("claude-code", config_path).read_entry()
    except ConfigUnreadable as exc:
        raise ValueError(str(exc)) from exc


def install_claude_code(
    python_exe: str,
    server_path: Path,
    output_dir: Optional[Path],
    cache_enabled: bool,
    dry_run: bool = False,
    extra_env: Optional[dict] = None,
    config_path: Optional[Path] = None,
    runner: Optional[Callable] = None,
    claude_bin: Optional[str] = None,
) -> str:
    """Déclare le serveur dans Claude Code (portée utilisateur)."""
    adapter = _adapter("claude-code", config_path, runner, claude_bin)
    entry = _entry_for(adapter, python_exe, server_path, output_dir,
                       cache_enabled, extra_env)
    result = adapter.install(entry, dry_run=dry_run)
    return _render_install(adapter, result, entry, adapter.secret_values())


def uninstall_claude_code(config_path: Optional[Path] = None,
                          dry_run: bool = False,
                          runner: Optional[Callable] = None,
                          claude_bin: Optional[str] = None) -> str:
    """Retire l'entrée moisson-shs de Claude Code."""
    adapter = _adapter("claude-code", config_path, runner, claude_bin)
    return _render_uninstall(adapter, adapter.uninstall(dry_run=dry_run),
                             adapter.secret_values())


def status_claude_code(config_path: Optional[Path] = None) -> str:
    """État de la déclaration dans Claude Code."""
    return _render_status(_adapter("claude-code", config_path))


# === Vérifications pré-installation ===

def check_python_has_mcp(python_exe: str) -> bool:
    """
    Vérifie que l'interpréteur Python a le module mcp installé.

    Args:
        python_exe: Chemin vers le binaire Python

    Returns:
        True si `import mcp` réussit dans cet interpréteur
    """
    import subprocess
    try:
        result = subprocess.run(
            [python_exe, "-c", "import mcp"],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def check_dependencies(python_exe: str) -> tuple[bool, list[str]]:
    """
    Vérifie la présence des dépendances obligatoires.

    Returns:
        (all_ok, missing_packages)
    """
    import subprocess
    needed = ["requests", "sickle", "lxml", "unidecode", "mcp"]
    missing = []
    for pkg in needed:
        # sickle s'importe en `Sickle` mais pip name est `sickle`
        import_name = "sickle" if pkg == "sickle" else pkg
        result = subprocess.run(
            [python_exe, "-c", f"import {import_name}"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            missing.append(pkg)
    return (not missing, missing)




# === Orchestration (setup universel) ===

def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def _state(detection) -> str:
    if detection.declared:
        text = f"déclaré · clés {api_keys_filled(detection.env)}/4"
    elif detection.found:
        text = "détecté, non déclaré"
    else:
        text = "non détecté"
    if not detection.writable:
        text += f" · extrait à coller ({detection.reason})"
    elif detection.reason:
        text += f" · {detection.reason}"
    return text


def _choose(adapters, detections, uninstall: bool, input_func, print_func):
    """Menu des clients (terminal) ; ids retenus ou None (quitter)."""
    if uninstall:
        pool = [a for a in adapters if detections[a.id].declared]
        if not pool:
            print_func(f"Aucun client ne déclare '{SERVER_KEY}'.")
            return []
        rows = [menu.Row(a.id, a.label, str(a.config_path()),
                         _state(detections[a.id]), False) for a in pool]
        return menu.ask_selection(rows, input_func=input_func,
                                  print_func=print_func, verb="Retirer de")
    rows = [menu.Row(a.id, a.label, str(a.config_path()), _state(detections[a.id]),
                     detections[a.id].found) for a in adapters]
    return menu.ask_selection(rows, input_func=input_func, print_func=print_func,
                              verb="Configurer")


def _closed_or_skip(adapter, interactive: bool, force: bool, input_func,
                    print_func) -> Optional[str]:
    """None si l'écriture peut avoir lieu, sinon le motif du saut. Un client
    ouvert qui réécrit sa configuration pourrait effacer la modification."""
    names = adapter.running_process_names
    if not names:
        return None
    running = procs.is_running(names)
    if running is None:
        print_func(f"ℹ️ {adapter.label} : impossible de vérifier si l'application "
                   f"est ouverte ; mieux vaut la fermer avant l'écriture.")
        return None
    if not running:
        return None
    if force:
        print_func(f"⚠️ {adapter.label} est ouvert : écriture forcée (--force).")
        return None
    if not interactive:
        return "application ouverte, client sauté (la quitter, ou --force)"
    for _ in range(3):
        try:
            answer = input_func(
                f"⚠️ {adapter.label} est ouvert et réécrit sa configuration : la "
                f"modification pourrait être perdue. Quitter l'application puis "
                f"Entrée · c = continuer quand même · s = sauter : ")
        except (EOFError, KeyboardInterrupt):
            return "sauté"
        answer = (answer or "").strip().lower()
        if answer == "c":
            return None
        if answer == "s":
            return "sauté à la demande"
        if not procs.is_running(names):
            return None
    return "application toujours ouverte, client sauté"


def _overrides(args) -> Dict[str, Path]:
    overrides = dict(args.client_config or [])
    if args.config:
        overrides["claude-desktop"] = args.config
    if args.claude_code_config:
        overrides["claude-code"] = args.claude_code_config
    return overrides


def _summary_line(adapter, result: Result) -> str:
    text = {"added": "ajoutée", "updated": "mise à jour", "removed": "retirée",
            "dry-run": "aperçu seulement", "snippet": "extrait à coller",
            "absent": "rien à retirer", "skipped": "sautée",
            "error": "erreur"}.get(result.action, result.action)
    return f"  {adapter.label.ljust(44)} {text}"


def _run(args, adapters=None, interactive: Optional[bool] = None,
         input_func: Callable = input, print_func: Callable = print,
         secret_input: Optional[Callable] = None) -> int:
    """Déroulé de ``moisson-shs setup`` une fois les arguments lus."""
    adapters = adapters if adapters is not None else build_registry(_overrides(args))
    by_id = {a.id: a for a in adapters}
    if interactive is None:
        interactive = _stdin_is_tty()
    detections = {a.id: a.detect() for a in adapters}

    # --- clients visés ---
    if args.client == ["all"]:
        if args.status:
            ids = list(by_id)
        elif args.uninstall:
            ids = [i for i in by_id if detections[i].declared]
        else:
            ids = [i for i in by_id if detections[i].found and detections[i].writable]
    elif args.client:
        ids = list(args.client)
    elif args.status:
        ids = list(by_id)
    elif interactive:
        ids = _choose(adapters, detections, args.uninstall, input_func, print_func)
        if ids is None:
            print_func("Aucune modification.")
            return 0
    else:
        ids = ["claude-desktop"]
    selected = [by_id[i] for i in ids if i in by_id]
    if not selected:
        print_func("Aucun client à traiter.")
        return 0

    # --- état ---
    if args.status:
        for adapter in selected:
            print_func(_render_status(adapter))
            print_func("")
        return 0

    results = []

    # --- retrait ---
    if args.uninstall:
        for adapter in selected:
            reason = (None if args.dry_run else
                      _closed_or_skip(adapter, interactive, args.force, input_func,
                                      print_func))
            result = (Result(adapter.id, "skipped", ok=False, detail=reason)
                      if reason else adapter.uninstall(dry_run=args.dry_run))
            results.append((adapter, result))
            print_func(_render_uninstall(adapter, result, adapter.secret_values()))
            print_func("")
    else:
        # --- installation ---
        python_exe = args.python or get_python_executable()
        server_path = (getattr(args, "server_path_resolved", None)
                       or args.server_path or get_mcp_server_path())
        if not args.skip_deps_check and not args.dry_run:
            all_ok, missing = check_dependencies(python_exe)
            if not all_ok:
                print(f"⚠️ L'interpréteur {python_exe}", file=sys.stderr)
                print(f"   ne dispose pas des dépendances suivantes : "
                      f"{', '.join(missing)}", file=sys.stderr)
                print_func("")
                print_func("Installer avec :")
                print_func(f"  {python_exe} -m pip install moisson-shs[mcp]")
                print_func("")
                print_func("Ou utiliser --skip-deps-check pour forcer (déconseillé).")
                return 1

        # Clés : options et shell, puis une seule saisie masquée partagée.
        provided = collect_api_key_env(args)
        if not args.dry_run:
            if not interactive and not args.no_prompt:
                print_func("ℹ️ Terminal non interactif : saisie masquée des clés non "
                           "proposée (relancer dans un terminal pour la saisie).")
            provided.update(prompt_missing_api_keys(
                provided, no_prompt=args.no_prompt, interactive=interactive,
                prompt_func=secret_input))
        if provided:
            print_func(f"🔑 Clés API fournies : {', '.join(sorted(provided))}")
        pooled, conflicts = pool_api_keys(d.env for d in detections.values())
        for var in conflicts:
            if var not in provided:
                print_func(f"⚠️ {var} diffère selon les clients : non mise en commun "
                           f"(aucune valeur affichée).")
        known = list(provided.values()) + list(pooled.values())

        for adapter in selected:
            entry = _entry_for(adapter, python_exe, server_path,
                               args.output_dir, args.cache, provided, pooled,
                               conflicts)
            reason = (None if args.dry_run else
                      _closed_or_skip(adapter, interactive, args.force, input_func,
                                      print_func))
            result = (Result(adapter.id, "skipped", ok=False, detail=reason)
                      if reason else adapter.install(entry, dry_run=args.dry_run))
            results.append((adapter, result))
            secrets = known + list((entry.get("env") or {}).values()) \
                + adapter.secret_values()
            print_func(_render_install(adapter, result, entry, secrets))
            print_func("")

    # --- récapitulatif ---
    if len(results) > 1:
        print_func("Récapitulatif :")
        for adapter, result in results:
            print_func(_summary_line(adapter, result))
        print_func("")
    changed = [a for a, r in results if r.action in ("added", "updated", "removed")]
    if changed:
        print_func("🔄 Pour activer la modification :")
        for adapter in changed:
            print_func(f"   {adapter.restart_hint}")
        if not args.uninstall:
            print_func("")
            print_func("📚 Pour tester : « Liste mes corpus disponibles », ou « Moissonne "
                       "les sources SHS sur \"discours numérique\", plafond 100 ».")
    return 0 if all(r.ok for _a, r in results) else 1


# === CLI ===

def _parse_args(argv: Optional[list] = None):
    """Arguments de ``moisson-shs setup`` (parseur complet, puis lecture)."""
    parser = argparse.ArgumentParser(
        prog="moisson-shs setup",
        description=(
            "Configure le serveur MCP moisson-shs dans les clients MCP : Claude "
            "Desktop, Claude Code, OpenAI Codex (application ChatGPT, CLI, IDE), "
            "Gemini CLI, Antigravity, Hermes Agent. Sans --client : menu des "
            "clients détectés en terminal, Claude Desktop seul sinon. Préserve "
            "les autres serveurs MCP ; sauvegarde horodatée avant toute écriture ; "
            "le bloc env porte toujours les quatre clés d'API (vides si non "
            "fournies)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Clients (ids, alias) : claude-desktop (desktop), claude-code (code),
  codex (chatgpt), gemini-cli (gemini), antigravity (agy), antigravity-ide,
  hermes (hermes-agent) ; all = tous les clients détectés.

Exemples :
  moisson-shs setup
  moisson-shs setup --client codex,gemini
  moisson-shs setup --client all --dry-run
  moisson-shs setup --status
  moisson-shs setup --uninstall --client gemini
  moisson-shs setup --client-config hermes=~/autre/config.yaml --client hermes
        """,
    )
    parser.add_argument(
        "--client",
        type=registry.parse_client_spec,
        default=None,
        metavar="IDS",
        help="Clients à configurer, séparés par des virgules (ids ou alias, "
             "voir plus bas) ; all = tous les clients détectés. Absent : menu "
             "en terminal, Claude Desktop seul sinon",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Chemin personnalisé vers claude_desktop_config.json "
             "(défaut : auto-détection selon l'OS)",
    )
    parser.add_argument(
        "--claude-code-config",
        type=Path,
        default=None,
        help="Fichier d'état de Claude Code lu pour l'entrée existante "
             "(défaut : ~/.claude.json)",
    )
    parser.add_argument(
        "--client-config",
        type=registry.parse_client_config,
        action="append",
        default=[],
        metavar="ID=CHEMIN",
        help="Fichier de configuration imposé pour un client (répétable), "
             "ex. hermes=~/autre/config.yaml",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Répertoire où le serveur stockera les corpus produits "
             "(MOISSON_OUTPUT_DIR). Défaut : `output/` à côté de mcp_server.py, "
             "ou la valeur déjà déclarée chez le client.",
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help="Active le cache HTTP par défaut côté serveur (MOISSON_CACHE=1)",
    )
    parser.add_argument(
        "--python",
        type=str,
        default=None,
        help="Chemin Python à utiliser (défaut : sys.executable, c.-à-d. "
             "le Python qui exécute cette commande)",
    )
    parser.add_argument(
        "--server-path",
        type=Path,
        default=None,
        help="Chemin vers mcp_server.py (défaut : auto-détection)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche ce qui serait écrit sans modifier aucun fichier",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Retire l'entrée moisson-shs (préserve les autres serveurs)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Affiche l'état de chaque client (déclaration, délais, clés)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Écrit même si l'application cliente est ouverte (hors terminal, "
             "un client ouvert est sinon sauté)",
    )
    parser.add_argument(
        "--skip-deps-check",
        action="store_true",
        help="Ne vérifie pas la présence des dépendances dans le Python cible",
    )
    parser.add_argument(
        "--openalex-key",
        type=str,
        default=None,
        help="Clé API OpenAlex à inscrire dans le bloc env (OPENALEX_API_KEY). "
             "Par défaut, reprise depuis l'environnement si présente.",
    )
    parser.add_argument(
        "--semantic-scholar-key",
        type=str,
        default=None,
        help="Clé API Semantic Scholar (SEMANTIC_SCHOLAR_API_KEY, optionnelle).",
    )
    parser.add_argument(
        "--core-key",
        type=str,
        default=None,
        help="Clé API CORE (CORE_API_KEY) à inscrire dans le bloc env.",
    )
    parser.add_argument(
        "--serpapi-key",
        type=str,
        default=None,
        help="Clé API SerpAPI (SERPAPI_API_KEY) pour --sources google_scholar. "
             "Source facturée à la requête : voir .env.example.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Ne pas demander interactivement les clés API manquantes "
             "(installation non interactive / scriptée). Par défaut, en "
             "terminal interactif, les clés absentes des flags et de "
             "l'environnement sont demandées en saisie masquée.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] = None) -> int:
    """Point d'entrée appelé via `moisson-shs setup [...]`."""
    args = _parse_args(argv)
    try:
        args.server_path_resolved = args.server_path or get_mcp_server_path()
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
