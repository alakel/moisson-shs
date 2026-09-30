"""
Tests OPT-19 — comparaison de deux corpus Zotero internes
(``utils.corpus_diff``). Hors réseau, sans socket, dossiers temporaires.

- Identité : DOI (champ ou ``extra``), identifiant HAL (ligne ``HAL ID`` ou
  URL de portail, version retirée), URL canonique (titres concordants
  exigés, comme la déduplication) ; garde-fou DOI (deux DOI différents
  jamais appariés) ; par défaut, régime d'égalité exacte de la
  déduplication seulement, ``fuzzy=True`` apparie aussi par similarité de
  titre, avec année et premier auteur.
- Compteurs : ajouts, retraits, communs ; enrichies (DOI, résumé, auteurs,
  date), modifiées, identiques ; la provenance seule (source de plus,
  extrait Google Scholar) n'enrichit pas.
- Symétrie : ``diff(B, A)`` échange ajouts et retraits et inverse chaque
  changement ; permutation des entrées sans effet.
- Ajouts : nouvelle publication, nouveau dépôt HAL, indéterminée, notice
  récupérée, sans date ; retraits : hors fenêtre, sources non interrogées,
  en échec, plafonnées.
- Avertissements lus dans les manifestes : mode bibliographie, modes
  différents, collecte plafonnée, corpus sans manifeste, requêtes, sources,
  filtres et plafonds différents, ordre chronologique, corpus dérivé.
- Entrées refusées proprement (CSL par le nom ou le contenu, manifeste,
  rapports, statistiques, export, JSON invalide), sans rien écrire.
- Rapport ``<radical>.diff.json`` : fichier associé, sans chemin absolu,
  complet, non listé par l'index, jamais remplacé par une autre comparaison
  (nom unique), sauf nom choisi.
"""

import json
import random
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from utils import corpus_diff as cd  # noqa: E402
from utils import manifest as mf  # noqa: E402
from utils.output_files import (  # noqa: E402
    DIFF_REPORT_SUFFIX, ROLE_DIFF_REPORT, corpus_kind, corpus_stem_of,
    file_role, is_associated_file, is_corpus_file, is_downloadable_file,
    is_publishable_file,
)


# === Fabriques ===

def art(title, date="2020", *, author="Dupont", doi=None, extra=None, **kw):
    item = {"itemType": kw.pop("itemType", "journalArticle"), "title": title,
            "date": date,
            "creators": ([{"creatorType": "author", "lastName": author,
                           "firstName": "Jean"}] if author else [])}
    if doi:
        item["DOI"] = doi
    lines = list(extra or [])
    if lines:
        item["extra"] = "\n".join(lines)
    item.update(kw)
    return item


REF_DAY = [2026, 8, 1]
CTX_A = {"name": "a.json", "items": 0, "harvest_day": REF_DAY,
         "harvest_date": "2026-08-01T10:00:00"}


def diff(a, b, **kw):
    kw.setdefault("context_a", dict(CTX_A))
    return cd.diff_corpora(a, b, **kw)


def manifest_doc(stem, *, date, mode="bibliographie", query="discours numérique",
                 sources=None, from_date=None, to_date=None, max_per_source=200,
                 kind="moisson", **extra):
    doc = {
        "format": "moisson-shs/manifest", "version": 1, "stem": stem,
        "kind": kind, "started_at": date, "finished_at": date,
        "query": {"text": query, "mode": mode,
                  "sources": sorted((sources or {}).keys()),
                  "from_date": from_date, "to_date": to_date,
                  "languages": [], "query_variants": {}},
        "filters": {"from_date": from_date, "to_date": to_date,
                    "languages": []},
        "settings": {"max_per_source": max_per_source},
        "sources": {label: dict(entry, source=entry.get("source", label.lower()))
                    for label, entry in (sources or {}).items()},
        "files": [],
    }
    doc.update(extra)
    return doc


