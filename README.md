# moisson-shs — Moissonneur bibliographique SHS multi-sources

Moissonne en une commande **onze sources** bibliographiques majeures — sept en sciences humaines et sociales francophones, quatre internationales — et produit un corpus directement importable dans Zotero.

### Sources francophones (sans clé)

| Source | Couverture | Stratégie |
|--------|------------|-----------|
| **HAL** (archives-ouvertes.fr) | Production scientifique française | API Solr REST native |
| **Isidore** (Huma-Num) | Moteur SHS, indexe les autres | API REST native |
| **OpenEdition** | Journals + Books + Calenda + Hypothèses | Proxy Isidore + OAI optionnel |
| **Cairn.info** | Revues SHS commerciales francophones | Proxy Isidore + OAI optionnel |
| **Persée** | Périodiques SHS patrimoniaux français | Proxy Isidore + OAI optionnel |
| **Theses.fr** | Base nationale des thèses françaises | Proxy Isidore |
| **Érudit** | Revues SHS québécoises et canadiennes | Proxy Isidore + OAI optionnel |

### Sources internationales (REST natif, clés API)

| Source | Couverture | Clé | Spécificité |
|--------|------------|-----|-------------|
| **CrossRef** | Métadonnées d'éditeurs (~160M) | aucune (polite pool) | DOI, financements |
| **OpenAlex** | Graphe scientifique ouvert | **requise** (gratuite) | concepts, `cited_by_count`, OA |
| **Semantic Scholar** | Academic Graph | optionnelle | citations influentes, champs d'étude |
| **CORE** | Agrégateur OA (~290M) | **requise** (Bearer) | texte intégral + PDF |

Les sources internationales s'activent via `--sources international` (ou `all`), ou en les nommant. Configuration des clés : voir [`.env.example`](.env.example) et [docs/API_SPECS.md](docs/API_SPECS.md). Une source sans clé requise est **ignorée proprement** (la moisson des autres sources se poursuit).

Format de sortie : **CSL-JSON** (Citation Style Language JSON, importable Zotero) en parallèle d'un format Zotero JSON interne riche pour exploitation programmatique.

## Pour qui ?

Chercheurs et étudiants SHS qui ont besoin de constituer rapidement une bibliographie thématique exhaustive, suivre la production d'un auteur, ou maintenir une veille bibliographique sur un sujet.

Le moissonneur est aussi exposé comme **serveur MCP** : une fois configuré dans Claude Desktop (via `moisson-shs setup`), il devient interactif depuis l'interface de chat — vous demandez à Claude « Fais-moi la bibliographie de Patrick Charaudeau » et il appelle l'outil pour vous.

---

## Démarrage rapide (5 minutes)

### 1. Installation

```bash
# Cloner le dépôt
git clone https://github.com/amarlakel/moisson-shs
cd moisson-shs

# Créer un venv et installer
python3 -m venv venv
source venv/bin/activate         # macOS / Linux
# venv\Scripts\activate.bat       # Windows

pip install -e ".[mcp]"
```

### 2. Première moisson

```bash
moisson-shs "discours numérique" --max 100 --workers 2
```

Pendant ~30 secondes, le moissonneur interroge les 7 sources en parallèle, déduplique les doublons inter-sources, et produit deux fichiers dans `output/` :

| Fichier | Usage |
|---------|-------|
| `corpus_<requête>_<ts>.json` | Format Zotero interne, consommé par le MCP et l'analyse programmatique |
| `corpus_<requête>_<ts>.csl.json` | **CSL-JSON, importable directement dans Zotero Desktop** |
| `corpus_<requête>_<ts>_stats.txt` | Statistiques (sources, types, années, couverture DOI/résumé) |

### 3. Import dans Zotero

1. Ouvrir **Zotero Desktop**
2. Menu **Fichier > Importer**
3. Choisir **« Un fichier (BibTeX, RIS, Zotero RDF…) »**
4. Sélectionner le fichier **`corpus_*.csl.json`** (`.csl.json`, pas `.json`)
5. Cocher « Placer les éléments importés dans une nouvelle collection »

Zotero détecte automatiquement le format CSL JSON et crée la collection avec tous les items, les auteurs, les DOIs, les abstracts et les sources d'origine (préservées dans le champ `extra`).

### 4. (Optionnel) Configuration Claude Desktop

Pour utiliser le moissonneur depuis Claude Desktop en langage naturel :

