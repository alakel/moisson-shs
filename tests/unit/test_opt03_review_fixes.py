"""
Tests des correctifs de revue d'OPT-03 (politique HTTP commune, utils.http).

Chaque classe couvre un constat de revue et échoue sur la version d'OPT-03
antérieure au correctif :

- jeton de pagination Semantic Scholar (``token``) pris pour une clé de
  compte : une barrière de politesse neuve par page, donc aucune attente
  entre pages ;
- ChunkedEncodingError / ContentDecodingError (connexion rompue en cours de
  lecture du corps) traitées comme définitives : lot perdu, source tronquée ;
- revalidation conditionnelle (304) comptée comme hit de cache et non comme
  accès réseau ;
- Retry-After respecté par le seul thread qui l'a reçu ;
- budget global non revérifié après l'attente de politesse et l'attente
  d'une place en vol.

Aucun accès réseau : sessions simulées, ou CachedSession requests-cache dont
le transport est un adaptateur local. L'horloge de utils.http est simulée,
sauf pour le test du plafond en vol (quelques dixièmes de seconde).
"""

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
    """Horloge simulée : ``sleep`` fait avancer le temps sans attendre."""

    def __init__(self, start=1000.0):
        self.now = start
        self.wall = 1_700_000_000.0
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
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.from_cache = False
        self.reason = ""
        self.url = ""
        self.text = ""

    def json(self):
        return self._payload


class _TimedSession:
    """
    Session simulée : file de réponses (ou d'exceptions) ; note l'heure
    simulée de chaque départ et fait avancer l'horloge d'une latence fixe.
    """

    def __init__(self, clock, responses, latency=0.0):
        self.clock = clock
        self._responses = list(responses)
        self.latency = latency
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params, "headers": headers,
                           "timeout": timeout, "at": self.clock.now})
        self.clock.now += self.latency
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def mount(self, *args):
        pass

    def close(self):
        pass


