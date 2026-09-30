"""
OPT-11 : registre des capacités de requête (connectors/capabilities.py) et
tableau daté de docs/API_SPECS.md.
"""

import re
from pathlib import Path

import pytest

from connectors import capabilities as caps_mod
from connectors.capabilities import (
    CAPABILITIES, STATUS_DOCUMENTED, STATUS_VERIFIED,
    STATUS_VERIFIED_VIA_ISIDORE, SYNTAX_BAG, capabilities_markdown,
    capabilities_table, get_capabilities,
)
from connectors.registry import (
    CONNECTORS, GROUP_PROXY_SOURCES, METERED_SOURCES, SOURCE_GROUPS,
)

ROOT = Path(__file__).resolve().parents[2]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_every_registered_source_is_described_and_nothing_else():
    assert set(CAPABILITIES) == set(CONNECTORS)


@pytest.mark.parametrize("key", sorted(CONNECTORS))
def test_description_is_complete(key):
    c = get_capabilities(key)
    assert c.key == key and c.label and c.endpoint
    for attr in ("pagination", "limits", "dates_detail", "languages_detail",
                 "sorts", "field_search"):
        assert getattr(c, attr), f"{key}.{attr} vide"
    assert set(c.ordering) == {"bibliographie", "collecte"}
    assert c.dates in ("native", "local")
    assert c.languages in ("native", "local", "none")
    assert c.key_required in ("none", "optional", "required")
    assert c.accents in ("folded", "significant", "unknown")
    assert c.corpus_language in ("fr", "en", "multi")
    assert DATE_RE.match(c.verified_on)
    assert c.evidence
    assert c.status in (STATUS_VERIFIED, STATUS_VERIFIED_VIA_ISIDORE,
                        STATUS_DOCUMENTED)
    if c.languages == "native":
        assert c.language_transport in ("search_kwarg", "constructor_lr")


def test_statuses_live_versus_documentation():
    documented = {k for k, c in CAPABILITIES.items()
                  if c.status == STATUS_DOCUMENTED}
    assert documented == {"core", "google_scholar"}
    for key in ["hal", "isidore", "crossref", "openalex", "semantic_scholar"]:
        assert CAPABILITIES[key].status == STATUS_VERIFIED
        assert CAPABILITIES[key].verified


def test_isidore_subsources_inherit_isidore_query_syntax():
    isidore = CAPABILITIES["isidore"]
    for key in GROUP_PROXY_SOURCES:
        c = CAPABILITIES[key]
        assert c.proxy_of == "isidore"
        assert c.status == STATUS_VERIFIED_VIA_ISIDORE
        for attr in ("syntax", "phrase", "and_op", "or_op", "not_op",
                     "not_style", "parentheses", "dates", "languages",
                     "corpus_language"):
            assert getattr(c, attr) == getattr(isidore, attr), (key, attr)


class TestVerifiedFacts:
    """Ce que les sondes du 2026-09-29 ont établi (voir docs/API_SPECS.md)."""

    def test_crossref_is_a_bag_of_words_without_language_filter(self):
        c = CAPABILITIES["crossref"]
        assert c.syntax == SYNTAX_BAG
        assert not c.phrase and c.and_op is c.or_op is c.not_op is None
        assert c.languages == "none" and c.accents == "significant"

    def test_openalex_honours_phrases_and_booleans(self):
        c = CAPABILITIES["openalex"]
        assert c.phrase and (c.and_op, c.or_op, c.not_op) == ("AND", "OR", "NOT")
        assert c.parentheses and c.languages == "native"
        assert c.accents == "significant"

    def test_semantic_scholar_bulk_symbols(self):
        c = CAPABILITIES["semantic_scholar"]
        assert c.phrase and (c.and_op, c.or_op, c.not_op) == ("+", "|", "-")
        assert c.not_style == "prefix" and c.parentheses
        assert not c.relevance_ranking and c.languages == "none"

    def test_isidore_french_operators(self):
        c = CAPABILITIES["isidore"]
        assert (c.and_op, c.or_op, c.not_op) == ("ET", "OU", "SAUF")
        assert c.dates == "local" and c.languages == "local"

    def test_hal_solr(self):
        c = CAPABILITIES["hal"]
        assert (c.and_op, c.or_op, c.not_op) == ("AND", "OR", "NOT")
        assert c.field_passthrough and c.languages == "native"
        assert c.apostrophe_restricts

    def test_core_not_is_undocumented(self):
        assert CAPABILITIES["core"].not_op is None
        assert CAPABILITIES["core"].wrap_top_level_or

    def test_no_source_claims_to_ignore_all_operators_but_crossref(self):
        without = {k for k, c in CAPABILITIES.items() if not c.supports_booleans}
        assert without == {"crossref"}


def test_scholar_stays_voluntary():
    """Invariant 4 : inchangé par OPT-11."""
    assert "google_scholar" in METERED_SOURCES
    assert "google_scholar" not in SOURCE_GROUPS["all"]
    assert "google_scholar" not in SOURCE_GROUPS["international"]
    assert CAPABILITIES["google_scholar"].corpus_language == "multi"


def test_table_and_serialization():
    rows = capabilities_table()
    assert [r["key"] for r in rows] == list(CAPABILITIES)
    assert all(isinstance(r["native_fields"], list) for r in rows)
    md = capabilities_markdown()
    assert md.count("\n") == 1 + 7        # en-tête, séparateur, 7 sources
    assert "`\\|`" in md                  # « | » échappé dans le tableau


def test_api_specs_table_matches_the_registry():
    text = (ROOT / "docs" / "API_SPECS.md").read_text(encoding="utf-8")
    block = text.split("<!-- capacites:debut -->", 1)[1].split(
        "<!-- capacites:fin -->", 1)[0].strip()
    assert block == capabilities_markdown()
    assert "vérifiées le 2026-09-29" in text


def test_registry_module_is_standalone():
    """Le registre se lit sans importer de connecteur ni ouvrir le réseau."""
    src = Path(caps_mod.__file__).read_text(encoding="utf-8")
    assert "import requests" not in src
    assert not re.search(r"^from connectors\.(?!capabilities)", src, re.M)
