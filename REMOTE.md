# REMOTE.md — Serveur MCP distant `moisson-shs`

Guide complet du **serveur MCP distant** : un agent (Claude Desktop, `.mcp.json`, agent custom)
appelle les 8 outils de moisson via **HTTPS authentifié**, sans installation locale. Le serveur est
le **même** `Server("moisson-shs")` que le mode stdio (8 outils inchangés) ; seul le **transport**
change.

> Mise en production pas-à-pas (Docker + TLS) : [`DEPLOY.md`](DEPLOY.md).
> Architecture détaillée : [`.claude/dev/sprint_remote_plan.md`](.claude/dev/sprint_remote_plan.md).

## 1. Vue d'ensemble

```
 agent distant ──HTTPS+Bearer──▶ Caddy (TLS) ──http──▶ uvicorn (1 worker)
                                                          └ Server("moisson-shs")
                                                            • /mcp        (Streamable HTTP, auth)
                                                            • /download/… (corpus, auth)
                                                            • /healthz    (public)
                                                            • /metrics    (auth)
```

Différences clés avec le mode **stdio** (local, Claude Desktop) :

| Aspect | stdio (local) | HTTP (distant) |
|--------|---------------|----------------|
| Auth | aucune (process de confiance) | **Bearer obligatoire** ; refus de démarrer sans jeton |
| Retour des outils | JSON inline + `corpus://` | **URLs de téléchargement** ; jamais de JSON inline ni de chemin disque |
| Chemins d'entrée | libres | **confinés sous `OUTPUT_DIR`** (anti-traversal) |
| Moissons longues | best-effort | progression SSE + deadline + concurrence bornée + plafond anti-DoS |
| Observabilité | logs plats | logs **JSON**, **audit JSONL**, `/metrics` |

## 2. Démarrer (rapide)

### Option A — Docker + Caddy (recommandé, prod)

Voir [`DEPLOY.md`](DEPLOY.md). En résumé :

```bash
cp .env.example .env          # renseigner MOISSON_API_TOKEN, MOISSON_ALLOWED_HOSTS,
                              # MOISSON_PUBLIC_BASE_URL, MOISSON_DOMAIN, ACME_EMAIL
docker compose up -d
./scripts/smoke_remote.sh moisson.example.org "$MOISSON_API_TOKEN"
```

### Option B — local (dev / derrière votre propre TLS)

```bash
pip install -e ".[remote]"
moisson-shs token generate                 # secret affiché UNE fois → MOISSON_API_TOKEN
export MOISSON_API_TOKEN=<le secret>
moisson-shs serve --http --host 127.0.0.1 --port 8000
```

## 3. Gestion des jetons (`moisson-shs token`)

```bash
moisson-shs token generate     # crée un jeton (secret affiché 1×), stocke son sha256 (0600)
moisson-shs token status       # empreintes + dates, SANS révéler le secret
moisson-shs token rotate       # nouveau jeton ; l'ancien reste valide (recouvrement)
moisson-shs token revoke --fingerprint <empreinte>   # révoque l'ancien après migration
moisson-shs token revoke       # révoque TOUS
```

- Le **secret** n'est jamais persisté : seul son **sha256** (fichier `0600`,
  `~/.config/moisson_shs/tokens.json` ou `MOISSON_TOKEN_STORE`).
- Le serveur s'authentifie via `MOISSON_API_TOKEN(S)` (secret en env, injecté au conteneur).
- **Rotation sans coupure** : `MOISSON_API_TOKENS="ancien,nouveau"` (les deux valides), migrer les
  clients, puis retirer l'ancien.

## 4. Config client (`moisson-shs setup --remote`)

```bash
moisson-shs setup --remote https://moisson.example.org/mcp --token "$MOISSON_API_TOKEN"
```

Imprime un bloc copiable (sans aucun chemin disque local du serveur) :

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

