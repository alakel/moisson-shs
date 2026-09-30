# FAQ — Foire aux questions

## Sources et clés API

### Quelles sources sont interrogées par défaut ?

Le groupe `francophone` : HAL, Isidore, OpenEdition, Cairn, Persée, Theses.fr et Érudit, sans aucune clé. Les autres s'activent par groupe ou par nom :

```bash
moisson-shs search "AI regulation" --sources international   # CrossRef, OpenAlex, Semantic Scholar, CORE
moisson-shs search "discours numérique" --sources all        # francophones + internationales (sans Google Scholar)
moisson-shs search "LLM" --sources crossref openalex         # à la carte
moisson-shs search '"large language models"' --sources scholar   # Google Scholar seul (facturé)
```

En MCP : `sources: ["international"]`, `["all"]`, `["scholar"]` ou une liste de noms dans `moisson_search`.

### Quelles clés faut-il, et comment les fournir ?

- **CrossRef** : aucune clé (pool poli : l'adresse `MOISSON_CONTACT_EMAIL` est déclarée).
- **OpenAlex** : clé gratuite **requise** (`OPENALEX_API_KEY`).
- **Semantic Scholar** : clé **optionnelle** (`SEMANTIC_SCHOLAR_API_KEY`) ; sans elle, pool partagé souvent en 429.
- **CORE** : clé **requise** (`CORE_API_KEY`).
- **Google Scholar** : clé SerpAPI **requise** (`SERPAPI_API_KEY`), et source **facturée à la requête**.

Trois façons de les fournir : un fichier `.env` à la racine du projet (voir `.env.example` ; nécessite `python-dotenv`, extra `[dotenv]`), des variables d'environnement du shell, ou le bloc `env` de `claude_desktop_config.json` pour le serveur MCP (voir `MCP_INSTALL.md`, ou `moisson-shs setup --openalex-key …`). Le bloc `env` prime sur le `.env` ; `MOISSON_NO_DOTENV=1` interdit toute lecture de `.env`. **Ne jamais committer de clé** : `.env` est ignoré par git.

### Que se passe-t-il si une clé requise est absente ?

La source n'est pas interrogée : elle apparaît avec le statut `skipped` (motif `missing_key`, « clé d'API absente ») dans le retour, le manifeste et la sortie de la CLI ; les autres sources continuent et le corpus est publié. Un budget OpenAlex épuisé en cours de moisson arrête proprement cette seule source (`partial` ou `error`, motif `budget_exhausted`).

### Pourquoi Google Scholar n'est-il pas dans le groupe `all` ?

Parce que chaque requête est **facturée** (un crédit SerpAPI par page de vingt résultats, sur un quota de 5 000 requêtes par mois partagé avec le projet MyWebIntelligence). La source reste donc volontaire : elle ne s'active que nommée (`scholar` ou `google_scholar`), chaque appel est plafonné (`MOISSON_SERPAPI_MAX_PAGES`, 10 pages) et refusé sous un solde de 100 crédits (`MOISSON_SERPAPI_MIN_CREDITS`, contrôle gratuit). Aucun test ne consomme de crédit. `--plan-only` (ou `plan_only` en MCP) montre la requête qui lui serait envoyée sans rien consommer.

### Pourquoi mon corpus rétrécit-il beaucoup avec `--sources all` ?

C'est attendu : un même article est souvent présent dans une source francophone (Cairn, HAL…) et dans CrossRef ou OpenAlex. La déduplication réunit ces notices (voir [Dédoublonnage](#dédoublonnage)) en gardant pour chaque champ la meilleure valeur et son origine. Le rapport de déduplication (`dedup_report` du retour, et le manifeste) et le fichier `_stats.txt` détaillent les recouvrements.

## Requêtes

### Comment écrire une requête avec des opérateurs ?

La requête accepte des guillemets (expression exacte), `AND`/`ET`, `OR`/`OU`, `NOT`/`SAUF`/`-terme`, et des parenthèses. Précédence : NOT, puis AND (écrit ou mots juxtaposés), puis OR ; en cas de doute, parenthéser. Le **plan de requête** traduit la requête pour chaque source, selon ses capacités vérifiées le 2026-09-29 (`docs/API_SPECS.md`) :

| Source | Reçoit |
|--------|--------|
| HAL, OpenAlex | `AND`, `OR`, `NOT`, guillemets, parenthèses |
| Isidore et ses sous-sources | `ET`, `OU`, `SAUF` (les opérateurs anglais y seraient lus comme des mots) |
| Semantic Scholar | `+`, `\|`, `-` (« AND » et « NOT » en toutes lettres y sont ignorés) |
| CORE | `AND`, `OR` (exclusions retirées, avec avertissement) |
| CrossRef | sac de mots : variante dérivée (mots des parties positives), toujours affichée |
| Google Scholar | `"…"`, `OR`, `-terme` |

Pour voir ce qui part vers chaque source, sans aucune requête ni fichier :

```bash
moisson-shs search '"discours numérique" NOT politique' --sources all --plan-only
```

