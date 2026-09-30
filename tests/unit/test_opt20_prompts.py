"""
Tests OPT-20 — les quatre prompts MCP historiques.

Hors réseau, sans socket : appels directs de ``list_prompts`` et
``get_prompt``.

- Génération avec et sans arguments (``None``, ``{}``, arguments complets) :
  aucun gabarit vide (« « » », « {theme} »), l'argument manquant est demandé
  à l'utilisateur.
- Aucun texte ne mentionne ``workers`` (le défaut du serveur s'applique) ;
  aucune requête forcée entre guillemets (OPT-11, constat 4).
- Règles communes : sources indisponibles, limites du fournisseur et
  plafonds ; explication de la sélection (mode, tri, classement) ; résultats
  bibliographiques distincts de l'interprétation ; un résumé n'est pas le
  texte intégral ; lecture des résumés par ``corpus_query(ids=…)``.
- ``etat-de-l-art`` : variantes ``fr`` et ``en`` explicites, aucune
  traduction cachée, plan montré avant la moisson.
- ``profil-chercheur`` : flux d'homonymie (``ambiguous``, ``candidates``,
  ``profiles_to_confirm``, second appel), aucune promesse de production
  complète, ORCID seul : HAL et OpenAlex.
- Cohérence : chaque argument déclaré sert au texte ; chaque ``argument=``
  suggéré par un prompt existe dans le schéma d'un outil ; chaque outil nommé
  existe.
"""

import asyncio
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402

PROMPTS = ("bibliographie-thematique", "profil-chercheur", "veille-mensuelle",
           "etat-de-l-art")

FULL_ARGS = {
    "bibliographie-thematique": {"theme": "discours numérique",
                                 "max_per_source": "300"},
    "profil-chercheur": {"nom": "Patrick Charaudeau",
                         "idhal": "patrick-charaudeau",
                         "orcid": "0000-0002-3892-9611"},
    "veille-mensuelle": {"theme": "humanités numériques",
                         "from_date": "2026-09-01", "to_date": "2026-09-30"},
    "etat-de-l-art": {"question": "régulation des plateformes numériques",
                      "question_en": "platform regulation"},
}


def _run(coro):
    return asyncio.run(coro)


def _text(name, args):
    result = _run(mcp_server.get_prompt(name, args))
    assert result.messages and result.messages[0].role == "user"
    return result.messages[0].content.text


def _declared():
    return {p.name: p for p in _run(mcp_server.list_prompts())}


def _tool_properties():
    tools = _run(mcp_server.list_tools())
    return ({t.name for t in tools},
            {name for t in tools for name in t.inputSchema["properties"]})


# === Déclaration ===

class TestDeclaration:
    def test_four_prompts_are_declared(self):
        assert set(PROMPTS) <= set(_declared())

    def test_arguments(self):
        declared = _declared()
        args = {name: [(a.name, a.required) for a in declared[name].arguments]
                for name in PROMPTS}
        # Arguments historiques conservés, dans le même ordre ; ajout
        # facultatif seulement (question_en) ; nom devient facultatif (un
        # identifiant suffit, search_by_author accepte un nom vide).
        assert args["bibliographie-thematique"] == [
            ("theme", True), ("max_per_source", False)]
        assert args["profil-chercheur"] == [
            ("nom", False), ("idhal", False), ("orcid", False)]
        assert args["veille-mensuelle"] == [
            ("theme", True), ("from_date", True), ("to_date", True)]
        assert args["etat-de-l-art"] == [
            ("question", True), ("question_en", False)]

    def test_every_argument_is_described(self):
        for name, prompt in _declared().items():
            assert prompt.description and len(prompt.description) > 40
            for arg in prompt.arguments:
                assert arg.description and len(arg.description) > 10, (
                    name, arg.name)

    def test_descriptions_make_no_false_promise(self):
        declared = _declared()
        biblio = declared["bibliographie-thematique"]
        assert "bibliographie thématique exhaustive" not in \
            biblio.description.lower()
        assert "pas une collecte exhaustive" in biblio.description
        mps = next(a for a in biblio.arguments if a.name == "max_per_source")
        assert "500" not in mps.description
        profil = declared["profil-chercheur"].description
        assert "complète" not in profil and "homonym" in profil


# === Génération avec et sans arguments ===

