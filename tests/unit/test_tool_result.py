"""
Tests du Sprint 05 — Retour MCP enrichi (lien + JSON inline).

Couvre :
- #28 : helper ``_build_corpus_tool_result`` (petit→inline / gros→lien seul)
        et non-régression du dispatcher ``call_tool`` (str / list / tuple).
- #29/#30 : retour enrichi des outils producteurs (corpus_filter,
            corpus_to_csl, corpus_merge) bout-en-bout, sans réseau.
- #31 : garde de volumétrie ``INLINE_MAX_ITEMS`` + override d'environnement
        ``MOISSON_INLINE_MAX_ITEMS``.
- round-trip ``corpus://`` (lien produit → relu par ``read_resource``).
- sérialisation JSON de tous les blocs (CallToolResult).

Aucun test ici ne touche le réseau : les outils réseau (moisson_search,
search_by_author, fetch_by_doi) sont couverts indirectement via le helper
et le dispatcher.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402


# === Helpers de test ===

def _run(coro):
    """Exécute une coroutine dans une boucle dédiée."""
    return asyncio.run(coro)


def _make_items(n: int) -> list:
    """Fabrique n items Zotero minimaux distincts."""
    return [
        {
            "itemType": "journalArticle",
            "title": f"Article {i}",
            "creators": [
                {"creatorType": "author", "firstName": "A", "lastName": f"Auteur{i}"}
            ],
            "date": str(2000 + (i % 25)),
            "language": "fr",
            "DOI": f"10.0000/test.{i}",
            "extra": "Sources: Test",
        }
        for i in range(n)
    ]


def _blocks_by_type(blocks, type_name):
    return [b for b in blocks if getattr(b, "type", None) == type_name]


def _write_corpus(path: Path, items: list) -> Path:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# === #28 — Helper _build_corpus_tool_result ===

class TestBuildCorpusToolResult:
    def test_small_corpus_is_inlined(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 300)
        items = _make_items(5)
        primary = _write_corpus(tmp_path / "corpus_x.json", items)

        blocks, structured = mcp_server._build_corpus_tool_result(
            summary_text="résumé",
            primary_items=items,
            primary_path=primary,
            primary_mime="application/json",
            extra_files=[],
            structured={"query": "x"},
        )

        # Bloc 0 = résumé texte (toujours présent, non altéré sous le seuil)
        assert blocks[0].type == "text"
        assert blocks[0].text == "résumé"
        # EmbeddedResource présent et porte bien le JSON complet
        embedded = _blocks_by_type(blocks, "resource")
        assert len(embedded) == 1
        payload = json.loads(embedded[0].resource.text)
        assert len(payload) == 5
        # uri est coercé en AnyUrl par pydantic → comparer via str()
        assert str(embedded[0].resource.uri) == "corpus://corpus_x.json"
        assert embedded[0].resource.mimeType == "application/json"
        # structuredContent
        assert structured["inline_json"] is True
        assert structured["item_count"] == 5
        assert structured["inline_threshold"] == 300
        assert "inline_skipped_reason" not in structured

    def test_large_corpus_is_link_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 5)
        items = _make_items(10)  # 10 > seuil 5
        primary = _write_corpus(tmp_path / "corpus_big.json", items)

        blocks, structured = mcp_server._build_corpus_tool_result(
            summary_text="résumé",
            primary_items=items,
            primary_path=primary,
            primary_mime="application/json",
            extra_files=[],
            structured={},
        )

        # PAS d'EmbeddedResource au-delà du seuil
        assert _blocks_by_type(blocks, "resource") == []
        # ResourceLink toujours présent
        links = _blocks_by_type(blocks, "resource_link")
        assert len(links) == 1
        assert str(links[0].uri) == "corpus://corpus_big.json"
        # Le résumé est augmenté d'une mention explicite
        assert "non inliné" in blocks[0].text
        assert "corpus://corpus_big.json" in blocks[0].text
        # structuredContent
        assert structured["inline_json"] is False
        assert structured["item_count"] == 10
        assert structured["inline_skipped_reason"] == "10 items > seuil 5"

    def test_boundary_equal_threshold_is_inlined(self, tmp_path, monkeypatch):
        # n == seuil → inline (condition <=)
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 5)
        items = _make_items(5)
        primary = _write_corpus(tmp_path / "corpus_eq.json", items)
        blocks, structured = mcp_server._build_corpus_tool_result(
            "r", items, primary, "application/json", [], {}
        )
        assert structured["inline_json"] is True
        assert len(_blocks_by_type(blocks, "resource")) == 1

    def test_empty_corpus_is_inlined(self, tmp_path):
        items = []
        primary = _write_corpus(tmp_path / "corpus_empty.json", items)
        blocks, structured = mcp_server._build_corpus_tool_result(
            "r", items, primary, "application/json", [], {}
        )
        assert structured["item_count"] == 0
        assert structured["inline_json"] is True
        embedded = _blocks_by_type(blocks, "resource")
        assert json.loads(embedded[0].resource.text) == []

    def test_extra_files_become_links(self, tmp_path):
        items = _make_items(3)
        primary = _write_corpus(tmp_path / "corpus_p.json", items)
        csl = _write_corpus(tmp_path / "corpus_p.csl.json", [{"id": "a"}])
        stats = tmp_path / "corpus_p_stats.txt"
        stats.write_text("stats", encoding="utf-8")

        blocks, structured = mcp_server._build_corpus_tool_result(
            "r", items, primary, "application/json", [csl, stats], {}
        )

        links = _blocks_by_type(blocks, "resource_link")
        # primary + csl + stats = 3 liens
        assert len(links) == 3
        uris = {str(l.uri) for l in links}
        assert uris == {
            "corpus://corpus_p.json",
            "corpus://corpus_p.csl.json",
            "corpus://corpus_p_stats.txt",
        }
        # mimeType : .txt → text/plain, .json → application/json
        by_uri = {str(l.uri): l.mimeType for l in links}
        assert by_uri["corpus://corpus_p_stats.txt"] == "text/plain"
        assert by_uri["corpus://corpus_p.json"] == "application/json"
        # structured.files et resources couvrent les 3 fichiers
        assert set(structured["files"].keys()) == {
            "corpus_p.json", "corpus_p.csl.json", "corpus_p_stats.txt"
        }
        assert len(structured["resources"]) == 3

    def test_nonexistent_extra_file_skipped_in_links(self, tmp_path):
        items = _make_items(2)
        primary = _write_corpus(tmp_path / "corpus_q.json", items)
        ghost = tmp_path / "ghost.json"  # n'existe pas

        blocks, structured = mcp_server._build_corpus_tool_result(
            "r", items, primary, "application/json", [ghost], {}
        )
        link_uris = {str(l.uri) for l in _blocks_by_type(blocks, "resource_link")}
        # Le fichier inexistant n'est pas exposé en lien
        assert "corpus://ghost.json" not in link_uris
        assert "corpus://corpus_q.json" in link_uris

    def test_structured_preserves_caller_keys(self, tmp_path):
        items = _make_items(1)
        primary = _write_corpus(tmp_path / "corpus_k.json", items)
        blocks, structured = mcp_server._build_corpus_tool_result(
            "r", items, primary, "application/json", [],
            {"query": "discours", "by_source": {"HAL": 1}},
        )
        assert structured["query"] == "discours"
        assert structured["by_source"] == {"HAL": 1}
        # + clés ajoutées par le helper
        for key in ("item_count", "inline_json", "inline_threshold",
                    "files", "resources"):
            assert key in structured


# === #28 — Non-régression du dispatcher call_tool ===

class TestDispatcherNormalization:
    def test_unknown_tool_returns_textcontent_list(self):
        # OPT-07 (C30) : un échec est une erreur MCP (isError), même texte.
        result = _run(mcp_server.call_tool("outil_inexistant", {}))
        assert isinstance(result, types.CallToolResult) and result.isError
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert "Outil inconnu" in result.content[0].text

    def test_str_tool_wrapped_in_textcontent(self, tmp_path, monkeypatch):
        # corpus_list renvoie une str (outil non-producteur)
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        result = _run(mcp_server.call_tool("corpus_list", {}))
        assert isinstance(result, list)
        assert result[0].type == "text"

    def test_tuple_passthrough(self, monkeypatch):
        sentinel = (
            [types.TextContent(type="text", text="ok")],
            {"item_count": 1},
        )

        async def fake_tool(args):
            return sentinel

        monkeypatch.setattr(mcp_server, "_tool_corpus_list", fake_tool)
        result = _run(mcp_server.call_tool("corpus_list", {}))
        assert result is sentinel  # renvoyé tel quel → CombinationContent

    def test_list_passthrough(self, monkeypatch):
        blocks = [types.TextContent(type="text", text="liste")]

        async def fake_tool(args):
            return blocks

        monkeypatch.setattr(mcp_server, "_tool_corpus_list", fake_tool)
        result = _run(mcp_server.call_tool("corpus_list", {}))
        assert result is blocks

    def test_exception_returns_textcontent(self, monkeypatch):
        async def boom(args):
            raise RuntimeError("boom interne")

        monkeypatch.setattr(mcp_server, "_tool_corpus_list", boom)
        result = _run(mcp_server.call_tool("corpus_list", {}))
        # OPT-07 (C30) : exception → erreur MCP (isError), même texte.
        assert isinstance(result, types.CallToolResult) and result.isError
        assert result.content[0].type == "text"
        assert "Erreur" in result.content[0].text
        assert "boom interne" in result.content[0].text


# === #30 — Outils producteurs bout-en-bout (sans réseau) ===

class TestCorpusFilterEnriched:
    def test_filter_returns_enriched_tuple(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 300)
        src = _write_corpus(tmp_path / "corpus_src.json", _make_items(10))

        result = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": str(src), "year_min": 2000,
                              "inline": "full"}
        ))
        assert isinstance(result, tuple)
        blocks, structured = result
        assert blocks[0].type == "text"
        assert len(_blocks_by_type(blocks, "resource_link")) == 1
        assert len(_blocks_by_type(blocks, "resource")) == 1
        assert structured["source_corpus"] == str(src)
        assert "filtered_count" in structured
        assert structured["inline_json"] is True

    def test_filter_large_corpus_link_only(self, tmp_path, monkeypatch):
        """DoD #4 : seuil=5 + corpus > 5 items via un VRAI outil producteur
        ⇒ pas d'EmbeddedResource, inline_json False, ResourceLink présent."""
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 5)
        src = _write_corpus(tmp_path / "corpus_lg.json", _make_items(10))

        result = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": str(src), "inline": "full"}
        ))
        assert isinstance(result, tuple)
        blocks, structured = result
        assert _blocks_by_type(blocks, "resource") == []          # pas inliné
        assert len(_blocks_by_type(blocks, "resource_link")) == 1  # lien présent
        assert structured["inline_json"] is False
        assert "inline_skipped_reason" in structured

    def test_filter_missing_file_is_legacy_str(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        result = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": str(tmp_path / "nope.json")}
        ))
        # OPT-07 (C30) : texte historique, désormais en erreur MCP (isError).
        assert isinstance(result, types.CallToolResult) and result.isError
        assert "introuvable" in result.content[0].text.lower()


