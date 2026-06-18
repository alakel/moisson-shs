"""
Client HTTP robuste avec retry exponentiel et cache disque optionnel.

Ce module encapsule la logique de résilience face aux erreurs
transitoires des API publiques (timeouts, 502, 503, 429). Il
applique un backoff exponentiel et respecte les délais de
civilité configurés globalement.

L'usage d'une session requests partagée permet la réutilisation
des connexions TCP, ce qui réduit la latence sur les requêtes
en série et limite la charge sur les serveurs distants.

Cache HTTP : activable via le constructeur (use_cache=True) ou
la variable d'environnement MOISSON_CACHE=1. Le cache est persistant
sur disque (SQLite via requests-cache) et applique un TTL configurable
(MOISSON_CACHE_TTL en secondes, défaut 86400 = 24h). Utile pour :
- accélérer les ré-exécutions identiques (< 30 s pour 500 items)
- limiter la charge sur les API publiques pendant les sessions debug
- rejouer une moisson sans réseau (mode offline)
"""

import logging
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional, Dict, Any

import requests
from requests.adapters import HTTPAdapter

from config import (
    HTTP_TIMEOUT,
    HTTP_MAX_RETRIES,
    HTTP_BACKOFF_FACTOR,
    USER_AGENT,
    REQUEST_DELAY,
)

# Configuration du logger pour tracer les échecs réseau
logger = logging.getLogger(__name__)


