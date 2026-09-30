# -*- coding: utf-8 -*-
"""
OPT-10 : résolution de DOI — examen de tous les candidats (statut ambigu),
erreur réseau distinguée d'une absence de candidat, budgets en temps et en
nombre, rapport complet ``<stem>.doi_report.json`` sans chemin disque, seuils
non abaissables (refus explicite), client HTTP et cache mémoire partagés.

La couche réseau est simulée au point d'appel ``utils.doi_resolver.get_json``,
avec des réponses indexées par titre (les requêtes partent en parallèle).
Aucun réseau.
"""

import asyncio
import json
import re
import threading
import time
from unittest.mock import patch

import pytest
import requests

import utils.doi_resolver as dr

# Référence capturée à l'import : les tests neutralisent « utils.http.time.sleep »,
# c'est-à-dire time.sleep lui-même (même objet module).
_real_sleep = time.sleep


def _item(title, year="2020", last="Dupont", doi=None, item_type="journalArticle",
          extra="Sources: Google Scholar (SerpAPI)"):
    item = {"itemType": item_type, "title": title, "date": year,
            "creators": ([{"creatorType": "author", "firstName": "A",
                           "lastName": last}] if last else []),
            "extra": extra, "tags": [], "collections": [], "relations": {}}
    if doi:
        item["DOI"] = doi
    return item


def _cand(title, year=2020, families=("Dupont",), doi="10.1000/x"):
    c = {"title": [title], "DOI": doi,
         "author": [{"family": f} for f in families]}
    if year:
        c["issued"] = {"date-parts": [[year]]}
    return c


def _msg(items):
    return {"message": {"items": items}}


class _Fake:
    """get_json simulé : réponse (ou exception) indexée par titre demandé."""

    def __init__(self, table, default=None, delay=0.0):
        self.table = table
        self.default = default if default is not None else _msg([])
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, url, params=None, **kwargs):
        with self._lock:
            self.calls.append(dict(params))
        if self.delay:
            _real_sleep(self.delay)
        answer = self.table.get(params["query.bibliographic"], self.default)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _isolated_cache():
    dr.shared_query_cache().clear()
    yield
    dr.shared_query_cache().clear()


def _resolve(items, table, **kwargs):
    fake = _Fake(table, delay=kwargs.pop("delay", 0.0))
    with patch.object(dr, "get_json", side_effect=fake), \
            patch("utils.http.time.sleep", lambda s: None):
        out, rep = dr.resolve_dois(items, **kwargs)
    return out, rep, fake


# === Tous les candidats : ambiguïté ===

class TestAmbiguity:
    def test_two_distinct_admissible_works_are_ambiguous(self):
        title = "Le discours numerique en question"
        out, rep, _ = _resolve([_item(title)], {title: _msg([
            _cand(title, doi="10.1/preprint"),
            _cand(title, doi="10.1/article"),
        ])})
        assert rep["ambiguous"] == 1 and rep["resolved"] == 0
        assert not out[0].get("DOI") and "DOI:" not in out[0]["extra"]
        entry = rep["entries"][0]
        assert entry["status"] == "ambigu"
        assert [c["admissible"] for c in entry["candidates"]] == [True, True]
        assert "10.1/preprint" in entry["reason"]
        assert "10.1/article" in entry["reason"]

    def test_same_work_twice_is_not_ambiguous(self):
        """Deux enregistrements du même DOI (casse différente) : un travail."""
        title = "Le discours numerique en question"
        out, rep, _ = _resolve([_item(title)], {title: _msg([
            _cand(title, doi="10.1/ABC"), _cand(title, doi="10.1/abc"),
        ])})
        assert rep["resolved"] == 1 and out[0]["DOI"] == "10.1/ABC"

    def test_admissible_candidate_without_doi_makes_it_ambiguous(self):
        title = "Le discours numerique en question"
        out, rep, _ = _resolve([_item(title)], {title: _msg([
            _cand(title, doi="10.1/abc"), _cand(title, doi=""),
        ])})
        assert rep["ambiguous"] == 1 and not out[0].get("DOI")

    def test_all_candidates_are_examined_not_only_the_first(self):
        """La baseline s'arrêtait au premier admissible (ici 10.1/a)."""
        title = "Le discours numerique en question"
        _, rep, _ = _resolve([_item(title)], {title: _msg([
            _cand("Sans rapport aucun avec le sujet", doi="10.1/z"),
            _cand(title, doi="10.1/a"),
            _cand(title + " !", doi="10.1/b"),
        ])})
        entry = rep["entries"][0]
        assert len(entry["candidates"]) == 3
        assert entry["status"] == "ambigu"


