"""
Connecteur Isidore : recherche par mot-clé via l'API REST JSON.

Endpoint : https://api.isidore.science/resource/search
Documentation : https://isidore.science/api

La structure JSON renvoyée (2026) est la suivante :

    response
    ├── header
    │   ├── query                # paramètres et metadata de la requête
    │   └── performance.@durationMs
    └── replies
        ├── meta
        │   ├── @items           # nombre total disponible
        │   └── @pageItems       # nombre dans cette page
        └── content
            └── reply            # LISTE d'items
                └── [item]
                    ├── @uri
                    └── isidore
                        ├── title             # liste multilingue [{@xml:lang, $}]
                        ├── url               # URL primaire
                        ├── enrichedCreators
                        │   └── creator       # liste ou dict unique
                        ├── date
                        │   ├── @startDate
                        │   ├── normalizedDate
                        │   └── year
                        ├── types.type        # liste ou string
                        ├── source            # objet avec name multilingue
                        │   └── name [{@xml:lang, $}]
                        ├── ore.aggregates    # URLs alternatives
                        ├── abstract          # liste multilingue
                        ├── subject           # liste de mots-clés enrichis
                        └── language

La pagination utilise `page=` et `replies=` (max 100 par page).

Le filtrage par source originelle peut se faire de deux façons :
- par NOM (source_filter="HAL SHS...") — peu fiable car Isidore indexe par revue
  individuelle plutôt que par agrégateur
- par DOMAINE D'URL (url_marker="openedition.org") — utilisé par les connecteurs
  OpenEdition et Cairn comme stratégie proxy.
"""

import logging
import math
from typing import List, Dict, Optional, Any

from connectors.base import Connector
from mappers.isidore_mapper import map_isidore_to_zotero
from config import (
    ISIDORE_SEARCH_URL, ISIDORE_REPLIES_PER_PAGE,
)

logger = logging.getLogger(__name__)


