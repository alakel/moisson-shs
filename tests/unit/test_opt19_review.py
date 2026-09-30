"""
Tests OPT-19 — non-régression des constats de revue de ``corpus_diff`` et du
prompt ``veille-mensuelle``. Hors réseau, sans socket, dossiers temporaires.

1. Identité : le régime d'égalité exacte de la déduplication (titre
   normalisé identique, année, premier auteur, contraintes dures) s'applique
   par défaut, comme dans ``deduplicate_with_report(fuzzy=False)`` ; la
   similarité 0,93 seulement avec ``fuzzy=True``. ``diff(X, X)`` ne rend
   aucun ajout ni retrait, même pour une notice sans identifiant.
2. Rapport par défaut : une comparaison ne remplace jamais le rapport d'une
   autre ; le lien rendu par un appel sert toujours le rapport de cet appel
   (stdio et HTTP) ; ``output_path`` garde un nom stable, remplacé.
3. Libellé « nouveau dépôt HAL » : la publication n'est pas dite antérieure
   quand sa date est de même période que la moisson de référence.
4. ``queries_match`` : requête longue dont le 60e caractère du nom est un
   séparateur (corpus ancien contre moisson avec manifeste).
5. Prompt de veille : repli explicite sur un corpus ancien, avec les
   libellés réellement affichés par ``corpus_list``.
"""

import asyncio
import errno
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import mcp_server  # noqa: E402
from utils import corpus_diff as cd  # noqa: E402
from utils.deduplication import deduplicate_with_report  # noqa: E402
from utils.output_files import DIFF_REPORT_SUFFIX, file_role  # noqa: E402


def art(title, date="2021", *, author="Dupont", doi=None, extra=None, **kw):
    item = {"itemType": kw.pop("itemType", "journalArticle"), "title": title,
            "date": date,
            "creators": ([{"creatorType": "author", "lastName": author,
                           "firstName": "Jean"}] if author else [])}
    if doi:
        item["DOI"] = doi
    if extra:
        item["extra"] = "\n".join(extra)
    item.update(kw)
    return item


REF_DAY = [2026, 8, 1]
CTX_A = {"name": "a.json", "items": 0, "harvest_day": REF_DAY,
         "harvest_date": "2026-08-01T10:00:00"}
SA = "corpus_discours_numerique_20260801_100000_aaaaaaaa"
SA2 = "corpus_discours_numerique_20260815_100000_cccccccc"
SB = "corpus_discours_numerique_20260901_100000_bbbbbbbb"
UNIQUE_RE = re.compile(re.escape(SB) + r"_[0-9a-f]{8}" + re.escape(DIFF_REPORT_SUFFIX) + "$")


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


def _text(res):
    if isinstance(res, tuple):
        return res[0][0].text
    if isinstance(res, list):
        return res[0].text
    return res.content[0].text


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


# === 1. Régime d'égalité exacte par défaut ===

TITLE = "Une histoire sociale des écritures numériques ordinaires"