# === Erreur réseau et absence de candidat ===

class TestErrorsVersusNoCandidate:
    def test_distinct_counters(self):
        items = [_item("Titre en panne reseau"), _item("Titre en erreur 503"),
                 _item("Titre sans candidat")]
        err503 = requests.HTTPError("HTTP 503")
        err503.response = type("R", (), {"status_code": 503})()
        out, rep, _ = _resolve(items, {
            "Titre en panne reseau": requests.ConnectionError("coupure"),
            "Titre en erreur 503": err503,
            "Titre sans candidat": _msg([]),
        })
        statuses = [e["status"] for e in rep["entries"]]
        assert statuses == ["erreur", "erreur", "sans_candidat"]
        assert rep["errors"] == 2 and rep["no_candidate"] == 1
        assert "ConnectionError" in rep["entries"][0]["reason"]
        assert "HTTP 503" in rep["entries"][1]["reason"]
        assert rep["error_list"][0][1] == "ConnectionError"

    def test_errors_are_never_cached(self):
        cache = dr.QueryCache()
        title = "Titre en panne reseau"
        _resolve([_item(title)], {title: requests.Timeout("lent")},
                 query_cache=cache)
        assert len(cache) == 0
        _, rep, fake = _resolve(
            [_item(title)], {title: _msg([_cand(title, doi="10.1/ok")])},
            query_cache=cache)
        assert rep["resolved"] == 1 and len(fake.calls) == 1

    def test_malformed_answer_is_an_error_not_an_absence(self):
        title = "Titre a reponse bizarre"
        _, rep, _ = _resolve([_item(title)], {title: {"status": "ok"}})
        assert rep["entries"][0]["status"] == "erreur"


# === Budgets ===

class TestBudgets:
    def test_time_budget_stops_cleanly(self):
        items = [_item(f"Titre numero {i} du corpus", last=f"A{i}")
                 for i in range(30)]
        out, rep, fake = _resolve(items, {}, delay=0.05, max_seconds=0.12,
                                  workers=1)
        assert rep["stopped_early"] is True
        assert "budget" in rep["stop_reason"]
        assert 0 < rep["not_processed"] < 30
        assert len(fake.calls) == rep["attempted"]
        # Toutes les notices sont comptées, aucune n'est perdue.
        assert (rep["attempted"] + rep["not_processed"]) == 30
        assert len(out) == 30
        assert all(e["status"] in ("sans_candidat", "non_traite")
                   for e in rep["entries"])

    def test_count_budget(self):
        items = [_item(f"Titre numero {i} du corpus", last=f"A{i}")
                 for i in range(5)]
        _, rep, fake = _resolve(items, {}, max_items=2)
        assert len(fake.calls) == 2
        assert rep["not_processed"] == 3 and rep["stop_reason"] == "max_items"
        assert [e["status"] for e in rep["entries"]][2:] == ["non_traite"] * 3

    def test_stop_event(self):
        stop = threading.Event()
        stop.set()
        _, rep, fake = _resolve([_item("Titre quelconque ici")], {},
                                stop_event=stop)
        assert fake.calls == [] and rep["not_processed"] == 1


# === Notices déjà pourvues (extracteur commun ou repli) ===

