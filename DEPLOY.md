# DEPLOY.md — Déploiement du serveur MCP distant `moisson-shs`

Runbook de mise en production du **serveur MCP distant** (mode Streamable HTTP) dans un conteneur
**Docker** derrière **Caddy** (TLS automatique Let's Encrypt), sur un VPS.

> Vue d'ensemble sécurité/architecture : [`REMOTE.md`](REMOTE.md). Plan directeur :
> [`.claude/dev/sprint_remote_plan.md`](.claude/dev/sprint_remote_plan.md).

> **Deux variantes, un seul fichier compose à la fois.** Les commandes `docker compose` des § 1 à
> 8 visent la variante Caddy (`docker-compose.yml`). Derrière un Nginx existant (§ 9), **toutes**
> prennent `-f docker-compose.nginx.yml` : mélanger les deux fichiers sur un même hôte recrée
> `app` sans sa publication et fait tomber le service (§ 0).

## 0. Redéploiement après le chantier d'optimisation (2026-09) — à faire

Le code actuel ferme un écart de confinement de `read_resource` (constat C03 : avec un jeton
valide, une URI `corpus://` pouvait lire des fichiers de service de `OUTPUT_DIR`, dont le magasin
OAuth `.oauth/oauth_store.json` et le journal `.audit/`). **Un serveur construit avant ce correctif
reste exposé tant qu'il n'est pas reconstruit.**

⚠️ **Identifier d'abord la variante en service : chaque commande `docker compose` de cette section
doit viser le même fichier compose que le déploiement en place.** Les deux fichiers partagent le
dossier (donc le nom de projet) et le service `app`. Sur un hôte où l'application tourne derrière
un Nginx existant (`docker-compose.nginx.yml`, § 9), un `docker compose` **sans** `-f` recrée `app`
d'après `docker-compose.yml`, qui ne publie pas `127.0.0.1:8010` (Nginx répond alors 502), puis
tente de lancer Caddy sur les ports 80 et 443 déjà tenus par Nginx : le service tombe pendant
l'opération. En cas de doute, `docker compose ls` et `docker ps` montrent le fichier et les ports
en service.

Variante Caddy (`docker-compose.yml`) :

```bash
git pull
MOISSON_BUILD_COMMIT=$(git rev-parse HEAD) docker compose build
docker compose up -d
docker compose logs app | grep -E "Bearer statique|OAuth"
curl -I https://moisson.example.org/healthz
```

Variante Nginx (`docker-compose.nginx.yml`, § 9) :

```bash
git pull
MOISSON_BUILD_COMMIT=$(git rev-parse HEAD) docker compose -f docker-compose.nginx.yml build
docker compose -f docker-compose.nginx.yml up -d
docker compose -f docker-compose.nginx.yml logs app | grep -E "Bearer statique|OAuth"
curl -I http://127.0.0.1:8010/healthz
curl -I https://moisson.example.org/healthz
```

`MOISSON_BUILD_COMMIT` (facultatif) inscrit le commit dans l'image, dont le contexte de construction exclut `.git` : `/healthz` rend alors `version` et `commit`, et chaque manifeste porte le commit, l'état de l'arbre et l'empreinte du code (PAT-03).

Puis, dans cet ordre (variante Nginx : `docker compose -f docker-compose.nginx.yml` à la place
de `docker compose` dans **chacune** des commandes ci-dessous, sans exception) :

1. **Jetons Bearer** : générer un nouveau jeton (`moisson-shs token rotate`, ou
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`), le placer dans `.env`
   (`MOISSON_API_TOKEN`, l'ancien dans `MOISSON_API_TOKENS` le temps de migrer les clients), puis
   `docker compose up -d` (Nginx : `docker compose -f docker-compose.nginx.yml up -d`) ; retirer
   l'ancien jeton une fois les clients migrés.
2. **OAuth** (si `MOISSON_OAUTH_ENABLED=1`) : `docker compose stop app` (Nginx :
   `docker compose -f docker-compose.nginx.yml stop app`), supprimer
   `/data/output/.oauth/oauth_store.json` dans le volume `moisson_output` (clients et jetons),
   changer `MOISSON_OAUTH_PASSWORD` dans `.env`, `docker compose up -d` (Nginx :
   `docker compose -f docker-compose.nginx.yml up -d`) ; les connecteurs refont leur
   enregistrement et leur consentement.
3. **Clés d'API** présentes dans `.env` (`OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`,
   `CORE_API_KEY`, `SERPAPI_API_KEY`) : les régénérer sur le tableau de bord de chaque source,
   mettre `.env` à jour, `docker compose up -d` (Nginx :
   `docker compose -f docker-compose.nginx.yml up -d`).
4. **Contrôles** : `/healthz` à 200 ; `initialize` authentifié à 200 et 401 sans jeton
   (`./scripts/smoke_remote.sh`) ; `tools/list` rend **12 outils** ; le cache HTTP est dans le
   volume (`docker compose exec app ls /data/cache`, ou
   `docker compose -f docker-compose.nginx.yml exec app ls /data/cache`, montre `http_mcp.sqlite`
   après une moisson).

Le cache HTTP du serveur vit dans le volume `moisson_cache` grâce à `MOISSON_CACHE_DIR=/data/cache/http`,
posé par le `Dockerfile` (fichier `/data/cache/http_mcp.sqlite`) : il survit aux reconstructions.
Durée de vie 6 h (`MOISSON_MCP_CACHE_TTL`), entretien au plus une fois par heure, plafond 512 Mo
(`MOISSON_MCP_CACHE_MAX_MB`), environ 15 Mo par moisson distincte.

## 1. Pré-requis

- Un **VPS** avec **Docker** + **docker compose** (plugin v2).
- Un **nom de domaine** (ex. `moisson.example.org`) dont l'enregistrement **A/AAAA pointe vers le
  VPS** (vérifier avant le 1er `up`, sinon le challenge ACME échoue).
- Ports **80 et 443** ouverts sur le VPS (Caddy obtient et renouvelle le certificat).
- Un **jeton Bearer** secret :
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

## 2. Configuration (`.env`)

```bash
cp .env.example .env
# Éditer .env et renseigner AU MINIMUM :
#   MOISSON_API_TOKEN=<le secret généré>
#   MOISSON_ALLOWED_HOSTS=moisson.example.org      # = votre domaine (anti DNS-rebinding)
#   MOISSON_PUBLIC_BASE_URL=https://moisson.example.org
#   MOISSON_DOMAIN=moisson.example.org             # pour Caddy/ACME
#   ACME_EMAIL=vous@example.org
```

> `.env` est dans `.gitignore` **et** `.dockerignore` — il n'est ni commité ni embarqué dans
> l'image. Le jeton est injecté au conteneur via `env_file`.

⚠️ Sans `MOISSON_API_TOKEN`, le serveur **refuse de démarrer** (pas de serveur anonyme). Sans
`MOISSON_ALLOWED_HOSTS` sur un bind non-loopback (le cas en Docker, `0.0.0.0`), il **refuse aussi**
(anti DNS-rebinding).

## 3. Lancement

```bash
docker compose build
docker compose up -d
docker compose ps           # app: healthy, caddy: running
docker compose logs -f app  # vérifier la ligne « Bearer *** : ACTIF | OAuth : … » (message « Bearer statique : … », le journal masque le mot qui suit « Bearer »)
```

> Le `Dockerfile` construit les wheels avec `pip wheel --wheel-dir /wheels -c constraints.txt ".[mcp]"` : l'image
> reprend exactement les versions testées (voir § 6, « Versions épinglées »). Ne pas retirer
> `-c constraints.txt` de cette ligne.

## 4. Vérification (smoke test)

```bash
# certificat valide + 401 sans jeton + handshake authentifié
./scripts/smoke_remote.sh moisson.example.org "$MOISSON_API_TOKEN"

# ou manuellement :
curl -I https://moisson.example.org/healthz          # 200, TLS valide (sans -k)
```

DoD attendue :
- `curl -I https://<domaine>/healthz` → **200**, chaîne TLS OK **sans** `-k`.
- `initialize` sur `https://<domaine>/mcp` + `Bearer` → **200** + `Mcp-Session-Id` ; sans/mauvais
  jeton → **401**.
- `tools/list` rend **12 outils** : les neuf historiques (`moisson_search`, `corpus_analyze`,
  `corpus_list`, `fetch_by_doi`, `search_by_author`, `corpus_merge`, `corpus_to_csl`,
  `corpus_filter`, `corpus_resolve_dois`) plus `corpus_export`, `corpus_query` et `corpus_diff`.
- Le port interne **8000 n'est PAS publié** sur l'hôte :
  ```bash
  docker compose port app 8000   # (vide)
  ss -tlnp | grep 8000 || echo "8000 non exposé sur l'hôte : OK"
  ```

## 5. Décision : **1 worker** uvicorn (stateful)

Les sessions Streamable HTTP **stateful** vivent dans un `dict` **process-local** du SDK. **Plusieurs
workers casseraient les sessions** (un appel peut tomber sur un worker sans la session → 404). Le
Sprint 09 exige le mode **stateful** pour délivrer la **progression SSE** des moissons longues.

➡️ **Recommandation retenue : `workers=1`** (l'ENTRYPOINT lance un seul uvicorn) — suffisant en
mono-utilisateur. Alternative `stateless_http=True` (scale horizontal) **perd la progression SSE** →
non retenue. Ne PAS ajouter `--workers N` ni `--reload` (le gestionnaire de sessions Streamable
`run()` n'est appelable qu'une fois par process).

## 6. Exploitation

### Mise à jour (sans perte de volume)

```bash
git pull
docker compose build
docker compose up -d            # recrée les conteneurs ; les volumes nommés persistent
```

Variante Nginx (§ 9) : `docker compose -f docker-compose.nginx.yml build`, puis
`docker compose -f docker-compose.nginx.yml up -d`.

Volumes : `moisson_output` (`/data/output` : corpus, manifestes, index `.index.json`, magasin
OAuth `.oauth/`, audit `.audit/`), `moisson_cache` (`/data/cache` : cache HTTP du serveur),
`caddy_data` et `caddy_config` (certificats). Après une mise à jour, l'index des corpus se
reconstruit seul à la première lecture ; `corpus_list(rebuild=true)` le force.

### Versions épinglées : SDK MCP `<2` et `constraints.txt`

- **Borne `mcp>=1.27,<2`** dans `pyproject.toml` (extras `mcp`, `http`, `remote`) et
  `requirements.txt`. La série **mcp 2.x retire les décorateurs** `Server.list_tools`,
  `call_tool`, `list_resources` (et, constaté sous 2.2.0, `read_resource`, `list_prompts`,
  `get_prompt`) sur lesquels repose `mcp_server.py` : `import mcp_server` lève
  `AttributeError: 'Server' object has no attribute 'list_tools'` et le conteneur refuse de
  démarrer. Sans borne, toute reconstruction tirait la dernière version publiée. La **migration
  vers mcp 2.x est un chantier distinct** (réécriture de l'enregistrement des outils, ressources et
  prompts), à planifier avec la révision du plan directeur distant ; ne pas lever la borne sans lui.
- **`constraints.txt`** (versionné) fige les versions **testées** de toutes les dépendances
  (runtime, transport HTTP, tests), vérifiées installables sous Python 3.10 à 3.12. Il est utilisé
  par le `Dockerfile` (`pip wheel -c constraints.txt`), par la CI et pour tout venv neuf :
  ```bash
  pip install -e ".[dev,mcp]" -c constraints.txt
  ```
- **Monter une version** : modifier la ligne dans `constraints.txt`, relancer la suite complète
  (`pytest tests/ -m "not integration"`, tests `sockets` compris), reconstruire l'image
  (`docker compose build`) et vérifier `/healthz` avant `docker compose up -d`.

### Sauvegarde / restauration du volume corpus

```bash
# Sauvegarde de moisson_output (corpus produits)
docker run --rm -v moisson_shs_moisson_output:/data -v "$PWD":/backup alpine \
  tar czf /backup/moisson_output_$(date +%F).tar.gz -C /data .

# Restauration
docker run --rm -v moisson_shs_moisson_output:/data -v "$PWD":/backup alpine \
  sh -c "cd /data && tar xzf /backup/moisson_output_AAAA-MM-JJ.tar.gz"
```

> Sauvegarder aussi le volume **`caddy_data`** (certificats Let's Encrypt) pour éviter une
> ré-émission (limite LE : 5/semaine/domaine). Le volume `moisson_cache` peut être perdu sans
> dommage (il se reconstitue) ; le magasin OAuth, lui, est dans `moisson_output`.

> Le préfixe des volumes (`moisson_shs_`) dépend du nom du projet compose (dossier). Vérifier avec
> `docker volume ls`.

### Persistance vérifiée

```bash
docker compose down && docker compose up -d
# puis via un client MCP : corpus_list retrouve les fichiers (volume intact).
```

### Rotation des logs

Les logs sont plafonnés dans `docker-compose.yml` (`json-file`, `max-size: 10m`, `max-file: 3`).

### Rotation du jeton (sans coupure)

Voir `REMOTE.md` : ajouter le nouveau jeton à `MOISSON_API_TOKENS` (liste), migrer les clients, puis
retirer l'ancien — fenêtre de recouvrement pendant laquelle **les deux** jetons sont valides. Les
jetons sont lus au démarrage : chaque changement de `.env` demande un `docker compose up -d`.

### Sélections de `corpus_query`

En HTTP, chaque appel de `corpus_query` publie sa sélection (`<radical>_sel_<uuid>.json` et son
manifeste) pour la rendre téléchargeable. Les dix dernières par corpus source sont gardées
(`MOISSON_QUERY_HTTP_KEEP`), les plus anciennes supprimées : le volume ne grossit pas sans borne.

## 7. Dépannage

| Symptôme | Cause probable | Action |
|----------|----------------|--------|
| `app` redémarre en boucle | `MOISSON_API_TOKEN` (ou OAuth complet) ou `MOISSON_ALLOWED_HOSTS` absent | `docker compose logs app` → renseigner `.env` |
| `app` redémarre, « REFUS : MOISSON_OAUTH_ENABLED=1 sans MOISSON_OAUTH_PASSWORD » | OAuth activé sans mot de passe de consentement ou sans `MOISSON_PUBLIC_BASE_URL` | renseigner les deux dans `.env` |
| `app` redémarre en boucle, `AttributeError: 'Server' object has no attribute 'list_tools'` | image construite avec mcp 2.x (contraintes retirées du `Dockerfile` ou borne `<2` levée) | rétablir `-c constraints.txt` et `mcp>=1.27,<2`, puis `docker compose build --no-cache` (§ 6, « Versions épinglées ») |
| 502 via Caddy | `app` pas `healthy` | `docker compose ps` ; vérifier `/healthz` interne |
| Certificat non émis | DNS non propagé / 80-443 fermés | vérifier l'A record + le pare-feu ; tenter ACME staging |
| 421 sur `/mcp` | `MOISSON_ALLOWED_HOSTS` ne matche pas l'en-tête `Host` reçu | aligner sur le **Host exact** transmis par le proxy. En 443 standard : `MOISSON_ALLOWED_HOSTS=moisson.example.org` (sans port). Si un port figure dans le Host, utiliser le wildcard `moisson.example.org:*`. `header_up Host {host}` est déjà dans le Caddyfile |
| Progression absente | `--stateless`/`json_response` activés | les laisser à 0 (stateful) |
| Moisson coupée à ~15 min | `MOISSON_DEADLINE_SECONDS` (900 s, échéance dure : rien n'est publié) | augmenter si besoin dans `.env` |
| « serveur occupé » | `MOISSON_MAX_CONCURRENT` opérations longues déjà en cours (moisson, auteur, DOI) | réessayer, ou relever `MOISSON_MAX_CONCURRENT` / `MOISSON_ACQUIRE_TIMEOUT` |
| Cache perdu à chaque reconstruction | `MOISSON_CACHE_DIR` retiré de l'image ou volume `moisson_cache` absent | rétablir `MOISSON_CACHE_DIR=/data/cache/http` et le volume (§ 0) |

## 8. Sécurité — rappels

- uvicorn **bind interne** (`expose: 8000`, jamais `ports:`) ; **Caddy** est le seul point d'entrée.
- Limite de corps posée **au proxy** (`request_body max_size 1MB`) **et** dans l'app (413).
- Access-logs : ne jamais logguer `Authorization` (la redaction applicative est en place ; ne pas
  l'ajouter au format de log Caddy).
- Détails du modèle de menace : `REMOTE.md`.

## 9. Variante : derrière un Nginx existant

Sur un hôte où Nginx termine déjà TLS pour plusieurs sites, `docker-compose.nginx.yml` ne lance que
l'application (pas de Caddy) et la publie sur le **loopback** de l'hôte, `127.0.0.1:8010` : seul Nginx
l'atteint. Mêmes volumes (`moisson_output`, `moisson_cache`) et même `.env` que la variante Caddy.

```bash
docker compose -f docker-compose.nginx.yml build
docker compose -f docker-compose.nginx.yml up -d
curl -I http://127.0.0.1:8010/healthz
```

Côté Nginx : `proxy_pass http://127.0.0.1:8010;`, transmettre l'en-tête `Host` d'origine (il doit
figurer dans `MOISSON_ALLOWED_HOSTS`), désactiver la mise en tampon pour la progression SSE
(`proxy_buffering off;`), prévoir un délai de lecture supérieur à `MOISSON_DEADLINE_SECONDS`, et ne
jamais journaliser l'en-tête `Authorization`. Certificat : `certbot --nginx`.
