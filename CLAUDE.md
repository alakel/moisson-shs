# Contexte projet — moisson_shs

Moissonneur Python multi-sources d'articles SHS francophones (HAL, Isidore, OpenEdition, Cairn) avec sortie au format **Zotero JSON natif**. Exposé comme **serveur MCP** local pour Claude Desktop.

## Stack

- **Python ≥ 3.10** (testé sur 3.14)
- Dépendances : `requests`, `sickle` (OAI-PMH), `lxml`, `unidecode`
- SDK MCP optionnel : `mcp>=1.0.0` pour `mcp_server.py`
- venv local dans `venv/`

## Documentation principale

- [README.md](README.md) — vue d'ensemble, installation, usage CLI et MCP
- [PLAN.md](PLAN.md) — plan de développement initial avec stratégies par source
- [MCP_INSTALL.md](MCP_INSTALL.md) — installation du serveur MCP dans Claude Desktop

## Plan de développement en 4 sprints

Le travail de finalisation est organisé en 4 sprints. Chaque fichier contient : objectif, tâches incluses, ordre d'exécution, critères de sortie (DoD), détails techniques, risques, livrables et estimation.

| Sprint | Fichier | Objectif | Statut |
|--------|---------|----------|--------|
| **01** | [.claude/dev/sprint_01.md](.claude/dev/sprint_01.md) | Déblocage et validation fonctionnelle | ✅ **TERMINÉ + import Zotero validé utilisateur** |
| **02** | [.claude/dev/sprint_02.md](.claude/dev/sprint_02.md) | Robustesse, performance et qualité du code | ✅ **TERMINÉ** |
| **03** | [.claude/dev/sprint_03.md](.claude/dev/sprint_03.md) | Extension : nouvelles sources et outils MCP avancés | ✅ **TERMINÉ** |
| **04** | [.claude/dev/sprint_04.md](.claude/dev/sprint_04.md) | Distribution, CI/CD et documentation utilisateur | ✅ **TERMINÉ** |
| **05** | [.claude/dev/sprint_feedback.md](.claude/dev/sprint_feedback.md) | Retour MCP enrichi : lien vers fichier + JSON inline au chat/agent | ✅ **TERMINÉ** |

### Chantier « Serveur MCP distant » (double transport stdio + HTTP) — ✅ TERMINÉ (2026-06-18)

Plan directeur : [.claude/dev/sprint_remote_plan.md](.claude/dev/sprint_remote_plan.md) — `moisson-shs serve` reste stdio par défaut et gagne un **mode serveur distant HTTP** (`serve --http`) : Streamable HTTP, auth **Bearer**, **Docker + Caddy (TLS)**, retour par **URLs de téléchargement**, opérations longues robustes, observabilité. Le **même** `Server` (8 outils inchangés) alimente les deux transports. Guide : [REMOTE.md](REMOTE.md) · Déploiement : [DEPLOY.md](DEPLOY.md).

| Sprint | Fichier | Objectif | Statut | Rapport |
|--------|---------|----------|--------|---------|
| **06** | [.claude/dev/sprint_06.md](.claude/dev/sprint_06.md) | Socle transport HTTP (Streamable HTTP) + double `serve` | ✅ TERMINÉ | [report](.claude/dev/sprint_06_report.md) |
| **07** | [.claude/dev/sprint_07.md](.claude/dev/sprint_07.md) | Auth Bearer + sandbox des chemins + durcissement | ✅ TERMINÉ | [report](.claude/dev/sprint_07_report.md) |
| **08** | [.claude/dev/sprint_08.md](.claude/dev/sprint_08.md) | Retour distant : URLs de téléchargement + `/download` | ✅ TERMINÉ | [report](.claude/dev/sprint_08_report.md) |
| **09** | [.claude/dev/sprint_09.md](.claude/dev/sprint_09.md) | Opérations longues : progression, deadline, concurrence, anti-DoS | ✅ TERMINÉ | [report](.claude/dev/sprint_09_report.md) |
| **10** | [.claude/dev/sprint_10.md](.claude/dev/sprint_10.md) | Déploiement Docker + reverse proxy TLS (Caddy) | ✅ TERMINÉ | [report](.claude/dev/sprint_10_report.md) |
| **11** | [.claude/dev/sprint_11.md](.claude/dev/sprint_11.md) | Onboarding client distant, CLI jetons, observabilité, `REMOTE.md` | ✅ TERMINÉ | [report](.claude/dev/sprint_11_report.md) |

