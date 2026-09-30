# -*- coding: utf-8 -*-
"""
Intégration du lot B2, première passe (OPT-07 × OPT-06 bis). Aucun accès
réseau.

Couvre les raccords entre les deux tâches, menées en parallèle :

- le moteur (``engine.orchestrator.deduplicate_items``) déduplique par
  ``deduplicate_with_report`` et reçoit ``(notices, rapport)`` : la
  corroboration d'auteur saisi autrement (OPT-06 bis) et la clé HAL dérivée
  d'une URL de portail du CCSD passent par le moteur commun (OPT-07) ;
- demande croisée d'OPT-06 bis : ``utils.names.split_display_name`` coupe la
  forme « Nom, Prénom » (une seule virgule) sur la virgule. Avant, CORE
  (dépôts OAI) et une partie des auteurs OpenAlex donnaient des notices au
  nom inversé : nom « Jean-Baptiste », prénom « Alayrac, » (420 occurrences
  dans des notices OpenAlex de ``output/``) ; une virgule finale restait
  collée au nom (« Porquier, », Semantic Scholar). Les chaînes qui
  ressemblent à une liste d'auteurs ou à une institution gardent
  l'heuristique générale.
"""

import copy
import json
from pathlib import Path

import pytest

from engine import orchestrator
from mappers.core_mapper import map_core_item
from mappers.openalex_mapper import map_openalex_item
from mappers.semantic_scholar_mapper import map_s2_item
from utils.deduplication import (
    deduplicate_with_report, first_author_key, parse_evidence,
)
from utils.names import _split_given_family, split_display_name

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "dedup_pairs.json"
PAIRS = {p["id"]: p for group in ("positives", "negatives")
         for p in json.loads(FIXTURE.read_text(encoding="utf-8"))[group]}

TITLE = "Les formules du discours politique contemporain en campagne électorale"


# === split_display_name : forme « Nom, Prénom » ===

class TestSplitInverted:
    @pytest.mark.parametrize("full, expected", [
        # Noms réels relevés dans output/ (OpenAlex), mal découpés avant.
        ("Alayrac, Jean-Baptiste", ("Jean-Baptiste", "Alayrac")),
        ("Freitas, Tiago Carvalho", ("Tiago Carvalho", "Freitas")),
        ("Costa Neto, Alvaro", ("Alvaro", "Costa Neto")),
        ("Pereira, Maria João Varanda", ("Maria João Varanda", "Pereira")),
        ("Chung, Hyung Won", ("Hyung Won", "Chung")),
        # Forme CORE (dépôts OAI, dc:creator), reproduite par le banc.
        ("Bernard, Anne", ("Anne", "Bernard")),
        ("Marques dos Santos, Clecio", ("Clecio", "Marques dos Santos")),
        ("CHOPPIN DE JANVRY, Thibaud", ("Thibaud", "CHOPPIN DE JANVRY")),
        # « y » fait partie de certains noms espagnols : pas un séparateur.
        ("Ortega y Gasset, José", ("José", "Ortega y Gasset")),
        # Espaces superflus.
        ("  Muller ,   Élise  ", ("Élise", "Muller")),
    ])
    def test_last_first(self, full, expected):
        assert split_display_name(full) == expected

    @pytest.mark.parametrize("full, expected", [
        ("R. Porquier,", ("R.", "Porquier")),
        (", Maria Glushkova", ("Maria", "Glushkova")),
        ("Foucault,", ("", "Foucault")),
        (",", ("", "")),
    ])
    def test_isolated_comma_ignored(self, full, expected):
        assert split_display_name(full) == expected

    def test_generational_suffix_goes_with_given_name(self):
        assert split_display_name("John Smith, Jr.") == ("John, Jr.", "Smith")
        assert split_display_name("Smith, Jr") == ("Jr", "Smith")
        assert split_display_name("Martin Luther King, III") == (
            "Martin Luther, III", "King")

    @pytest.mark.parametrize("full", [
        # Chaînes réelles de output/ : listes d'auteurs ou institutions dans
        # un seul champ. Comportement antérieur conservé (heuristique
        # générale), la forme « Nom, Prénom » n'y est pas supposée.
        "M.M. Rahman, K. Kadirgama and Azma Salwani Ab Aziz",
        "Cheng-Yu Li, Fang-Jung Shiou, and Geo-Ry Tang",
        "Chief Executive Officer (CEO), GM Holdings Limited (Deshbandhu Group), "
        "Dhaka, Bangladesh",
        "Parker, Stephanie J [Idaho National Laboratory]; Anderson, Matthew "
        "William [Idaho National Laboratory]",
        "Dupont & Durand, Jean",
        "Smith, John, Jr.",
    ])
    def test_lists_and_institutions_unchanged(self, full):
        assert split_display_name(full) == _split_given_family(full.strip())

    @pytest.mark.parametrize("full, expected", [
        ("Patrick Charaudeau", ("Patrick", "Charaudeau")),
        ("Foucault", ("", "Foucault")),
        ("Jean Pierre Dupont", ("Jean Pierre", "Dupont")),
        ("Pierre van der Berg", ("Pierre", "van der Berg")),
        ("A Lakel", ("A", "Lakel")),
        ("", ("", "")),
        (None, ("", "")),
    ])
    def test_given_family_unchanged(self, full, expected):
        assert split_display_name(full) == expected


