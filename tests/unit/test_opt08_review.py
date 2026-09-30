"""
Tests des corrections de revue d'OPT-08 (parcours Isidore mutualisé).

Sans aucun accès réseau (doublure ``FakeIsidore`` du module de tests
d'OPT-08) et avec une horloge simulée pour l'âge des erreurs :

- une erreur Isidore passagère pendant la source directe n'est plus gardée
  pour toute l'exécution : un consommateur arrivé après l'échec reprend la
  page qui a échoué (pages acquises conservées), au-delà du délai
  ``ScanRegistry.error_retry_after_s`` ;
- pendant une panne franche, les consommateurs qui se suivent de près ou qui
  attendent la même page partagent l'erreur (pas de 6 × 3 tentatives) ;
- un client de longue durée ne garde pas un flux en erreur pendant une heure ;
- la borne ISIDORE_SCAN_MAX_RECORDS franchie sur la dernière page (partielle
  ou atteignant le total annoncé) est signalée « borne », sous-source
  incomplète, et non « épuisé ».
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
from connectors import isidore_scan  # noqa: E402
from connectors.cairn import CairnConnector  # noqa: E402
from connectors.isidore import IsidoreConnector  # noqa: E402
from connectors.isidore_scan import IsidoreScan, ScanRegistry, registry_for  # noqa: E402
from test_opt08_isidore_scan import (  # noqa: E402,F401
    PROXIES,
    FakeIsidore,
    _baseline_select,
    _uris,
    pages_of_100,   # fixture : pages de 100 (scénarios écrits sur deux pages)
    run_all,
    universe,
)


class _Clock:
    """Horloge monotone simulée (seul ``time.monotonic`` est utilisé)."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(isidore_scan, "time", c)
    # Délai par défaut imposé : le test ne dépend pas d'une surcharge locale.
    monkeypatch.setattr(ScanRegistry, "error_retry_after_s", 5.0)
    return c


Q = "discours numérique"  # requête de run_all


def _flow(http):
    flows = registry_for(http).flows()
    assert len(flows) == 1
    return flows[0]


# === Constat 1 : erreur passagère pendant la source directe ===

@pytest.mark.parametrize("fault", [["error"], ["html", "html"]])
def test_transient_error_during_direct_source_is_retried_by_later_proxies(clock, fault,
                                                                         pages_of_100):
    """
    Scénario de la revue : Isidore est en panne pendant la source directe
    (groupe des natives), revenu quand le groupe des proxies démarre. Avant
    correction : les cinq sous-sources renvoyaient [] sans aucune requête.
    """
    records = universe(500)
    fake = FakeIsidore(records, window=200, faults={1: list(fault)})
    direct = IsidoreConnector(http_client=fake)
    assert direct.search("q", max_results=200) == []
    assert direct.last_scan_status.error
    calls_after_direct = len(fake.calls)

    clock.advance(30)  # autres natives (HAL, CORE…) : Isidore est revenu
    for cls, marker, label in PROXIES:
        c = cls(http_client=fake)
        items = c.search("q", max_results=200)
        st = c.last_scan_status
        assert st.error is None, label
        assert _uris(items) == _baseline_select(records[:200], marker, 200), label
        assert st.window_reached and st.incomplete, label
    # Une seule reprise de la page 1, puis la page 2 : toutes deux partagées.
    assert fake.calls[calls_after_direct:] == [1, 2]
    assert _flow(fake).snapshot()["error_resumes"] == 1


def test_hard_outage_is_shared_by_consumers_following_closely(clock):
    """Panne franche : pas une série de tentatives par consommateur."""
    fake = FakeIsidore(universe(500), window=200, faults={1: ["error"] * 10})
    results = run_all(fake, max_results=50)
    assert fake.calls == [1]
    for name, (items, c) in results.items():
        assert items == [] and "ConnectionError" in c.last_scan_status.error, name

    clock.advance(4.9)  # erreur trop récente : partagée
    assert CairnConnector(http_client=fake).search(Q, max_results=5) == []
    assert fake.calls == [1]

    clock.advance(0.2)  # délai écoulé : une reprise, qui échoue encore
    c = CairnConnector(http_client=fake)
    assert c.search(Q, max_results=5) == []
    assert fake.calls == [1, 1] and c.last_scan_status.error
    # Le consommateur suivant, dans la foulée, partage la nouvelle erreur.
    assert CairnConnector(http_client=fake).search(Q, max_results=5) == []
    assert fake.calls == [1, 1]


