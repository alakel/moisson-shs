"""
Mapper CORE (work) → item Zotero JSON.

CORE agrège ~290 M de documents en accès ouvert avec texte intégral. On trace
le statut texte intégral et l'URL du PDF (``downloadUrl`` /
``sourceFulltextUrls``) dans ``extra``, et l'identifiant CORE pour la dédup.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mappers.scholarly_types import core_type_to_zotero
from mappers.zotero_schema import (
    append_to_extra, make_empty_item, make_tags, validate_and_clean,
)
from utils.names import split_display_name

SOURCE_LABEL = "CORE"


def _first(seq) -> Optional[Any]:
    if isinstance(seq, list):
        for v in seq:
            if v:
                return v
        return None
    return seq or None


def map_core_item(raw: Dict) -> Dict:
    """Convertit un work CORE (un élément de ``results``) en item Zotero."""
    item_type = core_type_to_zotero(raw.get("documentType", ""))
    item = make_empty_item(item_type)

    # Titre
    item["title"] = raw.get("title", "") or ""

    # Auteurs : liste de {name} ou de chaînes
    creators: List[Dict] = []
    for a in raw.get("authors", []) or []:
        name = a.get("name", "") if isinstance(a, dict) else str(a)
        first, last = split_display_name(name)
        if first or last:
            creators.append({
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            })
    item["creators"] = creators

    # Résumé
    if raw.get("abstract"):
        item["abstractNote"] = raw["abstract"]

    # Date
    year = raw.get("yearPublished")
    date = raw.get("publishedDate") or (str(year) if year else "")
    if date:
        item["date"] = date

    # Langue : objet {code, name} ou chaîne
    lang = raw.get("language")
    language = lang.get("code", "") if isinstance(lang, dict) else (lang or "")
    if language:
        item["language"] = language

    # Conteneur (revue)
    journals = raw.get("journals", []) or []
    journal_title = (
        journals[0].get("title", "")
        if journals and isinstance(journals[0], dict) else ""
    )
    if journal_title:
        if item_type == "conferencePaper":
            item["proceedingsTitle"] = journal_title
        elif item_type == "bookSection":
            item["bookTitle"] = journal_title
        else:
            item["publicationTitle"] = journal_title

    # Identifiants
    doi = (raw.get("doi") or "").strip()
    if doi:
        item["DOI"] = doi

    # Sujets/topics → mots-clés (CORE expose parfois 'subjects' ou 'topics')
    topics = [t for t in (raw.get("topics") or raw.get("subjects") or []) if t]
    if topics:
        item["tags"] = make_tags([str(t) for t in topics])

    # URL : DOI prioritaire, sinon PDF de téléchargement
    pdf_url = raw.get("downloadUrl") or _first(raw.get("sourceFulltextUrls", [])) or ""
    item["url"] = (f"https://doi.org/{doi}" if doi else pdf_url)

    # === Traçabilité (extra) ===
    has_fulltext = bool(raw.get("fullText"))
    append_to_extra(item, "Sources", SOURCE_LABEL)
    if raw.get("id"):
        append_to_extra(item, "CORE ID", raw["id"])
    append_to_extra(item, "Open Access", "yes")
    append_to_extra(
        item, "Full text available",
        "yes" if has_fulltext else "metadata only",
    )
    if pdf_url:
        append_to_extra(item, "PDF", pdf_url)
    if doi:
        append_to_extra(item, "DOI", doi)

    return validate_and_clean(item)
