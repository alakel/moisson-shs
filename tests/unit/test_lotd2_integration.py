"""
Tests d'intégration du lot D2 (OPT-17 digest et ``corpus_query``, OPT-19
``corpus_diff``).

Raccords vérifiés, hors réseau et sans socket :

- détection du format entrant (``utils.manifest.zotero_input_error``) : un
  rapport de comparaison (``.diff.json``, ou contenu ``moisson-shs/diff``
  renommé) et un rapport DOI renommé ``.json`` sont refusés clairement par
  tous les outils qui exigent un corpus Zotero interne ;
- ``_load_zotero_input`` refuse un contenu qui n'est pas un tableau de
  notices (auparavant une ``AttributeError`` dans corpus_filter) ;
- même contrat de retour pour ``corpus_diff`` et ``corpus_query`` : plafond
  en octets en stdio (échantillons par défaut sous ``DIGEST_MAX_BYTES``,
  demande explicite sous ``QUERY_MAX_BYTES``), liens seuls en HTTP (aucun
  chemin disque, aucun titre ni JSON bibliographique inline) ;
- outils : les neuf historiques plus ``corpus_export``, ``corpus_query`` et
  ``corpus_diff``, sans doublon.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
from utils import corpus_query as CQ  # noqa: E402
from utils import manifest as mf  # noqa: E402

HISTORICAL_TOOLS = {
    "moisson_search", "corpus_analyze", "corpus_list", "fetch_by_doi",
    "search_by_author", "corpus_merge", "corpus_to_csl", "corpus_filter",
    "corpus_resolve_dois",
}
LOT_D_TOOLS = {"corpus_export", "corpus_query", "corpus_diff"}

SA = "corpus_veille_20260801_100000_aaaaaaaa"
SB = "corpus_veille_20260901_100000_bbbbbbbb"

# Outils qui exigent un corpus Zotero interne, et leurs arguments.
INPUT_TOOLS = {
    "corpus_analyze": lambda p: {"corpus_path": str(p)},
    "corpus_filter": lambda p: {"corpus_path": str(p)},
    "corpus_resolve_dois": lambda p: {"corpus_path": str(p)},
    "corpus_to_csl": lambda p: {"corpus_path": str(p)},
    "corpus_merge": lambda p: {"corpus_paths": [str(p)]},
    "corpus_query": lambda p: {"corpus": str(p)},
    "corpus_export": lambda p: {"corpus_path": str(p), "format": "bibtex"},
}


def _run(coro):
    return asyncio.run(coro)


def _call(name, args):
    return _run(mcp_server.call_tool(name, args))


def _is_error(res) -> bool:
    return isinstance(res, types.CallToolResult) and bool(res.isError)


def _text(res) -> str:
    blocks = res.content if isinstance(res, types.CallToolResult) else res[0]
    return "\n".join(b.text for b in blocks if isinstance(b, types.TextContent))


def _size(res) -> int:
    blocks, structured = res
    return mcp_server._measure_result(blocks, structured)


def art(i, title, *, doi_prefix="10.1000/b", date="2020"):
    return {"itemType": "journalArticle", "title": title, "date": date,
            "DOI": f"{doi_prefix}.{i}",
            "creators": [{"creatorType": "author", "lastName": f"Nom{i}",
                          "firstName": "Jean"}],
            "abstractNote": "Résumé d'éditeur " * 20,
            "extra": "Sources: HAL"}


def long_title(i, tag):
    return (f"{tag} {i} : " + "discours numérique, plateformes et "
            "médiations éditoriales dans les sciences humaines " * 3)[:190]


def write(directory: Path, name: str, content) -> Path:
    path = directory / name
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def stdio_dir(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", out)
    return out


@pytest.fixture
def http_dir(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", out)
    monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                        "https://moisson.example.org")
    return out


# === Détection du format entrant ===

class TestInputFormat:
    def test_report_format_constants_match_their_modules(self):
        from utils.corpus_diff import DIFF_FORMAT
        from utils.doi_resolver import REPORT_FORMAT
        assert mf.DIFF_REPORT_FORMAT == DIFF_FORMAT
        assert mf.DOI_REPORT_FORMAT == REPORT_FORMAT

    def test_zotero_input_error_refuses_reports(self, tmp_path):
        diff_doc = {"format": "moisson-shs/diff", "version": 1}
        doi_doc = {"format": "moisson-shs/doi-report", "version": 1}
        by_name = mf.zotero_input_error(tmp_path / f"{SB}.diff.json",
                                        tool="corpus_filter")
        assert by_name and "rapport de comparaison (corpus_diff)" in by_name
        renamed = mf.zotero_input_error(tmp_path / "corpus_x.json", diff_doc,
                                        tool="corpus_filter", loaded=True)
        assert renamed and "rapport de comparaison" in renamed
        doi = mf.zotero_input_error(tmp_path / "corpus_x.json", doi_doc,
                                    tool="corpus_filter", loaded=True)
        assert doi and "rapport de résolution DOI" in doi
        for message in (by_name, renamed, doi):
            assert str(tmp_path) not in message
        # Un corpus Zotero reste accepté ; un objet quelconque n'est pas
        # confondu avec un rapport.
        assert mf.zotero_input_error(tmp_path / "corpus_x.json",
                                     [art(1, "T")], tool="t",
                                     loaded=True) is None
        assert mf.zotero_input_error(tmp_path / "corpus_x.json",
                                     {"items": []}, tool="t",
                                     loaded=True) is None

    @pytest.mark.parametrize("tool", sorted(INPUT_TOOLS))
    @pytest.mark.parametrize("name,content,fragment", [
        (f"{SB}.diff.json", {"format": "moisson-shs/diff", "version": 1},
         None),
        ("corpus_rapport_renomme.json",
         {"format": "moisson-shs/diff", "version": 1},
         "rapport de comparaison"),
        ("corpus_doi_renomme.json",
         {"format": "moisson-shs/doi-report", "version": 1},
         "rapport de résolution DOI"),
    ])
    def test_every_input_tool_refuses_reports(self, stdio_dir, tool, name,
                                              content, fragment):
        path = write(stdio_dir, name, content)
        before = sorted(p.name for p in stdio_dir.iterdir())
        res = _call(tool, INPUT_TOOLS[tool](path))
        assert _is_error(res), (tool, res)
        text = _text(res)
        assert "AttributeError" not in text and "Traceback" not in text
        if fragment is None:
            # Par le nom : message commun, sauf corpus_export qui refuse
            # tout fichier associé (message d'OPT-18, figé par ses tests).
            fragment = ("fichier associé" if tool == "corpus_export"
                        else "rapport de comparaison")
        assert fragment in text
        assert str(stdio_dir) not in text
        # Rien d'écrit (ni corpus fusionné, ni sélection, ni export).
        assert sorted(p.name for p in stdio_dir.iterdir()) == before

    @pytest.mark.parametrize("content", [{"items": []}, "texte", [1, 2],
                                         [{"itemType": "book"}, "x"]])
    def test_corpus_filter_refuses_non_array_content(self, stdio_dir, content):
        path = write(stdio_dir, "corpus_pas_un_tableau.json", content)
        res = _call("corpus_filter", {"corpus_path": str(path)})
        assert _is_error(res)
        text = _text(res)
        assert "AttributeError" not in text
        assert "tableau d'items Zotero" in text
        assert [p.name for p in stdio_dir.iterdir()] == [path.name]

    @pytest.mark.parametrize("tool", ["corpus_analyze", "corpus_resolve_dois",
                                      "corpus_to_csl"])
    def test_historical_non_array_message_unchanged(self, stdio_dir, tool):
        path = write(stdio_dir, "corpus_objet.json", {"items": []})
        res = _call(tool, {"corpus_path": str(path)})
        assert _is_error(res)
        assert "Le fichier ne contient pas un tableau d'items Zotero." in _text(res)

    def test_real_diff_report_is_refused_by_derived_tools(self, stdio_dir):
        a = write(stdio_dir, f"{SA}.json", [art(1, "Commun"), art(2, "Retiré")])
        b = write(stdio_dir, f"{SB}.json", [art(1, "Commun"), art(3, "Nouveau")])
        res = _call("corpus_diff", {"corpus_a": str(a), "corpus_b": str(b)})
        assert isinstance(res, tuple), res
        report = stdio_dir / f"{SB}.diff.json"
        assert report.is_file()
        for tool in ("corpus_filter", "corpus_analyze", "corpus_to_csl",
                     "corpus_merge", "corpus_query"):
            res = _call(tool, INPUT_TOOLS[tool](report))
            assert _is_error(res)
            assert "rapport de comparaison" in _text(res), tool


# === Contrat de retour : corpus_diff et corpus_query ===

def _big_pair(directory: Path):
    """A et B sans notice commune : 150 retraits et 150 ajouts aux titres
    longs (échantillons par défaut au-delà de 15 Ko avant plafond)."""
    items_a = [art(i, long_title(i, "Ancien"), doi_prefix="10.1000/a")
               for i in range(150)]
    items_b = [art(i, long_title(i, "Récent"), doi_prefix="10.1000/b",
                   date="2026-09-15") for i in range(150)]
    return (write(directory, f"{SA}.json", items_a),
            write(directory, f"{SB}.json", items_b))


class TestReturnContract:
    def test_diff_default_sample_fits_the_digest_budget(self, stdio_dir):
        a, b = _big_pair(stdio_dir)
        res = _call("corpus_diff", {"corpus_a": str(a), "corpus_b": str(b)})
        assert isinstance(res, tuple), res
        blocks, st = res
        assert _size(res) <= CQ.DIGEST_MAX_BYTES
        cap = st["byte_cap"]
        assert cap == {"max_bytes": CQ.DIGEST_MAX_BYTES, "reduced": True,
                       "sample_requested": 20}
        n = st["sample_size"]
        assert 0 < n < 20
        assert "Échantillons ramenés à" in blocks[0].text
        # Échantillons = têtes des listes complètes du rapport (même ordre).
        report = json.loads((stdio_dir / f"{SB}.diff.json").read_text("utf-8"))
        assert len(report["added"]) == len(report["removed"]) == 150
        assert [e["id"] for e in st["samples"]["added"]] == \
            [e["id"] for e in report["added"][:n]]
        assert [e["id"] for e in st["samples"]["removed"]] == \
            [e["id"] for e in report["removed"][:n]]

    def test_diff_explicit_sample_uses_the_consultation_cap(self, stdio_dir):
        a, b = _big_pair(stdio_dir)
        res = _call("corpus_diff", {"corpus_a": str(a), "corpus_b": str(b),
                                    "sample": 100})
        blocks, st = res
        size = _size(res)
        assert CQ.DIGEST_MAX_BYTES < size <= CQ.QUERY_MAX_BYTES
        assert st["byte_cap"]["max_bytes"] == CQ.QUERY_MAX_BYTES
        assert st["sample_size"] <= 100
        assert st["byte_cap"]["reduced"] is (st["sample_size"] < 100)

    def test_diff_small_result_is_not_reduced(self, stdio_dir):
        a = write(stdio_dir, f"{SA}.json", [art(1, "Commun")])
        b = write(stdio_dir, f"{SB}.json", [art(1, "Commun"), art(2, "Neuf")])
        blocks, st = _call("corpus_diff", {"corpus_a": str(a),
                                           "corpus_b": str(b)})
        assert st["byte_cap"]["reduced"] is False
        assert st["sample_size"] == 20
        assert "Échantillons ramenés" not in blocks[0].text

    def test_diff_cap_holds_with_a_small_digest_budget(self, stdio_dir,
                                                       monkeypatch):
        monkeypatch.setattr(CQ, "DIGEST_MAX_BYTES", 6000)
        a, b = _big_pair(stdio_dir)
        res = _call("corpus_diff", {"corpus_a": str(a), "corpus_b": str(b)})
        assert _size(res) <= 6000
        assert res[1]["sample_size"] < 20

    def test_query_default_page_is_bounded(self, stdio_dir):
        _a, b = _big_pair(stdio_dir)
        res = _call("corpus_query", {"corpus": str(b)})
        assert isinstance(res, tuple), res
        assert _size(res) <= CQ.QUERY_MAX_BYTES
        res = _call("corpus_query", {"corpus": str(b), "limit": 100,
                                     "fields": ["id", "title", "abstract"]})
        assert _size(res) <= CQ.QUERY_MAX_BYTES

    def test_http_both_tools_return_links_without_paths_or_titles(
            self, http_dir):
        a, b = _big_pair(http_dir)
        diff = _call("corpus_diff", {"corpus_a": a.name, "corpus_b": b.name})
        query = _call("corpus_query", {"corpus": b.name, "query": "discours"})
        for res in (diff, query):
            assert isinstance(res, tuple), res
            blocks, st = res
            dumped = json.dumps(st, ensure_ascii=False) + _text(res)
            assert str(http_dir) not in dumped
            assert "Récent 1 :" not in dumped          # aucun titre inline
            assert "Résumé d'éditeur" not in dumped    # aucun résumé inline
            assert not [x for x in blocks
                        if isinstance(x, (types.ResourceLink,
                                          types.EmbeddedResource))]
            urls = st.get("download_urls") or {}
            assert urls and all(u.startswith("https://moisson.example.org/")
                                for u in urls.values())
            assert _size(res) <= CQ.DIGEST_MAX_BYTES
        assert diff[1]["byte_cap"]["max_bytes"] == CQ.DIGEST_MAX_BYTES


# === Outils déclarés ===

def test_tools_are_the_historical_nine_plus_lot_d():
    names = [t.name for t in _run(mcp_server.list_tools())]
    assert len(names) == len(set(names))
    assert HISTORICAL_TOOLS | LOT_D_TOOLS <= set(names)
    props = {t.name: t.inputSchema["properties"]
             for t in _run(mcp_server.list_tools())}
    # corpus_diff et corpus_query ne sont pas des producteurs de corpus :
    # pas d'argument inline ; les producteurs l'ont tous.
    for name in ("corpus_diff", "corpus_query"):
        assert "inline" not in props[name]
    for name in ("moisson_search", "corpus_filter", "corpus_to_csl",
                 "corpus_merge", "search_by_author", "corpus_resolve_dois",
                 "corpus_export"):
        assert {"inline", "top"} <= set(props[name])
