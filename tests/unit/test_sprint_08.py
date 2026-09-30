"""
Tests du Sprint 08 — Retour distant = URLs de téléchargement + /download.

Couvre la DoD :
- **STDIO inchangé** : le helper en mode stdio émet toujours EmbeddedResource +
  ResourceLink (test_tool_result reste vert par ailleurs).
- **Branche HTTP** : `_build_corpus_tool_result` et les outils producteurs en
  HTTP → PAS d'EmbeddedResource, PAS de ResourceLink, `download_urls` (URLs
  absolues), AUCUN chemin disque (ni content, ni structuredContent).
- **Route /download** : endpoint (403 séparateur, 404 ext/inexistant, 200 +
  nosniff/Content-Disposition) ; e2e auth (401 sans jeton, 200 avec).

Loopback uniquement.
"""

import asyncio
import json
import socket
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
import auth  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _make_items(n):
    return [{"itemType": "journalArticle", "title": f"T{i}", "date": "2024",
             "DOI": f"10.0/{i}", "extra": "Sources: Test"} for i in range(n)]


def _types(blocks, t):
    return [b for b in blocks if getattr(b, "type", None) == t]


@pytest.fixture
def http_mode(tmp_path, monkeypatch):
    """Mode HTTP + OUTPUT_DIR temporaire + base URL publique."""
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL", "https://moisson.example.org")
    return tmp_path


# === Helper transport-aware : branche HTTP ===

class TestHelperHttpBranch:
    def test_http_emits_urls_no_inline_no_path(self, http_mode):
        items = _make_items(3)
        primary = http_mode / "corpus_x.json"
        primary.write_text(json.dumps(items), encoding="utf-8")
        csl = http_mode / "corpus_x.csl.json"
        csl.write_text("[]", encoding="utf-8")
        stats = http_mode / "corpus_x_stats.txt"
        stats.write_text("stats", encoding="utf-8")

        blocks, structured = mcp_server._build_corpus_tool_result(
            summary_text=(
                "Résumé\nChemin complet : " + str(http_mode / "corpus_x.json")
            ),
            primary_items=items,
            primary_path=primary,
            primary_mime="application/json",
            extra_files=[csl, stats],
            structured={"source_corpus": str(primary)},
        )

        # Aucun EmbeddedResource ni ResourceLink en HTTP.
        assert _types(blocks, "resource") == []
        assert _types(blocks, "resource_link") == []
        # download_urls : 3 URLs absolues https.
        urls = structured["download_urls"]
        assert set(urls) == {"corpus_x.json", "corpus_x.csl.json",
                             "corpus_x_stats.txt"}
        for u in urls.values():
            assert u.startswith("https://moisson.example.org/download/")
        assert structured["transport"] == "http"
        assert structured["item_count"] == 3
        # Pas de clé 'files' (chemins).
        assert "files" not in structured
        # AUCUN chemin disque dans le texte…
        text = blocks[0].text
        assert str(http_mode) not in text
        assert "/download/corpus_x.json" in text or \
               "https://moisson.example.org/download/corpus_x.json" in text
        # …ni dans structuredContent (source_corpus réduit au basename).
        assert structured["source_corpus"] == "corpus_x.json"
        # Sérialisable.
        json.dumps(structured)

    def test_http_relative_urls_when_no_base(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL", "")
        primary = tmp_path / "corpus_r.json"
        primary.write_text(json.dumps(_make_items(2)), encoding="utf-8")
        blocks, structured = mcp_server._build_corpus_tool_result(
            "r", _make_items(2), primary, "application/json", [], {}
        )
        u = structured["download_urls"]["corpus_r.json"]
        assert u == "/download/corpus_r.json"   # repli relatif

    def test_moisson_search_http_branch(self, http_mode, monkeypatch):
        # Stub réseau : run_source du moteur (OPT-07 : l'exécution d'une
        # source vit dans engine.orchestrator, main.run_source n'en est plus
        # qu'un alias que le moteur n'appelle pas).
        monkeypatch.setattr("engine.orchestrator.run_source",
                            lambda *a, **k: _make_items(2))
        blocks, structured = mcp_server._run_moisson_sync(
            query="discours", max_per_source=10, sources=["hal", "isidore"],
            enrich_oai=False, do_dedup=True, workers=1,
        )
        # HTTP : pas d'inline, 4 URLs (json + csl + stats + manifeste, fichier
        # associé téléchargeable ajouté par OPT-16).
        assert _types(blocks, "resource") == []
        assert _types(blocks, "resource_link") == []
        assert len(structured["download_urls"]) == 4
        assert any(n.endswith(".manifest.json")
                   for n in structured["download_urls"])
        # Aucun chemin disque dans le texte.
        assert str(http_mode) not in blocks[0].text


# === STDIO inchangé (contrôle de non-régression) ===

class TestStdioUnchanged:
    def test_stdio_still_inlines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 300)
        items = _make_items(3)
        primary = tmp_path / "corpus_s.json"
        primary.write_text(json.dumps(items), encoding="utf-8")
        blocks, structured = mcp_server._build_corpus_tool_result(
            "résumé", items, primary, "application/json", [], {}
        )
        # En stdio : EmbeddedResource + ResourceLink présents, pas de download_urls.
        assert len(_types(blocks, "resource")) == 1
        assert len(_types(blocks, "resource_link")) == 1
        assert "download_urls" not in structured
        assert structured["inline_json"] is True


