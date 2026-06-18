"""
Authentification Bearer du serveur MCP distant (mode HTTP).

Le mode HTTP est authentifié par un **jeton Bearer statique** (clé API),
comparé à `MOISSON_API_TOKEN` et/ou `MOISSON_API_TOKENS` (liste, pour la
rotation sans coupure). Pas d'OAuth (cf. plan directeur §9.1 — hors périmètre).

Points de sécurité (correctifs revue) :
- Comparaison **à temps constant** sur des **bytes** (`secrets.compare_digest`),
  robuste aux jetons non-ASCII et sans oracle de timing.
- **Pas de court-circuit** : la boucle parcourt TOUTES les entrées (ne révèle
  pas le nombre de jetons valides par le timing).
- Le jeton réel **n'est jamais stocké** dans `AccessToken.token` (placeholder
  `"***"`) → pas de fuite si un log dumpe l'objet token.

Ce module est inerte en mode stdio (jamais importé par la boucle stdio).
"""

from __future__ import annotations

import os
import secrets

from mcp.server.auth.provider import AccessToken, TokenVerifier


# Identité logique du client mono-utilisateur (sert de client_id pour les logs
# et l'audit — jamais le secret).
PERSONAL_CLIENT_ID = "moisson-personal"


def load_tokens() -> list[str]:
    """
    Charge les jetons valides depuis l'environnement.

    Sources cumulées (rotation) :
    - `MOISSON_API_TOKEN` : jeton principal (un seul).
    - `MOISSON_API_TOKENS` : liste séparée par virgules (jetons additionnels
      valides simultanément, le temps de migrer les clients).

    Returns:
        Liste des jetons valides (chaînes), sans entrées vides. Peut être vide
        (⇒ le serveur HTTP doit refuser de démarrer, cf. `serve_http`).
    """
    tokens: list[str] = []
    primary = os.environ.get("MOISSON_API_TOKEN", "").strip()
    if primary:
        tokens.append(primary)
    extra = os.environ.get("MOISSON_API_TOKENS", "")
    tokens += [t.strip() for t in extra.split(",") if t.strip()]
    return tokens


class StaticTokenVerifier(TokenVerifier):
    """
    `TokenVerifier` du SDK comparant le jeton reçu à une liste statique.

    Les jetons sont lus **à la construction** (au démarrage du serveur). Une
    rotation se fait en redémarrant avec `MOISSON_API_TOKENS` mis à jour.
    """

    def __init__(self, tokens: list[str] | None = None):
        raw = tokens if tokens is not None else load_tokens()
        # Encodés en bytes une fois pour `compare_digest` (robuste non-ASCII).
        self._tokens: list[bytes] = [t.encode("utf-8") for t in raw]

    def has_tokens(self) -> bool:
        """True si au moins un jeton est configuré."""
        return bool(self._tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Valide le jeton Bearer reçu.

        Returns:
            `AccessToken` (avec placeholder `"***"`, jamais le secret réel) si
            le jeton correspond à une entrée valide ; `None` sinon (⇒ 401).
        """
        got = token.encode("utf-8")
        matched = False
        # Parcours COMPLET (pas de break) : ne révèle pas le nombre de jetons.
        for candidate in self._tokens:
            if secrets.compare_digest(got, candidate):
                matched = True
        if not self._tokens or not matched:
            return None
        # AccessToken est strictement par mots-clés (pydantic). On NE stocke
        # PAS le jeton réel (anti-fuite logs) : placeholder.
        return AccessToken(token="***", client_id=PERSONAL_CLIENT_ID, scopes=[])


class CompositeTokenVerifier(TokenVerifier):
    """
    Combine plusieurs `TokenVerifier` : essaie chacun, renvoie le premier
    résultat non nul. Permet d'accepter SIMULTANÉMENT le Bearer statique
    (Claude Desktop/Code) et les jetons émis par OAuth (claude.ai web).
    """

    def __init__(self, verifiers: list):
        self._verifiers = [v for v in verifiers if v is not None]

    def has_tokens(self) -> bool:
        return any(getattr(v, "has_tokens", lambda: True)()
                   for v in self._verifiers)

    async def verify_token(self, token: str) -> AccessToken | None:
        for v in self._verifiers:
            res = await v.verify_token(token)
            if res is not None:
                return res
        return None