**Serveur distant — capacités livrées :** `serve --http` (Streamable HTTP `/mcp` + `/download` + `/healthz` public + `/metrics` auth, uvicorn 1 worker derrière Caddy TLS) ; **auth Bearer** (refus sans jeton/allowlist Host) ; **sandbox** HTTP-only (anti-traversal, stdio inchangé) ; **retour distant** par URLs HTTPS (jamais de chemin disque ni JSON inline) ; **opérations longues** (progression SSE, deadline+annulation, sémaphore, plafond anti-DoS, uuid) ; **CLI** `token {generate,rotate,status,revoke}` + `setup --remote` ; **observabilité** (logs JSON+redaction, audit JSONL, `/metrics`). Image Docker non-root **build+run validés**. Revue adversariale multi-agents (10 agents) : **4 findings corrigés** (dont fuite de chemins `corpus_list` en HTTP). **+93 tests** → **517 PASS** hors intégration, **0 régression stdio**. Modules ajoutés : `auth.py`, `remote_auth.py`, `utils/sandbox.py` ; `Dockerfile`/`docker-compose.yml`/`deploy/Caddyfile`/`DEPLOY.md`/`REMOTE.md`/`scripts/smoke_remote.sh`.

## Rapports d'exécution

Au fur et à mesure de la réalisation des sprints, un **fichier rapport** est produit dans `.claude/dev/` au format `sprint_0X_report.md`. Il documente les découvertes, corrections appliquées, résultats des tests et statut final des tâches. Les rapports sont la trace historique du projet — ne jamais les supprimer.

| Sprint | Rapport |
|--------|---------|
| 01 | [.claude/dev/sprint_01_report.md](.claude/dev/sprint_01_report.md) |
| 02 | [.claude/dev/sprint_02_report.md](.claude/dev/sprint_02_report.md) |
| 03 | [.claude/dev/sprint_03_report.md](.claude/dev/sprint_03_report.md) |
| 04 | [.claude/dev/sprint_04_report.md](.claude/dev/sprint_04_report.md) |
| 05 | [.claude/dev/sprint_05_report.md](.claude/dev/sprint_05_report.md) |

## Où nous en sommes (mise à jour 2026-06-16)

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
- Revue adversariale multi-agent (12 agents) : 6 findings corrigés, dont un **showstopper CrossRef** (`select=language` → HTTP 400 → 0 résultat) confirmé puis corrigé en live.
- **Tests : 420 PASS** (hors réseau) ; couverture mappers+utils **82 %** ; tests d'intégration `skip` sans clé ; CI : job `integration` conditionné aux secrets.

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
3. Structure de réponse Isidore complètement différente du modèle initial → mapper réécrit
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
| `corpus_*.csl.json` | **CSL-JSON (BibTeX JSON standard)** | **Import direct Zotero Desktop**, lisible par Claude/LLMs |

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

**Lien ProfilBiblio** : la skill `anthropic-skills:profilbiblio` peut désormais déléguer la récupération bibliographique à `search_by_author` via MCP.

### Outils MCP disponibles

