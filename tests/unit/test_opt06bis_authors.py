"""
OPT-06 bis : corroboration par le premier auteur tolérante à l'inversion
nom / prénom et aux noms composés (``utils.deduplication.names_equivalent``).

Cas réels (moisson live du 2026-09-28) : « CHOPPIN DE JANVRY, Thibaud » /
« DE JANVRY, Thibaud Choppin » (même article LIMES exposé deux fois) ;
« Marques dos Santos, Clecio » / « Santos, Clecio Marques dos » (mémoire
DUMAS). Règle : jetons de nom et de prénom (particules et jetons de moins de
trois lettres écartés) ; jetons de l'un tous dans l'autre, au moins deux
communs, et un jeton de nom commun avec un jeton de prénom commun, ou
l'inversion exacte des deux champs ; revue : aucun jeton qui change de champ
n'est lié des deux côtés, par un trait d'union, à des jetons différents
(« Martin-Paul, Jean » / « Martin, Jean-Paul »). Rien d'autre n'est
relâché : seuil de titre, année, types, titres génériques et régime des
titres courts inchangés.
"""

import copy
import json
import random
import sys
import time
from pathlib import Path

import pytest

from utils.deduplication import (
    _Features, _fuzzy_edges, _match, deduplicate, deduplicate_with_report,
    name_profile, names_equivalent, parse_evidence,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "dedup_pairs.json"
PAIRS = {p["id"]: p for group in ("positives", "negatives")
         for p in json.loads(FIXTURE.read_text(encoding="utf-8"))[group]}

T = "Les formules du discours politique contemporain en campagne électorale"


def au(last, first=None, kind="author"):
    c = {"creatorType": kind, "lastName": last}
    if first is not None:
        c["firstName"] = first
    return c


def rec(source, creators, title=T, date="2015", **kw):
    item = {"itemType": kw.pop("itemType", "journalArticle"), "title": title,
            "creators": creators, "date": date,
            "extra": f"Sources: {source}"}
    item.update(kw)
    return item


def prof(last, first):
    return name_profile(au(last, first))


def evidence(record):
    return parse_evidence(record) or {"count": 1, "fuzzy": []}


# === Profils de nom ===

class TestNameProfile:
    def test_tokens_particles_and_short_tokens(self):
        key, surname, given, bound = prof("CHOPPIN DE JANVRY", "Thibaud")
        assert key == "choppindejanvry"
        assert surname == {"choppin", "janvry"} and given == {"thibaud"}
        assert bound == frozenset()
        _k, surname, given, _b = prof("Santos", "Clecio Marques dos")
        assert surname == {"santos"} and given == {"clecio", "marques"}

    def test_hyphen_and_transliteration(self):
        _k, surname, given, bound = prof("Kerbrat-Orecchioni", "Cathérine")
        assert surname == {"kerbrat", "orecchioni"} and given == {"catherine"}
        assert bound == {frozenset({"kerbrat", "orecchioni"})}

    def test_compounds_of_both_fields(self):
        """Revue OPT-06 bis : les jetons liés par un trait d'union (ou tout
        signe sans espace) forment un composé, dans chaque champ ; une
        particule ou une initiale n'en fait pas partie."""
        _k, _s, _g, bound = prof("Martin-Paul", "Jean-Pierre")
        assert bound == {frozenset({"martin", "paul"}),
                         frozenset({"jean", "pierre"})}
        assert prof("Martin", "Jean Paul")[3] == frozenset()
        assert prof("Martin-de-Janvry", "J.-Paul Anne")[3] == {
            frozenset({"martin", "janvry"})}

    @pytest.mark.parametrize("creator", [
        au("Flynn", "S"),                       # initiale
        au("Fiil-Flynn", "SM"),                 # initiales accolées
        au("Santos", "C. M."),
        au("Dupont"),                           # sans prénom
        {"creatorType": "author", "name": "Clecio Marques dos Santos"},
        au("Li", "Wei"),                        # nom de deux lettres
        # Plus de NAME_MAX_TOKENS jetons : liste d'auteurs dans un champ.
        au("Alpha Beta Gamma Delta", "Epsilon Zeta Theta"),
    ])
    def test_no_profile(self, creator):
        assert name_profile(creator) is None


# === Équivalence de deux noms ===

class TestNamesEquivalent:
    @pytest.mark.parametrize("a,b", [
        (("CHOPPIN DE JANVRY", "Thibaud"), ("DE JANVRY", "Thibaud Choppin")),
        (("Marques dos Santos", "Clecio"), ("Santos", "Clecio Marques dos")),
        (("Marques dos Santos", "Clecio"), ("Santos", "Clecio")),
        (("Kerbrat-Orecchioni", "Catherine"), ("Orecchioni", "Catherine Kerbrat")),
        (("Torres Quiroga", "Jessica"), ("Quiroga", "Jessica Torres")),
        (("Dupont", "Jean"), ("Jean", "Dupont")),                 # inversion exacte
        (("Bernard", "Anne"), ("Anne", "Bernard,")),              # « Nom, Prénom » mal découpé
        (("García Márquez", "Gabriel"), ("Garcia Marquez", "Gabriel")),
    ])
    def test_same_person(self, a, b):
        pa, pb = prof(*a), prof(*b)
        assert names_equivalent(pa, pb) and names_equivalent(pb, pa)

    @pytest.mark.parametrize("a,b", [
        # Membres d'une même famille : nom composé commun, prénoms différents.
        (("Marques dos Santos", "Clecio"), ("Santos", "Maria Marques dos")),
        (("Dupont Martin", "Jean"), ("Martin", "Marie Dupont")),
        # Cousins partageant deux noms.
        (("Da Silva Costa", "João"), ("Costa", "Joana Silva")),
        # Prénom de l'un pris pour le nom de l'autre.
        (("Martin", "Paul"), ("Paul", "Jacques Martin")),
        # Un seul jeton commun.
        (("Boulic-Bouadjio", "Anne"), ("Boulic", "Marc")),
        (("Santos", "Carlos"), ("Marques dos Santos", "Clecio")),
        # Jetons propres de part et d'autre.
        (("Garcia Marquez", "Gabriel"), ("Marquez", "Gabriel Jose")),
        # Nom composé commun, prénom absent d'un côté.
        (("Marques dos Santos", "Clecio"), ("Santos", "Marques")),
        # Revue OPT-06 bis : composés à trait d'union contradictoires
        # (Jean Martin-Paul et Jean-Paul Martin).
        (("Martin-Paul", "Jean"), ("Martin", "Jean-Paul")),
        (("Pierre-Martin", "Jean"), ("Martin", "Jean-Pierre")),
        (("Dupont-Marie", "Anne"), ("Dupont", "Anne-Marie")),
    ])
    def test_different_people(self, a, b):
        pa, pb = prof(*a), prof(*b)
        assert not names_equivalent(pa, pb) and not names_equivalent(pb, pa)

    @pytest.mark.parametrize("a,b", [
        # Trait d'union d'un seul côté : perdu par l'autre source.
        (("Martin-Paul", "Jean"), ("Paul", "Jean Martin")),
        (("Boulic-Bouadjio", "Anne"), ("Bouadjio", "Anne Boulic")),
        (("Martin-Dupont", "Jean-Paul"), ("Dupont", "Jean-Paul Martin")),
        # Même composé des deux côtés, champs exactement inversés.
        (("Martin", "Jean-Paul"), ("Jean-Paul", "Martin")),
    ])
    def test_compound_on_one_side_or_preserved(self, a, b):
        """Revue OPT-06 bis : le resserrement ne refuse que les composés
        contradictoires ; un composé présent d'un seul côté, ou le même
        composé des deux côtés, reste accepté."""
        pa, pb = prof(*a), prof(*b)
        assert names_equivalent(pa, pb) and names_equivalent(pb, pa)

    def test_space_separated_variant_is_a_documented_limit(self):
        """Limite documentée (docstring de names_equivalent) : sans trait
        d'union, « Martin Paul, Jean » / « Martin, Jean Paul » a exactement
        la structure du cas réel Janvry (P52) ; aucune règle sur les seuls
        jetons ne sépare les deux. Si ce test échoue, P52 doit rester
        fusionnée (test_real_janvry_pair_merges)."""
        janvry = (prof("CHOPPIN DE JANVRY", "Thibaud"),
                  prof("DE JANVRY", "Thibaud Choppin"))
        spaced = (prof("Martin Paul", "Jean"), prof("Martin", "Jean Paul"))
        shape = [(len(p[1]), len(p[2]), len(p[1] & q[2]), len(p[2] & q[1]))
                 for p, q in (janvry, spaced)]
        assert shape[0] == shape[1]
        assert names_equivalent(*janvry) and names_equivalent(*spaced)

    def test_initials_never_corroborate(self):
        assert not names_equivalent(prof("Fiil-Flynn", "SM"), prof("Flynn", "S"))
        assert not names_equivalent(None, prof("Flynn", "Sean"))


# === Déduplication ===

class TestDeduplication:
    def test_real_janvry_pair_merges(self):
        """Échoue avant OPT-06 bis (cas réel P52)."""
        pair = PAIRS["P52"]
        out = deduplicate([copy.deepcopy(pair["a"]), copy.deepcopy(pair["b"])])
        assert len(out) == 1
        motif = evidence(out[0])["fuzzy"][0]
        assert "auteur choppindejanvry~janvry (nom complet)" in motif

    def test_real_dumas_pair_merges_without_the_hal_key(self):
        """La corroboration seule suffit aussi (identifiants retirés)."""
        pair = PAIRS["P51"]
        a, b = copy.deepcopy(pair["a"]), copy.deepcopy(pair["b"])
        for item in (a, b):
            item.pop("url", None)
            item["extra"] = item["extra"].split("\n")[0]
        assert len(deduplicate([a, b])) == 1

    def test_motif_independent_of_input_order(self):
        pair = PAIRS["P52"]
        one = deduplicate([copy.deepcopy(pair["a"]), copy.deepcopy(pair["b"])])
        two = deduplicate([copy.deepcopy(pair["b"]), copy.deepcopy(pair["a"])])
        assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)

    def test_first_author_against_a_coauthor(self):
        """Comme la règle historique : le premier auteur de l'une figure,
        sous un nom saisi autrement, parmi les auteurs de l'autre."""
        a = rec("HAL", [au("CHOPPIN DE JANVRY", "Thibaud")])
        b = rec("OpenAlex", [au("Rey", "Anne"), au("DE JANVRY", "Thibaud Choppin")])
        assert len(deduplicate([a, b])) == 1

    def test_core_style_inverted_names(self):
        """Mapper CORE : « Bernard, Anne » découpé en nom « Anne », prénom
        « Bernard, » (cas du banc, scénario international)."""
        a = rec("CORE", [au("Anne", "Bernard,"), au("Agnès", "Dubois,")])
        b = rec("Semantic Scholar", [au("Bernard", "Anne"), au("Dubois", "Agnès")])
        assert len(deduplicate([a, b])) == 1

    # --- Rien d'autre n'est relâché ---

    def test_year_gap_still_required(self):
        a = rec("HAL", [au("CHOPPIN DE JANVRY", "Thibaud")], date="2015")
        b = rec("Isidore", [au("DE JANVRY", "Thibaud Choppin")], date="2017")
        assert len(deduplicate([a, b])) == 2

    def test_title_threshold_unchanged(self):
        a = rec("HAL", [au("CHOPPIN DE JANVRY", "Thibaud")])
        b = rec("Isidore", [au("DE JANVRY", "Thibaud Choppin")],
                title="Les formules du discours syndical contemporain en grève")
        assert len(deduplicate([a, b])) == 2

    def test_types_still_compatible_only(self):
        a = rec("HAL", [au("CHOPPIN DE JANVRY", "Thibaud")], itemType="book")
        b = rec("Isidore", [au("DE JANVRY", "Thibaud Choppin")],
                itemType="bookSection")
        assert len(deduplicate([a, b])) == 2

    def test_generic_title_still_blocked(self):
        a = rec("HAL", [au("CHOPPIN DE JANVRY", "Thibaud")], title="Introduction")
        b = rec("Isidore", [au("DE JANVRY", "Thibaud Choppin")], title="Introduction")
        assert len(deduplicate([a, b])) == 2

    def test_short_title_requires_the_name_as_written(self):
        """Titre court (moins de quatre mots significatifs) : même premier
        auteur exigé, saisi à l'identique (régime inchangé)."""
        short = "Dialogisme et polyphonie"
        a = rec("HAL", [au("CHOPPIN DE JANVRY", "Thibaud")], title=short)
        b = rec("Isidore", [au("DE JANVRY", "Thibaud Choppin")], title=short)
        assert len(deduplicate([a, b])) == 2
        c = rec("OpenAlex", [au("Choppin de Janvry", "T.")], title=short)
        assert len(deduplicate([a, c])) == 1

    def test_contradictory_dois_still_apart(self):
        a = rec("CrossRef", [au("CHOPPIN DE JANVRY", "Thibaud")], DOI="10.1234/a")
        b = rec("OpenAlex", [au("DE JANVRY", "Thibaud Choppin")], DOI="10.1234/b")
        assert len(deduplicate([a, b])) == 2

    @pytest.mark.parametrize("pid", ["N57", "N58", "N59", "N60", "N61", "N62",
                                     "N63", "N64", "N65", "N66"])
    def test_new_traps_never_merged(self, pid):
        pair = PAIRS[pid]
        for fuzzy in (True, False):
            out = deduplicate([copy.deepcopy(pair["a"]), copy.deepcopy(pair["b"])],
                              fuzzy=fuzzy)
            assert len(out) == 2, pair["note"]

    def test_permutations_and_idempotence(self):
        items = [copy.deepcopy(PAIRS[p][s]) for p in
                 ("P51", "P52", "P53", "P54", "N57", "N59", "N61", "N62")
                 for s in ("a", "b")]
        ref = json.dumps(deduplicate(copy.deepcopy(items)), sort_keys=True)
        for seed in range(8):
            shuffled = copy.deepcopy(items)
            random.Random(seed).shuffle(shuffled)
            assert json.dumps(deduplicate(shuffled), sort_keys=True) == ref
        once = json.loads(ref)
        assert json.dumps(deduplicate(copy.deepcopy(once)), sort_keys=True) == ref
        assert len(once) == 4 + 8


