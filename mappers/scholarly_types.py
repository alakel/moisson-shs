"""
Tables de correspondance des types de documents des sources internationales
(CrossRef, OpenAlex, Semantic Scholar, CORE) vers les ``itemType`` Zotero.

Référence Zotero : https://api.zotero.org/schema

Les ``itemType`` peu usuels (``preprint``, ``dataset``, ``presentation``)
doivent round-tripper vers CSL — vérifié dans ``mappers/csl_exporter.py`` et
``mappers/doc_type_mapping.py``.
"""

from __future__ import annotations

from typing import Iterable, Optional


# --- CrossRef (champ 'type') ------------------------------------------------
_CROSSREF = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "proceedings": "conferencePaper",
    "book": "book",
    "monograph": "book",
    "reference-book": "book",
    "edited-book": "book",
    "book-chapter": "bookSection",
    "book-section": "bookSection",
    "book-part": "bookSection",
    "book-track": "bookSection",
    "dissertation": "thesis",
    "report": "report",
    "report-component": "report",
    "report-series": "report",
    "posted-content": "preprint",
    "dataset": "dataset",
    "journal-issue": "document",
    "journal-volume": "document",
    "peer-review": "document",
    "standard": "document",
    "component": "document",
    "grant": "document",
    "other": "document",
}


def crossref_type_to_zotero(t: Optional[str]) -> str:
    """Convertit un champ CrossRef ``type`` en ``itemType`` Zotero."""
    return _CROSSREF.get((t or "").lower().strip(), "document")


# --- OpenAlex (champ 'type') -------------------------------------------------
# OpenAlex utilise depuis 2024 des libellés Crossref (type_crossref) et un
# vocabulaire propre (type). On couvre les deux.
_OPENALEX = {
    "article": "journalArticle",
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "proceedings": "conferencePaper",
    "book": "book",
    "monograph": "book",
    "reference-book": "book",
    "book-chapter": "bookSection",
    "book-part": "bookSection",
    "dissertation": "thesis",
    "report": "report",
    "preprint": "preprint",
    "posted-content": "preprint",
    "dataset": "dataset",
    "reference-entry": "encyclopediaArticle",
    "review": "journalArticle",
    "editorial": "journalArticle",
    "letter": "letter",
    "paratext": "document",
    "supplementary-materials": "document",
    "standard": "document",
    "grant": "document",
    "other": "document",
}


def openalex_type_to_zotero(t: Optional[str]) -> str:
    """Convertit un champ OpenAlex ``type`` en ``itemType`` Zotero."""
    return _OPENALEX.get((t or "").lower().strip(), "document")


# --- Semantic Scholar (liste 'publicationTypes') -----------------------------
_S2 = {
    "JournalArticle": "journalArticle",
    "Conference": "conferencePaper",
    "Book": "book",
    "BookSection": "bookSection",
    "Review": "journalArticle",
    "Dataset": "dataset",
    "Editorial": "journalArticle",
    "News": "magazineArticle",
    "Study": "document",
    "LettersAndComments": "letter",
    "CaseReport": "document",
    "ClinicalTrial": "document",
}


def s2_type_to_zotero(types: Optional[Iterable[str]]) -> str:
    """
    Convertit la liste S2 ``publicationTypes`` en ``itemType`` Zotero.

    On retient le premier type reconnu de la liste (S2 fournit parfois
    plusieurs valeurs, ex : ["JournalArticle", "Review"]).
    """
    for t in (types or []):
        if t in _S2:
            return _S2[t]
    return "document"


# --- CORE (champ 'documentType') ---------------------------------------------
_CORE = {
    "research": "journalArticle",
    "article": "journalArticle",
    "journal article": "journalArticle",
    "thesis": "thesis",
    "doctoral thesis": "thesis",
    "presentation": "presentation",
    "slides": "presentation",
    "book": "book",
    "book chapter": "bookSection",
    "report": "report",
    "dataset": "dataset",
    "review": "journalArticle",
    "preprint": "preprint",
    "conference object": "conferencePaper",
    "unknown": "document",
}


def core_type_to_zotero(t: Optional[str]) -> str:
    """Convertit un champ CORE ``documentType`` en ``itemType`` Zotero."""
    return _CORE.get((t or "").lower().strip(), "document")