class TestExactRegime:
    def test_identical_normalized_titles_match_by_default(self):
        # Accents et point final : titres identiques une fois normalisés.
        a = [art(TITLE)]
        b = [art("Une histoire sociale des ecritures numeriques ordinaires.")]
        _, dedup = deduplicate_with_report(a + b, fuzzy=False)
        assert dedup["output"] == 1
        r = cd.diff_corpora(a, b, context_a=dict(CTX_A))
        assert (r["counts"]["added"], r["counts"]["removed"],
                r["counts"]["unchanged"]) == (0, 0, 1)
        assert r["rules"]["fuzzy"] is False

    def test_self_diff_with_a_keyless_record(self, tmp_path):
        keyless = art("Les mots du numérique ordinaire", date="2019",
                      itemType="book", extra=["Sources: Cairn"])
        keyed = art("Autre", date="2020", doi="10.1/x", author=None)
        path = write_corpus(tmp_path, SA, [keyless, keyed])
        report = cd.diff_corpus_files(path, path, write=False).report
        c = report["counts"]
        assert (c["added"], c["removed"]) == (0, 0)
        assert c["identical"] == c["unchanged"] == 2
        assert c["without_keys_a"] == c["without_keys_b"] == 1

    def test_same_work_from_two_sources_without_doi(self):
        # Même travail rendu par Cairn puis par OpenEdition, sans DOI : les
        # deux notices ont une clé (URL), jamais partagée.
        def rec(url, src):
            return art("Les écritures numériques ordinaires en contexte "
                       "scolaire", url=url, extra=[f"Sources: {src}"])
        a = [rec("https://www.cairn.info/revue-x-2021-1-page-10.htm", "Cairn")]
        b = [rec("https://journals.openedition.org/x/1234", "OpenEdition")]
        _, dedup = deduplicate_with_report(a + b, fuzzy=False)
        assert dedup["output"] == 1
        r = cd.diff_corpora(a, b, context_a=dict(CTX_A))
        assert r["counts"]["unchanged"] == 1
        assert r["counts"]["added"] == r["counts"]["removed"] == 0
        # Source de plus : provenance, jamais un enrichissement.
        assert r["counts"]["enriched"] == 0

    def test_similar_titles_need_fuzzy(self):
        a = [art(TITLE)]
        b = [art("Une histoire sociale des écritures numériques ordinaire")]
        assert deduplicate_with_report(a + b, fuzzy=False)[1]["output"] == 2
        assert deduplicate_with_report(a + b, fuzzy=True)[1]["output"] == 1
        r = cd.diff_corpora(a, b, context_a=dict(CTX_A))
        assert (r["counts"]["added"], r["counts"]["removed"]) == (1, 1)
        assert "sans_cle" in {w["code"] for w in r["warnings"]}
        r = cd.diff_corpora(a, b, context_a=dict(CTX_A), fuzzy=True)
        assert r["counts"]["unchanged"] == 1

    @pytest.mark.parametrize("a,b", [
        # Titre court : même année et même premier auteur exigés.
        (art("Dialogisme et polyphonie", date="2019"),
         art("Dialogisme et polyphonie", date="2020")),
        (art("Dialogisme et polyphonie", date="2019"),
         art("Dialogisme et polyphonie", date="2019")),
        # Premier auteur différent : jamais apparié.
        (art(TITLE, author="Martin"), art(TITLE, author="Durand")),
        # Année trop éloignée.
        (art(TITLE, date="2015"), art(TITLE, date="2021")),
        # Contrainte dure de type (chapitre et livre de même titre).
        (art(TITLE, itemType="book"), art(TITLE, itemType="bookSection",
                                          bookTitle="Recueil")),
        # Deux DOI différents : garde-fou.
        (art(TITLE, doi="10.1/a"), art(TITLE, doi="10.1/b")),
    ])
    @pytest.mark.parametrize("fuzzy", [False, True])
    def test_default_regime_agrees_with_deduplication(self, a, b, fuzzy):
        merged = deduplicate_with_report([a, b], fuzzy=fuzzy)[1]["output"] == 1
        r = cd.diff_corpora([a], [b], context_a=dict(CTX_A), fuzzy=fuzzy)
        assert (r["counts"]["unchanged"] == 1) is merged

    def test_rules_version_and_wording(self):
        assert cd.DIFF_RULES_VERSION != "opt19-1"
        r = cd.diff_corpora([art(TITLE)], [art(TITLE + ".")],
                            context_a=dict(CTX_A))
        assert "titre normalisé identique" in r["rules"]["identity"]
        text = "\n".join(cd.summary_lines(r))
        assert "titre normalisé identique" in text
        assert "aucune passe floue" not in text


# === 2. Rapport par défaut jamais remplacé par une autre comparaison ===

A1 = [art("x", doi="10.1/x")]
A2 = [art("z", doi="10.1/z"), art("w", doi="10.1/w"), art("v", doi="10.1/v")]
B = [art("x", doi="10.1/x"), art("y", doi="10.1/y")]


