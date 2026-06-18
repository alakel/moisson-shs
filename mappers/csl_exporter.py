"""
Export du corpus au format CSL-JSON (Citation Style Language JSON).

CSL-JSON est le format JSON bibliographique standard supporté nativement
par Zotero Desktop (translator « CSL JSON »), Pandoc, citeproc-py et la
plupart des outils de citation. C'est le format JSON privilégié pour
l'import dans Zotero (le format interne « Zotero API JSON » de notre
moissonneur n'est PAS reconnu par l'interface Import de Zotero Desktop).

Avantages CSL-JSON :
- Import direct dans Zotero (Fichier > Importer > corpus.csl.json)
- Lisible et exploitable par les LLMs (Claude via MCP, ChatGPT, etc.)
- Standard W3C-like maintenu par citeproc.org
- Format compact et structuré (pas de XML, pas de namespace)

Référence : https://docs.citationstyles.org/en/stable/specification.html
Schéma : https://github.com/citation-style-language/schema/blob/master/csl-data.json

Mapping itemType Zotero ↔ type CSL :
    journalArticle    → article-journal
    bookSection       → chapter
    book              → book
    conferencePaper   → paper-conference
    thesis            → thesis
    report            → report
    preprint          → article  (manuscript dans certains usages)
    blogPost          → post-weblog
    webpage           → webpage
    magazineArticle   → article-magazine
    encyclopediaArticle → entry-encyclopedia
    document          → document
    presentation      → speech
    artwork           → graphic
    audioRecording    → song
    videoRecording    → motion_picture
    map               → map
    computerProgram   → software
    patent            → patent
"""

import re
from typing import Dict, List, Any, Optional


# Table de correspondance itemType Zotero → type CSL
ZOTERO_TO_CSL_TYPE = {
    "journalArticle": "article-journal",
    "bookSection": "chapter",
    "book": "book",
    "conferencePaper": "paper-conference",
    "thesis": "thesis",
    "report": "report",
    "preprint": "article",
    "dataset": "dataset",
    "blogPost": "post-weblog",
    "webpage": "webpage",
    "magazineArticle": "article-magazine",
    "newspaperArticle": "article-newspaper",
    "encyclopediaArticle": "entry-encyclopedia",
    "dictionaryEntry": "entry-dictionary",
    "document": "document",
    "presentation": "speech",
    "artwork": "graphic",
    "audioRecording": "song",
    "videoRecording": "motion_picture",
    "map": "map",
    "computerProgram": "software",
    "patent": "patent",
    "manuscript": "manuscript",
    "letter": "personal_communication",
    "interview": "interview",
    "case": "legal_case",
    "bill": "bill",
    "statute": "legislation",
}


# Mapping creatorType Zotero → champ CSL
ZOTERO_TO_CSL_CREATOR = {
    "author": "author",
    "editor": "editor",
    "translator": "translator",
    "seriesEditor": "collection-editor",
    "bookAuthor": "container-author",
    "reviewedAuthor": "reviewed-author",
    "contributor": "author",   # CSL n'a pas "contributor", on rabat sur author
    "interviewer": "interviewer",
    "interviewee": "author",
    "director": "director",
    "producer": "producer",
    "composer": "composer",
    "presenter": "author",
    "cartographer": "author",
    "inventor": "author",
    "wordsBy": "author",
    "performer": "performer",
}


