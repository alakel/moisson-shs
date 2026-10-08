# Installation du serveur MCP moisson-shs dans les clients MCP

Ce document décrit l'installation du serveur MCP **local** (transport stdio), qui expose le moissonneur aux clients MCP (Claude Desktop, Claude Code, OpenAI Codex, Gemini CLI, Antigravity, Hermes Agent) sous la forme de douze outils, quatre prompts et des ressources `corpus://`. Pour un serveur **distant** (HTTPS authentifié), voir [REMOTE.md](REMOTE.md) et [DEPLOY.md](DEPLOY.md).

## Prérequis

- Python 3.10 ou supérieur.
- Un client MCP : Claude Desktop, Claude Code, OpenAI Codex (application ChatGPT, CLI ou IDE), Gemini CLI, Antigravity ou Hermes Agent (macOS, Windows ou Linux).
- Le projet `moisson_shs/` sur le poste.
- Un accès Internet vers les API des sources (HAL, Isidore, et, selon les sources demandées, OAI-PMH d'OpenEdition, Cairn, Persée et Érudit, CrossRef, OpenAlex, Semantic Scholar, CORE, SerpAPI).

## Étape 1. Installation des dépendances

```bash
cd /chemin/vers/moisson_shs
python -m venv venv
source venv/bin/activate          # Linux / macOS (Windows : venv\Scripts\activate.bat)
pip install -e ".[mcp]" -c constraints.txt
```

- L'extra `[mcp]` installe le SDK MCP officiel (paquet `mcp`), borné à **`mcp>=1.27,<2`**. La série 2 retire les décorateurs `Server.list_tools`, `call_tool` et `list_resources` qu'utilise le serveur : sans la borne, `import mcp_server` échoue avec `AttributeError: 'Server' object has no attribute 'list_tools'`.
- `constraints.txt` fige les versions testées de toutes les dépendances (la CI et l'image Docker l'utilisent aussi).
- Si le dossier du projet est déplacé après l'installation, relancer la même commande `pip install` : l'entrée `venv/bin/moisson-shs` garde sinon l'ancien chemin.

## Étape 2. Installation automatique (recommandée)

```bash
moisson-shs setup
```

**En terminal**, la commande affiche les clients MCP connus, ceux détectés sur la machine déjà cochés :

```
Clients MCP (cochés : ceux qui seront traités) :
  [x] 1. Claude Desktop                                déclaré · clés 4/4
  [x] 3. OpenAI Codex (application ChatGPT, CLI, IDE)  déclaré · clés 1/4
  [ ] 7. Hermes Agent (Nous Research)                  non détecté
Entrée = valider · numéros (ex. « 3 4 ») = cocher ou décocher · tous · aucun · q = quitter
```

Les clés d'API sont ensuite demandées **une seule fois** (saisie masquée ; Entrée vide : garder la valeur déjà déclarée) et inscrites chez chaque client retenu. Une clé vide chez un client est complétée par la valeur déjà déclarée chez les autres clients, si elle est la même partout ; si elle diffère, rien n'est mis en commun et setup le signale, sans afficher de valeur. **Hors terminal** (script), sans `--client`, seul Claude Desktop est configuré, comme avant.

Pour chaque client, setup :
- vérifie que l'application n'est pas ouverte (Claude Desktop, l'application ChatGPT/Codex et Hermes réécrivent leur fichier) : en terminal, il propose de la quitter, de continuer ou de sauter ; hors terminal, le client est sauté sauf `--force` ;
- fait une sauvegarde horodatée `<fichier>.moisson-bak-AAAAmmjj-HHMMSS` (les cinq plus récentes sont gardées), puis écrit de façon atomique ;
- contrôle que rien d'autre que l'entrée `moisson-shs` ne change dans le fichier ; sinon (fichier JSON avec commentaires, mise en forme non reproductible, bibliothèque absente), il n'écrit pas et affiche un **extrait à coller**, clés remplacées par des marqueurs `<NOM_DE_LA_CLÉ>` ;
- passe en 0600 un fichier lisible par d'autres comptes qui reçoit des clés ;
- conserve les variables `env` déjà déclarées (un `MOISSON_OUTPUT_DIR` ou un `MOISSON_KEY_FALLBACK` posés à la main).

Options utiles : `--client` (liste d'ids ou d'alias, ou `all` pour tous les clients détectés), `--client-config ID=CHEMIN`, `--output-dir`, `--dry-run` (aperçu : entrée masquée et noms des autres serveurs, jamais leur contenu), `--status` (état de chaque client : déclaration, chemins, délais, clés), `--uninstall`, `--force`, et les options de clés décrites plus bas.

### Clients pris en charge

| Client (id, alias) | Fichier (macOS, Linux) | Écriture | À savoir |
|---|---|---|---|
| Claude Desktop (`claude-desktop`, `desktop`) | `~/Library/Application Support/Claude/claude_desktop_config.json` (Windows `%APPDATA%\Claude\`, Linux `~/.config/Claude/`) | JSON `mcpServers` | Quitter l'application (Cmd+Q) avant, la relancer après |
| Claude Code (`claude-code`, `code`) | `~/.claude.json` (lu seulement) | `claude mcp add-json --scope user` | Jamais `.mcp.json` (versionné) ; clés visibles dans la liste des processus le temps de l'appel ; relancer les sessions, vérifier avec `/mcp` |
| OpenAI Codex (`codex`, `chatgpt`) | `$CODEX_HOME/config.toml` ou `~/.codex/config.toml` (commun à l'application ChatGPT, au CLI et à l'IDE) | TOML `[mcp_servers.moisson-shs]` (tomlkit) | Ne transmet au serveur que le bloc `env` ; délais relevés : `startup_timeout_sec = 60`, `tool_timeout_sec = 960` |
| Gemini CLI (`gemini-cli`, `gemini`) | `~/.gemini/settings.json` | JSON `mcpServers` | Les serveurs ne se connectent que dans un dossier de confiance (`gemini trust`) ; `timeout = 960000` ms |
| Antigravity CLI (`antigravity`, `agy`) | `~/.gemini/config/mcp_config.json` | JSON `mcpServers` | Remplace Gemini CLI pour les comptes gratuits ; chemin d'après la documentation de Google |
| Antigravity IDE (`antigravity-ide`) | `~/.gemini/antigravity/mcp_config.json` | JSON `mcpServers` | Relancer l'IDE |
| Hermes Agent (`hermes`, `hermes-agent`) | `$HERMES_HOME/config.yaml` ou `~/.hermes/config.yaml` (Windows `%LOCALAPPDATA%\hermes\`) | YAML `mcp_servers` (ruamel.yaml) | `connect_timeout = 120`, `timeout = 960` ; `/reload-mcp` dans la session, la passerelle relit le fichier en une minute environ |

Les délais ne sont jamais abaissés : 960 s couvre l'échéance maximale de `moisson_search` (900 s) ; c'est le serveur qui borne la durée, pas le client. Sans `tomlkit` ou `ruamel.yaml` (extra `[mcp]`), setup affiche l'extrait à coller pour Codex ou Hermes.

Passer directement à l'étape 5.

## Étape 3. Test du serveur en mode autonome (installation manuelle)

```bash
python mcp_server.py        # ou : moisson-shs serve
```

Le processus attend sur stdin ; stdout est réservé au protocole JSON-RPC, les journaux vont sur stderr :

```
[INFO] moisson_mcp: Démarrage serveur MCP moisson-shs
[INFO] moisson_mcp: OUTPUT_DIR = /chemin/vers/moisson_shs/output
```

Interrompre avec Ctrl+C.

## Étape 4. Configuration manuelle de Claude Desktop

Le fichier `claude_desktop_config.json` se trouve à :

- **macOS** : `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows** : `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux** : `~/.config/Claude/claude_desktop_config.json`

Ajouter l'entrée `moisson-shs` dans `mcpServers` (sans écraser les autres serveurs) :

```json
{
  "mcpServers": {
    "moisson-shs": {
      "command": "/chemin/vers/moisson_shs/venv/bin/python",
      "args": ["/chemin/vers/moisson_shs/mcp_server.py"],
      "env": {
        "MOISSON_OUTPUT_DIR": "/chemin/vers/output"
      }
    }
  }
}
```

- `command` pointe vers le Python du venv où `mcp` et les dépendances sont installés, pas vers le Python système.
- `args` contient le chemin absolu de `mcp_server.py`.
- `MOISSON_OUTPUT_DIR` (optionnel) fixe le répertoire des corpus ; par défaut `moisson_shs/output/`.

### Clés API (optionnel)

Les sources internationales ne sont moissonnées que demandées (`sources: ["international"]`, `["all"]`, ou nommées). Google Scholar, facturé à la requête, n'appartient à aucun groupe et doit être nommé (`["scholar"]` ou `["google_scholar"]`). Les clés passent par le bloc `env` :

```json
"moisson-shs": {
  "command": "/chemin/venv/bin/python",
  "args": ["/chemin/mcp_server.py"],
  "env": {
    "MOISSON_OUTPUT_DIR": "/chemin/output",
    "OPENALEX_API_KEY": "…",
    "SEMANTIC_SCHOLAR_API_KEY": "…",
    "CORE_API_KEY": "…",
    "SERPAPI_API_KEY": "…"
  }
}
```

- **OpenAlex** : clé gratuite **requise** — https://openalex.org/settings/api
- **Semantic Scholar** : clé **optionnelle** (sans clé : pool partagé, souvent en 429) ; en mode bibliographie, trié par nombre de citations, pas par pertinence — https://www.semanticscholar.org/product/api
- **CORE** : clé **requise** (sur inscription) — https://core.ac.uk/services/api
- **CrossRef** : aucune clé (pool poli par l'adresse `MOISSON_CONTACT_EMAIL`).
- **SerpAPI (Google Scholar)** : clé **requise** — https://serpapi.com/manage-api-key — et seule source **facturée à la requête** (un crédit par page de vingt résultats ; offre à 5 000 requêtes par mois, partagée avec le projet MyWebIntelligence). Plafonds : `MOISSON_SERPAPI_MAX_PAGES` (10 pages par appel) et `MOISSON_SERPAPI_MIN_CREDITS` (refus sous 100 crédits restants).

Une source dont la clé requise manque n'est pas interrogée : statut `skipped` (motif `missing_key`), les autres sources continuent. Sans aucune clé, HAL, Isidore et ses sous-sources, et CrossRef restent disponibles.

`moisson-shs setup` inscrit **toujours** les quatre variables dans la déclaration du client. Chaque valeur vient, par ordre de priorité, des options `--openalex-key`, `--semantic-scholar-key`, `--core-key`, `--serpapi-key` ; sinon des variables du shell ; sinon de la saisie masquée en terminal interactif (désactivable par `--no-prompt` ; hors terminal, la saisie n'est pas proposée et `setup` le dit) ; sinon de la valeur déjà déclarée ; sinon elle reste vide, et `setup` liste les clés vides à renseigner. Les valeurs sont masquées à l'affichage (`setup`, `setup --status`).

**Réserve du `.env`.** Le fichier `.env` **à la racine du projet**, créé par le développeur à partir de `.env.example`, est lu au démarrage par `utils/secrets.py` (chemin résolu depuis le module : le client lance le serveur depuis un répertoire arbitraire). La déclaration du client fait autorité ; la clé du `.env` ne sert que :

| Clé déclarée par le client | Clé dans `.env` | Clé utilisée |
|---|---|---|
| renseignée et acceptée | indifférent | celle du client |
| non déclarée (CLI, ancienne déclaration) | présente | celle du `.env` |
| vide | présente, source dans `MOISSON_KEY_FALLBACK` | celle du `.env` |
| refusée (HTTP 401 ou 403) | présente, différente, source dans `MOISSON_KEY_FALLBACK` | celle du `.env`, signalée dans le statut de la source |
| vide ou refusée | absente, ou source hors `MOISSON_KEY_FALLBACK` | aucune : source non exécutée (ou en échec si refusée) |

`MOISSON_KEY_FALLBACK` vaut par défaut `openalex,semantic_scholar,core` : SerpAPI, seule source facturée, n'est jamais complétée ni remplacée par la clé du développeur sans l'y ajouter. Après un refus, la réserve sert pour le reste du processus (une seule requête refusée). `setup` ne copie jamais les clés du `.env` dans la déclaration ; `setup --status` indique pour chaque clé son état dans la déclaration, la présence d'une réserve et si le repli est permis. `MOISSON_NO_DOTENV=1` interdit toute lecture de `.env`. Les réglages `MOISSON_*` lus au démarrage se posent dans le bloc `env` : le `.env` est lu après `config.py`.

**Rotation** : régénérer la clé sur le tableau de bord de la source, remplacer la valeur dans le bloc `env` (ou relancer `moisson-shs setup --<source>-key …`), puis redémarrer le client. Ne jamais committer de clé : `.env` est ignoré par git (voir `.env.example`).

### Autres réglages utiles dans le bloc `env`

| Variable | Défaut | Effet |
|----------|--------|-------|
| `MOISSON_MCP_MAX_PER_SOURCE` | 200 | Plafond par source de `moisson_search` quand l'agent ne le fixe pas |
| `MOISSON_MCP_WORKERS` | 4 | Sources interrogées en même temps |
| `MOISSON_CACHE` | absent | Absent : cache actif (6 h, `MOISSON_MCP_CACHE_TTL`) ; `0` le désactive et `1` l'active quand l'agent ne précise pas `cache` (un `cache` explicite prime toujours) |
| `MOISSON_STDIO_SOFT_DEADLINE` | 50 | Échéance souple de la collecte (secondes ; 0 la désactive) |
| `MOISSON_INLINE_DEFAULT` | digest | Retour des outils producteurs : `digest`, `full` (sprint 05) ou `none` |
| `MOISSON_RANKING` | native | Classement par défaut (`native`, `local`, `off`) |

La liste complète, commentée, est dans [`.env.example`](.env.example).

## Étape 5. Redémarrer Claude Desktop

Quitter complètement l'application (pas seulement fermer la fenêtre) puis la relancer : elle lance `mcp_server.py` et ouvre la connexion stdio.

## Vérification

Dans une nouvelle conversation :

> Liste les corpus disponibles dans mon répertoire de moissonnage.

L'outil `corpus_list` répond (vingt corpus au plus par défaut, lus dans l'index).

> Moissonne les sources SHS sur la requête « discours numérique » depuis 2018.

L'outil `moisson_search` s'exécute avec ses défauts (sept sources francophones, 200 notices par source, 4 sources à la fois, cache actif, mode bibliographie) : **quelques secondes** en temps normal (3,2 s mesurées le 2026-09-29, 2 requêtes réseau). Le retour donne le statut de chaque source, les liens vers les fichiers et un digest des 40 premières notices.

## Ce que renvoie une moisson

- **Digest** (défaut) : résumé, `ResourceLink` vers le corpus Zotero, le CSL-JSON et les statistiques, puis les 40 premières notices (rang, titre, auteurs, année, support, type, DOI, sources, termes appariés, réserves), le tout sous **15 000 octets**. L'agent obtient les notices complètes par `corpus_query(corpus=…, ids=[…])` et la suite par `corpus_query(offset=…)`.
- **`inline="full"`** restitue le retour du sprint 05 (corpus inliné jusqu'à 300 notices et 2 Mo) ; **`inline="none"`** ne rend que le résumé et les liens.
- **Statuts** : `complete`, `empty`, `partial`, `skipped`, `error`, `cancelled`, avec un motif. Les sous-sources d'Isidore sont souvent `partial` (motif `provider_window`) : c'est la fenêtre de 200 résultats d'Isidore, une limite du fournisseur, pas une panne. Si toutes les sources échouent, l'outil rend une erreur MCP (`isError`) et n'écrit rien.
- **Manifeste** : `<radical>.manifest.json` garde le plan de requête, les statuts, les rangs et les scores ; il est publié en dernier.

## Délai d'appel d'outil

Claude Desktop annule un appel d'outil trop long (`MCP error -32001: Request timed out`), et une moisson annulée ne publie rien. Ce délai n'est pas documenté : 240 s ont été mesurées dans les journaux de l'application (juin 2026), mais 60 s est le défaut du SDK client et d'autres clients. Le serveur retient **60 s** comme borne prudente : en stdio, `moisson_search` arrête sa collecte à **50 s** (échéance souple), publie le corpus des sources qui ont répondu et nomme les sources arrêtées (motif `deadline`). Pour une moisson longue (`mode="collecte"` avec un gros `max_per_source`, `enrich_oai`), relever `deadline_seconds` (jusqu'à 180 environ avec Claude Desktop) ou passer par la CLI. Détails : [docs/FAQ.md](docs/FAQ.md).

`corpus_resolve_dois` a son propre budget (`max_seconds`, 240 s par défaut) : avec un client limité à 60 s, le fixer en dessous.

## Diagnostic en cas d'échec

### Le serveur ne démarre pas

Journaux de Claude Desktop :

- **macOS** : `~/Library/Logs/Claude/mcp*.log`
- **Windows** : `%APPDATA%\Claude\logs\mcp*.log`
- **Linux** : `~/.config/Claude/logs/mcp*.log`

- `ModuleNotFoundError` : `command` ne pointe pas vers le Python du venv où les dépendances sont installées.
- `ImportError` sur `mcp` : le SDK n'est pas installé dans ce venv.
- `AttributeError: 'Server' object has no attribute 'list_tools'` : SDK de la série 2 ; réinstaller avec `pip install -e ".[mcp]" -c constraints.txt`.

### Les outils n'apparaissent pas

Vérifier que `claude_desktop_config.json` est du JSON strict :

```bash
python -m json.tool < claude_desktop_config.json
```

`moisson-shs setup --status` affiche l'entrée configurée.

### Erreurs réseau pendant la moisson

Derrière un proxy d'entreprise, ajouter les variables nécessaires au bloc `env` :

```json
"env": {
  "MOISSON_OUTPUT_DIR": "...",
  "HTTPS_PROXY": "http://proxy.example.org:8080",
  "HTTP_PROXY": "http://proxy.example.org:8080"
}
```

### Erreur 429 (Too Many Requests)

La politique HTTP respecte `Retry-After` et fait au plus trois tentatives, avec un délai croissant. Si l'erreur persiste, relever l'intervalle entre deux requêtes vers un même hôte dans le bloc `env` : `MOISSON_REQUEST_DELAY` (0.3 s par défaut), ou l'intervalle propre à la source (`MOISSON_REQUEST_DELAY_S2`, `MOISSON_REQUEST_DELAY_CORE`, `MOISSON_REQUEST_DELAY_CROSSREF_LIST`).

## Outils exposés

| Outil | Fonction | Durée typique |
|---|---|---|
| `moisson_search` | Moisson multi-sources (douze sources, groupes), plan de requête, classement | quelques secondes ; bornée à 50 s en stdio par l'échéance souple |
| `corpus_analyze` | Statistiques d'un corpus | moins d'une seconde |
| `corpus_list` | Corpus disponibles, lus dans l'index (`limit`, `pattern`, `since`) | quelques millisecondes |
| `corpus_filter` | Filtre par année, type, source, langue, titre | moins d'une seconde |
| `corpus_to_csl` | Conversion en CSL-JSON | moins d'une seconde |
| `corpus_merge` | Fusion de plusieurs corpus avec déduplication | quelques secondes |
| `fetch_by_doi` | Notice d'un DOI, six résolveurs en parallèle | environ 1 s ; 60 s au plus |
| `search_by_author` | Production d'un auteur, homonymes signalés | 2 à 8 s mesurées en réseau réel |
| `corpus_resolve_dois` | DOI manquants par appariement CrossRef | environ 0,34 s par notice sans DOI ; budget 240 s |
| `corpus_export` | Export BibTeX, BibLaTeX ou RIS | moins d'une seconde |
| `corpus_query` | Recherche, filtres, pagination, notices complètes dans un corpus | moins d'une seconde |
| `corpus_diff` | Comparaison de deux corpus (veille) | moins d'une seconde |

Arguments et défauts de chaque outil : [README.md](README.md#outils-12). Prompts : `bibliographie-thematique`, `profil-chercheur`, `veille-mensuelle`, `etat-de-l-art`.

## Sécurité et isolation

Le serveur local s'exécute avec les privilèges de l'utilisateur de Claude Desktop. Il accède :

- en lecture et écriture à `MOISSON_OUTPUT_DIR` (corpus, manifestes, index) et au cache HTTP (`~/.cache/moisson_shs/http_mcp.sqlite`, ou `MOISSON_CACHE_DIR` suffixé de `_mcp`) ;
- aux chemins que l'agent lui passe en argument : **en stdio, les chemins ne sont pas confinés** (un corpus peut être lu, filtré ou exporté n'importe où sur le disque, et les fichiers dérivés sont écrits à côté de lui) ;
- à Internet, pour interroger les API des sources demandées.

Le confinement des chemins sous `OUTPUT_DIR`, l'authentification et l'absence de chemin disque dans les retours valent pour le transport HTTP (voir [REMOTE.md](REMOTE.md)). Pour isoler davantage le serveur local, l'exécuter dans un conteneur ou un bac à sable dédié.

## Désinstallation

```bash
moisson-shs setup --uninstall
```

En terminal, la commande propose les clients où l'entrée `moisson-shs` est déclarée (rien n'est coché d'avance) ; hors terminal, elle vise Claude Desktop, ou les clients de `--client`. Elle retire l'entrée après une sauvegarde horodatée, sans toucher aux autres serveurs. Redémarrer les clients modifiés. Les corpus de `MOISSON_OUTPUT_DIR` restent intacts.

## Référence

- Protocole MCP : https://modelcontextprotocol.io
- SDK Python MCP : https://github.com/modelcontextprotocol/python-sdk
- Utilisation d'un serveur MCP local : https://modelcontextprotocol.io/quickstart/user
