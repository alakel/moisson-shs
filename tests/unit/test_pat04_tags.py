"""
PAT-04 (retour d'expérience du 2026-09-30, B1) : ``tags`` ne porte que des
mots-clés déclarés. Les classifications attribuées automatiquement
(concepts OpenAlex, champs d'étude Semantic Scholar, catégories de revue
CrossRef) sont tracées dans ``extra`` et n'arrivent plus dans Zotero comme
mots-clés, ni directement ni par la fusion des notices.
"""

from __future__ import annotations

from mappers.hal_mapper import map_hal_to_zotero as map_hal_doc
from mappers.openalex_mapper import map_openalex_item
from utils.deduplication import deduplicate
from utils.lookups import _map_crossref_work

# Concepts réels de la notice « Stop saying LLM » (corpus auteur du
# 2026-09-30), dont le retour d'expérience relevait l'arrivée en mots-clés.
CONCEPTS = [
    ("Identity (music)", 0.61), ("Product (mathematics)", 0.55),
    ("Space (punctuation)", 0.42), ("Selection (genetic algorithm)", 0.40),
    ("Sociology", 0.35), ("Bruit", 0.10),
]


def _openalex_work(doi="10.48550/arxiv.2512.19117"):
    return {
        "id": "https://openalex.org/W7117161368",
        "doi": f"https://doi.org/{doi}",
        "title": "Stop saying LLM: Large Discourse Models (LDM) and Artificial "
                 "Discursive Agent (ADA)?",
        "publication_year": 2025,
        "publication_date": "2025-12-22",
        "type": "article",
        "authorships": [{"author": {"display_name": "Amar Lakel"}}],
        "concepts": [{"display_name": n, "score": s} for n, s in CONCEPTS],
        "primary_location": {"source": {"display_name": "Revue test"}},
    }


def _tags(item):
    return [t["tag"] for t in item.get("tags") or []]


def _extra(item, key):
    return [l.split(": ", 1)[1] for l in (item.get("extra") or "").split("\n")
            if l.startswith(key + ": ")]


def test_openalex_concepts_never_become_tags():
    item = map_openalex_item(_openalex_work())
    assert _tags(item) == []
    line = _extra(item, "OpenAlex concepts")[0]
    for name, score in CONCEPTS:
        assert (name in line) == (score >= 0.3), name


def test_merge_keeps_only_declared_keywords():
    """Une notice HAL (mots-clés d'auteur) fusionnée avec la notice OpenAlex
    du même DOI ne reçoit aucun concept en mot-clé."""
    doi = "10.4000/quaderni.2731"
    hal = map_hal_doc({
        "halId_s": "hal-04766242", "docType_s": "ART",
        "title_s": ["Juan Branco, influenceur éphémère ou figure d'un nouvel "
                    "« intellectuel numérique » ?"],
        "authFirstName_s": ["Franck", "Amar"],
        "authLastName_s": ["Cormerais", "Lakel"],
        "producedDate_s": "2023", "doiId_s": doi,
        "keyword_s": ["Cartographie web", "Controverse numérique"],
        "journalTitle_s": "Quaderni",
    })
    work = _openalex_work(doi)
    work["title"] = hal["title"]
    work["publication_date"] = "2023-04-01"
    work["publication_year"] = 2023
    work["authorships"] = [{"author": {"display_name": "Franck Cormerais"}},
                           {"author": {"display_name": "Amar Lakel"}}]
    merged = deduplicate([hal, map_openalex_item(work)])
    assert len(merged) == 1
    assert sorted(_tags(merged[0])) == ["Cartographie web",
                                        "Controverse numérique"]
    assert _extra(merged[0], "OpenAlex concepts")


def test_crossref_subjects_from_fetch_by_doi_path_go_to_extra():
    item = _map_crossref_work({
        "DOI": "10.1234/x", "type": "journal-article",
        "title": ["Titre"], "subject": ["Sociology and Political Science",
                                        "Communication"],
    })
    assert _tags(item) == []
    assert _extra(item, "CrossRef subjects") == [
        "Sociology and Political Science, Communication"]