class TestAlreadyHasDoi:
    @pytest.mark.parametrize("item", [
        _item("Avec DOI", doi="https://doi.org/10.1/x"),
        _item("Livre", item_type="book",
              extra="Sources: HAL\nDOI: 10.1/deja"),
    ])
    def test_items_with_a_doi_are_not_queried(self, item):
        _, rep, fake = _resolve([item], {})
        assert fake.calls == [] and rep["already_had_doi"] == 1

    def test_item_doi_helper(self):
        assert dr.item_doi(_item("x", doi="DOI:10.1/AB")) == "10.1/ab"
        assert dr.item_doi(_item("x", doi="pas un doi")) == ""
        assert dr.normalize_doi("https://dx.doi.org/10.1/X") == "10.1/x"
        assert dr.normalize_doi("10.1/x%2Fy.") == "10.1/x/y"
        # La ligne « DOI source » du résolveur n'est jamais lue comme un DOI.
        assert dr.item_doi(_item("x", extra="DOI source: appariement "
                                               "10.1/faux")) == ""

    def test_item_doi_is_the_shared_extractor(self):
        """Intégration OPT-06 / OPT-10 : plus de repli local, la lecture du
        DOI est celle de utils.identity (mêmes résultats, chaîne vide au lieu
        de None)."""
        from utils import identity
        assert not hasattr(dr, "_local_extract_doi")
        assert not hasattr(dr, "_shared_extract_doi")
        items = [
            _item("x", item_type="book", extra="Sources: HAL\nDOI: 10.1/Deja"),
            _item("x", extra="DOI source: 10.1/faux"),
            _item("x", doi="https://doi.org/10.1/A"),
            _item("x", doi="pas un doi", extra="DOI: doi:10.2/B"),
            _item("x"),
        ]
        for item in items:
            assert dr.item_doi(item) == (identity.extract_doi(item) or "")
        assert [dr.item_doi(i) for i in items] == [
            "10.1/deja", "", "10.1/a", "10.2/b", ""]
        for value in ("https://dx.doi.org/10.1/X", "10.1/x%2Fy.", "doi:10.3/Z",
                      "pas un doi", "", None, 42):
            shared = (identity.normalize_doi(value)
                      if isinstance(value, str) and value else None)
            assert dr.normalize_doi(value) == (shared or "")

    def test_shared_extractor_is_used(self, monkeypatch):
        from utils import identity
        seen = []
        monkeypatch.setattr(identity, "extract_doi",
                            lambda item: seen.append(1) or "10.1/partage")
        assert dr.item_doi(_item("x")) == "10.1/partage" and seen


# === Seuils non abaissables ===

class TestThresholdBounds:
    @pytest.mark.parametrize("kwargs, fragment", [
        ({"threshold": 0.89}, "abaissé"),
        ({"threshold": 0.5}, "abaissé"),
        ({"strict_threshold": 0.96}, "abaissé"),
        ({"year_tolerance": 2}, "au plus 1"),
        ({"rows": 3}, "au moins 5"),
        ({"threshold": 1.5}, "invalide"),
        ({"year_tolerance": -1}, "invalide"),
        ({"workers": 0}, "invalide"),
    ])
    def test_weakening_is_refused(self, kwargs, fragment):
        with patch.object(dr, "get_json") as gj:
            with pytest.raises(dr.DoiRuleError, match=fragment):
                dr.resolve_dois([_item("Un titre")], **kwargs)
        gj.assert_not_called()

    def test_hardening_is_allowed(self):
        title = "Modeles de langage et discours"
        _, rep, _ = _resolve([_item(title, year="2024", last="Lakel")], {
            title: _msg([_cand("Modeles de langage et le discours", 2024,
                               ["Lakel"], "10.1/ml")])},
            threshold=0.99, strict_threshold=0.99, year_tolerance=0, rows=20)
        assert rep["resolved"] == 0 and rep["rejected"] == 1
        assert rep["parameters"]["title_threshold"] == 0.99

    def test_environment_value_under_the_floor_is_refused(self):
        """Une variable d'environnement sous le plancher n'est pas appliquée :
        les valeurs par défaut sont revalidées à l'usage."""
        with pytest.raises(dr.DoiRuleError):
            dr.check_rule_parameters(0.85, 0.97, 1)

    def test_schema_bounds_match_the_module_floors(self):
        import mcp_server
        tools = asyncio.run(mcp_server.list_tools())
        props = next(t for t in tools
                     if t.name == "corpus_resolve_dois").inputSchema["properties"]
        assert props["title_threshold"]["minimum"] == dr.TITLE_THRESHOLD_FLOOR
        assert (props["strict_title_threshold"]["minimum"]
                == dr.STRICT_THRESHOLD_FLOOR)
        assert props["year_tolerance"]["maximum"] == dr.YEAR_TOLERANCE_CEILING
        assert props["max_items"]["minimum"] == 1
        assert "max_seconds" in props


