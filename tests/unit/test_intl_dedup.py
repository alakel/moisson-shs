"""
Tests de l'intégration cross-corpus FR/EN :
- déduplication multi-sources (DOI partagé par 5 sources) + priorité de fusion ;
- reconnaissance des identifiants natifs OpenAlex/S2/CORE dans get_source_uri ;
- matrice de recouvrement ;
- round-trip CSL des types preprint/dataset ;
- résolution des groupes de sources (registry).
"""

import pytest

from utils.deduplication import (
    get_source_uri, merge_items, deduplicate, source_overlap_matrix,
    item_sources, SOURCE_FIELD_PRIORITY,
)


def _five_sources_same_doi():
    doi = "10.1/shared"
    return [
        {"itemType": "journalArticle", "title": "Shared Title", "DOI": doi,
         "abstractNote": "HAL abs", "publicationTitle": "Revue FR",
         "extra": "Sources: HAL\nHAL ID: hal-1"},
        {"itemType": "journalArticle", "title": "Shared Title", "DOI": doi,
         "abstractNote": "CR abs",
         "extra": "Sources: CrossRef\nCited by: 99"},
        {"itemType": "journalArticle", "title": "Shared Title", "DOI": doi,
         "abstractNote": "OA abs",
         "extra": "Sources: OpenAlex\nOpenAlex ID: https://openalex.org/W1\nCited by: 21"},
        {"itemType": "journalArticle", "title": "Shared Title", "DOI": doi,
         "abstractNote": "S2 abs",
         "extra": "Sources: Semantic Scholar\nS2 corpus id: 555\n"
                  "Influential citations: 5\nCited by: 34"},
        {"itemType": "journalArticle", "title": "Shared Title", "DOI": doi,
         "abstractNote": "CORE abs",
         "extra": "Sources: CORE\nCORE ID: 77\nFull text available: yes"},
    ]


class TestGetSourceUriIntl:
    def test_openalex_id(self):
        item = {"extra": "Sources: OpenAlex\nOpenAlex ID: https://openalex.org/W1"}
        assert "https://openalex.org/W1" in get_source_uri(item)

    def test_s2_corpus_id(self):
        item = {"extra": "Sources: Semantic Scholar\nS2 corpus id: 555"}
        assert "555" in get_source_uri(item)

    def test_core_id(self):
        item = {"extra": "Sources: CORE\nCORE ID: 77"}
        assert "77" in get_source_uri(item)

    def test_priority_over_line_order(self):
        # Régression : pour un item portant plusieurs IDs natifs, le type le
        # plus fiable (HAL ID) doit l'emporter quel que soit l'ordre des lignes.
        item_oa_first = {"extra": "Sources: OpenAlex, HAL\n"
                                  "OpenAlex ID: https://openalex.org/W1\n"
                                  "HAL ID: hal-99"}
        item_hal_only = {"extra": "Sources: HAL\nHAL ID: hal-99"}
        # Les deux doivent résoudre vers l'URI HAL → groupables ensemble
        assert get_source_uri(item_oa_first) == get_source_uri(item_hal_only)
        assert "hal-99" in get_source_uri(item_oa_first)

    def test_oa_hal_share_hal_id_merge(self):
        items = [
            {"title": "T", "extra": "Sources: OpenAlex, HAL\n"
                                    "OpenAlex ID: W1\nHAL ID: hal-99"},
            {"title": "T", "extra": "Sources: HAL\nHAL ID: hal-99"},
        ]
        assert len(deduplicate(items)) == 1


class TestItemSources:
    def test_parses_multiple(self):
        item = {"extra": "Sources: HAL, OpenAlex\nfoo: bar"}
        assert item_sources(item) == {"hal", "openalex"}

    def test_normalizes_semantic_scholar(self):
        item = {"extra": "Sources: Semantic Scholar"}
        assert "semantic_scholar" in item_sources(item)


class TestMultiSourceDedup:
    def test_five_sources_merge_to_one(self):
        result = deduplicate(_five_sources_same_doi())
        assert len(result) == 1

    def test_sources_aggregated(self):
        merged = deduplicate(_five_sources_same_doi())[0]
        sources_line = [l for l in merged["extra"].split("\n")
                        if l.startswith("Sources:")][0]
        for src in ("HAL", "CrossRef", "OpenAlex", "Semantic Scholar", "CORE"):
            assert src in sources_line

    def test_abstract_uses_priority_source(self):
        # CrossRef est prioritaire pour abstractNote
        merged = deduplicate(_five_sources_same_doi())[0]
        assert merged["abstractNote"] == "CR abs"

    def test_publication_title_falls_back_to_hal(self):
        # crossref/openalex sans publicationTitle → hal (Revue FR) l'emporte
        merged = deduplicate(_five_sources_same_doi())[0]
        assert merged["publicationTitle"] == "Revue FR"

    def test_citation_counts_preserved(self):
        merged = deduplicate(_five_sources_same_doi())[0]
        assert "Cited by: 21" in merged["extra"]   # OpenAlex
        assert "Cited by: 34" in merged["extra"]   # S2
        assert "Influential citations: 5" in merged["extra"]
        assert "OpenAlex ID: https://openalex.org/W1" in merged["extra"]
        assert "S2 corpus id: 555" in merged["extra"]


