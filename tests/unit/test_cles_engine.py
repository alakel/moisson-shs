"""
Sprint « clés » (CLE-03, CLE-07) : statuts des sources et moisson sans clé.

- Une clé du client refusée puis remplacée par la réserve du `.env` est
  signalée dans le statut de la source, sans jamais citer de valeur.
- Une clé déclarée vide que le `.env` ne remplace pas (SerpAPI, hors
  ``MOISSON_KEY_FALLBACK``) donne un motif ``missing_key`` qui le dit.
- Contrainte de l'utilisateur : sans aucune clé, les sources qui n'en
  exigent pas restent disponibles et la moisson n'est pas une erreur.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import pytest

from engine import orchestrator
from engine.models import HarvestResult, RunContext, SearchRequest
from utils import secrets
from utils.http import HTTPClient, reset_http_policy


OA = "OPENALEX_API_KEY"
SERP = "SERPAPI_API_KEY"


# === Doublures ===

class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None):
        self.status_code = status
        self._payload = payload
        self.headers = {"Content-Type": "application/json"}
        self.from_cache = False
        self.url = ""
        self.reason = ""
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.content = body
        self.text = body.decode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("corps vide")
        return self._payload


class FakeSession:
    """``handler(url, params, headers) -> FakeResponse`` ; appels notés."""

    def __init__(self, handler: Callable):
        self.handler = handler
        self.calls: List[tuple] = []
        self.headers: Dict[str, str] = {}

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        params, headers = dict(params or {}), dict(headers or {})
        self.calls.append((url, params, headers))
        return self.handler(url, params, headers)

    def mount(self, *a, **k):
        pass

    def close(self):
        pass


def fake_client(handler: Callable) -> HTTPClient:
    client = HTTPClient(use_cache=False)
    client.session = FakeSession(handler)
    return client


class _ClientFactory:
    def __init__(self, handler):
        self.handler = handler

    def __call__(self, use_cache=None, **kw):
        return fake_client(self.handler)


def _hal_docs(n: int) -> List[Dict]:
    return [{"docid": i, "halId_s": f"hal-{i:05d}", "title_s": [f"Titre HAL {i}"],
             "producedDate_s": "2020", "docType_s": "ART",
             "authFullName_s": [f"Auteur{i} Jean"],
             "uri_s": f"https://hal.science/hal-{i:05d}"} for i in range(n)]


def _crossref_items(n: int) -> List[Dict]:
    return [{"DOI": f"10.1/cr{i}", "title": [f"Titre CR {i}"],
             "type": "journal-article", "issued": {"date-parts": [[2020]]},
             "author": [{"family": f"Nom{i}", "given": "Jean"}]} for i in range(n)]


def _keyless_handler(url, params, headers):
    """HAL et CrossRef servent dix notices ; Semantic Scholar refuse sans
    clé (403) ; OpenAlex, CORE et SerpAPI ne doivent jamais être appelés."""
    if "archives-ouvertes" in url:
        if params.get("cursorMark") not in (None, "*"):
            return FakeResponse(payload={"response": {"numFound": 10, "docs": []},
                                         "nextCursorMark": params["cursorMark"]})
        return FakeResponse(payload={"response": {"numFound": 10,
                                                  "docs": _hal_docs(10)},
                                     "nextCursorMark": "fin"})
    if "crossref" in url:
        return FakeResponse(payload={"message": {"items": _crossref_items(10)}})
    if "semanticscholar" in url:
        return FakeResponse(status=403, payload={})
    raise AssertionError(f"requête inattendue : {url}")


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr("utils.http.time.sleep", lambda s: None)
    reset_http_policy()
    for var in secrets.API_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(secrets.KEY_FALLBACK_ENV, raising=False)
    monkeypatch.setattr(secrets, "_RESERVE", {})
    secrets.reset_key_state()
    yield
    secrets.reset_key_state()
    reset_http_policy()


def run(sources, client, **kw) -> HarvestResult:
    request = SearchRequest(query="q", sources=list(sources),
                            max_per_source=kw.pop("max_per_source", 50), **kw)
    return orchestrator.harvest(request, RunContext(http_client=client))


def by_name(result: HarvestResult):
    return {r.display_name: r for r in result.sources}


# === CLE-03 : traçabilité ===

class TestKeyReserveInStatuses:
    def test_refused_client_key_replaced_is_reported(self, monkeypatch):
        monkeypatch.setenv(OA, "CLIENT-OA")
        monkeypatch.setattr(secrets, "_RESERVE", {OA: "RESERVE-OA"})

        def handler(url, params, headers):
            assert "openalex" in url
            if params.get("api_key") == "RESERVE-OA":
                return FakeResponse(payload={"results": [], "meta": {}})
            return FakeResponse(status=401, payload={})

        res = by_name(run(["openalex"], fake_client(handler)))["OpenAlex"]
        assert res.status == "empty"
        notes = " ".join(res.warnings)
        assert "refusée (HTTP 401)" in notes and ".env" in notes
        assert "CLIENT-OA" not in notes and "RESERVE-OA" not in notes

    def test_accepted_client_key_is_not_reported(self, monkeypatch):
        monkeypatch.setenv(OA, "CLIENT-OA")
        monkeypatch.setattr(secrets, "_RESERVE", {OA: "RESERVE-OA"})
        client = fake_client(lambda url, p, h: FakeResponse(
            payload={"results": [], "meta": {}}))
        res = by_name(run(["openalex"], client))["OpenAlex"]
        assert res.status == "empty" and res.warnings == []

    def test_declared_empty_metered_key_names_the_policy(self, monkeypatch):
        monkeypatch.setenv(SERP, "")
        monkeypatch.setattr(secrets, "_RESERVE", {SERP: "RESERVE-SERP"})
        client = fake_client(lambda *a: pytest.fail("aucune requête attendue"))
        res = by_name(run(["google_scholar"], client,
                          allow_metered=True))["Google Scholar"]
        assert res.status == "skipped" and res.reason == "missing_key"
        assert secrets.KEY_FALLBACK_ENV in res.detail
        assert "RESERVE-SERP" not in res.detail

    def test_missing_key_detail_names_the_client_declaration(self):
        client = fake_client(lambda *a: pytest.fail("aucune requête attendue"))
        res = by_name(run(["core"], client))["CORE"]
        assert res.status == "skipped" and res.reason == "missing_key"
        assert "CORE_API_KEY" in res.detail and "client" in res.detail


# === CLE-07 : sans aucune clé, les autres sources restent disponibles ===

SOURCES = ["hal", "crossref", "openalex", "semantic_scholar", "core"]


class TestWithoutAnyKey:
    @pytest.mark.parametrize("declared_empty", [False, True])
    def test_keyless_sources_still_harvest(self, monkeypatch, declared_empty):
        if declared_empty:
            # Déclaration écrite par setup sans clé : quatre variables vides.
            for var in secrets.API_KEY_VARS:
                monkeypatch.setenv(var, "")
        result = run(SOURCES, fake_client(_keyless_handler))
        st = by_name(result)
        assert st["HAL"].status == "complete" and st["HAL"].item_count == 10
        assert st["CrossRef"].status == "complete"
        assert st["CrossRef"].item_count == 10
        for name in ("OpenAlex", "CORE", "Semantic Scholar"):
            assert st[name].status == "skipped", name
            assert st[name].reason == "missing_key", name
        assert not result.all_failed

    def test_mcp_call_is_not_an_error(self, monkeypatch, tmp_path):
        import mcp_server

        for var in secrets.API_KEY_VARS:
            monkeypatch.setenv(var, "")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "HTTPClient",
                            _ClientFactory(_keyless_handler))
        out = mcp_server._run_moisson_sync("q", 50, SOURCES, False, True)
        assert not isinstance(out, mcp_server.ToolError)
        _blocks, structured = out
        st = structured["by_source_status"]
        assert st["HAL"]["status"] == "complete"
        assert st["CrossRef"]["status"] == "complete"
        assert {st[n]["status"] for n in ("OpenAlex", "CORE",
                                          "Semantic Scholar")} == {"skipped"}
        assert structured["total_unique"] == 20
