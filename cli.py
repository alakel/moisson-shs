"""
Point d'entrée unifié de la commande `moisson-shs`.

Sous-commandes :
    moisson-shs setup ...      → configure le MCP dans Claude Desktop
    moisson-shs serve          → lance le serveur MCP en mode stdio
    moisson-shs search Q ...   → moisson sur la requête Q (alias : moisson-shs Q)
    moisson-shs version        → affiche la version

Le dispatcher est rétro-compatible : si le premier argument n'est pas
une sous-commande connue, il est interprété comme une requête de
recherche (compatible avec l'ancien `python main.py "query"`).
"""

from __future__ import annotations

import sys


SUBCOMMANDS = {"setup", "serve", "search", "token", "version", "--version", "-V", "help", "--help", "-h"}


def _print_top_level_help() -> None:
    print("""moisson-shs — Moissonneur SHS multi-sources (HAL, Isidore, OpenEdition,
                 Cairn, Persée, Theses.fr, Érudit) → Zotero JSON + CSL-JSON

Usage :
  moisson-shs setup [options]      Configure le serveur MCP dans Claude Desktop
  moisson-shs serve                Lance le serveur MCP en mode stdio (debug)
  moisson-shs serve --http         Lance le serveur MCP distant (Streamable HTTP)
  moisson-shs search QUERY ...     Moisson sur la requête QUERY
  moisson-shs QUERY ...            Alias rétro-compatible (search implicite)
  moisson-shs version              Affiche la version

Exemples :
  moisson-shs setup
  moisson-shs setup --output-dir ~/Documents/corpus_shs
  moisson-shs setup --status
  moisson-shs serve --http --host 127.0.0.1 --port 8000
  moisson-shs search "discours numérique" --max 500 --workers 2 --cache
  moisson-shs "Foucault" --from-date 2020 --to-date 2024

Aide détaillée par sous-commande :
  moisson-shs setup --help
  moisson-shs serve --help
  moisson-shs search --help
""")


def _get_version() -> str:
    try:
        from importlib.metadata import version
        return version("moisson-shs")
    except Exception:
        return "0.3.0 (source / non installé)"


def _dispatch_token(token_argv: list[str]) -> int:
    """
    Sous-commande ``token {generate,rotate,status,revoke}``.

    Gère les jetons Bearer du serveur distant : le **secret** n'est affiché
    qu'une fois (génération/rotation) ; seul son sha256 est stocké (0600).
    """
    import argparse
    import remote_auth

    p = argparse.ArgumentParser(prog="moisson-shs token")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("generate", help="Génère un jeton (remplace le store).")
    sub.add_parser("rotate", help="Ajoute un jeton (recouvrement ; révoquer l'ancien après).")
    sub.add_parser("status", help="État (empreintes + dates, sans secret).")
    rev = sub.add_parser("revoke", help="Révoque un jeton (par empreinte) ou tous.")
    rev.add_argument("--fingerprint", help="Préfixe d'empreinte à révoquer (défaut : tous).")
    a = p.parse_args(token_argv)

    if a.action == "generate":
        secret = remote_auth.generate()
        print("Jeton généré (AFFICHÉ UNE SEULE FOIS — copiez-le maintenant) :\n")
        print(f"    {secret}\n")
        print(f"Empreinte sha256 stockée dans : {remote_auth.store_path()}")
        print("Injectez-le côté serveur via MOISSON_API_TOKEN (.env / conteneur).")
        return 0

    if a.action == "rotate":
        secret = remote_auth.rotate()
        print("Nouveau jeton (recouvrement — l'ancien reste valide) :\n")
        print(f"    {secret}\n")
        print("Migrez les clients, puis révoquez l'ancien : "
              "moisson-shs token revoke --fingerprint <empreinte>")
        return 0

    if a.action == "status":
        import json
        st = remote_auth.status()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    if a.action == "revoke":
        n = remote_auth.revoke(fingerprint=a.fingerprint)
        cible = (f"empreinte {a.fingerprint}" if a.fingerprint else "TOUS les jetons")
        print(f"{n} jeton(s) révoqué(s) ({cible}).")
        return 0

    return 0


def _dispatch_setup_remote(setup_argv: list[str]) -> int:
    """
    ``setup --remote <url> [--token T] [--name N]`` : imprime la config CLIENT
    distante (url + en-tête Bearer), copiable dans `.mcp.json`/Claude Desktop.
    """
    import argparse
    import json
    import os
    from setup_claude_desktop import build_remote_client_entry

    p = argparse.ArgumentParser(prog="moisson-shs setup --remote")
    p.add_argument("--remote", required=True, metavar="URL",
                   help="URL HTTPS du endpoint MCP distant (…/mcp).")
    p.add_argument("--token", default=os.environ.get("MOISSON_API_TOKEN"),
                   help="Jeton Bearer (défaut : MOISSON_API_TOKEN).")
    p.add_argument("--name", default="moisson-shs-remote",
                   help="Nom de l'entrée mcpServers.")
    a = p.parse_args(setup_argv)

    if not a.token:
        print("Erreur : aucun jeton. Passez --token <T> ou exportez "
              "MOISSON_API_TOKEN.", file=sys.stderr)
        return 2

    entry = build_remote_client_entry(a.remote, a.token, name=a.name)
    print("Config client distante (à coller dans .mcp.json ou Claude Desktop) :\n")
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    print("\nNote : pour un connecteur distant claude.ai, renseignez l'URL et "
          "l'en-tête Authorization dans l'UI dédiée. OAuth serait requis pour un "
          "connecteur PUBLIC multi-utilisateurs (hors périmètre).")
    return 0


