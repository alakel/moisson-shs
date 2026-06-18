# DEPLOY.md — Déploiement du serveur MCP distant `moisson-shs`

Runbook de mise en production du **serveur MCP distant** (mode Streamable HTTP) dans un conteneur
**Docker** derrière **Caddy** (TLS automatique Let's Encrypt), sur un VPS.

> Vue d'ensemble sécurité/architecture : [`REMOTE.md`](REMOTE.md). Plan directeur :
> [`.claude/dev/sprint_remote_plan.md`](.claude/dev/sprint_remote_plan.md).

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
docker compose logs -f app  # vérifier « Authentification Bearer : ACTIVE »
```

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
> ré-émission (limite LE : 5/semaine/domaine).

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
retirer l'ancien — fenêtre de recouvrement pendant laquelle **les deux** jetons sont valides.

## 7. Dépannage

| Symptôme | Cause probable | Action |
|----------|----------------|--------|
| `app` redémarre en boucle | `MOISSON_API_TOKEN` ou `MOISSON_ALLOWED_HOSTS` absent | `docker compose logs app` → renseigner `.env` |
| 502 via Caddy | `app` pas `healthy` | `docker compose ps` ; vérifier `/healthz` interne |
| Certificat non émis | DNS non propagé / 80-443 fermés | vérifier l'A record + le pare-feu ; tenter ACME staging |
| 421 sur `/mcp` | `MOISSON_ALLOWED_HOSTS` ne matche pas l'en-tête `Host` reçu | aligner sur le **Host exact** transmis par le proxy. En 443 standard : `MOISSON_ALLOWED_HOSTS=moisson.example.org` (sans port). Si un port figure dans le Host, utiliser le wildcard `moisson.example.org:*`. `header_up Host {host}` est déjà dans le Caddyfile |
| Progression absente | `--stateless`/`json_response` activés | les laisser à 0 (stateful) |
| Moisson coupée à ~15 min | `MOISSON_DEADLINE_SECONDS` (900 s) | augmenter si besoin dans `.env` |

## 8. Sécurité — rappels

- uvicorn **bind interne** (`expose: 8000`, jamais `ports:`) ; **Caddy** est le seul point d'entrée.
- Limite de corps posée **au proxy** (`request_body max_size 1MB`) **et** dans l'app (413).
- Access-logs : ne jamais logguer `Authorization` (la redaction applicative est en place ; ne pas
  l'ajouter au format de log Caddy).
- Détails du modèle de menace : `REMOTE.md`.
