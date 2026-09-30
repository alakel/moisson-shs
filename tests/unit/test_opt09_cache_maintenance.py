"""
Revue d'OPT-09 : entretien du fichier de cache du serveur MCP.

Le cache HTTP est actif par défaut dans moisson_search (TTL 6 h). requests-cache
ne retire jamais une réponse expirée (il ne la remplace que si la même requête
revient) : sans entretien, chaque moisson distincte ajoutait ses réponses au
fichier pour toujours (environ 15 Mo par moisson francophone). Le serveur
retire désormais, au plus une fois par ``MCP_CACHE_PRUNE_INTERVAL`` et par
fichier, les réponses expirées, puis les plus anciennes au-delà de
``MCP_CACHE_MAX_MB``, et compacte le fichier quand l'espace libre domine.

Aucun accès réseau : adaptateur requests local, entrées expirées simulées en
SQL (colonne ``expires``), sans attente réelle.
"""

import io
import sqlite3
from pathlib import Path

import pytest
import urllib3
from requests.adapters import BaseAdapter, HTTPAdapter

import config
import mcp_server
from utils import http as http_mod
from utils.http import HTTPClient, prune_sqlite_cache, reset_http_policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BODY_SIZE = 200_000   # octets par réponse simulée


class _LocalAdapter(BaseAdapter):
    """Adaptateur requests sans réseau : réponse JSON de ``BODY_SIZE`` octets."""

    def send(self, request, **kw):
        body = b'{"x": "' + b"a" * BODY_SIZE + b'"}'
        raw = urllib3.HTTPResponse(
            body=io.BytesIO(body), status=200, preload_content=False,
            headers={"Content-Type": "application/json"},
            request_url=request.url)
        return HTTPAdapter().build_response(request, raw)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Politesse remise à zéro ; mémoire d'entretien propre au test."""
    reset_http_policy()
    monkeypatch.setattr(mcp_server, "_MCP_CACHE_MAINTAINED_AT", {})
    monkeypatch.delenv("MOISSON_CACHE", raising=False)
    yield
    reset_http_policy()


def _client(cache_dir: str) -> HTTPClient:
    client = HTTPClient(use_cache=True, cache_ttl=config.MCP_CACHE_TTL,
                        cache_dir=cache_dir)
    client.session.mount("https://", _LocalAdapter())
    return client


def _harvest_like(client: HTTPClient, query: str, pages: int = 2) -> None:
    for page in range(1, pages + 1):
        # Sans politesse : aucun serveur réel derrière l'adaptateur local.
        client.get("https://api.isidore.science/resource/search",
                   params={"q": query, "page": page}, min_interval=0)


def _db(cache_dir: str) -> str:
    return cache_dir + ".sqlite"


def _rows(cache_dir: str) -> int:
    con = sqlite3.connect(_db(cache_dir))
    try:
        return con.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
    finally:
        con.close()


def _expire_all(cache_dir: str) -> None:
    """Simule le passage de la durée de vie : toutes les réponses expirées."""
    con = sqlite3.connect(_db(cache_dir), isolation_level=None)
    try:
        con.execute("UPDATE responses SET expires = 1")
    finally:
        con.close()


# === Constantes ===

def test_constants():
    assert config.MCP_CACHE_PRUNE_INTERVAL == 3600
    assert config.MCP_CACHE_MAX_MB == 512
    assert mcp_server.MCP_CACHE_PRUNE_INTERVAL == config.MCP_CACHE_PRUNE_INTERVAL
    assert mcp_server.MCP_CACHE_MAX_MB == config.MCP_CACHE_MAX_MB


# === Le constat de la revue ===

def test_expired_responses_are_purged_by_the_next_mcp_client(tmp_path):
    """
    Deux moissons distinctes, la première expirée avant la seconde : avant
    la revue, le fichier gardait les réponses de chaque moisson pour toujours
    (4, 6, 8… réponses). Désormais, le client MCP suivant retire les
    réponses expirées et compacte le fichier.
    """
    cache_dir = str(tmp_path / "http_mcp")
    first = _client(cache_dir)
    # 6 pages de 200 Ko : l'espace libéré dépasse le minimum de compactage
    # (1 Mio).
    _harvest_like(first, "requete 0", pages=6)
    first.close()
    assert _rows(cache_dir) == 6
    _expire_all(cache_dir)
    size_before = Path(_db(cache_dir)).stat().st_size

    second = _client(cache_dir)
    stats = mcp_server._maintain_mcp_cache(second, cache_dir)
    assert stats["expired_removed"] == 6 and stats["size_removed"] == 0
    assert stats["vacuumed"] is True
    assert _rows(cache_dir) == 0
    assert Path(_db(cache_dir)).stat().st_size < size_before / 4
    _harvest_like(second, "requete 1")
    second.close()
    assert _rows(cache_dir) == 2


