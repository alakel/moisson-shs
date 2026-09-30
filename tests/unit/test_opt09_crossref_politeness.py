"""
OPT-09 : politesse CrossRef (point ouvert 5 de LOTAB1) et tailles de lot
(constat C16).

- CrossRef compte les requêtes de liste (/works?query…) et d'un travail
  unique (/works/{doi}) dans deux pools : 3 requêtes/s et 3 simultanées pour
  les listes du pool poli, 10/s et 3 pour un travail unique (billet du
  2025-12-01, en-têtes relevés en live le 2026-09-28). Intervalles propres
  (0,34 s et 0,15 s), barrières distinctes, 3 requêtes en vol ;
- les en-têtes x-rate-limit-* et x-concurrency-limit durcissent la politesse
  à l'exécution, sans jamais la relâcher ;
- CrossRef rows = min(1000, plafond), OpenAlex per-page = min(200, plafond).

Aucun accès réseau : horloge simulée, sessions et clients simulés.
"""

import threading
import time

import pytest

import config
import utils.http as http_mod
from utils.http import policy_get

CR = "https://api.crossref.org/works"
CR_DOI = "https://api.crossref.org/works/10.1234/abc"


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += max(0.0, s)


class _Resp:
    def __init__(self, status=200, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {}
        self.from_cache = False
        self.url = ""
        self.reason = "OK"

    def json(self):
        return self._payload


class _Session:
    def __init__(self, headers=None):
        self.headers = {}
        self.calls = []
        self._resp_headers = headers or {}

    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        return _Resp(headers=self._resp_headers)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(http_mod, "time", c)
    http_mod.reset_http_policy()
    yield c
    http_mod.reset_http_policy()


@pytest.fixture
def fresh_policy():
    http_mod.reset_http_policy()
    yield
    http_mod.reset_http_policy()


# === Familles de requêtes ===

class TestRequestClasses:
    @pytest.mark.parametrize("url,expected", [
        ("https://api.crossref.org/works", "list"),
        ("https://api.crossref.org/works?query=x", "list"),
        ("https://api.crossref.org/v1/works", "list"),
        ("https://api.crossref.org/works/10.1234/abc", "single"),
        ("https://api.crossref.org/v1/works/10.1002/cphy.cp010129", "single"),
        ("https://api.crossref.org/journals/1234-5678/works", "list"),
        ("https://api.crossref.org/members/98", "list"),
    ])
    def test_crossref_classes(self, url, expected):
        assert http_mod.request_class("api.crossref.org", url) == expected

    def test_other_hosts_have_no_class(self):
        assert http_mod.request_class("api.openalex.org",
                                      "https://api.openalex.org/works") == ""

    def test_intervals_and_in_flight(self):
        assert config.REQUEST_DELAY_CROSSREF_LIST == 0.34
        assert config.REQUEST_DELAY_CROSSREF == 0.15
        assert http_mod.request_min_interval("api.crossref.org", CR) == 0.34
        assert http_mod.request_min_interval("api.crossref.org", CR_DOI) == 0.15
        assert http_mod.request_min_interval(
            "api.archives-ouvertes.fr", "https://api.archives-ouvertes.fr/search/"
        ) == config.REQUEST_DELAY
        # Intervalle par défaut de l'hôte inchangé (travail unique).
        assert http_mod.host_min_interval("api.crossref.org") == 0.15
        assert http_mod.host_max_in_flight("api.crossref.org") == 3
        assert http_mod.host_max_in_flight("api.isidore.science") == \
            config.HTTP_MAX_IN_FLIGHT_PER_HOST
        # 3 requêtes/s annoncées : jamais dépassées, quelle que soit la latence.
        assert 1 / config.REQUEST_DELAY_CROSSREF_LIST < 3


class TestClassPoliteness:
    def test_list_requests_are_spaced_by_the_list_interval(self, clock):
        sess = _Session()
        policy_get(sess, CR, params={"q": 1})
        policy_get(sess, CR, params={"q": 2})
        assert clock.sleeps == [pytest.approx(0.34)]

    def test_single_requests_keep_their_interval(self, clock):
        sess = _Session()
        policy_get(sess, CR_DOI)
        policy_get(sess, "https://api.crossref.org/works/10.1234/def")
        assert clock.sleeps == [pytest.approx(0.15)]

    def test_list_and_single_pools_are_independent(self, clock):
        sess = _Session()
        policy_get(sess, CR, params={"q": 1})
        policy_get(sess, CR_DOI)            # autre pool : pas d'attente
        assert clock.sleeps == []
        policy_get(sess, CR, params={"q": 2})
        assert clock.sleeps == [pytest.approx(0.34)]


# === Limites annoncées ===

class TestAnnouncedLimits:
    @pytest.mark.parametrize("headers,expected", [
        ({"x-rate-limit-limit": "3", "x-rate-limit-interval": "1s",
          "x-concurrency-limit": "3"}, (3.0 / 1.0, 3)),
        ({"x-rate-limit-limit": "10", "x-rate-limit-interval": "1s"}, (10.0, None)),
        ({"x-rate-limit-limit": "2", "x-rate-limit-interval": "500ms"}, (4.0, None)),
        ({"x-rate-limit-limit": "60", "x-rate-limit-interval": "1m"}, (1.0, None)),
        ({"x-rate-limit-limit": "abc", "x-rate-limit-interval": "1s"}, (None, None)),
        ({"x-concurrency-limit": "0"}, (None, None)),
        ({}, (None, None)),
    ])
    def test_parse(self, headers, expected):
        interval, conc = http_mod.announced_limits(headers)
        rate, exp_conc = expected
        if rate is None:
            assert interval is None
        else:
            assert interval == pytest.approx(1.0 / rate * http_mod.ANNOUNCED_RATE_MARGIN)
        assert conc == exp_conc

    def test_live_headers_match_the_configured_floor(self):
        # En-têtes relevés en live (pool poli, requêtes de liste) : la limite
        # annoncée tombe sur le plancher configuré.
        interval, conc = http_mod.announced_limits({
            "x-rate-limit-limit": "3", "x-rate-limit-interval": "1s",
            "x-concurrency-limit": "3", "x-api-pool": "polite-array"})
        assert interval == pytest.approx(config.REQUEST_DELAY_CROSSREF_LIST, abs=0.001)
        assert conc == 3

    def test_stricter_announcement_tightens_the_interval(self, clock):
        sess = _Session({"x-rate-limit-limit": "1", "x-rate-limit-interval": "1s"})
        policy_get(sess, CR, params={"q": 1})
        policy_get(sess, CR, params={"q": 2})
        assert clock.sleeps == [pytest.approx(1.0 * http_mod.ANNOUNCED_RATE_MARGIN)]

    def test_generous_announcement_never_relaxes(self, clock):
        sess = _Session({"x-rate-limit-limit": "50", "x-rate-limit-interval": "1s"})
        policy_get(sess, CR, params={"q": 1})
        policy_get(sess, CR, params={"q": 2})
        assert clock.sleeps == [pytest.approx(0.34)]

    def test_announcement_is_per_request_family(self, clock):
        sess = _Session({"x-rate-limit-limit": "1", "x-rate-limit-interval": "1s"})
        policy_get(sess, CR, params={"q": 1})          # pool des listes durci
        plain = _Session()
        policy_get(plain, CR_DOI)
        policy_get(plain, "https://api.crossref.org/works/10.1/x")
        assert clock.sleeps == [pytest.approx(0.15)]

    def test_headers_of_other_hosts_are_ignored(self, clock):
        """Seuls les en-têtes vérifiés (CrossRef) sont lus : un homonyme chez
        un autre fournisseur ne ralentit pas ses requêtes."""
        sess = _Session({"x-rate-limit-limit": "1", "x-rate-limit-interval": "1h",
                         "x-concurrency-limit": "1"})
        url = "https://api.openalex.org/works"
        policy_get(sess, url, params={"q": 1})
        policy_get(sess, url, params={"q": 2})
        assert clock.sleeps == [pytest.approx(config.REQUEST_DELAY)]
        assert http_mod._inflight_semaphore("api.openalex.org").limit == \
            config.HTTP_MAX_IN_FLIGHT_PER_HOST

    def test_cached_response_headers_are_ignored(self, clock):
        class _CachedSession(_Session):
            def get(self, url, params=None, **kw):
                r = super().get(url, params=params, **kw)
                r.from_cache = True     # servie par le cache, non revalidée
                return r
        sess = _CachedSession({"x-rate-limit-limit": "1",
                               "x-rate-limit-interval": "1s"})
        policy_get(sess, CR, params={"q": 1})
        assert http_mod._gate("api.crossref.org", http_mod.account_fingerprint(
            {"q": 1}, None), "list").announced_interval == 0.0

    def test_announced_concurrency_lowers_in_flight(self, fresh_policy):
        state = {"active": 0, "max": 0}
        lock = threading.Lock()

        class Slow:
            headers = {}

            def get(self, url, **kwargs):
                with lock:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                time.sleep(0.03)
                with lock:
                    state["active"] -= 1
                return _Resp(headers={"x-concurrency-limit": "1"})

        # Première réponse : annonce une concurrence de 1.
        policy_get(Slow(), CR_DOI, min_interval=0)
        state["max"] = 0
        threads = [threading.Thread(target=policy_get,
                                    args=(Slow(), f"{CR_DOI}{i}"),
                                    kwargs={"min_interval": 0})
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert state["max"] == 1
        assert http_mod._inflight_semaphore("api.crossref.org").limit == 1

    def test_three_in_flight_to_crossref(self, fresh_policy):
        state = {"active": 0, "max": 0}
        lock = threading.Lock()
        gate = threading.Barrier(3, timeout=2)

        class Slow:
            headers = {}

            def get(self, url, **kwargs):
                with lock:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    pass
                time.sleep(0.02)
                with lock:
                    state["active"] -= 1
                return _Resp()

        threads = [threading.Thread(target=policy_get,
                                    args=(Slow(), f"{CR_DOI}{i}"),
                                    kwargs={"min_interval": 0})
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert state["max"] == 3


class TestHostSlots:
    def test_acquire_release_and_timeout(self):
        slots = http_mod._HostSlots(2)
        assert slots.acquire() and slots.acquire(timeout=0.01)
        assert slots.acquire(timeout=0.01) is False
        slots.release()
        assert slots.acquire(timeout=0.01)
        slots.release()
        slots.release()
        with pytest.raises(ValueError):
            slots.release()

    def test_announcement_never_raises_the_configured_cap(self):
        slots = http_mod._HostSlots(2)
        slots.announce(10)
        assert slots.limit == 2
        slots.announce(1)
        assert slots.limit == 1


# === Résolution de DOI : requêtes de liste ===

def test_doi_resolver_uses_the_list_interval_by_default():
    import inspect
    from utils.doi_resolver import resolve_dois
    default = inspect.signature(resolve_dois).parameters["min_interval"].default
    assert default == config.REQUEST_DELAY_CROSSREF_LIST


# === Tailles de lot ===

class _FakeCR:
    """Client simulé de /works : ``total`` notices, curseur."""

    def __init__(self, total):
        self.total = total
        self.calls = []

    def get(self, url, params=None, headers=None, **kw):
        params = dict(params or {})
        self.calls.append(params)
        rows = int(params["rows"])
        start = int(params["cursor"]) if params["cursor"] != "*" else 0
        n = max(0, min(rows, self.total - start))
        items = [{"DOI": f"10.1/{start + i}", "title": [f"T{start + i}"],
                  "type": "journal-article"} for i in range(n)]
        nxt = str(start + n) if start + n < self.total else None
        return _Resp(payload={"message": {"items": items, "next-cursor": nxt}})


class TestBatchSizes:
    def test_crossref_rows_is_the_api_max_bounded_by_the_cap(self):
        from connectors import crossref
        assert crossref.ROWS_PER_BATCH == 1000
        fake = _FakeCR(5000)
        c = crossref.CrossrefConnector(http_client=fake)
        assert len(c.search("q", max_results=200)) == 200
        assert [p["rows"] for p in fake.calls] == [200]
        fake = _FakeCR(5000)
        c = crossref.CrossrefConnector(http_client=fake)
        assert len(c.search("q", max_results=1500)) == 1500
        assert [p["rows"] for p in fake.calls] == [1000, 1000]
        assert c.last_status.limit_reached

    def test_crossref_small_result_set(self):
        from connectors import crossref
        fake = _FakeCR(120)
        c = crossref.CrossrefConnector(http_client=fake)
        assert len(c.search("q", max_results=1000)) == 120
        assert len(fake.calls) == 1

    def test_openalex_per_page_is_the_api_max_bounded_by_the_cap(self, monkeypatch):
        from connectors import openalex
        assert openalex.PER_PAGE == 200
        calls = []

        def fake_get_json(url, params=None, **kw):
            calls.append(dict(params))
            per = int(params["per-page"])
            start = 0 if params["cursor"] == "*" else int(params["cursor"])
            results = [{"id": f"W{start + i}", "title": f"T{start + i}",
                        "display_name": f"T{start + i}", "type": "article",
                        "authorships": []} for i in range(per)]
            return {"results": results, "meta": {"next_cursor": str(start + per)}}

        monkeypatch.setattr(openalex, "get_json", fake_get_json)

        class _H:
            session = object()
        c = openalex.OpenAlexConnector(http_client=_H(), api_key="k")
        assert len(c.search("q", max_results=150)) == 150
        assert [p["per-page"] for p in calls] == [150]
        calls.clear()
        assert len(c.search("q", max_results=500)) == 500
        assert [p["per-page"] for p in calls] == [200, 200, 200]
