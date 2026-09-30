# -*- coding: utf-8 -*-
"""
Revue OPT-10 : tests de non-régression des constats corrigés.

1. Budget en temps de ``resolve_dois`` tenu : le reste du budget est transmis
   à la politique HTTP ; aucune tentative ne part après l'échéance, aucun
   réessai ne la dépasse (vrai ``HTTPClient``, session qui n'atteint jamais
   que son délai de lecture, horloge simulée ou accélérée). Aucun réseau.
2. DOI déclaré non canonique : la notice est pourvue, sa valeur n'est jamais
   écrasée, elle est signalée dans le rapport.
3. ``fetch_by_doi`` : un résolveur abandonné n'envoie plus rien ; une rafale
   d'appels sur un hôte lent n'empile pas de requêtes derrière les places en
   vol d'une moisson concurrente.
4. Préfixe arrêté à une frontière de mot, sans jamais rendre la règle plus
   permissive ; numéros de titre.
"""

import json
import threading
import time as _real_time
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

import utils.doi_resolver as dr
import utils.http as http_mod
from utils.http import HTTPClient


def _item(title, year="2020", last="Dupont", doi=None, extra="Sources: HAL",
          item_type="journalArticle"):
    item = {"itemType": item_type, "title": title, "date": year,
            "creators": ([{"creatorType": "author", "firstName": "A",
                           "lastName": last}] if last else []),
            "extra": extra, "tags": [], "collections": [], "relations": {}}
    if doi is not None:
        item["DOI"] = doi
    return item


def _wait_resolvers_idle(timeout=5.0):
    """Attend la fin des résolveurs abandonnés (requêtes déjà parties), qui
    occupent la place de résolveur de leur hôte."""
    deadline = _real_time.monotonic() + timeout
    while _real_time.monotonic() < deadline:
        if not any(t.name.startswith("fetch-by-doi")
                   for t in threading.enumerate()):
            return
        _real_time.sleep(0.02)


@pytest.fixture(autouse=True)
def _isolated_state():
    _wait_resolvers_idle()
    http_mod.reset_http_policy()
    dr.shared_query_cache().clear()
    yield
    _wait_resolvers_idle()
    http_mod.reset_http_policy()
    dr.shared_query_cache().clear()


# === 1) Budget en temps ===

class _SimClock:
    """Horloge simulée : ``sleep`` avance le temps sans attendre."""

    def __init__(self):
        self.now = 1000.0
        self._lock = threading.Lock()

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        if seconds > 0:
            with self._lock:
                self.now += seconds


class _ScaledClock:
    """Horloge accélérée : ``sleep(s)`` dort ``s / factor`` secondes réelles."""

    def __init__(self, factor):
        self.factor = factor
        self._t0 = _real_time.monotonic()

    def monotonic(self):
        return 1_000_000.0 + (_real_time.monotonic() - self._t0) * self.factor

    def time(self):
        return _real_time.time()

    def sleep(self, seconds):
        if seconds > 0:
            _real_time.sleep(seconds / self.factor)


class _HangingSession:
    """CrossRef qui ne répond jamais : chaque GET dure tout son délai de
    lecture, puis lève ``ReadTimeout``. Enregistre (instant, délai)."""

    headers = {}

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self._lock = threading.Lock()

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        read = timeout[1] if isinstance(timeout, tuple) else timeout
        with self._lock:
            self.calls.append((self.clock.monotonic(), read))
        self.clock.sleep(read)
        raise requests.ReadTimeout("read timeout")

    def mount(self, *a):
        pass

    def close(self):
        pass


def _hanging_client(clock):
    client = HTTPClient(use_cache=False)
    client.session = _HangingSession(clock)
    return client