# === Outil MCP : rapport, retour, refus ===

def _write_corpus(path, items):
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _corpus_items():
    items = [
        _item("Deja pourvu", doi="10.1/deja"),
        _item("Titre accepte sans hesitation"),
        _item("Titre ambigu entre deux versions"),
        _item("Titre sans candidat du tout"),
        _item("Titre en panne reseau"),
        _item(""),
    ]
    items += [_item(f"Titre rejete numero {i}", last=f"R{i}") for i in range(12)]
    return items


def _table():
    table = {
        "Titre accepte sans hesitation": _msg([
            _cand("Titre accepte sans hesitation", doi="10.1/ok")]),
        "Titre ambigu entre deux versions": _msg([
            _cand("Titre ambigu entre deux versions", doi="10.1/v1"),
            _cand("Titre ambigu entre deux versions", doi="10.1/v2")]),
        "Titre en panne reseau": requests.ConnectionError("coupure"),
    }
    for i in range(12):
        table[f"Titre rejete numero {i}"] = _msg([
            _cand("Un tout autre travail sans rapport", doi=f"10.1/r{i}")])
    return table


class TestMcpTool:
    def _run(self, args, table=None):
        import mcp_server
        fake = _Fake(table if table is not None else _table())
        with patch.object(dr, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            result = asyncio.run(mcp_server._tool_corpus_resolve_dois(args))
        return result, fake

    def test_report_written_complete_and_without_path(self, tmp_path,
                                                       monkeypatch):
        import mcp_server
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        src = _write_corpus(tmp_path / "corpus_scholar.json", _corpus_items())
        (blocks, structured), _ = self._run({"corpus_path": str(src)})

        report_path = tmp_path / "corpus_scholar_doi.doi_report.json"
        assert report_path.exists()
        doc = json.loads(report_path.read_text(encoding="utf-8"))
        assert doc["format"] == "moisson-shs/doi-report"
        assert doc["rules_version"] == dr.RULES_VERSION
        assert doc["source_corpus"] == "corpus_scholar.json"
        assert doc["output_corpus"] == "corpus_scholar_doi.json"
        counts = doc["counts"]
        assert counts["accepte"] == 1 and counts["ambigu"] == 1
        assert counts["rejete"] == 12 and counts["sans_candidat"] == 1
        assert counts["erreur"] == 1 and counts["sans_titre"] == 1
        assert counts["non_traite"] == 0 and counts["deja_pourvu"] == 1
        assert doc["parameters"]["title_threshold"] == 0.90
        assert doc["floors"]["title_threshold_min"] == 0.90
        assert len(doc["entries"]) == 17           # toutes les notices sans DOI
        scored = [e for e in doc["entries"] if e["candidates"]]
        assert all("score" in c and "admissible" in c
                   for e in scored for c in e["candidates"])
        # Aucun chemin disque dans le rapport (téléchargeable en HTTP)
        for s in _strings(doc):
            assert str(tmp_path) not in s
            assert not s.startswith("/") and not re.match(r"^[A-Za-z]:\\", s)
        # Passé en fichier associé : listé, jamais en ResourceLink
        assert report_path.name in structured["files"]
        links = [b for b in blocks if getattr(b, "type", "") == "resource_link"]
        assert all("doi_report" not in str(b.uri) for b in links)
        assert structured["doi_report"] == report_path.name
        assert structured["ambiguous"] == 1 and structured["errors"] == 1

    def test_summary_says_when_the_list_is_truncated(self, tmp_path,
                                                     monkeypatch):
        import mcp_server
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        src = _write_corpus(tmp_path / "corpus_scholar.json", _corpus_items())
        (blocks, _), _ = self._run({"corpus_path": str(src)})
        text = blocks[0].text
        assert ("10 premiers sur 12 ; liste complète dans "
                "corpus_scholar_doi.doi_report.json") in text
        assert "Ambigus (plusieurs travaux admissibles" in text
        assert "Erreurs réseau ou de service : 1" in text
        assert "Sans candidat CrossRef : 1" in text

    def test_http_mode_exposes_the_report_as_a_download(self, tmp_path,
                                                        monkeypatch):
        import mcp_server
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                            "https://moisson.example.org")
        _write_corpus(tmp_path / "corpus_scholar.json", _corpus_items())
        (blocks, structured), _ = self._run(
            {"corpus_path": "corpus_scholar.json"})
        urls = structured["download_urls"]
        assert "corpus_scholar_doi.doi_report.json" in urls
        assert str(tmp_path) not in json.dumps(structured)
        assert str(tmp_path) not in blocks[0].text

    @pytest.mark.parametrize("args, fragment", [
        ({"title_threshold": 0.5}, "abaissé"),
        ({"title_threshold": 0}, "abaissé"),
        ({"strict_title_threshold": 0.9}, "abaissé"),
        ({"year_tolerance": 3}, "au plus 1"),
        ({"max_items": 0}, "max_items"),
        ({"max_seconds": 5000}, "au plus"),
        ({"max_seconds": -1}, "max_seconds"),
    ])
    def test_refusal_is_explicit_and_nothing_is_written(self, tmp_path,
                                                        monkeypatch, args,
                                                        fragment):
        import mcp_server
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        src = _write_corpus(tmp_path / "corpus_scholar.json", _corpus_items())
        out, fake = self._run({"corpus_path": str(src), **args})
        assert isinstance(out, str) and out.startswith("Paramètre refusé")
        assert fragment in out
        assert fake.calls == []
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "corpus_scholar.json"]

    def test_env_default_under_the_floor_is_refused(self, tmp_path,
                                                    monkeypatch):
        import config
        import mcp_server
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(config, "DOI_TITLE_THRESHOLD", 0.8)
        src = _write_corpus(tmp_path / "corpus_scholar.json", _corpus_items())
        out, fake = self._run({"corpus_path": str(src)})
        assert out.startswith("Paramètre refusé") and fake.calls == []

    def test_shared_client_and_shared_cache_are_passed(self, tmp_path,
                                                       monkeypatch):
        import mcp_server
        from utils.http import get_default_client
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        seen = {}
        real = dr.resolve_dois

        def spy(items, **kwargs):
            seen.update(kwargs)
            return real(items, **kwargs)

        monkeypatch.setattr(dr, "resolve_dois", spy)
        src = _write_corpus(tmp_path / "corpus_scholar.json", _corpus_items())
        self._run({"corpus_path": str(src)})
        assert seen["http_client"] is get_default_client()
        assert seen["query_cache"] is dr.shared_query_cache()
        assert seen["workers"] == 3
        assert seen["max_seconds"] == 240

    def test_second_run_is_served_by_the_memory_cache(self, tmp_path,
                                                      monkeypatch):
        import mcp_server
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        src = _write_corpus(tmp_path / "corpus_scholar.json", _corpus_items())
        _, fake1 = self._run({"corpus_path": str(src)})
        (_, structured), fake2 = self._run(
            {"corpus_path": str(src), "output_suffix": "_bis"})
        # 16 notices à interroger (1 acceptée, 1 ambiguë, 1 sans candidat,
        # 1 en erreur, 12 rejetées) ; la notice sans titre ne l'est pas.
        assert len(fake1.calls) == 16
        # Seule la notice en erreur réseau est réinterrogée.
        assert [c["query.bibliographic"] for c in fake2.calls] == [
            "Titre en panne reseau"]
        assert structured["query_cache_hits"] == 15