class TestGeneration:
    @pytest.mark.parametrize("name", PROMPTS)
    @pytest.mark.parametrize("args", [None, {}])
    def test_without_arguments(self, name, args):
        text = _text(name, args)
        assert len(text) > 300
        assert "« »" not in text and "{" + "theme}" not in text
        assert "None" not in text
        # L'argument obligatoire manquant est demandé à l'utilisateur.
        assert "demande d'abord à l'utilisateur" in text

    @pytest.mark.parametrize("name", PROMPTS)
    def test_with_all_arguments(self, name):
        text = _text(name, FULL_ARGS[name])
        assert "demande d'abord à l'utilisateur" not in text
        assert "« »" not in text and "None" not in text

    @pytest.mark.parametrize("name", PROMPTS)
    def test_every_declared_argument_reaches_the_text(self, name):
        """Chaque argument déclaré est lu par get_prompt (et inversement,
        get_prompt ne lit que des arguments déclarés : une valeur non
        déclarée n'apparaît pas)."""
        prompt = _declared()[name]
        args = {a.name: f"VALEUR-{i}-{a.name}"
                for i, a in enumerate(prompt.arguments)}
        # Valeurs plausibles là où le texte les transforme.
        text = _text(name, dict(args, intrus="VALEUR-INTRUSE"))
        for value in args.values():
            assert value in text, (name, value)
        assert "VALEUR-INTRUSE" not in text

    def test_unknown_prompt(self):
        assert "Prompt inconnu" in _text("inexistant", None)


# === Règles communes ===

class TestCommonRules:
    @pytest.mark.parametrize("name", PROMPTS)
    def test_no_workers(self, name):
        for args in (None, FULL_ARGS[name]):
            assert "workers" not in _text(name, args)
        assert "workers" not in _declared()[name].description

    @pytest.mark.parametrize("name", ["bibliographie-thematique",
                                      "veille-mensuelle", "etat-de-l-art"])
    def test_query_is_not_forced_between_quotes(self, name):
        args = FULL_ARGS[name]
        value = args.get("theme") or args.get("question")
        text = _text(name, args)
        assert f"« {value} »" not in text
        assert f'"{value}"' not in text
        assert "sans guillemets ajoutés" in text
        assert value in text

    @pytest.mark.parametrize("name", PROMPTS)
    def test_sources_limits_and_caps(self, name):
        text = _text(name, FULL_ARGS[name])
        assert "non exécutée" in text and "en échec" in text
        assert "limite du fournisseur" in text and "pas une panne" in text
        assert "plafond" in text

    @pytest.mark.parametrize("name", PROMPTS)
    def test_selection_is_explained(self, name):
        text = _text(name, FULL_ARGS[name])
        assert "Sélection" in text
        if name == "profil-chercheur":
            assert "année décroissante" in text and "identity_basis" in text
        else:
            for fragment in ("mode", "bibliographie", "collecte",
                             "query_plan", "top_ranked"):
                assert fragment in text, (name, fragment)

    @pytest.mark.parametrize("name", PROMPTS)
    def test_results_versus_interpretation(self, name):
        text = _text(name, FULL_ARGS[name])
        assert "résultats bibliographiques" in text
        assert "interprétation" in text

    @pytest.mark.parametrize("name", PROMPTS)
    def test_abstract_is_not_the_full_text(self, name):
        text = _text(name, FULL_ARGS[name])
        assert "texte intégral" in text
        assert "ne prétends jamais avoir lu" in text

    @pytest.mark.parametrize("name", PROMPTS)
    def test_reads_abstracts_through_the_digest_and_corpus_query(self, name):
        text = _text(name, FULL_ARGS[name])
        assert "digest" in text
        assert "corpus_query" in text and "ids" in text
        assert "serveur distant" in text


# === Cohérence avec les outils ===

class TestToolCoherence:
    @pytest.mark.parametrize("name", PROMPTS)
    def test_suggested_arguments_exist(self, name):
        tool_names, properties = _tool_properties()
        for args in (None, FULL_ARGS[name]):
            text = _text(name, args)
            suggested = set(re.findall(r"\b([a-z][a-z_]*)=", text))
            assert suggested, name
            assert suggested <= properties, (name, suggested - properties)

    @pytest.mark.parametrize("name", PROMPTS)
    def test_named_tools_exist(self, name):
        tool_names, properties = _tool_properties()
        text = _text(name, FULL_ARGS[name])
        # Les arguments de corpus_diff (corpus_a, corpus_b) ne sont pas des
        # outils : seuls les autres noms en corpus_… doivent en être.
        named = set(re.findall(
            r"\b(moisson_search|search_by_author|fetch_by_doi|corpus_[a-z_]+)"
            r"\b", text)) - properties
        assert named and named <= tool_names, named - tool_names

    def test_default_cap_matches_the_server(self):
        from config import MCP_DEFAULT_MAX_PER_SOURCE
        text = _text("bibliographie-thematique", {"theme": "x y"})
        assert f"{MCP_DEFAULT_MAX_PER_SOURCE} par source" in text
        assert "max_per_source=" not in text      # défaut du serveur
        text = _text("bibliographie-thematique",
                     {"theme": "x y", "max_per_source": "350"})
        assert "max_per_source=350" in text


