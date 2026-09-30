"""
Mapper HAL Solr JSON → Zotero JSON.

Transforme un document JSON tel que renvoyé par
l'API Solr de HAL en item Zotero conforme au schéma
Zotero JSON. Le mapping est conçu pour préserver le
maximum d'information ; les champs sans équivalent
direct sont migrés vers le champ 'extra'.

Référence champs HAL : https://api.archives-ouvertes.fr/docs/search
"""

from typing import Dict, List, Any, Optional

from mappers.zotero_schema import (
    make_empty_item, validate_and_clean, make_tags, append_to_extra,
)
from mappers.doc_type_mapping import map_hal_doctype
from utils.cleaner import clean_string, clean_list, parse_hal_author


def _pick(doc: Dict, *keys: str) -> Optional[Any]:
    """
    Renvoie la première valeur non vide trouvée parmi les clés.

    Utile pour gérer les variantes de champs HAL multilingues
    (fr_title_s vs en_title_s vs title_s).

    Args:
        doc: Document Solr
        *keys: Clés à tester par ordre de priorité

    Returns:
        Première valeur non vide, ou None
    """
    for key in keys:
        value = doc.get(key)
        if value:
            # Solr renvoie souvent des listes même pour des valeurs uniques
            if isinstance(value, list):
                non_empty = [v for v in value if v]
                if non_empty:
                    return non_empty[0]
            else:
                return value
    return None


def _join_pages(pages_field) -> str:
    """
    Formate le champ page_s qui peut être une chaîne ou liste.
    """
    if not pages_field:
        return ""
    if isinstance(pages_field, list):
        return clean_string(pages_field[0]) if pages_field else ""
    return clean_string(pages_field)