class TestTimeBudget:
    def test_single_attempt_bounded_by_budget(self, monkeypatch):
        """Avant la revue : 3 tentatives de 20 s (63 s pour un budget de
        20 s). Désormais : une tentative, bornée par le budget ; la notice
        suivante ne part pas ; toutes deux « non traité »."""
        clock = _SimClock()
        monkeypatch.setattr(http_mod, "time", clock)
        monkeypatch.setattr(dr, "time", clock)
        client = _hanging_client(clock)
        start = clock.now
        items = [_item("Un titre quelconque pour le test"),
                 _item("Un second titre quelconque du corpus")]
        _, rep = dr.resolve_dois(items, http_client=client, workers=1,
                                 max_seconds=20)
        calls = [(round(t - start, 3), read) for t, read in
                 client.session.calls]
        assert calls == [(0.0, 20.0)]
        assert rep["duration_s"] <= 20.1 + 1e-6
        assert [e["status"] for e in rep["entries"]] == ["non_traite"] * 2
        assert "budget de temps de 20 s" in rep["entries"][0]["reason"]
        assert rep["errors"] == 0 and rep["stopped_early"]

    def test_no_departure_after_deadline_with_three_workers(self, monkeypatch):
        """Trois notices, trois workers, deux places en vol par hôte : la
        troisième attend sa place jusqu'à l'échéance et ne part jamais ;
        durée ≤ budget + un délai minimal de tentative (tolérance de
        réveil des threads, multipliée par l'accélération)."""
        factor = 25.0
        clock = _ScaledClock(factor)
        monkeypatch.setattr(http_mod, "time", clock)
        monkeypatch.setattr(dr, "time", clock)
        # Deux places en vol vers CrossRef, comme lors de la revue (OPT-09
        # en accorde 3, la concurrence annoncée) : la troisième requête doit
        # attendre sa place.
        monkeypatch.setitem(http_mod.HOST_MAX_IN_FLIGHT, "api.crossref.org", 2)
        http_mod.reset_http_policy()
        client = _hanging_client(clock)
        start = clock.monotonic()
        items = [_item(f"Titre numero {i} du corpus de test") for i in range(3)]
        _, rep = dr.resolve_dois(items, http_client=client, workers=3,
                                 max_seconds=25)
        elapsed = clock.monotonic() - start
        departures = sorted(t - start for t, _ in client.session.calls)
        assert departures and all(t < 25.0 for t in departures), departures
        assert len(departures) <= 2
        assert elapsed <= 25.0 + 0.1 + 1.5, elapsed
        assert [e["status"] for e in rep["entries"]] == ["non_traite"] * 3

    def test_large_budget_keeps_retries_within_budget(self, monkeypatch):
        """Budget large : la politique rejoue normalement (3 tentatives de
        30 s), la notice est en erreur (le service est en cause), et la durée
        reste sous le budget."""
        clock = _SimClock()
        monkeypatch.setattr(http_mod, "time", clock)
        monkeypatch.setattr(dr, "time", clock)
        client = _hanging_client(clock)
        _, rep = dr.resolve_dois([_item("Un titre quelconque pour le test")],
                                 http_client=client, workers=1,
                                 max_seconds=240)
        assert len(client.session.calls) == 3
        assert rep["entries"][0]["status"] == "erreur"
        assert rep["duration_s"] < 240

    def test_budget_is_forwarded_to_get_json_when_accepted(self):
        seen = {}

        def fake(url, params=None, budget=None, **kwargs):
            seen["budget"] = budget
            return {"message": {"items": []}}

        with patch.object(dr, "get_json", side_effect=fake):
            candidates, error = dr.query_crossref_detailed("Un titre",
                                                           budget=12.5)
        assert (candidates, error) == ([], None)
        assert seen["budget"] == 12.5

    def test_budget_falls_back_to_policy_get_with_quota_hook(self):
        """``http_scholarly.get_json`` n'expose pas encore ``budget`` : la
        requête bornée passe par ``policy_get``, avec le budget et le
        contrôle de quota."""
        captured = {}

        class _Resp:
            def json(self):
                return {"message": {"items": []}}

        def fake_policy_get(session, url, **kwargs):
            captured.update(kwargs, url=url)
            return _Resp()

        def no_budget_get_json(url, params=None, headers=None, timeout=None,
                               max_retries=None, base_backoff=None,
                               session=None, min_interval=None,
                               use_cache=True):
            raise AssertionError("get_json sans budget ne doit pas servir")

        with patch.object(dr, "get_json", no_budget_get_json), \
                patch.object(dr, "policy_get", fake_policy_get):
            candidates, error = dr.query_crossref_detailed(
                "Un titre", budget=7.0, session=object())
        assert (candidates, error) == ([], None)
        assert captured["budget"] == 7.0
        assert callable(captured["response_hook"])
        assert captured["params"]["query.bibliographic"] == "Un titre"

    def test_no_budget_keeps_historical_call(self):
        seen = {}

        def fake(url, params=None, **kwargs):
            seen.update(kwargs)
            return {"message": {"items": []}}

        with patch.object(dr, "get_json", side_effect=fake):
            dr.query_crossref_detailed("Un titre")
        assert "budget" not in seen


