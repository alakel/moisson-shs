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

## Plan de développement initial (sprints 01 à 05)

Le travail de finalisation est organisé en cinq sprints. Chaque fichier contient : objectif, tâches incluses, ordre d'exécution, critères de sortie (DoD), détails techniques, risques, livrables et estimation.

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

### Sprint « clés » (déclaration du client, réserve du `.env`) — ✅ TERMINÉ (2026-10-08)

Plan : [.claude/dev/sprint-cles.md](.claude/dev/sprint-cles.md) (CLE-01 à CLE-08) · Rapport : [.claude/dev/sprint-cles_report.md](.claude/dev/sprint-cles_report.md). Arbre de travail de `sprint-patch`, aucun commit. `setup` inscrit **toujours** les quatre clés dans la déclaration du client (vides si non fournies), pour Claude Desktop et désormais Claude Code (`--client desktop|code|all`, portée utilisateur par `claude mcp add-json`) ; `--status` donne l'état de chaque clé. Le `.env` du projet devient la **réserve** du développeur : ses clés ne vont plus dans `os.environ` ; elles servent si la clé du client n'est pas déclarée, ou, pour les sources de `MOISSON_KEY_FALLBACK` (défaut `openalex,semantic_scholar,core`, jamais SerpAPI), si elle est vide ou refusée (401, 403 : une requête de plus dans `policy_get`, refus mémorisé, signalé dans le statut de la source). Sans aucune clé, les autres sources restent disponibles (testé). Fuite corrigée au passage : `setup --dry-run` imprimait les clés des autres serveurs MCP. **Tests : 3 959 verts** hors intégration et sockets (+ 25 sockets). `config.py` inchangé.

**En attente de l'utilisateur** : `venv/bin/python cli.py setup --client all` dans Terminal.app, puis relance des clients ; rotation des clés d'autres serveurs affichées pendant le sprint (rapport § 5) ; réinstallation du paquet (`venv/bin/moisson-shs` pointe vers l'ancien dossier) ; relecture et commits ; version (0.4.0 ou 0.4.1).

### Sprint « universel » (setup des clients MCP) — ✅ TERMINÉ (2026-10-08)

Plan : [.claude/dev/sprint-universel.md](.claude/dev/sprint-universel.md) · Rapport : [.claude/dev/sprint-universel_report.md](.claude/dev/sprint-universel_report.md). Arbre de travail de `sprint-patch`, aucun commit. `moisson-shs setup` configure sept clients : Claude Desktop, Claude Code, OpenAI Codex (application ChatGPT, CLI, IDE ; TOML par `tomlkit`), Gemini CLI, Antigravity CLI et IDE, Hermes Agent (YAML par `ruamel.yaml`). Sans `--client` en terminal : menu des clients détectés, déjà cochés ; hors terminal : Claude Desktop seul. `--client` accepte ids et alias séparés par des virgules ; `all` = tous les clients détectés (changement de sens). Nouveau paquet `mcp_clients/` (un adaptateur par client) ; chaque écriture : sauvegarde horodatée `.moisson-bak-*`, écriture atomique, garde sémantique (sinon extrait à coller, clés en marqueurs), passage en 0600 si le fichier porte des clés ; application ouverte vérifiée (`--force` hors terminal) ; clés saisies une fois et mises en commun entre clients ; délais Codex et Hermes relevés à 960 s. Aucune valeur secrète affichée (testé sur les sept clients). **Tests : 4 138 verts** hors intégration et sockets (180 nouveaux), 25 sockets verts.

**En attente de l'utilisateur** : `venv/bin/python cli.py setup` dans Terminal.app (cocher au moins Codex : son entrée pointe vers l'ancien dossier du projet) ; relance des clients ; relecture et commits ; version (0.5.0 ou 0.4.1).

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
| Sprint « clés » | [.claude/dev/sprint-cles_report.md](.claude/dev/sprint-cles_report.md) |
| Sprint « universel » | [.claude/dev/sprint-universel_report.md](.claude/dev/sprint-universel_report.md) |

