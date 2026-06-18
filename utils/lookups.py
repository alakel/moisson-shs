"""
Fonctions de recherche ciblée par identifiant ou auteur.

Ces fonctions sont utilisées par les outils MCP fetch_by_doi et
search_by_author. Elles orchestrent plusieurs connecteurs pour
maximiser la couverture et la richesse des métadonnées récupérées.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import requests

from utils.http import HTTPClient
from utils.deduplication import deduplicate
from connectors.hal import HALConnector
from connectors.isidore import IsidoreConnector
from connectors.openedition import OpenEditionConnector
from connectors.cairn import CairnConnector
from mappers.zotero_schema import (
    make_empty_item, validate_and_clean, make_tags, append_to_extra,
)
from mappers.doc_type_mapping import map_dublin_core_type
from mappers.openalex_mapper import map_openalex_item
from mappers.semantic_scholar_mapper import map_s2_item
from mappers.core_mapper import map_core_item
from utils.cleaner import clean_string
from utils.secrets import get_api_key
from config import (
    USER_AGENT, HTTP_TIMEOUT, CONTACT_EMAIL,
    OPENALEX_API, S2_GRAPH_API, CORE_SEARCH_WORKS,
    ENV_OPENALEX_KEY, ENV_S2_KEY, ENV_CORE_KEY,
)

logger = logging.getLogger(__name__)

# Champs S2 demandés pour fetch_by_doi / search_by_author (cohérent avec le
# connecteur Semantic Scholar).
_S2_FIELDS = (
    "title,abstract,year,publicationDate,authors,externalIds,citationCount,"
    "influentialCitationCount,publicationTypes,publicationVenue,venue,journal,"
    "openAccessPdf,s2FieldsOfStudy,fieldsOfStudy,url"
)
_OPENALEX_SELECT = (
    "id,doi,title,display_name,publication_year,publication_date,language,"
    "type,authorships,primary_location,abstract_inverted_index,concepts,"
    "cited_by_count,open_access,ids,biblio"
)


# === fetch_by_doi ===

def fetch_by_doi(doi: str, http_client: HTTPClient = None) -> Optional[Dict]:
    """
    Récupère un item Zotero complet à partir d'un DOI.

    Stratégie :
    1. Crossref API → métadonnées principales fiables
    2. HAL Solr (filtre doiId_s) → enrichissement si HAL dépôt
    3. Fusion par deduplicate

    Args:
        doi: DOI canonique (ex : 10.3917/comla.204.0045)
        http_client: Client HTTP partagé optionnel

    Returns:
        Item Zotero unique fusionné ou None si rien trouvé
    """
    doi = _normalize_doi(doi)
    if not doi:
        return None

    own_http = http_client is None
    if own_http:
        http_client = HTTPClient()

    items = []
    try:
        # 1) Crossref — source de vérité pour les métadonnées de citation
        cr_item = _fetch_from_crossref(doi)
        if cr_item:
            items.append(cr_item)

        # 2) HAL — peut contenir un dépôt PDF en accès libre
        hal_item = _fetch_from_hal(doi, http_client)
        if hal_item:
            items.append(hal_item)

        # 3) Isidore — enrichissement supplémentaire
        iso_item = _fetch_from_isidore(doi, http_client)
        if iso_item:
            items.append(iso_item)

        # 4) Sources internationales : compte de citations + statut OA
        #    (best-effort ; chacune se désactive proprement sans clé/erreur)
        for resolver in (_fetch_from_openalex, _fetch_from_s2, _fetch_from_core):
            intl_item = resolver(doi)
            if intl_item:
                items.append(intl_item)

    finally:
        if own_http:
            http_client.close()

    if not items:
        return None

    # Fusion via la déduplication standard
    merged = deduplicate(items)
    return merged[0] if merged else None


def _normalize_doi(doi: str) -> str:
    """Normalise un DOI : retire les préfixes URL et casse."""
    if not doi:
        return ""
    doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
    doi = doi.strip().lower()
    return doi if doi.startswith("10.") else ""


def _fetch_from_crossref(doi: str) -> Optional[Dict]:
    """
    Crossref REST API : métadonnées canoniques pour un DOI.

    Cf. https://api.crossref.org/works/{doi}
    """
    try:
        r = requests.get(
            f"https://api.crossref.org/works/{doi}",
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return None
        data = r.json().get("message", {})
    except Exception as e:
        logger.warning(f"Crossref échec sur {doi} : {e}")
        return None

    # Mapping Crossref → Zotero
    cr_type = data.get("type", "")
    item_type_map = {
        "journal-article": "journalArticle",
        "book-chapter": "bookSection",
        "book": "book",
        "proceedings-article": "conferencePaper",
        "report": "report",
        "posted-content": "preprint",
        "dissertation": "thesis",
    }
    item = make_empty_item(item_type_map.get(cr_type, "document"))

    # Titre
    titles = data.get("title", [])
    if isinstance(titles, list) and titles:
        item["title"] = clean_string(titles[0])

    # Auteurs
    for author in data.get("author", []):
        family = author.get("family", "").strip()
        given = author.get("given", "").strip()
        if family or given:
            item["creators"].append({
                "creatorType": "author",
                "firstName": given,
                "lastName": family,
            })

    # Date
    issued = data.get("issued", {}).get("date-parts", [])
    if issued and isinstance(issued[0], list):
        parts = issued[0]
        if len(parts) >= 3:
            item["date"] = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
        elif len(parts) >= 2:
            item["date"] = f"{parts[0]:04d}-{parts[1]:02d}"
        elif parts:
            item["date"] = str(parts[0])

    # Container
    ct = data.get("container-title", [])
    if isinstance(ct, list) and ct:
        if item["itemType"] == "journalArticle":
            item["publicationTitle"] = clean_string(ct[0])
        elif item["itemType"] == "bookSection":
            item["bookTitle"] = clean_string(ct[0])

    # Volume / Issue / Pages
    if data.get("volume"):
        item["volume"] = str(data["volume"])
    if data.get("issue"):
        item["issue"] = str(data["issue"])
    if data.get("page"):
        item["pages"] = str(data["page"])

    # ISSN / ISBN
    issn_list = data.get("ISSN", [])
    if isinstance(issn_list, list) and issn_list:
        item["ISSN"] = issn_list[0]
    isbn_list = data.get("ISBN", [])
    if isinstance(isbn_list, list) and isbn_list:
        item["ISBN"] = isbn_list[0]

    # Publisher
    if data.get("publisher"):
        item["publisher"] = clean_string(data["publisher"])

    # Abstract
    if data.get("abstract"):
        item["abstractNote"] = clean_string(data["abstract"])

    # Language
    if data.get("language"):
        item["language"] = data["language"]

    # DOI et URL
    item["DOI"] = doi
    if data.get("URL"):
        item["url"] = data["URL"]

    # Subject → tags
    subjects = data.get("subject", [])
    if subjects:
        item["tags"] = make_tags(subjects)

    # Source (libellé canonique aligné sur mappers/crossref_mapper.SOURCE_LABEL)
    append_to_extra(item, "Sources", "CrossRef")
    return validate_and_clean(item)


def _fetch_from_hal(doi: str, http_client: HTTPClient) -> Optional[Dict]:
    """Recherche un DOI dans HAL par filtre doiId_s."""
    try:
        hal = HALConnector(http_client=http_client)
        # Requête ciblée : pas une recherche full-text mais un filtre
        items = hal.search(f'doiId_s:"{doi}"', max_results=1)
        return items[0] if items else None
    except Exception as e:
        logger.warning(f"HAL échec sur {doi} : {e}")
        return None


def _fetch_from_isidore(doi: str, http_client: HTTPClient) -> Optional[Dict]:
    """Recherche un DOI dans Isidore (full-text + filtrage par DOI)."""
    try:
        iso = IsidoreConnector(http_client=http_client)
        items = iso.search(doi, max_results=5)
        # Garder uniquement les items dont le DOI matche exactement
        target = doi.lower()
        for item in items:
            if (item.get("DOI") or "").lower() == target:
                return item
            if target in (item.get("url") or "").lower():
                return item
    except Exception as e:
        logger.warning(f"Isidore échec sur {doi} : {e}")
    return None


def _fetch_from_openalex(doi: str) -> Optional[Dict]:
    """OpenAlex : Work par DOI (compte de citations, concepts, OA). Best-effort."""
    api_key = get_api_key(ENV_OPENALEX_KEY, required=False)
    if not api_key:
        # OpenAlex exige une clé depuis fév. 2026 : on s'abstient sans clé.
        return None
    try:
        r = requests.get(
            f"{OPENALEX_API}/doi:{doi}",
            params={"api_key": api_key, "select": _OPENALEX_SELECT},
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return None
        return map_openalex_item(r.json())
    except Exception as e:
        logger.warning(f"OpenAlex échec sur {doi} : {e}")
        return None


def _fetch_from_s2(doi: str) -> Optional[Dict]:
    """Semantic Scholar : paper par DOI (citations influentes). Best-effort."""
    api_key = get_api_key(ENV_S2_KEY, required=False)
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        r = requests.get(
            f"{S2_GRAPH_API}/paper/DOI:{doi}",
            params={"fields": _S2_FIELDS},
            timeout=HTTP_TIMEOUT,
            headers=headers,
        )
        if r.status_code != 200:
            return None
        return map_s2_item(r.json())
    except Exception as e:
        logger.warning(f"Semantic Scholar échec sur {doi} : {e}")
        return None


def _fetch_from_core(doi: str) -> Optional[Dict]:
    """CORE : recherche par DOI (texte intégral / PDF OA). Best-effort."""
    api_key = get_api_key(ENV_CORE_KEY, required=False)
    if not api_key:
        return None
    try:
        r = requests.get(
            CORE_SEARCH_WORKS,
            params={"q": f'doi:"{doi}"', "limit": 1},
            timeout=HTTP_TIMEOUT,
            headers={"Authorization": f"Bearer {api_key}",
                     "User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
        return map_core_item(results[0]) if results else None
    except Exception as e:
        logger.warning(f"CORE échec sur {doi} : {e}")
        return None


# === search_by_author ===

def search_by_author(
    name: str,
    idhal: str = None,
    orcid: str = None,
    max_per_source: int = 200,
    http_client: HTTPClient = None,
) -> List[Dict]:
    """
    Recherche la production complète d'un auteur sur les sources principales.

    Stratégie :
    - HAL : requête sur authIdHal_s (si fourni), sinon authFullName_s
    - Isidore : recherche full-text sur le nom (couvre OpenEdition/Cairn/Persée/Érudit)
    - Crossref (optionnel) si ORCID fourni

    Args:
        name: Nom complet (ex : "Patrick Charaudeau") — utilisé en fallback
        idhal: Identifiant HAL author (ex : "patrick-charaudeau") — précis
        orcid: ORCID (ex : "0000-0002-1234-5678") — précis
        max_per_source: Plafond par source
        http_client: Client HTTP partagé

    Returns:
        Liste d'items Zotero dédupliquée, triée par année décroissante
    """
    own_http = http_client is None
    if own_http:
        http_client = HTTPClient()

    all_items: List[Dict] = []
    try:
        # HAL : requête Solr ciblée
        hal_query = _build_hal_author_query(name, idhal, orcid)
        if hal_query:
            try:
                hal = HALConnector(http_client=http_client)
                items = hal.search(hal_query, max_results=max_per_source)
                logger.info(f"[search_by_author] HAL : {len(items)} items")
                all_items.extend(items)
            except Exception as e:
                logger.warning(f"HAL author search échec : {e}")

        # Isidore : recherche full-text sur le nom (entre guillemets pour
        # l'exactitude). Couvre indirectement OpenEdition/Cairn/Persée/Érudit.
        if name:
            try:
                iso = IsidoreConnector(http_client=http_client)
                items = iso.search(f'"{name}"', max_results=max_per_source)
                logger.info(f"[search_by_author] Isidore : {len(items)} items")
                all_items.extend(items)
            except Exception as e:
                logger.warning(f"Isidore author search échec : {e}")

        # Couverture internationale (best-effort) : OpenAlex (désambiguïsation
        # par author.id) et Semantic Scholar (endpoint auteur). Alimente la
        # skill ProfilBiblio pour les chercheurs non francophones.
        if name:
            oa_items = _search_openalex_author(name, max_per_source, orcid)
            if oa_items:
                logger.info(f"[search_by_author] OpenAlex : {len(oa_items)} items")
                all_items.extend(oa_items)
            s2_items = _search_s2_author(name, max_per_source)
            if s2_items:
                logger.info(f"[search_by_author] S2 : {len(s2_items)} items")
                all_items.extend(s2_items)

    finally:
        if own_http:
            http_client.close()

    # Déduplication
    deduplicated = deduplicate(all_items)

    # Tri par année décroissante (les plus récents en premier)
    def _year_key(item):
        date = item.get("date") or ""
        import re
        m = re.search(r"\b(\d{4})\b", date)
        return -int(m.group(1)) if m else 0
    deduplicated.sort(key=_year_key)

    logger.info(
        f"[search_by_author] {name}: {len(all_items)} bruts → "
        f"{len(deduplicated)} uniques"
    )
    return deduplicated


def _build_hal_author_query(
    name: str, idhal: Optional[str], orcid: Optional[str]
) -> str:
    """Construit la requête Solr HAL la plus précise possible."""
    if idhal:
        return f'authIdHal_s:"{idhal}"'
    if orcid:
        return f'authOrcidId_s:"{orcid}"'
    if name:
        return f'authFullName_s:"{name}"'
    return ""


def _search_openalex_author(
    name: str, max_per_source: int, orcid: Optional[str] = None
) -> List[Dict]:
    """
    Recherche la production d'un auteur sur OpenAlex (best-effort).

    Désambiguïsation : ORCID si fourni (filtre direct), sinon recherche par nom
    puis sélection du meilleur auteur (``/authors``) et de ses works
    (``authorships.author.id``). Désactivé proprement sans clé.
    """
    api_key = get_api_key(ENV_OPENALEX_KEY, required=False)
    if not api_key:
        return None
    base_params = {"api_key": api_key}
    try:
        author_filter = None
        if orcid:
            author_filter = f"author.orcid:{orcid}"
        else:
            ra = requests.get(
                "https://api.openalex.org/authors",
                params={**base_params, "search": name, "per-page": 1},
                timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT},
            )
            if ra.status_code == 200:
                results = (ra.json() or {}).get("results") or []
                if results and results[0].get("id"):
                    author_filter = f"authorships.author.id:{results[0]['id']}"
        if not author_filter:
            return None

        rw = requests.get(
            OPENALEX_API,
            params={
                **base_params,
                "filter": author_filter,
                "per-page": min(200, max(1, max_per_source)),
                "select": _OPENALEX_SELECT,
                "sort": "publication_date:desc",
            },
            timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT},
        )
        if rw.status_code != 200:
            return None
        works = (rw.json() or {}).get("results") or []
        return [map_openalex_item(w) for w in works[:max_per_source]]
    except Exception as e:
        logger.warning(f"OpenAlex author search échec ({name}) : {e}")
        return None


def _search_s2_author(name: str, max_per_source: int) -> List[Dict]:
    """
    Recherche la production d'un auteur sur Semantic Scholar (best-effort).

    Recherche d'auteur (``/author/search``) puis ses papers
    (``/author/{id}/papers``). Fonctionne sans clé (pool partagé).
    """
    headers = {"User-Agent": USER_AGENT}
    api_key = get_api_key(ENV_S2_KEY, required=False)
    if api_key:
        headers["x-api-key"] = api_key
    try:
        rs = requests.get(
            f"{S2_GRAPH_API}/author/search",
            params={"query": name, "fields": "name", "limit": 1},
            timeout=HTTP_TIMEOUT, headers=headers,
        )
        if rs.status_code != 200:
            return None
        data = (rs.json() or {}).get("data") or []
        if not data or not data[0].get("authorId"):
            return None
        author_id = data[0]["authorId"]

        rp = requests.get(
            f"{S2_GRAPH_API}/author/{author_id}/papers",
            params={"fields": _S2_FIELDS, "limit": min(100, max(1, max_per_source))},
            timeout=HTTP_TIMEOUT, headers=headers,
        )
        if rp.status_code != 200:
            return None
        papers = (rp.json() or {}).get("data") or []
        return [map_s2_item(p) for p in papers[:max_per_source]]
    except Exception as e:
        logger.warning(f"Semantic Scholar author search échec ({name}) : {e}")
        return None
