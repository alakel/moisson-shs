"""
Tests OPT-13 — classement dans le moteur, le serveur MCP et la CLI.

Sans réseau ni socket (exécutants de source remplacés) :

- ``harvest`` calcule toujours le classement ``native`` après la
  déduplication ; ``local`` seulement sur demande ; ``off`` : aucun ; la
  valeur par défaut vient de ``config.RANKING_DEFAULT`` (``MOISSON_RANKING``,
  ``native`` dans un processus neuf : ``local`` désactivé par défaut) ;
- l'ordre et le contenu des notices sont identiques quel que soit le
  classement (invariant 1, ordre déterministe d'OPT-06) ;
- une demande de classement invalide est refusée avant toute source ; une
  erreur interne du classement ne fait pas échouer la moisson ; une
  annulation pendant le classement ne publie rien ;
- acquisition et classement mesurés séparément (``acquisition_s``,
  ``duration_s``) ;
- ``moisson_search`` : argument ``ranking`` (ajout, anciens arguments
  conservés), ``structuredContent.top_ranked`` (identifiants, rang, score,
  version) sans chemin disque, absent avec ``off`` ;
- CLI : ``--ranking`` transmis au moteur, classement affiché, fichier de
  corpus inchangé.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.models import RunContext, SearchRequest, SourceResult  # noqa: E402
from utils import ranking  # noqa: E402
from utils.http import OperationCancelled  # noqa: E402
from utils.ranking import RANKING_VERSION  # noqa: E402

HAL_SORT = "score desc,docid asc"


# === Univers simulé ===

def _art(title, *, doi=None, label="HAL", abstract="", lines=(), url=None,
         author="Dupont", year="2021"):
    item = {"itemType": "journalArticle", "title": title,
            "creators": [{"creatorType": "author", "firstName": "Anne",
                          "lastName": author}],
            "date": year, "abstractNote": abstract, "tags": []}
    if doi:
        item["DOI"] = doi
    if url:
        item["url"] = url
    item["extra"] = "\n".join([f"Sources: {label}", *lines])
    return item


def _isidore(n, title, label, **kw):
    return _art(title, label=label, author="Martin",
                lines=(f"Isidore URI: https://isidore.science/document/10670/1.{n:06d}",),
                **kw)


def _universe():
    """Notices par source (items, tri natif) : HAL et CrossRef classés par
    pertinence, Isidore et Cairn (même flux), Semantic Scholar par
    citations."""
    hal = [_art(f"Discours numérique et institutions {i}", doi=f"10.7/h{i}",
                abstract="Le discours numérique des institutions.")
           for i in range(12)]
    crossref = [_art(f"Discours numérique et institutions {i}", doi=f"10.7/h{i}",
                     label="CrossRef") for i in range(0, 12, 3)]
    crossref += [_art(f"Chimie organique {i}", doi=f"10.7/c{i}", label="CrossRef",
                      lines=("Cited by: 50000",)) for i in range(3)]
    isidore = [_isidore(i, f"Revues savantes en ligne {i}", "Isidore",
                        abstract="Revues et discours numérique.")
               for i in range(6)]
    cairn = [_isidore(i, f"Revues savantes en ligne {i}", "Cairn",
                      abstract="Revues et discours numérique.")
             for i in range(0, 6, 2)]
    s2 = [_art(f"Chimie organique {i}", doi=f"10.7/c{i}",
               label="Semantic Scholar", lines=("Cited by: 50000",))
          for i in range(3)]
    return {
        "hal": (hal, HAL_SORT, None),
        "crossref": (crossref, "score:desc", None),
        "isidore": (isidore, "isidore_default", None),
        "cairn": (cairn, "isidore_default", [1, 3, 5]),
        "semantic_scholar": (s2, "citationCount:desc", None),
    }


def _runner(universe=None, calls=None):
    data = universe or _universe()

    def stub(name, cls, query, mps, output_dir, run_id, http_client,
             from_date=None, to_date=None, *, run_context=None,
             source_key=None, **kw):
        if calls is not None:
            calls.append(source_key)
        items, sort, ranks = data[source_key]
        items = copy.deepcopy(items)
        result = SourceResult(source=source_key, display_name=name,
                              items=items, status="complete")
        if sort:
            result.native_sort = sort
            result.native_ranks = [
                {"rank": r, "score": None, "sort": sort}
                for r in (ranks or range(1, len(items) + 1))]
            result.bind_ranks()
        return result
    return stub


class _Client:
    cache_enabled = False
    cancel_signal = None

    def close(self):
        pass


SOURCES = ["hal", "crossref", "isidore", "cairn", "semantic_scholar"]


def _harvest(ranking_mode=None, **kw):
    request = SearchRequest(query="discours numérique", sources=list(SOURCES),
                            workers=2, **kw)
    ctx = RunContext(http_client=_Client())
    if ranking_mode is None:
        return orchestrator.harvest(request, ctx)
    return orchestrator.harvest(request, ctx, ranking=ranking_mode)


@pytest.fixture
def stub_sources(monkeypatch):
    monkeypatch.setattr(orchestrator, "run_source", _runner())
    monkeypatch.setattr(config, "RANKING_DEFAULT", "native")


# === Moteur ===

class TestHarvestRanking:
    def test_native_by_default(self, stub_sources):
        result = _harvest()
        rk = result.ranking
        assert rk["mode"] == "native" and rk["version"] == RANKING_VERSION
        assert len(rk["entries"]) == len(result.items) > 0
        assert sorted(e["index"] for e in rk["entries"]) == list(range(len(result.items)))
        # Acquisition et classement mesurés séparément.
        assert rk["acquisition_s"] >= 0
        assert set(rk["duration_s"]) == {"mapping", "native", "local", "total"}
        assert rk["duration_s"]["local"] == 0.0
        assert rk["mapping"]["unmapped"] == 0 and rk["mapping"]["ambiguous"] == 0

    def test_local_only_on_request(self, stub_sources):
        assert _harvest("native").ranking["local"] is None
        local = _harvest("local").ranking
        assert local["mode"] == "local"
        assert local["local"]["lexical_model"] == "simple"
        assert local["duration_s"]["local"] >= 0
        # Le classement natif reste calculé et exposé dans chaque entrée.
        assert all("native" in e and "local" in e for e in local["entries"])

    def test_off_computes_nothing(self, stub_sources, monkeypatch):
        called = []
        monkeypatch.setattr(ranking, "rank_records",
                            lambda *a, **k: called.append(1))
        result = _harvest("off")
        assert result.ranking is None and called == []

    def test_default_from_config(self, stub_sources, monkeypatch):
        monkeypatch.setattr(config, "RANKING_DEFAULT", "local")
        assert _harvest().ranking["mode"] == "local"
        monkeypatch.setattr(config, "RANKING_DEFAULT", "off")
        assert _harvest().ranking is None

    def test_request_attribute_is_honoured(self, stub_sources):
        request = SearchRequest(query="discours numérique", sources=["hal"])
        request.ranking = "local"      # attribut facultatif (ajout futur)
        result = orchestrator.harvest(request, RunContext(http_client=_Client()))
        assert result.ranking["mode"] == "local"

    def test_items_order_and_content_unchanged(self, stub_sources):
        off = _harvest("off").items
        native = _harvest("native").items
        local = _harvest("local").items
        assert off == native == local
        for item in native:
            assert not {"rank", "score", "ranking", "justification"} & set(item)
            assert "Ranking" not in (item.get("extra") or "")

    def test_invalid_ranking_refused_before_any_source(self, monkeypatch):
        calls = []
        monkeypatch.setattr(orchestrator, "run_source", _runner(calls=calls))
        with pytest.raises(ValueError):
            _harvest("pertinence")
        assert calls == []

    def test_ranking_error_does_not_fail_the_harvest(self, stub_sources,
                                                     monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("panne du classement")
        monkeypatch.setattr(ranking, "rank_records", boom)
        result = _harvest("native")
        assert result.items
        assert result.ranking["error"].startswith("RuntimeError")
        assert result.ranking["entries"] == []

    def test_cancellation_during_ranking_publishes_nothing(self, stub_sources,
                                                           monkeypatch):
        real = ranking.rank_records
        holder = {}

        def cancel_then_rank(*a, check=None, **k):
            holder["ctx"].cancel()
            return real(*a, check=check, **k)
        monkeypatch.setattr(ranking, "rank_records", cancel_then_rank)
        request = SearchRequest(query="discours numérique", sources=["hal"])
        ctx = RunContext(http_client=_Client())
        holder["ctx"] = ctx
        with pytest.raises(OperationCancelled) as exc:
            orchestrator.harvest(request, ctx, ranking="native")
        assert exc.value.result is not None
        assert exc.value.result.items == []
        assert exc.value.result.ranking is None

    def test_isidore_family_counts_once_through_the_engine(self, stub_sources):
        result = _harvest("native")
        rk = result.ranking
        by_title = {result.items[e["index"]]["title"]: e for e in rk["entries"]}
        revue = by_title["Revues savantes en ligne 2"]
        dims = revue["dimensions"]["sources"]
        assert dims["independent"] == 1 and set(dims["sources"]) == {"Isidore", "Cairn"}
        contrib = revue["components"]["native_contributions"]
        assert [c["provider"] for c in contrib] == ["isidore"]
        # Même rang dans le flux partagé (3) : un seul terme, poids 0,5.
        assert revue["score"] == pytest.approx(0.5 / (60 + 3))
        classes = {s["source"]: s["class"] for s in rk["sources"]}
        assert classes == {"hal": "relevance", "crossref": "relevance",
                           "isidore": "presumed", "cairn": "presumed",
                           "semantic_scholar": "other"}

    def test_highly_cited_off_topic_notice_is_not_first(self, stub_sources):
        for mode in ("native", "local"):
            rk = _harvest(mode).ranking
            first = rk["entries"][0]
            assert first["dimensions"]["citations"] is None, mode


# === Défaut dans un processus neuf ===

@pytest.mark.parametrize("env_value, expected", [
    (None, "native"), ("local", "local"), ("OFF", "off"),
])
def test_config_default_in_a_fresh_process(env_value, expected):
    env = {k: v for k, v in os.environ.items() if k != "MOISSON_RANKING"}
    if env_value is not None:
        env["MOISSON_RANKING"] = env_value
    out = subprocess.run(
        [sys.executable, "-c", "import config; print(config.RANKING_DEFAULT)"],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
        timeout=60, check=True)
    assert out.stdout.strip() == expected


# === Serveur MCP ===

def _sync(tmp_path, monkeypatch, transport="stdio", **kw):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", transport)
    monkeypatch.setattr(orchestrator, "run_source", _runner())
    monkeypatch.setattr(config, "RANKING_DEFAULT", "native")
    return mcp_server._run_moisson_sync(
        "discours numérique", 50, list(SOURCES), False, True, cache=False,
        workers=2, **kw)


def _corpus_file(tmp_path):
    # OPT-16 : le manifeste (<stem>.manifest.json) s'ajoute aux fichiers.
    files = [p for p in tmp_path.glob("corpus_*.json")
             if not p.name.endswith((".csl.json", ".manifest.json"))]
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


class TestMcp:
    def test_schema_adds_ranking_and_keeps_the_old_arguments(self):
        tools = {t.name: t for t in asyncio.run(mcp_server.list_tools())}
        # Neuf outils historiques présents (le lot D en ajoute : corpus_export,
        # OPT-18…) ; présence vérifiée plutôt qu'un nombre figé.
        assert {"moisson_search", "corpus_analyze", "corpus_list",
                "corpus_filter", "corpus_resolve_dois", "corpus_to_csl",
                "fetch_by_doi", "search_by_author", "corpus_merge"} <= set(tools)
        props = tools["moisson_search"].inputSchema["properties"]
        assert props["ranking"]["enum"] == ["native", "local", "off"]
        for old in ("query", "max_per_source", "sources", "enrich_oai", "dedup",
                    "from_date", "to_date", "cache", "workers", "mode",
                    "query_variants", "languages", "plan_only"):
            assert old in props
        assert tools["moisson_search"].inputSchema["required"] == ["query"]

    def test_invalid_ranking_refused(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("aucune moisson attendue")
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", boom)
        result = asyncio.run(mcp_server._tool_moisson_search(
            {"query": "discours", "sources": ["hal"], "ranking": "bm25"}))
        assert isinstance(result, mcp_server.ToolError)
        assert result.startswith("Paramètre refusé")

    def test_ranking_argument_reaches_the_engine(self, monkeypatch):
        seen = {}

        def fake_sync(*args, **kwargs):
            seen.update(kwargs)
            return mcp_server.ToolError("arrêt du test")
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", fake_sync)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        asyncio.run(mcp_server._tool_moisson_search(
            {"query": "discours", "sources": ["hal"], "ranking": "local"}))
        assert seen.get("ranking") == "local"
        seen.clear()
        asyncio.run(mcp_server._tool_moisson_search(
            {"query": "discours", "sources": ["hal"]}))
        assert "ranking" not in seen      # défaut du serveur

    def test_top_ranked_in_structured_content(self, tmp_path, monkeypatch):
        # Extrait complet de top_ranked : retour « full » (OPT-17 : en mode
        # digest, les notices classées passent dans top_items).
        blocks, structured = _sync(tmp_path, monkeypatch, inline="full")
        top = structured["top_ranked"]
        assert top["version"] == RANKING_VERSION and top["mode"] == "native"
        assert top["records"] == structured["total_unique"]
        assert 0 < len(top["items"]) <= 20
        for item in top["items"]:
            assert set(item) == {"rank", "score", "id", "index"}
        corpus = _corpus_file(tmp_path)
        first = corpus[top["items"][0]["index"]]
        assert first["title"].startswith("Discours numérique")
        assert str(tmp_path) not in json.dumps(top, ensure_ascii=False)

    def test_corpus_order_is_the_same_for_every_ranking(self, tmp_path,
                                                        monkeypatch):
        orders = []
        for mode in ("off", "native", "local"):
            out = tmp_path / mode
            out.mkdir()
            blocks, structured = _sync(out, monkeypatch, ranking=mode)
            if mode == "off":
                assert "top_ranked" not in structured
            else:
                assert structured["top_ranked"]["mode"] == mode
            orders.append(_corpus_file(out))
        assert orders[0] == orders[1] == orders[2]

    def test_http_top_ranked_without_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                            "https://moisson.example.org")
        blocks, structured = _sync(tmp_path, monkeypatch, transport="http")
        assert structured["top_ranked"]["items"]
        blob = json.dumps(structured["top_ranked"], ensure_ascii=False)
        assert str(tmp_path) not in blob and "/Users" not in blob

    def test_fake_harvest_with_historical_signature_still_works(
            self, tmp_path, monkeypatch):
        # Les doublures harvest(request, ctx) des tests existants : sans
        # ranking explicite, l'adaptateur n'ajoute aucun argument.
        from engine.models import HarvestResult

        def fake_harvest(request, ctx):
            items = [_art("Seule notice", doi="10.7/seule")]
            return HarvestResult(
                run_id=ctx.run_id, request=request,
                sources=[SourceResult("hal", "HAL", items=items)],
                items=items, total_before_dedup=1)
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "harvest", fake_harvest)
        blocks, structured = mcp_server._run_moisson_sync(
            "q", 5, ["hal"], False, True, cache=False)
        assert "top_ranked" not in structured


# === CLI ===

class TestCli:
    def _argv(self, tmp_path, *extra):
        return ["main.py", "discours numérique", "--sources", *SOURCES,
                "--output", str(tmp_path), "--no-cache", *extra]

    def test_ranking_option_reaches_the_engine(self, tmp_path, monkeypatch):
        import main as main_module
        seen = []

        class _Stop(Exception):
            pass

        def fake_harvest(request, ctx, **kw):
            seen.append(kw)
            raise _Stop()
        monkeypatch.setattr(main_module, "harvest", fake_harvest)
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path, "--ranking", "local"))
        with pytest.raises(_Stop):
            main_module.main()
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path))
        with pytest.raises(_Stop):
            main_module.main()
        assert seen == [{"ranking": "local"}, {}]

    def test_invalid_option_rejected(self, tmp_path, monkeypatch):
        import main as main_module
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path, "--ranking", "bm25"))
        with pytest.raises(SystemExit) as exc:
            main_module.main()
        assert exc.value.code == 2

    def test_ranking_printed_and_corpus_unchanged(self, tmp_path, monkeypatch,
                                                  capsys):
        import main as main_module
        monkeypatch.setattr(orchestrator, "run_source", _runner())
        monkeypatch.setattr(config, "RANKING_DEFAULT", "native")
        corpora = {}
        for mode in ("off", "local"):
            out = tmp_path / mode
            monkeypatch.setattr(sys, "argv", [*self._argv(out), "--ranking", mode])
            assert main_module.main() == 0
            printed = capsys.readouterr().out
            if mode == "off":
                assert "Classement" not in printed
            else:
                assert "Classement local (" + RANKING_VERSION + ")" in printed
                assert "l'ordre du fichier de corpus est inchangé" in printed
                assert "rangs natifs" in printed
            corpora[mode] = _corpus_file(out)
        assert corpora["off"] == corpora["local"]
