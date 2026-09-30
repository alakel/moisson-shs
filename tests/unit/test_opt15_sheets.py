# -*- coding: utf-8 -*-
"""
OPT-15 : fiches de jugement en aveugle (aucun accès réseau).

Aucune fuite de source, de score ni de rang dans les fiches CSV et HTML ;
ordre de présentation indépendant des classements (même ensemble de
candidats, classements différents : même ordre) ; graine fixe ; second
jugement sur 20 % ; résumé tronqué proprement ; lien neutre ; CSV lisible par
un tableur et relu à l'identique.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Import anticipé : urllib3 sonde IPv6 à l'import (bind local, sans connexion) ;
# la passe floue de la déduplication importe requests paresseusement.
import requests  # noqa: E402,F401

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from eval import common as C  # noqa: E402
from eval import sheets as S  # noqa: E402
from eval import synthetic as T  # noqa: E402

TITLES = ["Le discours numérique en question", "Pratiques numériques des étudiants",
          "Histoire de la presse", "Digital discourse analysis", "Une thèse sur les forums"]

# Traces de provenance, de score ou de rang qui ne doivent jamais figurer
# dans une fiche (lignes d'``extra``, identifiants de moteurs, noms de
# classeurs et de sources).
FORBIDDEN = ("Sources:", "Dedup evidence", "Isidore URI", "OpenAlex ID", "S2 Paper ID",
             "HAL ID:", "semanticscholar.org", "isidore.science", "local:simple",
             "native", "score", "CrossRef", "Semantic Scholar")


def _pools(order_a, order_b):
    snap = T.sample_snapshot()
    rankers = [T.fixed_ranker("native", order_a), T.fixed_ranker("local:simple", order_b)]
    return snap, T.pool_for(snap, rankers, depth=5)


def test_rows_expose_only_bibliographic_fields():
    _snap, pool = _pools(TITLES, list(reversed(TITLES)))
    rows = S.judgment_rows([pool])
    assert len(rows) == 5
    for row in rows:
        assert list(row) == S.COLUMNS
    header = {c.lower() for c in S.COLUMNS}
    assert not header & {"source", "sources", "score", "rang", "rank", "classeur"}
    blob = json.dumps(rows, ensure_ascii=False)
    for needle in FORBIDDEN:
        assert needle not in blob, needle


def test_order_does_not_depend_on_rankings():
    _s, pool1 = _pools(TITLES, list(reversed(TITLES)))
    _s, pool2 = _pools(list(reversed(TITLES)), TITLES[2:] + TITLES[:2])
    assert set(pool1["pool"]) == set(pool2["pool"])
    order1 = [r["candidat_id"] for r in S.judgment_rows([pool1])]
    order2 = [r["candidat_id"] for r in S.judgment_rows([pool2])]
    assert order1 == order2
    # L'ordre n'est ni celui d'un classeur ni celui des identifiants triés.
    native = pool1["rankers"]["native"]["ranking"][:5]
    assert order1 != native and order1 != sorted(order1) or len(order1) < 3
    assert [r["numero"] for r in S.judgment_rows([pool1])] == ["1", "2", "3", "4", "5"]


def test_blind_order_is_seeded_and_salted():
    cids = [f"c{i:03d}" for i in range(40)]
    a1 = S.blind_order(cids, 7, "D01", "A")
    assert a1 == S.blind_order(list(reversed(cids)), 7, "D01", "A")
    assert a1 != S.blind_order(cids, 7, "D01", "B")
    assert a1 != S.blind_order(cids, 8, "D01", "A")
    assert a1 != S.blind_order(cids, 7, "D02", "A")
    assert sorted(a1) == sorted(cids)


def test_second_sample_is_twenty_percent_and_blind():
    cids = [f"c{i:03d}" for i in range(47)]
    sample = S.second_sample(cids, 7, "D01")
    assert len(sample) == math.ceil(0.2 * 47) == 10
    assert set(sample) <= set(cids)
    assert sample == S.second_sample(list(reversed(cids)), 7, "D01")
    assert len(S.second_sample(["c1"], 7, "D01")) == 1
    assert S.second_sample([], 7, "D01") == []
    _s, pool = _pools(TITLES, TITLES)
    prefill = {("D01", cid): {"jugement": "3", "remarque": ""} for cid in pool["pool"]}
    rows_b = S.judgment_rows([pool], salt="B", fraction=0.2, prefill=None)
    rows_a = S.judgment_rows([pool], salt="A", prefill=prefill)
    assert len(rows_b) == 1 and rows_b[0]["jugement"] == ""
    assert all(r["jugement"] == "3" for r in rows_a)


def test_generate_writes_csv_and_offline_html_without_leak(tmp_path):
    _s, pool = _pools(TITLES, list(reversed(TITLES)))
    out_dir = tmp_path / "2026-09-29"
    written = S.generate(out_dir, [pool], seed=11)
    csv_path = written["jugement_A.csv"]
    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")               # BOM : tableurs
    header = raw.decode("utf-8-sig").splitlines()[0]
    assert header == ";".join(S.COLUMNS)
    rows = S.read_sheet(csv_path)
    assert [r["candidat_id"] for r in rows] == [r["candidat_id"] for r in S.judgment_rows([pool], 11)]
    html = written["jugement_A.html"].read_text(encoding="utf-8")
    # Autonome : aucune ressource externe chargée.
    assert not re.search(r"<(script|link|img)[^>]+(src|href)=\"https?://", html)
    payload = re.search(r'<script type="application/json" id="sheet-data">(.*?)</script>',
                        html, re.S).group(1)
    data = json.loads(payload)
    assert data["columns"] == S.COLUMNS and len(data["rows"]) == 5
    # Nom du fichier exporté : celui que metrics.py cherche par défaut.
    assert data["file_stem"] == "jugement_A" and "_rempli.csv" in html
    rows_blob = json.dumps(data["rows"], ensure_ascii=False)
    for needle in FORBIDDEN:
        assert needle not in rows_blob, needle
    for name in ("local:simple", "entrelacement"):
        assert name not in html
    assert "</script" not in payload.lower()
    b_rows = S.read_sheet(written["jugement_B_echantillon.csv"])
    assert len(b_rows) == math.ceil(0.2 * 5)


def test_prefill_round_trip(tmp_path):
    _s, pool = _pools(TITLES, TITLES)
    out_dir = tmp_path / "2026-09-29"
    written = S.generate(out_dir, [pool])
    rows = S.read_sheet(written["jugement_A.csv"])
    rows[0]["jugement"], rows[0]["remarque"] = "2", "avec ; point-virgule"
    filled = tmp_path / "rempli.csv"
    C.write_text(filled, S.rows_to_csv(rows, S.COLUMNS), bom=True)
    known = S.read_prefill([filled])
    assert known == {("D01", rows[0]["candidat_id"]): {"jugement": "2",
                                                        "remarque": "avec ; point-virgule"}}
    again = S.generate(out_dir, [pool], prefill_paths=[filled])
    rows2 = S.read_sheet(again["jugement_A.csv"])
    assert sum(1 for r in rows2 if r["jugement"]) == 1


def test_abstract_excerpt_cuts_cleanly():
    text = "Première phrase courte. " + "mot " * 400
    out = C.abstract_excerpt(text, 100)
    assert out.endswith(" […]") and len(out) <= 105
    assert not out[:-4].rstrip().endswith("mo")          # jamais au milieu d'un mot
    sentence = ("A" * 70) + ". Suite de la phrase qui dépasse largement la limite fixée."
    assert C.abstract_excerpt(sentence, 90) == ("A" * 70) + ". […]"
    assert C.abstract_excerpt("<p>Court &amp; net</p>", 100) == "Court & net"


def test_neutral_link():
    doi_item = T.item("x", doi="10.1000/a", url="https://www.semanticscholar.org/paper/1")
    assert C.neutral_link(doi_item) == "https://doi.org/10.1000/a"
    hal_item = T.item("x", url="https://hal.science/hal-01000001")
    assert C.neutral_link(hal_item) == "https://hal.science/hal-01000001"
    agg = T.item("Titre à chercher", url="https://www.semanticscholar.org/paper/1",
                 extra=["Alternative URLs: https://isidore.science/r/1 | https://journals.openedition.org/x/1"])
    assert C.neutral_link(agg) == "https://journals.openedition.org/x/1"
    only_agg = T.item("Titre à chercher", url="https://isidore.science/resource/1")
    link = C.neutral_link(only_agg)
    assert link.startswith(C.NEUTRAL_SEARCH) and "isidore" not in link


def test_query_label_mentions_variant_and_dates():
    q = {"query": "gentrification", "query_variants": {"en": "gentrification"}, "to_date": "2015"}
    assert S.query_label(q) == ("gentrification (variante anglaise : gentrification) — "
                                "publications jusqu'en 2015")
    assert S.query_label({"query": "x", "from_date": "2005", "to_date": "2010"}).endswith(
        "publications de 2005 à 2010")


def test_verification_sheets_hide_nothing_needed_and_sample(tmp_path):
    out_dir = tmp_path / "2026-09-29"
    snap = T.sample_snapshot()
    C.write_json(out_dir / C.SNAPSHOTS / "D01.json", snap)
    rows = S.fusion_rows(out_dir, n_strong=5, n_fuzzy=5)
    assert len(rows) == 1                       # a (HAL) + a (CrossRef), même DOI
    assert rows[0]["n_notices"] == "2" and "DOI 10.1000/a" in rows[0]["notice_1"]
    assert rows[0]["_kind"] == "forte"
    doi_report = {"eval": {"qid": "D01", "cids": ["c1", "c2"]}, "entries": [
        {"index": 1, "status": "accepte", "doi": "10.1/ok", "title": "T", "year": "2020"},
        {"index": 0, "status": "rejete", "doi": None, "title": "U"}]}
    C.write_json(out_dir / C.DOI_DIR / "D01.doi_report.json", doi_report)
    drows = S.doi_rows(out_dir)
    assert len(drows) == 1 and drows[0]["candidat_id"] == "c2"
    assert drows[0]["lien_doi"] == "https://doi.org/10.1/ok"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js absent")
def test_html_export_is_read_back_by_the_metrics(tmp_path):
    """Fonctions CSV de la fiche HTML exécutées sous Node : l'export d'une
    fiche remplie est relu par ``read_judgments`` (jugements, remarques avec
    « ; » et guillemets) et l'import relit un CSV écrit par Python."""
    from eval import metrics as M

    _s, pool = _pools(TITLES, TITLES)
    rows = S.judgment_rows([pool])
    for n, row in enumerate(rows):
        row["jugement"] = str(n % 4)
        row["remarque"] = 'avec ; point-virgule et "guillemets"' if n == 0 else ""
    script = tmp_path / "export.js"
    script.write_text(S._JS_CSV + """
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
fs.writeFileSync(process.argv[3], exportCsv(data.cols, data.rows), 'utf8');
const back = parseCsv(fs.readFileSync(process.argv[4], 'utf8').replace(/^\\ufeff/, ''));
fs.writeFileSync(process.argv[5], JSON.stringify(back));
""", encoding="utf-8")
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"cols": S.COLUMNS, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    py_csv = C.write_text(tmp_path / "python.csv", S.rows_to_csv(rows, S.COLUMNS), bom=True)
    exported, parsed = tmp_path / "export.csv", tmp_path / "parsed.json"
    subprocess.run(["node", str(script), str(data), str(exported), str(py_csv), str(parsed)],
                   check=True, capture_output=True, timeout=60)
    assert exported.read_bytes().startswith(b"\xef\xbb\xbf")
    grades, errors = M.read_judgments([exported])
    assert not errors
    assert grades == {("D01", r["candidat_id"]): int(r["jugement"]) for r in rows}
    assert S.read_sheet(exported)[0]["remarque"] == 'avec ; point-virgule et "guillemets"'
    back = json.loads(parsed.read_text(encoding="utf-8"))
    assert back[0] == S.COLUMNS
    assert [r[S.COLUMNS.index("candidat_id")] for r in back[1:]] == [r["candidat_id"] for r in rows]
    assert back[1][S.COLUMNS.index("remarque")] == 'avec ; point-virgule et "guillemets"'
