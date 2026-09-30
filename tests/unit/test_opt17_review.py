# -*- coding: utf-8 -*-
"""
OPT-17, revue : corrections des constats des deux relecteurs.

- l'appel ``corpus_query`` conseillé par l'en-tête du digest se résout en
  stdio quel que soit le répertoire courant du processus (lancé par le
  client MCP local, il n'est jamais OUTPUT_DIR) : URI ``corpus://<nom>`` pour un
  fichier de OUTPUT_DIR, chemin absolu ailleurs (corpus source de
  corpus_to_csl, sortie de corpus_filter à côté de sa source) ; un nom nu
  est aussi cherché dans OUTPUT_DIR puis dans le registre stdio ;
- HTTP : une sélection de ``corpus_query`` ne recopie plus la requête de la
  moisson source (``corpus_list(pattern=<thème>)`` ne la retient pas) et
  les sélections d'un même corpus sont bornées (purge des plus anciennes,
  jamais d'un autre fichier) ;
- deux lectures d'un fichier remplacé entre-temps : relecture, ou refus,
  jamais une page triée sur un contenu et projetée sur un autre ;
- page dont ``fields`` exclut titre ou type : aucun libellé de remplacement ;
- tableau d'autre chose que des notices : refus commun ;
- DOI nu contenant « : » (DOI SICI) reconnu dans ``ids`` ;
- description de l'exclusion conforme au comportement.

Aucun réseau.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
from pathlib import Path

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.models import SourceResult  # noqa: E402
from utils import corpus_query as CQ  # noqa: E402
from utils.manifest import KIND_HARVEST, Publication  # noqa: E402
from utils.output_files import ROLE_ZOTERO  # noqa: E402
from utils.ranking import record_id  # noqa: E402


def _call(name, args):
    return asyncio.run(mcp_server.call_tool(name, args))


def _ok(name, args):
    res = _call(name, args)
    assert isinstance(res, tuple), getattr(res, "content", res)
    return res


def _error_text(res):
    assert isinstance(res, types.CallToolResult) and res.isError, res
    return res.content[0].text


def _digest_text(blocks):
    found = [b.text for b in blocks if getattr(b, "type", None) == "text"
             and b.text.startswith(CQ.DIGEST_HEADER)]
    assert len(found) == 1
    return found[0]


_ARG_RE = re.compile(r'Notices complètes : corpus_query\(corpus=("(?:[^"\\]|\\.)*"), '
                     r'ids=\[…\]\) ; suivantes : offset=(\d+)\.')


def _advised(blocks):
    """Argument ``corpus`` et ``offset`` de l'appel conseillé par l'en-tête."""
    m = _ARG_RE.search(_digest_text(blocks))
    assert m, _digest_text(blocks)[:400]
    return json.loads(m.group(1)), int(m.group(2))


def _item(i, *, title=None, year=None):
    return {
        "itemType": "journalArticle",
        "title": title or f"Discours numérique, étude {i}",
        "creators": [{"creatorType": "author", "firstName": "Anne",
                      "lastName": f"Nom{i}"}],
        "date": str(year or 2000 + i % 20),
        "publicationTitle": "Revue",
        "abstractNote": "Résumé du discours numérique. " * 5,
        "tags": [],
        "DOI": f"10.4/p{i}",
        "extra": "Sources: HAL",
    }


def _harvest_corpus(directory: Path, n=12):
    """Corpus de moisson publié dans ``directory`` (rangs inversés)."""
    items = [_item(i) for i in range(n)]
    ranks = list(range(n, 0, -1))
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


@pytest.fixture
def out(tmp_path, monkeypatch):
    """OUTPUT_DIR et répertoire courant distincts (cas du client MCP local)."""
    output = tmp_path / "sortie"
    output.mkdir()
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", output)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    monkeypatch.chdir(elsewhere)
    return output


@pytest.fixture
def harvest_stub(monkeypatch):
    data = [_item(i) for i in range(60)]

    def stub(name, cls, query, mps, output_dir, run_id, http_client,
             *a, source_key=None, **kw):
        return SourceResult(source=source_key, display_name=name,
                            items=copy.deepcopy(data), status="complete")
    monkeypatch.setattr(orchestrator, "run_source", stub)
    monkeypatch.setattr(config, "RANKING_DEFAULT", "native")
    return data


def _query_ids(corpus, **kw):
    _b, st = _ok("corpus_query", {"corpus": corpus, "fields": ["id"], **kw})
    return [e["id"] for e in st["items"]]


# === Appel conseillé par le digest, en stdio ===

class TestAdvisedCall:
    def test_harvest_digest_call_resolves_from_another_cwd(self, out,
                                                           harvest_stub):
        blocks, st = mcp_server._run_moisson_sync(
            "discours numérique", 100, ["hal"], False, True, cache=False,
            workers=1)
        corpus, offset = _advised(blocks)
        assert corpus.startswith("corpus://")
        assert Path.cwd() != out and not (Path.cwd() / corpus[9:]).exists()
        k = st["digest"]["returned"]
        assert offset == k
        # Notices complètes par ids : l'appel conseillé réussit.
        first = st["top_items"][0]["id"]
        _b, got = _ok("corpus_query", {"corpus": corpus, "ids": [first]})
        assert got["found"] == 1 and got["missing_ids"] == []
        # Suite à offset=k : même ordre que le digest, sans trou.
        whole = _query_ids(corpus, limit=100)
        assert whole[:k] == [e["id"] for e in st["top_items"]]
        assert _query_ids(corpus, offset=offset, limit=5) == whole[k:k + 5]

    def test_to_csl_of_a_source_outside_output_dir(self, out, tmp_path):
        src_dir = tmp_path / "sources"
        src_dir.mkdir()
        path, items = _harvest_corpus(src_dir)
        blocks, st = _ok("corpus_to_csl", {"corpus_path": str(path)})
        corpus, _off = _advised(blocks)
        # Corpus source hors de OUTPUT_DIR, jamais inscrit au registre :
        # chemin absolu, seul argument qui se résout.
        assert Path(corpus).is_absolute()
        assert Path(corpus).resolve() == path.resolve()
        _b, got = _ok("corpus_query", {"corpus": corpus,
                                       "ids": [st["top_items"][0]["id"]]})
        assert got["found"] == 1
        assert got["items"][0]["id"] == record_id(items[-1])

    def test_filter_output_next_to_its_source(self, out, tmp_path):
        src_dir = tmp_path / "sources"
        src_dir.mkdir()
        path, _items = _harvest_corpus(src_dir)
        blocks, st = _ok("corpus_filter", {"corpus_path": str(path),
                                           "year_min": 2003})
        corpus, offset = _advised(blocks)
        assert Path(corpus).is_absolute()
        assert Path(corpus).parent.resolve() == src_dir.resolve()
        ids = _query_ids(corpus, limit=offset)
        assert ids == [e["id"] for e in st["top_items"]]

    def test_output_dir_file_uses_the_uri(self, out):
        path, _items = _harvest_corpus(out)
        blocks, _st = _ok("corpus_to_csl", {"corpus_path": str(path)})
        corpus, _off = _advised(blocks)
        assert corpus == f"corpus://{path.name}"

    def test_bare_name_found_in_output_dir_then_registry(self, out, tmp_path):
        path, items = _harvest_corpus(out)
        assert len(_query_ids(path.name, limit=3)) == 3
        # Fichier hors de OUTPUT_DIR inscrit au registre (sortie d'un
        # producteur) : trouvé par son nom nu.
        src_dir = tmp_path / "sources"
        src_dir.mkdir()
        other = src_dir / "corpus_ailleurs.json"
        other.write_text(json.dumps(items[:2]), encoding="utf-8")
        mcp_server._EMITTED_RESOURCES[other.name] = other.resolve()
        assert len(_query_ids(other.name)) == 2
        # Nom inconnu : refus clair ; fichier de service : jamais cherché.
        assert "introuvable" in _error_text(
            _call("corpus_query", {"corpus": "corpus_absent.json"})).lower()
        assert "introuvable" in _error_text(
            _call("corpus_query", {"corpus": ".index.json"})).lower()

    def test_http_bare_name_unchanged(self, out, monkeypatch):
        path, _items = _harvest_corpus(out)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                            "https://moisson.example.org")
        _b, st = _ok("corpus_query", {"corpus": path.name, "limit": 2})
        assert st["transport"] == "http" and st["returned"] == 2

    def test_header_quotes_the_argument_as_json(self):
        lines = CQ.digest_header("ordre", 3, 9, 'C:\\corpus\\a"b.json')
        assert lines[-1] == ('Notices complètes : corpus_query(corpus='
                             '"C:\\\\corpus\\\\a\\"b.json", ids=[…]) ; '
                             'suivantes : offset=3.')
        # Le recalcul de l'en-tête (digest coupé) garde l'argument.
        again = CQ.retarget_header(lines, 2)
        assert again[-1].endswith("suivantes : offset=2.")
        assert '"C:\\\\corpus\\\\a\\"b.json"' in again[-1]


# === HTTP : sélections ===

@pytest.fixture
def http_out(out, monkeypatch):
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
    monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                        "https://moisson.example.org")
    return out


def _selections(directory: Path, stem: str):
    return sorted(p.name for p in directory.glob(f"{stem}_sel_*.json")
                  if not p.name.endswith(".manifest.json"))


class TestHttpSelections:
    def test_selection_does_not_inherit_the_source_query(self, http_out):
        path, _items = _harvest_corpus(http_out)
        _b, st = _ok("corpus_query", {"corpus": path.name, "limit": 3})
        doc = json.loads((http_out / st["manifest"]).read_text("utf-8"))
        assert doc.get("query") in (None, {}, "")
        sel = doc["filters"]["corpus_query"]
        assert sel["source_query"] == "discours numérique"
        assert sel["limit"] == 3

    def test_harvest_stays_listed_by_theme(self, http_out):
        path, _items = _harvest_corpus(http_out, n=30)
        for off in range(0, 30, 3):
            _ok("corpus_query", {"corpus": path.name, "offset": off,
                                 "limit": 3})
        listing = _call("corpus_list", {"pattern": "discours numérique"})
        text = listing if isinstance(listing, str) else listing[0].text
        stems = [line.strip() for line in text.split("\n")
                 if line.startswith("  corpus_")]
        assert stems == [path.stem]

    def test_selections_are_bounded_per_source(self, http_out, monkeypatch):
        monkeypatch.setattr(CQ, "QUERY_HTTP_KEEP_SELECTIONS", 3)
        path, items = _harvest_corpus(http_out, n=30)
        stem = path.stem
        # Fichiers qui ressemblent à une sélection sans en être une : jamais
        # supprimés (manifeste de moisson, autre corpus source).
        decoy = f"{stem}_sel_deadbeef"
        with Publication(http_out, decoy, KIND_HARVEST) as pub:
            pub.add_json(http_out / f"{decoy}.json", items[:1],
                         role=ROLE_ZOTERO)
            pub.commit({"query": {"text": "autre"}})
        other_src = http_out / "corpus_autre_20260101_000000_00000000.json"
        other_src.write_text(json.dumps(items[:5]), encoding="utf-8")
        _ok("corpus_query", {"corpus": other_src.name, "limit": 1})
        last = None
        for off in range(0, 24, 3):
            _b, last = _ok("corpus_query", {"corpus": path.name,
                                            "offset": off, "limit": 3})
        kept = [n for n in _selections(http_out, stem) if n != f"{decoy}.json"]
        assert len(kept) == 3
        assert last["selection"] in kept
        assert last["selections_kept"] == 3
        for name in kept:
            assert (http_out / name.replace(".json", ".manifest.json")).is_file()
        # Aucun manifeste orphelin des sélections supprimées.
        manifests = [p.name for p in http_out.glob(f"{stem}_sel_*.manifest.json")]
        assert len(manifests) == 4          # 3 sélections + le leurre
        assert (http_out / f"{decoy}.json").is_file()
        assert len(_selections(http_out, other_src.stem)) == 1
        # L'index ne liste plus les sélections supprimées.
        listing = _call("corpus_list", {"limit": 50})
        text = listing if isinstance(listing, str) else listing[0].text
        listed = [line.strip() for line in text.split("\n")
                  if line.startswith("  corpus_") and "_sel_" in line
                  and line.strip() != decoy and other_src.stem not in line]
        assert sorted(f"{s}.json" for s in listed) == kept

    def test_page_notices_come_from_the_projection_pass(self, http_out,
                                                        monkeypatch):
        path, items = _harvest_corpus(http_out)

        def no_third_read(*a, **kw):
            raise AssertionError("troisième lecture du corpus")
        monkeypatch.setattr(CQ, "read_positions", no_third_read)
        _b, st = _ok("corpus_query", {"corpus": path.name, "limit": 4})
        written = json.loads((http_out / st["selection"]).read_text("utf-8"))
        # Rang du manifeste : dernière notice en tête.
        assert written == list(reversed(items))[:4]


# === Lecture en deux passes d'un fichier remplacé ===

def _write(path: Path, items) -> Path:
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


class TestTwoPassConsistency:
    def test_replacement_during_pass_one_is_reread(self, tmp_path):
        path = _write(tmp_path / "corpus_r.json",
                      [_item(i, title=f"Discours {i}") for i in range(6)])
        new = [{**_item(i, title=f"Botanique {i}"),
                "abstractNote": "Plantes et jardins."} for i in range(5)]
        calls = {"n": 0}

        def replace_once():
            calls["n"] += 1
            if calls["n"] == 1:
                tmp = tmp_path / "tmp.json"
                _write(tmp, new)
                os.replace(tmp, path)
        result = CQ.query_corpus(path, query="discours", check=replace_once)
        assert result.snapshot == CQ._snapshot(path)
        assert result.matched == 0 and result.items == []
        result = CQ.query_corpus(path, query="botanique")
        assert [it["title"] for it in result.items] == \
            [f"Botanique {i}" for i in range(5)]

    def test_file_changing_at_every_attempt_is_refused(self, tmp_path,
                                                       monkeypatch):
        path = _write(tmp_path / "corpus_r.json", [_item(i) for i in range(3)])
        counter = iter(range(10 ** 6))
        monkeypatch.setattr(CQ, "_stat_key", lambda p: next(counter))
        with pytest.raises(CQ.CorpusChangedError):
            CQ.query_corpus(path)
        with pytest.raises(CQ.CorpusChangedError):
            CQ.query_corpus(path, ids=["doi:10.4/p1"])

    def test_tool_reports_a_changing_corpus(self, out, monkeypatch):
        path = _write(out / "corpus_r.json", [_item(i) for i in range(3)])
        counter = iter(range(10 ** 6))
        monkeypatch.setattr(CQ, "_stat_key", lambda p: next(counter))
        text = _error_text(_call("corpus_query", {"corpus": str(path)}))
        assert "modifié pendant la lecture" in text
        assert str(out) not in text


# === Texte d'une page sans titre ni type projetés ===

class TestProjectedText:
    def test_no_placeholder_for_unprojected_title_and_type(self, out):
        path = _write(out / "corpus_f.json",
                      [{**_item(i), "itemType": "book"} for i in range(2)])
        blocks, _st = _ok("corpus_query", {"corpus": str(path),
                                           "fields": ["id", "year"]})
        text = blocks[0].text
        assert "(sans titre)" not in text and "type inconnu" not in text
        assert "1. (2000) — doi:10.4/p0" in text

    def test_projected_but_empty_title_is_still_said(self, out):
        empty = {**_item(0), "title": ""}
        empty.pop("itemType")
        path = _write(out / "corpus_f.json", [empty])
        blocks, _st = _ok("corpus_query", {"corpus": str(path),
                                           "fields": ["id", "title", "type"]})
        assert "(sans titre)" in blocks[0].text
        assert "type inconnu" in blocks[0].text


# === Tableau qui ne contient pas de notices ===

class TestNotNotices:
    @pytest.mark.parametrize("content", [[1, 2, 3], ["a", "b"], [None],
                                         "MIXED"])
    def test_refused_like_the_other_tools(self, out, content):
        if content == "MIXED":
            content = [_item(0), "x", 3]
        path = _write(out / "corpus_n.json", content)
        assert _error_text(_call("corpus_query", {"corpus": str(path)})) == \
            CQ.NOT_NOTICES_MESSAGE
        assert _error_text(_call("corpus_query", {
            "corpus": str(path), "ids": ["doi:10.4/p9"]})) == \
            CQ.NOT_NOTICES_MESSAGE

    def test_empty_array_is_an_empty_corpus(self, out):
        path = _write(out / "corpus_e.json", [])
        _b, st = _ok("corpus_query", {"corpus": str(path)})
        assert st["total_items"] == 0 and st["matched"] == 0


# === Identifiants : DOI nu contenant « : » ===

SICI = "10.1002/(SICI)1097-4571(199806)49:8<693::AID-ASI1>3.0.CO;2-O"


class TestBareDoi:
    def test_normalize_id(self):
        assert CQ.normalize_id(SICI) == f"doi:{SICI.lower()}"
        assert CQ.normalize_id(f"doi:{SICI}") == f"doi:{SICI.lower()}"
        assert CQ.normalize_id("https://doi.org/10.1/AB:C") == "doi:10.1/ab:c"
        assert CQ.normalize_id("10.1/X") == "doi:10.1/x"
        assert CQ.normalize_id("hal:hal-0001") == "hal:hal-0001"
        assert CQ.normalize_id("url:https://exemple.org/a") == \
            "url:https://exemple.org/a"

    def test_sici_doi_found_by_ids(self, out):
        items = [{**_item(i), "DOI": SICI.replace("ASI1", f"ASI{i}")}
                 for i in range(3)]
        path = _write(out / "corpus_s.json", items)
        _b, st = _ok("corpus_query", {"corpus": str(path), "ids": [SICI]})
        assert st["found"] == 1 and st["missing_ids"] == []
        assert st["items"][0]["position"] == 2


# === Description de l'exclusion ===

class TestExclusionDescription:
    def test_description_says_exclusion_only_lowers_the_rank(self):
        tools = {t.name: t for t in asyncio.run(mcp_server.list_tools())}
        tool = tools["corpus_query"]
        assert "exclusion par '-' ou NOT," not in tool.description
        assert "ne retire pas la notice" in tool.description
        assert "sans retirer la notice" in \
            tool.inputSchema["properties"]["query"]["description"]
        assert {"moisson_search", "corpus_analyze", "corpus_list",
                "fetch_by_doi", "search_by_author", "corpus_merge",
                "corpus_to_csl", "corpus_filter", "corpus_resolve_dois",
                "corpus_query"} <= set(tools)
