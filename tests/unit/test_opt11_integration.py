"""
OPT-11 : intégration du plan de requête dans le moteur, le serveur MCP et la
CLI. Aucun accès réseau : ``run_source`` est remplacé par des doublures.
"""

import asyncio
import json
import sys

import pytest

import mcp_server
from engine import orchestrator
from engine.models import RunContext, SearchRequest, SourceResult
from connectors.registry import resolve_sources
from utils.query_plan import QueryPlanError

ISIDORE_FAMILY = ["isidore", "openedition", "cairn", "persee", "theses_fr",
                  "erudit"]
EXPECTED_TOOLS = {
    "moisson_search", "corpus_analyze", "corpus_list", "corpus_filter",
    "corpus_resolve_dois", "corpus_to_csl", "fetch_by_doi",
    "search_by_author", "corpus_merge",
}
# Arguments de moisson_search avant OPT-11 (invariant 2 : conservés).
LEGACY_ARGS = {"query", "max_per_source", "sources", "enrich_oai", "dedup",
               "from_date", "to_date", "cache", "workers", "mode"}


class _NullClient:
    cache_enabled = False
    cancel_signal = None

    def get(self, *a, **k):  # pragma: no cover - jamais appelé
        raise AssertionError("aucun accès réseau attendu")

    def close(self):
        pass


def _boom(*a, **k):
    raise AssertionError("appel inattendu (aucune exécution attendue)")


def _items(name, languages=("fr", "en", None)):
    out = []
    for k, lang in enumerate(languages):
        item = {"itemType": "journalArticle", "title": f"{name} {k}",
                "creators": [], "tags": [], "collections": [], "relations": {},
                "url": f"https://example.org/{name}/{k}"}
        if lang:
            item["language"] = lang
        out.append(item)
    return out


def _named_recorder(calls):
    """Doublure à paramètres nommés ``mode`` et ``languages`` (comme le
    ``run_source`` réel depuis OPT-12)."""
    def fake(name, cls, query, max_per_source, output_dir, run_id,
             http_client, from_date=None, to_date=None, *, run_context=None,
             source_key=None, mode=None, languages=None, **kwargs):
        calls[source_key] = {"query": query, "mode": mode,
                             "languages": languages,
                             "ctor": {k: v for k, v in kwargs.items()
                                      if k not in ("scan_registry",
                                                   "enrich_oai")}}
        return SourceResult(source=source_key, display_name=name,
                            items=_items(name))
    return fake


def _harvest(monkeypatch, runner, **kw):
    monkeypatch.setattr(orchestrator, "run_source", runner)
    kw.setdefault("dedup", False)
    request = SearchRequest(**kw)
    return orchestrator.harvest(request, RunContext(http_client=_NullClient()))


# === Moteur ===

