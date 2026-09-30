"""
OPT-11 : analyse de la requête et plan par source (utils/query_plan.py).

Aucun accès réseau : le plan se calcule à partir du registre des capacités
(connectors/capabilities.py).
"""

import json

import pytest

from connectors.registry import resolve_sources
from utils.query_plan import (
    QueryPlanError, apply_local_language_filter, build_query_plan,
    check_date_range, item_languages, looks_french, normalize_item_language,
    normalize_languages, normalize_variants, parse_query, validate_date,
)

ALL = resolve_sources(["all"])
EVERY = resolve_sources(["all", "google_scholar"])
ISIDORE_FAMILY = ["isidore", "openedition", "cairn", "persee", "theses_fr",
                  "erudit"]


def sent(plan, key):
    return plan.for_source(key).query_sent


def plan_of(query, sources=EVERY, **kw):
    return build_query_plan(query, sources, **kw)


# === Règle d'identité : mots seuls ===

class TestSimpleQueries:
    @pytest.mark.parametrize("query", [
        "discours numérique",
        "Covid-19 : une crise",
        "science and society",          # opérateurs en minuscules = mots
        "guerre et paix ou rien",
        "quantitatif/qualitatif",
        "numéri*",
    ])
    def test_words_only_are_sent_unchanged_everywhere(self, query):
        plan = plan_of(query)
        for entry in plan.sources:
            assert entry.query_sent == query
            assert entry.translation == "unchanged"

    def test_whitespace_is_collapsed_only(self):
        plan = plan_of("  discours \t numérique  ")
        assert {e.query_sent for e in plan.sources} == {"discours numérique"}

    def test_accents_never_stripped(self):
        plan = plan_of("Écriture ÉCOLE œuvre")
        assert {e.query_sent for e in plan.sources} == {"Écriture ÉCOLE œuvre"}


# === Expressions exactes ===

class TestPhrases:
    @pytest.mark.parametrize("query", [
        '"discours numérique" twitter',
        '« discours numérique » twitter',
        '« discours numérique » twitter',     # espaces insécables
        '“discours numérique” twitter',
    ])
    def test_quotes_of_all_kinds_become_a_phrase(self, query):
        plan = plan_of(query)
        for key in ["hal", "isidore", "openalex", "semantic_scholar", "core",
                    "google_scholar"]:
            assert sent(plan, key) == '"discours numérique" twitter', key

    def test_crossref_receives_words_without_quotes_and_a_warning(self):
        plan = plan_of('"discours numérique" twitter')
        entry = plan.for_source("crossref")
        assert entry.query_sent == "discours numérique twitter"
        assert entry.translation == "bag_of_words"
        assert any("expression exacte non prise en charge" in w
                   for w in entry.warnings)
        assert '"' not in entry.query_sent

    def test_apostrophes_are_kept_inside_words_and_phrases(self):
        plan = plan_of("« l'école numérique » OU aujourd’hui")
        assert sent(plan, "hal") == '"l\'école numérique" OR aujourd’hui'
        assert sent(plan, "isidore") == '"l\'école numérique" OU aujourd’hui'
        assert sent(plan, "crossref") == "l'école numérique aujourd’hui"

    def test_hal_elision_note_only_when_an_apostrophe_is_present(self):
        with_apos = plan_of("\"l'école\" ET lecture").for_source("hal")
        without = plan_of('"école" ET lecture').for_source("hal")
        assert any("apostrophe" in n for n in with_apos.notes)
        assert not any("apostrophe" in n for n in without.notes)

    def test_accent_note_for_accent_sensitive_sources(self):
        plan = plan_of('"écriture numérique" OR lecture')
        assert any("accents significatifs" in n
                   for n in plan.for_source("crossref").notes)
        assert any("accents significatifs" in n
                   for n in plan.for_source("openalex").notes)
        assert not any("accents" in n for n in plan.for_source("hal").notes)

    def test_multilingual_phrases_are_kept_verbatim(self):
        plan = plan_of('"discours numérique" OR "digital discourse" '
                       'OR "discurso digital"')
        assert sent(plan, "hal") == ('"discours numérique" OR "digital '
                                     'discourse" OR "discurso digital"')
        assert sent(plan, "semantic_scholar") == (
            '"discours numérique" | "digital discourse" | "discurso digital"')


