"""
Tests OPT-16 : publication atomique (C33).

- temporaires dans le même dossier, nom commençant par un point (jamais
  listés, lus ni téléchargés), renommage atomique, manifeste publié en
  dernier ;
- exception avant la publication : rien n'est publié, aucun temporaire ne
  reste ;
- interruption simulée avant le manifeste : le corpus n'est jamais présenté
  comme complet (marqueur ``.<stem>.publishing``, statut « incomplet ») ;
- règle documentée : sans manifeste ni marqueur = ancien ; marqueur =
  incomplet ; marqueur périmé d'une publication achevée retiré ;
- temporaires orphelins anciens supprimés, récents conservés ;
- productions simultanées : aucun nom commun, aucune entrée d'index perdue ;
  ajouts simultanés au même manifeste : rien de perdu ; republication d'un
  même nom : fichier et manifeste cohérents.
"""

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.outputs import write_outputs, write_raw  # noqa: E402
from utils import manifest as mf  # noqa: E402
from utils.output_files import (  # noqa: E402
    corpus_kind, is_downloadable_file, is_publishable_file, is_service_path,
    is_temp_file, publishing_marker_name, stem_of_publishing_marker,
    temp_name,
)


def _items(name, n=3):
    return [{"itemType": "journalArticle", "title": f"{name} titre {i}",
             "date": "2020", "DOI": f"10.0/{name.lower()}.{i}",
             "extra": f"Sources: {name}"} for i in range(n)]


def _names(directory: Path):
    return sorted(p.name for p in directory.iterdir())


class _Crash(BaseException):
    """Interruption brutale simulée (comme un arrêt du processus)."""


# === Classification des fichiers de service ===

class TestServiceFiles:
    @pytest.mark.parametrize("name", [
        temp_name("corpus_x.json", "abc123"),
        publishing_marker_name("corpus_x"),
        ".moisson.lock",
        ".index.json",
        ".index.json.abc.tmp",
    ])
    def test_never_listed_read_or_downloaded(self, name):
        assert name.startswith(".")
        assert is_service_path(name)
        assert corpus_kind(name) is None
        assert not is_publishable_file(name)
        assert not is_downloadable_file(name)

    def test_temp_and_marker_helpers(self):
        assert is_temp_file(temp_name("corpus_x.csl.json", "0123456789ab"))
        # Motif exact (revue OPT-16) : jeton de 12 chiffres hexadécimaux.
        assert not is_temp_file(temp_name("corpus_x.csl.json", "t0k"))
        assert not is_temp_file("corpus_x.json")
        assert stem_of_publishing_marker(".corpus_x.publishing") == "corpus_x"
        assert stem_of_publishing_marker("corpus_x.publishing") is None

    def test_native_audit_dir_is_service(self):
        assert is_service_path(".native_audit/20260101_000000_abcd1234.jsonl")
        assert not is_downloadable_file(".native_audit/x.jsonl")

    def test_read_resource_refuses_temp_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        tmp = tmp_path / temp_name("corpus_x.json", "abc")
        tmp.write_text(json.dumps(_items("HAL")), encoding="utf-8")
        text = asyncio.run(mcp_server.read_resource(f"corpus://{tmp.name}"))
        assert "error" in json.loads(text)


# === Écriture atomique ===

