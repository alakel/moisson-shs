"""
Tests de non-régression pour les ajouts du Sprint 03.

Couvre :
- #16 : registry des connecteurs
- #13/#14/#15 : nouveaux connecteurs Persée / Theses.fr / Érudit
- #17 : fetch_by_doi (Crossref + HAL/Isidore)
- #18 : search_by_author (HAL Solr + Isidore)
- #19 : corpus_merge (fusion multi-corpus + rapport)
- #21/#22 : Resources et Prompts MCP (présence dans le serveur)
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# === #16 : Registry ===

class TestRegistry:
    def test_registry_contains_seven_francophone_sources(self):
        from connectors.registry import CONNECTORS, list_sources
        sources = list_sources()
        # Les 7 sources francophones du Sprint 03 restent présentes…
        for fr in ("hal", "isidore", "openedition", "cairn", "persee",
                   "theses_fr", "erudit"):
            assert fr in sources
        # …complétées par les 4 sources internationales (extension internationale)
        for intl in ("crossref", "openalex", "semantic_scholar", "core"):
            assert intl in sources
        assert len(sources) == 11

    def test_get_connector_class_resolves(self):
        from connectors.registry import get_connector_class
        from connectors.hal import HALConnector
        assert get_connector_class("hal") is HALConnector
        assert get_connector_class("HAL") is HALConnector  # insensible casse

    def test_get_connector_class_unknown_raises(self):
        from connectors.registry import get_connector_class
        with pytest.raises(ValueError, match="Source inconnue"):
            get_connector_class("nonexistent")

    def test_split_by_group(self):
        from connectors.registry import split_by_group
        native, proxy = split_by_group(
            ["hal", "openedition", "isidore", "cairn", "persee"]
        )
        assert set(native) == {"hal", "isidore"}
        assert set(proxy) == {"openedition", "cairn", "persee"}

    def test_proxy_sources_marked(self):
        from connectors.registry import is_proxy_source
        assert is_proxy_source("hal") is False
        assert is_proxy_source("isidore") is False
        assert is_proxy_source("openedition") is True
        assert is_proxy_source("cairn") is True
        assert is_proxy_source("persee") is True
        assert is_proxy_source("theses_fr") is True
        assert is_proxy_source("erudit") is True


# === #13/#14/#15 : Nouveaux connecteurs ===

class TestNewConnectorInstantiation:
    def test_persee_instantiates(self):
        from connectors.persee import PerseeConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            c = PerseeConnector(http_client=http, enrich_oai=False)
            assert c.SOURCE_NAME == "Persée"
        finally:
            http.close()

    def test_theses_fr_instantiates(self):
        from connectors.theses_fr import ThesesFrConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            c = ThesesFrConnector(http_client=http)
            assert c.SOURCE_NAME == "Theses.fr"
        finally:
            http.close()

    def test_erudit_instantiates(self):
        from connectors.erudit import EruditConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            c = EruditConnector(http_client=http, enrich_oai=False)
            assert c.SOURCE_NAME == "Érudit"
        finally:
            http.close()

    def test_persee_oai_id_extraction(self):
        from connectors.persee import PerseeConnector
        item_doc = {
            "url": "https://www.persee.fr/doc/ranam_0557-6989_2018_num_51_1_1564",
            "extra": "",
        }
        oai_id = PerseeConnector._extract_oai_id(item_doc)
        assert oai_id == "oai:persee:article/ranam_0557-6989_2018_num_51_1_1564"

        item_issue = {
            "url": "https://www.persee.fr/issue/ranam_0557-6989_2018_num_51_1",
            "extra": "",
        }
        oai_id = PerseeConnector._extract_oai_id(item_issue)
        assert oai_id == "oai:persee:issue/ranam_0557-6989_2018_num_51_1"

    def test_erudit_oai_id_extraction(self):
        from connectors.erudit import EruditConnector
        item = {"url": "http://id.erudit.org/iderudit/1006724ar", "extra": ""}
        oai_id = EruditConnector._extract_oai_id(item)
        assert oai_id == "oai:erudit.org:1006724ar"


# === #17 : fetch_by_doi ===

class TestFetchByDoi:
    def test_normalize_doi_strips_url(self):
        from utils.lookups import _normalize_doi
        assert _normalize_doi("https://doi.org/10.1/x") == "10.1/x"
        assert _normalize_doi("doi:10.1/x") == "10.1/x"
        assert _normalize_doi("10.1/X") == "10.1/x"

    def test_normalize_doi_invalid_returns_empty(self):
        from utils.lookups import _normalize_doi
        assert _normalize_doi("") == ""
        assert _normalize_doi("not-a-doi") == ""

    def test_build_hal_author_query_priority(self):
        from utils.lookups import _build_hal_author_query
        # idhal prime sur orcid qui prime sur name
        assert (
            _build_hal_author_query("Doe", "doe-id", "0000-1234")
            == 'authIdHal_s:"doe-id"'
        )
        assert (
            _build_hal_author_query("Doe", None, "0000-1234")
            == 'authOrcidId_s:"0000-1234"'
        )
        assert (
            _build_hal_author_query("Doe", None, None)
            == 'authFullName_s:"Doe"'
        )
        assert _build_hal_author_query("", None, None) == ""

    def test_fetch_from_crossref_mock(self):
        """Mock Crossref API pour valider le mapping."""
        from utils.lookups import _fetch_from_crossref
        fake_payload = {
            "message": {
                "type": "journal-article",
                "title": ["Article test"],
                "author": [{"family": "Doe", "given": "Jane"}],
                "issued": {"date-parts": [[2020, 5, 15]]},
                "container-title": ["Journal X"],
                "DOI": "10.1/x",
                "URL": "https://doi.org/10.1/x",
                "ISSN": ["1234-5678"],
                "publisher": "Pub",
                "abstract": "Le résumé.",
                "language": "fr",
            }
        }
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: fake_payload,
            )
            item = _fetch_from_crossref("10.1/x")
        assert item["itemType"] == "journalArticle"
        assert item["title"] == "Article test"
        assert item["publicationTitle"] == "Journal X"
        assert item["DOI"] == "10.1/x"
        assert "CrossRef" in item.get("extra", "")


# === #19 : corpus_merge ===

class TestCorpusMerge:
    def test_merge_two_disjoint(self, tmp_path):
        from utils.corpus_ops import merge_corpora
        c1 = tmp_path / "c1.json"
        c2 = tmp_path / "c2.json"
        c1.write_text(json.dumps([
            {"itemType": "journalArticle", "title": "A", "DOI": "10.1/a",
             "extra": "Sources: HAL"}
        ]))
        c2.write_text(json.dumps([
            {"itemType": "journalArticle", "title": "B", "DOI": "10.1/b",
             "extra": "Sources: Isidore"}
        ]))
        merged, report = merge_corpora([c1, c2])
        assert len(merged) == 2
        assert report["_summary"]["total_unique"] == 2
        assert report["_summary"]["duplicates_removed"] == 0

    def test_merge_with_doi_duplicate(self, tmp_path):
        from utils.corpus_ops import merge_corpora
        c1 = tmp_path / "c1.json"
        c2 = tmp_path / "c2.json"
        c1.write_text(json.dumps([
            {"itemType": "journalArticle", "title": "A", "DOI": "10.1/a",
             "extra": "Sources: HAL"}
        ]))
        c2.write_text(json.dumps([
            {"itemType": "journalArticle", "title": "A", "DOI": "10.1/A",
             "extra": "Sources: Isidore"}  # même DOI casse différente
        ]))
        merged, report = merge_corpora([c1, c2])
        assert len(merged) == 1
        assert report["_summary"]["duplicates_removed"] == 1

    def test_merge_report_format(self, tmp_path):
        from utils.corpus_ops import merge_corpora, format_merge_report
        c1 = tmp_path / "c1.json"
        c1.write_text(json.dumps([
            {"itemType": "book", "title": "X", "extra": "Sources: HAL"}
        ]))
        _, report = merge_corpora([c1])
        text = format_merge_report(report)
        assert "Rapport de fusion" in text
        assert "Items chargés totaux : 1" in text

    def test_merge_empty_list(self):
        from utils.corpus_ops import merge_corpora
        merged, report = merge_corpora([])
        assert merged == []
        assert report == {}

    def test_merge_skips_invalid_file(self, tmp_path):
        from utils.corpus_ops import merge_corpora
        c_ok = tmp_path / "ok.json"
        c_bad = tmp_path / "bad.json"
        c_ok.write_text(json.dumps([{"itemType": "book", "title": "X"}]))
        c_bad.write_text("not json{{{")
        merged, report = merge_corpora([c_ok, c_bad])
        # Le corpus valide est conservé, le mauvais est compté à 0
        assert len(merged) == 1
        assert report["bad.json"]["loaded"] == 0


# === #21/#22 : Resources et Prompts MCP ===

class TestMcpResourcesAndPrompts:
    """Vérifie que le serveur MCP expose bien les nouveautés Sprint 03."""

    def test_mcp_server_imports(self):
        """Le module mcp_server doit charger sans erreur."""
        import mcp_server
        assert mcp_server is not None

    def test_mcp_server_exposes_all_tools(self):
        import mcp_server
        import asyncio
        tools = asyncio.run(mcp_server.list_tools())
        names = {t.name for t in tools}
        # Sprint 01-02
        assert "moisson_search" in names
        assert "corpus_analyze" in names
        assert "corpus_to_csl" in names
        # Sprint 03
        assert "fetch_by_doi" in names
        assert "search_by_author" in names
        assert "corpus_merge" in names

    def test_mcp_server_exposes_four_prompts(self):
        import mcp_server
        import asyncio
        prompts = asyncio.run(mcp_server.list_prompts())
        names = {p.name for p in prompts}
        assert "bibliographie-thematique" in names
        assert "profil-chercheur" in names
        assert "veille-mensuelle" in names
        assert "etat-de-l-art" in names

    def test_mcp_get_prompt_profil_chercheur(self):
        import mcp_server
        import asyncio
        result = asyncio.run(mcp_server.get_prompt(
            "profil-chercheur",
            {"nom": "Patrick Charaudeau", "idhal": "patrick-charaudeau"},
        ))
        assert result.messages
        text = result.messages[0].content.text
        assert "Patrick Charaudeau" in text
        assert "search_by_author" in text
        assert "patrick-charaudeau" in text

    def test_mcp_get_prompt_bibliographie_thematique(self):
        import mcp_server
        import asyncio
        result = asyncio.run(mcp_server.get_prompt(
            "bibliographie-thematique",
            {"theme": "discours numérique"},
        ))
        text = result.messages[0].content.text
        assert "discours numérique" in text
        assert "moisson_search" in text


# === Integration : moisson sur les 3 nouvelles sources ===

@pytest.mark.integration
class TestNewConnectorsIntegration:
    def test_persee_returns_items(self):
        from connectors.persee import PerseeConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            c = PerseeConnector(http_client=http)
            items = c.search("histoire", max_results=3)
            assert len(items) >= 1
            # Tous les items doivent avoir une URL persee.fr quelque part
            for item in items:
                blob = item.get("url", "") + (item.get("extra") or "")
                assert "persee.fr" in blob
        finally:
            http.close()

    def test_theses_fr_returns_items(self):
        from connectors.theses_fr import ThesesFrConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            c = ThesesFrConnector(http_client=http)
            items = c.search("discours numérique", max_results=3)
            assert len(items) >= 1
            for item in items:
                blob = item.get("url", "") + (item.get("extra") or "")
                assert "theses.fr" in blob
        finally:
            http.close()

    def test_erudit_returns_items(self):
        from connectors.erudit import EruditConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            c = EruditConnector(http_client=http)
            items = c.search("discours", max_results=3)
            assert len(items) >= 1
            for item in items:
                blob = item.get("url", "") + (item.get("extra") or "")
                assert "erudit.org" in blob
        finally:
            http.close()
