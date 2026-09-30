# -*- coding: utf-8 -*-
"""
OPT-14, revue : non-régression des constats corrigés.

- second appel (mode identifiant) : une source revenue au nom et dont le
  profil n'est relié à aucun identifiant fourni est mise de côté (profil
  proposé à confirmer) ; Isidore, sans identifiant d'auteur, est retenu mais
  signalé comme non séparé des homonymes ;
- complétude : jamais promise quand des profils de nom compatible sont
  écartés, quand des dépôts HAL sans auteur identifié sont inclus, ni quand
  des notices reposent sur le nom seul ;
- dépôts HAL non rattachés signalés dans les réserves et le statut ;
- nom vide : refusé ; sans nom, les sources non interrogées figurent au
  rapport ;
- saisie « NOM Prénom » : signalée (jamais supposée) ;
- ORCID : forme ancrée et clé de contrôle ;
- prénoms comparés sur toute la longueur ;
- conseil ORCID du retour d'homonymie corrigé.

Aucun accès réseau (doublures de ``test_opt14_author``).
"""

from __future__ import annotations

import asyncio

import anyio
import pytest
import requests

import mcp_server
import utils.lookups as lookups

from test_opt14_author import (  # noqa: E402
    HAL_ONE_PERSON, NAME, S2_HOMONYMS, Router, _sdk_call, hal_facets,
    homonym_router, install, is_hal, is_openalex_authors, is_openalex_works,
    is_s2_papers, is_s2_search, item, oa_work, s2_details, s2_paper,
    use_router,
)

SOCIOLOGIST_ORCID = "0000-0002-3892-9611"
HISTORIAN_ORCID = "0000-0002-2384-5677"


@pytest.fixture(autouse=True)
def _schema_isolated(monkeypatch):
    import mappers.zotero_schema_loader as zsl
    monkeypatch.setattr(zsl, "load_zotero_schema", lambda *a, **k: None,
                        raising=False)


@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.setattr(lookups, "get_api_key",
                        lambda name, required=False: None)


@pytest.fixture
def keys(monkeypatch):
    table = {"OPENALEX_API_KEY": "KEY"}
    monkeypatch.setattr(lookups, "get_api_key",
                        lambda name, required=False: table.get(name))


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
    return tmp_path


def by_source(out):
    return {r.display_name: r for r in out.report.sources}


# Notices Isidore de deux homonymes (sociologue, historienne des teintures),
# indiscernables par le nom : cas réel rejoué d'après R13.
ISIDORE_MIXED = [
    item("Culture numérique", year="2019"),
    item("La garrigue, monde de l'écarlate", year="1999"),
]

S2_ONE_PRODUCTIVE = {"data": [
    {"authorId": "87578754", "name": "D. Cardon", "paperCount": 173,
     "affiliations": ["Sciences Po"]},
    {"authorId": "89654421", "name": NAME, "paperCount": 2},
]}


# === Second appel : profils choisis sur le nom mis de côté ===

