"""Tests unitaires pour mappers.isidore_mapper sur fixture réelle."""

import pytest

from mappers.isidore_mapper import (
    map_isidore_to_zotero,
    _pick_multilang, _all_multilang, _extract_creators, _extract_date,
    _extract_types, _extract_all_urls,
)


class TestPickMultilang:
    def test_french_priority(self):
        field = [
            {"@xml:lang": "en", "$": "English"},
            {"@xml:lang": "fr", "$": "Français"},
        ]
        assert _pick_multilang(field) == "Français"

    def test_first_available_fallback(self):
        field = [
            {"@xml:lang": "es", "$": "Español"},
        ]
        assert _pick_multilang(field) == "Español"

    def test_string_passthrough(self):
        assert _pick_multilang("simple string") == "simple string"

    def test_dict_with_dollar(self):
        assert _pick_multilang({"$": "value"}) == "value"

    def test_none_returns_empty(self):
        assert _pick_multilang(None) == ""

    def test_mixed_list_with_strings(self):
        field = ["chaîne", {"@xml:lang": "en", "$": "string"}]
        # Premier non vide
        result = _pick_multilang(field)
        assert result in ("chaîne", "string")


class TestAllMultilang:
    def test_dedup(self):
        field = [{"@xml:lang": "fr", "$": "X"}, {"@xml:lang": "en", "$": "X"}]
        assert _all_multilang(field) == ["X"]

    def test_collects_all_unique(self):
        field = [
            {"@xml:lang": "fr", "$": "A"},
            {"@xml:lang": "en", "$": "B"},
            "C",
        ]
        result = _all_multilang(field)
        assert set(result) == {"A", "B", "C"}


class TestExtractCreators:
    def test_single_creator_dict(self):
        enriched = {"creator": {"firstname": "Jane", "lastname": "Doe"}}
        creators = _extract_creators(enriched)
        assert len(creators) == 1
        assert creators[0]["firstName"] == "Jane"
        assert creators[0]["lastName"] == "Doe"

    def test_multiple_creators_list(self):
        enriched = {"creator": [
            {"firstname": "Jane", "lastname": "Doe"},
            {"firstname": "John", "lastname": "Smith"},
        ]}
        creators = _extract_creators(enriched)
        assert len(creators) == 2

    def test_normalized_author_fallback(self):
        enriched = {"creator": {"@normalizedAuthor": "Bourdieu, Pierre"}}
        creators = _extract_creators(enriched)
        assert creators[0]["lastName"] == "Bourdieu"
        assert creators[0]["firstName"] == "Pierre"

    def test_empty_returns_empty_list(self):
        assert _extract_creators(None) == []
        assert _extract_creators({}) == []


class TestExtractDate:
    def test_normalized_date_priority(self):
        d = {"normalizedDate": "2020-01-01", "@startDate": "2020-06-01"}
        assert _extract_date(d) == "2020-01-01"

    def test_year_fallback(self):
        d = {"year": "2020"}
        assert _extract_date(d) == "2020"

    def test_string_passthrough(self):
        assert _extract_date("2024-05") == "2024-05"

    def test_none_returns_empty(self):
        assert _extract_date(None) == ""


class TestExtractTypes:
    def test_list_of_strings(self):
        result = _extract_types({"type": ["a", "b"]})
        assert result == ["a", "b"]

    def test_single_string(self):
        result = _extract_types({"type": "info:eu-repo/semantics/article"})
        assert result == ["info:eu-repo/semantics/article"]

    def test_empty(self):
        assert _extract_types({}) == []
        assert _extract_types(None) == []


class TestExtractAllUrls:
    def test_primary_and_aggregates(self):
        iso = {
            "url": "https://primary.com",
            "ore": {"aggregates": [
                {"$": "https://alt1.com"},
                {"$": "https://alt2.com"},
            ]}
        }
        urls = _extract_all_urls(iso)
        assert urls[0] == "https://primary.com"
        assert "https://alt1.com" in urls
        assert "https://alt2.com" in urls

    def test_dedup_when_primary_in_aggregates(self):
        iso = {
            "url": "https://x.com",
            "ore": {"aggregates": [{"$": "https://x.com"}]}
        }
        urls = _extract_all_urls(iso)
        assert urls.count("https://x.com") == 1


class TestMapIsidoreToZoteroFixture:
    """Tests sur la fixture réelle Isidore."""

    def test_returns_dict_with_basics(self, isidore_first_item):
        item = map_isidore_to_zotero(isidore_first_item)
        assert "itemType" in item
        assert "title" in item

    def test_title_not_empty(self, isidore_first_item):
        item = map_isidore_to_zotero(isidore_first_item)
        assert item["title"]

    def test_sources_isidore_in_extra(self, isidore_first_item):
        item = map_isidore_to_zotero(isidore_first_item)
        assert "Isidore" in item.get("extra", "")

    def test_isidore_uri_in_extra(self, isidore_first_item):
        item = map_isidore_to_zotero(isidore_first_item)
        if isidore_first_item.get("@uri"):
            assert "Isidore URI" in item["extra"]

    def test_no_empty_creators_dicts(self, isidore_first_item):
        item = map_isidore_to_zotero(isidore_first_item)
        for c in item.get("creators", []):
            # Soit firstName+lastName, soit name, mais pas dict vide
            assert (
                c.get("firstName") or c.get("lastName") or c.get("name")
            ), f"Creator vide : {c}"

    def test_no_crash_on_empty_record(self):
        item = map_isidore_to_zotero({})
        assert item["itemType"] == "document"
        assert item["title"] == ""

    def test_no_crash_on_minimal_record(self):
        record = {"@uri": "test", "isidore": {"title": "Hello"}}
        item = map_isidore_to_zotero(record)
        assert item["title"] == "Hello"
