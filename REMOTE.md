# REMOTE.md — Serveur MCP distant `moisson-shs`

Guide du **serveur MCP distant** : un agent (client MCP de bureau, `.mcp.json`, connecteur web, agent maison) appelle les douze outils du moissonneur par **HTTPS authentifié**, sans installation locale. Le serveur est le **même** `Server("moisson-shs")` que le mode stdio (mêmes outils, mêmes arguments) ; seul le **transport** change, avec les garanties propres au distant décrites ici.

> Mise en production pas à pas (Docker + TLS) : [`DEPLOY.md`](DEPLOY.md).
> Architecture détaillée : [`.claude/dev/sprint_remote_plan.md`](.claude/dev/sprint_remote_plan.md).

## 0. Note de redéploiement (chantier d'optimisation, 2026-09)

Le chantier d'optimisation a fermé dans le code un écart de confinement de `read_resource` (constat C03) : avant correction, un porteur de jeton valide pouvait lire par une URI `corpus://` des fichiers de service de `OUTPUT_DIR`, dont le magasin OAuth (`.oauth/oauth_store.json`) et le journal d'audit (`.audit/`). **Un serveur déployé avant ce correctif y reste exposé jusqu'à son redéploiement.** Procédure :

1. **Redéployer** l'image à partir du code actuel, avec le fichier compose du déploiement en place à **chaque** commande : variante Caddy `docker compose build && docker compose up -d` ; derrière un Nginx existant `docker compose -f docker-compose.nginx.yml build && docker compose -f docker-compose.nginx.yml up -d`. Sur un hôte Nginx, un `docker compose` sans `-f` recrée l'application sans sa publication `127.0.0.1:8010` et tente de lancer Caddy sur 80 et 443 (voir [DEPLOY.md](DEPLOY.md) § 0 et § 9).
2. **Faire tourner les jetons Bearer** (`MOISSON_API_TOKEN`, `MOISSON_API_TOKENS`, § 3), puis migrer les clients.
3. **Révoquer les jetons OAuth** si OAuth était activé : arrêter le service, supprimer `OUTPUT_DIR/.oauth/oauth_store.json` (clients, jetons d'accès et de rafraîchissement), changer `MOISSON_OAUTH_PASSWORD`, redémarrer ; les connecteurs refont leur enregistrement et leur consentement.
4. **Faire tourner les clés d'API** présentes dans l'environnement du conteneur (`OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `CORE_API_KEY`, `SERPAPI_API_KEY`), par prudence.
5. Vérifier que le cache HTTP est bien dans le volume : `MOISSON_CACHE_DIR=/data/cache/http` (posé par le `Dockerfile`), le serveur écrit `/data/cache/http_mcp.sqlite`.

## 1. Vue d'ensemble

```
 agent distant ──HTTPS + Bearer/OAuth──▶ Caddy (TLS) ──http──▶ uvicorn (1 worker)
                                                                └ Server("moisson-shs")
                                                                  • /mcp        (Streamable HTTP, auth)
                                                                  • /download/… (fichiers, auth)
                                                                  • /healthz    (public)
                                                                  • /metrics    (auth)
                                                                  • routes OAuth (si activé)
```

Le reverse proxy TLS peut être Caddy (`docker-compose.yml`) ou un Nginx déjà en place sur l'hôte (`docker-compose.nginx.yml`, application publiée sur `127.0.0.1:8010`).

Différences avec le mode **stdio** (local) :

| Aspect | stdio (local) | HTTP (distant) |
|--------|---------------|----------------|
| Authentification | aucune (processus de confiance) | **Bearer** et/ou **OAuth 2.1** ; refus de démarrer sans moyen d'authentification |
| Retour des outils | digest (défaut), `inline="full"` ou `"none"`, liens `corpus://` | **URL de téléchargement** ; jamais de chemin disque ni de JSON bibliographique inline ; `inline` sans effet |
| Chemins d'entrée | libres | **confinés sous `OUTPUT_DIR`** (anti-traversée, liens symboliques sortants refusés) |
| Échéance | souple, 50 s, publication des sources terminées | **dure**, `MOISSON_DEADLINE_SECONDS` (900 s), annulation sans publication partielle |
| Concurrence | aucune borne | sémaphore partagé par `moisson_search`, `search_by_author`, `fetch_by_doi` et `corpus_resolve_dois` |
| Plafond par source | 200 par défaut | idem, plafonné par `MOISSON_MAX_ITEMS_PER_SOURCE` (3000) |
| Observabilité | journaux sur stderr | journaux **JSON**, **audit JSONL**, `/metrics` |

## 2. Démarrer

### Option A — Docker + Caddy (production)

Voir [`DEPLOY.md`](DEPLOY.md). En résumé :

```bash
cp .env.example .env          # renseigner MOISSON_API_TOKEN, MOISSON_ALLOWED_HOSTS, MOISSON_PUBLIC_BASE_URL, MOISSON_DOMAIN, ACME_EMAIL
docker compose up -d
./scripts/smoke_remote.sh moisson.example.org "$MOISSON_API_TOKEN"
```

### Option B — local (développement, ou derrière votre propre TLS)

```bash
pip install -e ".[remote]" -c constraints.txt
moisson-shs token generate                 # secret affiché UNE fois → MOISSON_API_TOKEN
export MOISSON_API_TOKEN=<le secret>
moisson-shs serve --http --host 127.0.0.1 --port 8000
```

L'extra `[remote]` a les mêmes dépendances que `[mcp]` (SDK borné à `mcp>=1.27,<2`).

## 3. Jetons Bearer (`moisson-shs token`)

```bash
moisson-shs token generate     # crée un jeton (secret affiché 1×), stocke son sha256 (0600)
moisson-shs token status       # empreintes et dates, sans révéler le secret
moisson-shs token rotate       # nouveau jeton ; l'ancien reste valide (recouvrement)
moisson-shs token revoke --fingerprint <empreinte>   # révoque l'ancien après migration
moisson-shs token revoke       # révoque TOUS
```

- Le **secret** n'est jamais persisté : seule son empreinte SHA-256 l'est (fichier `0600`, `~/.config/moisson_shs/tokens.json` ou `MOISSON_TOKEN_STORE`).
- Le serveur compare le jeton reçu à `MOISSON_API_TOKEN` et `MOISSON_API_TOKENS` (comparaison à temps constant), lus au démarrage.
- **Rotation sans coupure** : `MOISSON_API_TOKENS="ancien,nouveau"` (les deux valides), redémarrer, migrer les clients, puis retirer l'ancien.

## 4. OAuth 2.1 (connecteur web)

Certains clients (connecteurs distants d'une application web) n'acceptent qu'OAuth 2.1. Le serveur fournit un serveur d'autorisation **mono-utilisateur**, en plus du Bearer statique qui reste valable pour les clients de bureau :

- activation : `MOISSON_OAUTH_ENABLED=1`, avec **`MOISSON_OAUTH_PASSWORD`** (mot de passe de la page de consentement, obligatoire : sans lui, le serveur refuse de démarrer) et **`MOISSON_PUBLIC_BASE_URL`** (émetteur et points d'accès) ;
- le SDK gère le protocole (PKCE S256, enregistrement dynamique des clients RFC 7591, métadonnées du serveur d'autorisation et de la ressource protégée, révocation) ; le serveur ajoute la page de consentement `/oauth/consent` ;
- jetons d'accès valables 1 h, de rafraîchissement 30 jours, codes 5 min ;
- stockage : `OUTPUT_DIR/.oauth/oauth_store.json` (clients, jetons), fichier de service jamais listé, lu comme ressource ni téléchargé ;
- une réponse 401 sur `/mcp` annonce l'URL des métadonnées de la ressource protégée (`WWW-Authenticate`).

Révocation globale : voir la note du § 0, étape 3.

## 5. Configuration du client (`moisson-shs setup --remote`)

```bash
moisson-shs setup --remote https://moisson.example.org/mcp --token "$MOISSON_API_TOKEN"
```

Imprime un bloc copiable, sans aucun chemin disque du serveur :

```json
{
  "mcpServers": {
    "moisson-shs-remote": {
      "url": "https://moisson.example.org/mcp",
      "headers": { "Authorization": "Bearer <TOKEN>" }
    }
  }
}
```

Pour un connecteur web qui exige OAuth, renseigner seulement l'URL `https://<domaine>/mcp` : le client découvre le serveur d'autorisation et ouvre la page de consentement.

## 6. Outils en mode distant

Les douze outils (`moisson_search`, `corpus_analyze`, `corpus_list`, `corpus_filter`, `corpus_to_csl`, `corpus_merge`, `fetch_by_doi`, `search_by_author`, `corpus_resolve_dois`, `corpus_export`, `corpus_query`, `corpus_diff`) gardent leurs arguments (voir [README.md](README.md#outils-12)). Règles propres au transport HTTP :

- **Retour** : texte sans chemin, `structuredContent` avec `download_urls` (`<MOISSON_PUBLIC_BASE_URL>/download/<nom>`) ; ni `ResourceLink` vers un chemin, ni JSON bibliographique, ni digest. L'argument `inline` est sans effet.
- **Chemins** : `corpus_path`, `corpus_paths`, `output_path`, `corpus`, `corpus_a`, `corpus_b` et `add_to_corpus` sont résolus sous `OUTPUT_DIR` ; un chemin qui en sort (`..`, absolu hors zone, lien symbolique) est refusé. `corpus_export` n'accepte qu'un nom plat de `OUTPUT_DIR`, d'extension `.bib` ou `.ris`.
- **`/download/<nom>`** sert les corpus, le CSL-JSON, les statistiques, les fichiers associés (`.manifest.json`, `.doi_report.json`, `.diff.json`) et les exports (`.bib` en `application/x-bibtex`, `.ris` en `application/x-research-info-systems`) ; jamais un fichier de service (`.oauth/`, `.audit/`, `.index.json`, temporaires).
- **`corpus_query`** : chaque sélection est publiée comme corpus dérivé `<radical>_sel_<uuid>.json`, avec son manifeste, et seule son URL revient ; les dix dernières sélections par corpus source sont gardées (`MOISSON_QUERY_HTTP_KEEP`), les plus anciennes supprimées. Le manifeste d'une sélection ne reprend pas la requête de la moisson source comme requête (elle est gardée sous `filters.corpus_query.source_query`).
- **`corpus_diff`** : URL du rapport `.diff.json` et identifiants des notices, sans titre.
- **Ressources** : `list_resources` ne donne aucun chemin ; `read_resource` n'accepte que les noms plats de `OUTPUT_DIR` en `.json`, `.csl.json` ou `_stats.txt`, lus par pages (`corpus://<nom>?offset=&limit=`).
- **Opérations longues** : `moisson_search`, `search_by_author`, `fetch_by_doi` et `corpus_resolve_dois` partagent le sémaphore (`MOISSON_MAX_CONCURRENT`, attente `MOISSON_ACQUIRE_TIMEOUT`) et l'échéance dure (`MOISSON_DEADLINE_SECONDS`) ; une progression SSE est émise si le client la demande. L'argument `deadline_seconds` (échéance souple) ne vaut qu'en stdio.
- **Erreurs** : un échec de toutes les sources, un paramètre invalide ou une entrée refusée rendent une erreur d'outil (`isError`), sans chemin dans le message.

## 7. Variables d'environnement

| Variable | Défaut | Rôle |
|----------|--------|------|
| `MOISSON_API_TOKEN` | *(vide)* | Jeton Bearer attendu. Sans lui ni OAuth, **refus de démarrer**. |
| `MOISSON_API_TOKENS` | *(vide)* | Jetons valides additionnels (rotation), séparés par des virgules. |
| `MOISSON_TOKEN_STORE` | `~/.config/moisson_shs/tokens.json` | Fichier d'empreintes de la CLI `token`. |
| `MOISSON_OAUTH_ENABLED` | `0` | `1` active OAuth 2.1 (§ 4). |
| `MOISSON_OAUTH_PASSWORD` | *(vide)* | Mot de passe de la page de consentement, obligatoire avec OAuth. |
| `MOISSON_PUBLIC_BASE_URL` | *(vide)* | Base HTTPS publique des URL de téléchargement (et émetteur OAuth). Vide : URL relatives. |
| `MOISSON_DOWNLOAD_PREFIX` | `/download` | Préfixe de la route de téléchargement. |
| `MOISSON_OUTPUT_DIR` | `<projet>/output` | Espace des corpus = racine du confinement. |
| `MOISSON_CACHE_DIR` | `~/.cache/moisson_shs/http` | Base du cache HTTP ; le serveur MCP utilise `<base>_mcp.sqlite`. L'image Docker pose `/data/cache/http`. |
| `MOISSON_MCP_CACHE_TTL` | `21600` | Durée de vie du cache côté serveur (6 h). |
| `MOISSON_MCP_CACHE_MAX_MB` | `512` | Plafond du cache à l'entretien (horaire, `MOISSON_MCP_CACHE_PRUNE_INTERVAL`). |
| `MOISSON_HTTP_HOST` | `127.0.0.1` | Interface uvicorn (Docker derrière proxy : `0.0.0.0`). |
| `MOISSON_HTTP_PORT` | `8000` | Port uvicorn. |
| `MOISSON_HTTP_PATH` | `/mcp` | Chemin de la route Streamable HTTP. |
| `MOISSON_ALLOWED_HOSTS` | *(obligatoire si écoute non locale)* | Liste blanche Host (anti DNS-rebinding). |
| `MOISSON_ALLOWED_ORIGINS` | *(vide)* | Liste blanche Origin. |
| `MOISSON_HTTP_MAX_BODY` | `1000000` | Taille maximale du corps (413 au-delà). |
| `MOISSON_RATE_LIMIT` | *(vide = désactivé)* | Requêtes par minute et par IP (429). Garde-fou de dernier recours. |
| `MOISSON_MAX_ITEMS_PER_SOURCE` | `3000` | Plafond serveur de `max_per_source`. |
| `MOISSON_MAX_CONCURRENT` | `2` | Opérations longues simultanées (sémaphore). |
| `MOISSON_ACQUIRE_TIMEOUT` | `30` | Attente d'une place avant le refus « serveur occupé » (secondes). |
| `MOISSON_DEADLINE_SECONDS` | `900` | Échéance dure d'une opération longue (secondes). |
| `MOISSON_PROGRESS_SEND_TIMEOUT` | `5` | Délai maximal d'envoi d'une notification de progression. |
| `MOISSON_QUERY_HTTP_KEEP` | `10` | Sélections de `corpus_query` gardées par corpus source. |
| `MOISSON_HTTP_STATELESS` / `MOISSON_HTTP_JSON_RESPONSE` | `0` | **Laisser à 0** (sinon plus de progression SSE). |
| `MOISSON_DOMAIN` / `ACME_EMAIL` | — | Docker et Caddy (TLS). |

Toutes les autres variables (sources, politique HTTP, déduplication, classement, publication) valent aussi en distant : voir [`.env.example`](.env.example).

## 8. Observabilité

- **Journaux JSON** sur stderr (une ligne par événement : `ts`, `level`, `msg`, plus `request_id`, `tool`, `duration_ms`, `status`, `client_id`). Masquage systématique des `Bearer …` et de `Authorization`.
- **Audit** : une ligne JSONL par appel d'outil dans `OUTPUT_DIR/.audit/audit-AAAAMMJJ.jsonl` (`ts`, `request_id`, `client_id`, `tool`, `arg_keys` — **pas les valeurs** —, `duration_ms`, `status`, `item_count`).
- **`GET /healthz`** (public) : `{status, version, mode, uptime_s, output_dir_writable}`, sans chemin ni secret.
- **`GET /metrics`** (**authentifié**) : `{calls_total, errors_total, by_tool, duration_ms_p50, duration_ms_p95}`, en mémoire (perdus au redémarrage ; cohérent avec un seul worker).
- **Manifestes** : chaque corpus publié porte son manifeste (`<radical>.manifest.json`, téléchargeable) : requête, plan, statuts par source, rangs et scores.

## 9. Liste de contrôle sécurité

- [ ] `MOISSON_API_TOKEN` défini (ou OAuth activé avec `MOISSON_OAUTH_PASSWORD`) ; sinon le serveur **refuse de démarrer**.
- [ ] `MOISSON_ALLOWED_HOSTS` = domaine public exact (sinon refus sur écoute non locale, ou 421).
- [ ] uvicorn **à l'écoute interne** (`expose`, jamais `ports:`) ; **Caddy** seul point d'entrée (TLS).
- [ ] `MOISSON_PUBLIC_BASE_URL` en `https://`.
- [ ] `.env` hors Git et hors image (`.gitignore`, `.dockerignore`).
- [ ] Volumes `moisson_output`, `moisson_cache` et `caddy_data` sauvegardés (corpus, magasin OAuth, cache, certificats).
- [ ] Limite de corps au proxy (`request_body max_size`) **et** dans l'application (413).
- [ ] Jeton généré par `secrets` (≥ 32 octets) ; rotation documentée (§ 3).
- [ ] Serveur **redéployé depuis le correctif C03**, jetons et clés renouvelés (§ 0).

## 10. Modèle de menace (STRIDE léger)

| Menace | Vecteur | Parade |
|--------|---------|--------|
| **S**poofing | Accès anonyme aux outils | Bearer ou OAuth obligatoire ; `compare_digest` sur octets ; refus de démarrer sans moyen d'authentification ; consentement OAuth protégé par mot de passe |
| **T**ampering | Traversée de chemin (`corpus_path`, `output_path`, dérivés, `/download`, `corpus://`) | Confinement `resolve_in_output_dir` (refus de `..`, d'un absolu hors zone, d'un lien symbolique sortant) ; chemins dérivés confinés ; `/download` et `read_resource` confinés sous `OUTPUT_DIR`, liste blanche d'extensions, sans repli sur le registre stdio |
| **R**epudiation | Pas de trace | Audit JSONL par appel (`request_id`, `client_id`, `tool`, durée, statut) |
| **I**nfo. disclosure | Fuite de chemins, de jetons, de fichiers de service | HTTP n'émet que des URL ; fichiers de service (`.oauth/`, `.audit/`, `.index.json`) jamais listés, lus ni téléchargés ; masquage des chaînes et des journaux ; `AccessToken.token="***"` ; `/healthz` sans chemin ; `/metrics` authentifié ; audit natif expurgé |
| **D**enial of service | Moisson géante, corps géant, rafale | Plafond `MOISSON_MAX_ITEMS_PER_SOURCE` + concurrence bornée + échéance dure ; limite de corps (413) au proxy et dans l'application ; plafond en octets des retours ; rate-limit optionnel (429) |
| **E**levation of privilege | DNS-rebinding, Host usurpé | `TransportSecuritySettings` (hosts et origins autorisés) ; refus d'une écoute publique sans liste blanche ; `header_up Host` cohérent au proxy |

### Hors périmètre (évolutions possibles)

- **Multi-utilisateur isolé** : sous-répertoire `OUTPUT_DIR/<client_id>/` dérivé de l'`AccessToken` ; le confinement centralisé rend l'extension localisée. L'OAuth actuel est mono-utilisateur.
- **Migration vers le SDK mcp 2.x** : chantier distinct (réécriture de l'enregistrement des outils, ressources et prompts).
