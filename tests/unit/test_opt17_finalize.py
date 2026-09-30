# -*- coding: utf-8 -*-
"""
OPT-17, finalisation : cas que les premiers tests du digest ne couvraient pas.

- ``search_by_author`` avec un rapport de recherche réaliste (profils
  homonymes, profil à confirmer, candidats avec exemples de titres et
  co-auteurs, calqué sur la recherche live du lot C : 191 notices, rapport de
  11,7 Ko) : avant compaction, le rapport seul remplissait le plafond, le
  digest perdait toutes ses notices et le retour dépassait 15 Ko. Le rapport
  complet reste au manifeste ; le mode ``full`` et le HTTP sont inchangés ;
- digest raccourci par le plafond d'octets : l'en-tête annonce les notices
  réellement rendues et la suite reprend à ``offset`` = ce nombre (il
  annonçait ``offset=40`` quel que soit le nombre rendu, d'où un trou) ; la
  suite par ``corpus_query`` continue sans trou ni doublon ;
- isolation des variables d'environnement d'OPT-17 (``tests/conftest.py``).

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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import mcp_server  # noqa: E402
from engine import orchestrator  # noqa: E402
from engine.models import SourceResult  # noqa: E402
from utils import corpus_query as CQ  # noqa: E402
from utils import lookups  # noqa: E402

CONFTEST_PATH = PROJECT_ROOT / "tests" / "conftest.py"


def _call(name, args):
    return asyncio.run(mcp_server.call_tool(name, args))


def _digest_text(blocks):
    found = [b.text for b in blocks if getattr(b, "type", None) == "text"
             and b.text.startswith(CQ.DIGEST_HEADER)]
    assert len(found) == 1
    return found[0]


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
    return tmp_path


# === search_by_author : rapport de recherche réaliste ===

def _work(i):
    return {
        "itemType": "journalArticle" if i % 3 else "preprint",
        "title": (f"Les plateformes numériques et la fabrique des publics, "
                  f"enquête {i} : controverses, métriques et algorithmes"),
        "creators": [{"creatorType": "author", "firstName": f"Prénom{k}",
                      "lastName": f"Coauteur{i}x{k}"} for k in range(4)]
        + [{"creatorType": "author", "firstName": "Dominique",
            "lastName": "Cardon"}],
        "date": str(2026 - i // 8),
        "publicationTitle": "Revue française de sociologie des réseaux",
        "abstractNote": ("Cet article examine les usages des plateformes et "
                         "la mesure des publics numériques. " * 20),
        "DOI": f"10.3917/res.{i:04d}",
        "url": f"https://doi.org/10.3917/res.{i:04d}",
        "language": "fr",
        "tags": [{"tag": "plateformes"}, {"tag": "algorithmes"}],
        "extra": f"Sources: OpenAlex\nOpenAlex ID: W{i:08d}\nCited by: {i % 17}",
    }


def _s2_candidate(k):
    cand = {"source": "Semantic Scholar", "id": f"8757{k:04d}",
            "name": "D. Cardon", "works_count": 173 - 20 * k,
            "plausible": True, "argument": {"s2_author_id": f"8757{k:04d}"},
            "cited_by_count": 1900 - 200 * k, "h_index": 23 - k}
    if k < lookups.AUTHOR_CANDIDATE_DETAILS:
        # Seuls les premiers candidats sont détaillés (défaut : 3, comme en
        # live) : exemples de titres, période, co-auteurs principaux.
        cand.update({
            "sample_titles": [
                f"Une cartographie web de l'écosystème médiatique, volet {k} (2025)",
                "Chapitre 18 - De quelle(s) couleur(s) sont les Gilets jaunes ? "
                "Plonger des posts Facebook dans un espace idéologique latent (2023)",
                f"The dynamics of the digital public space on Twitter {k} (2026)"],
            "period": "2023–2025 (dernières publications)",
            "top_coauthors": ["Benjamin Ooghe-Tabanou", "Guillaume Plique",
                              "Jean-Philippe Cointet"]})
    return cand


def _author_report(n_items):
    """Rapport calqué sur le second appel live du lot C (openalex_author_id)."""
    hal_identity = {"source": "HAL", "id": "1017568", "name": "Dominique Cardon",
                    "works_count": 226, "argument": {"idhal": "dominique-cardon"},
                    "idhal": "dominique-cardon"}
    hal = lookups.AuthorSourceResult(
        source="hal", display_name="HAL", status="skipped",
        reason=lookups.REASON_UNCONFIRMED_PROFILE,
        detail="200 notice(s) non retenue(s)", pages=1,
        counters={"unlinked_included": 4, "discarded": 200},
        warnings=["1 dépôt(s) d'autres auteurs HAL homonymes exclus : 838230",
                  "4 dépôt(s) sans auteur HAL identifié inclus (identité non "
                  "vérifiée)"],
        method=lookups.METHOD_NAME,
        query='(authFullName_s:"Dominique Cardon" AND NOT authIdPerson_i:'
              '(838230)) OR authIdPerson_i:1017568',
        identity=hal_identity, identity_basis=lookups.BASIS_SINGLE,
        candidates=[dict(hal_identity, plausible=True, selected=True),
                    {"source": "HAL", "id": "838230", "name": "Dominique Cardon",
                     "works_count": 1, "argument": {"hal_person_id": "838230"}}],
        excluded=[{"source": "HAL", "id": "838230", "name": "Dominique Cardon",
                   "works_count": 1, "argument": {"hal_person_id": "838230"}}])
    isidore = lookups.AuthorSourceResult(
        source="isidore", display_name="Isidore", status="skipped",
        reason=lookups.REASON_AWAITING_IDENTITY,
        detail="50 notice(s) obtenue(s) par le nom, non retenues : homonymes "
               "signalés par Semantic Scholar", pages=1,
        counters={"scanned": 200, "retained": 200, "total_available": 1392,
                  "received": 200, "rejected_not_author": 150, "discarded": 50},
        method=lookups.METHOD_NAME_TEXT, query='"Dominique Cardon"',
        identity_basis=lookups.BASIS_NAME_ONLY)
    openalex = lookups.AuthorSourceResult(
        source="openalex", display_name="OpenAlex",
        items=[{} for _ in range(200)], status="complete", pages=1,
        limit_reached=True, method=lookups.METHOD_OPENALEX_ID,
        query="author.id:A5102880673",
        identity={"source": "OpenAlex",
                  "argument": {"openalex_author_id": "A5102880673"}},
        identity_verified=True, identity_basis=lookups.BASIS_IDENTIFIER,
        available=237)
    s2 = lookups.AuthorSourceResult(
        source="semantic_scholar", display_name="Semantic Scholar",
        status="skipped", reason=lookups.REASON_AMBIGUOUS_AUTHOR,
        detail="6 profils homonymes d'au moins 5 travaux",
        method=lookups.METHOD_NAME, candidates=[_s2_candidate(k) for k in range(8)])
    return lookups.AuthorSearchReport(
        name="Dominique Cardon",
        identifiers={"openalex_author_id": "A5102880673"},
        sources=[hal, isidore, openalex, s2],
        total_before_dedup=200, total_unique=n_items, duration_s=3.2)


@pytest.fixture
def author(out, monkeypatch):
    items = [_work(i) for i in range(191)]
    report = _author_report(len(items))

    def fake(name, **kw):
        return lookups.AuthorItems(copy.deepcopy(items), report=report)
    monkeypatch.setattr(mcp_server, "search_by_author", fake)
    return {"name": "Dominique Cardon", "openalex_author_id": "A5102880673"}


def _manifest(out, structured):
    return json.loads((out / structured["manifest"]).read_text("utf-8"))


class TestAuthorRealisticReport:
    def test_digest_fits_with_notices_where_the_report_filled_the_cap(
            self, author, out):
        full = _call("search_by_author", {**author, "inline": "full"})
        assert mcp_server._measure_result(*full) > 300_000   # JSON inline
        blocks, st = _call("search_by_author", author)
        total = mcp_server._measure_result(blocks, st)
        assert total <= CQ.DIGEST_MAX_BYTES
        info = st["digest"]
        assert info["total_bytes"] == total
        # Avant la compaction : 0 notice (rapport seul au plafond).
        assert info["returned"] >= CQ.DIGEST_MIN_FULL
        assert len(st["top_items"]) == info["returned"]
        text = _digest_text(blocks)
        assert f": {info['returned']} sur 191 ;" in text
        assert f"suivantes : offset={info['returned']}." in text

    def test_compact_report_keeps_what_the_agent_needs(self, author, out):
        blocks, st = _call("search_by_author", author)
        assert st["report_compact"] is True
        assert st["report_detail"]["manifest"] == st["manifest"]
        by = st["by_source_status"]
        oa = by["OpenAlex"]
        assert oa["status"] == "complete" and oa["items"] == 200
        assert oa["method"] == "openalex_author_id" and oa["pages"] == 1
        assert oa["limit_reached"] is True and oa["available"] == 237
        assert oa["identity_established"] is True
        assert oa["identity"]["argument"] == {"openalex_author_id": "A5102880673"}
        assert "query" not in oa and "native_sort" not in oa
        s2 = by["Semantic Scholar"]
        assert s2["reason"] == "ambiguous_author" and s2["candidates_count"] == 8
        assert "candidates" not in s2
        assert by["HAL"]["excluded_profiles_count"] == 1
        assert by["Isidore"]["rejected_not_author"] == 150
        # Candidats : identification et argument, sans exemples ni co-auteurs.
        assert [c["argument"] for c in st["candidates"]] == [
            {"s2_author_id": f"8757{k:04d}"} for k in range(8)]
        assert all("sample_titles" not in c and "top_coauthors" not in c
                   for c in st["candidates"])
        (hal,) = st["profiles_to_confirm"]
        assert hal["argument"] == {"idhal": "dominique-cardon"}
        assert hal["other_compatible_profiles"] == [
            {"id": "838230", "works_count": 1,
             "argument": {"hal_person_id": "838230"}}]
        assert st["caveats"] and st["production_complete"] is False
        # Le texte garde les lignes de décision (candidats, exemples).
        summary = blocks[0].text
        assert 's2_author_id="87570000"' in summary
        assert "ex. : Une cartographie web" in summary
        assert 'idhal="dominique-cardon"' in summary
        # Fichiers sur une ligne ; les dix plus récentes sont dans le digest.
        assert "Corpus sauvegardé" not in summary
        assert re.search(r"^Fichiers corpus_author_dominique_cardon_\S+ : "
                         r"\.json .*\.manifest\.json", summary, flags=re.M)
        assert "10 publications les plus récentes" not in summary

    def test_manifest_keeps_the_full_report(self, author, out):
        _blocks, st = _call("search_by_author", author)
        doc = _manifest(out, st)
        s2 = doc["sources"]["Semantic Scholar"]
        assert len(s2["candidates"]) == 8
        assert s2["candidates"][0]["sample_titles"]
        assert doc["sources"]["OpenAlex"]["query"] == "author.id:A5102880673"
        report = doc["author_report"]
        assert len(report["candidates"]) == 8
        assert report["candidates"][0]["top_coauthors"]
        assert "report_compact" not in report

    def test_none_is_compact_too(self, author, out):
        blocks, st = _call("search_by_author", {**author, "inline": "none"})
        assert st["report_compact"] is True and "top_items" not in st
        assert mcp_server._measure_result(blocks, st) <= CQ.DIGEST_MAX_BYTES

    def test_full_keeps_the_historical_report(self, author, out):
        blocks, st = _call("search_by_author", {**author, "inline": "full"})
        assert "report_compact" not in st and "report_detail" not in st
        s2 = st["by_source_status"]["Semantic Scholar"]
        assert len(s2["candidates"]) == 8 and s2["candidates"][0]["sample_titles"]
        assert st["by_source_status"]["OpenAlex"]["query"]
        assert "Corpus sauvegardé :" in blocks[0].text
        assert "10 publications les plus récentes" in blocks[0].text

    def test_http_contract_unchanged(self, author, out, monkeypatch):
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
        monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                            "https://moisson.example.org")
        blocks, st = _call("search_by_author", author)
        assert [b.type for b in blocks] == ["text"]
        assert "report_compact" not in st and "top_items" not in st
        assert "files" not in st and st["transport"] == "http"
        blob = json.dumps(st, ensure_ascii=False) + blocks[0].text
        assert str(out) not in blob
        assert "Les plateformes numériques et la fabrique" not in \
            json.dumps(st, ensure_ascii=False)
        assert all(u.startswith("https://moisson.example.org/")
                   for u in st["download_urls"].values())


# === Digest raccourci : en-tête et suite par corpus_query ===

HAL_SORT = "score desc,docid asc"


def _long_art(i):
    return {
        "itemType": "journalArticle",
        "title": ("Un très long titre sur le discours numérique " * 6) + str(i),
        "creators": [{"creatorType": "author", "firstName": "Anne",
                      "lastName": f"Auteur{i}x{k}"} for k in range(3)],
        "date": str(2000 + i % 25),
        "publicationTitle": "Revue des discours",
        "abstractNote": "Le discours numérique des institutions. " * 3,
        "tags": [],
        "DOI": f"10.7/l{i}",
        "extra": "Sources: HAL",
    }


@pytest.fixture
def long_harvest(out, monkeypatch):
    data = [_long_art(i) for i in range(80)]

    def stub(name, cls, query, mps, output_dir, run_id, http_client,
             *a, source_key=None, **kw):
        result = SourceResult(source=source_key, display_name=name,
                              items=copy.deepcopy(data), status="complete")
        result.native_sort = HAL_SORT
        # Rang natif inversé : l'ordre du classement diffère de celui du
        # fichier (la suite doit suivre le classement).
        result.native_ranks = [{"rank": 81 - r, "score": None, "sort": HAL_SORT}
                               for r in range(1, 81)]
        result.bind_ranks()
        return result
    monkeypatch.setattr(orchestrator, "run_source", stub)
    monkeypatch.setattr(config, "RANKING_DEFAULT", "native")
    return out


def _corpus_path(out):
    (path,) = [p for p in out.glob("corpus_*.json")
               if not p.name.endswith((".csl.json", ".manifest.json"))]
    return path


def _query_ids(path, offset, limit):
    _blocks, st = _call("corpus_query", {"corpus": str(path), "offset": offset,
                                         "limit": limit, "fields": ["id"]})
    return [e["id"] for e in st["items"]]


class TestTruncatedHeader:
    def test_header_follows_returned_notices_and_offset_continues(
            self, long_harvest, monkeypatch):
        monkeypatch.setattr(CQ, "DIGEST_MAX_BYTES", 6_000)
        blocks, st = mcp_server._run_moisson_sync(
            "discours", 100, ["hal"], False, True, cache=False, workers=1)
        info = st["digest"]
        k = info["returned"]
        assert info["truncated"] is True and 0 < k < 40
        text = _digest_text(blocks)
        assert f": {k} sur 80 ;" in text and ": 40 sur 80" not in text
        assert f"suivantes : offset={k}." in text
        assert "offset=40" not in text
        # Suite par corpus_query : même ordre, ni trou ni doublon.
        path = _corpus_path(long_harvest)
        digest_ids = [e["id"] for e in st["top_items"]]
        assert _query_ids(path, 0, k) == digest_ids
        whole = _query_ids(path, 0, 80)
        assert _query_ids(path, k, 10) == whole[k:k + 10]
        assert len(set(whole)) == 80
        _b, top100 = mcp_server._run_moisson_sync(
            "discours", 100, ["hal"], False, True, cache=False, workers=1,
            top=100, inline="digest")
        # Même classement, sur toute la liste qui tient sous le plafond.
        n = top100["digest"]["returned"]
        assert [e["id"] for e in top100["top_items"]] == whole[:n]

    def test_fallback_after_soft_deadline_line_retargets_the_header(
            self, long_harvest):
        blocks, st = mcp_server._run_moisson_sync(
            "discours", 100, ["hal"], False, True, cache=False, workers=1)
        st["by_source_status"]["HAL"].update({"reason": "deadline",
                                              "status": "partial"})
        result = mcp_server._note_soft_deadline((blocks, st), 50.0)
        cap = mcp_server._measure_result(*result) - 2_000
        mcp_server._fit_digest(result[0], result[1], cap)
        blocks, st = result
        k = len(st["top_items"])
        assert st["digest"]["returned"] == k and 0 < k < 40
        assert mcp_server._measure_result(blocks, st) <= cap
        text = _digest_text(blocks)
        assert f": {k} sur 80 ;" in text
        assert f"suivantes : offset={k}." in text
        assert text.count(CQ.DIGEST_TRUNCATED_PREFIX) == 1

    def test_retarget_header_and_digest_text(self):
        spec = CQ.DigestSpec(items=[_long_art(i) for i in range(5)],
                             corpus_name="corpus_x.json")
        d = CQ.build_digest(spec, 5)
        assert "offset=5." in d.text() and ": 5 sur 5 ;" in d.text()
        short = d.text(count=2)
        assert ": 2 sur 5 ;" in short and "offset=2." in short
        lines = CQ.retarget_header(d.header, 3)
        assert lines[0].startswith("Notices en tête (") and ": 3 sur 5 ;" in lines[0]
        assert lines[2].endswith("suivantes : offset=3.")
        assert lines[1] == d.header[1]          # légende intacte


def test_export_digest_size_measured_after_the_export_note(out):
    """corpus_export : la note propre à l'export est ajoutée après la mesure
    du digest ; la taille rendue est celle du retour final."""
    path = out / "corpus_export_test.json"
    path.write_text(json.dumps([_long_art(i) for i in range(12)]),
                    encoding="utf-8")
    blocks, st = _call("corpus_export", {"corpus_path": str(path)})
    assert "Export non inliné : inline='digest'" in blocks[0].text
    total = mcp_server._measure_result(blocks, st)
    assert st["digest"]["total_bytes"] == total <= CQ.DIGEST_MAX_BYTES


# === Isolation des variables d'OPT-17 (tests/conftest.py) ===

OPT17_VARS = {
    "MOISSON_INLINE_DEFAULT": ("DEFAULT_INLINE", "digest"),
    "MOISSON_DIGEST_TOP": ("DIGEST_DEFAULT_TOP", 40),
    "MOISSON_DIGEST_MAX_TOP": ("DIGEST_MAX_TOP", 100),
    "MOISSON_DIGEST_MAX_BYTES": ("DIGEST_MAX_BYTES", 15_000),
    "MOISSON_QUERY_LIMIT": ("QUERY_DEFAULT_LIMIT", 20),
    "MOISSON_QUERY_MAX_LIMIT": ("QUERY_MAX_LIMIT", 100),
    "MOISSON_QUERY_MAX_BYTES": ("QUERY_MAX_BYTES", 100_000),
    "MOISSON_RESOURCE_PAGE": ("RESOURCE_PAGE_DEFAULT", 50),
    "MOISSON_RESOURCE_PAGE_MAX": ("RESOURCE_PAGE_MAX", 200),
}


@pytest.fixture(scope="module")
def conftest_module(request):
    """Le module tests/conftest.py tel que pytest l'a chargé."""
    target = CONFTEST_PATH.resolve()
    for plugin in request.config.pluginmanager.get_plugins():
        path = getattr(plugin, "__file__", None)
        if path and Path(path).resolve() == target:
            return plugin
    pytest.fail("tests/conftest.py introuvable parmi les plugins pytest")


@pytest.mark.parametrize("var", sorted(OPT17_VARS))
def test_opt17_variables_are_not_inherited(var, conftest_module):
    """Lues à l'import de utils.corpus_query : un shell qui les exporte ne
    change pas les tests (20 échecs avec MOISSON_INLINE_DEFAULT=full et
    quelques bornes avant leur ajout au conftest)."""
    assert var in conftest_module._INHERITED_TUNING_VARS
    assert var not in os.environ
    attr, default = OPT17_VARS[var]
    assert getattr(CQ, attr) == default
