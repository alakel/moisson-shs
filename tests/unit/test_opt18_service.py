"""
Tests OPT-18 — service d'export de fichier (``mappers/bibtex_exporter.py``) :
contrôle du corpus d'entrée (refus clair d'un CSL-JSON, d'un fichier associé,
d'un contenu qui n'est pas un tableau de notices Zotero), contrôle du nom de
sortie, nom unique par défaut, publication atomique par le chemin réel
(``export_corpus_file`` → ``publish_export`` → ``utils.manifest.Publication``),
radical du manifeste d'un export écrit ailleurs. Hors réseau.
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mappers.bibtex_exporter as bx  # noqa: E402
from mappers.bibtex_exporter import (  # noqa: E402
    ExportInputError, check_export_output, default_export_path,
    export_corpus_file, export_manifest_stem, load_zotero_items,
)
from utils import manifest as mf  # noqa: E402
from utils.output_files import is_temp_file  # noqa: E402

ITEMS = [{"itemType": "journalArticle", "title": "Titre", "date": "2020",
          "creators": [{"creatorType": "author", "lastName": "Nom",
                        "firstName": "P"}], "extra": "Sources: HAL"}]


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class TestLoadZoteroItems:
    def test_accepts_zotero_corpus(self, tmp_path):
        p = _write(tmp_path / "corpus_x.json", ITEMS)
        assert load_zotero_items(p) == ITEMS
        assert load_zotero_items(_write(tmp_path / "corpus_e.json", [])) == []

    @pytest.mark.parametrize("name,fragment", [
        ("corpus_x.csl.json", "CSL-JSON"),
        ("corpus_x.CSL.JSON", "CSL-JSON"),
        ("corpus_x.manifest.json", "fichier associé"),
        ("corpus_x.doi_report.json", "fichier associé"),
        ("corpus_x_stats.txt", "statistiques"),
        (".index.json", "service"),
    ])
    def test_refuses_by_name(self, tmp_path, name, fragment):
        p = _write(tmp_path / name, ITEMS)
        with pytest.raises(ExportInputError) as e:
            load_zotero_items(p)
        assert fragment in str(e.value)
        assert str(tmp_path) not in str(e.value)

    def test_refuses_csl_content_under_a_zotero_name(self, tmp_path):
        csl = [{"id": "a", "type": "article-journal", "title": "T",
                "issued": {"date-parts": [[2020]]}}]
        p = _write(tmp_path / "corpus_x.json", csl)
        with pytest.raises(ExportInputError, match="CSL-JSON"):
            load_zotero_items(p)

    def test_refuses_invalid_content(self, tmp_path):
        bad = tmp_path / "corpus_bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(ExportInputError, match="parsing JSON"):
            load_zotero_items(bad)
        with pytest.raises(ExportInputError, match="tableau"):
            load_zotero_items(_write(tmp_path / "corpus_d.json", {"a": 1}))
        with pytest.raises(ExportInputError, match="pas des notices"):
            load_zotero_items(_write(tmp_path / "corpus_l.json", [1, "x"]))
        with pytest.raises(ExportInputError, match="introuvable"):
            load_zotero_items(tmp_path / "corpus_absent.json")


class TestOutputName:
    @pytest.mark.parametrize("name", ["x.json", "x.csl.json", "x_stats.txt",
                                      "X.JSON", ".cache.bib"])
    def test_reserved_names_refused(self, name):
        with pytest.raises(ExportInputError):
            check_export_output(Path(name), "bibtex", strict_suffix=False)

    def test_mismatched_extension_refused(self):
        with pytest.raises(ExportInputError, match="incohérente"):
            check_export_output(Path("x.ris"), "bibtex", strict_suffix=False)
        with pytest.raises(ExportInputError, match="incohérente"):
            check_export_output(Path("x.bib"), "ris", strict_suffix=False)

    def test_strict_suffix(self):
        check_export_output(Path("x.bib"), "biblatex", strict_suffix=True)
        check_export_output(Path("x.ris"), "ris", strict_suffix=True)
        with pytest.raises(ExportInputError, match="attendue"):
            check_export_output(Path("x.txt"), "bibtex", strict_suffix=True)
        # En local, une extension libre reste permise.
        check_export_output(Path("refs.bibtex"), "bibtex", strict_suffix=False)

    def test_default_name_is_unique(self, tmp_path):
        corpus = tmp_path / "corpus_x_20260101_000000_abcd1234.json"
        names = {default_export_path(corpus, "bibtex").name for _ in range(50)}
        assert len(names) == 50
        for name in names:
            assert re.fullmatch(
                r"corpus_x_20260101_000000_abcd1234_[0-9a-f]{8}\.bib", name)
        other = tmp_path / "out"
        other.mkdir()
        p = default_export_path(corpus, "ris", other)
        assert p.parent == other and p.suffix == ".ris"


def _visible(directory):
    """Fichiers visibles (hors verrou et fichiers de service à point)."""
    return sorted(p.name for p in directory.iterdir()
                  if not p.name.startswith("."))


class TestAtomicPublication:
    """Écriture atomique par le chemin réel : ``export_corpus_file`` publie
    par ``utils.manifest.Publication`` (plus de fonction d'écriture propre)."""

    def test_writes_exact_crlf_bytes_and_leaves_no_temp(self, tmp_path):
        corpus = _write(tmp_path / "corpus_x.json", ITEMS * 2)
        res = export_corpus_file(corpus, "ris", tmp_path / "refs.ris")
        raw = res.path.read_bytes()
        # Octets exacts : ceux du texte rendu, fins de ligne CRLF intactes
        # (aucune conversion par la publication), aucun LF isolé.
        assert raw == res.text.encode("utf-8")
        assert raw.count(b"\r\n") == raw.count(b"\n") > 0
        assert raw.endswith(b"ER  - \r\n")
        assert not [p for p in tmp_path.iterdir() if is_temp_file(p)]
        assert _visible(tmp_path) == ["corpus_x.json", "corpus_x.manifest.json",
                                      "refs.ris"]

    def test_failure_keeps_old_export_and_leaves_no_temp(self, tmp_path,
                                                         monkeypatch):
        corpus = _write(tmp_path / "corpus_x.json", ITEMS)
        target = tmp_path / "refs.bib"
        first = export_corpus_file(corpus, "bibtex", target)
        old_bytes = target.read_bytes()
        manifest = first.manifest_path
        old_manifest = manifest.read_bytes()

        def boom(src, dst):
            raise OSError("disque plein")

        # Échec au renommage final, dans Publication.commit.
        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="disque plein"):
            export_corpus_file(corpus, "bibtex", target,
                               include_provenance=True)
        monkeypatch.undo()
        assert target.read_bytes() == old_bytes          # ancien export intact
        assert manifest.read_bytes() == old_manifest      # manifeste inchangé
        assert not [p for p in tmp_path.iterdir() if is_temp_file(p)]
        assert _visible(tmp_path) == ["corpus_x.json", "corpus_x.manifest.json",
                                      "refs.bib"]

    def test_failure_before_commit_writes_nothing(self, tmp_path, monkeypatch):
        corpus = _write(tmp_path / "corpus_x.json", ITEMS)

        def boom(*args, **kwargs):
            raise OSError("disque plein")

        monkeypatch.setattr(mf.Publication, "commit", boom)
        with pytest.raises(OSError):
            export_corpus_file(corpus, "ris")
        monkeypatch.undo()
        assert not [p for p in tmp_path.iterdir() if is_temp_file(p)]
        assert _visible(tmp_path) == ["corpus_x.json"]

    def test_no_private_writer_left(self):
        # L'ancienne écriture atomique propre au module n'est plus appelée
        # en production : elle a été retirée plutôt que testée à vide.
        assert not hasattr(bx, "write_text_atomic")


