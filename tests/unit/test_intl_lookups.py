"""
Tests des résolveurs internationaux de utils.lookups (fetch_by_doi et
search_by_author), avec la couche réseau simulée et des clés simulées.

Depuis la politique HTTP commune (OPT-03), les résolveurs ne font plus
d'appel ``requests.get`` brut : ils passent par ``utils.http.HTTPClient``.
Les tests injectent donc un vrai ``HTTPClient`` dont la session est simulée
(file de réponses) et dont l'horloge est simulée : la politique réelle
(une tentative sur 4xx, politesse par hôte) est exercée sans réseau ni
attente. Auparavant, ils patchaient ``requests.get`` globalement.
"""

from unittest.mock import patch

import pytest

import utils.http as http_mod
from utils.http import HTTPClient
from utils.lookups import (
    _fetch_from_openalex, _fetch_from_s2, _fetch_from_core,
    _search_openalex_author, _search_s2_author,
)


@pytest.fixture(autouse=True)
def _schema_isolated(monkeypatch):
    """
    Isole le chargement du schéma Zotero officiel.

    Les mappers appellent ``validate_and_clean``, qui charge le schéma Zotero
    (``mappers.zotero_schema_loader``). Sans cache mémoire ni cache disque
    frais, ce chargeur appelle lui aussi ``requests.get`` : un patch global
    de ``requests.get`` comptait alors ce téléchargement parmi les appels du
    résolveur (cause constatée de l'échec intermittent de
    ``test_openalex_author_by_orcid`` : 2 appels au lieu d'un). Le schéma est
    donc neutralisé ici (repli sur la table embarquée), sans réseau.
    """
    import mappers.zotero_schema_loader as zsl
    monkeypatch.setattr(zsl, "load_zotero_schema", lambda *a, **k: None,
                        raising=False)


class _FakeClock:
    """Horloge simulée de utils.http (le sommeil fait avancer le temps)."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if seconds > 0:
            self.now += seconds


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.from_cache = False
        self.reason = ""
        self.url = ""

    def json(self):
        return self._payload


class _FakeSession:
    """Session simulée : sert une file de réponses, enregistre les appels."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self._responses.pop(0)

    def mount(self, *args):
        pass

    def close(self):
        pass


@pytest.fixture
def make_client(monkeypatch):
    """Fabrique un HTTPClient réel sur session simulée, horloge simulée."""
    monkeypatch.setattr(http_mod, "time", _FakeClock())
    http_mod.reset_http_policy()

    def _make(responses):
        client = HTTPClient(use_cache=False)
        client.session = _FakeSession(responses)
        return client
    yield _make
    # Créneaux de politesse réservés sur l'horloge simulée : oubliés.
    http_mod.reset_http_policy()


class TestFetchResolvers:
    def test_openalex_requires_key(self, monkeypatch, make_client):
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        client = make_client([])
        # Sans clé : pas d'appel réseau, retourne None
        assert _fetch_from_openalex("10.1/x", client) is None
        assert client.session.calls == []

    def test_openalex_maps_with_key(self, monkeypatch, openalex_work,
                                    make_client):
        monkeypatch.setenv("OPENALEX_API_KEY", "KEY")
        client = make_client([_Resp(200, openalex_work)])
        item = _fetch_from_openalex("10.1234/jaip.2023.12.45", client)
        assert item is not None
        assert item["DOI"] == "10.1234/jaip.2023.12.45"
        assert "Sources: OpenAlex" in item["extra"]
        call = client.session.calls[0]
        assert call["url"].startswith("https://api.openalex.org/works/doi:")
        assert call["params"]["api_key"] == "KEY"

    def test_s2_works_without_key(self, monkeypatch, s2_paper, make_client):
        monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
        client = make_client([_Resp(200, s2_paper)])
        item = _fetch_from_s2("10.5555/llm.2022.99", client)
        assert item is not None
        assert item["DOI"] == "10.5555/llm.2022.99"
        assert "Sources: Semantic Scholar" in item["extra"]
        # Sans clé : aucun en-tête x-api-key
        assert client.session.calls[0]["headers"] is None

    def test_s2_key_sent_as_header(self, monkeypatch, s2_paper, make_client):
        monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "S2KEY")
        client = make_client([_Resp(200, s2_paper)])
        assert _fetch_from_s2("10.5555/llm.2022.99", client) is not None
        assert client.session.calls[0]["headers"]["x-api-key"] == "S2KEY"

    def test_s2_non_200_returns_none(self, make_client):
        client = make_client([_Resp(404, {})])
        assert _fetch_from_s2("10.5555/x", client) is None
        # 404 définitif : une seule tentative
        assert len(client.session.calls) == 1

    def test_core_requires_key(self, monkeypatch, make_client):
        monkeypatch.delenv("CORE_API_KEY", raising=False)
        client = make_client([])
        assert _fetch_from_core("10.1/x", client) is None
        assert client.session.calls == []

    def test_core_maps_with_key(self, monkeypatch, core_work, make_client):
        monkeypatch.setenv("CORE_API_KEY", "KEY")
        client = make_client([_Resp(200, {"results": [core_work]})])
        item = _fetch_from_core("10.7777/core.2021.1", client)
        assert item is not None
        assert item["DOI"] == "10.7777/core.2021.1"
        assert "Sources: CORE" in item["extra"]
        assert client.session.calls[0]["headers"]["Authorization"] == \
            "Bearer KEY"

    def test_core_empty_results(self, monkeypatch, make_client):
        monkeypatch.setenv("CORE_API_KEY", "KEY")
        client = make_client([_Resp(200, {"results": []})])
        assert _fetch_from_core("10.7777/none", client) is None

    def test_default_client_used_without_client(self, monkeypatch, s2_paper,
                                                make_client):
        # Sans client fourni : le client partagé de utils.http, jamais
        # requests.get brut.
        monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
        client = make_client([_Resp(200, s2_paper)])
        with patch("utils.lookups.get_default_client", return_value=client), \
                patch("requests.get") as raw_get:
            item = _fetch_from_s2("10.5555/llm.2022.99")
        assert item is not None
        assert len(client.session.calls) == 1
        raw_get.assert_not_called()