# === Paires candidates et coût ===

_GIVEN = ["Thibaud", "Clecio", "Maria", "Jean", "Anne", "Paul", "Marc", "João"]
_SURNAMES = ["Janvry", "Choppin", "Santos", "Marques", "Dupont", "Martin",
             "Costa", "Silva", "Boulic"]


def _random_name(rng, hyphens=False):
    """Nom et prénom tirés parmi des listes qui se recouvrent (prénoms
    composés, noms composés, inversions). ``hyphens`` : les jetons d'un même
    champ sont liés au hasard par un trait d'union ou un espace (composés
    concordants ou contradictoires) ; sans lui, les tirages sont ceux de la
    mesure d'origine."""
    def join(tokens):
        sep = rng.choice((" ", "-")) if hyphens and len(tokens) > 1 else " "
        return sep.join(tokens)
    surname = join(rng.sample(_SURNAMES, rng.randint(1, 2)))
    given = join(rng.sample(_GIVEN, rng.randint(1, 2)))
    pool = [(surname, given), (given, surname)]
    words = (surname + " " + given).replace("-", " ").split()
    rng.shuffle(words)
    cut = rng.randint(1, len(words) - 1)
    pool.append((join(words[:cut]), join(words[cut:])))
    return rng.choice(pool)


def _without_compounds(profile):
    return None if profile is None else profile[:3] + (frozenset(),)


