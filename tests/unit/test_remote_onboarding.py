"""
Tests du Sprint 11 — Onboarding distant, CLI jetons, observabilité.

Couvre la DoD :
- `remote_auth` : generate/rotate/status/revoke (sha256 0600, jamais le secret),
  `FileTokenVerifier`.
- `build_remote_client_entry` : forme config client (url + Bearer, sans chemin).
- Logging JSON structuré + **redaction** des secrets.
- Audit JSONL par appel d'outil (clés d'args, pas les valeurs) — HTTP only.
- Métriques + `/healthz` enrichi (public) + `/metrics` (derrière auth).
"""

import asyncio
import json
import logging
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
import remote_auth  # noqa: E402
import auth  # noqa: E402
from setup_claude_desktop import build_remote_client_entry  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# === remote_auth : gestion des jetons ===

class TestTokenManagement:
    def test_generate_stores_hash_not_secret(self, tmp_path):
        store = tmp_path / "tokens.json"
        secret = remote_auth.generate(store)
        assert secret and len(secret) >= 20
        raw = store.read_text(encoding="utf-8")
        # Le secret n'est JAMAIS dans le fichier ; seul son sha256.
        assert secret not in raw
        assert remote_auth._sha256(secret) in raw

    def test_store_permissions_0600(self, tmp_path):
        store = tmp_path / "tokens.json"
        remote_auth.generate(store)
        mode = store.stat().st_mode & 0o777
        assert mode == 0o600

    def test_status_hides_secret(self, tmp_path):
        store = tmp_path / "tokens.json"
        secret = remote_auth.generate(store)
        st = remote_auth.status(store)
        assert st["count"] == 1
        blob = json.dumps(st)
        assert secret not in blob
        assert st["tokens"][0]["fingerprint"]  # empreinte courte présente

    def test_rotate_keeps_overlap(self, tmp_path):
        store = tmp_path / "tokens.json"
        s1 = remote_auth.generate(store)
        s2 = remote_auth.rotate(store)
        assert s1 != s2
        # Les DEUX jetons sont valides (fenêtre de recouvrement).
        v = remote_auth.FileTokenVerifier(store)
        assert _run(v.verify_token(s1)) is not None
        assert _run(v.verify_token(s2)) is not None

    def test_revoke_by_fingerprint(self, tmp_path):
        store = tmp_path / "tokens.json"
        s1 = remote_auth.generate(store)
        remote_auth.rotate(store)
        fp1 = remote_auth._sha256(s1)[:12]
        removed = remote_auth.revoke(store, fingerprint=fp1)
        assert removed == 1
        v = remote_auth.FileTokenVerifier(store)
        assert _run(v.verify_token(s1)) is None      # ancien révoqué

    def test_revoke_all(self, tmp_path):
        store = tmp_path / "tokens.json"
        remote_auth.generate(store)
        remote_auth.revoke(store)
        assert remote_auth.status(store)["count"] == 0

    def test_file_token_verifier_rejects_wrong(self, tmp_path):
        store = tmp_path / "tokens.json"
        remote_auth.generate(store)
        v = remote_auth.FileTokenVerifier(store)
        assert _run(v.verify_token("pas-le-bon")) is None

    def test_file_token_verifier_no_tokens(self, tmp_path):
        v = remote_auth.FileTokenVerifier(tmp_path / "absent.json")
        assert v.has_tokens() is False
        assert _run(v.verify_token("x")) is None


# === Config client distante ===

class TestRemoteClientEntry:
    def test_shape_url_and_bearer_no_path(self):
        entry = build_remote_client_entry("https://h/mcp", "TOK", name="srv")
        srv = entry["mcpServers"]["srv"]
        assert srv["url"] == "https://h/mcp"
        assert srv["headers"]["Authorization"] == "Bearer TOK"
        # Aucun chemin disque local (pas de command/args/env).
        assert "command" not in srv and "args" not in srv
        json.dumps(entry)  # sérialisable


# === Logging JSON + redaction ===

