# -*- coding: utf-8 -*-
"""
Lot D3 : intégration d'OPT-20 (prompts, descriptions) et d'OPT-21
(documentation).

Raccords vérifiés, sans réseau ni socket :

- tableau des prompts du README : pour chaque prompt servi, exactement ses
  arguments déclarés, avec la même obligation (requis / facultatif) ;
- docstring de tête de ``mcp_server.py`` : les douze outils servis, les deux
  transports, l'installation bornée par ``constraints.txt`` ; plus « Trois
  outils » ni « quatre API » ;
- docstring de ``GoogleScholarConnector.search`` : requête reçue du plan de
  requête ; plus d'affirmation « OpenAlex en sac de mots » ;
- aide de la CLI (``cli.py``, ``main.py``) : douze sources, groupes,
  sous-commandes, sources enrichies par OAI égales à celles du moteur, clés
  d'API exactes, plus de ``--workers 2`` ;
- docstring de ``utils/secrets.py`` : toutes les clés de ``config`` ;
  commentaire du registre sur les clés exact.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import mcp_server  # noqa: E402
from connectors.registry import CONNECTORS, SOURCE_GROUPS  # noqa: E402
from engine.orchestrator import ENRICH_OAI_SOURCES  # noqa: E402
from utils.source_labels import SOURCE_DISPLAY_NAMES, display_name  # noqa: E402

HISTORICAL_TOOLS = {
    "moisson_search", "corpus_analyze", "corpus_list", "fetch_by_doi",
    "search_by_author", "corpus_merge", "corpus_to_csl", "corpus_filter",
    "corpus_resolve_dois",
}
NEW_TOOLS = {"corpus_export", "corpus_query", "corpus_diff"}


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _split_row(line: str) -> list[str]:
    """Cellules d'une ligne de tableau Markdown."""
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [c.strip() for c in cells]


@pytest.fixture(scope="module")
def prompts():
    return {p.name: p for p in asyncio.run(mcp_server.list_prompts())}


@pytest.fixture(scope="module")
def tools():
    return {t.name for t in asyncio.run(mcp_server.list_tools())}


class _Captured(Exception):
    def __init__(self, parser):
        super().__init__("parseur capturé")
        self.parser = parser


def _parser_help(monkeypatch, fn, *args) -> str:
    """Aide du parseur construit par ``fn`` (parse_args intercepté)."""
    def fake_parse_args(self, *a, **k):
        raise _Captured(self)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse_args)
    try:
        fn(*args)
    except _Captured as captured:
        return captured.parser.format_help()
    finally:
        monkeypatch.undo()
    raise AssertionError(f"aucun parseur construit par {fn!r}")


# === Prompts : README contre list_prompts ===

class TestReadmePromptTable:
    def _rows(self):
        text = _read("README.md")
        start = text.index("### Prompts")
        section = text[start:text.index("\n### ", start + 5)]
        rows = {}
        for line in section.splitlines():
            if line.startswith("| `"):
                cells = _split_row(line)
                name = re.match(r"`([a-z-]+)`", cells[0]).group(1)
                rows[name] = cells[1]
        return rows

    def test_every_prompt_has_a_row(self, prompts):
        assert set(self._rows()) == set(prompts)

    def test_row_lists_exactly_the_declared_arguments(self, prompts):
        rows = self._rows()
        for name, prompt in prompts.items():
            cited = set(re.findall(r"`([a-z_]+)`", rows[name]))
            declared = {a.name for a in prompt.arguments or []}
            assert cited == declared, (name, cited ^ declared)

    def test_required_flags_match(self, prompts):
        rows = self._rows()
        for name, prompt in prompts.items():
            cell = rows[name]
            for arg in prompt.arguments or []:
                # L'obligation est énoncée après l'argument ou pour le groupe.
                after = cell[cell.index(f"`{arg.name}`"):]
                word = re.search(r"\((requis|facultatif|tous facultatifs)", after)
                assert word, (name, arg.name)
                assert (word.group(1) == "requis") == bool(arg.required), \
                    (name, arg.name, word.group(1))

    def test_profil_chercheur_accepts_identifier_alone(self, prompts):
        nom = [a for a in prompts["profil-chercheur"].arguments if a.name == "nom"]
        assert nom and not nom[0].required
        assert "`question_en` (facultatif)" in self._rows()["etat-de-l-art"]


