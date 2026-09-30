"""
PAT-19 (retour d'expérience du 2026-09-30, décision DP-7) : requête ambiguë.

La requête « "actantial model" Greimas computational OR automatic » était
lue « ("actantial model" Greimas computational) OR automatic » et partait
telle quelle vers Google Scholar, source facturée, avec un avertissement
relégué en fin de plan. Désormais : lecture enregistrée dans le plan
(``ambiguities``, donc dans le manifeste), avertissement en tête du retour,
refus par défaut quand une source facturée est demandée ; ``plan_only`` ne
refuse jamais.
"""

from __future__ import annotations

import asyncio

import pytest

import mcp_server
from utils.query_plan import build_query_plan, parse_query

AMBIGUOUS = '"actantial model" Greimas computational OR automatic'
READING = '("actantial model" Greimas computational) OR automatic'


def _run(coro):
    return asyncio.run(coro)


class TestParsing:
    def test_ambiguous_reading_recorded(self):
        assert parse_query(AMBIGUOUS).ambiguity == READING

    @pytest.mark.parametrize("query", [
        '("actantial model" Greimas computational) OR automatic',
        "discours numérique", "twitter OR facebook", '"a b" c',
    ])
    def test_unambiguous_queries(self, query):
        assert parse_query(query).ambiguity is None

    def test_plan_exposes_ambiguities(self):
        plan = build_query_plan(AMBIGUOUS, ["hal"])
        assert plan.as_dict()["ambiguities"] == [
            {"query": "original", "reading": READING}]
        assert plan.ambiguity_lines()[0].startswith(
            "Attention : requête ambiguë, lue « ")


@pytest.fixture
def no_harvest(monkeypatch):
    """Toute moisson réelle fait échouer le test (aucun appel réseau)."""
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("moisson atteinte")

    monkeypatch.setattr(mcp_server, "harvest", fake)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    return calls


def _call(args):
    return _run(mcp_server.call_tool("moisson_search", args))


def _text(result):
    content = getattr(result, "content", result)
    if isinstance(content, tuple):
        content = content[0]
    return "\n".join(getattr(b, "text", "") for b in content)


class TestMcp:
    def test_plan_only_puts_warning_first(self, no_harvest):
        res = _call({"query": AMBIGUOUS, "sources": ["google_scholar"],
                     "plan_only": True})
        text = _text(res)
        assert text.split("\n")[1].startswith("Attention : requête ambiguë")
        assert not no_harvest

    def test_refused_by_default_with_metered_source(self, no_harvest):
        res = _call({"query": AMBIGUOUS, "sources": ["google_scholar"]})
        assert getattr(res, "isError", False) is True
        assert "non envoyée" in _text(res) and "Google Scholar" in _text(res)
        assert not no_harvest

    def test_warn_lets_it_through(self, no_harvest):
        _call({"query": AMBIGUOUS, "sources": ["google_scholar"],
               "on_ambiguity": "warn"})
        assert no_harvest            # la moisson a été tentée

    def test_free_sources_warn_by_default(self, no_harvest):
        _call({"query": AMBIGUOUS, "sources": ["hal"]})
        assert no_harvest

    def test_refuse_applies_to_free_sources_too(self, no_harvest):
        res = _call({"query": AMBIGUOUS, "sources": ["hal"],
                     "on_ambiguity": "refuse"})
        assert getattr(res, "isError", False) is True
        assert not no_harvest

    def test_invalid_value_refused(self, no_harvest):
        res = _call({"query": "a", "sources": ["hal"], "on_ambiguity": "x"})
        assert getattr(res, "isError", False) is True
