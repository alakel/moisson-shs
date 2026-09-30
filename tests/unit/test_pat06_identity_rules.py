"""
Règles d'identité du sprint patch (retour d'expérience du 2026-09-30),
chacune derrière son drapeau, désactivée par défaut :

- PAT-06 (B2) : co-diffusion d'un même article sur deux plateformes (deux
  DOI déclarés équivalents, DOI Cairn conservé : décision DP-2) ;
- PAT-07 (B4) : chapitres Google Books (numéro de chapitre ignoré, type
  « book » du lien Google Books non bloquant entre notices Scholar) ;
- PAT-08 (B3) : DOI de concept et de version d'un même dépôt Zenodo.

Notices réelles : ``tests/fixtures/patch_2026-09-30.json``.
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path

import pytest

from utils.deduplication import (
    ALIAS_KEY, CODIFFUSION_ENV, RULES_VERSION, SCHOLAR_CHAPTERS_ENV,
    ZENODO_VERSIONS_ENV, _keep_doi, dedup_rules, deduplicate,
    deduplicate_with_report, parse_evidence,
)

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                      / "patch_2026-09-30.json").read_text(encoding="utf-8"))

ALL_ON = dedup_rules(codiffusion=True, zenodo_versions=True,
                     scholar_chapters=True)
ALL_OFF = dedup_rules(codiffusion=False, zenodo_versions=False,
                      scholar_chapters=False)


@pytest.fixture(autouse=True)
def _flags_unset(monkeypatch):
    for env in (CODIFFUSION_ENV, ZENODO_VERSIONS_ENV, SCHOLAR_CHAPTERS_ENV):
        monkeypatch.delenv(env, raising=False)


def notices(name):
    return copy.deepcopy(FIXTURE[name])


def extra(item, key):
    return [l.split(": ", 1)[1] for l in (item.get("extra") or "").split("\n")
            if l.startswith(key + ": ")]


def dedup(items, rules):
    out, report = deduplicate_with_report(items, rules=rules)
    return out, report


# === Drapeaux ===

class TestFlags:
    def test_all_rules_off_by_default(self):
        assert dedup_rules() == ALL_OFF

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "oui"])
    def test_env_enables(self, monkeypatch, value):
        monkeypatch.setenv(CODIFFUSION_ENV, value)
        assert dedup_rules().codiffusion is True

    def test_default_behaviour_unchanged(self):
        """Drapeaux absents : les quatre paires du retour restent en deux
        notices chacune, comme avec les règles d'OPT-06."""
        for name in ("quaderni", "mwi_zenodo", "gjerstad", "franzosi"):
            assert len(deduplicate(notices(name))) == 2, name

    def test_rules_version_and_report(self):
        assert RULES_VERSION == "dedup/2"
        _out, report = dedup(notices("quaderni"), ALL_OFF)
        assert report["rules"] == ALL_OFF.as_dict()
        assert report["doi_equivalences"] == 0


# === PAT-06 : co-diffusion ===