@pytest.mark.parametrize("hyphens", [False, True])
def test_candidate_pairs_are_those_the_corroboration_can_accept(hyphens):
    """Les paires candidates (même nom normalisé, ou profil équivalent)
    donnent exactement les appariements d'une comparaison de toutes les
    paires, noms inversés et composés compris ; avec des traits d'union,
    le refus des composés contradictoires est exercé."""
    rng = random.Random(7)
    base = ["Les marqueurs discursifs du français parlé contemporain",
            "Acquisition du lexique verbal chez l'enfant bilingue en contexte",
            "Corpus oraux et annotation prosodique automatisée des dialogues"]
    items = []
    for _k in range(120):
        creators = [au(*_random_name(rng, hyphens))
                    for _ in range(rng.randint(1, 3))]
        items.append(rec("HAL", creators,
                         title=rng.choice(base) + rng.choice(["", " !", "s"]),
                         date=str(rng.randint(2014, 2016))))
    feats = [_Features(r) for r in items]
    fast = _fuzzy_edges(feats)
    brute = {}
    for i in range(len(feats)):
        for j in range(i + 1, len(feats)):
            m = _match(feats[i], feats[j])
            if m:
                brute[(i, j)] = m
    assert fast == brute
    assert any("(nom complet)" in m[4] for m in brute.values())
    if hyphens:
        refused = [(f.author_profile, o) for f in feats for g in feats
                   for o in g.profiles
                   if names_equivalent(_without_compounds(f.author_profile),
                                       _without_compounds(o))
                   and not names_equivalent(f.author_profile, o)]
        assert refused


