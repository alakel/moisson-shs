"""
Tests des boucles de pagination des connecteurs internationaux, avec la couche
HTTP entièrement simulée (aucun réseau). Couvre : cursor CrossRef/OpenAlex,
token S2, offset CORE, arrêt sur budget épuisé, respect du plafond max_results.
"""

from unittest.mock import patch

import pytest


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHTTP:
    """HTTPClient simulé : sert une file de payloads, enregistre les appels."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []
        self.session = object()

    def get(self, url, params=None, headers=None, timeout=None, **policy):
        # **policy : options de la politique HTTP commune (min_interval…)
        self.calls.append({"url": url, "params": params, "headers": headers,
                           "policy": policy})
        return _Resp(self._payloads.pop(0))

    def close(self):
        pass


def _cr_page(items, cursor):
    return {"message": {"items": items, "next-cursor": cursor}}


def _cr_item(doi):
    return {"type": "journal-article", "title": [f"Title {doi}"], "DOI": doi}


# === CrossRef ===

class TestCrossrefConnector:
    def test_paginates_until_empty(self):
        from connectors.crossref import CrossrefConnector
        http = _FakeHTTP([
            _cr_page([_cr_item("10.1/a"), _cr_item("10.1/b")], "c2"),
            _cr_page([], "c3"),  # lot vide → stop
        ])
        c = CrossrefConnector(http_client=http)
        items = c.search("ai", max_results=100)
        assert len(items) == 2
        assert items[0]["DOI"] == "10.1/a"

    def test_respects_max_results(self):
        from connectors.crossref import CrossrefConnector
        http = _FakeHTTP([
            _cr_page([_cr_item(f"10.1/{i}") for i in range(5)], "c2"),
        ])
        c = CrossrefConnector(http_client=http)
        items = c.search("ai", max_results=3)
        assert len(items) == 3

    def test_date_filter_and_mailto_in_params(self):
        from connectors.crossref import CrossrefConnector
        http = _FakeHTTP([_cr_page([], None)])
        c = CrossrefConnector(http_client=http)
        c.search("ai", max_results=10, from_date="2020", to_date="2022")
        params = http.calls[0]["params"]
        assert "from-pub-date:2020-01-01" in params["filter"]
        assert "until-pub-date:2022-12-31" in params["filter"]
        assert "mailto" in params

    def test_stops_when_no_cursor(self):
        from connectors.crossref import CrossrefConnector
        http = _FakeHTTP([_cr_page([_cr_item("10.1/a")], None)])
        c = CrossrefConnector(http_client=http)
        items = c.search("ai", max_results=100)
        assert len(items) == 1

    def test_select_excludes_invalid_language_field(self):
        # Régression : 'language' n'est PAS un champ select valide côté CrossRef
        # et provoque un HTTP 400 sur TOUTE requête (corpus vide silencieux).
        from connectors import crossref
        fields = crossref.SELECT_FIELDS.split(",")
        assert "language" not in fields
        # Les champs réellement utilisés restent présents
        for f in ("DOI", "title", "abstract", "issued",
                  "is-referenced-by-count"):
            assert f in fields

    def test_select_param_sent_without_language(self):
        from connectors.crossref import CrossrefConnector
        http = _FakeHTTP([_cr_page([], None)])
        CrossrefConnector(http_client=http).search("ai", max_results=10)
        assert "language" not in http.calls[0]["params"]["select"]


# === OpenAlex (get_json patché) ===

class TestOpenAlexConnector:
    def test_paginates_cursor(self):
        from connectors import openalex
        pages = [
            {"results": [{"id": "W1", "title": "A", "type": "article"}],
             "meta": {"next_cursor": "c2"}},
            {"results": [{"id": "W2", "title": "B", "type": "article"}],
             "meta": {"next_cursor": None}},
        ]
        with patch.object(openalex, "get_json", side_effect=pages):
            c = openalex.OpenAlexConnector(http_client=_FakeHTTP([]), api_key="K")
            items = c.search("ai", max_results=100)
        assert len(items) == 2

    def test_budget_exhausted_stops_gracefully(self):
        from connectors import openalex
        from utils.http_scholarly import BudgetExhausted

        seq = [
            {"results": [{"id": "W1", "title": "A", "type": "article"}],
             "meta": {"next_cursor": "c2"}},
            BudgetExhausted("budget"),
        ]

        def fake_get_json(*a, **k):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v

        with patch.object(openalex, "get_json", side_effect=fake_get_json):
            c = openalex.OpenAlexConnector(http_client=_FakeHTTP([]), api_key="K")
            items = c.search("ai", max_results=100)
        # La 1re page est conservée malgré l'épuisement de budget ensuite
        assert len(items) == 1

    def test_year_filter_built(self):
        from connectors import openalex
        captured = {}

        def fake_get_json(url, params=None, **k):
            captured["params"] = params
            return {"results": [], "meta": {"next_cursor": None}}

        with patch.object(openalex, "get_json", side_effect=fake_get_json):
            c = openalex.OpenAlexConnector(http_client=_FakeHTTP([]), api_key="K")
            c.search("ai", max_results=10, from_date="2021", to_date="2023")
        assert "publication_year:2021-2023" in captured["params"]["filter"]
        assert captured["params"]["api_key"] == "K"


# === Semantic Scholar (self.http.get + politesse par hôte) ===

class TestS2Connector:
    def test_token_pagination(self):
        from connectors import semantic_scholar
        http = _FakeHTTP([
            {"data": [{"title": "A", "externalIds": {"DOI": "10.1/a"}}],
             "token": "t2"},
            {"data": [{"title": "B", "externalIds": {"DOI": "10.1/b"}}],
             "token": None},
        ])
        with patch("utils.http.time.sleep", lambda s: None):
            c = semantic_scholar.SemanticScholarConnector(
                http_client=http, api_key="K"
            )
            items = c.search("llm", max_results=100)
        assert len(items) == 2
        # La clé doit être passée en en-tête x-api-key
        assert http.calls[0]["headers"]["x-api-key"] == "K"

    def test_stops_when_no_token(self):
        from connectors import semantic_scholar
        http = _FakeHTTP([
            {"data": [{"title": "A", "externalIds": {}}], "token": None},
        ])
        with patch("utils.http.time.sleep", lambda s: None):
            c = semantic_scholar.SemanticScholarConnector(http_client=http)
            items = c.search("llm", max_results=100)
        assert len(items) == 1
        # Sans clé : pas d'en-tête x-api-key
        assert http.calls[0]["headers"] is None


# === CORE (get_json patché + politesse par hôte) ===

class TestCoreConnector:
    def test_offset_pagination(self):
        from connectors import core
        pages = [
            {"results": [{"id": 1, "title": "A", "documentType": "research"}],
             "totalHits": 2},
            {"results": [{"id": 2, "title": "B", "documentType": "research"}],
             "totalHits": 2},
            {"results": [], "totalHits": 2},
        ]
        with patch.object(core, "get_json", side_effect=pages), \
                patch("utils.http.time.sleep", lambda s: None):
            c = core.CoreConnector(http_client=_FakeHTTP([]), api_key="K")
            items = c.search("oa", max_results=100)
        assert len(items) == 2

    def test_budget_exhausted_stops(self):
        from connectors import core
        from utils.http_scholarly import BudgetExhausted

        def fake_get_json(*a, **k):
            raise BudgetExhausted("quota")

        with patch.object(core, "get_json", side_effect=fake_get_json), \
                patch("utils.http.time.sleep", lambda s: None):
            c = core.CoreConnector(http_client=_FakeHTTP([]), api_key="K")
            items = c.search("oa", max_results=100)
        assert items == []

    def test_year_filter_in_query(self):
        from connectors import core
        captured = {}

        def fake_get_json(url, params=None, **k):
            captured["params"] = params
            return {"results": [], "totalHits": 0}

        with patch.object(core, "get_json", side_effect=fake_get_json), \
                patch("utils.http.time.sleep", lambda s: None):
            c = core.CoreConnector(http_client=_FakeHTTP([]), api_key="K")
            c.search("oa", max_results=10, from_date="2020", to_date="2022")
        assert "yearPublished>=2020" in captured["params"]["q"]
        assert "yearPublished<=2022" in captured["params"]["q"]


# === Clés requises ===

class TestRequiredKeys:
    def test_openalex_missing_key_raises(self, monkeypatch):
        from connectors.openalex import OpenAlexConnector
        from utils.secrets import MissingAPIKey
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        with pytest.raises(MissingAPIKey):
            OpenAlexConnector(http_client=_FakeHTTP([]))

    def test_core_missing_key_raises(self, monkeypatch):
        from connectors.core import CoreConnector
        from utils.secrets import MissingAPIKey
        monkeypatch.delenv("CORE_API_KEY", raising=False)
        with pytest.raises(MissingAPIKey):
            CoreConnector(http_client=_FakeHTTP([]))

    def test_s2_no_key_ok(self, monkeypatch):
        from connectors.semantic_scholar import SemanticScholarConnector
        monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
        c = SemanticScholarConnector(http_client=_FakeHTTP([]))
        assert c.api_key is None
