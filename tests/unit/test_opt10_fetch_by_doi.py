# -*- coding: utf-8 -*-
"""
OPT-10 : fetch_by_doi en parallèle, ordre de préférence, vérification du DOI,
erreur réseau distinguée de « non trouvé ».

Un vrai ``HTTPClient`` est utilisé (politique commune : politesse par hôte,
une tentative sur 4xx), sur une session simulée qui répond par hôte avec une
latence réelle courte. Aucun réseau.
"""

import copy
import json
import threading
import time
from urllib.parse import urlsplit

import pytest
import requests

import utils.http as http_mod
from utils.http import HTTPClient

DOI = "10.3917/comla.204.0045"

CROSSREF = "api.crossref.org"
HAL = "api.archives-ouvertes.fr"
ISIDORE = "api.isidore.science"
S2 = "api.semanticscholar.org"


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.from_cache = False
        self.reason = ""
        self.url = ""

    def json(self):
        return self._payload


class _RoutedSession:
    """Session simulée : un gestionnaire et une latence (réelle) par hôte."""

    def __init__(self, routes, latency=None):
        self.routes = routes
        self.latency = latency or {}
        self.calls = []
        self.headers = {}
        self._lock = threading.Lock()

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        host = urlsplit(url).hostname
        with self._lock:
            self.calls.append((host, url, dict(params or {})))
        delay = self.latency.get(host, 0.0)
        if delay:
            time.sleep(delay)
        handler = self.routes.get(host)
        result = handler(url, params or {}) if handler else _Resp(404)
        if isinstance(result, BaseException):
            raise result
        return result

    def mount(self, *a):
        pass

    def close(self):
        pass


def crossref_ok(doi=DOI, title="Titre selon CrossRef"):
    return lambda url, params: _Resp(200, {"message": {
        "DOI": doi, "type": "journal-article", "title": [title],
        "author": [{"family": "Dupont", "given": "Anne"}],
        "issued": {"date-parts": [[2020, 5, 1]]},
        "container-title": ["Communication et langages"],
    }})


def hal_ok(doi=DOI, title="Titre selon HAL"):
    return lambda url, params: _Resp(200, {"response": {"numFound": 1, "docs": [{
        "docid": 1, "halId_s": "hal-0001", "title_s": [title],
        "doiId_s": doi, "authFullName_s": ["Anne Dupont"],
        "authLastName_s": ["Dupont"], "authFirstName_s": ["Anne"],
        "producedDate_s": "2020", "docType_s": "ART",
        "uri_s": "https://hal.science/hal-0001",
    }]}})


def isidore_empty(url, params):
    return _Resp(200, {"response": {"replies": {
        "meta": {"@items": "0"}, "content": {"reply": []}}}})


def not_found(url, params):
    return _Resp(404)


