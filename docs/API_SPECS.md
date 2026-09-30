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
| HAL | `https://api.archives-ouvertes.fr/search/` | aucune | 2026-09-29 (live : tri par score, `start`/`rows`, `cursorMark`) |
| Isidore | `https://api.isidore.science/resource/search` | aucune | 2026-09-29 (live : fenêtre de 200 résultats, `replies=200`) |
| OpenEdition | OAI-PMH `https://metadata.openedition.org/oai` (format `oai_openaire`) | aucune | 2026-05 |
| Cairn | OAI-PMH `https://oai.cairn.info/oai.php` (format `oai_dc`) | aucune | 2026-05 |
| Persée | OAI-PMH `https://oai.persee.fr/oai` (format `oai_dc`) | aucune | 2026-05 |
| Theses.fr | flux Isidore (API REST publique retirée : 404 sur `/api/v1/*`) | aucune | 2026-05 |
| Érudit | OAI-PMH `https://oai.erudit.org/oai/request` (format `oai_dc`) | aucune | 2026-05 |

### HAL

- Mode `bibliographie` (défaut du serveur MCP) : `sort=score desc,docid asc`, pagination
  par rang (`start`/`rows`), bornée à `start + rows ≤ HAL_RANKED_MAX_RESULTS` (10 000) ; au-delà,
  la source est `partial` (motif `scan_bound`). `fl` demande aussi `score`. Le guide Solr déconseille
  le tri par score sous `cursorMark` en SolrCloud, d'où ce choix.
- Mode `collecte` (défaut de la CLI) : `cursorMark=*` avec `sort=docid asc`, parcours exhaustif et
  reproductible.
- Taille de lot : `rows = min(HAL_ROWS_PER_BATCH, reste à collecter)` (1 000 au plus). Mesure live du
  2026-09-29 : `rows=200` en 0,37 s (0,85 Mo), `rows=1000` en 0,76 à 0,91 s (4,9 Mo).
- Filtres natifs : dates (`fq=producedDateY_i:[…]`, ou au jour), langue (`language_s`), type.
- Contrôle live (2026-09-29, 1 963 résultats) : 198 des 200 notices retenues figurent dans le top 200
  d'une sonde triée par score ; les scores varient légèrement d'un réplica SolrCloud à l'autre.

### Isidore et ses sous-sources

- Paramètres : `q`, `replies`, `page`, `output=json`. L'API annonce le total réel (`@items`) mais ne
  sert que les **200 premiers résultats** ; toute page au-delà répète la dernière (vérifié les
  2026-09-28 et 2026-09-29 : `replies=250` rend 200 notices ; pages 11 et 12 de 20 identiques à la
  page 10). Constantes : `ISIDORE_RESULT_WINDOW` (200), `ISIDORE_REPLIES_PER_PAGE` (200 : une seule
  requête de 9,3 Mo environ sert toute la fenêtre), `ISIDORE_SCAN_MAX_RECORDS` (3 000, borne de
  sécurité si la fenêtre était levée).
- Une moisson fait **un seul parcours** par `(requête, dates)`, partagé par la source `isidore` et les
  cinq sous-sources (`connectors/isidore_scan.py`), qui filtrent par **domaine d'URL**
  (`openedition.org`, `cairn.info`, `persee.fr`, `theses.fr`, `erudit.org`). Une sous-source
  plafonnée par la fenêtre est `partial`, motif `provider_window`.
- Aucun score exposé : le rang d'une notice est sa position dans la fenêtre ; l'ordre par défaut
  n'est pas documenté (poids réduit dans le classement `native`). `sort=date_desc` n'est pas fiable,
  le filtre `language` (URI lexvo) est très sous-inclusif : dates et langues sont filtrées localement.
- Reprise différée d'une page en échec : une tentative après `ISIDORE_DEFERRED_RETRY_S` (5 s), bornée
  par `ISIDORE_DEFERRED_RETRY_BUDGET_S` (15 s).
- OpenEdition ne capte ni Hypothèses (`*.hypotheses.org`) ni Calenda (`calenda.org`).

### Enrichissement OAI-PMH (`enrich_oai`)

OpenEdition, Cairn, Persée et Érudit peuvent enrichir chaque notice par un `GetRecord` OAI-PMH
(bibliothèque Sickle), une requête par notice. Ces requêtes passent hors de la politique HTTP
commune et ne sont pas capturées par le mode audit (`native_audit.not_captured` dans le manifeste).

## Sources internationales (REST natif)

Dernière vérification d'ensemble : **2026-06** ; paramètres de tri, de lot et de politesse
revérifiés en live les 2026-09-28 et 2026-09-29.

