# -*- coding: utf-8 -*-
"""
OPT-21 : cohérence de la documentation avec le code livré (constat C34).

La documentation dérivée avait divergé du code (quatre outils annoncés pour
neuf, sept sources pour douze, chiffres de performance jamais mesurés,
affirmations fausses sur la syntaxe des sources). Ces tests vérifient, sans
réseau ni socket, que les guides nomment ce qui existe :

- outils MCP : tout outil cité dans les guides existe (``list_tools``) ;
  chaque outil exposé est présenté dans README.md, MCP_INSTALL.md et
  REMOTE.md ; le nombre annoncé par le README est celui du serveur ;
- arguments : le tableau des outils du README cite, pour chaque outil,
  exactement les propriétés de son schéma d'entrée ;
- sources, groupes, prompts, formats et fichiers associés : présents dans le
  README ; champs de ``structuredContent`` cités : rendus par l'outil ;
- variables d'environnement : toute variable ``MOISSON_*`` citée dans les
  guides ou dans ``.env.example`` est lue par le code (ou le déploiement) ;
  toute variable lue par le code est documentée dans ``.env.example`` ; les
  valeurs par défaut qu'y affiche ``.env.example`` sont celles du code ;
- options de la CLI citées dans les tableaux du README : existent dans les
  parseurs ;
- défauts énoncés par les guides : égaux aux constantes du code ;
- note de redéploiement (fichier compose de la variante Nginx à chaque
  commande), pin du SDK ; phrases périmées absentes ;
- revue d'OPT-21 : arguments requis marqués pour tous les outils, appel
  « ORCID seul » conforme au schéma (``name=""``), noms dérivés sans uuid,
  empreinte du classement citée par EVALUATION.md confrontée au module,
  mesure de référence identique dans le README et la FAQ, import de
  ``tomllib`` compatible avec Python 3.10 (matrice CI).
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

try:  # Python >= 3.11
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 (matrice CI)
    try:
        import tomli as tomllib  # vient avec pytest sous Python 3.10
    except ImportError:
        tomllib = None

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import mcp_server  # noqa: E402
from connectors.registry import CONNECTORS, SOURCE_GROUPS  # noqa: E402
from utils import corpus_query as CQ  # noqa: E402
from utils import output_files as OF  # noqa: E402

GUIDES = ("README.md", "MCP_INSTALL.md", "REMOTE.md", "DEPLOY.md",
          "docs/FAQ.md", "docs/API_SPECS.md", "docs/EVALUATION.md")
ENV_EXAMPLE = ".env.example"
DEPLOY_FILES = ("docker-compose.yml", "docker-compose.nginx.yml", "Dockerfile",
                "deploy/Caddyfile")

# Répertoires qui ne contiennent pas le code applicatif.
_SKIP_DIRS = {"venv", "tests", "scripts", "__pycache__", "build", "dist",
              "output", "node_modules"}

_ENV_RE = re.compile(r"\bMOISSON_[A-Z0-9_]*[A-Z0-9]")
_ENV_LITERAL_RE = re.compile(r"""["'](MOISSON_[A-Z0-9_]*[A-Z0-9])["']""")
_TOOL_RE = re.compile(r"`((?:corpus|moisson|fetch|search)_[a-z_]+)(?=`|\()")


# === Aides ===

def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _python_files(skip=_SKIP_DIRS):
    """Fichiers Python du projet (sans venv, tests, scripts ni dossiers
    cachés), parcours élagué : le venv n'est jamais visité."""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in skip and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _code_env_names() -> set[str]:
    """Variables MOISSON_* nommées par un littéral du code applicatif."""
    names = set()
    for path in _python_files():
        names.update(_ENV_LITERAL_RE.findall(path.read_text(encoding="utf-8")))
    return names


def _known_env_names() -> set[str]:
    """Variables reconnues : code applicatif, scripts, conftest des tests,
    fichiers de déploiement."""
    names = _code_env_names()
    for path in _python_files(skip=_SKIP_DIRS - {"scripts"}):
        names.update(_ENV_LITERAL_RE.findall(path.read_text(encoding="utf-8")))
    names.update(_ENV_RE.findall(_read("tests/conftest.py")))
    for rel in DEPLOY_FILES:
        if (ROOT / rel).exists():
            names.update(_ENV_RE.findall(_read(rel)))
    return names


def _cited_env_names(text: str) -> tuple[set[str], set[str]]:
    """(noms complets, préfixes suivis de « * ») cités dans un texte."""
    names, prefixes = set(), set()
    for m in _ENV_RE.finditer(text):
        tail = text[m.end():m.end() + 2]
        if tail.startswith("*") or tail == "_*":
            prefixes.add(m.group(0))
        else:
            names.add(m.group(0))
    return names, prefixes


def _env_example_assignments() -> dict[str, str]:
    """``VARIABLE=valeur`` de .env.example, commentées ou non."""
    out = {}
    for line in _read(ENV_EXAMPLE).splitlines():
        m = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _literal(node):
    """Valeur d'un défaut simple : constante, arithmétique de constantes,
    ``str(...)`` d'une telle expression ; None sinon."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp):
        left, right = _literal(node.left), _literal(node.right)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            ops = {ast.Mult: lambda a, b: a * b, ast.Add: lambda a, b: a + b,
                   ast.Sub: lambda a, b: a - b}
            op = ops.get(type(node.op))
            return op(left, right) if op else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "str" and len(node.args) == 1):
        value = _literal(node.args[0])
        return None if value is None else str(value)
    return None


def _code_defaults() -> dict[str, set]:
    """Défauts des lectures ``os.environ.get("MOISSON_X", défaut)`` et
    ``_env_int("MOISSON_X", défaut, …)`` du code applicatif, indépendants de
    l'environnement du processus de test."""
    readers = {"get", "getenv", "_env_int"}
    found: dict[str, set] = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)
                    and first.value.startswith("MOISSON_")):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in readers:
                continue
            default = _literal(node.args[1]) if len(node.args) > 1 else None
            found.setdefault(first.value, set()).add(default)
    return found


