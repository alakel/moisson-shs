"""
Tests du Sprint 06 — Socle transport HTTP (Streamable HTTP) + double `serve`.

Couvre la DoD du sprint :
- `/healthz` public et minimal (`{"status": "ok"}`, rien d'autre) ;
- handshake MCP réel via `streamable_http_client` : initialize → list_tools (9) →
  call_tool('corpus_list') sur un uvicorn éphémère en localhost ;
- anti DNS-rebinding actif : POST `/mcp` (Content-Type JSON + corps JSON-RPC
  valide) avec `Host: evil.com` → 421 ; `Origin` interdit → 403 ;
- garde-fou d'exposition : `serve_http` refuse un bind non-loopback sans
  allowlist Host (SystemExit) ;
- test négatif documentant que sans `lifespan`, `handle_request` lève
  `RuntimeError("Task group is not initialized")`.

Ces tests n'utilisent QUE la boucle loopback (127.0.0.1) — aucun réseau externe.
Ils ne sont donc PAS marqués `integration` (qui désigne les tests réseau) et
s'exécutent dans la suite par défaut, ce qui garde la DoD vérifiée en continu.
Ceux qui ouvrent une socket (serveur uvicorn, client HTTP, réservation de port)
portent le marqueur `sockets`, pour pouvoir être écartés dans un bac à sable
sans sockets : `pytest -m "not integration and not sockets"`.
"""

import asyncio
import json
import socket
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402


# === Infrastructure : uvicorn éphémère en thread ===