class TestAuthorSearch:
    """
    Recherche d'auteur OpenAlex et Semantic Scholar.

    Adaptation OPT-14 : ``_search_openalex_author`` et ``_search_s2_author``
    rendent désormais un ``AuthorSourceResult`` (notices, statut, motif,
    profil retenu) au lieu d'une liste ou de ``None`` ; ``None`` confondait
    clé absente, aucun candidat et panne (OPT-07, §9.2). Les fixtures de
    profils portent un nom (``display_name``), nécessaire au contrôle de
    compatibilité du nom ajouté par OPT-14.
    """

    def test_s2_author_two_calls(self, monkeypatch, s2_paper, make_client):
        monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
        client = make_client([
            _Resp(200, {"data": [{"authorId": "A99", "name": "Alice Wong"}]}),
            _Resp(200, {"data": [s2_paper]}),
        ])
        res = _search_s2_author("Alice Wong", max_per_source=10,
                                http_client=client)
        assert res.status == "complete" and len(res.items) == 1
        assert res.items[0]["DOI"] == "10.5555/llm.2022.99"
        assert res.identity["id"] == "A99"
        assert len(client.session.calls) == 2

    def test_s2_author_no_match(self, make_client):
        client = make_client([_Resp(200, {"data": []})])
        res = _search_s2_author("Nobody", max_per_source=10,
                                http_client=client)
        assert res.items == [] and res.status == "empty"
        assert res.reason == "no_author_candidate"

    def test_openalex_author_requires_key(self, monkeypatch, make_client):
        monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
        client = make_client([])
        res = _search_openalex_author("X", 10, http_client=client)
        assert res.status == "skipped" and res.reason == "missing_key"
        assert client.session.calls == []

    def test_openalex_author_by_name(self, monkeypatch, openalex_work,
                                     make_client):
        monkeypatch.setenv("OPENALEX_API_KEY", "KEY")
        client = make_client([
            _Resp(200, {"results": [{"id": "https://openalex.org/A1",
                                     "display_name": "Jane Doe",
                                     "works_count": 12}]}),
            _Resp(200, {"results": [openalex_work]}),
        ])
        res = _search_openalex_author("Jane Doe", max_per_source=10,
                                      http_client=client)
        assert res.status == "complete" and len(res.items) == 1
        assert res.items[0]["DOI"] == "10.1234/jaip.2023.12.45"
        assert res.identity["id"] == "A1"
        assert client.session.calls[1]["params"]["filter"] == "author.id:A1"

    def test_openalex_author_by_orcid(self, monkeypatch, openalex_work,
                                      make_client):
        monkeypatch.setenv("OPENALEX_API_KEY", "KEY")
        # Avec ORCID, un seul appel works (pas de résolution d'auteur)
        client = make_client([_Resp(200, {"results": [openalex_work]})])
        res = _search_openalex_author(
            "Jane Doe", 10, orcid="0000-0002-1234-5678", http_client=client
        )
        assert res.status == "complete" and len(res.items) == 1
        # Seuls les appels vers OpenAlex comptent (le schéma est isolé et ne
        # passe de toute façon pas par ce client).
        openalex_calls = [c for c in client.session.calls
                          if "api.openalex.org" in c["url"]]
        assert len(openalex_calls) == 1
        assert openalex_calls[0]["params"]["filter"] == \
            "author.orcid:0000-0002-1234-5678"