### CrossRef
- Endpoint : `https://api.crossref.org/works`
- Auth : aucune. *Pool poli* via `mailto` (paramètre et User-Agent, `MOISSON_CONTACT_EMAIL`).
- Limites (en vigueur depuis le 2025-12-01, en-têtes `x-rate-limit-limit`, `x-rate-limit-interval`,
  `x-concurrency-limit`, `x-api-pool` relevés le 2026-09-28) : requêtes de **liste** (pool
  `polite-array`) 3 par seconde et 3 simultanées ; requêtes d'un **travail unique** (`/works/{doi}`,
  pool `polite-single`) 10 par seconde et 3 simultanées ; sans `mailto`, 1/s et 5/s, 1 simultanée.
  Le client applique les planchers `REQUEST_DELAY_CROSSREF_LIST` (0,34 s), `REQUEST_DELAY_CROSSREF`
  (0,15 s) et `CROSSREF_MAX_IN_FLIGHT` (3), et la limite annoncée par les en-têtes si elle est plus
  stricte.
- Pagination : `cursor=*` → `message.next-cursor` ; `rows = min(1000, max_results)` (la durée d'une
  requête dépend peu de la taille du lot).
- Plein texte : `query.bibliographic=`, traité en **sac de mots** (guillemets, AND et NOT sans effet,
  vérifié). Filtres : `from-pub-date`/`until-pub-date`, `type` ; `filter=language` refusé (400).
- Tri : `sort=score&order=desc` en mode bibliographie (compatible avec le curseur) ; ordre de l'index
  en mode collecte. Score natif capturé (`score`).
- Résumés : `abstract` en JATS XML (nettoyé par le mapper).

### OpenAlex
- Endpoint : `https://api.openalex.org/works`
- Auth : **clé gratuite requise** (`OPENALEX_API_KEY`, paramètre `api_key`).
  https://openalex.org/settings/api
- Coût (vérifié le 2026-09-28) : facturé **par appel** (0,001 $), pas par notice ; clé gratuite 1 $/j,
  sans clé environ 0,1 $/j. En-tête de solde : `X-RateLimit-Prepaid-Remaining-USD`. 402/409 ou 429 avec
  solde nul ⇒ budget épuisé (arrêt propre de la source, motif `budget_exhausted`).
- Pagination : `cursor=*` → `meta.next_cursor` ; `per-page = min(200, max_results)`.
- Plein texte : `?search=` (guillemets, AND/OR/NOT et parenthèses honorés). Filtres :
  `publication_year`, `type`, `language`, `is_oa`.
- Tri : pertinence par défaut avec `search` (`relevance_score:desc`, score capturé).
- Résumés : `abstract_inverted_index` (index inversé recomposé par le mapper).
- Métadonnées riches : `concepts` (seuil `MOISSON_OPENALEX_CONCEPT_SCORE_MIN`), `cited_by_count`,
  `open_access`, `ids`, `referenced_works`.
- Auteurs : `/authors` (profils), filtre `author.id` ou `author.orcid` sur `/works`, pagination par
  curseur (`MOISSON_AUTHOR_OPENALEX_PAGE_SIZE`, 200).

### Semantic Scholar
- Base : `https://api.semanticscholar.org/graph/v1`
- Endpoint moisson : `/paper/search/bulk` (≤ 1 000 résultats par appel, jeton `token`).
- Auth : en-tête `x-api-key` si clé présente (`SEMANTIC_SCHOLAR_API_KEY`, **optionnelle**) ; sinon pool
  partagé, en 429 dès une dizaine de requêtes. Débit : au moins 1,05 s entre deux appels
  (`MOISSON_REQUEST_DELAY_S2`).
- Tri : sans `sort`, `bulk` renvoie les résultats par `paperId` croissant (vérifié le 2026-09-29) :
  mode collecte. En mode bibliographie, `sort=citationCount:desc` (`MOISSON_S2_BIBLIO_SORT`) : biais
  documenté vers les travaux anciens et très cités, exclu de la fusion de rangs. `/paper/search`
  (classé par pertinence) est impraticable sans clé (8 réponses 429 sur 8 le 2026-09-29).
- Syntaxe : `"…"`, `+`, `|`, `-`, parenthèses ; « AND » et « NOT » en toutes lettres ignorés.
- Filtres : `year=YYYY-YYYY`, `publicationTypes`. `fields=` contrôle les champs retournés. Aucun filtre
  de langue.
- Dédup : `externalIds.DOI`, sinon `externalIds.CorpusId`.
- Hydratation et auteur : `/paper/DOI:{doi}`, `/author/search`, `/author/{id}/papers` (pages de
  `MOISSON_AUTHOR_S2_PAGE_SIZE`, 500).

