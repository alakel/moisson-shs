"""
Tests du parcours Isidore mutualisé (OPT-08, constats C09, C10, C35).

Couvre, sans aucun accès réseau (doublure de l'API Isidore) :
- une page utile = un téléchargement par exécution, même sans cache, pour la
  source Isidore et les cinq sous-sources, en séquentiel et en threads ;
- fin de fenêtre détectée (pages répétées, bloc ``page`` d'Isidore, fenêtre
  ISIDORE_RESULT_WINDOW) : plus jamais de téléchargement en boucle ;
- mêmes identifiants retenus que l'algorithme historique pour chaque
  sous-source sur un flux sans répétition ;
- sous-source n'apparaissant qu'après le plafond de la source directe ;
- borne ISIDORE_SCAN_MAX_RECORDS respectée et signalée ;
- filtre de domaine strict (hôte analysé) ;
- deux clients HTTP n'échangent rien ; registre explicite partagé ;
- corps non JSON signalé dans le statut (et une nouvelle tentative) ;
- copies profondes et annotations « Sources: » des sous-sources.

Les imports de ``connectors.isidore_scan`` sont faits dans les tests : les
tests de contrat (téléchargements, filtre de domaine) n'utilisent que l'API
publique des connecteurs et peuvent être rejoués sur le code d'avant
chantier, où ils échouent.
"""

import gc
import io
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse

import config
from connectors.cairn import CairnConnector
from connectors.erudit import EruditConnector
from connectors.isidore import IsidoreConnector
from connectors.openedition import OpenEditionConnector
from connectors.persee import PerseeConnector
from connectors.theses_fr import ThesesFrConnector


# === Doublure de l'API Isidore ===

DOMAINS = [
    "journals.openedition.org", "www.cairn.info", "www.persee.fr",
    "id.erudit.org", "www.theses.fr", "hal.science", "www.example.org",
]

PROXIES = [
    (OpenEditionConnector, "openedition.org", "OpenEdition"),
    (CairnConnector, "cairn.info", "Cairn"),
    (PerseeConnector, "persee.fr", "Persée"),
    (EruditConnector, "erudit.org", "Érudit"),
    (ThesesFrConnector, "theses.fr", "Theses.fr"),
]


def make_record(i, host=None, year=None):
    """Notice Isidore minimale mais complète pour le mapper réel."""
    host = host or DOMAINS[i % len(DOMAINS)]
    year = year if year is not None else 2010 + (i % 15)
    url = f"https://{host}/article/w{i}"
    return {
        "@uri": f"10670/1.w{i}",
        "isidore": {
            "title": [{"@xml:lang": "fr", "$": f"Titre de la notice {i}"}],
            "url": url,
            "enrichedCreators": {"creator": {
                "@origin": "Dupont, Jean", "firstname": "Jean", "lastname": "Dupont"}},
            "date": {"@startDate": f"{year}-01-01", "normalizedDate": f"{year}-01-01",
                     "year": str(year)},
            "types": {"type": ["article"]},
            "ore": {"aggregates": [{"$": url}]},
            "language": "fr",
        },
    }


