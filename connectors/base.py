"""
Interface abstraite pour les connecteurs de sources bibliographiques.

Chaque source (HAL, Isidore, OpenEdition, Cairn) implémente cette
interface, permettant à l'orchestrateur de les manipuler de manière
uniforme. La méthode 'search' renvoie une liste d'items Zotero
validés et prêts à la fusion.

L'approche par classe abstraite plutôt que par fonction permet
de partager le client HTTP, les compteurs et les logs entre
les différentes requêtes paginées d'une même session.

Statut et annulation (OPT-07) : ``search`` garde sa signature et son type de
retour, mais publie un compte rendu dans ``self.last_status``
(``engine.models.SearchReport`` : pages, plafond atteint, épuisement, fenêtre
du fournisseur, erreur après des pages, clé absente…), dont l'orchestrateur
déduit le statut de la source (``complete``, ``empty``, ``partial``,
``skipped``, ``error``). Le contexte d'exécution (``run_context``, passé au
constructeur) porte le signal d'annulation : ``check_cancelled()`` est
appelé entre deux pages ; les requêtes HTTP le reçoivent aussi par le
client (``HTTPClient.cancel_signal``).

Échéance souple (OPT-09 ter, contrat ``RunContext.soft_stop``) : un
connecteur paginé arrêté par la SEULE échéance souple de la moisson (serveur
MCP en stdio), entre deux pages ou pendant une requête, rend les notices déjà
reçues au lieu de propager l'arrêt (``soft_stopped``) ; son compte rendu
porte ``partial``, motif ``deadline``, et aucune page supplémentaire n'est
demandée. Toute autre annulation (client, échéance dure) est propagée telle
quelle : la moisson est annulée, rien n'est publié.
"""

from abc import ABC, abstractmethod
from typing import Any, List, Dict, Optional
import logging

from engine.models import (
    REASON_DEADLINE, SOFT_STOP_DURING_ENRICHMENT, STATUS_PARTIAL, SearchReport,
)
from utils.http import CancelSignal, HTTPClient

logger = logging.getLogger(__name__)