# === etat-de-l-art ===

class TestEtatDeLArt:
    def test_explicit_fr_and_en_variants(self):
        text = _text("etat-de-l-art", FULL_ARGS["etat-de-l-art"])
        assert "query_variants" in text
        assert '"fr"' in text and '"en"' in text
        assert "platform regulation" in text
        assert "le serveur ne traduit rien" in text
        # Plan montré avant la moisson (aucune traduction cachée).
        assert text.index("plan_only=true") < text.index("sans plan_only")
        assert 'sources=["all"]' in text

    def test_without_english_variant_the_agent_proposes_and_shows_it(self):
        text = _text("etat-de-l-art", {"question": "régulation des "
                                                    "plateformes numériques"})
        assert "Propose une formulation anglaise" in text
        assert "montre-la à l'utilisateur" in text
        assert '"fr"' in text and '"en"' in text

    def test_five_year_window(self):
        from datetime import datetime
        year = datetime.now().year
        text = _text("etat-de-l-art", FULL_ARGS["etat-de-l-art"])
        assert f"from_date={year - 5}" in text

    def test_scholar_only_on_request_and_keys(self):
        text = _text("etat-de-l-art", FULL_ARGS["etat-de-l-art"])
        assert "google_scholar" in text and "facturée" in text
        assert "OpenAlex et CORE exigent une clé" in text
        assert "max_per_source=1000" not in text


# === profil-chercheur ===

class TestProfilChercheur:
    def test_ambiguity_flow(self):
        text = _text("profil-chercheur", {"nom": "Jean Dupont"})
        for fragment in ("ambiguous", "Homonymie", "candidates",
                         "profiles_to_confirm", "argument", "hal_person_id",
                         "openalex_author_id", "s2_author_id",
                         "jamais le premier candidat"):
            assert fragment in text, fragment
        # Présenter, faire choisir, puis second appel.
        assert text.index("candidates") < text.index("second appel")

    def test_never_promises_the_complete_production(self):
        text = _text("profil-chercheur", FULL_ARGS["profil-chercheur"])
        assert "production_complete" in text
        assert "ne présente jamais la liste comme la « production complète »" \
            in text
        assert text.count("production complète") == 1

    def test_orcid_alone_queries_hal_and_openalex(self):
        text = _text("profil-chercheur", {"orcid": "0000-0002-3892-9611"})
        assert "l'ORCID seul interroge HAL et OpenAlex" in text
        assert "Isidore et Semantic Scholar ne sont pas interrogés" in text
        assert 'name=""' in text

    def test_orcid_with_name(self):
        text = _text("profil-chercheur", {"nom": "Jean Dupont",
                                          "orcid": "0000-0002-3892-9611"})
        assert "HAL et OpenAlex par identifiant" in text
        assert "name=Jean Dupont" in text and "orcid=0000-0002-3892-9611" \
            in text

    def test_idhal_alone(self):
        text = _text("profil-chercheur", {"idhal": "jean-dupont"})
        assert "seul HAL est interrogé" in text

    def test_names_are_not_quoted(self):
        text = _text("profil-chercheur", {"nom": "Patrick Charaudeau"})
        assert "« Patrick Charaudeau »" not in text
        assert "name=Patrick Charaudeau" in text


# === veille-mensuelle : harmonisation sans casser OPT-19 ===

class TestVeille:
    def test_reads_abstracts_of_priority_references(self):
        text = _text("veille-mensuelle", FULL_ARGS["veille-mensuelle"])
        step4 = text[text.index("4. "):text.index("5. ")]
        assert "corpus_query" in step4 and "ids" in step4
        assert "interprétation" in step4

    def test_missing_period_is_asked(self):
        text = _text("veille-mensuelle", {"theme": "humanités numériques"})
        assert "demande d'abord à l'utilisateur" in text
        assert re.search(r"(from|to)_date=(\s|,|\.|$)", text) is None
