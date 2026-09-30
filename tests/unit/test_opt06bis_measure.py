"""
OPT-06 bis : liste exhaustive des groupes qui changent entre deux versions
de la déduplication (``scripts/measure_duplicates.py --diff-out``), pour
l'examen manuel de chaque nouvelle fusion.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "measure_duplicates.py"


def _load():
    spec = importlib.util.spec_from_file_location("measure_duplicates_t", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(title, last, first):
    return {"itemType": "journalArticle", "title": title, "date": "2020",
            "creators": [{"creatorType": "author", "lastName": last,
                          "firstName": first}],
            "extra": "Sources: HAL"}


def test_group_diff_lists_new_grown_and_lost_groups():
    md = _load()
    inputs = {"c.json": [_item(f"T{i}", "Nom", "Prénom") for i in range(5)]}
    before = {"corpora": [{"file": "c.json", "groups": [[0, 1], [3, 4]],
                           "merged_records": {}}]}
    after = {"corpora": [{"file": "c.json", "groups": [[0, 1, 2]],
                          "merged_records": {"0,1,2": {"title": "T0"}}}]}
    diff = md.group_diff(before, after, inputs)
    assert diff["new_total"] == 1 and diff["lost_total"] == 2
    new = diff["new"][0]
    assert [m["title"] for m in new["members"]] == ["T0", "T1", "T2"]
    assert new["previous_groups"] == [[0, 1]]
    assert new["merged"] == {"title": "T0"}
    assert {tuple(m["title"] for m in g["members"]) for g in diff["lost"]} == \
        {("T0", "T1"), ("T3", "T4")}


def test_describe_keeps_first_names():
    md = _load()
    desc = md.describe(_item("T", "DE JANVRY", "Thibaud Choppin"))
    assert desc["authors"] == ["DE JANVRY"]
    assert desc["authors_full"] == ["DE JANVRY, Thibaud Choppin"]


def test_end_to_end_same_tree_has_no_diff(tmp_path):
    """Même arbre avant et après : aucune différence ; aucun chemin absolu
    dans les sorties."""
    md = _load()
    out_dir = tmp_path / "corpus"
    out_dir.mkdir()
    items = [_item("Dire, toucher, capter. Le langage à l'épreuve du numérique",
                   "CHOPPIN DE JANVRY", "Thibaud"),
             _item("Dire, toucher, capter. Le langage à l'épreuve du numérique",
                   "DE JANVRY", "Thibaud Choppin")]
    (out_dir / "corpus_test.json").write_text(json.dumps(items), encoding="utf-8")
    diff_out, json_out = tmp_path / "diff.json", tmp_path / "mesure.json"
    rc = md.main(["--output-dir", str(out_dir), "--baseline-tree", str(ROOT),
                  "--tree", str(ROOT), "--no-fuzzy-run", "--json-out",
                  str(json_out), "--diff-out", str(diff_out)])
    assert rc == 0
    diff = json.loads(diff_out.read_text(encoding="utf-8"))
    assert diff["new_total"] == 0 and diff["lost_total"] == 0
    summary = json.loads(json_out.read_text(encoding="utf-8"))
    after = [s for s in summary["summaries"] if s["label"] == "après"][0]
    assert after["items_in"] == 2 and after["items_out"] == 1
    assert str(tmp_path) not in json_out.read_text(encoding="utf-8")
    assert str(tmp_path) not in diff_out.read_text(encoding="utf-8")
