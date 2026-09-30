"""
Tests OPT-07 — annulation et échéance propagées (constat C31).

Aucun accès réseau (sessions et API simulées). Les attentes réelles sont
courtes (quelques dixièmes de seconde) : elles mesurent qu'une attente de
politesse ou de réessai est bien interrompue, et le délai entre l'annulation
et l'arrêt effectif.

Couvre :
- ``CancelSignal`` : attente interruptible faite de ``time.sleep`` (comptée
  par le banc), échéance ;
- ``policy_get`` : aucune requête après annulation ; attente de réessai et
  créneau de politesse interrompus ; délai borné par l'échéance ;
- connecteurs : aucune nouvelle page après la détection (pagination HAL,
  flux Isidore partagé) ; OAI vérifié entre deux enregistrements ;
- moteur : annulation → ``OperationCancelled`` avec résultat partiel, aucun
  corpus publié, payloads bruts supprimés, sessions fermées ; délai
  d'arrêt mesuré ;
- ``resolve_dois`` : ``stop_event`` branché jusqu'aux attentes réseau
  (notices « non traité », jamais « sans candidat »).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from engine.models import RunContext, SearchRequest  # noqa: E402
from engine import orchestrator  # noqa: E402
from utils import http as uhttp  # noqa: E402
from utils.http import (  # noqa: E402
    CancelSignal, HTTPClient, OperationCancelled, policy_get, reset_http_policy,
)


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {"Content-Type": "application/json"}
        self.from_cache = False
        self.url = ""
        self.reason = ""
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("corps vide")
        return self._payload


class FakeSession:
    def __init__(self, handler, latency: float = 0.0):
        self.handler = handler
        self.latency = latency
        self.calls: List[dict] = []
        self.headers: Dict[str, str] = {}
        self.closed = False

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append(dict(params or {}))
        if self.latency:
            time.sleep(self.latency)
        return self.handler(url, dict(params or {}), len(self.calls))

    def mount(self, *a, **k):
        pass

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fresh_policy():
    reset_http_policy()
    yield
    reset_http_policy()


def _set_after(signal_or_event, delay: float) -> threading.Thread:
    """Déclenche l'annulation après ``delay`` secondes (fil de fond)."""
    def go():
        time.sleep(delay)
        signal_or_event.set()
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


# === Signal d'annulation ===

class TestCancelSignal:
    def test_wait_is_sliced_sleep_and_interruptible(self, monkeypatch):
        slept = []
        sig = CancelSignal()
        monkeypatch.setattr(uhttp.time, "sleep",
                            lambda s: (slept.append(s), sig.set() if len(slept) == 3 else None))
        assert sig.wait(10.0) is True
        assert len(slept) == 3 and all(s <= uhttp.CANCEL_WAIT_SLICE for s in slept)

    def test_wait_without_cancel_sums_to_requested(self, monkeypatch):
        slept = []
        monkeypatch.setattr(uhttp.time, "sleep", lambda s: slept.append(s))
        assert CancelSignal().wait(1.0) is False
        assert sum(slept) == pytest.approx(1.0)

    def test_deadline(self):
        sig = CancelSignal(deadline=time.monotonic() + 0.05)
        t0 = time.monotonic()
        assert sig.wait(5.0) is True
        assert time.monotonic() - t0 < 1.0
        assert sig.reason() == "deadline"
        with pytest.raises(OperationCancelled) as exc:
            sig.check()
        assert exc.value.reason == "deadline"

    def test_accepts_event_like_objects(self):
        class _Flag:
            def __init__(self):
                self.v = False

            def is_set(self):
                return self.v
        flag = _Flag()
        sig = CancelSignal.coerce(flag)
        assert not sig.is_set()
        flag.v = True
        assert sig.is_set() and sig.reason() == "cancelled"

    def test_operation_cancelled_crosses_except_exception(self):
        def swallow():
            try:
                raise OperationCancelled()
            except Exception:  # noqa: BLE001 - ce que font les connecteurs
                return "avalée"
        with pytest.raises(OperationCancelled):
            swallow()


# === Politique HTTP ===

