"""
Tests OPT-16 : index dérivé ``OUTPUT_DIR/.index.json`` (C15).

- 400 corpus listés en moins de 50 ms, index chaud (mesure à froid
  consignée) ; aucun corpus relu quand l'index est à jour ;
- péremption détectée par radical (fichier modifié, ajouté, supprimé,
  marqueur), index illisible reconstruit, ``rebuild`` ;
- ``corpus_list(limit, pattern, since, rebuild)`` : filtres, refus d'un
  ``since`` illisible, aucun chemin en HTTP ;
- ``list_resources`` lit l'index ;
- dossier en lecture seule : listage en mémoire.
"""

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import mcp_server  # noqa: E402
from engine.outputs import write_outputs  # noqa: E402
from utils import manifest as mf  # noqa: E402


def _items(name, n=3):
    return [{"itemType": "journalArticle", "title": f"{name} titre {i}",
             "date": "2020", "DOI": f"10.0/{name.lower()}.{i}",
             "extra": f"Sources: {name}"} for i in range(n)]


def _run(coro):
    return asyncio.run(coro)


def _populate(directory: Path, n: int, *, manifests: bool = True) -> None:
    """``n`` corpus (JSON, CSL-JSON, statistiques ; manifeste ou non)."""
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        stem = f"corpus_theme{i % 7}_202601{(i % 28) + 1:02d}_120000_{i:08x}"
        if manifests:
            write_outputs(_items(f"S{i}", 5), directory, stem, f"thème {i}",
                          run_id=stem[-24:])
        else:
            (directory / f"{stem}.json").write_text(
                json.dumps(_items(f"S{i}", 5)), encoding="utf-8")
            (directory / f"{stem}.csl.json").write_text("[]", encoding="utf-8")
            (directory / f"{stem}_stats.txt").write_text("s", encoding="utf-8")


@pytest.fixture
def out(tmp_path, monkeypatch):
    base = tmp_path / "out"
    base.mkdir()
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", base)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(config, "PUBLISH_FSYNC", False)
    return base


