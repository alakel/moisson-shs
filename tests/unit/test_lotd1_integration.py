"""
Tests d'intégration du lot D1 (OPT-16 manifeste, OPT-18 exports, OPT-09 ter).

Raccords vérifiés, hors réseau et sans socket :

- classification des exports (``.bib``, ``.ris``) : téléchargeables avec
  leur type MIME, jamais corpus, jamais fichier associé, jamais ressource en
  HTTP, jamais fichier de service ;
- ``corpus_export`` publie atomiquement et produit un manifeste : ajout au
  manifeste du corpus source (rôle ``export``, événement ``export``), corpus
  ancien complété, manifeste propre pour un export écrit ailleurs ; deux
  exports simultanés sans perte ;
- les exports n'apparaissent jamais comme corpus dans l'index, dans
  ``corpus_list`` ni dans ``list_resources`` ; leur suppression ne rend pas
  le corpus source incomplet ;
- en HTTP, l'export est désormais servi (URL seule, manifeste sans chemin) ;
- détection du format entrant unifiée (manifeste renommé ``.json`` refusé) ;
- CLI ``moisson-shs export`` : manifeste annoncé ;
- outils : les neuf historiques plus ``corpus_export``.
"""

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import cli  # noqa: E402
import config  # noqa: E402
import mcp_server  # noqa: E402
from engine.outputs import write_outputs  # noqa: E402
from mappers.bibtex_exporter import (  # noqa: E402
    ExportInputError, export_corpus_file, load_zotero_items,
)
from utils import manifest as mf  # noqa: E402
from utils.output_files import (  # noqa: E402
    INDEX_NAME, ROLE_EXPORT, corpus_kind, export_media_type, file_role,
    is_associated_file, is_corpus_file, is_downloadable_file, is_export_file,
    is_publishable_file,
)

HISTORICAL_TOOLS = {
    "moisson_search", "corpus_analyze", "corpus_list", "fetch_by_doi",
    "search_by_author", "corpus_merge", "corpus_to_csl", "corpus_filter",
    "corpus_resolve_dois",
}

STEM = "corpus_archives_20260929_050000_abcd1234"


def _items(n=3):
    return [{"itemType": "journalArticle", "title": f"Archives numériques {i}",
             "creators": [{"creatorType": "author", "lastName": f"Nom{i}",
                           "firstName": "Jean"}],
             "date": "2020", "DOI": f"10.1000/arch.{i}",
             "extra": "Sources: HAL"} for i in range(n)]


def _run(coro):
    return asyncio.run(coro)


def _call(name, args):
    return _run(mcp_server.call_tool(name, args))


def _manifest(directory: Path, stem: str = STEM) -> dict:
    doc = mf.read_manifest(directory / f"{stem}.manifest.json")
    assert doc is not None
    return doc


def _exports_in(doc: dict) -> list:
    return [f for f in doc["files"] if f.get("role") == ROLE_EXPORT]


@pytest.fixture
def stdio_out(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", out)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(config, "PUBLISH_FSYNC", False)
    return out


@pytest.fixture
def http_out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
    monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                        "https://moisson.example.org")
    monkeypatch.setattr(config, "PUBLISH_FSYNC", False)
    return tmp_path


def _harvest(directory: Path, stem: str = STEM, n: int = 3) -> Path:
    """Corpus publié avec manifeste (JSON, CSL-JSON, statistiques)."""
    write_outputs(_items(n), directory, stem, "archives numériques",
                  run_id=stem[-24:])
    return directory / f"{stem}.json"


# === Classification ===

class TestClassification:
    @pytest.mark.parametrize("name,media", [
        ("corpus_x_1234abcd.bib", "application/x-bibtex; charset=utf-8"),
        ("refs.BIB", "application/x-bibtex; charset=utf-8"),
        ("corpus_x_1234abcd.ris",
         "application/x-research-info-systems; charset=utf-8"),
    ])
    def test_export_is_downloadable_only(self, name, media):
        assert is_export_file(name)
        assert export_media_type(name) == media
        assert is_downloadable_file(name)
        assert corpus_kind(name) is None
        assert not is_corpus_file(name)
        assert not is_publishable_file(name)
        assert not is_associated_file(name)
        assert file_role(name) is None       # jamais regroupé par l'index

    @pytest.mark.parametrize("name", [".x.bib", ".bib", "x.bibx", "x.ris.txt"])
    def test_service_or_foreign_names_refused(self, name):
        assert not is_export_file(name)
        assert export_media_type(name) is None

    def test_http_read_resource_refuses_exports(self, http_out):
        (http_out / "corpus_x_1234abcd.bib").write_text("@misc{a,\n}\n",
                                                        encoding="utf-8")
        text = _run(mcp_server.read_resource("corpus://corpus_x_1234abcd.bib"))
        assert "non exposée" in text
        assert str(http_out) not in text


