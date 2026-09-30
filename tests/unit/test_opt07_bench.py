"""
Tests OPT-07 — banc de mesure adapté au moteur.

Chaque mesure tourne dans un processus neuf avec le simulateur d'API du banc
(garde-socket active, aucune socket réelle, sortie dans ``tmp_path``) :

- la CLI et le serveur MCP (``_run_moisson_sync``) produisent les MÊMES
  notices sur le simulateur (même moteur, mêmes réponses) ;
- le banc rapporte le statut de chaque source ;
- scénario ``reponse_invalide`` corrigé : la panne se déclenche avec le flux
  Isidore mutualisé (page 2 et sa reprise sans cache illisibles) et chaque
  consommateur est signalé ``partial`` (motif ``invalid_body``) ;
- scénario ``source_indisponible`` : les six consommateurs d'Isidore sont en
  ``error``, jamais « vides sans erreur ».
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import benchmark_moisson as bm  # noqa: E402
from bench import scenarios  # noqa: E402


def _opts():
    return argparse.Namespace(workers=None, sleep_scale=0.0, tracemalloc=False,
                              trace=False, live_keys=False)


def _run(name, scenario, entry, run_dir):
    spec = bm.make_spec(name, scenario, entry, "sim", PROJECT_ROOT, 0.0,
                        _opts(), run_dir)
    result = bm.run_in_subprocess(spec, timeout=180)
    assert not result.get("bench_error"), result.get("log_tail")
    assert result["real_socket_attempts"] == []
    return result


def _corpus(run_dir: Path):
    out = run_dir / "output"
    # OPT-16 : le manifeste (<stem>.manifest.json) s'ajoute aux fichiers.
    files = [p for p in out.glob("corpus_*.json")
             if not p.name.endswith((".csl.json", ".manifest.json"))]
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


def _canonical(items):
    return sorted(json.dumps(i, sort_keys=True, ensure_ascii=False) for i in items)


@pytest.mark.parametrize("mode", ["collecte", "bibliographie"])
def test_cli_and_mcp_produce_the_same_notices_on_the_simulator(tmp_path, mode):
    # OPT-12 : le mode agit sur les connecteurs (HAL classé par score en
    # bibliographie) et les défauts diffèrent (CLI : collecte ; MCP :
    # bibliographie) : la parité des adaptateurs se vérifie à mode égal.
    scenario = scenarios.get("smoke")
    scenario["params"]["mode"] = mode
    cli = _run("smoke", scenario, "cli", tmp_path / "cli")
    mcp = _run("smoke", scenario, "mcp", tmp_path / "mcp")
    assert cli["error"] is None and mcp["error"] is None
    cli_items, mcp_items = _corpus(tmp_path / "cli"), _corpus(tmp_path / "mcp")
    assert len(cli_items) > 0
    assert _canonical(cli_items) == _canonical(mcp_items)
    # Mêmes statuts par source, rapportés par le banc.
    st_cli = {x["source"]: x.get("status") for x in cli["stages"]["sources"]}
    st_mcp = {x["source"]: x.get("status") for x in mcp["stages"]["sources"]}
    assert st_cli == st_mcp
    assert set(st_cli) == {"HAL", "Isidore", "OpenEdition"}
    assert all(s in ("complete", "partial", "empty") for s in st_cli.values())


def test_reponse_invalide_triggers_with_the_shared_flow(tmp_path):
    scenario = scenarios.get("reponse_invalide")
    fault = scenario["sim"]["faults"][0]
    # OPT-12 : le flux tient en une requête (replies=200) ; la panne vise
    # donc la première (after=0).
    assert fault["after"] == 0
    result = _run("reponse_invalide", scenario, "mcp", tmp_path / "r")
    statuses = {x["source"]: (x.get("status"), x.get("reason"))
                for x in result["stages"]["sources"]}
    isidore_family = ["Isidore", "OpenEdition", "Cairn", "Persée",
                      "Theses.fr", "Érudit"]
    for name in isidore_family:
        # Aucune page acquise : échec nommé, jamais « aucun résultat ».
        assert statuses[name] == ("error", "invalid_body"), (name, statuses)
    assert statuses["HAL"][0] == "complete"
    assert result["mcp_return"]["is_error"] is False   # succès partiel nommé
    # La requête de la fenêtre, sa reprise sans cache, puis la reprise
    # différée du flux de la moisson (OPT-09) : trois corps illisibles
    # (scénario first=3).
    assert result["network"]["hosts"]["api.isidore.science"]["non_json_200"] == 3
    summary = bm.summarize([result])
    assert summary["source_statuses"]["Isidore"] == "error/invalid_body"
    assert summary["source_statuses"]["HAL"] == "complete"


def test_source_indisponible_reports_errors_not_empty_sources(tmp_path):
    scenario = scenarios.get("source_indisponible")
    result = _run("source_indisponible", scenario, "mcp", tmp_path / "s")
    statuses = {x["source"]: (x.get("status"), x.get("reason"))
                for x in result["stages"]["sources"]}
    for name in ("Isidore", "OpenEdition", "Cairn", "Persée", "Theses.fr",
                 "Érudit"):
        assert statuses[name] == ("error", "http_error"), (name, statuses)
    assert statuses["HAL"][0] == "complete"
    assert result["mcp_return"]["is_error"] is False   # succès partiel nommé
