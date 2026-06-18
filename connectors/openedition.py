"""
Connecteur OpenEdition : stratégie hybride proxy Isidore + OAI.

Le protocole OAI-PMH ne permet pas la recherche par mot-clé.
Pour palier cette limitation, ce connecteur procède en deux temps :

1. Recherche full-text sur Isidore filtrée par source='OpenEdition'.
   Isidore moissonne OpenEdition régulièrement et indexe leur
   full-text, permettant ainsi une recherche keyword qui serait
   impossible directement sur l'OAI d'OpenEdition.

2. Enrichissement par GetRecord OAI sur chaque identifiant
   retourné, en format qdc ou oai_openaire pour récupérer
   les métadonnées riches non exposées par Isidore.

Cette approche préserve la richesse maximale de chaque source
tout en permettant la recherche par mot-clé.
"""

import logging
import time
from typing import List, Dict, Optional

from sickle import Sickle
from sickle.iterator import OAIItemIterator

from connectors.base import Connector
from connectors.isidore import IsidoreConnector
from mappers.oai_mapper import map_oai_dc_to_zotero, map_qdc_to_zotero
from mappers.zotero_schema import append_to_extra
from config import (
    OPENEDITION_OAI_URL,
    OPENEDITION_OAI_FORMAT,
    OPENEDITION_URL_MARKER,
    REQUEST_DELAY,
    USER_AGENT,
)

logger = logging.getLogger(__name__)


