"""
OPT-09 bis : échéance souple de la collecte (moteur).

Une source muette (délais de lecture) retenait la moisson jusqu'au budget de
requête (120 s) : un client MCP qui expire avant annule l'appel, et rien
n'est publié. Avec une échéance souple (``RunContext.soft_deadline``) :

- chaque source reçoit son propre signal (``RunContext.for_source``) : à
  l'échéance, ses requêtes en vol ont leur délai réseau borné et ses attentes
  s'interrompent, sans lever le signal de l'exécution ;
- la source arrêtée est ``error`` (``partial`` avec des notices, ``skipped``
  si elle n'a pas démarré), motif ``deadline`` ; les autres sont publiées ;
- une source encore occupée après le délai de grâce est abandonnée, son
  payload brut tardif supprimé ;
- l'annulation par le client annule toujours toute l'exécution (rien publié) ;
- sans source lente, rien ne change ; sans échéance (CLI, HTTP), les chemins
  historiques sont gardés ;
- la progression continue pendant l'attente (valeurs strictement
  croissantes), jamais après l'échéance.

Aucun accès réseau ni socket : transport ``requests`` remplacé par des
adaptateurs en mémoire, ou exécutant de source remplacé.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
import requests
from requests.adapters import BaseAdapter

import config
from connectors.base import Connector
from engine import orchestrator
from engine.models import (
    REASON_DEADLINE, STATUS_CANCELLED, STATUS_COMPLETE, STATUS_ERROR,
    STATUS_PARTIAL, STATUS_SKIPPED, RunContext, SearchRequest,
    SourceResult, current_soft_deadline, soft_deadline_scope,
)
from utils.http import (
    CANCEL_REASON_CANCELLED, HTTPClient, OperationCancelled, reset_http_policy,
)


# === Transport en mémoire ===

def _response(request, payload, status=200):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(payload).encode("utf-8")
    resp.headers["Content-Type"] = "application/json"
    resp.encoding = "utf-8"
    resp.url = request.url
    resp.request = request
    return resp


class _MuteAdapter(BaseAdapter):
    """Service muet : attend le délai de lecture reçu, puis ReadTimeout."""

    def __init__(self):
        super().__init__()
        self.timeouts = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        read = timeout[1] if isinstance(timeout, tuple) else timeout
        self.timeouts.append(read)
        time.sleep(float(read or 0))
        raise requests.exceptions.ReadTimeout("service muet (simulé)",
                                              request=request)

    def close(self):
        pass


class _JsonAdapter(BaseAdapter):
    """Service qui répond aussitôt un JSON fixe."""

    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def send(self, request, stream=False, timeout=None, verify=True, cert=None,
             proxies=None):
        return _response(request, self.payload)

    def close(self):
        pass


def _items(label, n=2):
    return [{"itemType": "journalArticle", "title": f"{label} titre {i}",
             "date": "2024", "DOI": f"10.9999/{label.lower()}.{i}",
             "creators": [{"creatorType": "author", "lastName": f"{label}{i}"}],
             "extra": f"Sources: {label}"} for i in range(n)]


class _FastConnector(Connector):
    SOURCE_NAME = "Rapide"

    def search(self, query, max_results=1000, **kwargs):
        report = self._start_report()
        report.page(2)
        report.done()
        return _items(self.SOURCE_NAME)


class _MuteHttpConnector(Connector):
    """Une requête vers un hôte muet (une seule tentative, délai de lecture
    ``READ`` secondes) ; une erreur réseau devient un statut, comme les vrais
    connecteurs ; l'annulation (BaseException) traverse."""

    SOURCE_NAME = "Muet"
    URL = "https://muet.opt09bis.test/search"
    READ = 2.0

    def search(self, query, max_results=1000, **kwargs):
        report = self._start_report()
        try:
            self.http.get(self.URL, params={"q": query},
                          timeout=(0.5, self.READ), max_attempts=1)
        except Exception as exc:  # noqa: BLE001 - comme les connecteurs réels
            report.fail_with(exc)
        return []


def _classes(mapping):
    def get(key):
        return mapping[key]
    return get


@pytest.fixture(autouse=True)
def _fresh_politeness():
    reset_http_policy()
    yield
    reset_http_policy()


