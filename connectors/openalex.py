"""
Connecteur OpenAlex — API REST native, clé gratuite requise.

Depuis février 2026, OpenAlex impose une clé API gratuite et un budget
quotidien en USD (clé gratuite : ~1 $/j ; 100 req/s). Recherche plein-texte via
``?search=`` ; pagination ``cursor=*`` → ``meta.next_cursor``. Métadonnées
riches : concepts, ``cited_by_count``, statut OA, identifiants natifs.

La clé (``OPENALEX_API_KEY``) est **requise** : ``__init__`` lève ``MissingAPIKey``
en son absence, et ``main.run_source`` capture l'exception pour désactiver
proprement la source (la moisson des autres sources se poursuit). La couche HTTP
budget-aware (``utils.http_scholarly.get_json``) lit l'en-tête de solde et
distingue throttling transitoire vs épuisement de budget (``BudgetExhausted``).

Classement et langues (OPT-12 ; vérifications live du 2026-09-29, sans clé,
« digital discourse » depuis 2018) : avec ``search=``, l'ordre par défaut est
la pertinence décroissante, conservé sous ``cursor`` (page 1 : 1 036 à 597 ;
page 2 : 521 à 430) ; ``relevance_score`` n'est renvoyé que s'il figure dans
``select`` (ajouté ici ; le mappeur l'ignore). Rang natif (position servie)
et score vont dans le compte rendu (``SearchReport.native_ranks``), jamais
dans la notice Zotero. Le mode (``bibliographie`` / ``collecte``) ne change
rien : l'ordre de pertinence d'OpenAlex est déjà celui qu'on veut tronquer.
Filtre de langue natif ``language:fr|en`` (codes ISO 639-1 ; live :
577 004 résultats, 431 143 en fr ou en, 2 804 en fr, tous ``fr``).

Endpoint : https://api.openalex.org/works
Documentation : https://docs.openalex.org (vérifier annuellement)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from connectors.base import Connector
from connectors.hal import language_codes
from config import ENV_OPENALEX_KEY, OPENALEX_API
from mappers.openalex_mapper import map_openalex_item
from utils.http import OperationCancelled
from utils.http_scholarly import BudgetExhausted, get_json
from utils.secrets import get_api_key

logger = logging.getLogger(__name__)

SELECT_FIELDS = ",".join([
    "id", "doi", "title", "display_name", "publication_year",
    "publication_date", "language", "type", "authorships",
    "primary_location", "abstract_inverted_index", "concepts",
    "cited_by_count", "open_access", "ids", "biblio",
    # Score de pertinence (OPT-12) : présent seulement s'il est demandé.
    "relevance_score",
])

# Ordre natif avec search= (défaut de l'API, vérifié en live).
NATIVE_SORT = "relevance_score:desc"

# Taille de page (OPT-09) : le maximum de l'API, bornée par le plafond
# demandé (``per-page = min(PER_PAGE, max_results)``, fixe pour toute la
# pagination par curseur). Vérification live du 2026-09-28 (21 h 18, sans
# clé, requête de référence) : le coût est facturé par appel et non par
# notice (en-têtes X-RateLimit-Cost-USD 0.001 et X-RateLimit-Credits-Used 10
# pour 100 comme pour 200 notices ; meta.cost_usd identique), et la durée
# d'un appel ne croît pas avec la page (100 : 1,75 et 0,98 s ; 200 : 1,43 et
# 0,98 s). 200 par page divise donc par deux le nombre d'appels, le coût et
# la durée au-delà de 100 notices.
PER_PAGE = 200


def _year(value) -> str:
    s = str(value).strip()
    return s[:4] if len(s) >= 4 and s[:4].isdigit() else ""


class OpenAlexConnector(Connector):
    """Connecteur pour OpenAlex (recherche scholarly + concepts + citations)."""

    SOURCE_NAME = "OpenAlex"

    def __init__(self, http_client=None, api_key: Optional[str] = None, **kwargs):
        super().__init__(http_client=http_client, **kwargs)
        # Clé obligatoire depuis fév. 2026 ; en cas d'absence, run_source
        # capture l'exception et désactive proprement la source.
        self.api_key = api_key or get_api_key(ENV_OPENALEX_KEY, required=True)

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        extra_filters: Optional[str] = None,
        *,
        languages: Optional[List[str]] = None,
        **kwargs,
    ) -> List[Dict]:
        """
        Moissonne OpenAlex.

        Args:
            extra_filters: filtres OpenAlex additionnels
                (ex : "type:article,language:en,is_oa:true").
            languages: codes ISO 639-1 ; filtre natif ``language:fr|en``
                (sans effet si ``extra_filters`` filtre déjà la langue).
            **kwargs: options ignorées (dont ``mode``, cf. docstring du
                module).
        """
        items: List[Dict] = []
        cursor = "*"

        filters = []
        lo = _year(from_date) if from_date else ""
        hi = _year(to_date) if to_date else ""
        if lo and hi:
            filters.append(f"publication_year:{lo}-{hi}")
        elif lo:
            filters.append(f"from_publication_date:{lo}-01-01")
        elif hi:
            filters.append(f"to_publication_date:{hi}-12-31")
        if extra_filters:
            filters.append(extra_filters)
        report = self._start_report()   # compte rendu (OPT-07)
        report.sorted_by(NATIVE_SORT)
        codes = language_codes(languages)
        if codes and "language:" not in (extra_filters or ""):
            filters.append("language:" + "|".join(codes))
            report.filtered("languages", codes)

        # Page fixe pour toute la pagination : jamais plus que le plafond.
        per_page = max(1, min(PER_PAGE, int(max_results)))
        received = 0                    # notices servies, dans l'ordre natif
        try:
            while len(items) < max_results:
                self.check_cancelled()      # annulation entre deux pages
                params = {
                    "search": query,
                    "per-page": per_page,
                    "cursor": cursor,
                    "select": SELECT_FIELDS,
                }
                if self.api_key:
                    params["api_key"] = self.api_key
                if filters:
                    params["filter"] = ",".join(filters)

                try:
                    data = get_json(
                        OPENALEX_API, params=params, session=self.http.session,
                        **self._cancel_kwargs(),
                    )
                except BudgetExhausted as e:
                    logger.error(f"[OpenAlex] Arrêt (budget épuisé) — {e}")
                    report.fail_with(e)
                    break
                except Exception as e:
                    logger.error(f"[OpenAlex] Échec requête : {e}")
                    report.fail_with(e)
                    break

                batch = data.get("results", [])
                if not batch:
                    report.done()
                    break
                report.page(len(batch))
                for index, raw in enumerate(batch):
                    try:
                        items.append(map_openalex_item(raw))
                    except Exception as e:
                        report.bump("mapping_errors")
                        logger.warning(
                            f"[OpenAlex] Échec mapping {raw.get('id', '?')} : {e}"
                        )
                        continue
                    report.rank(received + index + 1, raw.get("relevance_score"))
                    if len(items) >= max_results:
                        report.limit()
                        report.keep_ranks(max_results)
                        return items[:max_results]
                received += len(batch)

                cursor = (data.get("meta") or {}).get("next_cursor")
                if not cursor:
                    report.done()
                    break
                logger.info(f"[OpenAlex] {len(items)} items cumulés")
        except OperationCancelled as exc:
            # Échéance souple (OPT-09 ter, RunContext.soft_stop) : les notices
            # des pages déjà reçues sont rendues, aucune page de plus n'est
            # demandée ; toute autre annulation est propagée.
            if not self.soft_stopped(exc, report, len(items)):
                raise

        logger.info(f"[OpenAlex] Terminé : {len(items)} items collectés")
        report.keep_ranks(max_results)
        return items[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    res = OpenAlexConnector().search(
        "artificial intelligence regulation discourse",
        max_results=50, from_date="2022", to_date="2026",
        extra_filters="type:article",
    )
    print(f"{len(res)} items")
    for it in res[:3]:
        print("-", it.get("title"), "|", it.get("DOI"))
