# Contexte projet — moisson_shs

Moissonneur Python multi-sources d'articles SHS (douze sources : sept francophones, quatre internationales, Google Scholar à nommer) avec sortie au format **Zotero JSON natif** et **CSL-JSON**, exports BibTeX, BibLaTeX et RIS. Exposé comme **serveur MCP** local pour Claude Desktop (stdio) et comme serveur distant (Streamable HTTP).

## Stack

- **Python ≥ 3.10** (CI : 3.10, 3.11, 3.12 ; venv local : 3.14)
- Dépendances : `requests`, `requests-cache`, `sickle` (OAI-PMH), `lxml`, `unidecode`
- SDK MCP (extra `[mcp]`) : **`mcp>=1.27,<2`** — la série 2 retire les décorateurs du `Server` bas niveau ; versions exactes testées dans `constraints.txt` (`pip install -e ".[dev,mcp]" -c constraints.txt`)
- venv local dans `venv/` ; toujours appeler `venv/bin/python` (ou `venv/bin/moisson-shs`)
- Version : `_version.py`, seule source (0.4.0) ; commit, état de l'arbre et empreinte du code dans chaque manifeste (`utils/build_info.py`)

## Documentation principale

- [README.md](README.md) — vue d'ensemble, installation, usage CLI et MCP, mesures
- [PLAN.md](PLAN.md) — plan de développement initial avec stratégies par source
- [MCP_INSTALL.md](MCP_INSTALL.md) — installation du serveur MCP dans Claude Desktop
- [REMOTE.md](REMOTE.md) · [DEPLOY.md](DEPLOY.md) — serveur distant, déploiement, note de redéploiement (§ 0)
- [docs/FAQ.md](docs/FAQ.md) · [docs/API_SPECS.md](docs/API_SPECS.md) (capacités de requête vérifiées par source) · [docs/EVALUATION.md](docs/EVALUATION.md) (protocole d'évaluation du classement)
- [.claude/dev/spritn_optimise.md](.claude/dev/spritn_optimise.md) · [.claude/dev/sprint-optimise_report.md](.claude/dev/sprint-optimise_report.md) — plan et rapport du chantier d'optimisation

## Plan de développement en 4 sprints

Le travail de finalisation est organisé en 4 sprints. Chaque fichier contient : objectif, tâches incluses, ordre d'exécution, critères de sortie (DoD), détails techniques, risques, livrables et estimation.

| Sprint | Fichier | Objectif | Statut |
|--------|---------|----------|--------|
| **01** | [.claude/dev/sprint_01.md](.claude/dev/sprint_01.md) | Déblocage et validation fonctionnelle | ✅ **TERMINÉ + import Zotero validé utilisateur** |
| **02** | [.claude/dev/sprint_02.md](.claude/dev/sprint_02.md) | Robustesse, performance et qualité du code | ✅ **TERMINÉ** |
| **03** | [.claude/dev/sprint_03.md](.claude/dev/sprint_03.md) | Extension : nouvelles sources et outils MCP avancés | ✅ **TERMINÉ** |
| **04** | [.claude/dev/sprint_04.md](.claude/dev/sprint_04.md) | Distribution, CI/CD et documentation utilisateur | ✅ **TERMINÉ** |
| **05** | [.claude/dev/sprint_feedback.md](.claude/dev/sprint_feedback.md) | Retour MCP enrichi : lien vers fichier + JSON inline au client MCP | ✅ **TERMINÉ** |

### Chantier « Serveur MCP distant » (double transport stdio + HTTP) — ✅ TERMINÉ (2026-06-18)

Plan directeur : [.claude/dev/sprint_remote_plan.md](.claude/dev/sprint_remote_plan.md) — `moisson-shs serve` reste stdio par défaut et gagne un **mode serveur distant HTTP** (`serve --http`) : Streamable HTTP, auth **Bearer**, **Docker + Caddy (TLS)**, retour par **URLs de téléchargement**, opérations longues robustes, observabilité. Le **même** `Server` (9 outils à l'époque, 12 depuis le chantier d'optimisation) alimente les deux transports. Guide : [REMOTE.md](REMOTE.md) · Déploiement : [DEPLOY.md](DEPLOY.md).

| Sprint | Fichier | Objectif | Statut | Rapport |
|--------|---------|----------|--------|---------|
| **06** | [.claude/dev/sprint_06.md](.claude/dev/sprint_06.md) | Socle transport HTTP (Streamable HTTP) + double `serve` | ✅ TERMINÉ | [report](.claude/dev/sprint_06_report.md) |
| **07** | [.claude/dev/sprint_07.md](.claude/dev/sprint_07.md) | Auth Bearer + sandbox des chemins + durcissement | ✅ TERMINÉ | [report](.claude/dev/sprint_07_report.md) |
| **08** | [.claude/dev/sprint_08.md](.claude/dev/sprint_08.md) | Retour distant : URLs de téléchargement + `/download` | ✅ TERMINÉ | [report](.claude/dev/sprint_08_report.md) |
| **09** | [.claude/dev/sprint_09.md](.claude/dev/sprint_09.md) | Opérations longues : progression, deadline, concurrence, anti-DoS | ✅ TERMINÉ | [report](.claude/dev/sprint_09_report.md) |
| **10** | [.claude/dev/sprint_10.md](.claude/dev/sprint_10.md) | Déploiement Docker + reverse proxy TLS (Caddy) | ✅ TERMINÉ | [report](.claude/dev/sprint_10_report.md) |
| **11** | [.claude/dev/sprint_11.md](.claude/dev/sprint_11.md) | Onboarding client distant, CLI jetons, observabilité, `REMOTE.md` | ✅ TERMINÉ | [report](.claude/dev/sprint_11_report.md) |
| **12** | — | OAuth 2.1 en parallèle du Bearer (connecteur web) | ✅ TERMINÉ | [report](.claude/dev/sprint_12_report.md) |

**Serveur distant — capacités livrées :** `serve --http` (Streamable HTTP `/mcp` + `/download` + `/healthz` public + `/metrics` auth, uvicorn 1 worker derrière Caddy TLS) ; **auth Bearer** (refus sans jeton/allowlist Host) ; **sandbox** HTTP-only (anti-traversal, stdio inchangé) ; **retour distant** par URLs HTTPS (jamais de chemin disque ni JSON inline) ; **opérations longues** (progression SSE, deadline+annulation, sémaphore, plafond anti-DoS, uuid) ; **CLI** `token {generate,rotate,status,revoke}` + `setup --remote` ; **observabilité** (logs JSON+redaction, audit JSONL, `/metrics`). Image Docker non-root **build+run validés**. Revue adversariale croisée (10 relecteurs) : **4 findings corrigés** (dont fuite de chemins `corpus_list` en HTTP). **+93 tests** → **517 PASS** hors intégration, **0 régression stdio**. Modules ajoutés : `auth.py`, `remote_auth.py`, `utils/sandbox.py` ; `Dockerfile`/`docker-compose.yml`/`deploy/Caddyfile`/`DEPLOY.md`/`REMOTE.md`/`scripts/smoke_remote.sh`.

### Chantier optimisation (sprint-optimise) — ✅ TERMINÉ (2026-09-29)

Plan : [.claude/dev/spritn_optimise.md](.claude/dev/spritn_optimise.md) (audit fusionné, tâches OPT-00 à OPT-21) · Rapport : [.claude/dev/sprint-optimise_report.md](.claude/dev/sprint-optimise_report.md) (baseline, décisions, détail par tâche, `config.py`, limites, actions requises). Chantier mené du 2026-09-28 au 2026-09-29 : socle réinstallable, contrat MCP fermé, identité des notices, collecte sans redondance, pertinence gouvernée, retour borné, consultation et exports. Aucun commit : tout est à relire.

| Lot | Tâches | Objet | Statut |
|-----|--------|-------|--------|
| **0** Socle et sécurité | OPT-00 à OPT-04 | Banc simulé et baseline ; pin `mcp>=1.27,<2` + `constraints.txt` ; `read_resource` confiné (C03), `list_resources` sans chemin, plafond en octets ; politique HTTP unifiée (4xx définitif = 1 tentative, 429/`Retry-After`, politesse par hôte et par compte, cache à trois états) ; mémoire d'échec du schéma Zotero | ✅ |
| **A** Identité | OPT-06, OPT-06 bis | `utils/identity.py`, déduplication par union-find sur clés fortes, passe floue 0,93 + année + auteur, fusion déterministe avec provenance (`Dedup evidence`), portails CCSD, noms inversés | ✅ |
| **B** Collecte | OPT-07, OPT-08, OPT-09, OPT-09 bis, OPT-09 ter, OPT-10 | Moteur `engine/` (statuts, `isError`, annulation) ; flux Isidore partagé (fenêtre de 200, une requête) ; pool unique et défauts MCP ; échéance souple stdio 50 s ; pages conservées à l'échéance ; DOI parallèles et vérifiés (`doi-match/3`, rapport `.doi_report.json`) | ✅ (OPT-10 partiel : plancher CrossRef) |
| **C** Pertinence | OPT-11 à OPT-15 | Registre de capacités vérifié et plan de requête (`plan_only`, variantes FR/EN, langues) ; HAL au score, CrossRef `sort=score`, S2 par citations ; classement `native`/`local` explicable (`opt13-2`) ; recherche d'auteur par identifiants, homonymes rendus ; protocole d'évaluation outillé | ✅ (gate OPT-15 en attente des jugements) |
| **D** Consultation, distribution | OPT-16 à OPT-21, OPT-21a | Manifeste par corpus, publication atomique, index `.index.json` ; digest ≤ 15 Ko et `corpus_query` ; `corpus_export` (BibTeX, BibLaTeX, RIS) ; `corpus_diff` et veille ; prompts et descriptions ; documentation, paquet testé hors dépôt | ✅ (OPT-18 : import Zotero manuel à faire) |

**Capacités livrées :** installation depuis zéro (roue construite hors du dépôt, venv neuf, `serve` stdio et HTTP vérifiés) ; statuts par source `complete`/`empty`/`partial`/`skipped`/`error`/`cancelled` avec motifs, vraie erreur MCP (`isError`) si tout échoue, code 3 en CLI ; retour stdio en **digest** de 40 notices ≤ 15 000 octets (`inline="full"` restitue le sprint 05, `inline="none"` liens seuls) ; notices complètes à la demande par `corpus_query(ids=…)` ; ressources paginées `corpus://<nom>?offset=&limit=` ; modes `bibliographie` (MCP) et `collecte` (CLI) ; classement local désactivé par défaut (`MOISSON_RANKING=native`) ; manifeste `<radical>.manifest.json` (rangs et scores par notice, **jamais dans les notices**) ; audit des réponses natives en option ; en HTTP, contrat inchangé (liens de téléchargement seulement).

**Chiffres clés** (banc simulé contre la copie d'avant chantier remesurée le 2026-09-29, sauf mention) :

| Mesure | Avant | Après |
|--------|-------|-------|
| Moisson de référence (« discours numérique », francophone, 200/source, depuis 2018) : requêtes | 583 (581 pages Isidore) | 3 (1 requête Isidore) |
| Même moisson : durée, latence nulle / 150 ms | 210,6 s / 305,1 s | 0,95 s (÷221) / 1,13 s (÷270) |
| Même moisson en réseau réel, défauts MCP | interrompue à 381 s (≈ 26 min projetées) | 3,2 s |
| Doublons résiduels, 209 corpus réels | 4,0 % | 0,44 % |
| Fausses fusions (66 paires négatives) / DOI erronés (33 cas négatifs) | 9 / 19 | 0 / 0 |
| Retour MCP d'une moisson (stdio) | corpus inline jusqu'à 300 notices (≈ 391 Ko) | ≤ 15 000 octets (max. 14 998) |
| `fetch_by_doi` | 2,836 s | 1,065 s (×2,66) |
| `resolve_dois`, 100 notices | 31,0 s | 34,0 s (plancher de politesse CrossRef, 3 req/s annoncées) |
| Listing de 400 corpus (chaud / froid) | — | 10,2 ms / 43,5 ms |
| Tests hors intégration | 607 verts, 1 échec | **3 681 verts** (3 656 sans réseau ni socket, 25 `sockets`, 13 `integration`) ; couverture 91,68 % |

**Outils MCP (12)** : les neuf historiques (`moisson_search`, `corpus_analyze`, `corpus_list`, `corpus_filter`, `corpus_to_csl`, `fetch_by_doi`, `search_by_author`, `corpus_merge`, `corpus_resolve_dois`) + **`corpus_export`**, **`corpus_query`**, **`corpus_diff`** ; 4 prompts ; gabarit de ressource paginée. Arguments ajoutés (aucun retiré) : `moisson_search` (`mode`, `query_variants`, `languages`, `plan_only`, `ranking`, `deadline_seconds`), `search_by_author` (`openalex_author_id`, `s2_author_id`, `hal_person_id`), `corpus_resolve_dois` (`max_seconds`, `strict_title_threshold`), `corpus_list` (`limit`, `pattern`, `since`, `rebuild`), `inline` et `top` sur les sept producteurs. CLI : `search --mode --lang --query-en --variant --plan-only --ranking --cache/--no-cache`, sous-commande `export`.

**`config.py` (règle projet) :** 43 constantes ajoutées, 2 modifiées (`HAL_ROWS_PER_BATCH` désormais surchargeable ; `ISIDORE_REPLIES_PER_PAGE` **100 → 200**), 0 retirée ; toutes surchargeables par variable d'environnement sauf les bornes 10–900 s de `deadline_seconds`. `REQUEST_DELAY` garde sa valeur mais devient un intervalle minimal entre deux départs vers un même hôte. Liste exhaustive : rapport § 9. Principales variables nouvelles : `MOISSON_MCP_MAX_PER_SOURCE` (200), `MOISSON_MCP_WORKERS` (4), `MOISSON_MCP_CACHE_TTL` (6 h), `MOISSON_STDIO_SOFT_DEADLINE` (50), `MOISSON_RANKING` (`native`), `MOISSON_ISIDORE_REPLIES_PER_PAGE` (200), `MOISSON_HTTP_REQUEST_BUDGET` (120), `MOISSON_REQUEST_DELAY_CROSSREF_LIST` (0,34), `MOISSON_AUDIT_NATIVE` (0). Hors `config.py` : `MOISSON_INLINE_MAX_BYTES`, `MOISSON_DIGEST_*`, `MOISSON_QUERY_*` (dont `MOISSON_QUERY_HTTP_KEEP`), `MOISSON_DEDUP_FUZZY`, `MOISSON_SCHEMA_RETRY_AFTER`, `MOISSON_ZOTERO_SCHEMA_FILE`, `MOISSON_AUTHOR_*`, `MOISSON_NO_DOTENV`, `MOISSON_TEST_REAL_KEYS`. `.env.example` recense les 108 variables `MOISSON_*` lues par le code (défauts vérifiés par `tests/unit/test_opt21_docs.py`). Aucun seuil d'identité ni de DOI abaissé.

**Fichiers produits par corpus :** `<radical>.json`, `<radical>.csl.json`, `<radical>_stats.txt`, plus `<radical>.manifest.json` (publié en dernier) et, selon l'outil, `.doi_report.json`, `.diff.json`, exports `.bib`/`.ris` ; index `OUTPUT_DIR/.index.json` ; fichiers de service (`.oauth/`, `.audit/`, `.native_audit/`, temporaires, verrou) jamais listés, lus ni téléchargés.

**En attente de l'utilisateur** (détail et commandes : rapport § 10) : jugements d'OPT-15 (`output/eval/2026-09-29/fiches/`, environ une journée) puis reconstruction des ensembles et décision ; redéploiement du serveur distant puis rotation des jetons Bearer/OAuth et des clés ; archivage de la copie de référence d'avant chantier (répertoire temporaire) ; reconstruction de l'image Docker ; import manuel des exports dans Zotero ; décisions ouvertes (ordre de chargement du `.env`, sélections HTTP de `corpus_query`, critère « 100 notices < 15 s »).

### Sprint patch (retour d'expérience du 30 septembre) — ✅ TERMINÉ (2026-09-30)

Plan : [.claude/dev/sprint-patch.md](.claude/dev/sprint-patch.md) (PAT-01 à PAT-22, décisions DP-1 à DP-10) · Rapport : [.claude/dev/sprint-patch_report.md](.claude/dev/sprint-patch_report.md). Traite le retour « moisson-shs 0.3.0 : bugs et limitations constatés » (état évalué `e61c2b9`, tag local `v0.3.0`). Branche `sprint-patch`, un commit par lot (`95f7675` à `9e47931`), version **0.4.0**, tag local `v0.4.0` ; ni fusion dans `main` ni push.

| Lot | Tâches | Objet | Statut |
|-----|--------|-------|--------|
| **0** Configuration | PAT-01, PAT-02 | Clés Semantic Scholar et CORE | à faire (utilisateur) |
| **1** Traçabilité | PAT-03 | `_version.py` seule source de version ; `utils/build_info.py` : commit, état de l'arbre, empreinte du code dans manifestes, `_stats.txt`, `/healthz`, `moisson-shs version` ; `serverInfo.version` = version du paquet | ✅ |
| **2** Correctifs, règles | PAT-04 à PAT-08, PAT-20 | Classifications automatiques (OpenAlex, S2, CrossRef) hors des mots-clés, tracées dans `extra` (B1) ; erreurs de source et 401/403 sans clé (B5) ; règles `dedup/2` : co-diffusion, versions Zenodo, chapitres Google Books (B2 à B4) ; `doi-match/4` (titre coupé : année **et** auteur) ; DOI des liens Taylor & Francis | ✅ (règles désactivées jusqu'au gate) |
| **3** Vérification humaine | PAT-09 | `scripts/eval/rules_check.py` : fiches à l'aveugle, second jugement, kappa, gate G1 à G5 | ✅ (jugement à faire) |
| **4** Instances | PAT-10 | Verrou de repli sans `flock` ; registre `OUTPUT_DIR/.instances/` ; avertissement d'une instance au code différent ; rangs appliqués si le manifeste décrit le fichier (SHA-256) | ✅ |
| **5** Finitions | PAT-11 à PAT-17 | Dépôt ≠ revue (B6), comptages (B7), séparateurs (B8), HAL (B9), BibTeX (B10), statuts de source (panne d'Isidore groupée, sources non configurées à part), `extra` sans contradiction | ✅ |
| **6** Carnet de bord | PAT-18, PAT-19, PAT-21, PAT-22 | Contexte des entrées d'une fusion (manifeste, `corpus_diff`) ; requêtes ambiguës (`on_ambiguity`) ; notices sans langue gardées et comptées (`keep_unknown_language`) ; HAL : texte intégral en option (`hal_fulltext`) | ✅ |

**Règles optionnelles** (désactivées tant que le gate du sprint n'est pas passé ; `.env.example`) : `MOISSON_DEDUP_CODIFFUSION`, `MOISSON_DEDUP_ZENODO_VERSIONS`, `MOISSON_DEDUP_SCHOLAR_CHAPTERS`, `MOISSON_DOI_CHAPTER_PREFIX`. Autre variable : `MOISSON_BUILD_COMMIT` (image Docker). `config.py` : aucune constante ajoutée.

**Mesures de clôture** : banc de référence identique à `e61c2b9` (3 requêtes, 328 notices en MCP, 0 fusion erronée, retour 14 997 octets) ; doublons résiduels sur 244 corpus 0,43 % (règles désactivées), 0,39 % (règles actives), déterminisme tenu dans les deux cas ; HAL cherche les métadonnées : « "modèle actantiel" » 7 notices, 228 avec le texte intégral (sondes du 2026-09-30). **Tests : 3 909 verts** hors intégration.

**En attente de l'utilisateur** : clés S2 et CORE puis redémarrage complet de Claude Desktop ; jugement des fiches `output/eval/patch-2026-09-30/fiches/verification_regles.html` (environ 20 min) puis `verification_regles_B.html` (environ 4 min) et calcul (`rules_check.py metrics` puis `decide`) ; relecture puis fusion de `sprint-patch` dans `main` et push des tags ; redéploiement du serveur distant (FIN-02) ; ménage de `output/` (annexe B du plan).

## Rapports d'exécution

Au fur et à mesure de la réalisation des sprints, un **fichier rapport** est produit dans `.claude/dev/` au format `sprint_0X_report.md`. Il documente les découvertes, corrections appliquées, résultats des tests et statut final des tâches. Les rapports sont la trace historique du projet — ne jamais les supprimer.

| Sprint | Rapport |
|--------|---------|
| 01 | [.claude/dev/sprint_01_report.md](.claude/dev/sprint_01_report.md) |
| 02 | [.claude/dev/sprint_02_report.md](.claude/dev/sprint_02_report.md) |
| 03 | [.claude/dev/sprint_03_report.md](.claude/dev/sprint_03_report.md) |
| 04 | [.claude/dev/sprint_04_report.md](.claude/dev/sprint_04_report.md) |
| 05 | [.claude/dev/sprint_05_report.md](.claude/dev/sprint_05_report.md) |
| 06 à 12 | voir le tableau du chantier « Serveur MCP distant » ci-dessus |
| Extension internationale | [.claude/dev/PLAN_EXTENSION_INTERNATIONALE_report.md](.claude/dev/PLAN_EXTENSION_INTERNATIONALE_report.md) |
| Extension Google Scholar | [.claude/dev/EXTENSION_GOOGLE_SCHOLAR_report.md](.claude/dev/EXTENSION_GOOGLE_SCHOLAR_report.md) |
| Chantier optimisation | [.claude/dev/sprint-optimise_report.md](.claude/dev/sprint-optimise_report.md) |
| Sprint patch | [.claude/dev/sprint-patch_report.md](.claude/dev/sprint-patch_report.md) |

## Où nous en sommes (mise à jour 2026-09-30)

**État actuel** : sprint patch terminé (version 0.4.0, branche `sprint-patch`, section ci-dessus) après le chantier d'optimisation ; 12 sources, 12 outils MCP, 4 prompts, 3 909 tests verts hors intégration. Les sections qui suivent retracent l'historique des extensions et des sprints : les chiffres qu'elles citent (nombre de sources, d'outils, de tests, durées) sont ceux de leur date.

### Extension internationale ✅ TERMINÉE (2026-06-16)

Ajout de **4 sources internationales** (CrossRef, OpenAlex, Semantic Scholar,
CORE) → **11 sources** au total. Plan :
[.claude/dev/PLAN_EXTENSION_INTERNATIONALE_moisson-shs.md](.claude/dev/PLAN_EXTENSION_INTERNATIONALE_moisson-shs.md)
· Rapport :
[.claude/dev/PLAN_EXTENSION_INTERNATIONALE_report.md](.claude/dev/PLAN_EXTENSION_INTERNATIONALE_report.md).

- Connecteurs REST natifs (cursor/token/offset, throttle, budget-aware via `utils/http_scholarly.py`).
- Secrets : `utils/secrets.py` + `.env`/`.env.example` (OpenAlex/CORE requis, S2 optionnel) ; clé manquante = source ignorée proprement.
- Groupes de sources `--sources francophone|international|all` (défaut francophone) ; `resolve_sources` (registry).
- Dédup FR/EN : IDs OpenAlex/S2/CORE + `SOURCE_FIELD_PRIORITY` + matrice de recouvrement.
- MCP étendu : `moisson_search` (+4 + groupes), `fetch_by_doi` (+OpenAlex/S2/CORE), `search_by_author` (+OpenAlex/S2), prompt `etat-de-l-art` multilingue.
- `setup` injecte les clés dans le bloc `env` (valeurs masquées à l'affichage).
- Revue adversariale croisée (12 relecteurs) : 6 findings corrigés, dont un **showstopper CrossRef** (`select=language` → HTTP 400 → 0 résultat) confirmé puis corrigé en live.
- **Tests : 420 PASS** (hors réseau) ; couverture mappers+utils **82 %** ; tests d'intégration `skip` sans clé ; CI : job `integration` conditionné aux secrets.

### Extension Google Scholar (SerpAPI) — ✅ TERMINÉE (2026-09-22)

Ajout d'une **douzième source**, Google Scholar via SerpAPI. Rapport :
[.claude/dev/EXTENSION_GOOGLE_SCHOLAR_report.md](.claude/dev/EXTENSION_GOOGLE_SCHOLAR_report.md).

- Connecteur `connectors/google_scholar.py` + `mappers/google_scholar_mapper.py`.
- **Seule source facturée à la requête** (1 crédit par page de 20 résultats ;
  compte payant à 5 000 requêtes/mois, partagé avec MyWebIntelligence).
  Elle est **exclue des groupes `international` et `all`** (constante
  `METERED_SOURCES` dans le registry) : elle reste volontaire, pour son coût à
  la requête et son quota partagé avec MyWebIntelligence. *(Motif corrigé le
  2026-09-29 : l'argument de syntaxe d'origine — Scholar honorerait des
  guillemets et booléens que les quatre sources internationales ignoreraient —
  est faux ; la vérification live d'OPT-11 montre que seul CrossRef traite la
  requête en sac de mots, OpenAlex et Semantic Scholar honorant phrases et
  opérateurs. Chaque source reçoit désormais la requête traduite dans sa
  syntaxe par le plan de requête.)* Elle ne s'active donc que nommée, ou
  par le groupe dédié `scholar`. Plafond `MOISSON_SERPAPI_MAX_PAGES` (défaut
  10) et refus de démarrage sous `MOISSON_SERPAPI_MIN_CREDITS` (défaut 100,
  contrôle gratuit via l'Account API).
- Métadonnées pauvres par nature : pas de DOI (le champ n'est rempli que depuis
  une URL `doi.org` ou `/doi/`, jamais deviné), pas de résumé (l'extrait Scholar
  est signalé comme tel dans `extra`), auteurs et revue tronqués par Google et
  marqués `Scholar truncation`. Conservés en revanche : `Cited by`,
  `Scholar cluster ID`, lien de texte intégral.
- **Correctif de fond** : `utils/secrets.py` résolvait le `.env` depuis le
  répertoire courant, donc jamais en production (le serveur MCP est lancé depuis
  un répertoire arbitraire). Le chemin est désormais résolu depuis le module.
  Le bloc `env` de `claude_desktop_config.json` reste prioritaire.
- **Outil MCP `corpus_resolve_dois`** (9e outil) : Scholar ne renvoyant aucun
  DOI, le rangement automatique dans Zotero (`zotero_add_by_doi`) tombait. Le
  module `utils/doi_resolver.py` retrouve les DOI manquants par appariement
  bibliographique contre CrossRef (gratuit, sans clé). Règle d'acceptation
  stricte et traçable : similarité de titre ≥ 0.90, corroborée par l'année
  (±1 an) et le nom du premier auteur ; appariement par préfixe pour les
  sous-titres absents d'un côté, jamais accepté sans corroboration ; ligne
  `DOI source` dans `extra` énonçant le score. **Ne jamais abaisser le seuil
  pour relever le taux** : un DOI faux entre silencieusement dans Zotero.
- Tests : **+67** (`test_serpapi_connector.py` 38, `test_doi_resolver.py` 29),
  suite à **558 PASS**.

### Sprint 01 ✅ TERMINÉ et VALIDÉ utilisateur (2026-05-23)

Toutes les tâches du sprint sont complétées (+ hotfix CSL-JSON anticipé). **Import dans Zotero Desktop confirmé fonctionnel par l'utilisateur** sur le corpus de 103 items.

- ✅ **#1** — Test end-to-end sur requête réelle (`discours numérique`, 30 items × 4 sources → 103 uniques)
- ✅ **#2** — Validation Zotero (0 erreur / 0 avertissement contre le schéma officiel `https://api.zotero.org/schema`)
- ✅ **#4** — Vérification des endpoints actuels (HAL, Isidore, OpenEdition v2, Cairn)
- ✅ **#5** — Correction de la pagination Isidore avec source_filter
- ✅ **#6** — Vérification du format d'identifiant OAI Cairn
- ✅ **#20** (anticipé) — Export CSL-JSON (BibTeX JSON) pour import Zotero direct

**Découvertes critiques traitées :**

1. URL Cairn OAI obsolète → corrigée en `/oai.php`
2. Format identifiant OAI Cairn fautif (`{DOI}` → `{CODE_REVUE_VOL_PAGE}`)
3. Structure de réponse Isidore complètement différente du format initialement supposé → mapper réécrit
4. Champs Isidore tous multilingues `[{@xml:lang, $}]`
5. Filtrage par nom de source remplacé par filtrage par domaine d'URL
6. +20 types Dublin Core ajoutés au mapping
7. **Hotfix CSL-JSON** : le format Zotero API JSON natif n'est PAS reconnu par Zotero Desktop à l'import. Ajout d'un exporteur CSL-JSON (Citation Style Language JSON, le format « BibTeX JSON » standard) en sortie parallèle.

**Artefacts produits :**

- [tests/test_sprint_01.py](tests/test_sprint_01.py) — suite de non-régression (**30/30 PASS**)
- [mappers/csl_exporter.py](mappers/csl_exporter.py) — exporteur CSL-JSON
- `output/corpus_discours_numerique_*.json` — corpus Zotero JSON interne (riche, consommé par le MCP)
- `output/corpus_discours_numerique_*.csl.json` — corpus CSL-JSON (importable Zotero Desktop)
- `output/corpus_discours_numerique_*_stats.txt` — statistiques
- `output/raw/` — payloads bruts par source pour audit

**Deux formats de sortie à chaque moisson :**

| Fichier | Format | Usage |
|---------|--------|-------|
| `corpus_*.json` | Zotero API JSON interne | Consommé par le MCP, analyses programmatiques, filtrage |
| `corpus_*.csl.json` | **CSL-JSON (BibTeX JSON standard)** | **Import direct Zotero Desktop**, lisible par tout outil CSL |

**Outils MCP exposés (5) :**
1. `moisson_search` — moisson multi-sources, produit les deux formats
2. `corpus_analyze` — statistiques d'un corpus
3. `corpus_list` — liste les corpus disponibles
4. `corpus_filter` — filtrage par critères
5. `corpus_to_csl` — convertit un corpus interne en CSL-JSON (utile pour anciens corpus)

### Sprint 02 ✅ TERMINÉ (2026-05-23)

- ✅ **#3** — Tests unitaires (148 unit + 16 sprint_02 + 15 sprint_01 + 15 csl + integration = **214 tests, tous PASS, couverture 82%** sur mappers+utils)
- ✅ **#8** — Signature uniforme des connecteurs (`**kwargs`) → préalable au registry du Sprint 03
- ✅ **#9** — Moissonnage parallèle (`--workers 2`, ThreadPoolExecutor, gain ÷2)
- ✅ **#10** — Cache HTTP local (`--cache`, requests-cache, **gain ×17 mesuré** : 21 s → 1.2 s)
- ✅ **#11** — Validation contre schéma Zotero officiel (téléchargement + cache 30 j + fallback embarqué)
- ✅ **#12** — Filtres temporels `--from-date` / `--to-date` (HAL natif Solr `fq`, autres post-pagination)

**Nouveau module** : [mappers/zotero_schema_loader.py](mappers/zotero_schema_loader.py) — chargement du schéma officiel `https://api.zotero.org/schema`.

**Architecture tests** :
```
tests/
├── conftest.py                  # fixtures pytest
├── fixtures/                    # payloads réels API (HAL, Isidore, OAI)
└── unit/                        # 8 fichiers de tests unitaires
```

**Lancer les tests** :
```bash
source venv/bin/activate
python -m pytest tests/ -q                                  # 214 PASS
python -m pytest tests/ --cov=mappers --cov=utils -q       # avec couverture
```

### Sprint 03 ✅ TERMINÉ (2026-05-23)

- ✅ **#16** — Registry pattern : `connectors/registry.py` posé, ajouter une source = 1 import + 1 entrée
- ✅ **#13** — Connecteur **Persée** (proxy Isidore + OAI optionnel)
- ✅ **#14** — Connecteur **Theses.fr** (proxy Isidore uniquement, API REST publique retirée en 2026)
- ✅ **#15** — Connecteur **Érudit** (proxy Isidore + OAI optionnel)
- ✅ **#17** — Outil MCP `fetch_by_doi` (Crossref + HAL + Isidore)
- ✅ **#18** — Outil MCP `search_by_author` (HAL Solr + Isidore, trié par année)
- ✅ **#19** — Outil MCP `corpus_merge` (fusion N corpus avec rapport)
- ✅ **#21** — Resources MCP : corpus exposés via `corpus://<filename>`
- ✅ **#22** — 4 Prompts MCP : `bibliographie-thematique`, `profil-chercheur`, `veille-mensuelle`, `etat-de-l-art`

**7 sources couvertes** : HAL, Isidore, OpenEdition, Cairn, Persée, Theses.fr, Érudit

**8 outils MCP + 4 prompts + Resources** exposés. Tests : 241 PASS.

**Lien ProfilBiblio** : ProfilBiblio (profil bibliographique d'un chercheur) peut désormais déléguer la récupération bibliographique à `search_by_author` via MCP.

### Outils MCP disponibles (12, état au 2026-09-30)

| Outil | Description |
|-------|-------------|
| `moisson_search` | Moisson multi-sources (12 sources ; Google Scholar seulement nommé) ; digest de 40 notices en stdio, plan de requête, statuts par source, échéance souple 50 s ; requête ambiguë refusée par défaut si une source facturée est demandée (`on_ambiguity`) ; texte intégral HAL en option (`hal_fulltext`) |
| `corpus_analyze` | Stats descriptives d'un corpus |
| `corpus_list` | Lister les corpus (index dérivé ; `limit`, `pattern`, `since`, `rebuild`) |
| `corpus_filter` | Filtrer par critères (année, type, langue, source) ; notices sans langue gardées et comptées (`keep_unknown_language`) |
| `corpus_to_csl` | Conversion Zotero JSON → CSL-JSON |
| `fetch_by_doi` | Récupération ciblée par DOI (six résolveurs en parallèle, DOI vérifié) |
| `search_by_author` | Production d'un chercheur, par identifiants de préférence (idHal, ORCID, OpenAlex, S2) ; homonymes rendus comme candidats ; jamais présentée comme complète à tort |
| `corpus_merge` | Fusion N corpus avec rapport |
| `corpus_resolve_dois` | Résolution des DOI manquants par appariement CrossRef (règles `doi-match/4`, rapport `.doi_report.json`) |
| `corpus_export` | Export BibTeX, BibLaTeX ou RIS |
| `corpus_query` | Consultation d'un corpus : recherche classée, filtres, pagination, notices complètes par `ids` (≤ 20) |
| `corpus_diff` | Comparaison de deux corpus d'une même requête (ajouts, retraits, enrichissements ; rapport `.diff.json`) ; une fusion garde le contexte de ses entrées |

### Sprint 04 ✅ TERMINÉ (2026-05-23)

- ✅ **#7** — `mcp>=1.0.0` activé dans requirements.txt et pyproject extras
- ✅ **#23** — `pyproject.toml` PEP 621 avec entry_point `moisson-shs`
- ✅ **#24** — `git init` (branche main) + `.gitignore` complet
- ✅ **#25** — CI GitHub Actions (matrix Python 3.10/3.11/3.12 + smoke test CLI)
- ✅ **#26** — Tutoriel README en 4 étapes + référence outils MCP
- ✅ **#27** — `docs/FAQ.md` avec 17 questions/réponses
- ✅ **Bonus** — `moisson-shs setup` : installation Claude Desktop automatisée cross-platform

**Installation utilisateur final (3 commandes)** :

```bash
pip install -e ".[mcp]"      # ou pipx install moisson-shs[mcp]
moisson-shs setup            # configure Claude Desktop automatiquement
# → Quitter et relancer Claude Desktop
```

Le script `setup_claude_desktop.py` détecte l'OS, le venv Python, le chemin du serveur MCP, **préserve les autres serveurs MCP existants**, crée un backup `.bak`, valide les dépendances.

### Sprint 05 ✅ TERMINÉ (2026-06-16)

Retour MCP enrichi : à la fin d'une recherche, les outils producteurs renvoient
**directement au client MCP** le lien cliquable vers le(s) fichier(s) + le JSON
du corpus inline, en un seul retour (sous garde de volumétrie).

- ✅ **#28** — Helper `_build_corpus_tool_result` + dispatcher acceptant `str` / `list[ContentBlock]` / `tuple(content, structured)` (*CombinationContent* SDK)
- ✅ **#29** — `moisson_search` : `ResourceLink`(json/csl/stats) + `EmbeddedResource` Zotero inline + `structuredContent`
- ✅ **#30** — Généralisation : `corpus_filter`, `corpus_to_csl` (CSL primaire), `corpus_merge`, `search_by_author`
- ✅ **#31** — Seuil `INLINE_MAX_ITEMS` (défaut 300, env `MOISSON_INLINE_MAX_ITEMS`), aligné sur la garde 2 Mo de `read_resource`
- ✅ **#32** — `tests/unit/test_tool_result.py` : **27 tests PASS**
- ✅ **#33** — README (section « Retour MCP ») + AGENTS.md + `sprint_05_report.md`
- ✅ **Correctif latent** — `read_resource` durci aux `AnyUrl` (le SDK passe un `AnyUrl`, pas une `str` : `uri.startswith` plantait en production ; round-trip `corpus://` désormais sûr)
- ✅ **Correctif revue** — registre `_EMITTED_RESOURCES` : les liens `corpus://` des fichiers écrits HORS de OUTPUT_DIR (`corpus_filter` à côté de la source, `corpus_to_csl`/`corpus_merge` avec `output_path` arbitraire) sont désormais résolus par `read_resource` (round-trip fiable, DoD #80)
- ✅ **Durcissement réseau** — `config.py` : `HTTP_TIMEOUT` devient un tuple `(connect=8, read=45)`, `HTTP_MAX_RETRIES=3`, backoff `1.0`, pas de sleep après la dernière tentative, tout surchargeable par env. Pire cas par source morte : **~262 s → ~27 s** ⇒ `moisson_search` ne dépasse plus le délai d'outil de Claude Desktop quand une source est lente/injoignable. Couvert par `tests/unit/test_http_resilience.py` (5 tests)

**5 outils producteurs** au retour enrichi : `moisson_search`, `corpus_filter`,
`corpus_to_csl`, `corpus_merge`, `search_by_author`. `fetch_by_doi` reste hors
périmètre (pas de fichier primaire). `outputSchema` écarté (masquerait les
erreurs ; cf. rapport).

**État du projet à la fin du sprint 05** (historique ; état actuel : section « Chantier optimisation ») :

| Métrique | Valeur |
|----------|--------|
| Sources couvertes | **7** (HAL, Isidore, OpenEdition, Cairn, Persée, Theses.fr, Érudit) |
| Outils MCP exposés | **9** + 4 prompts + Resources `corpus://` (5 producteurs au retour enrichi lien+JSON) |
| Tests | **308** (303 non-integration + 5 integration network) — dont 32 Sprint 05 ; `tests/unit/` = 273 PASS en ~1,8 s hors ligne |
| Couverture mappers+utils | **78%** |
| Formats de sortie | Zotero JSON interne + CSL-JSON (Zotero import) |
| Retour MCP | Résumé texte + `ResourceLink` + `EmbeddedResource` (JSON inline ≤ seuil) + `structuredContent` |
| Performance | chiffre d'alors « 21 s pour 4×500 items » : projection du sprint 02 (mesure sur 4×20), non une mesure ; remplacé par les mesures du chantier d'optimisation |
| Packaging | `pip install -e .` opérationnel, entry_point `moisson-shs` |
| Git | Initialisé branche `main` (pas de commit auto) |
| Documentation | README walkthrough + FAQ 17 questions + 5 rapports sprints |

### Commandes utiles (mises à jour le 2026-09-29, depuis la racine du projet)

```bash
# Installation / réinstallation (versions épinglées ; à refaire si le dossier du projet est déplacé)
venv/bin/python -m pip install -e ".[dev,mcp]" -c constraints.txt

# Moisson CLI (défauts : mode collecte, 1000 par source, 1 worker, cache hérité de MOISSON_CACHE)
venv/bin/moisson-shs search "votre requête" --max 500 --workers 4 --cache
venv/bin/moisson-shs search "votre requête" --plan-only                      # plan de requête par source, sans exécution
venv/bin/moisson-shs search "votre requête" --mode bibliographie --lang fr en --query-en "english query"
venv/bin/moisson-shs export output/corpus_x.json --format ris                # bibtex (défaut), biblatex, ris

# Setup MCP
venv/bin/moisson-shs setup --status        # état actuel
venv/bin/moisson-shs setup                 # installation
venv/bin/moisson-shs setup --dry-run       # aperçu sans modifier
venv/bin/moisson-shs setup --uninstall     # désinstallation propre

# Serveur MCP standalone (debug) : stdio, ou distant
venv/bin/moisson-shs serve
venv/bin/moisson-shs serve --http --host 127.0.0.1 --port 8000

# Tests (3 909 verts hors intégration au 2026-09-30)
venv/bin/python -m pytest tests/ -m "not integration" -q -p no:cacheprovider                # comme la CI, sans réseau externe
venv/bin/python -m pytest tests/ -m "not integration and not sockets" -q -p no:cacheprovider # ni réseau ni socket locale
venv/bin/python -m pytest tests/ -m sockets -q -p no:cacheprovider                           # transport HTTP local (25 tests)
venv/bin/python -m pytest tests/ -m "not integration" -q -p no:cacheprovider --cov=mappers --cov=utils --cov=connectors --cov-fail-under=70
MOISSON_TEST_REAL_KEYS=1 venv/bin/python -m pytest tests/integration -m integration -q       # API réelles, clés du shell (jamais SerpAPI)
venv/bin/python -m pytest tests/unit/test_opt10_performance.py -q -p no:cacheprovider        # test de durée sensible à la charge : relancer seul avant de conclure

# Banc de mesure (simulé, sans réseau)
venv/bin/python scripts/benchmark_moisson.py --scenario all --sleep-scale 0                  # compteurs exacts, durées non réalistes
venv/bin/python scripts/benchmark_moisson.py --scenario reference --entry both --repeat 5 --latency-ms 0,150 --json-out /tmp/ref.json
venv/bin/python scripts/benchmark_moisson.py --compare tests/fixtures/bench/baseline_2026-09-29.summary.json /tmp/ref.json

# Doublons résiduels sur les corpus de output/ (lecture seule)
venv/bin/python scripts/measure_duplicates.py --check-determinism --json-out /tmp/dup.json

# Évaluation du classement (OPT-15, hors réseau une fois la collecte faite ; voir docs/EVALUATION.md)
venv/bin/python scripts/eval/pool.py --date 2026-09-29 status
venv/bin/python scripts/eval/pool.py --date 2026-09-29 build && venv/bin/python scripts/eval/sheets.py --date 2026-09-29 generate && venv/bin/python scripts/eval/metrics.py --date 2026-09-29 && venv/bin/python scripts/eval/decide.py --date 2026-09-29

# Vérification des règles d'identité et de DOI du sprint patch (docs/EVALUATION.md § 9), une fois les fiches rendues
venv/bin/python scripts/eval/rules_check.py metrics --date patch-2026-09-30 && venv/bin/python scripts/eval/rules_check.py decide --date patch-2026-09-30
```

## Système de suivi des tâches

Les 27 tâches du backlog complet sont suivies via `TaskList` / `TaskUpdate`. Marquer `in_progress` au démarrage d'une tâche, `completed` à la fin (jamais en avance). Au démarrage d'une nouvelle session, faire un `TaskList` pour reprendre le contexte.

## Conventions techniques

- **Sortie** : tous les corpus produits vont dans `output/corpus_*.json` (+ `.csl.json`, `_stats.txt`, `.manifest.json` publié en dernier ; selon l'outil `.doi_report.json`, `.diff.json`, `.bib`/`.ris`). `output/raw/<source>_<run_id>.json` contient les notices normalisées par source (pas les réponses natives : celles-ci vont dans `.native_audit/` si `MOISSON_AUDIT_NATIVE=1`). Index dérivé `output/.index.json`. Fichiers de service (commençant par un point : `.oauth/`, `.audit/`, `.native_audit/`, temporaires, `.moisson.lock`) jamais listés, lus ni téléchargés. Ces dossiers sont à `.gitignore`. Scores, rangs et plan de requête : dans le manifeste, **jamais** dans le JSON Zotero ni dans `extra`.
- **Logs** : niveau INFO par défaut, DEBUG avec `--verbose`. Sur MCP (stdio), les logs vont sur stderr (stdout réservé au JSON-RPC).
- **Tests** : `tests/test_sprint_0X.py` par sprint, exécutables avec `venv/bin/python tests/test_sprint_0X.py` (mode standalone) ou `pytest` ; chantier d'optimisation : `tests/unit/test_opt<NN>_*.py` et `tests/unit/test_lot*_integration.py`. Aucune clé ni `.env` pour les tests unitaires (`tests/conftest.py`) ; marqueurs `integration` (API réelles) et `sockets` (transport local).
- **Pas de modification du config.py** sans mise à jour conjointe du report concerné — le fichier centralise les endpoints et constantes critiques. Toute constante ajoutée est surchargeable par une variable `MOISSON_*`, commentée, et recensée dans `.env.example`.
- **Seuils d'identité et de DOI jamais abaissés** (0,90 titre, 0,97 sans corroboration, 0,93 passe floue, corroboration année et auteur) ; Google Scholar reste volontaire (hors `all` et `international`). Les règles optionnelles `dedup/2` et `doi-match/4` (co-diffusion, versions Zenodo, chapitres Google Books) restent derrière leurs drapeaux tant que leur vérification humaine n'a pas passé le gate (`docs/EVALUATION.md` § 9).
- **Git** : aucun commit automatique ; les commits se font à la demande de l'utilisateur, avec un message factuel signé de l'auteur et sans mention d'outil. `main` = `e61c2b9` (poussé, tag local `v0.3.0`) ; le sprint patch est sur la branche `sprint-patch` (tag local `v0.4.0`), ni fusionnée ni poussée.

## Lancer une moisson de test

```bash
venv/bin/python main.py "votre requête" --max 30 --verbose
```

Le corpus CSL produit (`.csl.json`) est immédiatement importable dans Zotero Desktop via Fichier > Importer.

## Lancer la suite de tests Sprint 01

```bash
venv/bin/python tests/test_sprint_01.py
```

Sortie attendue : `30 passés, 0 échoués` (le mode standalone exécute aussi les 3 tests `integration`, qui interrogent Cairn et Isidore : réseau requis ; sous pytest, `-m "not integration"` les écarte).
