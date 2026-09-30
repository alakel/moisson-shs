# -*- coding: utf-8 -*-
"""
OPT-10 : durée de la résolution de DOI sous la politesse CrossRef réelle.

Scénario : 100 notices sans DOI, latence simulée de 300 ms par requête
CrossRef, politesse réelle du projet (depuis OPT-09 :
``REQUEST_DELAY_CROSSREF_LIST`` entre deux départs de requêtes de liste vers
``api.crossref.org``, ``CROSSREF_MAX_IN_FLIGHT`` requêtes en vol), pool de
``DOI_RESOLVE_WORKERS`` workers, client ``HTTPClient`` réel sur session
simulée. Aucun réseau.

Le temps est mis à l'échelle (horloge de ``utils.http`` et de
``utils.doi_resolver`` accélérée d'un facteur ``SCALE``, défaut 5, variable
``MOISSON_TEST_TIME_SCALE`` ; 1 = temps réel) pour que le test dure environ
7 s réelles au lieu de 34 s ; les durées annoncées sont en secondes
simulées. Le surcoût par requête (réveil des threads après leur créneau,
≈ 3 ms réelles mesurées sur macOS) est lui aussi multiplié par ``SCALE`` :
la mesure accélérée est pessimiste.

Plancher théorique (OPT-09) : CrossRef annonce 3 requêtes de liste par
seconde pour le pool poli (en-têtes x-rate-limit-*, relevés en live le
2026-09-28) ; 100 départs espacés de 0,34 s occupent 99 × 0,34 = 33,66 s,
plus 0,3 s de latence pour la dernière réponse, soit 33,96 s. Avant OPT-09,
l'intervalle de 0,15 s donnait 15,15 s mais dépassait la limite annoncée
dès que la latence descendait sous 0,67 s. Le critère « 100 notices < 15 s »
du plan est donc inatteignable sous la limite annoncée par CrossRef (écart
consigné par OPT-09) ; le test vérifie que la résolution atteint ce
plancher sans jamais enfreindre la politesse, soit plus vite que le
séquentiel (latence + intervalle par requête, ≈ 64 s).
"""

import os
import threading
import time as _real_time

import pytest

import config
import utils.http as http_mod
from utils.http import HTTPClient

# Secondes simulées par seconde réelle (1 = mesure en temps réel).
SCALE = float(os.environ.get("MOISSON_TEST_TIME_SCALE", "5"))
LATENCY = 0.30         # latence simulée d'une requête CrossRef (s)
N_ITEMS = 100
# Surcoût toléré par requête, en secondes réelles (réveil des threads,
# conversion des notices), multiplié par SCALE en temps simulé.
OVERHEAD_PER_REQUEST = 0.008
# Retard toléré sur l'horodatage d'un départ, en secondes réelles : l'instant
# est relevé dans la session simulée, après le créneau de politesse ; un fil
# retardé entre les deux (trois fils en vol depuis OPT-09) rapproche en
# apparence deux départs. Multiplié par SCALE en temps simulé.
STAMP_JITTER = 0.015


def _min_gap_tolerated(interval: float) -> float:
    """Plus petit écart admis entre deux départs horodatés."""
    return interval - max(0.1 * interval, STAMP_JITTER * SCALE)


class _ScaledClock:
    """Horloge accélérée : ``sleep(s)`` dort ``s / SCALE`` secondes réelles."""

    def __init__(self, factor: float):
        self.factor = factor
        self._t0 = _real_time.monotonic()

    def monotonic(self) -> float:
        return 1_000_000.0 + (_real_time.monotonic() - self._t0) * self.factor

    def time(self) -> float:
        return _real_time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            _real_time.sleep(seconds / self.factor)


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.headers = {}
        self.from_cache = False
        self.reason = "OK"
        self.url = ""

    def json(self):
        return self._payload


class _CrossRefSession:
    """Session simulée : CrossRef répond en LATENCY secondes simulées avec un
    candidat concordant ; enregistre l'instant simulé de chaque départ."""

    def __init__(self, clock: _ScaledClock):
        self.clock = clock
        self.starts = []
        self.headers = {}
        self._lock = threading.Lock()

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        with self._lock:
            self.starts.append(self.clock.monotonic())
        self.clock.sleep(LATENCY)
        title = params["query.bibliographic"]
        family = params.get("query.author", "")
        return _Resp({"message": {"items": [{
            "DOI": f"10.9999/opt10.{abs(hash(title)) % 10**8}",
            "title": [title],
            "author": [{"family": family.capitalize()}],
            "issued": {"date-parts": [[2020]]},
        }]}})

    def mount(self, *a):
        pass

    def close(self):
        pass


