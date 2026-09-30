"""
OPT-06 (C22) : ``merge_corpora`` indexe les corpus par identité de fichier
(chemin résolu), et non plus par nom ; rapport lisible (nom et dossier
parent), sans chemin disque absolu.
"""

import json

from utils.corpus_ops import corpus_labels, format_merge_report, merge_corpora


def _write(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def _item(title, doi=None, source="HAL"):
    it = {"itemType": "journalArticle", "title": title,
          "creators": [{"creatorType": "author", "lastName": "Rabatel"}],
          "date": "2015", "extra": f"Sources: {source}"}
    if doi:
        it["DOI"] = doi
    return it


def test_homonymous_files_in_two_folders_are_two_corpora(tmp_path):
    """Échoue sur l'ancienne indexation par path.name : le second corpus
    recouvrait silencieusement le premier."""
    a = _write(tmp_path / "moisson_a" / "corpus.json",
               [_item("Premier travail", "10.1/a")])
    b = _write(tmp_path / "moisson_b" / "corpus.json",
               [_item("Second travail", "10.1/b"), _item("Troisième", "10.1/c")])
    merged, report = merge_corpora([a, b])
    assert len(merged) == 3
    assert report["_summary"]["total_loaded"] == 3
    assert report["_summary"]["corpora_count"] == 2
    assert report["moisson_a/corpus.json"]["loaded"] == 1
    assert report["moisson_b/corpus.json"]["loaded"] == 2


def test_same_file_given_twice_is_loaded_once(tmp_path):
    a = _write(tmp_path / "x" / "c1.json", [_item("Travail", "10.1/a")])
    alias = tmp_path / "x" / ".." / "x" / "c1.json"
    merged, report = merge_corpora([a, alias])
    assert len(merged) == 1
    summary = report["_summary"]
    assert summary["total_loaded"] == 1
    assert summary["corpora_count"] == 1
    assert summary["inputs_count"] == 2
    assert summary["duplicate_inputs"] == 1


def test_report_has_no_absolute_path(tmp_path):
    a = _write(tmp_path / "dossier" / "c1.json", [_item("Travail", "10.1/a")])
    _merged, report = merge_corpora([a])
    entry = report["c1.json"]
    assert entry["file"] == "dossier/c1.json"
    text = json.dumps(report) + format_merge_report(report)
    assert str(tmp_path) not in text


def test_unique_and_exclusive_counts(tmp_path):
    shared = _item("Travail partagé", "10.1/shared")
    a = _write(tmp_path / "c1.json", [shared, _item("Seulement ici", "10.1/a")])
    b = _write(tmp_path / "c2.json", [dict(shared, extra="Sources: OpenAlex")])
    merged, report = merge_corpora([a, b])
    assert len(merged) == 2
    assert report["c1.json"] == {"loaded": 2, "unique_ids": 2,
                                 "only_to_this_corpus": 1, "file": f"{tmp_path.name}/c1.json"}
    assert report["c2.json"]["unique_ids"] == 1
    assert report["c2.json"]["only_to_this_corpus"] == 0
    assert report["_summary"]["duplicates_removed"] == 1


def test_labels_disambiguate_with_parents():
    from pathlib import Path
    paths = [Path("/r/a/x/corpus.json"), Path("/r/b/x/corpus.json"),
             Path("/r/b/other.json")]
    assert corpus_labels(paths) == ["a/x/corpus.json", "b/x/corpus.json",
                                    "other.json"]


def test_format_shows_folder_for_homonyms(tmp_path):
    a = _write(tmp_path / "m1" / "corpus.json", [_item("A", "10.1/a")])
    b = _write(tmp_path / "m2" / "corpus.json", [_item("B", "10.1/b")])
    text = format_merge_report(merge_corpora([a, b])[1])
    assert "m1/corpus.json" in text and "m2/corpus.json" in text
    assert "Items chargés totaux : 2" in text
