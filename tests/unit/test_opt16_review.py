"""
Tests OPT-16 : corrections de la revue (manifeste, publication, index).

- filet anti-chemins : un dossier de publication RELATIF (``./output``,
  défaut de la CLI) n'est jamais retiré comme une sous-chaîne des noms, de la
  requête ou des identifiants ; seuls des préfixes de chemin absolus sont
  remplacés ; un ajout ne repasse pas le manifeste existant au filet ;
- nettoyage des temporaires : seul le motif exact du moissonneur est
  supprimé, jamais un ``.<x>.tmp`` d'un autre logiciel (dossier de
  l'utilisateur en stdio, ``OUTPUT_DIR`` pendant un listage) ;
- marqueur de publication resté après le renommage du manifeste (arrêt
  brutal réel, processus tué) : reconnu comme périmé et retiré ;
- ``LockTimeout`` n'est pas une échéance serveur ;
- corpus ancien complété par une conversion CSL ou un export : date
  d'origine, type « ancien », requête déduite du nom ;
- index : un radical sans fichier de données ne fait pas réécrire l'index ;
- audit natif : limite OAI-PMH (Sickle) signalée dans le manifeste.

Aucun accès réseau.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import main as main_module  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.outputs import write_outputs  # noqa: E402
from utils import manifest as mf  # noqa: E402
from utils.output_files import (  # noqa: E402
    INDEX_NAME, is_temp_file, publishing_marker_name, temp_name,
)


def _run(coro):
    return asyncio.run(coro)


def _items(name="HAL", n=3, title="titre"):
    return [{"itemType": "journalArticle", "title": f"{name} {title} {i}",
             "date": "2020", "DOI": f"10.0/{name.lower()}.{i}",
             "creators": [{"creatorType": "author", "lastName": f"{name}{i}"}],
             "extra": f"Sources: {name}"} for i in range(n)]


def _write(path: Path, items) -> Path:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def _doc(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _hex():
    return uuid.uuid4().hex[:12]


def _text(result) -> str:
    content = getattr(result, "content", None)
    if content is not None and hasattr(result, "isError"):
        blocks = content
    else:
        blocks = result[0] if isinstance(result, tuple) else result
    if isinstance(blocks, str):
        return blocks
    return "\n".join(getattr(b, "text", "") or "" for b in blocks)


@pytest.fixture
def stdio(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", out)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    return out


# === Filet anti-chemins : dossiers relatifs, préfixes seulement ===

class TestRelativeDirectoryScrub:
    @pytest.mark.parametrize("outdir,query", [
        ("./output", "research output"),
        ("output", "scientific output"),
        ("./corpus", "discours numérique"),
    ])
    def test_relative_output_dir_keeps_names_and_query(self, tmp_path,
                                                       monkeypatch, outdir,
                                                       query):
        monkeypatch.chdir(tmp_path)
        out = Path(outdir)                  # comme argparse type=Path
        slug = main_module.slugify(query)
        stem = f"corpus_{slug}_20260929_120000_abcd1234"
        files = write_outputs(_items(title=f"{query} mesure"), out, stem, query,
                              manifest={"query": {"text": query},
                                        "records": mf.simple_records(_items())})
        doc = _doc(files.manifest_path)
        on_disk = sorted(p.name for p in out.iterdir()
                         if not p.name.startswith("."))
        assert doc["stem"] == stem
        assert doc["primary"] == f"{stem}.json"
        assert {f["name"] for f in doc["files"]} | {
            f"{stem}.manifest.json"} == set(on_disk)
        assert doc["query"]["text"] == query
        assert [r["id"] for r in doc["records"]] == [
            r["id"] for r in mf.simple_records(_items())]
        (entry,) = mf.list_corpora(out).entries
        assert entry["status"] == mf.STATUS_COMPLETE, entry["notes"]

    def test_cli_default_output_dir(self, tmp_path, monkeypatch):
        """Bout en bout : ``main.main()`` avec ``--output`` par défaut
        (``./output``) et une requête qui contient « output »."""
        monkeypatch.chdir(tmp_path)

        def runner(name, *a, **k):
            return [{"itemType": "journalArticle",
                     "title": f"Scientific output {name}", "date": "2020",
                     "creators": [{"lastName": "Doe", "creatorType": "author"}],
                     "DOI": f"10.1234/{name}", "extra": f"Sources: {name}"}]

        monkeypatch.setattr(orchestrator, "run_source", runner)
        monkeypatch.setattr(sys, "argv", ["main.py", "scientific output",
                                          "--sources", "hal", "--max", "5"])
        assert main_module.main() == 0
        out = tmp_path / "output"
        (manifest,) = out.glob("corpus_*.manifest.json")
        doc = _doc(manifest)
        assert manifest.name == f"{doc['stem']}.manifest.json"
        assert "scientific_output" in doc["stem"]
        assert doc["query"]["text"] == "scientific output"
        assert all((out / f["name"]).is_file() for f in doc["files"])
        (entry,) = mf.list_corpora(out).entries
        assert entry["status"] == mf.STATUS_COMPLETE, entry["notes"]

    def test_append_with_relative_dir_keeps_existing_manifest(self, tmp_path,
                                                              monkeypatch):
        stem = "corpus_research_output_20260929_000000_abcd1234"
        out = tmp_path / "output"
        files = write_outputs(_items(), out, stem, "research output",
                              manifest={"query": {"text": "research output"}})
        before = _doc(files.manifest_path)
        monkeypatch.chdir(tmp_path)
        rel = Path("output")
        with mf.Publication(rel, stem, mf.KIND_EXPORT, append=True,
                            path_roots=[rel]) as pub:
            pub.add_bytes(f"{stem}_0a1b2c3d.bib", b"@article{x,}\n",
                          role="export", items=1)
            pub.commit({"format": "bibtex",
                        "notes": [f"écrit dans {out}/{stem}_0a1b2c3d.bib"]})
        after = _doc(files.manifest_path)
        for key in ("stem", "primary", "query", "started_at", "finished_at"):
            assert after[key] == before[key], key
        assert [f["name"] for f in after["files"][:3]] == [
            f["name"] for f in before["files"]]
        assert after["history"][-1]["notes"] == [
            f"écrit dans {stem}_0a1b2c3d.bib"]
        assert not mf.absolute_paths_in(after)
        (entry,) = mf.list_corpora(out).entries
        assert entry["status"] == mf.STATUS_COMPLETE, entry["notes"]


class TestScrubPathsPrefixOnly:
    def test_absolute_prefix_becomes_relative(self, tmp_path):
        root = tmp_path / "output"
        doc = {"a": f"{root}/corpus_x.json", "b": f"voir {root}{os.sep}y.json",
               "c": str(root), "d": [f"{root.resolve()}/z.txt"]}
        clean = mf.scrub_paths(doc, [root])
        assert clean == {"a": "corpus_x.json", "b": "voir y.json", "c": "",
                         "d": ["z.txt"]}

    def test_relative_root_never_removed_as_a_word(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        doc = {"stem": "corpus_research_output_1", "q": "output corpus output",
               "url": "https://example.org/output/1"}
        assert mf.scrub_paths(doc, [Path("output"), Path("corpus")]) == doc

    def test_sibling_directory_with_same_prefix_untouched(self, tmp_path):
        root = tmp_path / "out"
        other = f"{tmp_path}/output/x.json"
        suffixed = f"{root}.json"
        doc = {"o": other, "s": suffixed}
        assert mf.scrub_paths(doc, [root]) == doc

    def test_path_inside_other_absolute_path_untouched(self, tmp_path):
        root = (tmp_path / "a").resolve()
        value = f"/autre{root}/x.json"      # la racine n'y commence pas un chemin
        assert mf.scrub_paths({"v": value}, [root]) == {"v": value}


# === Temporaires : motif exact, fichiers étrangers jamais supprimés ===

FOREIGN_TMP = (".sauvegarde.tmp", ".DS_Store.tmp", ".swap.x.tmp",
               ".brouillon.0123456789ab.tmp", ".x.abc.tmp",
               ".notes.txt.0123456789ab.tmp")


def _age(path: Path, seconds: float = 7200) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


class TestForeignTempFiles:
    @pytest.mark.parametrize("name", FOREIGN_TMP)
    def test_foreign_names_are_not_temps(self, name):
        assert not is_temp_file(name)

    @pytest.mark.parametrize("final", [
        "corpus_x.json", "corpus_x.csl.json", "corpus_x_stats.txt",
        "corpus_x.manifest.json", "corpus_x.doi_report.json",
        "corpus_x_0a1b2c3d.bib", "biblio.ris", INDEX_NAME, "hal_1.json"])
    def test_own_temps_recognised(self, final):
        assert is_temp_file(temp_name(final, _hex()))
        assert not is_temp_file(temp_name(final, "abc"))
        # Jeton qui contient forcément une lettre : un uuid4 tronqué à 12
        # chiffres hexadécimaux n'en contient aucune dans ≈ 0,36 % des cas,
        # et .upper() ne changerait alors rien (échec aléatoire).
        assert not is_temp_file(temp_name(final, ("a" + _hex()[1:]).upper()))

    def test_written_temp_matches_motif(self, tmp_path):
        tmp = mf.write_temp_bytes(tmp_path, "corpus_x.json", b"[]")
        assert is_temp_file(tmp.name)
        tmp = mf.write_temp_bytes(tmp_path, INDEX_NAME, b"{}")
        assert is_temp_file(tmp.name)

    def _plant(self, directory: Path):
        foreign = []
        for name in FOREIGN_TMP:
            p = directory / name
            p.write_text("données d'un autre logiciel", encoding="utf-8")
            _age(p)
            foreign.append(p)
        own = directory / temp_name("corpus_old.json", _hex())
        own.write_text("[", encoding="utf-8")
        _age(own)
        return foreign, own

    def test_filter_in_user_folder_keeps_foreign_tmp(self, stdio, tmp_path,
                                                     monkeypatch):
        monkeypatch.setattr(config, "PUBLISH_TEMP_MAX_AGE_S", 3600.0)
        user = tmp_path / "Documents"
        user.mkdir()
        src = _write(user / "ma_biblio.json", _items())
        foreign, own = self._plant(user)
        mcp_server._corpus_filter_sync({"corpus_path": str(src),
                                        "year_min": 2019})
        assert (user / "ma_biblio_filtered.json").is_file()
        assert all(p.exists() for p in foreign)
        assert not own.exists()             # orphelin du moissonneur retiré

    def test_listing_keeps_foreign_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PUBLISH_TEMP_MAX_AGE_S", 3600.0)
        _write(tmp_path / "corpus_a.json", _items())
        foreign, own = self._plant(tmp_path)
        mf.list_corpora(tmp_path)
        assert all(p.exists() for p in foreign)
        assert not own.exists()

    def test_cleanup_orphans_keeps_foreign_tmp(self, tmp_path, monkeypatch):
        foreign, own = self._plant(tmp_path)
        assert mf.cleanup_orphans(tmp_path, max_age=3600.0) == 1
        assert all(p.exists() for p in foreign)
        assert not own.exists()


# === Marqueur resté après le renommage du manifeste ===

_KILL_CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import utils.manifest as mf

def killed(directory):
    # Appelé juste après le renommage du manifeste, avant le retrait du
    # marqueur : arrêt brutal du processus (aucun nettoyage Python).
    os._exit(17)

mf._fsync_dir = killed
d = Path(sys.argv[2])
with mf.Publication(d, "corpus_kill_1", mf.KIND_HARVEST, run_id="r1") as pub:
    pub.add_json("corpus_kill_1.json", [{"itemType": "book", "title": "t"}],
                 role="zotero")
    pub.commit({"query": {"text": "q"}})
os._exit(0)
"""


