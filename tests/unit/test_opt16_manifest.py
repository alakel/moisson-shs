"""
Tests OPT-16 : manifeste versionné par corpus.

- moisson MCP et CLI : manifeste ``<stem>.manifest.json`` publié avec le
  corpus (format, version, identité, fichiers avec taille et empreinte
  exactes, requête et plan, sources et statuts, versions, déduplication,
  échéance souple) ;
- rangs et scores natifs par notice finale et classement dans le manifeste,
  **jamais** dans les notices (invariant 1) ;
- aucun chemin absolu dans le manifeste (téléchargeable en HTTP) ;
- noms uniques (uuid court) pour la CLI, search_by_author et corpus_merge
  (C33) ;
- producteurs dérivés : filtre, conversion CSL (ajout au corpus existant,
  corpus ancien complété), fusion, résolution DOI, recherche par auteur.

Aucun accès réseau : exécutants de source et résolveurs simulés.
"""

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import main as main_module  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.models import SourceResult  # noqa: E402
from utils import manifest as mf  # noqa: E402
from utils.output_files import is_downloadable_file  # noqa: E402

RUN_ID_RE = re.compile(r"_\d{8}_\d{6}_[0-9a-f]{8}$")
RANK_KEYS = {"rank", "score", "native_rank", "ranking", "records", "position"}


def _run(coro):
    return asyncio.run(coro)


def _items(name: str, n: int = 3) -> List[Dict]:
    return [{"itemType": "journalArticle", "title": f"{name} titre {i}",
             "date": "2020", "DOI": f"10.0/{name.lower()}.{i}",
             "creators": [{"creatorType": "author", "lastName": f"{name}{i}"}],
             "extra": f"Sources: {name}"} for i in range(n)]


def _ranked_runner(name, cls, query, mps, output_dir, run_id, http_client,
                   run_context=None, source_key=None, **kw):
    """Source simulée qui publie rangs et scores natifs (tri par score)."""
    items = _items(name)
    res = SourceResult(
        source=source_key, display_name=name, items=items, status="complete",
        native_sort="score desc",
        native_ranks=[{"rank": i + 1, "score": 10.0 - i, "sort": "score desc"}
                      for i in range(len(items))])
    res.bind_ranks()
    return res


@pytest.fixture
def out(tmp_path, monkeypatch):
    base = tmp_path / "out"
    base.mkdir()
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", base)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    monkeypatch.setattr(orchestrator, "run_source", _ranked_runner)
    return base


def _manifest_of(directory: Path) -> Dict:
    paths = sorted(directory.glob("corpus_*.manifest.json"))
    assert len(paths) == 1, paths
    return json.loads(paths[0].read_text(encoding="utf-8"))


def _moisson(out, **kw):
    return mcp_server._run_moisson_sync("discours numérique", 10,
                                        ["hal", "crossref"], False, True,
                                        workers=1, **kw)


# === Moisson MCP ===