class TestAtomicWrites:
    def test_success_leaves_no_temp_and_manifest_last(self, tmp_path):
        order = []
        real = os.replace

        def spy(src, dst):
            order.append(Path(dst).name)
            return real(src, dst)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mf.os, "replace", spy)
            files = write_outputs(_items("HAL"), tmp_path, "corpus_q_1", "q")
        assert order[-1] == "corpus_q_1.manifest.json"
        assert set(order[:-1]) == {"corpus_q_1.json", "corpus_q_1.csl.json",
                                   "corpus_q_1_stats.txt"}
        assert not [n for n in _names(tmp_path) if n.endswith(".tmp")]
        assert not (tmp_path / publishing_marker_name("corpus_q_1")).exists()
        assert files.manifest_path.name == "corpus_q_1.manifest.json"
        assert json.loads(files.json_path.read_text(encoding="utf-8")) == _items("HAL")

    def test_exception_before_commit_publishes_nothing(self, tmp_path):
        with pytest.raises(RuntimeError):
            with mf.Publication(tmp_path, "corpus_z", mf.KIND_FILTER) as pub:
                pub.add_json("corpus_z.json", _items("HAL"), role="zotero")
                assert [n for n in _names(tmp_path) if is_temp_file(n)]
                raise RuntimeError("échec pendant la production")
        assert _names(tmp_path) == []

    def test_interruption_before_manifest_is_not_complete(self, tmp_path,
                                                          monkeypatch):
        real = os.replace

        def crash_on_manifest(src, dst):
            if str(dst).endswith(".manifest.json"):
                raise _Crash()
            return real(src, dst)

        monkeypatch.setattr(mf.os, "replace", crash_on_manifest)
        with pytest.raises(_Crash):
            write_outputs(_items("HAL"), tmp_path, "corpus_crash_1", "q")
        monkeypatch.setattr(mf.os, "replace", real)
        names = _names(tmp_path)
        assert "corpus_crash_1.json" in names
        assert "corpus_crash_1.manifest.json" not in names
        assert publishing_marker_name("corpus_crash_1") in names
        assert not [n for n in names if is_temp_file(n)]
        listing = mf.list_corpora(tmp_path)
        (entry,) = listing.entries
        assert entry["status"] == mf.STATUS_INCOMPLETE
        assert entry["status"] != mf.STATUS_COMPLETE

    def test_interruption_shown_incomplete_by_corpus_list(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        (tmp_path / "corpus_k.json").write_text(json.dumps(_items("HAL")),
                                                encoding="utf-8")
        (tmp_path / publishing_marker_name("corpus_k")).write_text("",
                                                                   encoding="utf-8")
        text = asyncio.run(mcp_server._tool_corpus_list({}))
        assert "incomplet" in text and "· complet" not in text

    def test_rule_without_manifest_or_marker_is_legacy(self, tmp_path):
        (tmp_path / "corpus_old.json").write_text(json.dumps(_items("HAL")),
                                                  encoding="utf-8")
        (entry,) = mf.list_corpora(tmp_path).entries
        assert entry["status"] == mf.STATUS_LEGACY
        assert entry["item_count"] == 3

    def test_stale_marker_after_completed_publication_is_removed(self, tmp_path,
                                                                 monkeypatch):
        write_outputs(_items("HAL"), tmp_path, "corpus_s", "q")
        marker = tmp_path / publishing_marker_name("corpus_s")
        marker.write_text("", encoding="utf-8")
        old = time.time() - 7200
        os.utime(marker, (old, old))
        monkeypatch.setattr(config, "PUBLISH_TEMP_MAX_AGE_S", 3600.0)
        assert mf.cleanup_orphans(tmp_path) == 1
        assert not marker.exists()
        (entry,) = mf.list_corpora(tmp_path).entries
        assert entry["status"] == mf.STATUS_COMPLETE

    def test_marker_without_newer_manifest_is_kept(self, tmp_path, monkeypatch):
        (tmp_path / "corpus_i.json").write_text("[]", encoding="utf-8")
        marker = tmp_path / publishing_marker_name("corpus_i")
        marker.write_text("", encoding="utf-8")
        old = time.time() - 7200
        os.utime(marker, (old, old))
        monkeypatch.setattr(config, "PUBLISH_TEMP_MAX_AGE_S", 3600.0)
        assert mf.cleanup_orphans(tmp_path) == 0
        assert marker.exists()

    def test_old_orphan_temps_removed_recent_kept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PUBLISH_TEMP_MAX_AGE_S", 3600.0)
        old_tmp = tmp_path / temp_name("corpus_a.json", "0123456789ab")
        new_tmp = tmp_path / temp_name("corpus_b.json", "ba9876543210")
        for p in (old_tmp, new_tmp):
            p.write_text("[", encoding="utf-8")
        past = time.time() - 7200
        os.utime(old_tmp, (past, past))
        mf.list_corpora(tmp_path)             # le parcours de l'index nettoie
        assert not old_tmp.exists()
        assert new_tmp.exists()

    def test_publication_sweeps_old_orphans(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PUBLISH_TEMP_MAX_AGE_S", 3600.0)
        orphan = tmp_path / temp_name("corpus_a.json", "00000000ffff")
        orphan.write_text("[", encoding="utf-8")
        past = time.time() - 7200
        os.utime(orphan, (past, past))
        write_outputs(_items("HAL"), tmp_path, "corpus_b", "q")
        assert not orphan.exists()

    def test_raw_payload_written_atomically(self, tmp_path):
        path = write_raw(tmp_path, "HAL", "run1", _items("HAL"))
        assert json.loads(path.read_text(encoding="utf-8")) == _items("HAL")
        assert not [n for n in _names(path.parent) if is_temp_file(n)]

    def test_files_outside_directory_refused(self, tmp_path):
        other = tmp_path / "autre"
        other.mkdir()
        with mf.Publication(tmp_path, "corpus_q", mf.KIND_FILTER) as pub:
            with pytest.raises(ValueError):
                pub.add_json(other / "corpus_q.json", [], role="zotero")

    def test_lock_timeout(self, tmp_path):
        held = threading.Event()
        release = threading.Event()

        def holder():
            with mf.directory_lock(tmp_path):
                held.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        try:
            with pytest.raises(mf.LockTimeout):
                with mf.directory_lock(tmp_path, timeout=0.1):
                    pass
        finally:
            release.set()
            t.join()


# === Productions simultanées ===

class TestConcurrency:
    def test_parallel_productions_no_collision_no_index_loss(self, tmp_path,
                                                             monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(orchestrator, "run_source",
                            lambda name, *a, **k: _items(name))
        # Même seconde pour toutes les exécutions : seul l'uuid distingue.
        import engine.models as models
        real = models.new_run_id
        monkeypatch.setattr(mcp_server, "new_run_id",
                            lambda timestamp=None, unique=True:
                            real("20260101_000000", unique))
        mf.list_corpora(tmp_path)                 # index présent (vide)
        barrier = threading.Barrier(6)
        errors = []

        def produce():
            try:
                barrier.wait(5)
                mcp_server._run_moisson_sync("même requête", 5, ["hal"], False,
                                             True, workers=1)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=produce) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        manifests = sorted(tmp_path.glob("corpus_*.manifest.json"))
        assert len(manifests) == 6
        # L'index sur disque (sans relecture du dossier) a les six entrées.
        raw_index = json.loads((tmp_path / ".index.json").read_text(encoding="utf-8"))
        stems = {m.name[:-len(".manifest.json")] for m in manifests}
        assert stems <= set(raw_index["corpora"])
        for stem in stems:
            assert raw_index["corpora"][stem]["status"] == mf.STATUS_COMPLETE
        for m in manifests:
            doc = json.loads(m.read_text(encoding="utf-8"))
            for entry in doc["files"]:
                data = (tmp_path / entry["name"]).read_bytes()
                assert hashlib.sha256(data).hexdigest() == entry["sha256"]

    def test_concurrent_appends_to_same_manifest_lose_nothing(self, tmp_path):
        write_outputs(_items("HAL"), tmp_path, "corpus_c", "q")
        barrier = threading.Barrier(8)
        errors = []

        def append(i):
            try:
                barrier.wait(5)
                with mf.Publication(tmp_path, "corpus_c", mf.KIND_EXPORT,
                                    append=True) as pub:
                    pub.add_text(f"corpus_c_export_{i}.txt", f"export {i}",
                                 role="export")
                    pub.commit({"format": f"f{i}"})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=append, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        doc = json.loads((tmp_path / "corpus_c.manifest.json").read_text(encoding="utf-8"))
        names = {f["name"] for f in doc["files"]}
        assert {f"corpus_c_export_{i}.txt" for i in range(8)} <= names
        assert {"corpus_c.json", "corpus_c.csl.json", "corpus_c_stats.txt"} <= names
        assert len(doc["history"]) == 8
        assert doc["kind"] == mf.KIND_HARVEST

    def test_same_name_republication_is_consistent(self, tmp_path):
        barrier = threading.Barrier(4)

        def publish(i):
            barrier.wait(5)
            with mf.Publication(tmp_path, "corpus_x_filtered", mf.KIND_FILTER) as pub:
                pub.add_json("corpus_x_filtered.json", _items(f"S{i}", 50),
                             role="zotero")
                pub.commit({})

        threads = [threading.Thread(target=publish, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        doc = json.loads((tmp_path / "corpus_x_filtered.manifest.json")
                         .read_text(encoding="utf-8"))
        data = (tmp_path / "corpus_x_filtered.json").read_bytes()
        (entry,) = doc["files"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(json.loads(data)) == 50
        assert not [n for n in _names(tmp_path) if is_temp_file(n)]


# === Plusieurs processus (verrou fcntl) ===

def _publish_many(directory: str, prefix: str, n: int) -> None:
    """Processus fils : publie ``n`` corpus dans ``directory``."""
    sys.path.insert(0, str(PROJECT_ROOT))
    import config as child_config
    from engine.outputs import write_outputs as child_write
    child_config.PUBLISH_FSYNC = False
    for i in range(n):
        child_write(_items(f"{prefix}{i}"), Path(directory),
                    f"corpus_{prefix}_{i}", "q")


class TestMultiProcess:
    def test_two_processes_lose_no_index_entry(self, tmp_path):
        import multiprocessing
        mf.list_corpora(tmp_path)                 # index présent (vide)
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_publish_many,
                             args=(str(tmp_path), prefix, 5))
                 for prefix in ("p", "q")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            assert p.exitcode == 0
        raw_index = json.loads((tmp_path / ".index.json").read_text(encoding="utf-8"))
        expected = {f"corpus_{prefix}_{i}" for prefix in ("p", "q") for i in range(5)}
        assert expected <= set(raw_index["corpora"])
        assert all(raw_index["corpora"][s]["status"] == mf.STATUS_COMPLETE
                   for s in expected)
