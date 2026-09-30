# -*- coding: utf-8 -*-
"""
OPT-17 : outil ``corpus_query`` (consultation locale d'un corpus).

- recherche locale classée, même normalisation que ``utils.ranking`` (casse,
  accents, élisions, pluriels ; expressions exactes, exclusions) ;
- filtres (année, type, langue, source, DOI, résumé), refus d'un filtre
  inconnu ; projection de champs, résumé tronqué hors ``ids`` ;
- pagination stable : ni trou ni doublon, quel que soit le tri et la taille
  de page ;
- notices complètes par ``ids`` (au plus 20, DOI nu et clé secondaire
  acceptés, introuvables signalés), identiques au fichier (aucune donnée de
  classement) ; lecture arrêtée à la dernière notice utile grâce au manifeste ;
- corpus CSL, manifeste ou fichier illisible refusés (isError) ;
- plafond en octets en stdio ; HTTP : sélection publiée en fichier
  téléchargeable, métadonnées seules, aucun JSON bibliographique ni chemin.

Aucun réseau.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
from utils import corpus_query as CQ  # noqa: E402
from utils.manifest import KIND_HARVEST, Publication  # noqa: E402
from utils.output_files import ROLE_ZOTERO  # noqa: E402
from utils.ranking import record_id  # noqa: E402


def _item(i, *, title=None, year=None, itype="journalArticle", lang="fr",
          source="HAL", doi=True, abstract=None, hal=False):
    item = {
        "itemType": itype,
        "title": title or f"Notice {i}",
        "creators": [{"creatorType": "author", "firstName": "Jean",
                      "lastName": f"Nom{i}"}],
        "date": str(year or 2000 + i % 20),
        "language": lang,
        "abstractNote": abstract if abstract is not None
        else f"Résumé détaillé de la notice {i}. " * 30,
        "tags": [],
        "extra": f"Sources: {source}",
    }
    if doi:
        item["DOI"] = f"10.5/n{i}"
    if hal:
        item["url"] = f"https://hal.science/hal-0000{i}"
        item["extra"] += f"\nHAL ID: hal-0000{i}"
    return item


def _corpus_items():
    items = [_item(i) for i in range(30)]
    items[3]["title"] = "Les écritures numériques des institutions"
    items[4]["title"] = "L'écriture numérique au collège"
    items[5]["title"] = "Discours numérique et médias"
    items[6]["title"] = "Numérique : discours et pratiques"      # non contigu
    items[7].update(title="Publicité et discours numérique", itemType="book")
    items[8].update(language="en", title="Digital discourse")
    items[9].pop("DOI")
    items[10]["abstractNote"] = ""
    items[11]["extra"] = "Sources: Isidore, Cairn"
    return items


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    return tmp_path


def _write(path: Path, items) -> Path:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def _published(directory: Path, items, ranks=None) -> Path:
    """Corpus publié avec manifeste (records : identifiants et rangs)."""
    stem = "corpus_test_20260929_120000_abcd1234"
    records = []
    for i, item in enumerate(items):
        rec = {"position": i + 1, "id": record_id(item)}
        if ranks is not None:
            rec["ranking"] = {"rank": ranks[i], "score": 1.0 / ranks[i]}
        records.append(rec)
    with Publication(directory, stem, KIND_HARVEST) as pub:
        pub.add_json(directory / f"{stem}.json", items, role=ROLE_ZOTERO)
        pub.commit({"query": {"text": "discours numérique"},
                    "records": records,
                    "ranking": {"version": "opt13-2", "mode": "native"}})
    return directory / f"{stem}.json"


def _call(args):
    return asyncio.run(mcp_server.call_tool("corpus_query", args))


def _ok(args):
    res = _call(args)
    assert isinstance(res, tuple), res
    return res


def _error_text(res):
    assert isinstance(res, types.CallToolResult) and res.isError, res
    return res.content[0].text


# === Recherche locale ===

class TestSearch:
    def test_same_normalisation_as_ranking(self, out):
        path = _write(out / "corpus_q.json", _corpus_items())
        _b, st = _ok({"corpus": str(path), "query": "l'écriture numérique"})
        titles = [it["title"] for it in st["items"]]
        # Élision, accents, casse et pluriel : notices 3 et 4 en tête.
        assert set(titles[:2]) == {"Les écritures numériques des institutions",
                                   "L'écriture numérique au collège"}
        assert st["sort"] == "relevance"
        assert all("score" in it for it in st["items"])

    def test_exact_phrase_and_exclusion(self, out):
        path = _write(out / "corpus_q.json", _corpus_items())
        _b, st = _ok({"corpus": str(path),
                      "query": '"discours numérique" -publicité',
                      "fields": ["title"]})
        titles = [it["title"] for it in st["items"]]
        assert titles[0] == "Discours numérique et médias"
        assert "Numérique : discours et pratiques" not in titles[:1]
        # L'exclusion pénalise sans retirer (même règle que le classement).
        assert titles.index("Publicité et discours numérique") > 0

    def test_terms_in_text_and_structured(self, out):
        path = _write(out / "corpus_q.json", _corpus_items())
        blocks, st = _ok({"corpus": str(path), "query": "discours",
                          "limit": 3})
        text = blocks[0].text
        assert "+discours(t)" in text or "+discours(t,r)" in text
        assert st["items"][0]["terms"]


# === Filtres et projection ===

class TestFilters:
    def test_filters(self, out):
        path = _write(out / "corpus_q.json", _corpus_items())
        _b, st = _ok({"corpus": str(path), "limit": 100, "fields": ["id"],
                      "filters": {"item_types": ["book"]}})
        assert [it["id"] for it in st["items"]] == ["doi:10.5/n7"]
        _b, st = _ok({"corpus": str(path), "limit": 100,
                      "filters": {"languages": ["en"]}})
        assert [it["id"] for it in st["items"]] == ["doi:10.5/n8"]
        _b, st = _ok({"corpus": str(path), "limit": 100,
                      "filters": {"has_doi": False}})
        assert st["matched"] == 1
        _b, st = _ok({"corpus": str(path), "limit": 100,
                      "filters": {"has_abstract": False}})
        assert [it["id"] for it in st["items"]] == ["doi:10.5/n10"]
        _b, st = _ok({"corpus": str(path), "limit": 100,
                      "filters": {"sources": ["cairn"]}})
        assert [it["id"] for it in st["items"]] == ["doi:10.5/n11"]
        _b, st = _ok({"corpus": str(path), "limit": 100,
                      "filters": {"year_min": 2018, "year_max": 2019}})
        assert st["matched"] == 2          # notices 18 et 19
        assert all(2018 <= it["year"] <= 2019 for it in st["items"])

    @pytest.mark.parametrize("args,fragment", [
        ({"filters": {"annee": 2020}}, "filtre(s) inconnu(s) : annee"),
        ({"filters": {"year_min": "vingt"}}, "year_min"),
        ({"filters": {"year_min": 2020, "year_max": 2010}}, "year_min"),
        ({"fields": ["notice"]}, "champ(s) inconnu(s)"),
        ({"sort": "hasard"}, "tri 'hasard' inconnu"),
        ({"sort": "relevance"}, "sans requête"),
        ({"limit": 0}, "limit"),
        ({"offset": -1}, "offset"),
        ({"ids": [f"doi:10.5/n{i}" for i in range(21)]}, "au plus 20"),
        ({"query": 3}, "query"),
    ])
    def test_invalid_parameters_are_tool_errors(self, out, args, fragment):
        path = _write(out / "corpus_q.json", _corpus_items())
        text = _error_text(_call({"corpus": str(path), **args}))
        assert text.startswith("Paramètre refusé") and fragment in text

    def test_projection_and_truncated_abstract(self, out):
        path = _write(out / "corpus_q.json", _corpus_items())
        _b, st = _ok({"corpus": str(path), "limit": 2,
                      "fields": ["title", "abstract", "language"]})
        item = st["items"][0]
        assert set(item) <= {"id", "title", "abstract", "abstract_truncated",
                             "language"}
        assert item["abstract_truncated"] is True
        assert len(item["abstract"]) <= CQ.QUERY_ABSTRACT_CHARS


# === Pagination stable ===

class TestPagination:
    @pytest.mark.parametrize("sort,query", [
        ("rank", None), ("position", None), ("year_desc", None),
        ("year_asc", None), ("title", None), ("relevance", "notice résumé"),
    ])
    @pytest.mark.parametrize("limit", [1, 4, 7, 30])
    def test_no_gap_no_duplicate(self, out, sort, query, limit):
        items = _corpus_items()
        path = _published(out, items, ranks=[(i * 7) % 30 + 1 for i in range(30)])
        seen, off, matched = [], 0, None
        while off is not None:
            args = {"corpus": str(path), "sort": sort, "offset": off,
                    "limit": limit, "fields": ["id"]}
            if query:
                args["query"] = query
            _b, st = _ok(args)
            seen += [it["id"] for it in st["items"]]
            matched = st["matched"]
            off = st["next_offset"]
        assert len(seen) == len(set(seen)) == matched

    def test_rank_sort_uses_the_manifest(self, out):
        items = _corpus_items()
        ranks = list(range(30, 0, -1))            # dernière notice en tête
        path = _published(out, items, ranks=ranks)
        _b, st = _ok({"corpus": str(path), "limit": 3, "fields": ["id", "rank"]})
        assert [it["rank"] for it in st["items"]] == [1, 2, 3]
        assert st["items"][0]["id"] == record_id(items[-1])
        assert st["order"].startswith("classement natif")

    def test_offset_beyond_the_end(self, out):
        path = _write(out / "corpus_q.json", _corpus_items())
        _b, st = _ok({"corpus": str(path), "offset": 500})
        assert st["items"] == [] and st["next_offset"] is None
        assert st["matched"] == 30


# === Notices complètes par identifiants ===

class TestIds:
    def test_full_notices_in_requested_order(self, out):
        items = _corpus_items()
        path = _write(out / "corpus_q.json", items)
        blocks, st = _ok({"corpus": str(path),
                          "ids": ["doi:10.5/n12", "10.5/N3", "doi:10.9/absent"]})
        notices = json.loads(blocks[1].text)
        assert [n["notice"] for n in notices] == [items[12], items[3]]
        # Résumé entier (seul chemin qui le rend), notice identique au
        # fichier : aucune donnée de classement dans la notice.
        assert notices[0]["notice"]["abstractNote"] == items[12]["abstractNote"]
        assert "rank" not in notices[0]["notice"]
        assert st["missing_ids"] == ["doi:10.9/absent"]
        assert [e["requested"] for e in st["items"]] == ["doi:10.5/n12",
                                                         "doi:10.5/n3"]
        assert "notice" not in st["items"][0]      # pas de doublon

    def test_secondary_strong_key_accepted(self, out):
        items = [_item(1, hal=True), _item(2)]
        path = _write(out / "corpus_q.json", items)
        _b, st = _ok({"corpus": str(path), "ids": ["hal:hal-00001"]})
        assert st["found"] == 1 and st["items"][0]["id"] == "doi:10.5/n1"

    def test_manifest_stops_reading_after_the_last_wanted(self, out,
                                                          monkeypatch):
        items = _corpus_items()
        path = _published(out, items, ranks=list(range(1, 31)))
        seen = []
        real = CQ.iter_json_array

        def spy(p, **kw):
            for x in real(p, **kw):
                seen.append(1)
                yield x
        monkeypatch.setattr(CQ, "iter_json_array", spy)
        _b, st = _ok({"corpus": str(path), "ids": [record_id(items[2])]})
        assert st["found"] == 1 and st["items"][0]["rank"] == 3
        assert len(seen) <= 4              # positions 0 à 3, pas 30
        assert st["total_items"] == 30     # nombre repris du manifeste

    def test_byte_cap_on_ids(self, out, monkeypatch):
        items = [_item(i, abstract="x" * 4000) for i in range(10)]
        path = _write(out / "corpus_q.json", items)
        monkeypatch.setattr(CQ, "QUERY_MAX_BYTES", 12_000)
        blocks, st = _ok({"corpus": str(path),
                          "ids": [f"doi:10.5/n{i}" for i in range(10)]})
        assert mcp_server._measure_result(blocks, st) <= 12_000
        assert 1 <= st["found"] < 10
        assert st["not_returned_ids"]
        assert len(json.loads(blocks[1].text)) == st["found"]


# === Formats refusés ===

class TestFormats:
    def test_csl_by_name_or_content_refused(self, out):
        csl = _write(out / "corpus_q.csl.json", [{"id": "a", "type": "book"}])
        assert "CSL-JSON" in _error_text(_call({"corpus": str(csl)}))
        csl2 = _write(out / "corpus_r.json", [{"id": "a", "type": "book"}])
        assert "CSL-JSON" in _error_text(_call({"corpus": str(csl2)}))

    def test_manifest_and_not_an_array_refused(self, out):
        path = _published(out, _corpus_items())
        manifest = path.with_name(path.name[:-5] + ".manifest.json")
        assert "manifeste" in _error_text(_call({"corpus": str(manifest)}))
        bad = out / "corpus_bad.json"
        bad.write_text('{"items": 3}', encoding="utf-8")
        assert "tableau JSON" in _error_text(_call({"corpus": str(bad)}))
        assert "introuvable" in _error_text(
            _call({"corpus": str(out / "absent.json")})).lower()

    def test_corpus_uri_accepted_and_confined(self, out):
        path = _write(out / "corpus_q.json", _corpus_items())
        _b, st = _ok({"corpus": "corpus://corpus_q.json", "limit": 2})
        assert st["corpus"] == path.name
        # Fichier de service, traversée, fichier associé : refusés.
        for uri in ("corpus://.index.json", "corpus://../x.json"):
            assert "URI invalide" in _error_text(_call({"corpus": uri}))
        (out / "corpus_q.manifest.json").write_text("{}", encoding="utf-8")
        assert "introuvable" in _error_text(
            _call({"corpus": "corpus://corpus_q.manifest.json"})).lower()


# === Plafond stdio et HTTP ===

class TestTransport:
    def test_stdio_page_byte_cap_keeps_pagination_gapless(self, out,
                                                          monkeypatch):
        path = _write(out / "corpus_q.json",
                      [_item(i, title="T" * 300) for i in range(40)])
        monkeypatch.setattr(CQ, "QUERY_MAX_BYTES", 10_000)
        seen, off = [], 0
        while off is not None:
            blocks, st = _ok({"corpus": str(path), "limit": 40, "offset": off,
                              "fields": ["id", "title"]})
            assert mcp_server._measure_result(blocks, st) <= 10_000
            seen += [it["id"] for it in st["items"]]
            off = st["next_offset"]
        assert len(seen) == len(set(seen)) == 40

    def test_http_selection_is_a_downloadable_file(self, out, monkeypatch):
        items = _corpus_items()
        path = _published(out, items, ranks=list(range(1, 31)))
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                            "https://moisson.example.org")
        blocks, st = _ok({"corpus": path.name, "query": "discours numérique",
                          "limit": 3})
        assert st["transport"] == "http"
        assert "items" not in st
        sel = st["selection"]
        assert sel.startswith("corpus_test_20260929_120000_abcd1234_sel_")
        assert st["download_urls"][sel].startswith("https://moisson.example.org/")
        # Fichier : notices complètes de la page, dans l'ordre rendu.
        written = json.loads((out / sel).read_text(encoding="utf-8"))
        assert len(written) == st["returned"] == 3
        assert written[0]["title"] == "Discours numérique et médias"
        assert (out / st["manifest"]).is_file()
        # Aucun JSON bibliographique ni chemin dans le retour.
        blob = json.dumps(st, ensure_ascii=False) + "".join(
            b.text for b in blocks)
        assert str(out) not in blob
        assert "Discours numérique et médias" not in blob
        assert "Résumé détaillé" not in blob

    def test_http_ids_selection(self, out, monkeypatch):
        items = _corpus_items()
        path = _write(out / "corpus_q.json", items)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                            "https://moisson.example.org")
        blocks, st = _ok({"corpus": "corpus_q.json",
                          "ids": ["doi:10.5/n3", "doi:10.9/absent"]})
        written = json.loads((out / st["selection"]).read_text("utf-8"))
        assert written == [items[3]]
        assert st["missing_ids"] == ["doi:10.9/absent"]
        assert "Les écritures numériques" not in blocks[0].text

    def test_tool_listed_with_its_schema(self):
        tools = {t.name: t for t in asyncio.run(mcp_server.list_tools())}
        schema = tools["corpus_query"].inputSchema
        assert schema["required"] == ["corpus"]
        assert {"corpus", "query", "filters", "fields", "sort", "offset",
                "limit", "ids"} <= set(schema["properties"])
        assert {"moisson_search", "corpus_analyze", "corpus_list",
                "fetch_by_doi", "search_by_author", "corpus_merge",
                "corpus_to_csl", "corpus_filter",
                "corpus_resolve_dois"} <= set(tools)