class TestHarvestManifest:
    def test_manifest_published_with_corpus(self, out):
        blocks, structured = _moisson(out)
        doc = _manifest_of(out)
        assert doc["format"] == "moisson-shs/manifest"
        assert doc["version"] == 1
        assert doc["kind"] == "moisson"
        assert RUN_ID_RE.search(doc["stem"])
        assert doc["run_id"] and doc["stem"].endswith(doc["run_id"])
        assert doc["item_count"] == structured["total_unique"] == 6
        assert doc["primary"] == f"{doc['stem']}.json"
        assert structured["manifest"] == f"{doc['stem']}.manifest.json"
        assert doc["started_at"] <= doc["finished_at"]

    def test_files_sizes_and_digests_match(self, out):
        _moisson(out)
        doc = _manifest_of(out)
        roles = {f["role"] for f in doc["files"]}
        assert roles == {"zotero", "csl", "stats"}
        for entry in doc["files"]:
            data = (out / entry["name"]).read_bytes()
            assert entry["bytes"] == len(data)
            assert entry["sha256"] == hashlib.sha256(data).hexdigest()
            assert "/" not in entry["name"]
        zot = next(f for f in doc["files"] if f["role"] == "zotero")
        assert zot["items"] == 6

    def test_query_plan_sources_versions_dedup(self, out):
        _moisson(out)
        doc = _manifest_of(out)
        assert doc["query"]["text"] == "discours numérique"
        assert doc["query"]["mode"] == "bibliographie"
        assert doc["query"]["sources"] == ["hal", "crossref"]
        assert isinstance(doc["query_plan"], dict)
        assert "sources" in doc["query_plan"]
        assert set(doc["sources"]) == {"HAL", "CrossRef"}
        hal = doc["sources"]["HAL"]
        assert hal["status"] == "complete"
        assert hal["native_sort"] == "score desc"
        assert hal["ranked_items"] == 3
        assert doc["settings"]["max_per_source"] == 10
        versions = doc["versions"]
        for key in ("package", "manifest", "dedup_rules", "ranking",
                    "doi_rules", "query_plan"):
            assert key in versions, key
        from utils.ranking import RANKING_VERSION
        from utils.doi_resolver import RULES_VERSION
        assert versions["ranking"] == RANKING_VERSION
        assert versions["doi_rules"] == RULES_VERSION
        assert versions["dedup_rules"]["fuzzy_title_threshold"] == 0.93
        assert doc["dedup"]["input"] == 6 and "groups" not in doc["dedup"]
        assert doc["harvest_status"] == "ok"
        assert doc["soft_deadline"] is None     # pas d'échéance souple hors portée

    def test_ranks_and_scores_per_record_in_manifest(self, out):
        _moisson(out)
        doc = _manifest_of(out)
        corpus = json.loads((out / doc["primary"]).read_text(encoding="utf-8"))
        records = doc["records"]
        assert [r["position"] for r in records] == list(range(1, len(corpus) + 1))
        from utils.ranking import record_id
        assert [r["id"] for r in records] == [record_id(it) for it in corpus]
        by_id = {r["id"]: r for r in records}
        hal0 = by_id["doi:10.0/hal.0"]
        assert hal0["native"] == [{"source": "hal", "rank": 1, "score": 10.0}]
        assert hal0["ranking"]["native_rank"] >= 1
        assert "summary" in hal0["ranking"]
        cr2 = by_id["doi:10.0/crossref.2"]
        assert cr2["native"][0]["rank"] == 3 and cr2["native"][0]["score"] == 8.0
        # Le classement résumé (sans ses entrées) accompagne les records.
        assert doc["ranking"]["version"] and "entries" not in doc["ranking"]

    def test_ranks_absent_from_published_notices(self, out):
        _moisson(out)
        doc = _manifest_of(out)
        for name in (doc["primary"], f"{doc['stem']}.csl.json"):
            text = (out / name).read_text(encoding="utf-8")
            for item in json.loads(text):
                assert not RANK_KEYS & set(item), item.keys()
                assert "rank" not in (item.get("extra") or "").lower()
            assert "native_rank" not in text and "score desc" not in text

    def test_ranking_off_still_attaches_native_ranks(self, out):
        _moisson(out, ranking="off")
        doc = _manifest_of(out)
        assert doc["ranking"] is None
        by_id = {r["id"]: r for r in doc["records"]}
        assert by_id["doi:10.0/hal.1"]["native"] == [
            {"source": "hal", "rank": 2, "score": 9.0}]
        assert "ranking" not in by_id["doi:10.0/hal.1"]

    def test_no_absolute_path_in_manifest(self, out, tmp_path):
        _moisson(out)
        text = next(out.glob("corpus_*.manifest.json")).read_text(encoding="utf-8")
        assert str(tmp_path) not in text
        assert str(out.resolve()) not in text
        assert mf.absolute_paths_in(json.loads(text)) == []

    def test_manifest_is_downloadable_associated_file_in_http(self, out,
                                                              monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        blocks, structured = _moisson(out)
        names = list(structured["download_urls"])
        manifest = [n for n in names if n.endswith(".manifest.json")]
        assert len(manifest) == 1 and is_downloadable_file(manifest[0])
        assert str(out) not in json.dumps(structured, ensure_ascii=False)

    def test_manifest_not_a_resource_link_in_stdio(self, out):
        blocks, structured = _moisson(out)
        links = [str(b.uri) for b in blocks if b.type == "resource_link"]
        assert not any("manifest" in u for u in links)
        assert any(n.endswith(".manifest.json") for n in structured["files"])

    def test_soft_deadline_recorded(self, out, monkeypatch):
        import time
        from engine.models import soft_deadline_scope
        with soft_deadline_scope(time.monotonic() + 30.0, 30.0):
            _moisson(out)
        doc = _manifest_of(out)
        assert doc["soft_deadline"]["seconds"] == pytest.approx(30.0, abs=1)
        assert doc["soft_deadline"]["stopped_sources"] == []

    def test_native_audit_summary_when_enabled(self, out, monkeypatch):
        import config
        monkeypatch.setattr(config, "AUDIT_NATIVE", True)
        _moisson(out)
        doc = _manifest_of(out)
        audit = doc["native_audit"]
        assert audit["file"].startswith(".native_audit/")
        assert not mf.absolute_paths_in(audit)


# === Moisson CLI ===

class TestCliManifest:
    def test_cli_unique_names_and_manifest(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(orchestrator, "run_source", _ranked_runner)
        outdir = tmp_path / "cli"
        monkeypatch.setattr(sys, "argv", [
            "main.py", "discours", "--sources", "hal", "--output", str(outdir)])
        assert main_module.main() == 0
        doc = _manifest_of(outdir)
        assert RUN_ID_RE.search(doc["stem"])
        assert doc["query"]["mode"] == "collecte"
        assert doc["records"][0]["native"][0]["source"] == "hal"
        # raw/ garde le même identifiant d'exécution (unique).
        raws = sorted(p.name for p in (outdir / "raw").glob("*.json"))
        assert raws == [] or all(doc["run_id"] in n for n in raws)
        assert "Manifeste" in capsys.readouterr().out

    def test_two_cli_runs_same_second_do_not_collide(self, tmp_path, monkeypatch):
        import engine.models as models
        monkeypatch.setattr(orchestrator, "run_source", _ranked_runner)
        fixed = "20260101_000000"
        real = models.new_run_id
        monkeypatch.setattr(main_module, "new_run_id",
                            lambda timestamp=None, unique=True: real(fixed, unique))
        outdir = tmp_path / "cli"
        for _ in range(2):
            monkeypatch.setattr(sys, "argv", [
                "main.py", "discours", "--sources", "hal", "--output", str(outdir)])
            assert main_module.main() == 0
        stems = sorted(p.name for p in outdir.glob("corpus_*.manifest.json"))
        assert len(stems) == 2 and stems[0] != stems[1]
        assert all(fixed in s for s in stems)


# === Producteurs dérivés ===

def _write(path: Path, items) -> Path:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


class TestDerivedProducers:
    def test_filter_manifest_inherits_records(self, out):
        _moisson(out)
        src = out / _manifest_of(out)["primary"]
        blocks, structured = _run(mcp_server._tool_corpus_filter(
            {"corpus_path": str(src), "title_contains": "HAL"}))
        doc = json.loads((out / structured["manifest"]).read_text(encoding="utf-8"))
        assert doc["kind"] == "filtre"
        assert doc["filters"]["title_contains"] == "hal"
        assert doc["inputs"][0]["name"] == src.name
        assert doc["inputs"][0]["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
        assert doc["item_count"] == 3
        assert [r["position"] for r in doc["records"]] == [1, 2, 3]
        assert all(r["native"][0]["source"] == "hal" for r in doc["records"])
        assert doc["records"][0]["source_position"] >= 1
        assert doc["query"]["text"] == "discours numérique"

    def test_to_csl_appends_to_existing_manifest(self, out):
        _moisson(out)
        before = _manifest_of(out)
        src = out / before["primary"]
        _run(mcp_server._tool_corpus_to_csl({"corpus_path": str(src)}))
        after = _manifest_of(out)
        assert after["kind"] == "moisson"          # corpus d'origine
        assert after["records"] == before["records"]
        assert after["history"][-1]["kind"] == "conversion_csl"
        csl = next(f for f in after["files"] if f["role"] == "csl")
        data = (out / csl["name"]).read_bytes()
        assert csl["sha256"] == hashlib.sha256(data).hexdigest()

    def test_to_csl_on_legacy_corpus_describes_it(self, out):
        src = _write(out / "corpus_ancien_20250101_101010.json", _items("HAL"))
        _run(mcp_server._tool_corpus_to_csl({"corpus_path": str(src)}))
        doc = json.loads((out / "corpus_ancien_20250101_101010.manifest.json")
                         .read_text(encoding="utf-8"))
        # Revue OPT-16 : production d'origine inconnue (« ancien ») ; la
        # conversion n'apparaît que dans l'historique.
        assert doc["kind"] == "ancien"
        assert doc["history"][-1]["kind"] == "conversion_csl"
        assert doc["primary"] == src.name and doc["item_count"] == 3
        zot = next(f for f in doc["files"] if f["role"] == "zotero")
        assert zot["described_after"] is True
        assert zot["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()

    def test_to_csl_other_output_is_a_new_corpus(self, out, tmp_path):
        src = _write(out / "corpus_x.json", _items("HAL"))
        target = out / "corpus_export_csl.csl.json"
        _run(mcp_server._tool_corpus_to_csl({"corpus_path": str(src),
                                             "output_path": str(target)}))
        doc = json.loads((out / "corpus_export_csl.manifest.json")
                         .read_text(encoding="utf-8"))
        assert doc["kind"] == "conversion_csl"
        assert doc["primary"] == target.name
        assert doc["inputs"][0]["name"] == "corpus_x.json"
        assert not (out / "corpus_x.manifest.json").exists()

    def test_merge_unique_name_and_manifest(self, out, monkeypatch):
        a = _write(out / "corpus_a.json", _items("HAL"))
        b = _write(out / "corpus_b.json", _items("Isidore"))
        import engine.models as models
        fixed = "20260101_000000"
        real = models.new_run_id
        monkeypatch.setattr(mcp_server, "new_run_id",
                            lambda timestamp=None, unique=True: real(fixed, unique))
        names = set()
        for _ in range(2):
            blocks, structured = _run(mcp_server._tool_corpus_merge(
                {"corpus_paths": [str(a), str(b)]}))
            names.add(structured["manifest"])
        assert len(names) == 2                      # même seconde, deux noms
        for name in names:
            assert RUN_ID_RE.search(name[:-len(".manifest.json")])
            doc = json.loads((out / name).read_text(encoding="utf-8"))
            assert doc["kind"] == "fusion"
            assert [i["name"] for i in doc["inputs"]] == ["corpus_a.json",
                                                         "corpus_b.json"]
            assert doc["merge_report"]["_summary"]["total_unique"] == 6
            assert len(doc["records"]) == 6

    def test_merge_inputs_from_elsewhere_keep_no_path(self, out, tmp_path):
        other = tmp_path / "ailleurs"
        other.mkdir()
        a = _write(other / "corpus_a.json", _items("HAL"))
        blocks, structured = _run(mcp_server._tool_corpus_merge(
            {"corpus_paths": [str(a)]}))
        text = (out / structured["manifest"]).read_text(encoding="utf-8")
        assert str(other) not in text and str(tmp_path) not in text
        assert mf.absolute_paths_in(json.loads(text)) == []

    def test_search_by_author_unique_name_and_manifest(self, out, monkeypatch):
        monkeypatch.setattr(mcp_server, "search_by_author",
                            lambda *a, **k: _items("HAL"))
        blocks, structured = _run(mcp_server._tool_search_by_author(
            {"name": "Jean Dupont"}))
        name = structured["manifest"]
        stem = name[:-len(".manifest.json")]
        assert stem.startswith("corpus_author_jean_dupont_")
        assert RUN_ID_RE.search(stem)
        doc = json.loads((out / name).read_text(encoding="utf-8"))
        assert doc["kind"] == "auteur"
        assert doc["query"]["text"] == "author:Jean Dupont"
        assert len(doc["records"]) == 3

    def test_resolve_dois_manifest_references_report(self, out, monkeypatch):
        from utils import doi_resolver as dr

        def no_network(*a, **k):
            raise AssertionError("aucune requête attendue (DOI déjà présents)")

        monkeypatch.setattr(dr, "get_json", no_network)
        # Notices déjà pourvues d'un DOI : résolution réelle, sans requête.
        src = _write(out / "corpus_scholar.json", _items("HAL"))
        blocks, structured = _run(mcp_server._tool_corpus_resolve_dois(
            {"corpus_path": str(src)}))
        doc = json.loads((out / "corpus_scholar_doi.manifest.json")
                         .read_text(encoding="utf-8"))
        assert doc["kind"] == "resolution_doi"
        assert doc["doi_report"] == "corpus_scholar_doi.doi_report.json"
        roles = {f["role"] for f in doc["files"]}
        assert roles == {"zotero", "csl", "doi_report"}
        assert doc["doi_resolution"]["rules_version"] == dr.RULES_VERSION
        assert doc["settings"]["title_threshold"] >= 0.90
        assert structured["manifest"] == "corpus_scholar_doi.manifest.json"


# === Lecture tolérante des anciens corpus ===

class TestLegacyCorpora:
    def test_describe_legacy_corpus(self, tmp_path):
        path = _write(tmp_path / "corpus_vieux_20240101_120000.json", _items("HAL", 4))
        info = mf.describe_corpus(path)
        assert info["status"] == "legacy"
        assert info["item_count"] == 4
        assert info["format"] == "zotero"
        assert info["created"]

    def test_describe_detects_csl_content(self, tmp_path):
        path = _write(tmp_path / "corpus_mal_nomme.json",
                      [{"id": "a", "type": "article-journal", "title": "t"}])
        assert mf.describe_corpus(path)["format"] == "csl"

    def test_invalid_manifest_is_ignored(self, tmp_path):
        path = _write(tmp_path / "corpus_a.json", _items("HAL"))
        (tmp_path / "corpus_a.manifest.json").write_text('{"items": 3}',
                                                         encoding="utf-8")
        assert mf.load_manifest_for(path) is None
        assert mf.describe_corpus(path)["status"] == "legacy"

    def test_legacy_corpus_accepted_by_filter(self, out):
        src = _write(out / "corpus_vieux.json", _items("HAL"))
        blocks, structured = _run(mcp_server._tool_corpus_filter(
            {"corpus_path": str(src), "year_min": 2019}))
        assert structured["filtered_count"] == 3
        doc = json.loads((out / structured["manifest"]).read_text(encoding="utf-8"))
        assert doc["inputs"][0]["manifest"] is None
        assert [r["id"] for r in doc["records"]] == [
            "doi:10.0/hal.0", "doi:10.0/hal.1", "doi:10.0/hal.2"]


class TestSerialization:
    def test_compact_records_valid_json(self):
        doc = {"format": mf.MANIFEST_FORMAT, "version": 1,
               "query": {"text": "a \"guillemet\" et é"},
               "records": [{"position": i + 1, "id": f"doi:10.0/x.{i}",
                            "native": [{"source": "hal", "rank": i + 1,
                                        "score": 1.5}]} for i in range(3)]}
        data = mf.manifest_bytes(doc)
        assert json.loads(data) == doc
        lines = data.decode("utf-8").splitlines()
        assert sum(1 for l in lines if l.lstrip().startswith('{"position"')) == 3

    def test_without_records(self):
        doc = {"format": mf.MANIFEST_FORMAT, "version": 1, "records": []}
        assert json.loads(mf.manifest_bytes(doc)) == doc