> Pour un **connecteur distant claude.ai**, renseigner l'URL et l'en-tête `Authorization` dans l'UI
> dédiée. Un connecteur **public multi-utilisateurs** exigerait OAuth 2.1 (hors périmètre, cf. plan
> directeur §9.1 — le SDK fournit déjà les briques pour l'ajouter sans toucher aux outils).

## 5. Variables d'environnement

| Variable | Défaut | Rôle |
|----------|--------|------|
| `MOISSON_API_TOKEN` | *(vide ⇒ refus)* | Jeton Bearer attendu. **Obligatoire en HTTP.** |
| `MOISSON_API_TOKENS` | *(option)* | Jetons valides additionnels (rotation, séparés par virgule). |
| `MOISSON_PUBLIC_BASE_URL` | *(none)* | Base HTTPS publique pour les URLs de téléchargement. |
| `MOISSON_OUTPUT_DIR` | `<projet>/output` | Espace des corpus = **racine de la sandbox**. |
| `MOISSON_HTTP_HOST` | `127.0.0.1` | Interface uvicorn (Docker derrière proxy : `0.0.0.0`). |
| `MOISSON_HTTP_PORT` | `8000` | Port uvicorn. |
| `MOISSON_HTTP_PATH` | `/mcp` | Chemin de la route Streamable. |
| `MOISSON_ALLOWED_HOSTS` | *(obligatoire si bind ≠ loopback)* | Allowlist Host (anti DNS-rebinding). |
| `MOISSON_ALLOWED_ORIGINS` | *(vide)* | Allowlist Origin. |
| `MOISSON_HTTP_MAX_BODY` | `1000000` | Taille max du corps (413 au-delà). |
| `MOISSON_RATE_LIMIT` | *(0=off)* | Token-bucket/IP/min (429). Garde-fou de dernier recours. |
| `MOISSON_MAX_ITEMS_PER_SOURCE` | `3000` | Plafond serveur dur (clamp anti-DoS). |
| `MOISSON_MAX_CONCURRENT` | `2` | Moissons simultanées max (sémaphore). |
| `MOISSON_DEADLINE_SECONDS` | `900` | Deadline serveur d'une moisson. |
| `MOISSON_HTTP_STATELESS` / `MOISSON_HTTP_JSON_RESPONSE` | `0` | **Laisser à 0** (sinon pas de progression SSE). |
| `MOISSON_TOKEN_STORE` | `~/.config/moisson_shs/tokens.json` | Fichier d'empreintes (CLI `token`). |
| `MOISSON_DOMAIN` / `ACME_EMAIL` | — | Docker/Caddy (TLS). |

## 6. Observabilité

- **Logs JSON** sur stderr (1 ligne/événement : `ts`, `level`, `msg`, + `request_id`/`tool`/
  `duration_ms`/`status`/`client_id`). **Redaction** systématique des `Bearer …`/`Authorization`.
- **Audit** : 1 ligne JSONL/appel d'outil dans `OUTPUT_DIR/.audit/audit-YYYYMMDD.jsonl`
  (`ts`, `request_id`, `client_id`, `tool`, `arg_keys` — **pas les valeurs**, `duration_ms`,
  `status`, `item_count`).
- **`GET /healthz`** (public) : `{status, version, mode, uptime_s, output_dir_writable}` — aucun
  chemin, aucun secret.
- **`GET /metrics`** (**derrière auth**) : `{calls_total, errors_total, by_tool, duration_ms_p50,
  duration_ms_p95}`. Compteurs **en mémoire** (perdus au redémarrage ; cohérent `workers=1`).

## 7. Checklist sécurité

- [ ] `MOISSON_API_TOKEN` défini (sinon le serveur **refuse de démarrer**).
- [ ] `MOISSON_ALLOWED_HOSTS` = domaine public exact (sinon refus sur bind non-loopback / 421).
- [ ] uvicorn **bind interne** (`expose`, jamais `ports:`) ; **Caddy** seul point d'entrée (TLS).
- [ ] `MOISSON_PUBLIC_BASE_URL` en `https://`.
- [ ] `.env` hors Git/Docker (déjà dans `.gitignore`/`.dockerignore`).
- [ ] Volume `caddy_data` sauvegardé (certificats).
- [ ] Limite de corps au proxy (`request_body max_size`) **et** dans l'app (413).
- [ ] Jeton secret généré par `secrets` (≥ 32 octets) ; rotation documentée.

## 8. Modèle de menace (STRIDE léger)

| Menace | Vecteur | Mitigation |
|--------|---------|------------|
| **S**poofing | Accès anonyme aux outils | Bearer obligatoire ; `compare_digest` (bytes) ; refus sans jeton |
| **T**ampering | Path traversal (`corpus_path`, `output_path`, dérivés, `/download`) | Sandbox `resolve_in_output_dir` (rejet `..`/absolu hors zone/symlink) ; chemins dérivés confinés ; `/download` confiné dur sous `OUTPUT_DIR` (sans repli registre) + whitelist d'extensions |
| **R**epudiation | Pas de trace | Audit JSONL par appel (`request_id`, `client_id`, `tool`, durée, statut) |
| **I**nfo. disclosure | Fuite de chemins/jetons | HTTP n'émet que des URLs ; redaction de toute `str` (corpus_list) et des logs ; `AccessToken.token="***"` ; `/healthz` sans chemin ; `/metrics` derrière auth |
| **D**enial of service | Moisson géante, corps géant, flood | Plafond `MAX_ITEMS_PER_SOURCE` (clamp) + concurrence bornée + deadline ; limite de corps (413, header + chunked) au proxy et dans l'app ; rate-limit optionnel (429) |
| **E**levation of privilege | DNS-rebinding / Host spoofing | `TransportSecuritySettings` (allowed_hosts/origins) ; refus bind public sans allowlist ; `header_up Host` cohérent au proxy |

### Hors périmètre (évolutions futures)

- **OAuth 2.1** (connecteur public multi-utilisateurs) : remplacer `StaticTokenVerifier` par un
  `ProviderTokenVerifier` + `create_auth_routes` — **sans toucher aux outils**.
- **Multi-tenant isolé** : sous-répertoire `OUTPUT_DIR/<client_id>/` dérivé de l'`AccessToken` ;
  la sandbox centralisée rend l'extension localisée.
