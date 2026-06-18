"""
Interface abstraite pour les connecteurs de sources bibliographiques.

Chaque source (HAL, Isidore, OpenEdition, Cairn) implémente cette
interface, permettant à l'orchestrateur de les manipuler de manière
uniforme. La méthode 'search' renvoie une liste d'items Zotero
validés et prêts à la fusion.

L'approche par classe abstraite plutôt que par fonction permet
de partager le client HTTP, les compteurs et les logs entre
les différentes requêtes paginées d'une même session.
"""

from abc import ABC, abstractmethod
from typing import List, Dict
import logging

from utils.http import HTTPClient

logger = logging.getLogger(__name__)


class Connector(ABC):
    """
    Classe de base pour tous les connecteurs de sources.

    Sous-classes responsables de :
    - implémenter la pagination spécifique à leur API
    - assurer le mapping vers le schéma Zotero via leur mapper dédié
    - tracer le nom de source dans le champ 'extra' Zotero
    """

    # Nom canonique de la source, à surcharger dans chaque sous-classe
    SOURCE_NAME: str = "unknown"

    def __init__(self, http_client: HTTPClient = None, **kwargs):
        """
        Args:
            http_client: Client HTTP partagé (créé si non fourni)
            **kwargs: Options spécifiques au connecteur. Les kwargs non
                reconnus sont ignorés silencieusement. Permet à l'orchestrateur
                d'invoquer tous les connecteurs avec la même signature
                (ex : enrich_oai n'est utilisé que par OpenEdition/Cairn,
                ignoré par HAL/Isidore).
        """
        self.http = http_client or HTTPClient()
        # Indique si le client a été créé ici et doit être fermé
        self._owns_client = http_client is None

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
