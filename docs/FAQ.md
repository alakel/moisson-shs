# FAQ — Foire aux questions

## Sources internationales et clés API

### Comment moissonner les sources internationales (CrossRef, OpenAlex, Semantic Scholar, CORE) ?

Elles ne sont pas incluses par défaut. Les activer par groupe ou par nom :

```bash
moisson-shs search "AI regulation" --sources international   # les 4
moisson-shs search "discours numérique" --sources all        # FR + internationales
moisson-shs search "LLM" --sources crossref openalex         # à la carte
```

En MCP : `sources: ["international"]` ou `["all"]` dans `moisson_search`.

### Quelles clés faut-il, et comment les fournir ?

- **CrossRef** : aucune clé.
- **OpenAlex** : clé gratuite **requise** (`OPENALEX_API_KEY`).
- **Semantic Scholar** : clé **optionnelle** (`SEMANTIC_SCHOLAR_API_KEY`) ; sans elle, pool partagé plus lent.
- **CORE** : clé Bearer **requise** (`CORE_API_KEY`).

Trois façons de les fournir : un fichier `.env` à la racine (voir `.env.example` ;
nécessite `python-dotenv`), des variables d'environnement du shell, ou le bloc
`env` de `claude_desktop_config.json` pour le MCP (cf. `MCP_INSTALL.md`).
**Ne jamais committer de clé** — `.env` est git-ignoré.

### Que se passe-t-il si une clé requise est absente ?

La source concernée est **ignorée proprement** : un message d'erreur est journalisé,
mais la moisson des autres sources se poursuit et le corpus partiel est écrit.
Idem si le budget OpenAlex est épuisé en cours de moisson (arrêt propre de cette
source uniquement).

### Pourquoi mon corpus rétrécit-il beaucoup avec `--sources all` ?

C'est attendu : un même article publié est souvent présent à la fois dans une
source francophone (Cairn, HAL…) et dans CrossRef/OpenAlex. La **déduplication par
DOI** fusionne ces doublons en un seul item, en agrégeant les métadonnées (compte
de citations issu d'OpenAlex/Semantic Scholar, meilleur résumé selon une priorité
de source). Le fichier `_stats.txt` détaille les recouvrements par paire de sources.

## Format et import

### Pourquoi deux fichiers `.json` et `.csl.json` à chaque moisson ?

Le moissonneur produit deux formats avec des usages distincts :