# === Manifeste produit par corpus_export ===

class TestExportManifest:
    def test_appended_to_the_source_manifest(self, stdio_out):
        corpus = _harvest(stdio_out)
        before = _manifest(stdio_out)
        blocks, st = _call("corpus_export", {"corpus_path": str(corpus)})
        name = next(iter(st["files"]))
        export = stdio_out / name
        assert export.suffix == ".bib"
        assert st["manifest"] == f"{STEM}.manifest.json"
        assert f"Manifeste : {STEM}.manifest.json" in blocks[0].text

        doc = _manifest(stdio_out)
        entry, = _exports_in(doc)
        size, sha = mf.file_digest(export)
        assert entry == {"name": name, "role": ROLE_EXPORT, "bytes": size,
                         "sha256": sha, "items": 3}
        # Le corpus reste celui de la moisson (fichiers, primaire, notices).
        assert doc["kind"] == mf.KIND_HARVEST
        assert doc["primary"] == before["primary"] == corpus.name
        assert doc.get("records") == before.get("records")
        assert doc["item_count"] == before["item_count"] == 3
        event = doc["history"][-1]
        assert event["kind"] == mf.KIND_EXPORT and event["files"] == [name]
        assert event["format"] == "bibtex"
        assert event["export"]["file"] == name
        assert event["export"]["entries"] == 3
        assert event["inputs"][0]["name"] == corpus.name
        assert event["inputs"][0]["manifest"] == f"{STEM}.manifest.json"
        assert not mf.absolute_paths_in(doc)
        # Aucune métadonnée de moisson dans l'export (invariant 1).
        assert "Sources" not in export.read_text(encoding="utf-8")

    def test_export_never_listed_as_a_corpus(self, stdio_out):
        corpus = _harvest(stdio_out)
        mf.list_corpora(stdio_out)           # index construit avant l'export
        _call("corpus_export", {"corpus_path": str(corpus), "format": "ris"})
        _call("corpus_export", {"corpus_path": str(corpus)})
        index = json.loads((stdio_out / INDEX_NAME).read_text(encoding="utf-8"))
        assert list(index["corpora"]) == [STEM]
        entry = index["corpora"][STEM]
        assert entry["status"] == mf.STATUS_COMPLETE
        assert not any(n.endswith((".bib", ".ris")) for n in entry["files"])
        listing = mf.list_corpora(stdio_out, rebuild=True)
        assert [e["stem"] for e in listing.entries] == [STEM]
        assert listing.entries[0]["status"] == mf.STATUS_COMPLETE
        text = _call("corpus_list", {})[0].text
        assert ".bib" not in text and ".ris" not in text
        uris = [str(r.uri) for r in _run(mcp_server.list_resources())]
        assert uris and not any(u.endswith((".bib", ".ris")) for u in uris)

    def test_deleting_an_export_keeps_the_corpus_complete(self, stdio_out):
        corpus = _harvest(stdio_out)
        _, st = _call("corpus_export", {"corpus_path": str(corpus)})
        (stdio_out / next(iter(st["files"]))).unlink()
        listing = mf.list_corpora(stdio_out, rebuild=True)
        assert listing.entries[0]["status"] == mf.STATUS_COMPLETE
        assert listing.entries[0]["notes"] == []

    def test_legacy_corpus_gets_a_manifest(self, stdio_out):
        corpus = stdio_out / "corpus_ancien_20250101_120000.json"
        corpus.write_text(json.dumps(_items(2)), encoding="utf-8")
        _, st = _call("corpus_export", {"corpus_path": str(corpus)})
        stem = "corpus_ancien_20250101_120000"
        assert st["manifest"] == f"{stem}.manifest.json"
        doc = _manifest(stdio_out, stem)
        roles = {f["name"]: f["role"] for f in doc["files"]}
        assert roles[corpus.name] == "zotero"
        assert list(roles.values()).count(ROLE_EXPORT) == 1
        assert doc["primary"] == corpus.name and doc["item_count"] == 2
        assert [r["position"] for r in doc["records"]] == [1, 2]
        entry, = mf.list_corpora(stdio_out).entries
        assert entry["stem"] == stem
        assert entry["status"] == mf.STATUS_COMPLETE
        assert entry["primary"] == corpus.name and entry["item_count"] == 2

    def test_export_elsewhere_has_its_own_manifest(self, stdio_out, tmp_path):
        corpus = _harvest(stdio_out)
        elsewhere = tmp_path / "ailleurs"
        elsewhere.mkdir()
        target = elsewhere / "refs.ris"
        _, st = _call("corpus_export", {"corpus_path": str(corpus),
                                        "format": "ris",
                                        "output_path": str(target)})
        assert st["manifest"] == "refs.manifest.json"
        doc = _manifest(elsewhere, "refs")
        assert doc["kind"] == mf.KIND_EXPORT
        assert doc["primary"] == "refs.ris" and doc["item_count"] == 3
        assert doc["inputs"][0]["name"] == corpus.name
        assert doc["export"]["format"] == "ris"
        assert not mf.absolute_paths_in(doc)
        # Le manifeste du corpus source n'est pas touché.
        assert not _exports_in(_manifest(stdio_out))
        assert mf.list_corpora(elsewhere).entries == []

    def test_concurrent_exports_are_all_recorded(self, stdio_out):
        corpus = _harvest(stdio_out)
        errors = []

        def one(fmt):
            try:
                export_corpus_file(corpus, fmt)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=one, args=(fmt,))
                   for fmt in ("bibtex", "ris", "biblatex", "ris",
                               "bibtex", "biblatex")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        doc = _manifest(stdio_out)
        exports = _exports_in(doc)
        assert len(exports) == 6
        on_disk = sorted(p.name for p in stdio_out.glob(f"{STEM}_*.*")
                         if p.suffix in (".bib", ".ris"))
        assert sorted(e["name"] for e in exports) == on_disk
        assert sum(1 for e in doc["history"] if e["kind"] == mf.KIND_EXPORT) == 6
        assert not list(stdio_out.glob(".*.tmp"))


