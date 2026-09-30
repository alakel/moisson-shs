# -*- coding: utf-8 -*-
"""
OPT-17 : digest des autres producteurs (corpus_filter, corpus_to_csl,
corpus_merge, corpus_resolve_dois, search_by_author, corpus_export).

- ordre du digest : classement hérité du manifeste source (filtre,
  conversion, résolution DOI, export), ordre du corpus sinon (fusion,
  recherche par auteur, année décroissante) ;
- ``corpus_query`` désigné sur le fichier Zotero interrogeable ;
- ``search_by_author`` : retour <= 15 Ko en digest (il dépassait 340 Ko en
  JSON inline) ;
- ``corpus_export`` : clés de citation dans le digest, aucun texte exporté
  inliné hors ``full`` ;
- valeur ``inline`` refusée : erreur d'outil, rien écrit ;
- HTTP inchangé.

Aucun réseau.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
import utils.doi_resolver as dr  # noqa: E402
import utils.lookups as lookups  # noqa: E402
from utils import corpus_query as CQ  # noqa: E402
from utils.manifest import KIND_HARVEST, Publication  # noqa: E402
from utils.output_files import ROLE_ZOTERO  # noqa: E402
from utils.ranking import record_id  # noqa: E402


def _item(i, *, source="HAL", abstract="Résumé. " * 20, doi=True, year=None):
    item = {
        "itemType": "journalArticle",
        "title": f"Discours numérique, étude {i}",
        "creators": [{"creatorType": "author", "firstName": "Anne",
                      "lastName": f"Nom{i}"}],
        "date": str(year or 2000 + i % 20),
        "publicationTitle": "Revue",
        "abstractNote": abstract,
        "tags": [],
        "extra": f"Sources: {source}",
    }
    if doi:
        item["DOI"] = f"10.4/p{i}"
    return item


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    return tmp_path


def _harvest_corpus(directory: Path, n=12, ranks=None):
    """Corpus de moisson publié : manifeste avec rangs (dernière notice en
    tête par défaut)."""
    items = [_item(i) for i in range(n)]
    ranks = ranks or list(range(n, 0, -1))
    stem = "corpus_discours_numerique_20260929_120000_abcd1234"
    with Publication(directory, stem, KIND_HARVEST) as pub:
        pub.add_json(directory / f"{stem}.json", items, role=ROLE_ZOTERO)
        pub.commit({
            "query": {"text": "discours numérique", "mode": "bibliographie"},
            "sources": {"HAL": {"source": "hal", "status": "complete"}},
            "records": [{"position": i + 1, "id": record_id(it),
                         "native": [{"source": "hal", "rank": ranks[i]}],
                         "ranking": {"rank": ranks[i], "score": 1 / ranks[i]}}
                        for i, it in enumerate(items)],
        })
    return directory / f"{stem}.json", items


def _call(name, args):
    return asyncio.run(mcp_server.call_tool(name, args))


def _digest(blocks):
    texts = [b.text for b in blocks if getattr(b, "type", None) == "text"
             and b.text.startswith(CQ.DIGEST_HEADER)]
    assert len(texts) == 1
    return texts[0]


class TestDerivedCorpora:
    def test_filter_inherits_the_source_ranking(self, out):
        path, items = _harvest_corpus(out)
        blocks, st = _call("corpus_filter", {"corpus_path": str(path),
                                             "year_min": 2003})
        assert st["inline_mode"] == "digest"
        kept = [it for it in items if int(it["date"]) >= 2003]
        # Ordre : rang hérité (dernière notice d'abord), rangs renumérotés.
        assert [e["id"] for e in st["top_items"]] == \
            [record_id(it) for it in reversed(kept)]
        assert [e["rank"] for e in st["top_items"]] == list(range(1, len(kept) + 1))
        text = _digest(blocks)
        assert "classement hérité du corpus source" in text
        assert f'corpus_query(corpus="corpus://{path.stem}_filtered.json"' in text
        assert "+discours(t)" in text     # requête reprise du manifeste

    def test_to_csl_digest_describes_the_zotero_source(self, out):
        path, items = _harvest_corpus(out)
        blocks, st = _call("corpus_to_csl", {"corpus_path": str(path)})
        assert st["format"] == "csl-json"
        assert not [b for b in blocks if getattr(b, "type", None) == "resource"]
        assert st["top_items"][0]["id"] == record_id(items[-1])
        assert f'corpus_query(corpus="corpus://{path.name}"' in _digest(blocks)

    def test_merge_uses_the_merge_order(self, out):
        a = out / "corpus_a.json"
        b = out / "corpus_b.json"
        a.write_text(json.dumps([_item(i) for i in range(3)]), encoding="utf-8")
        b.write_text(json.dumps([_item(i) for i in range(2, 6)]), encoding="utf-8")
        blocks, st = _call("corpus_merge", {"corpus_paths": [str(a), str(b)]})
        assert st["merged_count"] == 6
        assert [e["rank"] for e in st["top_items"]] == list(range(1, 7))
        assert "score" not in st["top_items"][0]
        assert "ordre du corpus (ordre de la fusion)" in _digest(blocks)

    def test_resolve_dois_inherits_by_position(self, out):
        items = [_item(i, doi=False) for i in range(4)]
        stem = "corpus_scholar_20260929_120000_abcd1234"
        with Publication(out, stem, KIND_HARVEST) as pub:
            pub.add_json(out / f"{stem}.json", items, role=ROLE_ZOTERO)
            pub.commit({"records": [
                {"position": i + 1, "id": record_id(it),
                 "ranking": {"rank": 4 - i, "score": 0.1 * (4 - i)}}
                for i, it in enumerate(items)]})
        with patch.object(dr, "get_json",
                          side_effect=lambda *a, **k: {"message": {"items": []}}), \
                patch("utils.http.time.sleep", lambda s: None):
            blocks, st = asyncio.run(mcp_server._tool_corpus_resolve_dois(
                {"corpus_path": str(out / f"{stem}.json")}))
        assert st["inline_mode"] == "digest"
        assert [e["rank"] for e in st["top_items"]] == [1, 2, 3, 4]
        assert st["top_items"][0]["id"] == record_id(items[3])
        assert f"{stem}_doi.json" in _digest(blocks)


def _install_author_sources(monkeypatch, n_hal=150, n_oa=160):
    def item(i, src):
        return {"itemType": "journalArticle",
                "title": f"Travail {i} sur les discours numériques et les "
                         f"médias sociaux : une approche {i}",
                "date": str(1990 + i % 35),
                "creators": [{"creatorType": "author", "firstName": "Anne",
                              "lastName": "Dupont"},
                             {"creatorType": "author", "firstName": "Paul",
                              "lastName": f"Co{i}"}],
                "DOI": f"10.1/{i}", "publicationTitle": "Revue de test",
                "abstractNote": "Résumé " * 180, "extra": f"Sources: {src}",
                "tags": [{"tag": "discours"}]}
    hal = [item(i, "HAL") for i in range(n_hal)]
    oa = [item(i, "OpenAlex") for i in range(100, 100 + n_oa)]

    class _Conn:
        def __init__(self, http_client=None, **kw):
            pass

        def search(self, query, max_results=1000, **kw):
            return copy.deepcopy(hal)

    class _Empty(_Conn):
        def search(self, query, max_results=1000, **kw):
            return []
    monkeypatch.setattr(lookups, "HALConnector", _Conn)
    monkeypatch.setattr(lookups, "IsidoreConnector", _Empty)
    monkeypatch.setattr(lookups, "_search_openalex_author",
                        lambda *a, **k: copy.deepcopy(oa))
    monkeypatch.setattr(lookups, "_search_s2_author", lambda *a, **k: [])


class TestSearchByAuthor:
    def test_digest_under_15_kb_where_full_inlined_hundreds(self, out,
                                                           monkeypatch):
        _install_author_sources(monkeypatch)
        args = {"name": "Anne Dupont", "openalex_author_id": "A1"}
        full = _call("search_by_author", {**args, "inline": "full"})
        digest = _call("search_by_author", args)
        full_bytes = mcp_server._measure_result(*full)
        digest_bytes = mcp_server._measure_result(*digest)
        assert full_bytes > 300_000                  # JSON inline historique
        assert digest_bytes <= 15_000
        blocks, st = digest
        assert st["digest"]["returned"] == 40
        text = _digest(blocks)
        assert "année décroissante" in text
        import re
        years = [int(y) for y in re.findall(r"^\d+\. .*? \((\d{4})\)",
                                             text, flags=re.M)]
        assert len(years) == 40 and years == sorted(years, reverse=True)
        # Les dix plus récentes ne sont pas répétées dans le résumé.
        assert "10 publications les plus récentes" not in blocks[0].text

    def test_none_keeps_the_ten_most_recent(self, out, monkeypatch):
        _install_author_sources(monkeypatch, n_hal=20, n_oa=0)
        blocks, st = _call("search_by_author",
                           {"name": "Anne Dupont", "inline": "none"})
        assert "10 publications les plus récentes" in blocks[0].text
        assert "top_items" not in st


class TestExport:
    def test_digest_lists_citation_keys_without_inlining_the_export(self, out):
        path, items = _harvest_corpus(out, n=5)
        blocks, st = _call("corpus_export", {"corpus_path": str(path)})
        assert not [b for b in blocks if isinstance(b, types.EmbeddedResource)]
        assert st["inline_export"] is False
        assert st["inline_skipped_reason"] == "inline='digest'"
        keys = [e["key"] for e in st["top_items"]]
        assert all(keys) and len(keys) == 5
        assert f"clé {keys[0]}" in _digest(blocks)
        link, = [b for b in blocks if isinstance(b, types.ResourceLink)]
        assert link.mimeType == "application/x-bibtex"

    def test_ris_digest_has_no_keys(self, out):
        path, _items = _harvest_corpus(out, n=3)
        _blocks, st = _call("corpus_export", {"corpus_path": str(path),
                                              "format": "ris"})
        assert all("key" not in e for e in st["top_items"])


class TestRefusalsAndHttp:
    @pytest.mark.parametrize("name,extra", [
        ("corpus_filter", {}), ("corpus_to_csl", {}), ("corpus_export", {}),
    ])
    def test_invalid_inline_refused_nothing_written(self, out, name, extra):
        path, _ = _harvest_corpus(out)
        before = sorted(p.name for p in out.iterdir())
        res = _call(name, {"corpus_path": str(path), "inline": "complet",
                           **extra})
        assert isinstance(res, types.CallToolResult) and res.isError
        assert "Paramètre refusé : inline" in res.content[0].text
        assert sorted(p.name for p in out.iterdir()) == before

    def test_merge_invalid_top_refused(self, out):
        a = out / "corpus_a.json"
        a.write_text(json.dumps([_item(0)]), encoding="utf-8")
        res = _call("corpus_merge", {"corpus_paths": [str(a)], "top": -2})
        assert isinstance(res, types.CallToolResult) and res.isError

    def test_http_filter_unchanged(self, out, monkeypatch):
        path, _ = _harvest_corpus(out)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                            "https://moisson.example.org")
        blocks, st = _call("corpus_filter", {"corpus_path": path.name})
        assert [b.type for b in blocks] == ["text"]
        assert "top_items" not in st and "files" not in st
        assert "Discours numérique, étude" not in json.dumps(st) + blocks[0].text
