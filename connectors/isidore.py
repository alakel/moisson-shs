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
        │   ├── @items           # nombre total annoncé
        │   └── @pageItems       # nombre dans cette page
        ├── content
        │   └── reply            # LISTE d'items (objet unique si 1 résultat)
        │       └── [item]
        │           ├── @uri
        │           └── isidore
        │               ├── title             # liste multilingue [{@xml:lang, $}]
        │               ├── url               # URL primaire
        │               ├── enrichedCreators
        │               │   └── creator       # liste ou dict unique
        │               ├── date
        │               │   ├── @startDate
        │               │   ├── normalizedDate
        │               │   └── year
        │               ├── types.type        # liste ou string
        │               ├── source            # objet avec name multilingue
        │               │   ├── name [{@xml:lang, $}]
        │               │   └── collection.url  # agrégateur (Cairn.info…)
        │               ├── ore.aggregates    # URLs alternatives
        │               ├── abstract          # liste multilingue
        │               ├── subject           # liste de mots-clés enrichis
        │               └── language
        └── page                 # @previous, @current, @next (absent si
                                 # une seule page)

La pagination utilise `page=` et `replies=`. L'API ne sert que les 200
premiers résultats d'une requête (constat C35, ``ISIDORE_RESULT_WINDOW``) :
au-delà, elle répète la dernière page servie.

Parcours mutualisé (connectors/isidore_scan.py) : la source Isidore directe
et les sous-sources qui passent par Isidore (OpenEdition, Cairn, Persée,
Érudit, Theses.fr) lisent le MÊME flux de pages pour une requête et une
exécution ; chaque page n'est téléchargée qu'une fois. Chaque consommateur
garde son filtre, ses dates et son plafond :

- par DOMAINE D'URL (url_marker="openedition.org") : hôte analysé de l'URL
  primaire ou d'un agrégat égal au domaine ou à l'un de ses sous-domaines —
  stratégie des sous-sources ;
- par NOM (source_filter="HAL SHS...") — peu fiable car Isidore indexe par revue
  individuelle plutôt que par agrégateur.