class TestCorpusToCslEnriched:
    def test_to_csl_inlines_csl_as_primary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        src = _write_corpus(tmp_path / "corpus_z.json", _make_items(4))

        result = _run(mcp_server.call_tool(
            "corpus_to_csl", {"corpus_path": str(src), "inline": "full"}
        ))
        assert isinstance(result, tuple)
        blocks, structured = result
        assert structured["format"] == "csl-json"
        embedded = _blocks_by_type(blocks, "resource")
        assert len(embedded) == 1
        # Le primaire inliné est le CSL : URI pointe sur le .csl.json
        assert str(embedded[0].resource.uri).endswith(".csl.json")
        # …et le CONTENU inliné est bien du CSL (clé 'type'), pas du Zotero
        # ('itemType') : on inline le produit converti, pas la source.
        csl_payload = json.loads(embedded[0].resource.text)
        assert csl_payload, "CSL inliné vide"
        first = csl_payload[0]
        assert "type" in first          # marque CSL (ex. 'article-journal')
        assert "itemType" not in first  # ce n'est PAS l'item Zotero source
        assert structured["csl_count"] == len(csl_payload)


class TestCorpusMergeEnriched:
    def test_merge_returns_enriched_tuple(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        a = _write_corpus(tmp_path / "corpus_a.json", _make_items(3))
        b = _write_corpus(tmp_path / "corpus_b.json", _make_items(5))

        result = _run(mcp_server.call_tool(
            "corpus_merge", {"corpus_paths": [str(a), str(b)]}
        ))
        assert isinstance(result, tuple)
        blocks, structured = result
        # primary (json) + extra (csl) = 2 liens
        assert len(_blocks_by_type(blocks, "resource_link")) == 2
        assert "merged_count" in structured
        assert "report" in structured
        # report doit rester JSON-sérialisable
        json.dumps(structured["report"])


# === Round-trip corpus:// ===

class TestRoundTripCorpusUri:
    def test_resource_link_uri_read_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        src = _write_corpus(tmp_path / "corpus_rt.json", _make_items(6))

        blocks, structured = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": str(src)}
        ))
        link = _blocks_by_type(blocks, "resource_link")[0]
        # read_resource doit relire l'URI sans erreur
        content = _run(mcp_server.read_resource(link.uri))
        data = json.loads(content)
        assert isinstance(data, list)
        # pas de marqueur d'erreur
        assert not (isinstance(data, dict) and "error" in data)