class TestSecondCallIdentity:
    def test_s2_id_holds_hal_profile_and_flags_isidore(self, monkeypatch,
                                                       no_keys):
        # Second appel prescrit après l'homonymie : s2_author_id seul. HAL
        # n'a qu'une personne sous ce nom (choisie sur le nom, reliée à
        # aucun identifiant fourni) : mise de côté, proposée à confirmer.
        hal, _ = install(monkeypatch,
                         hal_items=[item("Dépôt HAL de 1017568", doi="10.1/h")],
                         isidore_items=ISIDORE_MIXED)
        router = Router([(is_hal, HAL_ONE_PERSON),
                         (is_s2_papers, {"data": [s2_paper(1)]})])
        out = lookups.search_by_author(NAME, s2_author_id="87578754",
                                       http_client=router)
        by = by_source(out)
        assert by["HAL"].reason == lookups.REASON_UNCONFIRMED_PROFILE
        assert by["HAL"].status == "skipped" and by["HAL"].items == []
        assert by["HAL"].counters["discarded"] == 1
        assert "Dépôt HAL de 1017568" not in {it["title"] for it in out}
        # Profil proposé à confirmer, avec l'argument à ajouter.
        (confirm,) = out.report.profiles_to_confirm
        assert confirm["argument"] == {"idhal": "dominique-cardon"}
        assert confirm["other_compatible_profiles"][0]["id"] == "838230"
        # Isidore : aucun identifiant d'auteur ; retenu mais signalé.
        assert by["Isidore"].unseparated and by["Isidore"].item_count == 2
        caveats = out.report.caveats()
        assert any(c.startswith("Isidore : notices retenues sur le nom seul, "
                                "non séparées d'éventuels homonymes")
                   for c in caveats)
        assert any('ajouter idhal="dominique-cardon"' in c for c in caveats)
        assert out.report.production_complete is False
        assert out.report.overall_status == "partial"
        assert by["Semantic Scholar"].identity_basis == lookups.BASIS_IDENTIFIER

    def test_orcid_not_on_s2_profile_holds_it(self, monkeypatch, keys):
        # Historienne choisie (OpenAlex A2 + son ORCID) : le seul profil S2
        # productif (sociologue, sans ORCID) n'est plus retenu sur le nom.
        install(monkeypatch)
        router = Router([
            (is_openalex_works, {"results": [oa_work(1)], "meta": {"count": 1}}),
            (is_s2_search, S2_ONE_PRODUCTIVE),
            (is_s2_papers, {"data": [s2_paper(1), s2_paper(2)]}),
        ])
        out = lookups.search_by_author(NAME, orcid=HISTORIAN_ORCID,
                                       openalex_author_id="A5027803088",
                                       http_client=router)
        s2 = by_source(out)["Semantic Scholar"]
        assert s2.reason == lookups.REASON_UNCONFIRMED_PROFILE
        assert "aucun profil examiné ne porte l'ORCID fourni" in s2.detail
        assert {it["title"] for it in out} == {"Travail OpenAlex numéro 1"}
        assert [p["argument"] for p in out.report.profiles_to_confirm] == [
            {"s2_author_id": "87578754"}]
        assert not out.report.production_complete

    def test_orcid_carried_by_profile_is_retained(self, monkeypatch, keys):
        install(monkeypatch)
        router = Router([
            (is_openalex_works, {"results": [], "meta": {"count": 0}}),
            (is_s2_search, {"data": [
                {"authorId": "87578754", "name": "D. Cardon",
                 "paperCount": 173,
                 "externalIds": {"ORCID": SOCIOLOGIST_ORCID}}]}),
            (is_s2_papers, {"data": [s2_paper(1)]}),
        ])
        out = lookups.search_by_author(NAME, orcid=SOCIOLOGIST_ORCID,
                                       http_client=router)
        s2 = by_source(out)["Semantic Scholar"]
        assert s2.identity_basis == lookups.BASIS_ORCID_MATCH
        assert s2.status == "complete" and s2.item_count == 1

    def test_ambiguity_in_identifier_mode_holds_single_profiles(
            self, monkeypatch, keys):
        # idHal fourni pour une autre source que HAL n'existe pas ici :
        # openalex_author_id seul, S2 ambigu dans l'appel. HAL (seule
        # personne, choisie sur le nom) mis de côté ; Isidore écarté.
        install(monkeypatch, hal_items=[item("HAL A")],
                isidore_items=[item("Isidore A")])
        router = Router([
            (is_hal, HAL_ONE_PERSON),
            (is_openalex_works, {"results": [oa_work(1)], "meta": {"count": 1}}),
            (is_s2_search, S2_HOMONYMS),
            (lambda u, p: is_s2_papers(u, p) and "offset" not in p, s2_details),
        ])
        out = lookups.search_by_author(NAME, openalex_author_id="A1",
                                       http_client=router)
        by = by_source(out)
        assert by["HAL"].reason == lookups.REASON_UNCONFIRMED_PROFILE
        assert by["Isidore"].reason == lookups.REASON_AWAITING_IDENTITY
        assert by["Semantic Scholar"].ambiguous
        assert {it["title"] for it in out} == {"Travail OpenAlex numéro 1"}

    def test_all_held_is_not_a_total_failure(self, monkeypatch, no_keys,
                                             out_dir):
        # OpenAlex sans clé (identifiant inutilisable), HAL et S2 choisis
        # sur le nom et mis de côté, Isidore en panne : pas « aucune source
        # n'a pu être interrogée » ; retour non-erreur, profils à confirmer.
        install(monkeypatch, hal_items=[item("HAL A")], isidore_fail=True)
        router = Router([(is_hal, HAL_ONE_PERSON),
                         (is_s2_search, S2_ONE_PRODUCTIVE),
                         (is_s2_papers, {"data": [s2_paper(1)]})])
        use_router(monkeypatch, router)
        out = lookups.search_by_author(NAME, openalex_author_id="A1",
                                       http_client=router)
        assert not out.report.all_failed and len(out) == 0
        res = anyio.run(_sdk_call, "search_by_author",
                        {"name": NAME, "openalex_author_id": "A1"})
        assert res.isError is False
        text = res.content[0].text
        assert "Profils trouvés par le nom, non retenus" in text
        assert 'idhal="dominique-cardon"' in text
        assert 's2_author_id="87578754"' in text
        assert "parcourues jusqu'au bout" not in text
        assert len(res.structuredContent["profiles_to_confirm"]) == 2
        assert list(out_dir.glob("corpus_*")) == []

    def test_name_only_without_ambiguity_keeps_single_profiles(
            self, monkeypatch, no_keys):
        # Non-régression : sans identifiant ni homonymie, le seul profil
        # productif reste retenu (aucune source ne l'a contredit).
        install(monkeypatch, hal_items=[item("HAL A")])
        router = Router([(is_hal, HAL_ONE_PERSON),
                         (is_s2_search, S2_ONE_PRODUCTIVE),
                         (is_s2_papers, {"data": [s2_paper(1)]})])
        out = lookups.search_by_author(NAME, http_client=router)
        by = by_source(out)
        assert by["HAL"].status == "complete" and by["HAL"].item_count == 1
        assert by["Semantic Scholar"].status == "complete"
        assert out.report.profiles_to_confirm == []


