"""
Connecteur HAL : recherche par mot-clé via l'API Solr REST.

Deux modes de pagination (OPT-12, constat C25) :

- ``collecte`` (défaut de la CLI, et comportement historique quand aucun mode
  n'est transmis) : ``cursorMark`` avec ``sort=docid asc``, le seul parcours
  exhaustif et reproductible au-delà de 10 000 résultats. Quand
  ``numFound > max_results``, les documents retenus sont les plus anciens
  identifiants de dépôt : c'est voulu (ordre déterministe), pas un classement ;
- ``bibliographie`` (défaut du serveur MCP) : ``sort=score desc,docid asc``
  avec pagination ``start``/``rows``, bornée à ``HAL_RANKED_MAX_RESULTS``
  (10 000) documents : les documents retenus sont ceux de meilleur score. Le
  guide Solr déconseille le tri par score sous ``cursorMark`` en SolrCloud, et
  la vérification live du 2026-09-29 le confirme : le score d'un même
  document varie d'une requête à l'autre (réplicas : 7,370 puis 7,348 pour le
  premier résultat de la requête de référence), si bien qu'un curseur posé
  sur le score pourrait sauter ou répéter des documents. Un document déjà vu
  (même ``docid``) sur une page précédente est écarté.

Dans les deux modes : chaque lot demande ``min(HAL_ROWS_PER_BATCH, reste à
collecter)`` documents, le premier comme le dernier (plus jamais 1 000
documents téléchargés pour en garder 200, y compris au dernier lot quand
``max_results`` dépasse 1 000 ; sous ``cursorMark``, ``rows`` peut varier
d'une page à l'autre, vérifié en live), ``fl`` inclut
``score`` (renvoyé aussi sous ``cursorMark``, vérifié en live), et chaque
notice retenue reçoit son rang natif (position dans l'ordre demandé) et son
score dans le compte rendu (``SearchReport.native_ranks``), jamais dans la
notice Zotero. Filtre de langue natif : ``fq=language_s:(fr OR en)`` (codes
ISO 639-1, vérifié en live : 1 963 documents, 1 929 en fr ou en, 1 695 en fr).

Endpoint : https://api.archives-ouvertes.fr/search/
Documentation : https://api.archives-ouvertes.fr/docs/search
"""

import logging
from typing import Dict, Iterable, List, Optional

from connectors.base import Connector
from engine.models import MODE_BIBLIOGRAPHIE, REASON_SCAN_BOUND
from mappers.hal_mapper import map_hal_to_zotero
from utils.http import OperationCancelled
from config import (
    HAL_SEARCH_URL, HAL_ROWS_PER_BATCH, HAL_FIELDS, HAL_RANKED_MAX_RESULTS,
)

logger = logging.getLogger(__name__)

# Tris demandés à Solr. Bibliographie : score décroissant, départagé par la
# clé unique (ordre stable entre deux requêtes à score égal). Collecte : clé
# unique seule, imposée par cursorMark.
SORT_BIBLIOGRAPHIE = "score desc,docid asc"
SORT_COLLECTE = "docid asc"


def language_codes(languages: Optional[Iterable[str]]) -> List[str]:
    """Codes ISO 639-1 valides (deux lettres, minuscules, sans doublon)."""
    codes: List[str] = []
    for value in languages or ():
        code = str(value or "").strip().lower()
        if len(code) == 2 and code.isalpha() and code.isascii() and code not in codes:
            codes.append(code)
    return codes


