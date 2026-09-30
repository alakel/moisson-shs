"""
Tests du banc de mesure OPT-00 (``scripts/benchmark_moisson.py``,
``scripts/bench/``).

Aucun appel réseau : une garde remplace ``socket.socket.connect`` (et
``create_connection``) pour lever à la moindre tentative de connexion réelle ;
le simulateur d'API répond au niveau de ``requests.adapters.HTTPAdapter.send``.

Couvre :
- la sémantique de pagination simulée de HAL (``cursorMark``, ``start/rows``,
  ``fq`` de dates, ``fl`` avec ``score``) et d'Isidore (``page``,
  ``replies`` plafonné, objet unique, domaines des sous-sources) ;
- les contrats des autres fournisseurs exercés par les connecteurs (select
  CrossRef validé, ``per-page`` OpenAlex borné, jeton S2, Bearer CORE) ;
- l'injection de pannes et la distinction reprise (même appel logique) /
  appel répété après échec (autre appel) / téléchargement redondant ;
- une erreur de lecture du corps relevée comme sans banc (revue d'OPT-00) ;
- la vérité terrain (doublons résiduels, fusions erronées) et la couverture
  par identifiants (travaux perdus et gagnés dans ``--compare``) ;
- ``--compare`` : conditions de mesure (``sleep_scale``, ``workers``) et
  réserves ; ``--merge --summary-only`` sans chemin absolu ;
- la liaison par signature de ``_run_moisson_sync`` (erreur explicite) ;
- un scénario de bout en bout, dans un processus isolé, en quelques secondes,
  sans socket réelle et sans écrire dans ``output/`` du projet.
"""

from __future__ import annotations

import io
import json
import re
import socket
import sys
import time
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bench import analysis, synth  # noqa: E402
from bench.instrument import (  # noqa: E402
    BenchError, NetworkRecorder, install_network_layer,
)
from bench.simulator import ApiSimulator  # noqa: E402

HAL_URL = "https://api.archives-ouvertes.fr/search/"
ISIDORE_URL = "https://api.isidore.science/resource/search"


# === Garde : aucune socket réelle pendant ces tests ===