### CORE
- Base : `https://api.core.ac.uk/v3` — endpoint : `/search/works`
- Auth : **clé requise** (`CORE_API_KEY`, en-tête `Authorization: Bearer`).
  https://core.ac.uk/services/api (inscription ; validation parfois différée).
- Quota : propre au compte (en-têtes `X-RateLimit-*`) ; au moins 6 s entre deux appels
  (`MOISSON_REQUEST_DELAY_CORE`).
- Pagination : `offset` + `limit` (pages de 100), plafonnés vers 10 000 ; au-delà `scroll`/`scrollId`.
- Syntaxe : AND, OR, parenthèses, `champ:valeur` (NOT non documenté : exclusions retirées). Aucun tri
  ni filtre de langue documenté (filtre local).
- Spécificité : texte intégral en accès ouvert, URL de PDF (`downloadUrl`, `sourceFulltextUrls`).
- Non vérifié en live (clé absente lors des sondes).

### Google Scholar (SerpAPI)
- Endpoint : `https://serpapi.com/search.json?engine=google_scholar` ; solde :
  `https://serpapi.com/account.json` (gratuit, hors quota).
- Auth : **clé requise** (`SERPAPI_API_KEY`, paramètre `api_key`).
- Coût : **une requête = une page de 20 résultats = un crédit** ; compte payant à 5 000 requêtes par
  mois, partagé avec le projet MyWebIntelligence. Plafonds : `MOISSON_SERPAPI_PAGE_SIZE` (20),
  `MOISSON_SERPAPI_MAX_PAGES` (10 pages par appel), `MOISSON_SERPAPI_MIN_CREDITS` (refus sous
  100 crédits restants) ; au moins 1 s entre deux appels (`MOISSON_REQUEST_DELAY_SERPAPI`).
- Paramètres : `q` (avec `author:`, `source:`), `as_ylo`/`as_yhi`, `lr=lang_fr|lang_en`, `start`,
  `num` ≤ 20.
- Métadonnées : ligne de notice tronquée par Google, ni DOI ni résumé ; `Cited by` et
  `Scholar cluster ID` conservés.
- Hors des groupes `international` et `all` ; décrit d'après la documentation, **jamais appelé** par
  les vérifications ni par les tests.

### Recherches ciblées

- `fetch_by_doi` interroge en parallèle CrossRef (`/works/{doi}`), HAL (`doiId_s`), Isidore,
  OpenAlex, Semantic Scholar (`/paper/DOI:{doi}`) et CORE ; la notice rendue doit porter le DOI
  demandé. Délai de grâce des résolveurs moins prioritaires : `MOISSON_FETCH_BY_DOI_GRACE` (5 s) ;
  durée maximale `MOISSON_FETCH_BY_DOI_MAX_SECONDS` (60 s).
- `corpus_resolve_dois` interroge CrossRef par `query.bibliographic` (5 candidats), règles
  `doi-match/3` : titre ≥ 0,90 corroboré par l'année (± 1) et le premier auteur, 0,97 sans
  corroboration ; ces seuils ne peuvent être que durcis.

### Schéma Zotero

- `https://api.zotero.org/schema`, mis en cache 30 jours (`MOISSON_SCHEMA_CACHE`,
  `MOISSON_SCHEMA_CACHE_AGE`) ; un échec est mémorisé 600 s (`MOISSON_SCHEMA_RETRY_AFTER`) ; repli
  embarqué si le réseau manque ; `MOISSON_ZOTERO_SCHEMA_FILE` impose un fichier local.

## Capacités de requête par source (vérifiées le 2026-09-29)

Registre de référence : `connectors/capabilities.py` (une description par
source, avec preuves) ; il gouverne le plan de requête (`utils/query_plan.py`) :
requête envoyée à chaque source, filtres natifs ou locaux, avertissements.

