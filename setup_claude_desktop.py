"""
Configuration automatique du serveur MCP moisson-shs dans Claude Desktop.

Usage :
    moisson-shs setup
    moisson-shs setup --output-dir /chemin/personnalise
    moisson-shs setup --uninstall
    moisson-shs setup --dry-run

Le script :
1. Détecte l'OS et localise `claude_desktop_config.json`
2. Détecte l'interpréteur Python actif (venv prioritaire)
3. Calcule le chemin absolu de mcp_server.py
4. Insère/met à jour la section `mcpServers.moisson-shs`
   en préservant les autres serveurs MCP existants
5. Affiche un récapitulatif et demande à l'utilisateur de redémarrer
   Claude Desktop pour activer le serveur.

Le script ne modifie aucun autre fichier ni paramètre du système.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
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
        redacted["env"] = env_copy
    return redacted


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

    # Conserver les clés API déjà présentes dans l'entrée moisson-shs si elles
    # ne sont pas re-fournies à cette exécution (évite de les effacer sur un
    # `setup` ultérieur sans clé).
    merged_env = dict(extra_env or {})
    if previous and isinstance(previous.get("env"), dict):
        for var in API_KEY_ENV_VARS:
            if var not in merged_env and previous["env"].get(var):
                merged_env[var] = previous["env"][var]

    config["mcpServers"][SERVER_KEY] = build_server_entry(
        python_exe, server_path, output_dir, cache_enabled,
        extra_env=merged_env or None,
    )

    # Sérialisation finale (réelle, écrite sur disque — clés en clair)
    serialized = json.dumps(config, indent=2, ensure_ascii=False)

    if dry_run:
        # Aperçu console avec clés masquées
        display_config = dict(config)
        display_config["mcpServers"] = dict(config["mcpServers"])
        display_config["mcpServers"][SERVER_KEY] = _redact_env(
            config["mcpServers"][SERVER_KEY]
        )
        serialized_display = json.dumps(
            display_config, indent=2, ensure_ascii=False
        )
        return (
            f"--- DRY RUN ---\n"
            f"Aucun fichier n'a été modifié. Aperçu du contenu qui aurait "
            f"été écrit dans {config_path} (clés API masquées) :\n\n"
            f"{serialized_display}"
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
    other = [k for k in servers if k != SERVER_KEY]
    if other:
        lines.append(f"   Autres serveurs MCP configurés : {', '.join(other)}")
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
            "Configure le serveur MCP moisson-shs dans Claude Desktop. "
            "Détecte automatiquement l'OS, le venv Python et le chemin du "
            "serveur. Préserve les autres serveurs MCP existants."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  moisson-shs setup
  moisson-shs setup --output-dir ~/Documents/corpus_shs
  moisson-shs setup --cache
  moisson-shs setup --dry-run
  moisson-shs setup --status
  moisson-shs setup --uninstall
        """,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Chemin personnalisé vers claude_desktop_config.json "
             "(défaut : auto-détection selon l'OS)",
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

    # Mode status
    if args.status:
        print(status(config_path))
        return 0

    # Mode uninstall
    if args.uninstall:
        print(uninstall(config_path, dry_run=args.dry_run))
        if not args.dry_run:
            print()
            print("⚠️ Redémarrez Claude Desktop pour que le changement soit pris en compte.")
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

    # Clés API à injecter dans le bloc env : flags / environnement, puis
    # saisie interactive des clés encore manquantes (sauf --no-prompt /
    # dry-run / terminal non interactif). Les clés déjà présentes dans la
    # config sont préservées par install() si on les saute (Entrée vide).
    api_key_env = collect_api_key_env(args)
    if not args.dry_run:
        api_key_env.update(
            prompt_missing_api_keys(api_key_env, no_prompt=args.no_prompt)
        )
    if api_key_env:
        print(f"🔑 Clés API injectées : {', '.join(sorted(api_key_env))}")

    # Installation
    print(install(
        config_path=config_path,
        python_exe=python_exe,
        server_path=server_path,
        output_dir=args.output_dir,
        cache_enabled=args.cache,
        dry_run=args.dry_run,
        extra_env=api_key_env,
    ))

    if not args.dry_run:
        print()
        print("🔄 Étape suivante : quitter et relancer **complètement** Claude Desktop")
        print("   (Cmd+Q sur macOS, fermer toutes les fenêtres puis relancer).")
        print()
        print("📚 Dans Claude Desktop, vous pouvez tester avec :")
        print('   « Liste mes corpus disponibles »')
        print('   « Moissonne les sources SHS sur "discours numérique", plafond 100 »')

    return 0


if __name__ == "__main__":
    sys.exit(main())