```bash
moisson-shs setup
```

La commande détecte automatiquement votre OS, votre venv Python, et insère la section `moisson-shs` dans `claude_desktop_config.json` en préservant vos autres serveurs MCP.

Puis **quittez complètement Claude Desktop** (Cmd+Q sur macOS, fermer toutes les fenêtres) et **relancez-le**.

Dans une nouvelle conversation :

> Moissonne les sources SHS sur "discours numérique", plafond 200 items par source.

> Liste mes corpus disponibles.

> Fais-moi le profil bibliographique de Patrick Charaudeau.

> Analyse le corpus `output/corpus_X.json` : top auteurs, top revues, distribution chronologique.

---

## Options CLI complètes

### moisson-shs search (ou moisson-shs Q directement)

```bash
moisson-shs search "intelligence artificielle" \
    --max 500 \
    --sources hal isidore openedition cairn persee erudit theses_fr \
    --workers 2 \
    --cache \
    --from-date 2020 \
    --to-date 2024 \
    --enrich-oai \
    --verbose

# Inclure les sources internationales (clés requises pour OpenAlex/CORE)
moisson-shs search "AI regulation" --sources international --from-date 2022
moisson-shs search "discours numérique" --sources all --workers 2
```

| Option | Effet |
|--------|-------|
| `--max N` | Plafond par source (défaut : 1000) |
| `--sources S1 S2 …` | Sources et/ou **groupes** : noms individuels, ou `francophone` (défaut), `international`, `all` |
| `--workers N` | Parallélisme — recommandé : 2 (×2 à ×3 plus rapide) |
| `--cache` | Active le cache HTTP local — re-requête identique en <30s |
| `--from-date YYYY` | Filtre temporel inclusif (HAL natif Solr, autres post-pagination) |
| `--to-date YYYY` | Idem haut |
| `--enrich-oai` | Enrichissement OAI sur OpenEdition / Cairn / Persée / Érudit (plus lent mais métadonnées plus riches) |
| `--no-dedup` | Désactive la déduplication (uniquement pour audit) |
| `--output PATH` | Répertoire de sortie (défaut : `./output`) |
| `--verbose` | Logs DEBUG |

### moisson-shs setup

```bash
moisson-shs setup                    # installation par défaut
moisson-shs setup --output-dir ~/corpus_shs
moisson-shs setup --cache            # cache HTTP par défaut côté serveur
moisson-shs setup --dry-run          # aperçu sans modifier
moisson-shs setup --status           # état actuel
moisson-shs setup --uninstall        # désinstaller
```

### moisson-shs serve

Lance le serveur MCP en mode **stdio** (défaut, Claude Desktop local). Utile pour debug ou intégration manuelle non-Claude.

```bash
moisson-shs serve
```

### moisson-shs serve --http (serveur MCP distant)

Lance le **même** serveur (8 outils inchangés) en mode **distant HTTP** (Streamable HTTP), pour un agent accédant via HTTPS authentifié — sans installation locale.

```bash
moisson-shs token generate            # secret affiché 1× → MOISSON_API_TOKEN
export MOISSON_API_TOKEN=<le secret>
moisson-shs serve --http --host 127.0.0.1 --port 8000
```

En mode HTTP : **auth Bearer obligatoire**, chemins **confinés** sous `OUTPUT_DIR`, retour par **URLs de téléchargement** (jamais de chemin disque ni de gros JSON inline), opérations longues robustes (progression, deadline, anti-DoS), observabilité (logs JSON, audit, `/metrics`).