# === Sérialisation JSON des blocs ===

class TestSerialization:
    def test_all_blocks_json_serializable(self, tmp_path):
        items = _make_items(2)
        primary = _write_corpus(tmp_path / "corpus_s.json", items)
        stats = tmp_path / "corpus_s_stats.txt"
        stats.write_text("x", encoding="utf-8")
        blocks, structured = mcp_server._build_corpus_tool_result(
            "r", items, primary, "application/json", [stats], {"q": "v"}
        )
        # Reconstruire un CallToolResult comme le ferait le SDK et le sérialiser
        result = types.CallToolResult(
            content=list(blocks),
            structuredContent=structured,
            isError=False,
        )
        dumped = result.model_dump_json()
        reparsed = json.loads(dumped)
        assert reparsed["structuredContent"]["item_count"] == 2
        # content contient bien les 3 types de blocs
        kinds = {b["type"] for b in reparsed["content"]}
        assert {"text", "resource_link", "resource"} <= kinds


# === Round-trip pour fichiers écrits HORS de OUTPUT_DIR (fix revue #1) ===

class TestRoundTripOutsideOutputDir:
    """corpus_filter écrit à côté de la SOURCE, pas dans OUTPUT_DIR. Le lien
    corpus:// doit malgré tout être résoluble par read_resource (registre)."""

    def test_filter_link_resolves_when_source_outside_output_dir(
        self, tmp_path, monkeypatch
    ):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        src_dir = tmp_path / "ailleurs"   # hors OUTPUT_DIR
        src_dir.mkdir()
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", out_dir)
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 300)
        # Repartir d'un registre propre pour l'isolation du test
        monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})

        src = _write_corpus(src_dir / "corpus_mine.json", _make_items(6))
        blocks, structured = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": str(src)}
        ))
        link = _blocks_by_type(blocks, "resource_link")[0]
        # Le fichier produit est hors OUTPUT_DIR…
        assert not (out_dir / "corpus_mine_filtered.json").exists()
        # …mais le lien corpus:// se résout quand même (via le registre)
        content = _run(mcp_server.read_resource(link.uri))
        data = json.loads(content)
        assert isinstance(data, list)
        assert len(data) == 6

    def test_unregistered_unknown_uri_still_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
        content = _run(mcp_server.read_resource("corpus://jamais_vu.json"))
        assert json.loads(content).get("error")