def _client(**adapters):
    client = HTTPClient(use_cache=False)
    for prefix, adapter in adapters.items():
        client.session.mount(prefix, adapter)
    return client


def _soft_ctx(seconds, grace=0.3, **kwargs):
    return RunContext(soft_deadline=time.monotonic() + seconds,
                      soft_deadline_s=seconds, soft_grace_s=grace, **kwargs)


# === Contexte : portée, signal par source, vue ===

class TestRunContextSoftDeadline:
    def test_no_scope_no_soft_deadline(self):
        ctx = RunContext()
        assert ctx.soft_deadline is None and not ctx.soft_deadline_active
        assert ctx.for_source() is ctx
        assert "soft_deadline_s" not in ctx.budgets

    def test_scope_gives_the_deadline_to_new_contexts(self):
        at = time.monotonic() + 30
        with soft_deadline_scope(at, 30):
            assert current_soft_deadline() == (at, 30.0)
            ctx = RunContext(budgets={"max_per_source": 5})
        assert current_soft_deadline() is None
        assert ctx.soft_deadline == at and ctx.soft_deadline_s == 30
        assert ctx.budgets == {"max_per_source": 5, "soft_deadline_s": 30.0}
        assert RunContext().soft_deadline is None      # hors portée

    def test_neutral_scope(self):
        with soft_deadline_scope(None):
            assert RunContext().soft_deadline is None

    def test_scope_is_per_thread(self):
        seen = {}
        with soft_deadline_scope(time.monotonic() + 30, 30):
            t = threading.Thread(target=lambda: seen.update(
                ctx=RunContext().soft_deadline))
            t.start()
            t.join()
        assert seen["ctx"] is None

    def test_source_signal_keeps_the_client_cancellation(self):
        ctx = _soft_ctx(0.05)
        sig = ctx.source_signal()
        assert not sig.is_set()
        time.sleep(0.08)
        # Échéance souple : la source s'arrête (motif deadline), pas
        # l'exécution.
        assert sig.reason() == REASON_DEADLINE
        assert not ctx.signal.is_set() and not ctx.cancelled()
        ctx.cancel()                       # annulation par le client
        assert sig.reason() == CANCEL_REASON_CANCELLED
        assert ctx.source_signal().reason() == CANCEL_REASON_CANCELLED

    def test_source_signal_takes_the_nearest_deadline(self):
        now = time.monotonic()
        ctx = RunContext(deadline=now + 5, soft_deadline=now + 50)
        assert ctx.source_signal().deadline == now + 5
        ctx = RunContext(deadline=now + 50, soft_deadline=now + 5)
        assert ctx.source_signal().deadline == now + 5

    def test_view_carries_its_signal_to_the_http_client(self):
        client = HTTPClient(use_cache=False)
        ctx = _soft_ctx(10, http_client=client)
        view = ctx.for_source()
        assert view is not ctx and view.run_id == ctx.run_id
        assert view.signal is not ctx.signal
        assert view.http_client.cancel_signal is view.signal
        assert view.http_client.session is client.session   # même session
        view.http_client.close()             # une vue ne ferme pas la session
        assert client.session.adapters

    def test_view_of_a_double_without_with_cancel(self):
        class _Double:
            cancel_signal = None
        double = _Double()
        view = _soft_ctx(10, http_client=double).for_source()
        assert view.http_client is double


# === Statut d'une source arrêtée ===

