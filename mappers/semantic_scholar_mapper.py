"""
Mapper Semantic Scholar (paper) → item Zotero JSON.

Déduplication par ``externalIds.DOI`` sinon ``externalIds.CorpusId``. Les
métriques propres à S2 (``citationCount``, ``influentialCitationCount``,
``s2FieldsOfStudy``) et l'identifiant CorpusId sont tracés dans ``extra``.
"""

from __future__ import annotations

from typing import Dict, List

from mappers.scholarly_types import s2_type_to_zotero
from mappers.zotero_schema import (
    append_to_extra, make_empty_item, make_tags, validate_and_clean,
)
from utils.names import split_display_name

SOURCE_LABEL = "Semantic Scholar"


def map_s2_item(raw: Dict) -> Dict:
    """Convertit un paper S2 (un élément de ``data``) en item Zotero."""
    pub_types = raw.get("publicationTypes") or []
    item_type = s2_type_to_zotero(pub_types)
    item = make_empty_item(item_type)

    # Titre
    item["title"] = raw.get("title") or ""

    # Auteurs (name → split heuristique)
    creators: List[Dict] = []
    for a in raw.get("authors", []) or []:
        first, last = split_display_name(a.get("name", ""))
        if first or last:
            creators.append({
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            })
    item["creators"] = creators

    # Résumé (servi en clair par S2 quand disponible)
    if raw.get("abstract"):
        item["abstractNote"] = raw["abstract"]

    # Date
    date = raw.get("publicationDate") or str(raw.get("year") or "")
    if date:
        item["date"] = date

    # Conteneur (venue / journal)
    venue_obj = raw.get("publicationVenue") or {}
    journal = raw.get("journal") or {}
    pub_title = (
        venue_obj.get("name") or journal.get("name") or raw.get("venue", "") or ""
    )
    if pub_title:
        if item_type == "conferencePaper":
            item["proceedingsTitle"] = pub_title
        elif item_type == "bookSection":
            item["bookTitle"] = pub_title
        else:
            item["publicationTitle"] = pub_title

    if journal.get("volume"):
        item["volume"] = str(journal["volume"]).strip()
    if journal.get("pages"):
        item["pages"] = str(journal["pages"]).strip()

    # ISSN (parfois fourni comme chaîne sur la venue)
    issn = venue_obj.get("issn")
    if isinstance(issn, str) and issn:
        item["ISSN"] = issn

    # Identifiants
    ext = raw.get("externalIds") or {}
    doi = (ext.get("DOI") or "").strip()
    if doi:
        item["DOI"] = doi

    # Champs d'étude → mots-clés
    fields = sorted({
        f.get("category") for f in (raw.get("s2FieldsOfStudy") or [])
        if f.get("category")
    })
    if not fields:
        fields = [f for f in (raw.get("fieldsOfStudy") or []) if f]
    if fields:
        item["tags"] = make_tags(fields)

    # URL : PDF OA, sinon DOI, sinon page S2
    oa = raw.get("openAccessPdf") or {}
    item["url"] = (
        oa.get("url")
        or (f"https://doi.org/{doi}" if doi else "")
        or raw.get("url", "")
    )

    # === Traçabilité (extra) ===
    append_to_extra(item, "Sources", SOURCE_LABEL)
    if ext.get("CorpusId"):
        append_to_extra(item, "S2 corpus id", ext["CorpusId"])
    # str() pour préserver la valeur 0 (cf. garde `if not value` du helper).
    if raw.get("citationCount") is not None:
        append_to_extra(item, "Cited by", str(raw["citationCount"]))
    if raw.get("influentialCitationCount") is not None:
        append_to_extra(
            item, "Influential citations",
            str(raw["influentialCitationCount"]),
        )
    append_to_extra(item, "Open Access", "yes" if oa.get("url") else "unknown")
    if ext.get("ArXiv"):
        append_to_extra(item, "arXiv", ext["ArXiv"])
    if doi:
        append_to_extra(item, "DOI", doi)
    if fields:
        append_to_extra(item, "S2 fields", ", ".join(fields))

    return validate_and_clean(item)