class TestHarvest:
    def test_each_source_receives_its_own_query(self, monkeypatch):
        calls = {}
        _harvest(monkeypatch, _named_recorder(calls),
                 query='"discours numérique" AND (twitter OR facebook) '
                       'NOT publicité',
                 sources=resolve_sources(["all"]) + ["google_scholar"])
        assert calls["hal"]["query"] == ('"discours numérique" AND (twitter OR '
                                         'facebook) NOT publicité')
        assert calls["crossref"]["query"] == "discours numérique twitter facebook"
        assert calls["semantic_scholar"]["query"] == (
            '"discours numérique" + (twitter | facebook) -publicité')
        assert calls["google_scholar"]["query"] == (
            '"discours numérique" (twitter OR facebook) -publicité')
        # Un seul flux Isidore : même requête pour la source et ses
        # sous-sources.
        assert {calls[k]["query"] for k in ISIDORE_FAMILY} == {
            '"discours numérique" ET (twitter OU facebook) SAUF publicité'}

    def test_simple_query_reaches_every_source_unchanged(self, monkeypatch):
        calls = {}
        _harvest(monkeypatch, _named_recorder(calls), query="discours numérique",
                 sources=resolve_sources(["all"]))
        assert {c["query"] for c in calls.values()} == {"discours numérique"}

    def test_mode_is_passed_to_run_source(self, monkeypatch):
        calls = {}
        _harvest(monkeypatch, _named_recorder(calls), query="q",
                 sources=["hal", "crossref"], mode="collecte")
        assert {c["mode"] for c in calls.values()} == {"collecte"}
        calls.clear()
        _harvest(monkeypatch, _named_recorder(calls), query="q",
                 sources=["hal"])
        assert calls["hal"]["mode"] == "bibliographie"

    def test_languages_passed_only_where_native(self, monkeypatch):
        calls = {}
        _harvest(monkeypatch, _named_recorder(calls), query="q",
                 sources=resolve_sources(["all"]) + ["google_scholar"],
                 languages=["fr"])
        assert calls["hal"]["languages"] == ["fr"]
        assert calls["openalex"]["languages"] == ["fr"]
        for key in ISIDORE_FAMILY + ["crossref", "semantic_scholar", "core"]:
            assert calls[key]["languages"] is None, key
        assert calls["google_scholar"]["ctor"] == {"lr": "lang_fr"}
        assert all(not c["ctor"] for k, c in calls.items()
                   if k != "google_scholar")

    def test_runner_without_named_options_still_works(self, monkeypatch):
        """Doublure historique (``**kwargs`` seulement) : appel accepté."""
        seen = []
        monkeypatch.setattr(orchestrator, "run_source",
                            lambda name, *a, **k: seen.append(k) or [])
        orchestrator.harvest(
            SearchRequest(query="q", sources=["hal"], languages=["fr"]),
            RunContext(http_client=_NullClient()))
        assert seen and seen[0]["languages"] == ["fr"]
        assert seen[0]["mode"] == "bibliographie"

    def test_local_language_filter_on_sources_without_native_filter(
            self, monkeypatch):
        calls = {}
        result = _harvest(monkeypatch, _named_recorder(calls), query="q",
                          sources=["hal", "isidore", "core", "crossref"],
                          languages=["fr"])
        by = {r.source: r for r in result.sources}
        # HAL : filtre natif, rien d'écarté localement.
        assert by["hal"].item_count == 3
        assert "language_filter_removed" not in by["hal"].counters
        # Isidore et CORE : notice « en » écartée, notice sans langue gardée.
        for key in ("isidore", "core"):
            res = by[key]
            assert [i.get("language") for i in res.items] == ["fr", None]
            assert res.counters["language_filter_removed"] == 1
            assert res.counters["language_filter_unknown_kept"] == 1
            assert any("filtre local de langue" in w for w in res.warnings)
        # CrossRef : aucun filtre possible (vérifié), rien d'écarté.
        assert by["crossref"].item_count == 3
        plan = result.query_plan
        applied = {s["source"]: s.get("applied") for s in plan["sources"]}
        assert applied["isidore"] == {"language_filter": {
            "languages": ["fr"], "kept": 2, "removed": 1, "unknown_kept": 1}}
        assert applied["hal"] is None and applied["crossref"] is None
        assert result.total_before_dedup == 3 + 2 + 2 + 3

    def test_plan_never_enters_the_notices(self, monkeypatch):
        """Invariant 1 : plan, requêtes envoyées et compteurs hors notices."""
        result = _harvest(monkeypatch, _named_recorder({}),
                          query='"a b" OR c', sources=["hal", "crossref"],
                          languages=["fr"])
        assert result.items == _items("HAL") + _items("CrossRef")
        blob = json.dumps(result.items, ensure_ascii=False)
        assert "query_sent" not in blob and "language_filter" not in blob

    def test_query_plan_is_attached_to_the_result(self, monkeypatch):
        result = _harvest(monkeypatch, _named_recorder({}),
                          query="discours", sources=["hal", "crossref"],
                          query_variants={"en": "discourse"})
        plan = result.query_plan
        json.dumps(plan, ensure_ascii=False)
        assert [s["source"] for s in plan["sources"]] == ["hal", "crossref"]
        assert plan["sources"][1]["query_sent"] == "discourse"
        assert plan["sources"][1]["query_origin"] == "variant:en"

    def test_invalid_request_fails_before_any_call(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_source", _boom)
        with pytest.raises(QueryPlanError):
            orchestrator.harvest(SearchRequest(query="AND OR", sources=["hal"]),
                                 RunContext(http_client=_NullClient()))
        with pytest.raises(QueryPlanError):
            orchestrator.harvest(
                SearchRequest(query="q", sources=["hal"], from_date="2024",
                              to_date="2020"),
                RunContext(http_client=_NullClient()))
        with pytest.raises(ValueError):
            SearchRequest(query="q", sources=["hal"], languages=["xx"])
        with pytest.raises(ValueError):
            SearchRequest(query="q", sources=["hal"],
                          query_variants={"english": "x"})

    def test_cancelled_harvest_partial_result_carries_the_plan(
            self, monkeypatch):
        from utils.http import OperationCancelled

        def cancelled(name, cls, *a, source_key=None, **k):
            return SourceResult(source=source_key, display_name=name,
                                status="cancelled", reason="cancelled")
        monkeypatch.setattr(orchestrator, "run_source", cancelled)
        with pytest.raises(OperationCancelled) as exc:
            orchestrator.harvest(
                SearchRequest(query='"a b" OR c', sources=["hal"]),
                RunContext(http_client=_NullClient()))
        plan = exc.value.result.query_plan
        assert plan["sources"][0]["query_sent"] == '"a b" OR c'

    def test_request_plan_is_computed_once(self):
        request = SearchRequest(query="q", sources=["hal"])
        assert request.query_plan() is request.query_plan()


# === Serveur MCP ===

def _run(coro):
    return asyncio.run(coro)


class TestMcpSchema:
    def test_nine_tools_unchanged_and_new_arguments_added(self):
        tools = _run(mcp_server.list_tools())
        # Les neuf outils historiques restent exposés ; le lot D en ajoute
        # (corpus_export, OPT-18…) : présence vérifiée, pas un nombre figé.
        names = [t.name for t in tools]
        assert EXPECTED_TOOLS <= set(names)
        assert len(names) == len(set(names))
        schema = next(t for t in tools if t.name == "moisson_search").inputSchema
        props = set(schema["properties"])
        assert LEGACY_ARGS <= props
        assert {"query_variants", "languages", "plan_only"} <= props
        assert schema["required"] == ["query"]
        assert schema["properties"]["query_variants"]["type"] == "object"
        assert schema["properties"]["languages"]["type"] == "array"


class TestMcpPlanOnly:
    ARGS = {"query": '"discours numérique" NOT publicité',
            "sources": ["all"], "languages": ["fr"],
            "query_variants": {"en": '"digital discourse"'},
            "plan_only": True}

    def _forbid_execution(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "HTTPClient", _boom)
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", _boom)
        monkeypatch.setattr(orchestrator, "run_source", _boom)

    def test_stdio_preview_runs_nothing_and_writes_nothing(
            self, monkeypatch, tmp_path):
        self._forbid_execution(monkeypatch, tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        blocks, structured = _run(mcp_server._tool_moisson_search(self.ARGS))
        assert "Aperçu du plan de requête" in blocks[0].text
        assert structured["plan_only"] is True
        plan = structured["query_plan"]
        by = {s["source"]: s for s in plan["sources"]}
        assert by["openalex"]["query_sent"] == '"digital discourse"'
        assert by["isidore"]["query_sent"] == '"discours numérique" SAUF publicité'
        assert list(tmp_path.iterdir()) == []

    def test_http_preview_has_no_disk_path(self, monkeypatch, tmp_path):
        self._forbid_execution(monkeypatch, tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        blocks, structured = _run(mcp_server._tool_moisson_search(self.ARGS))
        text = blocks[0].text + json.dumps(structured, ensure_ascii=False)
        assert str(tmp_path) not in text
        assert structured["transport"] == "http"
        assert list(tmp_path.iterdir()) == []

    def test_dispatcher_returns_structured_content(self, monkeypatch, tmp_path):
        self._forbid_execution(monkeypatch, tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        result = _run(mcp_server.call_tool("moisson_search", dict(self.ARGS)))
        blocks, structured = result
        assert structured["query_plan"]["languages"] == ["fr"]


class TestMcpArguments:
    def test_variants_and_languages_reach_the_engine(self, monkeypatch):
        captured = {}

        def fake_sync(*args, **kwargs):
            captured.update(kwargs)
            return ([], {"total_unique": 0})

        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", fake_sync)
        _run(mcp_server._tool_moisson_search({
            "query": "discours", "sources": ["hal"], "languages": ["fr"],
            "query_variants": {"en": "discourse"}}))
        assert captured["languages"] == ["fr"]
        assert captured["query_variants"] == {"en": "discourse"}

    def test_absent_new_arguments_are_not_transmitted(self, monkeypatch):
        captured = {}

        def fake_sync(*args, **kwargs):
            captured.update(kwargs)
            return ([], {"total_unique": 0})

        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", fake_sync)
        _run(mcp_server._tool_moisson_search({"query": "discours",
                                              "sources": ["hal"]}))
        assert "languages" not in captured and "query_variants" not in captured

    @pytest.mark.parametrize("bad", [
        {"query": "NOT politique"},
        {"query": "AND OR"},
        {"query": "discours", "languages": ["xx"]},
        {"query": "discours", "query_variants": {"en": ""}},
        {"query": "discours", "from_date": "2020-13-01"},
        {"query": "discours", "from_date": "2024", "to_date": "2020"},
    ])
    def test_invalid_arguments_are_refused_before_harvest(self, monkeypatch,
                                                          bad):
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", _boom)
        args = {"sources": ["hal"], **bad}
        result = _run(mcp_server._tool_moisson_search(args))
        assert isinstance(result, mcp_server.ToolError)
        assert result.startswith("Paramètre refusé")


class TestMcpHarvestReturn:
    def test_query_plan_always_in_structured_content(self, monkeypatch,
                                                     tmp_path):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(orchestrator, "run_source", _named_recorder({}))
        blocks, structured = mcp_server._run_moisson_sync(
            '"discours numérique" OR récit', 10, ["hal", "crossref"], False,
            False, languages=["fr"])
        plan = structured["query_plan"]
        assert [s["source"] for s in plan["sources"]] == ["hal", "crossref"]
        assert plan["sources"][1]["translation"] == "bag_of_words"
        assert "Plan de requête" in blocks[0].text

    def test_query_plan_present_on_total_failure(self, monkeypatch, tmp_path):
        def failing(name, cls, *a, source_key=None, **k):
            return SourceResult(source=source_key, display_name=name,
                                status="error", reason="http_error",
                                detail="HTTP 503")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(orchestrator, "run_source", failing)
        result = mcp_server._run_moisson_sync("discours", 10, ["hal"], False,
                                              True)
        assert isinstance(result, mcp_server.ToolError)
        assert result.structured["query_plan"]["sources"][0]["source"] == "hal"

    def test_http_harvest_plan_without_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                            "https://moisson.example.org")
        monkeypatch.setattr(orchestrator, "run_source", _named_recorder({}))
        blocks, structured = mcp_server._run_moisson_sync(
            '"discours numérique" NOT publicité', 10, ["hal", "crossref"],
            False, False)
        assert "query_plan" in structured
        blob = json.dumps(structured, ensure_ascii=False) + blocks[0].text
        assert str(tmp_path) not in blob


# === CLI ===

class TestCli:
    def test_plan_only_prints_and_writes_nothing(self, monkeypatch, tmp_path,
                                                 capsys):
        import main as main_module
        out = tmp_path / "out"
        monkeypatch.setattr(main_module, "HTTPClient", _boom)
        monkeypatch.setattr(orchestrator, "run_source", _boom)
        monkeypatch.setattr(sys, "argv", [
            "main.py", '"discours numérique" NOT publicité', "--sources", "all",
            "--query-en", '"digital discourse"', "--lang", "fr", "en",
            "--plan-only", "--output", str(out)])
        assert main_module.main() == 0
        text = capsys.readouterr().out
        assert "Aperçu" in text and "Plan de requête (mode collecte" in text
        assert '"discours numérique" ET' not in text     # SAUF sans ET
        assert '"discours numérique" SAUF publicité' in text
        assert "digital discourse" in text
        assert not out.exists()

    def test_variant_option_and_language_are_passed(self, monkeypatch,
                                                    tmp_path):
        import main as main_module
        calls = {}
        monkeypatch.setattr(orchestrator, "run_source", _named_recorder(calls))
        monkeypatch.setattr(sys, "argv", [
            "main.py", "discours", "--sources", "hal", "crossref",
            "--variant", "en=discourse", "--lang", "fr",
            "--output", str(tmp_path)])
        assert main_module.main() == 0
        assert calls["crossref"]["query"] == "discourse"
        assert calls["hal"]["query"] == "discours"
        assert calls["hal"]["languages"] == ["fr"]
        assert calls["hal"]["mode"] == "collecte"

    @pytest.mark.parametrize("argv", [
        ["AND OR", "--plan-only"],
        ["discours", "--lang", "xx", "--plan-only"],
        ["discours", "--variant", "en", "--plan-only"],
        ["discours", "--from-date", "2024", "--to-date", "2020", "--plan-only"],
    ])
    def test_invalid_options_exit_2(self, monkeypatch, tmp_path, argv):
        import main as main_module
        monkeypatch.setattr(main_module, "HTTPClient", _boom)
        monkeypatch.setattr(sys, "argv", ["main.py", *argv,
                                          "--output", str(tmp_path / "o")])
        with pytest.raises(SystemExit) as exc:
            main_module.main()
        assert exc.value.code == 2
        assert not (tmp_path / "o").exists()
