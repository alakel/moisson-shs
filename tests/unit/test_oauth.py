"""
Tests du Sprint 12 — OAuth 2.1 pour le connecteur claude.ai (web).

Valide le flux serveur de bout en bout (la partie testable sans l'UI claude.ai) :
DCR → métadonnées → /authorize → consentement (mot de passe) → /token (PKCE) →
usage du jeton sur /mcp. Plus : unitaires du provider, coexistence Bearer+OAuth,
mauvais mot de passe, mauvais code_verifier (PKCE), persistance du store.

Loopback uniquement.
"""

import asyncio
import base64
import hashlib
import json
import secrets
import socket
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
import auth  # noqa: E402
from oauth_provider import FileOAuthProvider, ConsentError  # noqa: E402
from mcp.server.auth.provider import ProviderTokenVerifier  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


# === Provider unitaire ===

class TestProviderUnit:
    def _provider(self, tmp_path, pw="consent-pw"):
        return FileOAuthProvider(
            base_url="https://moisson.example.org",
            resource_url="https://moisson.example.org/mcp",
            password=pw, store_path=tmp_path / ".oauth" / "store.json")

    def test_register_and_get_client(self, tmp_path):
        from mcp.shared.auth import OAuthClientInformationFull
        p = self._provider(tmp_path)
        client = OAuthClientInformationFull(
            client_id="c1", redirect_uris=["https://cl/cb"],
            token_endpoint_auth_method="none")
        _run(p.register_client(client))
        got = _run(p.get_client("c1"))
        assert got is not None and got.client_id == "c1"
        # Persisté sur disque.
        assert (tmp_path / ".oauth" / "store.json").exists()

    def test_consent_wrong_password_rejected(self, tmp_path):
        from mcp.shared.auth import OAuthClientInformationFull
        from mcp.server.auth.provider import AuthorizationParams
        p = self._provider(tmp_path)
        client = OAuthClientInformationFull(
            client_id="c1", redirect_uris=["https://cl/cb"],
            token_endpoint_auth_method="none")
        _run(p.register_client(client))
        params = AuthorizationParams(
            state="s", scopes=[], code_challenge="ch",
            redirect_uri="https://cl/cb", redirect_uri_provided_explicitly=True,
            resource="https://moisson.example.org/mcp")
        url = _run(p.authorize(client, params))
        rid = parse_qs(urlparse(url).query)["rid"][0]
        with pytest.raises(ConsentError):
            p.approve_consent(rid, "MAUVAIS")
        # bon mot de passe → URL de redirection avec code
        redirect = p.approve_consent(rid, "consent-pw")
        assert "code=" in redirect and "state=s" in redirect

    def test_access_token_lifecycle(self, tmp_path):
        p = self._provider(tmp_path)
        tok = p._issue("c1", [], "https://moisson.example.org/mcp")
        at = _run(p.load_access_token(tok.access_token))
        assert at is not None and at.client_id == "c1"
        _run(p.revoke_token(at))
        assert _run(p.load_access_token(tok.access_token)) is None

    def test_store_survives_reload(self, tmp_path):
        from mcp.shared.auth import OAuthClientInformationFull
        store = tmp_path / ".oauth" / "store.json"
        p1 = FileOAuthProvider(base_url="https://x", resource_url="https://x/mcp",
                               password="pw", store_path=store)
        _run(p1.register_client(OAuthClientInformationFull(
            client_id="cZ", redirect_uris=["https://cl/cb"],
            token_endpoint_auth_method="none")))
        tok = p1._issue("cZ", [], "https://x/mcp")
        # Nouveau provider (redémarrage) → relit le store.
        p2 = FileOAuthProvider(base_url="https://x", resource_url="https://x/mcp",
                               password="pw", store_path=store)
        assert _run(p2.get_client("cZ")) is not None
        assert _run(p2.load_access_token(tok.access_token)) is not None


# === Flux OAuth e2e (uvicorn réel) ===

def _free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UvicornThread:
    def __init__(self, app, host, port):
        import uvicorn
        cfg = uvicorn.Config(app, host=host, port=port, log_level="warning",
                             log_config=None)
        self.server = uvicorn.Server(cfg)
        self._t = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self._t.start()

    def stop(self):
        self.server.should_exit = True
        self._t.join(timeout=10)


_PW = "consent-pw-12"
_STATIC = "static-tok-12"


@pytest.fixture(scope="module")
def oauth_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("oauth")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    provider = FileOAuthProvider(
        base_url=base, resource_url=f"{base}/mcp",
        password=_PW, store_path=tmp / ".oauth" / "store.json")
    composite = auth.CompositeTokenVerifier([
        auth.StaticTokenVerifier([_STATIC]),
        ProviderTokenVerifier(provider),
    ])
    app = mcp_server.build_http_app(
        path="/mcp", allowed_hosts=["127.0.0.1:*", "localhost:*"],
        token_verifier=composite, oauth_provider=provider,
        public_base_url=base, max_body=1_000_000)
    srv = _UvicornThread(app, "127.0.0.1", port)
    srv.start()
    deadline = time.time() + 15
    ok = False
    while time.time() < deadline:
        try:
            if httpx.get(base + "/healthz", timeout=1.0).status_code == 200:
                ok = True
                break
        except Exception:
            pass
        time.sleep(0.1)
    if not ok:
        srv.stop()
        pytest.fail("oauth_server non démarré")
    yield base
    srv.stop()


