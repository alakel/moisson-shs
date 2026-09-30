"""
PAT-22 (retour d'expérience du 2026-09-30) : HAL et le texte intégral.

La requête part dans le champ par défaut de HAL (``text`` : métadonnées),
que le code appelait à tort « full-text ». Sondes du 2026-09-30 :
« "modèle actantiel" » rend 7 notices ainsi, 224 par ``fulltext_t``. L'option
``hal_fulltext`` étend la requête HAL au texte intégral, avec un
avertissement ; sans effet ailleurs.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

import mcp_server
from engine.models import SearchRequest
from utils.query_plan import (
    HAL_FULLTEXT_WARNING, build_query_plan, hal_fulltext_query,
)

QUERY = '"modèle actantiel"'


def _hal(plan):
    return plan.for_source("hal")


class TestPlan:
    def test_default_searches_metadata_only(self):
        plan = build_query_plan(QUERY, ["hal"])
        assert "fulltext_t" not in _hal(plan).query_sent
        assert plan.as_dict()["hal_fulltext"] is False

    def test_option_extends_hal_query(self):
        plan = build_query_plan(QUERY, ["hal", "crossref"], hal_fulltext=True)
        hal = _hal(plan)
        assert hal.query_sent == hal_fulltext_query('"modèle actantiel"') \
            == '("modèle actantiel") OR fulltext_t:("modèle actantiel")'
        assert HAL_FULLTEXT_WARNING in hal.warnings
        assert "fulltext_t" not in plan.for_source("crossref").query_sent
        assert plan.as_dict()["hal_fulltext"] is True

    def test_without_hal_the_option_is_flagged(self):
        plan = build_query_plan(QUERY, ["crossref"], hal_fulltext=True)
        assert any("hal_fulltext sans effet" in w for w in plan.warnings)

    def test_request_carries_the_option(self):
        req = SearchRequest(query=QUERY, sources=["hal"], hal_fulltext=True)
        assert "fulltext_t" in req.query_plan().for_source("hal").query_sent

    def test_boolean_query_keeps_its_structure(self):
        plan = build_query_plan("(discours OR récit) AND numérique", ["hal"],
                                hal_fulltext=True)
        sent = _hal(plan).query_sent
        assert sent.startswith("((discours OR récit) AND numérique) OR "
                               "fulltext_t:(")


def _call(args):
    return asyncio.run(mcp_server.call_tool("moisson_search", args))


def _structured(result):
    return getattr(result, "structuredContent", None) or (
        result[1] if isinstance(result, tuple) else None)


class TestMcp:
    @pytest.fixture(autouse=True)
    def _stdio(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")

    def test_plan_only_shows_extended_query(self):
        res = _call({"query": QUERY, "sources": ["hal"], "plan_only": True,
                     "hal_fulltext": True})
        plan = _structured(res)["query_plan"]
        assert plan["hal_fulltext"] is True
        assert "fulltext_t" in plan["sources"][0]["query_sent"]

    def test_non_boolean_refused(self):
        res = _call({"query": QUERY, "sources": ["hal"], "plan_only": True,
                     "hal_fulltext": "oui"})
        assert getattr(res, "isError", False) is True


def test_cli_flag(monkeypatch, tmp_path, capsys):
    import main as main_module
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", [
        "main.py", QUERY, "--sources", "hal", "--hal-fulltext", "--plan-only",
        "--output", str(out)])
    assert main_module.main() == 0
    text = capsys.readouterr().out
    assert 'fulltext_t:("modèle actantiel")' in text
    assert not out.exists()
