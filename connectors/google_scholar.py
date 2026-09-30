"""
Connecteur Google Scholar via SerpAPI (serpapi.com) — clé requise.

Google Scholar n'a pas d'API publique et son interface web interdit le
moissonnage automatisé. SerpAPI exécute la requête et renvoie les résultats en
JSON. Le connecteur ne parle donc jamais à Google : il parle à SerpAPI, et
hérite de ses contraintes.

**Contrainte de coût.** Une requête SerpAPI = une page de résultats (20
notices au plus) = un crédit facturé. Le compte utilisé est une offre payante
à 5 000 requêtes par mois, partagée avec le projet MyWebIntelligence (voir
``config.py``). Toutes les autres sources du projet sont gratuites et
plafonnées par le seul ``max_results`` ; ici le plafond est double :
``max_results`` *et* ``SERPAPI_MAX_PAGES`` (défaut 10, variable
``MOISSON_SERPAPI_MAX_PAGES``, soit 200 notices pour 10 crédits au plus par
appel). Le connecteur refuse en outre de démarrer si le solde du compte passe
sous ``SERPAPI_MIN_CREDITS`` (défaut 100, variable
``MOISSON_SERPAPI_MIN_CREDITS``) : la vérification passe par l'Account API,
qui est gratuite et hors quota. La source n'appartient ni au groupe
``international`` ni au groupe ``all`` (``METERED_SOURCES``,
``connectors/registry.py``), pour qu'aucune moisson de routine ne consomme le
quota à l'insu de l'appelant : elle ne se déclenche que nommée
(``google_scholar``) ou par le groupe dédié ``scholar``.

**Syntaxe de requête.** Le connecteur reçoit la requête traduite par le plan
de requête (``utils.query_plan``, syntaxe ``scholar`` : expressions exactes
entre guillemets, ``OR``, exclusion ``-terme`` ; parenthèses non documentées).
D'après la documentation de SerpAPI et l'aide de Google Scholar, ``author:``,
``source:`` et ``allintitle:`` sont aussi reconnus ; ce comportement est
documenté, non vérifié en direct (source facturée, jamais appelée pour une
vérification : ``connectors/capabilities.py``). Scholar n'est pas la seule
source à honorer ces opérateurs : les sondes du 2026-09-29 montrent que HAL
et OpenAlex honorent guillemets, AND/OR/NOT et parenthèses, Semantic Scholar
guillemets, ``+``, ``|``, ``-`` et parenthèses, Isidore guillemets et
ET/OU/SAUF ; seul CrossRef traite la requête en sac de mots (tableau des
capacités de ``docs/API_SPECS.md``).

**Contrainte de métadonnées.** Scholar ne renvoie ni DOI, ni résumé, ni liste
d'auteurs complète. Voir ``mappers/google_scholar_mapper.py`` pour le détail de
ce qui est extrait, de ce qui est laissé vide, et des drapeaux de fiabilité
posés dans ``extra``.

Ce que la source apporte malgré cela : une couverture que les bases à DOI n'ont
pas (actes, chapitres, littérature grise, dépôts institutionnels, thèses), le
plurilinguisme réel, et un compteur de citations utilisable pour hiérarchiser
un corpus.

Base : https://serpapi.com/search.json?engine=google_scholar
Documentation : https://serpapi.com/google-scholar-api (vérifier annuellement)
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional

from config import (
    ENV_SERPAPI_KEY, REQUEST_DELAY_SERPAPI, SERPAPI_ACCOUNT, SERPAPI_MAX_PAGES,
    SERPAPI_MIN_CREDITS, SERPAPI_PAGE_SIZE, SERPAPI_SEARCH,
)
from connectors.base import Connector
from engine.models import REASON_BUDGET, REASON_CREDITS
from mappers.google_scholar_mapper import map_scholar_item
from utils.http import OperationCancelled
from utils.http_scholarly import BudgetExhausted, get_json, served_from_cache
from utils.secrets import get_api_key

logger = logging.getLogger(__name__)

# Plafond dur de l'API Google Scholar de SerpAPI (paramètre `num`).
MAX_PAGE_SIZE = 20


class SerpAPIQuotaExhausted(RuntimeError):
    """Solde de requêtes SerpAPI insuffisant pour lancer la moisson."""


class GoogleScholarConnector(Connector):
    """Connecteur Google Scholar (passerelle SerpAPI, facturé à la requête)."""

    SOURCE_NAME = "Google Scholar"

    def __init__(
        self,
        http_client=None,
        api_key: Optional[str] = None,
        min_interval: float = REQUEST_DELAY_SERPAPI,
        max_pages: int = SERPAPI_MAX_PAGES,
        page_size: int = SERPAPI_PAGE_SIZE,
        min_credits: int = SERPAPI_MIN_CREDITS,
        hl: str = "en",
        lr: Optional[str] = None,
        include_patents: bool = False,
        **kwargs,
    ):
        """
        Args:
            api_key: Clé SerpAPI ; à défaut, lue dans ``SERPAPI_API_KEY``.
            min_interval: Espacement minimal entre deux requêtes (secondes).
            max_pages: Plafond de requêtes facturées par appel à ``search``.
            page_size: Résultats par requête (borné à 20 par l'API).
            min_credits: Solde en deçà duquel la moisson est refusée ;
                0 désactive la vérification préalable.
            hl: Langue d'interface Google Scholar (influence le classement).
            lr: Restriction de langue des documents (``lang_fr``, ``lang_en``,
                ou plusieurs séparées par ``|``). ``None`` = sans restriction.
            include_patents: Inclure les brevets (``as_sdt=7``). Faux par
                défaut : hors périmètre SHS.
        """
        super().__init__(http_client=http_client, **kwargs)
        # Clé requise ; absente → run_source capture et désactive la source.
        self.api_key = api_key or get_api_key(ENV_SERPAPI_KEY, required=True)
        # Espacement minimal entre deux accès réseau réels à SerpAPI, appliqué
        # par la politesse par hôte et par compte de utils.http : le compte
        # (empreinte de la clé, jamais la clé) est partagé entre threads, et
        # une page déjà en cache ne coûte ni crédit ni attente.
        self.min_interval = min_interval
        self.max_pages = max(1, int(max_pages))
        self.page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        self.min_credits = int(min_credits)
        self.hl = hl
        self.lr = lr
        self.include_patents = include_patents

    # --- Quota ---------------------------------------------------------

    def account_status(self) -> Dict:
        """
        Interroge l'Account API SerpAPI (gratuite, hors quota).

        Returns:
            Le corps JSON du compte (``plan_name``, ``searches_per_month``,
            ``total_searches_left``, ``this_month_usage``…), ou ``{}`` si
            l'appel échoue : l'indisponibilité de cette API ne doit jamais
            faire échouer une moisson.

        Le solde n'est jamais lu ni écrit dans le cache HTTP (``use_cache=
        False``) : un solde mis en cache pour 24 h laisserait passer une
        moisson sur un compte déjà vidé, par ce projet ou par
        MyWebIntelligence.
        """
        try:
            return get_json(
                SERPAPI_ACCOUNT, params={"api_key": self.api_key},
                session=self.http.session, timeout=30, max_retries=2,
                use_cache=False, **self._cancel_kwargs(),
            ) or {}
        except Exception as e:  # noqa: BLE001 - diagnostic non bloquant
            logger.warning(f"[Google Scholar] Solde indisponible : {e}")
            return {}

    def _check_credits(self, planned: int) -> None:
        """
        Refuse la moisson si le solde ne couvre pas ``planned`` requêtes.

        Raises:
            SerpAPIQuotaExhausted: Solde connu et inférieur au plancher.
        """
        if self.min_credits <= 0:
            return
        status = self.account_status()
        left = status.get("total_searches_left")
        if left is None:
            return  # solde inconnu : on laisse passer, l'API arbitrera
        logger.info(
            f"[Google Scholar] Solde SerpAPI : {left} requêtes restantes "
            f"({status.get('plan_name', 'offre inconnue')}) — "
            f"{planned} requête(s) prévue(s) pour cette moisson"
        )
        if left < max(self.min_credits, planned):
            raise SerpAPIQuotaExhausted(
                f"solde insuffisant ({left} restantes, {planned} nécessaires, "
                f"plancher {self.min_credits})"
            )

    # --- Moisson -------------------------------------------------------

    def search(
        self,
        query: str,
        max_results: int = 1000,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        **kwargs,
    ) -> List[Dict]:
        """
        Moissonne Google Scholar pour ``query``. Retourne des items Zotero JSON.

        ``query`` est la requête que le plan de requête (``utils.query_plan``)
        a déjà traduite dans la syntaxe ``scholar`` : expressions exactes entre
        guillemets, ``OR``, exclusion ``-terme``. Le connecteur la transmet à
        SerpAPI sans la retoucher. D'après la documentation (comportement non
        vérifié en direct, source facturée), Google Scholar reconnaît aussi
        ``author:`` et ``source:``. Parmi les autres sources, seul CrossRef
        traite la requête en sac de mots (voir la docstring du module et
        ``connectors/capabilities.py``).

        Args:
            query: Requête issue du plan de requête, syntaxe ``scholar``.
            max_results: Plafond d'items ; effectivement borné par
                ``max_pages × page_size``.
            from_date: Année de début (``as_ylo``) ; seuls les 4 premiers
                caractères sont retenus.
            to_date: Année de fin (``as_yhi``).

        Returns:
            Liste d'items Zotero (vide en cas d'échec ou de quota insuffisant).
        """
        report = self._start_report()   # compte rendu (OPT-07)
        pages = min(
            self.max_pages,
            max(1, math.ceil(max_results / self.page_size)),
        )
        effective_cap = pages * self.page_size
        if max_results > effective_cap:
            logger.warning(
                f"[Google Scholar] max_results={max_results} ramené à "
                f"{effective_cap} par le plafond de {pages} requête(s) "
                f"facturée(s) (MOISSON_SERPAPI_MAX_PAGES)"
            )
            report.warn(
                f"plafond ramené à {effective_cap} notices par la limite de "
                f"{pages} requête(s) facturée(s) (MOISSON_SERPAPI_MAX_PAGES)"
            )

        try:
            self._check_credits(pages)
        except SerpAPIQuotaExhausted as e:
            logger.error(f"[Google Scholar] Moisson annulée — {e}")
            # Non exécutée : aucun crédit consommé (solde sous le plancher).
            report.skip(REASON_CREDITS, str(e))
            return []

        base = {
            "engine": "google_scholar",
            "q": query,
            "api_key": self.api_key,
            "num": self.page_size,
            "hl": self.hl,
            "as_sdt": "7" if self.include_patents else "0",
        }
        if self.lr:
            base["lr"] = self.lr
        if from_date:
            base["as_ylo"] = str(from_date)[:4]
        if to_date:
            base["as_yhi"] = str(to_date)[:4]

        items: List[Dict] = []
        spent = 0
        cached = 0

        try:
            for page in range(pages):
                self.check_cancelled()      # annulation entre deux pages
                params = dict(base, start=page * self.page_size)
                # Une page servie par le cache HTTP ne coûte aucun crédit : elle
                # n'est pas comptée comme requête facturée (faute d'information
                # sur la réponse, la page est comptée, par prudence).
                origin = {"from_cache": False}
                try:
                    payload = get_json(
                        SERPAPI_SEARCH, params=params,
                        session=self.http.session, timeout=60,
                        min_interval=self.min_interval,
                        on_response=lambda r: origin.update(
                            from_cache=served_from_cache(r)),
                        **self._cancel_kwargs(),
                    )
                    if origin["from_cache"]:
                        cached += 1
                    else:
                        spent += 1
                    report.counters["billed_requests"] = spent
                    report.counters["cached_pages"] = cached
                except BudgetExhausted as e:
                    logger.error(f"[Google Scholar] Arrêt (quota épuisé) — {e}")
                    report.fail_with(e)
                    break
                except Exception as e:
                    # Le message d'erreur requests contient l'URL, donc la clé :
                    # on ne journalise que le type et le statut éventuel.
                    status = getattr(getattr(e, "response", None), "status_code", "")
                    logger.error(
                        f"[Google Scholar] Échec requête (page {page + 1}, "
                        f"{type(e).__name__}{f' HTTP {status}' if status else ''})"
                    )
                    report.fail_with(e)
                    break

                # SerpAPI répond 200 avec une clé `error` quand Scholar ne renvoie
                # rien : cas nominal de fin de pagination, pas une panne.
                if payload.get("error") and not payload.get("organic_results"):
                    logger.info(
                        f"[Google Scholar] Fin de pagination : {payload['error']}"
                    )
                    report.done()
                    break

                batch = payload.get("organic_results") or []
                if not batch:
                    report.done()
                    break
                report.page(len(batch))

                for raw in batch:
                    try:
                        items.append(map_scholar_item(raw))
                    except Exception as e:
                        report.bump("mapping_errors")
                        logger.warning(f"[Google Scholar] Échec mapping : {e}")
                    if len(items) >= max_results:
                        logger.info(
                            f"[Google Scholar] Terminé : {len(items)} items, "
                            f"{spent} requête(s) facturée(s)"
                        )
                        report.limit()
                        return items[:max_results]

                total = (payload.get("search_information") or {}).get(
                    "total_results"
                )
                logger.info(
                    f"[Google Scholar] {len(items)} items cumulés "
                    f"(total annoncé {total}), {spent} requête(s) facturée(s)"
                )

                # Scholar a rendu une page incomplète : il n'y a rien après.
                if len(batch) < self.page_size:
                    report.done()
                    break
            else:
                # Toutes les pages permises ont été lues sans fin de résultats.
                if max_results > effective_cap:
                    # Le plafond de requêtes facturées (budget serveur) a tronqué
                    # le périmètre demandé : source partielle, jamais « complète ».
                    report.truncated(
                        REASON_BUDGET,
                        f"plafond de {pages} requête(s) facturée(s) "
                        f"(MOISSON_SERPAPI_MAX_PAGES) atteint : {len(items)} "
                        f"notice(s) sur {max_results} demandée(s)",
                    )
                else:
                    # Toutes les pages nécessaires au plafond demandé ont été
                    # lues (seules des notices non lues au mappage expliquent le
                    # manque) : plafond atteint, comme avant.
                    report.limit()
        except OperationCancelled as exc:
            # Échéance souple (OPT-09 ter, RunContext.soft_stop) : les pages
            # déjà reçues (et facturées) sont rendues, aucune page de plus n'est
            # demandée ; toute autre annulation est propagée.
            if not self.soft_stopped(exc, report, len(items)):
                raise

        logger.info(
            f"[Google Scholar] Terminé : {len(items)} items collectés, "
            f"{spent} requête(s) facturée(s)"
        )
        return items[:max_results]


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    c = GoogleScholarConnector()
    print("Compte :", {
        k: v for k, v in c.account_status().items()
        if k in ("plan_name", "searches_per_month", "total_searches_left",
                 "this_month_usage")
    })
    res = c.search(
        '"large language models" discourse analysis',
        max_results=20, from_date="2022", to_date="2026",
    )
    print(f"{len(res)} items")
    for it in res[:3]:
        print("-", it.get("title"), "|", it.get("date"), "|", it.get("DOI"))