# === Opérateurs ===

class TestOperators:
    QUERY = '"discours numérique" AND (twitter OR facebook) NOT publicité'

    def test_hal_and_openalex_keep_english_operators(self):
        plan = plan_of(self.QUERY)
        expected = '"discours numérique" AND (twitter OR facebook) NOT publicité'
        assert sent(plan, "hal") == expected
        assert sent(plan, "openalex") == expected

    def test_isidore_receives_french_operators_only(self):
        """Vérifié : Isidore lit AND/OR/NOT comme des mots (sondes n° 17,
        19, 21)."""
        plan = plan_of(self.QUERY)
        for key in ISIDORE_FAMILY:
            q = sent(plan, key)
            assert q == ('"discours numérique" ET (twitter OU facebook) '
                         'SAUF publicité')
            assert not {"AND", "OR", "NOT"} & set(q.split())

    def test_isidore_family_shares_one_query(self):
        plan = plan_of(self.QUERY, languages=["fr"], from_date="2018")
        assert len({sent(plan, k) for k in ISIDORE_FAMILY}) == 1

    def test_semantic_scholar_symbols_and_no_literal_not(self):
        """Vérifié : « NOT » en toutes lettres est ignoré par le bulk (sonde
        n° 65) ; l'exclusion doit partir en « - »."""
        q = sent(plan_of(self.QUERY), "semantic_scholar")
        assert q == '"discours numérique" + (twitter | facebook) -publicité'
        assert "NOT" not in q.split() and "AND" not in q.split()

    def test_crossref_never_receives_an_excluded_word(self):
        """Vérifié : NOT n'exclut rien chez CrossRef (sondes n° 28 à 31) ;
        envoyer le mot exclu le rendrait positif."""
        entry = plan_of(self.QUERY).for_source("crossref")
        assert entry.query_sent == "discours numérique twitter facebook"
        assert "publicité" not in entry.query_sent
        assert any("publicité" in w and "exclusion" in w
                   for w in entry.warnings)
        assert any("OR non pris en charge" in w for w in entry.warnings)

    def test_core_drops_undocumented_not_and_wraps_top_level_or(self):
        plan = plan_of("(discours OR récit) NOT publicité")
        entry = plan.for_source("core")
        assert entry.query_sent == "(discours OR récit)"
        assert any("exclusion non documentée" in w for w in entry.warnings)
        assert sent(plan_of("discours OR récit"), "core") == "(discours OR récit)"

    def test_google_scholar_syntax(self):
        plan = plan_of(self.QUERY)
        assert sent(plan, "google_scholar") == (
            '"discours numérique" (twitter OR facebook) -publicité')

    def test_french_operators_in_input_are_translated_for_hal(self):
        plan = plan_of("discours ET numérique SAUF politique")
        assert sent(plan, "hal") == "discours AND numérique NOT politique"
        assert sent(plan, "isidore") == "discours ET numérique SAUF politique"
        assert sent(plan, "semantic_scholar") == "discours + numérique -politique"

    def test_minus_prefix_is_an_exclusion_and_hyphen_is_not(self):
        plan = plan_of("communication politique -publicité e-learning")
        assert sent(plan, "hal") == ("communication politique e-learning "
                                     "NOT publicité")
        assert sent(plan, "crossref") == "communication politique e-learning"

    def test_plus_prefix_is_a_plain_term(self):
        plan = plan_of("+discours +numérique")
        assert sent(plan, "hal") == "discours numérique"

    def test_or_not_branch_is_dropped_with_a_warning(self):
        plan = plan_of("discours OR NOT numérique")
        assert sent(plan, "isidore") == "discours"
        assert any("OR NOT" in w for w in plan.warnings)

    def test_parenthesis_errors_are_repaired_and_reported(self):
        plan = plan_of("(discours OR récit")
        assert sent(plan, "hal") == "discours OR récit"
        assert any("non fermée" in w for w in plan.warnings)
        plan = plan_of("discours) numérique")
        assert sent(plan, "hal") == "discours numérique"
        assert any("sans ouverture" in w for w in plan.warnings)

    def test_dangling_operators_are_reported(self):
        plan = plan_of("discours AND")
        assert sent(plan, "hal") == "discours"
        assert any("AND sans terme" in w for w in plan.warnings)

    def test_unclosed_quote_reads_rest_as_phrase(self):
        plan = plan_of('"discours numérique')
        assert sent(plan, "hal") == '"discours numérique"'
        assert any("non fermé" in w for w in plan.warnings)


