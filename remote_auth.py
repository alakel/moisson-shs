"""
Gestion locale des jetons Bearer du serveur MCP distant (CLI `moisson-shs token`).

Le **secret** n'est affiché qu'**une seule fois** (à la génération) ; seul son
**sha256** est persisté, dans un fichier en permissions `0600`. Le serveur,
lui, s'authentifie via `MOISSON_API_TOKEN(S)` (secret en clair injecté dans
l'environnement du conteneur) — c'est `auth.StaticTokenVerifier`. Pour un
déploiement **fichier** (sans secret en env), `FileTokenVerifier` compare le
sha256 du jeton reçu aux empreintes stockées.

Rotation : `rotate` ajoute une nouvelle empreinte **sans retirer l'ancienne**
(fenêtre de recouvrement — les deux jetons valides le temps de migrer les
clients) ; `revoke <empreinte>` retire ensuite l'ancienne.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from mcp.server.auth.provider import AccessToken, TokenVerifier

from auth import PERSONAL_CLIENT_ID


def store_path() -> Path:
    """Chemin du fichier d'empreintes (surchargeable par MOISSON_TOKEN_STORE)."""
    env = os.environ.get("MOISSON_TOKEN_STORE")
    if env:
        return Path(env)
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "moisson_shs" / "tokens.json"


def _sha256(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    try:
        os.chmod(path, 0o600)   # secret-adjacent : permissions restrictives
    except OSError:
        pass


def generate(path: Path | None = None, *, replace: bool = True) -> str:
    """
    Génère un jeton, persiste son **sha256** (0600), renvoie le **secret**
    (à afficher une seule fois). `replace=True` repart d'un store vide.
    """
    path = path or store_path()
    secret = secrets.token_urlsafe(32)
    entries = [] if replace else _load(path)
    entries.append({
        "sha256": _sha256(secret),
        "added": datetime.now().isoformat(timespec="seconds"),
    })
    _save(path, entries)
    return secret


def rotate(path: Path | None = None) -> str:
    """
    Ajoute un NOUVEAU jeton **sans retirer l'ancien** (fenêtre de recouvrement).
    Renvoie le nouveau secret. Révoquer l'ancien après migration des clients.
    """
    return generate(path, replace=False)


def status(path: Path | None = None) -> dict:
    """État (sans révéler aucun secret) : nombre + empreintes courtes + dates."""
    path = path or store_path()
    entries = _load(path)
    return {
        "store": str(path),
        "count": len(entries),
        "tokens": [
            {"fingerprint": e["sha256"][:12], "added": e.get("added")}
            for e in entries
        ],
    }


def revoke(path: Path | None = None, *, fingerprint: str | None = None) -> int:
    """
    Révoque un jeton par empreinte (préfixe), ou TOUS si `fingerprint` est None.
    Renvoie le nombre d'empreintes retirées.
    """
    path = path or store_path()
    entries = _load(path)
    if fingerprint is None:
        n = len(entries)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return n
    kept = [e for e in entries if not e["sha256"].startswith(fingerprint)]
    removed = len(entries) - len(kept)
    _save(path, kept)
    return removed


class FileTokenVerifier(TokenVerifier):
    """
    `TokenVerifier` comparant `sha256(jeton reçu)` aux empreintes du fichier.

    Alternative à `StaticTokenVerifier` pour un déploiement **fichier** (pas de
    secret en clair dans l'environnement). Les empreintes sont lues à la
    construction.
    """

    def __init__(self, path: Path | None = None):
        entries = _load(path or store_path())
        self._hashes = [e["sha256"] for e in entries]

    def has_tokens(self) -> bool:
        return bool(self._hashes)

    async def verify_token(self, token: str) -> AccessToken | None:
        got = _sha256(token)
        matched = False
        for h in self._hashes:                 # parcours complet (pas de court-circuit)
            if secrets.compare_digest(got, h):
                matched = True
        if not self._hashes or not matched:
            return None
        return AccessToken(token="***", client_id=PERSONAL_CLIENT_ID, scopes=[])
