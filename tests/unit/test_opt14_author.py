# -*- coding: utf-8 -*-
"""
OPT-14 : recherche d'auteur fiable (constat C28).

- identifiants explicites prioritaires : ORCID, idHal, identifiant de
  personne HAL, identifiants d'auteur OpenAlex et Semantic Scholar ; avec un
  identifiant, aucune recherche par nom pour la source concernée ;
- recherche par nom : plusieurs profils examinés ; homonymes (au moins deux
  profils plausibles d'au moins 5 travaux) rendus comme candidats, sans
  moissonner le premier ; un seul candidat plausible retenu et nommé ;
- pagination OpenAlex (curseur) et Semantic Scholar (décalage) au-delà d'une
  page, jusqu'au plafond ou à épuisement ;
- Isidore : notices dont les créateurs ne portent pas le nom écartées et
  comptées (``rejected_not_author``) ;
- statuts par source, ``isError`` si toutes les sources échouent, retour
  non-erreur avec ``structuredContent.candidates`` en cas d'homonymie ;
- arguments historiques inchangés.

Aucun accès réseau : client HTTP simulé (routeur de réponses), connecteurs
HAL et Isidore remplacés par des doublures.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import anyio
import pytest
import requests

import mcp_server
import utils.lookups as lookups
from engine.models import SearchReport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OPENALEX_WORK = json.loads((FIXTURES / "openalex_work.json").read_text("utf-8"))
S2_PAPER = json.loads((FIXTURES / "s2_paper.json").read_text("utf-8"))

NAME = "Dominique Cardon"


@pytest.fixture(autouse=True)
def _schema_isolated(monkeypatch):
    """Schéma Zotero neutralisé (repli embarqué, aucun téléchargement)."""
    import mappers.zotero_schema_loader as zsl
    monkeypatch.setattr(zsl, "load_zotero_schema", lambda *a, **k: None,
                        raising=False)


@pytest.fixture
def keys(monkeypatch):
    """Clés simulées : OpenAlex présente, Semantic Scholar absente."""
    table = {"OPENALEX_API_KEY": "KEY"}
    monkeypatch.setattr(lookups, "get_api_key",
                        lambda name, required=False: table.get(name))
    return table


# === Doublures ===

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.from_cache = False

    def json(self):
        return self._payload


class Router:
    """
    Client HTTP simulé : chaque requête est servie par la première route
    dont le prédicat accepte ``(url, params)`` ; une requête sans route fait
    échouer le test (aucun appel inattendu).
    """

    cancel_signal = None

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def get(self, url, params=None, headers=None, **kwargs):
        params = dict(params or {})
        self.calls.append((url, params))
        for match, answer in self.routes:
            if match(url, params):
                if callable(answer):
                    answer = answer(url, params)
                if isinstance(answer, int):
                    raise requests.HTTPError(response=_Resp({}, answer))
                if isinstance(answer, Exception):
                    raise answer
                return _Resp(answer)
        raise AssertionError(f"requête inattendue : {url} {params}")

    def close(self):
        pass

    def urls(self, fragment):
        return [(u, p) for u, p in self.calls if fragment in u]


def is_openalex_authors(url, params):
    return url.endswith("/authors")


def is_openalex_works(url, params):
    return url.endswith("api.openalex.org/works")


def is_s2_search(url, params):
    return url.endswith("/author/search")


def is_s2_papers(url, params):
    return "/author/" in url and url.endswith("/papers")


def is_hal(url, params):
    return "archives-ouvertes" in url


def oa_work(i):
    w = copy.deepcopy(OPENALEX_WORK)
    w["id"] = f"https://openalex.org/W{i}"
    w["doi"] = f"https://doi.org/10.9999/oa.{i}"
    w["title"] = w["display_name"] = f"Travail OpenAlex numéro {i}"
    w["ids"] = {"openalex": w["id"], "doi": w["doi"]}
    return w


def s2_paper(i):
    p = copy.deepcopy(S2_PAPER)
    p["title"] = f"Travail Semantic Scholar numéro {i}"
    p["externalIds"] = {"DOI": f"10.9999/s2.{i}"}
    p["url"] = f"https://www.semanticscholar.org/paper/p{i}"
    return p


def item(title, first="Dominique", last="Cardon", year="2020", doi=None,
         creators=None):
    it = {"itemType": "journalArticle", "title": title, "date": year,
          "creators": creators if creators is not None else [
              {"creatorType": "author", "firstName": first, "lastName": last}],
          "tags": [], "collections": [], "relations": {}, "extra": ""}
    if doi:
        it["DOI"] = doi
    return it


def make_connector(items=(), fail=None, calls=None):
    """Doublure de connecteur : notices fixes, compte rendu, requêtes notées."""
    calls = calls if calls is not None else []

    class _Fake:
        def __init__(self, http_client=None, **kwargs):
            self.http = http_client
            self.last_status = None

        def search(self, query, max_results=1000, **kwargs):
            calls.append((query, max_results))
            self.last_status = SearchReport(source="fake")
            if fail is not None:
                self.last_status.fail("http_error", "HTTP 503")
                return []
            everything = copy.deepcopy(list(items))
            out = everything[:max_results]
            self.last_status.page(len(out))
            if len(everything) > max_results:
                self.last_status.limit()     # comme les vrais connecteurs
            else:
                self.last_status.done()
            return out
    _Fake.calls = calls
    return _Fake


def install(monkeypatch, hal_items=(), isidore_items=(), hal_fail=None,
            isidore_fail=None):
    hal = make_connector(hal_items, hal_fail)
    iso = make_connector(isidore_items, isidore_fail)
    monkeypatch.setattr(lookups, "HALConnector", hal)
    monkeypatch.setattr(lookups, "IsidoreConnector", iso)
    return hal, iso


def hal_facets(*entries, total=None):
    """Réponse HAL de la facette auteur (entrées : personne, idHal, nombre)."""
    values = []
    for person, idhal, count in entries:
        values += [f"{NAME}_FacetSep_{person}_FacetSep_{idhal}", count]
    n = total if total is not None else sum(e[2] for e in entries)
    return {"response": {"numFound": n, "docs": []},
            "facet_counts": {"facet_fields": {
                "authFullNamePersonIDIDHal_fs": values}}}


HAL_ONE_PERSON = hal_facets(("1017568", "dominique-cardon", 226),
                            ("838230", "", 1), ("0", "", 4))


# === Comparaison de noms ===

class TestNameMatching:
    @pytest.mark.parametrize("first, last", [
        ("Dominique", "Cardon"), ("D.", "Cardon"), ("DOMINIQUE", "CARDON"),
        ("dominique", "cardon"),
    ])
    def test_accepted_variants(self, first, last):
        assert lookups.names_match(NAME, first, last)

    def test_inverted_full_form_and_accents(self):
        assert lookups.names_match(NAME, full="Cardon, Dominique")
        assert lookups.names_match("Hélène Dupré", "Helene", "DUPRE")
        assert lookups.names_match("Helene Dupre", full="Dupré, Hélène")

    @pytest.mark.parametrize("first, last", [
        ("Dominique", "Cardon-Martin"),   # autre nom de famille
        ("Dominique", ""),                # nom absent
        ("", "Cardon"),                   # prénom absent : invérifiable
        ("Denis", "Cardon"),              # autre prénom
        ("P.", "Cardon"),                 # initiale incompatible
    ])
    def test_rejected_variants(self, first, last):
        assert not lookups.names_match(NAME, first, last)

    def test_compound_given_names_and_particles(self):
        assert lookups.names_match("Jean-Pierre Martin", "J.-P.", "Martin")
        assert lookups.names_match("Jean-Pierre Martin", "Jean Pierre", "Martin")
        assert not lookups.names_match("Jean Martin", "Jean-Pierre", "Martin")
        assert lookups.names_match("Michel de Certeau", "Michel", "Certeau")
        assert lookups.names_match("Jean-Pierre Olivier de Sardan",
                                   "Jean-Pierre", "Olivier de Sardan")

    def test_no_first_last_inversion(self):
        # « Martin Olivier » est une autre personne que « Olivier Martin ».
        assert not lookups.names_match("Olivier Martin", full="Martin Olivier")
        assert not lookups.names_match("Olivier Martin", full="M. Olivier")
        assert lookups.names_match("Olivier Martin", full="O. Martin")
        # Revue OPT-14 : prénoms comparés sur toute la longueur ; des
        # initiales en plus sont refusées comme un prénom complet en plus
        # (« Jean » ≠ « Jean-Pierre », donc « Jean » ≠ « Jean-P. »).
        assert not lookups.names_match("Olivier Martin",
                                       full="Olivier J. F. Martin")
        assert lookups.names_match("Olivier J. F. Martin",
                                   full="O. J. F. Martin")


# === Isidore : faux positifs écartés et comptés ===

class TestIsidoreCreators:
    def test_citing_notices_rejected_and_counted(self, monkeypatch):
        authored = [
            item("Culture numérique"),
            item("À quoi rêvent les algorithmes", first="D.", last="CARDON"),
            item("Teintures", creators=[
                {"creatorType": "author", "firstName": "Dominique",
                 "lastName": "Cardon"},
                {"creatorType": "editor", "name": "CNRS"}]),
        ]
        citing = [
            item("Compte rendu : Dominique Cardon, Culture numérique",
                 first="Peppe", last="Cavallari"),
            item("Séminaire avec Dominique Cardon", first="Dominique",
                 last="Boullier"),
        ]
        install(monkeypatch, isidore_items=authored + citing)
        router = Router([(is_hal, hal_facets()),
                         (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, http_client=router)
        iso = next(r for r in out.report.sources if r.display_name == "Isidore")
        assert iso.item_count == 3
        assert iso.rejected_not_author == 2
        assert iso.counters["received"] == 5
        assert iso.as_dict()["rejected_not_author"] == 2
        assert {it["title"] for it in out} == {it["title"] for it in authored}
        # Identité jamais vérifiable sur Isidore : signalée.
        assert iso.identity_basis == lookups.BASIS_NAME_ONLY
        assert any("Isidore : identité non vérifiée" in c
                   for c in out.report.caveats())

    def test_cap_counts_received_notices(self, monkeypatch):
        # Le plafond d'Isidore porte sur les notices reçues (avant filtre) :
        # la réserve le dit, sans présenter les retenues comme le plafond.
        install(monkeypatch, isidore_items=[
            item("A"), item("B", first="Autre", last="Personne"), item("C"),
            item("D")])
        router = Router([(is_hal, hal_facets()),
                         (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, max_per_source=3,
                                       http_client=router)
        iso = next(r for r in out.report.sources if r.display_name == "Isidore")
        assert iso.limit_reached and iso.item_count == 2
        assert ("Isidore : plafond de 3 notices reçues atteint, 2 retenues, "
                "d'autres travaux peuvent exister") in out.report.caveats()

    def test_isidore_query_is_unchanged(self, monkeypatch):
        _, iso = install(monkeypatch, isidore_items=[item("A")])
        router = Router([(is_hal, hal_facets()),
                         (is_s2_search, {"data": []})])
        lookups.search_by_author(NAME, max_per_source=50, http_client=router)
        assert iso.calls == [(f'"{NAME}"', 50)]


# === Homonymes : candidats rendus, rien moissonné ===

OA_HOMONYMS = {"results": [
    {"id": "https://openalex.org/A1", "display_name": NAME, "works_count": 237,
     "orcid": "https://orcid.org/0000-0002-3892-9611",
     "last_known_institutions": [
         {"display_name": "Institut d'Etudes Politiques de Paris"}],
     "counts_by_year": [{"year": 2012, "works_count": 3},
                        {"year": 2024, "works_count": 5}]},
    {"id": "https://openalex.org/A2", "display_name": NAME, "works_count": 180,
     "orcid": "https://orcid.org/0000-0002-2384-5677",
     "last_known_institutions": [{"display_name": "CNRS"}],
     "counts_by_year": [{"year": 1990, "works_count": 1}]},
    {"id": "https://openalex.org/A3", "display_name": NAME, "works_count": 3},
    {"id": "https://openalex.org/A4", "display_name": "Denis Cardon",
     "works_count": 90},
]}

S2_HOMONYMS = {"data": [
    {"authorId": "111", "name": "D. Cardon", "paperCount": 40,
     "affiliations": ["Sciences Po"]},
    {"authorId": "222", "name": "Dominique Cardon", "paperCount": 25},
    {"authorId": "333", "name": "M. Cardon", "paperCount": 99},
]}


def oa_details(url, params):
    ident = params["filter"].split(":")[1]
    return {"results": [
        {"title": f"Titre célèbre de {ident}", "publication_year": 2015,
         "authorships": [{"author": {"id": f"https://openalex.org/{ident}",
                                     "display_name": NAME}},
                         {"author": {"id": "https://openalex.org/X9",
                                     "display_name": "Co Auteur"}}]}]}


def s2_details(url, params):
    return {"data": [{"title": "Dernier article", "year": 2023,
                      "authors": [{"authorId": "9", "name": "Autre Personne"}]}]}


def homonym_router():
    return Router([
        (is_hal, HAL_ONE_PERSON),
        (lambda u, p: is_openalex_authors(u, p), OA_HOMONYMS),
        (lambda u, p: is_openalex_works(u, p) and p.get("sort")
         == "cited_by_count:desc", oa_details),
        (is_s2_search, S2_HOMONYMS),
        (lambda u, p: is_s2_papers(u, p) and "offset" not in p, s2_details),
    ])


class TestHomonyms:
    def test_name_only_returns_candidates_and_harvests_nothing(
            self, monkeypatch, keys):
        install(monkeypatch, hal_items=[item("HAL A", doi="10.1/h")],
                isidore_items=[item("Isidore A")])
        router = homonym_router()
        out = lookups.search_by_author(NAME, http_client=router)
        report = out.report
        assert out == [] and report.ambiguous
        assert report.overall_status == "ambiguous"
        assert not report.all_failed and not report.production_complete
        # Aucune moisson des profils : ni curseur OpenAlex, ni pages S2.
        assert not [p for u, p in router.urls("openalex.org/works")
                    if "cursor" in p]
        assert not [p for u, p in router.urls("/papers") if "offset" in p]
        by = {r.display_name: r for r in report.sources}
        assert by["OpenAlex"].reason == "ambiguous_author"
        assert by["Semantic Scholar"].reason == "ambiguous_author"
        # Sources non ambiguës : notices obtenues par le nom, écartées.
        assert by["HAL"].reason == "awaiting_identity"
        assert by["HAL"].counters["discarded"] == 1
        assert by["Isidore"].counters["discarded"] == 1
        # Candidats : plausibles seulement (« Denis », « M. » exclus).
        oa = [c for c in report.candidates if c["source"] == "OpenAlex"]
        assert [c["id"] for c in oa] == ["A1", "A2", "A3"]
        a1 = oa[0]
        assert a1["argument"] == {"openalex_author_id": "A1"}
        assert a1["orcid"] == "0000-0002-3892-9611"
        assert a1["affiliation"] == "Institut d'Etudes Politiques de Paris"
        assert a1["period"] == "2012–2024" and a1["works_count"] == 237
        assert a1["sample_titles"] == ["Titre célèbre de A1 (2015)"]
        assert a1["top_coauthors"] == ["Co Auteur"]
        s2 = [c for c in report.candidates if c["source"] == "Semantic Scholar"]
        assert [c["id"] for c in s2] == ["111", "222"]
        assert s2[0]["argument"] == {"s2_author_id": "111"}
        assert s2[0]["sample_titles"] == ["Dernier article (2023)"]
        assert "A3" not in [c["id"] for c in oa if c.get("sample_titles")]

    def test_single_plausible_candidate_is_used_and_named(
            self, monkeypatch, keys):
        install(monkeypatch)
        router = Router([
            (is_hal, hal_facets()),
            (is_openalex_authors, {"results": [
                {"id": "https://openalex.org/A1", "display_name": NAME,
                 "works_count": 50},
                {"id": "https://openalex.org/A3", "display_name": NAME,
                 "works_count": 2},
                {"id": "https://openalex.org/A4", "display_name": "Denis Cardon",
                 "works_count": 90}]}),
            (is_openalex_works, {"results": [oa_work(1), oa_work(2)],
                                 "meta": {"count": 2}}),
            (is_s2_search, {"data": []}),
        ])
        out = lookups.search_by_author(NAME, http_client=router)
        oa = next(r for r in out.report.sources if r.display_name == "OpenAlex")
        assert oa.status == "complete" and oa.item_count == 2
        assert oa.identity["id"] == "A1"
        assert oa.identity_basis == lookups.BASIS_SINGLE
        assert oa.query == "author.id:A1"
        assert any("A3" in w for w in oa.warnings)   # autre profil signalé
        assert "profil retenu : Dominique Cardon (OpenAlex A1" in oa.line()
        assert not out.report.ambiguous

    def test_hal_homonyms_from_facets(self, monkeypatch):
        hal, _ = install(monkeypatch, hal_items=[item("HAL A")])
        router = Router([
            (lambda u, p: is_hal(u, p) and p.get("facet") == "true",
             hal_facets(("746589", "oliviermartin-mosar", 110),
                        ("1058031", "", 37), ("0", "", 72))),
            (lambda u, p: is_hal(u, p) and "stats" in p,
             {"response": {"numFound": 43, "docs": [
                 {"title_s": ["Synthèse"], "producedDateY_i": 2024,
                  "authFullName_s": ["Floriane Heis", NAME]}]},
              "stats": {"stats_fields": {"producedDateY_i": {
                  "min": 2009.0, "max": 2024.0}}}}),
            (is_s2_search, {"data": []}),
        ])
        out = lookups.search_by_author(NAME, http_client=router)
        hal_res = next(r for r in out.report.sources if r.display_name == "HAL")
        assert hal_res.reason == "ambiguous_author" and hal.calls == []
        args = [c["argument"] for c in out.report.candidates]
        assert {"idhal": "oliviermartin-mosar"} in args
        assert {"hal_person_id": "1058031"} in args   # sans idHal
        detailed = next(c for c in out.report.candidates if c["id"] == "1058031")
        assert detailed["period"] == "2009–2024"
        assert detailed["top_coauthors"] == ["Floriane Heis"]

    def test_hal_single_person_excludes_other_homonyms(self, monkeypatch):
        hal, _ = install(monkeypatch, hal_items=[item("HAL A")])
        router = Router([(is_hal, HAL_ONE_PERSON), (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, http_client=router)
        (query, _), = hal.calls
        assert query == (f'(authFullName_s:"{NAME}" AND NOT '
                         f'authIdPerson_i:(838230)) OR authIdPerson_i:1017568')
        hal_res = out.report.sources[0]
        assert hal_res.identity["idhal"] == "dominique-cardon"
        assert any("4 dépôt(s) sans auteur HAL identifié" in w
                   for w in hal_res.warnings)

    def test_identifier_mode_keeps_verified_sources(self, monkeypatch, keys):
        # idHal fourni, S2 ambigu : HAL (identifiant) et OpenAlex (ORCID)
        # gardés ; S2 écarté avec ses candidats ; Isidore (nom seul) écarté.
        hal, _ = install(monkeypatch, hal_items=[item("HAL A", doi="10.1/h")],
                         isidore_items=[item("Isidore A")])
        router = Router([
            (is_openalex_works, {"results": [oa_work(1)], "meta": {"count": 1}}),
            (is_s2_search, S2_HOMONYMS),
            (lambda u, p: is_s2_papers(u, p) and "offset" not in p, s2_details),
        ])
        out = lookups.search_by_author(NAME, idhal="dominique-cardon",
                                       orcid="0000-0002-3892-9611",
                                       http_client=router)
        by = {r.display_name: r for r in out.report.sources}
        assert by["HAL"].status == "complete"
        assert by["OpenAlex"].status == "complete"
        assert by["Semantic Scholar"].reason == "ambiguous_author"
        assert by["Isidore"].reason == "awaiting_identity"
        assert not out.report.ambiguous
        assert out.report.overall_status == "partial"
        assert len(out) == 2
        assert [c["id"] for c in out.report.candidates] == ["111", "222"]
        assert any("Semantic Scholar : homonymes" in c
                   for c in out.report.caveats())


# === Identifiants explicites : aucune recherche par nom ===

class TestIdentifiers:
    def test_orcid_without_name_search(self, monkeypatch, keys):
        hal, _ = install(monkeypatch, hal_items=[item("HAL A")])
        router = Router([
            (is_openalex_works, {"results": [oa_work(1)], "meta": {"count": 1}}),
            (is_s2_search, {"data": [
                {"authorId": "111", "name": "D. Cardon", "paperCount": 40},
                {"authorId": "222", "name": NAME, "paperCount": 25,
                 "externalIds": {"ORCID": "0000-0002-3892-9611"}}]}),
            (is_s2_papers, {"data": [s2_paper(1)]}),
        ])
        out = lookups.search_by_author(NAME, orcid="https://orcid.org/0000-0002-3892-9611",
                                       http_client=router)
        # HAL et OpenAlex par ORCID : ni facette HAL, ni /authors OpenAlex.
        assert hal.calls[0][0] == 'authORCIDIdExt_s:"0000-0002-3892-9611"'
        assert not router.urls("archives-ouvertes")
        assert not router.urls("/authors")
        (_, oa_params), = router.urls("openalex.org/works")
        assert oa_params["filter"] == "author.orcid:0000-0002-3892-9611"
        # S2 : le profil portant l'ORCID est retenu malgré l'homonyme.
        s2 = next(r for r in out.report.sources
                  if r.display_name == "Semantic Scholar")
        assert s2.identity["id"] == "222" and s2.identity_verified
        assert s2.identity_basis == lookups.BASIS_ORCID_MATCH
        assert router.urls("/author/222/papers")

    def test_hal_orcid_without_deposit_is_explained(self, monkeypatch):
        # Constat live du 2026-09-29 : l'ORCID d'un auteur de 226 dépôts
        # n'apparaît dans aucun dépôt HAL ; la réserve propose idhal.
        install(monkeypatch)
        router = Router([(is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, orcid="0000-0002-3892-9611",
                                       http_client=router)
        assert out.report.sources[0].status == "empty"
        assert any("HAL : aucun dépôt ne porte cet ORCID" in c
                   for c in out.report.caveats())

    def test_openalex_and_s2_ids_without_name_search(self, monkeypatch, keys):
        install(monkeypatch)
        router = Router([
            (is_hal, hal_facets()),
            (is_openalex_works, {"results": [oa_work(1)], "meta": {"count": 1}}),
            (is_s2_papers, {"data": [s2_paper(1)]}),
        ])
        out = lookups.search_by_author(
            NAME, openalex_author_id="https://openalex.org/A1, a2",
            s2_author_id="https://www.semanticscholar.org/author/X/222",
            http_client=router)
        assert not router.urls("/authors") and not router.urls("/author/search")
        (_, oa_params), = router.urls("openalex.org/works")
        assert oa_params["filter"] == "author.id:A1|A2"
        assert router.urls("/author/222/papers")
        assert out.report.identifier_mode
        assert out.report.identifiers["openalex_author_id"] == ["A1", "A2"]

    def test_no_name_queries_only_sources_with_identifiers(self, monkeypatch,
                                                           keys):
        hal, iso = install(monkeypatch)
        router = Router([(is_s2_papers, {"data": []})])
        lookups.search_by_author("", hal_person_id="1058031",
                                 s2_author_id="222", http_client=router)
        assert hal.calls == [("authIdPerson_i:1058031", 200)]
        assert iso.calls == []                       # Isidore : nom requis
        assert not router.urls("openalex")           # ni nom ni identifiant

    @pytest.mark.parametrize("kwargs", [
        {"orcid": "0000-12"}, {"openalex_author_id": "W123"},
        {"s2_author_id": "abc"}, {"hal_person_id": "x1"},
    ])
    def test_malformed_identifier_is_refused(self, kwargs):
        with pytest.raises(ValueError):
            lookups.search_by_author(NAME, http_client=Router([]), **kwargs)


# === Pagination ===

class TestPagination:
    def test_openalex_cursor_beyond_one_page(self, monkeypatch, keys):
        monkeypatch.setattr(lookups, "AUTHOR_OPENALEX_PAGE_SIZE", 2)
        pages = {"*": ([oa_work(1), oa_work(2)], "c2"),
                 "c2": ([oa_work(3), oa_work(4)], "c3"),
                 "c3": ([oa_work(5)], None)}

        def works(url, params):
            batch, nxt = pages[params["cursor"]]
            return {"results": batch, "meta": {"count": 5, "next_cursor": nxt}}
        router = Router([(is_openalex_works, works)])
        res = lookups._search_openalex_author(NAME, 10, http_client=router,
                                              author_ids="A1")
        assert res.item_count == 5 and res.pages == 3
        assert res.status == "complete" and res.exhausted
        assert not res.limit_reached
        assert [p["cursor"] for _, p in router.calls] == ["*", "c2", "c3"]

    def test_openalex_cap_is_reported(self, monkeypatch, keys):
        monkeypatch.setattr(lookups, "AUTHOR_OPENALEX_PAGE_SIZE", 2)

        def works(url, params):
            n = {"*": 0, "c2": 2}[params["cursor"]]
            return {"results": [oa_work(n + 1), oa_work(n + 2)],
                    "meta": {"count": 9, "next_cursor": "c2" if n == 0 else "c3"}}
        router = Router([(is_openalex_works, works)])
        res = lookups._search_openalex_author(NAME, 3, http_client=router,
                                              author_ids="A1")
        assert res.item_count == 3 and res.pages == 2
        assert res.limit_reached and res.available == 9
        assert "plafond atteint (9 disponibles), liste incomplète" in res.describe()

    def test_s2_offset_beyond_one_page(self, monkeypatch):
        monkeypatch.setattr(lookups, "AUTHOR_S2_PAGE_SIZE", 2)

        def papers(url, params):
            off = params["offset"]
            data = [s2_paper(off + i) for i in range(min(2, 5 - off))]
            body = {"offset": off, "data": data}
            if off + len(data) < 5:
                body["next"] = off + len(data)
            return body
        router = Router([(is_s2_papers, papers)])
        res = lookups._search_s2_author(NAME, 10, http_client=router,
                                        author_ids="222")
        assert res.item_count == 5 and res.pages == 3
        assert res.status == "complete" and res.exhausted
        assert [p["offset"] for _, p in router.calls] == [0, 2, 4]

    def test_s2_cap_stops_and_is_reported(self, monkeypatch):
        monkeypatch.setattr(lookups, "AUTHOR_S2_PAGE_SIZE", 2)

        def papers(url, params):
            off = params["offset"]
            return {"offset": off, "next": off + params["limit"],
                    "data": [s2_paper(off + i) for i in range(params["limit"])]}
        router = Router([(is_s2_papers, papers)])
        res = lookups._search_s2_author(NAME, 3, http_client=router,
                                        author_ids="222")
        assert res.item_count == 3
        assert [p["limit"] for _, p in router.calls] == [2, 1]
        assert res.limit_reached and not res.exhausted

    def test_page_failure_after_success_is_partial(self, monkeypatch, keys):
        monkeypatch.setattr(lookups, "AUTHOR_OPENALEX_PAGE_SIZE", 1)

        def works(url, params):
            if params["cursor"] == "*":
                return {"results": [oa_work(1)], "meta": {"next_cursor": "c2"}}
            return 503
        res = lookups._search_openalex_author(
            NAME, 10, http_client=Router([(is_openalex_works, works)]),
            author_ids="A1")
        assert res.status == "partial" and res.item_count == 1
        assert res.reason == "http_error"


# === Statuts et isError ===

@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_MOISSON_SEM", None)
    return tmp_path


def use_router(monkeypatch, router):
    import utils.http
    monkeypatch.setattr(utils.http, "get_default_client", lambda: router)


async def _sdk_call(name, args):
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(
            mcp_server.server) as client:
        return await client.call_tool(name, args)


class TestStatusesAndErrors:
    def test_all_sources_failing_is_error(self, monkeypatch, keys, out_dir):
        install(monkeypatch, hal_fail=True, isidore_fail=True)
        router = Router([(is_hal, 503), (is_openalex_authors, 503),
                         (is_s2_search, requests.ConnectionError("x"))])
        out = lookups.search_by_author(NAME, http_client=router)
        assert out.report.all_failed and out.report.overall_status == "error"
        use_router(monkeypatch, router)
        res = anyio.run(_sdk_call, "search_by_author", {"name": NAME})
        assert res.isError is True
        assert "aucune source n'a pu être interrogée" in res.content[0].text
        st = res.structuredContent["by_source_status"]
        assert {v["status"] for v in st.values()} == {"error"}
        assert st["Semantic Scholar"]["reason"] == "network_error"
        assert list(out_dir.glob("corpus_*")) == []

    def test_empty_results_are_not_an_error(self, monkeypatch, keys):
        install(monkeypatch)
        router = Router([(is_hal, hal_facets()),
                         (is_openalex_authors, {"results": []}),
                         (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, http_client=router)
        assert not out.report.all_failed
        assert out.report.overall_status == "ok"
        by = {r.display_name: r.status for r in out.report.sources}
        assert by == {"HAL": "empty", "Isidore": "empty", "OpenAlex": "empty",
                      "Semantic Scholar": "empty"}

    def test_ambiguity_is_a_non_error_return_with_candidates(
            self, monkeypatch, keys, out_dir):
        install(monkeypatch, hal_items=[item("HAL A")])
        use_router(monkeypatch, homonym_router())
        res = anyio.run(_sdk_call, "search_by_author", {"name": NAME})
        assert res.isError is False
        sc = res.structuredContent
        assert sc["harvest_status"] == "ambiguous" and sc["item_count"] == 0
        assert {c["argument"].get("openalex_author_id") for c in sc["candidates"]
                if c["source"] == "OpenAlex"} == {"A1", "A2", "A3"}
        text = res.content[0].text
        assert "Homonymie" in text and 'openalex_author_id="A1"' in text
        assert list(out_dir.glob("corpus_*")) == []

    def test_capped_source_never_promises_completeness(self, monkeypatch,
                                                        out_dir):
        install(monkeypatch, hal_items=[item(f"HAL {i}", doi=f"10.1/{i}")
                                        for i in range(5)])
        router = Router([(is_hal, hal_facets(("0", "", 5))),
                         (is_s2_search, {"data": []})])
        use_router(monkeypatch, router)
        result = asyncio.run(mcp_server._tool_search_by_author(
            {"name": NAME, "max_per_source": 3}))
        blocks, sc = result
        assert sc["production_complete"] is False
        assert sc["by_source_status"]["HAL"]["limit_reached"] is True
        assert sc["by_source_status"]["OpenAlex"]["status"] == "skipped"
        text = blocks[0].text
        assert "Production possiblement incomplète" in text
        assert "HAL : plafond de 3 notices atteint sur 5" in text
        assert "production complète" not in text.lower()
        assert sc["item_count"] == 3 and list(out_dir.glob("corpus_*.json"))

    def test_malformed_identifier_refused_by_tool(self, out_dir):
        res = anyio.run(_sdk_call, "search_by_author",
                        {"name": NAME, "openalex_author_id": "W42"})
        assert res.isError is True and "Paramètre refusé" in res.content[0].text


# === Compatibilité ===

class TestCompatibility:
    def test_historical_arguments_and_list_return(self, monkeypatch, keys):
        hal, iso = install(monkeypatch, hal_items=[item("HAL A")])
        router = Router([(is_openalex_authors, {"results": []}),
                         (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, "dominique-cardon", None, 50,
                                       router)
        assert isinstance(out, list) and len(out) == 1
        assert hal.calls == [('authIdHal_s:"dominique-cardon"', 50)]
        assert iso.calls == [(f'"{NAME}"', 50)]

    def test_schema_keeps_old_arguments_and_adds_identifiers(self):
        tools = asyncio.run(mcp_server.list_tools())
        tool = next(t for t in tools if t.name == "search_by_author")
        props = tool.inputSchema["properties"]
        assert {"name", "idhal", "orcid", "max_per_source",
                "save_corpus"} <= set(props)
        assert {"openalex_author_id", "s2_author_id", "hal_person_id"} <= set(props)
        assert tool.inputSchema["required"] == ["name"]
        assert props["max_per_source"]["default"] == 200
        assert "production complète" not in tool.description

    def test_report_serialises_without_zotero_fields(self, monkeypatch, keys):
        install(monkeypatch, isidore_items=[item("Isidore A")])
        router = Router([(is_hal, hal_facets()),
                         (is_openalex_authors, {"results": []}),
                         (is_s2_search, {"data": []})])
        out = lookups.search_by_author(NAME, http_client=router)
        json.dumps(out.report.as_dict())            # sérialisable
        # Invariant 1 : aucune métadonnée de recherche dans les notices.
        assert all("extra" not in it or "identity" not in it["extra"]
                   for it in out)
        assert set(out[0]) == set(item("x"))