class TestJsonLogging:
    def test_formatter_redacts_bearer(self):
        rec = logging.LogRecord("x", logging.INFO, "f", 1,
                                "header auth Bearer abc123SECRET", None, None)
        out = mcp_server._JsonLogFormatter().format(rec)
        assert "abc123SECRET" not in out
        assert "***" in json.loads(out)["msg"]

    def test_formatter_redacts_authorization(self):
        rec = logging.LogRecord("x", logging.INFO, "f", 1,
                                'Authorization: tok_SECRET_42', None, None)
        out = mcp_server._JsonLogFormatter().format(rec)
        assert "tok_SECRET_42" not in out

    def test_formatter_structured_fields(self):
        rec = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
        rec.tool = "corpus_list"
        rec.duration_ms = 12.3
        rec.status = "ok"
        rec.request_id = "r1"
        rec.client_id = "moisson-personal"
        data = json.loads(mcp_server._JsonLogFormatter().format(rec))
        assert data["tool"] == "corpus_list"
        assert data["status"] == "ok"
        assert data["duration_ms"] == 12.3
        assert data["client_id"] == "moisson-personal"
        assert data["level"] == "INFO"


# === Audit JSONL ===

class TestAudit:
    def test_audit_written_in_http(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        _run(mcp_server.call_tool("corpus_list", {"jeton_secret": "VALEUR"}))
        files = list((tmp_path / ".audit").glob("audit-*.jsonl"))
        assert files
        rec = json.loads(files[0].read_text(encoding="utf-8").strip().splitlines()[-1])
        assert rec["tool"] == "corpus_list"
        assert rec["status"] == "ok"
        assert "duration_ms" in rec
        # Seules les CLÉS d'args sont auditées, jamais les valeurs.
        assert rec["arg_keys"] == ["jeton_secret"]
        assert "VALEUR" not in files[0].read_text(encoding="utf-8")

    def test_no_audit_in_stdio(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        _run(mcp_server.call_tool("corpus_list", {}))
        assert not (tmp_path / ".audit").exists()


# === Métriques ===

class TestMetrics:
    def test_snapshot_counts_increment(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        before = mcp_server._metrics_snapshot()["calls_total"]
        _run(mcp_server.call_tool("corpus_list", {}))
        after = mcp_server._metrics_snapshot()
        assert after["calls_total"] == before + 1
        assert "corpus_list" in after["by_tool"]

    def test_percentile_empty(self):
        assert mcp_server._percentile([], 50) is None

    def test_percentile_basic(self):
        vals = [10, 20, 30, 40, 100]
        assert mcp_server._percentile(vals, 50) == 30


# === /healthz enrichi + /metrics (auth) — e2e uvicorn ===

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


_TOKEN = "secret-token-sprint11"


@pytest.fixture(scope="module")
def obs_server():
    port = _free_port()
    app = mcp_server.build_http_app(
        path="/mcp", allowed_hosts=["127.0.0.1:*", "localhost:*"],
        token_verifier=auth.StaticTokenVerifier([_TOKEN]), max_body=1_000_000,
    )
    srv = _UvicornThread(app, "127.0.0.1", port)
    srv.start()
    base = f"http://127.0.0.1:{port}"
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
        pytest.fail("obs_server non démarré")
    yield base
    srv.stop()


@pytest.mark.sockets
class TestHealthzAndMetricsE2E:
    def test_healthz_enriched_public(self, obs_server):
        r = httpx.get(obs_server + "/healthz", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        for k in ("version", "mode", "uptime_s", "output_dir_writable"):
            assert k in body

    def test_metrics_requires_auth(self, obs_server):
        r = httpx.get(obs_server + "/metrics", timeout=5)
        assert r.status_code == 401

    def test_metrics_with_token(self, obs_server):
        r = httpx.get(obs_server + "/metrics",
                      headers={"Authorization": f"Bearer {_TOKEN}"}, timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert "calls_total" in body and "by_tool" in body