class TestManifestStemElsewhere:
    """Export écrit hors du dossier du corpus source : son manifeste propre
    ne doit jamais décrire un corpus existant de même radical."""

    OTHER = [{"itemType": "journalArticle", "title": f"T{i}", "date": "2020"}
             for i in range(7)]

    def _corpora(self, directory):
        return [(e.get("stem"), e.get("status"), e.get("kind"),
                 e.get("primary"), e.get("item_count"))
                for e in mf.list_corpora(directory, rebuild=True).entries]

    def test_legacy_corpus_with_same_stem_left_untouched(self, tmp_path):
        a_dir, b_dir = tmp_path / "A", tmp_path / "B"
        a_dir.mkdir()
        b_dir.mkdir()
        corpus = _write(a_dir / "corpus_a.json", ITEMS)
        _write(b_dir / "corpus_b.json", self.OTHER)     # corpus ancien
        before = self._corpora(b_dir)
        assert before == [("corpus_b", mf.STATUS_LEGACY, None,
                           "corpus_b.json", 7)]
        res = export_corpus_file(corpus, "bibtex", b_dir / "corpus_b.bib")
        # Avant correction : corpus_b devenait « complete / export »,
        # primaire corpus_b.bib, 1 notice.
        assert self._corpora(b_dir) == before
        assert not (b_dir / "corpus_b.manifest.json").exists()
        assert res.manifest_path.name == "corpus_b.bib.manifest.json"
        doc = mf.read_manifest(res.manifest_path)
        assert doc["kind"] == mf.KIND_EXPORT
        assert doc["primary"] == "corpus_b.bib" and doc["item_count"] == 1
        assert [f["name"] for f in doc["files"]] == ["corpus_b.bib"]
        assert doc["inputs"][0]["name"] == "corpus_a.json"

    def test_published_corpus_manifest_not_completed(self, tmp_path):
        a_dir, b_dir = tmp_path / "A", tmp_path / "B"
        a_dir.mkdir()
        b_dir.mkdir()
        corpus = _write(a_dir / "corpus_a.json", ITEMS)
        other = _write(b_dir / "corpus_b.json", self.OTHER)
        # Corpus ancien de B complété par un premier export écrit à côté
        # de lui : son manifeste (type export) le décrit.
        export_corpus_file(other, "ris")
        manifest = b_dir / "corpus_b.manifest.json"
        doc_before = mf.read_manifest(manifest)
        before = self._corpora(b_dir)
        res = export_corpus_file(corpus, "bibtex", b_dir / "corpus_b.bib")
        assert mf.read_manifest(manifest) == doc_before
        assert self._corpora(b_dir) == before
        assert res.manifest_path.name == "corpus_b.bib.manifest.json"

    def test_export_manifest_under_same_stem_is_completed(self, tmp_path):
        a_dir, b_dir = tmp_path / "A", tmp_path / "B"
        a_dir.mkdir()
        b_dir.mkdir()
        corpus = _write(a_dir / "corpus_a.json", ITEMS)
        first = export_corpus_file(corpus, "bibtex", b_dir / "refs.bib")
        second = export_corpus_file(corpus, "ris", b_dir / "refs.ris")
        assert first.manifest_path == second.manifest_path == \
            b_dir / "refs.manifest.json"
        names = [f["name"] for f in mf.read_manifest(
            second.manifest_path)["files"]]
        assert sorted(names) == ["refs.bib", "refs.ris"]
        assert mf.list_corpora(b_dir, rebuild=True).entries == []

    @pytest.mark.parametrize("taken", [
        "corpus_b.json", "corpus_b.csl.json", "corpus_b_stats.txt",
        "corpus_b.doi_report.json"])
    def test_stem_taken_by_any_data_or_associated_file(self, tmp_path, taken):
        (tmp_path / taken).write_text("[]", encoding="utf-8")
        assert export_manifest_stem(tmp_path / "corpus_b.bib") == \
            "corpus_b.bib"
        assert export_manifest_stem(tmp_path / "autre.bib") == "autre"

    def test_unreadable_manifest_is_never_overwritten(self, tmp_path):
        (tmp_path / "refs.manifest.json").write_text("{", encoding="utf-8")
        assert export_manifest_stem(tmp_path / "refs.bib") == "refs.bib"

    def test_both_stems_taken_refused_without_writing(self, tmp_path):
        a_dir = tmp_path / "A"
        a_dir.mkdir()
        corpus = _write(a_dir / "corpus_a.json", ITEMS)
        b_dir = tmp_path / "B"
        b_dir.mkdir()
        for name in ("x.json", "x.bib.json"):
            (b_dir / name).write_text("[]", encoding="utf-8")
        with pytest.raises(ExportInputError, match="autre nom"):
            export_corpus_file(corpus, "bibtex", b_dir / "x.bib")
        assert sorted(p.name for p in b_dir.iterdir()) == ["x.bib.json",
                                                            "x.json"]


