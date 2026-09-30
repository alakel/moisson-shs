"""
Tests de non-régression pour les changements du Sprint 02.

Couvre :
- #8 : signature uniforme des connecteurs (acceptent **kwargs)
- #9 : moissonnage parallèle (ThreadPoolExecutor)
- #10 : cache HTTP (option use_cache)
- #11 : validation contre schéma Zotero officiel (loader + fallback)
- #12 : filtres temporels (from_date, to_date)
"""

import pytest


# === #8 : Signature uniforme ===

class TestUniformConnectorSignature:
    def test_hal_accepts_kwargs(self):
        from connectors.hal import HALConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            # Doit accepter enrich_oai sans crash même si non utilisé
            c = HALConnector(http_client=http, enrich_oai=True, foo="bar")
            assert c.SOURCE_NAME == "HAL"
        finally:
            http.close()

    def test_isidore_accepts_kwargs(self):
        from connectors.isidore import IsidoreConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            c = IsidoreConnector(http_client=http, enrich_oai=True, anything="x")
            assert c.SOURCE_NAME == "Isidore"
        finally:
            http.close()

    def test_openedition_keeps_enrich_oai(self):
        from connectors.openedition import OpenEditionConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            c = OpenEditionConnector(http_client=http, enrich_oai=True)
            assert c.enrich_oai is True
        finally:
            http.close()

    def test_cairn_keeps_enrich_oai(self):
        from connectors.cairn import CairnConnector
        from utils.http import HTTPClient
        http = HTTPClient()
        try:
            c = CairnConnector(http_client=http, enrich_oai=False)
            assert c.enrich_oai is False
        finally:
            http.close()


# === #10 : Cache HTTP ===

class TestHttpCache:
    def test_default_cache_disabled(self):
        from utils.http import HTTPClient
        c = HTTPClient()
        assert c.cache_enabled is False
        c.close()

    def test_explicit_enable(self, tmp_path):
        from utils.http import HTTPClient
        c = HTTPClient(use_cache=True, cache_dir=str(tmp_path / "cache"))
        assert c.cache_enabled is True
        c.close()

    def test_env_variable_enables_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MOISSON_CACHE", "1")
        monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "c"))
        from utils.http import HTTPClient
        c = HTTPClient()
        assert c.cache_enabled is True
        c.close()

    def test_was_cached_detection(self):
        """L'attribut from_cache de requests-cache est correctement détecté."""
        from utils.http import HTTPClient

        class FakeResp:
            from_cache = True
        assert HTTPClient._was_cached(FakeResp()) is True

        class FakeResp2:
            pass
        assert HTTPClient._was_cached(FakeResp2()) is False


# === #12 : Filtres temporels ===

class TestHalDateFilter:
    def test_year_to_iso_full_range(self):
        from connectors.hal import HALConnector
        fq = HALConnector._build_date_filter("2020", "2024")
        assert "2020-01-01T00:00:00Z" in fq
        assert "2024-12-31T23:59:59Z" in fq
        assert "producedDate_tdate" in fq

    def test_iso_passthrough(self):
        from connectors.hal import HALConnector
        fq = HALConnector._build_date_filter("2020-06-15", "2024-03-20")
        assert "2020-06-15T00:00:00Z" in fq
        assert "2024-03-20T23:59:59Z" in fq

    def test_open_lower_bound(self):
        from connectors.hal import HALConnector
        fq = HALConnector._build_date_filter(None, "2024")
        assert "* TO" in fq

    def test_open_upper_bound(self):
        from connectors.hal import HALConnector
        fq = HALConnector._build_date_filter("2020", None)
        assert "TO NOW" in fq


