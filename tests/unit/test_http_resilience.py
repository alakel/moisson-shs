"""
Tests de résilience réseau du client HTTP (durcissement moisson_search).

Objectif : garantir qu'un hôte lent/mort ne fait plus exploser le temps
total d'un moisson_search (cause des timeouts d'outil côté Claude Desktop).

Vérifie :
- `HTTP_TIMEOUT` est un tuple ``(connect, read)`` et est transmis à requests ;
- le nombre de tentatives est borné par `HTTP_MAX_RETRIES` ;
- AUCUN backoff n'est appliqué après la dernière tentative (le backoff final,
  inutile, était la principale source de latence) ;
- l'exception réelle (avec code de statut pour les 5xx) est propagée.

L'horloge de utils.http est simulée : les tests sont instantanés.
"""

import sys
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from utils.http import HTTPClient  # noqa: E402
import utils.http as http_mod  # noqa: E402


class _FakeResp:
    def __init__(self, status=200):
        self.status_code = status
        self.from_cache = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class _FakeClock:
    """
    Horloge simulée de utils.http : ``sleep`` enregistre la durée et fait
    avancer ``monotonic`` sans attendre réellement.

    L'horloge doit avancer avec les sommeils : la politesse par hôte mesure
    l'intervalle écoulé depuis le départ précédent, et un sommeil qui ne fait
    pas passer le temps fabriquerait des attentes de politesse fictives.
    """

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


@pytest.fixture
def captured_sleeps(monkeypatch):
    """Horloge simulée dans utils.http ; renvoie la liste des sommeils."""
    clock = _FakeClock()
    monkeypatch.setattr(http_mod, "time", clock)
    http_mod.reset_http_policy()
    yield clock.sleeps
    # Les créneaux réservés sur l'horloge simulée ne doivent pas survivre
    # au test (ils seraient comparés à l'horloge réelle ensuite).
    http_mod.reset_http_policy()


def _max_legit_backoff():
    # Plus grand backoff légitime = celui de l'AVANT-dernière tentative.
    # Le backoff de la dernière tentative (2**(N-1)) ne doit JAMAIS survenir.
    return config.HTTP_BACKOFF_FACTOR * (2 ** (config.HTTP_MAX_RETRIES - 2))


def test_http_timeout_is_connect_read_tuple():
    assert isinstance(config.HTTP_TIMEOUT, tuple)
    assert len(config.HTTP_TIMEOUT) == 2
    connect, read = config.HTTP_TIMEOUT
    assert connect > 0 and read > 0
    # Connexion plus courte que lecture : échec rapide sur hôte mort
    assert connect <= read


def test_timeout_passed_through_to_session():
    client = HTTPClient()
    seen = {}

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            seen["timeout"] = timeout
            return _FakeResp(200)

        def close(self):
            pass

    client.session = FakeSession()
    client.get("https://example.test/x")
    assert seen["timeout"] == config.HTTP_TIMEOUT


def test_connect_timeout_bounded_no_final_backoff(captured_sleeps):
    client = HTTPClient()
    calls = {"n": 0}

    class DeadSession:
        def get(self, url, params=None, timeout=None):
            calls["n"] += 1
            raise requests.ConnectTimeout("host injoignable")

        def close(self):
            pass

    client.session = DeadSession()
    with pytest.raises(requests.ConnectTimeout):
        client.get("https://dead.test/x")

    # Tentatives bornées
    assert calls["n"] == config.HTTP_MAX_RETRIES
    # Un backoff entre chaque tentative SAUF après la dernière
    assert len(captured_sleeps) == config.HTTP_MAX_RETRIES - 1
    # Le backoff final (le plus long) n'a jamais été appliqué
    assert max(captured_sleeps) <= _max_legit_backoff()


def test_retryable_5xx_exhausts_then_raises_http_error(captured_sleeps):
    client = HTTPClient()
    calls = {"n": 0}

    class FlakySession:
        def get(self, url, params=None, timeout=None):
            calls["n"] += 1
            return _FakeResp(503)  # toujours en erreur retryable

        def close(self):
            pass

    client.session = FlakySession()
    with pytest.raises(requests.HTTPError):
        client.get("https://flaky.test/x")

    assert calls["n"] == config.HTTP_MAX_RETRIES
    # Aucun sleep ne dépasse le plus grand backoff légitime → pas de backoff
    # final (REQUEST_DELAY éventuel reste petit devant les backoffs).
    assert max(captured_sleeps) <= _max_legit_backoff()


def test_success_first_try_no_retry(captured_sleeps):
    client = HTTPClient()
    calls = {"n": 0}

    class OkSession:
        def get(self, url, params=None, timeout=None):
            calls["n"] += 1
            return _FakeResp(200)

        def close(self):
            pass

    client.session = OkSession()
    resp = client.get("https://ok.test/x")
    assert resp.status_code == 200
    assert calls["n"] == 1  # aucune nouvelle tentative
