"""
Tests de la politique HTTP commune (OPT-03) : utils.http.policy_get et ses
utilisateurs (HTTPClient, http_scholarly, connecteurs, lookups, doi_resolver).

Couvre :
- 4xx définitif = une seule tentative (constat C06 : trois tentatives et
  3,3 s d'attente sur la baseline) ;
- 429 avec Retry-After (secondes, date HTTP), plafond du Retry-After ;
- 503 répétés : tentatives bornées, plafond du temps d'attente cumulé ;
- délais dépassés : réessais bornés, budget global par requête logique ;
- réponse servie par requests-cache : aucune temporisation (constat C08,
  second volet : Semantic Scholar, CORE, Google Scholar, résolution de DOI) ;
- politesse par hôte et par compte, partagée entre threads ; plafond en vol ;
- fusion des requêtes identiques simultanées (single-flight) ;
- compteurs par hôte ; masquage des clés d'API.

Aucun accès réseau : sessions simulées, ou CachedSession requests-cache dont
le transport est remplacé par un adaptateur local. L'horloge de utils.http
est simulée (le sommeil fait avancer le temps sans attendre) ; les seuls
tests en temps réel (threads) n'attendent que quelques centièmes de seconde.
"""

import email.utils
import io
import threading
import time

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse

import config
import utils.http as http_mod
from utils.http import HTTPClient, policy_get


# === Outils de test ===

class _FakeClock:
    """
    Horloge simulée de utils.http.

    ``monotonic`` et ``time`` avancent ensemble à chaque ``sleep`` ; l'heure
    murale (pour les Retry-After en date HTTP) est distincte de l'horloge
    monotone, comme dans la réalité.
    """

    def __init__(self, monotonic_start=1000.0, wall_start=1_700_000_000.0):
        self.now = monotonic_start
        self.wall = wall_start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.wall

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if seconds > 0:
            self.now += seconds
            self.wall += seconds


class _Resp:
    def __init__(self, status=200, payload=None, headers=None, url=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.from_cache = False
        self.reason = ""
        self.url = url
        self.text = ""

    def json(self):
        return self._payload


class _Session:
    """Session simulée : file de réponses (ou d'exceptions), appels notés."""

    def __init__(self, responses, clock=None, cost=0.0):
        self._responses = list(responses)
        self.calls = []
        self.headers = {}
        self._clock = clock
        self._cost = cost

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params, "headers": headers,
                           "timeout": timeout})
        if self._clock is not None and self._cost:
            self._clock.now += self._cost
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def mount(self, *args):
        pass

    def close(self):
        pass


class _LocalAdapter(HTTPAdapter):
    """
    Transport local pour CachedSession : aucune requête ne sort de la machine.
    Chaque appel est noté ; le corps renvoyé est fixe.
    """

    def __init__(self, body: bytes):
        super().__init__()
        self.body = body
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        raw = HTTPResponse(
            body=io.BytesIO(self.body),
            headers={"Content-Type": "application/json"},
            status=200, preload_content=False, request_url=request.url,
        )
        return self.build_response(request, raw)


@pytest.fixture
def clock(monkeypatch):
    """Horloge simulée + politique et compteurs remis à zéro."""
    fake = _FakeClock()
    monkeypatch.setattr(http_mod, "time", fake)
    http_mod.reset_http_policy()
    http_mod.reset_http_stats()
    yield fake
    # Créneaux réservés sur l'horloge simulée : à oublier avant les tests
    # suivants (ils seraient comparés à l'horloge réelle).
    http_mod.reset_http_policy()


@pytest.fixture
def fresh_policy():
    """Politique et compteurs remis à zéro, horloge réelle."""
    http_mod.reset_http_policy()
    http_mod.reset_http_stats()
    yield
    http_mod.reset_http_policy()


def _cached_client(tmp_path, body: bytes):
    """HTTPClient à cache requests-cache réel, transport local."""
    client = HTTPClient(use_cache=True, cache_dir=str(tmp_path / "http"))
    adapter = _LocalAdapter(body)
    client.session.mount("https://", adapter)
    client.session.mount("http://", adapter)
    return client, adapter


# === 4xx définitifs : une seule tentative (C06) ===