Le statut du dernier parcours (pages, notices parcourues et retenues, fenêtre
atteinte, sous-source incomplète, erreur) est exposé dans
``last_scan_status`` ; le compte rendu de collecte (OPT-07, ``last_status``)
en est dérivé par ``report_from_scan`` : plafond atteint ou flux épuisé →
source complète ; fenêtre du fournisseur atteinte avant le plafond →
``partial`` (motif ``provider_window``) ; borne de parcours → ``partial``
(``scan_bound``) ; erreur du flux (partagée par tous les consommateurs) →
``error``, ou ``partial`` après des pages réussies.
"""

import logging
from typing import List, Dict, Optional

from connectors.base import Connector
from connectors.isidore_scan import (
    IsidoreScanStatus,
    ScanRegistry,
    consume,
    extract_source_name,
    extract_year,
    record_matches_domain,
)
from engine.models import (
    REASON_DEADLINE, REASON_EXCEPTION, REASON_SCAN_BOUND, SearchReport,
)

logger = logging.getLogger(__name__)

# Tri natif d'Isidore (OPT-12) : ordre par défaut de resource/search, sans
# paramètre de tri ni score exposé (vérifié le 2026-09-29 : années dans le
# désordre, donc pas un ordre chronologique). Le rang natif d'une notice est
# sa position dans la réponse de l'API (1 à 200, doublons compris),
# identique pour la source directe et les sous-sources, qui lisent le même
# flux. Réserve (revue OPT-12) : que cet ordre soit un classement par
# pertinence N'EST PAS VÉRIFIÉ (aucun score, aucun paramètre de tri
# documenté consultable : la documentation de l'API répondait 403 le
# 2026-09-29) ; un classement (fusion de rangs réciproques) ne doit pas le
# traiter comme tel sans vérification préalable.
NATIVE_SORT = "isidore_default"


class IsidoreConnector(Connector):
    """
    Connecteur pour Isidore (moteur de recherche SHS Huma-Num).

    Supporte deux types de filtrage pour permettre aux connecteurs
    OpenEdition, Cairn, Persée, Érudit et Theses.fr d'utiliser Isidore comme
    proxy de recherche full-text :
    - url_marker : par domaine d'URL (méthode privilégiée)
    - source_filter : par nom de source littéral (fallback)
    """

    SOURCE_NAME = "Isidore"

    def __init__(self, http_client=None, scan_registry: Optional[ScanRegistry] = None,
                 **kwargs):
        """
        Args:
            http_client: Client HTTP partagé ; par défaut, le registre des
                flux Isidore de l'exécution lui est attaché.
            scan_registry: Registre de flux explicite (contexte d'exécution),
                prioritaire sur celui du client HTTP.
            **kwargs: absorbés (signature uniforme des connecteurs).
        """
        super().__init__(http_client, **kwargs)
        self.scan_registry = scan_registry
        self.last_scan_status: Optional[IsidoreScanStatus] = None

    def _registry(self) -> Optional[ScanRegistry]:
        """Registre explicite, sinon celui du contexte d'exécution."""
        if self.scan_registry is not None:
            return self.scan_registry
        return getattr(self.run_context, "scan_registry", None)

    def search(
        self,
        query: str,
        max_results: int = 1000,
        source_filter: Optional[str] = None,
        url_marker: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        *,
        source_label: Optional[str] = None,
    ) -> List[Dict]:
        """
        Recherche full-text dans Isidore via le flux de pages partagé.

        Args:
            query: Requête mot-clé
            max_results: Plafond du nombre d'items retournés
            source_filter: Si non-None, ne retient que les items dont
                le champ isidore.source.name correspond (insensible à la
                casse, recherche par sous-chaîne).
            url_marker: Si non-None, ne retient que les items dont l'URL
                primaire ou un des aggregates relève de ce domaine (hôte égal
                au domaine ou sous-domaine, ex : "openedition.org"). Mode
                privilégié pour filtrer par agrégateur réel ; prioritaire sur
                source_filter.
            from_date: Date minimale au format YYYY-MM-DD ou YYYY.
                Filtre sur isidore.date (notices sans année rejetées).
            to_date: Date maximale idem.
            source_label: Nom du consommateur dans les journaux et le statut
                (défaut : "Isidore" ; les sous-sources passent le leur).

        Returns:
            Liste d'items Zotero (copies propres à cet appel)
        """
        label = source_label or self.SOURCE_NAME
        self.last_status = SearchReport(source=label)
        items, status = consume(
            self.http, query, max_results,
            source_label=label,
            url_marker=url_marker,
            source_filter=source_filter,
            from_date=from_date,
            to_date=to_date,
            scan_registry=self._registry(),
            cancel=self.cancel_signal(),
            # Échéance souple (OPT-09 ter) : notices déjà retenues rendues.
            soft_stop=self.soft_stop_applies,
        )
        self.last_scan_status = status
        self.last_status = report_from_scan(status, label)
        logger.info(f"[{label}] Terminé : {len(items)} items collectés")
        return items

    @staticmethod
    def _url_matches(record: Dict, marker: str) -> bool:
        """
        Teste si l'URL primaire ou un des aggregates relève du domaine.

        Comparaison sur l'hôte analysé (urllib.parse) : égal au domaine ou
        sous-domaine. "cairn.info" ne correspond ni à "notcairn.info" ni à
        une URL qui ne cite ce domaine que dans son chemin ou sa requête.

        Args:
            record: Item Isidore
            marker: Domaine (ex : "openedition.org")

        Returns:
            True si une URL correspond
        """
        return record_matches_domain(record, marker)

    @staticmethod
    def _extract_year(record: Dict) -> Optional[int]:
        """
        Extrait l'année (int) depuis isidore.date pour le filtrage temporel.

        Args:
            record: Item Isidore

        Returns:
            Année à 4 chiffres ou None si non détectable
        """
        return extract_year(record)

    @staticmethod
    def _extract_source(record: Dict) -> str:
        """
        Extrait le nom de la source originelle d'un record Isidore.

        Le champ source est un objet structuré :
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
        return extract_source_name(record)


def report_from_scan(status: Optional[IsidoreScanStatus],
                     label: str) -> SearchReport:
    """
    Compte rendu de collecte (``SearchReport``) d'un consommateur du flux
    Isidore, d'après son statut de parcours.

    - plafond atteint : ``limit_reached`` (source complète) ;
    - flux épuisé : ``exhausted`` (complète, ou vide sans notice retenue) ;
    - fenêtre de résultats du fournisseur atteinte avant le plafond (C35) :
      ``partial``, motif ``provider_window`` ;
    - borne ``ISIDORE_SCAN_MAX_RECORDS`` : ``partial``, motif ``scan_bound`` ;
    - erreur du flux : ``error`` (``partial`` après des pages réussies), avec
      le motif de l'erreur (HTTP, réseau, corps illisible) ;
    - parcours arrêté par l'échéance souple après des notices retenues
      (OPT-09 ter) : ``partial``, motif ``deadline``.

    Rangs natifs (OPT-12) : une entrée par notice retenue (position dans la
    fenêtre servie par Isidore, score None), alignée sur les notices.
    """
    report = SearchReport(source=label)
    if status is None:
        return report
    report.pages = status.pages_read
    report.records = status.scanned
    # Rangs natifs (OPT-12) : position dans la fenêtre, sans score.
    report.sorted_by(NATIVE_SORT)
    for position in status.ranks:
        report.rank(position)
    # Compteurs utiles au diagnostic, sans redire le statut (retour MCP
    # compact) ; le détail complet reste dans ``last_scan_status``.
    report.counters.update({
        "scanned": status.scanned,
        "retained": status.retained,
        "flow_records": status.flow_records,
        "total_available": status.total_available,
    })
    if status.mapping_errors:
        report.counters["mapping_errors"] = status.mapping_errors
    if status.cap_reached:
        report.limit()
        return report
    if status.soft_stopped:
        # Échéance souple (OPT-09 ter) : notices déjà retenues rendues ; une
        # erreur du flux survenue entre-temps est dite, sans primer (ce
        # consommateur s'est arrêté à l'échéance).
        if status.error is not None:
            report.warn(f"parcours Isidore : "
                        f"{status.error_detail or status.error}")
        report.truncated(
            REASON_DEADLINE,
            f"parcours Isidore interrompu par l'échéance souple après "
            f"{status.pages_read} page(s) lue(s) : {status.retained} notice(s) "
            f"retenue(s) sur {status.scanned} parcourue(s), aucune page "
            f"supplémentaire demandée")
        return report
    if status.error is not None:
        report.fail(status.error_reason or REASON_EXCEPTION,
                    status.error_detail or "parcours Isidore interrompu")
        return report
    if status.window_reached:
        announced = (f" sur {status.total_available} annoncés"
                     if status.total_available else "")
        report.window(
            f"Isidore ne sert que {status.flow_records} résultats distincts"
            f"{announced} ; {status.retained} retenus pour {label}"
        )
        return report
    if status.bound_reached:
        report.window(
            f"borne ISIDORE_SCAN_MAX_RECORDS atteinte ({status.flow_records} "
            f"notices parcourues)", reason=REASON_SCAN_BOUND,
        )
        return report
    if status.exhausted:
        report.done()
    return report