En MCP : `plan_only: true`. La syntaxe Solr propre à HAL (plages `champ:[a TO b]`, proximité `"…"~N`, pondération `^N`, champs comme `authFullName_s:"…"`) est transmise telle quelle à HAL ; ailleurs, elle est retirée ou simplifiée, avec un avertissement.

### Les sources internationales reçoivent-elles une traduction de ma requête ?

Non, **jamais de traduction automatique**. Sans variante, CrossRef, OpenAlex, Semantic Scholar et CORE reçoivent la requête d'origine, et le plan le signale. Pour les interroger en anglais, fournir la variante : `--query-en '"digital discourse"'` ou `--variant en='…'` en CLI, `query_variants: {"en": "\"digital discourse\""}` en MCP.

### Comment filtrer par langue ?

`--lang fr en` (CLI) ou `languages: ["fr", "en"]` (MCP). Le filtre est natif pour HAL, OpenAlex et Google Scholar, local pour Isidore, ses sous-sources et CORE (les notices sans langue lisible sont conservées), impossible pour CrossRef et Semantic Scholar (signalé dans le plan).

### Quelle différence entre les modes `bibliographie` et `collecte` ?

- `bibliographie` (défaut de l'outil MCP) : chaque source rend ses **meilleures** notices selon son ordre natif : HAL par score (pagination par rang, 10 000 au plus), CrossRef par score, OpenAlex par pertinence, Semantic Scholar par citations.
- `collecte` (défaut de la CLI) : ordre déterministe et reproductible (HAL par curseur et `docid`, CrossRef dans l'ordre de l'index, Semantic Scholar par `paperId`), exhaustif dans la limite du plafond.

En mode bibliographie, deux moissons de même requête à des dates différentes peuvent retenir des notices différentes (la sélection suit les scores) : `corpus_diff` le rappelle dans ses avertissements.

## Couverture

### Pourquoi les sous-sources d'Isidore sont-elles « limitées par le fournisseur » ?

L'API d'Isidore n'est servie que sur ses **200 premiers résultats**, quel que soit le total annoncé. Une moisson fait une seule requête Isidore (`replies=200`), partagée par la source `isidore` et par OpenEdition, Cairn, Persée, Theses.fr et Érudit, qui y retiennent les notices de leur domaine. Leur statut est alors `partial` avec le motif `provider_window` : le fournisseur a répondu normalement mais ne sert pas davantage. Ce n'est pas une panne, et le retour le dit ainsi. Sur une requête large, une sous-source peut rester vide (Persée : 0 notice sur « discours numérique » en 2026-09) : resserrer la requête (expression exacte, période) fait remonter d'autres notices dans la fenêtre.

### OpenEdition capte-t-il Hypothèses et Calenda ?

Non. OpenEdition retient les notices d'Isidore dont l'URL pointe vers `openedition.org` (Journals et Books). Les carnets Hypothèses (`*.hypotheses.org`) et Calenda (`calenda.org`) ne sont pas captés ; ils restent présents dans la source `isidore` elle-même.

### Pourquoi Cairn ne renvoie-t-il pas le texte intégral ?

Cairn n'expose publiquement que les **métadonnées** (titre, auteurs, résumé, références). Le texte intégral relève des conditions de Cairn.info et d'une convention (fouille de textes, accès institutionnel). Le corpus reste utile pour l'analyse bibliométrique : qui publie, où, avec qui, sur quoi, sur quelle période.

### Pourquoi Theses.fr renvoie-t-il peu de notices ?

L'API publique de Theses.fr a été retirée en 2026 : la source passe par le flux Isidore, donc par sa fenêtre de 200 résultats. Pour une thèse précise, utiliser `theses.fr` ou le Sudoc ; pour la production d'un laboratoire, HAL (`docType_s:THESE`).

### Comment moissonner la production d'un auteur ?

Avec l'outil MCP `search_by_author` (ou le prompt `profil-chercheur`). La recherche est prudente sur l'identité :

- **identifiants d'abord** : `idhal`, `orcid`, `openalex_author_id`, `s2_author_id`, `hal_person_id`. Un ORCID seul interroge HAL et OpenAlex par identifiant (Semantic Scholar ne sait pas chercher un profil par ORCID) ; `name` reste exigé par le schéma d'entrée de l'outil : le passer vide (`name=""`), sans quoi l'appel est refusé à la validation ;
- **par le nom seul**, plusieurs profils sont examinés par source ; s'il y a homonymie (au moins deux candidats d'au moins cinq travaux), rien n'est retenu : le retour vaut `ambiguous`, liste les candidats (travaux, période, exemples de titres, co-auteurs) et donne l'argument du second appel (`next_step`, `profiles_to_confirm`) ;
- les notices Isidore ne sont gardées que si leurs créateurs portent le nom cherché (compteur `rejected_not_author`) ;
- OpenAlex et Semantic Scholar sont paginés jusqu'au plafond (`max_per_source`, 200) ou à épuisement ;
- `production_complete` n'est vrai que si aucune source n'est plafonnée, indisponible ou ambiguë : le résultat n'est jamais présenté comme « la production complète » sans cela.