def _dispatch_serve(serve_argv: list[str]) -> int:
    """
    Dispatch de la sous-commande ``serve``.

    Sans ``--http`` : lance le serveur MCP en mode **stdio** (défaut historique,
    Claude Desktop local). Avec ``--http`` : lance le serveur **distant**
    Streamable HTTP (uvicorn + Starlette), pensé pour les agents distants.
    """
    import argparse
    import os

    p = argparse.ArgumentParser(
        prog="moisson-shs serve",
        description=(
            "Lance le serveur MCP. Défaut : stdio (Claude Desktop local). "
            "--http : serveur distant Streamable HTTP."
        ),
    )
    p.add_argument(
        "--http", action="store_true",
        help="Active le transport HTTP distant (Streamable HTTP) au lieu de stdio.",
    )
    p.add_argument(
        "--host", default=os.environ.get("MOISSON_HTTP_HOST", "127.0.0.1"),
        help="Interface d'écoute uvicorn (défaut 127.0.0.1).",
    )
    p.add_argument(
        "--port", type=int, default=int(os.environ.get("MOISSON_HTTP_PORT", "8000")),
        help="Port d'écoute uvicorn (défaut 8000).",
    )
    p.add_argument(
        "--path", default=os.environ.get("MOISSON_HTTP_PATH", "/mcp"),
        help="Chemin de la route Streamable HTTP (défaut /mcp).",
    )
    p.add_argument(
        "--public-base-url",
        default=os.environ.get("MOISSON_PUBLIC_BASE_URL"),
        metavar="URL",
        help="Base HTTPS publique pour les URLs de téléchargement "
             "(ex. https://moisson.example.org).",
    )
    p.add_argument(
        "--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
        help="Host autorisé (anti DNS-rebinding). Répétable. Obligatoire si "
             "bind non-loopback. Motif host:* accepté.",
    )
    p.add_argument(
        "--allowed-origin", action="append", default=[], metavar="ORIGIN",
        help="Origin autorisé (anti DNS-rebinding). Répétable.",
    )
    p.add_argument(
        "--json-response", action="store_true",
        help="Réponses JSON simples (désactive le flux SSE de progression).",
    )
    p.add_argument(
        "--stateless", action="store_true",
        help="Mode sans session (désactive resumabilité ET progression SSE — "
             "déconseillé).",
    )
    a = p.parse_args(serve_argv)

    if a.http:
        from mcp_server import serve_http
        try:
            serve_http(
                a.host, a.port,
                path=a.path,
                json_response=a.json_response,
                stateless=a.stateless,
                allowed_hosts=a.allowed_host or None,
                allowed_origins=a.allowed_origin or None,
                public_base_url=a.public_base_url,
            )
        except KeyboardInterrupt:
            print("\n[moisson-shs serve --http] interrompu par l'utilisateur",
                  file=sys.stderr)
        return 0

    # Mode stdio (défaut, équivalent : python mcp_server.py)
    import asyncio
    from mcp_server import run_stdio
    try:
        asyncio.run(run_stdio())
    except KeyboardInterrupt:
        print("\n[moisson-shs serve] interrompu par l'utilisateur", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée appelé par l'entry_point pip."""
    argv = argv if argv is not None else sys.argv[1:]

    if not argv:
        _print_top_level_help()
        return 0

    first = argv[0]

    if first in ("--help", "-h", "help"):
        _print_top_level_help()
        return 0

    if first in ("--version", "-V", "version"):
        print(f"moisson-shs {_get_version()}")
        return 0

    if first == "setup":
        # `setup --remote <url>` émet la config CLIENT distante (intercepté ici) ;
        # sinon délégué à l'installeur Claude Desktop LOCAL.
        if "--remote" in argv[1:]:
            return _dispatch_setup_remote(argv[1:])
        from setup_claude_desktop import main as setup_main
        return setup_main(argv[1:])

    if first == "serve":
        return _dispatch_serve(argv[1:])

    if first == "token":
        return _dispatch_token(argv[1:])

    if first == "search":
        # Décale les args pour qu'argparse de main.py les reçoive
        from main import main as cli_main
        old_argv = sys.argv
        try:
            sys.argv = ["moisson-shs search"] + argv[1:]
            return cli_main()
        finally:
            sys.argv = old_argv

    # Cas par défaut : on suppose que c'est une requête de recherche
    # (rétro-compatibilité avec l'ancien `python main.py "query"`)
    from main import main as cli_main
    old_argv = sys.argv
    try:
        sys.argv = ["moisson-shs"] + argv
        return cli_main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    sys.exit(main())
