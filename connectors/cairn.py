"""
Connecteur Cairn : stratégie hybride proxy Isidore + OAI.

Cairn n'expose pas d'API de recherche full-text publique.
Son entrepôt OAI-PMH est libre d'accès mais ne permet que
le moissonnage par set (revue). Pour effectuer une recherche
par mot-clé, on procède comme pour OpenEdition :

1. Recherche full-text sur Isidore filtrée par source='Cairn.info'.
2. Enrichissement optionnel par GetRecord OAI sur l'entrepôt Cairn.

Limitation importante : Cairn ne fournit que les métadonnées,
pas le texte intégral des articles. Tout corpus Cairn produit
par ce connecteur sera donc utilisable pour des analyses
bibliométriques mais pas pour des analyses textuelles directes
sans démarche complémentaire (scraping, convention TDM).
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
    CAIRN_OAI_URL,
    CAIRN_OAI_FORMAT,
    CAIRN_URL_MARKER,
    USER_AGENT,
)

logger = logging.getLogger(__name__)


class CairnConnector(Connector):
    """
    Connecteur Cairn.

    Utilise Isidore comme proxy de recherche et propose un
    enrichissement OAI optionnel via l'entrepôt Cairn.
    """

    SOURCE_NAME = "Cairn"

    def __init__(self, http_client=None, enrich_oai: bool = False,
                 scan_registry=None, **kwargs):
        """
        Args:
            http_client: Client HTTP partagé (porte le registre des flux
                Isidore de l'exécution)
            enrich_oai: Si True, enrichit chaque item via GetRecord
                OAI sur l'entrepôt Cairn (lent, une requête par item)
            scan_registry: Registre de flux Isidore explicite (contexte
                d'exécution), cf. connectors/isidore_scan.py
            **kwargs: kwargs absorbés depuis la signature uniforme du
                base.Connector (ignorés ici sauf enrich_oai).
        """
        super().__init__(http_client, **kwargs)
        self.enrich_oai = enrich_oai
        self.scan_registry = scan_registry
        # Statut du dernier parcours Isidore (IsidoreScanStatus)
        self.last_scan_status = None
        self._sickle = None

    def _get_sickle(self) -> Sickle:
        """Lazy init du client OAI-PMH Cairn."""
        if self._sickle is None:
            self._sickle = Sickle(
                CAIRN_OAI_URL,
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
        Recherche par mot-clé Cairn via proxy Isidore.

        Args:
            query: Requête mot-clé
            max_results: Plafond du nombre d'items
            from_date: Date minimale YYYY-MM-DD ou YYYY (propagée à Isidore)
            to_date: Date maximale idem

        Returns:
            Liste d'items Zotero marqués 'Sources: Cairn'
        """
        logger.info(f"[Cairn] Démarrage via proxy Isidore: {query}")

        # Étape 1 : recherche Isidore filtrée par DOMAINE D'URL Cairn.
        # Le filtrage nominal "Cairn.info" n'opère plus depuis qu'Isidore
        # indexe par revue individuelle plutôt que par agrégateur.
        # Flux de pages Isidore partagé avec les autres consommateurs de
        # l'exécution (cf. connectors/isidore_scan.py).
        with IsidoreConnector(http_client=self.http,
                              scan_registry=self.scan_registry,
                              run_context=self.run_context) as isidore:
            items = isidore.search(
                query,
                max_results=max_results,
                url_marker=CAIRN_URL_MARKER,
                from_date=from_date,
                to_date=to_date,
                source_label=self.SOURCE_NAME,
            )
            self.last_scan_status = isidore.last_scan_status
            # Compte rendu de collecte (OPT-07) : celui du parcours Isidore
            # de cette sous-source (fenêtre, erreur partagée, plafond).
            self.last_status = isidore.last_status

        # Reannotation de la source canonique
        annotate_proxy_source(items, "Cairn")
        for item in items:
            # Annotation systématique : les items Cairn n'ont pas
            # de texte intégral exposé via l'API
            if "Full Text Available" not in item["extra"]:
                from mappers.zotero_schema import append_to_extra
                append_to_extra(item, "Full Text Available", "metadata only")

        logger.info(f"[Cairn] {len(items)} items via Isidore")

        # Étape 2 (optionnelle) : enrichissement OAI
        if self.enrich_oai:
            items = self._enrich_with_oai(items)

        logger.info(f"[Cairn] Terminé : {len(items)} items")
        return items

    def _enrich_with_oai(self, items: List[Dict]) -> List[Dict]:
        """
        Enrichit chaque item par GetRecord OAI sur Cairn.

        Args:
            items: Items issus du proxy Isidore

        Returns:
            Items enrichis
        """
        logger.info(f"[Cairn] Enrichissement OAI de {len(items)} items")

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
                    polite_wait(CAIRN_OAI_URL, cancel=self.cancel_signal())
                    record = self._get_sickle().GetRecord(
                        identifier=oai_id,
                        metadataPrefix=CAIRN_OAI_FORMAT,
                    )
                    oai_item = map_oai_dc_to_zotero(
                        record.metadata,
                        oai_identifier=oai_id,
                        source_name="Cairn",
                    )
                    merged = self._merge_records(item, oai_item)
                    enriched.append(merged)
                except Exception as e:
                    logger.warning(
                        f"[Cairn] Échec enrichissement OAI {oai_id}: {e}"
                    )
                    failures += 1
                    enriched.append(item)

                if (i + 1) % 50 == 0:
                    logger.info(f"[Cairn] Enrichi {i + 1}/{len(items)}")
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
        Extrait l'identifiant OAI Cairn depuis l'URL ou 'extra'.

        Format réel observé sur l'entrepôt Cairn (vérifié via
        ListIdentifiers en 2026) :
            oai:cairn.info:<REVUE_CODE>_<VOLUME>_<PAGE>
            ex : oai:cairn.info:AATC_125_0001
                 oai:cairn.info:PUF_STEBE_2011_02_0003

        Les URL Cairn ont la forme :
            https://shs.cairn.info/revue-<slug>-<year>-<issue>-page-<num>?lang=fr
            https://www.cairn.info/revue-<slug>-<year>-<issue>-page-<num>.htm
            https://www.cairn.info/load_pdf.php?ID_ARTICLE=<CODE>

        L'identifiant `oai:cairn.info:{doi}` historiquement supposé est
        FAUX et conduit systématiquement à une erreur OAI idDoesNotExist.

        Args:
            item: Item Zotero

        Returns:
            Identifiant OAI Cairn ou None si non reconstructible.
            Dans le doute, retourne None plutôt qu'une supposition
            (l'enrichissement est skippé sans crash).
        """
        # Priorité 1 : extra explicite (set par le mapper si présent dans
        # Isidore ou la moisson OAI native)
        extra = item.get("extra", "")
        for line in extra.split("\n"):
            if line.startswith("OAI Identifier:"):
                value = line[len("OAI Identifier:"):].strip()
                if value.startswith("oai:cairn.info:"):
                    return value

        # Priorité 2 : reconstruction depuis l'URL load_pdf.php
        # qui contient directement le CODE_ARTICLE
        import re
        url = item.get("url", "")
        m = re.search(r"ID_ARTICLE=([A-Z0-9_]+)", url)
        if m:
            return f"oai:cairn.info:{m.group(1)}"

        # Priorité 3 : reconstruction depuis une URL canonique d'article
        # cairn.info/revue-<slug>-<year>-<issue>-page-<num>
        # Le mapping slug→CODE_REVUE n'est pas bijectif sans table, donc
        # cette reconstruction est best-effort : on tente une heuristique
        # à partir des composants visibles.
        # En production, mieux vaut récupérer l'OAI ID via ListIdentifiers
        # initial puis matcher par URL ; mais pour un enrichissement
        # ponctuel, on retourne None.

        # Pas d'OAI ID exploitable : skip enrichissement, pas d'erreur
        return None

    @staticmethod
    def _merge_records(base: Dict, enrichment: Dict) -> Dict:
        """
        Fusion item de base (Isidore) + enrichissement OAI.

        Identique à la logique d'OpenEditionConnector.

        Args:
            base: Item Isidore
            enrichment: Item OAI

        Returns:
            Item fusionné
        """
        merged = dict(base)
        for key, value in enrichment.items():
            if key in ("creators", "tags", "collections", "relations"):
                if value and (not merged.get(key) or len(value) > len(merged.get(key, []))):
                    merged[key] = value
            elif key == "extra":
                existing_lines = set(merged.get("extra", "").split("\n"))
                new_lines = value.split("\n")
                for line in new_lines:
                    if line and line not in existing_lines:
                        existing_lines.add(line)
                merged["extra"] = "\n".join(l for l in existing_lines if l)
            else:
                if value and (not merged.get(key) or
                              (isinstance(value, str) and len(value) > len(str(merged.get(key, ""))))):
                    merged[key] = value
        return merged