class TestOutcome:
    def _job(self, started):
        return {"key": "isidore", "name": "Isidore", "started_at": started}

    def _cancelled(self, items=()):
        return SourceResult(source="isidore", display_name="Isidore",
                            items=list(items), status=STATUS_CANCELLED,
                            reason=REASON_DEADLINE, detail="échéance dépassée",
                            duration_s=1.2)

    def test_stopped_without_items_is_error(self):
        ctx = _soft_ctx(-0.1)
        res = orchestrator._soft_deadline_outcome(
            self._cancelled(), self._job(ctx.soft_deadline - 1), ctx)
        assert (res.status, res.reason) == (STATUS_ERROR, REASON_DEADLINE)
        assert "échéance souple" in res.detail and "arrêtée" in res.detail
        assert res.failed and not res.executed

    def test_stopped_with_items_is_partial(self):
        ctx = _soft_ctx(-0.1)
        res = orchestrator._soft_deadline_outcome(
            self._cancelled(_items("Isidore", 3)),
            self._job(ctx.soft_deadline - 1), ctx)
        assert (res.status, res.reason) == (STATUS_PARTIAL, REASON_DEADLINE)
        assert res.item_count == 3 and "3 notice(s)" in res.detail
        assert not res.executed        # une panne, pas une limite fournisseur

    def test_started_after_the_deadline_is_skipped(self):
        ctx = _soft_ctx(-0.1)
        res = orchestrator._soft_deadline_outcome(
            self._cancelled(), self._job(ctx.soft_deadline + 0.01), ctx)
        assert (res.status, res.reason) == (STATUS_SKIPPED, REASON_DEADLINE)
        assert "avant le démarrage" in res.detail

    def test_client_cancellation_is_not_converted(self):
        ctx = _soft_ctx(-0.1)
        ctx.cancel()
        res = orchestrator._soft_deadline_outcome(
            self._cancelled(), self._job(ctx.soft_deadline - 1), ctx)
        assert res.status == STATUS_CANCELLED

    def test_hard_deadline_is_not_converted(self):
        now = time.monotonic()
        ctx = RunContext(deadline=now - 0.1, soft_deadline=now + 10)
        res = orchestrator._soft_deadline_outcome(
            self._cancelled(), self._job(now - 1), ctx)
        assert res.status == STATUS_CANCELLED

    def test_without_soft_deadline_nothing_changes(self):
        res = orchestrator._soft_deadline_outcome(
            self._cancelled(), self._job(0), RunContext())
        assert res.status == STATUS_CANCELLED
        ok = SourceResult(source="hal", display_name="HAL", items=_items("HAL"))
        assert orchestrator._soft_deadline_outcome(
            ok, self._job(0), _soft_ctx(-0.1)) is ok
        assert ok.status == STATUS_COMPLETE


# === Source muette, vrai run_source, vrai client HTTP ===

class TestMuteSource:
    SOURCES = ["hal", "isidore", "openedition"]

    def _setup(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "get_connector_class", _classes({
            "hal": _FastConnector, "isidore": _MuteHttpConnector,
            "openedition": _FastConnector}))
        return _client(**{"https://muet.opt09bis.test/": _MuteAdapter()})

    def test_before_the_mute_source_holds_the_harvest(self, monkeypatch, tmp_path):
        """Sans échéance souple : la moisson attend le délai de lecture de la
        source muette (2 s ici, jusqu'à 120 s en réel)."""
        client = self._setup(monkeypatch)
        t0 = time.monotonic()
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=self.SOURCES, workers=4),
            RunContext(http_client=client, output_dir=tmp_path))
        elapsed = time.monotonic() - t0
        assert elapsed >= _MuteHttpConnector.READ - 0.05
        st = {r.source: (r.status, r.reason) for r in res.sources}
        assert st["isidore"] == (STATUS_ERROR, "network_error")

    def test_soft_deadline_publishes_the_other_sources(self, monkeypatch, tmp_path):
        client = self._setup(monkeypatch)
        ctx = _soft_ctx(0.6, grace=0.3, http_client=client, output_dir=tmp_path)
        t0 = time.monotonic()
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=self.SOURCES, workers=4), ctx)
        elapsed = time.monotonic() - t0
        # Avant l'échéance plus la grâce, avec une marge.
        assert elapsed < 0.6 + 0.3 + 0.7
        assert elapsed >= 0.55
        st = {r.source: r for r in res.sources}
        assert st["isidore"].status == STATUS_ERROR
        assert st["isidore"].reason == REASON_DEADLINE
        assert "échéance souple de 0.6 s" in st["isidore"].detail
        assert st["hal"].status == STATUS_COMPLETE
        assert st["openedition"].status == STATUS_COMPLETE
        # Publication possible : notices des autres sources, pas d'échec
        # total, signal de l'exécution intact.
        assert len(res.items) == 2          # HAL et OpenEdition, même libellé
        assert not res.all_failed and res.overall_status == "partial"
        assert not ctx.cancelled()
        # Le délai de lecture de la requête en vol a été borné par l'échéance.
        adapter = client.session.get_adapter(_MuteHttpConnector.URL)
        assert adapter.timeouts and adapter.timeouts[0] <= 0.6 + 0.01
        # Payloads bruts : sources exécutées seulement.
        raws = sorted(p.name for p in (tmp_path / "raw").glob("*.json"))
        assert len(raws) == 2 and not any("isidore" in n for n in raws)

    def test_not_started_source_is_skipped(self, monkeypatch, tmp_path):
        """Un seul fil : la source muette occupe le pool jusqu'à l'échéance ;
        la suivante n'a pas démarré avant : ``skipped``, motif deadline."""
        client = self._setup(monkeypatch)
        monkeypatch.setattr(orchestrator, "plan_schedule",
                            lambda s: [["isidore", "hal"]])
        ctx = _soft_ctx(0.4, grace=0.3, http_client=client)
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=["isidore", "hal"], workers=1), ctx)
        st = {r.source: r for r in res.sources}
        assert (st["isidore"].status, st["isidore"].reason) == (
            STATUS_ERROR, REASON_DEADLINE)
        assert (st["hal"].status, st["hal"].reason) == (
            STATUS_SKIPPED, REASON_DEADLINE)
        assert "avant le démarrage" in st["hal"].detail
        # Rien d'exploitable : échec total (erreur d'outil côté MCP).
        assert res.all_failed and res.items == []


