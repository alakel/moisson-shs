"""
Règles ``doi-match/4`` (sprint patch, retour d'expérience du 2026-09-30) :

- PAT-07 (B4) : numéro de chapitre en tête d'un titre Google Books ignoré
  face à un chapitre CrossRef (drapeau ``MOISSON_DOI_CHAPTER_PREFIX``,
  désactivé par défaut) ; année et premier auteur exigés ; titres
  génériques exclus ; titre corrigé après acceptation seulement ;
- PAT-20 : titre coupé par Google Scholar (points de suspension) apparié
  seulement avec l'année **et** l'auteur, sur un préfixe substantiel
  (durcissement, toujours actif) ;
- rapport au format 2 : auteurs des candidats, règle appliquée,
  concordances d'année et d'auteur.

Cas réels : ``tests/fixtures/patch_2026-09-30.json`` (Franzosi 2023,
« 200 Years of Children in the Novel »).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import utils.doi_resolver as dr

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                      / "patch_2026-09-30.json").read_text(encoding="utf-8"))
FRANZOSI_GB = next(n for n in FIXTURE["franzosi"]
                   if n["title"] == "53 Narrative Analysis")
NUMBERED = FIXTURE["numbered_title"][0]


def cand(title, year=2023, families=("Franzosi",), doi="10.4324/9781003213277-62",
         ctype="book-chapter"):
    c = {"title": [title], "DOI": doi, "type": ctype,
         "author": [{"family": f} for f in families]}
    if year:
        c["issued"] = {"date-parts": [[year]]}
    return c


@pytest.fixture
def chapter_rule(monkeypatch):
    monkeypatch.setenv(dr.CHAPTER_PREFIX_ENV, "1")


@pytest.fixture(autouse=True)
def _rule_off_by_default(monkeypatch):
    monkeypatch.delenv(dr.CHAPTER_PREFIX_ENV, raising=False)
    dr.shared_query_cache().clear()
    yield
    dr.shared_query_cache().clear()


def test_rules_version():
    assert dr.RULES_VERSION == "doi-match/4"
    assert dr.REPORT_FORMAT_VERSION == 2


# === Numéro de chapitre Google Books ===

class TestChapterNumber:
    def test_rule_off_keeps_v3_rejection(self):
        d = dr.decide(copy.deepcopy(FRANZOSI_GB), [cand("Narrative Analysis")])
        assert d["status"] == dr.STATUS_REJECTED
        assert "numéros de titre discordants" in d["reason"]

    def test_franzosi_accepted_with_rule(self, chapter_rule):
        d = dr.decide(copy.deepcopy(FRANZOSI_GB), [cand("Narrative Analysis")])
        assert d["status"] == dr.STATUS_ACCEPTED, d["reason"]
        assert d["doi"] == "10.4324/9781003213277-62"
        assert "numéro de chapitre « 53 » ignoré" in d["reason"]
        assert d["title_correction"] == "Narrative Analysis"
        c = d["candidates"][0]
        assert c["rule"] == dr.RULE_CHAPTER_PREFIX
        assert c["authors"] == ["franzosi"]
        assert (c["year_match"], c["author_match"]) == (True, True)

    @pytest.mark.parametrize("candidate", [
        cand("Narrative Analysis", ctype="book"),             # ouvrage entier
        cand("Narrative Analysis", families=("Dupont",)),     # autre auteur
        cand("Narrative Analysis", year=None),                # année inconnue
        cand("Narrative Analysis", families=()),              # auteurs inconnus
        cand("53 Narrative Analysis"),                        # nombre des deux côtés
    ])
    def test_guards(self, chapter_rule, candidate):
        d = dr.decide(copy.deepcopy(FRANZOSI_GB), [candidate])
        assert d["status"] != dr.STATUS_ACCEPTED or \
            "numéro de chapitre" not in d["reason"], d["reason"]
        assert d.get("title_correction") is None

    def test_link_other_than_google_books(self, chapter_rule):
        item = copy.deepcopy(FRANZOSI_GB)
        item["url"] = "https://example.org/chapitre"
        d = dr.decide(item, [cand("Narrative Analysis")])
        assert d["status"] == dr.STATUS_REJECTED

    def test_generic_rest_excluded(self, chapter_rule):
        item = copy.deepcopy(FRANZOSI_GB)
        item["title"] = "1 Introduction"
        d = dr.decide(item, [cand("Introduction")])
        assert d["status"] == dr.STATUS_REJECTED

    def test_real_numbered_title_untouched(self, chapter_rule):
        """« 200 Years of Children in the Novel » : vrai titre (lien hors
        Google Books), jamais privé de son nombre."""
        item = copy.deepcopy(NUMBERED)
        title = item["title"][4:]
        d = dr.decide(item, [cand(title, year=2026,
                                  families=(item["creators"][0]["lastName"],))])
        assert d["status"] == dr.STATUS_REJECTED
        assert d.get("title_correction") is None

    def test_resolution_corrects_title_and_keeps_scholar_title(self, chapter_rule):
        answer = {"message": {"items": [cand("Narrative Analysis")]}}
        with patch.object(dr, "get_json", return_value=answer), \
                patch("utils.http.time.sleep", lambda s: None):
            out, report = dr.resolve_dois([copy.deepcopy(FRANZOSI_GB)])
        item = out[0]
        assert item["DOI"] == "10.4324/9781003213277-62"
        assert item["title"] == "Narrative Analysis"
        extra = item["extra"].split("\n")
        assert "Scholar title: 53 Narrative Analysis" in extra
        assert any(l.startswith("DOI source: ") and "doi-match/4" in l
                   and "numéro de chapitre « 53 » ignoré" in l for l in extra)


# === Titre coupé par Google Scholar (durcissement) ===

CUT = "Imagining action on climate change: A comparison of Belgian and…"
FULL = ("Imagining action on climate change: A comparison of Belgian and "
        "Norwegian surveys")


def _item(title, last="Gjerstad", year="2025"):
    return {"itemType": "journalArticle", "title": title, "date": year,
            "creators": [{"creatorType": "author", "firstName": "Ø",
                          "lastName": last}] if last else [],
            "extra": "Sources: Google Scholar (SerpAPI)", "tags": []}


class TestTruncatedTitle:
    def test_year_alone_no_longer_enough(self):
        d = dr.decide(_item(CUT), [cand(FULL, year=2025, families=())])
        assert d["status"] == dr.STATUS_REJECTED
        assert "année et premier auteur exigés" in d["reason"]

    def test_year_and_author_accepted(self):
        d = dr.decide(_item(CUT), [cand(FULL, year=2025,
                                        families=("Gjerstad",))])
        assert d["status"] == dr.STATUS_ACCEPTED, d["reason"]
        assert "titre coupé" in d["reason"]

    def test_short_cut_title_rejected(self):
        d = dr.decide(_item("Digital trace data as…"),
                      [cand("Digital trace data as measurement instruments",
                            year=2025, families=("Gjerstad",))])
        assert d["status"] == dr.STATUS_REJECTED

    def test_cut_after_the_differing_word_rejected(self):
        """Négatif de PAT-20 : titre coupé après « France », candidat sur
        l'Italie, même auteur et même année : aucun DOI."""
        d = dr.decide(_item("Comparing media coverage of migration flows in "
                            "France and…", last="Rossi", year="2021"),
                      [cand("Comparing media coverage of migration flows in "
                            "Italy and Spain", year=2021, families=("Rossi",),
                            doi="10.1000/it", ctype="journal-article")])
        assert d["status"] == dr.STATUS_REJECTED

    def test_cut_before_the_differing_word_is_ambiguous(self):
        """Coupe avant le mot qui distingue deux travaux du même auteur et de
        la même année : les deux candidats restent admissibles, aucun DOI
        écrit. (Avec un seul candidat renvoyé par CrossRef, la coupe le rend
        indiscernable : limite consignée au rapport du sprint.)"""
        base = "Comparing media coverage of migration flows in"
        d = dr.decide(_item(base + "…", last="Rossi", year="2021"),
                      [cand(base + " Italy", year=2021, families=("Rossi",),
                            doi="10.1000/it", ctype="journal-article"),
                       cand(base + " France", year=2021, families=("Rossi",),
                            doi="10.1000/fr", ctype="journal-article")])
        assert d["status"] == dr.STATUS_AMBIGUOUS

    def test_untruncated_title_keeps_v3_behaviour(self):
        """Titre sans coupe : une corroboration suffit toujours (v3)."""
        d = dr.decide(_item("Imagining action on climate change"),
                      [cand(FULL, year=2025, families=())])
        assert d["status"] == dr.STATUS_ACCEPTED, d["reason"]