def _same_value(documented: str, default) -> bool:
    try:
        return float(documented) == float(default)
    except (TypeError, ValueError):
        return documented == str(default)


def _split_row(line: str) -> list[str]:
    """Cellules d'une ligne de tableau Markdown (« \\| » n'est pas un séparateur)."""
    cells = re.split(r"(?<!\\)\|", line.strip())
    return [c.strip() for c in cells[1:-1]]


def _readme_tool_rows() -> dict[str, list[str]]:
    """Lignes du tableau « Outil | Rôle | Arguments (défaut) » du README."""
    lines = _read("README.md").splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.startswith("|") and "Arguments (défaut)" in line)
    rows = {}
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = _split_row(line)
        m = re.match(r"`([a-z_]+)`", cells[0])
        if m:
            rows[m.group(1)] = cells
    return rows


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(mcp_server.list_tools())}


@pytest.fixture(scope="module")
def prompts():
    return {p.name for p in asyncio.run(mcp_server.list_prompts())}


# === Outils MCP ===

class TestTools:
    # Identifiants de même forme qui ne sont pas des outils : volumes Docker.
    NOT_TOOLS = {"moisson_output", "moisson_cache", "moisson_shs_"}

    @pytest.mark.parametrize("guide", GUIDES)
    def test_cited_tools_exist(self, guide, tools):
        arguments = {a for t in tools.values()
                     for a in t.inputSchema.get("properties", {})}
        cited = set(_TOOL_RE.findall(_read(guide))) - arguments - self.NOT_TOOLS
        assert cited - set(tools) == set(), \
            f"{guide} cite des outils inexistants : {sorted(cited - set(tools))}"

    @pytest.mark.parametrize("guide", ["README.md", "MCP_INSTALL.md", "REMOTE.md"])
    def test_every_tool_is_presented(self, guide, tools):
        text = _read(guide)
        missing = [name for name in tools if f"`{name}`" not in text]
        assert missing == [], f"{guide} ne présente pas : {missing}"

    def test_announced_tool_count_matches_the_server(self, tools):
        m = re.search(r"^### Outils \((\d+)\)", _read("README.md"), re.M)
        assert m and int(m.group(1)) == len(tools)

    def test_historic_and_new_tools_are_all_served(self, tools):
        historic = {"moisson_search", "corpus_analyze", "corpus_list",
                    "fetch_by_doi", "search_by_author", "corpus_merge",
                    "corpus_to_csl", "corpus_filter", "corpus_resolve_dois"}
        added = {"corpus_export", "corpus_query", "corpus_diff"}
        assert historic | added <= set(tools)

    def test_readme_table_lists_exactly_the_schema_arguments(self, tools):
        rows = _readme_tool_rows()
        assert set(rows) == set(tools), \
            f"tableau du README : {sorted(set(tools) ^ set(rows))}"
        for name, cells in rows.items():
            cited = set(re.findall(r"`([a-z][a-z0-9_]*)`", cells[2]))
            schema = set(tools[name].inputSchema.get("properties", {}))
            assert cited == schema, (
                f"{name} : en trop {sorted(cited - schema)}, "
                f"manquants {sorted(schema - cited)}")

    def test_required_arguments_are_marked(self, tools):
        """Revue d'OPT-21 : chaque argument de ``inputSchema.required`` est
        marqué « (requis » dans le tableau, pour tous les outils (seul
        ``query`` l'était), et aucun argument facultatif ne l'est."""
        rows = _readme_tool_rows()
        assert tools["moisson_search"].inputSchema["required"] == ["query"]
        for name, cells in rows.items():
            required = set(tools[name].inputSchema.get("required") or [])
            marked = set(re.findall(r"`([a-z][a-z0-9_]*)` \(requis", cells[2]))
            assert marked == required, (
                f"{name} : requis non marqués {sorted(required - marked)}, "
                f"marqués à tort {sorted(marked - required)}")

    def test_orcid_only_author_call_matches_the_schema(self, tools):
        """Revue d'OPT-21 : ``name`` est requis par le schéma de
        search_by_author (le SDK valide les entrées), donc un appel « ORCID
        seul » passe ``name=""`` ; les guides ne le décrivent plus « sans
        nom »."""
        import jsonschema
        schema = tools["search_by_author"].inputSchema
        orcid = "0000-0002-1825-0097"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"orcid": orcid}, schema)
        jsonschema.validate({"name": "", "orcid": orcid}, schema)
        for guide in ("README.md", "docs/FAQ.md"):
            text = _read(guide)
            line = next(l for l in text.splitlines()
                        if "Un ORCID seul interroge HAL et OpenAlex" in l)
            assert '`name=""`' in line, guide

    def test_prompts_are_presented(self, prompts):
        text = _read("README.md")
        assert prompts and all(f"`{p}`" in text for p in prompts)


