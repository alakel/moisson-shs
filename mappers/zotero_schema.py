"""
Constructeur et validateur d'items Zotero JSON.

Ce module définit le squelette d'item Zotero, les champs
autorisés par type, et propose une fonction de validation
qui retire les champs non valides afin d'éviter les rejets
lors de l'import dans Zotero.

Le schéma de référence est consultable à
https://api.zotero.org/schema.

Le format Zotero JSON utilisé est celui accepté par
l'interface "Importer un fichier" de Zotero Desktop,
qui correspond au format d'export "Zotero RDF" décliné
en JSON, distinct du CSL-JSON.
"""

from typing import Dict, List, Optional, Any
from copy import deepcopy


# === Champs autorisés par itemType ===
# Liste non exhaustive mais couvrant les itemType utilisés en SHS.
# Les champs hors de cette liste sont déplacés vers 'extra'.

ZOTERO_ITEM_FIELDS = {
    "journalArticle": {
        "title", "abstractNote", "publicationTitle", "volume", "issue",
        "pages", "date", "series", "seriesTitle", "seriesText",
        "journalAbbreviation", "language", "DOI", "ISSN", "shortTitle",
        "url", "accessDate", "archive", "archiveLocation", "libraryCatalog",
        "callNumber", "rights", "extra",
    },
    "bookSection": {
        "title", "abstractNote", "bookTitle", "series", "seriesNumber",
        "volume", "numberOfVolumes", "edition", "place", "publisher",
        "date", "pages", "language", "ISBN", "shortTitle", "url",
        "accessDate", "archive", "archiveLocation", "libraryCatalog",
        "callNumber", "rights", "extra",
    },
    "book": {
        "title", "abstractNote", "series", "seriesNumber", "volume",
        "numberOfVolumes", "edition", "place", "publisher", "date",
        "numPages", "language", "ISBN", "shortTitle", "url", "accessDate",
        "archive", "archiveLocation", "libraryCatalog", "callNumber",
        "rights", "extra",
    },
    "conferencePaper": {
        "title", "abstractNote", "date", "proceedingsTitle",
        "conferenceName", "place", "publisher", "volume", "pages",
        "series", "language", "DOI", "ISBN", "shortTitle", "url",
        "accessDate", "archive", "archiveLocation", "libraryCatalog",
        "callNumber", "rights", "extra",
    },
    "thesis": {
        "title", "abstractNote", "thesisType", "university", "place",
        "date", "numPages", "language", "shortTitle", "url", "accessDate",
        "archive", "archiveLocation", "libraryCatalog", "callNumber",
        "rights", "extra",
    },
    "report": {
        "title", "abstractNote", "reportNumber", "reportType",
        "seriesTitle", "place", "institution", "date", "pages",
        "language", "shortTitle", "url", "accessDate", "archive",
        "archiveLocation", "libraryCatalog", "callNumber", "rights",
        "extra",
    },
    "preprint": {
        "title", "abstractNote", "genre", "repository", "archiveID",
        "place", "date", "series", "seriesNumber", "DOI", "citationKey",
        "url", "accessDate", "archive", "archiveLocation", "libraryCatalog",
        "callNumber", "rights", "extra", "language", "shortTitle",
    },
    "dataset": {
        "title", "abstractNote", "identifier", "type", "versionNumber",
        "repository", "repositoryLocation", "format", "date", "DOI",
        "citationKey", "url", "accessDate", "archive", "archiveLocation",
        "libraryCatalog", "callNumber", "rights", "extra", "language",
        "shortTitle",
    },
    "blogPost": {
        "title", "abstractNote", "blogTitle", "websiteType", "date",
        "url", "accessDate", "language", "shortTitle", "rights", "extra",
    },
    "webpage": {
        "title", "abstractNote", "websiteTitle", "websiteType", "date",
        "shortTitle", "url", "accessDate", "language", "rights", "extra",
    },
    "encyclopediaArticle": {
        "title", "abstractNote", "encyclopediaTitle", "series",
        "seriesNumber", "volume", "numberOfVolumes", "edition", "place",
        "publisher", "date", "pages", "ISBN", "language", "shortTitle",
        "url", "accessDate", "archive", "archiveLocation", "libraryCatalog",
        "callNumber", "rights", "extra",
    },
    "magazineArticle": {
        "title", "abstractNote", "publicationTitle", "volume", "issue",
        "date", "pages", "language", "ISSN", "shortTitle", "url",
        "accessDate", "archive", "archiveLocation", "libraryCatalog",
        "callNumber", "rights", "extra",
    },
    "document": {
        "title", "abstractNote", "publisher", "date", "language",
        "shortTitle", "url", "accessDate", "archive", "archiveLocation",
        "libraryCatalog", "callNumber", "rights", "extra",
        # DOI est techniquement autorisé sur document dans le schéma Zotero
        # officiel récent (cf. https://api.zotero.org/schema). Sans cela,
        # beaucoup d'items SHS génériques perdent leur DOI à la migration
        # vers 'document'.
        "DOI",
    },
    "presentation": {
        "title", "abstractNote", "presentationType", "date", "place",
        "meetingName", "url", "accessDate", "language", "shortTitle",
        "rights", "extra",
    },
}


