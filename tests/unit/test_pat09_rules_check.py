"""
PAT-09 : fiches de vérification humaine des règles du sprint patch
(``scripts/eval/rules_check.py``), sans réseau.

Un dossier de corpus temporaire est composé des notices réelles de
``tests/fixtures/patch_2026-09-30.json`` : la génération doit trouver les
paires positives de chaque règle, les mêler à des témoins, rester à
l'aveugle (aucun nom de règle, aucune source) ; les métriques et la décision
doivent lire les verdicts et appliquer le gate.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from eval import common as C  # noqa: E402
from eval import rules_check as RC  # noqa: E402

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "patch_2026-09-30.json")
                     .read_text(encoding="utf-8"))


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    """Corpus temporaires et racine d'évaluation temporaire."""
    out = tmp_path / "output"
    out.mkdir()
    corpora = {
        "corpus_a_20260930_000000_aaaaaaaa.json":
            FIXTURE["quaderni"] + FIXTURE["mwi_zenodo"] + FIXTURE["hdr"],
        "corpus_b_20260930_000000_bbbbbbbb.json":
            FIXTURE["gjerstad"] + FIXTURE["numbered_title"],
        "corpus_c_20260930_000000_cccccccc.json": FIXTURE["franzosi"],
    }
    for name, items in corpora.items():
        (out / name).write_text(json.dumps(items, ensure_ascii=False),
                                encoding="utf-8")
    monkeypatch.setattr(C, "EVAL_ROOT", tmp_path / "eval")
    return out


def _rows(path):
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")),
                               delimiter=";"))


def test_generate_finds_each_rule_and_stays_blind(campaign):
    written = RC.generate("patch-test", campaign)
    meta = C.read_json(written["keys"])
    counts = meta["counts"]
    assert counts.get("codiffusion") == 1
    assert counts.get("zenodo_versions") == 1
    assert counts.get("scholar_chapters") == 2
    assert counts.get("taylor_francis_doi") == 2
    rows = _rows(written["csv"])
    assert len(rows) == len(meta["lines"])
    text = written["csv"].read_text(encoding="utf-8-sig") + \
        written["html"].read_text(encoding="utf-8")
    for leak in ("codiffusion", "zenodo_versions", "scholar_chapters",
                 "Google Scholar (SerpAPI)", "Sources:", "OpenAlex ID"):
        assert leak not in text, leak
    # Page autonome : aucun script ni style externe.
    html = written["html"].read_text(encoding="utf-8")
    assert "<script src" not in html and "<link" not in html
    assert "localStorage" in html and "Exporter le CSV" in html
    # Second jugement : environ 20 %, lignes de la fiche A, autre ordre.
    second = _rows(written["csv_b"])
    ids_a = [r["ligne_id"] for r in rows]
    assert 1 <= len(second) <= len(rows)
    assert {r["ligne_id"] for r in second} <= set(ids_a)


def test_generate_refuses_to_overwrite(campaign):
    RC.generate("patch-test", campaign)
    with pytest.raises(C.EvalError):
        RC.generate("patch-test", campaign)


def _fill(path, verdicts, default="1"):
    rows = _rows(path)
    for r in rows:
        r["verdict"] = verdicts.get(r["ligne_id"], default)
    path.with_name(path.stem + "_rempli.csv").write_text(
        RC.rows_to_csv(rows), encoding="utf-8-sig")


def test_metrics_and_decision(campaign):
    written = RC.generate("patch-test", campaign)
    meta = C.read_json(written["keys"])
    # Toutes les lignes jugées « 1 », sauf la paire Zenodo jugée « 0 ».
    zen = next(rid for rid, info in meta["lines"].items()
               if info["rules"].get("zenodo_versions") == "positive")
    _fill(written["csv"], {zen: "0"})
    _fill(written["csv_b"], {zen: "0"})
    doc = RC.metrics("patch-test")
    assert doc["regles"]["zenodo_versions"]["0"] == 1
    assert doc["accord"]["kappa"] is None or doc["accord"]["kappa"] <= 1
    path = RC.decide("patch-test",
                     doi_negative_check=lambda: (True, "simulé"))
    report = path.read_text(encoding="utf-8")
    line = next(l for l in report.splitlines() if "versions Zenodo" in l)
    assert line.endswith("ne pas activer |")
    line = next(l for l in report.splitlines() if "co-diffusion" in l)
    assert line.endswith("à activer |")


def test_decision_undetermined_without_verdicts(campaign):
    RC.generate("patch-test", campaign)
    path = RC.decide("patch-test", doi_negative_check=lambda: (True, "simulé"))
    assert "indéterminé" in path.read_text(encoding="utf-8")


def test_cohen_kappa():
    assert RC.cohen_kappa([("1", "1"), ("0", "0")]) == 1.0
    assert RC.cohen_kappa([("1", "0"), ("0", "1")]) == -1.0
    assert RC.cohen_kappa([("?", "1")]) is None