def _corpus(n):
    return [{
        "itemType": "journalArticle",
        "title": f"Etude numero {i} sur les usages du numerique en SHS",
        "date": "2020",
        "creators": [{"creatorType": "author", "firstName": "A",
                      "lastName": f"Auteur{i}"}],
        "extra": "Sources: Google Scholar (SerpAPI)",
        "tags": [], "collections": [], "relations": {},
    } for i in range(n)]


@pytest.fixture
def scaled(monkeypatch):
    import utils.doi_resolver as dr
    clock = _ScaledClock(SCALE)
    monkeypatch.setattr(http_mod, "time", clock)
    monkeypatch.setattr(dr, "time", clock)
    http_mod.reset_http_policy()
    yield clock
    http_mod.reset_http_policy()


def test_100_notices_reach_the_politeness_floor(scaled):
    from utils.doi_resolver import resolve_dois
    client = HTTPClient(use_cache=False)
    client.session = _CrossRefSession(scaled)

    t0 = scaled.monotonic()
    out, rep = resolve_dois(_corpus(N_ITEMS), http_client=client)
    elapsed = scaled.monotonic() - t0

    interval = config.REQUEST_DELAY_CROSSREF_LIST
    floor = (N_ITEMS - 1) * interval + LATENCY
    # Séquentiel sous la même politesse : chaque requête attend sa réponse,
    # puis le créneau suivant.
    sequential = N_ITEMS * (LATENCY + interval)

    assert rep["resolved"] == N_ITEMS and rep["queries_sent"] == N_ITEMS
    assert all(it.get("DOI") for it in out)
    # Politesse réelle : jamais deux départs à moins de l'intervalle
    # (tolérance pour l'instant d'horodatage, pris après le créneau).
    starts = sorted(client.session.starts)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= _min_gap_tolerated(interval), min(gaps)
    # Débit moyen : jamais plus d'un départ par intervalle sur la durée.
    assert starts[-1] - starts[0] >= (N_ITEMS - 1) * interval - 0.2
    # La durée ne peut pas passer sous le plancher de politesse…
    assert elapsed >= floor - 0.2
    # … et l'atteint, au surcoût par requête près : un peu plus de la moitié
    # du séquentiel (≈ 64 s).
    margin = max(0.5, OVERHEAD_PER_REQUEST * SCALE * N_ITEMS)
    assert elapsed < floor + margin, (elapsed, floor, margin)
    assert elapsed < 0.65 * sequential
    print(f"\n[OPT-10] {N_ITEMS} notices : {elapsed:.2f} s simulées "
          f"(plancher {floor:.2f} s, séquentiel ≈ {sequential:.1f} s)")


def test_budgeted_path_keeps_the_same_throughput(scaled):
    """Voie de l'outil MCP (budget en temps de 240 s, transmis à la
    politique HTTP pour chaque requête, revue OPT-10) : même plancher, même
    politesse, aucune requête perdue. 50 notices (OPT-09 : plancher à 0,34 s
    d'intervalle ; la moitié du corpus suffit à vérifier le débit)."""
    from utils.doi_resolver import resolve_dois
    n_items = N_ITEMS // 2
    client = HTTPClient(use_cache=False)
    client.session = _CrossRefSession(scaled)
    t0 = scaled.monotonic()
    out, rep = resolve_dois(_corpus(n_items), http_client=client,
                            max_seconds=240)
    elapsed = scaled.monotonic() - t0
    interval = config.REQUEST_DELAY_CROSSREF_LIST
    floor = (n_items - 1) * interval + LATENCY
    starts = sorted(client.session.starts)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert rep["resolved"] == n_items and rep["not_processed"] == 0
    assert min(gaps) >= _min_gap_tolerated(interval), min(gaps)
    margin = max(0.5, OVERHEAD_PER_REQUEST * SCALE * n_items)
    assert floor - 0.2 <= elapsed < floor + margin, (elapsed, floor)


def test_duplicate_queries_are_sent_once(scaled):
    """Deux notices de même titre normalisé et même premier auteur partagent
    une seule requête (cache mémoire des requêtes normalisées)."""
    from utils.doi_resolver import resolve_dois
    client = HTTPClient(use_cache=False)
    client.session = _CrossRefSession(scaled)
    corpus = _corpus(3)
    twin = dict(corpus[0])
    twin["title"] = corpus[0]["title"].upper() + " !"   # même titre normalisé
    corpus.append(twin)
    out, rep = resolve_dois(corpus, http_client=client)
    assert rep["queries_sent"] == 3
    assert rep["resolved"] == 4
    assert out[3]["DOI"] == out[0]["DOI"]