| Outil | Description |
|-------|-------------|
| `moisson_search` | Moisson multi-sources (jusqu'à 7) |
| `corpus_analyze` | Stats descriptives d'un corpus |
| `corpus_list` | Lister les corpus disponibles |
| `corpus_filter` | Filtrer par critères (année, type, langue, source) |
| `corpus_to_csl` | Conversion Zotero JSON → CSL-JSON |
| `fetch_by_doi` | Récupération ciblée par DOI |
| `search_by_author` | Production complète d'un chercheur |
| `corpus_merge` | Fusion N corpus avec rapport |

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
**directement au chat/agent** le lien cliquable vers le(s) fichier(s) + le JSON
du corpus inline, en un seul retour (sous garde de volumétrie).

- ✅ **#28** — Helper `_build_corpus_tool_result` + dispatcher acceptant `str` / `list[ContentBlock]` / `tuple(content, structured)` (*CombinationContent* SDK)
- ✅ **#29** — `moisson_search` : `ResourceLink`(json/csl/stats) + `EmbeddedResource` Zotero inline + `structuredContent`
- ✅ **#30** — Généralisation : `corpus_filter`, `corpus_to_csl` (CSL primaire), `corpus_merge`, `search_by_author`
- ✅ **#31** — Seuil `INLINE_MAX_ITEMS` (défaut 300, env `MOISSON_INLINE_MAX_ITEMS`), aligné sur la garde 2 Mo de `read_resource`
- ✅ **#32** — `tests/unit/test_tool_result.py` : **27 tests PASS**
- ✅ **#33** — README (section « Retour MCP ») + CLAUDE.md + `sprint_05_report.md`
- ✅ **Correctif latent** — `read_resource` durci aux `AnyUrl` (le SDK passe un `AnyUrl`, pas une `str` : `uri.startswith` plantait en production ; round-trip `corpus://` désormais sûr)
- ✅ **Correctif revue** — registre `_EMITTED_RESOURCES` : les liens `corpus://` des fichiers écrits HORS de OUTPUT_DIR (`corpus_filter` à côté de la source, `corpus_to_csl`/`corpus_merge` avec `output_path` arbitraire) sont désormais résolus par `read_resource` (round-trip fiable, DoD #80)
- ✅ **Durcissement réseau** — `config.py` : `HTTP_TIMEOUT` devient un tuple `(connect=8, read=45)`, `HTTP_MAX_RETRIES=3`, backoff `1.0`, pas de sleep après la dernière tentative, tout surchargeable par env. Pire cas par source morte : **~262 s → ~27 s** ⇒ `moisson_search` ne dépasse plus le délai d'outil de Claude Desktop quand une source est lente/injoignable. Couvert par `tests/unit/test_http_resilience.py` (5 tests)

**5 outils producteurs** au retour enrichi : `moisson_search`, `corpus_filter`,
`corpus_to_csl`, `corpus_merge`, `search_by_author`. `fetch_by_doi` reste hors
périmètre (pas de fichier primaire). `outputSchema` écarté (masquerait les
erreurs ; cf. rapport).

**État final du projet** :

| Métrique | Valeur |
|----------|--------|
| Sources couvertes | **7** (HAL, Isidore, OpenEdition, Cairn, Persée, Theses.fr, Érudit) |
| Outils MCP exposés | **8** + 4 prompts + Resources `corpus://` (5 producteurs au retour enrichi lien+JSON) |
| Tests | **308** (303 non-integration + 5 integration network) — dont 32 Sprint 05 ; `tests/unit/` = 273 PASS en ~1,8 s hors ligne |
| Couverture mappers+utils | **78%** |
| Formats de sortie | Zotero JSON interne + CSL-JSON (Zotero import) |
| Retour MCP | Résumé texte + `ResourceLink` + `EmbeddedResource` (JSON inline ≤ seuil) + `structuredContent` |
| Performance | 21 s pour 4×500 items (workers=2), 1.2 s avec cache |
| Packaging | `pip install -e .` opérationnel, entry_point `moisson-shs` |
| Git | Initialisé branche `main` (pas de commit auto) |
| Documentation | README walkthrough + FAQ 17 questions + 5 rapports sprints |

### Commandes utiles

```bash
# Moisson
moisson-shs "votre requête" --max 500 --workers 2 --cache

# Setup MCP
moisson-shs setup --status        # état actuel
moisson-shs setup                 # installation
moisson-shs setup --dry-run       # aperçu sans modifier
moisson-shs setup --uninstall     # désinstallation propre

# Serveur MCP standalone (debug)
moisson-shs serve

# Tests
pytest tests/ -m "not integration" -q                          # 303 PASS (réseau requis pour 2 tests sprint_01)
pytest tests/unit/ -m "not integration" -q                     # 273 PASS en ~1,8 s, hors ligne
pytest tests/ -q                                                # 308 PASS (avec réseau)
pytest tests/ --cov=mappers --cov=utils --cov-report=term      # 78%
```

## Système de suivi des tâches

Les 27 tâches du backlog complet sont suivies via `TaskList` / `TaskUpdate`. Marquer `in_progress` au démarrage d'une tâche, `completed` à la fin (jamais en avance). Au démarrage d'une nouvelle session, faire un `TaskList` pour reprendre le contexte.

## Conventions techniques

- **Sortie** : tous les corpus produits vont dans `output/corpus_*.json`. Les payloads bruts par source vont dans `output/raw/<source>_<ts>.json` (audit). Ces dossiers sont à `.gitignore`.
- **Logs** : niveau INFO par défaut, DEBUG avec `--verbose`. Sur MCP (stdio), les logs vont sur stderr (stdout réservé au JSON-RPC).
- **Tests** : `tests/test_sprint_0X.py` par sprint, exécutables avec `python tests/test_sprint_0X.py` (mode standalone) ou `pytest`.
- **Pas de modification du config.py** sans mise à jour conjointe du report concerné — le fichier centralise les endpoints et constantes critiques.
- **Pas de commit git pour l'instant** — le projet n'est pas encore un repo (prévu Sprint 04, tâche #24).

## Lancer une moisson de test

```bash
source venv/bin/activate
python main.py "votre requête" --max 30 --verbose
```

Le corpus produit est immédiatement importable dans Zotero Desktop via Fichier > Importer.

## Lancer la suite de tests Sprint 01

```bash
source venv/bin/activate
python tests/test_sprint_01.py
```

Sortie attendue : `15 passés, 0 échoués`.
