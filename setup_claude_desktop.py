"""
Configuration automatique du serveur MCP moisson-shs dans Claude Desktop et
dans Claude Code.

Usage :
    moisson-shs setup
    moisson-shs setup --client code          # Claude Code (portée utilisateur)
    moisson-shs setup --client all           # les deux
    moisson-shs setup --output-dir /chemin/personnalise
    moisson-shs setup --uninstall
    moisson-shs setup --dry-run

Le script :
1. Détecte l'OS et localise `claude_desktop_config.json` (Claude Desktop) ou
   `~/.claude.json` (Claude Code, lu seulement : l'écriture passe par
   `claude mcp add-json --scope user`)
2. Détecte l'interpréteur Python actif (venv prioritaire)
3. Calcule le chemin absolu de mcp_server.py
4. Insère/met à jour l'entrée `moisson-shs` en préservant les autres serveurs
   MCP existants ; le bloc `env` porte TOUJOURS les quatre clés d'API
   (renseignées, ou vides : la déclaration montre ce qui reste à fournir)
5. Affiche un récapitulatif et demande à l'utilisateur de redémarrer le
   client pour activer le serveur.

Clés d'API (sprint « clés ») : la déclaration du client fait autorité ; le
`.env` du projet sert de réserve côté serveur (``utils.secrets``) quand une
clé y est vide ou refusée, selon ``MOISSON_KEY_FALLBACK``. ``setup`` ne copie
jamais les clés du `.env` dans la déclaration du client.

Le script ne modifie aucun autre fichier ni paramètre du système.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Callable


SERVER_KEY = "moisson-shs"


# === Détection des chemins ===

def get_claude_config_path() -> Path:
    """
    Renvoie le chemin du `claude_desktop_config.json` selon l'OS.

    Locations officielles documentées par Anthropic :
    - macOS  : ~/Library/Application Support/Claude/claude_desktop_config.json
    - Windows: %APPDATA%/Claude/claude_desktop_config.json
    - Linux  : ~/.config/Claude/claude_desktop_config.json
    """
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
        return home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    # Linux et autres Unix
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config) if xdg_config else home / ".config"
    return base / "Claude" / "claude_desktop_config.json"


def get_claude_code_config_path() -> Path:
    """Fichier d'état de Claude Code, qui porte les serveurs MCP de portée
    utilisateur (clé ``mcpServers`` de premier niveau). Lu seulement :
    Claude Code le réécrit en continu, l'écriture passe par sa CLI."""
    return Path.home() / ".claude.json"


def find_claude_cli() -> Optional[str]:
    """Chemin de la CLI ``claude`` (Claude Code), ou None."""
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.is_file() else None


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


# Variables d'environnement de clés API injectables dans le bloc `env`
# (les clés OpenAlex/CORE/Semantic Scholar sont liées au compte utilisateur).
API_KEY_ENV_VARS = (
    "OPENALEX_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
    "CORE_API_KEY",
    "SERPAPI_API_KEY",
)

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


def complete_api_key_env(provided: Optional[dict],
                         previous_env: Optional[dict]) -> dict:
    """
    Les quatre clés d'API de la déclaration du client : valeur fournie à
    cette exécution, sinon valeur déjà déclarée, sinon chaîne vide. Une
    variable vide reste visible (à renseigner) et le serveur la traite comme
    absente : la réserve du `.env` peut alors servir (``utils.secrets``).
    """
    provided = provided or {}
    previous_env = previous_env if isinstance(previous_env, dict) else {}
    out = {}
    for var in API_KEY_ENV_VARS:
        out[var] = (str(provided.get(var) or "").strip()
                    or str(previous_env.get(var) or "").strip())
    return out


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


def _redact_env(entry: dict) -> dict:
    """
    Copie d'une entrée serveur dont les VALEURS de clés API sont masquées,
    pour l'affichage console uniquement. Le fichier écrit sur disque conserve
    les vraies clés ; on évite seulement de les imprimer sur stdout (scrollback,
    logs CI, copier-coller de support).
    """
    redacted = dict(entry)
    env = redacted.get("env")
    if isinstance(env, dict):
        env_copy = dict(env)
        for var in API_KEY_ENV_VARS:
            if env_copy.get(var):
                n = len(str(env_copy[var]))
                env_copy[var] = f"*** masqué ({n} caractères) ***"
            elif var in env_copy:
                env_copy[var] = "(vide)"
        redacted["env"] = env_copy
    return redacted


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