_COST_WORDS = ("discours analyse corpus presse politique énonciation "
               "polyphonie dialogisme argumentation médias numérique récit "
               "point vue modalité lexique syntaxe pragmatique interaction "
               "genre").split()
_COST_NOTICES = 2500
# Comparaisons (appels de _match) mesurées le 2026-09-28 sur cette charge,
# identiques sous Python 3.10, 3.11 et 3.14 et pour toute graine de hachage :
# 161 557 avec la règle complète, 120 933 avec la seule règle d'OPT-06.
# Plafonds à + 15 % : neutraliser le filtre d'équivalence des paires
# candidates en donne 221 583.
_COST_MAX_CALLS = 185_000
_COST_MAX_EXTRA_CALLS = 47_000


def _cost_workload():
    rng = random.Random(11)
    items = []
    for _ in range(_COST_NOTICES):
        title = " ".join(rng.choice(_COST_WORDS)
                         for _ in range(rng.randint(6, 12)))
        creators = [au(*_random_name(rng)) for _ in range(rng.randint(1, 3))]
        items.append(rec("OpenAlex", creators, title=title,
                         date=str(rng.randint(2023, 2025))))
    return items


def _tracing() -> bool:
    """Vrai sous un traceur (couverture de la CI : cœur C ou Python avant
    Python 3.14, sys.monitoring ensuite)."""
    if sys.gettrace() is not None:
        return True
    monitoring = getattr(sys, "monitoring", None)
    return bool(monitoring and monitoring.get_tool(monitoring.COVERAGE_ID))