def _forbid_reads(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("corpus relu alors que l'index est à jour")
    monkeypatch.setattr(mf, "count_items", boom)
    monkeypatch.setattr(mf, "read_manifest", boom)


# === Performance ===

class TestPerformance:
    @pytest.mark.parametrize("manifests", [True, False],
                             ids=["avec_manifeste", "anciens"])
    def test_400_corpora_under_50_ms_warm(self, out, monkeypatch, manifests):
        _populate(out, 400, manifests=manifests)
        t0 = time.perf_counter()
        cold = mf.list_corpora(out, limit=20)
        cold_s = time.perf_counter() - t0
        assert cold.total == 400 and len(cold.entries) == 20
        assert cold.index_state == "rebuilt"
        _forbid_reads(monkeypatch)
        timings = []
        for _ in range(7):
            t0 = time.perf_counter()
            warm = mf.list_corpora(out, limit=20)
            timings.append(time.perf_counter() - t0)
        assert warm.index_state == "hot" and warm.total == 400
        median = statistics.median(timings)
        print(f"\n400 corpus ({'manifestes' if manifests else 'anciens'}) : "
              f"froid {cold_s * 1000:.1f} ms, chaud médiane "
              f"{median * 1000:.1f} ms (min {min(timings) * 1000:.1f} ms)")
        assert median < 0.050, timings

    def test_corpus_list_tool_warm_does_not_reread(self, out, monkeypatch):
        _populate(out, 30)
        _run(mcp_server._tool_corpus_list({}))
        _forbid_reads(monkeypatch)
        text = _run(mcp_server._tool_corpus_list({"limit": 5}))
        assert "5 corpus affiché(s) sur 30" in text

    def test_list_resources_reads_the_index(self, out, monkeypatch):
        _populate(out, 3)
        mf.list_corpora(out)
        _forbid_reads(monkeypatch)
        resources = _run(mcp_server.list_resources())
        assert len(resources) == 6                 # JSON + CSL-JSON
        assert all("5 items" in r.description for r in resources)


# === Péremption ===

class TestStaleness:
    def test_modified_added_removed(self, out):
        write_outputs(_items("A"), out, "corpus_a", "a")
        write_outputs(_items("B"), out, "corpus_b", "b")
        first = {e["stem"]: e for e in mf.list_corpora(out).entries}
        assert first["corpus_a"]["status"] == mf.STATUS_COMPLETE
        # Modification après publication : taille différente du manifeste.
        with open(out / "corpus_a.json", "a", encoding="utf-8") as fh:
            fh.write("\n")
        # Ajout d'un corpus ancien, suppression d'un corpus.
        (out / "corpus_c.json").write_text(json.dumps(_items("C", 4)),
                                           encoding="utf-8")
        for p in out.glob("corpus_b*"):
            p.unlink()
        listing = mf.list_corpora(out)
        entries = {e["stem"]: e for e in listing.entries}
        assert listing.index_state == "refreshed"
        assert entries["corpus_a"]["status"] == mf.STATUS_MODIFIED
        assert entries["corpus_c"]["status"] == mf.STATUS_LEGACY
        assert entries["corpus_c"]["item_count"] == 4
        assert "corpus_b" not in entries

    def test_missing_described_file_is_incomplete(self, out):
        write_outputs(_items("A"), out, "corpus_a", "a")
        (out / "corpus_a.csl.json").unlink()
        (entry,) = mf.list_corpora(out).entries
        assert entry["status"] == mf.STATUS_INCOMPLETE

    def test_invalid_index_is_rebuilt(self, out):
        write_outputs(_items("A"), out, "corpus_a", "a")
        (out / ".index.json").write_text('["pas un index"]', encoding="utf-8")
        listing = mf.list_corpora(out)
        assert listing.index_state == "rebuilt"
        assert [e["stem"] for e in listing.entries] == ["corpus_a"]
        doc = json.loads((out / ".index.json").read_text(encoding="utf-8"))
        assert doc["format"] == "moisson-shs/index"

    def test_rebuild_rereads_everything(self, out, monkeypatch):
        (out / "corpus_old.json").write_text(json.dumps(_items("O")),
                                             encoding="utf-8")
        mf.list_corpora(out)
        calls = []
        real = mf.count_items
        monkeypatch.setattr(mf, "count_items",
                            lambda p: calls.append(p) or real(p))
        mf.list_corpora(out)
        assert calls == []
        mf.list_corpora(out, rebuild=True)
        assert len(calls) == 1

    def test_publication_updates_existing_index_entry(self, out):
        mf.list_corpora(out)
        write_outputs(_items("A"), out, "corpus_a", "a")
        doc = json.loads((out / ".index.json").read_text(encoding="utf-8"))
        assert doc["corpora"]["corpus_a"]["status"] == mf.STATUS_COMPLETE
        assert doc["corpora"]["corpus_a"]["item_count"] == 3

    def test_read_only_directory_lists_in_memory(self, out):
        (out / "corpus_ro.json").write_text(json.dumps(_items("R")),
                                            encoding="utf-8")
        os.chmod(out, 0o555)
        try:
            listing = mf.list_corpora(out)
        finally:
            os.chmod(out, 0o755)
        assert listing.index_state == "memory"
        assert [e["stem"] for e in listing.entries] == ["corpus_ro"]


# === Outil corpus_list ===

class TestCorpusListTool:
    def test_limit_pattern_since(self, out):
        for stem, query in (("corpus_discours_20240101_100000", "discours"),
                            ("corpus_ecologie_20250601_100000", "écologie"),
                            ("corpus_numerique_20260105_100000", "numérique")):
            (out / f"{stem}.json").write_text(json.dumps(_items("X")),
                                              encoding="utf-8")
        text = _run(mcp_server._tool_corpus_list({"limit": 1}))
        assert "1 corpus affiché(s) sur 3" in text
        assert "corpus_numerique_20260105_100000" in text   # le plus récent
        text = _run(mcp_server._tool_corpus_list({"pattern": "ÉCOLOG"}))
        assert "corpus_ecologie" in text and "corpus_discours" not in text
        text = _run(mcp_server._tool_corpus_list({"pattern": "corpus_*_2024*"}))
        assert "corpus_discours" in text and "corpus_numerique" not in text
        text = _run(mcp_server._tool_corpus_list({"since": "2025-01"}))
        assert "corpus_discours" not in text
        assert "corpus_ecologie" in text and "corpus_numerique" in text
        assert "Requête (d'après le nom) : discours" in _run(
            mcp_server._tool_corpus_list({"pattern": "discours"}))

    def test_invalid_arguments_are_tool_errors(self, out):
        for args in ({"since": "hier"}, {"limit": 0}, {"limit": "dix"}):
            result = _run(mcp_server.call_tool("corpus_list", args))
            assert result.isError, args
            assert result.content[0].text.startswith("Paramètre refusé")

    def test_manifest_corpus_shows_kind_status_query(self, out):
        write_outputs(_items("A"), out, "corpus_a_20260101_000000_abcd1234",
                      "discours numérique",
                      manifest={"query": {"text": "discours numérique"}})
        text = _run(mcp_server._tool_corpus_list({}))
        assert "moisson · complet" in text
        assert "Requête : discours numérique" in text
        assert "Items : 3" in text
        # Fichiers associés jamais listés (le nom du test figure dans le
        # chemin du dossier, d'où un contrôle sur le nom du fichier).
        assert "corpus_a_20260101_000000_abcd1234.manifest.json" not in text

    def test_http_no_path(self, out, monkeypatch, tmp_path):
        write_outputs(_items("A"), out, "corpus_a", "a")
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        result = _run(mcp_server.call_tool("corpus_list", {}))
        text = result[0].text
        assert str(tmp_path) not in text and str(out.resolve()) not in text
        assert "corpus_a.json" in text

    def test_rebuild_argument(self, out):
        (out / "corpus_z.json").write_text("[]", encoding="utf-8")
        text = _run(mcp_server._tool_corpus_list({"rebuild": True}))
        assert "corpus_z" in text

    def test_empty_directory(self, out):
        text = _run(mcp_server._tool_corpus_list({}))
        assert text.startswith("Aucun corpus trouvé")
