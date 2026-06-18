"""
Tests des résolveurs internationaux de utils.lookups (fetch_by_doi et
search_by_author), avec requests mocké et clés simulées.
"""

from unittest.mock import patch

import pytest

from utils.lookups import (
    _fetch_from_openalex, _fetch_from_s2, _fetch_from_core,
    _search_openalex_author, _search_s2_author,
)


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class TestFetchResolvers:
    def test_openalex_requires_key(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        # Sans clé : pas d'appel réseau, retourne None
        with patch("requests.get") as mock_get:
            assert _fetch_from_openalex("10.1/x") is None
            mock_get.assert_not_called()

    def test_openalex_maps_with_key(self, monkeypatch, openalex_work):
        monkeypatch.setenv("OPENALEX_API_KEY", "KEY")
        with patch("requests.get", return_value=_Resp(200, openalex_work)):
            item = _fetch_from_openalex("10.1234/jaip.2023.12.45")
        assert item is not None
        assert item["DOI"] == "10.1234/jaip.2023.12.45"
        assert "Sources: OpenAlex" in item["extra"]

    def test_s2_works_without_key(self, monkeypatch, s2_paper):
        monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
        with patch("requests.get", return_value=_Resp(200, s2_paper)):
            item = _fetch_from_s2("10.5555/llm.2022.99")
        assert item is not None
        assert item["DOI"] == "10.5555/llm.2022.99"
        assert "Sources: Semantic Scholar" in item["extra"]

    def test_s2_non_200_returns_none(self):
        with patch("requests.get", return_value=_Resp(404, {})):
            assert _fetch_from_s2("10.5555/x") is None

    def test_core_requires_key(self, monkeypatch):
        monkeypatch.delenv("CORE_API_KEY", raising=False)
        with patch("requests.get") as mock_get:
            assert _fetch_from_core("10.1/x") is None
            mock_get.assert_not_called()

    def test_core_maps_with_key(self, monkeypatch, core_work):
        monkeypatch.setenv("CORE_API_KEY", "KEY")
        with patch("requests.get",
                   return_value=_Resp(200, {"results": [core_work]})):
            item = _fetch_from_core("10.7777/core.2021.1")
        assert item is not None
        assert item["DOI"] == "10.7777/core.2021.1"
        assert "Sources: CORE" in item["extra"]

    def test_core_empty_results(self, monkeypatch):
        monkeypatch.setenv("CORE_API_KEY", "KEY")
        with patch("requests.get", return_value=_Resp(200, {"results": []})):
            assert _fetch_from_core("10.7777/none") is None


class TestAuthorSearch:
    def test_s2_author_two_calls(self, monkeypatch, s2_paper):
        monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
        responses = [
            _Resp(200, {"data": [{"authorId": "A99", "name": "Alice Wong"}]}),
            _Resp(200, {"data": [s2_paper]}),
        ]
        with patch("requests.get", side_effect=responses):
            items = _search_s2_author("Alice Wong", max_per_source=10)
        assert items and len(items) == 1
        assert items[0]["DOI"] == "10.5555/llm.2022.99"

    def test_s2_author_no_match(self):
        with patch("requests.get", return_value=_Resp(200, {"data": []})):
            assert _search_s2_author("Nobody", max_per_source=10) is None

    def test_openalex_author_requires_key(self, monkeypatch):
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        with patch("requests.get") as mock_get:
            assert _search_openalex_author("X", 10) is None
            mock_get.assert_not_called()

    def test_openalex_author_by_name(self, monkeypatch, openalex_work):
        monkeypatch.setenv("OPENALEX_API_KEY", "KEY")
        responses = [
            _Resp(200, {"results": [{"id": "https://openalex.org/A1"}]}),
            _Resp(200, {"results": [openalex_work]}),
        ]
        with patch("requests.get", side_effect=responses):
            items = _search_openalex_author("Jane Doe", max_per_source=10)
        assert items and len(items) == 1
        assert items[0]["DOI"] == "10.1234/jaip.2023.12.45"

    def test_openalex_author_by_orcid(self, monkeypatch, openalex_work):
        monkeypatch.setenv("OPENALEX_API_KEY", "KEY")
        # Avec ORCID, un seul appel works (pas de résolution d'auteur)
        with patch("requests.get",
                   return_value=_Resp(200, {"results": [openalex_work]})) as mg:
            items = _search_openalex_author(
                "Jane Doe", 10, orcid="0000-0002-1234-5678"
            )
        assert items and len(items) == 1
        assert mg.call_count == 1