# === 2) DOI déclaré non canonique ===

class TestNonCanonicalDoi:
    def _run(self, items):
        calls = []

        def fake(url, params=None, **kwargs):
            calls.append(params)
            return {"message": {"items": [{
                "title": [items[0]["title"]], "DOI": "10.9999/autre",
                "author": [{"family": "Dupont"}],
                "issued": {"date-parts": [[2020]]}}]}}

        with patch.object(dr, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            out, rep = dr.resolve_dois(items)
        return out, rep, calls

    def test_field_value_is_never_overwritten(self):
        """Avant la revue : notice réinterrogée, « 10.1234/abc def »
        remplacé par le DOI d'un candidat, sans trace."""
        item = _item("Un titre assez long pour être apparié", doi="10.1234/abc def")
        out, rep, calls = self._run([item])
        assert calls == []
        assert out[0]["DOI"] == "10.1234/abc def"
        assert out[0]["extra"] == "Sources: HAL"
        assert rep["already_had_doi"] == 1 and rep["non_canonical_doi"] == 1
        assert rep["attempted"] == 0 and rep["entries"] == []
        assert rep["non_canonical_dois"] == [{
            "index": 0, "title": item["title"], "value": "10.1234/abc def"}]

    def test_extra_line_value_counts_as_declared(self):
        item = _item("Un titre assez long pour être apparié",
                     extra="Sources: HAL\nDOI: voir la version éditeur")
        out, rep, calls = self._run([item])
        assert calls == [] and rep["non_canonical_doi"] == 1
        assert "10.9999/autre" not in json.dumps(out)

    def test_doi_source_line_alone_is_not_a_declared_doi(self):
        item = _item("Un titre assez long pour être apparié",
                     extra="Sources: HAL\nDOI source: ancienne trace")
        assert dr.declared_doi_value(item) == ""

    def test_report_document_counts_them(self):
        item = _item("Un titre assez long pour être apparié", doi="10.1/a b")
        _, rep, _ = self._run([item, _item("Autre", doi="10.1/ok")])
        doc = dr.build_report_document(rep)
        assert doc["counts"]["deja_pourvu"] == 2
        assert doc["counts"]["deja_pourvu_doi_non_canonique"] == 1
        assert doc["non_canonical_dois"][0]["value"] == "10.1/a b"


# === 3) fetch_by_doi : aucun envoi après l'abandon ===

CROSSREF = "api.crossref.org"
ISIDORE = "api.isidore.science"


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.from_cache = False
        self.reason = ""
        self.url = ""

    def json(self):
        return self._payload


class _SlowIsidoreSession:
    """CrossRef répond aussitôt ; Isidore met ``latency`` secondes réelles ;
    le reste répond 404. Horodate chaque départ vers Isidore."""

    headers = {}

    def __init__(self, latency, log):
        self.latency = latency
        self.log = log

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        host = urlsplit(url).hostname
        if host == ISIDORE:
            self.log.append(_real_time.monotonic())
            _real_time.sleep(self.latency)
            return _Resp(200, {"response": {"replies": {"content": {}}}})
        if host == CROSSREF:
            doi = url.rsplit("/works/", 1)[1]
            return _Resp(200, {"message": {
                "DOI": doi, "type": "journal-article", "title": ["T"],
                "author": [{"family": "X"}],
                "issued": {"date-parts": [[2020]]}}})
        return _Resp(404)

    def mount(self, *a):
        pass

    def close(self):
        pass


class TestFetchByDoiAbandon:
    @pytest.fixture(autouse=True)
    def _no_keys(self, monkeypatch):
        for var in ("OPENALEX_API_KEY", "CORE_API_KEY",
                    "SEMANTIC_SCHOLAR_API_KEY"):
            monkeypatch.delenv(var, raising=False)

    def test_burst_sends_nothing_after_return_and_does_not_starve(self):
        """Avant la revue : 6 appels rendus, 4 requêtes Isidore parties
        APRÈS leur retour, et une requête de moisson attendait 7,2 s pour
        une latence propre de 2 s."""
        from utils.lookups import lookup_doi
        latency = 0.6
        log = []
        client = HTTPClient(use_cache=False)
        client.session = _SlowIsidoreSession(latency, log)
        results = [lookup_doi(f"10.1/doi{i}", client, grace=0.02)
                   for i in range(5)]
        t_return = _real_time.monotonic()
        assert all(r["status"] == "trouve" for r in results)
        assert all(next(x["status"] for x in r["resolvers"]
                        if x["name"] == "Isidore") == "abandonne"
                   for r in results)
        # Une moisson interroge Isidore aussitôt après, via un autre client
        # (mêmes places en vol par hôte) : elle n'attend pas derrière une
        # file de requêtes abandonnées.
        other = HTTPClient(use_cache=False)
        other.session = _SlowIsidoreSession(latency, [])
        t0 = _real_time.monotonic()
        other.get(f"https://{ISIDORE}/resource/search", params={"q": "x"})
        harvest_latency = _real_time.monotonic() - t0
        _real_time.sleep(latency)   # laisse finir une éventuelle requête
        late = [t for t in log if t > t_return]
        assert late == [], f"{len(late)} requête(s) partie(s) après le retour"
        assert harvest_latency < latency * 1.5, harvest_latency

    def test_resolver_requests_carry_the_remaining_budget(self):
        """Chaque requête de résolveur reçoit pour budget le reste de la
        durée maximale de l'appel (attente des places en vol comprise)."""
        from utils.lookups import lookup_doi
        budgets = []

        class _Spy(HTTPClient):
            def get(self, url, params=None, timeout=None, headers=None,
                    **kwargs):
                budgets.append(kwargs.get("budget"))
                return super().get(url, params=params, timeout=timeout,
                                   headers=headers, **kwargs)

        client = _Spy(use_cache=False)
        client.session = _SlowIsidoreSession(0.0, [])
        lookup_doi("10.1/x", client, max_seconds=30, grace=5)
        assert budgets and all(b is not None and 0 < b <= 30 for b in budgets)

    def test_abandoned_call_never_sends_a_queued_request(self):
        """Place de résolveur occupée sur l'hôte : la requête en attente
        d'un appel abandonné n'est jamais envoyée."""
        from utils import lookups
        abandon = threading.Event()
        sent = []

        class _Client:
            def get(self, url, **kwargs):
                sent.append(url)
                return _Resp(200, {})

        guarded = lookups._GuardedClient(_Client(), abandon, None)
        host_url = "https://slow.example/x"
        assert lookups._RESOLVER_SLOTS.acquire("slow.example",
                                               threading.Event(), None)
        try:
            outcome = {}

            def call():
                try:
                    guarded.get(host_url)
                    outcome["state"] = "envoyée"
                except lookups._LookupAbandoned:
                    outcome["state"] = "abandonnée"

            worker = threading.Thread(target=call)
            worker.start()
            _real_time.sleep(0.05)
            abandon.set()
            worker.join(2)
        finally:
            lookups._RESOLVER_SLOTS.release("slow.example")
        assert outcome == {"state": "abandonnée"} and sent == []


# === 4) Préfixe et numéros ===

class TestPrefixAndNumbering:
    def test_prefix_must_stop_at_a_word_boundary(self):
        ratio, by_prefix = dr.best_title_ratio(
            "Le discours politique en ligne. Partie I",
            "Le discours politique en ligne. Partie II")
        assert ratio < 1.0   # v2 : préfixe exact, 1,00

    def test_boundary_rule_is_never_more_permissive(self):
        """Préfixe coupé dans un mot : la similarité entière est retenue,
        la corroboration reste exigée (v2 : préfixe 1,00 refusé sans
        corroboration ; v3 : 0,98 entier, toujours refusé)."""
        ratio, by_prefix = dr.best_title_ratio(
            "Le discours numerique et la ville",
            "Le discours numerique et la villes")
        assert by_prefix and ratio < 1.0
        ok, _, motif = dr.evaluate_candidate(
            _item("Le discours numerique et la ville", year="", last=""),
            {"title": ["Le discours numerique et la villes"], "DOI": "10.1/v"})
        assert not ok and "préfixe" in motif

    @pytest.mark.parametrize("a, b, conflict", [
        ("histoire des medias tome 2", "histoire des medias tome 1", True),
        ("le discours partie ii", "le discours partie i", True),
        ("les usages 2", "les usages", True),
        ("rapport annuel 2020", "rapport annuel 2021", True),
        ("covid19 et plateformes", "covid 19 et plateformes", False),
        ("histoire des medias tome 2", "histoire des medias tome ii", False),
        ("what i learned from fieldwork", "what we learned from fieldwork",
         False),
        ("louis xiv et la presse", "louis xv et la presse", True),
    ])
    def test_numbering_conflict(self, a, b, conflict):
        assert bool(dr._numbering_conflict(a, b)) is conflict
