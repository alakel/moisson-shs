"""Tests unitaires pour mappers.oai_mapper."""

import pytest

from mappers.oai_mapper import map_oai_dc_to_zotero, map_qdc_to_zotero


def _make_dc(**fields):
    """Construit un dict metadata OAI Dublin Core (format sickle)."""
    # sickle renvoie chaque champ comme une liste de strings
    out = {}
    for k, v in fields.items():
        if isinstance(v, list):
            out[k] = v
        else:
            out[k] = [v] if v else []
    return out


class TestMapOaiDcBasic:
    def test_title_extracted(self):
        md = _make_dc(title="Un titre", type="article")
        item = map_oai_dc_to_zotero(md, "oai:foo:1", "TestSource")
        assert item["title"] == "Un titre"

    def test_itemtype_from_dc_type(self):
        md = _make_dc(title="T", type="info:eu-repo/semantics/article")
        item = map_oai_dc_to_zotero(md, "", "")
        assert item["itemType"] == "journalArticle"

    def test_creators_from_dc_creator(self):
        md = _make_dc(title="T", creator=["Doe, Jane", "Smith, John"])
        item = map_oai_dc_to_zotero(md, "", "")
        assert len(item["creators"]) == 2

    def test_date_extracted(self):
        md = _make_dc(title="T", date="2020-05-15")
        item = map_oai_dc_to_zotero(md, "", "")
        assert item["date"] == "2020-05-15"

    def test_picks_year_from_multiple_dates(self):
        md = _make_dc(title="T", date=["sans année", "2020"])
        item = map_oai_dc_to_zotero(md, "", "")
        assert item["date"] == "2020"

    def test_language_extracted(self):
        md = _make_dc(title="T", language="fr")
        item = map_oai_dc_to_zotero(md, "", "")
        assert item["language"] == "fr"

    def test_abstract_from_description(self):
        md = _make_dc(title="T", description="Voici le résumé.")
        item = map_oai_dc_to_zotero(md, "", "")
        assert item["abstractNote"] == "Voici le résumé."

    def test_tags_from_subjects(self):
        md = _make_dc(title="T", subject=["mot1", "mot2"])
        item = map_oai_dc_to_zotero(md, "", "")
        assert len(item["tags"]) == 2


class TestMapOaiDcIdentifiers:
    def test_url_extracted(self):
        md = _make_dc(title="T", identifier=["https://example.org/x"])
        item = map_oai_dc_to_zotero(md, "", "")
        assert item["url"] == "https://example.org/x"

    def test_doi_extracted(self):
        md = _make_dc(title="T", identifier=["https://doi.org/10.1/x"])
        item = map_oai_dc_to_zotero(md, "", "")
        assert item["DOI"] == "10.1/x"

    def test_url_and_doi_separated(self):
        md = _make_dc(
            title="T",
            identifier=["https://example.org/x", "https://doi.org/10.1/y"],
        )
        item = map_oai_dc_to_zotero(md, "", "")
        assert item["url"] == "https://example.org/x"
        assert item["DOI"] == "10.1/y"


class TestMapOaiDcExtra:
    def test_source_in_extra(self):
        md = _make_dc(title="T")
        item = map_oai_dc_to_zotero(md, "oai:revues.org:foo/1", "OpenEdition")
        assert "Sources: OpenEdition" in item["extra"]
        assert "OAI Identifier: oai:revues.org:foo/1" in item["extra"]

    def test_no_oai_id_omitted(self):
        md = _make_dc(title="T")
        item = map_oai_dc_to_zotero(md, "", "TestSource")
        assert "OAI Identifier" not in item["extra"]


class TestMapQdc:
    def test_qdc_abstract_overrides_description(self):
        md = _make_dc(
            title="T",
            description="ancien résumé",
            abstract="nouveau résumé (dcterms)",
        )
        item = map_qdc_to_zotero(md, "", "OpenEdition")
        assert item["abstractNote"] == "nouveau résumé (dcterms)"

    def test_qdc_ispartof_for_journal_article(self):
        md = _make_dc(
            title="T",
            type="info:eu-repo/semantics/article",
            isPartOf="Communication & langages",
        )
        item = map_qdc_to_zotero(md, "", "OpenEdition")
        assert item["publicationTitle"] == "Communication & langages"

    def test_qdc_open_access_extracted(self):
        md = _make_dc(
            title="T",
            accessRights="info:eu-repo/semantics/openAccess",
        )
        item = map_qdc_to_zotero(md, "", "OpenEdition")
        assert "Open Access" in item["extra"]


class TestMapOaiDcRobustness:
    def test_empty_metadata(self):
        item = map_oai_dc_to_zotero({}, "", "TestSource")
        assert item["itemType"] == "document"
        assert item["title"] == ""

    def test_unknown_type_falls_back_to_document(self):
        md = _make_dc(title="T", type="weirdUnknownType")
        item = map_oai_dc_to_zotero(md, "", "")
        assert item["itemType"] == "document"