def install(
    config_path: Path,
    python_exe: str,
    server_path: Path,
    output_dir: Optional[Path],
    cache_enabled: bool,
    dry_run: bool = False,
    extra_env: Optional[dict] = None,
) -> str:
    """
    Insère ou met à jour la section moisson-shs dans la config Claude Desktop.

    Returns:
        Texte décrivant l'action effectuée
    """
    config = load_existing_config(config_path)

    # Sauvegarde de la config existante (backup .bak)
    backup_path = None
    if config_path.exists() and not dry_run:
        backup_path = config_path.with_suffix(".json.bak")
        shutil.copy2(config_path, backup_path)

    # Préserver les autres serveurs MCP déjà configurés
    config.setdefault("mcpServers", {})
    previous = config["mcpServers"].get(SERVER_KEY)

    # Les quatre clés sont toujours déclarées : valeur fournie, sinon valeur
    # déjà présente dans l'entrée moisson-shs (un `setup` ultérieur sans clé
    # ne l'efface pas), sinon vide.
    merged_env = dict(extra_env or {})
    merged_env.update(complete_api_key_env(
        extra_env, previous.get("env") if previous else None))

    config["mcpServers"][SERVER_KEY] = build_server_entry(
        python_exe, server_path, output_dir, cache_enabled,
        extra_env=merged_env,
    )
    notice = missing_keys_notice(merged_env, str(config_path))

    # Sérialisation finale (réelle, écrite sur disque — clés en clair)
    serialized = json.dumps(config, indent=2, ensure_ascii=False)

    if dry_run:
        # Aperçu console : l'entrée moisson-shs seule, clés masquées. Les
        # autres serveurs sont seulement nommés : leurs blocs env portent
        # leurs propres secrets, que l'aperçu imprimait en clair.
        serialized_display = json.dumps(
            {SERVER_KEY: _redact_env(config["mcpServers"][SERVER_KEY])},
            indent=2, ensure_ascii=False,
        )
        other = [k for k in config["mcpServers"] if k != SERVER_KEY]
        return (
            f"--- DRY RUN ---\n"
            f"Aucun fichier n'a été modifié. Entrée qui serait écrite dans "
            f"{config_path} (clés API masquées) :\n\n"
            f"{serialized_display}"
            + (f"\n\nAutres serveurs MCP préservés : {', '.join(other)}"
               if other else "")
            + (f"\n\n{notice}" if notice else "")
        )

    # Écriture
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(serialized + "\n", encoding="utf-8")

    action = "mise à jour" if previous else "ajoutée"
    lines = [
        f"✅ Configuration moisson-shs {action} dans :",
        f"     {config_path}",
        "",
        f"Section installée (clés API masquées à l'affichage) :",
        json.dumps(
            {SERVER_KEY: _redact_env(config["mcpServers"][SERVER_KEY])},
            indent=2,
        ),
    ]
    if backup_path:
        lines += ["", f"💾 Sauvegarde précédente : {backup_path}"]
    if notice:
        lines += ["", notice]
    return "\n".join(lines)


def uninstall(config_path: Path, dry_run: bool = False) -> str:
    """Supprime la section moisson-shs en préservant les autres serveurs."""
    if not config_path.exists():
        return f"⚠️ Aucune configuration trouvée à {config_path} — rien à désinstaller."

    config = load_existing_config(config_path)
    servers = config.get("mcpServers", {})
    if SERVER_KEY not in servers:
        return f"⚠️ Aucune entrée 'moisson-shs' dans {config_path}."

    if dry_run:
        return (
            f"--- DRY RUN ---\n"
            f"L'entrée 'moisson-shs' serait supprimée de {config_path}. "
            f"Les autres serveurs ({len(servers) - 1}) seraient conservés."
        )

    backup_path = config_path.with_suffix(".json.bak")
    shutil.copy2(config_path, backup_path)
    del servers[SERVER_KEY]
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return (
        f"✅ Section 'moisson-shs' supprimée de {config_path}.\n"
        f"💾 Sauvegarde : {backup_path}"
    )


