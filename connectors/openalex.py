"""
Connecteur OpenAlex — API REST native, clé gratuite requise.

Depuis février 2026, OpenAlex impose une clé API gratuite et un budget
quotidien en USD (clé gratuite : ~1 $/j ; 100 req/s). Recherche plein-texte via
``?search=`` ; pagination ``cursor=*`` → ``meta.next_cursor``. Métadonnées
riches : concepts, ``cited_by_count``, statut OA, identifiants natifs.

La clé (``OPENALEX_API_KEY``) est **requise** : ``__init__`` lève ``MissingAPIKey``
en son absence, et ``main.run_source`` capture l'exception pour désactiver
proprement la source (la moisson des autres sources se poursuit). La couche HTTP
budget-aware (``utils.http_scholarly.get_json``) lit l'en-tête de solde et
distingue throttling transitoire vs épuisement de budget (``BudgetExhausted``).

Endpoint : https://api.openalex.org/works
Documentation : https://docs.openalex.org (vérifier annuellement)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from connectors.base import Connector
from config import ENV_OPENALEX_KEY, OPENALEX_API
from mappers.openalex_mapper import map_openalex_item
from utils.http_scholarly import BudgetExhausted, get_json
from utils.secrets import get_api_key

logger = logging.getLogger(__name__)

SELECT_FIELDS = ",".join([
    "id", "doi", "title", "display_name", "publication_year",
    "publication_date", "language", "type", "authorships",
    "primary_location", "abstract_inverted_index", "concepts",
    "cited_by_count", "open_access", "ids", "biblio",
])

PER_PAGE = 100  # max 200 ; 100 garde un coût lisible


def _year(value) -> str:
    s = str(value).strip()
    return s[:4] if len(s) >= 4 and s[:4].isdigit() else ""


class OpenAlexConnector(Connector):
    """Connecteur pour OpenAlex (recherche scholarly + concepts + citations)."""

    SOURCE_NAME = "OpenAlex"

    def __init__(self, http_client=None, api_key: Optional[str] = None, **kwargs):
        super().__init__(http_client=http_client, **kwargs)
        # Clé obligatoire depuis fév. 2026 ; en cas d'absence, run_source
        # capture l'exception et désactive proprement la source.
        self.api_key = api_key or get_api_key(ENV_OPENALEX_KEY, required=True)

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        extra_filters: Optional[str] = None,
        **kwargs,
    ) -> List[Dict]:
        """
        Moissonne OpenAlex.

        Args:
            extra_filters: filtres OpenAlex additionnels
                (ex : "type:article,language:en,is_oa:true").
        """
        items: List[Dict] = []
        cursor = "*"

        filters = []
        lo = _year(from_date) if from_date else ""
        hi = _year(to_date) if to_date else ""
        if lo and hi:
            filters.append(f"publication_year:{lo}-{hi}")
        elif lo:
            filters.append(f"from_publication_date:{lo}-01-01")
        elif hi:
            filters.append(f"to_publication_date:{hi}-12-31")
        if extra_filters:
            filters.append(extra_filters)

        while len(items) < max_results:
            params = {
                "search": query,
                "per-page": PER_PAGE,
                "cursor": cursor,
                "select": SELECT_FIELDS,
            }
            if self.api_key:
                params["api_key"] = self.api_key
            if filters:
                params["filter"] = ",".join(filters)

            try:
                data = get_json(
                    OPENALEX_API, params=params, session=self.http.session
                )
            except BudgetExhausted as e:
                logger.error(f"[OpenAlex] Arrêt (budget épuisé) — {e}")
                break
            except Exception as e:
                logger.error(f"[OpenAlex] Échec requête : {e}")
                break

            batch = data.get("results", [])
            if not batch:
                break
            for raw in batch:
                try:
                    items.append(map_openalex_item(raw))
                except Exception as e:
                    logger.warning(
                        f"[OpenAlex] Échec mapping {raw.get('id', '?')} : {e}"
                    )
                if len(items) >= max_results:
                    return items[:max_results]

            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
            logger.info(f"[OpenAlex] {len(items)} items cumulés")

        logger.info(f"[OpenAlex] Terminé : {len(items)} items collectés")
        return items[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    res = OpenAlexConnector().search(
        "artificial intelligence regulation discourse",
        max_results=50, from_date="2022", to_date="2026",
        extra_filters="type:article",
    )
    print(f"{len(res)} items")
    for it in res[:3]:
        print("-", it.get("title"), "|", it.get("DOI"))