# === Échéance intenable avant d'être atteinte ===

class TestUnreachableDeadline:
    def test_politeness_slot_beyond_the_deadline(self, monkeypatch):
        """Deuxième page d'une source dont le créneau de politesse tombe
        après l'échéance : la source s'arrête aussitôt (motif deadline,
        « intenable »), sans annuler la moisson ; l'autre est publiée."""
        class _TwoPages(Connector):
            SOURCE_NAME = "Paginé"
            URL = "https://lent.opt09bis.test/page"

            def search(self, query, max_results=1000, **kwargs):
                self._start_report()
                for page in (1, 2):
                    self.http.get(self.URL, params={"p": page},
                                  min_interval=5.0, max_attempts=1)
                    self.last_status.page(1)
                return _items("Paginé")

        monkeypatch.setattr(orchestrator, "get_connector_class", _classes({
            "hal": _FastConnector, "crossref": _TwoPages}))
        client = _client(**{"https://lent.opt09bis.test/": _JsonAdapter({})})
        ctx = _soft_ctx(2.0, http_client=client)
        t0 = time.monotonic()
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=["hal", "crossref"], workers=2), ctx)
        assert time.monotonic() - t0 < 1.0          # bien avant l'échéance
        st = {r.source: r for r in res.sources}
        assert (st["crossref"].status, st["crossref"].reason) == (
            STATUS_ERROR, REASON_DEADLINE)
        assert "intenable" in st["crossref"].detail
        assert st["hal"].status == STATUS_COMPLETE and len(res.items) == 2
        assert not ctx.cancelled()


# === Isidore réel : flux partagé et consommateurs arrêtés ensemble ===

class TestSharedIsidoreFlow:
    def test_flow_consumers_stop_at_the_deadline(self, monkeypatch,
                                                 hal_solr_response):
        """HAL (vrai connecteur, réponse de la fixture) répond ; Isidore est
        muet : la source Isidore et sa sous-source OpenEdition (vrais
        connecteurs, flux partagé) sont arrêtées à l'échéance, une seule
        requête Isidore part ; HAL est publiée."""
        mute = _MuteAdapter()
        client = _client(**{
            "https://api.archives-ouvertes.fr/": _JsonAdapter(hal_solr_response),
            "https://api.isidore.science/": mute,
        })
        ctx = _soft_ctx(0.8, grace=0.3, http_client=client)
        t0 = time.monotonic()
        res = orchestrator.harvest(
            SearchRequest(query="discours", sources=["hal", "isidore",
                                                     "openedition"],
                          workers=4, max_per_source=3), ctx)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.8 + 0.3 + 1.0
        st = {r.source: r for r in res.sources}
        assert st["hal"].status == STATUS_COMPLETE and st["hal"].item_count == 3
        for key in ("isidore", "openedition"):
            assert st[key].status == STATUS_ERROR, st[key].describe()
            assert st[key].reason == REASON_DEADLINE
        assert len(mute.timeouts) == 1       # une page, un téléchargement
        assert len(res.items) == 3 and res.overall_status == "partial"