**Méthode.** Sondes live du 2026-09-29, 00 h 04 à 00 h 09 (heure de Paris) :
65 requêtes sur un budget de 70, petites pages (3 notices ; 1 000 titres
pour le bulk de Semantic Scholar), au moins 1,2 s entre deux requêtes et
3,3 s pour Semantic Scholar ; OpenAlex et Semantic Scholar **sans clé**. Même
requête de référence partout (« discours numérique ») : on compare le nombre
de résultats et les premiers titres avec guillemets, avec AND/OR/NOT (ou
ET/OU/SAUF), avec parenthèses, puis les filtres, le tri, les accents
(« écriture » / « ecriture ») et les apostrophes (« l'école » / « école »).
CORE (clé absente) et Google Scholar (facturé, **jamais appelé**) : décrits
d'après la documentation officielle lue le même jour, **non vérifiés en
live**. Journal des sondes (URL, heure, statut, réponse clé) : annexe
« Journal des sondes du 2026-09-29 » ci-dessous ; les numéros (n°) cités
ici et dans `connectors/capabilities.py` (`evidence`) y renvoient.

<!-- capacites:debut -->
| Source | Expression exacte | Booléens | Parenthèses | Champs | Tri par pertinence | Dates | Langues | Accents | Statut |
|---|---|---|---|---|---|---|---|---|---|
| HAL | oui | `AND` `OR` `NOT` | oui | oui | oui | natif | natif | sans effet | vérifié (2026-09-29) |
| Isidore | oui | `ET` `OU` `SAUF` | oui | non | oui | local | local | sans effet | vérifié (2026-09-29) |
| CrossRef | non | non | non | non | oui | natif | impossible | significatifs | vérifié (2026-09-29) |
| OpenAlex | oui | `AND` `OR` `NOT` | oui | par filtre | oui | natif | natif | significatifs | vérifié (2026-09-29) |
| Semantic Scholar | oui | `+` `\|` `-` | oui | non | non | natif | impossible | sans effet | vérifié (2026-09-29) |
| CORE | oui | `AND` `OR` | oui | oui | oui | natif | local | non vérifié | documenté (non vérifié en live) (2026-09-29) |
| Google Scholar | oui | `OR` `-` | non | oui | oui | natif | natif | non vérifié | documenté (non vérifié en live) (2026-09-29) |
<!-- capacites:fin -->

Les sous-sources OpenEdition, Cairn, Persée, Theses.fr et Érudit passent par
le flux Isidore de la moisson : elles reçoivent la requête Isidore et en
héritent les capacités. « Tri par pertinence » : un classement par pertinence
est disponible (Semantic Scholar : le point d'accès `bulk` n'en a pas).

**L'affirmation « les sources internationales ignorent les opérateurs » est
fausse en bloc.** Résultat source par source (n° = sonde du journal) :

- **HAL** (Solr) : opérateur implicite ET (n° 1 et 3 : 2 754 = 2 754) ;
  expression exacte 179 ; OR 182 388 ; NOT 60 056 ; « (discours OR récit)
  AND numérique NOT politique » 2 644 ; `title_t:"discours numérique"` 45 ;
  filtres `fq` date + `language_s:fr` + `docType_s:ART` : 460, 3/3 conformes ;
  tri par score sans paramètre ; accents sans effet (26 885 = 26 885) ;
  « l'école » 696 079 contre 1 024 777 pour « école » (l'élision compte comme
  un mot) ; `start=10000` servi.
- **Isidore** : opérateur implicite ET (266 508 = 266 508) ; expression
  exacte 1 276 ; OU 2 110 979 ; SAUF 338 142 ; parenthèses 418 753. **AND, OR
  et NOT anglais sont lus comme des mots** : AND 251 777 (notices anglophones
  en tête), OR 128 289 (moins que la requête de base), NOT 33 649 avec « discours
  numériques » dans les premiers titres. Filtre `language` (URI lexvo) :
  7 276 notices sur 266 508, très sous-inclusif, non utilisé (filtre local) ;
  `sort=date_desc` : premières notices de mai 2008, non fiable ; filtre
  `date` incohérent (OPT-08) ; accents sans effet ; élision presque neutre.
- **CrossRef** (`query.bibliographic`) : **sac de mots vérifié** : avec ou
  sans guillemets, avec AND, avec NOT, 43 931 résultats et mêmes premières
  notices — NOT n'exclut rien ; `query.title` accepté (28 885) mais absent
  de la documentation ; `filter=language` refusé (400
  `filter-not-available`) ; tri et filtres de date et de type opérants ;
  **accents significatifs** (« écriture » 5 744, « ecriture » 466) ; élision
  neutre (13 710 = 13 710).
- **OpenAlex** (`search`, sans clé) : **honore guillemets, AND/OR/NOT et
  parenthèses** : ET implicite (45 699 = 45 699), expression exacte 696, OR
  738 066, NOT 510 262, parenthèses 51 284 ; `filter=title.search` 68 ;
  filtres année + `language:fr` + `type:article` : 15 728, 3/3 conformes ;
  **accents significatifs** (112 865 contre 15 804) ; 0,001 $ par appel,
  plafond sans clé 0,1 $ par jour.