def _killed_publication(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-c", _KILL_CHILD, str(PROJECT_ROOT), str(directory)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 17, proc.stderr


class TestStaleMarkerAfterManifest:
    def test_hard_kill_leaves_marker_older_or_equal_to_manifest(self, tmp_path):
        d = tmp_path / "k"
        _killed_publication(d)
        marker = d / publishing_marker_name("corpus_kill_1")
        manifest = d / "corpus_kill_1.manifest.json"
        assert marker.exists() and manifest.exists()
        assert manifest.stat().st_mtime_ns >= marker.stat().st_mtime_ns

    def test_listing_under_lock_removes_it(self, tmp_path):
        d = tmp_path / "k"
        _killed_publication(d)
        (entry,) = mf.list_corpora(d).entries
        assert entry["status"] == mf.STATUS_COMPLETE, entry["notes"]
        assert not (d / publishing_marker_name("corpus_kill_1")).exists()

    def test_cleanup_orphans_removes_it_once_old(self, tmp_path):
        d = tmp_path / "k"
        _killed_publication(d)
        marker = d / publishing_marker_name("corpus_kill_1")
        assert mf.cleanup_orphans(d, max_age=3600.0) == 0   # trop récent
        assert marker.exists()
        # Vieillissement du marqueur ET du manifeste (même écart) : l'ordre
        # réel des dates est conservé.
        for p in (marker, d / "corpus_kill_1.manifest.json"):
            st = p.stat()
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns - 7200 * 10**9))
        assert mf.cleanup_orphans(d, max_age=3600.0) == 1
        assert not marker.exists()

    def test_python_interruption_after_manifest_removes_marker(self, tmp_path,
                                                               monkeypatch):
        class Stop(BaseException):
            pass

        def stop(directory):
            raise Stop()

        monkeypatch.setattr(mf, "_fsync_dir", stop)
        with pytest.raises(Stop):
            write_outputs(_items(), tmp_path, "corpus_m_1", "q")
        monkeypatch.undo()
        assert (tmp_path / "corpus_m_1.manifest.json").exists()
        assert not (tmp_path / publishing_marker_name("corpus_m_1")).exists()
        (entry,) = mf.list_corpora(tmp_path).entries
        assert entry["status"] == mf.STATUS_COMPLETE

    def test_interruption_before_any_rename_keeps_previous_corpus(self, tmp_path,
                                                                  monkeypatch):
        write_outputs(_items(), tmp_path, "corpus_r_1", "q")
        before = sorted(os.listdir(tmp_path))

        class Stop(BaseException):
            pass

        real = os.replace

        def stop_first(src, dst):
            raise Stop()

        monkeypatch.setattr(mf.os, "replace", stop_first)
        with pytest.raises(Stop):
            write_outputs(_items("HAL", 5), tmp_path, "corpus_r_1", "q")
        monkeypatch.setattr(mf.os, "replace", real)
        assert sorted(os.listdir(tmp_path)) == before
        (entry,) = mf.list_corpora(tmp_path).entries
        assert entry["status"] == mf.STATUS_COMPLETE
        assert entry["item_count"] == 3

    def test_interrupted_republication_marker_is_kept(self, tmp_path):
        """Marqueur plus récent que le manifeste (republication interrompue
        après un renommage) : conservé, corpus incomplet."""
        write_outputs(_items(), tmp_path, "corpus_p_1", "q")
        manifest = tmp_path / "corpus_p_1.manifest.json"
        st = manifest.stat()
        os.utime(manifest, ns=(st.st_atime_ns, st.st_mtime_ns - 10 * 10**9))
        marker = tmp_path / publishing_marker_name("corpus_p_1")
        marker.write_text("r2 x\n", encoding="utf-8")
        (entry,) = mf.list_corpora(tmp_path).entries
        assert entry["status"] == mf.STATUS_INCOMPLETE
        assert marker.exists()


