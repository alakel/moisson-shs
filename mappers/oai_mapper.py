"""
Mapper Dublin Core (OAI-PMH) → Zotero JSON.

Utilisé pour parser les enregistrements OAI moissonnés depuis
les entrepôts d'OpenEdition et de Cairn. Les formats supportés
sont oai_dc (Dublin Core simple) et qdc (Qualified Dublin Core).

Le module sickle convertit les enregistrements XML en
objets Record dont l'attribut 'metadata' est un dict mappant
chaque champ Dublin Core à une liste de chaînes.
"""

from typing import Dict, List, Any, Optional

from mappers.zotero_schema import (
    make_empty_item, validate_and_clean, make_tags, append_to_extra,
)
from mappers.doc_type_mapping import map_dublin_core_types_list
from utils.cleaner import (
    clean_string, clean_list, split_authors, extract_year,
)


def _first(metadata: Dict, key: str) -> str:
    """
    Renvoie la première valeur non vide d'un champ DC,
    ou chaîne vide.
    """
    values = metadata.get(key, [])
    if isinstance(values, str):
        return values
    if isinstance(values, list):
        for v in values:
            if v and str(v).strip():
                return str(v)
    return ""


def _all(metadata: Dict, key: str) -> List[str]:
    """
    Renvoie toutes les valeurs d'un champ DC en liste.
    """
    values = metadata.get(key, [])
    if isinstance(values, str):
        return [values] if values.strip() else []
    if isinstance(values, list):
        return [str(v) for v in values if v and str(v).strip()]
    return []


def map_oai_dc_to_zotero(
    metadata: Dict,
    oai_identifier: str = "",
    source_name: str = "OAI",
) -> Dict:
    """
    Convertit un enregistrement OAI Dublin Core en item Zotero.

    Args:
        metadata: Dict du record OAI (sickle .metadata)
        oai_identifier: Identifiant OAI canonique du record
        source_name: Nom de la plateforme source ("OpenEdition", "Cairn")

    Returns:
        Item Zotero validé
    """
    # === Détermination de l'itemType ===
    dc_types = _all(metadata, "type")
    item_type = map_dublin_core_types_list(dc_types)
    item = make_empty_item(item_type)

    # === Titre ===
    title = _first(metadata, "title")
    if title:
        item["title"] = clean_string(title)

    # === Auteurs ===
    # En Dublin Core, dc:creator peut contenir plusieurs auteurs
    # ou un seul. Chaque valeur peut elle-même être 'Nom, Prénom'
    creator_strings = _all(metadata, "creator")
    for creator_str in creator_strings:
        item["creators"].extend(split_authors(creator_str))

    # Contributeurs (éditeurs, traducteurs)
    contributor_strings = _all(metadata, "contributor")
    for contrib_str in contributor_strings:
        # Conservation comme contributeur
        for c in split_authors(contrib_str):
            c["creatorType"] = "contributor"
            item["creators"].append(c)

    # === Résumé ===
    # dc:description peut contenir le résumé ou des notes diverses ;
    # on prend le premier descripteur non trivial
    descriptions = _all(metadata, "description")
    if descriptions:
        # Le premier descripteur est généralement le résumé
        item["abstractNote"] = clean_string(descriptions[0])

    # === Date ===
    dates = _all(metadata, "date")
    if dates:
        # Recherche d'une date avec année à 4 chiffres
        for d in dates:
            if extract_year(d):
                item["date"] = clean_string(d)
                break
        if not item["date"]:
            item["date"] = clean_string(dates[0])

    # === Langue ===
    lang = _first(metadata, "language")
    if lang:
        item["language"] = clean_string(lang)

    # === Mots-clés ===
    subjects = _all(metadata, "subject")
    if subjects:
        item["tags"] = make_tags(clean_list(subjects))

    # === URL ===
    # dc:identifier contient souvent l'URL canonique
    identifiers = _all(metadata, "identifier")
    url = ""
    doi = ""
    for ident in identifiers:
        ident_lower = ident.lower()
        if ident_lower.startswith("http") and not url:
            url = ident
        if "doi.org" in ident_lower or ident_lower.startswith("10."):
            doi = ident
    if url:
        item["url"] = clean_string(url)
    if doi:
        # Normalisation du DOI : retrait du préfixe URL
        doi_clean = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
        item["DOI"] = clean_string(doi_clean)

    # === Éditeur ===
    publisher = _first(metadata, "publisher")
    if publisher:
        if item_type in ("book", "bookSection", "report", "conferencePaper"):
            item["publisher"] = clean_string(publisher)

    # === Droits ===
    rights = _first(metadata, "rights")
    if rights:
        item["rights"] = clean_string(rights)

    # === Champs spécifiques aux articles de revue ===
    if item_type == "journalArticle":
        # dc:source contient parfois le titre de la revue
        source_field = _first(metadata, "source")
        if source_field:
            item["publicationTitle"] = clean_string(source_field)

        # Le format dc:identifier peut contenir l'ISSN
        for ident in identifiers:
            if "issn" in ident.lower():
                # Extrait l'ISSN du format 'issn:xxxx-xxxx' ou similaire
                import re
                match = re.search(r"\b(\d{4}-\d{3}[0-9Xx])\b", ident)
                if match:
                    item["ISSN"] = match.group(1)
                    break

    # === Enrichissement de 'extra' ===

    # Source canonique
    append_to_extra(item, "Sources", source_name)

    # Identifiant OAI
    if oai_identifier:
        append_to_extra(item, "OAI Identifier", oai_identifier)

    # Conservation des types DC qui n'ont pas pu être mappés
    if dc_types:
        types_str = ", ".join(dc_types)
        append_to_extra(item, "DC Types", types_str)

    # Format(s) du document
    formats = _all(metadata, "format")
    if formats:
        append_to_extra(item, "DC Formats", ", ".join(formats))

    # Couverture spatio-temporelle
    coverage = _all(metadata, "coverage")
    if coverage:
        append_to_extra(item, "DC Coverage", ", ".join(coverage))

    # Relations (autres versions, traductions, etc.)
    relations = _all(metadata, "relation")
    if relations:
        # On limite à 5 pour ne pas saturer 'extra'
        rel_str = ", ".join(relations[:5])
        append_to_extra(item, "DC Relations", rel_str)

    # === Validation finale ===
    return validate_and_clean(item)


