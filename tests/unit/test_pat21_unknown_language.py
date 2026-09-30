"""
PAT-21 (retour d'expérience du 2026-09-30, décision DP-8) : langue inconnue.

``corpus_filter(language=…)`` écartait en silence les notices sans langue
(CrossRef et Google Scholar n'en donnent pas), alors que la moisson les
garde. Les deux outils suivent désormais la règle de la moisson : notice sans
langue lisible gardée et comptée, ``keep_unknown_language: false`` l'écarte ;
codes ramenés à l'ISO 639-1 (« fre », « French » → fr).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import mcp_server
from utils.corpus_query import Filters, QueryError, query_corpus, query_lines
from utils.query_plan import apply_local_language_filter, notice_languages


def _item(title, language=None, extra="Sources: HAL", item_type="journalArticle"):
    it = {"itemType": item_type, "title": title, "date": "2024",
          "creators": [{"creatorType": "author", "firstName": "A",
                        "lastName": "Lakel"}], "extra": extra}
    if language is not None:
        it["language"] = language
    return it


ITEMS = [
    _item("Notice en français", "fre"),
    _item("An English notice", "English"),
    _item("Notice sans langue", extra="Sources: CrossRef"),
    _item("Notice indéterminée", "und"),
    _item("Logiciel", extra="Sources: Zenodo\nlanguage: eng",
          item_type="computerProgram"),
]


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    base = tmp_path / "out"
    base.mkdir()
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", base)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    path = base / "corpus_langues.json"
    path.write_text(json.dumps(ITEMS, ensure_ascii=False), encoding="utf-8")
    return path


def _filter(args):
    return asyncio.run(mcp_server._tool_corpus_filter(args))


def _titles(path):
    return [it["title"] for it in json.loads(Path(path).read_text(encoding="utf-8"))]


class TestNoticeLanguages:
    def test_field_and_extra_line(self):
        assert notice_languages(ITEMS[0]) == ["fr"]
        assert notice_languages(ITEMS[1]) == ["en"]
        assert notice_languages(ITEMS[2]) == []
        assert notice_languages(ITEMS[3]) == []
        assert notice_languages(ITEMS[4]) == ["en"]

    def test_harvest_filter_reads_extra(self):
        kept, counts = apply_local_language_filter(ITEMS, ["fr"])
        assert [it["title"] for it in kept] == [
            "Notice en français", "Notice sans langue", "Notice indéterminée"]
        assert counts == {"kept": 3, "removed": 2, "unknown_kept": 2}


class TestCorpusFilter:
    def test_unknown_kept_and_counted_by_default(self, corpus):
        blocks, structured = _filter({"corpus_path": str(corpus),
                                      "language": "fr"})
        out = corpus.with_name("corpus_langues_filtered.json")
        assert _titles(out) == ["Notice en français", "Notice sans langue",
                                "Notice indéterminée"]
        assert structured["unknown_language"] == {"kept": 2, "removed": 0}
        assert structured["filters"]["keep_unknown_language"] is True
        assert "Notices sans langue lisible gardées : 2" in blocks[0].text

    def test_unknown_removed_on_request(self, corpus):
        blocks, structured = _filter({"corpus_path": str(corpus),
                                      "language": "French",
                                      "keep_unknown_language": False})
        out = corpus.with_name("corpus_langues_filtered.json")
        assert _titles(out) == ["Notice en français"]
        assert structured["unknown_language"] == {"kept": 0, "removed": 2}
        assert "Notices sans langue lisible écartées : 2" in blocks[0].text

    def test_software_language_from_extra(self, corpus):
        _, structured = _filter({"corpus_path": str(corpus), "language": "en",
                                 "keep_unknown_language": False})
        out = corpus.with_name("corpus_langues_filtered.json")
        assert _titles(out) == ["An English notice", "Logiciel"]

    def test_no_language_filter_no_count(self, corpus):
        _, structured = _filter({"corpus_path": str(corpus),
                                 "title_contains": "notice"})
        assert "unknown_language" not in structured
        assert "keep_unknown_language" not in structured["filters"]


class TestCorpusQuery:
    def test_codes_normalized(self):
        assert Filters.parse({"languages": ["fre", "English", "xx"]}).languages \
            == ["fr", "en", "xx"]

    def test_unknown_kept_and_counted(self, corpus):
        res = query_corpus(corpus, filters={"languages": ["fr"]})
        assert res.matched == 3 and res.unknown_language_kept == 2
        assert res.as_dict()["unknown_language_kept"] == 2
        assert "Dont 2 sans langue lisible" in query_lines(res)[0]

    def test_unknown_removed_on_request(self, corpus):
        res = query_corpus(corpus, filters={"languages": ["fr"],
                                            "keep_unknown_language": False})
        assert res.matched == 1 and res.unknown_language_kept == 0
        assert res.filters["keep_unknown_language"] is False

    def test_default_not_echoed(self, corpus):
        res = query_corpus(corpus, filters={"languages": ["fr"]})
        assert "keep_unknown_language" not in res.filters

    def test_no_language_filter_no_count(self, corpus):
        res = query_corpus(corpus, filters={"year_min": 2020})
        assert "unknown_language_kept" not in res.as_dict()

    def test_bad_value_refused(self, corpus):
        with pytest.raises(QueryError):
            query_corpus(corpus, filters={"languages": ["fr"],
                                          "keep_unknown_language": "non"})
