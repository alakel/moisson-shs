"""
PAT-18 (retour d'expérience du 2026-09-30) : la fusion garde le contexte.

Le manifeste d'une fusion ne gardait de ses entrées que nom, taille et
empreinte : ``corpus_diff`` déduisait alors la requête du nom
« corpus_merged_… » (avertissement ``requetes_differentes`` factice) et datait
le corpus du jour de la fusion. Désormais : ``inputs[i]`` porte le manifeste,
le type et un contexte compact (requête, mode, sources, dates, statuts,
plafonds, tris, versions) ; ``records`` donne l'origine de chaque notice ;
``corpus_diff`` réunit les contextes des entrées (mode commun ou « mixte »,
union des sources, date de l'entrée la plus ancienne) et avertit
(``corpus_fusionne``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

import mcp_server
from utils import corpus_diff as cd
from utils import manifest as mf


def art(title, doi, date="2020", source="HAL"):
    return {"itemType": "journalArticle", "title": title, "date": date,
            "DOI": doi, "extra": f"Sources: {source}",
            "creators": [{"creatorType": "author", "lastName": "Dupont",
                          "firstName": "Jean"}]}


def manifest_doc(stem, *, date, mode, query, sources, kind="moisson",
                 max_per_source=200, from_date=None, records=None, path=None):
    files = []
    if path is not None:
        raw = Path(path).read_bytes()
        files = [{"name": Path(path).name, "role": "zotero", "bytes": len(raw),
                  "sha256": hashlib.sha256(raw).hexdigest()}]
    return {
        "format": "moisson-shs/manifest", "version": 1, "stem": stem,
        "kind": kind, "started_at": date, "finished_at": date,
        "query": {"text": query, "mode": mode, "sources": sorted(sources),
                  "from_date": from_date, "to_date": None, "languages": []},
        "filters": {"from_date": from_date, "to_date": None, "languages": []},
        "settings": {"max_per_source": max_per_source},
        "ranking": {"mode": "native"},
        "versions": {"package": "0.4.0.dev0", "commit": "abc1234",
                     "dirty": False, "python": "3.14"},
        "sources": {label: dict(entry, source=entry.get("source", label.lower()))
                    for label, entry in sources.items()},
        "files": files,
        "records": records or [],
    }


def write_corpus(directory, stem, items, **manifest):
    path = Path(directory) / f"{stem}.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    if manifest:
        doc = manifest_doc(stem, path=path, **manifest)
        (Path(directory) / f"{stem}.manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


HAL_CAPPED = {"source": "hal", "status": "complete", "items": 2,
              "limit_reached": True, "native_sort": "score desc"}
XREF_OK = {"source": "crossref", "status": "complete", "items": 2}
SA = "corpus_discours_numerique_20260801_100000_aaaaaaaa"
SB = "corpus_twitter_20260901_100000_bbbbbbbb"
SC = "corpus_discours_numerique_20261001_100000_cccccccc"


@pytest.fixture
def out(tmp_path, monkeypatch):
    base = tmp_path / "out"
    base.mkdir()
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", base)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    return base


@pytest.fixture
def two(out):
    a = write_corpus(out, SA, [art("X", "10.1/x"), art("Y", "10.1/y")],
                     date="2026-08-01T10:00:00+02:00", mode="bibliographie",
                     query="discours numérique", sources={"HAL": HAL_CAPPED},
                     records=[{"position": 1, "id": "doi:10.1/x",
                               "native": [{"source": "hal", "rank": 1,
                                           "score": 9.5}],
                               "ranking": {"rank": 1, "score": 9.5}},
                              {"position": 2, "id": "doi:10.1/y",
                               "native": [{"source": "hal", "rank": 2,
                                           "score": 7.0}],
                               "ranking": {"rank": 2, "score": 7.0}}])
    b = write_corpus(out, SB, [art("X", "10.1/x", source="CrossRef"),
                               art("Z", "10.1/z", source="CrossRef")],
                     date="2026-09-01T10:00:00+02:00", mode="collecte",
                     query="twitter", sources={"CrossRef": XREF_OK},
                     max_per_source=500)
    return a, b


def merge(paths):
    _blocks, structured = asyncio.run(mcp_server._tool_corpus_merge(
        {"corpus_paths": [str(p) for p in paths], "inline": "none"}))
    out = Path(mcp_server.OUTPUT_DIR)
    doc = json.loads((out / structured["manifest"]).read_text(encoding="utf-8"))
    primary = out / doc["primary"]
    return primary, doc


class TestMergeManifest:
    def test_inputs_carry_context(self, two):
        _path, doc = merge(two)
        first, second = doc["inputs"]
        assert first["manifest"] == f"{SA}.manifest.json"
        assert first["kind"] == "moisson" and first["items"] == 2
        assert first["context"]["query"]["mode"] == "bibliographie"
        assert first["context"]["query"]["text"] == "discours numérique"
        assert first["context"]["sources"]["HAL"]["limit_reached"] is True
        assert first["context"]["sources"]["HAL"]["native_sort"] == "score desc"
        assert first["context"]["settings"]["max_per_source"] == 200
        assert first["context"]["ranking"]["mode"] == "native"
        assert first["context"]["started_at"].startswith("2026-08-01")
        assert first["context"]["versions"] == {
            "package": "0.4.0.dev0", "commit": "abc1234", "dirty": False}
        assert second["context"]["query"]["mode"] == "collecte"
        assert second["context"]["settings"]["max_per_source"] == 500

    def test_records_give_origins_without_global_rank(self, two):
        _path, doc = merge(two)
        by_id = {r["id"]: r for r in doc["records"]}
        assert set(by_id) == {"doi:10.1/x", "doi:10.1/y", "doi:10.1/z"}
        x = by_id["doi:10.1/x"]
        assert x["origins"] == [
            {"input": 0, "position": 1,
             "native": [{"source": "hal", "rank": 1, "score": 9.5}]},
            {"input": 1, "position": 1}]
        assert by_id["doi:10.1/z"]["origins"] == [{"input": 1, "position": 2}]
        assert all("ranking" not in r and "native" not in r
                   for r in doc["records"])

    def test_stale_input_manifest_gives_no_ranks(self, two, out):
        a, b = two
        a.write_text(a.read_text(encoding="utf-8") + " ", encoding="utf-8")
        _path, doc = merge([a, b])
        x = next(r for r in doc["records"] if r["id"] == "doi:10.1/x")
        assert x["origins"][0] == {"input": 0, "position": 1}

    def test_input_without_manifest(self, two, out):
        a, _b = two
        bare = write_corpus(out, "corpus_libre", [art("W", "10.1/w")])
        _path, doc = merge([a, bare])
        assert doc["inputs"][1]["manifest"] is None
        assert doc["inputs"][1]["context"] is None

    def test_derived_input_follows_lineage(self, two, out):
        a, b = two
        _blocks, structured = asyncio.run(mcp_server._tool_corpus_filter(
            {"corpus_path": str(a), "title_contains": "X", "inline": "none"}))
        filtered = out / f"{SA}_filtered.json"
        _path, doc = merge([filtered, b])
        first = doc["inputs"][0]
        assert first["kind"] == "filtre"
        assert first["context"]["inherited_from"] == f"{SA}.manifest.json"
        assert first["context"]["query"]["mode"] == "bibliographie"

    def test_nested_merge_is_flattened(self, two, out):
        inner, _doc = merge(two)
        c = write_corpus(out, SC, [art("V", "10.1/v")],
                         date="2026-10-01T10:00:00+02:00",
                         mode="bibliographie", query="discours numérique",
                         sources={"HAL": HAL_CAPPED})
        _path, doc = merge([inner, c])
        nested = doc["inputs"][0]["context"]
        assert nested["kind"] == "fusion" and len(nested["inputs"]) == 2
        contexts = mf.merge_contexts(doc)
        assert [c["query"]["mode"] for c in contexts] == [
            "bibliographie", "collecte", "bibliographie"]


class TestDiffOfMerge:
    def _diff(self, a, b):
        return cd.diff_corpus_files(a, b, write=False).report

    def test_no_query_from_merge_name_and_warning(self, two, out):
        merged, _doc = merge(two)
        c = write_corpus(out, SC, [art("X", "10.1/x"),
                                   art("W", "10.1/w", source="OpenAlex")],
                         date="2026-10-01T10:00:00+02:00",
                         mode="bibliographie", query="discours numérique",
                         sources={"HAL": HAL_CAPPED})
        r = self._diff(merged, c)
        codes = {w["code"] for w in r["warnings"]}
        assert "requetes_differentes" not in codes
        assert {"corpus_fusionne", "mode_bibliographie"} <= codes
        ctx = r["corpus_a"]
        assert ctx["query"] is None and ctx["query_from_name"] is None
        assert ctx["mode"] == "mixte"
        assert ctx["harvest_date"].startswith("2026-08-01")
        assert ctx["harvest_date_basis"] == (
            "manifeste (entrée la plus ancienne de la fusion)")
        assert [s["label"] for s in ctx["sources"]] == ["CrossRef", "HAL"]
        message = next(w["message"] for w in r["warnings"]
                       if w["code"] == "corpus_fusionne")
        assert "fusion de 2 corpus" in message and "« twitter »" in message

    def test_same_query_inputs_keep_the_query(self, two, out):
        a, _b = two
        c = write_corpus(out, SC, [art("V", "10.1/v")],
                         date="2026-10-01T10:00:00+02:00",
                         mode="bibliographie", query="Discours  numérique",
                         sources={"HAL": HAL_CAPPED})
        merged, _doc = merge([a, c])
        ctx = cd.corpus_context(merged, [], merged.read_bytes())
        assert ctx["query"] == "discours numérique"
        assert ctx["mode"] == "bibliographie"
        assert ctx["max_per_source"] == 200
        other = write_corpus(out, "corpus_autre_20261101_100000_dddddddd",
                             [art("V", "10.1/v")],
                             date="2026-11-01T10:00:00+02:00",
                             mode="bibliographie", query="plateformes",
                             sources={"HAL": HAL_CAPPED})
        codes = {w["code"] for w in self._diff(merged, other)["warnings"]}
        assert "requetes_differentes" in codes          # vraie différence

    def test_notes_follow_merged_sources(self, two, out):
        merged, _doc = merge(two)
        c = write_corpus(out, SC, [art("X", "10.1/x"),
                                   art("T", "10.1/t", source="CrossRef"),
                                   art("W", "10.1/w", source="OpenAlex")],
                         date="2026-10-01T10:00:00+02:00",
                         mode="bibliographie", query="discours numérique",
                         sources={"HAL": HAL_CAPPED})
        added = {e["title"]: e.get("notes") or []
                 for e in self._diff(merged, c)["added"]}
        # CrossRef interrogé par une entrée de la fusion : pas de réserve.
        assert not any("non interrogées" in n for n in added["T"])
        assert any("non interrogées" in n for n in added["W"])

    def test_legacy_merge_without_context(self, out):
        a = write_corpus(out, "corpus_merged_20260801_100000_eeeeeeee",
                         [art("X", "10.1/x")])
        raw = a.read_bytes()
        doc = {"format": "moisson-shs/manifest", "version": 1,
               "stem": a.stem, "kind": "fusion",
               "started_at": "2026-08-01T10:00:00+02:00",
               "inputs": [{"name": "corpus_x.json", "bytes": 1,
                           "sha256": "0" * 64}],
               "files": [{"name": a.name, "bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()}]}
        (out / f"{a.stem}.manifest.json").write_text(json.dumps(doc),
                                                     encoding="utf-8")
        ctx = cd.corpus_context(a, [], raw)
        assert ctx["query_from_name"] is None and ctx["mode"] is None

    def test_author_name_is_not_a_query(self, out):
        a = write_corpus(out, "corpus_author_lakel_20260801_100000_ffffffff",
                         [art("X", "10.1/x")])
        ctx = cd.corpus_context(a, [], a.read_bytes())
        assert ctx["query_from_name"] is None