class TestFieldPriorityRegressions:
    """Le comportement « premier non vide gagne » reste valable en l'absence
    de source prioritaire."""

    def test_fallback_when_no_priority_source(self):
        a = {"DOI": "", "title": "T", "extra": "Sources: HAL"}
        b = {"DOI": "10.9/z", "title": "T", "extra": "Sources: Isidore"}
        merged = merge_items([a, b])
        # Isidore absent de la priorité DOI → fallback premier non vide
        assert merged["DOI"] == "10.9/z"

    def test_priority_lists_use_known_sources(self):
        for field, prio in SOURCE_FIELD_PRIORITY.items():
            assert isinstance(prio, list) and prio

    def test_crossref_casing_deduplicated_in_sources_line(self):
        # 'CrossRef' (mapper) et 'Crossref' (lookups) ne doivent apparaître
        # qu'une fois dans la ligne Sources fusionnée.
        a = {"DOI": "10.1/x", "title": "T", "extra": "Sources: CrossRef"}
        b = {"DOI": "10.1/x", "title": "T", "extra": "Sources: Crossref"}
        merged = merge_items([a, b])
        sources_line = [l for l in merged["extra"].split("\n")
                        if l.startswith("Sources:")][0]
        # Une seule occurrence (insensible à la casse)
        assert sources_line.lower().count("crossref") == 1


class TestOverlapMatrix:
    def test_counts_pairs(self):
        merged = deduplicate(_five_sources_same_doi())
        overlap = source_overlap_matrix(merged)
        # 5 sources => C(5,2) = 10 paires, chacune comptée 1 fois
        assert len(overlap) == 10
        assert overlap["crossref + hal"] == 1

    def test_no_overlap_single_source(self):
        items = [{"DOI": "10.1/a", "title": "T", "extra": "Sources: HAL"}]
        assert source_overlap_matrix(items) == {}


class TestCslRoundtrip:
    def test_preprint_maps_to_article(self):
        from mappers.csl_exporter import zotero_to_csl
        csl = zotero_to_csl({"itemType": "preprint", "title": "P",
                             "DOI": "10.1/p", "extra": "Sources: CrossRef"})
        assert csl["type"] == "article"
        assert csl["DOI"] == "10.1/p"
        assert csl["note"] == "Sources: CrossRef"

    def test_dataset_maps_to_dataset(self):
        from mappers.csl_exporter import zotero_to_csl, ZOTERO_TO_CSL_TYPE
        assert ZOTERO_TO_CSL_TYPE["dataset"] == "dataset"
        csl = zotero_to_csl({"itemType": "dataset", "title": "D",
                             "DOI": "10.1/d"})
        assert csl["type"] == "dataset"
        assert csl["DOI"] == "10.1/d"

    def test_dataset_validate_keeps_type_and_doi(self):
        from mappers.zotero_schema import validate_and_clean
        item = validate_and_clean({
            "itemType": "dataset", "title": "D", "DOI": "10.1/d",
            "creators": [], "tags": [], "extra": "",
        })
        assert item["itemType"] == "dataset"
        assert item["DOI"] == "10.1/d"

    def test_export_corpus_intl_items(self, crossref_work):
        from mappers.crossref_mapper import map_crossref_item
        from mappers.csl_exporter import export_corpus_to_csl
        item = map_crossref_item(crossref_work)
        csl = export_corpus_to_csl([item])
        assert len(csl) == 1
        assert csl[0]["type"] == "article-journal"
        assert csl[0]["DOI"] == "10.1234/jaip.2023.12.45"


class TestRegistryGroups:
    def test_resolve_all(self):
        from connectors.registry import resolve_sources
        assert len(resolve_sources(["all"])) == 11

    def test_resolve_international(self):
        from connectors.registry import resolve_sources
        assert resolve_sources(["international"]) == [
            "crossref", "openalex", "semantic_scholar", "core",
        ]

    def test_resolve_francophone(self):
        from connectors.registry import resolve_sources, FRANCOPHONE_SOURCES
        assert resolve_sources(["francophone"]) == list(FRANCOPHONE_SOURCES)

    def test_resolve_dedups_and_preserves_order(self):
        from connectors.registry import resolve_sources
        assert resolve_sources(["hal", "international", "hal", "crossref"]) == [
            "hal", "crossref", "openalex", "semantic_scholar", "core",
        ]

    def test_resolve_unknown_raises(self):
        from connectors.registry import resolve_sources
        with pytest.raises(ValueError, match="inconnu"):
            resolve_sources(["bogus"])

    def test_source_groups_defined(self):
        from connectors.registry import SOURCE_GROUPS
        assert set(SOURCE_GROUPS) == {
            "francophone", "international", "all", "scholar",
        }

    def test_metered_sources_excluded_from_free_groups(self):
        """Une source facturée ne doit jamais être atteinte par `all`."""
        from connectors.registry import METERED_SOURCES, SOURCE_GROUPS
        for group in ("francophone", "international", "all"):
            assert not METERED_SOURCES & set(SOURCE_GROUPS[group])

    def test_list_source_tokens_has_names_and_groups(self):
        from connectors.registry import list_source_tokens
        tokens = list_source_tokens()
        assert "openalex" in tokens
        assert "all" in tokens and "international" in tokens

    def test_international_connectors_resolvable(self):
        from connectors.registry import get_connector_class
        from connectors.crossref import CrossrefConnector
        from connectors.openalex import OpenAlexConnector
        from connectors.semantic_scholar import SemanticScholarConnector
        from connectors.core import CoreConnector
        assert get_connector_class("crossref") is CrossrefConnector
        assert get_connector_class("openalex") is OpenAlexConnector
        assert get_connector_class("semantic_scholar") is SemanticScholarConnector
        assert get_connector_class("core") is CoreConnector
