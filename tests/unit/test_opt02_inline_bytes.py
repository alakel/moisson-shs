"""
Tests OPT-02 — plafonds en octets (C05).

- `_build_corpus_tool_result` : le JSON inline est sérialisé UNE fois et
  n'est inclus que si sa taille en octets UTF-8 est <= INLINE_MAX_BYTES
  (en plus du plafond INLINE_MAX_ITEMS) ; le résumé dit pourquoi il ne l'est
  pas. Cas : notices volumineuses, résumés longs, caractères multioctets.
- `read_resource` : décision sur stat() avant toute lecture ; un gros fichier
  n'est jamais chargé en entier (ni read_text, ni json.loads du contenu),
  mémoire bornée ; sémantique historique de troncature conservée.
- classement des fichiers passés au helper (associés, service).
"""

import asyncio
import json
import os
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _types(blocks, t):
    return [b for b in blocks if getattr(b, "type", None) == t]


def _items(n, abstract="", title="Article"):
    return [{"itemType": "journalArticle", "title": f"{title} {i}",
             "abstractNote": abstract, "date": "2024"} for i in range(n)]


def _inline_size(items):
    return len(json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8"))


def _build(tmp_path, items, extra=(), name="corpus_x.json"):
    primary = tmp_path / name
    # Copie de la liste : le décompte des sérialisations (`obj is items`) ne
    # doit viser que le helper testé.
    primary.write_text(json.dumps(list(items), ensure_ascii=False),
                       encoding="utf-8")
    return mcp_server._build_corpus_tool_result(
        "résumé", items, primary, "application/json", list(extra), {})


@pytest.fixture
def stdio(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 300)
    return tmp_path


# === Plafond en octets du JSON inline ===

class TestInlineByteCap:
    def test_default_cap_is_2_000_000(self):
        env = dict(os.environ)
        env.pop("MOISSON_INLINE_MAX_BYTES", None)
        proc = subprocess.run(
            [sys.executable, "-c",
             "import mcp_server; print(mcp_server.INLINE_MAX_BYTES)"],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "2000000"

    def test_env_overrides_cap(self):
        env = dict(os.environ)
        env["MOISSON_INLINE_MAX_BYTES"] = "12345"
        proc = subprocess.run(
            [sys.executable, "-c",
             "import mcp_server; print(mcp_server.INLINE_MAX_BYTES)"],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "12345"

    def test_long_abstracts_under_item_cap_not_inlined(self, stdio):
        # 5 notices (<< 300) à résumé de 500 000 caractères : ≈ 2,5 Mo. Avant
        # OPT-02, seules les 300 notices comptaient : tout était inliné.
        items = _items(5, abstract="r" * 500_000)
        blocks, structured = _build(stdio, items)
        assert _types(blocks, "resource") == []
        assert structured["inline_json"] is False
        assert structured["inline_bytes"] == _inline_size(items)
        assert structured["inline_max_bytes"] == 2_000_000
        assert "octets" in structured["inline_skipped_reason"]
        text = blocks[0].text
        assert "non inliné" in text
        assert "MOISSON_INLINE_MAX_BYTES" in text
        assert "corpus://corpus_x.json" in text
        # Le lien reste présent pour accéder au corpus complet.
        assert len(_types(blocks, "resource_link")) == 1

    def test_boundary_equal_is_inlined(self, stdio, monkeypatch):
        items = _items(4, abstract="a" * 1000)
        size = _inline_size(items)
        monkeypatch.setattr(mcp_server, "INLINE_MAX_BYTES", size)
        _, structured = _build(stdio, items)
        assert structured["inline_json"] is True
        monkeypatch.setattr(mcp_server, "INLINE_MAX_BYTES", size - 1)
        blocks, structured = _build(stdio, items)
        assert structured["inline_json"] is False
        assert _types(blocks, "resource") == []

    def test_multibyte_counted_in_bytes_not_chars(self, stdio, monkeypatch):
        # « é » = 2 octets, « 漢 » = 3, « 𝄞 » = 4 : le nombre de caractères
        # est sous le plafond, le nombre d'octets au-dessus.
        items = _items(3, abstract="é漢𝄞" * 2000)
        text = json.dumps(items, ensure_ascii=False, indent=2)
        n_chars, n_bytes = len(text), len(text.encode("utf-8"))
        assert n_bytes > n_chars * 2
        monkeypatch.setattr(mcp_server, "INLINE_MAX_BYTES", n_chars + 10)
        blocks, structured = _build(stdio, items)
        assert structured["inline_json"] is False
        assert structured["inline_bytes"] == n_bytes

    def test_inlined_payload_respects_cap(self, stdio, monkeypatch):
        monkeypatch.setattr(mcp_server, "INLINE_MAX_BYTES", 50_000)
        for n in (1, 10, 40, 80):
            blocks, structured = _build(stdio, _items(n, abstract="z" * 900))
            for b in _types(blocks, "resource"):
                assert len(b.resource.text.encode("utf-8")) <= 50_000
            if structured["inline_json"]:
                assert structured["inline_bytes"] <= 50_000

    def test_serialized_once(self, stdio, monkeypatch):
        items = _items(7)
        calls = []
        real_dumps = json.dumps

        def counting_dumps(obj, *a, **k):
            if obj is items:
                calls.append(1)
            return real_dumps(obj, *a, **k)

        monkeypatch.setattr(json, "dumps", counting_dumps)
        blocks, structured = _build(stdio, items)
        assert structured["inline_json"] is True
        assert len(calls) == 1
        embedded = _types(blocks, "resource")[0]
        assert json.loads(embedded.resource.text) == items

    def test_item_cap_not_serialized_at_all(self, stdio, monkeypatch):
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 2)
        items = _items(5)
        calls = []
        real_dumps = json.dumps

        def counting_dumps(obj, *a, **k):
            if obj is items:
                calls.append(1)
            return real_dumps(obj, *a, **k)

        monkeypatch.setattr(json, "dumps", counting_dumps)
        blocks, structured = _build(stdio, items)
        assert calls == []
        assert structured["inline_skipped_reason"] == "5 items > seuil 2"
        assert "inline_bytes" not in structured

    def test_http_never_inlines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        blocks, structured = _build(tmp_path, _items(2))
        assert _types(blocks, "resource") == []
        assert "inline_bytes" not in structured


# === Classement des fichiers transmis au helper ===

class TestHelperFileClassification:
    def test_associated_file_listed_but_not_a_resource_stdio(self, stdio):
        manifest = stdio / "corpus_x.manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        blocks, structured = _build(stdio, _items(2), extra=[manifest])
        links = {str(b.uri) for b in _types(blocks, "resource_link")}
        assert links == {"corpus://corpus_x.json"}
        assert "corpus_x.manifest.json" in structured["files"]
        assert structured["resources"] == ["corpus://corpus_x.json"]
        assert "corpus_x.manifest.json" not in mcp_server._EMITTED_RESOURCES

    def test_associated_file_downloadable_http(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL", "https://m.example")
        report = tmp_path / "corpus_x.doi_report.json"
        report.write_text("{}", encoding="utf-8")
        _, structured = _build(tmp_path, _items(2), extra=[report])
        assert structured["download_urls"]["corpus_x.doi_report.json"] == \
            "https://m.example/download/corpus_x.doi_report.json"

    @pytest.mark.parametrize("mode", ["stdio", "http"])
    def test_service_file_never_exposed(self, tmp_path, monkeypatch, mode):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", mode)
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
        (tmp_path / ".oauth").mkdir()
        store = tmp_path / ".oauth" / "oauth_store.json"
        store.write_text("{}", encoding="utf-8")
        index = tmp_path / ".index.json"
        index.write_text("[]", encoding="utf-8")
        blocks, structured = _build(tmp_path, _items(1), extra=[store, index])
        dumped = json.dumps(structured) + " ".join(
            str(getattr(b, "uri", "")) for b in blocks)
        assert "oauth_store" not in dumped
        assert ".index.json" not in dumped
        assert "oauth_store.json" not in mcp_server._EMITTED_RESOURCES

    def test_http_download_url_is_percent_encoded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL", "")
        _, structured = _build(tmp_path, _items(1), name="mon export.json")
        assert structured["download_urls"]["mon export.json"] == \
            "/download/mon%20export.json"


# === read_resource : lecture bornée ===

def _write_items_file(path: Path, n: int, abstract: str = "", indent=None):
    path.write_text(json.dumps(_items(n, abstract=abstract),
                               ensure_ascii=False, indent=indent),
                    encoding="utf-8")
    return path


@pytest.fixture
def res_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    return tmp_path


class TestReadResourceBounded:
    def test_big_file_never_loaded_whole(self, res_dir, monkeypatch):
        # ≈ 12 Mo de petites notices : la troncature ne doit ni appeler
        # read_text/read_bytes, ni allouer l'équivalent du fichier.
        f = _write_items_file(res_dir / "corpus_big.json", 40_000,
                              abstract="a" * 250)
        size = f.stat().st_size
        assert size > 10_000_000

        def forbidden(self, *a, **k):
            if self.name == f.name:
                raise AssertionError("lecture intégrale du fichier")
            return real_read_text(self, *a, **k)

        real_read_text = Path.read_text
        monkeypatch.setattr(Path, "read_text", forbidden)
        monkeypatch.setattr(Path, "read_bytes", forbidden)

        tracemalloc.start()
        try:
            text = _run(mcp_server.read_resource("corpus://corpus_big.json"))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        data = json.loads(text)
        assert data["_truncated"] is True
        assert data["_total_items"] == 40_000
        assert data["_returned"] == 50
        assert [it["title"] for it in data["items"]][:2] == \
            ["Article 0", "Article 1"]
        # Mémoire de pointe très inférieure à la taille du fichier (la
        # lecture intégrale + json.loads en coûtait plusieurs fois la taille).
        assert peak < size / 4, (peak, size)

    def test_sample_stops_at_byte_cap(self, res_dir):
        # 30 notices de ≈ 200 Ko : les 50 premiers items pèseraient 6 Mo.
        _write_items_file(res_dir / "corpus_lourd.json", 30,
                          abstract="b" * 200_000)
        text = _run(mcp_server.read_resource("corpus://corpus_lourd.json"))
        data = json.loads(text)
        assert data["_total_items"] == 30
        assert data["_returned"] < 30
        assert data["_byte_cap_reached"] is True
        assert "plafond" in data["_message"]
        # Au plus une notice au-delà du plafond (indentation comprise).
        assert len(text.encode("utf-8")) <= 2_000_000 + 2 * 205_000

    def test_multibyte_file_decided_on_bytes(self, res_dir):
        # 1,1 million de caractères « é » = 2,2 Mo : sous 2 000 000 caractères
        # (l'ancienne garde comptait les caractères) mais au-dessus en octets.
        f = res_dir / "corpus_mb.json"
        f.write_text(json.dumps(_items(11, abstract="é" * 100_000),
                                ensure_ascii=False), encoding="utf-8")
        assert len(f.read_text(encoding="utf-8")) < 2_000_000 < f.stat().st_size
        data = json.loads(_run(mcp_server.read_resource("corpus://corpus_mb.json")))
        assert data["_truncated"] is True
        assert data["_total_items"] == 11

    def test_small_file_returned_verbatim(self, res_dir):
        f = _write_items_file(res_dir / "corpus_petit.json", 3, indent=2)
        assert _run(mcp_server.read_resource("corpus://corpus_petit.json")) == \
            f.read_text(encoding="utf-8")

    def test_big_non_array_refused_not_returned(self, res_dir):
        f = res_dir / "corpus_dict.json"
        f.write_text(json.dumps({"x": "y" * 2_100_000}), encoding="utf-8")
        text = _run(mcp_server.read_resource("corpus://corpus_dict.json"))
        assert len(text) < 1_000
        assert "error" in json.loads(text)

    def test_big_invalid_json_is_error_not_exception(self, res_dir):
        f = res_dir / "corpus_bad.json"
        f.write_text("[" + '{"a": 1},' * 300_000 + "{oops", encoding="utf-8")
        text = _run(mcp_server.read_resource("corpus://corpus_bad.json"))
        assert "error" in json.loads(text)

    def test_growth_between_stat_and_read(self, res_dir):
        # stat() annonce un petit fichier, la lecture en trouve plus que le
        # plafond : bascule sur la troncature, sans lecture non bornée.
        f = _write_items_file(res_dir / "corpus_grow.json", 60,
                              abstract="c" * 40_000)

        class _Shrunk:
            def __init__(self, p):
                self.p = p

            def stat(self):
                return type("S", (), {"st_size": 10})()

            def open(self, *a, **k):
                return self.p.open(*a, **k)

            def __fspath__(self):
                return str(self.p)

        text = mcp_server._read_resource_text(_Shrunk(f), f.name)
        data = json.loads(text)
        assert data["_truncated"] is True
        assert data["_total_items"] == 60

    def test_read_error_carries_no_path_in_http(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        f = tmp_path / "corpus_latin1.json"
        f.write_bytes('["caf\xe9"]'.encode("latin-1"))
        text = _run(mcp_server.read_resource("corpus://corpus_latin1.json"))
        assert "error" in json.loads(text)
        assert str(tmp_path) not in text
