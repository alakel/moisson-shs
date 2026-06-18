# Spécifications des API sources — dates de vérification

Ce fichier trace, par source, l'endpoint utilisé, le mode d'authentification,
les limites de quota et la **date de dernière vérification**. Les API publiques
évoluent (endpoints, en-têtes, quotas) ; **réviser ce tableau annuellement** et
mettre à jour `config.py` en conséquence.

> Convention : une valeur de quota/clé/endpoint sans date explicite hérite de la
> date d'en-tête de la section. En cas de divergence observée à l'exécution,
> corriger ici **et** dans `config.py` (source unique de vérité des endpoints).

## Sources francophones (sans clé)

| Source | Endpoint | Auth | Vérifié |
|---|---|---|---|
| HAL | `https://api.archives-ouvertes.fr/search/` | aucune | 2026-05 |
| Isidore | `https://api.isidore.science/resource/search` | aucune | 2026-05 |
| OpenEdition | OAI-PMH `https://metadata.openedition.org/oai` | aucune | 2026-05 |
| Cairn | OAI-PMH `https://oai.cairn.info/oai.php` | aucune | 2026-05 |
| Persée | OAI-PMH `https://oai.persee.fr/oai` | aucune | 2026-05 |
| Theses.fr | proxy Isidore (API REST publique retirée) | aucune | 2026-05 |
| Érudit | OAI-PMH `https://oai.erudit.org/oai/request` | aucune | 2026-05 |

## Sources internationales (REST natif)

Dernière vérification d'ensemble : **2026-06**.

### CrossRef
- Endpoint : `https://api.crossref.org/works`
- Auth : aucune. *Polite pool* via `mailto` (query param + User-Agent).
- Quota : pas de limite dure ; le polite pool offre une infra plus stable.
- Pagination : `cursor=*` → `message.next-cursor` (offset plafonné à 10 000).
- Plein-texte : `query.bibliographic=`. Filtres : `from-pub-date`/`until-pub-date`.
- Résumés : `abstract` en JATS XML (nettoyé par le mapper).

### OpenAlex
- Endpoint : `https://api.openalex.org/works`
- Auth : **clé gratuite requise** (`OPENALEX_API_KEY`, query param `api_key`).
  https://openalex.org/settings/api
- Quota (juin 2026) : sans clé ~0,01 $/j (test) ; clé gratuite 1 $/j ; 100 req/s.
  En-tête de solde : `X-RateLimit-Prepaid-Remaining-USD`. 402/409 ou 429+solde nul
  ⇒ budget épuisé (arrêt propre via `BudgetExhausted`).
- Pagination : `cursor=*` → `meta.next_cursor` (`per-page` ≤ 200).
- Plein-texte : `?search=`. Filtres : `publication_year`, `type`, `language`, `is_oa`.
- Résumés : `abstract_inverted_index` (index inversé recomposé par le mapper).
- Métadonnées riches : `concepts` (seuil de score), `cited_by_count`, `open_access`,
  `ids`, `referenced_works`.

### Semantic Scholar
- Base : `https://api.semanticscholar.org/graph/v1`
- Endpoint moisson : `/paper/search/bulk` (≤ 1000 résultats/appel, jeton `token`).
- Auth : en-tête `x-api-key` si clé présente (`SEMANTIC_SCHOLAR_API_KEY`, **optionnelle**) ;
  sinon pool partagé throttlé. Débit cible : ≥ 1,05 s entre appels (1 req/s avec clé).
- Filtres : `year=YYYY-YYYY`. `fields=` contrôle les champs retournés.
- Dédup : `externalIds.DOI`, sinon `externalIds.CorpusId`.
- Hydratation/auteur : `/paper/DOI:{doi}`, `/author/search`, `/author/{id}/papers`.

### CORE
- Base : `https://api.core.ac.uk/v3` — endpoint : `/search/works`
- Auth : **clé Bearer requise** (`CORE_API_KEY`, en-tête `Authorization: Bearer`).
  https://core.ac.uk/services/api (inscription ; validation parfois différée).
- Quota : propre au compte (en-têtes `X-RateLimit-*`) ; espacer (~6 s par défaut).
- Pagination : `offset`+`limit` plafonnés (~10 000) ; au-delà `scroll`/`scrollId`.
- Spécificité : texte intégral OA, URL de PDF (`downloadUrl`, `sourceFulltextUrls`).

## Notes d'implémentation

- Les codes exacts d'épuisement de budget (402 vs 409 vs 429+solde) et les noms
  d'en-têtes sont à confirmer contre les réponses réelles ; le code
  (`utils/http_scholarly.py`) les centralise et les commente.
- Le fort recouvrement par DOI entre sources FR et internationales est absorbé
  par la déduplication (`utils/deduplication.py`), complétée d'une priorité de
  fusion champ-à-champ (`SOURCE_FIELD_PRIORITY`).
