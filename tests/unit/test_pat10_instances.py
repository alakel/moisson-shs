"""
PAT-10 (retour d'expérience du 2026-09-30) : plusieurs instances du serveur
sur le même dossier de sortie.

- Verrou de publication sans ``flock`` (NFS, SMB, FUSE) : jusqu'ici réputé
  obtenu, d'où des entrées d'index et des ajouts de manifeste perdus
  (mesuré : la moitié avec deux processus) ; désormais repli sur un
  répertoire de verrou, qui sérialise vraiment (décision DP-9).
- Registre des instances : une instance vivante au code différent est
  signalée (décision DP-10), une inscription d'un processus mort est retirée.
- Rangs d'un manifeste appliqués seulement s'il décrit le fichier lu
  (taille **et** empreinte).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import utils.manifest as M
from utils import instances as I

ROOT = Path(__file__).resolve().parents[2]


def _no_flock(*_args, **_kwargs):
    raise OSError(errno.ENOTSUP, "flock non pris en charge (simulé)")


@pytest.fixture
def without_flock(monkeypatch):
    if M._fcntl is None:  # pragma: no cover - Windows
        pytest.skip("fcntl indisponible")
    monkeypatch.setattr(M._fcntl, "flock", _no_flock)
    M._FALLBACK_WARNED.clear()


# === Verrou de repli ===

class TestDirectoryLockFallback:
    def test_lock_directory_held_then_removed(self, tmp_path, without_flock,
                                             caplog):
        lock_dir = M.dir_lock_path(tmp_path)
        with caplog.at_level(logging.WARNING, logger="utils.manifest"):
            with M.directory_lock(tmp_path, timeout=1):
                assert lock_dir.is_dir()
                owner = json.loads((lock_dir / "owner.json").read_text())
                assert owner["pid"] == os.getpid()
            with M.directory_lock(tmp_path, timeout=1):
                pass
        assert not lock_dir.exists()
        warnings = [r for r in caplog.records if "repli sur un répertoire"
                    in r.getMessage()]
        assert len(warnings) == 1          # une fois par dossier

    def test_live_owner_blocks(self, tmp_path, without_flock):
        lock_dir = M.dir_lock_path(tmp_path)
        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text(json.dumps({
            "pid": os.getppid(), "host": M.socket.gethostname()}))
        with pytest.raises(M.LockTimeout):
            with M.directory_lock(tmp_path, timeout=0.2):
                pass

    def test_dead_owner_is_stale(self, tmp_path, without_flock):
        proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True, check=True)
        dead = int(proc.stdout.strip())
        lock_dir = M.dir_lock_path(tmp_path)
        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text(json.dumps({
            "pid": dead, "host": M.socket.gethostname()}))
        with M.directory_lock(tmp_path, timeout=1):
            pass
        assert not lock_dir.exists()

    def test_old_lock_without_owner_is_stale(self, tmp_path, without_flock):
        lock_dir = M.dir_lock_path(tmp_path)
        lock_dir.mkdir()
        old = time.time() - M.DIR_LOCK_STALE_S - 60
        os.utime(lock_dir, (old, old))
        with M.directory_lock(tmp_path, timeout=1):
            pass


_WORKER = textwrap.dedent("""
    import errno, sys
    sys.path.insert(0, sys.argv[3])
    import utils.manifest as M
    def boom(*a, **k):
        raise OSError(errno.ENOTSUP, "flock non pris en charge (simulé)")
    M._fcntl.flock = boom
    from pathlib import Path
    d, who = Path(sys.argv[1]), sys.argv[2]
    shared = "corpus_partage_20260930_000000_aaaaaaaa"
    for i in range(10):
        stem = f"corpus_w{who}_{i:02d}_20260930_000000_{who}{i:07d}"
        with M.Publication(d, stem, M.KIND_FILTER) as pub:
            pub.add_json(f"{stem}.json", [{"itemType": "journalArticle",
                                           "title": f"T {who} {i}"}],
                         role="zotero")
            pub.commit({})
        with M.Publication(d, shared, M.KIND_EXPORT, append=True) as pub:
            pub.add_text(f"{shared}_{who}{i}.bib", "@misc{x}", role="export")
            pub.commit({})
