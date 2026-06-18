# Installation du serveur MCP moisson-shs dans Claude Desktop

Ce document décrit la procédure d'installation du serveur MCP local exposant le moissonneur SHS comme outils invocables par Claude Desktop. Une fois configuré, Claude peut moissonner, analyser, lister et filtrer des corpus directement depuis l'interface de chat, sans passer par une commande shell manuelle.

## Prérequis

- Python 3.10 ou supérieur
- Claude Desktop installé (macOS, Windows ou Linux)
- Le projet `moisson_shs/` accessible sur le poste de l'utilisateur
- Un accès Internet libre (les API HAL, Isidore, OpenEdition et Cairn doivent être joignables)

## Étape 1. Installation des dépendances

```bash
cd /chemin/vers/moisson_shs
python -m venv venv
source venv/bin/activate          # Linux / macOS
# venv\Scripts\activate.bat       # Windows

pip install -r requirements.txt
pip install mcp                   # SDK MCP officiel d'Anthropic
```

Le paquet `mcp` est publié sur PyPI par Anthropic et fournit les classes `Server`, `stdio_server` et les types nécessaires au serveur.

## Étape 2. Test du serveur en mode autonome

Avant de l'intégrer à Claude Desktop, vérifier que le serveur démarre correctement :

```bash
python mcp_server.py
```

Le processus reste en attente sur stdin. Aucune sortie n'est attendue sur stdout (réservée au protocole JSON-RPC). Les logs de démarrage apparaissent sur stderr :

```
[INFO] moisson_mcp: Démarrage serveur MCP moisson-shs
[INFO] moisson_mcp: OUTPUT_DIR = /chemin/vers/moisson_shs/output
```

Interrompre avec Ctrl+C.

## Étape 3. Localiser le fichier de configuration Claude Desktop

Le fichier `claude_desktop_config.json` se trouve à :

- **macOS** : `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows** : `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux** : `~/.config/Claude/claude_desktop_config.json`

S'il n'existe pas, le créer.

## Étape 4. Ajouter le serveur dans la configuration

Éditer le fichier et ajouter la section `mcpServers` :

```json
{
  "mcpServers": {
    "moisson-shs": {
      "command": "/chemin/vers/moisson_shs/venv/bin/python",
      "args": [
        "/chemin/vers/moisson_shs/mcp_server.py"
      ],
      "env": {
        "MOISSON_OUTPUT_DIR": "/chemin/vers/output"
      }
    }
  }
}
```

Points importants :

- `command` doit pointer vers le binaire Python du venv (où `mcp` et les dépendances du moissonneur sont installés), pas vers le Python système.
- `args` contient le chemin absolu vers `mcp_server.py`.
- `MOISSON_OUTPUT_DIR` (optionnel) configure le répertoire où sont sauvegardés les corpus produits. Si omis, par défaut `moisson_shs/output/`.

Si d'autres serveurs MCP sont déjà configurés, ajouter `moisson-shs` comme nouvelle entrée du dictionnaire `mcpServers`, sans écraser les serveurs existants.

### Clés API des sources internationales (optionnel)

Les sources internationales (CrossRef, OpenAlex, Semantic Scholar, CORE) ne sont
moissonnées que si on les demande (`sources: ["international"]` ou `["all"]`, ou
en les nommant). Trois requièrent une clé, transmise via le bloc `env` (les clés
OpenAlex/CORE sont liées au compte utilisateur) :

```json
"moisson-shs": {
  "command": "/chemin/venv/bin/python",
  "args": ["/chemin/mcp_server.py"],
  "env": {
    "MOISSON_OUTPUT_DIR": "/chemin/output",
    "OPENALEX_API_KEY": "…",
    "SEMANTIC_SCHOLAR_API_KEY": "…",
    "CORE_API_KEY": "…"
  }
}
```

- **OpenAlex** : clé gratuite **requise** — https://openalex.org/settings/api
- **Semantic Scholar** : clé **optionnelle** (sans clé : pool partagé throttlé) — https://www.semanticscholar.org/product/api
- **CORE** : clé Bearer **requise** (sur inscription) — https://core.ac.uk/services/api
- **CrossRef** : aucune clé.

`moisson-shs setup` peut injecter ces clés automatiquement, par ordre de
priorité :

1. **flags** `--openalex-key` / `--semantic-scholar-key` / `--core-key` ;
2. sinon, reprise depuis les **variables d'environnement** du shell ;
3. sinon, **saisie interactive masquée** : en terminal interactif, `setup`
   demande chaque clé encore manquante (Entrée vide = passer / conserver une
   clé déjà configurée). Désactivable avec `--no-prompt` (installation
   scriptée) ; ignorée automatiquement hors terminal interactif (CI).

