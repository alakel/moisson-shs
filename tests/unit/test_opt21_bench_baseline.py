"""
Résumé de baseline remesuré à la validation finale (OPT-21) :
``tests/fixtures/bench/baseline_2026-09-29.summary.json``.

La copie d'avant chantier a été remesurée avec le simulateur actuel du banc
(celui de l'arbre de travail), dont les définitions de scénarios ont changé
depuis la baseline du 2026-09-28 (ordres de tri vérifiés en live, fenêtre
Isidore, ``reponse_invalide``) : c'est ce résumé qu'il faut passer à
``--compare``. L'ancien résumé reste versionné, intact.
"""

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "bench"
NEW = FIXTURES / "baseline_2026-09-29.summary.json"
OLD = FIXTURES / "baseline_2026-09-28.summary.json"

if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def _data():
    return json.loads(NEW.read_text(encoding="utf-8"))


def test_old_baseline_is_kept():
    assert OLD.is_file() and NEW.is_file()


def test_new_baseline_is_neutral():
    text = NEW.read_text(encoding="utf-8")
    assert not re.search(r'"/(private|Users|home|tmp|var)/', text)
    data = json.loads(text)
    for meta in data["meta"]["merged"]:
        assert meta["tree"] == "baseline_tree"
        assert meta["mode"] == "sim"
    assert all("runs" not in g for g in data["groups"])


def test_new_baseline_covers_every_scenario_in_both_series():
    from bench import scenarios

    groups = _data()["groups"]
    assert all("sleep_scale" in g and "workers" in g for g in groups)
    names = set(scenarios.names())
    quick = [g for g in groups if g["sleep_scale"] == 0]
    assert {g["scenario"] for g in quick} == names
    assert all(g["summary"]["counter_definition"] == 2 for g in quick)
    assert all(g["summary"]["real_socket_attempts"] == 0 for g in groups)
    assert all(g["summary"]["failed_runs"] == 0 for g in groups)
    for latency in (0.0, 150.0):
        real = {g["scenario"] for g in groups
                if g["sleep_scale"] == 1 and g["latency_ms"] == latency}
        assert real == names, latency
    ref = [g for g in quick if g["scenario"] == "reference" and g["entry"] == "mcp"]
    assert len(ref) == 1
    cov = ref[0]["summary"]["coverage"]
    assert cov["final"]["count"] > 0 and len(cov["sources"]) == 7


def test_new_baseline_reference_is_repeated_on_both_entries():
    groups = _data()["groups"]
    for entry in ("mcp", "cli"):
        for latency in (0.0, 150.0):
            g = [x for x in groups if x["scenario"] == "reference"
                 and x["entry"] == entry and x["sleep_scale"] == 1
                 and x["latency_ms"] == latency]
            assert len(g) == 1 and g[0]["summary"]["runs"] == 5, (entry, latency)
