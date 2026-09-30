"""
Tests OPT-16 : détection du format entrant.

Une opération qui exige le corpus Zotero JSON interne (``corpus_filter``,
``corpus_merge``, ``corpus_resolve_dois``, ``corpus_analyze``,
``corpus_to_csl``) refuse clairement, en erreur MCP (``isError``) et sans
rien écrire : un fichier CSL-JSON (par son nom ``.csl.json`` ou par son
contenu), un manifeste, un rapport DOI. Un corpus Zotero (même vide) est
accepté.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
from utils import manifest as mf  # noqa: E402
from utils.corpus_ops import CorpusFormatError, merge_corpora  # noqa: E402

ZOTERO = [{"itemType": "journalArticle", "title": "Titre", "date": "2020",
           "DOI": "10.0/x.1", "extra": "Sources: HAL"}]
CSL = [{"id": "x1", "type": "article-journal", "title": "Titre",
        "issued": {"date-parts": [[2020]]}}]
MANIFEST = {"format": "moisson-shs/manifest", "version": 1, "files": []}

SINGLE_INPUT_TOOLS = ["corpus_filter", "corpus_resolve_dois", "corpus_analyze",
                      "corpus_to_csl"]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def out(tmp_path, monkeypatch):
    base = tmp_path / "out"
    base.mkdir()
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", base)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    (base / "corpus_a.json").write_text(json.dumps(ZOTERO), encoding="utf-8")
    (base / "corpus_a.csl.json").write_text(json.dumps(CSL), encoding="utf-8")
    (base / "corpus_a.manifest.json").write_text(json.dumps(MANIFEST),
                                                 encoding="utf-8")
    (base / "corpus_a.doi_report.json").write_text('{"counts": {}}',
                                                   encoding="utf-8")
    # CSL-JSON sous un nom de corpus Zotero : détecté par le contenu.
    (base / "corpus_mal_nomme.json").write_text(json.dumps(CSL), encoding="utf-8")
    return base


def _names(directory):
    return sorted(p.name for p in directory.iterdir())


def _call(tool, path):
    return _run(mcp_server.call_tool(tool, {"corpus_path": str(path)}))


class TestSingleInputTools:
    @pytest.mark.parametrize("tool", SINGLE_INPUT_TOOLS)
    @pytest.mark.parametrize("name, fragment", [
        ("corpus_a.csl.json", "CSL-JSON"),
        ("corpus_mal_nomme.json", "CSL-JSON"),
        ("corpus_a.manifest.json", "manifeste"),
        ("corpus_a.doi_report.json", "rapport de résolution DOI"),
    ])
    def test_refused_as_tool_error_nothing_written(self, out, tool, name,
                                                   fragment):
        before = _names(out)
        result = _call(tool, out / name)
        assert result.isError, (tool, name)
        text = result.content[0].text
        assert text.startswith("Format refusé")
        assert fragment in text and tool in text
        assert str(out) not in text
        assert _names(out) == before

    def test_hint_names_the_zotero_sibling(self, out):
        text = _call("corpus_filter", out / "corpus_a.csl.json").content[0].text
        assert "utiliser corpus_a.json" in text

    @pytest.mark.parametrize("tool", ["corpus_filter", "corpus_analyze",
                                      "corpus_to_csl"])
    def test_zotero_corpus_accepted(self, out, tool):
        result = _call(tool, out / "corpus_a.json")
        assert not getattr(result, "isError", False), result

    def test_empty_zotero_corpus_accepted(self, out):
        (out / "corpus_vide.json").write_text("[]", encoding="utf-8")
        result = _call("corpus_filter", out / "corpus_vide.json")
        assert not getattr(result, "isError", False)

    def test_http_refusal_has_no_path(self, out, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        result = _call("corpus_filter", "corpus_a.csl.json")
        assert result.isError
        assert str(out) not in result.content[0].text


class TestMerge:
    @pytest.mark.parametrize("name", ["corpus_a.csl.json", "corpus_mal_nomme.json",
                                      "corpus_a.manifest.json"])
    def test_merge_refuses_non_zotero_input(self, out, name):
        before = _names(out)
        result = _run(mcp_server.call_tool("corpus_merge", {
            "corpus_paths": [str(out / "corpus_a.json"), str(out / name)]}))
        assert result.isError
        assert result.content[0].text.startswith("Format refusé")
        assert "corpus_merge" in result.content[0].text
        assert _names(out) == before

    def test_merge_corpora_raises(self, out):
        with pytest.raises(CorpusFormatError):
            merge_corpora([out / "corpus_a.json", out / "corpus_a.csl.json"])

    def test_merge_zotero_inputs_accepted(self, out):
        (out / "corpus_b.json").write_text(json.dumps(ZOTERO), encoding="utf-8")
        result = _run(mcp_server.call_tool("corpus_merge", {
            "corpus_paths": [str(out / "corpus_a.json"), str(out / "corpus_b.json")]}))
        assert isinstance(result, tuple)
        assert result[1]["merged_count"] == 1


class TestDetection:
    @pytest.mark.parametrize("data, expected", [
        (ZOTERO, "zotero"),
        (CSL, "csl"),
        ([], "empty"),
        (MANIFEST, "manifest"),
        ({"autre": 1}, "unknown"),
        ([1, 2], "unknown"),
        (ZOTERO + CSL, "unknown"),
    ])
    def test_detect_items_format(self, data, expected):
        assert mf.detect_items_format(data) == expected

    def test_zotero_input_error_by_name_and_content(self, tmp_path):
        assert mf.zotero_input_error(tmp_path / "corpus_x.json", ZOTERO,
                                     tool="t", loaded=True) is None
        assert mf.zotero_input_error(tmp_path / "corpus_x.csl.json",
                                     tool="t") is not None
        assert mf.zotero_input_error(tmp_path / "corpus_x.json", CSL,
                                     tool="t", loaded=True) is not None
        assert mf.zotero_input_error(tmp_path / "corpus_x.json", MANIFEST,
                                     tool="t", loaded=True) is not None
