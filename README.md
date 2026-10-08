# moisson-shs — Moissonneur bibliographique SHS multi-sources

Moissonne en une commande **douze sources** bibliographiques — sept en sciences humaines et sociales francophones, quatre internationales, plus Google Scholar — et produit un corpus directement importable dans Zotero.

- **Formats** : Zotero JSON interne (lu par les outils) et CSL-JSON (import Zotero Desktop) à chaque moisson ; exports BibTeX, BibLaTeX et RIS à la demande ; un **manifeste** par corpus trace la requête, les statuts des sources, les rangs et les scores.
- **Serveur MCP** : les mêmes fonctions sont exposées à un agent (Claude Desktop en local par stdio, ou un client distant par HTTPS authentifié) sous la forme de douze outils, quatre prompts et des ressources `corpus://`.

## Sources

### Sources francophones (sans clé)

| Source | Clé | Couverture | Accès |
|--------|-----|------------|-------|
| **HAL** | `hal` | Production scientifique française | API Solr native ; tri par score en mode bibliographie, parcours exhaustif par curseur en mode collecte |
| **Isidore** (Huma-Num) | `isidore` | Moteur SHS qui indexe les autres plateformes | API REST native ; **fenêtre de 200 résultats** (voir ci-dessous) |
| **OpenEdition** | `openedition` | OpenEdition Journals et Books (domaine `openedition.org`) | Flux Isidore partagé + OAI-PMH optionnel ; les carnets Hypothèses et Calenda ne sont pas captés |
| **Cairn.info** | `cairn` | Revues SHS francophones | Flux Isidore partagé + OAI-PMH optionnel |
| **Persée** | `persee` | Périodiques SHS patrimoniaux | Flux Isidore partagé + OAI-PMH optionnel |
| **Theses.fr** | `theses_fr` | Thèses françaises | Flux Isidore partagé (l'API REST publique a été retirée en 2026) |
| **Érudit** | `erudit` | Revues SHS québécoises et canadiennes | Flux Isidore partagé + OAI-PMH optionnel |

**La fenêtre d'Isidore et ses conséquences.** L'API d'Isidore annonce le nombre total de résultats (plusieurs centaines de milliers pour une requête large) mais n'en sert que les **200 premiers** ; toute page au-delà répète la dernière. Une moisson envoie donc **une seule requête Isidore** (`replies=200`), dont la réponse est distribuée à la source `isidore` et aux cinq sous-sources, chacune filtrant les notices de son domaine d'URL. Conséquences :

- une sous-source ne peut rendre que les notices de son domaine présentes parmi ces 200 résultats. Mesure live du 2026-09-29 (« discours numérique », depuis 2018) : Isidore 141 notices, OpenEdition 49, Cairn 4, Persée 0, Theses.fr 5, Érudit 6 ;
- ces sources sont alors `partial` avec le motif `provider_window` : c'est une **limite du fournisseur, pas une panne**, et le retour le dit ainsi (« limitée(s) par le fournisseur ») ;
- plusieurs sous-sources issues du même flux ne valent pas des confirmations indépendantes d'une même notice ;
- pour atteindre des notices plus loin dans le classement d'Isidore : une requête plus précise (expression exacte, période) ; HAL, lui, sert jusqu'à 10 000 notices classées par score.

### Sources internationales (REST natif)

| Source | Clé | Couverture | Clé d'API | Particularités |
|--------|-----|------------|-----------|----------------|
| **CrossRef** | `crossref` | Métadonnées d'éditeurs | aucune (pool poli par adresse de contact) | requête traitée en sac de mots ; tri par score en mode bibliographie |
| **OpenAlex** | `openalex` | Graphe scientifique ouvert | **requise** (gratuite) | honore guillemets et booléens ; score de pertinence ; `cited_by_count` |
| **Semantic Scholar** | `semantic_scholar` | Academic Graph | optionnelle | syntaxe `+ \| -` ; en mode bibliographie, trié par nombre de citations (pas par pertinence) |
| **CORE** | `core` | Agrégateur de dépôts en accès ouvert | **requise** | AND/OR ; texte intégral et PDF |

Une source dont la clé requise manque n'est pas interrogée : elle apparaît `skipped` (motif `missing_key`) et la moisson des autres sources se poursuit. Clés : voir [`.env.example`](.env.example) et [docs/API_SPECS.md](docs/API_SPECS.md).

### Google Scholar via SerpAPI : source facturée, à nommer

| Source | Clé | Couverture | Clé d'API | Particularités |
|--------|-----|------------|-----------|----------------|
| **Google Scholar** | `google_scholar` | La plus large : actes, chapitres, littérature grise, dépôts | **requise** (SerpAPI) | compteur de citations ; **ni DOI ni résumé** |

C'est la seule source **facturée à la requête** : une page de vingt résultats consomme un crédit du compte SerpAPI (offre payante à 5 000 requêtes par mois, partagée avec le projet MyWebIntelligence). Elle reste donc **volontaire** :

1. elle n'appartient **ni au groupe `international` ni au groupe `all`** ; elle ne s'active que nommée (`scholar`, `google_scholar`, ou une combinaison explicite), et `plan_only` permet de voir ce qui lui serait envoyé sans rien consommer ;
2. chaque appel est borné par `MOISSON_SERPAPI_MAX_PAGES` (défaut 10, soit 200 notices pour 10 crédits) et refusé si le solde du compte passe sous `MOISSON_SERPAPI_MIN_CREDITS` (défaut 100 ; contrôle gratuit, hors quota).

Le plan de requête transmet à Scholar sa syntaxe propre (`"…"`, `OR`, `-terme`), comme il transmet à chaque source la sienne : la syntaxe n'est plus une raison de tenir Scholar à part, le coût et le quota partagé le sont.

Contrepartie documentaire : Google Scholar ne renvoie aucune métadonnée structurée. Auteurs, revue et année sont extraits d'une ligne de notice que Google tronque ; les notices portent dans `extra` les avertissements `Scholar truncation` et `Scholar snippet`, et **jamais de DOI deviné**. L'outil `corpus_resolve_dois` retrouve ensuite les DOI manquants par appariement contre CrossRef, sous une règle stricte (titre ≥ 0,90, année et premier auteur corroborés). Ces notices servent à repérer et à hiérarchiser, pas à citer en l'état.

### Groupes de sources

| Groupe | Sources |
|--------|---------|
| `francophone` (défaut) | `hal`, `isidore`, `openedition`, `cairn`, `persee`, `theses_fr`, `erudit` |
| `international` | `crossref`, `openalex`, `semantic_scholar`, `core` |
| `all` | les onze précédentes (**sans** Google Scholar) |
| `scholar` | `google_scholar` seul |

Un groupe ne traduit jamais la requête : avec `all`, une requête française part telle quelle vers les sources internationales, avec un avertissement du plan, tant qu'aucune variante anglaise n'est fournie (`--query-en`, `query_variants`).

## Pour qui ?

Chercheurs et étudiants SHS qui ont besoin de constituer une bibliographie thématique, de suivre la production d'un auteur ou de tenir une veille. Configuré dans Claude Desktop (`moisson-shs setup`), le moissonneur répond aussi en langage naturel : « Fais-moi la bibliographie de Patrick Charaudeau » déclenche l'outil correspondant.

---

## Démarrage rapide

### 1. Installation

```bash
git clone https://github.com/amarlakel/moisson-shs
cd moisson-shs
python3 -m venv venv
source venv/bin/activate         # macOS / Linux (Windows : venv\Scripts\activate.bat)
pip install -e ".[mcp]" -c constraints.txt
```

`constraints.txt` fige les versions testées de toutes les dépendances. Le SDK MCP est borné à **`mcp>=1.27,<2`** : la série 2 retire les décorateurs `Server.list_tools`, `call_tool` et `list_resources` sur lesquels repose le serveur (`import mcp_server` échouerait). Si le dossier du projet a été déplacé après l'installation, réinstaller (`pip install -e ".[mcp]" -c constraints.txt`) : l'entrée `venv/bin/moisson-shs` garde l'ancien chemin.

### 2. Première moisson

```bash
moisson-shs "discours numérique" --max 100 --workers 4
```

Le moissonneur interroge les sept sources francophones (quelques secondes : voir [Mesures](#mesures)), déduplique les notices, puis publie dans `output/` :

| Fichier | Usage |
|---------|-------|
| `corpus_<requête>_<horodatage>_<uuid>.json` | Zotero JSON interne, lu par les outils (analyse, filtre, export…) |
| `corpus_<requête>_<horodatage>_<uuid>.csl.json` | **CSL-JSON, importable directement dans Zotero Desktop** |
| `corpus_<requête>_<horodatage>_<uuid>_stats.txt` | Statistiques (sources, types, années, couverture DOI et résumés) |
| `corpus_<requête>_<horodatage>_<uuid>.manifest.json` | Manifeste : requête et plan, réglages, statut de chaque source, rangs et scores par notice ; publié en dernier |
| `raw/<source>_<horodatage>_<uuid>.json` | Notices normalisées de chaque source, avant déduplication |

La fin de la sortie donne le statut de chaque source. Si aucune source n'a répondu sans panne et qu'aucune notice n'a été obtenue, rien n'est écrit et le code de sortie vaut **3**.

### 3. Import dans Zotero

1. Zotero Desktop, menu **Fichier > Importer**, « Un fichier (BibTeX, RIS, Zotero RDF…) ».
2. Choisir le **`.csl.json`** (pas le `.json`, que Zotero Desktop ne reconnaît pas), ou un export BibTeX / RIS (`moisson-shs export`).
3. Cocher « Placer les éléments importés dans une nouvelle collection ».

La provenance (sources d'origine, identifiants HAL ou OAI, preuves de fusion) passe dans le champ `note` du CSL, que Zotero range dans `extra`.

### 4. (Optionnel) Claude Desktop

```bash
moisson-shs setup
```

La commande détecte l'OS et le Python du venv, puis ajoute l'entrée `moisson-shs` à `claude_desktop_config.json` en préservant les autres serveurs (sauvegarde `.bak`). Quitter complètement Claude Desktop puis le relancer. Guide détaillé : [MCP_INSTALL.md](MCP_INSTALL.md).

---

## Ligne de commande

### `moisson-shs search` (ou `moisson-shs "requête"`)

```bash
moisson-shs search "intelligence artificielle" --max 500 --workers 4 --cache --from-date 2020 --to-date 2024
moisson-shs search "AI regulation" --sources international --from-date 2022
moisson-shs search '"discours numérique" AND (twitter OR facebook)' --sources all --query-en '"digital discourse"' --lang fr en --plan-only
moisson-shs search '"large language models" discourse' --sources scholar --max 40
```

| Option | Effet |
|--------|-------|
| `--max` | Plafond de notices par source (défaut 1000) |
| `--sources` | Sources et/ou groupes : noms individuels, `francophone` (défaut), `international`, `all`, `scholar` |
| `--workers` | Sources interrogées en même temps, dans un pool unique (défaut 1 ; 4 recommandé, défaut du serveur MCP ; borné par `MOISSON_HARVEST_MAX_WORKERS`). La politesse par serveur s'applique quel que soit ce nombre |
| `--mode` | `collecte` (défaut en CLI : ordre déterministe, exhaustif dans la limite du plafond) ou `bibliographie` (meilleures notices selon l'ordre natif de chaque source) |
| `--from-date`, `--to-date` | Bornes inclusives (`AAAA` ou `AAAA-MM-JJ`) ; filtre natif là où la source le permet, local ailleurs (voir `--plan-only`) |
| `--lang` | Langues des documents (codes ISO 639-1) : filtre natif (HAL, OpenAlex, Google Scholar), local ailleurs (notices sans langue conservées), impossible pour CrossRef et Semantic Scholar (signalé) |
| `--query-en` | Variante anglaise de la requête, envoyée aux sources majoritairement anglophones (CrossRef, OpenAlex, Semantic Scholar, CORE) ; sans elle, aucune traduction automatique |
| `--variant` | Variante explicite pour une langue, répétable : `--variant en='"digital discourse"'` |
| `--plan-only` | Affiche le plan de requête (requête envoyée à chaque source, filtres natifs et locaux, avertissements) sans aucune requête réseau ni fichier |
| `--on-ambiguity` | Requête ambiguë (OR mêlé sans parenthèses à des termes juxtaposés, à AND ou à une exclusion) : `warn` l'envoie avec un avertissement, `refuse` ne l'envoie pas ; défaut : `refuse` si une source facturée est demandée, `warn` sinon |
| `--hal-fulltext` | HAL cherche aussi dans le texte intégral des dépôts (`fulltext_t`, 36 % des dépôts) ; sans l'option, dans les métadonnées seulement (titre, résumé, mots-clés). Ensemble bien plus large et plus bruité |
| `--ranking` | Classement affiché en fin de moisson, sans modifier le fichier : `native` (défaut), `local` (expérimental), `off` |
| `--cache` / `--no-cache` | Active ou désactive le cache HTTP local (24 h, `MOISSON_CACHE_TTL`) ; sans l'une ni l'autre, valeur de `MOISSON_CACHE` (désactivé par défaut) |
| `--enrich-oai` | Enrichit OpenEdition, Cairn, Persée et Érudit par OAI-PMH (une requête par notice : plus lent) |
| `--no-dedup` | Désactive la déduplication (audit) |
| `--output` | Répertoire de sortie (défaut `./output`) |
| `--verbose` | Journal détaillé (DEBUG) |

La requête accepte les opérateurs booléens (précédence NOT > AND > OR, `-terme`, guillemets, parenthèses). Le **plan de requête** la traduit pour chaque source (voir [Plan de requête](#plan-de-requête)) ; `--plan-only` montre la lecture retenue.

### `moisson-shs export`

Exporte un corpus Zotero interne en BibTeX, BibLaTeX ou RIS.

```bash
moisson-shs export output/corpus_x.json --format ris
moisson-shs export output/corpus_x.json -f biblatex --ascii -o refs.bib
```

| Option | Effet |
|--------|-------|
| `--format` | `bibtex` (défaut, import Zotero et JabRef), `biblatex` (LaTeX avec biber) ou `ris` (Zotero, EndNote) |
| `--output` | Fichier produit (défaut : `<radical du corpus>_<uuid8>.bib` ou `.ris`, à côté du corpus) |
| `--ascii` | BibTeX / BibLaTeX : accents en commandes LaTeX (défaut UTF-8) |
| `--include-provenance` | Ajoute la provenance de moisson (lignes du champ `extra`) en note ; jamais de rang ni de score |

Les clés de citation sont stables (`nom + année + premier mot du titre`, suffixes `a`, `b`… en cas de collision) et ne dépendent pas de l'ordre des notices. Un extrait Google Scholar n'est jamais exporté comme résumé. Un CSL-JSON, un manifeste ou un rapport sont refusés (code 2). L'export est tracé dans le manifeste du corpus source (ou dans un manifeste propre s'il est écrit dans un autre dossier).

### `moisson-shs setup`

```bash
moisson-shs setup                    # installation dans Claude Desktop
moisson-shs setup --client code      # Claude Code, portée utilisateur (all : les deux)
moisson-shs setup --output-dir ~/corpus_shs
moisson-shs setup --dry-run          # aperçu sans modifier
moisson-shs setup --status           # état actuel
moisson-shs setup --uninstall        # désinstaller (préserve les autres serveurs)
moisson-shs setup --remote https://moisson.example.org/mcp --token "$MOISSON_API_TOKEN"   # bloc client d'un serveur distant
```

| Option | Effet |
|--------|-------|
| `--client` | Client configuré : `desktop` (défaut), `code` (Claude Code, portée utilisateur, par `claude mcp add-json`), `all` |
| `--output-dir` | Répertoire des corpus du serveur (`MOISSON_OUTPUT_DIR`) |
| `--cache` | Écrit `MOISSON_CACHE=1` dans le bloc `env` (le cache est déjà actif par défaut côté MCP) |
| `--openalex-key`, `--semantic-scholar-key`, `--core-key`, `--serpapi-key` | Clés inscrites dans le bloc `env` (sinon reprises du shell, sinon demandées en saisie masquée, sinon valeur déjà déclarée, sinon vides : les quatre variables sont toujours déclarées) |
| `--no-prompt` | Aucune saisie interactive (installation scriptée) |
| `--dry-run`, `--status`, `--uninstall` | Aperçu, état (dont celui de chaque clé : déclaration, réserve du `.env`, repli permis), désinstallation |
| `--python`, `--server-path`, `--config`, `--claude-code-config` | Chemins imposés (défaut : détection automatique) |
| `--remote` | Imprime le bloc de configuration d'un client distant (avec `--token`, `--name`) |

### `moisson-shs serve`

```bash
moisson-shs serve                                   # stdio (Claude Desktop local, débogage)
moisson-shs serve --http --host 127.0.0.1 --port 8000   # serveur distant (Streamable HTTP)
```

En HTTP : authentification **Bearer** (et OAuth 2.1 en option), chemins confinés sous `OUTPUT_DIR`, retour par **URL de téléchargement** (jamais de chemin disque ni de JSON bibliographique inline), progression, échéance serveur, plafonds anti-DoS, journaux JSON, audit, `/metrics`. Déploiement Docker + Caddy : [DEPLOY.md](DEPLOY.md). Guide complet : [REMOTE.md](REMOTE.md).

### `moisson-shs token` et `moisson-shs version`

`moisson-shs token {generate,rotate,status,revoke}` gère les jetons Bearer du serveur distant (seule l'empreinte SHA-256 est conservée) ; `moisson-shs version` affiche la version, le commit, l'état de l'arbre de travail et l'empreinte du code, les mêmes que ceux des manifestes.

---

## Serveur MCP

### Défauts d'une moisson lancée par l'agent

Une moisson lancée depuis un agent doit tenir sous le délai d'appel d'outil du client ; ses défauts diffèrent donc de ceux de la CLI.

| Réglage | Outil `moisson_search` | CLI |
|---------|------------------------|-----|
| Notices par source | 200 (`MOISSON_MCP_MAX_PER_SOURCE`) | 1000 |
| Sources en parallèle | 4 (`MOISSON_MCP_WORKERS`) | 1 |
| Cache HTTP | actif, 6 h (`MOISSON_MCP_CACHE_TTL`), fichier propre `http_mcp.sqlite` | inactif (sauf `MOISSON_CACHE=1`), 24 h |
| Mode | `bibliographie` | `collecte` |
| Échéance de collecte | souple, 50 s en stdio ; dure, 900 s en HTTP | aucune |
| Retour | digest de 40 notices, 15 000 octets au plus | sortie texte et fichiers |

En HTTP, `MOISSON_MAX_ITEMS_PER_SOURCE` (3000) plafonne toujours la valeur demandée.

### Outils (12)

Les neuf outils historiques sont conservés avec leurs arguments ; `corpus_export`, `corpus_query` et `corpus_diff` s'y ajoutent.

| Outil | Rôle | Arguments (défaut) |
|-------|------|--------------------|
| `moisson_search` † | Moisson multi-sources | `query` (requis), `sources` (groupe francophone), `max_per_source` (200), `from_date`, `to_date`, `mode` (bibliographie), `query_variants`, `languages`, `plan_only` (faux), `on_ambiguity` (refus si source facturée, sinon avertissement), `hal_fulltext` (faux), `ranking` (native), `deadline_seconds` (50 en stdio), `cache` (actif), `workers` (4), `enrich_oai` (faux), `dedup` (vrai), `inline` (digest), `top` (40) |
| `corpus_analyze` | Statistiques d'un corpus | `corpus_path` (requis) |
| `corpus_list` | Corpus disponibles, lus dans l'index | `limit` (20), `pattern`, `since`, `rebuild` (faux) |
| `corpus_filter` † | Filtre par année, type, source, langue, titre | `corpus_path` (requis), `year_min`, `year_max`, `item_types`, `sources`, `language`, `keep_unknown_language` (vrai), `title_contains`, `output_suffix` (suffixe _filtered), `inline`, `top` |
| `corpus_to_csl` † | Conversion Zotero → CSL-JSON | `corpus_path` (requis), `output_path`, `inline`, `top` |
| `corpus_merge` † | Fusion de plusieurs corpus, déduplication comprise | `corpus_paths` (requis), `output_path`, `inline`, `top` |
| `fetch_by_doi` | Notice d'un DOI (six résolveurs en parallèle) | `doi` (requis), `add_to_corpus` |
| `search_by_author` † | Production d'un auteur, avec contrôle d'identité | `name` (requis ; chaîne vide si un identifiant est fourni), `idhal`, `orcid`, `openalex_author_id`, `s2_author_id`, `hal_person_id`, `max_per_source` (200), `save_corpus` (vrai), `inline`, `top` |
| `corpus_resolve_dois` † | DOI manquants par appariement CrossRef | `corpus_path` (requis), `title_threshold` (0,90, plancher), `strict_title_threshold` (0,97, plancher), `year_tolerance` (1, plafond), `max_items`, `max_seconds` (240, plafond 900), `output_suffix` (suffixe _doi), `also_csl` (vrai), `inline`, `top` |
| `corpus_export` † | Export BibTeX, BibLaTeX ou RIS | `corpus_path` (requis), `format` (bibtex), `output_path`, `ascii` (faux), `include_provenance` (faux), `inline`, `top` |
| `corpus_query` | Consultation d'un corpus : recherche classée, filtres, projection, pagination, notices complètes | `corpus` (requis), `query`, `filters`, `fields`, `sort`, `offset` (0), `limit` (20), `ids` (20 au plus) |
| `corpus_diff` | Comparaison de deux corpus (veille) | `corpus_a` (requis), `corpus_b` (requis), `fuzzy` (faux), `sample` (20), `output_path` |

† Outils producteurs : ils publient un corpus (ou un export) et renvoient le retour décrit ci-dessous.

Précisions :

- **`search_by_author`** : priorité aux identifiants (idHal, ORCID, identifiants OpenAlex, Semantic Scholar, HAL). Par le nom seul, plusieurs profils sont examinés ; en cas d'homonymie (au moins deux candidats d'au moins cinq travaux), l'outil ne retient rien, rend `ambiguous` avec les candidats et l'argument du second appel (`next_step`, `profiles_to_confirm`), et n'écrit aucun fichier. Un ORCID seul interroge HAL et OpenAlex par identifiant ; `name` reste exigé par le schéma d'entrée : le passer vide (`name=""`), sans quoi l'appel est refusé à la validation. Les notices Isidore ne sont gardées que si leurs créateurs portent le nom. `production_complete` n'est vrai que si aucune source n'est plafonnée, indisponible ou ambiguë.
- **`corpus_resolve_dois`** : les seuils ne peuvent que durcir la règle ; une valeur plus permissive est refusée. Le rapport complet (acceptés, rejetés, ambigus, sans candidat, erreurs, non traités, version des règles `doi-match/4`) est écrit à côté du corpus enrichi `<radical><output_suffix>.json`, dans `<radical><output_suffix>.doi_report.json` (suffixe `_doi` par défaut ; une résolution de même suffixe remplace la précédente). Le budget par défaut (240 s) vise le délai de Claude Desktop ; avec un client limité à 60 s, passer `max_seconds`.
- **`corpus_query`** : même normalisation que le classement local (casse, accents, élisions, pluriels, expressions exactes, `OR`, exclusions, troncature). Pagination stable (`offset`, `limit` ≤ 100) ; `ids` rend les notices complètes, résumés entiers. En HTTP, la sélection est publiée comme corpus dérivé `<radical>_sel_<uuid>.json` (URL seule) ; les dix dernières sélections par corpus source sont gardées (`MOISSON_QUERY_HTTP_KEEP`).
- **`corpus_diff`** : appariement par identifiants, selon les règles de la déduplication (`fuzzy` ajoute la passe floue) ; catégories `added`, `removed`, `unchanged` (`enriched`, `modified`, `identical`) ; les ajouts distinguent nouvelle publication, nouveau dépôt et notice récupérée ; avertissements lus dans les manifestes (modes, tris, requêtes ou sources différents). Rapport complet `<radical de B>.diff.json` (format `moisson-shs/diff`), ou `<radical de B>_<uuid8>.diff.json` si le premier est pris : une comparaison ne remplace jamais le rapport d'une autre, sauf `output_path` explicite ; le retour est borné à 15 000 octets par défaut, 100 000 avec `sample`.
- **Entrées refusées** : les outils qui exigent un corpus Zotero interne refusent clairement (erreur d'outil) un CSL-JSON, un manifeste, un rapport DOI ou de comparaison, ou un contenu qui n'est pas un tableau de notices.

### Prompts

| Prompt | Arguments | Cas d'usage |
|--------|-----------|-------------|
| `bibliographie-thematique` | `theme` (requis), `max_per_source` (facultatif, défaut du serveur) | Bibliographie sur un thème, multi-sources, avec analyse (sélection classée, pas une collecte exhaustive) |
| `profil-chercheur` | `nom`, `idhal`, `orcid` (tous facultatifs ; un nom **ou** un identifiant suffit : `orcid` ou `idhal` seul est accepté) | Production d'un chercheur, flux d'homonymie compris (alimente la skill `ProfilBiblio`) ; la liste n'est jamais présentée comme la production complète |
| `veille-mensuelle` | `theme`, `from_date`, `to_date` (requis) | Veille sur une période, puis `corpus_diff` contre la dernière moisson de même requête |
| `etat-de-l-art` | `question` (requis), `question_en` (facultatif) | État de l'art sur les dernières années, variantes FR/EN explicites : sans `question_en`, l'agent propose une formulation anglaise et la fait valider avant la moisson (aucune traduction automatique) |

Les quatre prompts rappellent le statut et les limites de chaque source, expliquent la sélection, séparent les résultats bibliographiques de leur interprétation et interdisent de prétendre avoir lu un texte intégral à partir d'un résumé. Un argument obligatoire absent est demandé à l'utilisateur.

### Ressources

Chaque corpus de `OUTPUT_DIR` est exposé sous l'URI `corpus://<nom>`, et par pages sous `corpus://<nom>?offset=<n>&limit=<n>` (50 notices par défaut, 200 au plus). Les fichiers de service (`.oauth/`, `.audit/`, `.index.json`…) et les fichiers associés (manifestes, rapports) ne sont jamais exposés comme ressources.

### Retour des outils producteurs

L'argument `inline` règle ce qui revient à l'agent en stdio :

| `inline` | Contenu |
|----------|---------|
| `digest` (défaut) | Résumé texte, `ResourceLink` vers chaque fichier, puis les `top` premières notices (40 par défaut, 100 au plus) dans l'ordre du classement : rang, titre, trois auteurs, année, support, type, DOI ou identifiant, sources et rangs natifs, termes appariés, réserves. Plafond : **15 000 octets** pour le retour complet ; au-delà, les dernières notices passent en ligne courte puis sont omises, et l'en-tête donne la suite exacte (`offset`) |
| `full` | Comportement du sprint 05 : corpus Zotero inliné (`EmbeddedResource`) jusqu'à 300 notices (`MOISSON_INLINE_MAX_ITEMS`) et 2 000 000 octets (`MOISSON_INLINE_MAX_BYTES`) |
| `none` | Résumé et liens seulement |

Le digest renvoie à `corpus_query(corpus=…, ids=[…])` pour les notices complètes et à `corpus_query(offset=…)` pour la suite. Il ne contient que des métadonnées : rien n'autorise à prétendre avoir lu le texte intégral.

`structuredContent` reprend les données machine : `query`, `mode`, `settings`, `by_source` et `by_source_status` (statut et motif par source ; en mode `full`, aussi `native_sort`, `native_filters`, pages et compteurs), `harvest_status`, `lossy_sources`, `provider_limited_sources`, `dedup_report`, `query_plan`, `top_ranked`, `top_items`, `digest`, `soft_deadline`, `manifest`, `files`, `resources`, `item_count`. En mode digest, le plan et les statuts sont résumés et renvoient au manifeste, qui garde tout ; `search_by_author` compacte de même son rapport (`report_compact`, `report_detail`).

**En HTTP**, `inline` est sans effet : texte sans chemin et URL de téléchargement (`download_urls`), jamais de JSON bibliographique inline.

### Statuts par source et erreurs

Chaque source reçoit un statut, dans le retour, le manifeste et la sortie de la CLI :

| Statut | Sens |
|--------|------|
| `complete` | Périmètre demandé exécuté (plafond atteint ou fournisseur épuisé) |
| `empty` | Exécutée, sans résultat |
| `partial` | Tronquée : fenêtre du fournisseur (`provider_window`), borne de parcours (`scan_bound`), erreur après des pages réussies, budget, échéance (`deadline`) |
| `skipped` | Non exécutée : clé absente (`missing_key`), crédits SerpAPI insuffisants, échéance atteinte avant le démarrage |
| `error` | Échec sans résultat exploitable (`http_error`, `network_error`, `invalid_body`, `deadline`…) |
| `cancelled` | Annulation demandée par le client, ou échéance dure dépassée (HTTP) : rien n'est publié |

Une panne n'est jamais présentée comme « aucun article ». Si toutes les sources échouent, l'outil rend une **vraie erreur MCP** (`isError`), n'écrit aucun fichier, et la CLI sort avec le code 3. Un paramètre invalide est aussi une erreur d'outil, rendue avant toute requête. Une moisson où seules les sous-sources d'Isidore sont limitées par la fenêtre du fournisseur est annoncée « terminée, sans panne ».

### Échéance souple et délai d'outil

Un client MCP annule un appel d'outil qui dépasse son délai, et une moisson annulée ne publie rien. Le délai de Claude Desktop n'est pas documenté : **240 s** ont été mesurées dans ses journaux (juin 2026), mais 60 s est le défaut du SDK client et celui d'autres clients. **Le serveur retient 60 s comme borne prudente** (détail et sources : [docs/FAQ.md](docs/FAQ.md#le-serveur-mcp-expire-timeout-dans-claude-desktop)).

En stdio, `moisson_search` borne donc sa collecte par une **échéance souple de 50 s** (`MOISSON_STDIO_SOFT_DEADLINE`, ou l'argument `deadline_seconds`, ramené entre 10 et 900 s ; 0 la désactive). À l'échéance, les sources encore en cours sont arrêtées (motif `deadline`) et gardent les notices déjà reçues ; le corpus des autres sources est publié, avec une valeur suggérée de `deadline_seconds`. Mesure sur simulateur, Isidore muet, client limité à 60 s : retour à 50,1 s avec le corpus HAL publié, contre 120 s et une annulation avant cette échéance. En HTTP, l'échéance reste dure (`MOISSON_DEADLINE_SECONDS`, 900 s, sans publication partielle).

### Plan de requête

Le registre des capacités (`connectors/capabilities.py`, vérifié source par source le 2026-09-29, voir [docs/API_SPECS.md](docs/API_SPECS.md)) gouverne la traduction de la requête :

- HAL et OpenAlex reçoivent `AND`/`OR`/`NOT`, Isidore et ses sous-sources `ET`/`OU`/`SAUF`, Semantic Scholar `+ | -`, CORE `AND`/`OR` (exclusions retirées), Google Scholar `"…"`, `OR`, `-terme` ;
- CrossRef traite la requête en sac de mots : il reçoit une variante dérivée (mots des parties positives), toujours affichée ;
- **aucune traduction automatique** : une source anglophone ne reçoit une requête anglaise que si une variante `en` est fournie (`query_variants`, `--query-en`, `--variant`) ;
- `languages` et les dates sont appliqués nativement là où la source le permet, localement sinon, et signalés quand c'est impossible ;
- le plan complet (requête envoyée, filtres natifs et locaux, avertissements, bloc `applied`) figure dans `structuredContent.query_plan` et dans le manifeste ; `plan_only` (ou `--plan-only`) le rend sans rien exécuter.

### Classement

Après déduplication, un classement est calculé sans modifier les notices ni l'ordre du fichier (`MOISSON_RANKING`, argument `ranking`, option `--ranking`) :

- `native` (défaut) : fusion des rangs natifs des sources qui classent réellement par pertinence (HAL et CrossRef en mode bibliographie, OpenAlex ; Isidore avec un poids moindre, son ordre n'étant pas vérifié ; Semantic Scholar trié par citations n'y pèse pas) ;
- `local` : correspondance des concepts de la requête dans le titre, les mots-clés et le résumé, combinée au classement natif ; **désactivé par défaut** tant que le protocole d'évaluation ([docs/EVALUATION.md](docs/EVALUATION.md)) n'a pas conclu ;
- `off` : aucun classement.

Rangs, scores et justification (concepts appariés, réserves) sont écrits dans le **manifeste**, jamais dans les notices. Version des règles : `opt13-2`.

---

## Fichiers produits

Chaque production suit la même convention de nommage, partagée par `corpus_list`, `list_resources`, `read_resource` et `/download` :

| Fichier | Rôle | Exposition |
|---------|------|------------|
| `<radical>.json` | Corpus Zotero interne | listé, lisible, téléchargeable |
| `<radical>.csl.json` | Corpus CSL-JSON | listé, lisible, téléchargeable |
| `<radical>_stats.txt` | Statistiques | lisible, téléchargeable |
| `<radical>.manifest.json` | Manifeste (format `moisson-shs/manifest`) | téléchargeable seulement |
| `<radical>.doi_report.json` | Rapport de `corpus_resolve_dois` (format `moisson-shs/doi-report`) | téléchargeable seulement |
| `<radical>.diff.json` | Rapport de `corpus_diff` (format `moisson-shs/diff`) | téléchargeable seulement |
| `<nom>.bib`, `<nom>.ris` | Exports de `corpus_export` | téléchargeable seulement |
| `.index.json`, `.moisson.lock`, `.<nom>.<uuid>.tmp`, `.<radical>.publishing`, `.native_audit/`, `.audit/`, `.oauth/` | Fichiers de service | jamais |

- **Noms uniques ou dérivés** : les producteurs qui fabriquent un nom y ajoutent un uuid court : moissons CLI et MCP (`corpus_<requête>_<AAAAMMJJ_HHMMSS>_<uuid8>`), `search_by_author` (`corpus_author_<nom>_…`), `corpus_merge` sans `output_path` (`corpus_merged_…`), `corpus_export` sans `output_path` (`<radical>_<uuid8>.bib` ou `.ris`), sélections HTTP de `corpus_query` (`<radical>_sel_<uuid>`), rapport de `corpus_diff` quand `<radical de B>.diff.json` est déjà pris. Deux de ces productions ne se recouvrent jamais. En revanche, `corpus_filter`, `corpus_resolve_dois` et `corpus_to_csl` gardent un nom **dérivé** du corpus source (`<radical><output_suffix>.json`, suffixes `_filtered` et `_doi` par défaut ; `<radical>.csl.json`) : un nouvel appel de même suffixe **remplace** le fichier précédent (atomiquement, manifeste compris), y compris entre deux appels HTTP concurrents (même URL, contenu du dernier). Pour garder deux filtrages ou deux résolutions, passer un `output_suffix` distinct. Un `output_path` explicite désigne le fichier écrit, qu'il existe déjà ou non.
- **Publication atomique** : les fichiers sont écrits dans des temporaires du même dossier, synchronisés, renommés, puis le **manifeste est publié en dernier** comme marqueur de complétude. Un corpus interrompu avant son manifeste est `incomplete`, jamais présenté comme complet. Statuts : `complete`, `incomplete`, `modified` (taille différente du manifeste), `legacy` (corpus antérieur au manifeste, métadonnées calculées à la demande).
- **Manifeste** : identifiant d'exécution, type de production (`moisson`, `filtre`, `fusion`, `conversion_csl`, `auteur`, `resolution_doi`, `export`), fichiers avec tailles et empreintes SHA-256, requête et plan, filtres, réglages, statut de chaque source, rapport de déduplication, échéance souple, versions des règles, et une entrée par notice (`records` : identifiant, rangs et scores natifs, rang et justification du classement). Aucun chemin absolu.
- **Index dérivé** `OUTPUT_DIR/.index.json` : reconstruit paresseusement à partir des manifestes, il sert `corpus_list` et `list_resources` sans relire les corpus (209 corpus réels listés en 7,8 ms à chaud).
- **`raw/`** garde les notices **normalisées** de chaque source. Les réponses natives des API ne sont capturées qu'en **mode audit** (`MOISSON_AUDIT_NATIVE=1`) dans `.native_audit/<run_id>.jsonl`, expurgées (clés, jetons, adresse de contact) et bornées ; l'enrichissement OAI-PMH n'y passe pas (signalé dans le manifeste).

---

## Identité des notices et déduplication

- **Clés fortes** (`utils/identity.py`) : DOI du champ **ou** du champ `extra`, identifiants natifs (HAL, OAI, Isidore, OpenAlex, Semantic Scholar, CORE, Google Scholar, PMID, arXiv), URL canonique, ISBN d'un livre. Deux notices qui partagent une clé forte sont réunies (union-find), quel que soit le type de la clé.
- **Garde-fou DOI** : deux DOI différents ne sont jamais fusionnés, même par transitivité ; une notice sans DOI reliée à deux DOI reste à part (statut ambigu).
- **Règles optionnelles** (`utils/deduplication.py`) : Trois règles optionnelles, **désactivées par défaut** jusqu'à la vérification humaine du sprint patch, s'y ajoutent : **co-diffusion** d'un même article par deux plateformes (Cairn, OpenEdition, Persée, Érudit) ou par une plateforme et l'éditeur (`MOISSON_DEDUP_CODIFFUSION=1` : titre identique, même année, même premier auteur, même revue, mêmes pages ; DOI Cairn conservé, l'autre gardé en `DOI alias` dans `extra`) ; **versions Zenodo** (`MOISSON_DEDUP_ZENODO_VERSIONS=1` : DOI de concept et de version d'un même dépôt) ; **chapitres Google Books** (`MOISSON_DEDUP_SCHOLAR_CHAPTERS=1` : entre notices Google Scholar, numéro de chapitre en tête d'un titre Google Books ignoré, type « book » du lien non bloquant). Chaque fusion ainsi faite est tracée dans `Dedup evidence`.
- **Passe floue conservatrice** : similarité de titre ≥ **0,93**, année à un an près **et** premier auteur corroborés (ordre nom/prénom inversé toléré), titres génériques exclus, types incompatibles jamais réunis (compte rendu et article, prépublication et article, chapitre et livre). `MOISSON_DEDUP_FUZZY=0` revient au seul régime d'égalité exacte.
- **Fusion indépendante de l'ordre** : chaque champ est choisi par une règle stable, la liste d'auteurs la plus complète est gardée, et l'origine de chaque valeur est tracée dans une ligne `Dedup evidence` du champ `extra`. Une permutation des entrées produit une sortie identique ; l'ordre de sortie est auteur-date.
- **Mesure** (209 corpus réels, 15 976 notices) : doublons résiduels à titre et année identiques de **4,0 % à 0,44 %** ; 0 fusion erronée sur les 66 paires négatives du jeu témoin (`tests/fixtures/dedup_pairs.json`), 52 paires positives sur 54 réunies.

---

## Formats

### CSL-JSON (import Zotero Desktop)

Chaque notice du fichier `corpus_*.csl.json` suit la spécification [Citation Style Language](https://docs.citationstyles.org/). Exemple de forme (valeurs fictives) :

```json
{
  "type": "article-journal",
  "id": "10.1234/exemple.2020.45",
  "title": "Titre de l'article",
  "author": [
    {"family": "Nom", "given": "Prénom"},
    {"family": "Autrenom", "given": "Autreprénom"}
  ],
  "issued": {"date-parts": [[2020]]},
  "container-title": "Nom de la revue",
  "volume": "12",
  "issue": "2",
  "page": "45-67",
  "DOI": "10.1234/exemple.2020.45",
  "URL": "https://hal.science/hal-00000000",
  "abstract": "Résumé de l'article…",
  "language": "fr",
  "keyword": "mot-clé 1, mot-clé 2",
  "note": "Sources: HAL, Isidore\nHAL ID: hal-00000000"
}
```

Le champ `note` porte la provenance auditable (sources d'origine, identifiants natifs, `DOI source`, `Dedup evidence`) au format clé : valeur ; Zotero la range dans `extra`. Aucune métadonnée de moisson (rang, score, statut) n'entre dans les notices : elle va au manifeste.

### Zotero JSON interne, BibTeX, BibLaTeX, RIS

- **Zotero JSON interne** : format de l'API Zotero, validé contre le schéma officiel (`https://api.zotero.org/schema`, cache 30 jours, repli embarqué) ; lu par tous les outils.
- **BibTeX / BibLaTeX / RIS** : `corpus_export` ou `moisson-shs export`. Validés sur 15 976 notices réelles par trois analyseurs de référence (bibtexparser, pybtex, rispy), sans erreur.

---

## Configuration

Toutes les variables d'environnement utiles sont listées et commentées dans [`.env.example`](.env.example), avec leur valeur par défaut. Les plus courantes :

| Variable | Défaut | Rôle |
|----------|--------|------|
| `OPENALEX_API_KEY`, `CORE_API_KEY` | — | Clés requises par OpenAlex et CORE |
| `SEMANTIC_SCHOLAR_API_KEY` | — | Clé optionnelle de Semantic Scholar |
| `SERPAPI_API_KEY` | — | Clé SerpAPI (Google Scholar, facturée) |
| `MOISSON_OUTPUT_DIR` | `output/` à côté de `mcp_server.py` | Répertoire des corpus du serveur MCP |
| `MOISSON_CACHE`, `MOISSON_CACHE_DIR`, `MOISSON_CACHE_TTL` | inactif, `~/.cache/moisson_shs/http`, 86400 | Cache HTTP de la CLI (le serveur MCP ajoute le suffixe `_mcp`) |
| `MOISSON_MCP_MAX_PER_SOURCE`, `MOISSON_MCP_WORKERS`, `MOISSON_MCP_CACHE_TTL` | 200, 4, 21600 | Défauts de `moisson_search` |
| `MOISSON_STDIO_SOFT_DEADLINE` | 50 | Échéance souple de `moisson_search` en stdio (secondes) |
| `MOISSON_INLINE_DEFAULT`, `MOISSON_DIGEST_TOP`, `MOISSON_DIGEST_MAX_BYTES` | digest, 40, 15000 | Retour des outils producteurs |
| `MOISSON_RANKING` | native | Classement par défaut |
| `MOISSON_DEDUP_FUZZY` | actif | `0` désactive la passe floue |
| `MOISSON_REQUEST_DELAY` | 0.3 | Intervalle minimal entre deux requêtes vers un même hôte |
| `MOISSON_NO_DOTENV` | — | `1` : ne lire aucun `.env` |

Le fichier `.env` de la racine est lu par `utils/secrets.py` (chemin résolu depuis le module, pas depuis le répertoire courant) sans écraser l'environnement ; le bloc `env` du client MCP reste prioritaire. Ses clés d'API sont une réserve, jamais injectée dans l'environnement : elle sert quand la clé du client n'est pas déclarée, ou, pour les sources de `MOISSON_KEY_FALLBACK` (défaut `openalex,semantic_scholar,core`, jamais SerpAPI), quand elle est vide ou refusée (HTTP 401 ou 403) ; détail dans [MCP_INSTALL.md](MCP_INSTALL.md). Ce chargement a lieu après la lecture de `config.py` : il est garanti pour les clés d'API, mais les réglages `MOISSON_*` doivent être posés dans l'environnement réel du processus (shell, bloc `env`, ou `env_file` de Docker Compose, qui injecte tout le fichier). `config.py` centralise les endpoints et les constantes : toutes ses constantes de réglage sont surchargeables par variable d'environnement.

---

## Mesures

Chiffres du chantier d'optimisation (2026-09-28 et 2026-09-29), avec leurs conditions. Les rapports historiques de `.claude/dev/` gardent les chiffres de leur époque.

**Banc simulé** (`scripts/benchmark_moisson.py`, réponses rejouées sans réseau, attentes réelles `--sleep-scale 1`, latence simulée nulle ; médiane de 3 exécutions pour la référence, une exécution pour les autres scénarios ; moisson de référence : « discours numérique », groupe francophone, 200 notices par source, depuis 2018, défauts MCP ; copie d'avant chantier et état du lot C mesurés le 2026-09-29) :

| Scénario | Avant le chantier | Après | Remarque |
|----------|-------------------|-------|----------|
| Référence : requêtes | 583 (dont 581 pages Isidore) | 3 (1 requête Isidore) | flux Isidore partagé, fenêtre en une requête |
| Référence : durée | 217,7 s | 0,91 s | 309,4 s → 1,09 s avec 150 ms de latence simulée |
| Hôte injoignable (HAL) | 138,8 s, 305 requêtes | 27,2 s, 5 requêtes | une requête Isidore au lieu de 301 pages ; HAL nommé en échec |
| Erreur 404 | 7 requêtes, 6,7 s | 3 requêtes, 0,01 s | aucune reprise sur un 4xx définitif |
| Schéma Zotero indisponible | 200 tentatives | 1 | échec mémorisé 600 s |
| Cache chaud | 6,6 s | 0,35 s | aucune attente de politesse sur une réponse du cache |
| Retour MCP d'une moisson | corpus inliné jusqu'à 300 notices (114,6 Ko pour les 69 notices du scénario « rare » ; ≈ 391 Ko pour 300 notices réelles) | digest de 40 notices, 15 000 octets au plus (14 917 pour la référence) | notices complètes à la demande par `corpus_query` |

**Réseau réel** (client stdio réel, API gratuites, jamais SerpAPI) :

| Mesure | Résultat |
|--------|----------|
| Moisson de référence avant le chantier (2026-09-28) | interrompue par le banc après 381 s et 133 requêtes ; moisson complète projetée à environ 26 min (≈ 740 pages Isidore) |
| Moisson de référence, défauts MCP, sans clé (2026-09-29) | **3,2 s**, 2 requêtes (HAL 1, Isidore 1), 405 notices → 302 après déduplication |
| Même moisson, cache chaud (2026-09-28) | 0,2 s, aucune requête |
| Groupe `all`, sans clé / avec la clé OpenAlex (2026-09-28) | 4,3 s / 4,8 s |
| `search_by_author`, nom seul « Dominique Cardon » (2026-09-29) | 5,2 s ; homonymie détectée : 0 notice retenue, 9 profils OpenAlex et 8 Semantic Scholar proposés |
| `search_by_author`, ORCID seul (2026-09-29) | 2,3 s ; 240 travaux OpenAlex en 2 pages |
| HAL, mode bibliographie (2026-09-29) | 198 des 200 notices retenues figurent dans le top 200 par score d'une sonde indépendante (l'ancien ordre `docid asc` n'en partageait que 19) |

Autres mesures : `fetch_by_doi` 2,8 s → 1,1 s (banc, latences simulées de 0,25 à 1 s) ; `corpus_resolve_dois` limité par la politesse CrossRef à 0,34 s par requête (environ 34 s pour 100 notices sans DOI) ; export de 610 notices en 122 ms (BibTeX) et 46 ms (RIS).

---

## Architecture

```
moisson_shs/
├── cli.py                    # Point d'entrée : setup, serve, search, export, token, version
├── main.py                   # Adaptateur CLI de la moisson
├── mcp_server.py             # Serveur MCP (stdio et Streamable HTTP) : outils, prompts, ressources
├── config.py                 # Endpoints et constantes (surchargeables par variables d'environnement)
├── auth.py, remote_auth.py   # Jetons Bearer du serveur distant
├── oauth_provider.py         # OAuth 2.1 (connecteur web)
├── setup_claude_desktop.py   # Installation dans Claude Desktop
│
├── engine/                   # Moteur commun CLI et MCP
│   ├── models.py             # SearchRequest, RunContext, SourceResult, HarvestResult, statuts
│   ├── orchestrator.py       # harvest : pool unique, échéance, annulation
│   └── outputs.py            # Publication des corpus
│
├── connectors/               # Accès aux douze sources
│   ├── registry.py           # Registre, groupes, source facturée
│   ├── capabilities.py       # Capacités de requête vérifiées par source
│   ├── isidore_scan.py       # Flux Isidore partagé par la source et ses sous-sources
│   ├── hal.py, isidore.py, openedition.py, cairn.py, persee.py, theses_fr.py, erudit.py
│   └── crossref.py, openalex.py, semantic_scholar.py, core.py, google_scholar.py
│
├── mappers/                  # Conversion vers Zotero, CSL, BibTeX, RIS
│   ├── zotero_schema_loader.py, csl_exporter.py, bibtex_exporter.py, ris_exporter.py
│   └── hal_mapper.py, isidore_mapper.py, oai_mapper.py, …_mapper.py
│
├── utils/
│   ├── http.py               # Politique HTTP commune : reprises, politesse par hôte et par compte, cache
│   ├── identity.py, deduplication.py   # Identité des notices, fusion
│   ├── query_plan.py, ranking.py       # Plan de requête, classement
│   ├── lookups.py, doi_resolver.py     # fetch_by_doi, search_by_author, résolution de DOI
│   ├── manifest.py, output_files.py    # Manifeste, publication atomique, index, nommage
│   ├── corpus_query.py, corpus_diff.py, corpus_ops.py
│   └── secrets.py, sandbox.py, source_labels.py, cleaner.py, names.py
│
├── scripts/                  # benchmark_moisson.py + bench/, eval/, measure_duplicates.py
├── tests/                    # unit/, integration/, fixtures/
└── output/                   # Corpus produits (ignorés par git)
```

---

## Tests, banc et évaluation

```bash
pip install -e ".[dev,mcp]" -c constraints.txt
pytest tests/ -m "not integration"                         # sans réseau externe (comme la CI)
pytest tests/ -m "not integration and not sockets"         # ni réseau ni socket locale
pytest tests/ -m sockets                                   # transport HTTP local (uvicorn de test)
MOISSON_TEST_REAL_KEYS=1 pytest tests/integration -m integration   # API réelles, clés du shell
```

Au 2026-09-29, la suite compte plus de 3 400 tests hors intégration, tous exécutés sans clé ni `.env` (`tests/conftest.py`). La CI exige 70 % de couverture sur `mappers/`, `utils/` et `connectors/`.

- **Banc de mesure** : `scripts/benchmark_moisson.py` rejoue quinze scénarios sans réseau (référence, requête rare, dates restrictives, source lente, 429, source indisponible, hôte injoignable, grand corpus, métadonnées contradictoires, 404, réponse invalide, cache chaud, schéma indisponible…) et compte requêtes par hôte, pages, reprises, notices, durées et taille du retour MCP. `--live` mesure en réseau réel (API gratuites seulement), `--compare` compare deux séries. Référence d'avant chantier à comparer : `tests/fixtures/bench/baseline_2026-09-29.summary.json` (copie d'avant chantier remesurée avec le simulateur actuel) ; `baseline_2026-09-28.summary.json` garde la première mesure, faite avec les définitions de scénarios de l'époque.
- **Évaluation du classement** : protocole SHS outillé (`scripts/eval/`), 24 requêtes, jugement en aveugle ; il décide l'activation du classement local. Voir [docs/EVALUATION.md](docs/EVALUATION.md).

---

## Dépannage rapide

Voir la [FAQ](docs/FAQ.md).

| Symptôme | Solution rapide |
|----------|-----------------|
| `AttributeError: 'Server' object has no attribute 'list_tools'` | SDK MCP 2.x installé : `pip install -e ".[mcp]" -c constraints.txt` |
| `venv/bin/moisson-shs` refuse de démarrer après un déplacement du projet | Réinstaller le paquet éditable (même commande) |
| Erreur 429 persistante | Relever `MOISSON_REQUEST_DELAY` (ou l'intervalle propre à la source) dans l'environnement |
| Sous-sources d'Isidore « limitées par le fournisseur » | Normal : fenêtre de 200 résultats d'Isidore (voir [Sources](#sources)) |
| Le serveur MCP n'apparaît pas dans Claude Desktop | Quitter et relancer entièrement l'application ; lire `~/Library/Logs/Claude/mcp*.log` |
| Zotero refuse le `.json` | Importer le **`.csl.json`**, ou un export BibTeX / RIS |

---

## Limites connues

- **Isidore** : 200 résultats servis par requête ; ses sous-sources en héritent (Persée souvent vide sur une requête large) ; son ordre par défaut n'est pas documenté (poids réduit dans le classement).
- **Semantic Scholar** : en mode bibliographie, trié par citations, pas par pertinence (biais vers les travaux anciens et très cités) ; le point d'accès classé par pertinence renvoie des 429 sans clé.
- **HAL** : les scores varient légèrement d'un réplica à l'autre ; « les meilleurs scores » s'entend à la frontière près.
- **Google Scholar** : métadonnées tronquées, aucun DOI ni résumé ; facturé.
- **Profils d'auteur** : les profils des fournisseurs (OpenAlex, Semantic Scholar, idHal) mêlent parfois des homonymes ; aucun contrôle automatique ne peut le détecter.
- **Déduplication** : quelques vraies paires restent séparées (titres trop différents, auteurs saisis de façon incompatible) ; prépublication et article final ne sont jamais assimilés d'office.
- **Classement local** : désactivé tant que le jugement de pertinence n'a pas eu lieu.
- **Cache de la CLI** (`http.sqlite`) : aucun entretien automatique (celui du serveur MCP est entretenu, 512 Mo au plus).
- **Annulation** : une requête peut partir quelques millisecondes après le signal ; le mappage d'une grande page n'est pas interruptible.
- **Audit natif** : les réponses OAI-PMH (enrichissement) ne sont pas capturées.
- **Cairn, OpenEdition** : métadonnées seulement ; le texte intégral relève de conventions avec les éditeurs.

---

## Licence

MIT — voir [LICENSE](LICENSE).

## Références techniques

- API HAL : https://api.archives-ouvertes.fr/docs
- API Isidore : https://isidore.science/api
- OAI-PMH OpenEdition : https://oai-openedition.readthedocs.io/
- OAI-PMH Cairn : https://oai.cairn.info/
- OAI-PMH Persée : https://www.persee.fr/openaire
- OAI-PMH Érudit : https://www.erudit.org/fr/aide/oai-pmh
- CrossRef : https://api.crossref.org · OpenAlex : https://docs.openalex.org · Semantic Scholar : https://api.semanticscholar.org · CORE : https://core.ac.uk/services/api · SerpAPI : https://serpapi.com/google-scholar-api
- Schéma Zotero : https://api.zotero.org/schema
- CSL-JSON : https://docs.citationstyles.org/
- Protocole MCP : https://modelcontextprotocol.io