class IsidoreConnector(Connector):
    """
    Connecteur pour Isidore (moteur de recherche SHS Huma-Num).

    Supporte deux types de filtrage post-pagination pour permettre
    aux connecteurs OpenEdition et Cairn d'utiliser Isidore comme
    proxy de recherche full-text :
    - source_filter : par nom de source littéral (fallback)
    - url_marker : par domaine d'URL (méthode privilégiée)
    """

    SOURCE_NAME = "Isidore"

    def search(
        self,
        query: str,
        max_results: int = 1000,
        source_filter: Optional[str] = None,
        url_marker: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        Recherche full-text dans Isidore avec pagination.

        Args:
            query: Requête mot-clé
            max_results: Plafond du nombre d'items retournés
            source_filter: Si non-None, ne retient que les items dont
                le champ isidore.source.name correspond (insensible à la
                casse, recherche par sous-chaîne).
            url_marker: Si non-None, ne retient que les items dont l'URL
                primaire ou un des aggregates contient cette chaîne
                (ex : "openedition.org"). Mode privilégié pour filtrer
                par agrégateur réel.
            from_date: Date minimale au format YYYY-MM-DD ou YYYY.
                Filtre post-pagination sur isidore.date.year/normalizedDate.
            to_date: Date maximale idem.

        Returns:
            Liste d'items Zotero
        """
        # Précompilation des bornes pour le filtre temporel
        from_year = int(from_date[:4]) if from_date and len(from_date) >= 4 else None
        to_year = int(to_date[:4]) if to_date and len(to_date) >= 4 else None
        all_items = []
        page = 1
        total_available = None
        # Limite de sûreté : ne pas paginer indéfiniment quand le filtre
        # restreint fortement (cas extrême : 1 item sur 100 000)
        max_pages = math.ceil(max_results * 100 / ISIDORE_REPLIES_PER_PAGE) + 10

        while len(all_items) < max_results and page <= max_pages:
            params = {
                "q": query,
                "replies": ISIDORE_REPLIES_PER_PAGE,
                "page": page,
                "output": "json",
            }

            logger.info(
                f"[Isidore] Page {page} (q={query[:50]}, "
                f"déjà {len(all_items)}/{max_results})"
            )

            try:
                response = self.http.get(ISIDORE_SEARCH_URL, params=params)
                data = response.json()
            except Exception as e:
                logger.error(f"[Isidore] Échec page {page}: {e}")
                break

            # Extraction du total disponible (seulement à la première page)
            replies_block = data.get("response", {}).get("replies", {})
            if total_available is None:
                meta = replies_block.get("meta", {})
                items_str = meta.get("@items") or meta.get("items") or "0"
                try:
                    total_available = int(items_str)
                    logger.info(
                        f"[Isidore] {total_available} résultats disponibles "
                        f"au total"
                    )
                except (ValueError, TypeError):
                    total_available = 0

            # Extraction des items : reply peut être un dict (1 résultat)
            # ou une liste (>1). Le contenu est sous replies.content.reply.
            content = replies_block.get("content", {})
            if isinstance(content, dict):
                replies = content.get("reply", [])
            else:
                replies = content

            if isinstance(replies, dict):
                # Un seul item retourné — l'API ne wrap pas en liste
                replies = [replies]
            elif not isinstance(replies, list):
                replies = []

            if not replies:
                logger.info(f"[Isidore] Page {page} vide, fin de pagination")
                break

            # Mapping et filtrage de chaque résultat
            for record in replies:
                # Application du filtre URL (privilégié)
                if url_marker:
                    if not self._url_matches(record, url_marker):
                        continue
                # Application du filtre source (fallback)
                elif source_filter:
                    source_value = self._extract_source(record)
                    if (
                        not source_value
                        or source_filter.lower() not in source_value.lower()
                    ):
                        continue

                # Filtre temporel : extrait l'année depuis isidore.date
                if from_year or to_year:
                    item_year = self._extract_year(record)
                    if item_year is None:
                        # Sans année exploitable : on rejette par sécurité
                        continue
                    if from_year and item_year < from_year:
                        continue
                    if to_year and item_year > to_year:
                        continue

                try:
                    item = map_isidore_to_zotero(record)
                    all_items.append(item)
                except Exception as e:
                    logger.warning(f"[Isidore] Échec mapping: {e}")

                if len(all_items) >= max_results:
                    logger.info(
                        f"[Isidore] Plafond {max_results} atteint, arrêt"
                    )
                    return all_items

            # Progression : si la page courante a moins d'items que la
            # taille de page, c'est la dernière
            if len(replies) < ISIDORE_REPLIES_PER_PAGE:
                logger.info(
                    f"[Isidore] Page partielle ({len(replies)} items), fin"
                )
                break

            # Sécurité supplémentaire : ne pas aller au-delà du total
            if total_available and page * ISIDORE_REPLIES_PER_PAGE >= total_available:
                logger.info(f"[Isidore] Total disponible atteint, fin")
                break

            page += 1

        logger.info(f"[Isidore] Terminé : {len(all_items)} items collectés")
        return all_items

    @staticmethod
    def _url_matches(record: Dict, marker: str) -> bool:
        """
        Teste si l'URL primaire ou un des aggregates contient le marker.

        Args:
            record: Item Isidore
            marker: Sous-chaîne à chercher (ex : "openedition.org")

        Returns:
            True si une URL correspond
        """
        iso = record.get("isidore", {})
        if not isinstance(iso, dict):
            return False

        # URL primaire
        primary = iso.get("url", "")
        if isinstance(primary, str) and marker in primary:
            return True

        # Aggregates ORE : URLs alternatives
        ore = iso.get("ore", {})
        aggregates = ore.get("aggregates", []) if isinstance(ore, dict) else []
        if isinstance(aggregates, dict):
            aggregates = [aggregates]
        for agg in aggregates:
            if isinstance(agg, dict):
                url = agg.get("$", "")
            else:
                url = str(agg)
            if marker in url:
                return True

        return False

    @staticmethod
    def _extract_year(record: Dict) -> Optional[int]:
        """
        Extrait l'année (int) depuis isidore.date pour le filtrage temporel.

        Args:
            record: Item Isidore

        Returns:
            Année à 4 chiffres ou None si non détectable
        """
        iso = record.get("isidore", {})
        if not isinstance(iso, dict):
            return None
        date_field = iso.get("date")
        if not date_field:
            return None
        if isinstance(date_field, str):
            import re
            m = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", date_field)
            return int(m.group(1)) if m else None
        if isinstance(date_field, dict):
            # Priorité @year, year, normalizedDate, @startDate
            for key in ("@year", "year"):
                v = date_field.get(key)
                if v and str(v).isdigit():
                    return int(v)
            for key in ("normalizedDate", "@startDate"):
                v = date_field.get(key)
                if v and len(str(v)) >= 4:
                    try:
                        return int(str(v)[:4])
                    except ValueError:
                        pass
        return None

    @staticmethod
    def _extract_source(record: Dict) -> str:
        """
        Extrait le nom de la source originelle d'un record Isidore.

        Le champ source est désormais un objet structuré :
            isidore.source = {
                "@handle": "...",
                "name": [
                    {"@xml:lang": "fr", "$": "Lidil"},
                    {"@xml:lang": "en", "$": "Lidil"},
                    ...
                ]
            }

        Args:
            record: Dict de résultat Isidore

        Returns:
            Nom de source en français de préférence, sinon première valeur
        """
        iso = record.get("isidore", {})
        if not isinstance(iso, dict):
            return ""
        source = iso.get("source")
        if not source:
            return ""

        if isinstance(source, str):
            return source

        if isinstance(source, dict):
            names = source.get("name", [])
            if isinstance(names, list):
                # Priorité au français
                for n in names:
                    if (
                        isinstance(n, dict)
                        and n.get("@xml:lang") == "fr"
                        and n.get("$")
                    ):
                        return n["$"]
                # Sinon première valeur disponible
                for n in names:
                    if isinstance(n, dict) and n.get("$"):
                        return n["$"]
                    elif isinstance(n, str) and n:
                        return n
            elif isinstance(names, str):
                return names
            elif isinstance(names, dict) and names.get("$"):
                return names["$"]

        if isinstance(source, list) and source:
            return IsidoreConnector._extract_source({"isidore": {"source": source[0]}})

        return ""
