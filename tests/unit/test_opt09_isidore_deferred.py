"""
OPT-09 : règle de reprise d'erreur du flux Isidore en topologie plate.

Avec la topologie plate, la source Isidore et ses cinq sous-sources
démarrent ensemble : toutes sont « engagées avant l'échec » d'une page, et la
règle d'OPT-08 seule (un consommateur arrivé au moins 5 s après l'échec
reprend la page) leur faisait partager une erreur passagère pour toute la
moisson. Le flux d'une moisson (``ScanRegistry.for_run``) fait donc lui-même
une reprise différée : il attend ``deferred_retry_after`` secondes et retente
la page une seule fois (une tentative réseau, sans cache) ; tous les
consommateurs attendent et partagent l'issue.

Sans aucun accès réseau : doublures ``FakeIsidore`` (tests d'OPT-08) et
``FakeIsidoreClient`` (tests d'OPT-07).
"""

import os
import sys
import threading
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
from connectors import isidore_scan  # noqa: E402
from connectors.cairn import CairnConnector  # noqa: E402
from connectors.isidore import IsidoreConnector  # noqa: E402
from connectors.isidore_scan import IsidoreScan, ScanRegistry, registry_for  # noqa: E402
from utils.http import CancelSignal, OperationCancelled  # noqa: E402
from test_opt08_isidore_scan import (  # noqa: E402
    FakeIsidore,
    _baseline_select,
    _uris,
    run_all,
    universe,
)

FAMILY = ["IsidoreConnector", "OpenEditionConnector", "CairnConnector",
          "PerseeConnector", "EruditConnector", "ThesesFrConnector"]


def _flow(registry):
    flows = registry.flows()
    assert len(flows) == 1
    return flows[0]


# === Départ simultané des six consommateurs (topologie plate) ===

def test_transient_error_with_simultaneous_start_is_recovered_for_all():
    """
    Échec de la page 1 (après les tentatives de la politique HTTP) alors que
    les six consommateurs ont démarré ensemble : la reprise différée du flux
    rend la page à tous. Avant OPT-09 : six sources en erreur, aucune notice.
    """
    records = universe(500)
    fake = FakeIsidore(records, window=200, faults={1: ["error"]})
    registry = ScanRegistry(deferred_retry_after=0.05)
    results = run_all(fake, max_results=200, workers=6, scan_registry=registry)
    for name in FAMILY:
        items, c = results[name]
        st = c.last_scan_status
        assert st.error is None, name
        # Plafond atteint (source directe) ou fenêtre du fournisseur
        # (sous-sources) : jamais une erreur.
        assert st.cap_reached or st.window_reached, name
    items, _ = results["IsidoreConnector"]
    assert _uris(items) == _baseline_select(records[:200], None, 200)
    # Page 1 en échec puis sa reprise différée (réussie), qui sert toute la
    # fenêtre (OPT-12 : replies=200 ; auparavant, la page 2 suivait) : une
    # seule requête utile, partagée par les six consommateurs.
    assert fake.calls == [1, 1]
    snap = _flow(registry).snapshot()
    assert snap["deferred_retries"] == 1 and snap["deferred_recovered"] == 1
    assert snap["error"] is None


def test_hard_outage_with_simultaneous_start_is_shared_after_one_deferred_retry():
    """Panne franche : une tentative, une reprise différée, erreur partagée."""
    fake = FakeIsidore(universe(500), window=200, faults={1: ["error"] * 10})
    registry = ScanRegistry(deferred_retry_after=0.05)
    t0 = time.monotonic()
    results = run_all(fake, max_results=50, workers=6, scan_registry=registry)
    elapsed = time.monotonic() - t0
    assert fake.calls == [1, 1]
    for name in FAMILY:
        items, c = results[name]
        assert items == [], name
        assert "ConnectionError" in c.last_scan_status.error, name
    assert elapsed >= 0.05
    snap = _flow(registry).snapshot()
    assert snap["deferred_retries"] == 1 and snap["deferred_recovered"] == 0


