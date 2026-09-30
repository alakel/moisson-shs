"""
PAT-16 (retour d'expérience du 2026-09-30) : statuts de source lisibles.

- Une panne d'Isidore fait tomber ses cinq sous-sources (flux partagé) : une
  ligne groupée la nomme, avec les sources encore interrogées (« groupe
  francophone réduit à HAL »).
- Une source non configurée (clé d'API absente) n'est pas une perte : elle
  est nommée à part et ne rend plus la moisson « succès partiel ».
"""

from __future__ import annotations

import mcp_server
from engine.models import HarvestResult, SearchRequest, SourceResult

FLOW = ["isidore", "openedition", "cairn", "persee", "theses_fr", "erudit"]
NAMES = {"isidore": "Isidore", "openedition": "OpenEdition", "cairn": "Cairn",
         "persee": "Persée", "theses_fr": "Theses.fr", "erudit": "Érudit",
         "hal": "HAL", "core": "CORE"}


def _src(key, status="error", reason="network_error",
         detail="page 1 : ConnectionError", n=0):
    return SourceResult(source=key, display_name=NAMES[key],
                        items=[{"itemType": "journalArticle", "title": f"t{i}"}
                               for i in range(n)],
                        status=status, reason=reason, detail=detail)


def _result(sources):
    return HarvestResult(run_id="r", request=SearchRequest(query="q", sources=[
        s.source for s in sources]), sources=sources,
        items=[it for s in sources for it in s.items],
        total_before_dedup=sum(len(s.items) for s in sources))


def test_group_reduced_to_hal():
    res = _result([_src(k) for k in FLOW]
                  + [_src("hal", "complete", None, None, n=3)])
    outages = res.flow_outages()
    assert len(outages) == 1
    o = outages[0]
    assert o["label"] == "Isidore" and o["remaining"] == ["HAL"]
    assert o["dependents"] == ["OpenEdition", "Cairn", "Persée", "Theses.fr",
                               "Érudit"]
    line = res.flow_outage_lines()[0]
    assert line.startswith("Isidore indisponible (erreur réseau : page 1 : "
                           "ConnectionError) : OpenEdition, Cairn, Persée, "
                           "Theses.fr et Érudit en dépendent")
    assert line.endswith("sources encore interrogées : HAL.")


def test_no_grouping_when_flow_ok():
    res = _result([_src(k, "complete", None, None, n=2) for k in FLOW])
    assert res.flow_outages() == []


def test_no_grouping_when_motives_differ():
    sources = [_src(k) for k in FLOW]
    sources[2] = _src("cairn", detail="HTTP 503")
    assert _result(sources).flow_outages() == []


def test_all_failed_names_isidore():
    res = _result([_src(k) for k in FLOW])
    assert res.all_failed
    assert res.flow_outage_lines()[0].endswith(
        "sources encore interrogées : aucune.")


def test_unconfigured_is_not_a_loss():
    res = _result([_src("hal", "complete", None, None, n=2),
                   _src("core", "skipped", "missing_key",
                        "définir CORE_API_KEY")])
    assert [r.display_name for r in res.unconfigured] == ["CORE"]
    assert mcp_server._lossy_sources(res) == []


def test_missing_key_detail_names_variable_once(monkeypatch):
    from utils.secrets import MissingAPIKey, get_api_key
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    try:
        get_api_key("CORE_API_KEY", required=True)
    except MissingAPIKey as exc:
        assert exc.env_var == "CORE_API_KEY"
    else:  # pragma: no cover
        raise AssertionError("MissingAPIKey attendue")