""")


@pytest.mark.slow
def test_two_processes_without_flock_lose_nothing(tmp_path):
    """Deux processus × 10 publications et 10 ajouts au même manifeste, sans
    ``flock`` : aucune entrée d'index ni aucun ajout perdu."""
    if M._fcntl is None:  # pragma: no cover - Windows
        pytest.skip("fcntl indisponible")
    shared = "corpus_partage_20260930_000000_aaaaaaaa"
    with M.Publication(tmp_path, shared, M.KIND_FILTER) as pub:
        pub.add_json(f"{shared}.json", [{"itemType": "journalArticle",
                                         "title": "Partagé"}], role="zotero")
        pub.commit({})
    M.load_index(tmp_path)                 # index présent : mis à jour ensuite
    env = dict(os.environ, MOISSON_NO_DOTENV="1", PYTHONDONTWRITEBYTECODE="1")
    procs = [subprocess.Popen([sys.executable, "-c", _WORKER, str(tmp_path),
                               who, str(ROOT)], env=env)
             for who in ("a", "b")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    raw = json.loads((tmp_path / ".index.json").read_text(encoding="utf-8"))
    corpora = raw.get("corpora") or {}
    assert len([s for s in corpora if s.startswith("corpus_w")]) == 20
    manifest = M.read_manifest(tmp_path / f"{shared}.manifest.json")
    assert len(manifest.get("history") or []) >= 20
    names = {f["name"] for f in manifest.get("files") or []}
    assert len([n for n in names if n.endswith(".bib")]) == 20
    assert not M.dir_lock_path(tmp_path).exists()


# === Registre des instances ===

class TestInstanceRegistry:
    def test_register_and_list(self, tmp_path):
        I._registered.clear()
        path = I.register(tmp_path, "stdio")
        assert path.name == f"{os.getpid()}.json"
        doc = json.loads(path.read_text())
        assert doc["transport"] == "stdio" and doc["code_sha256"]
        assert any(d["pid"] == os.getpid() for d in I.list_instances(tmp_path))
        assert I.stale_warnings(tmp_path) == []

    def test_dead_instance_pruned(self, tmp_path):
        proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True, check=True)
        dead = int(proc.stdout.strip())
        d = tmp_path / I.INSTANCES_DIR
        d.mkdir()
        (d / f"{dead}.json").write_text(json.dumps({
            "pid": dead, "host": I.socket.gethostname(), "code_sha256": "x"}))
        assert I.list_instances(tmp_path) == []
        assert not (d / f"{dead}.json").exists()

    def test_live_instance_with_other_code_is_reported(self, tmp_path):
        d = tmp_path / I.INSTANCES_DIR
        d.mkdir()
        other = os.getppid()
        (d / f"{other}.json").write_text(json.dumps({
            "pid": other, "host": I.socket.gethostname(),
            "started_at": "2026-09-28T11:26:00+00:00", "version": "0.3.0",
            "commit": None, "code_sha256": "0" * 64}))
        warnings = I.stale_warnings(tmp_path)
        assert len(warnings) == 1
        assert f"PID {other}" in warnings[0] and "0.3.0" in warnings[0]

    def test_registry_is_a_service_path(self):
        from utils.output_files import is_service_path
        assert is_service_path(".instances/123.json")


# === Rangs d'un manifeste : taille et empreinte ===

def test_manifest_describes_needs_same_digest(tmp_path):
    stem = "corpus_x_20260930_000000_abcdef12"
    with M.Publication(tmp_path, stem, M.KIND_FILTER) as pub:
        pub.add_json(f"{stem}.json", [{"itemType": "book", "title": "AAAA"}],
                     role="zotero")
        pub.commit({})
    doc = M.read_manifest(tmp_path / f"{stem}.manifest.json")
    path = tmp_path / f"{stem}.json"
    assert M.manifest_describes(doc, path)
    # Même taille, autre contenu (republication concurrente) : refusé.
    text = path.read_text(encoding="utf-8").replace("AAAA", "BBBB")
    path.write_text(text, encoding="utf-8")
    assert not M.manifest_describes(doc, path)
