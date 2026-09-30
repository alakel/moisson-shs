# -*- coding: utf-8 -*-
"""
OPT-17 : ressources ``corpus://<nom>?offset=&limit=`` paginées.

- ``parse_corpus_uri`` : seuls ``offset`` et ``limit`` entiers bornés,
  chacun une fois ; nom validé comme avant (traversée, service refusés) ;
- page : notices, total, page suivante (``_next_offset``, ``_next_uri``),
  50 par défaut, borne haute, plafond d'octets sans trou ; total repris du
  manifeste ; parcours complet sans trou ni doublon ;
- mêmes refus qu'une lecture entière (fichier associé, service, HTTP hors
  liste blanche) ; pagination réservée aux corpus JSON ;
- SDK réel (transport en mémoire) : forme non dépréciée (aucun
  ``DeprecationWarning``), type MIME, modèle d'URI listé ; l'échantillon
  tronqué indique la page suivante.

Aucun réseau.
"""

from __future__ import annotations

import asyncio
import json
import sys
import warnings
from pathlib import Path

import anyio
import pytest
from pydantic import AnyUrl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
from utils import corpus_query as CQ  # noqa: E402
from utils.manifest import KIND_HARVEST, Publication  # noqa: E402
from utils.output_files import (  # noqa: E402
    ROLE_ZOTERO, corpus_name_from_uri, corpus_page_uri, parse_corpus_uri,
)
from utils.ranking import record_id  # noqa: E402


def _items(n, size=50):
    return [{"itemType": "journalArticle", "title": f"T{i}",
             "date": "2020", "abstractNote": "x" * size,
             "extra": "Sources: HAL"} for i in range(n)]


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    return tmp_path


def _write(path, items):
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def _read(uri):
    return json.loads(asyncio.run(mcp_server.read_resource(uri)))


# === URI ===

class TestUri:
    @pytest.mark.parametrize("uri,expected", [
        ("corpus://a.json", ("a.json", None)),
        ("corpus://a.json?offset=0&limit=50", ("a.json", {"offset": 0, "limit": 50})),
        ("corpus://a.json?limit=5", ("a.json", {"offset": None, "limit": 5})),
        ("corpus://a.json?limit=5&offset=10", ("a.json", {"offset": 10, "limit": 5})),
        ("corpus://mon%20corpus.json?offset=3", ("mon corpus.json",
                                                 {"offset": 3, "limit": None})),
    ])
    def test_accepted(self, uri, expected):
        assert parse_corpus_uri(uri) == expected
        assert parse_corpus_uri(AnyUrl(uri)) == expected

    @pytest.mark.parametrize("uri", [
        "corpus://a.json?", "corpus://a.json?offset=-1", "corpus://a.json?offset=%31",
        "corpus://a.json?offset=1&offset=2", "corpus://a.json?limit=0",
        "corpus://a.json?limit=10001", "corpus://a.json?offset=1.5",
        "corpus://a.json?x=1", "corpus://a.json?offset", "corpus://a.json#x",
        "corpus://a.json?offset=1#x", "corpus://../x.json?offset=0",
        "corpus://%2e%2e?limit=1", "corpus://.index.json?limit=1",
        "corpus://a.json?offset=1&", "corpus://a.json?offset=+1",
        "corpus://a.json?offset=999999999",
    ])
    def test_refused(self, uri):
        with pytest.raises(ValueError):
            parse_corpus_uri(uri)

    def test_flat_name_function_still_refuses_parameters(self):
        with pytest.raises(ValueError):
            corpus_name_from_uri("corpus://a.json?offset=0")

    def test_page_uri_round_trip(self):
        uri = corpus_page_uri("mon corpus.json", 10, 20)
        assert uri == "corpus://mon%20corpus.json?offset=10&limit=20"
        assert parse_corpus_uri(uri) == ("mon corpus.json",
                                         {"offset": 10, "limit": 20})


# === Pages ===

