"""
OPT-09 : topologie plate (constat C11).

Un seul pool sur toutes les sources, sans barrière « natives puis proxies » :
une sous-source d'Isidore démarre dès qu'un worker est libre, sans attendre
la fin des sources natives (CORE, 6 s entre deux pages). L'ordre de démarrage
place les sources longues et l'ouvreur du flux Isidore d'abord, les autres
consommateurs du flux ensuite ; le résultat suit l'ordre demandé. Aucun accès
réseau (exécutant de source remplacé).
"""

import threading
import time

import pytest

import config
from connectors.registry import (
    FRANCOPHONE_SOURCES, INTERNATIONAL_SOURCES, plan_order, resolve_sources,
    shared_flow_of,
)
from engine import orchestrator
from engine.models import RunContext, SearchRequest


class _NullClient:
    cache_enabled = False
    cancel_signal = None

    def get(self, *a, **k):  # pragma: no cover - jamais appelé
        raise AssertionError("aucun accès réseau attendu")

    def close(self):
        pass


def _harvest(sources, runner, monkeypatch, workers=4, **kw):
    monkeypatch.setattr(orchestrator, "run_source", runner)
    return orchestrator.harvest(
        SearchRequest(query="q", sources=list(sources), workers=workers, **kw),
        RunContext(http_client=_NullClient()))


# === Plan ===

class TestPlanOrder:
    def test_francophone_keeps_the_historical_sequential_order(self):
        assert plan_order(resolve_sources(["francophone"])) == list(FRANCOPHONE_SOURCES)

    def test_all_group_long_sources_first_flow_followers_last(self):
        order = plan_order(resolve_sources(["all"]))
        assert order[:2] == ["core", "semantic_scholar"]
        assert order.index("isidore") < order.index("crossref")
        followers = ["openedition", "cairn", "persee", "theses_fr", "erudit"]
        assert order[-5:] == followers
        assert sorted(order) == sorted(resolve_sources(["all"]))

    def test_first_consumer_opens_the_flow_when_isidore_is_absent(self):
        assert plan_order(["cairn", "persee", "hal"]) == ["cairn", "hal", "persee"]
        assert plan_order(["openedition", "hal"]) == ["openedition", "hal"]

    def test_ties_keep_the_requested_order(self):
        assert plan_order(["openalex", "crossref"]) == ["openalex", "crossref"]
        assert plan_order(INTERNATIONAL_SOURCES) == [
            "core", "semantic_scholar", "crossref", "openalex"]

    def test_shared_flow_membership(self):
        assert shared_flow_of("isidore") == "isidore"
        assert shared_flow_of("Persee") == "isidore"
        assert shared_flow_of("hal") is None

    def test_single_group(self):
        groups = orchestrator.plan_schedule(resolve_sources(["all"]))
        assert len(groups) == 1 and len(groups[0]) == 11
        assert orchestrator.plan_schedule([]) == []


class TestPoolBounds:
    def test_pool_size_bounded_by_sources_and_ceiling(self, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_MAX_WORKERS", 8)
        assert orchestrator.pool_size(4, 7) == 4
        assert orchestrator.pool_size(4, 2) == 2
        assert orchestrator.pool_size(50, 11) == 8
        assert orchestrator.pool_size(0, 3) == 1

    def test_search_request_clamps_workers_without_error(self, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_MAX_WORKERS", 6)
        assert SearchRequest(query="q", sources=["hal"], workers=100).workers == 6
        assert SearchRequest(query="q", sources=["hal"], workers=0).workers == 1

    def test_never_more_threads_than_workers(self, monkeypatch):
        state = {"active": 0, "max": 0}
        lock = threading.Lock()

        def runner(name, *a, **k):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.03)
            with lock:
                state["active"] -= 1
            return []

        _harvest(resolve_sources(["all"]), runner, monkeypatch, workers=3)
        assert state["max"] == 3


# === Pas de barrière ===

class TestNoBarrier:
    def test_proxy_starts_while_a_native_source_is_still_running(self, monkeypatch):
        """
        La sous-source Cairn démarre pendant que CORE (source native lente)
        tourne encore. Avec la barrière « natives puis proxies », Cairn
        attendait la fin de CORE : ce test échouait (délai dépassé).
        """
        cairn_started = threading.Event()
        seen = {}

        def runner(name, *a, source_key=None, **k):
            if source_key == "core":
                seen["cairn_during_core"] = cairn_started.wait(timeout=2.0)
            if source_key == "cairn":
                cairn_started.set()
            return []

        _harvest(["core", "cairn"], runner, monkeypatch, workers=2)
        assert seen["cairn_during_core"] is True

    def test_duration_is_the_longest_source_not_the_sum(self, monkeypatch):
        delays = {"core": 0.6, "hal": 0.1, "isidore": 0.2, "openedition": 0.3,
                  "cairn": 0.3}

        def runner(name, *a, source_key=None, **k):
            time.sleep(delays[source_key])
            return []

        t0 = time.monotonic()
        _harvest(list(delays), runner, monkeypatch, workers=5)
        elapsed = time.monotonic() - t0
        # Avec la barrière : max(natives) + max(proxies) = 0,9 s ; à plat :
        # 0,6 s (la plus longue), au surcoût près.
        assert elapsed < 0.8, elapsed

    def test_result_order_is_the_requested_order(self, monkeypatch):
        started = []

        def runner(name, *a, source_key=None, **k):
            started.append(source_key)
            return [{"itemType": "book", "title": f"T {source_key}",
                     "creators": [], "date": "2020", "extra": ""}]

        requested = ["cairn", "hal", "crossref", "core", "isidore"]
        result = _harvest(requested, runner, monkeypatch, workers=1, dedup=False)
        assert started == plan_order(requested)
        assert started != requested
        assert [r.source for r in result.sources] == requested

    def test_cancellation_stops_the_flat_pool(self, monkeypatch):
        """Une source annulée annule l'exécution ; aucune source non démarrée
        ne part ensuite (pool unique, file restante abandonnée)."""
        from engine.models import SourceResult
        from utils.http import OperationCancelled
        ran = []

        def runner(name, *a, source_key=None, run_context=None, **k):
            ran.append(source_key)
            if source_key == "core":
                return SourceResult("core", "CORE", status="cancelled",
                                    reason="cancelled")
            time.sleep(0.05)
            return []

        with pytest.raises(OperationCancelled):
            _harvest(resolve_sources(["all"]), runner, monkeypatch, workers=1)
        assert ran == ["core"]
