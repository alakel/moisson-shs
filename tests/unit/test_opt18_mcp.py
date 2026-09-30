"""
Tests OPT-18 — outil MCP ``corpus_export`` et sous-commande
``moisson-shs export``. Hors réseau, sans socket (appels directs du
dispatcher et de l'endpoint de téléchargement).

- Déclaration : outil listé avec son schéma, à côté des neuf outils
  historiques (arguments historiques inchangés).
- Stdio : fichier au nom unique à côté du corpus, lien de ressource au bon
  type MIME, contenu inliné = texte exporté (et non le JSON des notices),
  aller-retour ``corpus://`` y compris hors OUTPUT_DIR, ``output_path``
  honoré ; refus clairs (CSL-JSON par le nom ou le contenu, fichier associé,
  format inconnu, suffixe réservé, extension incohérente) sans écriture.
- Stdio, plafonds : décision d'inliner prise sur l'export lui-même (et non
  sur le JSON des notices), message propre à l'export, ``inline_json`` faux ;
  au-delà du plafond de lecture des ressources, aucun lien ``corpus://``
  mort n'est promis (export réel relu, sans taille simulée).
- HTTP : ``output_path`` confiné à OUTPUT_DIR (hors zone, sous-dossier,
  extension inexacte refusés) ; URL de téléchargement seule, aucun chemin
  disque ni contenu inliné ; refus explicite si ``/download`` ne servait pas
  l'extension (jamais de lien mort). La classification réelle des exports
  (``utils.output_files``) est utilisée, sans simulation.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import cli  # noqa: E402
import mcp_server  # noqa: E402
from utils.output_files import is_downloadable_file  # noqa: E402

HISTORICAL_TOOLS = {
    "moisson_search", "corpus_analyze", "corpus_list", "fetch_by_doi",
    "search_by_author", "corpus_merge", "corpus_to_csl", "corpus_filter",
    "corpus_resolve_dois",
}

ITEMS = [
    {"itemType": "journalArticle", "title": "Les archives de Foucault & co",
     "creators": [{"creatorType": "author", "lastName": "Dupont",
                   "firstName": "Jean"}],
     "date": "2020-05-15", "publicationTitle": "Revue", "DOI": "10.1000/x",
     "url": "https://ex.org/a", "tags": [{"tag": "archives"}],
     "extra": "Sources: HAL, Cairn\nHAL ID: hal-01\nAnnote: à relire"},
    {"itemType": "book", "title": "Surveiller et punir", "date": "1975",
     "creators": [{"creatorType": "author", "lastName": "Foucault",
                   "firstName": "Michel"}], "extra": "Sources: Isidore"},
]


def _run(coro):
    return asyncio.run(coro)


def _call(args):
    return _run(mcp_server.call_tool("corpus_export", args))


def _write_corpus(path, items=ITEMS):
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def _of_type(blocks, cls):
    return [b for b in blocks if isinstance(b, cls)]


@pytest.fixture
def stdio_dirs(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", out)
    return out, elsewhere


@pytest.fixture
def http_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                        "https://moisson.example.org")
    return tmp_path


# === Déclaration ===

class TestDeclaration:
    def test_tool_listed_next_to_the_historical_nine(self):
        tools = {t.name: t for t in _run(mcp_server.list_tools())}
        assert HISTORICAL_TOOLS <= set(tools)
        assert "corpus_export" in tools
        names = [t.name for t in _run(mcp_server.list_tools())]
        assert len(names) == len(set(names))
        schema = tools["corpus_export"].inputSchema
        assert schema["required"] == ["corpus_path"]
        props = schema["properties"]
        assert props["format"]["enum"] == ["bibtex", "biblatex", "ris"]
        assert props["format"]["default"] == "bibtex"
        assert {"output_path", "ascii", "include_provenance"} <= set(props)

    def test_historical_arguments_unchanged(self):
        tools = {t.name: t for t in _run(mcp_server.list_tools())}
        props = tools["corpus_to_csl"].inputSchema["properties"]
        assert {"corpus_path", "output_path"} <= set(props)


# === Stdio ===

class TestStdio:
    def test_bibtex_next_to_the_corpus_with_unique_name(self, stdio_dirs):
        out, elsewhere = stdio_dirs
        corpus = _write_corpus(elsewhere / "corpus_x.json")
        # Retour « full » (OPT-17) : texte exporté inliné.
        blocks, st = _call({"corpus_path": str(corpus), "inline": "full"})
        blocks2, st2 = _call({"corpus_path": str(corpus), "inline": "full"})
        files = sorted(p.name for p in elsewhere.glob("*.bib"))
        assert len(files) == 2 and files[0] != files[1]
        name = next(iter(st["files"]))
        path = elsewhere / name
        text = path.read_text(encoding="utf-8")
        # Lien de ressource au type MIME du format ; contenu inliné = texte
        # exporté, sous l'URI du fichier.
        links = _of_type(blocks, types.ResourceLink)
        assert len(links) == 1 and links[0].mimeType == "application/x-bibtex"
        embedded = _of_type(blocks, types.EmbeddedResource)
        assert len(embedded) == 1
        assert embedded[0].resource.mimeType == "application/x-bibtex"
        assert embedded[0].resource.text == text
        assert str(embedded[0].resource.uri) == f"corpus://{name}"
        assert st["format"] == "bibtex" and st["export_count"] == 2
        assert st["item_count"] == 2 and st["inline_format"] == "bibtex"
        # Le bloc inliné n'est pas du JSON : le drapeau du sprint 05 le dit.
        assert st["inline_json"] is False and st["inline_export"] is True
        assert st["inline_bytes"] == len(text.encode("utf-8"))
        assert st["annotes"] == 1 and st["include_provenance"] is False
        # Invariant 1 : aucune métadonnée de moisson.
        assert "Sources" not in text and "hal-01" not in text
        assert "annote = {à relire}" in text
        summary = blocks[0].text
        assert "Export BibTeX réussi" in summary and name in summary

    def test_resource_round_trip_outside_output_dir(self, stdio_dirs):
        out, elsewhere = stdio_dirs
        corpus = _write_corpus(elsewhere / "corpus_x.json")
        blocks, st = _call({"corpus_path": str(corpus), "format": "ris"})
        uri = st["resources"][0]
        assert uri.endswith(".ris")
        content = _run(mcp_server.read_resource(uri))
        assert content.startswith("TY  - JOUR")
        link = _of_type(blocks, types.ResourceLink)[0]
        assert link.mimeType == "application/x-research-info-systems"

    def test_output_path_honored_and_options(self, stdio_dirs):
        out, elsewhere = stdio_dirs
        corpus = _write_corpus(out / "corpus_x.json")
        target = elsewhere / "refs.bib"
        blocks, st = _call({"corpus_path": str(corpus), "format": "biblatex",
                            "output_path": str(target), "ascii": True,
                            "include_provenance": True})
        text = target.read_text(encoding="utf-8")
        assert text.isascii()
        assert "journaltitle = {Revue}" in text
        assert "note = {Sources: HAL, Cairn ; HAL ID: hal-01}" in text
        assert st["ascii"] is True and st["include_provenance"] is True

    def test_over_item_threshold_link_kept_and_readable(self, stdio_dirs,
                                                        monkeypatch):
        out, _ = stdio_dirs
        corpus = _write_corpus(out / "corpus_x.json")
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 1)
        blocks, st = _call({"corpus_path": str(corpus), "inline": "full"})
        assert not _of_type(blocks, types.EmbeddedResource)
        link, = _of_type(blocks, types.ResourceLink)
        text = blocks[0].text
        # Message propre à l'export (plus de « JSON non inliné »).
        assert "JSON non inliné" not in text
        assert f"(Export non inliné : 2 notices > seuil 1. Fichier complet " \
               f"via la resource {link.uri})" in text
        assert st["inline_json"] is False and st["inline_export"] is False
        assert st["inline_skipped_reason"] == "2 notices > seuil 1"
        assert "inline_bytes" not in st and "inline_format" not in st
        # Le lien annoncé se relit : c'est le texte BibTeX.
        name = next(iter(st["files"]))
        content = _run(mcp_server.read_resource(str(link.uri)))
        assert content == (out / name).read_text(encoding="utf-8")

    def test_small_export_inlined_although_notices_json_is_too_big(
            self, stdio_dirs, monkeypatch):
        out, _ = stdio_dirs
        # extra volumineux : alourdit le JSON des notices, jamais exporté.
        items = [dict(ITEMS[1], extra="Sources: Isidore\nNote: " + "x" * 5000)]
        corpus = _write_corpus(out / "corpus_x.json", items)
        monkeypatch.setattr(mcp_server, "INLINE_MAX_BYTES", 2000)
        blocks, st = _call({"corpus_path": str(corpus), "inline": "full"})
        name = next(iter(st["files"]))
        text = (out / name).read_text(encoding="utf-8")
        assert len(text.encode("utf-8")) < 2000
        embedded, = _of_type(blocks, types.EmbeddedResource)
        assert embedded.resource.text == text
        assert "non inliné" not in blocks[0].text
        assert st["inline_export"] is True and st["inline_json"] is False
        assert "inline_skipped_reason" not in st

    def test_over_byte_cap_link_kept_and_readable(self, stdio_dirs,
                                                  monkeypatch):
        out, _ = stdio_dirs
        corpus = _write_corpus(out / "corpus_x.json")
        monkeypatch.setattr(mcp_server, "INLINE_MAX_BYTES", 100)
        blocks, st = _call({"corpus_path": str(corpus), "format": "ris",
                            "inline": "full"})
        assert not _of_type(blocks, types.EmbeddedResource)
        link, = _of_type(blocks, types.ResourceLink)
        assert st["inline_skipped_reason"] == \
            f"{st['size_bytes']} octets > plafond 100 octets"
        assert "Fichier complet via la resource" in blocks[0].text
        assert _run(mcp_server.read_resource(str(link.uri))).startswith(
            "TY  - JOUR")

    def test_export_over_resource_cap_promises_no_dead_link(self, stdio_dirs):
        # Export réel au-delà de RESOURCE_MAX_BYTES (2 Mo, valeur par défaut) :
        # read_resource ne sait pas rendre un .bib tronqué, aucun lien n'est
        # donc promis ; le fichier reste désigné par son chemin.
        out, _ = stdio_dirs
        items = [{"itemType": "journalArticle", "title": f"Titre {i}",
                  "creators": [{"creatorType": "author", "lastName": f"A{i}",
                                "firstName": "J"}],
                  "date": "2021", "abstractNote": "Résumé " * 400}
                 for i in range(700)]
        corpus = _write_corpus(out / "corpus_gros.json", items)
        blocks, st = _call({"corpus_path": str(corpus)})
        assert st["size_bytes"] > mcp_server.RESOURCE_MAX_BYTES
        name = next(iter(st["files"]))
        uri = f"corpus://{name}"
        assert not _of_type(blocks, types.ResourceLink)
        assert not _of_type(blocks, types.EmbeddedResource)
        assert uri not in st["resources"]
        assert st["resource_skipped_reason"].endswith(
            f"> plafond de lecture {mcp_server.RESOURCE_MAX_BYTES} octets")
        text = blocks[0].text
        assert "Fichier complet via la resource" not in text
        assert "aucun lien corpus:// n'est émis" in text
        assert f"Chemin complet : {out / name}" in text
        assert st["files"][name] == str(out / name)
        # Ce qu'aurait donné le lien : une erreur, d'où son absence.
        assert "error" in json.loads(_run(mcp_server.read_resource(uri)))

    @pytest.mark.parametrize("name,content,fragment", [
        ("corpus_x.csl.json", ITEMS, "CSL-JSON"),
        ("corpus_y.json", [{"id": "a", "type": "article-journal",
                            "title": "T"}], "CSL-JSON"),
        ("corpus_x.manifest.json", {"format": 1}, "fichier associé"),
        ("corpus_z.json", {"pas": "une liste"}, "tableau"),
    ])
    def test_input_refusals(self, stdio_dirs, name, content, fragment):
        out, _ = stdio_dirs
        path = out / name
        path.write_text(json.dumps(content), encoding="utf-8")
        res = _call({"corpus_path": str(path)})
        assert isinstance(res, types.CallToolResult) and res.isError
        assert fragment in res.content[0].text
        assert not list(out.glob("*.bib"))

    def test_other_refusals(self, stdio_dirs):
        out, _ = stdio_dirs
        corpus = _write_corpus(out / "corpus_x.json")
        cases = [
            ({"format": "endnote"}, "Format inconnu"),
            ({"output_path": str(out / "x.json")}, "réservés"),
            ({"output_path": str(out / "x.ris")}, "incohérente"),
            ({"format": "ris", "output_path": str(out / "x.bib")},
             "incohérente"),
        ]
        for extra, fragment in cases:
            res = _call({"corpus_path": str(corpus), **extra})
            assert res.isError, extra
            assert fragment in res.content[0].text, (extra, res.content)
        res = _call({"corpus_path": str(out / "absent.json")})
        assert res.isError and "introuvable" in res.content[0].text
        assert sorted(p.name for p in out.iterdir()) == ["corpus_x.json"]


# === HTTP ===

class TestHttp:
    def test_refused_while_download_does_not_serve_the_extension(
            self, http_mode, monkeypatch):
        _write_corpus(http_mode / "corpus_x.json")
        monkeypatch.setattr(mcp_server, "is_downloadable_file",
                            lambda path: False)
        res = _call({"corpus_path": "corpus_x.json"})
        assert res.isError
        assert "téléchargement" in res.content[0].text
        assert str(http_mode) not in res.content[0].text
        assert not list(http_mode.glob("*.bib"))

    def test_download_urls_only_no_path_no_inline(self, http_mode):
        _write_corpus(http_mode / "corpus_x.json")
        blocks, st = _call({"corpus_path": "corpus_x.json", "format": "ris"})
        assert not _of_type(blocks, types.EmbeddedResource)
        assert not _of_type(blocks, types.ResourceLink)
        (name, url), = st["download_urls"].items()
        assert name.endswith(".ris") and (http_mode / name).is_file()
        assert url == f"https://moisson.example.org/download/{name}"
        dumped = json.dumps(st, ensure_ascii=False) + blocks[0].text
        assert str(http_mode) not in dumped
        assert "files" not in st and st["transport"] == "http"
        assert st["source_corpus"] == "corpus_x.json"
        # Jamais de contenu bibliographique inliné en HTTP.
        assert "TY  -" not in dumped

    def test_output_path_confined(self, http_mode, tmp_path_factory):
        _write_corpus(http_mode / "corpus_x.json")
        outside = tmp_path_factory.mktemp("dehors") / "x.bib"
        res = _call({"corpus_path": "corpus_x.json",
                     "output_path": str(outside)})
        assert res.isError and not outside.exists()
        sub = http_mode / "sous"
        sub.mkdir()
        res = _call({"corpus_path": "corpus_x.json",
                     "output_path": str(sub / "x.bib")})
        assert res.isError and "sous-dossier" in res.content[0].text
        res = _call({"corpus_path": "corpus_x.json",
                     "output_path": "x.txt"})
        assert res.isError and "attendue" in res.content[0].text
        res = _call({"corpus_path": "corpus_x.json",
                     "output_path": "../x.bib"})
        assert res.isError
        blocks, st = _call({"corpus_path": "corpus_x.json",
                            "output_path": "refs.bib"})
        assert list(st["download_urls"]) == ["refs.bib"]
        assert (http_mode / "refs.bib").is_file()

    def test_input_confined(self, http_mode, tmp_path_factory):
        outside = _write_corpus(tmp_path_factory.mktemp("dehors")
                                / "corpus_x.json")
        res = _call({"corpus_path": str(outside)})
        assert res.isError


@pytest.mark.parametrize("name,media", [
    ("corpus_x_12345678.bib", "application/x-bibtex; charset=utf-8"),
    ("corpus_x_12345678.ris",
     "application/x-research-info-systems; charset=utf-8"),
])
def test_download_endpoint_serves_exports(tmp_path, monkeypatch, name, media):
    # Classification des exports intégrée (utils.output_files, lot D1).
    assert is_downloadable_file(name)
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    (tmp_path / name).write_text("@misc{a,\n}\n", encoding="utf-8")
    resp = _run(mcp_server._download_endpoint(
        SimpleNamespace(path_params={"filename": name})))
    assert resp.status_code == 200
    assert resp.media_type == media


# === CLI ===

class TestCli:
    def test_export_subcommand(self, tmp_path, capsys):
        corpus = _write_corpus(tmp_path / "corpus_x.json")
        out = tmp_path / "refs.ris"
        assert cli.main(["export", str(corpus), "--format", "ris",
                         "-o", str(out)]) == 0
        assert out.read_bytes().startswith(b"TY  - JOUR\r\n")
        assert "Export RIS : 2 entrée(s)" in capsys.readouterr().out
        assert cli.main(["export", str(corpus)]) == 0
        assert len(list(tmp_path.glob("corpus_x_*.bib"))) == 1

    def test_export_refusal_exit_code(self, tmp_path, capsys):
        csl = tmp_path / "corpus_x.csl.json"
        csl.write_text("[]", encoding="utf-8")
        assert cli.main(["export", str(csl)]) == 2
        assert "CSL-JSON" in capsys.readouterr().err

    def test_help_lists_export(self, capsys):
        cli.main(["--help"])
        assert "moisson-shs export" in capsys.readouterr().out
        assert "export" in cli.SUBCOMMANDS