class TestOAuthDiscovery:
    def test_unauth_advertises_resource_metadata(self, oauth_server):
        r = httpx.post(oauth_server + "/mcp",
                       headers={"Content-Type": "application/json",
                                "Accept": "application/json, text/event-stream"},
                       content='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
                       timeout=5)
        assert r.status_code == 401
        www = " ".join(v for k, v in r.headers.items() if k.lower() == "www-authenticate")
        assert "resource_metadata" in www

    def test_as_metadata(self, oauth_server):
        r = httpx.get(oauth_server + "/.well-known/oauth-authorization-server",
                      timeout=5)
        assert r.status_code == 200
        meta = r.json()
        for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            assert k in meta

    def test_protected_resource_metadata(self, oauth_server):
        r = httpx.get(oauth_server + "/.well-known/oauth-protected-resource/mcp",
                      timeout=5)
        assert r.status_code == 200
        assert r.json().get("authorization_servers")


class TestOAuthFullFlow:
    def _register(self, base):
        r = httpx.post(base + "/register", json={
            "redirect_uris": ["https://client.example/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "test-client",
        }, timeout=5)
        assert r.status_code in (200, 201), r.text
        return r.json()["client_id"]

    def test_full_authorization_code_pkce_flow(self, oauth_server):
        base = oauth_server
        cid = self._register(base)
        verifier, challenge = _pkce()
        redirect_uri = "https://client.example/callback"

        # 1) /authorize → 302 vers la page de consentement
        r = httpx.get(base + "/authorize", params={
            "response_type": "code", "client_id": cid,
            "redirect_uri": redirect_uri, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "st-123",
            "resource": f"{base}/mcp",
        }, follow_redirects=False, timeout=5)
        assert r.status_code in (302, 307), r.text
        loc = r.headers["location"]
        assert "/oauth/consent" in loc
        rid = parse_qs(urlparse(loc).query)["rid"][0]

        # 2) consentement : mauvais mot de passe → 403
        bad = httpx.post(base + "/oauth/consent",
                         data={"rid": rid, "password": "NON"},
                         follow_redirects=False, timeout=5)
        assert bad.status_code == 403

        # 3) consentement : bon mot de passe → 302 vers redirect_uri?code&state
        good = httpx.post(base + "/oauth/consent",
                          data={"rid": rid, "password": _PW},
                          follow_redirects=False, timeout=5)
        assert good.status_code == 302
        cb = urlparse(good.headers["location"])
        q = parse_qs(cb.query)
        assert q["state"][0] == "st-123"
        code = q["code"][0]

        # 4) /token avec PKCE → access + refresh
        tok = httpx.post(base + "/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": cid,
            "code_verifier": verifier,
        }, timeout=5)
        assert tok.status_code == 200, tok.text
        body = tok.json()
        access = body["access_token"]
        assert body["token_type"].lower() == "bearer"
        assert "refresh_token" in body

        # 5) jeton OAuth utilisable sur /mcp (handshake complet)
        async def _use():
            from mcp.client.streamable_http import streamablehttp_client
            from mcp.client.session import ClientSession
            async with streamablehttp_client(
                base + "/mcp", headers={"Authorization": f"Bearer {access}"}
            ) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    tools = await s.list_tools()
                    return len(tools.tools)
        assert _run(_use()) == 8

    def test_wrong_pkce_verifier_rejected(self, oauth_server):
        base = oauth_server
        cid = self._register(base)
        _, challenge = _pkce()
        redirect_uri = "https://client.example/callback"
        r = httpx.get(base + "/authorize", params={
            "response_type": "code", "client_id": cid,
            "redirect_uri": redirect_uri, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "x",
            "resource": f"{base}/mcp",
        }, follow_redirects=False, timeout=5)
        rid = parse_qs(urlparse(r.headers["location"]).query)["rid"][0]
        good = httpx.post(base + "/oauth/consent", data={"rid": rid, "password": _PW},
                          follow_redirects=False, timeout=5)
        code = parse_qs(urlparse(good.headers["location"]).query)["code"][0]
        # Mauvais code_verifier → le SDK rejette (PKCE).
        tok = httpx.post(base + "/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": cid,
            "code_verifier": "mauvais-verifier-qui-ne-correspond-pas",
        }, timeout=5)
        assert tok.status_code == 400


class TestCoexistence:
    def test_static_bearer_still_works(self, oauth_server):
        # Le Bearer statique (Claude Desktop/Code) fonctionne EN MÊME TEMPS qu'OAuth.
        async def _use():
            from mcp.client.streamable_http import streamablehttp_client
            from mcp.client.session import ClientSession
            async with streamablehttp_client(
                oauth_server + "/mcp",
                headers={"Authorization": f"Bearer {_STATIC}"}
            ) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    return True
        assert _run(_use()) is True

    def test_bad_token_still_401(self, oauth_server):
        r = httpx.post(oauth_server + "/mcp",
                       headers={"Authorization": "Bearer n-importe-quoi",
                                "Content-Type": "application/json",
                                "Accept": "application/json, text/event-stream"},
                       content='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
                       timeout=5)
        assert r.status_code == 401
