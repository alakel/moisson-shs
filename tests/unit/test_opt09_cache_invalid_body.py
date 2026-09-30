"""
Vérification finale du lot B2 : le cache HTTP ne garde jamais un corps non JSON.

Le cache est actif par défaut dans moisson_search depuis OPT-09 (TTL 6 h).
requests-cache mettait en cache toute réponse 200, y compris une page
d'erreur HTML servie avec ce statut (corps illisible) : l'appel suivant la
resservait sans aucune requête, et la source restait en échec « corps
illisible » pendant toute la durée de vie du cache. Constaté par le client
stdio réel sur le simulateur : une seule page HTML de HAL, puis trois appels
identiques, trois fois HAL en échec et aucune requête après le premier.

Seul le flux Isidore retirait son entrée fautive (``_evict_cached``) ; HAL,
CrossRef, OpenAlex, Semantic Scholar, CORE et SerpAPI ne le faisaient pas.
Le filtre ``_cacheable_response`` (``filter_fn`` de requests-cache) règle le
cas pour toutes les sources, et purge une entrée fautive déjà stockée.

Aucun accès réseau : adaptateur requests local scripté.
"""

import io

import pytest
import urllib3
from requests.adapters import BaseAdapter, HTTPAdapter

from connectors.hal import HALConnector
from engine.models import resolve_source_status
from utils import http as http_mod
from utils.http import HTTPClient, reset_http_policy

HTML = b"<html><body>Service momentanement indisponible</body></html>"
SOLR_EMPTY = b'{"response": {"numFound": 0, "docs": []}}'


class _ScriptedAdapter(BaseAdapter):
    """Renvoie les corps de ``bodies`` dans l'ordre (le dernier ensuite)."""

    def __init__(self, bodies, content_type="text/html"):
        super().__init__()
        self.bodies = list(bodies)
        self.content_type = content_type
        self.calls = 0

    def send(self, request, **kw):
        body = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        ctype = "application/json" if body[:1] in (b"{", b"[") else self.content_type
        raw = urllib3.HTTPResponse(
            body=io.BytesIO(body), status=200, preload_content=False,
            headers={"Content-Type": ctype}, request_url=request.url)
        return HTTPAdapter().build_response(request, raw)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_policy(monkeypatch):
    reset_http_policy()
    monkeypatch.delenv("MOISSON_CACHE", raising=False)
    yield
    reset_http_policy()


def _client(tmp_path, adapter) -> HTTPClient:
    client = HTTPClient(use_cache=True, cache_ttl=21600,
                        cache_dir=str(tmp_path / "http_mcp"))
    client.session.mount("https://", adapter)
    return client


def _get(client, url="https://api.archives-ouvertes.fr/search/", q="discours"):
    return client.get(url, params={"q": q}, min_interval=0)


class _Resp:
    def __init__(self, status, content):
        self.status_code = status
        self.content = content


@pytest.mark.parametrize("status,content,expected", [
    (200, b'{"a": 1}', True),
    (200, b'  \n[1, 2]', True),
    (200, HTML, False),
    (200, b"", False),
    (200, b'{"a": tronqu', True),   # corps tronqué : le décodage échouera,
    # mais seul le premier octet est examiné (coût borné) ; le cas réel
    # observé est la page HTML.
    (203, b"<html/>", False),
    (301, b"<html>Moved</html>", True),   # redirection : non concernée
    (308, b"", True),
])
def test_cacheable_response_rule(status, content, expected):
    assert http_mod._cacheable_response(_Resp(status, content)) is expected


def test_unreadable_body_is_not_cacheable():
    class _Broken:
        status_code = 200

        @property
        def content(self):
            raise RuntimeError("flux coupé")

    assert http_mod._cacheable_response(_Broken()) is False


def test_html_page_served_with_200_is_not_cached(tmp_path):
    adapter = _ScriptedAdapter([HTML, SOLR_EMPTY])
    client = _client(tmp_path, adapter)
    try:
        first = _get(client)
        assert first.content == HTML
        # Appel suivant : la page HTML n'a pas été gardée, la requête part.
        second = _get(client)
        assert adapter.calls == 2
        assert second.json() == {"response": {"numFound": 0, "docs": []}}
        # Le JSON valide, lui, est bien mis en cache.
        third = _get(client)
        assert adapter.calls == 2
        assert getattr(third, "from_cache", False) is True
    finally:
        client.close()


def test_invalid_body_stored_before_the_filter_is_purged(tmp_path):
    """Une entrée fautive écrite sans le filtre (cache d'avant correction)
    n'est plus servie : la requête part, l'entrée est remplacée."""
    import requests_cache

    cache_name = str(tmp_path / "http_mcp")
    legacy = requests_cache.CachedSession(
        cache_name=cache_name, backend="sqlite", allowable_codes=[200],
        match_headers=False)
    legacy_adapter = _ScriptedAdapter([HTML])
    legacy.mount("https://", legacy_adapter)
    legacy.get("https://api.archives-ouvertes.fr/search/", params={"q": "discours"})
    assert legacy.get("https://api.archives-ouvertes.fr/search/",
                      params={"q": "discours"}).from_cache is True
    legacy.close()

    adapter = _ScriptedAdapter([SOLR_EMPTY])
    client = _client(tmp_path, adapter)
    try:
        resp = _get(client)
        assert adapter.calls == 1
        assert resp.json()["response"]["numFound"] == 0
        assert getattr(resp, "from_cache", False) is False
    finally:
        client.close()


def test_hal_recovers_on_the_next_call_after_a_transient_html_page(tmp_path):
    """Scénario constaté par le client stdio : HAL renvoie une fois une page
    HTML (statut 200). Le premier appel est en échec « corps illisible » ;
    le suivant interroge de nouveau HAL et n'est plus en échec."""
    adapter = _ScriptedAdapter([HTML, SOLR_EMPTY])
    client = _client(tmp_path, adapter)
    try:
        hal = HALConnector(http_client=client)
        assert hal.search("discours numérique", max_results=5) == []
        status, reason, _detail = resolve_source_status(hal.last_status, 0)
        assert (status, reason) == ("error", "invalid_body")

        hal2 = HALConnector(http_client=client)
        assert hal2.search("discours numérique", max_results=5) == []
        assert adapter.calls == 2
        assert resolve_source_status(hal2.last_status, 0)[0] == "empty"
    finally:
        client.close()
