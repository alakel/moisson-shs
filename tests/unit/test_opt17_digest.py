# -*- coding: utf-8 -*-
"""
OPT-17 : retour digest des outils producteurs (moisson_search).

- ``inline`` : ``digest`` par défaut en stdio, ``full`` = sprint 05, ``none``
  (résumé et liens) ; ``top`` (défaut 40, borné) ; valeurs refusées avant
  toute moisson ;
- digest dans l'ordre du classement (rang, score, identifiant) ; texte
  compact (titre, trois auteurs, année, support, type, identifiant, sources
  et rangs natifs, termes appariés, réserves) ; ``top_items`` ;
- plafond en octets du retour complet (lignes courtes puis notices omises),
  y compris après la ligne d'échéance souple ;
- plan de requête et statut par source compacts en mode digest, complets en
  mode full ;
- résumé : limite du fournisseur distincte d'une perte, filtre local de
  langue (avant/après) ;
- HTTP : contrat inchangé (aucun digest, aucun JSON inline, aucun chemin) ;
- moisson de référence du banc : retour <= 15 Ko avec 40 notices.

Aucun réseau (sources simulées, banc en processus fils avec garde-socket).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import config  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.models import HarvestResult, SearchRequest, SourceResult  # noqa: E402
from utils import corpus_query as CQ  # noqa: E402
from utils.ranking import RANKING_VERSION, record_id  # noqa: E402

HAL_SORT = "score desc,docid asc"


# === Univers simulé ===

def _art(i, *, label="HAL", doi=True, year=None, abstract=None, authors=2,
         title=None):
    item = {
        "itemType": "journalArticle",
        "title": title or f"Discours numérique et institutions, étude {i}",
        "creators": [{"creatorType": "author", "firstName": "Anne",
                      "lastName": f"Auteur{i}x{k}"} for k in range(authors)],
        "date": str(year or 2000 + i % 25),
        "publicationTitle": "Revue des discours",
        "abstractNote": ("Le discours numérique des institutions. " * 3
                         if abstract is None else abstract),
        "tags": [{"tag": "numérique"}],
        "extra": f"Sources: {label}",
    }
    if doi:
        item["DOI"] = f"10.7/d{i}"
    return item


def _runner(n_hal=60, n_crossref=20):
    data = {
        "hal": ([_art(i) for i in range(n_hal)], HAL_SORT),
        "crossref": ([_art(i, label="CrossRef") for i in range(0, n_crossref)],
                     "score:desc"),
    }

    def stub(name, cls, query, mps, output_dir, run_id, http_client,
             from_date=None, to_date=None, *, run_context=None,
             source_key=None, **kw):
        items, sort = data[source_key]
        items = copy.deepcopy(items)
        result = SourceResult(source=source_key, display_name=name,
                              items=items, status="complete")
        result.native_sort = sort
        result.native_ranks = [{"rank": r, "score": None, "sort": sort}
                               for r in range(1, len(items) + 1)]
        result.bind_ranks()
        return result
    return stub


@pytest.fixture
def stdio(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(config, "RANKING_DEFAULT", "native")
    monkeypatch.setattr(orchestrator, "run_source", _runner())
    return tmp_path


def _sync(**kw):
    return mcp_server._run_moisson_sync(
        "discours numérique", 100, ["hal", "crossref"], False, True,
        cache=False, workers=2, **kw)


def _types(blocks, name):
    return [b for b in blocks if getattr(b, "type", None) == name]


def _digest_block(blocks):
    found = [b for b in _types(blocks, "text")
             if b.text.startswith(CQ.DIGEST_HEADER)]
    assert len(found) == 1
    return found[0].text


def _entry_lines(text, n):
    lines = text.split("\n")
    return lines[len(lines) - n:]


def _corpus(tmp_path):
    files = [p for p in tmp_path.glob("corpus_*.json")
             if not p.name.endswith((".csl.json", ".manifest.json"))]
    assert len(files) == 1, files
    return files[0], json.loads(files[0].read_text(encoding="utf-8"))


# === Modes et paramètres ===

class TestModes:
    def test_digest_is_the_stdio_default(self, stdio):
        blocks, st = _sync()
        assert st["inline_mode"] == "digest"
        assert st["inline_json"] is False
        assert _types(blocks, "resource") == []            # aucun JSON inline
        assert len(_types(blocks, "resource_link")) == 3   # json, csl, stats
        items = st["top_items"]
        assert len(items) == st["digest"]["returned"] == 40
        assert [e["rank"] for e in items] == list(range(1, 41))
        text = _digest_block(blocks)
        assert text.startswith("Notices en tête (classement natif "
                               f"{RANKING_VERSION}) : 40 sur")
        # Une ligne par notice, dans l'ordre de top_items, identifiant compris.
        for line, entry in zip(_entry_lines(text, 40), items):
            assert line.startswith(f"{entry['rank']}. ")
            assert entry["id"] in line

    def test_full_restores_the_sprint_05_return(self, stdio):
        blocks, st = _sync(inline="full")
        assert st["inline_mode"] == "full"
        assert st["inline_json"] is True
        assert len(_types(blocks, "resource")) == 1
        assert "top_items" not in st and "digest" not in st
        # Plan et statuts complets ; extrait top_ranked complet.
        assert "compact" not in st["query_plan"]
        assert "native_sort" in st["by_source_status"]["HAL"]
        assert st["top_ranked"]["items"]

    def test_none_gives_summary_and_links_only(self, stdio):
        blocks, st = _sync(inline="none")
        assert st["inline_mode"] == "none"
        assert _types(blocks, "resource") == []
        assert "top_items" not in st
        assert not [b for b in _types(blocks, "text")
                    if b.text.startswith(CQ.DIGEST_HEADER)]
        assert len(_types(blocks, "resource_link")) == 3

    def test_top_is_honoured_and_bounded(self, stdio):
        _b, st = _sync(top=5)
        assert len(st["top_items"]) == 5 and st["digest"]["top"] == 5
        _b, st = _sync(top=10_000)
        assert st["digest"]["top"] == CQ.DIGEST_MAX_TOP

    @pytest.mark.parametrize("args", [{"inline": "tout"}, {"top": 0},
                                      {"top": "40"}, {"top": 2.5},
                                      {"inline": 3}])
    def test_invalid_values_refused_before_any_harvest(self, stdio,
                                                       monkeypatch, args):
        calls = []
        monkeypatch.setattr(orchestrator, "run_source",
                            lambda *a, **k: calls.append(1))
        res = asyncio.run(mcp_server._tool_moisson_search(
            {"query": "discours", "sources": ["hal"], **args}))
        assert mcp_server._is_tool_error(res)
        assert str(res).startswith("Paramètre refusé")
        assert calls == [] and not list(stdio.glob("corpus_*"))

    def test_tool_passes_inline_and_top_only_when_given(self, monkeypatch):
        seen = {}

        def fake_sync(*a, **k):
            seen.update(k)
            return ([], {"total_unique": 0})
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        monkeypatch.setattr(mcp_server, "_run_moisson_sync", fake_sync)
        asyncio.run(mcp_server._tool_moisson_search(
            {"query": "q", "sources": ["hal"]}))
        assert "inline" not in seen and "top" not in seen
        seen.clear()
        asyncio.run(mcp_server._tool_moisson_search(
            {"query": "q", "sources": ["hal"], "inline": "none", "top": 7}))
        assert seen["inline"] == "none" and seen["top"] == 7

    def test_schema_declares_inline_and_top_on_every_producer(self):
        tools = {t.name: t for t in asyncio.run(mcp_server.list_tools())}
        for name in ("moisson_search", "corpus_filter", "corpus_to_csl",
                     "corpus_merge", "search_by_author", "corpus_resolve_dois",
                     "corpus_export"):
            props = tools[name].inputSchema["properties"]
            assert props["inline"]["enum"] == ["digest", "full", "none"], name
            assert props["top"]["type"] == "integer", name
        # Arguments historiques conservés.
        assert {"query", "max_per_source", "sources", "ranking"} <= set(
            tools["moisson_search"].inputSchema["properties"])


# === Contenu du digest ===

class TestDigestContent:
    def test_order_score_and_ids_follow_the_engine_ranking(self, stdio):
        _b, full = _sync(inline="full", top=40)
        ranked = full["top_ranked"]["items"]          # 20 premières (OPT-13)
        _b, st = _sync()
        items = st["top_items"]
        assert [e["id"] for e in items[:20]] == [e["id"] for e in ranked]
        assert [e["score"] for e in items[:20]] == \
            [round(e["score"], 5) for e in ranked]
        assert set(items[0]) == {"rank", "score", "id"}
        assert st["top_ranked"]["items_in"] == "top_items"
        assert "items" not in st["top_ranked"]

    def test_line_carries_the_bibliographic_digest(self, stdio):
        blocks, st = _sync(top=3)
        line = _entry_lines(_digest_block(blocks), 3)[0]
        # Titre, année, trois auteurs au plus (« et al. »), support, type,
        # sources et rang natif, identifiant, termes appariés.
        assert line.startswith("1. Discours numérique et institutions")
        assert "Auteur0x0 A., Auteur0x1 A." in line
        assert "Revue des discours · article · " in line
        assert "HAL #1" in line and "CrossRef #1" in line
        assert "doi:10.7/d0" in line
        assert "+discours(t,r)" in line and "+numérique(t,m,r)" in line

    def test_authors_et_al_missing_terms_and_reserves(self, stdio,
                                                      monkeypatch):
        items = [_art(0, authors=5, abstract="", title="Un titre sans rapport")]
        items[0]["tags"] = []

        def stub(name, cls, query, mps, output_dir, run_id, http_client,
                 *a, source_key=None, **kw):
            return SourceResult(source=source_key, display_name=name,
                                items=copy.deepcopy(items), status="complete")
        monkeypatch.setattr(orchestrator, "run_source", stub)
        blocks, st = mcp_server._run_moisson_sync(
            "discours numérique", 10, ["hal"], False, True, cache=False,
            workers=1)
        line = _entry_lines(_digest_block(blocks), 1)[0]
        assert "Auteur0x0 A., Auteur0x1 A., Auteur0x2 A. et al." in line
        assert "−discours" in line and "−numérique" in line
        assert "⚠ sans résumé" in line

    def test_no_ranking_data_enters_the_notices(self, stdio):
        _b, st = _sync()
        path, corpus = _corpus(stdio)
        blob = path.read_text(encoding="utf-8")
        assert RANKING_VERSION not in blob and '"rank"' not in blob
        assert '"score"' not in blob
        assert [record_id(it) for it in corpus]      # notices intactes
        csl = path.with_name(path.name[:-5] + ".csl.json").read_text("utf-8")
        assert '"rank"' not in csl

    def test_digest_points_to_corpus_query(self, stdio):
        blocks, st = _sync()
        path, _ = _corpus(stdio)
        text = _digest_block(blocks)
        # Référence résolue en stdio quel que soit le répertoire courant
        # (revue d'OPT-17) : URI corpus:// d'un fichier de OUTPUT_DIR.
        assert f'corpus_query(corpus="corpus://{path.name}", ids=[…])' in text
        assert "offset=40" in text


# === Plafond en octets ===

class TestByteCap:
    def test_reference_sized_digest_stays_under_the_cap(self, stdio):
        blocks, st = _sync()
        total = mcp_server._measure_result(blocks, st)
        assert total <= CQ.DIGEST_MAX_BYTES
        assert st["digest"]["total_bytes"] == total

    def test_long_notices_shorten_then_drop(self, stdio, monkeypatch):
        long_title = "Un très long titre " * 20
        data = [_art(i, title=f"{long_title} {i}", authors=3) for i in range(80)]

        def stub(name, cls, query, mps, output_dir, run_id, http_client,
                 *a, source_key=None, **kw):
            result = SourceResult(source=source_key, display_name=name,
                                  items=copy.deepcopy(data), status="complete")
            result.native_sort = HAL_SORT
            result.native_ranks = [{"rank": r, "score": None, "sort": HAL_SORT}
                                   for r in range(1, 81)]
            result.bind_ranks()
            return result
        monkeypatch.setattr(orchestrator, "run_source", stub)
        monkeypatch.setattr(CQ, "DIGEST_MAX_BYTES", 6_000)
        blocks, st = mcp_server._run_moisson_sync(
            "discours", 100, ["hal"], False, True, cache=False, workers=1)
        info = st["digest"]
        assert mcp_server._measure_result(blocks, st) <= 6_000
        assert info["truncated"] is True
        assert info["full"] <= info["returned"] <= 40
        text = _digest_block(blocks)
        assert CQ.DIGEST_TRUNCATED_PREFIX in text
        lines = _entry_lines(text, info["returned"])
        assert len(st["top_items"]) == info["returned"]
        for line, entry in zip(lines, st["top_items"]):
            assert line.startswith(f"{entry['rank']}. ") and entry["id"] in line
        # Les lignes courtes ne portent plus le support.
        if info["full"] < info["returned"]:
            assert "Revue des discours" not in lines[-1]
            assert "Revue des discours" in lines[0]

    def test_cap_rechecked_after_the_soft_deadline_line(self, stdio):
        blocks, st = _sync()
        by_status = st["by_source_status"]
        by_status["HAL"].update({"reason": "deadline", "status": "partial"})
        by_status["CrossRef"].update({"reason": "deadline", "status": "error",
                                      "items": 0})
        result = mcp_server._note_soft_deadline((blocks, st), 50.0)
        mcp_server._fit_digest(result[0], result[1], CQ.DIGEST_MAX_BYTES)
        blocks, st = result
        assert "Échéance souple de 50 s" in blocks[0].text
        assert mcp_server._measure_result(blocks, st) <= CQ.DIGEST_MAX_BYTES
        text = _digest_block(blocks)
        lines = _entry_lines(text, len(st["top_items"]))
        for line, entry in zip(lines, st["top_items"]):
            assert line.startswith(f"{entry['rank']}. ")


# === Structured compact ===

class TestCompactStructured:
    def test_plan_and_statuses_are_compact_with_manifest_pointer(self, stdio):
        _b, st = _sync()
        plan = st["query_plan"]
        assert plan["compact"] is True
        assert plan["detail"] == {"manifest": st["manifest"], "key": "query_plan"}
        assert [s["source"] for s in plan["sources"]] == ["hal", "crossref"]
        assert all("translation" in s for s in plan["sources"])
        hal = st["by_source_status"]["HAL"]
        assert hal["status"] == "complete" and hal["items"] == 60
        assert "native_sort" not in hal and "duration_s" not in hal
        # Plan complet au manifeste.
        manifest = json.loads((stdio / st["manifest"]).read_text("utf-8"))
        assert manifest["query_plan"]["sources"][0]["capabilities_status"]

    def test_non_trivial_plan_keeps_translations(self, stdio, monkeypatch):
        blocks, st = mcp_server._run_moisson_sync(
            '"discours numérique" OR récit', 10, ["hal", "crossref"], False,
            True, cache=False, workers=2)
        by = {s["source"]: s for s in st["query_plan"]["sources"]}
        assert by["crossref"]["translation"] == "bag_of_words"
        assert by["crossref"]["query_sent"]


# === Résumé : limites du fournisseur, pertes, filtre de langue ===

def _fake_harvest(sources, items=None):
    def fake(request, ctx, **kw):
        its = items or [_art(0)]
        return HarvestResult(run_id=ctx.run_id, request=request,
                             sources=sources, items=its,
                             total_before_dedup=len(its))
    return fake


def _window(key, name, n, counters=None):
    return SourceResult(source=key, display_name=name, items=[_art(i) for i in range(n)],
                        status="partial", reason="provider_window",
                        detail=f"Isidore ne sert que 200 résultats distincts "
                               f"sur 266508 annoncés ; {n + (counters or {}).get('language_filter_removed', 0)} "
                               f"retenus pour {name}",
                        provider_window=True, counters=dict(counters or {}))


class TestSummary:
    def test_provider_window_is_not_a_partial_success(self, stdio,
                                                      monkeypatch):
        sources = [SourceResult("hal", "HAL", items=[_art(0)]),
                   _window("isidore", "Isidore", 3), _window("cairn", "Cairn", 0)]
        monkeypatch.setattr(mcp_server, "harvest", _fake_harvest(sources))
        for inline in ("digest", "full"):
            blocks, st = mcp_server._run_moisson_sync(
                "q", 5, ["hal", "isidore", "cairn"], False, True, cache=False,
                inline=inline)
            head = blocks[0].text.split("\n")[0]
            assert "succès partiel" not in blocks[0].text
            assert head.startswith("Moisson terminée, sans panne : 2 source(s) "
                                   "limitée(s) par le fournisseur")
            assert "limite du fournisseur, pas une panne" in blocks[0].text
            assert st["provider_limited_sources"] == ["Isidore", "Cairn"]
            assert st["lossy_sources"] == []
            assert st["harvest_status"] == "partial"     # contrat inchangé
        # Digest : une ligne pour la fenêtre partagée.
        blocks, st = mcp_server._run_moisson_sync(
            "q", 5, ["hal", "isidore", "cairn"], False, True, cache=False)
        assert "  - Isidore 3, Cairn 0 — limite du fournisseur" in blocks[0].text

    def test_real_loss_keeps_partial_success(self, stdio, monkeypatch):
        sources = [SourceResult("hal", "HAL", items=[_art(0)]),
                   _window("isidore", "Isidore", 3),
                   SourceResult("crossref", "CrossRef", status="error",
                                reason="http_error", detail="HTTP 503")]
        monkeypatch.setattr(mcp_server, "harvest", _fake_harvest(sources))
        blocks, st = mcp_server._run_moisson_sync(
            "q", 5, ["hal", "isidore", "crossref"], False, True, cache=False)
        head = blocks[0].text.split("\n")[0]
        assert head.startswith("Moisson terminée (succès partiel : 1 source(s)")
        assert "CrossRef : 0 — en échec (erreur HTTP : HTTP 503)" in blocks[0].text
        assert st["lossy_sources"] == ["CrossRef"]

    def test_local_language_filter_line_says_before_and_after(self, stdio,
                                                              monkeypatch):
        counters = {"language_filter_removed": 2,
                    "language_filter_unknown_kept": 26}
        sources = [_window("isidore", "Isidore", 139, counters)]
        monkeypatch.setattr(mcp_server, "harvest", _fake_harvest(sources))
        for inline in ("full", "digest"):
            blocks, _st = mcp_server._run_moisson_sync(
                "q", 5, ["isidore"], False, True, cache=False,
                languages=["fr", "en"], inline=inline)
            assert ("filtre local de langue (fr, en) : 141 → 139 notice(s) "
                    "(2 écartée(s), 26 sans langue lisible conservée(s))") \
                in blocks[0].text

    def test_soft_deadline_line_gets_a_concrete_suggestion(self):
        structured = {"by_source_status": {
            "HAL": {"status": "complete", "items": 5, "reason": None},
            "CORE": {"status": "partial", "reason": "deadline", "items": 9,
                     "counters": {}}}, "settings": {}}
        blocks = [types.TextContent(type="text", text="Moisson terminée.\nsuite")]
        out = mcp_server._note_soft_deadline((blocks, structured), 50.0, at=0.0)
        lines = out[0][0].text.split("\n")
        assert lines[1].startswith("Échéance souple de 50 s atteinte")
        assert lines[1].endswith("si le client accepte des appels plus longs.")
        assert lines[2].startswith("Valeur suggérée : deadline_seconds=100 ")
        assert mcp_server._soft_deadline_suggestion(230).startswith(
            "Échéance déjà proche du délai d'appel")
        err = mcp_server.ToolError("Moisson en échec.\nsuite",
                                   structured=dict(structured))
        text = str(mcp_server._note_soft_deadline(err, 20.0, at=0.0))
        assert text.split("\n")[2].startswith(
            "Valeur suggérée : deadline_seconds=50 ")

    def test_digest_relabels_window_reserve(self, stdio, monkeypatch):
        spec = CQ.DigestSpec(
            items=[_art(0, label="Isidore")],
            records=[{"position": 1, "id": "doi:10.7/d0",
                      "ranking": {"rank": 1, "score": 0.01,
                                  "reserves": ["source_partial"]}}],
            source_status={"Isidore": {"source": "isidore", "status": "partial",
                                       "reason": "provider_window"}})
        d = CQ.build_digest(spec, 5)
        assert d.entries[0].data == {"rank": 1, "score": 0.01, "id": "doi:10.7/d0"}
        assert "source partielle" not in d.entries[0].line
        spec.source_status = {"Isidore": {"source": "isidore", "status": "partial",
                                          "reason": "http_error"}}
        d = CQ.build_digest(spec, 5)
        assert "⚠ source partielle" in d.entries[0].line


# === HTTP : contrat inchangé ===

class TestHttp:
    def test_http_ignores_inline_no_digest_no_path(self, stdio, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                            "https://moisson.example.org")
        for inline in (None, "digest", "full"):
            kw = {} if inline is None else {"inline": inline}
            blocks, st = _sync(**kw)
            assert st["transport"] == "http"
            assert "top_items" not in st and "digest" not in st
            assert [b.type for b in blocks] == ["text"]
            blob = json.dumps(st, ensure_ascii=False) + blocks[0].text
            assert str(stdio) not in blob
            assert "Discours numérique et institutions" not in blob
            assert all(u.startswith("https://moisson.example.org/")
                       for u in st["download_urls"].values())


# === Moisson de référence du banc (OPT-00) ===

def _bench(tmp_path, entry):
    import benchmark_moisson as bm
    from bench import scenarios

    opts = argparse.Namespace(workers=None, sleep_scale=0.0, tracemalloc=False,
                              trace=False, live_keys=False)
    spec = bm.make_spec("reference", scenarios.get("reference"), entry, "sim",
                        PROJECT_ROOT, 0.0, opts, tmp_path / entry)
    result = bm.run_in_subprocess(spec, timeout=180)
    assert not result.get("bench_error"), result.get("log_tail")
    assert result["real_socket_attempts"] == []
    return result


@pytest.mark.parametrize("entry", ["mcp", "tool"])
def test_reference_harvest_return_is_at_most_15_kb(tmp_path, entry):
    result = _bench(tmp_path, entry)
    ret = result["mcp_return"]
    assert ret["is_error"] is False
    assert ret["total_bytes"] <= 15_000, ret
    assert ret["embedded_bytes"] == 0 and ret["inline_json"] is False
    # 40 notices dans le digest (les dernières éventuellement en ligne courte).
    out = tmp_path / entry / "output"
    manifest = next(out.glob("corpus_*.manifest.json"))
    assert manifest.exists()
    assert ret["blocks"]["text"] == 2