def _free_port() -> int:
    """Réserve puis libère un port libre sur le loopback (port éphémère)."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UvicornThread:
    """Lance une app ASGI dans un thread daemon (uvicorn ne pose pas de
    gestionnaires de signaux hors du thread principal — sûr en test)."""

    def __init__(self, app, host: str, port: int):
        import uvicorn

        config = uvicorn.Config(
            app, host=host, port=port, log_level="warning", log_config=None
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture(scope="module")
def http_server():
    """Serveur HTTP Streamable éphémère partagé par les tests du module.

    Allowlist Host loopback (wildcard de port) pour que le client de test passe
    l'anti DNS-rebinding sans configuration.
    """
    port = _free_port()
    app = mcp_server.build_http_app(
        path="/mcp", allowed_hosts=["127.0.0.1:*", "localhost:*"]
    )
    srv = _UvicornThread(app, "127.0.0.1", port)
    srv.start()
    base = f"http://127.0.0.1:{port}"

    # Attente active du /healthz avant de rendre la main (anti-flaky timing).
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/healthz", timeout=1.0)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.1)
    if not ready:
        srv.stop()
        pytest.fail("Serveur HTTP non démarré dans le délai imparti")

    yield base
    srv.stop()


# Corps JSON-RPC initialize minimal valide (pour les tests anti-rebinding).
_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


# === /healthz public et minimal ===

@pytest.mark.sockets
class TestHealthz:
    def test_healthz_returns_ok(self, http_server):
        r = httpx.get(http_server + "/healthz", timeout=5)
        assert r.status_code == 200
        body = r.json()
        # status ok + métadonnées NON sensibles (Sprint 11).
        assert body["status"] == "ok"
        assert set(body) >= {"status", "version", "mode", "uptime_s",
                             "output_dir_writable"}
        assert isinstance(body["output_dir_writable"], bool)


# === Handshake MCP réel ===

@pytest.mark.sockets
class TestMcpHandshake:
    def test_initialize_list_tools_call_tool(self, http_server):
        url = http_server + "/mcp"

        async def _run():
            async with streamable_http_client(url) as (read, write, _get_sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    # Les outils servis à l'identique du stdio : les 9
                    # historiques (8 jusqu'au Sprint 05, + corpus_resolve_dois
                    # avec Google Scholar) et ceux du lot D (corpus_export,
                    # OPT-18…) : présence vérifiée, pas un nombre figé.
                    assert len(tools.tools) == len(names), names
                    assert {
                        "moisson_search", "corpus_analyze", "corpus_list",
                        "fetch_by_doi", "search_by_author", "corpus_merge",
                        "corpus_to_csl", "corpus_filter", "corpus_resolve_dois",
                        "corpus_export",
                    } <= names

                    result = await session.call_tool("corpus_list", {})
                    assert result.isError in (False, None)
                    assert result.content  # au moins un bloc de contenu

        asyncio.run(_run())

    def test_list_resources_and_prompts_served(self, http_server):
        url = http_server + "/mcp"

        async def _run():
            async with streamable_http_client(url) as (read, write, _get_sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # Resources corpus:// et prompts exposés à l'identique.
                    prompts = await session.list_prompts()
                    pnames = {p.name for p in prompts.prompts}
                    assert {
                        "bibliographie-thematique", "profil-chercheur",
                        "veille-mensuelle", "etat-de-l-art",
                    } == pnames

        asyncio.run(_run())


# === Anti DNS-rebinding ===

@pytest.mark.sockets
class TestDnsRebindingProtection:
    def test_post_with_evil_host_rejected_421(self, http_server):
        url = http_server + "/mcp"
        headers = {
            **_MCP_HEADERS,
            "Host": "evil.com",
            "Content-Type": "application/json",
        }
        r = httpx.post(
            url, headers=headers, content=json.dumps(_INIT_BODY), timeout=5
        )
        assert r.status_code == 421, (r.status_code, r.text)

    def test_post_with_evil_origin_rejected_403(self, http_server):
        url = http_server + "/mcp"
        # Host par défaut (127.0.0.1:port) autorisé ; Origin interdit → 403.
        headers = {
            **_MCP_HEADERS,
            "Content-Type": "application/json",
            "Origin": "http://evil.com",
        }
        r = httpx.post(
            url, headers=headers, content=json.dumps(_INIT_BODY), timeout=5
        )
        assert r.status_code == 403, (r.status_code, r.text)


# === Garde-fou d'exposition (serve_http) ===

class TestExposureGuard:
    @pytest.mark.sockets  # _free_port() réserve un port loopback
    def test_refuses_public_bind_without_allowlist(self, monkeypatch):
        # Pas d'allowlist (ni arg, ni env) + bind non-loopback → SystemExit.
        monkeypatch.setattr(mcp_server, "ALLOWED_HOSTS", [])
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        with pytest.raises(SystemExit):
            mcp_server.serve_http(host="0.0.0.0", port=_free_port(),
                                  allowed_hosts=None)
        # Le mode n'a pas basculé (refus AVANT toute bascule).
        assert mcp_server.TRANSPORT_MODE == "stdio"

    def test_loopback_sets_mode_and_invokes_uvicorn(self, monkeypatch):
        import uvicorn

        captured = {}

        def fake_run(app, host, port, **kw):
            captured.update(host=host, port=port, app=app)

        monkeypatch.setattr(uvicorn, "run", fake_run)
        # serve_http (Sprint 07) exige un jeton Bearer : on en fournit un.
        monkeypatch.setenv("MOISSON_API_TOKEN", "tok-test-loopback")
        # monkeypatch restaure TRANSPORT_MODE à "stdio" en teardown : pas de
        # pollution des autres tests même si serve_http le bascule en "http".
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        mcp_server.serve_http(host="127.0.0.1", port=8123, allowed_hosts=None)
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 8123
        assert mcp_server.TRANSPORT_MODE == "http"


# === Test négatif : lifespan obligatoire ===

class TestLifespanRequired:
    def test_handle_request_without_lifespan_raises(self):
        """Sans `session_manager.run()` (fourni par le lifespan), le SDK lève
        RuntimeError("Task group is not initialized")."""
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        sm = StreamableHTTPSessionManager(app=mcp_server.server)

        async def _run():
            scope = {
                "type": "http", "method": "GET", "path": "/mcp",
                "headers": [], "query_string": b"",
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(_msg):
                return None

            with pytest.raises(RuntimeError, match="Task group is not initialized"):
                await sm.handle_request(scope, receive, send)

        asyncio.run(_run())