def _wait_resolvers_idle(timeout=5.0):
    """Attend la fin des résolveurs abandonnés d'un test (requêtes déjà
    parties) : ils occupent la place de résolveur de leur hôte
    (FETCH_BY_DOI_HOST_SLOTS) et retarderaient le test suivant."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(t.name.startswith("fetch-by-doi")
                   for t in threading.enumerate()):
            return
        time.sleep(0.02)


@pytest.fixture(autouse=True)
def _policy_reset(monkeypatch):
    for var in ("OPENALEX_API_KEY", "CORE_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    http_mod.reset_http_policy()
    yield
    _wait_resolvers_idle()
    http_mod.reset_http_policy()


def _client(routes, latency=None):
    client = HTTPClient(use_cache=False)
    client.session = _RoutedSession(routes, latency)
    return client


def _routes(**overrides):
    routes = {CROSSREF: crossref_ok(), HAL: hal_ok(), ISIDORE: isidore_empty,
              S2: not_found}
    routes.update(overrides)
    return routes


def _status_of(result, name):
    return next(r["status"] for r in result["resolvers"] if r["name"] == name)


# === Parallélisme ===

class TestParallel:
    def test_duration_close_to_max_not_sum(self):
        from utils.lookups import lookup_doi
        latency = {CROSSREF: 0.3, HAL: 0.3, ISIDORE: 0.3, S2: 0.3}
        client = _client(_routes(), latency)
        t0 = time.monotonic()
        result = lookup_doi(DOI, client)
        elapsed = time.monotonic() - t0
        assert result["status"] == "trouve"
        # Somme des latences : 1,2 s ; maximum : 0,3 s.
        assert elapsed < 0.7, elapsed
        hosts = sorted(h for h, _, _ in client.session.calls)
        assert hosts == sorted([CROSSREF, HAL, ISIDORE, S2])

    def test_crossref_request_joins_the_polite_pool(self):
        """Sans ``mailto``, CrossRef range /works/{doi} dans le pool public
        (5 requêtes/s, une seule simultanée, relevé live du 2026-09-28)."""
        import config
        from utils.lookups import lookup_doi
        client = _client(_routes())
        lookup_doi(DOI, client)
        crossref = [p for h, _, p in client.session.calls if h == CROSSREF]
        assert crossref == [{"mailto": config.CONTACT_EMAIL}]

    def test_fetch_by_doi_keeps_its_historical_signature(self):
        from utils.lookups import fetch_by_doi
        item = fetch_by_doi("https://doi.org/" + DOI.upper(),
                            http_client=_client(_routes()))
        assert item and item["DOI"] == DOI


# === Ordre de préférence ===

class TestPreferenceOrder:
    @pytest.mark.parametrize("latency", [
        {CROSSREF: 0.25, HAL: 0.0},     # le prioritaire arrive en dernier
        {CROSSREF: 0.0, HAL: 0.25},     # le prioritaire arrive en premier
    ])
    def test_priority_resolver_wins_whatever_the_arrival_order(self, latency):
        from utils.lookups import lookup_doi
        parallel = lookup_doi(DOI, _client(_routes(), latency))
        http_mod.reset_http_policy()
        sequential = lookup_doi(DOI, _client(_routes()), max_workers=1)
        assert parallel["item"]["title"] == "Titre selon CrossRef"
        # Notice identique à celle de l'exécution séquentielle.
        assert parallel["item"] == sequential["item"]
        assert "Sources: CrossRef, HAL" in parallel["item"]["extra"]

    def test_next_resolver_is_the_base_when_crossref_fails(self):
        from utils.lookups import lookup_doi
        result = lookup_doi(DOI, _client(_routes(**{CROSSREF: not_found})))
        assert result["status"] == "trouve"
        assert result["item"]["title"] == "Titre selon HAL"
        assert _status_of(result, "CrossRef") == "non_trouve"


# === Vérification du DOI ===

class TestDoiVerification:
    def test_mismatching_record_is_rejected(self):
        """CrossRef renvoie un autre DOI (alias, erreur de service) : la
        notice est écartée. La baseline recopiait le DOI demandé dans la
        notice CrossRef et l'acceptait."""
        from utils.lookups import lookup_doi
        routes = _routes(**{CROSSREF: crossref_ok(doi="10.9999/autre")})
        result = lookup_doi(DOI, _client(routes))
        assert _status_of(result, "CrossRef") == "doi_non_concordant"
        assert result["item"]["title"] == "Titre selon HAL"
        assert result["item"]["DOI"] == DOI

    def test_all_mismatching_means_not_found(self):
        from utils.lookups import lookup_doi, fetch_by_doi
        routes = _routes(**{CROSSREF: crossref_ok(doi="10.9999/autre"),
                            HAL: hal_ok(doi="10.9999/autre")})
        result = lookup_doi(DOI, _client(routes))
        assert result["status"] == "non_trouve" and result["item"] is None
        http_mod.reset_http_policy()
        assert fetch_by_doi(DOI, http_client=_client(routes)) is None

    def test_record_without_doi_is_rejected(self):
        from utils.lookups import _fetch_from_crossref
        routes = {CROSSREF: lambda u, p: _Resp(200, {"message": {
            "type": "journal-article", "title": ["Sans DOI"]}})}
        assert _fetch_from_crossref(DOI, _client(routes)) is None

    def test_doi_comparison_is_normalized(self):
        from utils.lookups import lookup_doi
        routes = _routes(**{CROSSREF: crossref_ok(doi=DOI.upper()),
                            HAL: hal_ok(doi="https://doi.org/" + DOI)})
        result = lookup_doi("doi:" + DOI.upper(), _client(routes))
        assert _status_of(result, "CrossRef") == "trouve"
        assert _status_of(result, "HAL") == "trouve"

    def test_isidore_url_substring_is_not_a_match(self, isidore_first_item):
        """Baseline : « DOI contenu dans l'URL » acceptait
        10.3917/comla.204.00451 pour 10.3917/comla.204.0045."""
        from utils.lookups import lookup_doi
        record = copy.deepcopy(isidore_first_item)
        record["isidore"]["url"] = "https://doi.org/" + DOI + "1"
        record["isidore"]["ore"] = {"aggregates": []}
        routes = _routes(**{
            CROSSREF: not_found, HAL: lambda u, p: _Resp(
                200, {"response": {"numFound": 0, "docs": []}}),
            ISIDORE: lambda u, p: _Resp(200, {"response": {"replies": {
                "meta": {"@items": "1"}, "content": {"reply": record}}}}),
        })
        result = lookup_doi(DOI, _client(routes))
        assert _status_of(result, "Isidore") == "doi_non_concordant"
        assert result["item"] is None

    def test_isidore_record_carrying_the_doi_is_accepted(self,
                                                         isidore_first_item):
        from utils.lookups import lookup_doi
        record = copy.deepcopy(isidore_first_item)   # DOI 10.4000/lidil.6033
        routes = _routes(**{
            CROSSREF: not_found, HAL: not_found,
            ISIDORE: lambda u, p: _Resp(200, {"response": {"replies": {
                "meta": {"@items": "1"}, "content": {"reply": [record]}}}}),
        })
        result = lookup_doi("10.4000/lidil.6033", _client(routes))
        assert _status_of(result, "Isidore") == "trouve"
        assert result["status"] == "trouve"