- **Semantic Scholar** (`/paper/search/bulk`, sans clé) : **honore `"…"`,
  `+`, `|`, `-` et parenthèses** : 1 800 (ET implicite), expression exacte
  121, `|` 201 507, `-` 91 173, `(discours | récit) + numérique` 2 417 ; **« AND »
  et « NOT » écrits en toutes lettres sont ignorés** (1 800, mêmes notices :
  une exclusion écrite « NOT » y renvoie l'inverse de ce qui est demandé) ;
  `sort=citationCount:desc`, `year=2018-`, `publicationTypes` opérants ;
  aucun filtre ni champ de langue ; `/paper/search` : appariement lâche
  (202 042), classé par pertinence, sans syntaxe ; 429 dès la 10e requête
  du pool partagé.
- **CORE** (documentation v3, non vérifié) : AND (AND, `+`, espace), OR,
  parenthèses, `champ:valeur`, bornes, `_exists_`, expression exacte ; **NOT
  absent du tableau des opérateurs** (les exclusions sont retirées de la
  requête envoyée, avec avertissement) ; 10 000 résultats au plus ; aucun tri
  ni filtre de langue documenté (filtre local sur `language.code`).
- **Google Scholar** (SerpAPI et aide Google Scholar, non vérifié, jamais
  appelé) : `author:`, `source:`, titre entre guillemets, `allintitle:`,
  `as_ylo`/`as_yhi`, `lr=lang_fr|lang_en`, `scisbd` (tri par date sur
  l'année), `num` ≤ 20 ; OR et `-` relèvent de la syntaxe Google générale,
  non décrite par ces pages. Toujours nommé explicitement (hors groupes).

**Conséquences pour le plan de requête.** Une requête de mots seuls part
telle quelle partout. Sinon : HAL et OpenAlex reçoivent AND/OR/NOT, Isidore
ET/OU/SAUF, Semantic Scholar `+ | -`, CORE AND/OR (exclusions retirées),
Google Scholar `"…"`, OR, `-terme` ; CrossRef reçoit une variante « sac de
mots » dérivée (mots des parties positives, termes exclus retirés), toujours
affichée. Aucune traduction automatique : une source anglophone ne reçoit
une requête anglaise que si l'utilisateur fournit la variante `en`.

**Lecture de la requête (plan de requête).** Précédence : NOT, puis AND
(écrit ou mots juxtaposés), puis OR (« a b OR c » se lit « (a b) OR c ») ;
une exclusion écrite au niveau d'une alternative, hors parenthèses, porte
sur toute l'alternative (« a OR b -c » se lit « (a OR b) -c », comme dans
Lucene et chez Google) ; une branche d'alternative sans terme positif
(« a OR NOT b ») est ignorée. Ces lectures sont signalées par un
avertissement du plan, qui montre la lecture retenue. La syntaxe Solr de
HAL (plages `champ:[a TO b]`, proximité `"…"~N`, pondération `^N`,
échappements par `\`) est transmise telle quelle à HAL ; ailleurs, une
plage est retirée, une proximité devient une recherche des mots ensemble et
une pondération est ignorée, avec avertissement ou note.

### Journal des sondes du 2026-09-29

Sondes exécutées de 00:04:05 à 00:09:29 (heure de Paris), une requête à la
fois, sans clé d'API, jamais SerpAPI ; en-tête `User-Agent:
moisson-shs/1.0 (verification capacites; mailto:<contact>)` (adresse de
`config.CONTACT_EMAIL`). Budget de 70 requêtes, 65 consommées ; espacement
d'au moins 1,2 s, 3,3 s pour Semantic Scholar. Paramètres fixes, retirés de
la colonne « Requête » :

- HAL : `rows=3` (n° 13 : `rows=1`), `fl=docid,title_s,docType_s,language_s,producedDateY_i,score`, `wt=json` ;
- Isidore : `replies=3`, `output=json` ;
- CrossRef : `rows=3`, `select=DOI,title,type,issued`, `mailto=<contact>` ;
- OpenAlex : `per-page=3`, `select=id,display_name,language,type,publication_year,relevance_score`, `mailto=<contact>` ;
- Semantic Scholar : `fields=title,year,publicationTypes,citationCount`
  (`limit=3` pour `/paper/search`, n° 57).

« Total » : `numFound` (HAL), `@items` (Isidore), `total-results`
(CrossRef), `meta.count` (OpenAlex), `total` (Semantic Scholar). Titres
tronqués à 55 caractères.

| n | Heure | Sonde | Requête (paramètres fixes retirés) | HTTP | Total | 1er titre / message |
|---|---|---|---|---|---|---|
| 1 | 00:04:05 | H01_base | `api.archives-ouvertes.fr/search/?q=discours numérique&rows=3` | 200 | 2754 | La pratique de l'analyse du discours numérique en Algér |
| 2 | 00:04:06 | H02_phrase | `api.archives-ouvertes.fr/search/?q="discours numérique"&rows=3` | 200 | 179 | La pratique de l'analyse du discours numérique en Algér |
| 3 | 00:04:08 | H03_AND | `api.archives-ouvertes.fr/search/?q=discours AND numérique&rows=3` | 200 | 2754 | La pratique de l'analyse du discours numérique en Algér |
| 4 | 00:04:09 | H04_OR | `api.archives-ouvertes.fr/search/?q=discours OR numérique&rows=3` | 200 | 182388 | La pratique de l'analyse du discours numérique en Algér |
| 5 | 00:04:10 | H05_NOT | `api.archives-ouvertes.fr/search/?q=discours NOT numérique&rows=3` | 200 | 60056 | Quelques éléments d'acquisition pour aborder le discour |
| 6 | 00:04:12 | H06_parens | `api.archives-ouvertes.fr/search/?q=(discours OR récit) AND numérique NOT politique&rows=3` | 200 | 2644 | Récit numérique et temporalité |
| 7 | 00:04:13 | H07_field_title | `api.archives-ouvertes.fr/search/?q=title_t:"discours numérique"&rows=3` | 200 | 45 | Discours numérique français |
| 8 | 00:04:15 | H08_filters_sort | `api.archives-ouvertes.fr/search/?q=discours numérique&rows=3&fq=producedDateY_i:[2018 TO *],language_s:fr,docType_s:ART&sort=producedDateY_i desc` | 200 | 460 | Réseaux sociaux : aider les adolescents à protéger leur |
| 9 | 00:04:16 | H09_ecriture_accent | `api.archives-ouvertes.fr/search/?q=écriture&rows=3` | 200 | 26885 | « Abréviations » ; « Caroline (écriture) »; « Cursive ( |
| 10 | 00:04:17 | H10_ecriture_plain | `api.archives-ouvertes.fr/search/?q=ecriture&rows=3` | 200 | 26885 | « Abréviations » ; « Caroline (écriture) »; « Cursive ( |
| 11 | 00:04:19 | H11_lecole_apos | `api.archives-ouvertes.fr/search/?q=l'école&rows=3` | 200 | 696079 | L’islam des manuels scolaires français: Comment l’école |
| 12 | 00:04:20 | H12_ecole | `api.archives-ouvertes.fr/search/?q=école&rows=3` | 200 | 1024777 | École doctorale de l'EPHE |
| 13 | 00:04:24 | H13_start_10000 | `api.archives-ouvertes.fr/search/?q=discours&rows=1&start=10000` | 200 | 62810 | Supports pédagogiques pour lutter contre les discrimina |
| 14 | 00:04:40 | I01_base | `api.isidore.science/resource/search?q=discours numérique&replies=3` | 200 | 266508 | Marie-Anne Paveau, L’analyse du discours numérique. Dic |
| 15 | 00:04:42 | I02_phrase | `api.isidore.science/resource/search?q="discours numérique"&replies=3` | 200 | 1276 | Marie-Anne Paveau, L’analyse du discours numérique. Dic |
| 16 | 00:04:44 | I03_ET | `api.isidore.science/resource/search?q=discours ET numérique&replies=3` | 200 | 266508 | Numérique |
| 17 | 00:04:46 | I04_AND | `api.isidore.science/resource/search?q=discours AND numérique&replies=3` | 200 | 251777 | Conversion numérique et modification épistémologique. A |
| 18 | 00:04:50 | I05_OU | `api.isidore.science/resource/search?q=discours OU numérique&replies=3` | 200 | 2110979 | Numérique |
| 19 | 00:04:52 | I06_OR | `api.isidore.science/resource/search?q=discours OR numérique&replies=3` | 200 | 128289 | L’alternative quantitatif/qualitatif à l’épreuve des un |
| 20 | 00:04:54 | I07_SAUF | `api.isidore.science/resource/search?q=discours SAUF numérique&replies=3` | 200 | 338142 | Discours |
| 21 | 00:04:56 | I08_NOT | `api.isidore.science/resource/search?q=discours NOT numérique&replies=3` | 200 | 33649 | Transmission, partage et circulation des discours numér |
| 22 | 00:04:59 | I09_parens | `api.isidore.science/resource/search?q=(discours OU récit) ET numérique&replies=3` | 200 | 418753 | La propagande terroriste : étude du récit propagandiste |
| 23 | 00:05:01 | I10_filters_sort | `api.isidore.science/resource/search?q=discours numérique&replies=3&language=http://lexvo.org/id/iso639-3/fra&type=http://isidore.science/ontology#article&sort=date_desc` | 200 | 2402 | Crime is in the Eye of the Cartoon Beholder: a Linguist |
| 24 | 00:05:03 | I11_ecriture_accent | `api.isidore.science/resource/search?q=écriture&replies=3` | 200 | 872163 | Écritures |
| 25 | 00:05:05 | I12_ecriture_plain | `api.isidore.science/resource/search?q=ecriture&replies=3` | 200 | 872163 | Écritures |
| 26 | 00:05:08 | I13_lecole_apos | `api.isidore.science/resource/search?q=l'école&replies=3` | 200 | 1059649 | L’école |
| 27 | 00:05:11 | I14_ecole | `api.isidore.science/resource/search?q=école&replies=3` | 200 | 1124625 | École |
| 28 | 00:05:57 | C01_base | `api.crossref.org/works?rows=3&query.bibliographic=discours numérique` | 200 | 43931 | 2. De la CDMA à l’analyse textométrique du discours num |
| 29 | 00:05:59 | C02_phrase | `api.crossref.org/works?rows=3&query.bibliographic="discours numérique"` | 200 | 43931 | 2. De la CDMA à l’analyse textométrique du discours num |
| 30 | 00:06:00 | C03_AND | `api.crossref.org/works?rows=3&query.bibliographic=discours AND numérique` | 200 | 43931 | 2. De la CDMA à l’analyse textométrique du discours num |
| 31 | 00:06:03 | C04_NOT | `api.crossref.org/works?rows=3&query.bibliographic=discours NOT numérique` | 200 | 43931 | 2. De la CDMA à l’analyse textométrique du discours num |
| 32 | 00:06:05 | C05_query_title | `api.crossref.org/works?rows=3&query.title=discours numérique` | 200 | 28885 | Discours numérique médié (DNM) et mondialisation |
| 33 | 00:06:06 | C06_filters_sort | `api.crossref.org/works?rows=3&query.bibliographic=discours numérique&filter=from-pub-date:2018-01-01,type:journal-article&sort=published&order=desc` | 200 | 10627 | Title Pending 18169 |
| 34 | 00:06:08 | C07_lang_filter | `api.crossref.org/works?rows=3&query.bibliographic=discours numérique&filter=language:fr` | 400 | None | [{'type': 'filter-not-available', 'value': 'language',  |
| 35 | 00:06:10 | C08_ecriture_accent | `api.crossref.org/works?rows=3&query.bibliographic=écriture` | 200 | 5744 | Milan Kundera : écriture de l'essai ou essai d'une écri |
| 36 | 00:06:12 | C09_ecriture_plain | `api.crossref.org/works?rows=3&query.bibliographic=ecriture` | 200 | 466 | Ecriture du groupe. Ecriture en groupe |
| 37 | 00:06:14 | C10_lecole_apos | `api.crossref.org/works?rows=3&query.bibliographic=l'école` | 200 | 13710 | Références bibliographiques du dossier : » école primai |
| 38 | 00:06:16 | C11_ecole | `api.crossref.org/works?rows=3&query.bibliographic=école` | 200 | 13710 | Références bibliographiques du dossier : » école primai |
| 39 | 00:06:45 | O01_base | `api.openalex.org/works?per-page=3&search=discours numérique` | 200 | 45699 | Technodiscursivités natives sur Twitter. Une écologie d |
| 40 | 00:06:54 | O02_phrase | `api.openalex.org/works?per-page=3&search="discours numérique"` | 200 | 696 | Technodiscursivités natives sur Twitter. Une écologie d |
| 41 | 00:06:55 | O03_OR | `api.openalex.org/works?per-page=3&search=discours OR numérique` | 200 | 738066 | A discrete numerical model for granular assemblies |
| 42 | 00:06:57 | O04_NOT | `api.openalex.org/works?per-page=3&search=discours NOT numérique` | 200 | 510262 | L'ordre du discours |
| 43 | 00:06:59 | O05_AND | `api.openalex.org/works?per-page=3&search=discours AND numérique` | 200 | 45699 | Discours de la me?thode |
| 44 | 00:07:00 | O06_filters_sort | `api.openalex.org/works?per-page=3&search=discours numérique&filter=publication_year:>2017,language:fr,type:article&sort=publication_date:desc` | 200 | 15728 | Vertiges pataphysiques et petites choses vidéoludiques  |
| 45 | 00:07:02 | O07_field_title | `api.openalex.org/works?per-page=3&filter=title.search:"discours numérique"` | 200 | 68 | Technodiscursivités natives sur Twitter. Une écologie d |
| 46 | 00:07:03 | O08_ecriture_accent | `api.openalex.org/works?per-page=3&search=écriture` | 200 | 112865 | Écriture et réécriture hagiographiques |
| 47 | 00:07:05 | O09_ecriture_plain | `api.openalex.org/works?per-page=3&search=ecriture` | 200 | 15804 | L' ecriture de l'histoire |
| 48 | 00:07:07 | O10_lecole_apos | `api.openalex.org/works?per-page=3&search=l'école` | 200 | 301138 | L'école conservatrice: Les inégalités devant l'école et |
| 49 | 00:07:08 | O11_parens | `api.openalex.org/works?per-page=3&search=(discours OR récit) AND numérique` | 200 | 51284 | Discours de la me?thode |
| 50 | 00:07:25 | S01_base | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=discours numérique` | 200 | 1800 | Un concept technologique de trace numérique |
| 51 | 00:07:30 | S02_phrase | `api.semanticscholar.org/graph/v1/paper/search/bulk?query="discours numérique"` | 200 | 121 | L’émojination dans la structuration de l’écrit numériqu |
| 52 | 00:07:34 | S03_OR_pipe | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=discours \| numérique` | 200 | 201507 | End wall effects on thermal stratification and heat tra |
| 53 | 00:07:40 | S04_NOT_minus | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=discours -numérique` | 200 | 91173 | Introduction. Insertion, biographisation, éducation, ch |
| 54 | 00:07:45 | S05_AND_literal | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=discours AND numérique` | 200 | 1800 | Un concept technologique de trace numérique |
| 55 | 00:07:49 | S06_parens | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=(discours \| récit) + numérique` | 200 | 2417 | Florilège 2 : Sexualité plurielle |
| 56 | 00:07:53 | S07_filters_sort | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=discours numérique&sort=citationCount:desc&year=2018-&publicationTypes=JournalArticle` | 200 | 271 | What are we saving? Tracing governing knowledge and tru |
| 57 | 00:07:58 | S08_relevance | `api.semanticscholar.org/graph/v1/paper/search?query=discours numérique&limit=3` | 200 | 202042 | Perspectives épistémologiques de l’analyse du discours  |
| 58 | 00:08:02 | S09_ecriture_accent | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=écriture` | 200 | 23355 | Ecriture de l'histoire et construction nationale |
| 59 | 00:08:07 | S10_ecriture_plain | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=ecriture` | 200 | 23355 | Ecriture de l'histoire et construction nationale |
| 60 | 00:08:11 | S11_lecole_apos | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=l'école` | 429 | None | Too Many Requests. Please wait and try again or apply f |
| 61 | 00:08:15 | S12_ecole | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=école` | 429 | None | Too Many Requests. Please wait and try again or apply f |
| 62 | 00:08:43 | I15_lang_only | `api.isidore.science/resource/search?q=discours numérique&replies=3&language=http://lexvo.org/id/iso639-3/fra` | 200 | 7276 | Compte rendu : Marie-Anne PAVEAU, L’Analyse du discours |
| 63 | 00:08:47 | I16_sort_date_only | `api.isidore.science/resource/search?q=discours numérique&replies=3&sort=date_desc` | 200 | 266508 | ‘‘C’est donc un amoureux qui parle et qui dit’’ : des F |
| 64 | 00:09:24 | S13_lecole_apos_retry | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=l'école` | 200 | 61396 | A l'école des chartes |
| 65 | 00:09:29 | S14_NOT_literal | `api.semanticscholar.org/graph/v1/paper/search/bulk?query=discours NOT numérique` | 200 | 1800 | Un concept technologique de trace numérique |

## Notes d'implémentation

- Toutes les requêtes des sources, des recherches ciblées et de la résolution de DOI passent par la
  politique HTTP commune (`utils/http.py`) : une seule tentative sur un 4xx définitif ; 429 et
  erreurs transitoires repris au plus `MOISSON_HTTP_MAX_RETRIES` fois (3 tentatives), `Retry-After`
  respecté jusqu'à `MOISSON_HTTP_RETRY_AFTER_MAX` (60 s) ; délais de connexion (8 s) et de lecture
  (45 s) séparés ; budget de `MOISSON_HTTP_REQUEST_BUDGET` (120 s) par requête ; politesse par hôte et
  par compte (empreinte de la clé), entre threads, jamais appliquée à une réponse du cache ; au plus
  `MOISSON_HTTP_MAX_IN_FLIGHT_PER_HOST` (2) requêtes simultanées par hôte (3 pour CrossRef).
  Exceptions : l'enrichissement OAI-PMH (Sickle) et le téléchargement du schéma Zotero.
- Les codes exacts d'épuisement de budget (402 vs 409 vs 429+solde) et les noms
  d'en-têtes sont à confirmer contre les réponses réelles ; le code
  (`utils/http_scholarly.py`) les centralise et les commente.
- Le fort recouvrement par DOI entre sources FR et internationales est absorbé
  par la déduplication (`utils/deduplication.py`), complétée d'une priorité de
  fusion champ-à-champ (`SOURCE_FIELD_PRIORITY`).
- Rangs et scores natifs (`native_sort`, `native_filters`, rang et score par notice) sont rendus dans
  `by_source_status` et le manifeste, jamais dans les notices.