# === Sources, formats, fichiers ===

class TestSourcesAndFiles:
    def test_every_source_and_group_is_named(self):
        text = _read("README.md")
        for key in list(CONNECTORS) + list(SOURCE_GROUPS):
            assert f"`{key}`" in text, key

    def test_twelve_sources(self):
        assert len(CONNECTORS) == 12
        for guide in ("README.md", "MCP_INSTALL.md", "REMOTE.md"):
            assert "douze" in _read(guide), guide

    def test_scholar_stays_out_of_collective_groups(self):
        assert "google_scholar" not in SOURCE_GROUPS["all"]
        assert "google_scholar" not in SOURCE_GROUPS["international"]
        text = _read("README.md")
        assert "**ni au groupe `international` ni au groupe `all`**" in text

    def test_file_suffixes_and_formats_are_documented(self):
        from utils.corpus_diff import DIFF_FORMAT
        from utils.doi_resolver import REPORT_FORMAT, RULES_VERSION
        from utils.manifest import MANIFEST_FORMAT
        from utils.ranking import RANKING_VERSION
        text = _read("README.md")
        for token in (OF.CSL_SUFFIX, OF.STATS_SUFFIX, OF.MANIFEST_SUFFIX,
                      OF.DOI_REPORT_SUFFIX, OF.DIFF_REPORT_SUFFIX,
                      OF.INDEX_NAME, OF.BIBTEX_SUFFIX, OF.RIS_SUFFIX,
                      OF.NATIVE_AUDIT_DIR, DIFF_FORMAT, REPORT_FORMAT,
                      MANIFEST_FORMAT, RULES_VERSION, RANKING_VERSION):
            assert token in text, token

    def test_derived_names_are_not_claimed_unique(self, tmp_path, monkeypatch):
        """Revue d'OPT-21 : corpus_filter garde un nom dérivé sans uuid ; un
        second filtrage de même suffixe remplace le premier. Le README le dit
        au lieu d'annoncer des noms uniques pour tous les producteurs."""
        import json
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        src = tmp_path / "corpus_t_20260929_120000_abcd1234.json"
        src.write_text(json.dumps([
            {"itemType": "journalArticle", "title": f"T{y}", "date": str(y),
             "creators": [], "extra": "Sources: HAL"} for y in (2010, 2020)]),
            encoding="utf-8")
        for low, high in ((2000, 2015), (2016, 2025)):
            asyncio.run(mcp_server._tool_corpus_filter(
                {"corpus_path": str(src), "year_min": low, "year_max": high,
                 "inline": "none"}))
        out = tmp_path / "corpus_t_20260929_120000_abcd1234_filtered.json"
        assert [i["title"] for i in json.loads(out.read_text(encoding="utf-8"))] == ["T2020"]
        para = next(l for l in _read("README.md").splitlines()
                    if l.startswith("- **Noms uniques ou dérivés**"))
        for token in ("`corpus_filter`", "`corpus_resolve_dois`", "`corpus_to_csl`",
                      "**remplace**", "`output_suffix` distinct"):
            assert token in para, token

    def test_resource_page_uri_is_documented(self):
        assert "corpus://<nom>?offset=<n>&limit=<n>" in _read("README.md")

    def test_structured_content_fields_exist(self, tmp_path, monkeypatch):
        """Les champs de ``structuredContent`` que cite le README sont rendus
        par ``moisson_search`` (sources simulées, stdio, digest) ; les champs
        propres au mode full ou à search_by_author existent dans le code."""
        from engine import orchestrator
        from engine.models import SourceResult

        def stub(name, cls, query, mps, output_dir, run_id, http_client,
                 from_date=None, to_date=None, *, run_context=None,
                 source_key=None, **kw):
            items = [{"itemType": "journalArticle",
                      "title": f"Discours numérique, étude {i}",
                      "creators": [{"creatorType": "author", "firstName": "A",
                                    "lastName": f"Nom{i}"}],
                      "date": "2020", "DOI": f"10.9/{source_key}{i}",
                      "extra": f"Sources: {name}"} for i in range(3)]
            result = SourceResult(source=source_key, display_name=name,
                                  items=items, status="complete")
            result.native_sort = "score desc,docid asc"
            result.native_ranks = [{"rank": r, "score": None,
                                    "sort": result.native_sort}
                                   for r in range(1, len(items) + 1)]
            result.bind_ranks()
            return result

        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(config, "RANKING_DEFAULT", "native")
        monkeypatch.setattr(orchestrator, "run_source", stub)
        blocks, structured = asyncio.run(mcp_server._tool_moisson_search(
            {"query": "discours numérique", "sources": ["hal"], "cache": False}))

        text = _read("README.md")
        para = next(line for line in text.splitlines()
                    if line.startswith("`structuredContent` reprend"))
        cited = set(re.findall(r"`([a-z_]+)`", para)) - {"structuredContent"}
        elsewhere = {"native_sort", "native_filters", "report_compact",
                     "report_detail", "search_by_author"} | set(CQ.INLINE_MODES)
        missing = sorted((cited - elsewhere) - set(structured))
        assert missing == [], f"champs absents du retour : {missing}"
        source = _read("mcp_server.py") + _read("engine/models.py")
        for name in (cited & elsewhere) - set(CQ.INLINE_MODES) - {"search_by_author"}:
            assert name in source, name