class TestCodiffusion:
    RULES = dedup_rules(codiffusion=True)

    def test_quaderni_merged_with_cairn_doi_kept(self):
        out, report = dedup(notices("quaderni"), self.RULES)
        assert len(out) == 1
        item = out[0]
        assert item["DOI"] == "10.3917/quad.109.0039"
        assert extra(item, ALIAS_KEY) == ["10.4000/quaderni.2731 (co-diffusion)"]
        # L'autre DOI n'est plus une ligne « DOI: » (lue comme DOI déclaré).
        assert "10.4000/quaderni.2731" not in extra(item, "DOI")
        ev = parse_evidence(item)
        assert ev["count"] == 3
        assert any(e.startswith("co-diffusion 10.3917/quad.109.0039 = "
                                "10.4000/quaderni.2731 (titre identique, "
                                "année 2023, auteur cormerais")
                   for e in ev["equivalences"]), ev
        assert report["doi_equivalences"] == 1

    def test_order_independent(self):
        base = notices("quaderni")
        ref = json.dumps(dedup(base, self.RULES)[0], sort_keys=True,
                         ensure_ascii=False)
        rng = random.Random(7)
        for _ in range(5):
            shuffled = copy.deepcopy(base)
            rng.shuffle(shuffled)
            got = json.dumps(dedup(shuffled, self.RULES)[0], sort_keys=True,
                             ensure_ascii=False)
            assert got == ref

    def test_idempotent_and_alias_honoured(self):
        merged, _ = dedup(notices("quaderni"), self.RULES)
        again, _ = dedup(copy.deepcopy(merged), self.RULES)
        assert again == merged
        # Fusion de corpus : la notice fusionnée et une nouvelle copie de la
        # notice OpenEdition se retrouvent par la ligne DOI alias.
        openedition = [n for n in notices("quaderni")
                       if n["DOI"].startswith("10.4000")]
        out, _ = dedup(copy.deepcopy(merged) + openedition, self.RULES)
        assert len(out) == 1
        assert out[0]["DOI"] == "10.3917/quad.109.0039"

    def test_alias_ignored_when_rule_off(self):
        merged, _ = dedup(notices("quaderni"), self.RULES)
        openedition = [n for n in notices("quaderni")
                       if n["DOI"].startswith("10.4000")]
        out, _ = dedup(copy.deepcopy(merged) + openedition, ALL_OFF)
        assert len(out) == 2

    @pytest.mark.parametrize("change", [
        {"date": "2022"},                                        # autre année
        {"pages": "59-78"},                                      # autres pages
        {"creators": [{"creatorType": "author", "firstName": "Jean",
                       "lastName": "Dupont"}]},                  # autre auteur
        {"publicationTitle": "Réseaux", "ISSN": "0751-7971"},    # autre revue
        {"itemType": "book"},                                    # type incompatible
        {"DOI": "10.3917/quad.109.0040"},                        # même préfixe
        {"DOI": "10.48550/arxiv.2301.00001"},                    # prépublication
    ])
    def test_negative_variants_stay_apart(self, change):
        a, b = notices("quaderni")
        b.update(change)
        if "DOI" in change:
            b["extra"] = "\n".join(l for l in b["extra"].split("\n")
                                   if not l.startswith("DOI:"))
        assert len(dedup([a, b], self.RULES)[0]) == 2

    def test_review_titles_never_codiffused(self):
        a, b = notices("quaderni")
        for n in (a, b):
            n["title"] = ("Compte rendu de Juan Branco, influenceur éphémère "
                          "ou figure d'un nouvel intellectuel numérique")
        assert len(dedup([a, b], self.RULES)[0]) == 2

    def test_same_journal_without_issn_accepted_by_title(self):
        a, b = notices("quaderni")
        b.pop("ISSN", None)
        out, _ = dedup([a, b], self.RULES)
        assert len(out) == 1

    @pytest.mark.parametrize("family, kept", [
        (["10.3917/a.1", "10.3406/b.2"], "10.3917/a.1"),         # Cairn d'abord
        (["10.3406/b.2", "10.4000/c.3"], "10.4000/c.3"),         # OpenEdition > Persée
        (["10.4074/d.4", "10.4000/c.3"], "10.4074/d.4"),         # éditeur > OpenEdition
        (["10.4074/d.4", "10.3917/a.1"], "10.3917/a.1"),         # Cairn > éditeur
        (["10.7202/e.5", "10.3406/b.2"], "10.7202/e.5"),         # Érudit > Persée
        (["10.5281/zenodo.9", "10.5281/zenodo.10"], "10.5281/zenodo.9"),
    ])
    def test_kept_doi_order(self, family, kept):
        assert _keep_doi(family) == kept


# === PAT-08 : versions Zenodo ===

class TestZenodoVersions:
    RULES = dedup_rules(zenodo_versions=True)

    def test_mwi_concept_doi_kept(self):
        out, report = dedup(notices("mwi_zenodo"), self.RULES)
        assert len(out) == 1
        assert out[0]["DOI"] == "10.5281/zenodo.18376428"
        assert extra(out[0], ALIAS_KEY) == [
            "10.5281/zenodo.18376429 (version Zenodo)"]
        assert report["doi_equivalences"] == 1

    def test_other_creators_stay_apart(self):
        a, b = notices("mwi_zenodo")
        b["creators"] = [{"creatorType": "author", "firstName": "Jean",
                          "lastName": "Dupont"}]
        assert len(dedup([a, b], self.RULES)[0]) == 2

    def test_other_year_stays_apart(self):
        a, b = notices("mwi_zenodo")
        b["date"] = "2024-01-26"
        assert len(dedup([a, b], self.RULES)[0]) == 2

    def test_codiffusion_flag_alone_does_not_merge_zenodo(self):
        assert len(dedup(notices("mwi_zenodo"),
                         dedup_rules(codiffusion=True))[0]) == 2


