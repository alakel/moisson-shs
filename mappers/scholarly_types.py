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


# --- Google Scholar via SerpAPI (champ 'type' + indices de notice) -----------
# Google Scholar ne typifie pas ses résultats : SerpAPI expose seulement le
# bandeau affiché devant le titre ([BOOK], [PDF], [CITATION], [HTML]), absent
# pour l'immense majorité des résultats. Le type réel se déduit donc surtout de
# la notice (présence d'un nom de revue, domaine de l'URL), d'où la signature
# à trois arguments et le défaut prudent ``document``.
_SCHOLAR_BANNER = {
    "book": "book",
    "b": "book",
    "citation": "document",   # notice sans document accessible
    "c": "document",
    "pdf": "",                # le bandeau ne dit rien du genre : on infère
    "html": "",
    "doc": "",
}


def scholar_type_to_zotero(
    banner: Optional[str] = None,
    venue: Optional[str] = None,
    url: Optional[str] = None,
) -> str:
    """
    Déduit un ``itemType`` Zotero pour un résultat Google Scholar.

    Args:
        banner: Champ ``type`` de SerpAPI (« Book », « Citation »…), souvent absent.
        venue: Nom de revue ou d'ouvrage extrait de ``publication_info.summary``.
        url: Lien principal du résultat.

    Returns:
        ``itemType`` Zotero. ``journalArticle`` quand une revue est nommée,
        ``book`` sur les liens Google Books, ``document`` à défaut.
    """
    mapped = _SCHOLAR_BANNER.get((banner or "").lower().strip())
    if mapped:
        return mapped

    u = (url or "").lower()
    if "books.google." in u:
        return "book"
    if "theses.fr" in u or "/tel-" in u or "tel.archives-ouvertes" in u:
        return "thesis"
    if "arxiv.org" in u or "/hal-" in u or "ssrn.com" in u or "osf.io" in u:
        return "preprint"

    v = (venue or "").strip()
    if v:
        low = v.lower()
        if "proceeding" in low or "conference" in low or "workshop" in low:
            return "conferencePaper"
        return "journalArticle"
    return "document"