# === Variables d'environnement ===

class TestEnvironment:
    @pytest.mark.parametrize("doc", GUIDES + (ENV_EXAMPLE,))
    def test_cited_variables_exist(self, doc):
        known = _known_env_names()
        names, prefixes = _cited_env_names(_read(doc))
        assert names - known == set(), \
            f"{doc} cite des variables inconnues : {sorted(names - known)}"
        for prefix in prefixes:
            assert any(k.startswith(prefix) for k in known), prefix

    def test_every_code_variable_is_in_env_example(self):
        documented = set(_env_example_assignments())
        missing = sorted(_code_env_names() - documented)
        assert missing == [], f".env.example ne documente pas : {missing}"

    def test_api_keys_and_test_variables_are_in_env_example(self):
        documented = set(_env_example_assignments())
        for name in (config.ENV_OPENALEX_KEY, config.ENV_S2_KEY,
                     config.ENV_CORE_KEY, config.ENV_SERPAPI_KEY,
                     "MOISSON_TEST_REAL_KEYS", "MOISSON_DOMAIN", "ACME_EMAIL"):
            assert name in documented, name

    def test_env_example_holds_no_secret(self):
        for name, value in _env_example_assignments().items():
            if name.endswith(("_KEY", "_TOKEN", "_TOKENS", "_PASSWORD")):
                assert value == "", f"{name} porte une valeur"

    def test_env_example_defaults_match_the_code(self):
        defaults = _code_defaults()
        checked = 0
        for name, value in _env_example_assignments().items():
            if not value or "example" in value or "/chemin" in value:
                continue
            known = {d for d in defaults.get(name, set()) if d is not None}
            if len(known) != 1:
                continue
            (default,) = known
            if default == "":
                continue
            assert _same_value(value, default), \
                f"{name} : .env.example dit {value!r}, le code {default!r}"
            checked += 1
        assert checked >= 80  # la quasi-totalité des réglages est comparée


