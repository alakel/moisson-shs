"""
Sprint « clés » (CLE-02) : repli sur la clé de réserve après un refus.

Toute requête passe par ``utils.http.policy_get``. Un refus 401 ou 403 reçu
avec la clé du client déclenche UNE requête de plus avec la clé de réserve
(`.env`) quand la source figure dans ``MOISSON_KEY_FALLBACK`` et que la
réserve diffère ; le refus est mémorisé, les requêtes suivantes partent
directement avec la réserve. Aucun autre statut ne déclenche de repli.
"""

import pytest
import requests

import utils.http as http_mod
from utils import secrets
from utils.http import policy_get


OA = "OPENALEX_API_KEY"
CORE = "CORE_API_KEY"
SERP = "SERPAPI_API_KEY"


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = {}
        self.from_cache = False
        self.reason = ""
        self.url = ""
        self.text = ""

    def json(self):
        return self._payload


class _Session:
    """Réponse choisie d'après la clé reçue ; appels notés."""

    def __init__(self, accepted, status_refused=401):
        self.accepted = set(accepted)
        self.status_refused = status_refused
        self.calls = []
        self.headers = {}

    def _credential(self, params, headers):
        params, headers = dict(params or {}), dict(headers or {})
        if "api_key" in params:
            return params["api_key"]
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):]
        return headers.get("x-api-key")

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        cred = self._credential(params, headers)
        self.calls.append(cred)
        if cred in self.accepted:
            return _Resp(200, {"ok": cred})
        return _Resp(self.status_refused)

    def mount(self, *a):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr("utils.http.time.sleep", lambda s: None)
    http_mod.reset_http_policy()
    for var in secrets.API_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(secrets.KEY_FALLBACK_ENV, raising=False)
    monkeypatch.setattr(secrets, "_RESERVE", {})
    secrets.reset_key_state()
    yield
    secrets.reset_key_state()
    http_mod.reset_http_policy()


def _keys(monkeypatch, var, client, reserve):
    monkeypatch.setenv(var, client)
    monkeypatch.setattr(secrets, "_RESERVE", {var: reserve})


class TestFallbackAfterRefusal:
    def test_refused_param_key_is_retried_with_the_reserve(self, monkeypatch):
        _keys(monkeypatch, OA, "CLIENT", "RESERVE")
        sess = _Session(accepted={"RESERVE"})
        resp = policy_get(sess, "https://api.openalex.org/works",
                          params={"api_key": "CLIENT", "search": "q"})
        assert resp.json() == {"ok": "RESERVE"}
        assert sess.calls == ["CLIENT", "RESERVE"]
        assert secrets.refused_status(OA) == 401

    @pytest.mark.parametrize("status", [401, 403])
    def test_bearer_header_and_both_statuses(self, monkeypatch, status):
        _keys(monkeypatch, CORE, "CLIENT", "RESERVE")
        sess = _Session(accepted={"RESERVE"}, status_refused=status)
        resp = policy_get(sess, "https://api.core.ac.uk/v3/search/works",
                          headers={"Authorization": "Bearer CLIENT"})
        assert resp.status_code == 200
        assert sess.calls == ["CLIENT", "RESERVE"]

    def test_following_requests_use_the_reserve_directly(self, monkeypatch):
        _keys(monkeypatch, OA, "CLIENT", "RESERVE")
        sess = _Session(accepted={"RESERVE"})
        policy_get(sess, "https://api.openalex.org/works",
                   params={"api_key": "CLIENT", "page": 1})
        policy_get(sess, "https://api.openalex.org/works",
                   params={"api_key": "CLIENT", "page": 2})
        assert sess.calls == ["CLIENT", "RESERVE", "RESERVE"]

    def test_caller_params_are_not_mutated(self, monkeypatch):
        _keys(monkeypatch, OA, "CLIENT", "RESERVE")
        params = {"api_key": "CLIENT"}
        policy_get(_Session(accepted={"RESERVE"}), "https://api.openalex.org/w",
                   params=params)
        assert params == {"api_key": "CLIENT"}

    def test_reserve_refused_too_raises_once(self, monkeypatch):
        _keys(monkeypatch, OA, "CLIENT", "RESERVE")
        sess = _Session(accepted=set())
        with pytest.raises(requests.HTTPError) as err:
            policy_get(sess, "https://api.openalex.org/works",
                       params={"api_key": "CLIENT"})
        assert err.value.response.status_code == 401
        assert sess.calls == ["CLIENT", "RESERVE"]
        assert "RESERVE" not in str(err.value) and "CLIENT" not in str(err.value)

    def test_metered_key_is_never_replaced(self, monkeypatch):
        _keys(monkeypatch, SERP, "CLIENT", "RESERVE")
        sess = _Session(accepted={"RESERVE"})
        with pytest.raises(requests.HTTPError):
            policy_get(sess, "https://serpapi.com/search.json",
                       params={"api_key": "CLIENT"})
        assert sess.calls == ["CLIENT"]

    def test_other_statuses_do_not_fall_back(self, monkeypatch):
        _keys(monkeypatch, OA, "CLIENT", "RESERVE")
        sess = _Session(accepted={"RESERVE"}, status_refused=404)
        with pytest.raises(requests.HTTPError):
            policy_get(sess, "https://api.openalex.org/works",
                       params={"api_key": "CLIENT"})
        assert sess.calls == ["CLIENT"]
        assert secrets.refused_status(OA) is None

    def test_keyless_request_is_unchanged(self, monkeypatch):
        _keys(monkeypatch, OA, "CLIENT", "RESERVE")
        sess = _Session(accepted=set(), status_refused=403)
        with pytest.raises(requests.HTTPError):
            policy_get(sess, "https://api.semanticscholar.org/graph/v1/x",
                       params={"query": "q"})
        assert sess.calls == [None]

    def test_accepted_client_key_is_kept(self, monkeypatch):
        _keys(monkeypatch, OA, "CLIENT", "RESERVE")
        sess = _Session(accepted={"CLIENT", "RESERVE"})
        policy_get(sess, "https://api.openalex.org/works",
                   params={"api_key": "CLIENT"})
        assert sess.calls == ["CLIENT"]
        assert secrets.refused_status(OA) is None
