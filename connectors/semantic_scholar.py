"""
Connecteur Semantic Scholar (Academic Graph) — clé optionnelle (recommandée).

Avec clé : 1 req/s dédiée. Sans clé : pool partagé throttlé. Moissonnage en
volume via l'endpoint *bulk* (``/paper/search/bulk`` : ≤ 1000 résultats par
appel, jeton de continuation ``token``). Déduplication par ``externalIds.DOI``
sinon ``externalIds.CorpusId``.

Ordre des résultats (OPT-12, constat C26 ; vérifications live du
2026-09-29, sans clé, requête « digital discourse ») :

- sans ``sort``, bulk renvoie les résultats par ``paperId`` croissant
  (identifiants de hachage : ordre sans rapport avec la requête), si bien que
  la troncature à ``max_results`` retenait un sous-ensemble arbitraire ;
- ``sort=citationCount:desc`` est accepté et se poursuit par ``token`` (page 2
  toujours décroissante) ; ``publicationDate:desc`` aussi ;
- ``/paper/search`` (classé par pertinence, ``offset + limit`` ≤ 1000,
  ``limit`` ≤ 100) est impraticable sans clé : 8 réponses 429 sur 8 en
  2 min 30, y compris après 90 s de pause, alors que bulk répondait.

D'où, en mode ``bibliographie`` : bulk avec ``sort=S2_BIBLIO_SORT``
(``citationCount:desc`` par défaut ; biais documenté : travaux anciens et
très cités favorisés, pas un classement par pertinence) ; en mode
``collecte`` (et sans mode) : bulk sans tri (``paperId`` croissant,
déterministe, requêtes identiques à l'historique). Rang natif : position
dans l'ordre servi ; score natif : aucun (le nombre de citations reste dans
la notice). L'API n'a pas de filtre de langue : ``languages`` est ignoré (le
filtre est fait localement par l'orchestrateur).

La clé est lue via ``SEMANTIC_SCHOLAR_API_KEY`` (optionnelle).

Base : https://api.semanticscholar.org/graph/v1
Documentation : https://api.semanticscholar.org (vérifier annuellement)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from connectors.base import Connector
from config import ENV_S2_KEY, REQUEST_DELAY_S2, S2_BIBLIO_SORT, S2_GRAPH_API
from engine.models import MODE_BIBLIOGRAPHIE, REASON_MISSING_KEY, keyless_refusal
from mappers.semantic_scholar_mapper import map_s2_item
from utils.http import OperationCancelled
from utils.secrets import get_api_key

logger = logging.getLogger(__name__)

S2_BULK = f"{S2_GRAPH_API}/paper/search/bulk"

FIELDS = ",".join([
    "title", "abstract", "year", "publicationDate", "authors",
    "externalIds", "citationCount", "influentialCitationCount",
    "publicationTypes", "publicationVenue", "venue", "journal",
    "openAccessPdf", "s2FieldsOfStudy", "fieldsOfStudy", "url",
])

# Tris admis par /paper/search/bulk (documentation de l'API ; citationCount
# et publicationDate vérifiés en live le 2026-09-29).
BULK_SORT_FIELDS = ("paperId", "publicationDate", "citationCount")
DEFAULT_BIBLIO_SORT = "citationCount:desc"
# Ordre de l'API sans paramètre sort (vérifié en live le 2026-09-29).
BULK_DEFAULT_ORDER = "paperId"


def bulk_sort(value: Optional[str]) -> str:
    """
    Valeur ``sort`` admise par bulk (``champ`` ou ``champ:asc|desc``) ;
    ``citationCount:desc`` si la valeur configurée n'est pas reconnue.
    """
    text = (value or "").strip()
    field_name, _, direction = text.partition(":")
    if field_name in BULK_SORT_FIELDS and direction in ("", "asc", "desc"):
        return text
    if text:
        logger.warning(f"[S2] Tri {text!r} non reconnu : {DEFAULT_BIBLIO_SORT}")
    return DEFAULT_BIBLIO_SORT


def _year_range(from_date, to_date) -> str:
    lo = str(from_date)[:4] if from_date else ""
    hi = str(to_date)[:4] if to_date else ""
    if lo and hi:
        return f"{lo}-{hi}"
    if lo:
        return f"{lo}-"
    if hi:
        return f"-{hi}"
    return ""


class SemanticScholarConnector(Connector):
    """Connecteur pour Semantic Scholar (bulk search throttlé à 1 req/s)."""

    SOURCE_NAME = "Semantic Scholar"

    def __init__(self, http_client=None, api_key: Optional[str] = None,
                 min_interval: float = REQUEST_DELAY_S2, **kwargs):
        super().__init__(http_client=http_client, **kwargs)
        # Clé optionnelle : si absente, on reste sur le pool partagé.
        self.api_key = api_key or get_api_key(ENV_S2_KEY, required=False)
        self.headers = {"x-api-key": self.api_key} if self.api_key else None
        # Espacement minimal entre deux accès réseau réels à l'API, appliqué
        # par la politesse par hôte et par compte de utils.http (partagée
        # entre threads ; aucune attente sur une réponse servie par le cache).
        self.min_interval = min_interval

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        *,
        mode: Optional[str] = None,
        **kwargs,
    ) -> List[Dict]:
        """
        Moissonne Semantic Scholar en mode bulk. Retourne des items Zotero.

        Args:
            mode: ``bibliographie`` : tri ``S2_BIBLIO_SORT`` (citations
                décroissantes par défaut) ; sinon ordre de l'API (paperId).
            **kwargs: options ignorées (dont ``languages`` : pas de filtre de
                langue dans l'API).
        """
        items: List[Dict] = []
        token = None
        year = _year_range(from_date, to_date)
        report = self._start_report()   # compte rendu (OPT-07)
        sort = bulk_sort(S2_BIBLIO_SORT) if mode == MODE_BIBLIOGRAPHIE else None
        report.sorted_by(sort or BULK_DEFAULT_ORDER)
        received = 0                    # résultats servis, dans l'ordre natif

        try:
            while len(items) < max_results:
                self.check_cancelled()      # annulation entre deux pages
                params = {"query": query, "fields": FIELDS}
                if year:
                    params["year"] = year
                if sort:
                    params["sort"] = sort
                if token:
                    params["token"] = token

                try:
                    response = self.http.get(
                        S2_BULK, params=params, headers=self.headers, timeout=60,
                        min_interval=self.min_interval,
                    )
                    payload = response.json()
                except Exception as e:
                    # 401 ou 403 sans clé, avant toute page : source non
                    # configurée (clé requise), non en panne (PAT-05).
                    refusal = (keyless_refusal(e, bool(self.api_key), ENV_S2_KEY)
                               if report.pages == 0 else None)
                    if refusal:
                        logger.warning(f"[S2] {refusal}")
                        report.skip(REASON_MISSING_KEY, refusal)
                    else:
                        logger.error(f"[S2] Échec requête : {e}")
                        report.fail_with(e)
                    break

                batch = payload.get("data") or []
                if not batch:
                    report.done()
                    break
                report.page(len(batch))
                for index, raw in enumerate(batch):
                    try:
                        items.append(map_s2_item(raw))
                    except Exception as e:
                        report.bump("mapping_errors")
                        logger.warning(f"[S2] Échec mapping : {e}")
                        continue
                    report.rank(received + index + 1)
                    if len(items) >= max_results:
                        report.limit()
                        report.keep_ranks(max_results)
                        return items[:max_results]
                received += len(batch)

                token = payload.get("token")
                logger.info(f"[S2] {len(items)} items cumulés")
                if not token:
                    report.done()
                    break
        except OperationCancelled as exc:
            # Échéance souple (OPT-09 ter, RunContext.soft_stop) : les notices
            # des pages déjà reçues sont rendues, aucune page de plus n'est
            # demandée ; toute autre annulation est propagée.
            if not self.soft_stopped(exc, report, len(items)):
                raise

        logger.info(f"[S2] Terminé : {len(items)} items collectés")
        report.keep_ranks(max_results)
        return items[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    res = SemanticScholarConnector().search(
        "large language models discourse",
        max_results=50, from_date="2022", to_date="2026",
    )
    print(f"{len(res)} items")
    for it in res[:3]:
        print("-", it.get("title"), "|", it.get("DOI"))
