"""Tests pour le chargement du schéma Zotero officiel."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mappers import zotero_schema_loader as loader


@pytest.fixture(autouse=True)
def reset_memory_cache(tmp_path, monkeypatch):
    """
    Remet le chargeur à zéro entre chaque test.

    Depuis OPT-04, l'état ne se réduit plus au cache mémoire : la fenêtre
    d'échec et le précalcul des champs sont remis à zéro aussi, sinon un
    échec simulé par un test couperait le réseau des suivants. Le cache
    disque est redirigé vers un répertoire temporaire (les replis ne lisent
    plus ~/.cache de la machine) et le fichier de schéma figé
    ($MOISSON_ZOTERO_SCHEMA_FILE, posé pour toute la suite) est retiré : ces
    tests exercent la stratégie ordinaire. La fenêtre d'échec et la durée
    de validité du cache, lues dans l'environnement à l'import du module,
    sont fixées à leurs valeurs par défaut (indépendance vis-à-vis du
    shell). L'état antérieur est restauré ensuite pour ne pas perturber les
    autres modules.
    """
    saved = (loader._MEMORY_CACHE, loader._UNAVAILABLE_UNTIL,
             loader._SCHEMA_ORIGIN, loader._INDEX)
    monkeypatch.delenv(loader.SCHEMA_FILE_ENV, raising=False)
    monkeypatch.setattr(loader, "CACHE_PATH", tmp_path / "schema.json")
    monkeypatch.setattr(loader, "SCHEMA_RETRY_AFTER_SECONDS", 600)
    monkeypatch.setattr(loader, "CACHE_MAX_AGE_SECONDS", 30 * 86400)
    loader.reset_state()
    yield
    (loader._MEMORY_CACHE, loader._UNAVAILABLE_UNTIL,
     loader._SCHEMA_ORIGIN, loader._INDEX) = saved


@pytest.fixture
def temp_cache():
    """Cache disque temporaire (déjà redirigé par la fixture automatique)."""
    return loader.CACHE_PATH


def _make_fake_schema():
    return {
        "version": 1,
        "itemTypes": [
            {
                "itemType": "journalArticle",
                "fields": [{"field": "title"}, {"field": "DOI"},
                           {"field": "publicationTitle"}],
                "creatorTypes": [
                    {"creatorType": "author"},
                    {"creatorType": "editor"},
                ],
            },
            {
                "itemType": "book",
                "fields": [{"field": "title"}, {"field": "publisher"}],
                "creatorTypes": [{"creatorType": "author"}],
            },
        ],
    }


class TestLoadZoteroSchema:
    def test_uses_memory_cache_if_set(self):
        loader._MEMORY_CACHE = _make_fake_schema()
        schema = loader.load_zotero_schema()
        assert schema == loader._MEMORY_CACHE

    def test_uses_disk_cache_when_fresh(self, temp_cache):
        fake = _make_fake_schema()
        temp_cache.parent.mkdir(parents=True, exist_ok=True)
        temp_cache.write_text(json.dumps(fake), encoding="utf-8")
        schema = loader.load_zotero_schema()
        assert schema == fake

    def test_falls_back_to_network_when_no_cache(self, temp_cache):
        fake = _make_fake_schema()
        with patch.object(loader, "_fetch_from_network", return_value=fake):
            schema = loader.load_zotero_schema()
        assert schema == fake
        assert temp_cache.exists()

    def test_returns_none_when_all_fail(self, temp_cache):
        with patch.object(loader, "_fetch_from_network", return_value=None) as f:
            schema = loader.load_zotero_schema()
            # OPT-04 : l'échec est mémorisé, pas de nouvelle tentative
            assert loader.load_zotero_schema() is None
        assert schema is None
        assert f.call_count == 1
        assert loader.schema_state() == loader.ETAT_INDISPONIBLE

    def test_force_refresh_ignores_caches(self, temp_cache):
        # Cache mémoire OBSOLÈTE
        loader._MEMORY_CACHE = {"version": 0}
        new_schema = _make_fake_schema()
        with patch.object(loader, "_fetch_from_network", return_value=new_schema):
            schema = loader.load_zotero_schema(force_refresh=True)
        assert schema == new_schema


class TestGetAllowedFields:
    def test_uses_schema_when_loaded(self):
        loader._MEMORY_CACHE = _make_fake_schema()
        fields = loader.get_allowed_fields("journalArticle")
        assert "DOI" in fields
        assert "title" in fields
        assert "publicationTitle" in fields
        # COMMON_FIELDS toujours inclus
        assert "itemType" in fields
        assert "creators" in fields

    def test_falls_back_to_embedded_when_no_schema(self):
        loader._MEMORY_CACHE = None
        with patch.object(loader, "_fetch_from_network", return_value=None):
            # Pas de cache disque non plus
            with patch.object(loader, "_is_cache_valid", return_value=False):
                fields = loader.get_allowed_fields("journalArticle")
        # Doit retourner les champs embarqués
        assert "title" in fields
        assert "DOI" in fields  # déjà dans le schéma embarqué

    def test_unknown_type_returns_document_fallback(self):
        loader._MEMORY_CACHE = _make_fake_schema()
        fields = loader.get_allowed_fields("unknownType")
        # Doit retourner au moins COMMON_FIELDS
        assert "creators" in fields
        assert "tags" in fields


class TestIsValidItemType:
    def test_valid_with_schema(self):
        loader._MEMORY_CACHE = _make_fake_schema()
        assert loader.is_valid_item_type("journalArticle") is True
        assert loader.is_valid_item_type("book") is True

    def test_invalid_with_schema(self):
        loader._MEMORY_CACHE = _make_fake_schema()
        assert loader.is_valid_item_type("blogPost") is False

    def test_fallback_embedded(self):
        loader._MEMORY_CACHE = None
        with patch.object(loader, "_fetch_from_network", return_value=None):
            with patch.object(loader, "_is_cache_valid", return_value=False):
                # journalArticle est dans le schéma embarqué
                assert loader.is_valid_item_type("journalArticle") is True
                assert loader.is_valid_item_type("invalid") is False


class TestCacheLifecycle:
    def test_save_and_load_roundtrip(self, temp_cache):
        fake = _make_fake_schema()
        loader._save_to_cache(temp_cache, fake)
        loaded = loader._load_from_cache(temp_cache)
        assert loaded == fake

    def test_cache_validity(self, temp_cache):
        fake = _make_fake_schema()
        loader._save_to_cache(temp_cache, fake)
        assert loader._is_cache_valid(temp_cache) is True

    def test_load_corrupt_cache_returns_none(self, temp_cache):
        temp_cache.parent.mkdir(parents=True, exist_ok=True)
        temp_cache.write_text("not valid json{{{", encoding="utf-8")
        assert loader._load_from_cache(temp_cache) is None

    def test_clear_cache(self, temp_cache):
        loader._MEMORY_CACHE = _make_fake_schema()
        loader._save_to_cache(temp_cache, _make_fake_schema())
        loader.clear_cache()
        assert loader._MEMORY_CACHE is None
        assert not temp_cache.exists()


@pytest.mark.integration
class TestIntegration:
    """Tests nécessitant le réseau (récupération réelle depuis api.zotero.org)."""

    def test_real_download(self, temp_cache):
        loader._MEMORY_CACHE = None
        schema = loader.load_zotero_schema(force_refresh=True)
        assert schema is not None
        assert "itemTypes" in schema
        # journalArticle doit exister dans le schéma officiel
        types = {it["itemType"] for it in schema["itemTypes"]}
        assert "journalArticle" in types
        assert "book" in types
        assert "thesis" in types
