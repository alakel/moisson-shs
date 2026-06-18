"""
Connecteur Érudit : stratégie hybride proxy Isidore + OAI optionnel.

Érudit (https://www.erudit.org) est la principale plateforme SHS
francophone du Québec et du Canada. Couvre la quasi-totalité des
revues SHS canadiennes francophones.

Stratégie identique à OpenEdition/Cairn/Persée :
1. Recherche full-text via proxy Isidore filtré par url_marker="erudit.org"
2. Enrichissement optionnel par GetRecord OAI sur https://oai.erudit.org/oai/request
   (format oai_dc)

Identifiants OAI Érudit typiques :
    oai:erudit:revue/<acronyme>/<volume>/<numero>/<article>
    ex : oai:erudit:as/v22n2/1010406ar
"""

import logging
import time
from typing import List, Dict, Optional

from sickle import Sickle

from connectors.base import Connector
from connectors.isidore import IsidoreConnector
from mappers.oai_mapper import map_oai_dc_to_zotero
from config import (
    ERUDIT_OAI_URL,
    ERUDIT_OAI_FORMAT,
    ERUDIT_URL_MARKER,
    REQUEST_DELAY,
    USER_AGENT,
)

logger = logging.getLogger(__name__)


class EruditConnector(Connector):
    """Connecteur Érudit via proxy Isidore."""

    SOURCE_NAME = "Érudit"

    def __init__(self, http_client=None, enrich_oai: bool = False, **kwargs):
        """
        Args:
            http_client: Client HTTP partagé
            enrich_oai: Si True, enrichit via GetRecord OAI sur oai.erudit.org
            **kwargs: absorbés depuis la signature uniforme
        """
        super().__init__(http_client, **kwargs)
        self.enrich_oai = enrich_oai
        self._sickle = None

    def _get_sickle(self) -> Sickle:
        if self._sickle is None:
            self._sickle = Sickle(
                ERUDIT_OAI_URL,
                max_retries=3,
                default_retry_after=10,
                headers={"User-Agent": USER_AGENT},
            )
        return self._sickle

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: str = None,
        to_date: str = None,
    ) -> List[Dict]:
        """
        Recherche Érudit via proxy Isidore.

        Args:
            query: Requête mot-clé
            max_results: Plafond du nombre d'items
            from_date: Date minimale YYYY ou YYYY-MM-DD
            to_date: Date maximale idem

        Returns:
            Liste d'items Zotero marqués 'Sources: Érudit'
        """
        logger.info(f"[Érudit] Démarrage via proxy Isidore: {query}")

        with IsidoreConnector(http_client=self.http) as isidore:
            items = isidore.search(
                query,
                max_results=max_results,
                url_marker=ERUDIT_URL_MARKER,
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
                    if "Érudit" not in sources:
                        sources.insert(0, "Érudit")
                    new_lines.append("Sources: " + ", ".join(sources))
                else:
                    new_lines.append(line)
            item["extra"] = "\n".join(new_lines)

        logger.info(f"[Érudit] {len(items)} items via Isidore")

        if self.enrich_oai:
            items = self._enrich_with_oai(items)

        logger.info(f"[Érudit] Terminé : {len(items)} items")
        return items

    def _enrich_with_oai(self, items: List[Dict]) -> List[Dict]:
        """Enrichit chaque item par GetRecord OAI Érudit."""
        logger.info(f"[Érudit] Enrichissement OAI de {len(items)} items")
        enriched = []
        for i, item in enumerate(items):
            oai_id = self._extract_oai_id(item)
            if not oai_id:
                enriched.append(item)
                continue
            try:
                record = self._get_sickle().GetRecord(
                    identifier=oai_id, metadataPrefix=ERUDIT_OAI_FORMAT,
                )
                oai_item = map_oai_dc_to_zotero(
                    record.metadata, oai_identifier=oai_id, source_name="Érudit",
                )
                enriched.append(self._merge_records(item, oai_item))
            except Exception as e:
                logger.warning(f"[Érudit] Échec enrichissement {oai_id}: {e}")
                enriched.append(item)
            time.sleep(REQUEST_DELAY)
            if (i + 1) % 50 == 0:
                logger.info(f"[Érudit] Enrichi {i + 1}/{len(items)}")
        return enriched

    @staticmethod
    def _extract_oai_id(item: Dict) -> Optional[str]:
        """
        Extrait l'identifiant OAI Érudit.

        Les URL Érudit ont la forme :
            https://www.erudit.org/fr/revues/<journal>/<year>-v<vol>-n<num>/<id>/
            http://id.erudit.org/iderudit/<article_id>
        L'identifiant OAI Érudit est typiquement :
            oai:erudit.org:<article_id>
        """
        url = item.get("url", "")
        import re

        # Format 1 : id.erudit.org/iderudit/<id>
        m = re.search(r"iderudit/([a-z0-9]+)", url)
        if m:
            return f"oai:erudit.org:{m.group(1)}"

        # Format 2 : article id en fin de path
        m = re.search(r"erudit\.org/.+?/([a-z0-9]+ar)/?(?:$|\?)", url)
        if m:
            return f"oai:erudit.org:{m.group(1)}"

        # Fallback : extra
        for line in item.get("extra", "").split("\n"):
            if line.startswith("OAI Identifier:") and "erudit" in line:
                return line[len("OAI Identifier:"):].strip()

        return None

    @staticmethod
    def _merge_records(base: Dict, enrichment: Dict) -> Dict:
        """Fusion item Isidore + enrichissement OAI."""
        merged = dict(base)
        for key, value in enrichment.items():
            if key in ("creators", "tags", "collections", "relations"):
                if value and (
                    not merged.get(key) or len(value) > len(merged.get(key, []))
                ):
                    merged[key] = value
            elif key == "extra":
                existing_lines = set(merged.get("extra", "").split("\n"))
                for line in value.split("\n"):
                    if line and line not in existing_lines:
                        existing_lines.add(line)
                merged["extra"] = "\n".join(l for l in existing_lines if l)
            else:
                if value and (
                    not merged.get(key)
                    or (isinstance(value, str)
                        and len(value) > len(str(merged.get(key, ""))))
                ):
                    merged[key] = value
        return merged