# === #29 — structuredContent de moisson_search (sans réseau) (revue #3) ===

class TestMoissonSearchStructured:
    def test_run_moisson_sync_structured_and_links(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "INLINE_MAX_ITEMS", 300)

        # Stub réseau : le moteur (OPT-07) résout run_source dans son module
        # à chaque source → patcher engine.orchestrator.run_source suffit.
        def fake_run_source(name, cls, query, max_per_source, output_dir,
                            timestamp, http_client, **kwargs):
            return _make_items(2)

        monkeypatch.setattr("engine.orchestrator.run_source", fake_run_source)

        blocks, structured = mcp_server._run_moisson_sync(
            query="discours numérique",
            max_per_source=10,
            sources=["hal", "isidore"],
            enrich_oai=False,
            do_dedup=True,
            workers=1,
            inline="full",
        )
        # DoD : clés structuredContent de moisson_search
        for key in ("query", "by_source", "total_unique", "duplicates_removed",
                    "item_count", "inline_json", "inline_threshold",
                    "files", "resources"):
            assert key in structured, f"clé manquante : {key}"
        assert structured["query"] == "discours numérique"
        assert set(structured["by_source"]) == {"HAL", "Isidore"}
        # 3 fichiers produits → 3 ResourceLink (json + csl + stats)
        links = _blocks_by_type(blocks, "resource_link")
        assert len(links) == 3
        suffixes = sorted(str(l.uri).rsplit(".", 1)[-1] for l in links)
        assert suffixes == ["json", "json", "txt"]
        # EmbeddedResource Zotero présent (sous le seuil)
        assert len(_blocks_by_type(blocks, "resource")) == 1


