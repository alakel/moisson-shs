"""
OPT-06 : jeu témoin ``tests/fixtures/dedup_pairs.json`` (54 paires
positives, 64 négatives) construit à partir de cas réels de ``output/`` et de
pièges construits (compte rendu / article, prépublication / version finale,
homonymes, rééditions, titres génériques, deux DOI, années éloignées…). La
revue d'OPT-06 a ajouté N51-N56 (chapitre / ouvrage de même titre court,
versions distinctes d'un même dépôt HAL, republication, tomes, URL de page de
revue). OPT-06 bis a ajouté P51-P54 (portail DUMAS, noms inversés ou
composés, dont deux paires réelles d'une moisson live) et N57-N64 (pièges de
la corroboration par les jetons du nom : homonymes partageant un nom
composé, frères et sœurs, prénom pris pour nom, auteurs inversés mais titres
différents).

Critères de sortie : 0 fusion erronée sur les négatives ; au moins 48 des 50
positives d'origine fusionnées et toutes les positives ajoutées, les manquées
étant exactement celles que la fixture documente (``expected_missed``) ;
aucune variation sur 10 permutations à graine fixe ; idempotence.
"""

import copy
import json
import random
from pathlib import Path

import pytest

from utils.deduplication import deduplicate

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "dedup_pairs.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
POSITIVES = DATA["positives"]
NEGATIVES = DATA["negatives"]


def _pair(p):
    return [copy.deepcopy(p["a"]), copy.deepcopy(p["b"])]


def _dump(items):
    return json.dumps(items, ensure_ascii=False, sort_keys=True)


def test_fixture_shape():
    assert len(POSITIVES) >= 54 and len(NEGATIVES) >= 66
    ids = [p["id"] for p in POSITIVES + NEGATIVES]
    assert len(ids) == len(set(ids))
    real = [p for p in POSITIVES + NEGATIVES if p["origin"].startswith("réel")]
    # Majorité de cas réels (notices réduites de output/, ou d'une moisson
    # live de vérification pour P51 et P52).
    assert len(real) >= 60
    traps = {p.get("trap") for p in NEGATIVES}
    for trap in ("compte_rendu", "prepublication_version_finale", "homonymes",
                 "reeditions", "titre_generique", "deux_doi_meme_titre",
                 "meme_titre_annees_eloignees", "livre_chapitre", "tomes",
                 "republication", "url_generique",
                 # OPT-06 bis : pièges de la corroboration par les jetons du nom.
                 "homonymes_nom_compose", "freres_soeurs_meme_nom",
                 "prenom_pris_pour_nom", "auteurs_inverses_titres_differents",
                 # Revue OPT-06 bis : « Martin-Paul, Jean » / « Martin, Jean-Paul ».
                 "composes_trait_union_contradictoires"):
        assert trap in traps
    opt06bis_traps = [p for p in NEGATIVES if "OPT-06 bis" in p["note"]]
    assert len(opt06bis_traps) >= 6


@pytest.mark.parametrize("pair", NEGATIVES, ids=[p["id"] for p in NEGATIVES])
def test_negative_never_merged(pair):
    assert len(deduplicate(_pair(pair))) == 2, pair["note"]


def test_positives_rate_and_documented_misses():
    missed = {p["id"] for p in POSITIVES if len(deduplicate(_pair(p))) != 1}
    expected = {p["id"] for p in POSITIVES if p.get("expected_missed")}
    assert len(POSITIVES[:50]) - len(missed & {p["id"] for p in POSITIVES[:50]}) >= 48
    assert missed == expected
    # OPT-06 bis : les positives ajoutées (portail DUMAS, noms inversés ou
    # composés) sont toutes fusionnées.
    assert not missed & {"P51", "P52", "P53", "P54"}


@pytest.mark.parametrize("pair", [p for p in POSITIVES if not p.get("expected_missed")],
                         ids=[p["id"] for p in POSITIVES if not p.get("expected_missed")])
def test_positive_merged_with_evidence(pair):
    out = deduplicate(_pair(pair))
    assert len(out) == 1, pair["note"]
    assert "Dedup evidence: 2 notices" in out[0]["extra"]


def test_whole_fixture_no_cross_pair_merge():
    positives = [x for p in POSITIVES for x in _pair(p)]
    negatives = [x for p in NEGATIVES for x in _pair(p)]
    n_missed = sum(1 for p in POSITIVES if p.get("expected_missed"))
    assert len(deduplicate(positives)) == len(POSITIVES) + n_missed
    assert len(deduplicate(negatives)) == 2 * len(NEGATIVES)


def test_ten_permutations_identical():
    items = [x for p in POSITIVES + NEGATIVES for x in _pair(p)]
    ref = _dump(deduplicate(copy.deepcopy(items)))
    for seed in range(10):
        shuffled = copy.deepcopy(items)
        random.Random(seed).shuffle(shuffled)
        assert _dump(deduplicate(shuffled)) == ref, f"graine {seed}"


def test_idempotent_on_fixture():
    items = [x for p in POSITIVES + NEGATIVES for x in _pair(p)]
    once = deduplicate(items)
    assert _dump(deduplicate(copy.deepcopy(once))) == _dump(once)


def test_fuzzy_disabled_keeps_negatives_apart():
    for pair in NEGATIVES:
        assert len(deduplicate(_pair(pair), fuzzy=False)) == 2, pair["id"]