class HALConnector(Connector):
    """
    Connecteur pour HAL Archive ouverte.

    La requête utilisateur est interprétée par Solr selon
    sa syntaxe native : les phrases entre guillemets sont
    recherchées exactement, les opérateurs booléens AND/OR/NOT
    sont supportés, et les caractères spéciaux Solr doivent
    être échappés par l'utilisateur si nécessaire.
    """

    SOURCE_NAME = "HAL"

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: str = None,
        to_date: str = None,
        *,
        mode: Optional[str] = None,
        languages: Optional[Iterable[str]] = None,
        **kwargs,
    ) -> List[Dict]:
        """
        Recherche par mot-clé dans HAL.

        Args:
            query: Requête full-text. Exemples :
                - 'discours numérique'
                - '"intelligence artificielle" AND société'
                - 'communication politique -publicité'
            max_results: Plafond du nombre d'items retournés
            from_date: Date minimale (inclusive) au format YYYY-MM-DD
                ou YYYY. Filtre sur producedDate_tdate.
            to_date: Date maximale (inclusive) idem.
            mode: ``bibliographie`` : tri par score, pagination start/rows
                (au plus ``HAL_RANKED_MAX_RESULTS`` documents) ; toute autre
                valeur, ou None : ``collecte`` (cursorMark, docid asc).
            languages: codes ISO 639-1 ; filtre natif ``language_s``.
            **kwargs: options de recherche inconnues, ignorées.

        Returns:
            Liste d'items Zotero (rangs et scores natifs dans
            ``self.last_status.native_ranks``, alignés sur la liste)
        """
        ranked = mode == MODE_BIBLIOGRAPHIE
        # Compte rendu de collecte (OPT-07) : pages, plafond, épuisement,
        # erreur après des pages réussies ; rangs natifs (OPT-12).
        report = self._start_report()
        max_results = int(max_results)
        all_items: List[Dict] = []
        if max_results <= 0:
            # Plafond nul : aucune requête.
            report.limit()
            return all_items

        # Paramètres Solr
        # - q : requête full-text
        # - fl : champs retournés (limite le payload), plus le score
        # - wt : format JSON pour parsing direct
        # - rows : taille du lot, jamais plus que le plafond
        # - sort / start (bibliographie) ou sort / cursorMark (collecte)
        rows = max(1, min(HAL_ROWS_PER_BATCH, max_results))
        params = {
            "q": query,
            "fl": ",".join(list(HAL_FIELDS) + ["score"]),
            "wt": "json",
            "rows": rows,
        }
        filters = []
        # Filtre temporel HAL : fq=producedDate_tdate:[FROM TO TO]
        if from_date or to_date:
            filters.append(self._build_date_filter(from_date, to_date))
            logger.info(f"[HAL] Filtre temporel : {filters[-1]}")
        codes = language_codes(languages)
        if codes:
            filters.append(self._build_language_filter(codes))
            report.filtered("languages", codes)
            logger.info(f"[HAL] Filtre de langue : {filters[-1]}")
        if filters:
            # Une chaîne pour un filtre unique (forme historique), une liste
            # (fq répété) sinon.
            params["fq"] = filters[0] if len(filters) == 1 else filters
        if ranked:
            params["sort"] = SORT_BIBLIOGRAPHIE
            params["start"] = 0
        else:
            params["sort"] = SORT_COLLECTE
            params["cursorMark"] = "*"
        report.sorted_by(params["sort"])

        batch_num = 0
        total_available = None
        received = 0          # documents reçus, dans l'ordre natif
        seen_docids = set()   # bibliographie : doublons entre pages

        try:
            while True:
                # Annulation vérifiée entre deux pages (aucune nouvelle page
                # après sa détection).
                self.check_cancelled()
                # Lot ramené au reste à collecter (revue OPT-12) : le dernier lot
                # ne télécharge plus 1 000 documents pour en garder 200 quand
                # max_results dépasse HAL_ROWS_PER_BATCH. Un manque (doublon de
                # réplica, mappage en échec) se comble par un lot de plus. Valable
                # sous cursorMark : le curseur encode la dernière valeur de tri,
                # rows peut varier d'une page à l'autre (vérifié en live le
                # 2026-09-29 : rows=3 puis rows=2 donnent les mêmes 5 docid que
                # rows=5).
                remaining = max(1, max_results - len(all_items))
                params["rows"] = min(rows, remaining)
                if ranked:
                    room = HAL_RANKED_MAX_RESULTS - params["start"]
                    if room <= 0:
                        report.window(
                            f"mode bibliographie : HAL classé par score, "
                            f"{HAL_RANKED_MAX_RESULTS} documents au plus"
                            + (f" sur {total_available}" if total_available else "")
                            + " (mode collecte pour un parcours exhaustif)",
                            reason=REASON_SCAN_BOUND,
                        )
                        logger.warning(
                            f"[HAL] Borne de la pagination par rang atteinte "
                            f"({HAL_RANKED_MAX_RESULTS} documents)")
                        break
                    params["rows"] = min(params["rows"], room)
                batch_num += 1
                if ranked:
                    logger.info(f"[HAL] Lot {batch_num} (start={params['start']}, "
                                f"rows={params['rows']}, tri par score)")
                else:
                    logger.info(
                        f"[HAL] Lot {batch_num} (cursorMark={params['cursorMark'][:20]}...)"
                    )

                try:
                    response = self.http.get(HAL_SEARCH_URL, params=params)
                    data = response.json()
                except Exception as e:
                    logger.error(f"[HAL] Échec du lot {batch_num}: {e}")
                    report.fail_with(e)
                    break

                # Solr renvoie response.numFound (total) et response.docs (lot)
                response_block = data.get("response", {})
                if total_available is None:
                    total_available = response_block.get("numFound", 0)
                    logger.info(f"[HAL] {total_available} résultats disponibles au total")

                docs = response_block.get("docs", [])
                if not docs:
                    logger.info(f"[HAL] Lot vide, fin de pagination")
                    report.done()
                    break
                report.page(len(docs))

                # Rang natif : position dans l'ordre demandé (start + indice en
                # bibliographie, documents déjà reçus + indice en collecte).
                base = params["start"] if ranked else received
                for index, doc in enumerate(docs):
                    if ranked:
                        docid = doc.get("docid")
                        if docid is not None:
                            if docid in seen_docids:
                                # Réplicas aux scores différents : un document
                                # peut revenir sur la page suivante.
                                report.bump("duplicates")
                                continue
                            seen_docids.add(docid)
                    # Mapping de chaque document vers Zotero
                    try:
                        item = map_hal_to_zotero(doc)
                    except Exception as e:
                        report.bump("mapping_errors")
                        logger.warning(
                            f"[HAL] Échec mapping doc {doc.get('docid', '?')}: {e}"
                        )
                        continue
                    all_items.append(item)
                    report.rank(base + index + 1, doc.get("score"))

                    # Respect du plafond
                    if len(all_items) >= max_results:
                        logger.info(
                            f"[HAL] Plafond {max_results} atteint, arrêt"
                        )
                        report.limit()
                        return all_items
                received += len(docs)

                if ranked:
                    params["start"] += len(docs)
                    if ((total_available is not None and params["start"] >= total_available)
                            or len(docs) < params["rows"]):
                        logger.info(f"[HAL] Résultats épuisés, fin de pagination")
                        report.done()
                        break
                    continue

                # Préparation du lot suivant via cursorMark
                next_cursor = data.get("nextCursorMark")
                if not next_cursor or next_cursor == params["cursorMark"]:
                    logger.info(f"[HAL] Curseur stable, fin de pagination")
                    report.done()
                    break
                params["cursorMark"] = next_cursor
        except OperationCancelled as exc:
            # Échéance souple (OPT-09 ter, RunContext.soft_stop) : les notices
            # des pages déjà reçues sont rendues, aucune page de plus n'est
            # demandée ; toute autre annulation est propagée.
            if not self.soft_stopped(exc, report, len(all_items)):
                raise

        logger.info(f"[HAL] Terminé : {len(all_items)} items collectés")
        return all_items

    @staticmethod
    def _build_language_filter(codes: List[str]) -> str:
        """
        Filtre Solr fq= sur ``language_s`` (codes ISO 639-1 de HAL).

        Returns:
            Chaîne Solr fq, ex : 'language_s:(fr OR en)'
        """
        return f"language_s:({' OR '.join(codes)})"

    @staticmethod
    def _build_date_filter(from_date: str = None, to_date: str = None) -> str:
        """
        Construit le filtre Solr fq= pour le champ producedDate_tdate.

        Args:
            from_date: Borne basse YYYY-MM-DD ou YYYY (None = *)
            to_date: Borne haute YYYY-MM-DD ou YYYY (None = NOW)

        Returns:
            Chaîne Solr fq, ex : 'producedDate_tdate:[2020-01-01T00:00:00Z TO 2023-12-31T23:59:59Z]'
        """
        def _normalize(d, is_end: bool) -> str:
            if not d:
                return "*" if not is_end else "NOW"
            # YYYY → étendre
            if len(d) == 4 and d.isdigit():
                return f"{d}-12-31T23:59:59Z" if is_end else f"{d}-01-01T00:00:00Z"
            # YYYY-MM-DD → étendre avec heure
            if len(d) == 10:
                return f"{d}T23:59:59Z" if is_end else f"{d}T00:00:00Z"
            return d

        lo = _normalize(from_date, is_end=False)
        hi = _normalize(to_date, is_end=True)
        return f"producedDate_tdate:[{lo} TO {hi}]"
