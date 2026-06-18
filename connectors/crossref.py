"""
Connecteur CrossRef — API REST native, sans authentification.

~160 M d'enregistrements ; recherche plein-texte sur les métadonnées via
``query.bibliographic``. Pas d'authentification : on rejoint le « polite pool »
(infrastructure plus stable) en déclarant un ``mailto``. Pagination profonde
par ``cursor=*`` (l'``offset`` est plafonné à 10 000).

Endpoint : https://api.crossref.org/works
Documentation : https://api.crossref.org/swagger-ui/index.html (vérifier annuellement)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from connectors.base import Connector
from config import CONTACT_EMAIL, CROSSREF_API
from mappers.crossref_mapper import map_crossref_item

logger = logging.getLogger(__name__)

# Champs demandés pour réduire la charge utile (select=). N'inclure QUE des
# champs valides pour la route /works : un champ inconnu fait échouer toute la
# requête en HTTP 400 (validation-failure). En particulier 'language' n'est PAS
# un champ « select » valide côté CrossRef (vérifié contre l'API) — il est donc
# absent ici ; la langue des items CrossRef reste vide (perte assumée, compensée
# par OpenAlex et la fusion FR/EN).
SELECT_FIELDS = ",".join([
    "DOI", "title", "author", "abstract", "container-title",
    "issued", "volume", "issue", "page", "ISSN", "ISBN", "type",
    "URL", "publisher", "subject", "is-referenced-by-count",
])

ROWS_PER_BATCH = 100  # max 1000 ; 100 limite la charge utile par page


def _as_date(value: str, start: bool) -> str:
    """Normalise 'YYYY' en 'YYYY-01-01' (début) ou 'YYYY-12-31' (fin)."""
    value = str(value).strip()
    if len(value) == 4 and value.isdigit():
        return f"{value}-01-01" if start else f"{value}-12-31"
    return value


class CrossrefConnector(Connector):
    """Connecteur pour CrossRef (recherche bibliographique plein-texte)."""

    SOURCE_NAME = "CrossRef"

    def __init__(self, http_client=None, contact_email: Optional[str] = None,
                 **kwargs):
        super().__init__(http_client=http_client, **kwargs)
        self.contact_email = contact_email or CONTACT_EMAIL
        self.headers = {
            "User-Agent": f"moisson-shs/1.0 (mailto:{self.contact_email})"
        }

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        **kwargs,
    ) -> List[Dict]:
        """Moissonne CrossRef pour ``query``. Retourne des items Zotero JSON."""
        items: List[Dict] = []
        cursor = "*"

        filters = []
        if from_date:
            filters.append(f"from-pub-date:{_as_date(from_date, start=True)}")
        if to_date:
            filters.append(f"until-pub-date:{_as_date(to_date, start=False)}")

        batch_num = 0
        while len(items) < max_results:
            batch_num += 1
            params = {
                "query.bibliographic": query,
                "rows": ROWS_PER_BATCH,
                "cursor": cursor,
                "select": SELECT_FIELDS,
                # Polite pool : identification par mailto
                "mailto": self.contact_email,
            }
            if filters:
                params["filter"] = ",".join(filters)

            try:
                response = self.http.get(
                    CROSSREF_API, params=params, headers=self.headers
                )
                message = response.json().get("message", {})
            except Exception as e:
                logger.error(f"[CrossRef] Échec du lot {batch_num} : {e}")
                break

            batch = message.get("items", [])
            if not batch:
                logger.info("[CrossRef] Lot vide, fin de pagination")
                break

            for raw in batch:
                try:
                    items.append(map_crossref_item(raw))
                except Exception as e:
                    logger.warning(
                        f"[CrossRef] Échec mapping {raw.get('DOI', '?')} : {e}"
                    )
                if len(items) >= max_results:
                    logger.info(f"[CrossRef] Plafond {max_results} atteint")
                    return items[:max_results]

            cursor = message.get("next-cursor")
            if not cursor:
                break
            logger.info(f"[CrossRef] {len(items)} items cumulés")

        logger.info(f"[CrossRef] Terminé : {len(items)} items collectés")
        return items[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    res = CrossrefConnector().search(
        "artificial intelligence regulation",
        max_results=20, from_date="2022", to_date="2026",
    )
    print(f"{len(res)} items")
    for it in res[:3]:
        print("-", it.get("title"), "|", it.get("DOI"))