class TestPolicyGet:
    def test_no_request_after_cancellation(self):
        sess = FakeSession(lambda *a: FakeResponse(payload={}))
        sig = CancelSignal()
        sig.set()
        with pytest.raises(OperationCancelled):
            policy_get(sess, "https://api.test/x", cancel=sig)
        assert sess.calls == []

    def test_retry_wait_is_interrupted(self):
        sess = FakeSession(lambda *a: FakeResponse(status=503, payload={}))
        sig = CancelSignal()
        _set_after(sig, 0.2)
        t0 = time.monotonic()
        with pytest.raises(OperationCancelled):
            policy_get(sess, "https://api.test/retry", cancel=sig,
                       backoff_factor=5.0, min_interval=0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0                # au lieu de 5 s de backoff
        assert len(sess.calls) == 1          # aucune nouvelle tentative

    def test_politeness_wait_is_interrupted(self):
        sess = FakeSession(lambda *a: FakeResponse(payload={"ok": 1}))
        sig = CancelSignal()
        policy_get(sess, "https://polite.test/a", cancel=sig, min_interval=5.0)
        _set_after(sig, 0.2)
        t0 = time.monotonic()
        with pytest.raises(OperationCancelled):
            policy_get(sess, "https://polite.test/b", cancel=sig, min_interval=5.0)
        assert time.monotonic() - t0 < 1.0
        assert len(sess.calls) == 1
        # Le créneau abandonné est rendu : la requête suivante (non annulée)
        # n'attend pas 10 s.
        reset_http_policy()

    def test_slot_beyond_deadline_is_not_taken(self):
        sess = FakeSession(lambda *a: FakeResponse(payload={}))
        sig = CancelSignal()
        policy_get(sess, "https://late.test/a", cancel=sig, min_interval=5.0)
        late = CancelSignal(deadline=time.monotonic() + 0.5)
        t0 = time.monotonic()
        with pytest.raises(OperationCancelled) as exc:
            policy_get(sess, "https://late.test/b", cancel=late, min_interval=5.0)
        assert exc.value.reason == "deadline"
        assert time.monotonic() - t0 < 0.3   # sans attendre l'échéance
        assert len(sess.calls) == 1

    def test_timeout_bounded_by_deadline(self):
        seen = {}

        class _S(FakeSession):
            def get(self, url, params=None, headers=None, timeout=None, **kw):
                seen["timeout"] = timeout
                return FakeResponse(payload={})
        sig = CancelSignal(deadline=time.monotonic() + 2.0)
        policy_get(_S(None), "https://t.test/x", cancel=sig, min_interval=0)
        assert max(seen["timeout"]) <= 2.0

    def test_http_client_carries_the_run_signal(self):
        client = HTTPClient(use_cache=False)
        client.session = FakeSession(lambda *a: FakeResponse(payload={}))
        sig = CancelSignal()
        view = client.with_cancel(sig)
        assert view.session is client.session and client.cancel_signal is None
        sig.set()
        with pytest.raises(OperationCancelled):
            view.get("https://v.test/x")
        client.get("https://v.test/y", min_interval=0)  # le client partagé n'est pas annulé
        view.close()
        assert not client.session.closed      # une vue ne ferme pas la session


# === Connecteurs et moteur ===

def hal_page(start, n, cursor):
    return {"response": {"numFound": 10000, "docs": [
        {"docid": start + k, "halId_s": f"hal-{start + k:05d}",
         "title_s": [f"Titre {start + k}"], "producedDate_s": "2020",
         "docType_s": "ART", "authFullName_s": [f"Nom{start + k} Jean"]}
        for k in range(n)]}, "nextCursorMark": cursor}


class TestConnectorsStopBetweenPages:
    def test_hal_no_new_page_after_detection(self, tmp_path):
        client = HTTPClient(use_cache=False)
        ctx = RunContext(http_client=client, output_dir=tmp_path)

        def handler(url, params, n):
            if n == 2:
                ctx.cancel()          # annulation pendant la 2e page
            return FakeResponse(payload=hal_page((n - 1) * 1000, 1000, f"c{n + 1}"))
        client.session = FakeSession(handler)
        request = SearchRequest(query="q", sources=["hal"], max_per_source=10000)
        with pytest.raises(OperationCancelled) as exc:
            orchestrator.harvest(request, ctx)
        assert len(client.session.calls) == 2   # aucune 3e page
        partial = exc.value.result
        assert partial is not None and partial.sources[0].status == "cancelled"
        assert list(tmp_path.glob("corpus_*")) == []
        raw = tmp_path / "raw"
        assert not raw.exists() or list(raw.iterdir()) == []

    def test_isidore_flow_stops_between_pages(self):
        from connectors.isidore import IsidoreConnector
        from connectors.isidore_scan import ScanRegistry

        pages = []
        ctx = RunContext()

        class _Client:
            def get(self, url, params=None, **kw):
                page = int(params["page"])
                pages.append(page)
                if page == 1:
                    ctx.cancel()
                recs = [{"@uri": f"u{page}.{i}", "isidore": {
                    "title": [{"@xml:lang": "fr", "$": f"T{page}.{i}"}],
                    "url": f"https://x.org/{page}/{i}",
                    "date": {"year": "2020"}}} for i in range(20)]
                return FakeResponse(payload={"response": {"replies": {
                    "meta": {"@items": "5000"},
                    "content": {"reply": recs},
                    "page": {"@current": str(page), "@next": str(page + 1)}}}})

        c = IsidoreConnector(http_client=_Client(), run_context=ctx,
                             scan_registry=ScanRegistry(replies=20, window=0))
        with pytest.raises(OperationCancelled):
            c.search("q", max_results=1000)
        assert pages == [1]

    def test_oai_enrichment_checked_between_records(self, monkeypatch):
        from connectors.openedition import OpenEditionConnector
        ctx = RunContext()
        calls = []

        class _Sickle:
            def GetRecord(self, identifier, metadataPrefix):
                calls.append(identifier)
                ctx.cancel()
                raise RuntimeError("pas de réseau ici")

        c = OpenEditionConnector(http_client=HTTPClient(use_cache=False),
                                 run_context=ctx, enrich_oai=True)
        c._sickle = _Sickle()
        monkeypatch.setattr("connectors.openedition.polite_wait",
                            lambda *a, **k: 0.0)
        items = [{"url": f"https://journals.openedition.org/rev/{i}",
                  "extra": "Sources: OpenEdition"} for i in range(5)]
        with pytest.raises(OperationCancelled):
            c._enrich_with_oai(items)
        assert len(calls) == 1

    def test_parallel_sources_join_before_raising(self, monkeypatch, tmp_path):
        """workers > 1 : les sources en cours voient l'annulation et le groupe
        attend leur fin réelle ; aucune ne continue après le retour."""
        running = []
        finished = []

        def slow_source(name, cls, query, mps, output_dir, run_id, http_client,
                        run_context=None, **kw):
            running.append(name)
            while not run_context.cancelled():
                time.sleep(0.01)
            finished.append(name)
            raise OperationCancelled(run_context.cancel_reason())

        monkeypatch.setattr(orchestrator, "run_source", slow_source)
        ctx = RunContext(output_dir=tmp_path,
                         http_client=HTTPClient(use_cache=False))
        _set_after(ctx.signal, 0.2)
        t0 = time.monotonic()
        with pytest.raises(OperationCancelled):
            orchestrator.harvest(SearchRequest(query="q", sources=["hal", "crossref"],
                                               workers=2), ctx)
        stop = time.monotonic() - t0
        assert sorted(finished) == sorted(running) == ["CrossRef", "HAL"]
        assert stop < 1.0

    def test_deadline_stops_a_slow_paginated_source(self, tmp_path):
        """Mesure : délai entre l'échéance et l'arrêt effectif, avec une
        latence de 50 ms par page et une politesse de 0,3 s."""
        client = HTTPClient(use_cache=False)
        client.session = FakeSession(
            lambda url, p, n: FakeResponse(payload=hal_page(n * 1000, 1000, f"c{n}")),
            latency=0.05)
        ctx = RunContext.with_timeout(0.5, http_client=client, output_dir=tmp_path)
        t0 = time.monotonic()
        with pytest.raises(OperationCancelled) as exc:
            orchestrator.harvest(SearchRequest(query="q", sources=["hal"],
                                               max_per_source=100000), ctx)
        overshoot = time.monotonic() - t0 - 0.5
        assert exc.value.reason == "deadline"
        assert overshoot < 0.3
        assert list(tmp_path.glob("corpus_*")) == []


# === resolve_dois : stop_event branché ===

class TestResolveDoisStopEvent:
    def _items(self, n):
        return [{"itemType": "journalArticle", "title": f"Un titre de test numero {i}",
                 "creators": [{"creatorType": "author", "lastName": f"Nom{i}"}],
                 "date": "2020", "extra": ""} for i in range(n)]

    def test_stop_during_retry_wait_marks_not_processed(self, monkeypatch):
        from utils import doi_resolver as dr
        stop = threading.Event()
        calls = []

        class _Sess:
            headers = {}

            def get(self, url, params=None, headers=None, timeout=None, **kw):
                calls.append(1)
                return FakeResponse(status=503, payload={})

        _set_after(stop, 0.2)
        t0 = time.monotonic()
        out, rep = dr.resolve_dois(self._items(3), session=_Sess(), workers=1,
                                   stop_event=stop, min_interval=0)
        assert time.monotonic() - t0 < 2.0       # backoff (1 s, 2 s) interrompu
        statuses = [e["status"] for e in rep["entries"]]
        assert statuses == ["non_traite"] * 3
        assert rep["errors"] == 0 and rep["no_candidate"] == 0
        assert rep["stop_reason"] == "arrêt demandé"
        assert len(calls) == 1