En Python :

```python
from utils.lookups import search_by_author
items = search_by_author("Patrick Charaudeau", idhal="patrick-charaudeau", max_per_source=500)
print(len(items), items.report.production_complete)
```

Limite : les profils des fournisseurs (OpenAlex, Semantic Scholar, idHal) mêlent parfois eux-mêmes des homonymes ; aucun contrôle automatique ne peut le détecter.

---

## Format, export et import

### Pourquoi deux fichiers `.json` et `.csl.json` à chaque moisson ?

- **`corpus_*.json`** : Zotero JSON interne, lu par les outils (`corpus_analyze`, `corpus_filter`, `corpus_query`, `corpus_export`…). Il porte la provenance auditable dans le champ `extra` (sources, identifiants HAL, OAI, ORCID, `DOI source`, `Dedup evidence`).
- **`corpus_*.csl.json`** : CSL-JSON standard, **importable directement dans Zotero Desktop** (et lisible par Pandoc, citeproc…).

**Pour importer dans Zotero : choisir le `.csl.json`**, ou un export BibTeX / RIS. Le Zotero JSON interne n'est pas reconnu par la fenêtre d'import de Zotero Desktop, qui attend CSL-JSON, BibTeX, RIS ou Zotero RDF.

Chaque moisson publie aussi `_stats.txt` et un **manifeste** `.manifest.json` (voir plus bas).

### Comment exporter en BibTeX, BibLaTeX ou RIS ?

```bash
moisson-shs export output/corpus_X.json                 # BibTeX, à côté du corpus
moisson-shs export output/corpus_X.json --format ris
moisson-shs export output/corpus_X.json -f biblatex --ascii -o refs.bib
```

En MCP : `corpus_export(corpus_path=…, format="bibtex"|"biblatex"|"ris")`. BibTeX pour Zotero et JabRef, BibLaTeX pour LaTeX avec biber, RIS pour Zotero et EndNote. Les clés de citation sont stables (`nom + année + mot`, suffixes en cas de collision) ; une ligne `Annote:` du champ `extra` devient `annote` (BibTeX) ou `N1` (RIS) ; la provenance de moisson n'est exportée qu'avec `--include-provenance` (jamais les rangs ni les scores) ; un extrait Google Scholar n'est jamais exporté comme résumé, et l'avertissement de troncature des auteurs Scholar est toujours écrit en note.

### Comment convertir un ancien corpus en CSL-JSON ?

En MCP : `corpus_to_csl(corpus_path=…)`, ou en langage naturel dans Claude Desktop : « Convertis le corpus `output/corpus_X.json` en CSL-JSON ». En Python :

```python
import json
from pathlib import Path
from mappers.csl_exporter import export_corpus_to_csl
src = Path("output/corpus_X.json")
Path("output/corpus_X.csl.json").write_text(json.dumps(export_corpus_to_csl(json.loads(src.read_text())), ensure_ascii=False, indent=2))
```

### Qu'est-ce que le manifeste ?

`<radical>.manifest.json` (format `moisson-shs/manifest`) décrit une production : identifiant d'exécution, type (moisson, filtre, fusion, conversion CSL, auteur, résolution DOI, export), fichiers avec tailles et empreintes SHA-256, requête et plan, filtres, réglages, statut de chaque source, rapport de déduplication, échéance souple, versions des règles, et une entrée par notice (`records` : identifiant, rangs et scores natifs, rang et justification du classement). Il est publié **en dernier** : un corpus sans manifeste valide (publication interrompue) est `incomplete`, jamais présenté comme complet. Les rangs et les scores ne sont écrits que là, jamais dans les notices. Les corpus antérieurs au manifeste restent lisibles (`legacy`) ; `corpus_to_csl` ou `corpus_export` leur en ajoutent un.

### Qu'est-ce que `.index.json` ?

Un index dérivé des manifestes, dans `OUTPUT_DIR`, qui sert `corpus_list` et `list_resources` sans relire chaque corpus. Il se reconstruit seul ; `corpus_list(rebuild=true)` le force. Comme tous les fichiers dont le nom commence par un point, il n'est jamais listé, lu comme ressource ni téléchargé.

---

## Dédoublonnage

### Comment la déduplication décide-t-elle ?