# === CLI ===

class _Captured(Exception):
    def __init__(self, parser):
        super().__init__("parser")
        self.parser = parser


def _parser_options(monkeypatch, fn, *args) -> set[str]:
    def fake_parse_args(self, *a, **k):
        raise _Captured(self)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse_args)
    try:
        fn(*args)
    except _Captured as captured:
        return {s for action in captured.parser._actions
                for s in action.option_strings}
    finally:
        monkeypatch.undo()
    raise AssertionError(f"aucun parseur construit par {fn!r}")


class TestCli:
    def test_options_in_readme_tables_exist(self, monkeypatch):
        import cli
        import main
        import setup_claude_desktop
        options = set()
        options |= _parser_options(monkeypatch, main.main)
        options |= _parser_options(monkeypatch, cli._dispatch_export, [])
        options |= _parser_options(monkeypatch, cli._dispatch_serve, [])
        options |= _parser_options(monkeypatch, cli._dispatch_token, [])
        options |= _parser_options(monkeypatch, cli._dispatch_setup_remote,
                                   ["--remote", "https://x.example/mcp"])
        options |= _parser_options(monkeypatch, setup_claude_desktop.main, [])
        cited = set()
        for line in _read("README.md").splitlines():
            if line.startswith("| `--"):
                cited.update(re.findall(r"`(--[a-z][a-z-]*)`", _split_row(line)[0]))
        assert len(cited) >= 30
        assert cited - options == set(), f"options inconnues : {sorted(cited - options)}"

    def test_new_search_options_are_documented(self):
        text = _read("README.md")
        for option in ("--cache", "--no-cache", "--mode", "--lang", "--query-en",
                       "--variant", "--plan-only", "--ranking", "--workers"):
            assert f"| `{option}`" in text or f"`{option}` |" in text \
                or f"/ `{option}`" in text, option
        assert "moisson-shs export" in text