class _Resp:
    def __init__(self, payload=None, status=200, body=None, ctype="application/json"):
        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.from_cache = False
        self.content = body if body is not None else b"{}"

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeIsidore:
    """
    Doublure sémantique de ``resource/search`` (client HTTP simplifié).

    Args:
        records: univers ordonné (liste de notices)
        window: fenêtre servie (0 : aucune) ; au-delà, la dernière fenêtre
            est répétée, comme l'API réelle
        page_block: émet le bloc ``page`` (@previous/@current/@next) comme
            l'API réelle ; False : bloc absent (détection par répétition)
        latency: attente par requête (secondes), pour les tests en threads
        faults: {page: ["html" | "error", ...]} consommés dans l'ordre
        total: total annoncé (@items), défaut : taille de l'univers
    """

    def __init__(self, records, window=200, page_block=True, latency=0.0,
                 faults=None, total=None):
        self.records = records
        self.window = window
        self.page_block = page_block
        self.latency = latency
        self.faults = {k: list(v) for k, v in (faults or {}).items()}
        self.total = len(records) if total is None else total
        self.calls = []
        self.kwargs_seen = []
        self._lock = threading.Lock()

    def payload(self, params):
        replies = int(params.get("replies", 10))
        page = int(params.get("page", 1))
        offset = (page - 1) * replies
        if self.window:
            replies = min(replies, self.window)
            offset = min(offset, max(self.window - replies, 0))
            served = min(self.window, len(self.records))
        else:
            served = len(self.records)
        chunk = self.records[offset:offset + replies]
        last_page = max(1, -(-served // replies))
        current = min(page, last_page)
        block = {}
        if self.page_block and chunk and (current > 1 or current < last_page):
            if current > 1:
                block["@previous"] = str(current - 1)
            block["@current"] = str(current)
            if current < last_page:
                block["@next"] = str(current + 1)
        reply = chunk[0] if len(chunk) == 1 else chunk
        replies_block = {"meta": {"@items": str(self.total), "@pageItems": str(len(chunk))},
                         "content": {"reply": reply} if chunk else {}}
        if block:
            replies_block["page"] = block
        return {"response": {"replies": replies_block}}

    def get(self, url, params=None, **kwargs):
        params = dict(params or {})
        page = int(params.get("page", 1))
        with self._lock:
            self.calls.append(page)
            self.kwargs_seen.append(kwargs)
            fault = self.faults.get(page, []).pop(0) if self.faults.get(page) else None
        if self.latency:
            time.sleep(self.latency)
        if fault == "html":
            return _Resp(None, body=b"<html><body>Maintenance</body></html>",
                         ctype="text/html")
        if fault == "error":
            raise requests.ConnectionError("connexion refusée (simulée)")
        return _Resp(self.payload(params))

    def close(self):
        pass


def universe(n=1000):
    return [make_record(i) for i in range(n)]


# OPT-12 : config.ISIDORE_REPLIES_PER_PAGE vaut 200, la fenêtre entière,
# servie en UNE requête. Les tests qui décrivent la mécanique multi-pages du
# flux (page répétée, bloc ``page``, page ramenée, sous-source trouvée page 2,
# borne franchie, erreur sur la page 2) fixent explicitement des pages de 100 :
# cette mécanique reste celle du code (taille surchargeable par
# MOISSON_ISIDORE_REPLIES_PER_PAGE, fenêtre levée ou désactivée).
@pytest.fixture
def pages_of_100(monkeypatch):
    monkeypatch.setattr(config, "ISIDORE_REPLIES_PER_PAGE", 100, raising=False)


# Taille de page et pages attendues pour une fenêtre de 200 : défaut OPT-12
# (une requête) et pages de 100 (deux requêtes).
PAGE_SIZES = [(200, [1]), (100, [1, 2])]


def _baseline_select(records, marker, max_results, from_year=None, to_year=None):
    """
    Sélection de l'algorithme d'avant chantier sur un flux sans répétition :
    les ``max_results`` premières notices, dans l'ordre, dont une URL contient
    le marqueur et dont l'année passe le filtre.
    """
    out = []
    for rec in records:
        iso = rec["isidore"]
        urls = [iso.get("url", "")] + [a.get("$", "") for a in iso["ore"]["aggregates"]]
        if marker and not any(marker in u for u in urls):
            continue
        year = int(iso["date"]["year"])
        if from_year and year < from_year:
            continue
        if to_year and year > to_year:
            continue
        out.append(rec["@uri"])
        if len(out) >= max_results:
            break
    return out


def _uris(items):
    out = []
    for it in items:
        for line in it.get("extra", "").split("\n"):
            if line.startswith("Isidore URI:"):
                out.append(line.rsplit("/resource/", 1)[1])
    return out


def run_all(http, max_results=200, from_date=None, to_date=None, workers=1,
            barrier=True, **connector_kwargs):
    """Source Isidore + cinq sous-sources, en séquentiel ou en threads."""
    classes = [IsidoreConnector] + [cls for cls, _, _ in PROXIES]
    kwargs = {}
    if from_date:
        kwargs["from_date"] = from_date
    if to_date:
        kwargs["to_date"] = to_date
    results = {}
    if workers <= 1:
        for cls in classes:
            c = cls(http_client=http, **connector_kwargs)
            results[cls.__name__] = (c.search("discours numérique", max_results=max_results,
                                              **kwargs), c)
        return results
    start = threading.Barrier(len(classes)) if barrier else None

    def one(cls):
        c = cls(http_client=http, **connector_kwargs)
        if start is not None:
            start.wait(timeout=5)
        return cls.__name__, c.search("discours numérique", max_results=max_results,
                                      **kwargs), c

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, items, c in pool.map(one, classes):
            results[name] = (items, c)
    return results


# === Une page utile = un téléchargement ===

@pytest.mark.parametrize("replies,pages", PAGE_SIZES)
def test_sequential_run_downloads_each_useful_page_once(monkeypatch, replies, pages):
    """
    Baseline : chaque proxy repaginait la requête et retéléchargeait la
    dernière fenêtre jusqu'à 210 pages (C09, C10, C35). OPT-12 : la fenêtre
    tient en une requête (replies=200).
    """
    monkeypatch.setattr(config, "ISIDORE_REPLIES_PER_PAGE", replies, raising=False)
    fake = FakeIsidore(universe(5000), window=200)
    results = run_all(fake, max_results=200, from_date="2018")
    assert fake.calls == pages
    for name, (items, _) in results.items():
        assert len(set(_uris(items))) == len(items), name  # aucune notice répétée


@pytest.mark.parametrize("replies,pages", PAGE_SIZES)
def test_threaded_run_downloads_each_useful_page_once(monkeypatch, replies, pages):
    monkeypatch.setattr(config, "ISIDORE_REPLIES_PER_PAGE", replies, raising=False)
    fake = FakeIsidore(universe(5000), window=200, latency=0.05)
    results = run_all(fake, max_results=200, from_date="2018", workers=6)
    assert sorted(fake.calls) == pages
    assert len(results) == 6


@pytest.mark.parametrize("workers", [1, 6])
def test_same_items_in_sequence_and_in_threads(workers):
    fake = FakeIsidore(universe(5000), window=200, latency=0.01)
    results = run_all(fake, max_results=30, workers=workers)
    expected = run_all(FakeIsidore(universe(5000), window=200), max_results=30)
    for name in expected:
        assert _uris(results[name][0]) == _uris(expected[name][0]), name


def test_no_cache_option_is_used_for_normal_pages():
    """Le partage ne dépend pas du cache HTTP : pages normales, cache par défaut."""
    fake = FakeIsidore(universe(500), window=200)
    run_all(fake, max_results=50)
    assert all("use_cache" not in kw for kw in fake.kwargs_seen)


# === Fin de fenêtre ===

def test_repeated_pages_stop_the_scan_without_page_block(monkeypatch, pages_of_100):
    """Sans bloc `page` ni règle de fenêtre : arrêt à la première page répétée."""
    monkeypatch.setattr(config, "ISIDORE_RESULT_WINDOW", 0, raising=False)
    fake = FakeIsidore(universe(5000), window=200, page_block=False)
    c = IsidoreConnector(http_client=fake)
    items = c.search("q", max_results=1000)
    assert fake.calls == [1, 2, 3]  # la page 3 répète la page 2 : arrêt
    assert len(items) == len(set(_uris(items))) == 200
    st = c.last_scan_status
    assert st.end_reason == "repeat"
    assert st.window_reached and st.incomplete and not st.cap_reached


def test_page_block_without_next_stops_before_the_repeated_page(monkeypatch, pages_of_100):
    """Bloc `page` sans @next (live : dernière page servie) : pas de page 3."""
    monkeypatch.setattr(config, "ISIDORE_RESULT_WINDOW", 0, raising=False)
    fake = FakeIsidore(universe(5000), window=200, page_block=True)
    c = IsidoreConnector(http_client=fake)
    c.search("q", max_results=1000)
    assert fake.calls == [1, 2]
    assert c.last_scan_status.end_reason == "window"


def test_clamped_current_page_is_detected(monkeypatch, pages_of_100):
    """
    Page servie différente de la page demandée (@current) : fin de fenêtre,
    même si la page apporte encore des notices nouvelles.
    """
    monkeypatch.setattr(config, "ISIDORE_RESULT_WINDOW", 0, raising=False)
    fake = FakeIsidore(universe(5000), window=0, page_block=False)
    orig = fake.payload

    def payload(params):
        data = orig(params)
        page = int(params["page"])
        data["response"]["replies"]["page"] = {"@current": str(min(page, 2)),
                                               "@next": str(page + 1)}
        return data

    fake.payload = payload
    c = IsidoreConnector(http_client=fake)
    items = c.search("q", max_results=1000)
    assert fake.calls == [1, 2, 3]
    assert len(items) == 300
    assert c.last_scan_status.end_reason == "window"


@pytest.mark.parametrize("replies,pages", PAGE_SIZES)
def test_window_constant_stops_at_the_verified_window(monkeypatch, replies, pages):
    monkeypatch.setattr(config, "ISIDORE_REPLIES_PER_PAGE", replies, raising=False)
    fake = FakeIsidore(universe(5000), window=200, page_block=False)
    c = IsidoreConnector(http_client=fake)
    c.search("q", max_results=1000)
    assert fake.calls == pages
    assert c.last_scan_status.end_reason == "window"
    assert c.last_scan_status.total_available == 5000


@pytest.mark.parametrize("replies,pages", PAGE_SIZES)
def test_small_result_set_is_exhausted_not_incomplete(monkeypatch, replies, pages):
    monkeypatch.setattr(config, "ISIDORE_REPLIES_PER_PAGE", replies, raising=False)
    fake = FakeIsidore(universe(150), window=200)
    c = CairnConnector(http_client=fake)
    items = c.search("q", max_results=200)
    st = c.last_scan_status
    assert fake.calls == pages
    assert st.end_reason == "exhausted"
    assert not st.incomplete
    assert len(items) == st.retained


def test_incomplete_subsource_is_logged(caplog):
    fake = FakeIsidore(universe(5000), window=200)
    with caplog.at_level(logging.WARNING):
        run_all(fake, max_results=200, from_date="2018")
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    cairn = [m for m in msgs if m.startswith("[Cairn]")]
    assert cairn and "fenêtre de résultats du fournisseur atteinte" in cairn[0]
    assert "5000 annoncés" in cairn[0] and "sous-source incomplète" in cairn[0]


# === Identifiants retenus identiques à l'algorithme historique ===

@pytest.mark.parametrize("from_date,to_date,cap", [
    (None, None, 40), ("2018", None, 25), ("2012", "2014", 7), (None, None, 1000),
])
def test_retained_ids_match_the_historical_algorithm_without_repetition(
        monkeypatch, from_date, to_date, cap):
    monkeypatch.setattr(config, "ISIDORE_RESULT_WINDOW", 0, raising=False)
    records = universe(1200)
    fake = FakeIsidore(records, window=0)
    results = run_all(fake, max_results=cap, from_date=from_date, to_date=to_date)
    fy = int(from_date) if from_date else None
    ty = int(to_date) if to_date else None
    assert _uris(results["IsidoreConnector"][0]) == _baseline_select(records, None, cap, fy, ty)
    for cls, marker, _label in PROXIES:
        got = _uris(results[cls.__name__][0])
        assert got == _baseline_select(records, marker, cap, fy, ty), cls.__name__
    # Univers entièrement lu au plus une fois : 12 pages de 100 (+0 répétée)
    assert fake.calls == sorted(set(fake.calls))
    assert len(fake.calls) <= 12


def test_subsource_found_after_the_direct_source_cap(pages_of_100):
    """La source directe s'arrête page 1 ; Cairn n'apparaît qu'en page 2."""
    records = [make_record(i, host="hal.science") for i in range(150)]
    records += [make_record(150 + i, host="shs.cairn.info") for i in range(50)]
    fake = FakeIsidore(records, window=200, total=9999)
    iso = IsidoreConnector(http_client=fake)
    assert len(iso.search("q", max_results=50)) == 50
    assert fake.calls == [1]
    cairn = CairnConnector(http_client=fake)
    items = cairn.search("q", max_results=10)
    assert fake.calls == [1, 2]
    assert _uris(items) == [f"10670/1.w{150 + i}" for i in range(10)]
    assert cairn.last_scan_status.cap_reached
    assert cairn.last_scan_status.pages_downloaded == 1
    assert iso.last_scan_status.cap_reached and not iso.last_scan_status.incomplete


# === Borne de sécurité ===

def test_scan_bound_is_respected_and_reported(monkeypatch, caplog, pages_of_100):
    monkeypatch.setattr(config, "ISIDORE_RESULT_WINDOW", 0, raising=False)
    monkeypatch.setattr(config, "ISIDORE_SCAN_MAX_RECORDS", 250, raising=False)
    fake = FakeIsidore(universe(10000), window=0)
    c = CairnConnector(http_client=fake)
    with caplog.at_level(logging.WARNING):
        items = c.search("q", max_results=1000)
    st = c.last_scan_status
    assert fake.calls == [1, 2, 3]
    assert st.scanned == 250 and st.flow_records == 250
    assert st.bound_reached and st.incomplete and st.end_reason == "bound"
    assert len(items) == len([u for u in _baseline_select(fake.records[:250], "cairn.info", 1000)])
    assert any("ISIDORE_SCAN_MAX_RECORDS" in r.getMessage() for r in caplog.records)


# === Filtre de domaine strict ===

@pytest.mark.parametrize("url,expected", [
    ("https://www.cairn.info/revue-x.htm", True),
    ("https://shs.cairn.info/revue-x?lang=fr", True),
    ("https://CAIRN.INFO:443/revue", True),
    ("www.cairn.info/revue-x.htm", True),
    ("https://cairn.info./revue", True),
    ("https://notcairn.info/revue", False),
    ("https://notcairn.info.example.com/revue", False),
    ("https://cairn.info.example.com/revue", False),
    ("https://example.com/?u=https://www.cairn.info/x", False),
    ("https://example.com/cairn.info/x", False),
    ("https://cairn.info@evil.example.com/x", False),
    ("", False),
])
def test_url_marker_matches_parsed_host_only(url, expected):
    record = {"isidore": {"url": url}}
    assert IsidoreConnector._url_matches(record, "cairn.info") is expected


def test_url_marker_checks_aggregates():
    record = {"isidore": {
        "url": "https://doi.org/10.4000/lidil.6033",
        "ore": {"aggregates": [{"$": "https://hdl.handle.net/20.500.13089/hzcj"},
                               {"$": "https://journals.openedition.org/lidil/6033"}]}}}
    assert IsidoreConnector._url_matches(record, "openedition.org")
    assert not IsidoreConnector._url_matches(record, "cairn.info")
    single = {"isidore": {"url": "", "ore": {"aggregates": {"$": "https://www.persee.fr/doc/x"}}}}
    assert IsidoreConnector._url_matches(single, "persee.fr")


def test_substring_lookalikes_are_not_retained_by_a_subsource():
    records = [make_record(0, host="notcairn.info"),
               make_record(1, host="cairn.info.example.com"),
               make_record(2, host="www.cairn.info")]
    records[0]["isidore"]["ore"]["aggregates"].append(
        {"$": "https://example.com/?ref=cairn.info"})
    fake = FakeIsidore(records, window=200)
    items = CairnConnector(http_client=fake).search("q", max_results=10)
    assert _uris(items) == ["10670/1.w2"]


# === Portée par exécution ===

def test_two_http_clients_share_nothing():
    from connectors.isidore_scan import registry_for
    records = universe(500)
    fake_a = FakeIsidore(records, window=200)
    fake_b = FakeIsidore(records, window=200)
    assert registry_for(fake_a) is not registry_for(fake_b)
    run_all(fake_a, max_results=50)
    run_all(fake_b, max_results=50)
    # Chaque client télécharge sa fenêtre, une seule fois (OPT-12 : une
    # requête de 200 ; auparavant deux pages de 100).
    assert fake_a.calls == [1] and fake_b.calls == [1]


def test_real_http_clients_do_not_exchange_flows(monkeypatch):
    """Deux HTTPClient réels (sans cache) : chacun télécharge ses pages."""
    import utils.http as http_mod
    from utils.http import HTTPClient

    monkeypatch.setitem(http_mod.HOST_MIN_INTERVALS, "api.isidore.science", 0.0)
    http_mod.reset_http_policy()
    records = universe(500)

    class _Adapter(HTTPAdapter):
        def __init__(self):
            super().__init__()
            self.pages = []
            self.fake = FakeIsidore(records, window=200)

        def send(self, request, **kwargs):
            params = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
            self.pages.append(int(params["page"]))
            body = json.dumps(self.fake.payload(params)).encode("utf-8")
            raw = HTTPResponse(body=io.BytesIO(body),
                               headers={"Content-Type": "application/json"},
                               status=200, preload_content=False,
                               request_url=request.url)
            return self.build_response(request, raw)

    clients, adapters = [], []
    try:
        for _ in range(2):
            client = HTTPClient(use_cache=False)
            adapter = _Adapter()
            client.session.mount("https://", adapter)
            clients.append(client)
            adapters.append(adapter)
            run_all(client, max_results=200, from_date="2018")
        # OPT-12 : la fenêtre en une requête par client (auparavant [1, 2]).
        assert adapters[0].pages == [1]
        assert adapters[1].pages == [1]
    finally:
        for client in clients:
            client.close()
        http_mod.reset_http_policy()


def test_explicit_registry_is_shared_across_clients():
    from connectors.isidore_scan import ScanRegistry
    records = universe(500)
    reg = ScanRegistry()
    fake_a = FakeIsidore(records, window=200)
    fake_b = FakeIsidore(records, window=200)
    IsidoreConnector(http_client=fake_a, scan_registry=reg).search("q", max_results=50)
    c = CairnConnector(http_client=fake_b, scan_registry=reg)
    c.search("q", max_results=5)
    assert fake_a.calls == [1] and fake_b.calls == []
    assert c.last_scan_status.shared_flow


def test_registry_is_released_with_its_http_client():
    from connectors import isidore_scan
    fake = FakeIsidore(universe(300), window=200)
    run_all(fake, max_results=20)
    # Registres orphelins des tests précédents libérés d'abord : seule la
    # libération de celui de ``fake`` est mesurée.
    gc.collect()
    before = len(isidore_scan._REGISTRIES)
    assert fake in isidore_scan._REGISTRIES
    del fake
    gc.collect()
    assert len(isidore_scan._REGISTRIES) == before - 1


def test_distinct_queries_have_distinct_flows():
    fake = FakeIsidore(universe(300), window=200)
    a = IsidoreConnector(http_client=fake)
    a.search("q1", max_results=5)
    a.search("q2", max_results=5)
    b = IsidoreConnector(http_client=fake)
    b.search("q1", max_results=5)
    assert fake.calls == [1, 1]
    assert b.last_scan_status.shared_flow is True
    assert a.last_scan_status.shared_flow is False


def test_zero_cap_makes_no_request():
    fake = FakeIsidore(universe(300), window=200)
    assert IsidoreConnector(http_client=fake).search("q", max_results=0) == []
    assert fake.calls == []


# === Erreurs ===

def test_non_json_body_is_reported_in_the_status(caplog, pages_of_100):
    fake = FakeIsidore(universe(5000), window=200, faults={2: ["html", "html"]})
    with caplog.at_level(logging.WARNING):
        results = run_all(fake, max_results=200)
    assert fake.calls == [1, 2, 2]  # une nouvelle tentative, puis l'erreur est partagée
    for name, (items, c) in results.items():
        st = c.last_scan_status
        assert st.error and "non JSON" in st.error and "page 2" in st.error, name
        assert st.end_reason == "error" and st.incomplete
        assert "text/html" in st.error
    items, _ = results["IsidoreConnector"]
    assert len(items) == 100  # la page 1 reste acquise
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("non JSON" in r.getMessage() for r in errors)


def test_non_json_body_once_is_retried_without_cache(pages_of_100):
    fake = FakeIsidore(universe(5000), window=200, faults={2: ["html"]})
    results = run_all(fake, max_results=200)
    assert fake.calls == [1, 2, 2]
    assert fake.kwargs_seen[2] == {"use_cache": False}
    items, c = results["IsidoreConnector"]
    assert len(items) == 200 and c.last_scan_status.error is None


def test_network_error_is_reported_once_for_all_consumers():
    fake = FakeIsidore(universe(5000), window=200, faults={1: ["error"]})
    results = run_all(fake, max_results=50)
    assert fake.calls == [1]
    for name, (items, c) in results.items():
        assert items == []
        assert "ConnectionError" in c.last_scan_status.error, name


# === Copies et annotations ===

def test_consumers_get_deep_copies_and_their_own_sources_line():
    records = [make_record(i, host="journals.openedition.org") for i in range(20)]
    fake = FakeIsidore(records, window=200)
    iso_items = IsidoreConnector(http_client=fake).search("q", max_results=5)
    oe_items = OpenEditionConnector(http_client=fake).search("q", max_results=5)
    again = IsidoreConnector(http_client=fake).search("q", max_results=5)
    assert _uris(iso_items) == _uris(oe_items)
    assert iso_items[0]["extra"].splitlines()[0] == "Sources: Isidore"
    assert oe_items[0]["extra"].splitlines()[0] == "Sources: OpenEdition, Isidore"
    assert again[0]["extra"].splitlines()[0] == "Sources: Isidore"
    assert iso_items[0] is not again[0]
    iso_items[0]["creators"].append({"lastName": "X"})
    assert len(again[0]["creators"]) == 1


def test_proxies_expose_the_scan_status():
    fake = FakeIsidore(universe(300), window=200)
    for cls, _marker, label in PROXIES:
        c = cls(http_client=fake)
        c.search("q", max_results=3)
        st = c.last_scan_status
        assert st is not None and st.source == label
        d = st.as_dict()
        assert {"pages_read", "scanned", "retained", "window_reached", "bound_reached",
                "incomplete", "error"} <= set(d)


# === Registre de longue durée ===

def test_registry_forgets_least_recently_used_flows():
    from connectors.isidore_scan import ScanRegistry
    reg = ScanRegistry()
    reg.max_flows = 3
    fake = FakeIsidore(universe(50), window=200)
    for q in ("q0", "q1", "q2", "q3"):
        IsidoreConnector(http_client=fake, scan_registry=reg).search(q, max_results=5)
    assert len(reg) == 3
    assert sorted(s.query for s in reg.flows()) == ["q1", "q2", "q3"]


def test_flow_older_than_max_age_is_recreated():
    from connectors.isidore_scan import ScanRegistry
    reg = ScanRegistry()
    reg.max_age_s = 0.0
    fake = FakeIsidore(universe(50), window=200)
    c = IsidoreConnector(http_client=fake, scan_registry=reg)
    c.search("q", max_results=5)
    c.search("q", max_results=5)
    assert fake.calls == [1, 1]
    assert c.last_scan_status.shared_flow is False


def test_page_size_above_the_window_is_one_request_and_reported_as_window():
    """replies=250 : Isidore sert 200 notices (live) ; fenêtre, pas épuisement."""
    from connectors.isidore_scan import ScanRegistry
    fake = FakeIsidore(universe(5000), window=200)
    c = IsidoreConnector(http_client=fake, scan_registry=ScanRegistry(replies=250))
    items = c.search("q", max_results=1000)
    assert fake.calls == [1]
    assert len(items) == 200
    st = c.last_scan_status
    assert st.end_reason == "window" and st.incomplete