1. **Clés fortes** : DOI (du champ ou du champ `extra`, ou lisible sans ambiguïté dans une URL), identifiants natifs (HAL, OAI, Isidore, OpenAlex, Semantic Scholar, CORE, Google Scholar, PMID, arXiv), URL canonique, ISBN d'un livre. Deux notices qui partagent une clé forte sont réunies, quel que soit le type de clé (une notice sans DOI mais au même identifiant HAL qu'une notice avec DOI la rejoint).
2. **Garde-fous** : deux DOI différents ne sont jamais fusionnés, même par transitivité ; une URL partagée par trop de titres est ignorée, et un lien d'URL exige des titres concordants ; un identifiant HAL ne relie pas deux notices à plus d'un an d'écart (versions distinctes d'un dépôt). Trois règles optionnelles, **désactivées par défaut** jusqu'à la vérification humaine du sprint patch, s'y ajoutent : **co-diffusion** d'un même article par deux plateformes (Cairn, OpenEdition, Persée, Érudit) ou par une plateforme et l'éditeur (`MOISSON_DEDUP_CODIFFUSION=1` : titre identique, même année, même premier auteur, même revue, mêmes pages ; DOI Cairn conservé, l'autre gardé en `DOI alias` dans `extra`) ; **versions Zenodo** (`MOISSON_DEDUP_ZENODO_VERSIONS=1` : DOI de concept et de version d'un même dépôt) ; **chapitres Google Books** (`MOISSON_DEDUP_SCHOLAR_CHAPTERS=1` : entre notices Google Scholar, numéro de chapitre en tête d'un titre Google Books ignoré, type « book » du lien non bloquant). Chaque fusion ainsi faite est tracée dans `Dedup evidence`.
3. **Passe floue conservatrice**, sur les notices restantes : titre similaire à 0,93 au moins, année à un an près **et** premier auteur corroborés (un nom saisi « Nom, Prénom » ou « Prénom Nom », ou un nom composé réparti autrement, est reconnu). Les titres génériques (« Introduction », « Compte rendu »…) et les types incompatibles (compte rendu et article, prépublication et article, chapitre et livre) ne sont jamais réunis. `MOISSON_DEDUP_FUZZY=0` la désactive (seule l'égalité exacte du titre normalisé reste).

La fusion ne dépend pas de l'ordre d'arrivée des sources : chaque champ est choisi par une règle stable, la liste d'auteurs la plus complète est gardée, et l'origine de chaque valeur est tracée dans la ligne `Dedup evidence` du champ `extra`. Mesure sur 209 corpus réels (15 976 notices) : doublons résiduels à titre et année identiques de 4,0 % avant le chantier à 0,44 % ; 0 fusion erronée sur les 66 paires négatives du jeu témoin.

### Pourquoi des doublons subsistent-ils ?

- **Versions distinctes, séparées volontairement** : prépublication et article publié, deux DOI de deux revues, compte rendu et ouvrage. Ce ne sont pas des erreurs : la prudence bibliographique l'emporte.
- **Variations lourdes de titre** (sous-titre réécrit, traduction) qui restent sous le seuil de 0,93.
- **Notice sans année ou sans auteur** : jamais réunie par la passe floue.

Après import dans Zotero, **Édition > Trouver des doublons** fait une seconde passe manuelle. `--enrich-oai` peut aussi apporter un DOI manquant aux notices OpenEdition ou Cairn, et `corpus_resolve_dois` retrouve des DOI par appariement CrossRef.

### Comment savoir d'où vient une notice après fusion ?

Le champ `extra` liste les sources et les preuves :

```
Sources: HAL, Isidore, OpenEdition
HAL ID: hal-04123456
Isidore URI: 20.500.13089/abc
Dedup evidence: 3 notices ; clés doi:10.4000/x, hal:hal-04123456 ; origines title+creators=HAL, abstractNote=OpenEdition
```

---

## Performance et cache

### Une moisson est-elle longue ?

Non, en temps normal. Mesures du 2026-09-29, en réseau réel, requête « discours numérique » depuis 2018, groupe francophone, 200 notices par source, 4 sources à la fois (défauts de l'outil MCP) : **3,2 s** et 2 requêtes (une à HAL, une à Isidore) ; 0,2 s à cache chaud. Avant le chantier d'optimisation, la même moisson parcourait environ 740 pages Isidore : environ 26 minutes projetées (moisson interrompue par le banc après 381 s et 133 requêtes, 2026-09-28). Le groupe `all` prend environ 4 à 5 s.

Ce qui allonge une moisson : `mode="collecte"` avec un gros plafond (plusieurs pages par source ; CORE espace ses pages de 6 s), `--enrich-oai` (une requête OAI par notice), une source lente ou injoignable. En CLI, `--workers 4` interroge quatre sources à la fois (la politesse par serveur s'applique toujours).

### Comment fonctionne le cache HTTP ?

- **CLI** : désactivé par défaut ; `--cache` l'active pour un appel, `--no-cache` le désactive même si `MOISSON_CACHE=1`. Durée de vie 24 h (`MOISSON_CACHE_TTL`), fichier `~/.cache/moisson_shs/http.sqlite` (`MOISSON_CACHE_DIR` + `.sqlite`).
- **Serveur MCP** : **actif par défaut**, durée de vie 6 h (`MOISSON_MCP_CACHE_TTL`), fichier propre `http_mcp.sqlite` (même base, suffixe `_mcp`), pour qu'une réponse écrite par la CLI (24 h) ne soit jamais servie au-delà de 6 h. L'argument `cache` de `moisson_search` l'active ou le désactive pour un appel ; `MOISSON_CACHE` dans le bloc `env` fixe le choix quand l'agent ne précise rien.
- Seules les réponses réussies et en JSON sont gardées : une page d'erreur passagère n'est jamais resservie.
- Aucune attente de politesse sur une réponse servie par le cache.

```bash
export MOISSON_CACHE=1
export MOISSON_CACHE_TTL=86400
export MOISSON_CACHE_DIR=~/.cache/moisson_shs/http
```

### Quelle taille prend le cache ?

Environ **15 Mo par moisson distincte** (une page Isidore de 200 notices pèse près de 9 Mo). Le cache du serveur MCP est entretenu avant une moisson, au plus une fois par heure (`MOISSON_MCP_CACHE_PRUNE_INTERVAL`) : réponses expirées retirées, puis les plus anciennes au-delà de 512 Mo (`MOISSON_MCP_CACHE_MAX_MB`), et compactage. Le cache de la CLI n'est pas entretenu automatiquement : le supprimer au besoin.

| OS | Emplacement par défaut |
|----|------------------------|
| macOS / Linux | `~/.cache/moisson_shs/http.sqlite` et `http_mcp.sqlite` |
| Windows | `%USERPROFILE%\.cache\moisson_shs\` |

Pour tout nettoyer : `rm -rf ~/.cache/moisson_shs/` (le schéma Zotero en cache, `zotero_schema.json`, sera retéléchargé).

### Que se passe-t-il sur une erreur réseau ?

La politique HTTP commune (`utils/http.py`) : une seule tentative sur une erreur définitive (400, 401, 403, 404…) ; au plus trois tentatives sur un 429 ou une erreur transitoire, en respectant `Retry-After` et avec un délai croissant ; délais de connexion (8 s) et de lecture (45 s) séparés ; budget de 120 s par requête. La politesse est tenue par hôte et par compte (0,3 s entre deux départs vers un même hôte par défaut, intervalles propres à Semantic Scholar, CORE, SerpAPI et CrossRef). Le schéma Zotero indisponible n'est retenté qu'après 600 s (`MOISSON_SCHEMA_RETRY_AFTER`).

---

## Serveur MCP

### Que renvoie une moisson à l'agent ?

Par défaut (`inline="digest"`) : un résumé (plan de requête, statut de chaque source, déduplication), des liens vers les fichiers, puis les **40 premières notices** dans l'ordre du classement (titre, trois auteurs, année, support, type, DOI, sources et rangs natifs, termes de la requête retrouvés, réserves comme « sans résumé » ou « extrait Scholar »), le tout sous **15 000 octets**. L'en-tête indique comment obtenir les notices complètes (`corpus_query(corpus=…, ids=[…])`) et la suite (`corpus_query(offset=…)`). `top` règle le nombre de notices (100 au plus).

- `inline="full"` restitue le comportement du sprint 05 : le corpus Zotero inliné jusqu'à 300 notices (`MOISSON_INLINE_MAX_ITEMS`) et 2 000 000 octets (`MOISSON_INLINE_MAX_BYTES`) ;
- `inline="none"` ne rend que le résumé et les liens.

Avant le chantier, une moisson inlinait jusqu'à 300 notices complètes (environ 391 Ko, de l'ordre de 100 000 tokens) ; la moisson de référence tient désormais en 14 917 octets. Le digest ne donne que des métadonnées : un résumé ne permet pas de prétendre avoir lu le texte intégral.

### Comment consulter un corpus sans tout charger ?

Avec `corpus_query` : recherche classée dans le corpus (même normalisation que le classement local : casse, accents, élisions, pluriels, expressions exactes, `OR`, exclusions, troncature), filtres (`year_min`, `year_max`, `item_types`, `languages`, `sources`, `has_doi`, `has_abstract`), choix des champs (`fields`), tri (`relevance`, `rank`, `position`, `year_desc`, `year_asc`, `title`), pages stables (`offset`, `limit` jusqu'à 100), et notices complètes par identifiants (`ids`, 20 au plus, résumés entiers). Les ressources `corpus://<nom>?offset=<n>&limit=<n>` donnent aussi des pages (50 notices par défaut). Si le fichier change pendant la lecture, la page est refusée (« Corpus modifié pendant la lecture ») plutôt que rendue incohérente.

### Comment comparer deux moissons (veille) ?

Avec `corpus_diff(corpus_a=<ancienne>, corpus_b=<nouvelle>)`, ou le prompt `veille-mensuelle`, qui enchaîne moisson, `corpus_list` et comparaison avec le dernier corpus de même requête. L'appariement suit les règles de la déduplication (identifiants ; `fuzzy=true` ajoute la passe floue). Le résultat distingue les ajouts (nouvelle publication, nouveau dépôt HAL, notice récupérée, sans date), les retraits (avec leur motif probable : hors période, source non interrogée ou en échec) et les notices communes enrichies, modifiées ou identiques. Les avertissements signalent ce qui fausse la comparaison : modes ou tris différents, mode bibliographie (la sélection suit les scores), requêtes, sources ou filtres différents, sources incomplètes. Le rapport complet est écrit dans `<radical de B>.diff.json` (ou `<radical de B>_<uuid8>.diff.json` si ce nom est déjà pris).

### Pourquoi le retour annonce-t-il un « succès partiel » ?

Parce qu'une source a réellement perdu des notices : échec, annulation, source non exécutée, source arrêtée par l'échéance ou tronquée par une panne. Une moisson dont seules les sous-sources d'Isidore sont limitées par la fenêtre du fournisseur est annoncée « terminée, sans panne ». Le détail est dans `by_source_status` (statut, motif) et `lossy_sources` / `provider_limited_sources`. Si **toutes** les sources échouent, l'outil rend une erreur MCP (`isError`) et n'écrit aucun fichier.

### Le serveur MCP expire (timeout) dans Claude Desktop

Quand un appel d'outil dépasse le délai du client, celui-ci l'annule
(`MCP error -32001: Request timed out`) et envoie une notification
d'annulation au serveur ; une moisson annulée ne publie rien.

**Quel est ce délai ?** Aucune documentation officielle ne le fixe pour
Claude Desktop, et il peut changer d'une version à l'autre. Ce qui est établi
(vérifié le 2026-09-29) :

| Source | Constat |
|---|---|
| Spécification MCP (cycle de vie, « Timeouts ») | Le délai appartient au client : il *devrait* en fixer un pour chaque requête, *peut* le relancer à chaque notification de progression, et *devrait* toujours imposer un maximum. |
| SDK client TypeScript (`protocol.ts`, branches `v1.x` et principale) | 60 s par défaut par requête (`DEFAULT_REQUEST_TIMEOUT_MSEC = 60000`) ; une notification de progression ne relance le délai que si le client active `resetTimeoutOnProgress` (désactivé par défaut) ; le jeton de progression n'est envoyé que si le client en demande. Code d'expiration : `-32001`. |
| Journaux de Claude Desktop sur le poste de développement (novembre 2025 à juin 2026, plusieurs versions de l'application) | **Juin 2026** : deux `moisson_search` annulés à **240,0 s et 240,1 s** (4 min, `-32001`, le 16 juin). **Mai et juin 2026** : des réponses d'autres serveurs reçues à 62 à 76 s ont été acceptées (26 mai, 3 juin). **Novembre 2025**, avec une version bien plus ancienne de l'application : cinq `initialize` expirés à 60 s (20 et 21 novembre) et des appels d'outil annulés à 242 à 267 s ; aucune expiration d'`initialize` n'est journalisée en 2026. Aucun des 836 appels d'outil journalisés sur toute la période ne portait de jeton de progression : la progression du serveur ne peut pas prolonger le délai de Claude Desktop. Le rapport du sprint 05 relevait déjà l'expiration à 4 min. |
| Signalements publics | 4 min pour Claude Desktop (Windows, avril 2026) ; environ 60 s pour d'autres clients MCP de bureau, dont certains ignorent la variable `MCP_TOOL_TIMEOUT` (mai 2026) ; les clients en ligne de commande appliquent en général `MCP_TOOL_TIMEOUT` et le `timeout` propre à chaque serveur. |

L'ancienne affirmation de cette FAQ (« 60 secondes par défaut pour Claude
Desktop ») ne correspond donc pas à ce qui a été mesuré pour Claude Desktop
(4 min), mais 60 s reste la valeur du SDK et celle d'autres clients. **Le
serveur retient 60 s comme borne prudente.**

**Ce que fait le serveur** : en local (stdio), une **échéance souple** borne la
collecte de `moisson_search`, 50 s par défaut (`MOISSON_STDIO_SOFT_DEADLINE`,
ou l'argument `deadline_seconds` pour un appel, ramené entre 10 et 900 s ;
0 la désactive). À l'échéance, les sources qui n'ont pas fini sont arrêtées
et marquées (motif `deadline`), et le corpus des autres sources est publié :
le retour arrive avant 60 s, avec un succès partiel nommé, au lieu d'une
annulation qui ne publierait rien. Le retour dit si l'échéance a été
atteinte, ou si une source a été arrêtée plus tôt parce que sa prochaine
requête ne pouvait partir à temps (échéance « intenable » : intervalle de
politesse de l'hôte, `Retry-After`), et suggère une valeur de
`deadline_seconds`. Mesure sur simulateur, Isidore muet, client limité à
60 s : retour à 50,1 s avec le corpus HAL publié, contre 120 s (et une
annulation) avant cette échéance.

Ce qu'une source arrêtée garde :

- une source paginée arrêtée en cours de collecte garde les notices des
  pages déjà reçues (y compris les pages de Google Scholar, facturées) et ne
  demande aucune page de plus : elle est « partielle » ;
- une source OpenEdition, Cairn, Persée ou Érudit arrêtée pendant
  l'enrichissement OAI (`enrich_oai`) garde toutes ses notices : celles déjà
  enrichies le restent, les autres gardent leurs métadonnées Isidore. Réserve :
  si la source reste bloquée dans une requête OAI au-delà du délai de grâce
  (une requête OAI ne peut pas être interrompue), elle est abandonnée et ses
  notices Isidore sont reprises sans aucun enrichissement, y compris celles
  qui avaient déjà été enrichies ;
- une source arrêtée avant d'avoir reçu la moindre notice est « en échec » ;
- une source qui n'a pas démarré est « non exécutée ».

Une moisson par défaut (200 notices par source, mode bibliographie, 4 sources
à la fois, cache actif) dure quelques secondes : seule une source qui ne
répond plus est alors concernée. Mais une source lente et saine l'est aussi
dès que la moisson s'allonge : `mode='collecte'` avec un `max_per_source`
élevé (CORE, par exemple, sert des pages de 100 notices à 6 s d'intervalle),
ou `enrich_oai` (une requête OAI par notice). Pour ces appels, relever
`deadline_seconds` (point 4 ci-dessous). En mode distant (HTTP), la deadline
du serveur (`MOISSON_DEADLINE_SECONDS`) reste celle d'avant : dépassée, la
moisson est annulée sans publication partielle.

**Pour une grosse moisson** :

1. Lancer la moisson en CLI (aucune échéance) puis l'analyser depuis l'agent :
   ```bash
   moisson-shs "X" --max 2000 --workers 4 --cache
   ```
   Puis : « Analyse le corpus `output/corpus_X_*.json` » (`corpus_analyze`, `corpus_query`).

2. Restreindre les sources : `--sources hal isidore`.

3. Garder un `max_per_source` modéré dans l'appel MCP (200, défaut, à 500).

4. Avec Claude Desktop, dont le délai mesuré est de 4 min (juin 2026),
   `deadline_seconds` peut être relevé (par exemple 180) pour laisser plus de
   temps aux sources lentes : c'est nécessaire avec `mode='collecte'` et un
   `max_per_source` élevé, ou avec `enrich_oai`. Avec un client limité à
   60 s, ne pas dépasser 50 : une telle moisson ne tient de toute façon pas
   dans son délai, préférer la CLI (point 1).

`corpus_resolve_dois` a son propre budget (`max_seconds`, 240 s par défaut, plafond 900) : avec un client limité à 60 s, le fixer en dessous. Les notices non traitées à l'échéance le sont par un nouvel appel sur le corpus produit.

### Comment vérifier que le serveur MCP est bien installé ?

```bash
moisson-shs setup --status
```

Affiche l'état : présence dans `claude_desktop_config.json`, chemins configurés, clés (masquées), autres serveurs détectés.

### Le serveur MCP ne démarre pas après `setup`

1. Lire les journaux de Claude Desktop :
   - macOS : `~/Library/Logs/Claude/mcp*.log`
   - Windows : `%APPDATA%\Claude\logs\mcp*.log`
   - Linux : `~/.config/Claude/logs/mcp*.log`
2. Tester le serveur à la main :
   ```bash
   moisson-shs serve
   ```
   (il doit attendre sur stdin sans erreur)
3. Vérifier que le Python de `command` est celui où `mcp` et les dépendances sont installés :
   ```bash
   moisson-shs setup --status
   /chemin/vers/python -c "import mcp_server; print('OK')"
   ```
4. Si le projet a été déplacé, réinstaller le paquet éditable (`pip install -e ".[mcp]" -c constraints.txt`) puis relancer `moisson-shs setup`.

### `AttributeError: 'Server' object has no attribute 'list_tools'` au démarrage

Le SDK MCP installé est de la **série 2.x**, qui retire les décorateurs `Server.list_tools`, `call_tool`, `list_resources` (et, sous 2.2.0, `read_resource`, `list_prompts`, `get_prompt`) utilisés par le serveur. Le projet exige **`mcp>=1.27,<2`** ; la migration vers la série 2 est un chantier distinct. Réinstaller aux versions testées, depuis la racine du dépôt, avec le Python indiqué par `moisson-shs setup --status` :

```bash
/chemin/vers/python -m pip install -e ".[mcp]" -c constraints.txt
/chemin/vers/python -c "import mcp_server; print('OK')"
```

`constraints.txt`, à la racine du dépôt, fige les versions testées de toutes les dépendances ; l'image Docker et la CI l'utilisent aussi (voir `DEPLOY.md`, § 6).

### Comment désinstaller le serveur MCP ?

```bash
moisson-shs setup --uninstall
```

La commande retire seulement l'entrée `moisson-shs` de `claude_desktop_config.json` ; les autres serveurs sont préservés et une sauvegarde `.bak` est créée.

---

## Données et confidentialité

### Le moissonneur envoie-t-il des données quelque part ?

Il n'envoie que les requêtes bibliographiques aux API des sources demandées (HAL, Isidore, OAI-PMH d'OpenEdition, Cairn, Persée et Érudit, CrossRef, OpenAlex, Semantic Scholar, CORE, SerpAPI pour Google Scholar), et, au premier lancement puis tous les 30 jours, une demande du schéma Zotero (`api.zotero.org/schema`). Aucune télémétrie. Les corpus restent sur le disque, dans `output/` (ou `MOISSON_OUTPUT_DIR`). Le mode audit (`MOISSON_AUDIT_NATIVE=1`), désactivé par défaut, garde localement les réponses brutes des API, expurgées de leurs clés.

### Le serveur MCP transmet-il les corpus à un service tiers ?

Le serveur local s'exécute sur la machine et dialogue avec le client MCP par stdin/stdout. Les corpus restent sur le disque ; le client (et le service de conversation qui l'accompagne) ne reçoit que ce que les outils renvoient : le digest, ou ce que l'agent demande par `corpus_query`, `read_resource`, `inline="full"`. La politique de confidentialité du client s'applique à ces contenus.

---

## Développement

### Comment ajouter une nouvelle source ?

1. Créer `connectors/<nom>.py` héritant de `connectors.base.Connector` et implémenter `search(query, max_results, **kwargs)` ; publier le compte rendu de la collecte (`last_status`, un `SearchReport`) pour que le moteur en déduise le statut.
2. Si nécessaire, créer un mapper `mappers/<nom>_mapper.py`.
3. Enregistrer la classe dans `connectors/registry.py` (`CONNECTORS`, et `GROUP_PROXY_SOURCES` si la source passe par le flux Isidore, `METERED_SOURCES` si elle est facturée).
4. Décrire ses capacités de requête dans `connectors/capabilities.py` (syntaxe, filtres, tri, langues, avec la date et la preuve de vérification) : le plan de requête s'en sert.
5. Ajouter ses libellés dans `utils/source_labels.py` et sa description (`SOURCE_DESCRIPTIONS`) pour l'aide.

`main.py`, `mcp_server.py` et l'aide de la CLI s'adaptent ensuite.

### Comment lancer les tests ?

```bash
pip install -e ".[dev,mcp]" -c constraints.txt   # versions testées (dont mcp<2)
pytest tests/ -m "not integration"                 # sans réseau externe (comme la CI)
pytest tests/ -m "not integration and not sockets" # ni réseau ni socket locale
pytest tests/ -m sockets                           # transport HTTP local
pytest tests/ -m "not integration" --cov=mappers --cov=utils --cov=connectors
```

Marqueurs : `integration` désigne les appels réels aux API des fournisseurs ; `sockets` les tests qui ouvrent une socket locale (serveur uvicorn de test, client HTTP loopback), exécutés en CI mais à écarter dans un bac à sable sans sockets. Par défaut, les tests ne lisent aucun `.env` et ne voient aucune clé API du poste (`tests/conftest.py`) : un test qui a besoin d'une clé la pose lui-même.

Pour vérifier en local une source à clé (OpenAlex, Semantic Scholar, CORE), exporter la clé dans le shell et poser l'opt-in :

```bash
MOISSON_TEST_REAL_KEYS=1 pytest tests/integration -m integration
```

Sans `MOISSON_TEST_REAL_KEYS=1`, une clé exportée est masquée : le test correspondant est ignoré avec le motif « … absente », et le résumé de fin de session nomme les clés masquées. Avec l'opt-in, les clés gratuites ne sont visibles que des tests marqués `integration` (les tests unitaires ne les voient jamais) ; le `.env` n'est toujours pas lu et la clé SerpAPI (source facturée) est retirée dans tous les cas.

### Comment mesurer les performances ?

Avec le banc `scripts/benchmark_moisson.py`, qui rejoue sans réseau quinze scénarios (référence, requête rare, dates restrictives, source lente, 429, source indisponible, hôte injoignable, grand corpus, métadonnées contradictoires, 404, réponse invalide, cache chaud, schéma indisponible…) :

```bash
python scripts/benchmark_moisson.py --list
python scripts/benchmark_moisson.py --scenario reference --entry mcp --repeat 3 --json-out bench.json
python scripts/benchmark_moisson.py --compare tests/fixtures/bench/baseline_2026-09-29.summary.json bench.json
```

La référence à comparer est `baseline_2026-09-29.summary.json` : la copie d'avant chantier remesurée avec le simulateur actuel. `baseline_2026-09-28.summary.json` garde la première mesure, faite avec les définitions de scénarios de l'époque (ordres de tri, fenêtre Isidore et `reponse_invalide` redéfinis depuis) : ses couvertures ne se comparent plus directement.

`--sleep-scale 0` supprime les attentes (compteurs seulement), `--latency-ms 0,150` simule une latence, `--live` mesure en réseau réel (API gratuites seulement, jamais SerpAPI). `scripts/measure_duplicates.py` mesure les doublons résiduels d'un dossier de corpus.

### Comment évaluer le classement ?

Le protocole SHS (24 requêtes, jugement en aveugle, P@20, nDCG@20, rappel sur références connues) est décrit dans [`docs/EVALUATION.md`](EVALUATION.md) ; il décide l'activation du classement local (`MOISSON_RANKING=local`), désactivé par défaut.

### Comment contribuer ?

PRs bienvenues sur https://github.com/amarlakel/moisson-shs/pulls. Avant la PR :

1. `pytest tests/ -m "not integration"` doit passer ;
2. la couverture sur `mappers/`, `utils/` et `connectors/` doit rester ≥ 70 % (seuil vérifié par la CI) ;
3. pour une nouvelle source, fournir une fixture réelle anonymisée dans `tests/fixtures/` et sa description de capacités.

---

## Encore une question ?

Ouvrir une issue : https://github.com/amarlakel/moisson-shs/issues