def map_qdc_to_zotero(
    metadata: Dict,
    oai_identifier: str = "",
    source_name: str = "OpenEdition",
) -> Dict:
    """
    Convertit un enregistrement OAI Qualified Dublin Core (qdc)
    en item Zotero.

    Le format qdc utilisé par OpenEdition fournit des champs
    supplémentaires : dcterms:bibliographicCitation, dcterms:isPartOf,
    dcterms:alternative, dcterms:abstract, etc.

    Args:
        metadata: Dict du record qdc
        oai_identifier: Identifiant OAI
        source_name: Nom de la plateforme source

    Returns:
        Item Zotero validé
    """
    # Le mapping qdc commence comme oai_dc puis ajoute les enrichissements
    item = map_oai_dc_to_zotero(metadata, oai_identifier, source_name)

    # === Enrichissements qdc / dcterms ===

    # Résumé spécifique dcterms:abstract (plus fiable que dc:description)
    abstract = _first(metadata, "abstract")
    if abstract:
        item["abstractNote"] = clean_string(abstract)

    # Titre alternatif (traduction du titre)
    alternatives = _all(metadata, "alternative")
    if alternatives:
        append_to_extra(item, "Alternative Titles", "; ".join(alternatives))

    # Citation bibliographique
    citation = _first(metadata, "bibliographicCitation")
    if citation:
        append_to_extra(item, "Bibliographic Citation", citation)

    # Élément parent (revue, ouvrage)
    is_part_of = _first(metadata, "isPartOf")
    if is_part_of:
        if item["itemType"] == "journalArticle" and not item.get("publicationTitle"):
            item["publicationTitle"] = clean_string(is_part_of)
        elif item["itemType"] == "bookSection" and not item.get("bookTitle"):
            item["bookTitle"] = clean_string(is_part_of)
        else:
            append_to_extra(item, "Is Part Of", is_part_of)

    # Date de création / publication précise
    created = _first(metadata, "created")
    if created and not item.get("date"):
        item["date"] = clean_string(created)

    # Droits d'accès (open / restricted / embargo)
    access_rights = _first(metadata, "accessRights")
    if access_rights:
        access_label = (
            "Open Access" if "openAccess" in access_rights
            else "Restricted" if "restrictedAccess" in access_rights
            else "Embargoed" if "embargoedAccess" in access_rights
            else access_rights
        )
        append_to_extra(item, "Access Rights", access_label)

    return item
