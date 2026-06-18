"""
Connecteur HAL : recherche par mot-clé via l'API Solr REST.

Utilise le mécanisme cursorMark pour la pagination profonde
(au-delà de 10 000 résultats, la pagination start/rows devient
instable sur Solr ; cursorMark est la solution recommandée).

Endpoint : https://api.archives-ouvertes.fr/search/
Documentation : https://api.archives-ouvertes.fr/docs/search
"""

import logging
from typing import List, Dict

from connectors.base import Connector
from mappers.hal_mapper import map_hal_to_zotero
from config import (
    HAL_SEARCH_URL, HAL_ROWS_PER_BATCH, HAL_FIELDS,
)

logger = logging.getLogger(__name__)


class HALConnector(Connector):
    """
    Connecteur pour HAL Archive ouverte.

    La requête utilisateur est interprétée par Solr selon
    sa syntaxe native : les phrases entre guillemets sont
    recherchées exactement, les opérateurs booléens AND/OR/NOT
    sont supportés, et les caractères spéciaux Solr doivent
    être échappés par l'utilisateur si nécessaire.
    """

    SOURCE_NAME = "HAL"

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: str = None,
        to_date: str = None,
    ) -> List[Dict]:
        """
        Recherche par mot-clé dans HAL avec pagination cursorMark.

        Args:
            query: Requête full-text. Exemples :
                - 'discours numérique'
                - '"intelligence artificielle" AND société'
                - 'communication politique -publicité'
            max_results: Plafond du nombre d'items retournés
            from_date: Date minimale (inclusive) au format YYYY-MM-DD
                ou YYYY. Filtre sur producedDate_tdate.
            to_date: Date maximale (inclusive) idem.

        Returns:
            Liste d'items Zotero
        """
        # Paramètres Solr
        # - q : requête full-text
        # - fl : liste des champs à retourner (limite le payload)
        # - wt : format JSON pour parsing direct
        # - rows : taille du lot (1000 est confortable)
        # - sort : obligatoire avec cursorMark, doit inclure un champ unique
        # - cursorMark : '*' pour le premier appel, puis nextCursorMark
        params = {
            "q": query,
            "fl": ",".join(HAL_FIELDS),
            "wt": "json",
            "rows": HAL_ROWS_PER_BATCH,
            "sort": "docid asc",
            "cursorMark": "*",
        }

        # Filtre temporel HAL : fq=producedDate_tdate:[FROM TO TO]
        if from_date or to_date:
            params["fq"] = self._build_date_filter(from_date, to_date)
            logger.info(f"[HAL] Filtre temporel : {params['fq']}")

        all_items = []
        batch_num = 0
        total_available = None

        while True:
            batch_num += 1
            logger.info(
                f"[HAL] Lot {batch_num} (cursorMark={params['cursorMark'][:20]}...)"
            )

            try:
                response = self.http.get(HAL_SEARCH_URL, params=params)
                data = response.json()
            except Exception as e:
                logger.error(f"[HAL] Échec du lot {batch_num}: {e}")
                break

            # Solr renvoie response.numFound (total) et response.docs (lot)
            response_block = data.get("response", {})
            if total_available is None:
                total_available = response_block.get("numFound", 0)
                logger.info(f"[HAL] {total_available} résultats disponibles au total")

            docs = response_block.get("docs", [])
            if not docs:
                logger.info(f"[HAL] Lot vide, fin de pagination")
                break

            # Mapping de chaque document vers Zotero
            for doc in docs:
                try:
                    item = map_hal_to_zotero(doc)
                    all_items.append(item)
                except Exception as e:
                    logger.warning(
                        f"[HAL] Échec mapping doc {doc.get('docid', '?')}: {e}"
                    )

                # Respect du plafond
                if len(all_items) >= max_results:
                    logger.info(
                        f"[HAL] Plafond {max_results} atteint, arrêt"
                    )
                    return all_items

            # Préparation du lot suivant via cursorMark
            next_cursor = data.get("nextCursorMark")
            if not next_cursor or next_cursor == params["cursorMark"]:
                logger.info(f"[HAL] Curseur stable, fin de pagination")
                break
            params["cursorMark"] = next_cursor

        logger.info(f"[HAL] Terminé : {len(all_items)} items collectés")
        return all_items

    @staticmethod
    def _build_date_filter(from_date: str = None, to_date: str = None) -> str:
        """
        Construit le filtre Solr fq= pour le champ producedDate_tdate.

        Args:
            from_date: Borne basse YYYY-MM-DD ou YYYY (None = *)
            to_date: Borne haute YYYY-MM-DD ou YYYY (None = NOW)

        Returns:
            Chaîne Solr fq, ex : 'producedDate_tdate:[2020-01-01T00:00:00Z TO 2023-12-31T23:59:59Z]'
        """
        def _normalize(d, is_end: bool) -> str:
            if not d:
                return "*" if not is_end else "NOW"
            # YYYY → étendre
            if len(d) == 4 and d.isdigit():
                return f"{d}-12-31T23:59:59Z" if is_end else f"{d}-01-01T00:00:00Z"
            # YYYY-MM-DD → étendre avec heure
            if len(d) == 10:
                return f"{d}T23:59:59Z" if is_end else f"{d}T00:00:00Z"
            return d

        lo = _normalize(from_date, is_end=False)
        hi = _normalize(to_date, is_end=True)
        return f"producedDate_tdate:[{lo} TO {hi}]"