class HTTPClient:
    """
    Wrapper autour de requests.Session implémentant un retry
    exponentiel avec respect des codes de statut HTTP.

    Les codes 429 (Too Many Requests) et 5xx déclenchent une
    nouvelle tentative ; les codes 4xx (sauf 429) sont remontés
    immédiatement car ils traduisent une erreur de requête côté
    client qui ne sera pas résolue par un nouvel essai.
    """

    # Codes HTTP qui justifient un nouvel essai
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, use_cache: Optional[bool] = None,
                 cache_ttl: Optional[int] = None,
                 cache_dir: Optional[str] = None):
        """
        Initialise la session avec les en-têtes par défaut.

        Args:
            use_cache: Active le cache HTTP disque. Si None (défaut),
                lit MOISSON_CACHE (0/1) — désactivé par défaut.
            cache_ttl: TTL du cache en secondes. Si None, lit
                MOISSON_CACHE_TTL (défaut 86400 = 24h).
            cache_dir: Répertoire de cache. Si None, lit
                MOISSON_CACHE_DIR ou utilise ~/.cache/moisson_shs/http.
        """
        # Résolution des paramètres de cache
        if use_cache is None:
            use_cache = os.environ.get("MOISSON_CACHE", "0") == "1"
        if cache_ttl is None:
            cache_ttl = int(os.environ.get("MOISSON_CACHE_TTL", str(86400)))
        if cache_dir is None:
            cache_dir = os.environ.get(
                "MOISSON_CACHE_DIR",
                str(Path.home() / ".cache" / "moisson_shs" / "http"),
            )

        self.cache_enabled = use_cache
        if use_cache:
            # Lazy import : requests-cache est optionnel
            try:
                import requests_cache
                cache_path = Path(cache_dir)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.session = requests_cache.CachedSession(
                    cache_name=str(cache_path),
                    backend="sqlite",
                    expire_after=timedelta(seconds=cache_ttl),
                    # Ne cache que les codes succès et redirections stables
                    allowable_codes=[200, 203, 300, 301, 308],
                    # Sépare le cache par URL+params, ignore les en-têtes
                    # volatils (date, etc.)
                    match_headers=False,
                )
                logger.info(
                    f"Cache HTTP activé : {cache_path} (TTL {cache_ttl}s)"
                )
            except ImportError:
                logger.warning(
                    "requests-cache non installé, cache HTTP désactivé. "
                    "Installer avec : pip install requests-cache"
                )
                self.cache_enabled = False
                self.session = requests.Session()
        else:
            self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": USER_AGENT,
            # Accept-Encoding gzip réduit le volume transféré,
            # particulièrement utile pour les gros lots Solr
            "Accept-Encoding": "gzip, deflate",
        })
        # Augmente le pool de connexions pour les requêtes concurrentes
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> requests.Response:
        """
        Effectue un GET avec retry exponentiel.

        Args:
            url: URL cible
            params: Paramètres de query string
            timeout: Timeout en secondes (défaut : HTTP_TIMEOUT)
            headers: En-têtes supplémentaires propres à cette requête
                (ex : Authorization Bearer, x-api-key). Fusionnés par
                requests avec les en-têtes par défaut de la session, sans
                muter celle-ci — sûr quand la session est partagée entre
                plusieurs sources.

        Returns:
            L'objet Response de requests

        Raises:
            requests.HTTPError si l'échec persiste après tous les retries
            requests.RequestException pour les erreurs réseau persistantes
        """
        effective_timeout = timeout or HTTP_TIMEOUT
        last_exception = None

        for attempt in range(HTTP_MAX_RETRIES):
            # Dernière tentative : on NE dort PAS après l'échec (un backoff
            # final ne sert à rien et ne fait qu'allonger le temps total —
            # crucial pour ne pas dépasser le délai d'outil MCP).
            is_last = attempt == HTTP_MAX_RETRIES - 1
            try:
                # On ne transmet `headers` que s'il est fourni : préserve la
                # compat avec toute session ne déclarant pas ce paramètre
                # (requests.Session l'accepte toujours).
                get_kwargs = {"params": params, "timeout": effective_timeout}
                if headers:
                    get_kwargs["headers"] = headers
                response = self.session.get(url, **get_kwargs)

                # Politesse réseau : délai inter-requêtes UNIQUEMENT si
                # la requête a réellement touché le réseau (pas servie
                # par le cache local)
                if attempt == 0 and not self._was_cached(response):
                    time.sleep(REQUEST_DELAY)

                # Si le statut justifie un retry, on attend et on recommence
                if response.status_code in self.RETRYABLE_STATUS:
                    if is_last:
                        # Plus de tentative : on lève l'erreur HTTP réelle
                        # (conserve le code de statut) sans dormir.
                        response.raise_for_status()
                        return response
                    backoff = HTTP_BACKOFF_FACTOR * (2 ** attempt)
                    logger.warning(
                        f"Statut {response.status_code} sur {url} "
                        f"(tentative {attempt + 1}/{HTTP_MAX_RETRIES}), "
                        f"attente {backoff:.1f}s"
                    )
                    time.sleep(backoff)
                    continue

                # Lève une exception sur les codes 4xx non-retryables
                response.raise_for_status()
                return response

            except requests.RequestException as e:
                last_exception = e
                if is_last:
                    logger.warning(
                        f"Échec définitif sur {url}: {type(e).__name__} "
                        f"(tentative {attempt + 1}/{HTTP_MAX_RETRIES})"
                    )
                    break
                backoff = HTTP_BACKOFF_FACTOR * (2 ** attempt)
                logger.warning(
                    f"Exception réseau sur {url}: {type(e).__name__} "
                    f"(tentative {attempt + 1}/{HTTP_MAX_RETRIES}), "
                    f"attente {backoff:.1f}s"
                )
                time.sleep(backoff)

        # Tous les retries ont échoué
        if last_exception:
            raise last_exception
        raise requests.HTTPError(
            f"Échec persistant après {HTTP_MAX_RETRIES} tentatives sur {url}"
        )

    @staticmethod
    def _was_cached(response) -> bool:
        """Renvoie True si la réponse provient du cache local."""
        return getattr(response, "from_cache", False)

    def close(self):
        """Ferme la session HTTP et libère les connexions."""
        self.session.close()

    def __enter__(self):
        """Support du context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Fermeture automatique en sortie de bloc with."""
        self.close()