class OpenEditionConnector(Connector):
    """
    Connecteur OpenEdition.

    Combine la recherche Isidore filtrée et l'enrichissement
    OAI direct sur l'entrepôt OpenEdition v2.
    """

    SOURCE_NAME = "OpenEdition"

    def __init__(self, http_client=None, enrich_oai: bool = False, **kwargs):
        """
        Args:
            http_client: Client HTTP partagé
            enrich_oai: Si True, enrichit chaque item via GetRecord OAI.
                Coûteux en temps (une requête par item). Désactivé
                par défaut ; les métadonnées Isidore sont déjà riches.
            **kwargs: kwargs absorbés depuis la signature uniforme du
                base.Connector (ignorés ici sauf enrich_oai).
        """
        super().__init__(http_client, **kwargs)
        self.enrich_oai = enrich_oai
        # Client OAI-PMH (sickle gère les resumptionTokens)
        self._sickle = None

    def _get_sickle(self) -> Sickle:
        """Lazy init du client OAI-PMH."""
        if self._sickle is None:
            self._sickle = Sickle(
                OPENEDITION_OAI_URL,
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
        Recherche par mot-clé via proxy Isidore puis enrichissement OAI.

        Args:
            query: Requête mot-clé
            max_results: Plafond du nombre d'items
            from_date: Date minimale YYYY-MM-DD ou YYYY (propagée à Isidore)
            to_date: Date maximale idem

        Returns:
            Liste d'items Zotero originellement marqués
            'Sources: OpenEdition'
        """
        logger.info(f"[OpenEdition] Démarrage via proxy Isidore: {query}")

        # Étape 1 : recherche Isidore filtrée par DOMAINE D'URL OpenEdition.
        # Isidore n'indexe pas par agrégateur ("OpenEdition") mais par revue
        # individuelle (Lidil, Semen, ...) ; le filtrage nominal historique
        # ne capture donc plus rien. Le filtrage par url_marker récupère
        # tous les items dont l'URL primaire ou un aggregate pointe vers
        # *.openedition.org (journals, books, calenda, hypotheses).
        with IsidoreConnector(http_client=self.http) as isidore:
            items = isidore.search(
                query,
                max_results=max_results,
                url_marker=OPENEDITION_URL_MARKER,
                from_date=from_date,
                to_date=to_date,
            )

        # Reannotation des items : la source canonique devient OpenEdition
        # tout en conservant Isidore comme source intermédiaire
        for item in items:
            extra = item.get("extra", "")
            # Substitution dans la ligne Sources
            new_extra_lines = []
            for line in extra.split("\n"):
                if line.startswith("Sources:"):
                    # Ajoute OpenEdition au début si pas déjà présent
                    sources = [s.strip() for s in line[len("Sources:"):].split(",")]
                    if "OpenEdition" not in sources:
                        sources.insert(0, "OpenEdition")
                    new_extra_lines.append("Sources: " + ", ".join(sources))
                else:
                    new_extra_lines.append(line)
            item["extra"] = "\n".join(new_extra_lines)

        logger.info(
            f"[OpenEdition] {len(items)} items via Isidore"
        )

        # Étape 2 (optionnelle) : enrichissement OAI ciblé
        if self.enrich_oai:
            items = self._enrich_with_oai(items)

        logger.info(f"[OpenEdition] Terminé : {len(items)} items")
        return items

    def _enrich_with_oai(self, items: List[Dict]) -> List[Dict]:
        """
        Enrichit chaque item par GetRecord OAI sur son identifiant.

        Args:
            items: Liste d'items issus du proxy Isidore

        Returns:
            Liste enrichie
        """
        logger.info(
            f"[OpenEdition] Enrichissement OAI de {len(items)} items"
        )

        enriched = []
        for i, item in enumerate(items):
            oai_id = self._extract_oai_id(item)
            if not oai_id:
                enriched.append(item)
                continue

            try:
                # GetRecord est l'opération OAI standard pour un item unique
                record = self._get_sickle().GetRecord(
                    identifier=oai_id,
                    metadataPrefix=OPENEDITION_OAI_FORMAT,
                )
                # Mapping du record OAI riche (qdc / openaire)
                # vers Zotero et fusion avec l'item Isidore existant
                oai_item = map_qdc_to_zotero(
                    record.metadata,
                    oai_identifier=oai_id,
                    source_name="OpenEdition",
                )
                # Fusion : conservation des champs Isidore, complétion
                # par les champs OAI non vides
                merged = self._merge_records(item, oai_item)
                enriched.append(merged)
            except Exception as e:
                logger.warning(
                    f"[OpenEdition] Échec enrichissement OAI {oai_id}: {e}"
                )
                enriched.append(item)

            # Politesse réseau sur l'entrepôt OAI
            time.sleep(REQUEST_DELAY)

            if (i + 1) % 50 == 0:
                logger.info(
                    f"[OpenEdition] Enrichi {i + 1}/{len(items)}"
                )

        return enriched

    @staticmethod
    def _extract_oai_id(item: Dict) -> Optional[str]:
        """
        Tente d'extraire un identifiant OAI exploitable
        depuis l'item Isidore.

        Les identifiants OAI OpenEdition ont la forme
        'oai:revues.org:<slug>/<id>' ou
        'oai:books.openedition.org:<slug>/<id>'.

        Args:
            item: Item Zotero

        Returns:
            Identifiant OAI ou None
        """
        extra = item.get("extra", "")
        url = item.get("url", "")

        # Extraction depuis URL OpenEdition
        # ex : https://journals.openedition.org/<slug>/<id>
        #   ou https://books.openedition.org/<slug>/<id>
        if "journals.openedition.org" in url:
            parts = url.rstrip("/").split("journals.openedition.org/", 1)
            if len(parts) == 2 and parts[1]:
                return f"oai:revues.org:{parts[1]}"
        elif "books.openedition.org" in url:
            parts = url.rstrip("/").split("books.openedition.org/", 1)
            if len(parts) == 2 and parts[1]:
                return f"oai:books.openedition.org:{parts[1]}"

        # Recherche dans 'extra' (format 'OAI Identifier: oai:...')
        for line in extra.split("\n"):
            if line.startswith("OAI Identifier:"):
                value = line[len("OAI Identifier:"):].strip()
                if value:
                    return value

        return None

    @staticmethod
    def _merge_records(base: Dict, enrichment: Dict) -> Dict:
        """
        Fusionne deux items en privilégiant la version la plus
        complète champ par champ.

        Args:
            base: Item de base (Isidore)
            enrichment: Item d'enrichissement (OAI)

        Returns:
            Item fusionné
        """
        merged = dict(base)
        for key, value in enrichment.items():
            if key in ("creators", "tags", "collections", "relations"):
                # Pour les listes, conservation de la version la plus longue
                if value and (not merged.get(key) or len(value) > len(merged.get(key, []))):
                    merged[key] = value
            elif key == "extra":
                # Fusion des extras
                existing_lines = set(merged.get("extra", "").split("\n"))
                new_lines = value.split("\n")
                for line in new_lines:
                    if line and line not in existing_lines:
                        existing_lines.add(line)
                merged["extra"] = "\n".join(l for l in existing_lines if l)
            else:
                # Pour les chaînes : la valeur enrichie remplace si plus longue
                if value and (not merged.get(key) or
                              (isinstance(value, str) and len(value) > len(str(merged.get(key, ""))))):
                    merged[key] = value
        return merged