## Où nous en sommes (mise à jour 2026-09-30)

**État actuel** : sprint patch terminé (version 0.4.0, branche `sprint-patch`, section ci-dessus) après le chantier d'optimisation ; 12 sources, 12 outils MCP, 4 prompts, 3 909 tests verts hors intégration. Historique des extensions et des sprints 01 à 05 (chiffres et commandes de leur date) : [docs/HISTORIQUE.md](docs/HISTORIQUE.md).

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

## Reprise de contexte

En début de session : section « Où nous en sommes », puis le rapport du dernier chantier (tableau « Rapports d'exécution »).

## Conventions techniques

- **Sortie** : tous les corpus produits vont dans `output/corpus_*.json` (+ `.csl.json`, `_stats.txt`, `.manifest.json` publié en dernier ; selon l'outil `.doi_report.json`, `.diff.json`, `.bib`/`.ris`). `output/raw/<source>_<run_id>.json` contient les notices normalisées par source (pas les réponses natives : celles-ci vont dans `.native_audit/` si `MOISSON_AUDIT_NATIVE=1`). Index dérivé `output/.index.json`. Fichiers de service (commençant par un point : `.oauth/`, `.audit/`, `.native_audit/`, temporaires, `.moisson.lock`) jamais listés, lus ni téléchargés. Ces dossiers sont à `.gitignore`. Scores, rangs et plan de requête : dans le manifeste, **jamais** dans le JSON Zotero ni dans `extra`.
- **Logs** : niveau INFO par défaut, DEBUG avec `--verbose`. Sur MCP (stdio), les logs vont sur stderr (stdout réservé au JSON-RPC).
- **Tests** : `tests/test_sprint_01.py`, seul exécutable hors pytest (`venv/bin/python tests/test_sprint_01.py`) ; sprints suivants : `tests/unit/test_sprint_0X.py` et `tests/integration/test_sprint_06_http.py` ; chantier d'optimisation : `tests/unit/test_opt<NN>_*.py` et `tests/unit/test_lot*_integration.py` ; sprint patch : `tests/unit/test_pat*.py`. Aucune clé ni `.env` pour les tests unitaires (`tests/conftest.py`) ; marqueurs `integration` (API réelles) et `sockets` (transport local).
- **Pas de modification du config.py** sans mise à jour conjointe du report concerné — le fichier centralise les endpoints et constantes critiques. Toute constante ajoutée est surchargeable par une variable `MOISSON_*`, commentée, et recensée dans `.env.example`.
- **Seuils d'identité et de DOI jamais abaissés** (0,90 titre, 0,97 sans corroboration, 0,93 passe floue, corroboration année et auteur) : un DOI faux entre silencieusement dans Zotero. Google Scholar reste volontaire (hors `all` et `international`) : facturé à la requête, quota SerpAPI partagé avec MyWebIntelligence. Les règles optionnelles `dedup/2` et `doi-match/4` (co-diffusion, versions Zenodo, chapitres Google Books) restent derrière leurs drapeaux tant que leur vérification humaine n'a pas passé le gate (`docs/EVALUATION.md` § 9).
- **Formats** : le `.csl.json` est le format d'import de Zotero Desktop (Fichier > Importer) ; le `.json` interne (format de l'API Zotero) n'y est pas reconnu : il sert au serveur MCP et aux outils de corpus.
- **Secrets** : `utils/secrets.py` lit le `.env` du projet, chemin résolu depuis le module (le serveur MCP est lancé depuis un répertoire arbitraire). La déclaration du client (bloc `env` de Claude Desktop ou de Claude Code, où `moisson-shs setup` inscrit toujours les quatre clés) fait autorité ; les clés du `.env` sont une réserve, jamais injectée dans `os.environ`, qui sert si la clé n'est pas déclarée, ou, selon `MOISSON_KEY_FALLBACK` (jamais SerpAPI par défaut), si elle est vide ou refusée (401, 403). `setup` ne copie jamais les clés du `.env` chez le client. Clé absente : source « non exécutée », la moisson continue.
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