class _RealSocketAttempt(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_real_socket(monkeypatch):
    """Toute tentative de connexion réseau réelle fait échouer le test."""
    def refuse(*args, **kwargs):
        raise _RealSocketAttempt(f"connexion réelle tentée : {args[1:] or args}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _sim(**config):
    base = {"universe": {"seed": "test", "size": 2500,
                         "subsource_weights": {"openedition": 0.4, "cairn": 0.2,
                                               "hal": 0.3, "autre": 0.1}},
            "providers": {"hal": {"target_total": 400}}}
    base.update(config)
    return ApiSimulator(base)


@pytest.fixture
def simulated(monkeypatch, tmp_path):
    """
    Installe un simulateur sous requests ; renvoie une fonction d'installation
    qui rend les compteurs. Le cache disque du schéma Zotero est redirigé vers
    ``tmp_path`` : un téléchargement servi par le simulateur n'écrit jamais
    dans le cache personnel du poste.
    """
    import mappers.zotero_schema_loader as loader
    monkeypatch.setattr(loader, "CACHE_PATH", tmp_path / "zotero_schema.json",
                        raising=False)
    installed = []

    def install(sim):
        rec = NetworkRecorder(parse_records=False)
        installed.append(install_network_layer(rec, inner_send=sim.send))
        return rec

    yield install
    for restore in installed:
        restore()


# === HAL (Solr) ===

def test_hal_cursor_pagination_is_complete_ordered_and_filtered(simulated):
    sim = _sim()
    simulated(sim)
    session = requests.Session()
    params = {"q": "discours", "fl": "docid,title_s,producedDateY_i", "wt": "json",
              "rows": 70, "sort": "docid asc", "cursorMark": "*",
              "fq": "producedDate_tdate:[2018-01-01T00:00:00Z TO NOW]"}
    seen, pages, num_found = [], 0, None
    while True:
        data = session.get(HAL_URL, params=params).json()
        pages += 1
        num_found = data["response"]["numFound"]
        docs = data["response"]["docs"]
        if not docs:
            assert data["nextCursorMark"] == params["cursorMark"]  # sémantique Solr
            break
        assert all(set(d) <= {"docid", "title_s", "producedDateY_i"} for d in docs)
        assert all(d["producedDateY_i"] >= 2018 for d in docs)
        seen.extend(int(d["docid"]) for d in docs)
        params["cursorMark"] = data["nextCursorMark"]
    assert num_found > 70  # plusieurs pages
    assert len(seen) == num_found == len(set(seen))
    assert seen == sorted(seen)  # docid asc
    assert pages == -(-num_found // 70) + 1  # dernière page vide incluse


def test_hal_start_rows_score_desc_and_cursor_requires_docid(simulated):
    sim = _sim()
    simulated(sim)
    data = requests.get(HAL_URL, params={"q": "x", "fl": "docid,score", "rows": 50,
                                         "start": 0, "sort": "score desc"}).json()
    scores = [d["score"] for d in data["response"]["docs"]]
    assert len(scores) == 50 and scores == sorted(scores, reverse=True)
    page2 = requests.get(HAL_URL, params={"q": "x", "fl": "docid,score", "rows": 50,
                                          "start": 50, "sort": "score desc"}).json()
    assert page2["response"]["docs"][0]["score"] <= scores[-1]
    # Comme Solr : cursorMark sans tri sur la clé unique -> 400.
    r = requests.get(HAL_URL, params={"q": "x", "rows": 10, "sort": "score desc",
                                      "cursorMark": "*"})
    assert r.status_code == 400


def test_hal_records_are_parsed_by_the_real_mapper(simulated):
    from mappers.hal_mapper import map_hal_to_zotero
    sim = _sim()
    simulated(sim)
    docs = requests.get(HAL_URL, params={"q": "x", "rows": 20, "sort": "docid asc",
                                         "cursorMark": "*"}).json()["response"]["docs"]
    items = [map_hal_to_zotero(d) for d in docs]
    assert all(it["title"] and it["creators"] for it in items)
    assert all("HAL ID: hal-4" in it["extra"] for it in items)


# === Isidore ===

def test_isidore_page_replies_semantics(simulated):
    # Sans fenêtre de résultats (isidore_max_window=0) : pagination complète.
    sim = _sim(universe={"seed": "iso", "size": 250}, isidore_max_window=0)
    simulated(sim)
    first = requests.get(ISIDORE_URL, params={"q": "x", "replies": 500, "page": 1,
                                              "output": "json"}).json()
    replies = first["response"]["replies"]
    assert replies["meta"]["@items"] == "250"
    assert len(replies["content"]["reply"]) == 100  # replies plafonné à 100
    last = requests.get(ISIDORE_URL, params={"q": "x", "replies": 100, "page": 3,
                                             "output": "json"}).json()
    assert len(last["response"]["replies"]["content"]["reply"]) == 50  # page partielle
    beyond = requests.get(ISIDORE_URL, params={"q": "x", "replies": 100, "page": 4}).json()
    assert not beyond["response"]["replies"]["content"]
    # Particularité de l'API : un résultat unique n'est pas enveloppé en liste.
    one = requests.get(ISIDORE_URL, params={"q": "x", "replies": 1, "page": 7}).json()
    assert isinstance(one["response"]["replies"]["content"]["reply"], dict)
    # Pagination stable : pages disjointes et contiguës.
    uris = []
    for page in (1, 2, 3):
        data = requests.get(ISIDORE_URL, params={"q": "x", "replies": 100, "page": page}).json()
        uris.extend(r["@uri"] for r in data["response"]["replies"]["content"]["reply"])
    assert len(uris) == len(set(uris)) == 250


def test_isidore_serves_only_a_200_result_window_like_the_live_api(simulated):
    """
    Comportement relevé en live le 2026-09-28 : ``@items`` annonce le total
    réel, mais seuls les 200 premiers résultats sont servis ; avec
    ``replies=100``, toutes les pages >= 2 renvoient les résultats 101 à 200 ;
    avec ``replies=5``, la page 41 répète la page 40.
    """
    sim = _sim(universe={"seed": "fenetre", "size": 5000})
    simulated(sim)

    def uris(replies, page):
        data = requests.get(ISIDORE_URL, params={"q": "x", "replies": replies,
                                                 "page": page}).json()
        rep = data["response"]["replies"]
        assert rep["meta"]["@items"] == "5000"
        reply = rep["content"]["reply"]
        return [r["@uri"] for r in (reply if isinstance(reply, list) else [reply])]

    p1, p2, p3, p30 = (uris(100, p) for p in (1, 2, 3, 30))
    assert not set(p1) & set(p2)
    assert p2 == p3 == p30
    assert uris(5, 41) == uris(5, 40) == p2[-5:]
    assert uris(50, 3) == p2[:50]


def test_isidore_subsource_domains_follow_configured_weights(simulated):
    from connectors.isidore import IsidoreConnector
    sim = _sim(universe={"seed": "dom", "size": 2000,
                         "subsource_weights": {"persee": 0.25, "erudit": 0.25,
                                               "theses": 0.25, "cairn": 0.25}})
    simulated(sim)
    data = requests.get(ISIDORE_URL, params={"q": "x", "replies": 100, "page": 1}).json()
    records = data["response"]["replies"]["content"]["reply"]
    counts = {m: sum(1 for r in records if IsidoreConnector._url_matches(r, m))
              for m in ("persee.fr", "erudit.org", "theses.fr", "cairn.info",
                        "openedition.org")}
    assert counts["openedition.org"] == 0
    assert sum(counts.values()) == 100
    assert all(10 <= counts[m] <= 40 for m in ("persee.fr", "erudit.org",
                                               "theses.fr", "cairn.info"))


def test_isidore_records_are_parsed_by_the_real_mapper(simulated):
    from mappers.isidore_mapper import map_isidore_to_zotero
    sim = _sim()
    simulated(sim)
    data = requests.get(ISIDORE_URL, params={"q": "x", "replies": 30, "page": 1}).json()
    items = [map_isidore_to_zotero(r) for r in data["response"]["replies"]["content"]["reply"]]
    assert all(it["title"] and it["creators"] and it["date"] for it in items)
    assert all("20.500.13089/simw" in it["extra"] for it in items)


# === Autres fournisseurs ===

def test_other_providers_contracts(simulated):
    sim = ApiSimulator({"universe": {"seed": "intl", "size": 3000,
                                     "subsource_weights": {"autre": 1.0},
                                     "doi_rates": {"autre": 0.9}}})
    simulated(sim)
    # CrossRef : champ select inconnu -> 400 ; curseur jusqu'à épuisement.
    bad = requests.get("https://api.crossref.org/works",
                       params={"query.bibliographic": "x", "select": "DOI,language"})
    assert bad.status_code == 400
    ok = requests.get("https://api.crossref.org/works",
                      params={"query.bibliographic": "x", "rows": 5, "cursor": "*",
                              "select": "DOI,title,score"}).json()["message"]
    assert len(ok["items"]) == 5 and ok["next-cursor"]
    assert all(set(it) <= {"DOI", "title", "score"} for it in ok["items"])
    # OpenAlex : clé exigée, per-page borné à 200.
    assert requests.get("https://api.openalex.org/works",
                        params={"search": "x"}).status_code == 403
    assert requests.get("https://api.openalex.org/works",
                        params={"search": "x", "api_key": "k", "per-page": 250}).status_code == 400
    oa = requests.get("https://api.openalex.org/works",
                      params={"search": "x", "api_key": "k", "per-page": 200,
                              "cursor": "*", "filter": "publication_year:2018-2026"}).json()
    assert len(oa["results"]) == 200 and oa["meta"]["next_cursor"]
    assert all(2018 <= r["publication_year"] <= 2026 for r in oa["results"])
    # Semantic Scholar bulk : 1000 par appel, jeton de continuation.
    s2 = requests.get("https://api.semanticscholar.org/graph/v1/paper/search/bulk",
                      params={"query": "x", "fields": "title,year"}).json()
    assert len(s2["data"]) == min(1000, s2["total"])
    # CORE : Bearer exigé.
    assert requests.get("https://api.core.ac.uk/v3/search/works",
                        params={"q": "x"}).status_code == 401


def test_forbidden_and_unknown_hosts_raise(simulated):
    sim = _sim()
    simulated(sim)
    with pytest.raises(requests.ConnectionError):
        requests.get("https://serpapi.com/search.json", params={"q": "x"})
    with pytest.raises(requests.ConnectionError):
        requests.get("https://example.invalid/")
    assert sim.snapshot()["forbidden"] == {"serpapi.com": 1}


# === Pannes et compteurs ===

def test_faults_retry_after_and_retry_vs_duplicate_classification(simulated):
    sim = _sim(faults=[{"host": "api.isidore.science", "kind": "status", "status": 429,
                        "retry_after": 3, "first": 1},
                       {"host": "api.archives-ouvertes.fr", "kind": "connect_error"}])
    rec = simulated(sim)
    params = {"q": "x", "replies": 10, "page": 1}
    r1 = requests.get(ISIDORE_URL, params=params)
    assert r1.status_code == 429 and r1.headers["Retry-After"] == "3"
    assert requests.get(ISIDORE_URL, params=params).status_code == 200  # reprise
    assert requests.get(ISIDORE_URL, params=params).status_code == 200  # redondant
    with pytest.raises(requests.ConnectionError):
        requests.get(HAL_URL, params={"q": "x"})
    snap = rec.snapshot()
    iso = snap["hosts"]["api.isidore.science"]
    assert iso["requests"] == 3 and iso["retries"] == 1 and iso["duplicate_fetches"] == 1
    assert iso["status"] == {"200": 2, "429": 1}
    assert snap["hosts"]["api.archives-ouvertes.fr"]["exceptions"] == {"ConnectionError": 1}


@pytest.mark.parametrize("k", [1, 3])
def test_bench_counts_exactly_the_attempts_made_on_a_definitive_404(simulated, k):
    """
    Mesure centrale de C06 : un client déterministe qui fait exactement k
    tentatives sur un 404 donne k requêtes, k - 1 reprises, k statuts 404.
    """
    sim = _sim(faults=[{"host": "api.archives-ouvertes.fr", "kind": "status", "status": 404}])
    rec = simulated(sim)
    session = requests.Session()
    for _ in range(k):  # boucle de retry d'un même appel logique
        assert session.get(HAL_URL, params={"q": "x"}).status_code == 404
    hal = rec.snapshot()["hosts"]["api.archives-ouvertes.fr"]
    assert hal["requests"] == k
    assert hal["retries"] == k - 1
    assert hal["repeats_after_failure"] == 0
    assert hal["status"] == {"404": k}


def test_http_client_attempts_on_a_definitive_404_are_consistently_counted(simulated):
    """
    Code mesuré (``HTTPClient``) : quelle que soit sa politique (3 tentatives
    avant chantier, 1 après OPT-03), toutes ses tentatives sont des reprises
    d'un même appel et toutes reçoivent le 404.
    """
    import utils.http as http_mod
    sim = _sim(faults=[{"host": "api.archives-ouvertes.fr", "kind": "status", "status": 404}])
    rec = simulated(sim)
    client = http_mod.HTTPClient(use_cache=False)
    original_sleep = time.sleep
    time.sleep = lambda s: None  # backoff instantané pour le test
    try:
        with pytest.raises(requests.HTTPError):
            client.get(HAL_URL, params={"q": "x"})
    finally:
        time.sleep = original_sleep
        client.close()
    hal = rec.snapshot()["hosts"]["api.archives-ouvertes.fr"]
    assert hal["requests"] >= 1
    assert hal["retries"] == hal["requests"] - 1
    assert hal["status"] == {"404": hal["requests"]}
    assert sim.snapshot()["requests"]["api.archives-ouvertes.fr"] == hal["requests"]


def test_independent_calls_on_the_same_url_are_not_retries(simulated):
    """
    Régression (revue d'OPT-00) : des appels indépendants sur la même URL
    après un échec (validations Zotero qui retéléchargent chacune le schéma,
    proxies Isidore successifs) ne sont pas des reprises ; seules les
    tentatives répétées au sein d'un même appel logique le sont.
    """
    from bench.instrument import StageRecorder
    sim = _sim(faults=[{"host": "api.zotero.org", "kind": "connect_error"}])
    rec = simulated(sim)
    stages = StageRecorder()
    rec.current_call = stages.current_call
    url = "https://api.zotero.org/schema"

    def fetch_once():  # une seule tentative par appel (chargeur de schéma)
        try:
            requests.get(url)
        except requests.ConnectionError:
            pass

    def fetch_with_retry():  # trois tentatives dans le même appel
        for _ in range(3):
            fetch_once()

    once = stages._make_wrapper(fetch_once, "chargement_schema", "plain")
    retrying = stages._make_wrapper(fetch_with_retry, "autre", "plain")
    for _ in range(3):
        once()
    zot = rec.snapshot()["hosts"]["api.zotero.org"]
    assert zot["requests"] == 3
    assert zot["retries"] == 0
    assert zot["repeats_after_failure"] == 2
    retrying()
    zot = rec.snapshot()["hosts"]["api.zotero.org"]
    assert zot["requests"] == 6
    assert zot["retries"] == 2
    assert zot["repeats_after_failure"] == 3  # 1re tentative du nouvel appel


# === Erreur pendant la lecture du corps (mode live) ===

class _BrokenBody(io.RawIOBase):
    """
    Corps coupé après ``cut`` octets, puis flux fermé (fin de fichier), comme
    ``http.client`` après une connexion interrompue en cours de transfert.
    """

    def __init__(self, data: bytes, cut: int):
        self.data, self.cut, self.pos, self.broken = data, cut, 0, False

    def readable(self):
        return True

    def readinto(self, buf):
        if self.pos >= self.cut:
            if self.broken:
                return 0
            self.broken = True
            raise ConnectionResetError("connexion coupée pendant le transfert")
        n = min(len(buf), self.cut - self.pos)
        buf[:n] = self.data[self.pos:self.pos + n]
        self.pos += n
        return n


def _flaky_server():
    """Envoi qui coupe le corps de la 1re réponse 200, puis répond normalement."""
    from urllib3.response import HTTPResponse
    calls = {"n": 0}

    def send(adapter, request, **kwargs):
        calls["n"] += 1
        body = b'{"ok": true, "pad": "' + b"x" * 5000 + b'"}'
        stream = (io.BufferedReader(_BrokenBody(body, 20)) if calls["n"] == 1
                  else io.BytesIO(body))
        raw = HTTPResponse(body=stream, headers={"Content-Type": "application/json"},
                           status=200, preload_content=False, decode_content=False,
                           request_method=request.method, request_url=request.url)
        return adapter.build_response(request, raw)

    return send, calls


@pytest.mark.parametrize("with_bench", [False, True])
def test_body_read_error_surfaces_exactly_as_without_the_bench(monkeypatch, with_bench):
    """
    Régression (revue d'OPT-00) : l'adaptateur compté lisait le corps et
    avalait l'erreur ; urllib3 ayant fermé le flux, requests relisait un corps
    vide et le code mesuré recevait un 200 sans exception, donc sans retry.
    Le banc doit relever l'erreur, et la compter comme exception, pas comme 200.
    """
    import requests.adapters
    send, calls = _flaky_server()
    rec = NetworkRecorder(parse_records=True)
    if with_bench:
        restore = install_network_layer(rec, inner_send=send)
    else:
        original = requests.adapters.HTTPAdapter.send
        requests.adapters.HTTPAdapter.send = send
        restore = lambda: setattr(requests.adapters.HTTPAdapter, "send", original)  # noqa: E731
    try:
        with pytest.raises(requests.exceptions.ChunkedEncodingError):
            requests.get(ISIDORE_URL, params={"q": "x"})
        assert requests.get(ISIDORE_URL, params={"q": "x"}).json()["ok"] is True
    finally:
        restore()
    assert calls["n"] == 2
    if with_bench:
        iso = rec.snapshot()["hosts"]["api.isidore.science"]
        assert iso["requests"] == 2
        assert iso["exceptions"] == {"ChunkedEncodingError": 1}
        assert iso["body_errors"] == 1
        assert iso["status"] == {"200": 1} and iso["ok"] == 1
        assert iso["retries"] == 1  # la 2e requête est bien une reprise


def test_http_client_retries_an_interrupted_body_as_often_with_the_bench(monkeypatch):
    """
    Même nombre de tentatives de ``HTTPClient`` avec et sans le banc sur un
    corps interrompu (le banc observe sans perturber le code mesuré).
    """
    import requests.adapters
    import utils.http as http_mod
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def attempts(with_bench):
        send, calls = _flaky_server()
        if with_bench:
            restore = install_network_layer(NetworkRecorder(), inner_send=send)
        else:
            original = requests.adapters.HTTPAdapter.send
            requests.adapters.HTTPAdapter.send = send
            restore = lambda: setattr(requests.adapters.HTTPAdapter, "send", original)  # noqa: E731
        client = http_mod.HTTPClient(use_cache=False)
        outcome = None
        try:
            outcome = client.get(ISIDORE_URL, params={"q": "x"}).json().get("ok")
        except requests.RequestException as e:
            outcome = type(e).__name__
        finally:
            client.close()
            restore()
        return calls["n"], outcome

    assert attempts(True) == attempts(False)


# === Univers et vérité terrain ===

def test_universe_is_deterministic_and_lazy():
    u1 = synth.Universe({"seed": "s", "size": 262129})
    u2 = synth.Universe({"seed": "s", "size": 262129})
    assert u1.work(123456) == u2.work(123456)
    assert u1.work(5)["title"] != u1.work(6)["title"]
    assert synth.hal_id(42) == "hal-40000042"


def test_ground_truth_detects_residual_duplicates_and_false_merges():
    items = [
        {"title": "A", "DOI": "10.5555/simw.1", "extra": "Sources: HAL"},
        {"title": "A", "extra": "Isidore URI: https://isidore.science/resource/20.500.13089/simw1"},
        {"title": "B", "extra": "HAL ID: hal-40000002\nOpenAlex ID: https://openalex.org/W8880000003"},
        {"title": "sans marqueur"},
    ]
    gt = analysis.gt_summary(items)
    assert gt["residual_duplicates"] == 1  # travail 1 dans deux notices
    assert gt["false_merges"] == 1  # travaux 2 et 3 dans une notice
    assert gt["unmarked_items"] == 1
    assert analysis.normalize_title("Écrire : le NUMÉRIQUE !") == "ecrire le numerique"


# === Liaison par signature ===

def test_mcp_signature_binding_reports_clear_errors():
    from bench.runner import bind_mcp_kwargs

    def current(query, max_per_source, sources, enrich_oai, do_dedup,
                from_date=None, to_date=None, cache=False, workers=1,
                progress=None, cancel_event=None):
        pass

    kwargs = bind_mcp_kwargs(current, {"query": "q", "max_per_source": 5,
                                       "sources": ["hal"], "enrich_oai": False,
                                       "do_dedup": True, "workers": None})
    assert kwargs == {"query": "q", "max_per_source": 5, "sources": ["hal"],
                      "enrich_oai": False, "do_dedup": True}

    def renamed(request, context=None):
        pass

    with pytest.raises(BenchError, match="Signature actuelle"):
        bind_mcp_kwargs(renamed, {"query": "q", "max_per_source": 5, "sources": []})


def test_import_tree_rejects_a_directory_that_is_not_a_copy(tmp_path):
    from bench.runner import import_tree
    with pytest.raises(BenchError, match="pas une copie"):
        import_tree(tmp_path, "cli")


# === Scénarios ===

def test_quota_429_scenario_sends_a_429_with_retry_after_to_isidore(simulated):
    """
    Régression (revue d'OPT-00) : la règle 429 d'Isidore (« every: 4 »
    placée après la règle 502) ne se déclenchait jamais. Isidore reçoit
    désormais un 429 (Retry-After: 1) à la 1re requête et un 502 à la 3e.
    """
    from bench import scenarios
    sim = ApiSimulator(scenarios.get("quota_429")["sim"])
    simulated(sim)
    params = {"q": "x", "replies": 5, "page": 1}
    first = requests.get(ISIDORE_URL, params=params)
    assert first.status_code == 429 and first.headers["Retry-After"] == "1"
    assert [requests.get(ISIDORE_URL, params=params).status_code
            for _ in range(3)] == [200, 502, 200]
    assert [requests.get(HAL_URL, params={"q": "x", "rows": 5}).status_code
            for _ in range(3)] == [429, 429, 200]


# === Agrégation et restitution ===

def _fake_run(duration, requests_, **extra):
    """Mesure brute minimale, de la forme produite par ``execute_run``."""
    run = {"duration_s": duration, "duration_adjusted_s": duration - 0.1,
           "network": {"total": {"requests": requests_, "retries": 0,
                                 "repeats_after_failure": 0,
                                 "duplicate_fetches": 2, "status_429": 0,
                                 "errors_http": 0, "exceptions": 0},
                       "hosts": {"api.isidore.science": {"requests": requests_,
                                                         "ok": requests_}},
                       "by_source": {"OpenEdition": {"api.isidore.science": 3}}},
           "sleeps": {"total_s": 1.0}, "items": {"returned_by_sources": 10,
                                                 "final_corpus": 8},
           "corpus": {"items": 8, "residual_title_year": 1},
           "stages": {"stages": {"deduplication": {"total_s": 0.01}},
                      "sources": [{"source": "OpenEdition", "items": 10,
                                   "duration_s": 0.5}]},
           "memory": {"maxrss_after_bytes": 1e8, "maxrss_delta_bytes": 1e7}}
    run.update(extra)
    return run


def test_summarize_and_table_handle_repeats_and_failures():
    import benchmark_moisson as bm
    run = _fake_run

    summary = bm.summarize([run(3.0, 5), run(1.0, 5), run(2.0, 6)])
    assert summary["duration_s"] == {"median": 2.0, "min": 1.0, "max": 3.0}
    assert summary["isidore_requests_by_source"] == {"OpenEdition": 3}
    assert summary["variable_counters"] == ["requests"]
    failed = bm.summarize([{"bench_error": "signature modifiée"}])
    table = bm.format_table([
        {"scenario": "a", "entry": "mcp", "mode": "sim", "latency_ms": 0.0, "summary": summary},
        {"scenario": "b", "entry": "cli", "mode": "live", "latency_ms": None, "summary": failed},
    ])
    assert "ÉCHEC : signature modifiée" in table
    assert "2.00 [1.00-3.00]" in table


def test_summary_keeps_caveats_and_never_relabels_initial_retries():
    """
    Réserves (moisson live tronquée) conservées dans le résumé et le
    tableau ; reprises mesurées avec la définition initiale (compteurs sans
    ``repeats_after_failure``) présentées sous ``retries_v1``, jamais comme
    ``retries``.
    """
    import benchmark_moisson as bm
    old = _fake_run(1.0, 5, caveats=["moisson tronquée par le banc"],
                    corrections={"raison": "instrumentation initiale"})
    old["network"]["total"].pop("repeats_after_failure")
    old["network"]["total"]["retries"] = 197
    summary = bm.summarize([old])
    assert summary["retries"] is None and summary["retries_v1"] == 197
    assert summary["counter_definition"] == 1
    assert summary["caveats"] == ["moisson tronquée par le banc"]
    assert summary["corrections"] == {"raison": "instrumentation initiale"}
    table = bm.format_table([{"scenario": "reference", "entry": "mcp", "mode": "live",
                              "latency_ms": None, "summary": summary}])
    assert "RÉSERVE : moisson tronquée par le banc" in table
    assert " 197* " in table
    assert bm.summarize([_fake_run(1.0, 5)])["retries"] == 0


def test_work_ids_encoding_roundtrip():
    ids = {0, 5, 6, 41, 266507, 1000000}
    rec = analysis.ids_record(ids)
    assert rec["count"] == 6
    assert analysis.decode_ids(rec["ids"]) == sorted(ids)
    assert analysis.decode_ids("") == [] and analysis.ids_record([])["count"] == 0
    assert rec["digest"] == analysis.ids_record(sorted(ids, reverse=True))["digest"]


def _results_file(path, groups, meta=None):
    path.write_text(json.dumps({"meta": meta or {}, "groups": groups}), encoding="utf-8")
    return path


def _group(sleep_scale, duration, requests_=7, workers=None, **summary):
    s = {"duration_s": {"median": duration}, "requests_total": requests_,
         "retries": 4, "caveats": []}
    s.update(summary)
    return {"scenario": "erreur_404", "entry": "mcp", "mode": "sim", "latency_ms": 0.0,
            "sleep_scale": sleep_scale, "workers": workers, "summary": s}


def _line(text, label):
    return next(ln for ln in text.splitlines() if ln.strip().startswith(label))


def test_compare_never_reports_durations_across_measurement_conditions(tmp_path):
    """
    Régression (revue d'OPT-00) : un essai rapide (--sleep-scale 0) comparé à
    la baseline (sleep_scale 1) affichait des gains de durée fictifs (x0,00).
    """
    import benchmark_moisson as bm
    base = _results_file(tmp_path / "base.json", [_group(1.0, 6.6508)])
    quick = _results_file(tmp_path / "quick.json", [_group(0.0, 0.0086)])
    out = bm.compare(base, quick)
    assert "ATTENTION" in out
    assert "(non comparable)" in _line(out, "durée médiane")
    assert "(x" not in _line(out, "durée médiane")
    assert "(x1.00)" in _line(out, "requêtes")  # compteurs : mêmes workers
    # Workers imposés différents : compteurs indicatifs, sans rapport.
    par = _results_file(tmp_path / "par.json", [_group(1.0, 3.0, workers=4)])
    out = bm.compare(base, par)
    assert "(x" not in _line(out, "requêtes")
    # Série de référence aux mêmes conditions présente : appariement exact.
    both = _results_file(tmp_path / "both.json", [_group(1.0, 6.6508), _group(0.0, 0.01)])
    out = bm.compare(both, quick)
    assert "ATTENTION" not in out
    assert "(x0.86)" in _line(out, "durée médiane")


def test_compare_old_files_without_conditions_use_meta_options(tmp_path):
    import benchmark_moisson as bm
    g = _group(None, 6.65)
    del g["sleep_scale"], g["workers"]
    base = _results_file(tmp_path / "old.json", [g],
                         meta={"options": {"sleep_scale": 1.0, "workers": None}})
    new = _results_file(tmp_path / "new.json", [_group(1.0, 3.3)])
    assert "(x0.50)" in _line(bm.compare(base, new), "durée médiane")
    merged_old = _results_file(tmp_path / "merged_old.json", [g], meta={"merged": []})
    out = bm.compare(merged_old, new)
    assert "inconnues" in out and "(non comparable)" in _line(out, "durée médiane")


def test_compare_shows_caveats_and_computes_no_ratio(tmp_path):
    import benchmark_moisson as bm
    base = _results_file(tmp_path / "b.json",
                         [_group(1.0, 381.1, caveats=["3 sources tronquées par le banc"])])
    new = _results_file(tmp_path / "n.json", [_group(1.0, 150.0)])
    out = bm.compare(base, new)
    assert "RÉSERVE (référence) : 3 sources tronquées par le banc" in out
    assert "(x" not in out


def test_compare_lists_lost_and_gained_works(tmp_path):
    """
    Couverture par identifiants (§7.1 « couverture identique ») : un échange
    de travaux à effectif constant est visible.
    """
    import benchmark_moisson as bm

    def cov(final, hal):
        return {"final": analysis.ids_record(final),
                "sources": {"HAL": analysis.ids_record(hal)}}

    base = _results_file(tmp_path / "b.json", [_group(1.0, 2.0, coverage=cov({1, 2, 3}, {1, 2}))])
    new = _results_file(tmp_path / "n.json", [_group(1.0, 2.0, coverage=cov({2, 3, 7}, {1, 2}))])
    out = bm.compare(base, new)
    assert "3 → 3  perdus 1 (ex. 1), gagnés 1" in _line(out, "couverture finale")
    assert "identique (1 sources)" in _line(out, "couverture par source")


def test_compare_takes_missing_counters_from_another_reference_series(tmp_path):
    """
    Reprises absentes de la série appariée (définition initiale) : reprises
    d'une autre série de référence du même scénario, signalées « * ».
    """
    import benchmark_moisson as bm
    full = _group(1.0, 6.65, retries=None, retries_v1=4)
    quick = _group(0.0, 0.01, retries=2, coverage={"final": analysis.ids_record({1}),
                                                   "sources": {}})
    base = _results_file(tmp_path / "b.json", [full, quick])
    new = _results_file(tmp_path / "n.json",
                        [_group(1.0, 0.02, retries=0,
                                coverage={"final": analysis.ids_record({1}), "sources": {}})])
    out = bm.compare(base, new)
    assert "2*" in _line(out, "reprises")
    assert "identique" in _line(out, "couverture finale")
    assert "* valeur de référence prise dans la série latence 0.0 ms, sleep_scale 0" in out


def test_merge_summary_only_writes_no_absolute_path(tmp_path):
    """
    Régression (revue d'OPT-00) : le résumé destiné au dépôt recopiait les
    chemins du répertoire de travail (copie mesurée, sortie JSON, travail).
    """
    import benchmark_moisson as bm
    work = tmp_path / "session" / "scratch"
    src = _results_file(tmp_path / "raw.json", [
        {"scenario": "rare", "entry": "mcp", "mode": "sim", "latency_ms": 0.0,
         "runs": [_fake_run(2.0, 9, log=str(work / "run.log"))]}],
        meta={"tree": str(work / "baseline_tree"), "tree_digest": "01496edb9c7a1d66",
              "options": {"tree": str(work / "baseline_tree"), "sleep_scale": 1.0,
                          "workers": None, "json_out": str(work / "r.json"),
                          "work_dir": str(work / "w"), "merge": [str(work / "a.json")]}})
    out = tmp_path / "summary.json"
    bm.merge_results([src], out, summary_only=True)
    text = out.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert not re.search(r'"(/|[A-Za-z]:\\\\)', text)
    data = json.loads(text)
    meta = data["meta"]["merged"][0]
    assert meta["tree"] == "baseline_tree" and meta["options"]["merge"] == ["a.json"]
    assert meta["tree_digest"] == "01496edb9c7a1d66"
    group = data["groups"][0]
    assert "runs" not in group
    assert group["sleep_scale"] == 1.0 and group["workers"] is None


def test_versioned_baseline_summary_is_neutral_and_complete():
    """
    Le résumé versionné : aucun chemin absolu ; conditions de mesure sur
    chaque groupe ; série rapide (sleep_scale 0) de tous les scénarios avec
    la couverture par identifiants ; réserves sur la moisson live tronquée.
    """
    from bench import scenarios
    path = PROJECT_ROOT / "tests" / "fixtures" / "bench" / "baseline_2026-09-28.summary.json"
    text = path.read_text(encoding="utf-8")
    assert not re.search(r'"/(private|Users|home|tmp|var)/', text)
    data = json.loads(text)
    groups = data["groups"]
    assert all("sleep_scale" in g and "workers" in g for g in groups)
    quick = [g for g in groups if g["mode"] == "sim" and g["sleep_scale"] == 0]
    assert {g["scenario"] for g in quick} == set(scenarios.names())
    assert all(g["summary"]["counter_definition"] == 2 for g in quick)
    for g in quick:
        if g["scenario"] == "reference":
            cov = g["summary"]["coverage"]
            assert cov["final"]["count"] > 0 and len(cov["sources"]) == 7
    live_ref = next(g for g in groups if g["mode"] == "live" and g["scenario"] == "reference")
    assert live_ref["summary"]["caveats"]
    q429 = [g for g in groups if g["scenario"] == "quota_429"]
    assert q429 and all(
        g["summary"]["requests_by_host"]["api.isidore.science"] == 4 for g in q429)


# === Mesure de l'arbre de travail (le banc y est contenu) ===

def test_tree_modules_excludes_the_bench_and_the_environment():
    """
    Mesurer la racine du dépôt ne doit instrumenter ni le banc (scripts/bench)
    ni les bibliothèques de l'environnement virtuel, même logé sous la racine.
    """
    import config  # noqa: F401 - module du code mesuré, chargé pour le test
    import bench.instrument as instrument
    from bench import runner

    mods = runner.tree_modules(PROJECT_ROOT)
    names = {m.__name__ for m in mods}
    assert "config" in names
    assert instrument not in mods
    assert not any(n == "bench" or n.startswith("bench.") for n in names)
    assert requests not in mods
    for mod in mods:
        parts = Path(mod.__file__).resolve().parts
        assert "site-packages" not in parts, mod.__name__


def test_sleep_recorder_at_real_scale_never_patches_itself(monkeypatch):
    """
    Régression : quand bench.instrument figurait parmi les modules mesurés,
    install() y remplaçait ORIGINAL_SLEEP par sleep() lui-même ; à
    --sleep-scale > 0, chaque attente partait en RecursionError, avalée par
    les connecteurs (sources vides, « sommeils » de plusieurs milliers de s).
    """
    import bench.instrument as instrument

    # Restaurés en fin de test, quoi que fasse install().
    monkeypatch.setattr(time, "sleep", time.sleep)
    monkeypatch.setattr(instrument, "ORIGINAL_SLEEP", instrument.ORIGINAL_SLEEP)
    recorder = instrument.SleepRecorder(scale=1.0)
    recorder.install([instrument])

    assert instrument.ORIGINAL_SLEEP is not recorder.sleep
    time.sleep(0.001)
    snap = recorder.snapshot()
    assert snap["calls"] == 1
    assert snap["total_s"] == pytest.approx(0.001)


# === Bout en bout, processus isolé ===

def test_smoke_scenario_end_to_end_without_network(tmp_path):
    """
    Petit scénario complet (HAL + Isidore + OpenEdition, CLI) dans un processus
    neuf : garde-socket active dans le processus de mesure, sortie dans
    ``tmp_path``, quelques secondes.
    """
    import argparse
    import benchmark_moisson as bm
    from bench import scenarios

    project_output = PROJECT_ROOT / "output"
    before = set(project_output.glob("*")) if project_output.is_dir() else set()
    opts = argparse.Namespace(workers=None, sleep_scale=0.0, tracemalloc=False,
                              trace=False, live_keys=False)
    spec = bm.make_spec("smoke", scenarios.get("smoke"), "cli", "sim", PROJECT_ROOT,
                        0.0, opts, tmp_path / "run")
    t0 = time.perf_counter()
    result = bm.run_in_subprocess(spec, timeout=120)
    elapsed = time.perf_counter() - t0

    assert not result.get("bench_error"), result.get("bench_error") or result.get("log_tail")
    assert result["error"] is None, result.get("entry_info")
    assert result["real_socket_attempts"] == []
    assert elapsed < 30
    assert Path(result["module_files"]["main"]).resolve() == (PROJECT_ROOT / "main.py").resolve()
    hosts = result["network"]["hosts"]
    # Flux Isidore partagé par Isidore et OpenEdition : la fenêtre en une
    # requête depuis OPT-12 (replies=200).
    assert hosts["api.isidore.science"]["ok"] >= 1
    assert hosts["api.zotero.org"]["requests"] >= 1  # schéma servi par le simulateur
    assert result["items"]["final_corpus"] > 0
    assert result["ground_truth"]["output"]["false_merges"] == 0
    # Couverture par identifiants : travaux par source et du corpus final.
    cov = result["coverage"]
    assert set(cov["sources"]) == {"HAL", "Isidore", "OpenEdition"}
    assert cov["final"]["count"] == result["ground_truth"]["output"]["works"]
    final = set(analysis.decode_ids(cov["final"]["ids"]))
    for rec in cov["sources"].values():
        assert set(analysis.decode_ids(rec["ids"])) <= final
    assert result["network"]["total"]["body_errors"] == 0
    assert list((tmp_path / "run" / "output").glob("*.csl.json"))
    after = set(project_output.glob("*")) if project_output.is_dir() else set()
    assert after == before  # rien d'écrit dans output/ du projet