def write_corpus(directory, stem, items, manifest=None):
    path = Path(directory) / f"{stem}.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    if manifest is not None:
        (Path(directory) / f"{stem}.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


HAL_OK = {"source": "hal", "status": "complete", "items": 1,
          "native_sort": "score desc"}
HAL_CAPPED = dict(HAL_OK, limit_reached=True)
XREF_OK = {"source": "crossref", "status": "complete", "items": 1}
XREF_ERR = {"source": "crossref", "status": "error", "reason": "network_error",
            "items": 0}

SA = "corpus_discours_numerique_20260801_100000_aaaaaaaa"
SB = "corpus_discours_numerique_20260901_100000_bbbbbbbb"


# === Identité ===

class TestIdentity:
    def test_match_by_doi_field_and_extra(self):
        a = [art("Livre", itemType="book", extra=["DOI: 10.1000/Livre"])]
        b = [art("Livre (réédition du titre)", itemType="book",
                 doi="https://doi.org/10.1000/livre")]
        r = diff(a, b)
        assert r["counts"]["unchanged"] == 1
        assert r["counts"]["added"] == r["counts"]["removed"] == 0

    def test_match_by_hal_id_line_and_portal_url(self):
        a = [art("Titre A", extra=["HAL ID: halshs-00367519v1"])]
        b = [art("Titre B tout autre", url="https://shs.hal.science/halshs-00367519v3")]
        r = diff(a, b)
        assert r["counts"]["unchanged"] == 1
        entry = (r["modified"] + r["enriched"])[0]
        assert entry["id_a"] == entry["id_b"] == "hal:halshs-00367519"

    def test_match_by_canonical_url(self):
        a = [art("Les écritures numériques",
                 url="https://www.revue.org/article/12?utm_source=x")]
        b = [art("Les écritures numériques", url="http://revue.org/article/12/")]
        r = diff(a, b)
        assert r["counts"]["unchanged"] == 1 and r["counts"]["identical"] == 1

    def test_url_with_discordant_titles_is_not_a_match(self):
        # Règle de la déduplication : un lien d'URL exige des titres
        # concordants (page de revue non reconnue comme générique).
        a = [art("Premier article du numéro", url="https://revue.org/numero/3")]
        b = [art("Un tout autre article", url="https://revue.org/numero/3")]
        r = diff(a, b)
        assert r["counts"]["unchanged"] == 0
        assert r["counts"]["added"] == r["counts"]["removed"] == 1

    def test_two_dois_are_never_matched(self):
        a = [art("Même titre", doi="10.1/a", url="https://ex.org/p/1")]
        b = [art("Même titre", doi="10.1/b", url="https://ex.org/p/1")]
        r = diff(a, b, fuzzy=True)
        assert r["counts"]["unchanged"] == 0
        assert r["counts"]["added"] == r["counts"]["removed"] == 1

    def test_no_fuzzy_by_default_fuzzy_on_request(self):
        # Titres différents une fois normalisés (singulier et pluriel) : la
        # passe floue seule les réunit. Un titre identique une fois
        # normalisé relève du régime exact, appliqué par défaut comme dans
        # la déduplication (tests/unit/test_opt19_review.py).
        a = [art("Une histoire sociale des écritures numériques ordinaires",
                 date="2021")]
        b = [art("Une histoire sociale des écritures numériques ordinaire",
                 date="2021")]
        r = diff(a, b)
        assert (r["counts"]["added"], r["counts"]["removed"]) == (1, 1)
        assert r["counts"]["without_keys_a"] == r["counts"]["without_keys_b"] == 1
        assert "sans_cle" in {w["code"] for w in r["warnings"]}
        r2 = diff(a, b, fuzzy=True)
        assert r2["counts"]["unchanged"] == 1
        assert r2["rules"]["fuzzy"] is True
        assert "sans_cle" not in {w["code"] for w in r2["warnings"]}

    def test_fuzzy_requires_author_corroboration(self):
        a = [art("Une histoire sociale des écritures numériques ordinaires",
                 author="Martin")]
        b = [art("Une histoire sociale des écritures numériques ordinaires",
                 author="Durand")]
        assert diff(a, b, fuzzy=True)["counts"]["unchanged"] == 0

    def test_ambiguous_record_stays_apart(self):
        a = [art("Version sans DOI", extra=["HAL ID: hal-01111111"])]
        b = [art("Prépublication", doi="10.1/pre", extra=["HAL ID: hal-01111111"]),
             art("Article", doi="10.1/pub", extra=["HAL ID: hal-01111111"])]
        r = diff(a, b)
        assert r["counts"]["removed"] == 1
        assert r["counts"]["ambiguous"] >= 1
        assert any("ambiguë" in n for n in r["removed"][0].get("notes", []))


# === Compteurs et changements ===

class TestChanges:
    def test_added_removed_unchanged_enriched(self):
        a = [art("Commun enrichi", date="2019", extra=["HAL ID: hal-01234567"]),
             art("Retiré", doi="10.1/old", date="2015"),
             art("Identique", doi="10.1/same")]
        b = [art("Commun enrichi", date="2019-03", doi="10.1/x",
                 abstractNote="Un vrai résumé d'éditeur.",
                 creators=[{"creatorType": "author", "lastName": "Dupont",
                            "firstName": "Jean"},
                           {"creatorType": "author", "lastName": "Martin",
                            "firstName": "Anne"}],
                 extra=["HAL ID: hal-01234567v2"]),
             art("Identique", doi="10.1/same"),
             art("Nouveau", doi="10.1/new", date="2026-09-10")]
        r = diff(a, b)
        c = r["counts"]
        assert (c["added"], c["removed"], c["unchanged"]) == (1, 1, 2)
        assert (c["enriched"], c["modified"], c["identical"]) == (1, 0, 1)
        entry = r["enriched"][0]
        changes = {ch["field"]: ch["change"] for ch in entry["changes"]}
        assert changes == {"DOI": "ajout", "abstract": "ajout",
                           "creators": "complément", "date": "complément"}
        assert set(entry["enriched_fields"]) == set(changes)
        assert c["enriched_fields"] == {"DOI": 1, "abstract": 1, "creators": 1,
                                        "date": 1}
        # Le résumé n'est jamais recopié : longueur seulement.
        abstract = next(ch for ch in entry["changes"] if ch["field"] == "abstract")
        assert "résumé d'éditeur" not in json.dumps(abstract, ensure_ascii=False)

    def test_modified_without_addition(self):
        a = [art("Titre initial", doi="10.1/m", pages="1-10", volume="3")]
        b = [art("Titre corrigé autrement", doi="10.1/m", pages="1-12")]
        r = diff(a, b)
        assert r["counts"]["modified"] == 1 and r["counts"]["enriched"] == 0
        changes = {ch["field"]: ch["change"] for ch in r["modified"][0]["changes"]}
        assert changes == {"title": "modification", "pages": "modification",
                           "volume": "retrait"}

    def test_case_and_punctuation_only_title_change_is_ignored(self):
        a = [art("Le Discours numérique", doi="10.1/t")]
        b = [art("le discours numérique.", doi="10.1/t")]
        assert diff(a, b)["counts"]["identical"] == 1

    def test_provenance_alone_does_not_enrich(self):
        a = [art("Titre", doi="10.1/p", extra=["Sources: HAL", "HAL ID: hal-02222222"])]
        b = [art("Titre", doi="10.1/p", extra=["Sources: HAL, OpenAlex",
                                               "HAL ID: hal-02222222",
                                               "OpenAlex ID: W123",
                                               "Cited by: 12"])]
        r = diff(a, b)
        assert r["counts"]["identical"] == 1 and r["counts"]["enriched"] == 0
        prov = {p["field"]: p for p in r["identical"][0]["provenance"]}
        assert prov["sources"]["change"] == "complément"
        assert prov["sources"]["b"] == "HAL, OpenAlex"
        assert prov["identifiers"]["added_keys"] == ["openalex:w123"]
        assert prov["citations"]["change"] == "ajout"

    def test_scholar_snippet_is_not_an_abstract(self):
        a = [art("Titre", doi="10.1/s", extra=["Sources: Google Scholar"])]
        b = [art("Titre", doi="10.1/s", abstractNote="… extrait de la page …",
                 extra=["Sources: Google Scholar",
                        "Scholar snippet: abstractNote = extrait Google Scholar"])]
        r = diff(a, b)
        assert r["counts"]["enriched"] == 0 and r["counts"]["identical"] == 1
        prov = {p["field"] for p in r["identical"][0]["provenance"]}
        assert "scholar_snippet" in prov

    def test_doi_added_by_resolver_is_flagged(self):
        a = [art("Titre", extra=["HAL ID: hal-03333333"])]
        b = [art("Titre", doi="10.1/r", extra=["HAL ID: hal-03333333",
                                               "DOI source: crossref titre 0.97"])]
        entry = diff(a, b)["enriched"][0]
        doi = next(ch for ch in entry["changes"] if ch["field"] == "DOI")
        assert "appariement" in doi["note"]

    def test_multiple_match_is_reported(self):
        a = [art("Titre", extra=["HAL ID: hal-04444444"]),
             art("Titre", url="https://hal.science/hal-04444444")]
        b = [art("Titre", extra=["HAL ID: hal-04444444"])]
        r = diff(a, b)
        assert r["counts"]["unchanged"] == 1
        assert r["counts"]["multiple_matches"] == 1
        assert r["counts"]["internal_duplicates_a"] == 1


# === Symétrie et déterminisme ===

def _mixed_corpora():
    a = [art("Commun", date="2019", extra=["HAL ID: hal-05555555"]),
         art("Retiré", doi="10.1/old"),
         art("Modifié", doi="10.1/mod", volume="2"),
         art("URL", url="https://revue.org/a/1")]
    b = [art("Commun", date="2019-02", doi="10.1/c",
             extra=["HAL ID: hal-05555555v2", "Sources: HAL, CrossRef"]),
         art("Modifié", doi="10.1/mod", volume="3"),
         art("URL", url="http://www.revue.org/a/1/"),
         art("Ajouté", doi="10.1/new", date="2026-10")]
    return a, b


class TestSymmetry:
    def test_swap_exchanges_added_and_removed_and_inverts_changes(self):
        a, b = _mixed_corpora()
        ab = cd.diff_corpora(a, b)
        ba = cd.diff_corpora(b, a)
        assert {e["id"] for e in ab["added"]} == {e["id"] for e in ba["removed"]}
        assert {e["id"] for e in ab["removed"]} == {e["id"] for e in ba["added"]}
        assert ab["counts"]["unchanged"] == ba["counts"]["unchanged"]

        def common(report):
            out = {}
            for bucket in ("enriched", "modified", "identical"):
                for e in report[bucket]:
                    out[frozenset((e["id_a"], e["id_b"]))] = e
            return out

        ca, cb = common(ab), common(ba)
        assert set(ca) == set(cb)
        for key, entry in ca.items():
            forward = {(c["field"], c["change"], c.get("a"), c.get("b"))
                       for c in entry.get("changes", [])}
            backward = {(c["field"], c["change"], c.get("a"), c.get("b"))
                        for c in (cd.invert_change(x) for x in
                                  cb[key].get("changes", []))}
            assert forward == backward
            fprov = {(c["field"], c["change"]) for c in entry.get("provenance", [])}
            bprov = {(c["field"], c["change"]) for c in
                     (cd.invert_change(x) for x in cb[key].get("provenance", []))}
            assert fprov == bprov
        # Enrichie dans un sens, modifiée (réduite) dans l'autre.
        assert ab["counts"]["enriched"] == 1 and ba["counts"]["enriched"] == 0

    def test_input_permutation_does_not_change_the_result(self):
        a, b = _mixed_corpora()
        ref = cd.diff_corpora(a, b)
        rng = random.Random(19)
        for _ in range(10):
            pa, pb = a[:], b[:]
            rng.shuffle(pa)
            rng.shuffle(pb)
            r = cd.diff_corpora(pa, pb)
            for key in ("added", "removed", "unchanged", "enriched",
                        "modified", "identical"):
                assert r["counts"][key] == ref["counts"][key]
            assert sorted(e["id"] for e in r["added"]) == \
                sorted(e["id"] for e in ref["added"])
            assert sorted(e["id_b"] for e in r["enriched"]) == \
                sorted(e["id_b"] for e in ref["enriched"])

    def test_self_diff_is_all_identical(self, tmp_path):
        a, _ = _mixed_corpora()
        path = write_corpus(tmp_path, SA, a)
        res = cd.diff_corpus_files(path, path, write=False)
        c = res.report["counts"]
        assert c["added"] == c["removed"] == 0
        assert c["identical"] == c["unchanged"] == len(a)
        assert "meme_contenu" in {w["code"] for w in res.report["warnings"]}


# === Classement des ajouts et des retraits ===

class TestClassification:
    @pytest.mark.parametrize("date,extra,expected", [
        ("2026-09-10", None, cd.CAT_NEW_PUBLICATION),
        ("2026-09", None, cd.CAT_NEW_PUBLICATION),
        ("2027", None, cd.CAT_NEW_PUBLICATION),
        ("2026-08", None, cd.CAT_UNDETERMINED),
        ("2026", None, cd.CAT_UNDETERMINED),
        ("2026-08-01", None, cd.CAT_UNDETERMINED),
        ("2026-07-31", None, cd.CAT_RETRIEVED),
        ("2019", None, cd.CAT_RETRIEVED),
        ("2018", ["Submitted Date: 2026-08-15 09:12:00"], cd.CAT_NEW_DEPOSIT),
        ("2018", ["Submitted Date: 2020-01-01"], cd.CAT_RETRIEVED),
        ("", ["Submitted Date: 2026-09-02"], cd.CAT_NEW_DEPOSIT),
        ("", None, cd.CAT_UNDATED),
    ])
    def test_added_categories(self, date, extra, expected):
        item = art("X", date=date, extra=extra)
        category, basis = cd.classify_added(item, REF_DAY)
        assert category == expected
        assert basis

    def test_added_are_listed_new_publications_first(self):
        b = [art("Ancienne", doi="10.1/1", date="2010"),
             art("Nouvelle", doi="10.1/2", date="2026-09-20"),
             art("Plus récente", doi="10.1/3", date="2026-10-01"),
             art("Sans date", doi="10.1/4", date="")]
        r = diff([], b)
        assert [e["title"] for e in r["added"]] == \
            ["Plus récente", "Nouvelle", "Ancienne", "Sans date"]
        assert r["counts"]["added_by_category"] == {
            "nouvelle_publication": 2, "nouveau_depot": 0, "indeterminee": 0,
            "notice_recuperee": 1, "sans_date": 1}

    def test_removed_reasons(self):
        a = [art("Hors fenêtre", doi="10.1/w", date="2019",
                 extra=["Sources: HAL"]),
             art("Source absente", doi="10.1/s", date="2026-09-05",
                 extra=["Sources: Semantic Scholar"]),
             art("Source en échec", doi="10.1/e", date="2026-09-05",
                 extra=["Sources: CrossRef"]),
             art("Source plafonnée", doi="10.1/p", date="2026-09-05",
                 extra=["Sources: HAL"]),
             art("Plafonnée et OK", doi="10.1/q", date="2026-09-05",
                 extra=["Sources: HAL, OpenAlex"])]
        ctx_b = {"name": "b.json", "items": 0,
                 "filters": {"from_date": "2026-09-01", "to_date": None},
                 "sources": [
                     {"source": "hal", "label": "HAL", "status": "complete",
                      "limit_reached": True, "native_sort": "score desc"},
                     {"source": "crossref", "label": "CrossRef",
                      "status": "error", "reason": "network_error"},
                     {"source": "openalex", "label": "OpenAlex",
                      "status": "complete"}]}
        r = diff(a, [], context_b=ctx_b)
        reasons = {e["title"]: e["reason"] for e in r["removed"]}
        assert reasons == {
            "Hors fenêtre": cd.REM_OUTSIDE_WINDOW,
            "Source absente": cd.REM_SOURCES_ABSENT,
            "Source en échec": cd.REM_SOURCES_FAILED,
            "Source plafonnée": cd.REM_TRUNCATED,
            "Plafonnée et OK": cd.REM_ABSENT,
        }
        assert r["counts"]["removed_by_reason"][cd.REM_OUTSIDE_WINDOW] == 1

    def test_added_notes_outside_window_of_a(self):
        ctx_a = dict(CTX_A, filters={"from_date": "2026-08-01", "to_date": "2026-08-31"})
        r = diff([], [art("Plus ancien", doi="10.1/o", date="2024")],
                 context_a=ctx_a)
        entry = r["added"][0]
        assert entry["category"] == cd.CAT_RETRIEVED
        assert any("hors de la fenêtre" in n for n in entry["notes"])

    def test_partial_window_is_not_outside(self):
        assert not cd.outside_window((2026,), "2026-09-01", None)
        assert cd.outside_window((2026, 8, 31), "2026-09-01", None)
        assert cd.outside_window((2027,), None, "2026-12-31")
        assert not cd.outside_window(None, "2026", "2026")

    def test_date_parsing(self):
        assert cd.parse_date("2020-05-15") == (2020, 5, 15)
        assert cd.parse_date("2020-5") == (2020, 5)
        assert cd.parse_date("15/05/2020") == (2020,)
        assert cd.parse_date("2020-13-01") == (2020,)
        assert cd.parse_date("") is None and cd.parse_date(None) is None


# === Contexte des moissons et avertissements ===

def _codes(report):
    return {w["code"] for w in report["warnings"]}


class TestWarnings:
    def test_bibliographie_mode_is_recalled_with_capped_sources(self, tmp_path):
        a = write_corpus(tmp_path, SA, [art("X", doi="10.1/x")],
                         manifest_doc(SA, date="2026-08-01T10:00:00+02:00",
                                      sources={"HAL": HAL_CAPPED}))
        b = write_corpus(tmp_path, SB, [art("X", doi="10.1/x")],
                         manifest_doc(SB, date="2026-09-01T10:00:00+02:00",
                                      sources={"HAL": HAL_CAPPED}))
        r = cd.diff_corpus_files(a, b, write=False).report
        assert "mode_bibliographie" in _codes(r)
        msg = next(w["message"] for w in r["warnings"]
                   if w["code"] == "mode_bibliographie")
        assert "score desc" in msg and "HAL" in msg
        assert r["corpus_a"]["mode"] == "bibliographie"
        assert r["corpus_a"]["sources"][0]["native_sort"] == "score desc"
        assert r["corpus_a"]["harvest_date"].startswith("2026-08-01")
        assert r["corpus_a"]["harvest_date_basis"] == "manifeste"
        assert "modes_differents" not in _codes(r)

    def test_different_modes_and_collecte_cap(self, tmp_path):
        capped = {"source": "hal", "status": "complete", "limit_reached": True,
                  "native_sort": "docid asc"}
        a = write_corpus(tmp_path, SA, [art("X", doi="10.1/x")],
                         manifest_doc(SA, date="2026-08-01T10:00:00+02:00",
                                      mode="collecte", sources={"HAL": capped}))
        b = write_corpus(tmp_path, SB, [art("X", doi="10.1/x")],
                         manifest_doc(SB, date="2026-09-01T10:00:00+02:00",
                                      sources={"HAL": HAL_OK}))
        codes = _codes(cd.diff_corpus_files(a, b, write=False).report)
        assert {"modes_differents", "mode_bibliographie", "collecte_plafonnee",
                "tris_differents"} <= codes

    def test_collecte_without_cap_has_no_selection_warning(self, tmp_path):
        ok = {"source": "hal", "status": "complete", "native_sort": "docid asc"}
        a = write_corpus(tmp_path, SA, [art("X", doi="10.1/x")],
                         manifest_doc(SA, date="2026-08-01T10:00:00+02:00",
                                      mode="collecte", sources={"HAL": ok}))
        b = write_corpus(tmp_path, SB, [art("X", doi="10.1/x")],
                         manifest_doc(SB, date="2026-09-01T10:00:00+02:00",
                                      mode="collecte", sources={"HAL": ok}))
        codes = _codes(cd.diff_corpus_files(a, b, write=False).report)
        assert not codes & {"mode_bibliographie", "modes_differents",
                            "collecte_plafonnee", "mode_inconnu",
                            "requetes_differentes", "tris_differents"}

    def test_legacy_corpus_mode_unknown_and_query_from_name(self, tmp_path):
        a = write_corpus(tmp_path, "corpus_discours_numerique_20250101_120000",
                         [art("X", doi="10.1/x")])
        b = write_corpus(tmp_path, SB, [art("X", doi="10.1/x")],
                         manifest_doc(SB, date="2026-09-01T10:00:00+02:00",
                                      sources={"HAL": HAL_OK}))
        r = cd.diff_corpus_files(a, b, write=False).report
        assert "mode_inconnu" in _codes(r)
        assert r["corpus_a"]["mode"] is None
        assert r["corpus_a"]["query_from_name"] == "discours numerique"
        # Requête du nom (sans accents) = requête du manifeste : même veille.
        assert "requetes_differentes" not in _codes(r)
        # Date de référence : horodatage du nom.
        assert r["corpus_a"]["harvest_date_basis"] == "nom"
        assert r["corpus_a"]["harvest_date"].startswith("2025-01-01")
        # Filtres inconnus d'un côté : jamais présentés comme différents.
        assert "filtres_differents" not in _codes(r)

    def test_query_sources_filters_caps_and_order(self, tmp_path):
        a = write_corpus(tmp_path, SA, [art("X", doi="10.1/x")],
                         manifest_doc(SA, date="2026-10-01T10:00:00+02:00",
                                      query="humanités numériques",
                                      sources={"HAL": HAL_OK, "CrossRef": XREF_ERR},
                                      from_date="2026-08-01", max_per_source=100))
        b = write_corpus(tmp_path, SB, [art("X", doi="10.1/x")],
                         manifest_doc(SB, date="2026-09-01T10:00:00+02:00",
                                      sources={"HAL": HAL_OK,
                                               "OpenAlex": dict(XREF_OK,
                                                                source="openalex")},
                                      from_date="2026-09-01"))
        r = cd.diff_corpus_files(a, b, write=False).report
        assert {"requetes_differentes", "sources_differentes",
                "filtres_differents", "plafonds_differents",
                "sources_incompletes", "ordre_chronologique"} <= _codes(r)

    def test_derived_corpus_inherits_harvest_context(self, tmp_path):
        src = write_corpus(tmp_path, SA, [art("X", doi="10.1/x")],
                           manifest_doc(SA, date="2026-08-01T10:00:00+02:00",
                                        mode="collecte",
                                        sources={"HAL": HAL_OK}))
        filt_stem = SA + "_filtered"
        filt_manifest = {
            "format": "moisson-shs/manifest", "version": 1, "stem": filt_stem,
            "kind": "filtre", "started_at": "2026-08-20T10:00:00+02:00",
            "finished_at": "2026-08-20T10:00:00+02:00",
            "query": manifest_doc(SA, date="x")["query"],
            "inputs": [{"name": src.name, "items": 1,
                        "manifest": f"{SA}.manifest.json"}],
            "files": []}
        filt = write_corpus(tmp_path, filt_stem, [art("X", doi="10.1/x")],
                            filt_manifest)
        b = write_corpus(tmp_path, SB, [art("X", doi="10.1/x")],
                         manifest_doc(SB, date="2026-09-01T10:00:00+02:00",
                                      mode="collecte", sources={"HAL": HAL_OK}))
        r = cd.diff_corpus_files(filt, b, write=False).report
        ctx = r["corpus_a"]
        assert ctx["kind"] == "filtre"
        assert ctx["inherited_from"] == f"{SA}.manifest.json"
        assert ctx["mode"] == "collecte"
        # Date de référence : celle de la moisson d'origine.
        assert ctx["harvest_date"].startswith("2026-08-01")
        assert "corpus_derive" in _codes(r)


# === Entrées refusées ===

class TestInputs:
    def _ok(self, tmp_path):
        return write_corpus(tmp_path, SB, [art("X", doi="10.1/x")])

    @pytest.mark.parametrize("name,content,fragment", [
        ("corpus_x.csl.json", [{"type": "article-journal", "title": "X"}],
         "CSL-JSON"),
        ("corpus_x.json", [{"type": "article-journal", "title": "X"}],
         "CSL-JSON"),
        ("corpus_x.manifest.json", {"format": "moisson-shs/manifest",
                                    "version": 1}, "manifeste"),
        ("corpus_x.doi_report.json", {"counts": {}}, "rapport de résolution DOI"),
        ("corpus_x.diff.json", {"format": "moisson-shs/diff"},
         "rapport de comparaison"),
        ("renomme.json", {"format": "moisson-shs/diff", "version": 1},
         "rapport de comparaison"),
        ("corpus_x.json", {"items": []}, "pas un corpus Zotero"),
        ("corpus_x.json", [{"title": "sans type"}], "pas un corpus Zotero"),
    ])
    def test_refused_without_writing(self, tmp_path, name, content, fragment):
        bad = tmp_path / name
        bad.write_text(json.dumps(content), encoding="utf-8")
        ok = self._ok(tmp_path)
        before = sorted(p.name for p in tmp_path.iterdir())
        for a, b, label in ((bad, ok, "corpus_a"), (ok, bad, "corpus_b")):
            with pytest.raises(cd.CorpusDiffInputError) as exc:
                cd.diff_corpus_files(a, b)
            message = str(exc.value)
            assert fragment in message and label in message
            assert str(tmp_path) not in message
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    @pytest.mark.parametrize("name,data", [
        ("corpus_x_stats.txt", "Requête : x"),
        ("corpus_x.bib", "@misc{a,}"),
        ("corpus_x.json", "{pas du json"),
    ])
    def test_non_corpus_files_refused(self, tmp_path, name, data):
        bad = tmp_path / name
        bad.write_text(data, encoding="utf-8")
        with pytest.raises(cd.CorpusDiffInputError):
            cd.diff_corpus_files(bad, self._ok(tmp_path))

    def test_empty_zotero_corpus_is_accepted(self, tmp_path):
        empty = write_corpus(tmp_path, SA, [])
        r = cd.diff_corpus_files(empty, self._ok(tmp_path), write=False).report
        assert r["counts"]["added"] == 1 and r["counts"]["removed"] == 0

    @pytest.mark.parametrize("name", [
        "rapport.json", ".cache.diff.json", ".diff.json",
        "rapport.diff.json.bak", "rapport.diff.txt", "x/.diff.json"])
    def test_report_name_refused(self, name):
        with pytest.raises(cd.CorpusDiffInputError):
            cd.check_report_output(Path(name))

    @pytest.mark.parametrize("name", [
        "/tmp/veille_sept.DIFF.json", "sous/dossier/veille.diff.json"])
    def test_report_name_accepted(self, name):
        # Le contrôle porte sur le NOM ; le dossier est confiné ailleurs
        # (_safe_path et contrôle du dossier en HTTP, dans mcp_server).
        cd.check_report_output(Path(name))


# === Rapport ===

class TestReport:
    def test_report_next_to_b_without_absolute_paths(self, tmp_path):
        d = tmp_path / "ailleurs"
        d.mkdir()
        a_items, b_items = _mixed_corpora()
        a = write_corpus(tmp_path, SA, a_items,
                         manifest_doc(SA, date="2026-08-01T10:00:00+02:00",
                                      sources={"HAL": HAL_OK}))
        b = write_corpus(d, SB, b_items)
        res = cd.diff_corpus_files(a, b)
        assert res.report_path == d / f"{SB}{DIFF_REPORT_SUFFIX}"
        doc = json.loads(res.report_path.read_text(encoding="utf-8"))
        assert doc["format"] == cd.DIFF_FORMAT and doc["version"] == 1
        assert doc["report"] == res.report_path.name
        assert doc["rules"]["version"] == cd.DIFF_RULES_VERSION
        assert not mf.absolute_paths_in(doc)
        assert str(tmp_path) not in res.report_path.read_text(encoding="utf-8")
        import hashlib
        assert doc["corpus_a"]["sha256"] == hashlib.sha256(a.read_bytes()).hexdigest()
        assert doc["corpus_a"]["name"] == a.name
        assert doc["corpus_b"]["manifest"] is None
        # Listes complètes (non bornées par l'échantillon).
        assert len(doc["added"]) == doc["counts"]["added"]
        assert len(doc["identical"]) == doc["counts"]["identical"]
        # Aucun temporaire laissé.
        assert not [p for p in d.iterdir() if p.name.startswith(".")]

    def test_default_report_is_never_replaced(self, tmp_path):
        # Revue d'OPT-19 : une comparaison ne remplace jamais le rapport
        # d'une autre (le lien rendu par le premier appel servirait sinon le
        # rapport du second) ; nom unique pour la seconde.
        a_items, b_items = _mixed_corpora()
        a = write_corpus(tmp_path, SA, a_items)
        other = write_corpus(tmp_path, SA.replace("aaaa", "cccc"), b_items)
        b = write_corpus(tmp_path, SB, b_items)
        first = cd.diff_corpus_files(a, b).report_path
        second = cd.diff_corpus_files(other, b).report_path
        assert first.name == f"{SB}{DIFF_REPORT_SUFFIX}"
        assert second != first and second.name.startswith(f"{SB}_")
        assert json.loads(first.read_text(encoding="utf-8"))["corpus_a"]["name"] == a.name
        doc = json.loads(second.read_text(encoding="utf-8"))
        assert doc["corpus_a"]["name"] == other.name
        assert doc["counts"]["identical"] == len(b_items)

    def test_chosen_report_name_is_replaced_atomically(self, tmp_path):
        a_items, b_items = _mixed_corpora()
        a = write_corpus(tmp_path, SA, a_items)
        other = write_corpus(tmp_path, SA.replace("aaaa", "cccc"), b_items)
        b = write_corpus(tmp_path, SB, b_items)
        target = tmp_path / "veille.diff.json"
        first = cd.diff_corpus_files(a, b, report_path=target).report_path
        second = cd.diff_corpus_files(other, b, report_path=target).report_path
        assert first == second == target
        doc = json.loads(second.read_text(encoding="utf-8"))
        assert doc["corpus_a"]["name"] == other.name
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

    def test_large_lists_are_complete_and_samples_bounded(self, tmp_path):
        b_items = [art(f"Notice {i}", doi=f"10.1/{i}", date="2026-09-15")
                   for i in range(150)]
        a = write_corpus(tmp_path, SA, [])
        b = write_corpus(tmp_path, SB, b_items)
        res = cd.diff_corpus_files(a, b)
        assert len(res.report["added"]) == 150
        st = cd.structured_summary(res.report, 500)
        assert st["sample_size"] == cd.SAMPLE_MAX
        assert len(st["samples"]["added"]) == cd.SAMPLE_MAX
        st = cd.structured_summary(res.report, 5)
        assert len(st["samples"]["added"]) == 5
        text = "\n".join(cd.summary_lines(res.report, 50))
        assert text.count("— nouvelle publication") == cd.TEXT_SAMPLE_MAX

    def test_report_file_classification_and_index(self, tmp_path):
        name = f"{SB}{DIFF_REPORT_SUFFIX}"
        assert is_associated_file(name) and is_associated_file(name.upper())
        assert not is_corpus_file(name) and corpus_kind(name) is None
        assert not is_publishable_file(name)
        assert is_downloadable_file(name)
        assert file_role(name) == ROLE_DIFF_REPORT
        assert corpus_stem_of(name) == SB
        a = write_corpus(tmp_path, SA, [art("X", doi="10.1/x")])
        b = write_corpus(tmp_path, SB, [art("Y", doi="10.1/y")],
                         manifest_doc(SB, date="2026-09-01T10:00:00+02:00",
                                      sources={"HAL": HAL_OK},
                                      files=[]))
        mf.list_corpora(tmp_path, rebuild=True)           # index construit
        cd.diff_corpus_files(a, b)
        listing = mf.list_corpora(tmp_path)
        stems = [e["stem"] for e in listing.entries]
        assert sorted(stems) == sorted([SA, SB])
        for entry in listing.entries:
            roles = {info["role"] for info in entry["files"].values()}
            if entry["stem"] == SB:
                assert ROLE_DIFF_REPORT in roles
                assert entry["status"] == "complete"
            assert entry["stem"] != f"{SB}.diff"