def test_report_candidates_have_format_2_fields():
    d = dr.decide(_item(FULL), [cand(FULL, year=2025, families=("Gjerstad",))])
    c = d["candidates"][0]
    for key in ("authors", "rule", "year_match", "author_match"):
        assert key in c
    assert c["rule"] == dr.RULE_STANDARD


# === DOI des liens Taylor & Francis (identifierValue) ===

TF = ("https://api.taylorfrancis.com/content/chapters/edit/download?"
      "identifierName=doi&identifierValue=10.4324/9781003630968-3&type=chapterpdf")


@pytest.mark.parametrize("url, expected", [
    (TF, "10.4324/9781003630968-3"),
    (TF.replace("identifierName=doi", "identifierName=isbn"), None),
    (TF.replace("api.taylorfrancis.com", "example.org"), None),
])
def test_doi_from_taylor_francis_query(url, expected):
    from mappers.google_scholar_mapper import extract_doi
    from utils.identity import doi_from_url
    assert doi_from_url(url) == expected
    assert (extract_doi(url) or None) == expected


def test_gjerstad_chapter_now_carries_its_doi():
    from utils.deduplication import _identity_doi
    twin = next(n for n in FIXTURE["gjerstad"] if "taylorfrancis" in n["url"])
    assert _identity_doi(twin) == "10.4324/9781003630968-3"