# === Docstring de tête de mcp_server.py ===

class TestServerModuleDocstring:
    def test_names_every_served_tool(self, tools):
        doc = mcp_server.__doc__
        assert HISTORICAL_TOOLS | NEW_TOOLS <= tools
        for name in tools:
            assert name in doc, name

    def test_no_stale_statements(self):
        doc = mcp_server.__doc__
        for stale in ("Trois outils", "quatre API", "pip install mcp\n",
                      "pip install -r requirements.txt"):
            assert stale not in doc, stale
        assert 'pip install -e ".[mcp]" -c constraints.txt' in doc
        assert "Douze sources" in doc and "Douze outils" in doc

    def test_both_transports_are_described(self):
        doc = mcp_server.__doc__
        for fragment in ("stdio", "run_stdio", "serve_http", "serve --http",
                         "/download", "Bearer", "OAuth"):
            assert fragment in doc, fragment
        assert callable(mcp_server.run_stdio) and callable(mcp_server.serve_http)


# === Docstring de GoogleScholarConnector.search ===

class TestScholarSearchDocstring:
    def test_query_comes_from_the_plan(self):
        from connectors.google_scholar import GoogleScholarConnector
        doc = " ".join(GoogleScholarConnector.search.__doc__.split())
        assert "utils.query_plan" in doc
        assert "transmise telle quelle" not in doc
        assert "OpenAlex qui traitent" not in doc
        assert "seul CrossRef" in doc


# === Aide de la CLI ===

class TestCliHelp:
    def test_top_level_help_lists_all_sources_and_subcommands(self, capsys):
        import cli
        cli._print_top_level_help()
        out = capsys.readouterr().out
        for key in CONNECTORS:
            assert display_name(key) in out, key
        for sub in ("setup", "serve", "serve --http", "search", "export",
                    "token", "version"):
            assert f"moisson-shs {sub}" in out, sub
        assert "--workers 2" not in out
        assert "--workers 4" in out

    def test_main_help_epilog_and_options(self, monkeypatch):
        import main
        help_text = " ".join(_parser_help(monkeypatch, main.main).split())
        for key in CONNECTORS:
            assert display_name(key) in help_text, key
        for group in SOURCE_GROUPS:
            assert group in help_text, group
        assert "3 d'entre elles" not in help_text
        enrich = help_text[help_text.index("--enrich-oai Active"):]
        enrich = enrich[:enrich.index("--no-dedup")]
        for key in ENRICH_OAI_SOURCES:
            assert SOURCE_DISPLAY_NAMES[key] in enrich, key

    def test_main_help_states_the_keys_exactly(self, monkeypatch):
        import main
        help_text = " ".join(_parser_help(monkeypatch, main.main).split())
        assert "openalex, core et google_scholar les exigent" in help_text
        assert "semantic_scholar la rend facultative" in help_text


# === Clés : docstring des secrets et commentaire du registre ===

class TestKeysStatements:
    def test_secrets_docstring_lists_every_key(self):
        from utils import secrets
        for const in ("ENV_OPENALEX_KEY", "ENV_S2_KEY", "ENV_CORE_KEY",
                      "ENV_SERPAPI_KEY"):
            assert getattr(config, const) in secrets.__doc__, const
        assert "config.py" in secrets.__doc__   # limite de l'ordre de chargement

    def test_registry_comment_on_keys(self):
        text = _read("connectors/registry.py")
        assert "trois d'entre elles exigent une clé API" not in text
        assert "OpenAlex et CORE exigent une clé API" in text