class TestReportNaming:
    def test_successive_diffs_of_the_same_b_keep_their_reports(self, tmp_path):
        a1 = write_corpus(tmp_path, SA, A1)
        a2 = write_corpus(tmp_path, SA2, A2)
        b = write_corpus(tmp_path, SB, B)
        first = cd.diff_corpus_files(a1, b).report_path
        second = cd.diff_corpus_files(a2, b).report_path
        third = cd.diff_corpus_files(a2, b).report_path
        assert first.name == f"{SB}{DIFF_REPORT_SUFFIX}"
        assert UNIQUE_RE.match(second.name) and UNIQUE_RE.match(third.name)
        assert len({first, second, third}) == 3
        doc1 = json.loads(first.read_text(encoding="utf-8"))
        doc2 = json.loads(second.read_text(encoding="utf-8"))
        assert doc1["corpus_a"]["name"] == a1.name
        assert doc1["report"] == first.name
        assert doc2["corpus_a"]["name"] == a2.name
        assert doc2["report"] == second.name
        for path in (first, second, third):
            assert file_role(path.name) == "diff_report"
        # Aucun temporaire laissé.
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

    def test_existing_default_name_is_never_replaced(self, tmp_path):
        # Rapport déjà publié sous le nom par défaut (par exemple par un
        # appel concurrent) : jamais remplacé.
        a = write_corpus(tmp_path, SA, A1)
        b = write_corpus(tmp_path, SB, B)
        existing = tmp_path / f"{SB}{DIFF_REPORT_SUFFIX}"
        existing.write_bytes(b'{"format": "moisson-shs/diff", "sentinelle": 1}')
        res = cd.diff_corpus_files(a, b)
        assert UNIQUE_RE.match(res.report_path.name)
        assert b"sentinelle" in existing.read_bytes()

    def test_without_hard_links_the_existing_report_is_kept(
            self, tmp_path, monkeypatch):
        # Système de fichiers sans liens physiques : repli sans remplacement.
        def no_link(src, dst, *args, **kwargs):
            raise OSError(errno.EPERM, "liens physiques indisponibles")
        monkeypatch.setattr(os, "link", no_link)
        a = write_corpus(tmp_path, SA, A1)
        b = write_corpus(tmp_path, SB, B)
        first = cd.diff_corpus_files(a, b).report_path
        before = first.read_bytes()
        second = cd.diff_corpus_files(a, b).report_path
        assert first.name == f"{SB}{DIFF_REPORT_SUFFIX}"
        assert UNIQUE_RE.match(second.name)
        assert first.read_bytes() == before
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

    def test_explicit_output_path_is_a_stable_replaced_name(self, tmp_path):
        a1 = write_corpus(tmp_path, SA, A1)
        a2 = write_corpus(tmp_path, SA2, A2)
        b = write_corpus(tmp_path, SB, B)
        target = tmp_path / "veille.diff.json"
        cd.diff_corpus_files(a1, b, report_path=target)
        res = cd.diff_corpus_files(a2, b, report_path=target)
        assert res.report_path == target
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["corpus_a"]["name"] == a2.name
        assert not (tmp_path / f"{SB}{DIFF_REPORT_SUFFIX}").exists()

    def test_http_link_of_the_first_call_still_serves_its_report(
            self, http_mode):
        write_corpus(http_mode, SA, A1)
        write_corpus(http_mode, SA2, A2)
        write_corpus(http_mode, SB, B)
        _b1, st1 = _call({"corpus_a": f"{SA}.json", "corpus_b": f"{SB}.json"})
        _b2, st2 = _call({"corpus_a": f"{SA2}.json", "corpus_b": f"{SB}.json"})
        (name1, url1), = st1["download_urls"].items()
        (name2, url2), = st2["download_urls"].items()
        assert url1 != url2 and name1 != name2
        assert st1["report"] == name1 and st2["report"] == name2
        assert st1["created_at"] and st2["created_at"]
        for name, st, a_name in ((name1, st1, f"{SA}.json"),
                                 (name2, st2, f"{SA2}.json")):
            resp = _run(mcp_server._download_endpoint(
                SimpleNamespace(path_params={"filename": name})))
            assert resp.status_code == 200
            served = json.loads((http_mode / name).read_text(encoding="utf-8"))
            assert served["corpus_a"]["name"] == a_name
            assert served["counts"] == st["counts"]
            assert served["created_at"] == st["created_at"]
            assert str(http_mode) not in json.dumps(st, ensure_ascii=False)

    def test_stdio_second_call_reports_its_own_file(self, stdio_dirs):
        out, elsewhere = stdio_dirs
        a1 = write_corpus(elsewhere, SA, A1)
        a2 = write_corpus(elsewhere, SA2, A2)
        b = write_corpus(elsewhere, SB, B)
        _b1, st1 = _call({"corpus_a": str(a1), "corpus_b": str(b)})
        blocks, st2 = _call({"corpus_a": str(a2), "corpus_b": str(b)})
        (name1, path1), = st1["files"].items()
        (name2, path2), = st2["files"].items()
        assert name1 == f"{SB}{DIFF_REPORT_SUFFIX}" and UNIQUE_RE.match(name2)
        assert Path(path2).parent == elsewhere
        assert json.loads(Path(path1).read_text("utf-8"))["corpus_a"]["name"] == a1.name
        assert json.loads(Path(path2).read_text("utf-8"))["corpus_a"]["name"] == a2.name
        assert name2 in blocks[0].text
        assert not list(out.iterdir())

    def test_unwritable_folder_message_without_path(self, stdio_dirs):
        out, elsewhere = stdio_dirs
        a = write_corpus(elsewhere, SA, A1)
        b = write_corpus(elsewhere, SB, B)
        os.chmod(elsewhere, 0o555)
        try:
            res = _call({"corpus_a": str(a), "corpus_b": str(b)})
        finally:
            os.chmod(elsewhere, 0o755)
        assert res.isError
        text = _text(res)
        assert "Écriture du rapport impossible" in text
        assert str(elsewhere) not in text