# === HTTP ===

class TestHttpExport:
    def test_export_served_with_its_media_type(self, http_out):
        _harvest(http_out)
        blocks, st = _call("corpus_export", {"corpus_path": f"{STEM}.json",
                                             "format": "ris"})
        (name, url), = st["download_urls"].items()
        assert url == f"https://moisson.example.org/download/{name}"
        assert st["manifest"] == f"{STEM}.manifest.json"
        dumped = json.dumps(st, ensure_ascii=False) + blocks[0].text
        assert str(http_out) not in dumped
        assert not [b for b in blocks if isinstance(b, types.EmbeddedResource)]
        resp = _run(mcp_server._download_endpoint(
            SimpleNamespace(path_params={"filename": name})))
        assert resp.status_code == 200
        assert resp.media_type == \
            "application/x-research-info-systems; charset=utf-8"
        # Le manifeste complété reste téléchargeable, sans chemin disque.
        resp = _run(mcp_server._download_endpoint(
            SimpleNamespace(path_params={"filename": f"{STEM}.manifest.json"})))
        assert resp.status_code == 200
        doc = _manifest(http_out)
        assert _exports_in(doc)[0]["name"] == name
        assert not mf.absolute_paths_in(doc)

    def test_service_names_still_refused(self, http_out):
        (http_out / ".x.bib").write_text("@misc{a,\n}\n", encoding="utf-8")
        resp = _run(mcp_server._download_endpoint(
            SimpleNamespace(path_params={"filename": ".x.bib"})))
        assert resp.status_code == 404


# === Détection du format entrant ===

class TestInputFormat:
    def test_manifest_content_named_json_refused(self, tmp_path):
        bad = tmp_path / "corpus_x.json"
        bad.write_text(json.dumps({"format": mf.MANIFEST_FORMAT, "version": 1,
                                   "files": []}), encoding="utf-8")
        with pytest.raises(ExportInputError, match="manifeste"):
            load_zotero_items(bad)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["corpus_x.json"]

    def test_csl_content_refused_without_writing(self, tmp_path):
        bad = tmp_path / "corpus_y.json"
        bad.write_text(json.dumps([{"id": "a", "type": "article-journal",
                                    "title": "T"}]), encoding="utf-8")
        with pytest.raises(ExportInputError, match="CSL-JSON"):
            export_corpus_file(bad, "bibtex")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["corpus_y.json"]


# === CLI et outils ===

class TestCliAndTools:
    def test_cli_export_announces_the_manifest(self, tmp_path, capsys,
                                               monkeypatch):
        monkeypatch.setattr(config, "PUBLISH_FSYNC", False)
        corpus = _harvest(tmp_path)
        code = cli.main(["export", str(corpus), "-f", "ris"])
        assert code == 0
        out = capsys.readouterr().out
        assert f"Manifeste : {STEM}.manifest.json" in out
        assert len(_exports_in(_manifest(tmp_path))) == 1

    def test_historical_tools_plus_corpus_export(self):
        names = [t.name for t in _run(mcp_server.list_tools())]
        assert len(names) == len(set(names))
        assert HISTORICAL_TOOLS <= set(names)
        assert "corpus_export" in names