# === Route /download : endpoint unitaire ===

class TestDownloadEndpointUnit:
    def _call(self, filename, output_dir, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", output_dir)
        req = SimpleNamespace(path_params={"filename": filename})
        return _run(mcp_server._download_endpoint(req))

    def test_rejects_separator_403(self, tmp_path, monkeypatch):
        resp = self._call("../etc/passwd", tmp_path, monkeypatch)
        assert resp.status_code == 403

    def test_rejects_slash_403(self, tmp_path, monkeypatch):
        resp = self._call("sub/corpus.json", tmp_path, monkeypatch)
        assert resp.status_code == 403

    def test_rejects_bad_extension_404(self, tmp_path, monkeypatch):
        (tmp_path / "evil.py").write_text("x", encoding="utf-8")
        resp = self._call("evil.py", tmp_path, monkeypatch)
        assert resp.status_code == 404

    def test_rejects_nonexistent_404(self, tmp_path, monkeypatch):
        resp = self._call("ghost.json", tmp_path, monkeypatch)
        assert resp.status_code == 404

    def test_serves_valid_file_200_with_headers(self, tmp_path, monkeypatch):
        f = tmp_path / "corpus_ok.csl.json"
        f.write_text("[]", encoding="utf-8")
        resp = self._call("corpus_ok.csl.json", tmp_path, monkeypatch)
        assert resp.status_code == 200
        assert resp.media_type == "application/json"
        assert resp.headers.get("x-content-type-options") == "nosniff"
        # Content-Disposition attachment avec le nom
        cd = resp.headers.get("content-disposition", "")
        assert "attachment" in cd and "corpus_ok.csl.json" in cd

    def test_serves_stats_txt_as_text(self, tmp_path, monkeypatch):
        f = tmp_path / "corpus_x_stats.txt"
        f.write_text("stats", encoding="utf-8")
        resp = self._call("corpus_x_stats.txt", tmp_path, monkeypatch)
        assert resp.status_code == 200
        assert resp.media_type == "text/plain"


# === Route /download : e2e auth (uvicorn réel) ===

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


_TOKEN = "secret-token-sprint08"


@pytest.fixture
def download_server(tmp_path, monkeypatch):
    """Serveur HTTP authentifié avec OUTPUT_DIR temporaire + un corpus servi."""
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    corpus = tmp_path / "corpus_dl.csl.json"
    corpus.write_text(json.dumps([{"id": "a", "type": "article-journal"}]),
                      encoding="utf-8")

    port = _free_port()
    app = mcp_server.build_http_app(
        path="/mcp",
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        token_verifier=auth.StaticTokenVerifier([_TOKEN]),
        max_body=1_000_000,
    )
    srv = _UvicornThread(app, "127.0.0.1", port)
    srv.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        try:
            if httpx.get(base + "/healthz", timeout=1.0).status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.1)
    if not ready:
        srv.stop()
        pytest.fail("download_server non démarré")
    yield base, corpus
    srv.stop()


@pytest.mark.sockets
class TestDownloadE2E:
    def test_download_with_token_200(self, download_server):
        base, corpus = download_server
        r = httpx.get(base + "/download/corpus_dl.csl.json",
                      headers={"Authorization": f"Bearer {_TOKEN}"}, timeout=5)
        assert r.status_code == 200
        assert r.json() == [{"id": "a", "type": "article-journal"}]
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert "attachment" in r.headers.get("content-disposition", "")

    def test_download_without_token_401(self, download_server):
        base, _ = download_server
        r = httpx.get(base + "/download/corpus_dl.csl.json", timeout=5)
        assert r.status_code == 401

    def test_download_bad_token_401(self, download_server):
        base, _ = download_server
        r = httpx.get(base + "/download/corpus_dl.csl.json",
                      headers={"Authorization": "Bearer nope"}, timeout=5)
        assert r.status_code == 401

    def test_download_nonexistent_with_token_404(self, download_server):
        base, _ = download_server
        r = httpx.get(base + "/download/absent.json",
                      headers={"Authorization": f"Bearer {_TOKEN}"}, timeout=5)
        assert r.status_code == 404

    def test_both_routes_on_same_app(self, download_server):
        # /mcp et /download coexistent sur la même app, même auth.
        base, _ = download_server
        # /mcp sans token → 401
        r = httpx.post(base + "/mcp",
                       headers={"Accept": "application/json, text/event-stream",
                                "Content-Type": "application/json"},
                       content=json.dumps({"jsonrpc": "2.0", "id": 1,
                                           "method": "initialize",
                                           "params": {}}), timeout=5)
        assert r.status_code == 401
