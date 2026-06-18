"""Tests unitaires pour utils.cleaner."""

import pytest

from utils.cleaner import (
    strip_html, clean_string, clean_list, normalize_for_fingerprint,
    extract_year, split_authors, parse_hal_author,
)


class TestStripHtml:
    def test_removes_simple_tags(self):
        assert strip_html("<p>Texte</p>") == "Texte"

    def test_removes_nested_tags(self):
        assert strip_html("<div><b>Gras</b> normal</div>") == "Gras normal"

    def test_decodes_entities(self):
        assert strip_html("&eacute;t&eacute;") == "été"

    def test_decodes_amp(self):
        assert strip_html("A &amp; B") == "A & B"

    def test_collapses_whitespace(self):
        assert strip_html("a   b\n\nc") == "a b c"

    def test_empty_string(self):
        assert strip_html("") == ""

    def test_none(self):
        assert strip_html(None) == ""


class TestCleanString:
    def test_preserves_unicode(self):
        assert clean_string("Études de communication") == "Études de communication"

    def test_strips_html(self):
        assert clean_string("<i>italic</i>") == "italic"

    def test_handles_none(self):
        assert clean_string(None) == ""

    def test_converts_non_string(self):
        assert clean_string(42) == "42"


class TestCleanList:
    def test_empty_input(self):
        assert clean_list([]) == []
        assert clean_list(None) == []

    def test_filters_empty(self):
        assert clean_list(["a", "", "b", None]) == ["a", "b"]

    def test_strips_html_per_item(self):
        assert clean_list(["<b>x</b>", "y"]) == ["x", "y"]

    def test_string_input_becomes_list(self):
        assert clean_list("solo") == ["solo"]


class TestNormalizeForFingerprint:
    def test_removes_accents(self):
        assert normalize_for_fingerprint("été") == "ete"

    def test_lowercases(self):
        assert normalize_for_fingerprint("FOO BAR") == "foobar"

    def test_removes_punctuation(self):
        assert normalize_for_fingerprint("Hello, World!") == "helloworld"

    def test_removes_spaces(self):
        assert normalize_for_fingerprint("a b c") == "abc"

    def test_consistent_across_typographic_variants(self):
        # Différentes formes typographiques d'un même titre
        v1 = normalize_for_fingerprint("L'Élément clé")
        v2 = normalize_for_fingerprint("L Element cle")
        v3 = normalize_for_fingerprint("L’élément clé !")
        assert v1 == v2 == v3

    def test_empty_string(self):
        assert normalize_for_fingerprint("") == ""

    def test_strips_html_first(self):
        assert normalize_for_fingerprint("<b>texte</b>") == "texte"


class TestExtractYear:
    def test_year_only(self):
        assert extract_year("2024") == "2024"

    def test_iso_date(self):
        assert extract_year("2024-05-15") == "2024"

    def test_year_in_sentence(self):
        assert extract_year("Publié en 2020 chez Foo") == "2020"

    def test_18th_century(self):
        assert extract_year("1789") == "1789"

    def test_no_year(self):
        assert extract_year("aucune date") is None

    def test_empty(self):
        assert extract_year("") is None
        assert extract_year(None) is None

    def test_invalid_year_pre_1500(self):
        # 1499 ne doit pas matcher (regex commence à 1500)
        assert extract_year("Vers 1234") is None


class TestSplitAuthors:
    def test_single_with_comma(self):
        result = split_authors("Doe, Jane")
        assert len(result) == 1
        assert result[0]["lastName"] == "Doe"
        assert result[0]["firstName"] == "Jane"
        assert result[0]["creatorType"] == "author"

    def test_multiple_semicolon_separated(self):
        result = split_authors("Doe, Jane; Smith, John")
        assert len(result) == 2
        assert result[0]["lastName"] == "Doe"
        assert result[1]["lastName"] == "Smith"

    def test_and_separator(self):
        result = split_authors("Jane Doe and John Smith")
        assert len(result) == 2

    def test_firstname_lastname_format(self):
        result = split_authors("Jane Doe")
        assert result[0]["firstName"] == "Jane"
        assert result[0]["lastName"] == "Doe"

    def test_single_name_uses_name_field(self):
        result = split_authors("InstitutNational")
        assert result[0].get("name") == "InstitutNational"

    def test_compound_firstname(self):
        result = split_authors("Marie-Anne Paveau")
        assert result[0]["firstName"] == "Marie-Anne"
        assert result[0]["lastName"] == "Paveau"

    def test_empty(self):
        assert split_authors("") == []
        assert split_authors(None) == []


class TestParseHalAuthor:
    def test_two_tokens(self):
        result = parse_hal_author("Jane Doe")
        assert result["firstName"] == "Jane"
        assert result["lastName"] == "Doe"

    def test_three_tokens(self):
        result = parse_hal_author("Marie-Anne Le Paveau")
        # Le dernier token devient lastName
        assert result["lastName"] == "Paveau"
        assert "Marie-Anne" in result["firstName"]

    def test_single_token_uses_name(self):
        result = parse_hal_author("Bourdieu")
        assert result.get("name") == "Bourdieu"

    def test_empty(self):
        assert parse_hal_author("") == {}