# === Abandon après le délai de grâce ===

class TestAbandon:
    def test_busy_source_is_abandoned_and_its_late_raw_removed(
            self, monkeypatch, tmp_path):
        finished = threading.Event()

        class _Stubborn(Connector):
            """Travail non interruptible (ignore le signal)."""
            SOURCE_NAME = "Isidore"

            def search(self, query, max_results=1000, **kwargs):
                self._start_report()
                time.sleep(1.2)
                finished.set()
                return _items("Isidore")

        writes = []
        real_write = orchestrator.write_raw

        def spy(output_dir, name, run_id, items):
            path = real_write(output_dir, name, run_id, items)
            writes.append(path)
            return path

        monkeypatch.setattr(orchestrator, "write_raw", spy)
        monkeypatch.setattr(orchestrator, "get_connector_class", _classes({
            "hal": _FastConnector, "isidore": _Stubborn}))
        ctx = _soft_ctx(0.3, grace=0.2, http_client=_client(),
                        output_dir=tmp_path)
        t0 = time.monotonic()
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=["hal", "isidore"], workers=2), ctx)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.3 + 0.2 + 0.4 and not finished.is_set()
        st = {r.source: r for r in res.sources}
        assert (st["isidore"].status, st["isidore"].reason) == (
            STATUS_ERROR, REASON_DEADLINE)
        assert "abandonnée" in st["isidore"].detail
        assert st["hal"].status == STATUS_COMPLETE
        assert [it["title"] for it in res.items] == ["Rapide titre 0",
                                                     "Rapide titre 1"]
        # La source abandonnée finit seule ; son payload brut tardif est
        # écrit puis supprimé, celui de HAL reste.
        assert finished.wait(3.0)
        late = orchestrator.raw_path(tmp_path, "Isidore", ctx.run_id)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and (late not in writes or late.exists()):
            time.sleep(0.02)
        assert late in writes and not late.exists()
        assert orchestrator.raw_path(tmp_path, "HAL", ctx.run_id).exists()


# === Annulation par le client : toujours rien de publié ===

class TestClientCancellation:
    def test_client_cancel_during_soft_deadline_cancels_the_run(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(orchestrator, "get_connector_class", _classes({
            "hal": _FastConnector, "isidore": _MuteHttpConnector}))
        client = _client(**{"https://muet.opt09bis.test/": _MuteAdapter()})
        ctx = _soft_ctx(5.0, http_client=client, output_dir=tmp_path)
        timer = threading.Timer(0.3, ctx.cancel)
        timer.start()
        t0 = time.monotonic()
        with pytest.raises(OperationCancelled) as info:
            orchestrator.harvest(
                SearchRequest(query="q", sources=["hal", "isidore"], workers=2),
                ctx)
        timer.cancel()
        assert info.value.reason == CANCEL_REASON_CANCELLED
        # La requête en vol n'est pas interrompue par l'événement (limite
        # connue d'OPT-07) : le fil s'arrête à la fin de son délai de lecture,
        # attendu avant de rendre la main.
        assert time.monotonic() - t0 < _MuteHttpConnector.READ + 1.0
        partial = info.value.result
        assert partial.items == []
        # Payloads bruts supprimés (celui de HAL compris) : rien publié.
        assert list((tmp_path / "raw").glob("*.json")) == []


# === Sans source lente : comportement inchangé ===

class TestUnchanged:
    def _run(self, monkeypatch, tmp_path, ctx_kwargs, workers=4):
        monkeypatch.setattr(orchestrator, "get_connector_class", _classes({
            "hal": _FastConnector, "isidore": _FastConnector,
            "openedition": _FastConnector}))
        out = tmp_path / str(len(list(tmp_path.iterdir())))
        ctx = RunContext(http_client=_client(), output_dir=out, run_id="r1",
                         **ctx_kwargs)
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=["hal", "isidore", "openedition"],
                          workers=workers), ctx)
        raws = sorted(p.name for p in (out / "raw").glob("*.json"))
        return res, raws

    @pytest.mark.parametrize("workers", [1, 4])
    def test_same_result_with_or_without_soft_deadline(self, monkeypatch,
                                                       tmp_path, workers):
        base, raws_base = self._run(monkeypatch, tmp_path, {}, workers)
        soft, raws_soft = self._run(monkeypatch, tmp_path, {
            "soft_deadline": time.monotonic() + 30, "soft_deadline_s": 30},
            workers)
        assert [r.as_dict() | {"duration_s": 0} for r in base.sources] == \
               [r.as_dict() | {"duration_s": 0} for r in soft.sources]
        assert json.dumps(base.items, sort_keys=True) == \
               json.dumps(soft.items, sort_keys=True)
        assert raws_base == raws_soft and len(raws_base) == 3
        assert base.overall_status == soft.overall_status == "ok"

    def test_historical_paths_without_soft_deadline_or_progress(self, monkeypatch,
                                                                tmp_path):
        """CLI (ni échéance ni progression) : le pool surveillé n'est pas
        utilisé, en série comme en parallèle."""
        def boom(*a, **k):
            raise AssertionError("pool surveillé inattendu")
        monkeypatch.setattr(orchestrator, "_run_watched_pool", boom)
        for workers in (1, 4):
            res, _ = self._run(monkeypatch, tmp_path, {}, workers)
            assert res.overall_status == "ok"