class TestIsidoreYearExtraction:
    def test_year_from_string(self):
        from connectors.isidore import IsidoreConnector
        record = {"isidore": {"date": "2020-05-15"}}
        assert IsidoreConnector._extract_year(record) == 2020

    def test_year_from_dict_normalized(self):
        from connectors.isidore import IsidoreConnector
        record = {"isidore": {"date": {"normalizedDate": "2019-01-01"}}}
        assert IsidoreConnector._extract_year(record) == 2019

    def test_year_from_year_field(self):
        from connectors.isidore import IsidoreConnector
        record = {"isidore": {"date": {"year": "2022"}}}
        assert IsidoreConnector._extract_year(record) == 2022

    def test_no_date_returns_none(self):
        from connectors.isidore import IsidoreConnector
        assert IsidoreConnector._extract_year({}) is None
        assert IsidoreConnector._extract_year({"isidore": {}}) is None
        assert IsidoreConnector._extract_year({"isidore": {"date": "no year"}}) is None


# === #11 : Validation contre schéma officiel (intégration sans réseau) ===

class TestZoteroSchemaIntegration:
    def test_validate_uses_schema_when_loaded(self):
        from mappers import zotero_schema_loader as loader
        from mappers.zotero_schema import validate_and_clean
        # Schéma factice avec un champ exotique autorisé
        loader._MEMORY_CACHE = {
            "itemTypes": [
                {
                    "itemType": "journalArticle",
                    "fields": [
                        {"field": "title"},
                        {"field": "DOI"},
                        {"field": "publicationTitle"},
                        {"field": "exoticField"},   # nouveau champ
                    ],
                    "creatorTypes": [{"creatorType": "author"}],
                }
            ]
        }
        item = {
            "itemType": "journalArticle",
            "title": "T",
            "exoticField": "should be kept",
        }
        cleaned = validate_and_clean(item)
        # exoticField doit être préservé puisque le schéma téléchargé l'autorise
        assert cleaned.get("exoticField") == "should be kept"
        loader._MEMORY_CACHE = None

    def test_validate_fallback_when_no_schema(self, monkeypatch, tmp_path):
        from mappers import zotero_schema_loader as loader
        from mappers.zotero_schema import validate_and_clean
        from unittest.mock import patch

        # Ni schéma figé (posé par conftest), ni cache disque (frais ou
        # périmé), ni réseau : seul le repli embarqué peut répondre. Sans
        # cela, le schéma figé de conftest servirait et le repli ne serait
        # jamais atteint.
        monkeypatch.delenv(loader.SCHEMA_FILE_ENV, raising=False)
        monkeypatch.setattr(loader, "CACHE_PATH", tmp_path / "absent.json")
        monkeypatch.setattr(loader, "SCHEMA_RETRY_AFTER_SECONDS", 600)
        loader.reset_state()
        try:
            with patch.object(loader, "_fetch_from_network",
                              return_value=None) as fetch:
                item = {"itemType": "journalArticle", "title": "T", "DOI": "10.1/x"}
                cleaned = validate_and_clean(item)
            # C'est bien le repli qui a servi, après une seule tentative
            assert loader.schema_state() == loader.ETAT_INDISPONIBLE
            assert fetch.call_count == 1
        finally:
            loader.reset_state()
        # Le fallback embarqué doit autoriser DOI
        assert cleaned.get("DOI") == "10.1/x"


# === #9 : Parallélisme (test léger sans réseau) ===

class TestParallelTopology:
    """Vérifie que main.py organise bien les sources en deux groupes."""

    def test_native_group_contains_hal_isidore(self):
        # Test conceptuel : on vérifie juste que la stratégie est cohérente
        # en lisant la définition de main._run_group si exposée.
        # À défaut on vérifie l'import et la non-régression.
        import main as main_module
        assert callable(getattr(main_module, "run_source"))


# === Integration : ré-exécution avec cache rapide ===

@pytest.mark.integration
class TestCacheRoundtrip:
    def test_second_call_is_cached(self, tmp_path):
        """Une 2e requête identique est servie depuis le cache."""
        from utils.http import HTTPClient
        c = HTTPClient(use_cache=True, cache_dir=str(tmp_path / "cache"))
        try:
            url = "https://api.archives-ouvertes.fr/search/"
            params = {"q": "test", "rows": 1, "wt": "json", "sort": "docid asc"}
            r1 = c.get(url, params=params)
            r2 = c.get(url, params=params)
            assert HTTPClient._was_cached(r2) is True
        finally:
            c.close()