# === 3. Libellé des nouveaux dépôts HAL ===

class TestDepositLabel:
    def test_same_period_publication_is_not_called_earlier(self):
        item = art("X", date="2026", extra=["Submitted Date: 2026-09-10"])
        category, basis = cd.classify_added(item, REF_DAY)
        assert category == cd.CAT_NEW_DEPOSIT
        assert "même période" in basis
        label = cd.ADDED_LABELS[cd.CAT_NEW_DEPOSIT]
        assert "date imprécise" in label
        r = cd.diff_corpora([], [dict(item, DOI="10.1/d")],
                            context_a=dict(CTX_A))
        text = "\n".join(cd.summary_lines(r))
        assert "nouveaux dépôts HAL (publication antérieure) :" not in text
        assert "date imprécise" in text

    def test_earlier_publication_basis_unchanged(self):
        item = art("X", date="2018", extra=["Submitted Date: 2026-08-15"])
        category, basis = cd.classify_added(item, REF_DAY)
        assert category == cd.CAT_NEW_DEPOSIT
        assert basis.endswith("publication 2018")


# === 4. Requête d'un corpus ancien à nom tronqué ===

LONG_QUERY = ("AI bubble OR investment OR valuation OR capital expenditure "
              "OR hype cycle")


class TestLongQueryName:
    def test_slug_with_trailing_separator_matches(self):
        import main
        slug = main.slugify(LONG_QUERY)
        assert slug.endswith("_")                  # 60e caractère : séparateur
        stem = f"corpus_{slug}_20260922_214425_fc2119a7"
        legacy = {"query": None, "query_from_name": cd._query_from_name(stem)}
        harvest = {"query": LONG_QUERY}
        assert cd.queries_match(legacy, harvest)
        assert cd.queries_match(harvest, legacy)
        assert not cd.queries_match(legacy, {"query": "AI bubble"})

    def test_no_false_warning_between_legacy_and_manifest(self, tmp_path):
        import main
        slug = main.slugify(LONG_QUERY)
        items = [art("X", doi="10.1/x")]
        a = write_corpus(tmp_path, f"corpus_{slug}_20260922_214425_fc2119a7",
                         items)
        stem_b = f"corpus_{slug}_20260929_100000_abcdef12"
        manifest = {
            "format": "moisson-shs/manifest", "version": 1, "stem": stem_b,
            "kind": "moisson", "started_at": "2026-09-29T10:00:00",
            "finished_at": "2026-09-29T10:01:00",
            "query": {"text": LONG_QUERY, "mode": "collecte",
                      "sources": ["hal"], "from_date": None, "to_date": None,
                      "languages": []},
            "filters": {"from_date": None, "to_date": None, "languages": []},
            "settings": {"max_per_source": 200},
            "sources": {"HAL": {"source": "hal", "status": "complete",
                                "items": 1}},
            "files": [],
        }
        b = write_corpus(tmp_path, stem_b, items, manifest)
        codes = {w["code"] for w in
                 cd.diff_corpus_files(a, b, write=False).report["warnings"]}
        assert "requetes_differentes" not in codes
        assert "mode_inconnu" in codes


# === 5. Prompt de veille : repli sur un corpus ancien ===

class TestPromptLegacyFallback:
    def _prompt(self, theme="discours numérique"):
        res = _run(mcp_server.get_prompt("veille-mensuelle", {
            "theme": theme, "from_date": "2026-09-01",
            "to_date": "2026-09-30"}))
        return res.messages[0].content.text

    def test_fallback_on_a_legacy_corpus_is_explicit(self):
        text = self._prompt()
        step1 = text[text.index("1. "):text.index("2. ")]
        # Préférence : moisson complète de requête identique ; à défaut,
        # corpus ancien dont la requête d'après le nom correspond.
        assert step1.index("statut complet") < step1.index("À défaut")
        assert "ancien (métadonnées calculées)" in step1
        assert "Requête (d'après le nom)" in step1
        assert "accents" in step1 and "60 caractères" in step1
        assert "mode et ses sources sont inconnus" in step1
        # Toujours le Zotero JSON interne, jamais le CSL.
        assert ".csl.json" in step1

    def test_prompt_labels_match_corpus_list_output(self, stdio_dirs):
        out, _ = stdio_dirs
        write_corpus(out, "corpus_discours_numerique_20260523_112626",
                     [art("X", doi="10.1/x")])
        listing = _text(_run(mcp_server.call_tool(
            "corpus_list", {"pattern": "discours numérique"})))
        assert "corpus_discours_numerique_20260523_112626" in listing
        text = self._prompt()
        for label in ("ancien (métadonnées calculées)",
                      "Requête (d'après le nom)"):
            assert label in listing and label in text
