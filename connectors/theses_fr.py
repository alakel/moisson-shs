"""
Connecteur Theses.fr : proxy Isidore uniquement.

Theses.fr est la base nationale des thèses françaises (Sudoc/ABES).
En 2026, l'API REST publique historique a été retirée (404 sur
/api/v1/*) ; aucun endpoint OAI public n'est exposé directement.

Seule stratégie viable : proxy Isidore filtré par url_marker="theses.fr".
Isidore moissonne et indexe régulièrement Theses.fr, donc le proxy
couvre l'essentiel des thèses françaises soutenues.

Limitations :
- Pas d'enrichissement OAI possible (entrepôt OAI non public).
- L'URL primaire pointe vers la page de notice (souvent avec PDF
  attaché côté HAL/archive-ouverte pour les thèses en accès libre).
- Le mapping vers itemType=thesis est garanti par Isidore qui type
  ces items comme `doctoralThesis` ou `Thèse ou essai doctoral accepté`.
"""

import logging
from typing import List, Dict

from connectors.base import Connector
from connectors.isidore import IsidoreConnector
from config import THESES_FR_URL_MARKER

logger = logging.getLogger(__name__)


class ThesesFrConnector(Connector):
    """Connecteur Theses.fr via proxy Isidore."""

    SOURCE_NAME = "Theses.fr"

    def __init__(self, http_client=None, **kwargs):
        """
        Args:
            http_client: Client HTTP partagé
            **kwargs: absorbés (pas d'enrichissement OAI possible ici)
        """
        super().__init__(http_client, **kwargs)

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: str = None,
        to_date: str = None,
    ) -> List[Dict]:
        """
        Recherche Theses.fr via proxy Isidore.

        Args:
            query: Requête mot-clé
            max_results: Plafond du nombre d'items
            from_date: Date minimale YYYY ou YYYY-MM-DD
            to_date: Date maximale idem

        Returns:
            Liste d'items Zotero (généralement itemType=thesis)
        """
        logger.info(f"[Theses.fr] Démarrage via proxy Isidore: {query}")

        with IsidoreConnector(http_client=self.http) as isidore:
            items = isidore.search(
                query,
                max_results=max_results,
                url_marker=THESES_FR_URL_MARKER,
                from_date=from_date,
                to_date=to_date,
            )

        # Annotation source canonique
        for item in items:
            extra = item.get("extra", "")
            new_lines = []
            for line in extra.split("\n"):
                if line.startswith("Sources:"):
                    sources = [s.strip() for s in line[len("Sources:"):].split(",")]
                    if "Theses.fr" not in sources:
                        sources.insert(0, "Theses.fr")
                    new_lines.append("Sources: " + ", ".join(sources))
                else:
                    new_lines.append(line)
            item["extra"] = "\n".join(new_lines)
            # Annotation systématique : limitation de la moisson
            if "Full Text Available" not in item["extra"]:
                from mappers.zotero_schema import append_to_extra
                append_to_extra(
                    item, "Theses.fr Note",
                    "Pour le PDF, suivre l'URL ou chercher l'identifiant Sudoc",
                )

        logger.info(f"[Theses.fr] Terminé : {len(items)} items via Isidore")
        return items
