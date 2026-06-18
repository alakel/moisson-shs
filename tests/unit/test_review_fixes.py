"""
Tests des correctifs issus de la revue adversariale (Sprints 06-08).

- [medium] corpus_list (et toute str legacy) fuyait les chemins disque absolus
  en HTTP → redaction globale dans call_tool.
- [low] ContentLengthLimitMiddleware contournable en Transfer-Encoding: chunked
  → wrapper compteur d'octets (413).
- [low] RateLimitMiddleware._buckets non borné → purge des seaux pleins.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# === [medium] Redaction des chemins dans les str legacy en HTTP ===

class TestCorpusListPathRedaction:
    def test_corpus_list_http_no_absolute_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        # Un corpus présent pour produire des lignes « Chemin : … ».
        (tmp_path / "corpus_demo.json").write_text(
            json.dumps([{"itemType": "journalArticle", "title": "x"}]),
            encoding="utf-8")

        result = _run(mcp_server.call_tool("corpus_list", {}))
        assert isinstance(result, list)
        text = result[0].text
        # Le chemin absolu du répertoire ne fuit PAS…
        assert str(tmp_path) not in text
        assert str(tmp_path.resolve()) not in text
        # …mais le basename du corpus reste visible (info utile préservée).
        assert "corpus_demo.json" in text

    def test_corpus_list_stdio_keeps_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        (tmp_path / "corpus_demo.json").write_text("[]", encoding="utf-8")
        result = _run(mcp_server.call_tool("corpus_list", {}))
        # En stdio (local de confiance), le chemin complet est conservé.
        assert str(tmp_path) in result[0].text

    def test_redact_helper_replaces_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        raw = f"Chemin : {tmp_path}/corpus_x.json dans {tmp_path}"
        out = mcp_server._redact_paths_in_text(raw)
        assert str(tmp_path) not in out
        assert "[corpus]/corpus_x.json" in out

    def test_error_string_redacted_in_http(self, tmp_path, monkeypatch):
        # Un message d'erreur contenant un chemin est aussi rédigé en HTTP.
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)

        async def boom(args):
            return f"Erreur sur {tmp_path}/secret.json"

        monkeypatch.setattr(mcp_server, "_tool_corpus_list", boom)
        result = _run(mcp_server.call_tool("corpus_list", {}))
        assert str(tmp_path) not in result[0].text
        assert "[corpus]/secret.json" in result[0].text


# === [low] ContentLength : garde chunked (compteur d'octets) ===

async def _body_reading_app(scope, receive, send):
    """App ASGI qui lit tout le corps puis répond 200."""
    while True:
        msg = await receive()
        if not msg.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _drive_with_body(mw, chunks, method="POST", headers=None):
    scope = {"type": "http", "method": method, "path": "/mcp",
             "headers": headers or [], "client": ("1.2.3.4", 5)}
    it = iter(chunks)

    async def receive():
        try:
            return next(it)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(m):
        sent.append(m)

    _run(mw(scope, receive, send))
    return sent


class TestChunkedBodyGuard:
    def test_chunked_over_limit_rejected_413(self):
        mw = mcp_server.ContentLengthLimitMiddleware(_body_reading_app, max_body=100)
        # 120 octets en deux chunks, SANS content-length (chunked).
        chunks = [
            {"type": "http.request", "body": b"x" * 60, "more_body": True},
            {"type": "http.request", "body": b"x" * 60, "more_body": False},
        ]
        sent = _drive_with_body(mw, chunks)
        assert sent[0]["status"] == 413

    def test_chunked_under_limit_passes(self):
        mw = mcp_server.ContentLengthLimitMiddleware(_body_reading_app, max_body=1000)
        chunks = [
            {"type": "http.request", "body": b"x" * 50, "more_body": True},
            {"type": "http.request", "body": b"x" * 50, "more_body": False},
        ]
        sent = _drive_with_body(mw, chunks)
        assert sent[0]["status"] == 200

    def test_get_not_body_counted(self):
        # GET (SSE) ne passe pas par le compteur de corps.
        mw = mcp_server.ContentLengthLimitMiddleware(_body_reading_app, max_body=10)
        chunks = [{"type": "http.request", "body": b"", "more_body": False}]
        sent = _drive_with_body(mw, chunks, method="GET")
        assert sent[0]["status"] == 200


# === [low] RateLimit : purge des seaux pleins (mémoire bornée) ===

async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class TestRateLimitPurge:
    def test_purge_drops_stale_full_buckets(self):
        mw = mcp_server.RateLimitMiddleware(_ok_app, per_min=60)  # refill 1/s
        now = time.monotonic()
        full_after = mw.capacity / mw.refill_per_sec  # 60 s
        mw._buckets = {
            "old1": (60.0, now - full_after - 1),   # inactif → purgé
            "old2": (60.0, now - full_after - 50),  # inactif → purgé
            "recent": (10.0, now - 1),              # actif → gardé
        }
        mw._purge_full(now)
        assert "old1" not in mw._buckets
        assert "old2" not in mw._buckets
        assert "recent" in mw._buckets

    def test_max_buckets_bound_set(self):
        mw = mcp_server.RateLimitMiddleware(_ok_app, per_min=60)
        assert mw._max_buckets > 0
