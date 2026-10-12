# Historique de moisson_shs : extensions et sprints 01 à 05

Sections retirées de `CLAUDE.md` et `AGENTS.md`, reprises telles quelles. Les chiffres (sources, outils, tests, durées) et les commandes sont ceux de leur date ; état et commandes actuels : `AGENTS.md`, section « Où nous en sommes » et « Commandes utiles ». Détail de chaque chantier : son rapport dans `.claude/dev/`.

### Extension internationale ✅ TERMINÉE (2026-06-16)

Ajout de **4 sources internationales** (CrossRef, OpenAlex, Semantic Scholar,
CORE) → **11 sources** au total. Plan :
[.claude/dev/PLAN_EXTENSION_INTERNATIONALE_moisson-shs.md](../.claude/dev/PLAN_EXTENSION_INTERNATIONALE_moisson-shs.md)
· Rapport :
[.claude/dev/PLAN_EXTENSION_INTERNATIONALE_report.md](../.claude/dev/PLAN_EXTENSION_INTERNATIONALE_report.md).

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
[.claude/dev/EXTENSION_GOOGLE_SCHOLAR_report.md](../.claude/dev/EXTENSION_GOOGLE_SCHOLAR_report.md).

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

- [tests/test_sprint_01.py](../tests/test_sprint_01.py) — suite de non-régression (**30/30 PASS**)
- [mappers/csl_exporter.py](../mappers/csl_exporter.py) — exporteur CSL-JSON
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

**Nouveau module** : [mappers/zotero_schema_loader.py](../mappers/zotero_schema_loader.py) — chargement du schéma officiel `https://api.zotero.org/schema`.

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
- ✅ **#33** — README (section « Retour MCP ») + CLAUDE.md + `sprint_05_report.md`
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
