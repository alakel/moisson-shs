"""Tests unitaires pour mappers.zotero_schema."""

import pytest

from mappers.zotero_schema import (
    make_empty_item, validate_and_clean, make_tags, append_to_extra,
    ZOTERO_ITEM_FIELDS, COMMON_FIELDS,
)


class TestMakeEmptyItem:
    def test_default_type_is_document(self):
        item = make_empty_item()
        assert item["itemType"] == "document"

    def test_explicit_type(self):
        item = make_empty_item("book")
        assert item["itemType"] == "book"

    def test_has_required_keys(self):
        item = make_empty_item()
        for key in ("title", "creators", "abstractNote", "date", "language",
                    "url", "extra", "tags", "collections", "relations"):
            assert key in item

    def test_creators_is_list(self):
        assert make_empty_item()["creators"] == []

    def test_tags_is_list(self):
        assert make_empty_item()["tags"] == []


class TestValidateAndClean:
    def test_keeps_allowed_fields(self):
        item = {
            "itemType": "journalArticle",
            "title": "T",
            "DOI": "10.1/x",
            "publicationTitle": "J",
        }
        cleaned = validate_and_clean(item)
        assert cleaned["DOI"] == "10.1/x"
        assert cleaned["publicationTitle"] == "J"

    def test_moves_orphan_fields_to_extra(self):
        item = {
            "itemType": "journalArticle",
            "title": "T",
            "thesisType": "PhD",  # thesisType n'existe pas pour journalArticle
        }
        cleaned = validate_and_clean(item)
        # Doit migrer dans extra
        assert "thesisType" not in cleaned or not cleaned.get("thesisType")
        assert "thesisType: PhD" in cleaned.get("extra", "")

    def test_preserves_existing_extra(self):
        item = {
            "itemType": "journalArticle",
            "title": "T",
            "extra": "Sources: HAL\nHAL ID: hal-1234",
        }
        cleaned = validate_and_clean(item)
        assert "Sources: HAL" in cleaned["extra"]
        assert "HAL ID: hal-1234" in cleaned["extra"]

    def test_deduplicates_extra_lines(self):
        item = {
            "itemType": "document",
            "title": "T",
            "extra": "Sources: A\nSources: A\nFoo: bar",
        }
        cleaned = validate_and_clean(item)
        # La ligne Sources: A ne doit apparaître qu'une fois
        assert cleaned["extra"].count("Sources: A") == 1

    def test_unknown_itemtype_falls_back_to_document(self):
        item = {"itemType": "unknownType", "title": "T", "url": "http://x"}
        cleaned = validate_and_clean(item)
        assert "url" in cleaned  # document accepte url

    def test_common_fields_always_present(self):
        item = {"itemType": "book"}
        cleaned = validate_and_clean(item)
        for key in ("itemType", "title", "creators", "tags", "collections",
                    "relations"):
            assert key in cleaned


class TestMakeTags:
    def test_simple_list(self):
        tags = make_tags(["a", "b", "c"])
        assert tags == [{"tag": "a"}, {"tag": "b"}, {"tag": "c"}]

    def test_filters_empty(self):
        tags = make_tags(["a", "", "  ", "b"])
        assert len(tags) == 2

    def test_deduplicates_case_insensitive(self):
        tags = make_tags(["Discours", "discours", "DISCOURS"])
        # Doublons insensibles à la casse → 1 seul tag (premier vu préservé)
        assert len(tags) == 1
        assert tags[0]["tag"] == "Discours"

    def test_empty_input(self):
        assert make_tags([]) == []


class TestAppendToExtra:
    def test_adds_to_empty(self):
        item = {"extra": ""}
        append_to_extra(item, "Foo", "bar")
        assert item["extra"] == "Foo: bar"

    def test_appends_with_newline(self):
        item = {"extra": "A: 1"}
        append_to_extra(item, "B", "2")
        assert item["extra"] == "A: 1\nB: 2"

    def test_skips_empty_value(self):
        item = {"extra": "X: 1"}
        append_to_extra(item, "Y", "")
        append_to_extra(item, "Z", None)
        assert item["extra"] == "X: 1"

    def test_skips_when_no_extra_key(self):
        # L'item peut ne pas avoir 'extra' au départ
        item = {}
        append_to_extra(item, "K", "v")
        assert item.get("extra") == "K: v"


class TestSchemaCompleteness:
    """Vérifications de cohérence du schéma embarqué."""

    def test_all_types_have_at_least_title(self):
        for type_name, fields in ZOTERO_ITEM_FIELDS.items():
            assert "title" in fields, f"{type_name} sans title"
            assert "extra" in fields, f"{type_name} sans extra"

    def test_common_fields_well_formed(self):
        assert "itemType" in COMMON_FIELDS
        assert "creators" in COMMON_FIELDS
        assert "tags" in COMMON_FIELDS

    def test_critical_shs_types_present(self):
        for t in ("journalArticle", "bookSection", "book", "conferencePaper",
                  "thesis", "report", "preprint", "document"):
            assert t in ZOTERO_ITEM_FIELDS, f"Type SHS critique manquant : {t}"