def _unique(values) -> List[str]:
    """Valeurs non vides, dédoublonnées dans l'ordre d'apparition."""
    seen, out = set(), []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def map_hal_to_zotero(doc: Dict) -> Dict:
    """
    Convertit un document HAL Solr en item Zotero.

    Args:
        doc: Dict tel que renvoyé par l'API HAL (un élément de
            response.docs)

    Returns:
        Item Zotero validé et nettoyé
    """
    # === Détermination de l'itemType ===
    doctype = doc.get("docType_s", "")
    item_type = map_hal_doctype(doctype)
    item = make_empty_item(item_type)

    # === Titre ===
    # Priorité : title_s (multilingue), puis fallback langues
    title = _pick(doc, "title_s", "fr_title_s", "en_title_s")
    item["title"] = clean_string(title) if title else ""

    # Sous-titre éventuel intégré au titre
    subtitle = _pick(doc, "subTitle_s")
    if subtitle:
        sub_clean = clean_string(subtitle)
        if sub_clean and sub_clean not in item["title"]:
            item["title"] = f"{item['title']} : {sub_clean}"

    # === Auteurs ===
    # HAL fournit authFullName_s en liste ordonnée
    authors = doc.get("authFullName_s", [])
    if isinstance(authors, str):
        authors = [authors]
    item["creators"] = []
    for author_name in authors:
        creator = parse_hal_author(author_name)
        if creator:
            item["creators"].append(creator)

    # === Résumé ===
    abstract = _pick(doc, "abstract_s", "fr_abstract_s", "en_abstract_s")
    if abstract:
        item["abstractNote"] = clean_string(abstract)

    # === Date ===
    # Priorité : producedDate_s (date de publication officielle)
    date_value = _pick(doc, "producedDate_s", "publicationDate_s", "submittedDate_s")
    if date_value:
        item["date"] = clean_string(date_value)

    # === Langue ===
    lang = doc.get("language_s")
    if lang:
        # HAL utilise des codes ISO 639-1 (fr, en, es...) en liste
        if isinstance(lang, list) and lang:
            item["language"] = lang[0]
        else:
            item["language"] = str(lang)

    # === Mots-clés (tags) ===
    keywords = doc.get("keyword_s", []) or doc.get("fr_keyword_s", []) or doc.get("en_keyword_s", [])
    if keywords:
        item["tags"] = make_tags(clean_list(keywords))

    # === URL canonique HAL ===
    uri = _pick(doc, "uri_s")
    if uri:
        item["url"] = clean_string(uri)

    # === DOI ===
    doi = _pick(doc, "doiId_s")
    if doi:
        item["DOI"] = clean_string(doi)

    # === Champs spécifiques par type ===

    if item_type == "journalArticle":
        # Revue
        journal = _pick(doc, "journalTitle_s")
        if journal:
            item["publicationTitle"] = clean_string(journal)
        # ISSN
        issn = _pick(doc, "journalIssn_s", "journalEissn_s", "issn_s")
        if issn:
            item["ISSN"] = clean_string(issn)
        # Volume, numéro, pages
        if doc.get("volume_s"):
            item["volume"] = _pick(doc, "volume_s") or ""
        if doc.get("issue_s"):
            item["issue"] = _pick(doc, "issue_s") or ""
        if doc.get("page_s"):
            item["pages"] = _join_pages(doc.get("page_s"))

    elif item_type == "bookSection":
        # Titre de l'ouvrage hôte
        book_title = _pick(doc, "bookTitle_s")
        if book_title:
            item["bookTitle"] = clean_string(book_title)
        # Éditeur commercial
        publisher = _pick(doc, "publisherImprint_s", "publisher_s")
        if publisher:
            item["publisher"] = clean_string(publisher)
        # Lieu
        place = _pick(doc, "publicationLocation_s")
        if place:
            item["place"] = clean_string(place)
        # ISBN
        isbn = _pick(doc, "isbn_s")
        if isbn:
            item["ISBN"] = clean_string(isbn)
        # Pages
        if doc.get("page_s"):
            item["pages"] = _join_pages(doc.get("page_s"))

    elif item_type == "book":
        publisher = _pick(doc, "publisherImprint_s", "publisher_s")
        if publisher:
            item["publisher"] = clean_string(publisher)
        place = _pick(doc, "publicationLocation_s")
        if place:
            item["place"] = clean_string(place)
        isbn = _pick(doc, "isbn_s")
        if isbn:
            item["ISBN"] = clean_string(isbn)

    elif item_type == "conferencePaper":
        conf = _pick(doc, "conferenceTitle_s")
        if conf:
            item["conferenceName"] = clean_string(conf)
        place = _pick(doc, "conferenceLocation_s")
        if place:
            item["place"] = clean_string(place)
        publisher = _pick(doc, "publisherImprint_s", "publisher_s")
        if publisher:
            item["publisher"] = clean_string(publisher)
        if doc.get("page_s"):
            item["pages"] = _join_pages(doc.get("page_s"))
        proceedings = _pick(doc, "bookTitle_s")
        if proceedings:
            item["proceedingsTitle"] = clean_string(proceedings)

    elif item_type == "thesis":
        # Distinction Thèse / HDR / Mémoire via docType_s
        if doctype.upper() == "HDR":
            item["thesisType"] = "Habilitation à diriger des recherches"
        elif doctype.upper() == "THESE":
            item["thesisType"] = "Thèse de doctorat"
        elif doctype.upper() == "MEM":
            item["thesisType"] = "Mémoire de master"
        # Institution
        inst = doc.get("instStructName_s") or doc.get("instStructAcronym_s")
        if inst:
            if isinstance(inst, list) and inst:
                item["university"] = clean_string(inst[0])
            else:
                item["university"] = clean_string(inst)

    elif item_type == "report":
        publisher = _pick(doc, "publisherImprint_s", "publisher_s")
        if publisher:
            item["institution"] = clean_string(publisher)

    # === Enrichissement de 'extra' ===

    # Source canonique
    append_to_extra(item, "Sources", "HAL")

    # HAL ID (identifiant pérenne)
    hal_id = _pick(doc, "halId_s")
    if hal_id:
        append_to_extra(item, "HAL ID", hal_id)

    # Domaine disciplinaire. HAL répète le code de niveau 0 devant chaque
    # code de niveau 1 (« 0.shs, 1.shs.info, 0.shs, 1.shs.scipo ») : valeurs
    # dédoublonnées dans l'ordre (retour d'expérience B9).
    domains = doc.get("domain_s") or doc.get("domainAllCode_s")
    if domains:
        if isinstance(domains, list):
            append_to_extra(item, "HAL Domain", ", ".join(_unique(domains)))
        else:
            append_to_extra(item, "HAL Domain", str(domains))

    # Structures de recherche (dédoublonnées de même)
    labs = doc.get("labStructAcronym_s") or doc.get("labStructName_s")
    if labs:
        if isinstance(labs, list):
            append_to_extra(item, "HAL Laboratory", ", ".join(_unique(labs)))
        else:
            append_to_extra(item, "HAL Laboratory", str(labs))

    # Accès libre
    open_access = doc.get("openAccess_bool")
    if open_access is not None:
        append_to_extra(item, "Open Access", "yes" if open_access else "no")

    # URL du fichier PDF si déposé
    pdf_url = _pick(doc, "fileMain_s")
    if pdf_url:
        append_to_extra(item, "PDF URL", pdf_url)

    # Date de dépôt sur HAL
    submitted = _pick(doc, "submittedDate_s")
    if submitted:
        append_to_extra(item, "Submitted Date", submitted)

    # Type de dépôt (file, notice, annex)
    submit_type = _pick(doc, "submitType_s")
    if submit_type:
        append_to_extra(item, "HAL Submit Type", submit_type)

    # ORCID des auteurs si disponibles
    orcids = doc.get("authOrcidId_s")
    if orcids:
        valid_orcids = [o for o in orcids if o and "0000" in str(o)]
        if valid_orcids:
            append_to_extra(item, "ORCID", ", ".join(valid_orcids))

    # === Validation finale ===
    return validate_and_clean(item)