- **`corpus_*.json`** — format Zotero JSON interne, riche en métadonnées spécifiques au moissonneur (sources d'origine, identifiants HAL/OAI/ORCID, domaines disciplinaires, statut open access). Ce fichier est consommé par le serveur MCP et les outils d'analyse programmatique (`corpus_analyze`, `corpus_filter`, etc.).
- **`corpus_*.csl.json`** — format CSL-JSON (Citation Style Language JSON) standard, **importable directement dans Zotero Desktop**. C'est le format universel reconnu par Zotero, Pandoc, citeproc, etc.

**Pour importer dans Zotero : toujours choisir le `.csl.json`.** L'autre format ne sera pas reconnu par l'interface Import de Zotero Desktop.

### J'obtiens « fichier non reconnu » à l'import dans Zotero

Vérifier que vous importez le `.csl.json` et non le `.json` sans suffixe. Le format Zotero JSON natif (utilisé par l'API web Zotero) **n'est pas reconnu** par l'interface Import de Zotero Desktop, qui attend CSL-JSON, BibTeX, RIS ou Zotero RDF.

### Comment convertir un corpus existant en CSL-JSON ?

Depuis Claude Desktop (avec le MCP configuré) :
> Convertis le corpus `output/corpus_X.json` en CSL-JSON

Ou en ligne de commande :
```bash
python -c "
import json
from pathlib import Path
from mappers.csl_exporter import export_corpus_to_csl
src = Path('output/corpus_X.json')
items = json.loads(src.read_text())
csl_items = export_corpus_to_csl(items)
Path('output/corpus_X.csl.json').write_text(
    json.dumps(csl_items, ensure_ascii=False, indent=2)
)
"
```

---

## Couverture et sources

### Pourquoi Cairn ne renvoie pas le texte intégral ?

Cairn n'expose que les **métadonnées** via OAI-PMH publiquement (titre, auteurs, abstract, références). Le texte intégral est protégé par les conditions de Cairn.info et nécessite une convention spécifique (TDM, accès institutionnel via licence Couperin) pour être moissonné.

Pour le full-text Cairn :
- Si vous êtes affilié à une institution abonnée, vous pouvez télécharger les PDF un par un depuis l'interface Cairn.
- Pour du text-mining systématique, contacter `contact@cairn.info` pour discuter d'une convention.

Le corpus produit reste **utile pour l'analyse bibliométrique** (qui publie, où, avec qui, sur quoi, sur quelle période).

### Pourquoi Theses.fr renvoie peu d'items ?

L'API publique de Theses.fr a été retirée en 2026. Le moissonneur utilise désormais Isidore comme proxy (Isidore moissonne Theses.fr régulièrement). La couverture reste bonne pour les thèses récentes mais peut être incomplète sur les thèses très anciennes ou très récentes (délai d'indexation Isidore).

Alternatives :
- Pour une recherche thèse précise : utiliser directement l'interface web `theses.fr` ou Sudoc.
- Pour une moisson exhaustive d'un labo : passer par HAL en filtrant `docType_s:THESE`.

### Comment construire une requête HAL avec opérateurs ?

HAL utilise la syntaxe Solr :

| Opérateur | Effet | Exemple |
|-----------|-------|---------|
| `" "` | Phrase exacte | `"intelligence artificielle"` |
| `AND` | ET logique (implicite par défaut) | `discours AND numérique` |
| `OR` | OU logique | `numérique OR digital` |
| `-` ou `NOT` | Exclusion | `discours -fiction` |
| `*` | Joker | `numéri*` matche numérique, numérisation, etc. |
| `~N` | Proximité (N mots) | `"discours numérique"~5` |
| `( )` | Groupement | `(IA OR "intelligence artificielle") AND société` |

**Caractères spéciaux à échapper** : `+ - && || ! ( ) { } [ ] ^ " ~ * ? : \` avec `\`.

Recherche par champ Solr :
- `authFullName_s:"Patrick Charaudeau"` — auteur exact
- `journalTitle_s:"Communication & langages"` — revue
- `producedDateY_i:[2020 TO 2024]` — plage d'années
- `docType_s:ART` — articles uniquement (codes : ART, COMM, OUV, COUV, THESE, HDR, REPORT)

### Comment moissonner toute la production d'un auteur ?

Via le serveur MCP (recommandé) :
> Fais-moi la production complète de Patrick Charaudeau

Via le CLI directement avec l'outil dédié :
```python
from utils.lookups import search_by_author
items = search_by_author("Patrick Charaudeau", max_per_source=500)
```

Pour une précision maximale, fournir l'identifiant HAL ou l'ORCID :
```python
items = search_by_author(
    "Patrick Charaudeau",
    idhal="patrick-charaudeau",   # plus précis que le nom
    orcid="0000-0002-XXXX-XXXX",
)
```

---

## Dédoublonnage

### Pourquoi des doublons subsistent malgré la déduplication ?

Le moissonneur déduplique par priorité décroissante de fiabilité :

1. **DOI** — quasi infaillible
2. **URI canonique** — HAL ID, OAI Identifier
3. **URL normalisée**
4. **Empreinte SHA-256** — sur `titre normalisé + premier auteur + année`

Les doublons résiduels viennent typiquement de :

- **Variations lourdes de titre** : « Une étude de X » vs « Étude sur X : approches théoriques » → empreintes différentes
- **Premier auteur différent** entre versions : article en version preprint vs version finale avec ordre d'auteurs ajusté
- **Année différente** : dépôt HAL en 2019 mais publication officielle en 2020
- **Item sans DOI ni HAL ID** : impossible de comparer fiablement, l'empreinte est notre dernier recours

**Mitigations** :
- Augmenter la fenêtre temporelle (`--from-date` / `--to-date`) pour éviter qu'un même item ait des dates différentes selon les sources.
- Après import dans Zotero, utiliser **Édition > Trouver des doublons** pour une seconde passe.
- Activer `--enrich-oai` qui peut renseigner un DOI manquant pour les items OpenEdition / Cairn.

### Comment savoir d'où vient un item après fusion ?

Le champ `extra` de chaque item liste toutes les sources d'origine :
```
Sources: HAL, Isidore, OpenEdition
HAL ID: hal-04123456
Isidore URI: 20.500.13089/abc
OAI Identifier: oai:revues.org:lidil/6033
```

Si vous voyez `Sources: HAL, Isidore` sur un item, cela signifie qu'il a été trouvé à la fois dans HAL et dans Isidore, puis fusionné.

---

## Performance et cache

### Le moissonnage est-il long ?

Pour 4 sources × 500 items, comptez ~3-5 minutes en mode séquentiel. Avec `--workers 2` (parallélisme), ~1-2 minutes. Avec `--cache` activé lors d'une re-requête identique, ~30 secondes.

### Comment activer le cache ?

```bash
moisson-shs search "X" --cache
```

ou via variable d'environnement :
```bash
export MOISSON_CACHE=1
export MOISSON_CACHE_TTL=86400      # 24h en secondes (défaut)
export MOISSON_CACHE_DIR=~/.cache/moisson_shs/http
```

Le cache est persistant entre sessions et utilise SQLite via `requests-cache`. Sa taille reste raisonnable (quelques Mo pour des requêtes typiques).

### Où sont stockés les fichiers de cache ?

| OS | Emplacement par défaut |
|----|----------------------|
| macOS / Linux | `~/.cache/moisson_shs/http.sqlite` |
| Windows | `%USERPROFILE%\.cache\moisson_shs\http.sqlite` |

Pour nettoyer le cache : `rm -rf ~/.cache/moisson_shs/`.

---

## Claude Desktop / MCP

### Le serveur MCP timeout dans Claude Desktop sur les grosses moissons

Claude Desktop a un timeout par défaut de 60 secondes par appel d'outil. Une moisson de 1000 items × 7 sources peut prendre plusieurs minutes et dépasser cette limite.

**Workarounds** :

1. Lancer la moisson en CLI puis utiliser `corpus_analyze` côté Claude :
   ```bash
   moisson-shs "X" --max 2000 --workers 2 --cache
   ```
   Puis dans Claude : « Analyse le corpus `output/corpus_X_*.json` »

2. Restreindre les sources : `--sources hal isidore` exclut les sources proxy plus lentes.

3. Utiliser `max_per_source` plus modéré dans l'appel MCP (200-500 au lieu de 1000+).

### Comment vérifier que le serveur MCP est bien installé ?

```bash
moisson-shs setup --status
```

Affiche l'état actuel : présence dans `claude_desktop_config.json`, chemins configurés, autres serveurs détectés.

### Le serveur MCP ne démarre pas après `setup`

1. Vérifier les logs Claude Desktop :
   - macOS : `~/Library/Logs/Claude/mcp*.log`
   - Windows : `%APPDATA%\Claude\logs\mcp*.log`
   - Linux : `~/.config/Claude/logs/mcp*.log`
2. Tester le serveur manuellement :
   ```bash
   moisson-shs serve
   ```
   (il doit rester en attente sur stdin sans erreur)
3. Vérifier que le Python pointé dans `command` est bien celui où `mcp` et les dépendances sont installées :
   ```bash
   moisson-shs setup --status
   # Reportez la valeur "command" et testez :
   /chemin/vers/python -c "import mcp; print('OK')"
   ```

### Comment désinstaller le serveur MCP ?

```bash
moisson-shs setup --uninstall
```

La commande retire uniquement la section `moisson-shs` du `claude_desktop_config.json` ; les autres serveurs MCP sont préservés. Une sauvegarde `.bak` est créée.

---

## Données et confidentialité

### Le moissonneur envoie-t-il des données quelque part ?

**Non**. Le moissonneur fait uniquement des requêtes vers les API publiques des 7 sources (HAL, Isidore, OpenEdition, Cairn, Persée, Theses.fr, Érudit) + Crossref pour `fetch_by_doi`. Aucune télémétrie, aucun reporting, aucune base de données externe.

Tous les corpus restent sur votre disque local dans `output/`.

### Le serveur MCP envoie-t-il les corpus à Anthropic ?

Le serveur MCP s'exécute **localement** sur votre machine. Il dialogue avec Claude Desktop via stdin/stdout. Les corpus restent sur votre disque ; Claude Desktop ne voit que ce que vous lui demandez d'analyser, dans le contexte de la conversation.

Cf. politique Anthropic sur le traitement des conversations Claude Desktop pour les détails côté Anthropic.

---

## Développement

### Comment ajouter une nouvelle source ?

1. Créer `connectors/<nom>.py` héritant de `connectors.base.Connector`. Implémenter la méthode `search(query, max_results, **kwargs)`.
2. (Si nécessaire) Créer un mapper dédié `mappers/<nom>_mapper.py`.
3. Importer la classe dans `connectors/registry.py` et l'ajouter à `CONNECTORS`. Si la source utilise Isidore comme proxy, l'ajouter à `GROUP_PROXY_SOURCES`.
4. Mettre à jour `SOURCE_DESCRIPTIONS` pour l'aide CLI.

Le main.py, mcp_server.py et l'aide CLI s'adaptent automatiquement — pas d'autre modification nécessaire.

### Comment lancer les tests ?

```bash
pip install -e ".[dev,mcp]"
pytest tests/ -v                           # tous les tests
pytest tests/ -m "not integration"         # sans réseau
pytest tests/ --cov=mappers --cov=utils    # avec couverture
```

### Comment contribuer ?

PRs bienvenues sur https://github.com/amarlakel/moisson-shs/pulls. Avant la PR :

1. `pytest tests/ -m "not integration"` doit passer
2. La couverture sur `mappers/` et `utils/` doit rester ≥ 70%
3. Pour une nouvelle source, fournir une fixture réelle anonymisée dans `tests/fixtures/`

---

## Encore une question ?

Ouvrir une issue : https://github.com/amarlakel/moisson-shs/issues