class TestFields:
    def test_hal_passes_solr_fields_through(self):
        plan = plan_of('title_t:"discours numérique" -politique')
        assert sent(plan, "hal") == 'title_t:"discours numérique" NOT politique'

    def test_other_sources_search_the_value_in_full_text(self):
        plan = plan_of('title_t:"discours numérique" -politique')
        entry = plan.for_source("openalex")
        assert entry.query_sent == '"discours numérique" NOT politique'
        assert any("title_t" in w for w in entry.warnings)

    def test_core_native_field(self):
        assert sent(plan_of('title:"machine learning" OR abstract:ai'),
                    "core") == '(title:"machine learning" OR abstract:ai)'

    def test_urls_are_not_fields(self):
        assert parse_query("http://example.org discours").simple

    def test_solr_escapes_slash_and_stray_colon_in_complex_queries(self):
        plan = plan_of('"analyse" OR quantitatif/qualitatif OR Covid-19:crise')
        assert sent(plan, "hal") == ('"analyse" OR quantitatif\\/qualitatif '
                                     'OR Covid-19\\:crise')
        # Requête de mots seuls : transmise telle quelle (comportement
        # antérieur), sans échappement.
        assert sent(plan_of("quantitatif/qualitatif"), "hal") == \
            "quantitatif/qualitatif"


# === Requêtes invalides ===

class TestInvalidQueries:
    @pytest.mark.parametrize("query", [
        "", "   ", "AND OR NOT", "ET OU", '""', "( )", "« »", "NOT x",
        "SAUF politique", "-politique", ":", "NOT a OR NOT b",
    ])
    def test_rejected_with_a_clear_error(self, query):
        with pytest.raises(QueryPlanError):
            build_query_plan(query, ["hal"])

    def test_error_is_a_value_error(self):
        with pytest.raises(ValueError):
            parse_query("AND")

    def test_invalid_variant_is_rejected(self):
        with pytest.raises(QueryPlanError, match="variante 'en'"):
            build_query_plan("discours", ["crossref"],
                             query_variants={"en": "NOT x"})


# === Variantes par langue : aucune traduction implicite ===

class TestVariants:
    def test_no_english_variant_means_original_query_and_a_warning(self):
        plan = plan_of("discours numérique", ALL)
        for key in ["crossref", "openalex", "semantic_scholar", "core"]:
            entry = plan.for_source(key)
            assert entry.query_sent == "discours numérique"
            assert entry.query_origin == "original"
            assert any("requête française envoyée à une source "
                       "majoritairement anglophone" in w for w in entry.warnings)
        for key in ["hal"] + ISIDORE_FAMILY:
            assert not any("anglophone" in w
                           for w in plan.for_source(key).warnings)
        assert any("aucune variante 'en'" in w for w in plan.warnings)

    def test_group_all_does_not_make_a_french_query_multilingual(self):
        plan = plan_of('"discours numérique" OR "analyse du discours"', ALL)
        words = set()
        for entry in plan.sources:
            words.update(entry.query_sent.replace('"', " ").split())
        assert "digital" not in words and "discourse" not in words
        assert plan.as_dict()["automatic_translation"] is False
        assert {e.query_origin for e in plan.sources} == {"original"}

    def test_english_variant_goes_to_international_sources_only(self):
        plan = plan_of("discours numérique", EVERY,
                       query_variants={"en": '"digital discourse"'})
        for key in ["crossref", "openalex", "semantic_scholar", "core"]:
            assert plan.for_source(key).query_origin == "variant:en"
        assert sent(plan, "openalex") == '"digital discourse"'
        assert sent(plan, "crossref") == "digital discourse"
        for key in ["hal", "google_scholar"] + ISIDORE_FAMILY:
            assert plan.for_source(key).query_origin == "original"
            assert sent(plan, key) == "discours numérique"
        assert not any("anglophone" in w for e in plan.sources
                       for w in e.warnings)

    def test_french_variant_goes_to_francophone_sources(self):
        plan = plan_of("digital discourse", ALL,
                       query_variants={"fr": "discours numérique"})
        assert sent(plan, "hal") == "discours numérique"
        assert sent(plan, "cairn") == "discours numérique"
        assert sent(plan, "crossref") == "digital discourse"
        assert not any("française" in w
                       for w in plan.for_source("crossref").warnings)
        assert any("requête d'origine envoyée" in w
                   for w in plan.for_source("crossref").warnings)

    def test_unused_variant_is_reported(self):
        plan = plan_of("discours", ["hal"], query_variants={"es": "discurso"})
        assert any("variante 'es' non utilisée" in w for w in plan.warnings)

    def test_variants_normalization(self):
        assert normalize_variants({"EN": " digital "}) == {"en": "digital"}
        assert normalize_variants(None) == {}
        for bad in ({"english": "x"}, {"en": ""}, {"en": 3}, ["en"]):
            with pytest.raises(QueryPlanError):
                normalize_variants(bad)