def test_outcome_does_not_depend_on_consumer_start_time():
    """
    Même issue pour tous, qu'ils démarrent avant l'échec, pendant la reprise
    différée ou juste après : une erreur passagère est rattrapée pour tous,
    une panne franche partagée par tous (aucune reprise par consommateur
    dans la foulée).
    """
    for faults, expect_ok in (({1: ["error"]}, True),
                              ({1: ["error"] * 10}, False)):
        fake = FakeIsidore(universe(500), window=200, faults=faults, latency=0.02)
        registry = ScanRegistry(deferred_retry_after=0.2)
        out = {}

        def consume(name, cls, delay):
            time.sleep(delay)
            c = cls(http_client=fake, scan_registry=registry)
            out[name] = (c.search("q", max_results=50), c.last_scan_status)

        threads = [
            threading.Thread(target=consume, args=("avant", IsidoreConnector, 0.0)),
            threading.Thread(target=consume, args=("pendant", CairnConnector, 0.1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        consume("apres", IsidoreConnector, 0.0)
        assert set(out) == {"avant", "pendant", "apres"}
        for name, (items, st) in out.items():
            if expect_ok:
                assert st.error is None, (faults, name)
                assert items, (faults, name)
            else:
                assert items == [] and st.error, (faults, name)
        if expect_ok:
            assert fake.calls[:2] == [1, 1] and fake.calls.count(1) == 2
        else:
            # Le consommateur « après » démarre moins de 5 s après l'échec
            # final : il le partage (pas de nouvelle série de tentatives).
            assert fake.calls == [1, 1]


# === Forme de la reprise différée ===

def test_deferred_retry_is_a_single_uncached_attempt():
    fake = FakeIsidore(universe(300), window=200, faults={1: ["error"]})
    scan = IsidoreScan("q", replies=100, window=200, max_records=3000,
                       deferred_retry_after=0.0)
    records = list(scan.iter_records(fake, consumer="A"))
    assert len(records) == 200
    assert fake.calls == [1, 1, 2]
    # Reprise différée : sans cache, une seule tentative de la politique, et
    # bornée par son propre budget (revue d'OPT-09).
    assert fake.kwargs_seen[1] == {
        "use_cache": False, "max_attempts": 1,
        "budget": config.ISIDORE_DEFERRED_RETRY_BUDGET_S}
    assert fake.kwargs_seen[0] == {} and fake.kwargs_seen[2] == {}


def test_deferred_retry_body_unreadable_twice_then_readable():
    """Corps illisible deux fois (tentative + reprise sans cache), lisible à
    la reprise différée : la page est acquise, pas d'erreur."""
    fake = FakeIsidore(universe(300), window=200, faults={2: ["html", "html"]})
    scan = IsidoreScan("q", replies=100, window=200, max_records=3000,
                       deferred_retry_after=0.0)
    assert len(list(scan.iter_records(fake, consumer="A"))) == 200
    assert fake.calls == [1, 2, 2, 2]
    assert scan.error is None and scan.deferred_recovered == 1


def test_definitive_client_error_is_not_retried():
    """Un refus définitif (4xx hors 429) n'appelle pas de reprise différée."""
    class _Refused(FakeIsidore):
        def get(self, url, params=None, **kwargs):
            with self._lock:
                self.calls.append(int((params or {}).get("page", 1)))
            err = requests.HTTPError("HTTP 400")
            err.response = type("R", (), {"status_code": 400})()
            raise err

    fake = _Refused(universe(10))
    scan = IsidoreScan("q", deferred_retry_after=0.0)
    assert list(scan.iter_records(fake, consumer="A")) == []
    assert fake.calls == [1]
    assert scan.end_reason == "error" and scan.deferred_retries == 0


def test_throttling_is_retried():
    """Un 429 (ou un 5xx) persistant reste transitoire : une reprise différée."""
    class _Throttled(FakeIsidore):
        def get(self, url, params=None, **kwargs):
            with self._lock:
                self.calls.append(int((params or {}).get("page", 1)))
            err = requests.HTTPError("HTTP 429")
            err.response = type("R", (), {"status_code": 429})()
            raise err

    fake = _Throttled(universe(10))
    scan = IsidoreScan("q", deferred_retry_after=0.0)
    assert list(scan.iter_records(fake, consumer="A")) == []
    assert fake.calls == [1, 1]


def test_cancellation_during_the_deferred_pause_releases_the_flow():
    """Annulation pendant l'attente : arrêt rapide, flux libéré sans être
    clos ni marqué en erreur ; aucune tentative supplémentaire."""
    fake = FakeIsidore(universe(300), window=200, faults={1: ["error"]})
    scan = IsidoreScan("q", deferred_retry_after=30.0)
    event = threading.Event()
    signal = CancelSignal(event)
    threading.Timer(0.2, event.set).start()
    t0 = time.monotonic()
    with pytest.raises(OperationCancelled):
        list(scan.iter_records(fake, consumer="A", cancel=signal))
    assert time.monotonic() - t0 < 2.0
    assert fake.calls == [1]
    assert not scan.finished and scan.error is None and not scan._fetching
    # Un consommateur ultérieur (autre exécution) relit la page normalement.
    assert len(list(scan.iter_records(fake, consumer="B"))) == 200


def test_no_deferred_retry_when_the_deadline_is_too_close():
    """Échéance plus proche que le délai de reprise : erreur finale tout de
    suite (les autres sources peuvent encore publier), pas d'annulation."""
    fake = FakeIsidore(universe(300), window=200, faults={1: ["error"]})
    scan = IsidoreScan("q", deferred_retry_after=5.0)
    signal = CancelSignal(threading.Event(), deadline=time.monotonic() + 3.0)
    t0 = time.monotonic()
    assert list(scan.iter_records(fake, consumer="A", cancel=signal)) == []
    assert time.monotonic() - t0 < 1.0
    assert fake.calls == [1]
    assert scan.end_reason == "error" and scan.deferred_retries == 0


def test_implicit_registry_keeps_the_opt08_rule():
    """Registre implicite (client HTTP, hors moisson) : pas de reprise
    différée, l'erreur est partagée comme avant (règle d'OPT-08)."""
    fake = FakeIsidore(universe(500), window=200, faults={1: ["error"]})
    results = run_all(fake, max_results=50)
    assert fake.calls == [1]
    for name in FAMILY:
        assert results[name][0] == [], name
    assert registry_for(fake).deferred_retry_after is None


def test_run_registry_reads_the_configured_delay(monkeypatch):
    monkeypatch.setattr(config, "ISIDORE_DEFERRED_RETRY_S", 1.5)
    assert ScanRegistry.for_run().deferred_retry_after == 1.5
    reg = ScanRegistry.for_run()
    scan, _ = reg.get_scan("q")
    assert scan.deferred_retry_after == 1.5
    monkeypatch.setattr(config, "ISIDORE_DEFERRED_RETRY_S", -1.0)
    assert ScanRegistry.for_run().deferred_retry_after is None
    scan, _ = ScanRegistry.for_run().get_scan("q")
    assert scan.deferred_retry_after is None


class _Clock:
    """Horloge simulée qui avance sur ``sleep`` (règle d'âge d'OPT-08)."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)

    def advance(self, seconds):
        self.t += seconds


def test_late_consumer_rule_still_applies_after_the_deferred_retry(monkeypatch):
    """Après l'échec de la reprise différée, un consommateur démarré au moins
    ``error_retry_after`` secondes plus tard reprend encore la page (client de
    longue durée, source démarrée tard)."""
    clock = _Clock()
    monkeypatch.setattr(isidore_scan, "time", clock)
    fake = FakeIsidore(universe(300), window=200, faults={1: ["error", "error"]})
    registry = ScanRegistry(deferred_retry_after=2.0)
    first = IsidoreConnector(http_client=fake, scan_registry=registry)
    assert first.search("q", max_results=50) == []
    assert fake.calls == [1, 1]
    # Tout de suite après : erreur partagée.
    assert CairnConnector(http_client=fake, scan_registry=registry).search(
        "q", max_results=5) == []
    assert fake.calls == [1, 1]
    clock.advance(5.1)
    late = IsidoreConnector(http_client=fake, scan_registry=registry)
    assert len(late.search("q", max_results=50)) == 50
    assert fake.calls == [1, 1, 1]
    assert _flow(registry).snapshot()["error_resumes"] == 1


# === Par le moteur (topologie plate, registre de moisson) ===

def test_harvest_recovers_a_transient_isidore_error_for_the_six_consumers(
        monkeypatch):
    """
    Moisson de la famille Isidore, 4 workers, page 1 en échec une fois : les
    six sources ont leurs notices (statut normal). Avant OPT-09 (barrière et
    registre sans reprise différée) : six sources en erreur.
    """
    from test_opt07_engine import FakeIsidoreClient, isidore_record
    from engine import orchestrator
    from engine.models import RunContext, SearchRequest

    monkeypatch.setattr(config, "ISIDORE_DEFERRED_RETRY_S", 0.05)
    hosts = ["journals.openedition.org", "www.cairn.info", "www.persee.fr",
             "id.erudit.org", "www.theses.fr", "www.example.org"]
    records = [isidore_record(i, host=hosts[i % len(hosts)]) for i in range(500)]
    state = {"failed": False}

    def fail(page):
        if page == 1 and not state["failed"]:
            state["failed"] = True
            raise requests.ConnectionError("connexion refusée (simulée)")
        return None

    client = FakeIsidoreClient(records, fail=fail)
    sources = ["isidore", "openedition", "cairn", "persee", "theses_fr", "erudit"]
    result = orchestrator.harvest(
        SearchRequest(query="q", sources=sources, max_per_source=20, workers=4),
        RunContext(http_client=client))
    statuses = {r.source: r.status for r in result.sources}
    assert all(s in ("complete", "partial") for s in statuses.values()), statuses
    assert all(r.reason != "network_error" for r in result.sources)
    assert all(r.item_count > 0 for r in result.sources), statuses
    # Page 1 en échec puis rendue par la reprise différée : deux requêtes
    # pour la page 1, pas une série par consommateur.
    assert client.pages[:2] == [1, 1] and client.pages.count(1) == 2
    assert [r.source for r in result.sources] == sources


# === Revue d'OPT-09 : délai dépassé, budget et échéance ===

class _AlwaysFailing(FakeIsidore):
    """Doublure dont chaque requête lève l'exception fournie."""

    def __init__(self, exc_factory, **kw):
        super().__init__(universe(10), **kw)
        self.exc_factory = exc_factory

    def get(self, url, params=None, **kwargs):
        with self._lock:
            self.calls.append(int((params or {}).get("page", 1)))
            self.kwargs_seen.append(kwargs)
        raise self.exc_factory()


def _read_timeout_in_body():
    """Délai de lecture survenu pendant le corps : requests lève une
    ConnectionError qui enveloppe le ReadTimeoutError de urllib3."""
    from urllib3.exceptions import ReadTimeoutError
    return requests.ConnectionError(
        ReadTimeoutError(None, "https://api.isidore.science", "Read timed out."))


@pytest.mark.parametrize("exc_factory", [
    lambda: requests.ConnectTimeout("délai de connexion dépassé (simulé)"),
    lambda: requests.ReadTimeout("délai de lecture dépassé (simulé)"),
    lambda: requests.Timeout("Budget de requête épuisé (120 s) (simulé)"),
    _read_timeout_in_body,
], ids=["connexion", "lecture", "budget", "lecture-du-corps"])
def test_no_deferred_retry_after_a_timeout(exc_factory):
    """
    Service muet ou injoignable : les tentatives de la politique commune ont
    déjà attendu chacune un délai complet ; pas de reprise différée (avant la
    revue : +5 s et un délai réseau complet de plus, pour la même issue).
    """
    fake = _AlwaysFailing(exc_factory)
    scan = IsidoreScan("q", deferred_retry_after=0.0)
    assert list(scan.iter_records(fake, consumer="A")) == []
    assert fake.calls == [1]
    assert scan.end_reason == "error" and scan.deferred_retries == 0


def test_fast_network_failure_is_still_retried():
    """Une connexion refusée (échec rapide) garde sa reprise différée."""
    fake = _AlwaysFailing(lambda: requests.ConnectionError("connexion refusée"))
    scan = IsidoreScan("q", deferred_retry_after=0.0)
    assert list(scan.iter_records(fake, consumer="A")) == []
    assert fake.calls == [1, 1]
    assert scan.deferred_retries == 1


def test_no_deferred_retry_when_the_retry_would_cross_the_deadline():
    """
    Reste compris entre délai + 1 s et délai + durée de la tentative : avant
    la revue, la reprise partait et la moisson entière était annulée. La
    marge exigée couvre désormais l'attente, le budget de la tentative et
    1 s.
    """
    fake = FakeIsidore(universe(300), window=200, faults={1: ["error"]})
    scan = IsidoreScan("q", deferred_retry_after=0.2, deferred_retry_budget=15.0)
    signal = CancelSignal(threading.Event(), deadline=time.monotonic() + 1.7)
    t0 = time.monotonic()
    assert list(scan.iter_records(fake, consumer="A", cancel=signal)) == []
    assert time.monotonic() - t0 < 0.5
    assert fake.calls == [1]
    assert scan.end_reason == "error" and scan.deferred_retries == 0


def test_deferred_budget_is_trimmed_to_finish_before_the_deadline():
    scan = IsidoreScan("q", deferred_retry_after=0.0, deferred_retry_budget=15.0)
    assert scan._deferred_budget(None) == 15.0
    far = CancelSignal(threading.Event(), deadline=time.monotonic() + 100.0)
    assert scan._deferred_budget(far) == 15.0
    near = CancelSignal(threading.Event(), deadline=time.monotonic() + 5.0)
    assert 3.5 < scan._deferred_budget(near) <= 4.0
    gone = CancelSignal(threading.Event(), deadline=time.monotonic() + 0.5)
    assert scan._deferred_budget(gone) is None


def test_scan_reads_the_configured_budget(monkeypatch):
    monkeypatch.setattr(config, "ISIDORE_DEFERRED_RETRY_BUDGET_S", 2.5)
    scan, _ = ScanRegistry.for_run().get_scan("q")
    assert scan.deferred_retry_budget == 2.5


def _hal_isidore_session(isidore_retry):
    """
    Session simulée : HAL répond une page complète ; la première requête
    Isidore reçoit un 503, les suivantes sont confiées à ``isidore_retry``
    (appelée avec le délai transmis par la politique HTTP).
    """
    from test_opt07_engine import FakeResponse, hal_page

    state = {"isidore": 0, "timeouts": []}

    class _Session:
        headers: dict = {}

        def get(self, url, params=None, headers=None, timeout=None, **kw):
            if "archives-ouvertes" in url:
                return FakeResponse(payload=hal_page(0, 20, 20, "*"))
            state["isidore"] += 1
            state["timeouts"].append(timeout)
            if state["isidore"] == 1:
                return FakeResponse(status=503, payload={})
            return isidore_retry(timeout)

        def mount(self, *a, **k):
            pass

        def close(self):
            pass

    return _Session(), state


def _harvest_hal_isidore(session, deadline_in):
    from engine import orchestrator
    from engine.models import RunContext, SearchRequest
    from utils.http import HTTPClient, reset_http_policy

    reset_http_policy()
    client = HTTPClient(use_cache=False)
    client.session = session
    ctx = RunContext(http_client=client, deadline=time.monotonic() + deadline_in)
    return orchestrator.harvest(
        SearchRequest(query="q", sources=["hal", "isidore"], max_per_source=50,
                      workers=2), ctx)


def test_harvest_publishes_when_the_deferred_retry_would_cross_the_deadline(
        monkeypatch):
    """
    Scénario de la revue : échéance à délai + 1,5 s, page 1 d'Isidore en 503,
    une reprise qui prendrait 2 s. Avant : reprise lancée, échéance franchie,
    moisson ANNULÉE (HAL complet perdu). Après : pas de reprise, la moisson
    publie HAL complet et Isidore en erreur.
    """
    monkeypatch.setattr(config, "ISIDORE_DEFERRED_RETRY_S", 0.3)
    monkeypatch.setattr("utils.http.HTTP_MAX_RETRIES", 1)

    def slow_retry(timeout):
        time.sleep(2.0)
        raise AssertionError("aucune reprise différée attendue")

    session, state = _hal_isidore_session(slow_retry)
    t0 = time.monotonic()
    result = _harvest_hal_isidore(session, deadline_in=1.8)
    assert time.monotonic() - t0 < 1.0
    statuses = {r.source: (r.status, r.item_count) for r in result.sources}
    assert statuses["hal"] == ("complete", 20)
    assert statuses["isidore"][0] == "error"
    assert state["isidore"] == 1


def test_harvest_deferred_retry_is_cut_by_its_budget_not_by_the_deadline(
        monkeypatch):
    """
    Assez de temps pour la reprise différée : sa tentative est bornée par son
    budget (0,5 s ici) ; un service qui ne répond pas dans ce budget donne
    une erreur de page, jamais l'annulation de la moisson (avant : délai
    borné par la seule échéance, moisson annulée).
    """
    monkeypatch.setattr(config, "ISIDORE_DEFERRED_RETRY_S", 0.1)
    monkeypatch.setattr(config, "ISIDORE_DEFERRED_RETRY_BUDGET_S", 0.5)
    monkeypatch.setattr("utils.http.HTTP_MAX_RETRIES", 1)

    def hanging_retry(timeout):
        read = timeout[1] if isinstance(timeout, (tuple, list)) else timeout
        time.sleep(min(float(read), 5.0))
        raise requests.ReadTimeout("délai de lecture dépassé (simulé)")

    session, state = _hal_isidore_session(hanging_retry)
    t0 = time.monotonic()
    result = _harvest_hal_isidore(session, deadline_in=5.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, elapsed
    statuses = {r.source: (r.status, r.item_count) for r in result.sources}
    assert statuses["hal"] == ("complete", 20)
    assert statuses["isidore"][0] == "error"
    # Première requête puis la reprise différée, bornée par son budget.
    assert state["isidore"] == 2
    assert max(state["timeouts"][1]) <= 0.5 + 1e-6
