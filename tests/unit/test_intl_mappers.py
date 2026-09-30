"""Tests des quatre mappers internationaux sur fixtures réelles réduites."""

import pytest

from mappers.crossref_mapper import map_crossref_item, date_from_parts
from mappers.openalex_mapper import map_openalex_item, reconstruct_abstract
from mappers.semantic_scholar_mapper import map_s2_item
from mappers.core_mapper import map_core_item


def _extra_lines(item):
    return item.get("extra", "").split("\n")


# === CrossRef ===

class TestCrossrefMapper:
    def test_basic_mapping(self, crossref_work):
        item = map_crossref_item(crossref_work)
        assert item["itemType"] == "journalArticle"
        assert item["title"] == "Regulating Artificial Intelligence in Europe"
        assert item["publicationTitle"] == "Journal of AI Policy"
        assert item["volume"] == "12"
        assert item["issue"] == "3"
        assert item["pages"] == "45-67"
        assert item["DOI"] == "10.1234/jaip.2023.12.45"
        assert item["ISSN"] == "2049-1234"
        assert item["date"] == "2023-06-15"
        assert item["language"] == "en"

    def test_jats_stripped(self, crossref_work):
        item = map_crossref_item(crossref_work)
        assert "<jats" not in item["abstractNote"]
        assert "regulatory" in item["abstractNote"]

    def test_creators(self, crossref_work):
        item = map_crossref_item(crossref_work)
        assert item["creators"][0] == {
            "creatorType": "author", "firstName": "Jane", "lastName": "Doe",
        }
        # auteur institutionnel via 'name'
        assert any(c.get("name") == "AI Policy Lab" for c in item["creators"])

    def test_source_and_citation_in_extra(self, crossref_work):
        item = map_crossref_item(crossref_work)
        assert "Sources: CrossRef" in item["extra"]
        assert "Cited by: 17" in item["extra"]

    def test_subjects_in_extra_not_in_tags(self, crossref_work):
        """PAT-04 : les sujets CrossRef (catégories de la revue) sont tracés
        dans extra, jamais en mots-clés."""
        item = map_crossref_item(crossref_work)
        assert not item.get("tags")
        line = next(l for l in _extra_lines(item)
                    if l.startswith("CrossRef subjects: "))
        assert "Artificial Intelligence" in line and "Law" in line

    def test_date_from_parts_variants(self):
        assert date_from_parts({"date-parts": [[2020, 5, 15]]}) == "2020-05-15"
        assert date_from_parts({"date-parts": [[2020, 5]]}) == "2020-05"
        assert date_from_parts({"date-parts": [[2020]]}) == "2020"
        assert date_from_parts({"date-parts": [[None]]}) == ""
        assert date_from_parts({}) == ""

    def test_minimal_no_crash(self):
        item = map_crossref_item({"type": "journal-article", "title": ["T"]})
        assert item["title"] == "T"
        assert item["itemType"] == "journalArticle"


# === OpenAlex ===

class TestOpenAlexMapper:
    def test_abstract_reconstruction(self):
        idx = {"Hello": [0], "brave": [2], "new": [3], "world": [1]}
        # positions : 0 Hello, 1 world, 2 brave, 3 new
        assert reconstruct_abstract(idx) == "Hello world brave new"

    def test_abstract_empty(self):
        assert reconstruct_abstract(None) == ""
        assert reconstruct_abstract({}) == ""

    def test_basic_mapping(self, openalex_work):
        item = map_openalex_item(openalex_work)
        assert item["itemType"] == "journalArticle"
        assert item["title"] == "Regulating Artificial Intelligence in Europe"
        assert item["DOI"] == "10.1234/jaip.2023.12.45"   # préfixe doi.org retiré
        assert item["publicationTitle"] == "Journal of AI Policy"
        assert item["date"] == "2023-06-15"
        assert item["pages"] == "45-67"
        assert item["abstractNote"] == "This paper examines regulatory frameworks"

    def test_concepts_threshold(self, openalex_work):
        """PAT-04 : les concepts retenus (score >= seuil) vont dans la ligne
        « OpenAlex concepts » d'extra, jamais dans les mots-clés."""
        item = map_openalex_item(openalex_work)
        assert not item.get("tags")
        line = next(l for l in _extra_lines(item)
                    if l.startswith("OpenAlex concepts: "))
        assert "Artificial intelligence" in line
        assert "Political science" in line  # 0.55 >= 0.3
        assert "Low confidence noise" not in line  # 0.12 filtré

    def test_extra_metadata(self, openalex_work):
        item = map_openalex_item(openalex_work)
        assert "Sources: OpenAlex" in item["extra"]
        assert "OpenAlex ID: https://openalex.org/W4389012345" in item["extra"]
        assert "Cited by: 21" in item["extra"]
        assert "Open Access: yes" in item["extra"]

    def test_authors_split(self, openalex_work):
        item = map_openalex_item(openalex_work)
        assert item["creators"][0] == {
            "creatorType": "author", "firstName": "Jane", "lastName": "Doe",
        }

    def test_zero_citation_count_preserved(self, openalex_work):
        # Régression : un compte de citations nul (cas fréquent des articles
        # récents) ne doit PAS disparaître (le helper écarte les valeurs falsy).
        work = dict(openalex_work)
        work["cited_by_count"] = 0
        item = map_openalex_item(work)
        assert "Cited by: 0" in item["extra"]

    def test_missing_citation_count_omitted(self, openalex_work):
        work = dict(openalex_work)
        work.pop("cited_by_count", None)
        item = map_openalex_item(work)
        assert "Cited by:" not in item["extra"]


