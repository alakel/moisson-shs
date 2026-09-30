"""
Finitions du sprint patch (retour d'expérience du 2026-09-30) :

- PAT-11 (B6) : un dépôt (HAL, arXiv, Zenodo) n'est jamais une revue ;
- PAT-12 (B7) : revues et auteurs comptés sous une clé normalisée ;
- PAT-14 (B9) : valeurs HAL répétées dédoublonnées ;
- PAT-15 (B10) : ponctuation hors des accolades de casse BibTeX ;
- PAT-17 : une seule ligne « Open Access » après fusion, « language: und »
  retirée.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mappers.bibtex_exporter import protect_title
from mappers.hal_mapper import map_hal_to_zotero
from mappers.openalex_mapper import map_openalex_item
from utils.corpus_ops import author_key, journal_key, top_forms
from utils.deduplication import deduplicate

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                      / "patch_2026-09-30.json").read_text(encoding="utf-8"))


def _extra(item, key):
    return [l.split(": ", 1)[1] for l in (item.get("extra") or "").split("\n")
            if l.startswith(key + ": ")]


# === PAT-11 : dépôt pris pour une revue ===

def _work(otype, source, locations=None):
    w = {"id": "https://openalex.org/W1", "title": "Titre de travail assez long",
         "type": otype, "publication_year": 2025,
         "authorships": [{"author": {"display_name": "Amar Lakel"}}],
         "primary_location": {"source": source,
                              "landing_page_url": "https://hal.science/hal-1"}}
    if locations is not None:
        w["locations"] = locations
    return w


HAL_SOURCE = {"display_name": "HAL (Le Centre pour la Communication "
                              "Scientifique Directe)", "type": "repository"}
ARXIV_SOURCE = {"display_name": "arXiv (Cornell University)",
                "type": "repository", "issn_l": "2331-8422",
                "issn": ["2331-8422"]}


class TestRepositoryIsNotAJournal:
    def test_preprint_gets_repository_field_and_no_issn(self):
        item = map_openalex_item(_work("preprint", ARXIV_SOURCE))
        assert item["itemType"] == "preprint"
        assert item.get("repository") == "arXiv"
        assert "ISSN" not in item
        assert not any(l.startswith("publicationTitle:")
                       for l in (item.get("extra") or "").split("\n"))

    def test_article_takes_journal_from_locations(self):
        journal = {"display_name": "Quaderni", "type": "journal",
                   "issn_l": "0987-1381"}
        item = map_openalex_item(_work("article", HAL_SOURCE, locations=[
            {"source": HAL_SOURCE}, {"source": journal}]))
        assert item["publicationTitle"] == "Quaderni"
        assert item["ISSN"] == "0987-1381"
        assert _extra(item, "Repository") == ["HAL"]

    def test_article_without_journal_location(self):
        item = map_openalex_item(_work("article", HAL_SOURCE))
        assert "publicationTitle" not in item
        assert _extra(item, "Repository") == ["HAL"]

    def test_repository_recognised_by_name_without_type(self):
        source = {"display_name": "Zenodo (CERN European Organization for "
                                  "Nuclear Research)"}
        item = map_openalex_item(_work("article", source))
        assert "publicationTitle" not in item
        assert _extra(item, "Repository") == ["Zenodo"]

    def test_journal_source_unchanged(self):
        item = map_openalex_item(_work("article", {
            "display_name": "Réseaux", "type": "journal",
            "issn_l": "0751-7971"}))
        assert item["publicationTitle"] == "Réseaux"
        assert item["ISSN"] == "0751-7971"

    def test_preprint_export_ignores_parked_container(self):
        """Ancien corpus : ligne « publicationTitle: HAL (…) » dans extra
        d'une prépublication ; l'export BibTeX et RIS ne la relit plus."""
        from mappers.bibtex_exporter import _container as bib_container
        from mappers.ris_exporter import _container as ris_container
        item = {"itemType": "preprint", "title": "T",
                "extra": "publicationTitle: HAL (Le Centre pour la "
                         "Communication Scientifique Directe)"}
        assert bib_container(item) == ""
        assert ris_container(item) == ""


# === PAT-12 : comptage des revues et des auteurs ===

class TestCounting:
    def test_journal_variants_share_a_key(self):
        assert journal_key("Communication et organisation") == \
            journal_key("Communication & Organisation") == \
            journal_key("COMMUNICATION AND ORGANISATION")

    def test_top_forms_keeps_most_frequent_form(self):
        pairs = [(journal_key(n), n) for n in (
            "Communication et organisation", "Communication et organisation",
            "Communication & Organisation", "Quaderni")]
        assert top_forms(pairs, 5) == [("Communication et organisation", 3),
                                       ("Quaderni", 1)]

    def test_authors_by_name_and_initial(self):
        a = author_key({"lastName": "Martin", "firstName": "Jean"})
        b = author_key({"lastName": "Martin", "firstName": "Paul"})
        c = author_key({"lastName": "MARTIN", "firstName": "jean"})
        assert a[0] != b[0] and a[1] == "Martin J."
        assert a[0] == c[0]


# === PAT-14 : valeurs HAL répétées ===

def test_hal_domains_deduplicated():
    item = map_hal_to_zotero({
        "halId_s": "tel-05525754", "docType_s": "HDR", "title_s": ["T"],
        "domain_s": ["0.shs", "1.shs.info", "0.scco", "1.scco.ling", "0.shs",
                     "1.shs.scipo", "0.shs", "1.shs.socio"],
        "labStructAcronym_s": ["MICA", "MICA"]})
    assert _extra(item, "HAL Domain") == [
        "0.shs, 1.shs.info, 0.scco, 1.scco.ling, 1.shs.scipo, 1.shs.socio"]
    assert _extra(item, "HAL Laboratory") == ["MICA"]


# === PAT-15 : accolades de casse ===

@pytest.mark.parametrize("title, expected", [
    ("Juan Branco, influenceur", "Juan {Branco}, influenceur"),
    ("Stop saying LLM: Large", "Stop saying {LLM}: {Large}"),
    ("Models (LDM) and Agent (ADA)?", "Models ({LDM}) and {Agent} ({ADA})?"),
    ("Les archives de Foucault et HAL", "Les archives de {Foucault} et {HAL}"),
])
def test_punctuation_outside_case_braces(title, expected):
    assert protect_title(title) == expected


def test_no_brace_encloses_punctuation_in_author_export():
    import re
    from mappers.bibtex_exporter import export_bibtex
    items = copy.deepcopy(FIXTURE["quaderni"])
    out = export_bibtex(items)
    text = out if isinstance(out, str) else getattr(out, "text", str(out))
    assert "{Branco}," in text
    assert not re.search(r"\{[A-Z][^{}\s]*[,:;?!]\}", text)


# === PAT-17 : lignes extra à valeur unique ===

def _pair(oa_a, oa_b, lang_line=""):
    base = {"itemType": "journalArticle", "title": "Un titre assez long pour "
            "la passe", "date": "2023", "DOI": "10.1234/abc",
            "creators": [{"creatorType": "author", "firstName": "A",
                          "lastName": "Lakel"}], "tags": []}
    a = dict(base, extra=f"Sources: HAL\nOpen Access: {oa_a}" + lang_line)
    b = dict(base, extra=f"Sources: OpenAlex\nOpen Access: {oa_b}")
    return [a, b]


@pytest.mark.parametrize("a, b, expected", [
    ("no", "yes", "yes"), ("unknown", "no", "no"),
    ("unknown", "unknown", "unknown"), ("yes", "yes", "yes"),
])
def test_single_open_access_line(a, b, expected):
    merged = deduplicate(_pair(a, b))
    assert len(merged) == 1
    assert _extra(merged[0], "Open Access") == [expected]


def test_language_und_dropped():
    merged = deduplicate(_pair("no", "yes", "\nlanguage: und"))
    assert _extra(merged[0], "language") == []