➡️ **Déploiement Docker + TLS (Caddy)** : [DEPLOY.md](DEPLOY.md). **Guide complet** (jetons, config client, variables d'env, modèle de menace) : [REMOTE.md](REMOTE.md).

---

## Outils MCP exposés

Une fois `moisson-shs setup` exécuté, Claude Desktop dispose de **8 outils** + **4 prompts** :

### Outils

| Outil | Usage typique en conversation |
|-------|-------------------------------|
| `moisson_search` † | « Moissonne sur la requête X » |
| `corpus_analyze` | « Analyse le corpus X » |
| `corpus_list` | « Quels corpus j'ai produits ? » |
| `corpus_filter` † | « Filtre le corpus X aux articles 2020-2024 en français » |
| `corpus_to_csl` † | « Convertis le corpus X en CSL-JSON » |
| `fetch_by_doi` | « Ajoute le DOI 10.3917/X au corpus Y » |
| `search_by_author` † | « Production complète de Patrick Charaudeau » |
| `corpus_merge` † | « Fusionne ces 3 corpus en gérant les doublons » |

> † Outils **producteurs de fichiers** : renvoient le [retour MCP enrichi](#retour-mcp--lien--json) (lien cliquable + JSON du corpus inline + `structuredContent`).

### Prompts pré-configurés

| Prompt | Cas d'usage |
|--------|-------------|
| `bibliographie-thematique` | Bibliographie sur un thème, multi-sources + analyse |
| `profil-chercheur` | Production exhaustive d'un chercheur (alimente la skill `ProfilBiblio`) |
| `veille-mensuelle` | Veille fenêtrée sur une période et un thème |
| `etat-de-l-art` | État de l'art sur les 5 dernières années avec sous-thèmes |

### Resources

Chaque corpus du `OUTPUT_DIR` est exposé comme URI `corpus://<filename>`, lisible directement par Claude sans appel d'outil.

### Retour MCP : lien + JSON

Les **5 outils producteurs de fichiers** (`moisson_search`, `corpus_filter`,
`corpus_to_csl`, `corpus_merge`, `search_by_author`) ne renvoient pas qu'un
résumé texte : ils émettent un **retour enrichi** en un seul appel.

| Élément du retour | Rôle |
|-------------------|------|
| `TextContent` (résumé) | Toujours présent ; conserve chemins absolus + URIs `corpus://` même si le client n'affiche pas les autres blocs. |
| `ResourceLink` (un par fichier) | Le **lien cliquable** vers chaque fichier produit (corpus JSON, CSL-JSON, statistiques). |
| `EmbeddedResource` | Le **JSON du corpus** remis directement à l'agent — Zotero interne pour la plupart des outils, **CSL-JSON** pour `corpus_to_csl`. |
| `structuredContent` | Métadonnées machine : `query`, `by_source`, `item_count`, `files`, `resources`, `inline_json`, `inline_threshold`… |

**Garde anti-saturation** : le JSON n'est inliné que si le corpus contient
`≤ MOISSON_INLINE_MAX_ITEMS` items (**défaut 300**, surchargeable par variable
d'environnement). Au-delà, seul le `ResourceLink` est émis (le résumé indique
la raison), et l'agent peut suivre le lien via la resource `corpus://`, où
`read_resource` applique en plus sa propre troncature à 2 Mo.

```bash
# Abaisser le seuil d'inlining (ex. corpus volumineux)
export MOISSON_INLINE_MAX_ITEMS=100
```

> Les clients MCP qui n'affichent pas les blocs `ResourceLink`/`EmbeddedResource`
> retrouvent toute l'information (chemins + URIs) dans le résumé texte.

---

## Architecture

```
moisson_shs/
├── cli.py                       # Entry point unifié (setup, serve, search)
├── main.py                      # Orchestrateur CLI de moisson
├── mcp_server.py                # Serveur MCP pour Claude Desktop
├── setup_claude_desktop.py      # Installation automatique
├── config.py                    # Endpoints et paramètres globaux
│
├── connectors/                  # Couche d'accès aux 7 sources
│   ├── registry.py              # Registre central — point d'extension
│   ├── base.py                  # Interface abstraite Connector
│   ├── hal.py                   # HAL Solr REST
│   ├── isidore.py               # Isidore REST + filtrage par URL
│   ├── openedition.py           # Proxy Isidore + OAI optionnel
│   ├── cairn.py                 # Proxy Isidore + OAI optionnel
│   ├── persee.py                # Proxy Isidore + OAI optionnel
│   ├── theses_fr.py             # Proxy Isidore uniquement
│   └── erudit.py                # Proxy Isidore + OAI optionnel
│
├── mappers/                     # Conversion vers schémas Zotero / CSL
│   ├── zotero_schema.py
│   ├── zotero_schema_loader.py  # Schéma officiel api.zotero.org
│   ├── doc_type_mapping.py
│   ├── hal_mapper.py
│   ├── isidore_mapper.py
│   ├── oai_mapper.py
│   └── csl_exporter.py          # Zotero JSON → CSL-JSON (BibTeX JSON)
│
├── utils/
│   ├── http.py                  # Client HTTP + retry + cache
│   ├── cleaner.py               # Nettoyage chaînes
│   ├── deduplication.py         # Multi-stratégies DOI/URI/empreinte
│   ├── lookups.py               # fetch_by_doi + search_by_author
│   └── corpus_ops.py            # merge_corpora
│
├── tests/                       # 308 tests (unit + integration)
│   ├── conftest.py
│   ├── fixtures/                # Payloads réels API capturés
│   └── unit/                    # Tests par module
│
└── output/                      # Corpus produits (gitignored)
```

---

## Format CSL-JSON

Chaque item du fichier `corpus_*.csl.json` suit la spécification [Citation Style Language](https://docs.citationstyles.org/) :

```json
{
  "type": "article-journal",
  "id": "10.3917/comla.204.0045",
  "title": "Pour une pragmatique énonciative des discours numériques",
  "author": [
    {"family": "Lakel", "given": "Amar"},
    {"family": "Cormerais", "given": "Franck"}
  ],
  "issued": {"date-parts": [[2020]]},
  "container-title": "Communication & langages",
  "volume": "204",
  "issue": "2",
  "page": "45-67",
  "DOI": "10.3917/comla.204.0045",
  "ISSN": "0336-1500",
  "URL": "https://hal.science/hal-04123456",
  "abstract": "Cet article propose...",
  "language": "fr",
  "keyword": "discours numérique, pragmatique",
  "note": "Sources: HAL, Isidore\nHAL ID: hal-04123456\nOpen Access: yes"
}
```

Le champ `note` préserve les métadonnées propres au moissonneur (sources d'origine, identifiants HAL/OAI, domaines disciplinaires, ORCID, statut open access) au format clé:valeur. Zotero les ré-importe automatiquement dans son champ `extra`.

---

## Déduplication

Le moissonneur applique une déduplication multi-stratégies par priorité décroissante de fiabilité :

1. **DOI** (le plus fiable) — normalisation casse et préfixes URL
2. **URI canonique** — HAL ID, OAI Identifier, Isidore URI
3. **URL normalisée** — minuscules, sans slash final
4. **Empreinte SHA-256** — sur titre normalisé + premier auteur + année

Quand un doublon est détecté, les items sont fusionnés en préservant les valeurs non vides de chaque source. Le champ `extra` final liste toutes les sources d'origine, ce qui est très utile pour tracer la provenance.

---

## Dépannage rapide

Voir la [FAQ détaillée](docs/FAQ.md).

| Symptôme | Solution rapide |
|----------|-----------------|
| Erreur 429 (Too Many Requests) | Le client applique un backoff exponentiel. Si persistant, augmenter `REQUEST_DELAY` dans `config.py` |
| Cairn / OpenEdition retournent 0 items | Vérifier que Isidore répond (proxy). Voir `output/raw/isidore_*.json` |
| Le serveur MCP n'apparaît pas dans Claude | Quitter et relancer **entièrement** Claude Desktop ; vérifier `~/Library/Logs/Claude/mcp*.log` |
| Le fichier `.json` n'est pas accepté par Zotero | Importer le **`.csl.json`** (format CSL-JSON standard) |

---

## Tests

```bash
pip install -e ".[dev,mcp]"
pytest tests/ -v
pytest tests/ -m "not integration" --cov=mappers --cov=utils
```

308 tests (303 hors réseau + 5 d'intégration réseau), 78% de couverture sur `mappers/` et `utils/`. La couche MCP (retour enrichi lien + JSON) est couverte par `tests/unit/test_tool_result.py` (27 tests) ; la résilience réseau par `tests/unit/test_http_resilience.py` (5 tests).

---

## Limitations connues

- **Cairn** : pas de texte intégral exposé via API publique, seules les métadonnées sont récupérées. TDM nécessite une convention.
- **OpenEdition** : texte intégral TEI réservé aux institutions partenaires (corpus de métadonnées toujours libre).
- **Theses.fr** : API REST publique retirée en 2026, fallback proxy Isidore.
- **HAL** : la syntaxe Solr exige d'échapper les caractères spéciaux (`+ - && || ! ( ) { } [ ] ^ " ~ * ? :`).
- **Déduplication** : ne résout que les doublons exacts ou quasi-exacts ; les variantes de titre lourdes peuvent persister.

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
- Schéma Zotero : https://api.zotero.org/schema
- CSL-JSON : https://docs.citationstyles.org/
- Protocole MCP : https://modelcontextprotocol.io
