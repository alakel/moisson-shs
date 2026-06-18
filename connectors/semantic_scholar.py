"""
Connecteur Semantic Scholar (Academic Graph) — clé optionnelle (recommandée).

Avec clé : 1 req/s dédiée. Sans clé : pool partagé throttlé. Moissonnage en
volume via l'endpoint *bulk* (``/paper/search/bulk`` : ≤ 1000 résultats par
appel, jeton de continuation ``token``). Déduplication par ``externalIds.DOI``
sinon ``externalIds.CorpusId``.

La clé est lue via ``SEMANTIC_SCHOLAR_API_KEY`` (optionnelle).

Base : https://api.semanticscholar.org/graph/v1
Documentation : https://api.semanticscholar.org (vérifier annuellement)
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from connectors.base import Connector
from config import ENV_S2_KEY, REQUEST_DELAY_S2, S2_GRAPH_API
from mappers.semantic_scholar_mapper import map_s2_item
from utils.secrets import get_api_key

logger = logging.getLogger(__name__)

S2_BULK = f"{S2_GRAPH_API}/paper/search/bulk"

FIELDS = ",".join([
    "title", "abstract", "year", "publicationDate", "authors",
    "externalIds", "citationCount", "influentialCitationCount",
    "publicationTypes", "publicationVenue", "venue", "journal",
    "openAccessPdf", "s2FieldsOfStudy", "fieldsOfStudy", "url",
])


def _year_range(from_date, to_date) -> str:
    lo = str(from_date)[:4] if from_date else ""
    hi = str(to_date)[:4] if to_date else ""
    if lo and hi:
        return f"{lo}-{hi}"
    if lo:
        return f"{lo}-"
    if hi:
        return f"-{hi}"
    return ""


class SemanticScholarConnector(Connector):
    """Connecteur pour Semantic Scholar (bulk search throttlé à 1 req/s)."""

    SOURCE_NAME = "Semantic Scholar"

    def __init__(self, http_client=None, api_key: Optional[str] = None,
                 min_interval: float = REQUEST_DELAY_S2, **kwargs):
        super().__init__(http_client=http_client, **kwargs)
        # Clé optionnelle : si absente, on reste sur le pool partagé.
        self.api_key = api_key or get_api_key(ENV_S2_KEY, required=False)
        self.headers = {"x-api-key": self.api_key} if self.api_key else None
        self.min_interval = min_interval
        self._last = 0.0

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        **kwargs,
    ) -> List[Dict]:
        """Moissonne Semantic Scholar en mode bulk. Retourne des items Zotero."""
        items: List[Dict] = []
        token = None
        year = _year_range(from_date, to_date)

        while len(items) < max_results:
            params = {"query": query, "fields": FIELDS}
            if year:
                params["year"] = year
            if token:
                params["token"] = token

            self._throttle()
            try:
                response = self.http.get(
                    S2_BULK, params=params, headers=self.headers, timeout=60
                )
                payload = response.json()
            except Exception as e:
                logger.error(f"[S2] Échec requête : {e}")
                break

            batch = payload.get("data") or []
            if not batch:
                break
            for raw in batch:
                try:
                    items.append(map_s2_item(raw))
                except Exception as e:
                    logger.warning(f"[S2] Échec mapping : {e}")
                if len(items) >= max_results:
                    return items[:max_results]

            token = payload.get("token")
            logger.info(f"[S2] {len(items)} items cumulés")
            if not token:
                break

        logger.info(f"[S2] Terminé : {len(items)} items collectés")
        return items[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    res = SemanticScholarConnector().search(
        "large language models discourse",
        max_results=50, from_date="2022", to_date="2026",
    )
    print(f"{len(res)} items")
    for it in res[:3]:
        print("-", it.get("title"), "|", it.get("DOI"))