def test_growth_is_bounded_over_successive_distinct_harvests(tmp_path, monkeypatch):
    """Série de moissons distinctes, chacune expirée avant la suivante : le
    fichier ne garde que la dernière ; sous le minimum de compactage, les
    pages libérées sont réutilisées et la taille se stabilise dès la
    deuxième (avant : croissance linéaire, une moisson de plus à chaque
    fois)."""
    monkeypatch.setattr(mcp_server, "MCP_CACHE_PRUNE_INTERVAL", 0)
    cache_dir = str(tmp_path / "http_mcp")
    sizes = []
    for i in range(4):
        client = _client(cache_dir)
        mcp_server._maintain_mcp_cache(client, cache_dir)
        _harvest_like(client, f"requete {i}")
        client.close()
        assert _rows(cache_dir) == 2
        sizes.append(Path(_db(cache_dir)).stat().st_size)
        _expire_all(cache_dir)
    assert sizes[1] == sizes[2] == sizes[3], sizes
    assert sizes[3] < 2 * sizes[0], sizes


def test_run_moisson_sync_maintains_the_cache_before_harvesting(tmp_path,
                                                                monkeypatch):
    """Le chemin réel de moisson_search entretient le fichier du serveur."""
    monkeypatch.setenv("MOISSON_CACHE_DIR", str(tmp_path / "http"))
    cache_dir = mcp_server.mcp_cache_dir()
    stale = _client(cache_dir)
    _harvest_like(stale, "ancienne requete", pages=3)
    stale.close()
    _expire_all(cache_dir)
    seen = {}

    def run_source(name, *a, **k):
        seen["rows_at_harvest"] = _rows(cache_dir)
        return []

    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("engine.orchestrator.run_source", run_source)
    mcp_server._run_moisson_sync("q", 5, ["hal"], False, True)
    assert seen["rows_at_harvest"] == 0
    assert cache_dir in mcp_server._MCP_CACHE_MAINTAINED_AT


# === Fréquence ===

def test_maintenance_runs_at_most_once_per_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "MCP_CACHE_PRUNE_INTERVAL", 3600)
    cache_dir = str(tmp_path / "http_mcp")
    client = _client(cache_dir)
    assert mcp_server._maintain_mcp_cache(client, cache_dir) is not None
    _harvest_like(client, "q")
    _expire_all(cache_dir)
    # Entretien récent : rien n'est retiré avant la fin de l'intervalle.
    assert mcp_server._maintain_mcp_cache(client, cache_dir) is None
    assert _rows(cache_dir) == 2
    # Intervalle écoulé : nouveau passage.
    mcp_server._MCP_CACHE_MAINTAINED_AT[cache_dir] -= 3601
    assert mcp_server._maintain_mcp_cache(client, cache_dir)["expired_removed"] == 2
    client.close()


def test_zero_interval_maintains_every_time_negative_never(tmp_path, monkeypatch):
    cache_dir = str(tmp_path / "http_mcp")
    client = _client(cache_dir)
    monkeypatch.setattr(mcp_server, "MCP_CACHE_PRUNE_INTERVAL", 0)
    assert mcp_server._maintain_mcp_cache(client, cache_dir) is not None
    assert mcp_server._maintain_mcp_cache(client, cache_dir) is not None
    monkeypatch.setattr(mcp_server, "MCP_CACHE_PRUNE_INTERVAL", -1)
    monkeypatch.setattr(mcp_server, "_MCP_CACHE_MAINTAINED_AT", {})
    _harvest_like(client, "q")
    _expire_all(cache_dir)
    assert mcp_server._maintain_mcp_cache(client, cache_dir) is None
    assert _rows(cache_dir) == 2
    client.close()


