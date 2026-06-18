"""Tests unitaires pour utils.deduplication."""

import pytest

from utils.deduplication import (
    get_doi, get_source_uri, get_url_normalized,
    compute_fingerprint, merge_items, deduplicate,
)


class TestGetDoi:
    def test_simple_doi(self):
        assert get_doi({"DOI": "10.1234/abc"}) == "10.1234/abc"

    def test_lowercases(self):
        assert get_doi({"DOI": "10.1234/ABC"}) == "10.1234/abc"

    def test_strips_https_prefix(self):
        assert get_doi({"DOI": "https://doi.org/10.1234/x"}) == "10.1234/x"

    def test_strips_http_prefix(self):
        assert get_doi({"DOI": "http://doi.org/10.1234/x"}) == "10.1234/x"

    def test_strips_doi_prefix(self):
        assert get_doi({"DOI": "doi:10.1234/x"}) == "10.1234/x"

    def test_empty(self):
        assert get_doi({}) is None
        assert get_doi({"DOI": ""}) is None
        assert get_doi({"DOI": "  "}) is None


class TestGetSourceUri:
    def test_hal_id(self):
        item = {"extra": "Sources: HAL\nHAL ID: hal-04123"}
        assert "hal-04123" in get_source_uri(item)
        assert "HAL ID:" in get_source_uri(item)

    def test_oai_identifier(self):
        item = {"extra": "OAI Identifier: oai:revues.org:foo/1"}
        assert "oai:revues.org:foo/1" in get_source_uri(item)

    def test_isidore_uri(self):
        item = {"extra": "Isidore URI: 20.500.13089/xyz"}
        assert "20.500.13089/xyz" in get_source_uri(item)

    def test_no_uri_returns_none(self):
        item = {"extra": "Sources: HAL"}
        assert get_source_uri(item) is None

    def test_empty_extra(self):
        assert get_source_uri({}) is None
        assert get_source_uri({"extra": ""}) is None


class TestGetUrlNormalized:
    def test_lowercases(self):
        assert get_url_normalized({"url": "HTTPS://Foo.COM/X"}) == "https://foo.com/x"

    def test_strips_trailing_slash(self):
        assert get_url_normalized({"url": "https://x.com/"}) == "https://x.com"

    def test_empty(self):
        assert get_url_normalized({}) is None
        assert get_url_normalized({"url": ""}) is None


class TestComputeFingerprint:
    def test_same_title_same_fingerprint(self):
        a = {"title": "Un titre", "creators": [{"lastName": "X"}], "date": "2020"}
        b = {"title": "Un titre", "creators": [{"lastName": "X"}], "date": "2020"}
        assert compute_fingerprint(a) == compute_fingerprint(b)

    def test_different_titles_different_fingerprint(self):
        a = {"title": "T1", "creators": [{"lastName": "X"}], "date": "2020"}
        b = {"title": "T2", "creators": [{"lastName": "X"}], "date": "2020"}
        assert compute_fingerprint(a) != compute_fingerprint(b)

    def test_typographic_variations_collapse(self):
        a = {"title": "L'Élément clé", "creators": [{"lastName": "Doe"}], "date": "2020"}
        b = {"title": "L Element cle", "creators": [{"lastName": "Doe"}], "date": "2020"}
        assert compute_fingerprint(a) == compute_fingerprint(b)

    def test_handles_missing_author(self):
        a = {"title": "T", "creators": [], "date": "2020"}
        # Doit pas planter
        fp = compute_fingerprint(a)
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_uses_name_field_when_no_lastname(self):
        a = {"title": "T", "creators": [{"name": "Bourdieu"}], "date": "2020"}
        b = {"title": "T", "creators": [{"lastName": "Bourdieu"}], "date": "2020"}
        assert compute_fingerprint(a) == compute_fingerprint(b)


class TestMergeItems:
    def test_single_item_returned_as_is(self):
        item = {"itemType": "book", "title": "X"}
        assert merge_items([item]) == item

    def test_empty_list(self):
        assert merge_items([]) == {}

    def test_fills_missing_fields(self):
        a = {"itemType": "journalArticle", "title": "T", "DOI": "",
             "abstractNote": "", "extra": "Sources: HAL"}
        b = {"itemType": "journalArticle", "title": "T", "DOI": "10.1/x",
             "abstractNote": "Résumé", "extra": "Sources: Isidore"}
        merged = merge_items([a, b])
        assert merged["DOI"] == "10.1/x"
        assert merged["abstractNote"] == "Résumé"

    def test_aggregates_sources_in_extra(self):
        a = {"title": "T", "extra": "Sources: HAL\nHAL ID: hal-1"}
        b = {"title": "T", "extra": "Sources: Isidore\nIsidore URI: x"}
        merged = merge_items([a, b])
        sources_line = [l for l in merged["extra"].split("\n")
                        if l.startswith("Sources:")][0]
        assert "HAL" in sources_line
        assert "Isidore" in sources_line

    def test_preserves_first_creators(self):
        a = {"title": "T", "creators": [{"lastName": "A"}, {"lastName": "B"}]}
        b = {"title": "T", "creators": [{"lastName": "C"}]}
        merged = merge_items([a, b])
        # Conservation des creators du premier item
        assert len(merged["creators"]) == 2


class TestDeduplicate:
    def test_doi_dedup(self):
        items = [
            {"DOI": "10.1/x", "title": "T1", "extra": "Sources: HAL"},
            {"DOI": "10.1/X", "title": "T1", "extra": "Sources: Isidore"},  # casse différente
        ]
        result = deduplicate(items)
        assert len(result) == 1
        # Source agrégée
        sources = [l for l in result[0]["extra"].split("\n")
                   if l.startswith("Sources:")][0]
        assert "HAL" in sources and "Isidore" in sources

    def test_uri_dedup_after_no_doi(self):
        items = [
            {"DOI": "", "title": "T", "extra": "Sources: HAL\nHAL ID: hal-1"},
            {"DOI": "", "title": "T", "extra": "Sources: Isidore\nHAL ID: hal-1"},
        ]
        result = deduplicate(items)
        assert len(result) == 1

    def test_url_dedup(self):
        items = [
            {"url": "https://x.com/a", "title": "T", "extra": "Sources: A"},
            {"url": "https://X.COM/a/", "title": "T", "extra": "Sources: B"},
        ]
        result = deduplicate(items)
        assert len(result) == 1

    def test_fingerprint_dedup(self):
        items = [
            {"title": "Même titre", "creators": [{"lastName": "Doe"}],
             "date": "2020", "extra": "Sources: A"},
            {"title": "même titre", "creators": [{"lastName": "Doe"}],
             "date": "2020", "extra": "Sources: B"},
        ]
        result = deduplicate(items)
        assert len(result) == 1

    def test_distinct_items_not_merged(self):
        items = [
            {"DOI": "10.1/x", "title": "T1", "extra": "Sources: A"},
            {"DOI": "10.2/y", "title": "T2", "extra": "Sources: B"},
        ]
        result = deduplicate(items)
        assert len(result) == 2

    def test_empty_list(self):
        assert deduplicate([]) == []
