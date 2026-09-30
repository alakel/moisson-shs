"""
Tests du Sprint 07 — Auth Bearer + sandbox des chemins + durcissement.

Couvre la DoD :
- `StaticTokenVerifier` : vide / mauvais / bon / non-ASCII / rotation ; jamais
  le secret réel dans `AccessToken.token`.
- `resolve_in_output_dir` : traversal `..`, absolu hors zone, symlink sortant
  → `PathSecurityError` ; chemin légitime → accepté ; `validate_suffix`.
- Outils sandboxés en mode HTTP : `corpus_analyze`/`corpus_filter`/
  `corpus_to_csl`/`corpus_merge`/`fetch_by_doi(add_to_corpus)` avec chemin
  hostile → rejet propre, aucune I/O hors OUTPUT_DIR.
- Middlewares de durcissement : `ContentLengthLimitMiddleware` (413),
  `RateLimitMiddleware` (429).
- Auth e2e (uvicorn réel) : `/healthz` public ; POST `/mcp` sans/avec mauvais
  jeton → 401 (+ WWW-Authenticate) ; handshake complet avec bon jeton → 200.
- Redaction : en HTTP, `call_tool` ne loggue pas les valeurs d'arguments.

Loopback uniquement (aucun réseau externe). Les tests e2e (uvicorn réel)
portent le marqueur `sockets`.
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
import mcp.types as types  # noqa: E402
import auth  # noqa: E402
from utils.sandbox import (  # noqa: E402
    resolve_in_output_dir, validate_suffix, PathSecurityError,
)


def _run(coro):
    return asyncio.run(coro)


# === StaticTokenVerifier ===

class TestStaticTokenVerifier:
    def test_no_tokens_rejects_everything(self):
        v = auth.StaticTokenVerifier([])
        assert v.has_tokens() is False
        assert _run(v.verify_token("anything")) is None
        assert _run(v.verify_token("")) is None

    def test_wrong_token_rejected(self):
        v = auth.StaticTokenVerifier(["bon-jeton"])
        assert _run(v.verify_token("mauvais")) is None

    def test_good_token_accepted_without_leaking_secret(self):
        v = auth.StaticTokenVerifier(["bon-jeton"])
        tok = _run(v.verify_token("bon-jeton"))
        assert tok is not None
        # Le secret réel n'est JAMAIS stocké : placeholder.
        assert tok.token == "***"
        assert tok.client_id == auth.PERSONAL_CLIENT_ID

    def test_non_ascii_token(self):
        v = auth.StaticTokenVerifier(["clé-é-ñ-🔑"])
        assert _run(v.verify_token("clé-é-ñ-🔑")) is not None
        assert _run(v.verify_token("cle-e-n")) is None

    def test_rotation_multiple_valid_tokens(self):
        v = auth.StaticTokenVerifier(["t1", "t2"])
        assert _run(v.verify_token("t1")) is not None
        assert _run(v.verify_token("t2")) is not None
        assert _run(v.verify_token("t3")) is None

    def test_load_tokens_from_env(self, monkeypatch):
        monkeypatch.setenv("MOISSON_API_TOKEN", "principal")
        monkeypatch.setenv("MOISSON_API_TOKENS", "rot1, rot2 ,")
        toks = auth.load_tokens()
        assert toks == ["principal", "rot1", "rot2"]

    def test_load_tokens_empty_env(self, monkeypatch):
        monkeypatch.delenv("MOISSON_API_TOKEN", raising=False)
        monkeypatch.delenv("MOISSON_API_TOKENS", raising=False)
        assert auth.load_tokens() == []


# === Sandbox primitive ===

class TestSandboxPrimitive:
    def test_relative_traversal_rejected(self, tmp_path):
        with pytest.raises(PathSecurityError):
            resolve_in_output_dir("../../etc/passwd", tmp_path)

    def test_absolute_outside_rejected(self, tmp_path):
        with pytest.raises(PathSecurityError):
            resolve_in_output_dir("/etc/passwd", tmp_path)

    def test_bare_name_confined(self, tmp_path):
        out = resolve_in_output_dir("corpus_x.json", tmp_path)
        assert out == (tmp_path.resolve() / "corpus_x.json")

    def test_absolute_inside_accepted(self, tmp_path):
        inside = tmp_path / "corpus_y.json"
        inside.write_text("[]", encoding="utf-8")
        out = resolve_in_output_dir(str(inside), tmp_path, must_exist=True)
        assert out == inside.resolve()

    def test_symlink_escaping_rejected(self, tmp_path):
        # Symlink DANS la zone pointant DEHORS → rejeté après résolution.
        secret = tmp_path.parent / "secret_outside.json"
        secret.write_text("TOPSECRET", encoding="utf-8")
        link = tmp_path / "innocent.json"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks non supportés sur cette plateforme")
        with pytest.raises(PathSecurityError):
            resolve_in_output_dir(str(link), tmp_path)

    def test_must_exist_rejects_missing(self, tmp_path):
        with pytest.raises(PathSecurityError):
            resolve_in_output_dir("absent.json", tmp_path, must_exist=True)

    def test_validate_suffix_ok(self):
        assert validate_suffix("_filtered") == "_filtered"
        assert validate_suffix("v2-final") == "v2-final"

    def test_validate_suffix_rejects_separators(self):
        for bad in ["/../evil", "a/b", "a.b", "x y", "", "..", "a;b"]:
            with pytest.raises(PathSecurityError):
                validate_suffix(bad)


# === Outils sandboxés en mode HTTP ===

def _error_text(result) -> str:
    """Texte d'un refus : erreur MCP (isError, OPT-07) portant le texte
    historique « Erreur : PathSecurityError… »."""
    assert isinstance(result, types.CallToolResult) and result.isError
    return result.content[0].text


def _make_items(n):
    return [{"itemType": "journalArticle", "title": f"T{i}", "date": "2024",
             "extra": "Sources: Test"} for i in range(n)]


class TestSandboxedToolsHttp:
    @pytest.fixture
    def http_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        return tmp_path

    def test_corpus_analyze_absolute_outside_rejected(self, http_mode):
        result = _run(mcp_server.call_tool(
            "corpus_analyze", {"corpus_path": "/etc/passwd"}
        ))
        text = _error_text(result)
        assert "PathSecurityError" in text or "zone autorisée" in text

    def test_corpus_filter_traversal_rejected(self, http_mode):
        result = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": "../../../etc/passwd"}
        ))
        text = _error_text(result)
        assert "PathSecurityError" in text or "Traversal" in text

    def test_corpus_filter_bad_suffix_rejected(self, http_mode):
        src = http_mode / "corpus_src.json"
        src.write_text(json.dumps(_make_items(3)), encoding="utf-8")
        # Instantané préalable : d'autres tests de la session peuvent avoir
        # laissé des fichiers « evil » dans la racine temporaire partagée
        # (ex. test_sprint_08 : evil.py) ; seul compte ce que CET appel écrit.
        before = set(http_mode.parent.glob("**/*evil*"))
        result = _run(mcp_server.call_tool(
            "corpus_filter",
            {"corpus_path": "corpus_src.json", "output_suffix": "/../evil"},
        ))
        text = _error_text(result)
        assert "PathSecurityError" in text or "Suffixe invalide" in text
        # Aucun fichier 'evil' écrit où que ce soit.
        assert set(http_mode.parent.glob("**/*evil*")) == before

    def test_corpus_to_csl_output_outside_rejected(self, http_mode):
        src = http_mode / "corpus_z.json"
        src.write_text(json.dumps(_make_items(2)), encoding="utf-8")
        result = _run(mcp_server.call_tool(
            "corpus_to_csl",
            {"corpus_path": "corpus_z.json",
             "output_path": "/tmp/evil_escape.json"},
        ))
        text = _error_text(result)
        assert "PathSecurityError" in text or "zone autorisée" in text
        assert not Path("/tmp/evil_escape.json").exists()

    def test_corpus_merge_input_outside_rejected(self, http_mode):
        result = _run(mcp_server.call_tool(
            "corpus_merge", {"corpus_paths": ["/etc/hosts", "/etc/passwd"]}
        ))
        text = _error_text(result)
        assert "PathSecurityError" in text or "zone autorisée" in text

    def test_legit_path_still_works_in_http(self, http_mode):
        # Un chemin légitime (basename sous OUTPUT_DIR) fonctionne en HTTP.
        src = http_mode / "corpus_ok.json"
        src.write_text(json.dumps(_make_items(4)), encoding="utf-8")
        result = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": "corpus_ok.json"}
        ))
        # Succès → tuple enrichi (pas une erreur).
        assert isinstance(result, tuple)
        blocks, structured = result
        assert structured["filtered_count"] == 4
        # Le fichier filtré est bien sous OUTPUT_DIR.
        assert (http_mode / "corpus_ok_filtered.json").exists()


# === Middlewares de durcissement (ASGI unitaire) ===

async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _drive(mw, headers, client=("1.2.3.4", 5)):
    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": headers, "client": client}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(m):
        sent.append(m)

    await mw(scope, receive, send)
    return sent


class TestContentLengthLimit:
    def test_oversize_rejected_413(self):
        mw = mcp_server.ContentLengthLimitMiddleware(_ok_app, max_body=1000)
        sent = _run(_drive(mw, [(b"content-length", b"99999999")]))
        assert sent[0]["status"] == 413

    def test_small_body_passes(self):
        mw = mcp_server.ContentLengthLimitMiddleware(_ok_app, max_body=1000)
        sent = _run(_drive(mw, [(b"content-length", b"10")]))
        assert sent[0]["status"] == 200


class TestRateLimit:
    def test_third_request_throttled_429(self):
        mw = mcp_server.RateLimitMiddleware(_ok_app, per_min=2)  # capacité 2
        s1 = _run(_drive(mw, []))
        s2 = _run(_drive(mw, []))
        s3 = _run(_drive(mw, []))
        assert s1[0]["status"] == 200
        assert s2[0]["status"] == 200
        assert s3[0]["status"] == 429
        # Retry-After présent
        hdrs = dict(s3[0]["headers"])
        assert b"retry-after" in hdrs

    def test_separate_ips_have_separate_buckets(self):
        mw = mcp_server.RateLimitMiddleware(_ok_app, per_min=1)
        a = _run(_drive(mw, [], client=("10.0.0.1", 1)))
        b = _run(_drive(mw, [], client=("10.0.0.2", 1)))
        assert a[0]["status"] == 200
        assert b[0]["status"] == 200  # IP différente → seau distinct


# === Auth e2e (uvicorn réel) ===

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


_TOKEN = "secret-token-sprint07"


@pytest.fixture(scope="module")
def auth_server():
    port = _free_port()
    verifier = auth.StaticTokenVerifier([_TOKEN])
    app = mcp_server.build_http_app(
        path="/mcp",
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        token_verifier=verifier,
        max_body=1_000_000,
    )
    srv = _UvicornThread(app, "127.0.0.1", port)
    srv.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        try:
            # /healthz est PUBLIC → ne nécessite pas de jeton.
            if httpx.get(base + "/healthz", timeout=1.0).status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.1)
    if not ready:
        srv.stop()
        pytest.fail("auth_server non démarré")
    yield base
    srv.stop()


_INIT_BODY = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "t", "version": "0"}},
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream",
                "Content-Type": "application/json"}


@pytest.mark.sockets
class TestAuthE2E:
    def test_healthz_is_public(self, auth_server):
        r = httpx.get(auth_server + "/healthz", timeout=5)
        assert r.status_code == 200
        # Public, sans auth (Sprint 11 : enrichi status/version/mode/uptime).
        assert r.json()["status"] == "ok"

    def test_no_auth_returns_401(self, auth_server):
        r = httpx.post(auth_server + "/mcp", headers=_MCP_HEADERS,
                       content=json.dumps(_INIT_BODY), timeout=5)
        assert r.status_code == 401
        assert "www-authenticate" in {k.lower() for k in r.headers}
        assert "invalid_token" in r.text

    def test_bad_token_returns_401(self, auth_server):
        h = {**_MCP_HEADERS, "Authorization": "Bearer mauvais-jeton"}
        r = httpx.post(auth_server + "/mcp", headers=h,
                       content=json.dumps(_INIT_BODY), timeout=5)
        assert r.status_code == 401

    def test_valid_token_full_handshake(self, auth_server):
        from mcp.client.streamable_http import streamable_http_client
        from mcp.client.session import ClientSession

        url = auth_server + "/mcp"

        async def _go():
            # Le client non déprécié ne prend plus d'en-têtes : l'en-tête
            # Authorization est porté par un httpx.AsyncClient fourni (délais
            # alignés sur les défauts MCP du SDK : 30 s, lecture SSE 300 s).
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {_TOKEN}"},
                timeout=httpx.Timeout(30.0, read=300.0),
                follow_redirects=True,
            ) as client, streamable_http_client(
                url, http_client=client
            ) as (read, write, _sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    # 9 outils historiques (corpus_resolve_dois inclus) et
                    # ceux du lot D (corpus_export, OPT-18…) : présence
                    # vérifiée, pas un nombre figé.
                    names = {t.name for t in tools.tools}
                    assert {"moisson_search", "corpus_analyze", "corpus_list",
                            "fetch_by_doi", "search_by_author", "corpus_merge",
                            "corpus_to_csl", "corpus_filter",
                            "corpus_resolve_dois", "corpus_export"} <= names

        _run(_go())


# === Redaction des logs en HTTP ===

class TestLogRedaction:
    @pytest.fixture(autouse=True)
    def isolated_output(self, tmp_path, monkeypatch):
        # corpus_list lit OUTPUT_DIR et, en HTTP, l'audit y écrit
        # (.audit/audit-*.jsonl) : jamais le output/ réel du projet.
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        return tmp_path

    def test_http_call_tool_does_not_log_arg_values(self, monkeypatch, caplog):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        with caplog.at_level("INFO", logger="moisson_mcp"):
            # corpus_list ignore ses arguments ; on en passe un « sensible ».
            _run(mcp_server.call_tool(
                "corpus_list", {"jeton_x": "VALEUR_SENSIBLE_42"}
            ))
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "corpus_list" in joined
        assert "jeton_x" in joined            # la CLÉ peut apparaître
        assert "VALEUR_SENSIBLE_42" not in joined  # la VALEUR jamais

    def test_stdio_call_tool_keeps_verbose_log(self, monkeypatch, caplog):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        with caplog.at_level("INFO", logger="moisson_mcp"):
            _run(mcp_server.call_tool("corpus_list", {"k": "v_local"}))
        joined = "\n".join(r.getMessage() for r in caplog.records)
        # En local de confiance, le log verbeux historique est conservé.
        assert "v_local" in joined