def status(config_path: Path) -> str:
    """Affiche l'état de l'installation."""
    if not config_path.exists():
        return f"❌ Aucune configuration Claude Desktop trouvée à {config_path}."

    try:
        config = load_existing_config(config_path)
    except ValueError as e:
        return f"❌ {e}"

    servers = config.get("mcpServers", {})
    if SERVER_KEY not in servers:
        return (
            f"⚠️ Configuration Claude Desktop trouvée à {config_path}\n"
            f"   mais aucune entrée 'moisson-shs' (lancez 'moisson-shs setup')."
        )

    entry = servers[SERVER_KEY]
    lines = [
        f"✅ Serveur moisson-shs installé dans {config_path}",
        f"   command : {entry.get('command')}",
        f"   args    : {entry.get('args')}",
    ]
    if entry.get("env"):
        # Masquer les valeurs de clés API à l'affichage
        lines.append(f"   env     : {_redact_env(entry)['env']}")
    lines += key_report(entry.get("env"))
    other = [k for k in servers if k != SERVER_KEY]
    if other:
        lines.append(f"   Autres serveurs MCP configurés : {', '.join(other)}")
    return "\n".join(lines)


# === Claude Code (portée utilisateur) ===

def read_claude_code_entry(config_path: Path) -> Optional[dict]:
    """Entrée ``moisson-shs`` de portée utilisateur de Claude Code (None :
    absente). Lecture seule de ``~/.claude.json``."""
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{config_path} illisible : {type(e).__name__}") from e
    entry = (data.get("mcpServers") or {}).get(SERVER_KEY)
    return entry if isinstance(entry, dict) else None


def _scrub(text: str, values) -> str:
    """Retire d'un message (sortie de la CLI) toute valeur de clé connue."""
    text = str(text or "").strip()
    for value in values:
        if value:
            text = text.replace(str(value), "***")
    return text


def _claude_cmd(runner, cmd: list):
    return runner(cmd, capture_output=True, text=True, timeout=60)


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
    """
    Déclare le serveur dans Claude Code, portée utilisateur, par
    ``claude mcp add-json --scope user`` (jamais ``.mcp.json`` : fichier de
    projet versionné, les clés y seraient en clair). Une entrée existante est
    retirée puis ajoutée de nouveau ; si l'ajout échoue, l'entrée précédente
    est restaurée. Les quatre clés sont toujours déclarées (voir
    ``complete_api_key_env``).

    Limite : la CLI reçoit l'entrée en argument, les clés sont donc visibles
    dans la liste des processus le temps de l'appel.

    Returns:
        Texte décrivant l'action effectuée (clés masquées).
    """
    config_path = config_path or get_claude_code_config_path()
    try:
        previous = read_claude_code_entry(config_path)
    except ValueError as e:
        return f"❌ {e}"
    previous_env = previous.get("env") if previous else None
    env = dict(extra_env or {})
    env.update(complete_api_key_env(extra_env, previous_env))
    entry = {"type": "stdio", **build_server_entry(
        python_exe, server_path, output_dir, cache_enabled, extra_env=env)}
    shown = json.dumps({SERVER_KEY: _redact_env(entry)}, indent=2,
                       ensure_ascii=False)
    notice = missing_keys_notice(
        env, f"{config_path} (entrée moisson-shs, portée utilisateur)")

    if dry_run:
        return "\n".join(
            ["--- DRY RUN (Claude Code) ---",
             "Aucune commande lancée. Entrée qui serait déclarée en portée "
             "utilisateur (clés masquées) :", "", shown]
            + (["", notice] if notice else []))

    claude = claude_bin or find_claude_cli()
    if not claude:
        return ("❌ CLI `claude` introuvable : installer Claude Code, ou "
                "utiliser --client desktop.")
    runner = runner or subprocess.run
    known = list(env.values()) + list((previous_env or {}).values())
    scope = ["--scope", "user"]

    if previous is not None:
        done = _claude_cmd(runner, [claude, "mcp", "remove", SERVER_KEY] + scope)
        if done.returncode != 0:
            return (f"❌ Échec du retrait de l'entrée existante (code "
                    f"{done.returncode}) : {_scrub(done.stderr, known)}")
    done = _claude_cmd(runner, [claude, "mcp", "add-json", SERVER_KEY,
                                json.dumps(entry, ensure_ascii=False)] + scope)
    if done.returncode != 0:
        lines = [f"❌ Échec de l'ajout dans Claude Code (code "
                 f"{done.returncode}) : {_scrub(done.stderr, known)}"]
        if previous is not None:
            back = _claude_cmd(runner, [claude, "mcp", "add-json", SERVER_KEY,
                                        json.dumps(previous, ensure_ascii=False)]
                               + scope)
            lines.append("   Entrée précédente restaurée." if back.returncode == 0
                         else "   ⚠️ Restauration de l'entrée précédente en "
                              "échec : relancer la commande.")
        return "\n".join(lines)

    action = "mise à jour" if previous else "ajoutée"
    lines = [f"✅ Configuration moisson-shs {action} dans Claude Code "
             f"(portée utilisateur, {config_path}) :", shown]
    if notice:
        lines += ["", notice]
    return "\n".join(lines)


