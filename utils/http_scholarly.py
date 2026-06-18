"""
GET HTTP avec gestion de quota/budget pour les sources internationales.

Couche dédiée aux sources à quota (OpenAlex, CORE), distincte du
``utils.http.HTTPClient`` généraliste. Elle gère :

- les réessais transitoires (429 throttling, 5xx) avec backoff exponentiel ;
- la distinction entre un 429 *transitoire* (Retry-After court → on attend
  puis réessaie) et un *épuisement de budget* (solde nul, Retry-After très
  long, ou 402/409 → ``BudgetExhausted`` levé pour un arrêt propre de la
  source sans faire échouer la moisson globale).

La fonction opère sur une *session* requests fournie par l'appelant, ce qui
permet de réutiliser la session mise en cache de ``HTTPClient`` (option
``--cache`` / ``MOISSON_CACHE``) : les réponses 200 sont alors mises en cache
et rejouables hors ligne.

Les codes HTTP exacts d'épuisement (402 vs 409 vs 429+solde nul) et les noms
d'en-têtes de quota sont datés de juin 2026 ; à confirmer contre les en-têtes
réels (voir docs/API_SPECS.md).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests

from config import HTTP_TIMEOUT

log = logging.getLogger("moisson.http_scholarly")

# En-tête de solde prépayé OpenAlex (USD restants sur la journée).
HEADER_REMAINING_USD = "X-RateLimit-Prepaid-Remaining-USD"

# Au-delà de ce délai (secondes), un Retry-After traduit un reset quotidien
# plutôt qu'un throttling : inutile d'attendre, on abandonne proprement.
_LONG_RESET_THRESHOLD = 3600.0


class BudgetExhausted(RuntimeError):
    """Budget/quota quotidien épuisé : réessayer après le reset (minuit UTC)."""


def _remaining_budget(resp: requests.Response) -> Optional[float]:
    """Lit l'en-tête de solde restant (OpenAlex) s'il est présent et numérique."""
    raw = resp.headers.get(HEADER_REMAINING_USD)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout=None,
    max_retries: int = 5,
    base_backoff: float = 1.0,
    session: Optional[requests.Session] = None,
) -> dict:
    """
    GET JSON robuste et budget-aware.

    Comportement par code de statut :
    - 200 : retourne le JSON décodé.
    - 402 / 409 : crédits de test épuisés / paiement requis → BudgetExhausted.
    - 429 : si solde nul (en-tête) ou Retry-After très long → BudgetExhausted ;
            sinon attente (Retry-After ou backoff exponentiel) puis réessai.
    - 5xx : backoff exponentiel puis réessai.
    - autres 4xx : ``raise_for_status`` (erreur de requête, non transitoire).

    Args:
        url: URL cible.
        params: Paramètres de query string.
        headers: En-têtes par requête (fusionnés avec ceux de la session).
        timeout: Timeout requests (défaut : ``config.HTTP_TIMEOUT``).
        max_retries: Nombre maximal de tentatives sur erreurs transitoires.
        base_backoff: Backoff de base (secondes) pour le délai exponentiel.
        session: Session requests à réutiliser (cache HTTP). ``requests`` par
            défaut (pas de cache, pas de réutilisation de connexion).

    Returns:
        Le corps JSON décodé (dict).

    Raises:
        BudgetExhausted: Budget/quota épuisé (arrêt propre attendu côté appelant).
        requests.HTTPError: Échec non transitoire ou retries épuisés.
    """
    sess = session or requests
    effective_timeout = timeout if timeout is not None else HTTP_TIMEOUT
    attempt = 0

    while True:
        attempt += 1
        resp = sess.get(
            url, params=params, headers=headers, timeout=effective_timeout
        )

        remaining = _remaining_budget(resp)
        if remaining is not None:
            log.debug("Budget restant (OpenAlex) : %s USD", remaining)

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code in (402, 409):
            raise BudgetExhausted(
                f"{resp.status_code} sur {url} : {resp.text[:200]}"
            )

        if resp.status_code == 429:
            # Solde explicitement nul → inutile de réessayer avant le reset
            if remaining is not None and remaining <= 0:
                raise BudgetExhausted("Solde de budget à 0 (en-tête).")

            retry_after = resp.headers.get("Retry-After")
            if retry_after and str(retry_after).strip().isdigit():
                wait = float(retry_after)
            else:
                wait = base_backoff * (2 ** (attempt - 1))

            # Retry-After > 1 h : reset quotidien, abandon propre
            if wait > _LONG_RESET_THRESHOLD:
                raise BudgetExhausted(f"Reset long ({wait:.0f}s) sur {url}.")

            log.warning(
                "429 sur %s : attente %.1fs (tentative %d/%d)",
                url, wait, attempt, max_retries,
            )
            if attempt >= max_retries:
                resp.raise_for_status()
                return resp.json()  # pragma: no cover - défensif
            time.sleep(wait)
            continue

        if resp.status_code in (500, 502, 503, 504):
            wait = base_backoff * (2 ** (attempt - 1))
            log.warning(
                "%d sur %s : backoff %.1fs (tentative %d/%d)",
                resp.status_code, url, wait, attempt, max_retries,
            )
            if attempt >= max_retries:
                resp.raise_for_status()
                return resp.json()  # pragma: no cover - défensif
            time.sleep(wait)
            continue

        # Tout autre code (4xx non géré) : erreur de requête, non transitoire.
        resp.raise_for_status()
        return resp.json()  # pragma: no cover - défensif