# === Erreur réseau et « non trouvé » ===

class TestErrorVersusNotFound:
    def test_network_error_is_not_reported_as_not_found(self, monkeypatch):
        from utils.lookups import lookup_doi
        monkeypatch.setattr(http_mod, "HTTP_MAX_RETRIES", 1)
        routes = _routes(
            **{CROSSREF: lambda u, p: requests.ConnectionError("coupure"),
               HAL: not_found})
        result = lookup_doi(DOI, _client(routes))
        assert result["status"] == "erreur" and result["item"] is None
        crossref = next(r for r in result["resolvers"]
                        if r["name"] == "CrossRef")
        assert crossref["status"] == "erreur"
        assert crossref["detail"] == "ConnectionError"

    def test_all_sources_answering_404_means_not_found(self):
        from utils.lookups import lookup_doi
        routes = _routes(**{CROSSREF: not_found, HAL: not_found})
        result = lookup_doi(DOI, _client(routes))
        assert result["status"] == "non_trouve"
        statuses = {r["name"]: r["status"] for r in result["resolvers"]}
        assert statuses["CrossRef"] == "non_trouve"
        assert statuses["OpenAlex"] == "ignore"      # sans clé : pas d'accès
        assert statuses["CORE"] == "ignore"

    def test_service_error_status_is_an_error(self):
        from utils.lookups import lookup_doi
        routes = _routes(**{CROSSREF: lambda u, p: _Resp(403),
                            HAL: not_found})
        result = lookup_doi(DOI, _client(routes))
        assert result["status"] == "erreur"
        assert _status_of(result, "CrossRef") == "erreur"

    def test_invalid_doi(self):
        from utils.lookups import lookup_doi
        result = lookup_doi("pas-un-doi", _client({}))
        assert result["status"] == "doi_invalide"


# === Grâce et abandon ===

