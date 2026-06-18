"""Tests unitaires pour mappers.hal_mapper sur fixture réelle."""

import pytest

from mappers.hal_mapper import map_hal_to_zotero


def test_returns_zotero_item(hal_first_doc):
    item = map_hal_to_zotero(hal_first_doc)
    assert "itemType" in item
    assert "title" in item


def test_title_extracted(hal_first_doc):
    item = map_hal_to_zotero(hal_first_doc)
    assert item["title"], "Title vide"
    assert isinstance(item["title"], str)


def test_creators_extracted(hal_first_doc):
    item = map_hal_to_zotero(hal_first_doc)
    # La fixture est censée avoir des auteurs
    if hal_first_doc.get("authFullName_s"):
        assert len(item["creators"]) >= 1
        first = item["creators"][0]
        # Soit firstName+lastName, soit name
        assert (first.get("firstName") or first.get("name")) or first.get("lastName")
        assert first["creatorType"] == "author"


def test_sources_contains_hal(hal_first_doc):
    item = map_hal_to_zotero(hal_first_doc)
    assert "Sources: HAL" in item.get("extra", "")


def test_hal_id_in_extra_when_present(hal_first_doc):
    item = map_hal_to_zotero(hal_first_doc)
    if hal_first_doc.get("halId_s"):
        assert "HAL ID:" in item["extra"]


def test_all_docs_have_valid_itemtype(hal_solr_response):
    """Tous les docs de la fixture doivent produire un itemType Zotero valide."""
    from mappers.zotero_schema import ZOTERO_ITEM_FIELDS
    valid = set(ZOTERO_ITEM_FIELDS.keys())
    docs = hal_solr_response["response"]["docs"]
    for doc in docs:
        item = map_hal_to_zotero(doc)
        assert item["itemType"] in valid or item["itemType"] in (
            "audioRecording", "artwork", "map", "patent", "computerProgram",
            "videoRecording"
        ), f"Type inconnu : {item['itemType']}"


def test_journal_article_has_publication_title(hal_solr_response):
    """Si un doc est ART avec journalTitle_s, publicationTitle doit être set."""
    for doc in hal_solr_response["response"]["docs"]:
        if doc.get("docType_s") == "ART" and doc.get("journalTitle_s"):
            item = map_hal_to_zotero(doc)
            if item["itemType"] == "journalArticle":
                assert item.get("publicationTitle"), (
                    f"journalArticle sans publicationTitle : {doc.get('docid')}"
                )
                break


def test_doi_extracted_when_present(hal_solr_response):
    for doc in hal_solr_response["response"]["docs"]:
        if doc.get("doiId_s"):
            item = map_hal_to_zotero(doc)
            assert item.get("DOI"), f"DOI absent malgré doiId_s={doc['doiId_s']}"


def test_no_crash_on_minimal_doc():
    """Doc minimal ne doit pas planter le mapper."""
    minimal = {"docid": "1", "title_s": "Titre minimal", "docType_s": "ART"}
    item = map_hal_to_zotero(minimal)
    assert item["title"] == "Titre minimal"
    assert item["itemType"] == "journalArticle"


def test_empty_doc_returns_document():
    item = map_hal_to_zotero({})
    assert item["itemType"] == "document"