class TestDefinitive4xx:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 410, 422])
    def test_one_attempt_no_retry_sleep(self, clock, status):
        client = HTTPClient(use_cache=False)
        client.session = _Session([_Resp(status)] * 3)
        with pytest.raises(requests.HTTPError) as err:
            client.get(f"https://definitive-{status}.test/x")
        assert len(client.session.calls) == 1
        assert err.value.response.status_code == status
        # Premier accès à l'hôte : ni politesse ni backoff
        assert clock.sleeps == []
        stats = http_mod.get_http_stats(f"definitive-{status}.test")
        assert stats["network"] == 1 and stats["retries"] == 0
        assert stats["errors"] == 1

    def test_http_scholarly_404_one_attempt(self, clock):
        from utils.http_scholarly import get_json
        sess = _Session([_Resp(404)] * 3)
        with pytest.raises(requests.HTTPError):
            get_json("https://scholarly-404.test/x", session=sess)
        assert len(sess.calls) == 1

    def test_budget_exhausted_is_not_retried(self, clock):
        from utils.http_scholarly import BudgetExhausted, get_json
        sess = _Session([_Resp(402)] * 3)
        with pytest.raises(BudgetExhausted):
            get_json("https://budget-402.test/x", session=sess)
        assert len(sess.calls) == 1


# === 429 et Retry-After ===

class TestRetryAfter:
    def test_429_retry_after_seconds_then_success(self, clock):
        sess = _Session([_Resp(429, headers={"Retry-After": "2"}),
                         _Resp(200, {"ok": 1})])
        resp = policy_get(sess, "https://ra.test/x")
        assert resp.status_code == 200
        assert len(sess.calls) == 2
        # L'attente demandée par le serveur est respectée à l'identique
        assert clock.sleeps == [2.0]
        stats = http_mod.get_http_stats("ra.test")
        assert stats["http_429"] == 1 and stats["retries"] == 1
        assert stats["retry_wait_s"] == pytest.approx(2.0)

    def test_429_retry_after_http_date(self, clock):
        when = email.utils.formatdate(clock.wall + 5, usegmt=True)
        sess = _Session([_Resp(429, headers={"Retry-After": when}),
                         _Resp(200, {"ok": 1})])
        policy_get(sess, "https://ra-date.test/x")
        assert len(sess.calls) == 2
        assert clock.sleeps == [pytest.approx(5.0)]

    def test_retry_after_above_cap_gives_up_without_waiting(self, clock):
        too_long = config.HTTP_RETRY_AFTER_MAX + 60
        sess = _Session([_Resp(429, headers={"Retry-After": str(too_long)}),
                         _Resp(200)])
        with pytest.raises(requests.HTTPError) as err:
            policy_get(sess, "https://ra-cap.test/x")
        assert err.value.response.status_code == 429
        assert len(sess.calls) == 1
        assert clock.sleeps == []

    def test_scholarly_long_reset_still_budget_exhausted(self, clock):
        from utils.http_scholarly import BudgetExhausted, get_json
        sess = _Session([_Resp(429, headers={"Retry-After": "7200"})])
        with pytest.raises(BudgetExhausted):
            get_json("https://reset.test/x", session=sess)
        assert clock.sleeps == []

    def test_parse_retry_after(self, clock):
        assert http_mod.parse_retry_after("3") == 3.0
        assert http_mod.parse_retry_after(" 0 ") == 0.0
        assert http_mod.parse_retry_after(None) is None
        assert http_mod.parse_retry_after("bientôt") is None
        past = email.utils.formatdate(clock.wall - 30, usegmt=True)
        assert http_mod.parse_retry_after(past) == 0.0


# === Transitoires : tentatives et attentes bornées ===

