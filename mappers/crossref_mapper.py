"""
Mapper CrossRef JSON → item Zotero JSON.

CrossRef renvoie les résumés en JATS XML (balises ``<jats:p>`` etc.) quand ils
sont présents : on les nettoie. Les dates arrivent sous forme ``date-parts``
``[[Y, M, D]]``. Le nom de source ("CrossRef") et le DOI sont tracés pour la
déduplication multi-sources.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from mappers.scholarly_types import crossref_type_to_zotero
from mappers.zotero_schema import (
    append_to_extra, make_empty_item, make_tags, validate_and_clean,
)

SOURCE_LABEL = "CrossRef"


def _strip_jats(text: str) -> str:
    """Retire les balises JATS/HTML d'un résumé et normalise les espaces."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _first(seq) -> Optional[Any]:
    """Premier élément non vide d'une liste, sinon None."""
    if isinstance(seq, list):
        for v in seq:
            if v:
                return v
        return None
    return seq or None


def date_from_parts(issued: Optional[Dict]) -> str:
    """Convertit ``issued.date-parts`` ``[[Y, M, D]]`` en ``YYYY[-MM[-DD]]``."""
    parts = (issued or {}).get("date-parts") or [[]]
    dp = parts[0] if parts and isinstance(parts[0], list) else []
    # CrossRef renvoie parfois [[None]] pour une date inconnue
    dp = [p for p in dp if isinstance(p, int)]
    if not dp:
        return ""
    y = dp[0]
    m = dp[1] if len(dp) > 1 else None
    d = dp[2] if len(dp) > 2 else None
    if m and d:
        return f"{y:04d}-{m:02d}-{d:02d}"
    if m:
        return f"{y:04d}-{m:02d}"
    return f"{y:04d}"


def map_crossref_item(raw: Dict) -> Dict:
    """Convertit un work CrossRef (un élément de ``message.items``) en item Zotero."""
    item_type = crossref_type_to_zotero(raw.get("type", ""))
    item = make_empty_item(item_type)

    # Titre (liste)
    item["title"] = _strip_jats(_first(raw.get("title", [])) or "")

    # Auteurs : CrossRef fournit given/family (pas besoin de split heuristique)
    creators: List[Dict] = []
    for a in raw.get("author", []) or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = (a.get("name") or "").strip()  # auteurs institutionnels
        if family or given:
            creators.append({
                "creatorType": "author",
                "firstName": given,
                "lastName": family,
            })
        elif name:
            creators.append({"creatorType": "author", "name": name})
    item["creators"] = creators

    # Résumé (JATS)
    abstract = _strip_jats(raw.get("abstract", "") or "")
    if abstract:
        item["abstractNote"] = abstract

    # Date
    date = date_from_parts(raw.get("issued"))
    if date:
        item["date"] = date

    # Langue
    if raw.get("language"):
        item["language"] = raw["language"]

    # Conteneur selon le type
    container = _first(raw.get("container-title", []))
    if container:
        if item_type == "journalArticle":
            item["publicationTitle"] = container
        elif item_type == "conferencePaper":
            item["proceedingsTitle"] = container
        elif item_type == "bookSection":
            item["bookTitle"] = container
        else:
            item["publicationTitle"] = container

    # Numérotation
    if raw.get("volume"):
        item["volume"] = str(raw["volume"])
    if raw.get("issue"):
        item["issue"] = str(raw["issue"])
    if raw.get("page"):
        item["pages"] = str(raw["page"])

    # Éditeur
    if raw.get("publisher"):
        item["publisher"] = raw["publisher"]

    # Identifiants
    doi = (raw.get("DOI") or "").strip()
    if doi:
        item["DOI"] = doi
    issn = _first(raw.get("ISSN", []))
    if issn:
        item["ISSN"] = issn
    isbn = _first(raw.get("ISBN", []))
    if isbn:
        item["ISBN"] = isbn

    # URL
    if raw.get("URL"):
        item["url"] = raw["URL"]

    # Sujets → tags
    subjects = [s for s in (raw.get("subject") or []) if s]
    if subjects:
        item["tags"] = make_tags(subjects)

    # === Traçabilité (extra) ===
    append_to_extra(item, "Sources", SOURCE_LABEL)
    if doi:
        append_to_extra(item, "DOI", doi)
    # Statut OA non fourni par CrossRef
    append_to_extra(item, "Open Access", "unknown")
    refs = raw.get("is-referenced-by-count")
    if isinstance(refs, int):
        # str() pour préserver la valeur 0 (garde `if not value` du helper).
        append_to_extra(item, "Cited by", str(refs))

    return validate_and_clean(item)