# Champs communs à tous les itemType
COMMON_FIELDS = {"itemType", "creators", "tags", "collections", "relations"}


def make_empty_item(item_type: str = "document") -> Dict:
    """
    Construit un item Zotero vide pour le type demandé.

    Args:
        item_type: itemType Zotero (défaut 'document')

    Returns:
        Dict avec tous les champs initialisés
    """
    item = {
        "itemType": item_type,
        "title": "",
        "creators": [],
        "abstractNote": "",
        "date": "",
        "language": "",
        "url": "",
        "extra": "",
        "tags": [],
        "collections": [],
        "relations": {},
    }
    return item


def validate_and_clean(item: Dict) -> Dict:
    """
    Filtre les champs incompatibles avec l'itemType déclaré
    et migre les champs orphelins vers 'extra'.

    Cette validation prévient les erreurs d'import Zotero qui
    rejette les items contenant des champs non autorisés pour
    leur type. Les valeurs orphelines sont préservées dans
    'extra' au format clé:valeur pour ne perdre aucune information.

    La liste de champs autorisés est résolue par le loader
    (schéma officiel téléchargé si possible, sinon table embarquée).

    Args:
        item: Item Zotero candidat

    Returns:
        Item Zotero validé
    """
    item_type = item.get("itemType", "document")

    # Import différé pour éviter une dépendance circulaire entre
    # zotero_schema.py et zotero_schema_loader.py
    try:
        from mappers.zotero_schema_loader import get_allowed_fields
        allowed_complete = get_allowed_fields(item_type)
    except Exception:
        # En cas d'erreur (réseau, import), fallback embarqué
        allowed = ZOTERO_ITEM_FIELDS.get(item_type, ZOTERO_ITEM_FIELDS["document"])
        allowed_complete = allowed | COMMON_FIELDS

    cleaned = {}
    extra_lines = []

    # Récupération des lignes 'extra' déjà présentes
    if item.get("extra"):
        extra_lines.extend(item["extra"].split("\n"))

    for key, value in item.items():
        if key == "extra":
            # Déjà traité ci-dessus
            continue
        if key in allowed_complete:
            cleaned[key] = value
        else:
            # Champ orphelin : conservation dans 'extra'
            if value and not isinstance(value, (list, dict)):
                extra_lines.append(f"{key}: {value}")
            elif isinstance(value, list) and value:
                # Liste non vide : sérialisation simple
                extra_lines.append(f"{key}: {', '.join(str(v) for v in value)}")

    # Reconstitution d'extra
    if extra_lines:
        # Déduplication des lignes 'extra' tout en préservant l'ordre
        seen = set()
        unique_lines = []
        for line in extra_lines:
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                unique_lines.append(line)
        cleaned["extra"] = "\n".join(unique_lines)
    else:
        cleaned["extra"] = ""

    # Garantie que les champs essentiels sont présents
    cleaned.setdefault("itemType", item_type)
    cleaned.setdefault("title", "")
    cleaned.setdefault("creators", [])
    cleaned.setdefault("tags", [])
    cleaned.setdefault("collections", [])
    cleaned.setdefault("relations", {})

    return cleaned


def make_tags(keywords: List[str]) -> List[Dict]:
    """
    Convertit une liste de mots-clés en format tags Zotero.

    Args:
        keywords: Liste de chaînes (mots-clés bruts)

    Returns:
        Liste de dicts {tag: ...}
    """
    seen = set()
    tags = []
    for kw in keywords:
        if not kw:
            continue
        kw_clean = kw.strip()
        if kw_clean and kw_clean.lower() not in seen:
            seen.add(kw_clean.lower())
            tags.append({"tag": kw_clean})
    return tags


def append_to_extra(item: Dict, key: str, value: Any) -> None:
    """
    Ajoute une paire clé:valeur dans le champ 'extra' Zotero.

    Modifie l'item en place. Pratique pour préserver des
    métadonnées qui n'ont pas d'équivalent dans le schéma
    Zotero (HAL ID, OAI Identifier, domaines disciplinaires).

    Args:
        item: Item Zotero
        key: Clé Zotero extra (ex : "HAL ID", "Sources")
        value: Valeur à ajouter
    """
    if not value:
        return
    current = item.get("extra", "")
    new_line = f"{key}: {value}"
    if current:
        item["extra"] = current + "\n" + new_line
    else:
        item["extra"] = new_line