class TestTransientBounds:
    def test_503_bounded_attempts_no_final_backoff(self, clock):
        sess = _Session([_Resp(503)] * 10)
        with pytest.raises(requests.HTTPError) as err:
            policy_get(sess, "https://flaky.test/x")
        assert err.value.response.status_code == 503
        assert len(sess.calls) == config.HTTP_MAX_RETRIES
        # Backoff exponentiel entre tentatives, rien après la dernière
        expected = [config.HTTP_BACKOFF_FACTOR * 2 ** i
                    for i in range(config.HTTP_MAX_RETRIES - 1)]
        assert clock.sleeps == expected

    def test_503_total_wait_cap(self, clock, monkeypatch):
        monkeypatch.setattr(http_mod, "HTTP_MAX_TOTAL_WAIT", 60.0)
        monkeypatch.setattr(http_mod, "HTTP_MAX_RETRIES", 5)
        sess = _Session([_Resp(503, headers={"Retry-After": "40"})] * 10)
        with pytest.raises(requests.HTTPError):
            policy_get(sess, "https://capped.test/x")
        # 40 s honorées une fois ; 40 + 40 > 60 : abandon sans attendre
        assert clock.sleeps == [40.0]
        assert len(sess.calls) == 2

    def test_timeout_bounded_retries(self, clock):
        sess = _Session([requests.ReadTimeout("lecture trop lente")] * 10)
        with pytest.raises(requests.ReadTimeout):
            policy_get(sess, "https://slow.test/x")
        assert len(sess.calls) == config.HTTP_MAX_RETRIES
        assert len(clock.sleeps) == config.HTTP_MAX_RETRIES - 1

    def test_connection_error_then_success(self, clock):
        sess = _Session([requests.ConnectionError("refusée"), _Resp(200)])
        assert policy_get(sess, "https://reco.test/x").status_code == 200
        assert len(sess.calls) == 2

    def test_global_budget_bounds_attempts_and_timeouts(self, clock):
        # Chaque tentative « consomme » 1 s d'horloge avant d'échouer.
        sess = _Session([requests.ConnectTimeout("injoignable")] * 10,
                        clock=clock, cost=1.0)
        with pytest.raises(requests.ConnectTimeout):
            policy_get(sess, "https://budget.test/x", budget=2.5)
        # t=0 : essai (1 s) ; attente 1 s ; t=2 : essai (1 s) ; la
        # troisième tentative dépasserait le budget.
        assert len(sess.calls) == 2
        # Les délais de chaque tentative sont bornés par le budget restant.
        assert sess.calls[0]["timeout"] == (2.5, 2.5)
        assert sess.calls[1]["timeout"] == (pytest.approx(0.5),
                                            pytest.approx(0.5))

    def test_scalar_timeout_keeps_short_connect(self, clock):
        sess = _Session([_Resp(200)])
        policy_get(sess, "https://scalar.test/x", timeout=60)
        assert sess.calls[0]["timeout"] == (config.HTTP_CONNECT_TIMEOUT, 60.0)

    def test_default_timeout_is_connect_read_tuple(self, clock):
        sess = _Session([_Resp(200)])
        policy_get(sess, "https://default.test/x")
        assert sess.calls[0]["timeout"] == config.HTTP_TIMEOUT


# === Politesse par hôte et par compte ===

