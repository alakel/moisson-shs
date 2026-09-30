"""
Connecteur CrossRef — API REST native, sans authentification.

~160 M d'enregistrements ; recherche plein-texte sur les métadonnées via
``query.bibliographic``. Pas d'authentification : on rejoint le « polite pool »
(infrastructure plus stable) en déclarant un ``mailto``. Pagination profonde
par ``cursor=*`` (l'``offset`` est plafonné à 10 000).

Classement (OPT-12 ; vérifications live du 2026-09-29, pool poli, requête de
référence depuis 2018, 16 746 résultats) :

- sous ``cursor``, SANS paramètre ``sort``, les notices NE sont PAS classées
  par pertinence : scores 8,8 / 9,3 / 9,5 / 8,9… dans le désordre, DOI dans
  l'ordre de l'index (mêmes DOI d'un appel à l'autre : ordre déterministe) ;
- ``sort=score&order=desc`` est compatible avec ``cursor`` (page 1 : scores
  25,1 à 22,8 ; page 2 par ``next-cursor`` : 22,5 à 21,6) et ramène des
  notices en rapport avec la requête ;
- le tri par date (``published``, ``issued``…) est refusé sous ``cursor``
  (400 ``sort-criteria-incompatible-with-cursor``) ;
- le score n'est renvoyé que s'il figure dans ``select`` ;
- l'API n'a pas de filtre de langue (400 ``filter-not-available``) :
  ``languages`` est ignoré (filtre local de l'orchestrateur).

D'où : en mode ``bibliographie``, ``sort=score&order=desc`` ; en mode
``collecte`` (et sans mode), requête historique sans tri (ordre de l'index,
déterministe). Dans les deux modes, ``score`` est demandé et le rang natif et
le score de chaque notice vont dans le compte rendu
(``SearchReport.native_ranks``), jamais dans la notice Zotero.

Endpoint : https://api.crossref.org/works
Documentation : https://api.crossref.org/swagger-ui/index.html (vérifier annuellement)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from connectors.base import Connector
from config import CONTACT_EMAIL, CROSSREF_API
from engine.models import MODE_BIBLIOGRAPHIE
from mappers.crossref_mapper import map_crossref_item
from utils.http import OperationCancelled

logger = logging.getLogger(__name__)

# Champs demandés pour réduire la charge utile (select=). N'inclure QUE des
# champs valides pour la route /works : un champ inconnu fait échouer toute la
# requête en HTTP 400 (validation-failure). En particulier 'language' n'est PAS
# un champ « select » valide côté CrossRef (vérifié contre l'API) — il est donc
# absent ici ; la langue des items CrossRef reste vide (perte assumée, compensée
# par OpenAlex et la fusion FR/EN). 'score' (pertinence de la notice pour la
# requête, OPT-12) est un champ « select » valide : sans lui, l'API ne le
# renvoie pas (vérifié le 2026-09-29) ; le mappeur l'ignore.
SELECT_FIELDS = ",".join([
    "DOI", "title", "author", "abstract", "container-title",
    "issued", "volume", "issue", "page", "ISSN", "ISBN", "type",
    "URL", "publisher", "subject", "is-referenced-by-count", "score",
])

# Tri en mode bibliographie (OPT-12) : pertinence décroissante, seul tri
# vérifié compatible avec le curseur (cf. docstring du module).
SORT_BIBLIOGRAPHIE = {"sort": "score", "order": "desc"}
# Libellés du tri natif dans le compte rendu.
NATIVE_SORT_BIBLIOGRAPHIE = "score:desc"
NATIVE_SORT_COLLECTE = "cursor_default"

# Taille de lot (OPT-09) : le maximum de l'API, borné par le plafond demandé
# (``rows = min(ROWS_PER_BATCH, max_results)``, fixe pour toute la pagination
# par curseur). Mesure live du 2026-09-28 (21 h 17 à 21 h 19, pool poli,
# requête de référence, 16 744 résultats depuis 2018) : la durée d'une
# requête est dominée par le temps de traitement du serveur, très variable
# (0,7 à 2,2 s) et presque indépendante de la taille du lot (médianes : 1,39 s
# pour 100 notices, 0,99 s pour 200, 1,77 s pour 500, 1,97 s pour 1000 ; 17 Ko
# à 278 Ko transférés), alors que la limite du pool poli porte sur le nombre
# de requêtes (3 par seconde). 1000 notices : environ 2 s en une requête,
# contre une dizaine de secondes en dix requêtes de 100 ; 200 notices (défaut
# MCP) : une requête au lieu de deux. Aucune erreur observée à 1000.
ROWS_PER_BATCH = 1000


def _as_date(value: str, start: bool) -> str:
    """Normalise 'YYYY' en 'YYYY-01-01' (début) ou 'YYYY-12-31' (fin)."""
    value = str(value).strip()
    if len(value) == 4 and value.isdigit():
        return f"{value}-01-01" if start else f"{value}-12-31"
    return value


class CrossrefConnector(Connector):
    """Connecteur pour CrossRef (recherche bibliographique plein-texte)."""

    SOURCE_NAME = "CrossRef"

    def __init__(self, http_client=None, contact_email: Optional[str] = None,
                 **kwargs):
        super().__init__(http_client=http_client, **kwargs)
        self.contact_email = contact_email or CONTACT_EMAIL
        self.headers = {
            "User-Agent": f"moisson-shs/1.0 (mailto:{self.contact_email})"
        }

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
        Moissonne CrossRef pour ``query``. Retourne des items Zotero JSON.

        Args:
            mode: ``bibliographie`` : ``sort=score&order=desc`` ; sinon ordre
                du curseur sans tri (historique).
            **kwargs: options ignorées (dont ``languages`` : pas de filtre de
                langue dans l'API).
        """
        items: List[Dict] = []
        cursor = "*"
        ranked = mode == MODE_BIBLIOGRAPHIE

        filters = []
        if from_date:
            filters.append(f"from-pub-date:{_as_date(from_date, start=True)}")
        if to_date:
            filters.append(f"until-pub-date:{_as_date(to_date, start=False)}")

        batch_num = 0
        # Lot fixe pour toute la pagination : jamais plus que le plafond.
        rows = max(1, min(ROWS_PER_BATCH, int(max_results)))
        report = self._start_report()   # compte rendu (OPT-07)
        report.sorted_by(NATIVE_SORT_BIBLIOGRAPHIE if ranked else NATIVE_SORT_COLLECTE)
        received = 0                    # notices servies, dans l'ordre natif
        try:
            while len(items) < max_results:
                self.check_cancelled()      # annulation entre deux pages
                batch_num += 1
                params = {
                    "query.bibliographic": query,
                    "rows": rows,
                    "cursor": cursor,
                    "select": SELECT_FIELDS,
                    # Polite pool : identification par mailto
                    "mailto": self.contact_email,
                }
                if ranked:
                    params.update(SORT_BIBLIOGRAPHIE)
                if filters:
                    params["filter"] = ",".join(filters)

                try:
                    response = self.http.get(
                        CROSSREF_API, params=params, headers=self.headers
                    )
                    message = response.json().get("message", {})
                except Exception as e:
                    logger.error(f"[CrossRef] Échec du lot {batch_num} : {e}")
                    report.fail_with(e)
                    break

                batch = message.get("items", [])
                if not batch:
                    logger.info("[CrossRef] Lot vide, fin de pagination")
                    report.done()
                    break
                report.page(len(batch))

                for index, raw in enumerate(batch):
                    try:
                        items.append(map_crossref_item(raw))
                    except Exception as e:
                        report.bump("mapping_errors")
                        logger.warning(
                            f"[CrossRef] Échec mapping {raw.get('DOI', '?')} : {e}"
                        )
                        continue
                    report.rank(received + index + 1, raw.get("score"))
                    if len(items) >= max_results:
                        logger.info(f"[CrossRef] Plafond {max_results} atteint")
                        report.limit()
                        report.keep_ranks(max_results)
                        return items[:max_results]
                received += len(batch)

                cursor = message.get("next-cursor")
                if not cursor:
                    report.done()
                    break
                logger.info(f"[CrossRef] {len(items)} items cumulés")
        except OperationCancelled as exc:
            # Échéance souple (OPT-09 ter, RunContext.soft_stop) : les notices
            # des pages déjà reçues sont rendues, aucune page de plus n'est
            # demandée ; toute autre annulation est propagée.
            if not self.soft_stopped(exc, report, len(items)):
                raise

        logger.info(f"[CrossRef] Terminé : {len(items)} items collectés")
        report.keep_ranks(max_results)
        return items[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    res = CrossrefConnector().search(
        "artificial intelligence regulation",
        max_results=20, from_date="2022", to_date="2026",
    )
    print(f"{len(res)} items")
    for it in res[:3]:
        print("-", it.get("title"), "|", it.get("DOI"))