class _RevalidatingAdapter(HTTPAdapter):
    """Transport local : 200 avec ETag, puis 304 sur requête conditionnelle."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        if request.headers.get("If-None-Match") == '"v1"':
            raw = HTTPResponse(body=io.BytesIO(b""), headers={"ETag": '"v1"'},
                               status=304, preload_content=False,
                               request_url=request.url)
        else:
            raw = HTTPResponse(
                body=io.BytesIO(b'{"ok": 1}'),
                headers={"Content-Type": "application/json", "ETag": '"v1"'},
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
    http_mod.reset_http_policy()


@pytest.fixture
def fresh_policy():
    """Politique et compteurs remis à zéro, horloge réelle."""
    http_mod.reset_http_policy()
    http_mod.reset_http_stats()
    yield
    http_mod.reset_http_policy()


def _s2_page(paper_id, token):
    return _Resp(200, {"data": [{"paperId": paper_id, "title": paper_id}],
                       "token": token})


# === Jeton de pagination Semantic Scholar ===

class TestPaginationTokenIsNotAnAccount:
    def test_pagination_tokens_do_not_change_the_account(self):
        assert http_mod.account_fingerprint({"token": "p2"}) == ""
        assert http_mod.account_fingerprint({"cursor": "*", "key": "x"}) == ""
        with_key = http_mod.account_fingerprint(
            {"token": "p2"}, {"x-api-key": "K"})
        assert with_key != ""
        assert with_key == http_mod.account_fingerprint(
            {"token": "p3"}, {"x-api-key": "K"})

    @pytest.mark.parametrize("api_key", ["K", None])
    def test_s2_pages_are_spaced_by_host_interval(self, clock, api_key):
        # Trois pages S2 (jetons de continuation) sur un vrai HTTPClient :
        # sur la version fautive, chaque page ouvrait une barrière neuve,
        # d'où des départs espacés de la seule latence (0,1 s).
        from connectors.semantic_scholar import SemanticScholarConnector
        client = HTTPClient(use_cache=False)
        client.session = _TimedSession(clock, [
            _s2_page("a", "tokPAGE2"), _s2_page("b", "tokPAGE3"),
            _s2_page("c", None),
        ], latency=0.1)
        c = SemanticScholarConnector(http_client=client, api_key="K")
        c.api_key = api_key
        c.headers = {"x-api-key": api_key} if api_key else None
        assert len(c.search("llm", max_results=10)) == 3
        calls = client.session.calls
        assert [(x["params"] or {}).get("token") for x in calls] == \
            [None, "tokPAGE2", "tokPAGE3"]
        starts = [x["at"] for x in calls]
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert gaps == [pytest.approx(config.REQUEST_DELAY_S2)] * 2
        # Une seule barrière pour l'hôte : le registre ne grossit plus
        # d'une entrée par page.
        gates = [k for k in http_mod._gates
                 if k[0] == "api.semanticscholar.org"]
        assert len(gates) == 1


# === Connexion rompue en cours de lecture du corps ===

class TestBrokenBodyIsTransient:
    @pytest.mark.parametrize("exc", [
        requests.exceptions.ChunkedEncodingError(
            "Connection broken: IncompleteRead(100 bytes read, 900 more "
            "expected)"),
        requests.exceptions.ContentDecodingError("gzip tronqué"),
    ])
    def test_retried_then_success(self, clock, exc):
        sess = _TimedSession(clock, [exc, _Resp(200, {"ok": 1})])
        resp = policy_get(sess, "https://broken-body.test/x")
        assert resp.status_code == 200
        assert len(sess.calls) == 2
        stats = http_mod.get_http_stats("broken-body.test")
        assert stats["retries"] == 1 and stats["errors"] == 0

    def test_request_errors_stay_definitive(self, clock):
        sess = _TimedSession(clock, [requests.exceptions.InvalidURL("x")] * 3)
        with pytest.raises(requests.exceptions.InvalidURL):
            policy_get(sess, "https://invalid.test/x")
        assert len(sess.calls) == 1

    def test_hal_batch_not_lost_on_incomplete_read(self, clock):
        # Sur la version fautive : lot 2 perdu, 1 notice sur 2 collectée.
        from connectors.hal import HALConnector

        def page(docid):
            return _Resp(200, {
                "response": {"numFound": 2, "docs": [{
                    "docid": docid, "title_s": [f"T{docid}"],
                    "docType_s": "ART", "halId_s": f"hal-{docid}"}]},
                "nextCursorMark": "c2",
            })

        client = HTTPClient(use_cache=False)
        client.session = _TimedSession(clock, [
            page(1),
            requests.exceptions.ChunkedEncodingError(
                "Connection broken: IncompleteRead"),
            page(2),
        ])
        items = HALConnector(http_client=client).search("q", max_results=10)
        assert len(items) == 2


# === Compteurs : revalidation conditionnelle ===

class TestRevalidationCounters:
    def test_304_revalidation_counts_as_network(self, clock, tmp_path):
        # TTL nul : chaque lecture après la première revalide l'entrée par
        # une requête conditionnelle (If-None-Match), servie en 304.
        client = HTTPClient(use_cache=True, cache_ttl=0,
                            cache_dir=str(tmp_path / "http"))
        adapter = _RevalidatingAdapter()
        client.session.mount("https://", adapter)
        for _ in range(3):
            assert client.get("https://reval.test/x",
                              min_interval=0).json() == {"ok": 1}
        assert len(adapter.sent) == 3
        assert all(r.headers.get("If-None-Match") == '"v1"'
                   for r in adapter.sent[1:])
        stats = http_mod.get_http_stats("reval.test")
        assert stats["network"] == 3
        assert stats["cache_hits"] == 0
        assert stats["cache_misses"] == 3
        assert stats["cache_hits"] + stats["cache_misses"] == stats["logical"]
        client.close()

    def test_cache_filled_after_probe_counts_once(self, clock, tmp_path,
                                                  monkeypatch):
        # Course : l'entrée apparaît entre la sonde et l'envoi. La requête
        # logique est un hit (aucun accès réseau), comptée une seule fois.
        client = HTTPClient(use_cache=True, cache_dir=str(tmp_path / "http"))
        adapter = _RevalidatingAdapter()
        client.session.mount("https://", adapter)
        client.get("https://race.test/x", min_interval=0)
        monkeypatch.setattr(http_mod, "_probe_cache", lambda *a, **k: None)
        client.get("https://race.test/x", min_interval=0)
        assert len(adapter.sent) == 1
        stats = http_mod.get_http_stats("race.test")
        assert stats["network"] == 1
        assert stats["cache_hits"] == 1
        assert stats["cache_misses"] == 1
        client.close()


# === Retry-After partagé entre threads ===

class TestRetryAfterSharedByHost:
    def test_gate_defer(self, clock):
        gate = http_mod._gate("defer.test", "")
        gate.defer(clock.now + 5)
        wait, _ = gate.reserve(0.1)
        assert wait == pytest.approx(5.0)
        # Une échéance plus proche ne raccourcit pas la précédente
        gate.defer(clock.now + 1)
        wait, _ = http_mod._gate("defer.test", "").reserve(0)
        assert wait >= 5.0

    def test_other_requests_wait_for_retry_after(self, clock, monkeypatch):
        # Pendant l'attente du Retry-After reçu par la requête A, une autre
        # requête (B) vise le même hôte : elle doit attendre l'échéance au
        # lieu de partir à l'intervalle normal (0,05 s).
        url = "https://retry-after-host.test/search"
        sess = _TimedSession(clock, [
            _Resp(429, headers={"Retry-After": "2"}),   # A, 1re tentative
            _Resp(200),                                 # B
            _Resp(200),                                 # A, réessai
        ])
        real_sleep = clock.sleep
        state = {"b_sent": False}

        def sleep(seconds):
            if not state["b_sent"]:
                state["b_sent"] = True
                policy_get(sess, url, params={"page": 2}, min_interval=0.05)
            real_sleep(seconds)

        monkeypatch.setattr(clock, "sleep", sleep)
        t0 = clock.now
        policy_get(sess, url, params={"page": 1}, min_interval=0.05)
        b_call = sess.calls[1]
        assert b_call["params"] == {"page": 2}
        assert b_call["at"] - t0 >= 2.0

    def test_other_account_not_deferred(self, clock):
        gate_a = http_mod._gate("acct-defer.test", "aaa")
        gate_a.defer(clock.now + 30)
        wait, _ = http_mod._gate("acct-defer.test", "bbb").reserve(0)
        assert wait == 0


# === Budget revérifié après les attentes ===

class TestBudgetAfterWaits:
    def test_polite_slot_beyond_budget_refused_without_sleeping(self, clock):
        sess = _TimedSession(clock, [_Resp(200)] * 3)
        url = "https://budget-polite.test/works"
        policy_get(sess, url, params={"offset": 0}, budget=5, min_interval=6)
        with pytest.raises(requests.Timeout):
            policy_get(sess, url, params={"offset": 10}, budget=5,
                       min_interval=6)
        # Refus immédiat : ni envoi, ni attente de 6 s
        assert len(sess.calls) == 1
        assert clock.sleeps == []
        assert http_mod.get_http_stats("budget-polite.test")["errors"] == 1
        # Le créneau refusé est rendu : la requête suivante n'attend que
        # l'intervalle depuis le premier départ, pas deux intervalles.
        policy_get(sess, url, params={"offset": 20}, budget=30,
                   min_interval=6)
        assert clock.sleeps == [pytest.approx(6.0)]

    def test_timeout_recomputed_after_polite_wait(self, clock):
        sess = _TimedSession(clock, [_Resp(200)] * 2)
        url = "https://budget-timeout.test/x"
        policy_get(sess, url, params={"i": 0}, budget=10, min_interval=3)
        policy_get(sess, url, params={"i": 1}, budget=10, min_interval=3)
        assert clock.sleeps == [pytest.approx(3.0)]
        # Reste réel après l'attente de politesse : 7 s
        expected = tuple(min(t, 7.0) for t in config.HTTP_TIMEOUT)
        assert sess.calls[1]["timeout"] == tuple(
            pytest.approx(t) for t in expected)

    def test_in_flight_wait_bounded_by_budget(self, fresh_policy,
                                              monkeypatch):
        monkeypatch.setattr(http_mod, "HTTP_MAX_IN_FLIGHT_PER_HOST", 1)
        http_mod.reset_http_policy()
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class Blocking:
            headers = {}

            def get(self, url, params=None, **kwargs):
                calls.append(params)
                entered.set()
                release.wait(5)
                return _Resp(200)

        session = Blocking()
        holder = threading.Thread(
            target=policy_get, args=(session, "https://inflight-budget.test/x"),
            kwargs={"params": {"i": 0}, "min_interval": 0})
        holder.start()
        # Filet : libère la place même si la requête bornée ne rend pas la
        # main (version fautive), pour que le test échoue sans se bloquer.
        timer = threading.Timer(2.0, release.set)
        timer.start()
        try:
            assert entered.wait(5)
            began = time.monotonic()
            with pytest.raises(requests.Timeout):
                policy_get(session, "https://inflight-budget.test/x",
                           params={"i": 1}, min_interval=0, budget=0.2)
            elapsed = time.monotonic() - began
        finally:
            release.set()
            timer.cancel()
            holder.join(5)
        assert elapsed < 1.0
        # La requête bornée n'a jamais été envoyée
        assert calls == [{"i": 0}]