def _parse_date_to_parts(date_str: str) -> Optional[Dict[str, Any]]:
    """
    Convertit une chaîne de date en structure CSL `date-parts`.

    CSL-JSON encode les dates ainsi :
        {"date-parts": [[2020, 5, 15]]}        # 15 mai 2020
        {"date-parts": [[2020, 5]]}            # mai 2020
        {"date-parts": [[2020]]}               # 2020
        {"date-parts": [[2020], [2021]]}       # range 2020-2021
        {"literal": "circa 1850"}              # date imprécise

    Args:
        date_str: Chaîne de date d'entrée

    Returns:
        Dict CSL date ou None si non parsable
    """
    if not date_str:
        return None
    date_str = str(date_str).strip()
    if not date_str:
        return None

    # Format ISO YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", date_str)
    if m:
        return {"date-parts": [[int(m.group(1)), int(m.group(2)), int(m.group(3))]]}

    # Format YYYY-MM
    m = re.match(r"^(\d{4})-(\d{2})$", date_str)
    if m:
        return {"date-parts": [[int(m.group(1)), int(m.group(2))]]}

    # Format YYYY uniquement (ou année trouvée dans une chaîne libre)
    m = re.search(r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b", date_str)
    if m:
        return {"date-parts": [[int(m.group(1))]]}

    # Date littérale non parsable : conservation telle quelle
    return {"literal": date_str}


def _convert_creators(creators: List[Dict]) -> Dict[str, List[Dict]]:
    """
    Groupe les creators Zotero par type CSL.

    Args:
        creators: Liste de dicts Zotero {creatorType, firstName, lastName, name}

    Returns:
        Dict {csl_creator_type: [list of {family, given} or {literal}]}
    """
    grouped: Dict[str, List[Dict]] = {}
    for c in creators:
        ctype = c.get("creatorType", "author")
        csl_type = ZOTERO_TO_CSL_CREATOR.get(ctype, "author")

        if c.get("name"):
            # Auteur institutionnel ou nom unique non décomposé
            entry = {"literal": c["name"]}
        else:
            entry = {}
            if c.get("lastName"):
                entry["family"] = c["lastName"]
            if c.get("firstName"):
                entry["given"] = c["firstName"]
            if not entry:
                continue

        grouped.setdefault(csl_type, []).append(entry)

    return grouped


def _convert_tags(tags: List[Dict]) -> List[str]:
    """
    Convertit les tags Zotero en liste plate de chaînes pour CSL `keyword`.

    Args:
        tags: Liste de dicts {tag: "..."}

    Returns:
        Liste de chaînes de mots-clés
    """
    keywords = []
    for t in tags:
        if isinstance(t, dict):
            kw = t.get("tag")
            if kw:
                keywords.append(str(kw))
        elif isinstance(t, str) and t:
            keywords.append(t)
    return keywords


def zotero_to_csl(item: Dict) -> Dict:
    """
    Convertit un item Zotero (format interne moissonneur) en item CSL-JSON.

    Args:
        item: Item Zotero JSON tel que produit par les mappers HAL/Isidore/OAI

    Returns:
        Item CSL-JSON conforme à csl-data.json schema
    """
    item_type = item.get("itemType", "document")
    csl_type = ZOTERO_TO_CSL_TYPE.get(item_type, "document")

    csl: Dict[str, Any] = {
        "type": csl_type,
    }

    # === Identifiant CSL ===
    # Préférer le DOI, sinon HAL ID ou OAI ID extrait de 'extra', sinon
    # un identifiant synthétique stable basé sur le titre + auteur
    csl_id = _build_csl_id(item)
    if csl_id:
        csl["id"] = csl_id

    # === Champs texte directs ===
    if item.get("title"):
        csl["title"] = item["title"]
    if item.get("shortTitle"):
        csl["title-short"] = item["shortTitle"]
    if item.get("abstractNote"):
        csl["abstract"] = item["abstractNote"]
    if item.get("language"):
        csl["language"] = item["language"]
    if item.get("rights"):
        csl["license"] = item["rights"]

    # === Auteurs et autres contributeurs ===
    creators_grouped = _convert_creators(item.get("creators", []))
    csl.update(creators_grouped)

    # === Date ===
    date_parts = _parse_date_to_parts(item.get("date", ""))
    if date_parts:
        csl["issued"] = date_parts

    # === Container (revue, livre hôte) ===
    if item.get("publicationTitle"):
        csl["container-title"] = item["publicationTitle"]
    elif item.get("bookTitle"):
        csl["container-title"] = item["bookTitle"]
    elif item.get("proceedingsTitle"):
        csl["container-title"] = item["proceedingsTitle"]
    elif item.get("encyclopediaTitle"):
        csl["container-title"] = item["encyclopediaTitle"]
    elif item.get("websiteTitle"):
        csl["container-title"] = item["websiteTitle"]
    elif item.get("blogTitle"):
        csl["container-title"] = item["blogTitle"]

    # Abréviation conteneur (rare)
    if item.get("journalAbbreviation"):
        csl["container-title-short"] = item["journalAbbreviation"]

    # === Numérotation ===
    if item.get("volume"):
        csl["volume"] = item["volume"]
    if item.get("issue"):
        csl["issue"] = item["issue"]
    if item.get("pages"):
        csl["page"] = item["pages"]
    if item.get("numPages"):
        csl["number-of-pages"] = item["numPages"]
    if item.get("numberOfVolumes"):
        csl["number-of-volumes"] = item["numberOfVolumes"]
    if item.get("edition"):
        csl["edition"] = item["edition"]
    if item.get("series"):
        csl["collection-title"] = item["series"]
    if item.get("seriesTitle"):
        csl["collection-title"] = item["seriesTitle"]
    if item.get("seriesNumber"):
        csl["collection-number"] = item["seriesNumber"]

    # === Éditeur et lieu ===
    if item.get("publisher"):
        csl["publisher"] = item["publisher"]
    if item.get("place"):
        csl["publisher-place"] = item["place"]
    if item.get("institution"):
        csl["publisher"] = item["institution"]
    if item.get("university"):
        csl["publisher"] = item["university"]

    # === Identifiants standardisés ===
    if item.get("DOI"):
        csl["DOI"] = item["DOI"]
    if item.get("ISBN"):
        csl["ISBN"] = item["ISBN"]
    if item.get("ISSN"):
        csl["ISSN"] = item["ISSN"]
    if item.get("url"):
        csl["URL"] = item["url"]
    if item.get("accessDate"):
        accessed = _parse_date_to_parts(item["accessDate"])
        if accessed:
            csl["accessed"] = accessed
    if item.get("callNumber"):
        csl["call-number"] = item["callNumber"]
    if item.get("archive"):
        csl["archive"] = item["archive"]
    if item.get("archiveLocation"):
        csl["archive_location"] = item["archiveLocation"]
    if item.get("libraryCatalog"):
        csl["source"] = item["libraryCatalog"]

    # === Champs spécifiques ===
    # Conference
    if item.get("conferenceName"):
        csl["event-title"] = item["conferenceName"]
    if item.get("meetingName"):
        csl["event-title"] = item["meetingName"]
    # Thesis
    if item.get("thesisType"):
        csl["genre"] = item["thesisType"]
    # Report
    if item.get("reportType"):
        csl["genre"] = item["reportType"]
    if item.get("reportNumber"):
        csl["number"] = item["reportNumber"]
    # Preprint
    if item.get("genre"):
        csl["genre"] = item["genre"]
    if item.get("repository"):
        csl["archive"] = item["repository"]
    if item.get("archiveID"):
        csl["archive_location"] = item["archiveID"]

    # === Mots-clés ===
    keywords = _convert_tags(item.get("tags", []))
    if keywords:
        csl["keyword"] = ", ".join(keywords)

    # === Note (champ 'extra' Zotero préservé intégralement) ===
    # 'extra' contient les sources, HAL ID, OAI ID, ORCID, etc.
    # Le translator « CSL JSON » de Zotero ré-importera ces lignes
    # dans le champ 'extra' Zotero correctement.
    if item.get("extra"):
        csl["note"] = item["extra"]

    return csl


def _build_csl_id(item: Dict) -> str:
    """
    Construit un identifiant stable pour l'item CSL.

    Priorité : DOI > HAL ID > OAI ID > Isidore URI > synthétique.

    Args:
        item: Item Zotero

    Returns:
        Chaîne d'identifiant non vide
    """
    # 1) DOI
    doi = item.get("DOI", "").strip()
    if doi:
        return doi

    # 2) Identifiants dans 'extra'
    extra = item.get("extra", "")
    for line in extra.split("\n"):
        line = line.strip()
        for prefix in ("HAL ID:", "OAI Identifier:", "Isidore URI:"):
            if line.startswith(prefix):
                value = line[len(prefix):].strip()
                if value:
                    return value

    # 3) URL nettoyée
    url = item.get("url", "").strip()
    if url:
        return url

    # 4) Synthétique : titre tronqué + premier auteur
    title = (item.get("title", "") or "").strip()
    title_part = re.sub(r"[^a-zA-Z0-9]+", "_", title)[:30].lower().strip("_")
    creators = item.get("creators", [])
    author_part = ""
    if creators:
        c = creators[0]
        author_part = (c.get("lastName") or c.get("name") or "").lower()
        author_part = re.sub(r"[^a-z0-9]+", "", author_part)
    if title_part or author_part:
        return f"{author_part}_{title_part}".strip("_") or "item"
    return "item"


def export_corpus_to_csl(items: List[Dict]) -> List[Dict]:
    """
    Convertit un corpus Zotero complet en corpus CSL-JSON.

    Args:
        items: Liste d'items Zotero JSON

    Returns:
        Liste d'items CSL-JSON prêts à sérialiser
    """
    csl_items = []
    seen_ids: Dict[str, int] = {}
    for item in items:
        try:
            csl = zotero_to_csl(item)
            # Garantir l'unicité des identifiants CSL (Zotero refuse les doublons)
            base_id = csl.get("id", "item")
            count = seen_ids.get(base_id, 0)
            if count > 0:
                csl["id"] = f"{base_id}_{count}"
            seen_ids[base_id] = count + 1
            csl_items.append(csl)
        except Exception as e:
            # Item malformé : on log mais on ne plante pas la conversion globale
            import logging
            logging.getLogger(__name__).warning(
                f"Échec conversion CSL d'un item : {e}"
            )
    return csl_items
