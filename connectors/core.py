"""
Connecteur CORE (api.core.ac.uk/v3) — clé Bearer requise.

~290 M de documents en accès ouvert, texte intégral. Authentification
``Authorization: Bearer {clé}`` (inscription gratuite ; délai de validation
possible). Quota propre au compte (en-têtes ``X-RateLimit-*``) : on espace les
appels. Pagination ``offset``+``limit`` plafonnée (~10 000) ; au-delà, l'API
expose ``scroll``/``scrollId`` (non requis pour des moissons ciblées).

La clé est lue via ``CORE_API_KEY``. La couche HTTP budget-aware
(``utils.http_scholarly.get_json``) gère 429/Retry-After et l'épuisement.

Base : https://api.core.ac.uk/v3
Documentation : https://api.core.ac.uk/docs/v3 (vérifier annuellement)
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from connectors.base import Connector
from config import CORE_SEARCH_WORKS, ENV_CORE_KEY, REQUEST_DELAY_CORE
from mappers.core_mapper import map_core_item
from utils.http_scholarly import BudgetExhausted, get_json
from utils.secrets import get_api_key

logger = logging.getLogger(__name__)

LIMIT = 100        # taille de page
OFFSET_CAP = 10000  # plafond offset CORE ; au-delà : scroll (hors périmètre)


class CoreConnector(Connector):
    """Connecteur pour CORE (recherche full-text OA, clé Bearer)."""

    SOURCE_NAME = "CORE"

    def __init__(self, http_client=None, api_key: Optional[str] = None,
                 min_interval: float = REQUEST_DELAY_CORE, **kwargs):
        super().__init__(http_client=http_client, **kwargs)
        # Clé requise ; absente → run_source capture et désactive la source.
        self.api_key = api_key or get_api_key(ENV_CORE_KEY, required=True)
        self.headers = {"Authorization": f"Bearer {self.api_key}"}
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
        """Moissonne CORE pour ``query``. Retourne des items Zotero JSON."""
        items: List[Dict] = []
        offset = 0

        q = query
        if from_date:
            q += f" AND yearPublished>={str(from_date)[:4]}"
        if to_date:
            q += f" AND yearPublished<={str(to_date)[:4]}"

        while len(items) < max_results and offset < OFFSET_CAP:
            params = {"q": q, "limit": LIMIT, "offset": offset}
            self._throttle()
            try:
                payload = get_json(
                    CORE_SEARCH_WORKS, params=params, headers=self.headers,
                    session=self.http.session, timeout=60,
                )
            except BudgetExhausted as e:
                logger.error(f"[CORE] Arrêt (quota épuisé) — {e}")
                break
            except Exception as e:
                logger.error(f"[CORE] Échec requête : {e}")
                break

            batch = payload.get("results") or []
            if not batch:
                break
            for raw in batch:
                try:
                    items.append(map_core_item(raw))
                except Exception as e:
                    logger.warning(f"[CORE] Échec mapping : {e}")
                if len(items) >= max_results:
                    return items[:max_results]

            offset += LIMIT
            logger.info(
                f"[CORE] {len(items)} items cumulés "
                f"(total annoncé {payload.get('totalHits')})"
            )

        logger.info(f"[CORE] Terminé : {len(items)} items collectés")
        return items[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    res = CoreConnector().search(
        "artificial intelligence regulation",
        max_results=30, from_date="2022", to_date="2026",
    )
    print(f"{len(res)} items")
    for it in res[:3]:
        print("-", it.get("title"), "|", it.get("DOI"))