# === PAT-07 : chapitres Google Books ===

class TestScholarChapters:
    RULES = dedup_rules(scholar_chapters=True)

    def test_gjerstad_merged(self):
        out, _ = dedup(notices("gjerstad"), self.RULES)
        assert len(out) == 1
        assert out[0]["title"].startswith("Imagining action on climate change")
        ev = parse_evidence(out[0])
        assert any("numéro de chapitre « 1 » ignoré" in f for f in ev["fuzzy"])

    def test_franzosi_merged_title_without_number(self):
        out, _ = dedup(notices("franzosi"), self.RULES)
        assert len(out) == 1
        assert out[0]["title"] == "Narrative Analysis"

    def test_real_numbered_title_untouched(self):
        items = notices("numbered_title") + notices("franzosi")
        out, _ = dedup(items, self.RULES)
        titles = sorted(i["title"] for i in out)
        assert any(t.startswith("200 Years of Children in the Novel")
                   for t in titles)
        assert len(out) == 2

    def test_non_scholar_notice_not_concerned(self):
        a, b = notices("franzosi")
        b["extra"] = b["extra"].replace("Sources: Google Scholar (SerpAPI)",
                                        "Sources: CrossRef")
        assert len(dedup([a, b], self.RULES)[0]) == 2

    def test_different_number_on_both_sides_not_stripped(self):
        a, b = notices("franzosi")
        b["title"] = "54 Narrative Analysis"
        assert len(dedup([a, b], self.RULES)[0]) == 2

    def test_other_author_stays_apart(self):
        a, b = notices("franzosi")
        b["creators"] = [{"creatorType": "author", "firstName": "Jean",
                          "lastName": "Dupont"}]
        assert len(dedup([a, b], self.RULES)[0]) == 2


def test_manifest_records_rules_version():
    from utils.manifest import software_versions
    software_versions.cache_clear()
    try:
        assert software_versions()["dedup_rules"]["version"] == "dedup/2"
    finally:
        software_versions.cache_clear()


# === Jeu témoin du sprint patch (paires réelles, tests/fixtures/dedup_pairs_patch.json) ===

PATCH_PAIRS = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                          / "dedup_pairs_patch.json").read_text(encoding="utf-8"))["pairs"]


@pytest.mark.parametrize("pair", PATCH_PAIRS, ids=[p["id"] for p in PATCH_PAIRS])
def test_patch_reference_pair_with_rules_on(pair):
    out, _ = dedup(copy.deepcopy(pair["items"]), ALL_ON)
    assert len(out) == pair["expected_notices"], (pair["id"], pair["note"])


@pytest.mark.parametrize("pair", PATCH_PAIRS, ids=[p["id"] for p in PATCH_PAIRS])
def test_patch_reference_pair_with_rules_off(pair):
    out, _ = dedup(copy.deepcopy(pair["items"]), ALL_OFF)
    assert len(out) == 2, pair["id"]


def test_patch_reference_set_has_no_cross_pair_merge():
    """Toutes les paires ensemble : aucune fusion entre paires distinctes.
    Une même notice figure dans plusieurs paires (« Présentation » de
    Persée) : ses copies se réunissent, rien d'autre."""
    from utils.deduplication import _identity_doi

    def key(it):
        return _identity_doi(it) or it.get("url")

    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            x = parent[x]
        return x

    for p in PATCH_PAIRS:
        a, b = (key(it) for it in p["items"])
        find(a), find(b)
        if p["expected_notices"] == 1:
            parent[find(a)] = find(b)
    expected = len({find(k) for k in list(parent)})
    items = [copy.deepcopy(it) for p in PATCH_PAIRS for it in p["items"]]
    out, _ = dedup(items, ALL_ON)
    assert len(out) == expected