# === Complétude : profils écartés, dépôts non rattachés, nom seul ===

class TestCompleteness:
    def test_excluded_compatible_profiles_forbid_completeness(
            self, monkeypatch, keys, out_dir):
        install(monkeypatch)
        router = Router([
            (is_hal, hal_facets()),
            (is_openalex_authors, {"results": [
                {"id": "https://openalex.org/A1", "display_name": NAME,
                 "works_count": 40},
                {"id": "https://openalex.org/A2", "display_name": NAME,
                 "works_count": 3}]}),
            (is_openalex_works, {"results": [oa_work(1)], "meta": {"count": 1}}),
            (is_s2_search, {"data": []}),
        ])
        out = lookups.search_by_author(NAME, http_client=router)
        oa = by_source(out)["OpenAlex"]
        assert [c["id"] for c in oa.excluded] == ["A2"]
        assert oa.as_dict()["excluded_profiles"][0]["works_count"] == 3
        assert not oa.identity_established
        assert out.report.overall_status == "ok"
        assert out.report.production_complete is False
        assert any("OpenAlex : profil retenu sur le nom ; 1 autre(s) "
                   "profil(s) de nom compatible non inclus (A2 : 3 travaux)"
                   in c for c in out.report.caveats())
        use_router(monkeypatch, router)
        blocks, sc = asyncio.run(mcp_server._tool_search_by_author(
            {"name": NAME}))
        assert sc["production_complete"] is False
        assert "parcourues jusqu'au bout" not in blocks[0].text
        assert "Production possiblement incomplète" in blocks[0].text

    def test_hal_unlinked_deposits_are_reported(self, monkeypatch, no_keys):
        install(monkeypatch, hal_items=[item(f"HAL {i}", doi=f"10.1/{i}")
                                        for i in range(3)])
        router = Router([(is_hal, hal_facets(("123", "jean-dupont", 30),
                                             ("0", "", 50))),
                         (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, http_client=router)
        hal = by_source(out)["HAL"]
        assert hal.identity_basis == lookups.BASIS_SINGLE
        assert hal.unlinked_included == 50
        assert not hal.identity_established
        assert ("HAL : 50 dépôt(s) sans auteur HAL identifié inclus "
                "(identité non vérifiée, homonymes possibles)"
                in out.report.caveats())
        assert "50 dépôt(s) sans auteur HAL identifié inclus" in hal.line()
        assert out.report.production_complete is False

    def test_name_only_notices_forbid_completeness(self, monkeypatch, no_keys):
        install(monkeypatch, isidore_items=[item("Isidore A")])
        router = Router([(is_hal, hal_facets()),
                         (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, http_client=router)
        assert out.report.production_complete is False

    def test_completeness_when_everything_is_established(
            self, monkeypatch, keys, out_dir):
        # Chaque source parcourue jusqu'au bout, identité unique sans autre
        # profil compatible, aucune notice au nom seul : complétude dite.
        install(monkeypatch, hal_items=[item("HAL A", doi="10.1/h")])
        router = Router([
            (is_hal, hal_facets(("1017568", "dominique-cardon", 226))),
            (is_openalex_authors, {"results": [
                {"id": "https://openalex.org/A1", "display_name": NAME,
                 "works_count": 40}]}),
            (is_openalex_works, {"results": [oa_work(1)], "meta": {"count": 1}}),
            (is_s2_search, {"data": [
                {"authorId": "87578754", "name": NAME, "paperCount": 1}]}),
            (is_s2_papers, {"data": [s2_paper(1)]}),
        ])
        out = lookups.search_by_author(NAME, http_client=router)
        assert out.report.caveats() == []
        assert out.report.production_complete is True
        use_router(monkeypatch, router)
        blocks, sc = asyncio.run(mcp_server._tool_search_by_author(
            {"name": NAME, "save_corpus": False}))
        assert sc["production_complete"] is True
        assert "parcourues jusqu'au bout" in blocks[0].text


# === Nom vide ===

class TestEmptyName:
    @pytest.mark.parametrize("name", ["", "   "])
    def test_library_refuses_empty_name_without_identifier(self, name):
        with pytest.raises(ValueError):
            lookups.search_by_author(name, http_client=Router([]))

    @pytest.mark.parametrize("name", ["", "   "])
    def test_tool_refuses_empty_name(self, name, out_dir):
        res = anyio.run(_sdk_call, "search_by_author", {"name": name})
        assert res.isError is True
        assert "Paramètre refusé : name vide" in res.content[0].text
        assert list(out_dir.glob("corpus_*")) == []

    def test_sources_not_queried_are_reported(self, monkeypatch, keys):
        # Vérification finale du lot C : sans nom, l'ORCID interroge HAL et
        # OpenAlex (filtre author.orcid, comme le promet l'argument orcid) ;
        # Isidore et Semantic Scholar, sans identifiant propre, figurent au
        # rapport sans être interrogés.
        hal, iso = install(monkeypatch, hal_items=[item("HAL A")])
        router = Router([(is_openalex_works,
                          {"results": [oa_work(1)], "meta": {"count": 1}})])
        out = lookups.search_by_author("", orcid=SOCIOLOGIST_ORCID,
                                       http_client=router)
        by = by_source(out)
        assert list(by) == ["HAL", "Isidore", "OpenAlex", "Semantic Scholar"]
        for key in ("Isidore", "Semantic Scholar"):
            assert by[key].status == "skipped"
            assert by[key].reason == lookups.REASON_NO_IDENTITY
        assert by["OpenAlex"].method == lookups.METHOD_ORCID
        assert by["OpenAlex"].item_count == 1
        assert [p.get("filter") for _, p in router.calls] == [
            f"author.orcid:{SOCIOLOGIST_ORCID}"]
        assert iso.calls == []
        assert out.report.overall_status == "partial"
        assert out.report.production_complete is False
        assert any(c.startswith("Isidore : non interrogée")
                   for c in out.report.caveats())


# === Saisie « NOM Prénom » ===

class TestInvertedName:
    @pytest.mark.parametrize("query", ["Cardon Dominique", "CARDON Dominique"])
    def test_inverted_query_is_signalled_not_assumed(self, monkeypatch,
                                                     no_keys, query):
        install(monkeypatch, isidore_items=[item("Culture numérique"),
                                            item("Métier")])
        router = Router([
            (is_hal, hal_facets()),
            (is_s2_search, {"data": [
                {"authorId": "87578754", "name": NAME, "paperCount": 173}]}),
        ])
        out = lookups.search_by_author(query, http_client=router)
        assert len(out) == 0          # la règle d'identité n'est pas assouplie
        by = by_source(out)
        assert by["Isidore"].name_hint == NAME
        assert by["Isidore"].counters["inverted_name_matches"] == 2
        assert by["Semantic Scholar"].name_hint == NAME
        assert "au nom « Dominique Cardon »" in by["Semantic Scholar"].detail
        caveat = out.report.caveats()[0]
        assert caveat.startswith("Nom peut-être saisi « NOM Prénom »")
        assert 'relancer avec name="Dominique Cardon"' in caveat
        assert out.report.production_complete is False

    def test_correct_query_gets_no_hint(self, monkeypatch, no_keys):
        install(monkeypatch, isidore_items=[
            item("A"), item("B", first="Cardon", last="Dominique")])
        router = Router([(is_hal, hal_facets()),
                         (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, http_client=router)
        assert by_source(out)["Isidore"].name_hint is None
        assert not any("NOM Prénom" in c for c in out.report.caveats())

    def test_inverted_name_form(self):
        assert lookups.inverted_name_form("Cardon Dominique",
                                          "Dominique", "Cardon") == NAME
        assert lookups.inverted_name_form(NAME, "Dominique", "Cardon") is None
        assert lookups.inverted_name_form("Cardon, Dominique",
                                          "Cardon", "Dominique") is None
        assert lookups.inverted_name_form(
            "Olivier de Sardan Jean-Pierre", "Jean-Pierre",
            "Olivier de Sardan") == "Jean-Pierre Olivier de Sardan"


# === ORCID ===

class TestOrcid:
    @pytest.mark.parametrize("value", [
        "0000-0002-3892-96111", "10000-0002-3892-9611",
        "https://orcid.org/0000-0002-3892-9611/works", "0000-0002-3892-9612",
        "ORCID 0000-0002-3892-9611 (sociologue)",
    ])
    def test_malformed_or_wrong_check_digit_is_refused(self, value):
        with pytest.raises(ValueError):
            lookups.normalize_orcid(value)

    @pytest.mark.parametrize("value, expected", [
        ("0000-0002-3892-9611", "0000-0002-3892-9611"),
        ("https://orcid.org/0000-0002-1694-233x/", "0000-0002-1694-233X"),
        (" orcid.org/0000-0002-1825-0097 ", "0000-0002-1825-0097"),
    ])
    def test_valid_forms_are_accepted(self, value, expected):
        assert lookups.normalize_orcid(value) == expected

    def test_check_digit(self):
        assert lookups.orcid_checksum_ok("0000-0002-1825-0097")
        assert not lookups.orcid_checksum_ok("0000-0002-1825-0096")

    def test_library_reports_wrong_check_digit(self, monkeypatch, no_keys):
        # Appelants historiques : pas d'exception, mais réserve en tête et
        # aucune promesse de complétude.
        install(monkeypatch)
        router = Router([(is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, orcid="0000-0002-3892-9612",
                                       http_client=router)
        assert out.report.notes
        assert out.report.caveats()[0].startswith(
            "ORCID 0000-0002-3892-9612 : clé de contrôle incorrecte")
        assert out.report.production_complete is False

    def test_tool_refuses_wrong_check_digit(self, out_dir):
        res = anyio.run(_sdk_call, "search_by_author",
                        {"name": NAME, "orcid": "0000-0002-3892-9612"})
        assert res.isError is True
        assert "clé de contrôle" in res.content[0].text


# === Prénoms sur toute la longueur ===

class TestGivenNames:
    @pytest.mark.parametrize("query, first", [
        ("Jean Martin", "Jean-P."), ("Jean Martin", "Jean P."),
        ("Jean Martin", "Jean-Pierre"), ("Dominique Cardon", "Dominique M."),
        ("Dominique Cardon", "Dominique Marie"), ("J. Martin", "Jean-Pierre"),
    ])
    def test_extra_given_name_is_refused(self, query, first):
        last = query.split()[-1]
        assert not lookups.names_match(query, first, last)

    @pytest.mark.parametrize("query, first", [
        ("Jean-Pierre Martin", "J.-P."), ("J. Martin", "Jean"),
        ("Jean-Pierre Martin", "Jean P."),
    ])
    def test_initials_position_by_position(self, query, first):
        assert lookups.names_match(query, first, query.split()[-1])

    def test_isidore_filter_is_consistent(self, monkeypatch, no_keys):
        install(monkeypatch, isidore_items=[
            item("A"), item("B", first="Dominique M."),
            item("C", first="Dominique Marie")])
        router = Router([(is_hal, hal_facets()),
                         (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, http_client=router)
        assert {it["title"] for it in out} == {"A"}
        assert by_source(out)["Isidore"].rejected_not_author == 2


# === Retour d'homonymie : conseil ORCID et profils à confirmer ===

class TestAmbiguityAdvice:
    def test_orcid_advice_and_profiles_to_confirm(self, monkeypatch, keys,
                                                  out_dir):
        install(monkeypatch, hal_items=[item("HAL A")])
        use_router(monkeypatch, homonym_router())
        res = anyio.run(_sdk_call, "search_by_author", {"name": NAME})
        text = res.content[0].text
        assert res.isError is False
        assert "un ORCID vaut pour HAL, OpenAlex et Semantic Scholar" not in text
        assert "rarement renseigné dans HAL et Semantic Scholar" in text
        assert "Profils trouvés par le nom dans les autres sources" in text
        assert 'idhal="dominique-cardon"' in text
        sc = res.structuredContent
        (hal,) = sc["profiles_to_confirm"]
        assert hal["argument"] == {"idhal": "dominique-cardon"}
        assert hal["source_status"] == lookups.REASON_AWAITING_IDENTITY
        assert "profiles_to_confirm" in sc["next_step"]
        # Candidats des sources ambiguës inchangés (HAL n'y figure pas).
        assert {c["source"] for c in sc["candidates"]} == {
            "OpenAlex", "Semantic Scholar"}

    def test_ambiguous_source_says_orcid_designated_nothing(self, monkeypatch,
                                                           no_keys):
        # Constat live E4 : l'ORCID du sociologue ne figure sur aucun profil
        # S2 ; la réserve de la source ambiguë le dit.
        install(monkeypatch)
        router = Router([
            (is_s2_search, S2_HOMONYMS),
            (lambda u, p: is_s2_papers(u, p) and "offset" not in p, s2_details),
        ])
        out = lookups.search_by_author(NAME, orcid=SOCIOLOGIST_ORCID,
                                       http_client=router)
        assert any(c.startswith("Semantic Scholar : homonymes")
                   and c.endswith("aucun profil examiné ne porte l'ORCID "
                                  "fourni") for c in out.report.caveats())

    def test_schema_orcid_description_is_cautious(self):
        tools = asyncio.run(mcp_server.list_tools())
        tool = next(t for t in tools if t.name == "search_by_author")
        desc = tool.inputSchema["properties"]["orcid"]["description"]
        assert "rarement renseigné dans HAL" in desc


def test_error_source_does_not_hide_held_profiles(monkeypatch, no_keys):
    """Une panne ailleurs n'efface pas les profils à confirmer."""
    install(monkeypatch, hal_items=[item("HAL A")])
    router = Router([(is_hal, HAL_ONE_PERSON),
                     (is_s2_search, requests.ConnectionError("x"))])
    out = lookups.search_by_author(NAME, openalex_author_id="A1",
                                   http_client=router)
    by = by_source(out)
    assert by["Semantic Scholar"].status == "error"
    assert by["HAL"].held and not out.report.all_failed