# === Langues ===

class TestLanguages:
    def test_normalization(self):
        assert normalize_languages(["FR", "en", "fr"]) == ["fr", "en"]
        assert normalize_languages("fr, en") == ["fr", "en"]
        assert normalize_languages(None) == []
        for bad in (["xx"], ["fra"], ["french"], [3], 5):
            with pytest.raises(QueryPlanError):
                normalize_languages(bad)

    def test_native_local_or_impossible_per_source(self):
        plan = plan_of("discours", EVERY, languages=["fr", "en"])
        for key in ["hal", "openalex"]:
            entry = plan.for_source(key)
            assert entry.search_kwargs == {"languages": ["fr", "en"]}
            assert "languages" in entry.native_filters
            assert entry.local_languages == []
        for key in ISIDORE_FAMILY + ["core"]:
            entry = plan.for_source(key)
            assert entry.local_languages == ["fr", "en"]
            assert "languages" in entry.local_filters
            assert "languages" not in entry.search_kwargs
        for key in ["crossref", "semantic_scholar"]:
            entry = plan.for_source(key)
            assert not entry.search_kwargs and not entry.local_languages
            assert any("filtre de langue impossible" in w
                       for w in entry.warnings)
        scholar = plan.for_source("google_scholar")
        assert scholar.connector_kwargs == {"lr": "lang_fr|lang_en"}

    def test_no_languages_no_filters(self):
        plan = plan_of("discours", EVERY)
        for entry in plan.sources:
            assert not entry.search_kwargs and not entry.connector_kwargs
            assert not entry.local_languages
            assert "languages" not in entry.native_filters
            assert "languages" not in entry.local_filters

    @pytest.mark.parametrize("value, code", [
        ("fr", "fr"), ("FR", "fr"), ("fre", "fr"), ("fra", "fr"),
        ("French", "fr"), ("français", "fr"), ("fr-FR", "fr"), ("en_US", "en"),
        ("http://lexvo.org/id/iso639-3/fra", "fr"), (["eng", "fre"], "en"),
        ({"code": "es", "name": "Spanish"}, "es"), ("", None), (None, None),
        ("zzz", None), ("und", None),
    ])
    def test_item_language_reading(self, value, code):
        assert normalize_item_language(value) == code

    def test_item_with_several_languages_matches_any(self):
        assert item_languages("fr; en") == ["fr", "en"]

    def test_local_filter_keeps_unknown_and_drops_other_languages(self):
        items = [{"title": "a", "language": "fr"},
                 {"title": "b", "language": "English"},
                 {"title": "c"},
                 {"title": "d", "language": "fre"},
                 {"title": "e", "language": ["de", "fr"]}]
        kept, stats = apply_local_language_filter(items, ["fr"])
        assert [i["title"] for i in kept] == ["a", "c", "d", "e"]
        assert stats == {"kept": 4, "removed": 1, "unknown_kept": 1}
        assert items[1]["language"] == "English"      # notices non modifiées

    def test_local_filter_without_languages_is_identity(self):
        items = [{"language": "en"}]
        kept, stats = apply_local_language_filter(items, [])
        assert kept == items and stats["removed"] == 0