# === #31 — Garde 2 Mo de read_resource (alignement seuil) (revue #4) ===

class TestReadResourceTruncation:
    def test_large_corpus_is_truncated_to_50(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        # 60 items × ~40 Ko de résumé ⇒ > 2 000 000 octets
        big = []
        for i in range(60):
            it = {"itemType": "journalArticle", "title": f"T{i}",
                  "abstractNote": "x" * 40000, "date": "2024"}
            big.append(it)
        f = tmp_path / "corpus_huge.json"
        f.write_text(json.dumps(big, ensure_ascii=False), encoding="utf-8")
        assert f.stat().st_size > 2_000_000

        content = _run(mcp_server.read_resource("corpus://corpus_huge.json"))
        data = json.loads(content)
        assert data["_truncated"] is True
        assert data["_total_items"] == 60
        assert len(data["items"]) == min(50, 60) == 50


# === Chemins legacy d'erreur des producteurs (revue #5) ===

class TestLegacyErrorPaths:
    def test_filter_invalid_json_returns_str(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        bad = tmp_path / "corpus_bad.json"
        bad.write_text("{not json", encoding="utf-8")
        result = _run(mcp_server.call_tool(
            "corpus_filter", {"corpus_path": str(bad)}
        ))
        # OPT-07 (C30) : même texte, en erreur MCP (isError).
        assert isinstance(result, types.CallToolResult) and result.isError
        assert "parsing JSON" in result.content[0].text

    def test_to_csl_non_list_returns_str(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        obj = tmp_path / "corpus_obj.json"
        obj.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        result = _run(mcp_server.call_tool(
            "corpus_to_csl", {"corpus_path": str(obj)}
        ))
        # OPT-07 (C30) : même texte, en erreur MCP (isError).
        assert isinstance(result, types.CallToolResult) and result.isError
        assert "tableau" in result.content[0].text.lower()


# === #31 — Override d'environnement MOISSON_INLINE_MAX_ITEMS ===

class TestEnvOverride:
    def test_env_var_overrides_threshold(self):
        """
        Validation DoD : MOISSON_INLINE_MAX_ITEMS=5 doit fixer le seuil.

        Exécuté en sous-processus pour ne pas polluer le module importé
        par les autres tests.
        """
        env = dict(os.environ)
        env["MOISSON_INLINE_MAX_ITEMS"] = "5"
        proc = subprocess.run(
            [sys.executable, "-c",
             "import mcp_server; print(mcp_server.INLINE_MAX_ITEMS)"],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "5"

    def test_default_threshold_is_300(self):
        env = dict(os.environ)
        env.pop("MOISSON_INLINE_MAX_ITEMS", None)
        proc = subprocess.run(
            [sys.executable, "-c",
             "import mcp_server; print(mcp_server.INLINE_MAX_ITEMS)"],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "300"