# === Défauts énoncés ===

class TestStatedDefaults:
    # (variable, défaut du code, extrait du README qui l'énonce)
    CASES = [
        ("MOISSON_MCP_MAX_PER_SOURCE", "200", "200 (`MOISSON_MCP_MAX_PER_SOURCE`)"),
        ("MOISSON_MCP_WORKERS", "4", "4 (`MOISSON_MCP_WORKERS`)"),
        ("MOISSON_MCP_CACHE_TTL", "21600", "6 h (`MOISSON_MCP_CACHE_TTL`)"),
        ("MOISSON_STDIO_SOFT_DEADLINE", "50", "**échéance souple de 50 s**"),
        ("MOISSON_DIGEST_TOP", 40, "`top` (40)"),
        ("MOISSON_DIGEST_MAX_BYTES", 15000, "**15 000 octets**"),
        ("MOISSON_SERPAPI_MAX_PAGES", "10", "`MOISSON_SERPAPI_MAX_PAGES` (défaut 10"),
        ("MOISSON_SERPAPI_MIN_CREDITS", "100", "`MOISSON_SERPAPI_MIN_CREDITS` (défaut 100"),
        ("MOISSON_ISIDORE_RESULT_WINDOW", "200", "**200 premiers**"),
        ("MOISSON_RANKING", "native", "`native` (défaut)"),
        ("MOISSON_MAX_ITEMS_PER_SOURCE", "3000", "`MOISSON_MAX_ITEMS_PER_SOURCE` (3000)"),
        ("MOISSON_DEADLINE_SECONDS", "900", "dure, 900 s en HTTP"),
        ("MOISSON_INLINE_MAX_ITEMS", "300", "300 notices (`MOISSON_INLINE_MAX_ITEMS`)"),
        ("MOISSON_INLINE_MAX_BYTES", "2000000", "2 000 000 octets (`MOISSON_INLINE_MAX_BYTES`)"),
        ("MOISSON_QUERY_HTTP_KEEP", 10, "dix dernières sélections"),
    ]

    @pytest.mark.parametrize("name,default,snippet", CASES)
    def test_readme_states_the_code_default(self, name, default, snippet):
        assert _code_defaults()[name] == {default}, name
        assert snippet in _read("README.md"), snippet

    def test_constants_behind_the_prose(self, tools):
        text = _read("README.md")
        assert config.DEFAULT_MAX_PER_SOURCE == 1000
        assert "Plafond de notices par source (défaut 1000)" in text
        assert (config.STDIO_SOFT_DEADLINE_MIN_S, config.STDIO_SOFT_DEADLINE_MAX_S) == (10, 900)
        assert "ramené entre 10 et 900 s" in text
        assert CQ.QUERY_MAX_IDS == 20 and "`ids` (20 au plus)" in text
        schema = tools["moisson_search"].inputSchema["properties"]
        assert schema["max_per_source"]["default"] == 200
        assert schema["workers"]["default"] == 4
        assert schema["mode"]["default"] == "bibliographie"
        assert schema["inline"]["default"] == "digest"
        doi = tools["corpus_resolve_dois"].inputSchema["properties"]
        assert doi["title_threshold"]["minimum"] == config.DOI_TITLE_THRESHOLD == 0.90
        assert doi["strict_title_threshold"]["minimum"] == 0.97
        assert doi["year_tolerance"]["maximum"] == 1

    def test_reference_projection_is_the_same_in_readme_and_faq(self):
        """Revue d'OPT-21 : la moisson de référence d'avant chantier est
        projetée à environ 26 min (LOTAB1), dans le README comme dans la FAQ."""
        assert "projetée à environ 26 min" in _read("README.md")
        assert "environ 26 minutes projetées" in _read("docs/FAQ.md")

    def test_tool_timeout_value_is_the_retained_bound(self):
        for guide in ("README.md", "MCP_INSTALL.md", "docs/FAQ.md"):
            text = _read(guide)
            assert "240" in text and "60 s" in text, guide
        assert "**Le serveur retient 60 s comme borne prudente**" in _read("README.md")