# === Dates ===

class TestDates:
    @pytest.mark.parametrize("value", ["2018", "2021-06-01", "2024-02-29"])
    def test_valid(self, value):
        assert validate_date(value, "from_date") == value

    @pytest.mark.parametrize("value", ["20", "2020-13-01", "2023-02-29",
                                       "2020/01/01", "2020-1-1", "hier"])
    def test_invalid(self, value):
        with pytest.raises(QueryPlanError):
            validate_date(value, "from_date")

    def test_range(self):
        check_date_range("2018", "2018")
        check_date_range("2020-06-01", "2020")
        check_date_range(None, "2020")
        with pytest.raises(QueryPlanError, match="Plage de dates vide"):
            check_date_range("2021", "2020")
        with pytest.raises(QueryPlanError):
            check_date_range("2020-06-02", "2020-06-01")
        with pytest.raises(QueryPlanError):
            build_query_plan("x", ["hal"], from_date="2024", to_date="2020")

    def test_native_and_local_date_filters(self):
        plan = plan_of("discours", EVERY, from_date="2018", to_date="2024")
        assert "dates" in plan.for_source("hal").native_filters
        assert "dates" in plan.for_source("crossref").native_filters
        for key in ISIDORE_FAMILY:
            assert "dates" in plan.for_source(key).local_filters
            assert "dates" not in plan.for_source(key).native_filters
        # Années entières : aucun avertissement de granularité.
        assert not any("ramenées" in w for e in plan.sources
                       for w in e.warnings)

    def test_year_granularity_warning_for_full_dates(self):
        plan = plan_of("discours", EVERY, from_date="2018-06-01")
        assert any("ramenées à l'année" in w
                   for w in plan.for_source("openalex").warnings)
        assert not any("ramenées" in w for w in plan.for_source("hal").warnings)
        assert not any("ramenées" in w
                       for w in plan.for_source("crossref").warnings)


# === Plan sérialisé et texte ===

class TestPlanOutput:
    def test_plan_is_json_serializable_and_ordered(self):
        plan = plan_of('"discours numérique" NOT publicité', ALL,
                       query_variants={"en": "digital discourse"},
                       languages=["fr"], from_date="2018", mode="collecte")
        data = plan.as_dict()
        json.dumps(data, ensure_ascii=False)
        assert data["version"] == 1
        assert [s["source"] for s in data["sources"]] == ALL
        assert data["mode"] == "collecte"
        assert data["derived"]["original"]["bag_of_words"] == "discours numérique"
        assert data["derived"]["original"]["excluded"] == ["publicité"]
        assert data["derived"]["en"]["simple"] is True
        # Rien d'exécutable (kwargs) ni de chemin dans le plan sérialisé.
        for entry in data["sources"]:
            assert "search_kwargs" not in entry and "connector_kwargs" not in entry

    def test_text_groups_sources_with_the_same_query(self):
        plan = plan_of('"discours numérique" OR récit', resolve_sources(
            ["francophone"]))
        lines = plan.text_lines()
        group = [l for l in lines if "Isidore" in l]
        assert len(group) == 1
        assert "OpenEdition" in group[0] and "Érudit" in group[0]

    def test_trivial_plan_has_a_one_line_summary(self):
        plan = plan_of("discours numérique", resolve_sources(["francophone"]))
        assert plan.trivial
        assert len(plan.summary_lines()) == 1
        busy = plan_of("discours numérique", ALL)
        assert not busy.trivial
        assert len(busy.summary_lines()) > 1

    def test_ordering_follows_the_mode(self):
        biblio = plan_of("discours", ["hal"], mode="bibliographie")
        collecte = plan_of("discours", ["hal"], mode="collecte")
        assert "score" in biblio.for_source("hal").ordering
        assert "docid" in collecte.for_source("hal").ordering

    def test_unknown_source_keeps_the_original_query(self):
        plan = build_query_plan('"a b" OR c', ["inconnue"])
        entry = plan.for_source("inconnue")
        assert entry.query_sent == '"a b" OR c'
        assert entry.warnings


def test_looks_french_heuristic():
    assert looks_french("discours numérique")
    assert looks_french("l'école")
    assert looks_french("histoire de la presse")
    assert not looks_french("digital discourse")
