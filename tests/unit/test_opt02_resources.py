"""
Tests OPT-02 — contrat des ressources MCP (C03, C04) et de /download.

- `read_resource` : fichiers de service (`.oauth/`, `.audit/`, `.index.json`)
  et fichiers associés refusés dans LES DEUX transports ; traversée refusée
  (URI `str`, `AnyUrl`, variantes encodées, chemin absolu) ; en HTTP,
  confinement identique à /download (liste blanche, lien sortant refusé, pas
  de repli `_EMITTED_RESOURCES`) ; en stdio, repli conservé pour un corpus émis
  hors OUTPUT_DIR, jamais pour un fichier de service.
- Vérification par le SDK réel (transport en mémoire, sans socket) : la
  normalisation `AnyUrl` laisse passer `corpus://../x` et `corpus:///abs`.
- `list_resources` : aucun chemin disque en HTTP, chemin conservé en stdio,
  fichiers associés et de service jamais listés.
- `corpus_list` et `/download` : même classement.

Sur la base de référence (avant OPT-02), les lectures de `.oauth`, `.audit`,
`corpus://../…` et `corpus:///…` renvoyaient le contenu du fichier.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from pydantic import AnyUrl

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402


SECRET = "JETON-OAUTH-NE-DOIT-PAS-FUIR"
AUDIT_SECRET = "LIGNE-AUDIT-NE-DOIT-PAS-FUIR"
PARENT_SECRET = "SECRET-DU-REPERTOIRE-PARENT"


def _run(coro):
    return asyncio.run(coro)


def _items(n, prefix="T"):
    return [{"itemType": "journalArticle", "title": f"{prefix}{i}",
             "date": "2024"} for i in range(n)]


@pytest.fixture
def out(tmp_path, monkeypatch):
    """OUTPUT_DIR peuplé selon la convention de nommage du chantier, avec des
    fichiers de service et un secret dans le répertoire parent."""
    base = tmp_path / "out"
    base.mkdir()
    (base / ".oauth").mkdir()
    (base / ".oauth" / "oauth_store.json").write_text(
        json.dumps({"access_tokens": {SECRET: {}}}), encoding="utf-8")
    (base / ".audit").mkdir()
    (base / ".audit" / "x.jsonl").write_text(
        json.dumps({"tool": AUDIT_SECRET}) + "\n", encoding="utf-8")
    (base / ".index.json").write_text(json.dumps([SECRET]), encoding="utf-8")
    (base / "corpus_a.json").write_text(json.dumps(_items(3)), encoding="utf-8")
    (base / "corpus_a.csl.json").write_text("[]", encoding="utf-8")
    (base / "corpus_a_stats.txt").write_text("stats", encoding="utf-8")
    (base / "corpus_a.manifest.json").write_text(
        json.dumps({"items": 3}), encoding="utf-8")
    (base / "corpus_a.doi_report.json").write_text(
        json.dumps({"accepted": []}), encoding="utf-8")
    (base / "notes.md").write_text("notes", encoding="utf-8")
    (tmp_path / "secret.env").write_text(PARENT_SECRET, encoding="utf-8")

    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", base)
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    return base


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", mode)


def _read(uri):
    return _run(mcp_server.read_resource(uri))


def _assert_refused(text, *secrets):
    data = json.loads(text)
    assert isinstance(data, dict) and "error" in data, text[:200]
    for s in secrets:
        assert s not in text


def _uri_forms(uri):
    """L'URI brute (str), sa forme AnyUrl, et celle que reçoit le handler
    quand le SDK valide une requête `resources/read` venue du réseau."""
    forms = [uri]
    try:
        forms.append(AnyUrl(uri))
        req = types.ReadResourceRequest(
            method="resources/read", params={"uri": uri})
        forms.append(req.params.uri)
    except Exception:
        pass  # refusée par pydantic : ne peut pas atteindre le handler
    return forms


SERVICE_URIS = [
    "corpus://.oauth/oauth_store.json",
    "corpus://.audit/x.jsonl",
    "corpus://.index.json",
    "corpus://%2eoauth%2foauth_store.json",
]

TRAVERSAL_URIS = [
    "corpus://../secret.env",
    "corpus://%2e%2e/secret.env",
    "corpus://%2E%2E%2Fsecret.env",
    "corpus://..%2fsecret.env",
    "corpus://out/../../secret.env",
    "corpus://..",
]


# === C03 : fichiers de service, les deux transports ===

class TestServiceFilesRefused:
    @pytest.mark.parametrize("mode", ["stdio", "http"])
    @pytest.mark.parametrize("uri", SERVICE_URIS)
    def test_service_file_refused(self, out, monkeypatch, mode, uri):
        _set_mode(monkeypatch, mode)
        for form in _uri_forms(uri):
            _assert_refused(_read(form), SECRET, AUDIT_SECRET)

    @pytest.mark.parametrize("mode", ["stdio", "http"])
    def test_symlink_to_service_file_refused(self, out, monkeypatch, mode):
        _set_mode(monkeypatch, mode)
        (out / "corpus_piege.json").symlink_to(out / ".oauth" / "oauth_store.json")
        _assert_refused(_read("corpus://corpus_piege.json"), SECRET)

    def test_registry_never_serves_service_file_in_stdio(self, out, monkeypatch):
        # Un registre (même falsifié) pointant vers un fichier de service ne
        # rend pas celui-ci lisible.
        mcp_server._EMITTED_RESOURCES["oauth_store.json"] = (
            out / ".oauth" / "oauth_store.json").resolve()
        _assert_refused(_read("corpus://oauth_store.json"), SECRET)


# === C03 : fichiers associés, les deux transports ===

class TestAssociatedFilesNotResources:
    @pytest.mark.parametrize("mode", ["stdio", "http"])
    @pytest.mark.parametrize("name", [
        "corpus_a.manifest.json", "corpus_a.doi_report.json",
        "corpus_a.MANIFEST.json",
    ])
    def test_associated_refused(self, out, monkeypatch, mode, name):
        _set_mode(monkeypatch, mode)
        _assert_refused(_read(f"corpus://{name}"))

    @pytest.mark.parametrize("mode", ["stdio", "http"])
    def test_symlink_to_associated_refused(self, out, monkeypatch, mode):
        _set_mode(monkeypatch, mode)
        (out / "corpus_b.json").symlink_to(out / "corpus_a.manifest.json")
        _assert_refused(_read("corpus://corpus_b.json"))


# === C03 : traversée (str, AnyUrl, requête SDK, variantes encodées) ===

class TestTraversalRefused:
    @pytest.mark.parametrize("mode", ["stdio", "http"])
    @pytest.mark.parametrize("uri", TRAVERSAL_URIS)
    def test_dotdot_refused(self, out, monkeypatch, mode, uri):
        _set_mode(monkeypatch, mode)
        for form in _uri_forms(uri):
            _assert_refused(_read(form), PARENT_SECRET)

    @pytest.mark.parametrize("mode", ["stdio", "http"])
    def test_absolute_path_refused(self, out, monkeypatch, mode, tmp_path):
        # corpus:///chemin/absolu : hôte vide, le SDK le transmet intact ;
        # avant OPT-02, `OUTPUT_DIR / "/chemin"` lisait n'importe quel fichier.
        _set_mode(monkeypatch, mode)
        uri = "corpus://" + str(tmp_path / "secret.env")
        for form in _uri_forms(uri):
            _assert_refused(_read(form), PARENT_SECRET)

    def test_sdk_keeps_dotdot_host_documented(self):
        # Constat vérifié : la normalisation pydantic ne retire pas « .. »
        # quand il est l'hôte opaque, ni le chemin absolu d'un hôte vide ;
        # elle normalise seulement les segments de chemin.
        assert str(AnyUrl("corpus://../x")) == "corpus://../x"
        assert str(AnyUrl("corpus://../../etc/passwd")) == \
            "corpus://../etc/passwd"
        assert str(AnyUrl("corpus:///etc/passwd")) == "corpus:///etc/passwd"
        assert str(AnyUrl("corpus://%2e%2e/x")) == "corpus://%2e%2e/x"
        assert str(AnyUrl("corpus://a/%2e%2e/b")) == "corpus://a/b"
        assert str(AnyUrl("corpus://.oauth/oauth_store.json")) == \
            "corpus://.oauth/oauth_store.json"

    def test_http_errors_carry_no_disk_path(self, out, monkeypatch, tmp_path):
        _set_mode(monkeypatch, "http")
        for uri in ["corpus://absent.json", "corpus://../secret.env",
                    "corpus://.oauth/oauth_store.json", "corpus://notes.md"]:
            text = _read(uri)
            assert str(tmp_path) not in text
            assert str(tmp_path.resolve()) not in text


# === HTTP : confinement identique à /download ===

class TestHttpConfinement:
    def test_whitelist_applies_in_http(self, out, monkeypatch):
        _set_mode(monkeypatch, "http")
        _assert_refused(_read("corpus://notes.md"))

    def test_stdio_keeps_historic_scope_outside_whitelist(self, out):
        # stdio (local de confiance) : pas de liste blanche d'extensions.
        assert _read("corpus://notes.md") == "notes"

    @pytest.mark.parametrize("mode", ["stdio", "http"])
    @pytest.mark.parametrize("name", [
        "corpus_a.json", "corpus_a.csl.json", "corpus_a_stats.txt",
    ])
    def test_publishable_files_readable(self, out, monkeypatch, mode, name):
        _set_mode(monkeypatch, mode)
        text = _read(f"corpus://{name}")
        assert text == (out / name).read_text(encoding="utf-8")
        assert text == _read(AnyUrl(f"corpus://{name}"))

    def test_outgoing_symlink_refused_in_http(self, out, monkeypatch, tmp_path):
        outside = tmp_path / "ailleurs.json"
        outside.write_text(json.dumps([PARENT_SECRET]), encoding="utf-8")
        (out / "corpus_link.json").symlink_to(outside)
        _set_mode(monkeypatch, "http")
        _assert_refused(_read("corpus://corpus_link.json"), PARENT_SECRET)

    def test_registry_fallback_refused_in_http(self, out, monkeypatch, tmp_path):
        emitted = tmp_path / "ailleurs" / "corpus_emis.json"
        emitted.parent.mkdir()
        emitted.write_text(json.dumps(_items(2)), encoding="utf-8")
        mcp_server._EMITTED_RESOURCES["corpus_emis.json"] = emitted.resolve()
        _set_mode(monkeypatch, "http")
        _assert_refused(_read("corpus://corpus_emis.json"))

    def test_registry_fallback_kept_in_stdio(self, out, tmp_path):
        emitted = tmp_path / "ailleurs" / "corpus_emis.json"
        emitted.parent.mkdir()
        emitted.write_text(json.dumps(_items(2)), encoding="utf-8")
        mcp_server._EMITTED_RESOURCES["corpus_emis.json"] = emitted.resolve()
        assert json.loads(_read("corpus://corpus_emis.json")) == _items(2)

    def test_stdio_round_trip_outside_output_dir_via_tool(self, out, tmp_path):
        # Périmètre historique du Sprint 05 : corpus_filter écrit à côté de sa
        # source, hors OUTPUT_DIR ; le lien corpus:// émis reste lisible.
        src = tmp_path / "ailleurs" / "corpus_src.json"
        src.parent.mkdir()
        src.write_text(json.dumps(_items(4)), encoding="utf-8")
        blocks, _ = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": str(src)}))
        link = [b for b in blocks if b.type == "resource_link"][0]
        assert json.loads(_read(link.uri)) == _items(4)

    def test_name_with_space_round_trips(self, out, tmp_path):
        # Avant OPT-02 : ResourceLink(uri="corpus://mon corpus…") levait une
        # ValidationError pydantic et l'outil échouait après écriture.
        src = tmp_path / "ailleurs" / "mon corpus.json"
        src.parent.mkdir()
        src.write_text(json.dumps(_items(2)), encoding="utf-8")
        blocks, _ = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": str(src)}))
        link = [b for b in blocks if b.type == "resource_link"][0]
        assert str(link.uri) == "corpus://mon%20corpus_filtered.json"
        assert json.loads(_read(link.uri)) == _items(2)


# === Vérification par le SDK réel (transport en mémoire, sans socket) ===

async def _sdk_session_calls(uris):
    from mcp.shared.memory import create_connected_server_and_client_session
    results = {}
    async with create_connected_server_and_client_session(
            mcp_server.server) as client:
        for uri in uris:
            res = await client.read_resource(AnyUrl(uri))
            results[uri] = res.contents[0].text
        listed = await client.list_resources()
    return results, listed


class TestThroughSdk:
    @pytest.mark.parametrize("mode", ["stdio", "http"])
    def test_sdk_round_trip(self, out, monkeypatch, tmp_path, mode):
        _set_mode(monkeypatch, mode)
        uris = SERVICE_URIS + TRAVERSAL_URIS + [
            "corpus://" + str(tmp_path / "secret.env"),
            "corpus://corpus_a.manifest.json",
            "corpus://corpus_a.json",
        ]
        results, listed = anyio.run(_sdk_session_calls, uris)
        for uri in uris[:-1]:
            _assert_refused(results[uri], SECRET, AUDIT_SECRET, PARENT_SECRET)
        assert json.loads(results["corpus://corpus_a.json"]) == _items(3)
        names = {str(r.uri) for r in listed.resources}
        assert names == {"corpus://corpus_a.json", "corpus://corpus_a.csl.json"}
        for r in listed.resources:
            if mode == "http":
                assert str(tmp_path) not in (r.description or "")
            else:
                assert str(out) in (r.description or "")


# === C04 : list_resources ===

class TestListResources:
    def test_http_descriptions_without_path(self, out, monkeypatch, tmp_path):
        _set_mode(monkeypatch, "http")
        resources = _run(mcp_server.list_resources())
        assert resources
        for r in resources:
            assert str(tmp_path) not in r.description
            assert str(tmp_path.resolve()) not in r.description
            assert "items" in r.description      # information utile conservée

    def test_stdio_descriptions_keep_path(self, out):
        resources = _run(mcp_server.list_resources())
        assert all(str(out) in r.description for r in resources)

    @pytest.mark.parametrize("mode", ["stdio", "http"])
    def test_only_corpora_listed(self, out, monkeypatch, mode):
        _set_mode(monkeypatch, mode)
        (out / "corpus_piege.json").symlink_to(out / ".oauth" / "oauth_store.json")
        uris = {str(r.uri) for r in _run(mcp_server.list_resources())}
        assert uris == {"corpus://corpus_a.json", "corpus://corpus_a.csl.json"}

    def test_http_outgoing_symlink_not_listed(self, out, monkeypatch, tmp_path):
        outside = tmp_path / "ailleurs.json"
        outside.write_text("[]", encoding="utf-8")
        (out / "corpus_link.json").symlink_to(outside)
        _set_mode(monkeypatch, "http")
        uris = {str(r.uri) for r in _run(mcp_server.list_resources())}
        assert "corpus://corpus_link.json" not in uris


# === corpus_list : même classement ===

class TestCorpusListClassification:
    @pytest.mark.parametrize("mode", ["stdio", "http"])
    def test_associated_files_not_listed(self, out, monkeypatch, mode):
        _set_mode(monkeypatch, mode)
        text = _run(mcp_server._tool_corpus_list({}))
        assert "corpus_a.json" in text
        assert "corpus_a.csl.json" in text
        assert "manifest" not in text
        assert "doi_report" not in text
        assert ".index.json" not in text

    @pytest.mark.parametrize("mode", ["stdio", "http"])
    def test_symlink_to_service_file_not_listed(self, out, monkeypatch, mode):
        _set_mode(monkeypatch, mode)
        (out / "corpus_piege.json").symlink_to(out / ".oauth" / "oauth_store.json")
        assert "corpus_piege" not in _run(mcp_server._tool_corpus_list({}))


# === /download : fichiers de service jamais servis ===

class TestDownloadClassification:
    def _call(self, filename):
        req = SimpleNamespace(path_params={"filename": filename})
        return _run(mcp_server._download_endpoint(req))

    def test_index_service_file_404(self, out):
        assert self._call(".index.json").status_code == 404

    def test_symlink_to_oauth_store_404(self, out):
        (out / "corpus_piege.json").symlink_to(out / ".oauth" / "oauth_store.json")
        assert self._call("corpus_piege.json").status_code == 404

    @pytest.mark.parametrize("name", [
        "corpus_a.manifest.json", "corpus_a.doi_report.json",
    ])
    def test_associated_files_downloadable(self, out, name):
        resp = self._call(name)
        assert resp.status_code == 200
        assert resp.media_type == "application/json"

    def test_nul_byte_403_not_500(self, out):
        assert self._call("corpus_a\x00.json").status_code == 403

    def test_corpus_still_served(self, out):
        assert self._call("corpus_a.json").status_code == 200