# === Paquet, déploiement ===

def _pyproject() -> dict:
    if tomllib is None:  # pragma: no cover - Python 3.10 sans tomli
        pytest.skip("ni tomllib (Python >= 3.11) ni tomli disponibles")
    return tomllib.loads(_read("pyproject.toml"))


class TestPackagingAndDeployment:
    def test_sdk_pin_is_documented_and_applied(self):
        project = _pyproject()["project"]
        for extra in ("mcp", "http", "remote"):
            assert "mcp>=1.27,<2" in project["optional-dependencies"][extra]
        m = re.search(r"^mcp==(\d+)\.", _read("constraints.txt"), re.M)
        assert m and m.group(1) == "1"
        for guide in ("README.md", "MCP_INSTALL.md", "DEPLOY.md", "docs/FAQ.md"):
            text = _read(guide)
            assert "mcp>=1.27,<2" in text and "constraints.txt" in text, guide

    def test_package_description_names_the_twelve_sources(self):
        project = _pyproject()["project"]
        description = project["description"]
        for label in ("HAL", "Isidore", "OpenEdition", "Cairn", "Persée",
                      "Theses.fr", "Érudit", "CrossRef", "OpenAlex",
                      "Semantic Scholar", "CORE", "Google Scholar"):
            assert label in description, label
        for keyword in ("bibtex", "ris", "openalex", "crossref", "mcp"):
            assert keyword in project["keywords"], keyword

    def test_redeployment_note(self):
        dockerfile = _read("Dockerfile")
        assert "MOISSON_CACHE_DIR=/data/cache/http" in dockerfile
        # Les deux fichiers de déploiement fournis sont documentés.
        deploy = _read("DEPLOY.md")
        for compose in DEPLOY_FILES[:2]:
            if (ROOT / compose).exists():
                assert compose in deploy, compose
        for guide in ("DEPLOY.md", "REMOTE.md"):
            text = _read(guide)
            assert "C03" in text and "redéploi" in text.lower(), guide
            assert "MOISSON_API_TOKEN" in text and "oauth_store.json" in text, guide
            assert "/data/cache/http" in text and "http_mcp.sqlite" in text, guide

    def test_nginx_redeployment_names_its_compose_file_everywhere(self):
        """Revue d'OPT-21 : la note de redéploiement de DEPLOY.md donne la
        variante Nginx AVANT et À CÔTÉ des commandes (un ``docker compose``
        sans ``-f`` sur un hôte Nginx recrée ``app`` sans sa publication
        127.0.0.1:8010 et tente de lancer Caddy sur 80/443)."""
        deploy = _read("DEPLOY.md")
        start = deploy.index("## 0.")
        section = deploy[start:deploy.index("## 1.", start)]
        nginx = "docker compose -f docker-compose.nginx.yml"
        warning = section.index("Identifier d'abord la variante en service")
        first_bare = section.index("docker compose build")
        assert warning < first_bare
        for command in ("build", "up -d", "stop app", "exec app ls /data/cache"):
            bare = section.count(f"docker compose {command}")
            assert bare and section.count(f"{nginx} {command}") >= 1, command
        assert "ci-dessous." not in section.split("Puis, dans cet ordre")[0]
        # Chaque étape numérotée qui cite une commande donne sa forme Nginx.
        for step in re.findall(r"^\d\. \*\*.*?(?=^\d\. \*\*|\Z)", section, re.M | re.S):
            if "docker compose" in step:
                assert nginx in step, step[:60]
        update = deploy[deploy.index("### Mise à jour"):deploy.index("### Versions épinglées")]
        assert f"{nginx} build" in update and f"{nginx} up -d" in update
        remote = _read("REMOTE.md")
        assert f"{nginx} build && {nginx} up -d" in remote

    def test_oauth_is_documented_as_delivered(self):
        text = _read("REMOTE.md")
        assert "## 4. OAuth 2.1" in text
        assert "MOISSON_OAUTH_ENABLED" in text and "MOISSON_OAUTH_PASSWORD" in text


