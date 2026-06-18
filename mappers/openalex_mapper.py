"""
Mapper OpenAlex Work → item Zotero JSON.

Spécificités OpenAlex :
- les résumés ne sont pas servis en clair (licence) mais via un index inversé
  ``abstract_inverted_index`` ``{token: [positions]}`` qu'on recompose ;
- les ``concepts`` (avec score) deviennent des mots-clés au-dessus d'un seuil ;
- ``cited_by_count``, statut OA et identifiants natifs sont tracés dans ``extra``
  (réimportés par Zotero dans son champ ``extra``).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from config import OPENALEX_CONCEPT_SCORE_MIN
from mappers.scholarly_types import openalex_type_to_zotero
from mappers.zotero_schema import (
    append_to_extra, make_empty_item, make_tags, validate_and_clean,
)
from utils.names import split_display_name

SOURCE_LABEL = "OpenAlex"


def reconstruct_abstract(inverted_index: Optional[Dict]) -> str:
    """
    Recompose un résumé depuis l'index inversé OpenAlex ``{token: [positions]}``.

    Les positions absentes/dupliquées sont tolérées : on ordonne par position
    croissante et on joint les tokens.
    """
    if not inverted_index:
        return ""
    positions: List = []
    for token, idxs in inverted_index.items():
        for i in idxs or []:
            positions.append((i, token))
    if not positions:
        return ""
    positions.sort(key=lambda p: p[0])
    return " ".join(tok for _, tok in positions)


def _pages(biblio: Dict) -> str:
    fp, lp = biblio.get("first_page"), biblio.get("last_page")
    if fp and lp:
        return f"{fp}-{lp}"
    return str(fp or lp or "")


def _clean_doi(doi: Optional[str]) -> str:
    return (doi or "").replace("https://doi.org/", "").replace(
        "http://doi.org/", "").strip()


def map_openalex_item(raw: Dict) -> Dict:
    """Convertit un Work OpenAlex (un élément de ``results``) en item Zotero."""
    item_type = openalex_type_to_zotero(raw.get("type", ""))
    item = make_empty_item(item_type)

    # Titre
    item["title"] = raw.get("title") or raw.get("display_name") or ""

    # Auteurs (display_name → split heuristique)
    creators: List[Dict] = []
    for au in raw.get("authorships", []) or []:
        name = (au.get("author") or {}).get("display_name", "")
        first, last = split_display_name(name)
        if first or last:
            creators.append({
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            })
    item["creators"] = creators

    # Résumé recomposé
    abstract = reconstruct_abstract(raw.get("abstract_inverted_index"))
    if abstract:
        item["abstractNote"] = abstract

    # Date
    date = raw.get("publication_date") or str(raw.get("publication_year") or "")
    if date:
        item["date"] = date

    # Langue
    if raw.get("language"):
        item["language"] = raw["language"]

    # Conteneur (revue / source primaire)
    loc = raw.get("primary_location") or {}
    source = loc.get("source") or {}
    biblio = raw.get("biblio") or {}
    if source.get("display_name"):
        if item_type == "journalArticle":
            item["publicationTitle"] = source["display_name"]
        elif item_type == "conferencePaper":
            item["proceedingsTitle"] = source["display_name"]
        elif item_type == "bookSection":
            item["bookTitle"] = source["display_name"]
        else:
            item["publicationTitle"] = source["display_name"]

    if biblio.get("volume"):
        item["volume"] = str(biblio["volume"])
    if biblio.get("issue"):
        item["issue"] = str(biblio["issue"])
    pages = _pages(biblio)
    if pages:
        item["pages"] = pages

    # ISSN
    issn_l = source.get("issn_l")
    issn_list = source.get("issn") or []
    issn = issn_l or (issn_list[0] if issn_list else "")
    if issn:
        item["ISSN"] = issn

    # Identifiants
    doi = _clean_doi(raw.get("doi"))
    if doi:
        item["DOI"] = doi

    # Concepts au-dessus du seuil → mots-clés
    concepts = [
        c.get("display_name", "")
        for c in (raw.get("concepts") or [])
        if c.get("display_name")
        and c.get("score", 0) >= OPENALEX_CONCEPT_SCORE_MIN
    ]
    if concepts:
        item["tags"] = make_tags(concepts)

    # URL : landing page, sinon URL OA, sinon l'ID OpenAlex
    oa = raw.get("open_access") or {}
    item["url"] = (
        loc.get("landing_page_url") or oa.get("oa_url") or raw.get("id", "")
    )

    # === Traçabilité (extra) ===
    append_to_extra(item, "Sources", SOURCE_LABEL)
    if raw.get("id"):
        append_to_extra(item, "OpenAlex ID", raw["id"])
    # str() pour préserver la valeur 0 (un compte nul est significatif et
    # serait sinon écarté par le garde `if not value` de append_to_extra).
    if raw.get("cited_by_count") is not None:
        append_to_extra(item, "Cited by", str(raw["cited_by_count"]))
    append_to_extra(item, "Open Access", "yes" if oa.get("is_oa") else "no")
    ids = raw.get("ids") or {}
    if ids.get("pmid"):
        append_to_extra(item, "PMID", ids["pmid"])
    if doi:
        append_to_extra(item, "DOI", doi)
    if concepts:
        append_to_extra(item, "OpenAlex concepts", ", ".join(concepts))

    return validate_and_clean(item)
