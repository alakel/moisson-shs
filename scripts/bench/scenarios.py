"""
Scénarios rejouables du banc (plan d'optimisation, OPT-00).

Chaque scénario définit :

- ``params`` : paramètres de moisson (requête, sources ou groupes, plafond par
  source, dates, et facultativement ``workers`` / ``cache``). Un paramètre
  absent n'est PAS transmis : c'est la valeur par défaut du point d'entrée qui
  s'applique (utile pour mesurer un changement de défaut, OPT-09) ;
- ``entries`` : points d'entrée mesurés par défaut (``mcp``, ``cli``,
  ``tool``) ;
- ``sim`` : configuration du simulateur (univers de travaux, taux
  d'inclusion ou total visé par fournisseur, latences, pannes) ;
- ``live`` : paramètres d'une exécution réelle (None : scénario simulé
  seulement). Le mode live n'appelle que des API gratuites, jamais SerpAPI.

Calibrage de l'univers de référence (« discours numérique »), d'après la
mesure live du 2026-09-28 (copie d'avant chantier, cf. notes OPT-00) :

- taille 266 508 : total annoncé par Isidore (``@items``) ; l'API ne sert
  pourtant que les 200 premiers résultats (fenêtre reproduite par le
  simulateur, ``isidore_max_window``) ;
- composition des 200 premiers résultats Isidore depuis 2018, relevée en live :
  141 notices distinctes, dont OpenEdition 49, Érudit 6, Theses.fr 5, Cairn 4,
  Persée 0, autres 77 ; d'où les poids ci-dessous et des profils d'années qui
  placent ~70 % des notices en 2018 ou après (Persée : fonds ancien) ;
- HAL : 1 962 résultats avec le filtre ``producedDate >= 2018`` (live), soit
  un total non filtré d'environ 2 800 avec ces profils d'années.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

REFERENCE_QUERY = "discours numérique"

# Univers calibré de la requête de référence (cf. docstring).
REFERENCE_UNIVERSE: Dict[str, Any] = {
    "seed": "discours numerique",
    "size": 266508,
    "subsource_weights": {
        "openedition": 0.35, "cairn": 0.029, "persee": 0.01, "erudit": 0.043,
        "theses": 0.036, "hal": 0.25, "autre": 0.30,
    },
    # (début, fin, asymétrie) : P(année >= 2018) ~ 0,70 hors Persée (~0,03).
    "year_profiles": {
        "openedition": [1998, 2026, 3.25], "cairn": [2001, 2026, 2.83],
        "erudit": [1992, 2026, 4.05], "theses": [1985, 2026, 5.0],
        "hal": [1995, 2026, 3.66], "autre": [1970, 2026, 7.0],
        "persee": [1890, 2024, 0.55],
    },
}

REFERENCE_PROVIDERS: Dict[str, Any] = {
    "hal": {"target_total": 2800},
}

# Univers « international » : travaux majoritairement anglophones et porteurs
# de DOI, tailles de résultats de l'ordre de celles des API généralistes.
INTERNATIONAL_UNIVERSE: Dict[str, Any] = {
    "seed": "digital discourse",
    "size": 120000,
    "english_rate": 0.85,
    "subsource_weights": {"autre": 1.0},
    "year_profiles": {"autre": [1975, 2026, 1.7]},
    "doi_rates": {"autre": 0.85},
}

INTERNATIONAL_PROVIDERS: Dict[str, Any] = {
    "crossref": {"target_total": 60000},
    "openalex": {"target_total": 45000},
    "semantic_scholar": {"target_total": 30000},
    "core": {"target_total": 9000},
}


def _francophone(max_per_source: int = 200, **extra) -> Dict[str, Any]:
    params = {"query": REFERENCE_QUERY, "sources": ["francophone"],
              "max_per_source": max_per_source, "from_date": "2018"}
    params.update(extra)
    return params


def _ref_sim(**extra) -> Dict[str, Any]:
    sim = {"universe": copy.deepcopy(REFERENCE_UNIVERSE),
           "providers": copy.deepcopy(REFERENCE_PROVIDERS)}
    sim.update(extra)
    return sim


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "reference": {
        "description": ("Requête de référence gelée : « discours numérique », "
                        "groupe francophone, max_per_source=200, from_date=2018."),
        "params": _francophone(),
        "entries": ["mcp", "cli"],
        "sim": _ref_sim(),
        "live": _francophone(),
    },
    "international": {
        "description": ("Groupe international sans Scholar (CrossRef, OpenAlex, "
                        "Semantic Scholar, CORE), clés factices, 200 par source "
                        "depuis 2018."),
        "params": {"query": "digital discourse", "sources": ["international"],
                   "max_per_source": 200, "from_date": "2018"},
        "entries": ["mcp"],
        "sim": {"universe": copy.deepcopy(INTERNATIONAL_UNIVERSE),
                "providers": copy.deepcopy(INTERNATIONAL_PROVIDERS)},
        # Live facultatif : CrossRef et Semantic Scholar, sans clé.
        "live": {"query": "digital discourse", "sources": ["crossref", "semantic_scholar"],
                 "max_per_source": 200, "from_date": "2018"},
    },
    "rare": {
        "description": "Requête rare : quelques dizaines de résultats au total.",
        "params": {"query": "sémiotique des interfaces vocales",
                   "sources": ["francophone"], "max_per_source": 200},
        "entries": ["mcp"],
        "sim": {"universe": {"seed": "rare", "size": 60,
                             "subsource_weights": REFERENCE_UNIVERSE["subsource_weights"]},
                "providers": {"hal": {"target_total": 9}}},
        "live": None,
    },
    "dates_restrictives": {
        "description": ("Référence restreinte à l'année 2024 : les proxies "
                        "Isidore filtrent localement et balaient loin."),
        "params": _francophone(from_date="2024", to_date="2024"),
        "entries": ["mcp"],
        "sim": _ref_sim(),
        "live": None,
    },
    "source_lente": {
        "description": ("Isidore lent (800 ms par réponse), HAL 300 ms, 50 par "
                        "source : la redondance des proxies se paie en temps."),
        "params": _francophone(max_per_source=50),
        "entries": ["mcp"],
        "sim": _ref_sim(latency_ms={"api.isidore.science": 800,
                                    "api.archives-ouvertes.fr": 300}),
        "live": None,
    },
    "quota_429": {
        "description": ("429 avec Retry-After sur HAL, Isidore, CrossRef et "
                        "OpenAlex, plus un 502 transitoire sur Isidore (1re "
                        "requête Isidore : 429 ; 3e : 502)."),
        "params": {"query": REFERENCE_QUERY, "sources": ["hal", "isidore", "crossref", "openalex"],
                   "max_per_source": 100, "from_date": "2018"},
        "entries": ["mcp"],
        "sim": _ref_sim(
            providers={"hal": {"target_total": 2800},
                       "crossref": {"target_total": 20000},
                       "openalex": {"target_total": 15000}},
            # Les règles d'un même hôte sont évaluées dans l'ordre et le
            # compteur d'une règle n'avance que lorsqu'elle est évaluée (une
            # règle qui répond arrête l'évaluation). Isidore : la règle 429
            # répond à la 1re requête ; la règle 502 n'est évaluée qu'à partir
            # de la 2e et répond à sa 2e évaluation, soit la 3e requête. (La
            # version initiale, 429 « every: 4 » placée après le 502, ne se
            # déclenchait jamais : 3 requêtes Isidore seulement.)
            faults=[
                {"host": "api.archives-ouvertes.fr", "kind": "status", "status": 429,
                 "retry_after": 2, "first": 2},
                {"host": "api.isidore.science", "kind": "status", "status": 429,
                 "retry_after": 1, "first": 1},
                {"host": "api.isidore.science", "kind": "status", "status": 502,
                 "after": 1, "first": 1},
                {"host": "api.crossref.org", "kind": "status", "status": 429,
                 "retry_after": 1, "first": 1},
                {"host": "api.openalex.org", "kind": "status", "status": 429,
                 "retry_after": 1, "first": 2},
            ]),
        "live": None,
    },
    "source_indisponible": {
        "description": "Isidore répond 503 à toutes les requêtes (six sources touchées).",
        "params": _francophone(max_per_source=100),
        "entries": ["mcp"],
        "sim": _ref_sim(faults=[{"host": "api.isidore.science", "kind": "status",
                                 "status": 503}]),
        "live": None,
    },
    "hote_injoignable": {
        "description": ("HAL injoignable : délai de connexion dépassé à chaque "
                        "tentative (attente réelle = timeout de connexion demandé)."),
        "params": _francophone(max_per_source=100),
        "entries": ["mcp"],
        "sim": _ref_sim(faults=[{"host": "api.archives-ouvertes.fr",
                                 "kind": "connect_timeout"}]),
        "live": None,
    },
    "grand_corpus": {
        "description": ("Grand corpus : 2 000 notices par source sur HAL, "
                        "Isidore, CrossRef et OpenAlex (mémoire, déduplication, "
                        "exports, taille du retour)."),
        "params": {"query": REFERENCE_QUERY, "sources": ["hal", "isidore", "crossref", "openalex"],
                   "max_per_source": 2000},
        "entries": ["mcp"],
        "sim": _ref_sim(providers={"hal": {"target_total": 6000},
                                   "crossref": {"target_total": 40000},
                                   "openalex": {"target_total": 30000}}),
        "live": None,
    },
    "metadonnees_contradictoires": {
        "description": ("Mêmes travaux chez plusieurs fournisseurs avec DOI chez "
                        "l'un et pas chez l'autre, sous-titres, ponctuation, "
                        "accents, année décalée, homonymes : mesure la "
                        "déduplication (doublons résiduels, fusions erronées)."),
        "params": {"query": "controverses numériques",
                   "sources": ["hal", "isidore", "openedition", "crossref", "openalex"],
                   "max_per_source": 200},
        "entries": ["mcp"],
        "sim": {
            "universe": {
                "seed": "contradictoire", "size": 400,
                "subsource_weights": {"openedition": 0.5, "hal": 0.5},
                "doi_rates": {"openedition": 0.8, "hal": 0.6},
                "variations": {
                    "doi_drop": 0.35, "doi_case": 0.1, "subtitle_drop": 0.4,
                    "punctuation": 0.2, "case": 0.1, "accents": 0.15,
                    "year_shift": 0.1, "authors_truncate": 0.2,
                    "homonym_rate": 0.03,
                },
            },
            "providers": {
                "hal": {"include": {"hal": 0.9, "openedition": 0.4}},
                "crossref": {"include": {"*": 0.95}, "doi_only": True},
                "openalex": {"include": {"*": 0.9}},
            },
        },
        "live": None,
    },
    "erreur_404": {
        "description": ("404 définitif sur HAL, CrossRef et OpenAlex : nombre de "
                        "tentatives par requête invalide (C06)."),
        "params": {"query": REFERENCE_QUERY, "sources": ["hal", "crossref", "openalex"],
                   "max_per_source": 50},
        "entries": ["mcp"],
        "sim": _ref_sim(faults=[
            {"host": "api.archives-ouvertes.fr", "kind": "status", "status": 404},
            {"host": "api.crossref.org", "kind": "status", "status": 404},
            {"host": "api.openalex.org", "kind": "status", "status": 404},
        ]),
        "live": None,
    },
    "reponse_invalide": {
        "description": ("Isidore renvoie une page HTML avec un statut 200 pour "
                        "la requête de la fenêtre, pour sa reprise sans cache "
                        "et pour la reprise différée du flux (1re, 2e et 3e "
                        "requêtes Isidore ; panne observée en live sur 3 pages "
                        "sur 131) : les consommateurs du flux mutualisé doivent "
                        "être signalés en échec (motif invalid_body), jamais "
                        "complets ni vides en silence."),
        "params": _francophone(max_per_source=100),
        "entries": ["mcp"],
        # OPT-07 : panne déplacée sur la page 2 (after=1). Avec le flux
        # Isidore mutualisé (OPT-08), une moisson ne fait plus que 2 requêtes
        # Isidore : la panne initiale (after=4, 5e requête) ne se déclenchait
        # plus. OPT-09 : le flux d'une moisson retente une fois la page en
        # échec après ISIDORE_DEFERRED_RETRY_S ; first=3 (au lieu de 2) pour
        # que la panne persiste à cette reprise et que le scénario mesure
        # toujours une réponse invalide signalée (une panne de deux réponses
        # est désormais rattrapée, cf. tests OPT-09). La comparaison avec la
        # baseline versionnée (after=4, first=1) n'est donc plus à scénario
        # constant pour ce seul scénario. OPT-12 : la fenêtre Isidore tient en
        # UNE requête (replies=200) ; panne déplacée sur cette requête
        # (after=0) : sans page acquise, les six consommateurs sont « error »
        # (et non plus « partial »), toujours de motif invalid_body.
        "sim": _ref_sim(faults=[{"host": "api.isidore.science", "kind": "invalid_body",
                                 "after": 0, "first": 3}]),
        "live": None,
    },
    "cache_chaud": {
        "description": ("Cache HTTP chaud : une moisson d'amorçage (non mesurée) "
                        "remplit le cache, puis la même moisson est mesurée "
                        "(attentes résiduelles sur réponses en cache, C08)."),
        "params": {"query": REFERENCE_QUERY,
                   "sources": ["hal", "isidore", "openedition", "semantic_scholar", "core"],
                   "max_per_source": 200, "from_date": "2018", "cache": True},
        "entries": ["mcp"],
        "warmup": True,
        "sim": _ref_sim(providers={"hal": {"target_total": 2800},
                                   "semantic_scholar": {"target_total": 20000},
                                   "core": {"target_total": 8000}}),
        "live": None,
    },
    "schema_indisponible": {
        "description": ("Schéma Zotero injoignable, sans cache disque : nombre de "
                        "tentatives de téléchargement (C07)."),
        "params": {"query": REFERENCE_QUERY, "sources": ["hal", "isidore"],
                   "max_per_source": 100, "from_date": "2018"},
        "entries": ["mcp"],
        "sim": _ref_sim(faults=[{"host": "api.zotero.org", "kind": "connect_error"}]),
        "live": None,
    },
}

# Scénarios de mesure complémentaires (OPT-09, topologie plate). Hors de
# SCENARIOS : ils ne font pas partie de la série versionnée (« all », résumé
# de référence tests/fixtures/bench/), mais s'appellent par leur nom
# (--scenario reference_serveur_chaud). Tous deux font une moisson d'amorçage
# non mesurée (« warmup ») SANS cache HTTP : elle ne précalcule que le côté
# serveur du simulateur (vues des fournisseurs sur l'univers de 266 508
# travaux, réponses générées). Ce calcul, fait dans le même processus que le
# code mesuré, coûte environ 1 s de CPU sur la référence et, sous le verrou
# global de l'interpréteur, sérialise les sources exécutées en parallèle : il
# serait compté comme un temps client alors qu'un vrai serveur le fait
# ailleurs. La moisson mesurée refait toutes ses requêtes, avec la latence
# simulée ; seul le cache mémoire du schéma Zotero est chaud (des deux côtés
# d'une comparaison).
EXTRA_SCENARIOS: Dict[str, Dict[str, Any]] = {
    "reference_serveur_chaud": {
        "description": ("Requête de référence (francophone, 200 par source, "
                        "depuis 2018), serveur simulé chaud (amorçage non "
                        "mesuré, sans cache HTTP) : durée du client à "
                        "latence égale, séquentiel contre parallèle (OPT-09)."),
        "params": _francophone(),
        "entries": ["mcp", "cli"],
        "warmup": True,
        "sim": _ref_sim(),
        "live": None,
    },
    "tous_groupes": {
        "description": ("Groupe « all » (sept sources francophones et quatre "
                        "internationales, clés factices), 200 par source "
                        "depuis 2018, serveur simulé chaud : effet de la "
                        "topologie plate quand CORE (6 s entre deux pages) et "
                        "Semantic Scholar sont de la moisson (OPT-09)."),
        "params": _francophone(sources=["all"]),
        "entries": ["mcp"],
        "warmup": True,
        "sim": _ref_sim(providers={
            "hal": {"target_total": 2800},
            "crossref": {"target_total": 16744},
            "openalex": {"target_total": 23803},
            "semantic_scholar": {"target_total": 20000},
            "core": {"target_total": 8000},
        }),
        "live": None,
    },
}

# Petit scénario de bout en bout pour les tests unitaires (quelques secondes).
SMOKE_SCENARIO: Dict[str, Any] = {
    "description": "Scénario minimal des tests unitaires.",
    "params": {"query": "discours numérique", "sources": ["hal", "isidore", "openedition"],
               "max_per_source": 20, "from_date": "2018"},
    "entries": ["cli"],
    "sim": {"universe": {"seed": "smoke", "size": 3000,
                         "subsource_weights": REFERENCE_UNIVERSE["subsource_weights"]},
            "providers": {"hal": {"target_total": 120}}},
    "live": None,
}


def get(name: str) -> Dict[str, Any]:
    if name == "smoke":
        return copy.deepcopy(SMOKE_SCENARIO)
    if name in EXTRA_SCENARIOS:
        return copy.deepcopy(EXTRA_SCENARIOS[name])
    if name not in SCENARIOS:
        raise KeyError(f"scénario inconnu : {name} (disponibles : "
                       f"{', '.join(list(SCENARIOS) + list(EXTRA_SCENARIOS) + ['smoke'])})")
    return copy.deepcopy(SCENARIOS[name])


def names() -> List[str]:
    return list(SCENARIOS)


def live_names() -> List[str]:
    return [n for n, s in SCENARIOS.items() if s.get("live")]