class TestPages:
    def test_page_fields_and_next(self, out):
        _write(out / "corpus_a.json", _items(120))
        doc = _read("corpus://corpus_a.json?offset=0")
        assert doc["_page"] is True and doc["_limit"] == CQ.RESOURCE_PAGE_DEFAULT
        assert doc["_returned"] == 50 and doc["_total_items"] == 120
        assert doc["_next_offset"] == 50
        assert doc["_next_uri"] == "corpus://corpus_a.json?offset=50&limit=50"
        assert [it["title"] for it in doc["items"]][:2] == ["T0", "T1"]
        last = _read("corpus://corpus_a.json?offset=100&limit=50")
        assert last["_returned"] == 20 and last["_next_offset"] is None
        assert last["_next_uri"] is None

    def test_limit_bounded(self, out):
        _write(out / "corpus_a.json", _items(500))
        doc = _read("corpus://corpus_a.json?limit=5000")
        assert doc["_limit"] == CQ.RESOURCE_PAGE_MAX
        assert doc["_returned"] == CQ.RESOURCE_PAGE_MAX

    @pytest.mark.parametrize("limit", [1, 7, 50, 200])
    def test_full_walk_without_gap_or_duplicate(self, out, limit):
        items = _items(123)
        _write(out / "corpus_a.json", items)
        uri, seen = f"corpus://corpus_a.json?offset=0&limit={limit}", []
        while uri:
            doc = _read(uri)
            seen += [it["title"] for it in doc["items"]]
            uri = doc["_next_uri"]
        assert seen == [it["title"] for it in items]

    def test_byte_cap_shortens_the_page_without_gap(self, out, monkeypatch):
        items = _items(30, size=2000)
        _write(out / "corpus_a.json", items)
        monkeypatch.setattr(mcp_server, "RESOURCE_MAX_BYTES", 9_000)
        doc = _read("corpus://corpus_a.json?offset=0&limit=30")
        assert doc["_byte_cap_reached"] is True
        assert 1 <= doc["_returned"] < 30
        assert doc["_next_offset"] == doc["_returned"]
        seen, uri = [], "corpus://corpus_a.json?offset=0&limit=30"
        while uri:
            page = _read(uri)
            seen += [it["title"] for it in page["items"]]
            uri = page["_next_uri"]
        assert seen == [it["title"] for it in items]

    def test_offset_beyond_the_end(self, out):
        _write(out / "corpus_a.json", _items(3))
        doc = _read("corpus://corpus_a.json?offset=10")
        assert doc["_returned"] == 0 and doc["_next_offset"] is None
        assert doc["_total_items"] == 3 and "_message" in doc

    def test_total_from_manifest_stops_reading(self, out, monkeypatch):
        items = _items(200)
        stem = "corpus_m_20260929_120000_abcd1234"
        with Publication(out, stem, KIND_HARVEST) as pub:
            pub.add_json(out / f"{stem}.json", items, role=ROLE_ZOTERO)
            pub.commit({"records": [{"position": i + 1, "id": record_id(it)}
                                    for i, it in enumerate(items)]})
        read = []
        real = CQ.iter_json_array

        def spy(p, **kw):
            for x in real(p, **kw):
                read.append(1)
                yield x
        monkeypatch.setattr(CQ, "iter_json_array", spy)
        doc = _read(f"corpus://{stem}.json?offset=0&limit=10")
        assert doc["_total_items"] == 200 and doc["_next_offset"] == 10
        assert len(read) <= 11

    def test_stdio_registry_file_outside_output_dir(self, out, tmp_path):
        elsewhere = tmp_path / "ailleurs"
        elsewhere.mkdir()
        src = _write(elsewhere / "corpus_x.json", _items(8))
        res = asyncio.run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": str(src), "inline": "none"}))
        name = "corpus_x_filtered.json"
        assert name in res[1]["files"]
        doc = _read(f"corpus://{name}?offset=6&limit=5")
        assert doc["_returned"] == 2 and doc["_total_items"] == 8


# === Refus ===

class TestRefusals:
    def test_associated_service_and_stats(self, out):
        _write(out / "corpus_a.json", _items(3))
        (out / "corpus_a.manifest.json").write_text("{}", encoding="utf-8")
        (out / "corpus_a_stats.txt").write_text("stats", encoding="utf-8")
        assert "error" in _read("corpus://corpus_a.manifest.json?offset=0")
        assert "error" in _read("corpus://.index.json?offset=0")
        doc = _read("corpus://corpus_a_stats.txt?offset=0")
        assert "Pagination réservée" in doc["error"]
        assert "URI invalide" in _read("corpus://corpus_a.json?page=2")["error"]

    def test_http_confinement_kept(self, out, monkeypatch, tmp_path):
        _write(out / "corpus_a.json", _items(3))
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        assert _read("corpus://corpus_a.json?limit=2")["_returned"] == 2
        outside = tmp_path.parent / "dehors.json"
        _write(outside, _items(2))
        (out / "corpus_lien.json").symlink_to(outside)
        doc = _read("corpus://corpus_lien.json?limit=2")
        assert "error" in doc and str(tmp_path) not in json.dumps(doc)

    def test_truncated_sample_points_to_the_next_page(self, out, monkeypatch):
        _write(out / "corpus_big.json", _items(80, size=40_000))
        doc = _read("corpus://corpus_big.json")
        assert doc["_truncated"] is True
        assert doc["_next_uri"] == (f"corpus://corpus_big.json?offset="
                                    f"{doc['_returned']}&limit="
                                    f"{CQ.RESOURCE_PAGE_DEFAULT}")


# === SDK réel ===

async def _session(uris):
    from mcp.shared.memory import create_connected_server_and_client_session
    out = {}
    async with create_connected_server_and_client_session(
            mcp_server.server) as client:
        for uri in uris:
            res = await client.read_resource(AnyUrl(uri))
            out[uri] = res.contents[0]
        templates = await client.list_resource_templates()
    return out, templates


class TestThroughSdk:
    def test_non_deprecated_form_mime_types_and_templates(self, out):
        _write(out / "corpus_a.json", _items(60))
        (out / "corpus_a_stats.txt").write_text("stats", encoding="utf-8")
        uris = ["corpus://corpus_a.json?offset=0&limit=5",
                "corpus://corpus_a.json", "corpus://corpus_a_stats.txt",
                "corpus://absent.json?offset=0"]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            contents, templates = anyio.run(_session, uris)
        assert not [w for w in caught
                    if issubclass(w.category, DeprecationWarning)
                    and "read_resource" in str(w.message)]
        page = contents[uris[0]]
        assert page.mimeType == "application/json"
        assert json.loads(page.text)["_next_offset"] == 5
        assert contents[uris[1]].mimeType == "application/json"
        assert contents[uris[2]].mimeType == "text/plain"
        assert contents[uris[2]].text == "stats"
        assert "error" in json.loads(contents[uris[3]].text)
        tpl = templates.resourceTemplates
        assert [t.uriTemplate for t in tpl] == [
            "corpus://{name}?offset={offset}&limit={limit}"]

    def test_list_resources_mentions_pages(self, out):
        _write(out / "corpus_a.json", _items(4))
        resources = asyncio.run(mcp_server.list_resources())
        assert all("?offset=0&limit=" in r.description for r in resources)