# === Progression pendant l'attente ===

class TestHeartbeat:
    def _slow_connector(self, seconds):
        class _Slow(Connector):
            SOURCE_NAME = "Lent"

            def search(self, query, max_results=1000, **kwargs):
                self._start_report()
                signal = self.cancel_signal()
                if signal is not None and signal.wait(seconds):
                    signal.check()
                return _items("Lent")
        return _Slow

    def test_progress_continues_while_waiting(self, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_PROGRESS_HEARTBEAT_S", 0.05)
        monkeypatch.setattr(orchestrator, "get_connector_class", _classes({
            "hal": _FastConnector, "isidore": self._slow_connector(0.5)}))
        reports = []
        ctx = RunContext(http_client=_client(),
                         progress=lambda d, t, m: reports.append((d, t, m)))
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=["hal", "isidore"], workers=2), ctx)
        assert res.overall_status == "ok"
        beats = [r for r in reports if r[2].startswith("En cours")]
        assert len(beats) >= 3
        assert all("Isidore" in m for _, _, m in beats)
        values = [d for d, _, _ in reports]
        assert values == sorted(values) and len(set(values)) == len(values)
        assert all(0 < d < 2 for d, _, _ in beats)
        assert reports[-1][0] == 2.0 and all(t == 2.0 for _, t, _ in reports)

    def test_no_heartbeat_after_the_soft_deadline(self, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_PROGRESS_HEARTBEAT_S", 0.05)
        monkeypatch.setattr(orchestrator, "get_connector_class", _classes({
            "hal": _FastConnector, "isidore": self._slow_connector(5.0)}))
        stamps = []
        ctx = _soft_ctx(0.4, grace=0.3, http_client=_client(),
                        progress=lambda d, t, m: stamps.append(
                            (time.monotonic(), d, m)))
        res = orchestrator.harvest(
            SearchRequest(query="q", sources=["hal", "isidore"], workers=2), ctx)
        st = {r.source: r for r in res.sources}
        assert (st["isidore"].status, st["isidore"].reason) == (
            STATUS_ERROR, REASON_DEADLINE)
        beats = [(t, m) for t, _, m in stamps if m.startswith("En cours")]
        assert beats and all(t < ctx.soft_deadline + 0.02 for t, _ in beats)
        assert any("échéance dans" in m for _, m in beats)
        values = [d for _, d, _ in stamps]
        assert values == sorted(values) and len(set(values)) == len(values)

    def test_heartbeat_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "HARVEST_PROGRESS_HEARTBEAT_S", 0)
        monkeypatch.setattr(orchestrator, "get_connector_class", _classes({
            "hal": _FastConnector, "isidore": self._slow_connector(0.3)}))
        reports = []
        ctx = RunContext(http_client=_client(),
                         progress=lambda d, t, m: reports.append(m))
        orchestrator.harvest(
            SearchRequest(query="q", sources=["hal", "isidore"], workers=2), ctx)
        assert not any(m.startswith("En cours") for m in reports)
