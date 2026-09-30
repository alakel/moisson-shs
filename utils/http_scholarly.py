"""
GET JSON avec gestion de quota/budget pour les sources internationales.

Couche dédiée aux sources à quota (OpenAlex, CORE, SerpAPI) et à la
résolution de DOI CrossRef. Elle applique la **politique HTTP commune**
(``utils.http.policy_get`` : une tentative sur 4xx définitif, réessais bornés
sur 429/5xx avec ``Retry-After`` et backoff, politesse par hôte et par compte,
sommeil uniquement sur accès réseau réel, fusion des requêtes identiques,
compteurs) et y ajoute la distinction propre aux API à budget :

- un 429 *transitoire* (Retry-After court) → attente puis nouvel essai ;
- un *épuisement de budget* (solde nul, Retry-After de plus d'une heure, ou
  402/409) → ``BudgetExhausted``, pour un arrêt propre de la source sans faire
  échouer la moisson globale.

La fonction opère sur une *session* requests fournie par l'appelant, ce qui
permet de réutiliser la session mise en cache de ``HTTPClient`` (option
``--cache`` / ``MOISSON_CACHE``) : les réponses 200 sont alors mises en cache
et rejouables hors ligne, sans temporisation. Sans session, le client partagé
de ``utils.http`` est utilisé (même politique).

Les codes HTTP exacts d'épuisement (402 vs 409 vs 429+solde nul) et les noms
d'en-têtes de quota sont datés de juin 2026 ; à confirmer contre les en-têtes
réels (voir docs/API_SPECS.md).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

import requests

from utils.http import get_default_client, parse_retry_after, policy_get

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
    raw = (getattr(resp, "headers", None) or {}).get(HEADER_REMAINING_USD)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _budget_hook(url: str):
    """
    Contrôle de budget appliqué à chaque réponse avant la politique commune.

    Lève ``BudgetExhausted`` sur 402/409, sur un 429 à solde nul et sur un
    429 dont le Retry-After dépasse une heure ; laisse la politique commune
    traiter tout le reste (réessai du 429 court, 5xx, 4xx définitif).
    """
    def hook(resp) -> None:
        remaining = _remaining_budget(resp)
        if remaining is not None:
            log.debug("Budget restant (OpenAlex) : %s USD", remaining)

        status = resp.status_code
        if status in (402, 409):
            raise BudgetExhausted(
                f"{status} sur {url} : {str(getattr(resp, 'text', ''))[:200]}"
            )
        if status == 429:
            # Solde explicitement nul → inutile de réessayer avant le reset
            if remaining is not None and remaining <= 0:
                raise BudgetExhausted("Solde de budget à 0 (en-tête).")
            wait = parse_retry_after(
                (getattr(resp, "headers", None) or {}).get("Retry-After")
            )
            # Retry-After > 1 h : reset quotidien, abandon propre
            if wait is not None and wait > _LONG_RESET_THRESHOLD:
                raise BudgetExhausted(f"Reset long ({wait:.0f}s) sur {url}.")
    return hook


def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout=None,
    max_retries: Optional[int] = None,
    base_backoff: Optional[float] = None,
    session: Optional[requests.Session] = None,
    min_interval: Optional[float] = None,
    use_cache: bool = True,
    budget: Optional[float] = None,
    cancel: Any = None,
    on_response: Optional[Callable[[requests.Response], None]] = None,
) -> dict:
    """
    GET JSON robuste et budget-aware.

    Comportement par code de statut :
    - 2xx : retourne le JSON décodé.
    - 402 / 409 : crédits de test épuisés / paiement requis → BudgetExhausted.
    - 429 : si solde nul (en-tête) ou Retry-After de plus d'une heure →
            BudgetExhausted ; sinon attente (Retry-After ou backoff
            exponentiel) puis réessai, selon la politique commune.
    - 5xx transitoire : backoff exponentiel puis réessai.
    - autres 4xx : ``requests.HTTPError`` dès la première tentative (erreur
      de requête, non transitoire).

    Args:
        url: URL cible.
        params: Paramètres de query string.
        headers: En-têtes par requête (fusionnés avec ceux de la session).
        timeout: ``None`` (``config.HTTP_TIMEOUT``), tuple ``(connexion,
            lecture)`` ou scalaire (lecture seule ; connexion bornée par
            ``config.HTTP_CONNECT_TIMEOUT``).
        max_retries: Nombre maximal de tentatives sur erreurs transitoires
            (défaut ``config.HTTP_MAX_RETRIES``, commun à toutes les sources).
        base_backoff: Backoff de base (secondes) pour le délai exponentiel
            (défaut ``config.HTTP_BACKOFF_FACTOR``).
        session: Session requests à réutiliser (cache HTTP). Par défaut, la
            session du client partagé ``utils.http.get_default_client()``.
        min_interval: Intervalle de politesse propre à cet appel (défaut :
            celui de l'hôte).
        use_cache: ``False`` : ni lecture ni écriture en cache (ex. solde
            d'un compte, qui doit toujours être frais).
        budget: Budget global de la requête logique (secondes), attentes et
            réessais compris (défaut : celui de la politique commune).
        cancel: Signal d'annulation de l'exécution (``utils.http.
            CancelSignal`` ou objet à ``is_set()``) : attentes interruptibles,
            ``OperationCancelled`` à l'annulation.
        on_response: Fonction appelée avec la réponse obtenue (statut < 400)
            avant le décodage, par exemple pour savoir si elle vient du cache
            (``from_cache``) : une page de SerpAPI servie par le cache ne
            coûte aucun crédit.

    Returns:
        Le corps JSON décodé (dict).

    Raises:
        BudgetExhausted: Budget/quota épuisé (arrêt propre attendu côté appelant).
        requests.HTTPError: Échec non transitoire ou retries épuisés.
        ValueError: corps de réponse non JSON.
    """
    sess = session if session is not None else get_default_client().session
    resp = policy_get(
        sess, url, params=params, headers=headers, timeout=timeout,
        max_attempts=max_retries, backoff_factor=base_backoff,
        min_interval=min_interval, use_cache=use_cache,
        response_hook=_budget_hook(url), budget=budget, cancel=cancel,
    )
    if on_response is not None:
        on_response(resp)
    return resp.json()


def served_from_cache(response: Any) -> bool:
    """
    Vrai si la réponse a été servie par le cache sans accès réseau.

    Une revalidation conditionnelle (304) a interrogé le serveur : elle
    compte comme un accès réseau (même règle que les compteurs de
    ``utils.http``).
    """
    return (bool(getattr(response, "from_cache", False))
            and getattr(response, "revalidated", False) is not True)
