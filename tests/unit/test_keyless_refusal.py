"""
PAT-05 : un refus d'accès (401 ou 403) reçu **sans clé** signale une source
non configurée (motif ``missing_key``), pas une panne. Constat du
2026-09-30 : Semantic Scholar répond 403 sans clé ; le refus était rendu
comme une erreur HTTP et, dans ``fetch_by_doi``, rendait le statut global
« erreur » : un DOI absent des autres sources ressortait en erreur réseau au
lieu de « non trouvé ». Avec une clé, un refus reste une erreur (clé
refusée).
"""

from __future__ import annotations

import pytest
import requests

import utils.lookups as lookups
from config import ENV_S2_KEY
from connectors.semantic_scholar import SemanticScholarConnector
from engine.models import keyless_refusal


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import mappers.zotero_schema_loader as zsl
    monkeypatch.setattr(zsl, "load_zotero_schema", lambda *a, **k: None,
                        raising=False)
    monkeypatch.delenv(ENV_S2_KEY, raising=False)
    monkeypatch.setattr(lookups, "get_api_key",
                        lambda name, required=False: None)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.from_cache = False

    def json(self):
        return self._payload


class Router:
    cancel_signal = None

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, **kwargs):
        self.calls.append(url)
        for fragment, answer in self.routes:
            if fragment in url:
                if isinstance(answer, int):
                    raise requests.HTTPError(response=_Resp({}, answer))
                if isinstance(answer, Exception):
                    raise answer
                return _Resp(answer)
        raise requests.ConnectionError("hôte non simulé")

    def close(self):
        pass


class TestHelper:
    @pytest.mark.parametrize("error", [
        403, 401, "HTTP 403", requests.HTTPError(response=_Resp({}, 403))])
    def test_refusal_without_key(self, error):
        detail = keyless_refusal(error, False, "X_KEY")
        assert detail.endswith("sans clé : définir X_KEY")

    @pytest.mark.parametrize("error", [404, 429, 500, "HTTP 404",
                                       "ConnectionError", None])
    def test_other_errors_are_not_refusals(self, error):
        assert keyless_refusal(error, False, "X_KEY") is None

    def test_refusal_with_key_is_an_error(self):
        assert keyless_refusal(403, True, "X_KEY") is None


class TestHarvest:
    def test_s2_harvest_403_without_key_is_skipped(self):
        conn = SemanticScholarConnector(http_client=Router([("semanticscholar", 403)]))
        assert conn.search("discours", max_results=5) == []
        st = conn.last_status
        assert (st.status, st.reason) == ("skipped", "missing_key")
        assert ENV_S2_KEY in st.detail

    def test_s2_403_with_key_stays_http_error(self):
        conn = SemanticScholarConnector(
            http_client=Router([("semanticscholar", 403)]), api_key="K")
        conn.search("discours", max_results=5)
        st = conn.last_status
        assert (st.status, st.reason, st.detail) == ("error", "http_error",
                                                     "HTTP 403")


class TestAuthor:
    def test_author_s2_403_without_key_is_skipped(self):
        router = Router([
            ("archives-ouvertes", 503),
            ("isidore", {"response": {"replies": {"meta": {"@items": "0"},
                                                  "content": {}}}}),
            ("semanticscholar", 403),
        ])
        out = lookups.search_by_author("Amar Lakel", http_client=router)
        s2 = {r.display_name: r for r in out.report.sources}["Semantic Scholar"]
        assert (s2.status, s2.reason) == ("skipped", "missing_key")
        assert ENV_S2_KEY in s2.detail


class TestFetchByDoi:
    def test_unknown_doi_is_not_found_when_s2_refuses_keyless(self):
        router = Router([
            ("crossref", 404),
            ("archives-ouvertes", {"response": {"numFound": 0, "docs": []}}),
            ("isidore", {"response": {"replies": {"meta": {"@items": "0"},
                                                  "content": {}}}}),
            ("semanticscholar", 403),
        ])
        result = lookups.lookup_doi("10.9999/inconnu.1", http_client=router)
        by = {r["name"]: r for r in result["resolvers"]}
        assert result["status"] == lookups.DOI_NOT_FOUND, result
        assert by["Semantic Scholar"]["status"] == lookups.LOOKUP_SKIPPED
        assert ENV_S2_KEY in by["Semantic Scholar"]["detail"]
