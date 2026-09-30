"""
OPT-06 : extracteur commun d'identifiants (``utils.identity``) et table
unique des sources (``utils.source_labels``).
"""

import pytest

from utils.identity import (
    arxiv_from_doi, arxiv_from_url, canonical_url, doi_from_url,
    doi_in_extra_only, extract_doi, hal_id_from_url, identity_doi,
    is_generic_url, normalize_doi, normalize_isbns, strong_key_sources,
    strong_keys,
)
from utils.source_labels import (
    SOURCE_DISPLAY_NAMES, canonical_source_key, display_name, is_known_source,
)


# === DOI ===

class TestExtractDoi:
    def test_field(self):
        assert extract_doi({"DOI": "10.1234/ABC"}) == "10.1234/abc"

    @pytest.mark.parametrize("raw", [
        "https://doi.org/10.1234/x", "http://dx.doi.org/10.1234/x",
        "doi:10.1234/x", "DOI: 10.1234/x", "10.1234%2Fx", " 10.1234/x. ",
    ])
    def test_prefixes_and_encoding(self, raw):
        assert extract_doi({"DOI": raw}) == "10.1234/x"

    def test_extra_line_when_no_field(self):
        # Format exact posé par validate_and_clean pour un type sans champ DOI.
        item = {"itemType": "book", "DOI": "",
                "extra": "Sources: OpenEdition\nDOI: 10.4000/books.pufc.40815"}
        assert extract_doi(item) == "10.4000/books.pufc.40815"
        assert doi_in_extra_only(item)

    def test_validate_and_clean_roundtrip(self):
        """Quel que soit le schéma (DOI conservé ou migré dans extra), le DOI
        reste lisible par l'extracteur."""
        from mappers.zotero_schema import validate_and_clean
        item = validate_and_clean({
            "itemType": "thesis", "title": "T", "DOI": "10.5555/Thesis.1",
            "creators": [], "tags": [], "extra": "Sources: HAL",
        })
        assert extract_doi(item) == "10.5555/thesis.1"

    def test_field_wins_over_extra(self):
        item = {"DOI": "10.1/field", "extra": "DOI: 10.1/extra"}
        assert extract_doi(item) == "10.1/field"
        assert not doi_in_extra_only(item)

    def test_doi_source_line_is_never_a_doi(self):
        item = {"extra": "DOI source: appariement bibliographique CrossRef "
                         "(titre 0.95, année exacte, auteur)"}
        assert extract_doi(item) is None
        assert extract_doi({"extra": "DOI source: 10.1/x"}) is None

    def test_invalid_values(self):
        assert extract_doi({"DOI": "hal-01234567"}) is None
        assert extract_doi({"DOI": "  "}) is None
        assert extract_doi({}) is None
        assert normalize_doi(None) is None

    def test_url_doi_is_not_declared(self):
        item = {"url": "https://doi.org/10.4000/semen.2776"}
        assert extract_doi(item) is None
        assert identity_doi(item) == "10.4000/semen.2776"


# === URL ===

class TestCanonicalUrl:
    def test_scheme_www_case_slash(self):
        assert canonical_url("HTTPS://WWW.Example.org/Path/To/") == \
            canonical_url("http://example.org/path/to") == "example.org/path/to"

    def test_tracking_params_removed_and_sorted(self):
        a = canonical_url("https://ex.org/a?b=2&utm_source=x&a=1&fbclid=z")
        assert a == canonical_url("https://ex.org/a?a=1&b=2") == "ex.org/a?a=1&b=2"

    def test_html_entities_and_jsessionid(self):
        assert canonical_url("http://ex.org/F?func=a&amp;doc=1") == \
            "ex.org/f?doc=1&func=a"
        assert canonical_url("https://ex.org/rec;jsessionid=ABC?id=3") == \
            "ex.org/rec?id=3"

    def test_fragment_dropped_but_page_fragment_is_generic(self):
        assert canonical_url("https://ex.org/a#top") == "ex.org/a"
        assert canonical_url("https://lib.org/x.pdf#page=172") is None

    @pytest.mark.parametrize("url", [
        "https://www.revue.fr/", "https://revue.fr", "https://ex.org/index.html",
        "https://ex.org/search?q=discours",
        "https://www.cairn.info/resultats_recherche.php?searchTerm=x",
        "https://doi.org/", "https://scholar.google.com/scholar?cluster=1",
        "https://books.google.com/books?id=AbC&pg=PA1", "", None, "not a url",
    ])
    def test_generic_urls_are_not_keys(self, url):
        assert canonical_url(url) is None
        assert is_generic_url(url)

    def test_doi_org_url(self):
        assert canonical_url("https://dx.doi.org/10.1/ABC") == "doi.org/10.1/abc"