# === Mappers ===

class TestMappers:
    def test_core_last_first(self):
        item = map_core_item({"title": TITLE, "yearPublished": 2015,
                              "authors": [{"name": "Bernard, Anne"},
                                          {"name": "Muller, Élise"}]})
        assert [(c["lastName"], c["firstName"]) for c in item["creators"]] == [
            ("Bernard", "Anne"), ("Muller", "Élise")]

    def test_core_given_family_unchanged(self):
        item = map_core_item({"title": TITLE,
                              "authors": [{"name": "Chen Li"}, "Maria Garcia"]})
        assert [(c["lastName"], c["firstName"]) for c in item["creators"]] == [
            ("Li", "Chen"), ("Garcia", "Maria")]

    def test_openalex_last_first(self):
        item = map_openalex_item({
            "title": TITLE, "type": "article",
            "authorships": [
                {"author": {"display_name": "Alayrac, Jean-Baptiste"}},
                {"author": {"display_name": "Karen Simonyan"}},
            ]})
        assert [(c["lastName"], c["firstName"]) for c in item["creators"]] == [
            ("Alayrac", "Jean-Baptiste"), ("Simonyan", "Karen")]


# === Déduplication par le moteur commun ===

def _core_s2_pair():
    core = map_core_item({"title": TITLE, "yearPublished": 2015,
                          "id": 424242,
                          "authors": [{"name": "Bernard, Anne"}]})
    s2 = map_s2_item({"title": TITLE, "year": 2015, "paperId": "abc123",
                      "authors": [{"name": "Anne Bernard"}]})
    core["extra"] = "Sources: CORE\n" + core.get("extra", "")
    s2["extra"] = "Sources: Semantic Scholar\n" + s2.get("extra", "")
    return core, s2


class TestEngineDedup:
    def test_engine_uses_report_variant(self):
        pair = PAIRS["P52"]
        out, report = orchestrator.deduplicate_items(
            [copy.deepcopy(pair["a"]), copy.deepcopy(pair["b"])])
        assert report is not None
        assert report["input"] == 2 and report["output"] == 1
        assert report["fuzzy_merges"] == 1
        ev = parse_evidence(out[0])
        assert any("(nom complet)" in m for m in ev["fuzzy"])

    def test_engine_matches_direct_call(self):
        items = [copy.deepcopy(PAIRS[k][s]) for k in ("P51", "P52", "P53",
                                                     "P54")
                 for s in ("a", "b")]
        via_engine, rep_engine = orchestrator.deduplicate_items(
            copy.deepcopy(items))
        direct, rep_direct = deduplicate_with_report(copy.deepcopy(items))
        assert via_engine == direct and rep_engine == rep_direct
        assert len(via_engine) == 4

    def test_ccsd_portal_key_through_engine(self):
        pair = PAIRS["P51"]
        out, report = orchestrator.deduplicate_items(
            [copy.deepcopy(pair["a"]), copy.deepcopy(pair["b"])])
        assert len(out) == 1 and report["strong_merges"] == 1
        assert any(k.startswith("hal:") for k in parse_evidence(out[0])["keys"])

    def test_core_name_now_matches_exactly(self):
        # Avant le correctif, CORE donnait nom « Anne », prénom « Bernard, » :
        # la fusion ne tenait qu'à la tolérance d'OPT-06 bis
        # (« anne~bernard (nom complet) »). Désormais le premier auteur est
        # le même des deux côtés.
        core, s2 = _core_s2_pair()
        assert first_author_key(core) == first_author_key(s2) == "bernard"
        out, report = orchestrator.deduplicate_items([core, s2])
        assert len(out) == 1 and report["fuzzy_merges"] == 1
        motifs = parse_evidence(out[0])["fuzzy"]
        assert motifs and all("~" not in m for m in motifs)
        assert any(m.endswith("auteur bernard") for m in motifs)

    def test_core_name_short_title_regime(self):
        # Régime des titres courts : le nom doit être saisi à l'identique (la
        # tolérance d'OPT-06 bis ne s'y applique pas). Le correctif du
        # découpage rend la fusion possible sans rien relâcher.
        core, s2 = _core_s2_pair()
        core["title"] = s2["title"] = "Dialogisme et polyphonie"
        out, _ = orchestrator.deduplicate_items([core, s2])
        assert len(out) == 1

    def test_distinct_people_same_surname_still_separate_when_titles_differ(self):
        core, s2 = _core_s2_pair()
        s2["title"] = "Une tout autre étude des pratiques numériques des lycéens"
        out, _ = orchestrator.deduplicate_items([core, s2])
        assert len(out) == 2