def test_compound_and_inverted_names_stay_fast(monkeypatch):
    """Cas défavorable à l'index des jetons : 2 500 notices dont les noms
    sont tirés d'un petit lot de jetons, composés, redistribués et inversés
    (beaucoup de profils équivalents).

    Aucune durée absolue serrée : sous la couverture de la CI (traceur C,
    Python 3.10 à 3.12), la même charge prend près de trois fois plus de
    temps. Trois contrôles indépendants de la machine et du traceur :

    - comparaisons (déterministes) sous les plafonds mesurés ;
    - durée au plus quatre fois celle de la seule règle d'OPT-06 sur les
      mêmes notices, dans le même processus (mesuré : 1,5 à 2,0 fois sous
      Python 3.10, 3.11 et 3.14, avec ou sans couverture) ;
    - plafond absolu très large contre une explosion commune aux deux
      passes (profils, index des sous-ensembles).
    """
    import utils.deduplication as dedup

    calls = [0]
    real_match = dedup._match

    def counting_match(a, b, approximate=True, rules=None):
        calls[0] += 1
        return real_match(a, b, approximate, rules)

    monkeypatch.setattr(dedup, "_match", counting_match)

    def run():
        items = _cost_workload()
        calls[0] = 0
        t0 = time.perf_counter()
        out, report = deduplicate_with_report(items)
        elapsed = time.perf_counter() - t0
        assert report["output"] == len(out) <= len(items)
        return elapsed, calls[0]

    full, full_calls = run()
    monkeypatch.setattr(dedup, "names_equivalent", lambda p, q: False)
    alone, alone_calls = run()

    assert full_calls <= _COST_MAX_CALLS
    assert full_calls - alone_calls <= _COST_MAX_EXTRA_CALLS
    assert full <= 4 * alone, (full, alone)
    assert full + alone < (120 if _tracing() else 30)