def uninstall_claude_code(config_path: Optional[Path] = None,
                          dry_run: bool = False,
                          runner: Optional[Callable] = None,
                          claude_bin: Optional[str] = None) -> str:
    """Retire l'entrée moisson-shs de portée utilisateur de Claude Code."""
    config_path = config_path or get_claude_code_config_path()
    try:
        entry = read_claude_code_entry(config_path)
    except ValueError as e:
        return f"❌ {e}"
    if entry is None:
        return (f"⚠️ Aucune entrée 'moisson-shs' en portée utilisateur dans "
                f"{config_path}.")
    if dry_run:
        return ("--- DRY RUN (Claude Code) ---\nL'entrée 'moisson-shs' de "
                "portée utilisateur serait retirée.")
    claude = claude_bin or find_claude_cli()
    if not claude:
        return "❌ CLI `claude` introuvable."
    done = _claude_cmd(runner or subprocess.run,
                       [claude, "mcp", "remove", SERVER_KEY, "--scope", "user"])
    if done.returncode != 0:
        known = list((entry.get("env") or {}).values())
        return (f"❌ Échec du retrait (code {done.returncode}) : "
                f"{_scrub(done.stderr, known)}")
    return "✅ Entrée 'moisson-shs' retirée de Claude Code (portée utilisateur)."


def status_claude_code(config_path: Optional[Path] = None) -> str:
    """État de la déclaration dans Claude Code (clés masquées)."""
    config_path = config_path or get_claude_code_config_path()
    try:
        entry = read_claude_code_entry(config_path)
    except ValueError as e:
        return f"❌ {e}"
    if entry is None:
        return (f"⚠️ Aucune entrée 'moisson-shs' en portée utilisateur dans "
                f"{config_path} (lancez 'moisson-shs setup --client code').")
    lines = [
        f"✅ Serveur moisson-shs déclaré dans Claude Code (portée utilisateur, "
        f"{config_path})",
        f"   command : {entry.get('command')}",
        f"   args    : {entry.get('args')}",
    ]
    if entry.get("env"):
        lines.append(f"   env     : {_redact_env(entry)['env']}")
    lines += key_report(entry.get("env"))
    return "\n".join(lines)


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


# === CLI ===