class TestPoliteness:
    def test_sequential_same_host_waits_interval(self, clock):
        sess = _Session([_Resp(200)] * 3)
        policy_get(sess, "https://polite.test/a", min_interval=0.5)
        policy_get(sess, "https://polite.test/b", min_interval=0.5)
        # Autre hôte : aucun effet du premier
        policy_get(sess, "https://other.test/a", min_interval=0.5)
        assert clock.sleeps == [0.5]

    def test_accounts_are_separate_on_same_host(self, clock):
        sess = _Session([_Resp(200)] * 3)
        policy_get(sess, "https://acct.test/x", params={"api_key": "A"},
                   min_interval=1.0)
        policy_get(sess, "https://acct.test/x", params={"api_key": "B"},
                   min_interval=1.0)
        assert clock.sleeps == []
        policy_get(sess, "https://acct.test/y", params={"api_key": "A"},
                   min_interval=1.0)
        assert clock.sleeps == [1.0]

    def test_host_default_intervals(self):
        assert http_mod.host_min_interval("api.semanticscholar.org") == \
            config.REQUEST_DELAY_S2
        assert http_mod.host_min_interval("api.core.ac.uk") == \
            config.REQUEST_DELAY_CORE
        assert http_mod.host_min_interval("serpapi.com") == \
            config.REQUEST_DELAY_SERPAPI
        assert http_mod.host_min_interval("api.crossref.org") == \
            config.REQUEST_DELAY_CROSSREF
        assert http_mod.host_min_interval("api.archives-ouvertes.fr") == \
            config.REQUEST_DELAY

    def test_gate_shared_between_threads(self, clock):
        # Horloge figée : trois threads réservent trois créneaux distincts
        # au lieu de partir ensemble.
        gate = http_mod._gate("threads.test", "")
        waits = []
        barrier = threading.Barrier(3)

        def worker():
            barrier.wait()
            waits.append(gate.reserve(0.3)[0])

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert sorted(round(w, 6) for w in waits) == [0.0, 0.3, 0.6]

    def test_interval_counts_from_actual_start(self, clock, monkeypatch):
        # Sommeil instantané qui ne fait pas avancer l'horloge (comme le banc
        # avec --sleep-scale 0) : les attentes comptées restent égales à
        # l'intervalle au lieu de croître à chaque requête.
        waits = []
        monkeypatch.setattr(clock, "sleep", lambda s: waits.append(s))
        sess = _Session([_Resp(200)] * 5)
        for i in range(5):
            policy_get(sess, "https://drift.test/x", params={"i": i},
                       min_interval=0.3)
        assert waits == [pytest.approx(0.3)] * 4

    def test_two_threads_same_host_real_time(self, fresh_policy):
        # Temps réel, intervalle court : les départs vers un même hôte sont
        # espacés d'au moins l'intervalle, même depuis plusieurs threads.
        interval = 0.05
        starts = []
        lock = threading.Lock()

        class Recorder:
            headers = {}

            def get(self, url, **kwargs):
                with lock:
                    starts.append(time.monotonic())
                return _Resp(200)

        session = Recorder()
        threads = [
            threading.Thread(target=policy_get, args=(session,
                             "https://realtime.test/x"),
                             kwargs={"params": {"i": i},
                                     "min_interval": interval})
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert len(gaps) == 2
        # Tolérance d'ordonnancement des threads (quelques ms)
        assert all(g >= interval - 0.015 for g in gaps), gaps

    def test_in_flight_cap_per_host(self, fresh_policy, monkeypatch):
        monkeypatch.setattr(http_mod, "HTTP_MAX_IN_FLIGHT_PER_HOST", 1)
        http_mod.reset_http_policy()
        state = {"active": 0, "max": 0}
        lock = threading.Lock()

        class Slow:
            headers = {}

            def get(self, url, **kwargs):
                with lock:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                time.sleep(0.02)
                with lock:
                    state["active"] -= 1
                return _Resp(200)

        session = Slow()
        threads = [
            threading.Thread(target=policy_get,
                             args=(session, "https://inflight.test/x"),
                             kwargs={"params": {"i": i}, "min_interval": 0})
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert state["max"] == 1


# === Fusion des requêtes identiques simultanées ===

class TestSingleFlight:
    def test_identical_concurrent_requests_hit_network_once(self,
                                                            fresh_policy):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class Blocking:
            headers = {}

            def get(self, url, **kwargs):
                calls.append(url)
                entered.set()
                release.wait(5)
                return _Resp(200, {"ok": 1})

        session = Blocking()
        results = []

        def worker():
            results.append(policy_get(session, "https://flight.test/x",
                                      params={"q": "a"}, min_interval=0))

        t1 = threading.Thread(target=worker)
        t1.start()
        assert entered.wait(5)
        t2 = threading.Thread(target=worker)
        t2.start()
        # Attendre que la seconde requête soit reçue (compteur « logical »),
        # puis lui laisser le temps de rejoindre la requête en cours.
        limit = time.monotonic() + 5
        while (http_mod.get_http_stats("flight.test")["logical"] < 2
               and time.monotonic() < limit):
            time.sleep(0.005)
        time.sleep(0.05)
        release.set()
        t1.join(5)
        t2.join(5)
        assert len(calls) == 1
        assert len(results) == 2 and results[0] is results[1]
        assert http_mod.get_http_stats("flight.test")["coalesced"] == 1

    def test_different_params_or_accounts_are_not_merged(self, clock):
        sess = _Session([_Resp(200)] * 3)
        policy_get(sess, "https://nf.test/x", params={"q": "a"})
        policy_get(sess, "https://nf.test/x", params={"q": "b"})
        policy_get(sess, "https://nf.test/x", params={"q": "a",
                                                       "api_key": "K"})
        assert len(sess.calls) == 3


# === Cache : aucune temporisation sur une réponse déjà disponible (C08) ===

class TestNoSleepOnCacheHit:
    def test_policy_cache_hit_no_sleep_and_counters(self, clock, tmp_path):
        client, adapter = _cached_client(tmp_path, b'{"ok": 1}')
        client.get("https://cached.test/x", params={"q": 1}, min_interval=5)
        client.get("https://cached.test/x", params={"q": 1}, min_interval=5)
        client.get("https://cached.test/x", params={"q": 1}, min_interval=5)
        assert len(adapter.requests) == 1
        # Horloge figée entre les appels : un accès réseau aurait attendu 5 s
        assert clock.sleeps == []
        stats = http_mod.get_http_stats("cached.test")
        assert stats["network"] == 1
        assert stats["cache_misses"] == 1
        assert stats["cache_hits"] == 2
        client.close()

    def test_semantic_scholar_cache_hit_no_throttle(self, clock, tmp_path):
        from connectors.semantic_scholar import SemanticScholarConnector
        body = (b'{"data": [{"title": "A", "externalIds": {"DOI": "10.1/a"}}],'
                b' "token": null}')
        client, adapter = _cached_client(tmp_path, body)
        c = SemanticScholarConnector(http_client=client, api_key="K")
        assert len(c.search("llm", max_results=1)) == 1
        assert len(c.search("llm", max_results=1)) == 1
        assert len(adapter.requests) == 1
        assert clock.sleeps == []
        client.close()

    def test_core_cache_hit_no_throttle(self, clock, tmp_path):
        from connectors.core import CoreConnector
        body = (b'{"results": [{"id": 1, "title": "A", '
                b'"documentType": "research"}], "totalHits": 1}')
        client, adapter = _cached_client(tmp_path, body)
        c = CoreConnector(http_client=client, api_key="K")
        assert len(c.search("oa", max_results=1)) == 1
        assert len(c.search("oa", max_results=1)) == 1
        assert len(adapter.requests) == 1
        # Sans cache, CORE imposerait 6 s entre les deux requêtes
        assert clock.sleeps == []
        client.close()

    def test_google_scholar_cached_page_costs_no_credit(self, clock,
                                                        tmp_path):
        # Transport local : SerpAPI n'est jamais contacté.
        from connectors.google_scholar import GoogleScholarConnector
        body = (b'{"search_information": {"total_results": 1},'
                b' "organic_results": [{"title": "Item", "result_id": "r1",'
                b' "link": "https://example.org/1", "publication_info":'
                b' {"summary": "A Author - Revue X, 2020 - example.org"}}]}')
        client, adapter = _cached_client(tmp_path, body)
        c = GoogleScholarConnector(http_client=client, api_key="K",
                                   min_credits=0)
        assert len(c.search("q", max_results=1)) == 1
        assert len(c.search("q", max_results=1)) == 1
        assert len(adapter.requests) == 1
        assert clock.sleeps == []
        client.close()

    def test_serpapi_account_balance_never_cached(self, clock, tmp_path):
        from connectors.google_scholar import GoogleScholarConnector
        client, adapter = _cached_client(
            tmp_path, b'{"total_searches_left": 4000, "plan_name": "X"}')
        c = GoogleScholarConnector(http_client=client, api_key="K",
                                   min_interval=0)
        assert c.account_status()["total_searches_left"] == 4000
        assert c.account_status()["total_searches_left"] == 4000
        # Deux lectures, deux accès : le solde est toujours frais
        assert len(adapter.requests) == 2
        assert all(r.headers.get("Cache-Control") == "no-store"
                   for r in adapter.requests)
        client.close()

    def test_doi_resolver_cache_hit_no_sleep(self, clock, tmp_path):
        from utils.doi_resolver import resolve_dois
        client, adapter = _cached_client(
            tmp_path, b'{"message": {"items": []}}')
        corpus = [
            {"itemType": "journalArticle", "title": f"Titre distinct {i}",
             "creators": [], "date": "2020", "extra": ""}
            for i in range(2)
        ]
        _, rep1 = resolve_dois(corpus, session=client.session)
        assert rep1["attempted"] == 2 and len(adapter.requests) == 2
        # Premier passage : politesse CrossRef entre les deux accès réseau
        # (requêtes de liste query.bibliographic : intervalle propre depuis
        # OPT-09, limite de 3 requêtes/s annoncée par CrossRef)
        assert clock.sleeps == [
            pytest.approx(config.REQUEST_DELAY_CROSSREF_LIST)]
        del clock.sleeps[:]
        _, rep2 = resolve_dois(corpus, session=client.session)
        assert rep2["attempted"] == 2
        # Second passage servi par le cache : aucune attente
        assert len(adapter.requests) == 2
        assert clock.sleeps == []
        client.close()


# === Compteurs ===

class TestCounters:
    def test_counters_by_host_and_reset(self, clock):
        client = HTTPClient(use_cache=False)
        client.session = _Session([
            _Resp(404),
            _Resp(429, headers={"Retry-After": "1"}), _Resp(200),
        ])
        with pytest.raises(requests.HTTPError):
            client.get("https://count-a.test/x")
        client.get("https://count-b.test/x")
        stats = http_mod.get_http_stats()
        assert stats["count-a.test"]["network"] == 1
        assert stats["count-a.test"]["errors"] == 1
        assert stats["count-a.test"]["retries"] == 0
        assert stats["count-b.test"]["network"] == 2
        assert stats["count-b.test"]["http_429"] == 1
        assert stats["count-b.test"]["retries"] == 1
        assert stats["count-b.test"]["errors"] == 0
        assert stats["count-b.test"]["wait_s"] == pytest.approx(1.0)
        # Copie : la modifier n'altère pas les compteurs
        stats["count-a.test"]["network"] = 99
        assert http_mod.get_http_stats("count-a.test")["network"] == 1
        http_mod.reset_http_stats()
        assert http_mod.get_http_stats() == {}
        assert http_mod.get_http_stats("count-a.test")["network"] == 0


# === Secrets ===

class TestSecrets:
    def test_fingerprint_never_contains_the_key(self):
        fp = http_mod.account_fingerprint({"api_key": "SECRET123"})
        assert fp and "SECRET123" not in fp
        assert fp == http_mod.account_fingerprint({"api_key": "SECRET123"})
        assert fp != http_mod.account_fingerprint({"api_key": "AUTRE"})
        assert http_mod.account_fingerprint({"q": "x"}) == ""
        assert http_mod.account_fingerprint(
            None, {"Authorization": "Bearer T"}) != ""

    def test_http_error_message_is_redacted(self, clock):
        resp = _Resp(401, url="https://serpapi.com/search.json?engine=g"
                              "&api_key=SECRET123")
        sess = _Session([resp])
        with pytest.raises(requests.HTTPError) as err:
            policy_get(sess, "https://serpapi.com/search.json",
                       params={"api_key": "SECRET123"})
        assert "SECRET123" not in str(err.value)
        assert err.value.response.status_code == 401

    def test_transport_error_message_is_redacted(self, clock):
        exc = requests.ConnectionError(
            "Max retries exceeded with url: /works?api_key=SECRET123&q=x")
        sess = _Session([exc] * 5)
        with pytest.raises(requests.ConnectionError) as err:
            policy_get(sess, "https://leak.test/works",
                       params={"api_key": "SECRET123"})
        assert "SECRET123" not in str(err.value)

    def test_serpapi_balance_failure_log_never_shows_the_key(self, clock,
                                                             caplog):
        # Sur la baseline, raise_for_status() produisait « … for url:
        # https://serpapi.com/account.json?api_key=<clé> », journalisé tel
        # quel par account_status.
        from connectors.google_scholar import GoogleScholarConnector
        client = HTTPClient(use_cache=False)
        client.session = _Session([_Resp(
            401, url="https://serpapi.com/account.json?api_key=SECRETKEY")])
        c = GoogleScholarConnector(http_client=client, api_key="SECRETKEY")
        with caplog.at_level("DEBUG"):
            assert c.account_status() == {}
        assert "SECRETKEY" not in caplog.text
        assert "api_key=***" in caplog.text

    def test_stats_keyed_by_host_only(self, clock):
        sess = _Session([_Resp(200)])
        policy_get(sess, "https://keyed.test/x", params={"api_key": "S3"})
        assert "S3" not in repr(http_mod.get_http_stats())


# === Lookups : une tentative par résolveur sur 404 (C06 + C13) ===

class TestLookupsPolicy:
    def test_fetch_by_doi_all_404_one_attempt_each(self, clock, monkeypatch):
        import mappers.zotero_schema_loader as zsl
        from utils.lookups import fetch_by_doi
        monkeypatch.setattr(zsl, "load_zotero_schema", lambda *a, **k: None,
                            raising=False)
        for var in ("OPENALEX_API_KEY", "CORE_API_KEY",
                    "SEMANTIC_SCHOLAR_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        client = HTTPClient(use_cache=False)
        client.session = _Session([_Resp(404)] * 20)
        assert fetch_by_doi("10.9999/inexistant", http_client=client) is None
        hosts = [c["url"].split("/")[2] for c in client.session.calls]
        # CrossRef, HAL, Isidore, Semantic Scholar : un accès chacun
        assert sorted(hosts) == sorted([
            "api.crossref.org", "api.archives-ouvertes.fr",
            "api.isidore.science", "api.semanticscholar.org",
        ])
