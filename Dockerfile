# syntax=docker/dockerfile:1
#
# Image du serveur MCP distant moisson-shs (mode Streamable HTTP).
# Multi-stage : un builder compile/collecte les wheels (lxml & co), un runtime
# mince et NON-ROOT exécute le serveur. Pensée pour tourner DERRIÈRE un reverse
# proxy TLS (Caddy) : uvicorn binde 0.0.0.0 dans le réseau Docker privé, le port
# n'est jamais publié sur l'hôte (cf. docker-compose.yml `expose` vs `ports`).

# === Stage builder : collecte des wheels ===
FROM python:3.12-slim AS builder

# Libs natives pour compiler lxml si aucun wheel manylinux n'est disponible.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY . /build

# Construit les wheels du projet + de toutes ses dépendances (extra [mcp]).
RUN pip install --upgrade pip wheel \
    && pip wheel --wheel-dir /wheels ".[mcp]"


# === Stage runtime : image mince, non-root ===
FROM python:3.12-slim AS runtime

# Libs natives runtime de lxml (insurance si wheel non statique).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Utilisateur non-root (uid fixe pour la propriété du volume).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin moisson

# Installe le projet UNIQUEMENT depuis les wheels (pas de toolchain au runtime).
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels "moisson-shs[mcp]" \
    && rm -rf /wheels

# Répertoire de données persistant (corpus + cache), monté en volume.
ENV MOISSON_OUTPUT_DIR=/data/output \
    MOISSON_HTTP_HOST=0.0.0.0 \
    MOISSON_HTTP_PORT=8000 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data/output /data/cache && chown -R 10001:10001 /data

USER 10001
WORKDIR /data

EXPOSE 8000

# Healthcheck : route publique minimale /healthz (Sprint 06).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD \
    python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# Démarre le serveur HTTP. L'auth Bearer + l'allowlist Host sont injectées par
# l'environnement (MOISSON_API_TOKEN, MOISSON_ALLOWED_HOSTS — cf. .env). Sans
# jeton, le serveur REFUSE de démarrer (garde-fou Sprint 07).
ENTRYPOINT ["moisson-shs", "serve", "--http", "--host", "0.0.0.0", "--port", "8000"]