def main(argv: list[str] = None) -> int:
    """Point d'entrée appelé via `moisson-shs setup [...]`."""
    parser = argparse.ArgumentParser(
        prog="moisson-shs setup",
        description=(
            "Configure le serveur MCP moisson-shs dans Claude Desktop et/ou "
            "Claude Code (portée utilisateur). Détecte automatiquement l'OS, "
            "le venv Python et le chemin du serveur. Préserve les autres "
            "serveurs MCP existants. Le bloc env porte toujours les quatre "
            "clés d'API (vides si non fournies)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  moisson-shs setup
  moisson-shs setup --client code
  moisson-shs setup --client all
  moisson-shs setup --output-dir ~/Documents/corpus_shs
  moisson-shs setup --cache
  moisson-shs setup --dry-run
  moisson-shs setup --status --client all
  moisson-shs setup --uninstall
        """,
    )
    parser.add_argument(
        "--client",
        choices=("desktop", "code", "all"),
        default="desktop",
        help="Client MCP à configurer : desktop (Claude Desktop, défaut), "
             "code (Claude Code, portée utilisateur, par `claude mcp "
             "add-json`), all (les deux)",
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
        "--output-dir",
        type=Path,
        default=None,
        help="Répertoire où le serveur stockera les corpus produits "
             "(MOISSON_OUTPUT_DIR). Défaut : `output/` à côté de mcp_server.py.",
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
        help="Affiche ce qui serait écrit sans modifier le fichier",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Supprime la section moisson-shs (préserve les autres serveurs)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Affiche l'état actuel de l'installation",
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
        help="Clé API OpenAlex à injecter dans le bloc env (OPENALEX_API_KEY). "
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
        help="Clé API CORE (CORE_API_KEY) à injecter dans le bloc env.",
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
    args = parser.parse_args(argv)

    # Résolution des chemins
    config_path = args.config or get_claude_config_path()
    python_exe = args.python or get_python_executable()

    try:
        server_path = args.server_path or get_mcp_server_path()
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    desktop = args.client in ("desktop", "all")
    code = args.client in ("code", "all")
    code_config = args.claude_code_config or get_claude_code_config_path()

    # Mode status
    if args.status:
        if desktop:
            print(status(config_path))
        if code:
            print(status_claude_code(code_config))
        return 0

    # Mode uninstall
    if args.uninstall:
        if desktop:
            print(uninstall(config_path, dry_run=args.dry_run))
        if code:
            print(uninstall_claude_code(code_config, dry_run=args.dry_run))
        if not args.dry_run:
            print()
            print("⚠️ Redémarrez le client (Claude Desktop, sessions Claude Code) "
                  "pour que le changement soit pris en compte.")
        return 0

    # Vérification des dépendances dans le Python cible
    if not args.skip_deps_check and not args.dry_run:
        all_ok, missing = check_dependencies(python_exe)
        if not all_ok:
            print(f"⚠️ L'interpréteur {python_exe}", file=sys.stderr)
            print(
                f"   ne dispose pas des dépendances suivantes : {', '.join(missing)}",
                file=sys.stderr,
            )
            print()
            print("Installer avec :")
            print(f"  {python_exe} -m pip install moisson-shs[mcp]")
            print()
            print("Ou utiliser --skip-deps-check pour forcer (déconseillé).")
            return 1

    # Clés API à inscrire dans le bloc env : flags / environnement, puis
    # saisie interactive des clés encore manquantes (sauf --no-prompt /
    # dry-run / terminal non interactif). Les clés déjà présentes dans la
    # déclaration sont préservées si on les saute (Entrée vide) ; les autres
    # sont inscrites vides.
    api_key_env = collect_api_key_env(args)
    if not args.dry_run:
        try:
            interactive = sys.stdin.isatty()
        except Exception:  # noqa: BLE001
            interactive = False
        if not interactive and not args.no_prompt:
            print("ℹ️ Terminal non interactif : saisie masquée des clés non "
                  "proposée (relancer dans un terminal pour la saisie).")
        api_key_env.update(
            prompt_missing_api_keys(api_key_env, no_prompt=args.no_prompt,
                                    interactive=interactive)
        )
    if api_key_env:
        print(f"🔑 Clés API fournies : {', '.join(sorted(api_key_env))}")

    # Installation
    if desktop:
        print(install(
            config_path=config_path,
            python_exe=python_exe,
            server_path=server_path,
            output_dir=args.output_dir,
            cache_enabled=args.cache,
            dry_run=args.dry_run,
            extra_env=api_key_env,
        ))
    if code:
        if desktop:
            print()
        print(install_claude_code(
            python_exe=python_exe,
            server_path=server_path,
            output_dir=args.output_dir,
            cache_enabled=args.cache,
            dry_run=args.dry_run,
            extra_env=api_key_env,
            config_path=code_config,
        ))

    if not args.dry_run:
        print()
        if desktop:
            print("🔄 Claude Desktop : quitter et relancer **complètement** "
                  "(Cmd+Q sur macOS, fermer toutes les fenêtres puis relancer).")
        if code:
            print("🔄 Claude Code : relancer les sessions ouvertes pour charger "
                  "la nouvelle entrée.")
        print()
        print("📚 Vous pouvez tester avec :")
        print('   « Liste mes corpus disponibles »')
        print('   « Moissonne les sources SHS sur "discours numérique", plafond 100 »')

    return 0


if __name__ == "__main__":
    sys.exit(main())