# === Semantic Scholar ===

class TestS2Mapper:
    def test_basic_mapping(self, s2_paper):
        item = map_s2_item(s2_paper)
        assert item["itemType"] == "journalArticle"
        assert item["DOI"] == "10.5555/llm.2022.99"
        assert item["publicationTitle"] == "Computational Linguistics Review"
        assert item["volume"] == "8"
        assert item["pages"] == "100-130"
        assert item["date"] == "2022-09-01"
        assert item["abstractNote"] == "We study LLMs in policy contexts."

    def test_extra_metadata(self, s2_paper):
        item = map_s2_item(s2_paper)
        assert "Sources: Semantic Scholar" in item["extra"]
        assert "S2 corpus id: 248123456" in item["extra"]
        assert "Cited by: 34" in item["extra"]
        assert "Influential citations: 5" in item["extra"]
        assert "arXiv: 2209.01234" in item["extra"]

    def test_fields_in_extra_not_in_tags(self, s2_paper):
        """PAT-04 : les champs d'étude S2 (classification automatique) sont
        tracés dans extra, jamais en mots-clés."""
        item = map_s2_item(s2_paper)
        assert not item.get("tags")
        line = next(l for l in _extra_lines(item) if l.startswith("S2 fields: "))
        assert "Computer Science" in line and "Linguistics" in line

    def test_url_prefers_oa_pdf(self, s2_paper):
        item = map_s2_item(s2_paper)
        assert item["url"] == "https://arxiv.org/pdf/2209.01234"

    def test_zero_counts_preserved(self, s2_paper):
        paper = dict(s2_paper)
        paper["citationCount"] = 0
        paper["influentialCitationCount"] = 0
        item = map_s2_item(paper)
        assert "Cited by: 0" in item["extra"]
        assert "Influential citations: 0" in item["extra"]


# === CORE ===

class TestCoreMapper:
    def test_basic_mapping(self, core_work):
        item = map_core_item(core_work)
        assert item["itemType"] == "journalArticle"
        assert item["DOI"] == "10.7777/core.2021.1"
        assert item["publicationTitle"] == "International Journal of Open Science"
        assert item["date"] == "2021-03-10"
        assert item["language"] == "en"

    def test_fulltext_and_pdf_in_extra(self, core_work):
        item = map_core_item(core_work)
        assert "Sources: CORE" in item["extra"]
        assert "CORE ID: 12345678" in item["extra"]
        assert "Full text available: yes" in item["extra"]
        assert "PDF: https://core.ac.uk/download/12345678.pdf" in item["extra"]

    def test_url_prefers_doi(self, core_work):
        item = map_core_item(core_work)
        assert item["url"] == "https://doi.org/10.7777/core.2021.1"

    def test_language_as_string(self):
        item = map_core_item({"title": "T", "documentType": "research",
                              "language": "fr"})
        assert item["language"] == "fr"

    def test_metadata_only_when_no_fulltext(self):
        item = map_core_item({"title": "T", "documentType": "research"})
        assert "Full text available: metadata only" in item["extra"]
