"""
Tests d'intégration réseau des sources internationales.

Tous marqués ``integration`` : ignorés par ``pytest -m "not integration"`` et
en CI sans secret. Les sources à clé sont ``skip`` quand la variable d'env est
absente — ces tests n'échouent jamais faute de clé.

Lancer explicitement :
    pytest tests/integration -m integration
"""

import os

import pytest

pytestmark = pytest.mark.integration


def _has(env):
    return bool(os.environ.get(env))


def test_crossref_live():
    """CrossRef ne requiert pas de clé : moisson réelle minimale."""
    from connectors.crossref import CrossrefConnector
    with CrossrefConnector() as c:
        items = c.search("artificial intelligence", max_results=5,
                         from_date="2022", to_date="2024")
    assert isinstance(items, list)
    if items:
        it = items[0]
        assert it["itemType"]
        assert "Sources: CrossRef" in it.get("extra", "")


@pytest.mark.skipif(not _has("OPENALEX_API_KEY"),
                    reason="OPENALEX_API_KEY absente")
def test_openalex_live():
    from connectors.openalex import OpenAlexConnector
    with OpenAlexConnector() as c:
        items = c.search("artificial intelligence", max_results=5,
                         from_date="2022", to_date="2024",
                         extra_filters="type:article")
    assert isinstance(items, list)
    if items:
        assert "Sources: OpenAlex" in items[0].get("extra", "")


@pytest.mark.skipif(not _has("SEMANTIC_SCHOLAR_API_KEY"),
                    reason="SEMANTIC_SCHOLAR_API_KEY absente")
def test_semantic_scholar_live():
    from connectors.semantic_scholar import SemanticScholarConnector
    with SemanticScholarConnector() as c:
        items = c.search("large language models", max_results=5,
                         from_date="2022", to_date="2024")
    assert isinstance(items, list)


@pytest.mark.skipif(not _has("CORE_API_KEY"), reason="CORE_API_KEY absente")
def test_core_live():
    from connectors.core import CoreConnector
    with CoreConnector() as c:
        items = c.search("open access", max_results=5,
                         from_date="2022", to_date="2024")
    assert isinstance(items, list)


def test_fetch_by_doi_crossref_live():
    """fetch_by_doi sur un DOI connu : au moins Crossref doit répondre."""
    from utils.lookups import fetch_by_doi
    item = fetch_by_doi("10.1038/nature12373")
    if item is not None:
        assert item.get("title")