# === Robustesse ===

def test_disabled_cache_is_not_maintained(tmp_path):
    client = HTTPClient(use_cache=False, cache_dir=str(tmp_path / "http_mcp"))
    assert client.prune_cache() is None
    assert mcp_server._maintain_mcp_cache(client, str(tmp_path / "http_mcp")) is None
    assert not (tmp_path / "http_mcp.sqlite").exists()


def test_maintenance_failure_never_blocks_the_harvest(tmp_path, caplog):
    class _Broken:
        cache_enabled = True

        def prune_cache(self, **kw):
            raise sqlite3.DatabaseError("file is not a database")

    with caplog.at_level("WARNING", logger="moisson_mcp"):
        assert mcp_server._maintain_mcp_cache(_Broken(), str(tmp_path / "x")) is None
    assert "Entretien du cache HTTP MCP impossible" in caplog.text


def test_locked_vacuum_is_skipped_without_error(tmp_path, monkeypatch):
    """Compactage impossible (verrou tenu ailleurs) : reporté, sans erreur ;
    les réponses expirées sont retirées quand même."""
    cache_dir = str(tmp_path / "http_mcp")
    client = _client(cache_dir)
    _harvest_like(client, "q")
    _expire_all(cache_dir)
    real_connect = sqlite3.connect

    class _LockedConnection:
        def execute(self, sql, *a):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    def connect(path, *a, **k):
        if str(path) == _db(cache_dir) and k.get("timeout") == 1.0:
            return _LockedConnection()
        return real_connect(path, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", connect)
    stats = client.prune_cache()
    assert stats["expired_removed"] == 2 and stats["vacuumed"] is False
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert _rows(cache_dir) == 0
    client.close()


def test_no_vacuum_when_the_remaining_data_is_large(tmp_path, monkeypatch):
    """Au-delà de _VACUUM_MAX_LIVE_BYTES conservés, pas de compactage (copie
    trop longue pour une autre moisson qui attendrait le verrou)."""
    monkeypatch.setattr(http_mod, "_VACUUM_MAX_LIVE_BYTES", 1000)
    cache_dir = str(tmp_path / "http_mcp")
    client = _client(cache_dir)
    _harvest_like(client, "ancienne", pages=6)
    _expire_all(cache_dir)
    _harvest_like(client, "recente", pages=1)
    stats = client.prune_cache()
    assert stats["expired_removed"] == 6 and stats["vacuumed"] is False
    assert _rows(cache_dir) == 1
    client.close()


# === Plafond de taille ===

def test_size_cap_removes_the_oldest_responses_first(tmp_path):
    cache_dir = str(tmp_path / "http_mcp")
    client = _client(cache_dir)
    _harvest_like(client, "q", pages=5)
    # Échéances distinctes : la page 1 est la plus ancienne.
    con = sqlite3.connect(_db(cache_dir), isolation_level=None)
    keys = [k for (k,) in con.execute("SELECT key FROM responses")]
    order = {}
    for key in keys:
        value = client.session.cache.responses[key]
        page = int(value.url.rsplit("page=", 1)[1].split("&")[0])
        order[key] = page
        con.execute("UPDATE responses SET expires = ? WHERE key = ?",
                    (4_000_000_000 + page, key))
    one = con.execute("SELECT MAX(length(value)) FROM responses").fetchone()[0]
    con.close()
    stats = prune_sqlite_cache(client.session.cache, max_bytes=int(one * 2.5),
                               vacuum=False)
    assert stats["expired_removed"] == 0 and stats["size_removed"] == 3
    assert stats["live_bytes"] <= one * 2.5
    remaining = sorted(order[k] for k in client.session.cache.responses.keys())
    assert remaining == [4, 5]
    client.close()


# === Déploiement Docker ===

def test_docker_image_puts_the_cache_in_the_volume():
    """Le cache du serveur va dans le volume /data/cache (docker-compose),
    pas dans la couche inscriptible du conteneur."""
    text = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "MOISSON_CACHE_DIR=/data/cache/http" in text
    for compose in ("docker-compose.yml", "docker-compose.nginx.yml"):
        assert "/data/cache" in (PROJECT_ROOT / compose).read_text(encoding="utf-8")
