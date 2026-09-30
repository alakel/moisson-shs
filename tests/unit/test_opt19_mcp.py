"""
Tests OPT-19 — outil MCP ``corpus_diff`` et prompt ``veille-mensuelle``.
Hors réseau, sans socket (appels directs du dispatcher, de l'endpoint de
téléchargement et des fonctions de prompts), dossiers temporaires.

- Déclaration : outil listé à côté des neuf outils historiques, schéma
  (corpus_a, corpus_b requis ; fuzzy, sample, output_path).
- Stdio : rapport ``<radical de B>.diff.json`` à côté de B, chemin dans
  ``files``, aucun lien ``corpus://`` ni contenu inliné (fichier associé),
  titres dans le texte et les échantillons ; nom nu cherché dans OUTPUT_DIR ;
  avertissement de mode lu dans les manifestes ; refus clairs en ``isError``
  sans rien écrire (CSL, manifeste, fichier absent, paramètres invalides).
- HTTP : entrées et ``output_path`` confinés, URL de téléchargement seule,
  aucun chemin disque, aucun titre (identifiants seuls) ; rapport servi par
  ``/download``, jamais listé ni lu comme ressource.
- Prompt : arguments historiques inchangés ; enchaînement corpus_list (avant
  la moisson) → moisson_search → corpus_diff ; distinctions et limites.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp.types as types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
from utils import manifest as mf  # noqa: E402

HISTORICAL_TOOLS = {
    "moisson_search", "corpus_analyze", "corpus_list", "fetch_by_doi",
    "search_by_author", "corpus_merge", "corpus_to_csl", "corpus_filter",
    "corpus_resolve_dois",
}

SA = "corpus_discours_numerique_20260801_100000_aaaaaaaa"
SB = "corpus_discours_numerique_20260901_100000_bbbbbbbb"


def art(title, date="2020", *, doi=None, extra=None, **kw):
    item = {"itemType": kw.pop("itemType", "journalArticle"), "title": title,
            "date": date,
            "creators": [{"creatorType": "author", "lastName": "Dupont",
                          "firstName": "Jean"}]}
    if doi:
        item["DOI"] = doi
    if extra:
        item["extra"] = "\n".join(extra)
    item.update(kw)
    return item


ITEMS_A = [
    art("Commun enrichi", date="2019", extra=["HAL ID: hal-01234567",
                                              "Sources: HAL"]),
    art("Article retiré de la sélection", doi="10.1/old", date="2015",
        extra=["Sources: HAL"]),
    art("Identique", doi="10.1/same", extra=["Sources: HAL"]),
]
ITEMS_B = [
    art("Commun enrichi", date="2019-03", doi="10.1/x",
        abstractNote="Un vrai résumé d'éditeur.",
        extra=["HAL ID: hal-01234567v2", "Sources: HAL"]),
    art("Identique", doi="10.1/same", extra=["Sources: HAL"]),
    art("Nouveauté de septembre", doi="10.1/new", date="2026-09-10",
        extra=["Sources: HAL"]),
]
TITLES = ["Commun enrichi", "Article retiré de la sélection",
          "Nouveauté de septembre"]


def manifest_doc(stem, *, date, mode="bibliographie", capped=True):
    hal = {"source": "hal", "status": "complete", "items": 3,
           "native_sort": "score desc", "limit_reached": capped}
    return {
        "format": "moisson-shs/manifest", "version": 1, "stem": stem,
        "kind": "moisson", "started_at": date, "finished_at": date,
        "query": {"text": "discours numérique", "mode": mode,
                  "sources": ["hal"], "from_date": None, "to_date": None,
                  "languages": []},
        "filters": {"from_date": None, "to_date": None, "languages": []},
        "settings": {"max_per_source": 200},
        "sources": {"HAL": hal},
        "files": [],
    }


def write_corpus(directory, stem, items, manifest=None):
    path = Path(directory) / f"{stem}.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    if manifest is not None:
        (Path(directory) / f"{stem}.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def _run(coro):
    return asyncio.run(coro)


def _call(args):
    return _run(mcp_server.call_tool("corpus_diff", args))


def _of_type(blocks, cls):
    return [b for b in blocks if isinstance(b, cls)]


@pytest.fixture
def stdio_dirs(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", out)
    return out, elsewhere


@pytest.fixture
def http_mode(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "http")
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", out)
    monkeypatch.setattr(mcp_server, "PUBLIC_BASE_URL",
                        "https://moisson.example.org")
    return out


# === Déclaration ===

class TestDeclaration:
    def test_tool_listed_next_to_the_historical_nine(self):
        tools = {t.name: t for t in _run(mcp_server.list_tools())}
        assert HISTORICAL_TOOLS <= set(tools)
        assert "corpus_diff" in tools
        names = [t.name for t in _run(mcp_server.list_tools())]
        assert len(names) == len(set(names))
        schema = tools["corpus_diff"].inputSchema
        assert schema["required"] == ["corpus_a", "corpus_b"]
        props = schema["properties"]
        assert {"corpus_a", "corpus_b", "fuzzy", "sample",
                "output_path"} == set(props)
        assert props["fuzzy"]["default"] is False
        assert props["sample"]["maximum"] == 100
        # Aucun seuil de similarité exposé (invariant 5).
        assert "threshold" not in json.dumps(schema)


# === Stdio ===

class TestStdio:
    def test_report_next_to_b_with_counts_and_titles(self, stdio_dirs):
        out, elsewhere = stdio_dirs
        a = write_corpus(elsewhere, SA, ITEMS_A)
        b = write_corpus(elsewhere, SB, ITEMS_B)
        res = _call({"corpus_a": str(a), "corpus_b": str(b)})
        assert isinstance(res, tuple), res
        blocks, st = res
        report = elsewhere / f"{SB}.diff.json"
        assert report.is_file()
        assert st["files"] == {report.name: str(report)}
        assert st["report"] == report.name
        c = st["counts"]
        assert (c["added"], c["removed"], c["unchanged"]) == (1, 1, 2)
        assert (c["enriched"], c["identical"]) == (1, 1)
        assert c["enriched_fields"] == {"DOI": 1, "abstract": 1, "date": 1}
        assert c["added_by_category"]["nouvelle_publication"] == 1
        # Fichier associé : ni lien corpus:// ni contenu inliné.
        assert not _of_type(blocks, types.ResourceLink)
        assert not _of_type(blocks, types.EmbeddedResource)
        text = blocks[0].text
        assert "Présents dans B seulement (ajouts) : 1" in text
        for title in TITLES:
            assert title in text
        assert st["samples"]["added"][0]["title"] == "Nouveauté de septembre"
        assert st["sample_titles"] is True
        doc = json.loads(report.read_text(encoding="utf-8"))
        assert not mf.absolute_paths_in(doc)
        assert str(elsewhere) not in report.read_text(encoding="utf-8")
        # Rien d'autre n'est écrit (ni dans OUTPUT_DIR, ni temporaire).
        assert sorted(p.name for p in elsewhere.iterdir()) == sorted(
            [a.name, b.name, report.name])
        assert not list(out.iterdir())

    def test_bare_names_are_looked_up_in_output_dir(self, stdio_dirs):
        out, _elsewhere = stdio_dirs
        write_corpus(out, SA, ITEMS_A)
        write_corpus(out, SB, ITEMS_B)
        blocks, st = _call({"corpus_a": f"{SA}.json", "corpus_b": f"{SB}.json"})
        assert (out / f"{SB}.diff.json").is_file()
        assert st["corpus_a"]["name"] == f"{SA}.json"

    def test_bibliographie_mode_warning_from_manifests(self, stdio_dirs):
        out, _ = stdio_dirs
        a = write_corpus(out, SA, ITEMS_A,
                         manifest_doc(SA, date="2026-08-01T10:00:00+02:00"))
        b = write_corpus(out, SB, ITEMS_B,
                         manifest_doc(SB, date="2026-09-01T10:00:00+02:00"))
        blocks, st = _call({"corpus_a": str(a), "corpus_b": str(b)})
        codes = {w["code"] for w in st["warnings"]}
        assert "mode_bibliographie" in codes
        assert st["corpus_a"]["mode"] == st["corpus_b"]["mode"] == "bibliographie"
        assert st["corpus_a"]["sources"][0]["native_sort"] == "score desc"
        assert "Mode bibliographie" in blocks[0].text
        assert "mode bibliographie" in blocks[0].text   # ligne de contexte

    def test_sample_and_fuzzy_parameters(self, stdio_dirs):
        out, _ = stdio_dirs
        a = write_corpus(out, SA, ITEMS_A)
        b = write_corpus(out, SB, ITEMS_B)
        blocks, st = _call({"corpus_a": str(a), "corpus_b": str(b),
                            "sample": 0, "fuzzy": True})
        assert st["sample_size"] == 0
        assert all(v == [] for v in st["samples"].values())
        assert st["identity"]["fuzzy"] is True
        assert st["counts"]["added"] == 1

    def test_output_path_honoured(self, stdio_dirs, tmp_path):
        out, elsewhere = stdio_dirs
        a = write_corpus(elsewhere, SA, ITEMS_A)
        b = write_corpus(elsewhere, SB, ITEMS_B)
        target = tmp_path / "veille_septembre.diff.json"
        blocks, st = _call({"corpus_a": str(a), "corpus_b": str(b),
                            "output_path": str(target)})
        assert target.is_file() and st["report"] == target.name
        assert not (elsewhere / f"{SB}.diff.json").exists()

    @pytest.mark.parametrize("bad_name,content,fragment", [
        (f"{SA}.csl.json", [{"type": "article-journal", "title": "X"}],
         "CSL-JSON"),
        ("corpus_csl_mal_nomme.json", [{"type": "article-journal",
                                        "title": "X"}], "CSL-JSON"),
        (f"{SA}.manifest.json", {"format": "moisson-shs/manifest",
                                 "version": 1}, "manifeste"),
        (f"{SA}.diff.json", {"format": "moisson-shs/diff"},
         "rapport de comparaison"),
    ])
    def test_refused_inputs_are_tool_errors_without_writing(
            self, stdio_dirs, bad_name, content, fragment):
        out, elsewhere = stdio_dirs
        bad = elsewhere / bad_name
        bad.write_text(json.dumps(content), encoding="utf-8")
        ok = write_corpus(elsewhere, SB, ITEMS_B)
        before = sorted(p.name for p in elsewhere.iterdir())
        for args in ({"corpus_a": str(bad), "corpus_b": str(ok)},
                     {"corpus_a": str(ok), "corpus_b": str(bad)}):
            res = _call(args)
            assert isinstance(res, types.CallToolResult) and res.isError
            assert fragment in res.content[0].text
        assert sorted(p.name for p in elsewhere.iterdir()) == before

    @pytest.mark.parametrize("extra,fragment", [
        ({"sample": 101}, "sample"),
        ({"sample": -1}, "sample"),
        ({"sample": True}, "sample"),
        ({"sample": 2.5}, "sample"),
        ({"fuzzy": "oui"}, "fuzzy"),
        ({"output_path": "rapport.json"}, ".diff.json"),
    ])
    def test_invalid_parameters(self, stdio_dirs, extra, fragment):
        out, elsewhere = stdio_dirs
        a = write_corpus(elsewhere, SA, ITEMS_A)
        b = write_corpus(elsewhere, SB, ITEMS_B)
        res = _call({"corpus_a": str(a), "corpus_b": str(b), **extra})
        assert isinstance(res, types.CallToolResult) and res.isError
        assert fragment in res.content[0].text
        assert not list(elsewhere.glob("*.diff.json"))

    def test_unwritable_folder_is_a_clean_tool_error(self, stdio_dirs):
        out, elsewhere = stdio_dirs
        a = write_corpus(elsewhere, SA, ITEMS_A)
        b = write_corpus(elsewhere, SB, ITEMS_B)
        elsewhere.chmod(0o555)
        try:
            res = _call({"corpus_a": str(a), "corpus_b": str(b)})
        finally:
            elsewhere.chmod(0o755)
        assert isinstance(res, types.CallToolResult) and res.isError
        assert "Écriture du rapport impossible" in res.content[0].text
        assert str(elsewhere) not in res.content[0].text
        assert not list(elsewhere.glob("*.diff.json"))
        assert not [p for p in elsewhere.iterdir() if p.name.startswith(".")]

    def test_missing_arguments_and_files(self, stdio_dirs):
        out, elsewhere = stdio_dirs
        b = write_corpus(elsewhere, SB, ITEMS_B)
        res = _call({"corpus_b": str(b)})
        assert res.isError and "corpus_a" in res.content[0].text
        res = _call({"corpus_a": str(elsewhere / "absent.json"),
                     "corpus_b": str(b)})
        assert res.isError and "introuvable" in res.content[0].text
        # Un fichier de service n'est jamais cherché dans OUTPUT_DIR.
        (out / ".index.json").write_text("[]", encoding="utf-8")
        res = _call({"corpus_a": ".index.json", "corpus_b": str(b)})
        assert res.isError


# === HTTP ===

class TestHttp:
    def test_download_url_only_no_path_no_title(self, http_mode):
        write_corpus(http_mode, SA, ITEMS_A,
                     manifest_doc(SA, date="2026-08-01T10:00:00+02:00"))
        write_corpus(http_mode, SB, ITEMS_B,
                     manifest_doc(SB, date="2026-09-01T10:00:00+02:00"))
        res = _call({"corpus_a": f"{SA}.json", "corpus_b": f"{SB}.json"})
        assert isinstance(res, tuple), res
        blocks, st = res
        name = f"{SB}.diff.json"
        assert (http_mode / name).is_file()
        assert st["download_urls"] == {
            name: f"https://moisson.example.org/download/{name}"}
        assert "files" not in st and st["transport"] == "http"
        assert not _of_type(blocks, types.ResourceLink)
        assert not _of_type(blocks, types.EmbeddedResource)
        dumped = json.dumps(st, ensure_ascii=False) + blocks[0].text
        assert str(http_mode) not in dumped
        assert str(http_mode.parent) not in dumped
        # Aucune donnée bibliographique inline : identifiants seulement.
        for title in TITLES:
            assert title not in dumped
        assert st["sample_titles"] is False
        added = st["samples"]["added"][0]
        assert added["id"] == "doi:10.1/new" and "title" not in added
        assert added["category"] == "nouvelle_publication"
        assert st["samples"]["removed"][0]["id"] == "doi:10.1/old"
        assert st["samples"]["enriched"][0]["changes"]
        # Compteurs et avertissements présents.
        assert st["counts"]["added"] == 1
        assert "mode_bibliographie" in {w["code"] for w in st["warnings"]}
        assert "Mode bibliographie" in blocks[0].text
        # Le rapport complet, lui, porte les titres (fichier téléchargé).
        doc = json.loads((http_mode / name).read_text(encoding="utf-8"))
        assert doc["added"][0]["title"] == "Nouveauté de septembre"
        assert not mf.absolute_paths_in(doc)

    def test_report_downloadable_never_listed_nor_read(self, http_mode):
        write_corpus(http_mode, SA, ITEMS_A)
        write_corpus(http_mode, SB, ITEMS_B)
        _call({"corpus_a": f"{SA}.json", "corpus_b": f"{SB}.json"})
        name = f"{SB}.diff.json"
        resp = _run(mcp_server._download_endpoint(
            SimpleNamespace(path_params={"filename": name})))
        assert resp.status_code == 200
        assert resp.media_type.startswith("application/json")
        listed = _run(mcp_server.list_resources())
        assert name not in {str(r.uri) for r in listed}
        assert all(".diff" not in str(r.uri) for r in listed)
        read = json.loads(_run(mcp_server.read_resource(f"corpus://{name}")))
        assert "error" in read
        listing = _run(mcp_server.call_tool("corpus_list", {}))
        text = listing[0].text if isinstance(listing, list) else listing.content[0].text
        assert ".diff.json" not in text and SB in text

    def test_inputs_and_output_confined(self, http_mode, tmp_path_factory):
        write_corpus(http_mode, SB, ITEMS_B)
        outside_dir = tmp_path_factory.mktemp("dehors")
        outside = write_corpus(outside_dir, SA, ITEMS_A)
        res = _call({"corpus_a": str(outside), "corpus_b": f"{SB}.json"})
        assert isinstance(res, types.CallToolResult) and res.isError
        assert str(outside_dir) not in res.content[0].text
        write_corpus(http_mode, SA, ITEMS_A)
        target = outside_dir / "x.diff.json"
        res = _call({"corpus_a": f"{SA}.json", "corpus_b": f"{SB}.json",
                     "output_path": str(target)})
        assert res.isError and not target.exists()
        sub = http_mode / "sous"
        sub.mkdir()
        res = _call({"corpus_a": f"{SA}.json", "corpus_b": f"{SB}.json",
                     "output_path": str(sub / "x.diff.json")})
        assert res.isError and "sous-dossier" in res.content[0].text
        res = _call({"corpus_a": f"{SA}.json", "corpus_b": f"{SB}.json",
                     "output_path": "../x.diff.json"})
        assert res.isError
        blocks, st = _call({"corpus_a": f"{SA}.json", "corpus_b": f"{SB}.json",
                            "output_path": "veille.diff.json"})
        assert list(st["download_urls"]) == ["veille.diff.json"]
        assert (http_mode / "veille.diff.json").is_file()

    def test_csl_refused_without_path(self, http_mode):
        write_corpus(http_mode, SB, ITEMS_B)
        (http_mode / f"{SA}.csl.json").write_text(
            json.dumps([{"type": "article-journal", "title": "X"}]),
            encoding="utf-8")
        res = _call({"corpus_a": f"{SA}.csl.json", "corpus_b": f"{SB}.json"})
        assert isinstance(res, types.CallToolResult) and res.isError
        assert "CSL-JSON" in res.content[0].text
        assert str(http_mode) not in res.content[0].text
        assert not list(http_mode.glob("*.diff.json"))


# === Prompt veille-mensuelle ===

class TestPrompt:
    def test_arguments_unchanged(self):
        prompts = {p.name: p for p in _run(mcp_server.list_prompts())}
        veille = prompts["veille-mensuelle"]
        assert [(a.name, a.required) for a in veille.arguments] == [
            ("theme", True), ("from_date", True), ("to_date", True)]
        assert "corpus_diff" in veille.description
        assert {"bibliographie-thematique", "profil-chercheur",
                "etat-de-l-art"} <= set(prompts)

    def test_chain_list_harvest_diff(self):
        res = _run(mcp_server.get_prompt("veille-mensuelle", {
            "theme": "humanités numériques", "from_date": "2026-09-01",
            "to_date": "2026-09-30"}))
        text = res.messages[0].content.text
        # Enchaînement : référence cherchée AVANT la nouvelle moisson.
        i_list = text.index("corpus_list")
        i_search = text.index("moisson_search")
        i_diff = text.index("Appelle corpus_diff")
        assert i_list < i_search < i_diff
        assert "pattern" in text and "since" in text
        assert "from_date=2026-09-01" in text and "to_date=2026-09-30" in text
        assert "corpus_a = le corpus de référence" in text
        # Thème passé sans guillemets (sinon expression exacte, OPT-11).
        assert "« humanités numériques »" not in text
        assert '"humanités numériques"' not in text
        assert "humanités numériques" in text
        # Distinctions demandées.
        for fragment in ("nouvelles publications", "nouvellement récupérées",
                         "enrichies", "retraits"):
            assert fragment in text
        # Limites rappelées.
        for fragment in ("sources partielles", "fenêtre de résultats",
                         "mode bibliographie", "plafond"):
            assert fragment in text
        assert ".csl.json" in text          # jamais le CSL en entrée


# === Bout en bout : deux vraies moissons (sources simulées) ===

class TestEndToEnd:
    """Deux moissons ``_run_moisson_sync`` (manifestes réels d'OPT-16),
    puis corpus_diff : mode, tri natif, statut des sources et date de
    référence lus dans les manifestes réellement produits."""

    @staticmethod
    def _runner_for(items_by_source, capped=()):
        from engine.models import SourceResult

        def runner(name, cls, query, mps, output_dir, run_id, http_client,
                   run_context=None, source_key=None, **kw):
            items = [dict(it) for it in items_by_source.get(source_key, [])]
            res = SourceResult(
                source=source_key, display_name=name, items=items,
                status="complete", native_sort="score desc",
                limit_reached=source_key in capped,
                native_ranks=[{"rank": i + 1, "score": 10.0 - i,
                               "sort": "score desc"}
                              for i in range(len(items))])
            res.bind_ranks()
            return res
        return runner

    def test_diff_of_two_real_harvests(self, stdio_dirs, monkeypatch):
        from engine import orchestrator

        out, _ = stdio_dirs
        monkeypatch.setattr(mcp_server, "_EMITTED_RESOURCES", {})
        first = {"hal": [art("Commun", date="2019", extra=["Sources: HAL",
                                                           "HAL ID: hal-07777777"]),
                         art("Ancien retenu", doi="10.1/anc", date="2012",
                             extra=["Sources: HAL"])]}
        second = {"hal": [art("Commun", date="2019", doi="10.1/commun",
                              extra=["Sources: HAL", "HAL ID: hal-07777777"]),
                          art("Parution du mois", doi="10.1/neuf",
                              date="2099-01-15", extra=["Sources: HAL"])]}
        monkeypatch.setattr(orchestrator, "run_source",
                            self._runner_for(first, capped={"hal"}))
        res_a = mcp_server._run_moisson_sync("discours numérique", 2, ["hal"],
                                             False, True, workers=1)
        monkeypatch.setattr(orchestrator, "run_source",
                            self._runner_for(second, capped={"hal"}))
        res_b = mcp_server._run_moisson_sync("discours numérique", 2, ["hal"],
                                             False, True, workers=1)
        corpus = sorted(p for p in out.glob("corpus_*.json")
                        if p.name.count(".") == 1)
        assert len(corpus) == 2, corpus
        names = [res_a[1]["files"], res_b[1]["files"]]
        a = next(p for p in corpus if p.name in names[0])
        b = next(p for p in corpus if p.name in names[1])
        blocks, st = _call({"corpus_a": a.name, "corpus_b": b.name})
        c = st["counts"]
        assert (c["added"], c["removed"], c["unchanged"]) == (1, 1, 1)
        assert c["enriched_fields"] == {"DOI": 1}
        assert c["added_by_category"]["nouvelle_publication"] == 1
        # Contexte lu dans les manifestes réels.
        for side in ("corpus_a", "corpus_b"):
            ctx = st[side]
            assert ctx["manifest"] and ctx["kind"] == "moisson"
            assert ctx["mode"] == "bibliographie"
            assert ctx["query"] == "discours numérique"
            assert ctx["harvest_date_basis"] == "manifeste"
            assert ctx["max_per_source"] == 2
            (hal,) = ctx["sources"]
            assert hal["native_sort"] == "score desc"
            assert hal["limit_reached"] is True
        codes = {w["code"] for w in st["warnings"]}
        assert "mode_bibliographie" in codes
        assert not codes & {"mode_inconnu", "requetes_differentes",
                            "sources_differentes", "modes_differents"}
        # Retrait : source plafonnée dans B (sélection par score).
        assert st["samples"]["removed"][0]["reason"] == "selection_plafonnee"
        # Le rapport s'ajoute au corpus B sans le rendre incomplet.
        listing = mf.list_corpora(out, rebuild=True)
        entry = next(e for e in listing.entries if e["stem"] == b.stem)
        assert entry["status"] == "complete"
        assert f"{b.stem}.diff.json" in entry["files"]