class TestUrlDerivedIdentifiers:
    @pytest.mark.parametrize("url,expected", [
        ("https://shs.hal.science/halshs-00367519v1", "halshs-00367519"),
        ("https://hal.univ-lorraine.fr/hal-01704097", "hal-01704097"),
        ("https://hal-univ-paris3.archives-ouvertes.fr/hal-01507295",
         "hal-01507295"),
        ("https://theses.hal.science/tel-03209752v1", "tel-03209752"),
        ("https://hal.science/hal-04613054v1/document", "hal-04613054"),
    ])
    def test_hal_id(self, url, expected):
        assert hal_id_from_url(url) == expected

    @pytest.mark.parametrize("url", [
        "https://hal.science/hal-02964840/document#page=172",
        "https://hal.science/search/index/?q=x",
        "https://example.org/hal-01234567",
    ])
    def test_no_hal_id(self, url):
        assert hal_id_from_url(url) is None

    def test_persee_doi_strict_pattern(self):
        assert doi_from_url(
            "https://www.persee.fr/doc/lgge_0458-726x_1992_num_26_105_1622"
        ) == "10.3406/lgge.1992.1622"
        # Sous-document d'une rubrique de comptes rendus : pas de DOI propre.
        assert doi_from_url(
            "https://www.persee.fr/doc/igram_0222-9838_2006_num_108_1_3806"
            "_t13_0055_0000_2") is None

    def test_publisher_paths(self):
        assert doi_from_url("https://www.science.org/doi/abs/10.1126/science.add6124") \
            == "10.1126/science.add6124"
        assert doi_from_url(
            "https://link.springer.com/chapter/10.1007/978-3-031-56066-8_14") \
            == "10.1007/978-3-031-56066-8_14"
        assert doi_from_url("https://example.org/10.1234/x") is None

    def test_arxiv(self):
        assert arxiv_from_url("http://arxiv.org/abs/2512.19117v2") == "2512.19117"
        assert arxiv_from_url("https://arxiv.org/pdf/2402.07043.pdf") == "2402.07043"
        assert arxiv_from_doi("10.48550/arXiv.2512.19117") == "2512.19117"


# === Clés fortes ===