Les clés déjà présentes dans l'entrée `moisson-shs` sont préservées lors d'un
`setup` ultérieur, et leurs valeurs sont **masquées** dans la sortie console
(`setup` et `setup --status`).

**Rotation / révocation** : régénérer une clé sur le tableau de bord de la source,
remplacer la valeur dans le bloc `env` (ou relancer `moisson-shs setup --<source>-key …`),
puis redémarrer Claude Desktop. Une source sans clé valide est ignorée proprement.
Ne jamais committer de clé : `.env` est git-ignoré (voir `.env.example`).

## Étape 5. Redémarrer Claude Desktop

Quitter complètement l'application (pas seulement fermer la fenêtre) et la relancer. Au démarrage, Claude Desktop spawn le processus `mcp_server.py` et établit la connexion stdio.

## Vérification

Dans une nouvelle conversation Claude Desktop, demander :

> Liste les corpus disponibles dans mon répertoire de moissonnage.

Claude devrait appeler l'outil `moisson_shs:corpus_list` et restituer le contenu (ou indiquer qu'aucun corpus n'est encore présent).

Pour lancer une moisson :

> Moissonne les sources SHS sur la requête « discours numérique », plafond 500 items par source.

Claude appelle `moisson_shs:moisson_search` avec les bons paramètres. Le moissonnage dure plusieurs minutes ; Claude affiche le résumé final (volume, durée, doublons fusionnés, chemin du corpus produit).

## Diagnostic en cas d'échec

### Le serveur ne démarre pas

Consulter les logs Claude Desktop :

- **macOS** : `~/Library/Logs/Claude/mcp*.log`
- **Windows** : `%APPDATA%\Claude\logs\mcp*.log`
- **Linux** : `~/.config/Claude/logs/mcp*.log`

Les erreurs `ModuleNotFoundError` indiquent un chemin Python incorrect dans `command` (le venv n'est pas celui où les dépendances sont installées). Les erreurs `ImportError` sur `mcp` indiquent que le SDK MCP n'est pas installé dans ce venv.

### Les outils n'apparaissent pas dans Claude

Vérifier que la section `mcpServers` est syntaxiquement valide (JSON strict). Une virgule manquante ou un guillemet manquant empêche le chargement. Tester :

```bash
python -m json.tool < claude_desktop_config.json
```

### Erreurs réseau pendant la moisson

Si vous êtes derrière un proxy d'entreprise, ajouter les variables d'environnement nécessaires dans la section `env` :

```json
"env": {
  "MOISSON_OUTPUT_DIR": "...",
  "HTTPS_PROXY": "http://proxy.example.org:8080",
  "HTTP_PROXY": "http://proxy.example.org:8080"
}
```

### Erreur 429 (Too Many Requests)

Le client HTTP du moissonneur applique un backoff exponentiel automatique. Si l'erreur persiste, augmenter `REQUEST_DELAY` dans `config.py` (passer de 0.3 à 1.0 seconde).

## Outils exposés

Une fois le serveur configuré, Claude Desktop dispose des quatre outils suivants :

| Outil | Fonction | Durée typique |
|---|---|---|
| `moisson_search` | Lance une moisson sur HAL, Isidore, OpenEdition et Cairn | 1 à 30 minutes selon volume et enrichissement |
| `corpus_analyze` | Statistiques descriptives d'un corpus JSON | Quelques secondes |
| `corpus_list` | Liste les corpus disponibles dans `MOISSON_OUTPUT_DIR` | Immédiat |
| `corpus_filter` | Filtre un corpus selon année, type, source, langue, mot-clé | Quelques secondes |

Tous ces outils sont accessibles via langage naturel : Claude interprète la demande et choisit l'outil approprié.

## Sécurité et isolation

Le serveur MCP s'exécute avec les privilèges de l'utilisateur Claude Desktop. Il accède :

- Au répertoire `MOISSON_OUTPUT_DIR` en lecture et écriture (pour stocker les corpus).
- À Internet, pour interroger les quatre API publiques.

Il n'accède pas au reste du système de fichiers de l'utilisateur. Pour renforcer l'isolation, exécuter le serveur dans un conteneur Docker ou un sandbox dédié reste possible mais n'est pas nécessaire dans le cas d'usage standard.

## Désinstallation

Supprimer la section `moisson-shs` dans `claude_desktop_config.json` et redémarrer Claude Desktop. Le processus `mcp_server.py` ne sera plus spawné. Les corpus déjà produits dans `MOISSON_OUTPUT_DIR` restent intacts.

## Référence

- Protocole MCP : https://modelcontextprotocol.io
- SDK Python MCP : https://github.com/modelcontextprotocol/python-sdk
- Documentation Claude Desktop MCP : https://modelcontextprotocol.io/quickstart/user