class TestAbandon:
    def test_stragglers_are_abandoned_after_grace(self):
        from utils.lookups import lookup_doi
        client = _client(_routes(), latency={ISIDORE: 1.5})
        t0 = time.monotonic()
        result = lookup_doi(DOI, client, grace=0.2)
        elapsed = time.monotonic() - t0
        assert result["status"] == "trouve"
        assert _status_of(result, "Isidore") == "abandonne"
        assert elapsed < 1.0, elapsed

    def test_priority_resolver_is_awaited_even_when_others_answered(self):
        """Le délai de grâce ne court qu'une fois le résultat prioritaire
        connu : un CrossRef lent n'est pas abandonné parce que HAL a déjà
        répondu."""
        from utils.lookups import lookup_doi
        client = _client(_routes(), latency={CROSSREF: 0.4})
        result = lookup_doi(DOI, client, grace=0.05)
        assert _status_of(result, "CrossRef") == "trouve"
        assert result["item"]["title"] == "Titre selon CrossRef"

    def test_max_seconds_without_answer_is_an_error(self):
        from utils.lookups import lookup_doi
        client = _client(_routes(**{HAL: not_found}),
                         latency={CROSSREF: 1.0})
        result = lookup_doi(DOI, client, max_seconds=0.3)
        assert result["status"] == "erreur"
        assert _status_of(result, "CrossRef") == "abandonne"


# === Outil MCP ===

def _canned(status, item=None):
    return {"doi": DOI, "status": status, "item": item, "duration_s": 0.1,
            "resolvers": [{"name": "CrossRef", "status":
                           "erreur" if status == "erreur" else "non_trouve",
                           "detail": "ConnectionError"
                           if status == "erreur" else "HTTP 404",
                           "duration_s": 0.1}]}


class TestMcpTool:
    def _call(self, monkeypatch, canned, args=None):
        import asyncio
        import mcp_server
        from utils import lookups
        monkeypatch.setattr(lookups, "lookup_doi", lambda doi, client: canned)
        return asyncio.run(mcp_server._tool_fetch_by_doi(args or {"doi": DOI}))

    def test_network_error_message(self, monkeypatch):
        blocks, structured = self._call(monkeypatch, _canned("erreur"))
        text = blocks[0].text
        assert "Erreur réseau" in text
        assert "Aucune métadonnée trouvée" not in text
        assert structured["status"] == "erreur"
        assert structured["resolvers"][0]["detail"] == "ConnectionError"

    def test_not_found_message(self, monkeypatch):
        blocks, structured = self._call(monkeypatch, _canned("non_trouve"))
        assert "Aucune métadonnée trouvée" in blocks[0].text
        assert "Erreur réseau" not in blocks[0].text
        assert structured["status"] == "non_trouve"

    def test_found_lists_sources_and_adds_to_corpus(self, monkeypatch,
                                                    tmp_path):
        item = {"itemType": "journalArticle", "title": "Un titre",
                "DOI": DOI, "creators": [], "date": "2020",
                "extra": "Sources: CrossRef", "tags": []}
        corpus = tmp_path / "corpus_x.json"
        corpus.write_text("[]", encoding="utf-8")
        canned = _canned("trouve", item)
        canned["resolvers"][0]["status"] = "trouve"
        blocks, structured = self._call(
            monkeypatch, canned, {"doi": DOI, "add_to_corpus": str(corpus)})
        assert "Titre : Un titre" in blocks[0].text
        assert "Sources : CrossRef : trouvé" in blocks[0].text
        assert structured["added_to"] == "corpus_x.json"
        assert len(json.loads(corpus.read_text(encoding="utf-8"))) == 1
        # Aucun JSON bibliographique ni chemin dans structuredContent
        assert "item" not in structured
        assert str(tmp_path) not in json.dumps(structured)

    def test_schema_mentions_status(self):
        import asyncio
        import mcp_server
        tools = asyncio.run(mcp_server.list_tools())
        tool = next(t for t in tools if t.name == "fetch_by_doi")
        assert tool.inputSchema["required"] == ["doi"]
        assert "non_trouve" in tool.description