class TestStrongKeys:
    def test_native_lines_of_the_mappers(self):
        item = {"extra": "\n".join([
            "Sources: HAL",
            "HAL ID: hal-01234567v2",
            "OAI Identifier: oai:revues.org:foo/1",
            "Isidore URI: https://isidore.science/resource/10670/1.abc",
            "OpenAlex ID: https://openalex.org/W123",
            "S2 corpus id: 555",
            "CORE ID: 77",
            "Scholar cluster ID: 14898197186914482422",
            "Scholar result ID: 9vjCT2gHwc4J",
            "PMID: 3141",
        ])}
        keys = strong_keys(item)
        for expected in ("hal:hal-01234567", "oai:revues.org:foo/1",
                         "isidore:10670/1.abc", "openalex:w123", "s2:555",
                         "core:77", "scholar:cluster/14898197186914482422",
                         "scholar:result/9vjCT2gHwc4J", "pmid:3141"):
            assert expected in keys
        assert keys == sorted(set(keys))

    def test_single_doi_key_declared_wins(self):
        item = {"DOI": "10.1/declared", "url": "https://doi.org/10.1/other"}
        assert [k for k in strong_keys(item) if k.startswith("doi:")] == \
            ["doi:10.1/declared"]

    def test_url_doi_used_when_not_declared(self):
        keys = dict(strong_key_sources({"url": "https://doi.org/10.1/x"}))
        assert keys == {"doi:10.1/x": True}

    def test_isbn_only_for_books(self):
        book = {"itemType": "book", "ISBN": "2-07-036822-X"}
        chapter = {"itemType": "bookSection", "ISBN": "2-07-036822-X"}
        assert "isbn:9782070368228" in strong_keys(book)
        assert not [k for k in strong_keys(chapter) if k.startswith("isbn:")]
        assert normalize_isbns("978-2-7535-4001-3, 2-07-036822-X") == \
            ["9782753540013", "9782070368228"]

    def test_alternative_urls_are_access_urls(self):
        item = {"url": "https://doi.org/10.4000/15bx2",
                "extra": "Alternative URLs: https://hdl.handle.net/20.500.13089/15bx2"
                         " | https://journals.openedition.org/ceg/23391"}
        keys = strong_keys(item)
        assert "url:journals.openedition.org/ceg/23391" in keys
        assert "url:hdl.handle.net/20.500.13089/15bx2" in keys
        assert "doi:10.4000/15bx2" in keys

    def test_arxiv_keys(self):
        preprint = {"DOI": "10.48550/arxiv.2512.19117"}
        scholar = {"url": "http://arxiv.org/abs/2512.19117"}
        assert "arxiv:2512.19117" in strong_keys(preprint)
        assert "arxiv:2512.19117" in strong_keys(scholar)

    def test_arxiv_line_of_a_published_version_is_not_a_key(self):
        # Semantic Scholar rattache la prépublication à l'article publié.
        published = {"DOI": "10.1/journal", "extra": "arXiv: 2402.07043",
                     "url": "https://arxiv.org/abs/2402.07043"}
        assert not [k for k in strong_keys(published) if k.startswith("arxiv:")]

    def test_generic_url_gives_no_key(self):
        assert strong_keys({"url": "https://www.revue.fr/"}) == []


# === Table des sources ===

class TestSourceLabels:
    @pytest.mark.parametrize("label,key", [
        ("HAL", "hal"), ("hal", "hal"), ("Isidore", "isidore"),
        ("OpenEdition", "openedition"), ("Cairn", "cairn"),
        ("Persée", "persee"), ("Persee", "persee"), ("PERSÉE", "persee"),
        ("Theses.fr", "theses_fr"), ("theses_fr", "theses_fr"),
        ("Érudit", "erudit"), ("Erudit", "erudit"),
        ("CrossRef", "crossref"), ("Crossref", "crossref"),
        ("OpenAlex", "openalex"), ("Semantic Scholar", "semantic_scholar"),
        ("semantic_scholar", "semantic_scholar"), ("CORE", "core"),
        ("Google Scholar", "google_scholar"),
        ("Google Scholar (SerpAPI)", "google_scholar"),
    ])
    def test_canonical_key(self, label, key):
        assert canonical_source_key(label) == key
        assert is_known_source(label)

    def test_unknown_label_is_normalized_not_rejected(self):
        assert canonical_source_key("Foo Bar") == "foo_bar"
        assert canonical_source_key("  ") == ""
        assert not is_known_source("Foo Bar")
        assert display_name("Foo Bar") == "Foo Bar"

    def test_display_names(self):
        assert display_name("persee") == "Persée"
        assert display_name("Crossref") == "CrossRef"
        assert display_name("google_scholar") == "Google Scholar"
        assert display_name("Google Scholar (SerpAPI)") == "Google Scholar"

    def test_table_covers_the_registry(self):
        from connectors.registry import get_connector_class, resolve_sources
        keys = set(resolve_sources(["all"])) | {"google_scholar"}
        assert keys == set(SOURCE_DISPLAY_NAMES)
        for key in keys:
            # Le libellé posé par chaque connecteur revient à sa clé.
            assert canonical_source_key(get_connector_class(key).SOURCE_NAME) == key

    def test_mapper_labels(self):
        from mappers import (
            core_mapper, crossref_mapper, google_scholar_mapper,
            openalex_mapper, semantic_scholar_mapper,
        )
        assert canonical_source_key(google_scholar_mapper.SOURCE_LABEL) == "google_scholar"
        assert canonical_source_key(crossref_mapper.SOURCE_LABEL) == "crossref"
        assert canonical_source_key(openalex_mapper.SOURCE_LABEL) == "openalex"
        assert canonical_source_key(semantic_scholar_mapper.SOURCE_LABEL) == "semantic_scholar"
        assert canonical_source_key(core_mapper.SOURCE_LABEL) == "core"
