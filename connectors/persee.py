"""
Connecteur Persée : stratégie hybride proxy Isidore + OAI optionnel.

Persée (https://www.persee.fr) est un portail patrimonial de périodiques
scientifiques (LSH/SHS principalement) avec un fonds historique remontant
au XIXe siècle. La plupart des contenus sont en accès libre.

Stratégie identique à OpenEdition/Cairn :
1. Recherche full-text via proxy Isidore filtré par url_marker="persee.fr"
2. Enrichissement optionnel par GetRecord OAI sur https://oai.persee.fr/oai
   (format oai_dc, seul universellement supporté)

Limitations :
- L'API OAI Persée ne permet pas la recherche par mot-clé (OAI-PMH limitation).
- Le proxy Isidore couvre la grande majorité des items Persée indexés.
"""

import logging
from typing import List, Dict, Optional

from sickle import Sickle

from connectors.base import Connector
from connectors.isidore import IsidoreConnector
from connectors.isidore_scan import annotate_proxy_source
from mappers.oai_mapper import map_oai_dc_to_zotero
from utils.http import OperationCancelled, polite_wait
from config import (
    PERSEE_OAI_URL,
    PERSEE_OAI_FORMAT,
    PERSEE_URL_MARKER,
    USER_AGENT,
)

logger = logging.getLogger(__name__)


class PerseeConnector(Connector):
    """Connecteur Persée via proxy Isidore."""

    SOURCE_NAME = "Persée"

    def __init__(self, http_client=None, enrich_oai: bool = False,
                 scan_registry=None, **kwargs):
        """
        Args:
            http_client: Client HTTP partagé (porte le registre des flux
                Isidore de l'exécution)
            enrich_oai: Si True, enrichit via GetRecord OAI sur oai.persee.fr
            scan_registry: Registre de flux Isidore explicite (contexte
                d'exécution), cf. connectors/isidore_scan.py
            **kwargs: absorbés depuis la signature uniforme
        """
        super().__init__(http_client, **kwargs)
        self.enrich_oai = enrich_oai
        self.scan_registry = scan_registry
        # Statut du dernier parcours Isidore (IsidoreScanStatus)
        self.last_scan_status = None
        self._sickle = None

    def _get_sickle(self) -> Sickle:
        if self._sickle is None:
            self._sickle = Sickle(
                PERSEE_OAI_URL,
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
        Recherche Persée via proxy Isidore.

        Args:
            query: Requête mot-clé
            max_results: Plafond du nombre d'items
            from_date: Date minimale YYYY ou YYYY-MM-DD
            to_date: Date maximale idem

        Returns:
            Liste d'items Zotero marqués 'Sources: Persée'
        """
        logger.info(f"[Persée] Démarrage via proxy Isidore: {query}")

        # Flux de pages Isidore partagé avec les autres consommateurs de
        # l'exécution (cf. connectors/isidore_scan.py).
        with IsidoreConnector(http_client=self.http,
                              scan_registry=self.scan_registry,
                              run_context=self.run_context) as isidore:
            items = isidore.search(
                query,
                max_results=max_results,
                url_marker=PERSEE_URL_MARKER,
                from_date=from_date,
                to_date=to_date,
                source_label=self.SOURCE_NAME,
            )
            self.last_scan_status = isidore.last_scan_status
            # Compte rendu de collecte (OPT-07) : celui du parcours Isidore
            # de cette sous-source (fenêtre, erreur partagée, plafond).
            self.last_status = isidore.last_status

        # Annotation source canonique
        annotate_proxy_source(items, "Persée")

        logger.info(f"[Persée] {len(items)} items via Isidore")

        if self.enrich_oai:
            items = self._enrich_with_oai(items)

        logger.info(f"[Persée] Terminé : {len(items)} items")
        return items

    def _enrich_with_oai(self, items: List[Dict]) -> List[Dict]:
        """Enrichit chaque item par GetRecord OAI sur oai.persee.fr."""
        logger.info(f"[Persée] Enrichissement OAI de {len(items)} items")
        enriched = []
        report = self.last_status
        failures = 0
        try:
            for i, item in enumerate(items):
                # Annulation vérifiée entre deux enregistrements OAI.
                self.check_cancelled()
                oai_id = self._extract_oai_id(item)
                if not oai_id:
                    enriched.append(item)
                    continue
                try:
                    # Politesse par hôte (utils.http) : créneau réservé avant
                    # l'accès OAI, partagé entre threads (remplace le
                    # sommeil fixe après chaque GetRecord).
                    polite_wait(PERSEE_OAI_URL, cancel=self.cancel_signal())
                    record = self._get_sickle().GetRecord(
                        identifier=oai_id, metadataPrefix=PERSEE_OAI_FORMAT,
                    )
                    oai_item = map_oai_dc_to_zotero(
                        record.metadata, oai_identifier=oai_id, source_name="Persée",
                    )
                    enriched.append(self._merge_records(item, oai_item))
                except Exception as e:
                    logger.warning(f"[Persée] Échec enrichissement {oai_id}: {e}")
                    failures += 1
                    enriched.append(item)
                if (i + 1) % 50 == 0:
                    logger.info(f"[Persée] Enrichi {i + 1}/{len(items)}")
        except OperationCancelled as exc:
            # Échéance souple (OPT-09 ter, RunContext.soft_stop) entre deux
            # enregistrements OAI : les notices déjà enrichies le restent, les
            # autres gardent leurs métadonnées Isidore ; aucune requête OAI de
            # plus. Toute autre annulation est propagée.
            done = len(enriched)
            if not self.soft_stopped(
                    exc, report, len(items),
                    detail=(f"enrichissement OAI interrompu par l'échéance souple "
                            f"après {done} notice(s) traitée(s) sur {len(items)} : "
                            f"les {len(items) - done} autre(s) gardent leurs "
                            f"métadonnées Isidore"),
                    during_enrichment=True):
                raise
            enriched.extend(items[done:])
        if report is not None and failures:
            report.bump("oai_failures", failures)
            report.warn(f"enrichissement OAI en échec pour {failures} "
                        f"notice(s) (métadonnées Isidore conservées)")
        return enriched

    @staticmethod
    def _extract_oai_id(item: Dict) -> Optional[str]:
        """
        Extrait l'identifiant OAI Persée.

        Les URL Persée ont la forme :
            https://www.persee.fr/doc/<journal>_<issn>_<year>_num_<vol>_<issue>_<page>
            https://www.persee.fr/issue/<journal>_<issn>_<year>_num_<vol>_<issue>
        L'identifiant OAI Persée est typiquement :
            oai:persee:article/<journal>_<issn>_<year>_num_<vol>_<issue>_<page>
        """
        url = item.get("url", "")
        import re
        m = re.search(r"persee\.fr/(doc|issue)/([^?#]+)", url)
        if m:
            kind = "article" if m.group(1) == "doc" else "issue"
            return f"oai:persee:{kind}/{m.group(2).rstrip('/')}"

        # Fallback : OAI Identifier explicite dans extra
        for line in item.get("extra", "").split("\n"):
            if line.startswith("OAI Identifier:") and "persee" in line:
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