class TestExportCorpusFile:
    def test_bibtex_default_path(self, tmp_path):
        corpus = _write(tmp_path / "corpus_x.json", ITEMS)
        res = export_corpus_file(corpus, "bibtex")
        assert res.path.parent == tmp_path and res.path.suffix == ".bib"
        assert res.path.read_text(encoding="utf-8") == res.text
        assert res.entries == 1 and res.stats["entries"] == 1
        assert "@article{nom2020titre," in res.text
        assert res.size_bytes == len(res.text.encode("utf-8"))

    def test_ris_explicit_path_crlf(self, tmp_path):
        corpus = _write(tmp_path / "corpus_x.json", ITEMS)
        res = export_corpus_file(corpus, "ris", tmp_path / "refs.ris")
        raw = res.path.read_bytes()
        assert raw.startswith(b"TY  - JOUR\r\n") and b"\r\nER  - \r\n" in raw

    def test_refusals(self, tmp_path):
        corpus = _write(tmp_path / "corpus_x.json", ITEMS)
        with pytest.raises(ExportInputError, match="Format inconnu"):
            export_corpus_file(corpus, "endnote")
        with pytest.raises(ExportInputError, match="réservés"):
            export_corpus_file(corpus, "bibtex", corpus)
        with pytest.raises(ExportInputError, match="introuvable"):
            export_corpus_file(corpus, "bibtex", tmp_path / "absent" / "x.bib")
        (tmp_path / "d.bib").mkdir()
        with pytest.raises(ExportInputError, match="dossier"):
            export_corpus_file(corpus, "bibtex", tmp_path / "d.bib")
        # Aucune écriture sur refus.
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "corpus_x.json", "d.bib"]

    def test_render_export_dispatch(self):
        assert bx.render_export(ITEMS, "ris").startswith("TY  - JOUR")
        assert "@article{" in bx.render_export(ITEMS, "biblatex")