# === LockTimeout n'est pas une échéance ===

class TestLockTimeoutIsNotADeadline:
    def test_not_a_timeout_error(self):
        assert not issubclass(mf.LockTimeout, TimeoutError)
        assert not issubclass(mf.LockTimeout, asyncio.TimeoutError)

    @staticmethod
    def _hold(directory):
        held, release = threading.Event(), threading.Event()

        def holder():
            with mf.directory_lock(directory):
                held.set()
                release.wait(10)

        t = threading.Thread(target=holder)
        t.start()
        assert held.wait(5)
        return t, release

    def test_guarded_producer_reports_lock_not_deadline(self, tmp_path,
                                                        monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(config, "PUBLISH_LOCK_TIMEOUT_S", 0.2)

        def producer():
            with mf.Publication(tmp_path, "corpus_x", mf.KIND_HARVEST) as pub:
                pub.add_json("corpus_x.json", [], role="zotero")
                return pub.commit({})

        t, release = self._hold(tmp_path)
        try:
            with pytest.raises(mf.LockTimeout) as err:
                _run(mcp_server._run_guarded(
                    lambda deadline: producer, cancel_event=threading.Event(),
                    label="Moisson", deadline_note="aucun corpus partiel publié"))
        finally:
            release.set()
            t.join()
        assert "rien n'a été publié" in str(err.value)
        assert not list(tmp_path.glob("corpus_x*"))

    def test_tool_error_says_lock(self, stdio, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PUBLISH_LOCK_TIMEOUT_S", 0.2)
        src = _write(stdio / "corpus_l.json", _items())
        t, release = self._hold(stdio)
        try:
            result = _run(mcp_server.call_tool(
                "corpus_filter", {"corpus_path": str(src), "year_min": 2019}))
        finally:
            release.set()
            t.join()
        assert getattr(result, "isError", False) is True
        text = _text(result)
        assert "verrou de publication" in text
        assert "rien n'a été publié" in text
        assert "deadline" not in text
        assert not (stdio / "corpus_l_filtered.json").exists()
        assert not [n for n in os.listdir(stdio) if is_temp_file(n)]


# === Corpus ancien complété par un ajout ===

class TestLegacyCorpusCompleted:
    def test_to_csl_keeps_origin_date_and_query_from_name(self, stdio):
        _write(stdio / "corpus_ecologie_20260901_090000.json", _items("ISI"))
        src = _write(stdio / "corpus_discours_numerique_20250101_120000.json",
                     _items())
        _run(mcp_server._tool_corpus_to_csl({"corpus_path": str(src)}))
        doc = _doc(stdio / "corpus_discours_numerique_20250101_120000.manifest.json")
        origin = datetime(2025, 1, 1, 12, 0, 0).timestamp()
        assert doc["kind"] == mf.KIND_LEGACY
        assert doc["run_id"] is None
        assert mf._ts_from_iso(doc["finished_at"]) == origin
        assert doc["origin"]["date_source"] == "name"
        assert doc["history"][-1]["kind"] == mf.KIND_CSL
        assert doc["history"][-1]["inputs"][0]["name"] == src.name
        assert len(doc["records"]) == 3
        listing = mf.list_corpora(stdio)
        first, second = listing.entries
        assert first["stem"] == "corpus_ecologie_20260901_090000"
        assert second["stem"] == "corpus_discours_numerique_20250101_120000"
        assert second["status"] == mf.STATUS_COMPLETE
        assert second["created_ts"] == origin
        assert second["query_from_name"] == "discours numerique"
        assert [e["stem"] for e in mf.list_corpora(stdio, since="2026").entries] \
            == ["corpus_ecologie_20260901_090000"]
        text = _run(mcp_server._tool_corpus_list({}))
        assert "Date : 2025-01-01 12:00:00" in text
        assert "Requête (d'après le nom) : discours numerique" in text
        assert "conversion CSL" not in text

    def test_origin_from_file_date_without_timestamp_in_name(self, stdio):
        src = _write(stdio / "corpus_vieux.json", _items())
        past = datetime(2024, 3, 1, 8, 0, 0).timestamp()
        os.utime(src, (past, past))
        _run(mcp_server._tool_corpus_to_csl({"corpus_path": str(src)}))
        doc = _doc(stdio / "corpus_vieux.manifest.json")
        assert doc["kind"] == mf.KIND_LEGACY
        assert doc["origin"]["date_source"] == "mtime"
        assert mf._ts_from_iso(doc["finished_at"]) == past

    def test_export_on_legacy_corpus(self, tmp_path):
        from mappers.bibtex_exporter import export_corpus_file
        src = _write(tmp_path / "corpus_ancien_20250101_101010.json", _items())
        export_corpus_file(src, "bibtex")
        doc = _doc(tmp_path / "corpus_ancien_20250101_101010.manifest.json")
        assert doc["kind"] == mf.KIND_LEGACY
        assert doc["history"][-1]["kind"] == mf.KIND_EXPORT
        assert mf._ts_from_iso(doc["finished_at"]) == datetime(
            2025, 1, 1, 10, 10, 10).timestamp()

    def test_export_elsewhere_is_not_legacy(self, tmp_path):
        from mappers.bibtex_exporter import export_corpus_file
        src = _write(tmp_path / "corpus_x.json", _items())
        other = tmp_path / "ailleurs"
        other.mkdir()
        export_corpus_file(src, "bibtex", other / "biblio.bib")
        doc = _doc(other / "biblio.manifest.json")
        assert doc["kind"] == mf.KIND_EXPORT

    def test_merge_has_no_query_from_name(self, stdio):
        a = _write(stdio / "corpus_a.json", _items("HAL"))
        b = _write(stdio / "corpus_b.json", _items("ISI"))
        _run(mcp_server._tool_corpus_merge({"corpus_paths": [str(a), str(b)]}))
        merged = [e for e in mf.list_corpora(stdio).entries
                  if e["kind"] == mf.KIND_MERGE]
        assert merged and merged[0]["query_from_name"] is None


# === Index : radicaux sans fichier de données ===

class TestIndexIgnoredStems:
    def test_orphan_associated_file_does_not_rewrite_index(self, tmp_path,
                                                           monkeypatch):
        for i in range(5):
            _write(tmp_path / f"corpus_c{i}.json", _items())
        orphan = tmp_path / "corpus_old_doi.doi_report.json"
        orphan.write_text("{}", encoding="utf-8")
        mf.list_corpora(tmp_path)                    # construction
        writes = []
        real = mf._write_index

        def spy(*a, **k):
            writes.append(1)
            return real(*a, **k)

        monkeypatch.setattr(mf, "_write_index", spy)
        states = {mf.list_corpora(tmp_path).index_state for _ in range(5)}
        assert states == {"hot"} and writes == []
        orphan.write_text('{"x": 1}', encoding="utf-8")   # signature changée
        assert mf.list_corpora(tmp_path).index_state == "refreshed"
        assert mf.list_corpora(tmp_path).index_state == "hot"
        orphan.unlink()
        assert mf.list_corpora(tmp_path).index_state == "refreshed"
        assert mf.list_corpora(tmp_path).index_state == "hot"
        assert len(writes) == 2

    def test_orphan_becomes_corpus_when_data_arrives(self, tmp_path):
        (tmp_path / "corpus_z.doi_report.json").write_text("{}", encoding="utf-8")
        assert mf.list_corpora(tmp_path).entries == []
        _write(tmp_path / "corpus_z.json", _items())
        (entry,) = mf.list_corpora(tmp_path).entries
        assert entry["stem"] == "corpus_z"


# === Audit natif : limite OAI-PMH signalée ===

class TestAuditOaiLimit:
    def _result(self, enrich, sources):
        return SimpleNamespace(
            request=SimpleNamespace(enrich_oai=enrich, query="q"),
            sources=[SimpleNamespace(source=s, display_name=s.title())
                     for s in sources],
            items=[], ranking=None)

    def test_flag_when_oai_enrichment_active(self, tmp_path):
        audit = mf.NativeAudit(tmp_path, "run1")
        fields = mf.harvest_manifest_fields(
            self._result(True, ["hal", "cairn", "persee"]), native_audit=audit)
        nc = fields["native_audit"]["not_captured"]
        assert nc["oai_pmh_getrecord"] == ["cairn", "persee"]

    @pytest.mark.parametrize("enrich,sources", [
        (False, ["cairn"]), (True, ["hal", "openalex"])])
    def test_no_flag_otherwise(self, tmp_path, enrich, sources):
        audit = mf.NativeAudit(tmp_path, "run1")
        fields = mf.harvest_manifest_fields(self._result(enrich, sources),
                                            native_audit=audit)
        assert "not_captured" not in fields["native_audit"]