# === Protocole d'évaluation ===

def test_evaluation_fingerprint_matches_the_module_or_asks_for_a_rebuild():
    """Revue d'OPT-21 : l'empreinte du classement que citent les ensembles
    de candidats (docs/EVALUATION.md) est celle du module actuel ; sinon le
    document demande de relancer le § 7 avant metrics.py et decide.py (le
    critère de version rendrait le verdict « indéterminé »)."""
    import hashlib
    import utils.ranking as ranking
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from eval.rankers import module_fingerprint
    current = module_fingerprint(ranking)
    assert current == hashlib.sha256(
        Path(ranking.__file__).read_bytes()).hexdigest()[:12]
    text = _read("docs/EVALUATION.md")
    assert "version finale du classement" not in text
    para = next(l for l in text.splitlines()
                if l.startswith("**Situation au "))
    m = re.search(r"portent l'empreinte du code `([0-9a-f]{12})`", para)
    assert m, "empreinte des ensembles non citée"
    if m.group(1) != current:
        for token in ("relancer le § 7", "`pool.py … build`", "`metrics.py`",
                      "`decide.py`", "« indéterminé »"):
            assert token in para, token
        assert "reconstruits hors réseau" in text.split("## 1.")[0]


# === Compatibilité Python 3.10 (matrice CI) ===

def test_tomllib_imports_are_guarded_for_python_310():
    """Revue d'OPT-21 : ``tomllib`` n'existe qu'à partir de Python 3.11 ;
    la CI exécute la suite sous 3.10. Tout ``import tomllib`` des tests est
    placé dans un ``try`` qui intercepte ImportError."""
    offenders = []
    for path in (ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guarded = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any(
                    isinstance(h.type, ast.Name)
                    and h.type.id in ("ImportError", "ModuleNotFoundError")
                    for h in node.handlers):
                for child in node.body:
                    for sub in ast.walk(child):
                        guarded.add(id(sub))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Import)
                    and any(a.name == "tomllib" for a in node.names)
                    and id(node) not in guarded):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], offenders


# === Phrases périmées (C34) ===

STALE = (
    "308 tests", "303 hors réseau", "21 s pour", "4 × 500", "4 x 500",
    "quatre outils", "8 outils", "9 outils", "100 crédits par mois",
    # Anciennes mentions de l'éditeur du SDK et de sa politique (retirées).
    "SDK MCP officiel d'", "publié sur PyPI par", "envoie-t-il les corpus à",
    "Cf. politique",
    "Couche d'accès aux 7 sources", "interroge les 7 sources",
    "API publiques des 7 sources",
    "traitent la requête en sac de mots",
    "honore les booléens que les autres ignorent",
    "honore les guillemets et les booléens",
    "--workers 2", "recommandé : 2", "Authentification Bearer : ACTIVE",
    "Il n'accède pas au reste du système de fichiers",
    "Le moissonnage dure plusieurs minutes",
    # Revue d'OPT-21.
    "ORCID seul, sans nom", "tous les producteurs ajoutent un uuid",
    "vingtaine de minutes projetées", "version finale du classement",
)


@pytest.mark.parametrize("doc", GUIDES + (ENV_EXAMPLE, "pyproject.toml"))
def test_no_stale_statement(doc):
    text = _read(doc)
    found = [phrase for phrase in STALE if phrase in text]
    assert found == [], f"{doc} : {found}"