def test_consumer_engaged_before_the_error_shares_it_even_without_delay():
    """
    Délai nul : seul compte le moment où le consommateur a commencé sa
    lecture. Engagé avant l'échec, il le partage ; arrivé après, il reprend.
    """
    fake = FakeIsidore(universe(500), window=0, faults={2: ["error"]})
    scan = IsidoreScan("q", replies=100, window=0, max_records=3000,
                       error_retry_after=0.0)
    gen_a = scan.iter_records(fake, consumer="A")
    first_page = [next(gen_a) for _ in range(100)]
    assert len(first_page) == 100 and fake.calls == [1]

    b = list(scan.iter_records(fake, consumer="B"))  # déclenche l'échec page 2
    assert len(b) == 100 and fake.calls == [1, 2]
    assert scan.end_reason == "error"

    assert list(gen_a) == []  # A était engagé avant l'échec : pas de reprise
    assert fake.calls == [1, 2]

    c = list(scan.iter_records(fake, consumer="C"))  # arrivé après : reprend
    assert len(c) == 500
    assert fake.calls == [1, 2, 2, 3, 4, 5]  # page 1 conservée, page 2 reprise
    snap = scan.snapshot()
    assert snap["error"] is None and snap["end_reason"] == "exhausted"
    assert snap["error_resumes"] == 1 and snap["pages_downloaded"] == 5


def test_threaded_consumers_waiting_on_the_failed_page_share_the_error(monkeypatch):
    """Six consommateurs qui attendent la même page : une seule requête."""
    monkeypatch.setattr(ScanRegistry, "error_retry_after_s", 0.0)
    fake = FakeIsidore(universe(500), window=200, latency=0.5,
                       faults={1: ["error"]})
    results = run_all(fake, max_results=50, workers=6)
    assert fake.calls == [1]
    for name, (items, c) in results.items():
        assert items == [] and c.last_scan_status.error, name


def test_resume_keeps_the_pages_already_acquired(clock, pages_of_100):
    records = universe(500)
    fake = FakeIsidore(records, window=200, faults={2: ["error"]})
    direct = IsidoreConnector(http_client=fake)
    items = direct.search("q", max_results=200)
    assert len(items) == 100 and direct.last_scan_status.error
    assert fake.calls == [1, 2]

    clock.advance(10)
    cairn = CairnConnector(http_client=fake)
    got = cairn.search("q", max_results=200)
    assert fake.calls == [1, 2, 2]  # seule la page en erreur est redemandée
    assert _uris(got) == _baseline_select(records[:200], "cairn.info", 200)
    assert cairn.last_scan_status.error is None
    assert cairn.last_scan_status.pages_downloaded == 1


# === Constat 2 : client de longue durée ===

def test_long_lived_client_does_not_keep_an_error_for_an_hour(clock):
    """
    Client partagé de longue durée (registre attaché par défaut) : avant
    correction, le flux en erreur était réutilisé jusqu'à ``max_age_s``
    (une heure) sans nouvelle requête.
    """
    fake = FakeIsidore(universe(500), window=200, faults={1: ["error"]})
    assert IsidoreConnector(http_client=fake).search("q", max_results=50) == []
    assert fake.calls == [1]

    clock.advance(1)  # dans le délai : l'erreur est partagée
    assert IsidoreConnector(http_client=fake).search("q", max_results=50) == []
    assert fake.calls == [1]

    clock.advance(60)  # appel ultérieur : Isidore répond de nouveau
    c = IsidoreConnector(http_client=fake)
    items = c.search("q", max_results=50)
    assert len(items) == 50 and fake.calls == [1, 1]
    assert c.last_scan_status.error is None and c.last_scan_status.shared_flow


# === Constat 3 : borne franchie sur la dernière page ===

@pytest.mark.parametrize("size,dropped", [
    (300, 50),   # page 3 pleine, total annoncé atteint
    (280, 30),   # page 3 partielle
])
def test_bound_crossed_on_the_last_page_is_reported_as_bound(monkeypatch, caplog,
                                                              size, dropped, pages_of_100):
    monkeypatch.setattr(config, "ISIDORE_RESULT_WINDOW", 0, raising=False)
    monkeypatch.setattr(config, "ISIDORE_SCAN_MAX_RECORDS", 250, raising=False)
    fake = FakeIsidore(universe(size), window=0)
    c = IsidoreConnector(http_client=fake)
    with caplog.at_level(logging.WARNING):
        items = c.search("q", max_results=1000)
    st = c.last_scan_status
    assert fake.calls == [1, 2, 3]
    assert len(items) == 250 and st.flow_records == 250
    assert st.end_reason == "bound" and st.bound_reached and not st.exhausted
    assert st.incomplete
    assert _flow(fake).snapshot()["truncated"] == dropped
    assert any("ISIDORE_SCAN_MAX_RECORDS" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("size,bound", [
    (250, 250),  # dernière page partielle, rien d'écarté
    (300, 300),  # dernière page pleine, total atteint, rien d'écarté
])
def test_bound_equal_to_the_result_set_is_exhausted_not_incomplete(monkeypatch, size, bound):
    monkeypatch.setattr(config, "ISIDORE_RESULT_WINDOW", 0, raising=False)
    monkeypatch.setattr(config, "ISIDORE_SCAN_MAX_RECORDS", bound, raising=False)
    fake = FakeIsidore(universe(size), window=0)
    c = IsidoreConnector(http_client=fake)
    items = c.search("q", max_results=1000)
    st = c.last_scan_status
    assert len(items) == size
    assert st.end_reason == "exhausted" and st.exhausted and not st.incomplete
    assert _flow(fake).snapshot()["truncated"] == 0
