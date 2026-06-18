"""
Serveur d'autorisation OAuth 2.1 minimal pour le connecteur **claude.ai (web)**.

claude.ai n'accepte que **OAuth 2.1** pour les connecteurs distants (pas de
jeton Bearer statique). Ce module fournit un `OAuthAuthorizationServerProvider`
mono-utilisateur branché sur les handlers du SDK (`create_auth_routes`) qui,
eux, gèrent tout le protocole sensible : **PKCE** (vérif S256 dans le token
handler), cohérence `redirect_uri`, **DCR** (RFC 7591), métadonnées. On
n'implémente donc QUE :
- le **stockage** (clients / codes / jetons), persisté en JSON dans le volume ;
- une **page de consentement** protégée par mot de passe (`MOISSON_OAUTH_PASSWORD`)
  — sinon n'importe qui connaissant l'URL obtiendrait un jeton.

Le Bearer statique (`auth.StaticTokenVerifier`) reste disponible en parallèle
pour Claude Desktop/Code ; OAuth est activé par `MOISSON_OAUTH_ENABLED=1`.
"""

from __future__ import annotations

import html
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


ACCESS_TTL = 3600              # 1 h
REFRESH_TTL = 30 * 24 * 3600   # 30 j
CODE_TTL = 300                 # 5 min
PENDING_TTL = 600              # 10 min


class ConsentError(Exception):
    """Échec du consentement (requête inconnue/expirée ou mot de passe faux)."""


class FileOAuthProvider(OAuthAuthorizationServerProvider):
    """Provider OAuth 2.1 mono-utilisateur, stockage fichier (volume Docker)."""

    def __init__(self, *, base_url: str, resource_url: str, password: str,
                 store_path: Path):
        self._base = base_url.rstrip("/")
        self._resource = resource_url
        self._password = password or ""
        self._store_path = Path(store_path)
        # Persistés : clients, access, refresh. Éphémères : codes, pending.
        self._clients: dict[str, dict] = {}
        self._access: dict[str, dict] = {}
        self._refresh: dict[str, dict] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._pending: dict[str, dict] = {}
        self._load()

    # --- persistance ---

    def _load(self) -> None:
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
            self._clients = data.get("clients", {})
            self._access = data.get("access", {})
            self._refresh = data.get("refresh", {})
        except (json.JSONDecodeError, OSError):
            pass

    def _save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "clients": self._clients,
                "access": self._access,
                "refresh": self._refresh,
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._store_path)
            self._store_path.chmod(0o600)
        except OSError:
            pass

    # --- clients (DCR) ---

    async def get_client(self, client_id: str):
        data = self._clients.get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info.model_dump(mode="json")
        self._save()

    # --- autorisation (consentement) ---

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        """Stocke la requête et renvoie l'URL de la page de consentement."""
        rid = secrets.token_urlsafe(24)
        self._pending[rid] = {
            "client_id": client.client_id,
            "params": params,
            "exp": time.time() + PENDING_TTL,
        }
        return f"{self._base}/oauth/consent?{urlencode({'rid': rid})}"

    def consent_page_html(self, rid: str) -> str:
        """Page de consentement minimale (mot de passe)."""
        rid_s = html.escape(rid)
        return (
            "<!doctype html><html lang=fr><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Autoriser moisson-shs</title></head>"
            "<body style='font-family:sans-serif;max-width:30rem;margin:4rem auto;padding:0 1rem'>"
            "<h2>Connexion à moisson-shs (MCP)</h2>"
            "<p>Un client (claude.ai) demande l'accès à votre serveur de moisson "
            "bibliographique. Entrez le mot de passe d'autorisation pour approuver.</p>"
            "<form method=post action='/oauth/consent'>"
            f"<input type=hidden name=rid value='{rid_s}'>"
            "<p><input type=password name=password autofocus required "
            "style='width:100%;padding:.6rem;font-size:1rem' "
            "placeholder='Mot de passe d&#39;autorisation'></p>"
            "<button type=submit style='padding:.6rem 1.2rem;font-size:1rem'>"
            "Autoriser</button></form></body></html>"
        )

    def approve_consent(self, rid: str, password: str) -> str:
        """Valide le consentement et renvoie l'URL de redirection (code+state)."""
        pending = self._pending.get(rid)
        if not pending or pending["exp"] < time.time():
            raise ConsentError("Requête d'autorisation inconnue ou expirée.")
        if not self._password or not secrets.compare_digest(
                password or "", self._password):
            raise ConsentError("Mot de passe incorrect.")
        params: AuthorizationParams = pending["params"]
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=pending["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        del self._pending[rid]
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state)

    # --- codes ---

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str):
        code = self._codes.get(authorization_code)
        if not code or code.client_id != client.client_id:
            return None
        if code.expires_at and code.expires_at < time.time():
            self._codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                         authorization_code: AuthorizationCode):
        # Le SDK a DÉJÀ vérifié PKCE (S256) et la cohérence redirect_uri.
        self._codes.pop(authorization_code.code, None)  # usage unique
        return self._issue(client.client_id, authorization_code.scopes,
                           authorization_code.resource)

    # --- refresh ---

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str):
        data = self._refresh.get(refresh_token)
        if not data or data.get("client_id") != client.client_id:
            return None
        rt = RefreshToken.model_validate(data)
        if rt.expires_at and rt.expires_at < time.time():
            self._refresh.pop(refresh_token, None)
            self._save()
            return None
        return rt

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                    refresh_token: RefreshToken,
                                    scopes: list[str]):
        # Rotation : on révoque l'ancien refresh et on en émet un nouveau.
        self._refresh.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes,
                           self._resource)

    # --- access ---

    async def load_access_token(self, token: str):
        data = self._access.get(token)
        if not data:
            return None
        at = AccessToken.model_validate(data)
        if at.expires_at and at.expires_at < time.time():
            self._access.pop(token, None)
            self._save()
            return None
        return at

    async def revoke_token(self, token) -> None:
        t = getattr(token, "token", None)
        if t:
            self._access.pop(t, None)
            self._refresh.pop(t, None)
            self._save()

    # --- helpers ---

    def _issue(self, client_id: str, scopes: list[str], resource) -> OAuthToken:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self._access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes or [],
            expires_at=int(now + ACCESS_TTL),
            resource=str(resource) if resource else None,
        ).model_dump(mode="json")
        self._refresh[refresh] = RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes or [],
            expires_at=int(now + REFRESH_TTL),
        ).model_dump(mode="json")
        self._save()
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
            scope=" ".join(scopes) if scopes else None, refresh_token=refresh,
        )