class Connector(ABC):
    """
    Classe de base pour tous les connecteurs de sources.

    Sous-classes responsables de :
    - implémenter la pagination spécifique à leur API
    - assurer le mapping vers le schéma Zotero via leur mapper dédié
    - tracer le nom de source dans le champ 'extra' Zotero
    - renseigner ``self.last_status`` (``SearchReport``) et appeler
      ``self.check_cancelled()`` entre deux pages
    """

    # Nom canonique de la source, à surcharger dans chaque sous-classe
    SOURCE_NAME: str = "unknown"

    def __init__(self, http_client: HTTPClient = None, run_context=None,
                 **kwargs):
        """
        Args:
            http_client: Client HTTP partagé (créé si non fourni)
            run_context: Contexte d'exécution (``engine.models.RunContext``) :
                signal d'annulation et échéance. None : aucune annulation
                autre que celle portée par le client HTTP.
            **kwargs: Options spécifiques au connecteur. Les kwargs non
                reconnus sont ignorés silencieusement. Permet à l'orchestrateur
                d'invoquer tous les connecteurs avec la même signature
                (ex : enrich_oai n'est utilisé que par OpenEdition/Cairn,
                ignoré par HAL/Isidore).
        """
        self.http = http_client or HTTPClient()
        # Indique si le client a été créé ici et doit être fermé
        self._owns_client = http_client is None
        self.run_context = run_context
        # Compte rendu de la dernière recherche (cf. engine.models)
        self.last_status: Optional[SearchReport] = None

    @abstractmethod
    def search(self, query: str, max_results: int = 1000) -> List[Dict]:
        """
        Effectue une recherche par mot-clé et renvoie les items
        au format Zotero JSON.

        Args:
            query: Requête mot-clé (peut contenir des opérateurs
                booléens et phrases entre guillemets selon le moteur)
            max_results: Plafond du nombre d'items retournés

        Returns:
            Liste d'items Zotero validés
        """
        raise NotImplementedError

    # --- statut et annulation (OPT-07) ---

    def _start_report(self) -> SearchReport:
        """Nouveau compte rendu pour la recherche qui commence."""
        self.last_status = SearchReport(source=self.SOURCE_NAME)
        return self.last_status

    def cancel_signal(self) -> Optional[CancelSignal]:
        """Signal d'annulation : celui du contexte, sinon celui du client."""
        ctx = getattr(self, "run_context", None)
        sig = getattr(ctx, "signal", None) if ctx is not None else None
        if isinstance(sig, CancelSignal):
            return sig
        # Seul un vrai signal compte (une doublure de client, un Mock, n'en
        # porte pas).
        sig = getattr(self.http, "cancel_signal", None)
        return sig if isinstance(sig, CancelSignal) else None

    def _cancel_kwargs(self) -> Dict[str, Any]:
        """Argument ``cancel`` pour ``utils.http_scholarly.get_json`` (vide
        sans signal, pour ne rien changer aux appels hors exécution)."""
        sig = self.cancel_signal()
        return {"cancel": sig} if sig is not None else {}

    def check_cancelled(self) -> None:
        """Lève ``OperationCancelled`` si l'exécution est annulée."""
        signal = self.cancel_signal()
        if signal is not None:
            signal.check()

    # --- échéance souple (OPT-09 ter) ---

    def soft_stop_applies(self, exc: BaseException) -> bool:
        """
        Vrai si ``exc`` arrête la source par la SEULE échéance souple de la
        moisson (``RunContext.soft_stop`` : ``OperationCancelled`` de motif
        ``deadline``, échéance souple active, signal de l'exécution non levé).
        Faux sans contexte, hors échéance souple (CLI, HTTP, résolveurs), ou
        pour une annulation par le client ou une échéance dure.
        """
        soft_stop = getattr(getattr(self, "run_context", None), "soft_stop", None)
        if not callable(soft_stop):
            return False
        try:
            return bool(soft_stop(exc))
        except Exception:  # noqa: BLE001 - doublure de contexte : propager
            return False

    def soft_stopped(self, exc: BaseException, report: Optional[SearchReport],
                     kept: int, detail: Optional[str] = None, *,
                     during_enrichment: bool = False) -> bool:
        """
        Arrêt par l'échéance souple : le connecteur doit-il rendre ses
        ``kept`` notices déjà reçues au lieu de propager ``exc`` ?

        À appeler dans ``except OperationCancelled as exc`` autour de la
        pagination (ou de la boucle d'enrichissement) : sur un vrai, le
        connecteur sort de sa boucle (aucune page supplémentaire) et rend ses
        notices ; sur un faux, il propage (``raise``).

        Vrai seulement si ``soft_stop_applies(exc)`` ET au moins une notice
        est conservée : le compte rendu passe alors ``partial``, motif
        ``deadline`` (``SearchReport.truncated``), avec ``detail`` ; une
        troncature déjà posée pour un autre motif (fenêtre du fournisseur…)
        est gardée dans les avertissements, un arrêt ``deadline`` déjà posé
        reste le motif, une erreur déjà posée prime (``detail`` va alors aux
        avertissements). Sans notice, l'arrêt est propagé : l'orchestrateur
        garde son statut historique (``error`` ou ``skipped``, motif
        ``deadline``, pages reçues nommées).

        Args:
            exc: l'annulation reçue.
            report: compte rendu de la recherche (``self.last_status`` si
                None ; un compte rendu neuf s'il n'y en a aucun).
            kept: nombre de notices déjà reçues et rendues.
            detail: phrase du compte rendu (défaut : pages reçues).
            during_enrichment: arrêt pendant l'enrichissement OAI (la
                recherche Isidore est terminée) ; s'il devient le motif de
                l'arrêt, le compteur ``SOFT_STOP_DURING_ENRICHMENT`` est posé
                (revue OPT-09 ter) pour que l'orchestrateur ne dise pas
                « collecte interrompue ».
        """
        if kept <= 0 or not self.soft_stop_applies(exc):
            return False
        if report is None:
            report = self.last_status
        if report is None:
            report = self._start_report()
        text = detail or (
            f"collecte interrompue par l'échéance souple après "
            f"{report.pages} page(s) reçue(s) : {kept} notice(s) conservée(s), "
            f"aucune page supplémentaire demandée")
        if report.status == STATUS_PARTIAL and report.reason == REASON_DEADLINE:
            # Déjà arrêtée par l'échéance (parcours Isidore d'une sous-source,
            # puis son enrichissement) : le premier arrêt reste le motif.
            report.warn(text)
        elif report.status in (None, STATUS_PARTIAL):
            if report.status == STATUS_PARTIAL and report.detail:
                # Troncature antérieure (fenêtre, borne) : dite, pas effacée.
                report.warn(report.detail)
            report.truncated(REASON_DEADLINE, text)
            if during_enrichment:
                report.counters[SOFT_STOP_DURING_ENRICHMENT] = 1
        else:
            # Erreur ou saut déjà posés : ils priment (``SearchReport._set``),
            # l'arrêt est seulement signalé.
            report.warn(text)
        logger.warning(f"[{report.source or self.SOURCE_NAME}] Échéance souple : "
                       f"{kept} notice(s) déjà reçue(s) conservée(s)")
        return True

    def close(self):
        """Libère les ressources si le client HTTP a été créé localement."""
        if self._owns_client:
            self.http.close()

    def __enter__(self):
        """Context manager : permet l'usage with."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Fermeture en sortie de bloc with."""
        self.close()

