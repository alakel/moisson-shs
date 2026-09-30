"""
Tests OPT-13 — classement explicable (``utils.ranking``), sans réseau.

- classes d'ordre natif d'après le tri réellement appliqué (pertinence
  vérifiée, ordre présumé d'Isidore, autres ordres, sources sans rang) ;
- fusion de rangs réciproques (k = 60) ; proxies d'Isidore comptés comme UN
  fournisseur (invariant 6) ; queue ordonnée par l'ordre natif ;
- reproductibilité : même entrée, même classement ; permutation des notices,
  des notices de chaque source et de l'ordre des sources sans effet ;
- justification pour chaque notice du top 20 ; réserves (résumé absent,
  extrait Scholar, identité floue ou ambiguë, rang non rattaché, source
  partielle, terme exclu, ordre présumé, absence de rang de pertinence) ;
- citations hors du score principal : une notice très citée hors sujet ne
  passe pas devant une notice centrale ;
- concepts du plan de requête (expressions exactes, alternatives,
  exclusions, variantes, mots vides, expression implicite), pondération
  simple et BM25 (formule vérifiée à la main), comparaison des deux ;
- rattachement des rangs aux notices fusionnées : clés fortes et repli sur
  le titre, contrôlés contre les groupes exacts de la déduplication (jeu
  témoin de 120 paires), ambiguïté jamais résolue au hasard ;
- invariant 1 : aucune notice modifiée.
"""

from __future__ import annotations

import copy
import json
import math
import random
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from engine.models import SourceResult  # noqa: E402
from utils import ranking  # noqa: E402
from utils.deduplication import deduplicate_with_report  # noqa: E402
from utils.query_plan import build_query_plan, parse_query  # noqa: E402
from utils.ranking import (  # noqa: E402
    CLASS_OTHER, CLASS_PRESUMED, CLASS_RELEVANCE, CLASS_UNRANKED,
    RANKING_VERSION, RRF_K, _source_inputs, classify_sort,
    concepts_from_parsed, map_source_items, rank_records, record_id,
    resolve_ranking_mode, tokens, top_ranked_payload,
)

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "dedup_pairs.json"

HAL_SORT = "score desc,docid asc"


# === Fabrique de notices ===

def art(title, *, doi=None, year="2020", author="Dupont", abstract="",
        tags=(), sources=("HAL",), lines=(), url=None,
        item_type="journalArticle"):
    item = {
        "itemType": item_type,
        "title": title,
        "creators": [{"creatorType": "author", "firstName": "Anne",
                      "lastName": author}],
        "date": year,
        "abstractNote": abstract,
        "tags": [{"tag": t} for t in tags],
    }
    if doi:
        item["DOI"] = doi
    if url:
        item["url"] = url
    item["extra"] = "\n".join([f"Sources: {', '.join(sources)}", *lines])
    return item


def src(key, items, sort=None, ranks=None, status="complete", reason=None):
    """SourceResult aligné : rangs 1..n (ou fournis) pour les sources qui en
    publient."""
    result = SourceResult(source=key, display_name=ranking.display_name(key),
                          items=list(items), status=status, reason=reason)
    if sort is not None:
        result.native_sort = sort
        if ranks is None:
            ranks = list(range(1, len(items) + 1))
        result.native_ranks = [{"rank": r, "score": None, "sort": sort}
                               for r in ranks]
        result.bind_ranks()
    return result


def isidore_item(n, title, label="Isidore", **kw):
    return art(title, sources=(label,), author=kw.pop("author", "Martin"),
               lines=(f"Isidore URI: https://isidore.science/document/10670/1.{n:06d}",),
               **kw)


def entries_by_id(result):
    return {e["id"]: e for e in result["entries"]}


def order_signature(result, records):
    """Ordre du classement exprimé par le contenu des notices (indépendant
    des positions dans la liste d'entrée)."""
    return [json.dumps(records[e["index"]], sort_keys=True, ensure_ascii=False)
            for e in result["entries"]]


def strip_durations(result):
    out = copy.deepcopy(result)
    out.pop("duration_s", None)
    return out


# === Mode demandé ===

class TestResolveMode:
    def test_default_is_native(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "RANKING_DEFAULT", "native")
        assert resolve_ranking_mode(None) == "native"

    def test_config_value_used(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "RANKING_DEFAULT", "local")
        assert resolve_ranking_mode(None) == "local"

    def test_invalid_config_falls_back_to_native(self, monkeypatch, caplog):
        import config
        monkeypatch.setattr(config, "RANKING_DEFAULT", "pertinence")
        assert resolve_ranking_mode(None) == "native"
        assert "MOISSON_RANKING" in caplog.text

    @pytest.mark.parametrize("value", ["native", "local", "off", " LOCAL "])
    def test_explicit_values(self, value):
        assert resolve_ranking_mode(value) == value.strip().lower()

    def test_invalid_explicit_value_is_refused(self):
        with pytest.raises(ValueError):
            resolve_ranking_mode("bm25")

    def test_rank_records_refuses_off_and_unknown_model(self):
        with pytest.raises(ValueError):
            rank_records([], [], mode="off")
        with pytest.raises(ValueError):
            rank_records([], [], mode="local", lexical_model="tfidf")


# === Classes d'ordre natif ===

class TestSortClasses:
    @pytest.mark.parametrize("sort, expected", [
        ("score desc,docid asc", CLASS_RELEVANCE),   # HAL bibliographie
        ("score:desc", CLASS_RELEVANCE),             # CrossRef bibliographie
        ("relevance_score:desc", CLASS_RELEVANCE),   # OpenAlex
        ("isidore_default", CLASS_PRESUMED),         # Isidore (non vérifié)
        ("docid asc", CLASS_OTHER),                  # HAL collecte
        ("cursor_default", CLASS_OTHER),             # CrossRef collecte
        ("paperId", CLASS_OTHER),                    # S2 collecte
        ("citationCount:desc", CLASS_OTHER),         # S2 bibliographie
        (None, CLASS_UNRANKED),
    ])
    def test_classify(self, sort, expected):
        assert classify_sort(sort) == expected

    def test_source_without_ranks_is_unranked_with_positions(self):
        items = [art("a"), art("b")]
        (inp,) = _source_inputs([src("core", items)])
        assert inp.rank_class == CLASS_UNRANKED
        assert [r["rank"] for r in inp.ranks] == [1, 2]

    def test_misaligned_ranks_are_dropped(self):
        items = [art("a"), art("b")]
        result = SourceResult(source="hal", display_name="HAL", items=items)
        result.native_sort = HAL_SORT
        result.native_ranks = [{"rank": 1, "score": 3.0, "sort": HAL_SORT}]
        (inp,) = _source_inputs([result])
        assert inp.rank_class == CLASS_UNRANKED

    def test_subsources_share_the_isidore_provider(self):
        for key in ("isidore", "openedition", "cairn", "persee", "erudit",
                    "theses_fr"):
            assert ranking.provider_of(key) == "isidore"
        assert ranking.provider_of("hal") == "hal"
        assert ranking.provider_of("crossref") == "crossref"


# === Fusion de rangs réciproques ===

class TestNativeRRF:
    def test_rrf_formula_and_weights(self):
        a, b, c = art("Alpha", doi="10.1/a"), art("Beta", doi="10.1/b"), \
            isidore_item(1, "Gamma")
        result = rank_records(
            [a, b, c],
            [src("hal", [a, b], HAL_SORT, [1, 3]),
             src("isidore", [c], "isidore_default", [2])])
        by_id = entries_by_id(result)
        assert by_id["doi:10.1/a"]["score"] == pytest.approx(1 / (RRF_K + 1))
        assert by_id["doi:10.1/b"]["score"] == pytest.approx(1 / (RRF_K + 3))
        gamma = next(e for e in result["entries"] if e["id"].startswith("isidore:"))
        assert gamma["score"] == pytest.approx(0.5 / (RRF_K + 2))
        assert [e["rank"] for e in result["entries"]] == [1, 2, 3]
        assert result["entries"][0]["id"] == "doi:10.1/a"
        assert result["version"] == RANKING_VERSION
        assert result["mode"] == "native"

    def test_multi_provider_sum(self):
        shared_hal = art("Commun", doi="10.1/x", sources=("HAL",))
        shared_cr = art("Commun", doi="10.1/x", sources=("CrossRef",))
        records, _ = deduplicate_with_report([shared_hal, shared_cr])
        result = rank_records(records, [src("hal", [shared_hal], HAL_SORT, [5]),
                                        src("crossref", [shared_cr], "score:desc", [7])])
        (entry,) = result["entries"]
        assert entry["score"] == pytest.approx(1 / 65 + 1 / 67)
        assert entry["dimensions"]["sources"]["independent"] == 2

    def test_non_relevance_orders_go_to_the_queue(self):
        ranked = art("Classée", doi="10.1/r")
        docid_first = art("Premier docid", doi="10.1/d1")
        docid_second = art("Second docid", doi="10.1/d2")
        cited = art("Très citée", doi="10.1/c", sources=("Semantic Scholar",),
                    lines=("Cited by: 99999",))
        result = rank_records(
            [docid_second, cited, docid_first, ranked],
            [src("hal", [docid_first, docid_second], "docid asc"),
             src("semantic_scholar", [cited], "citationCount:desc"),
             src("openalex", [ranked], "relevance_score:desc", [180])])
        ids = [e["id"] for e in result["entries"]]
        # La seule notice classée par pertinence passe devant, même au rang
        # 180 ; la queue suit l'ordre natif neutre (docid : position 1, 2) ;
        # l'ordre de Semantic Scholar par citations n'ordonne pas la queue
        # (revue d'OPT-13) : la notice citée 99 999 fois vient après.
        assert ids == ["doi:10.1/r", "doi:10.1/d1", "doi:10.1/d2", "doi:10.1/c"]
        queue = result["entries"][1:]
        assert all(e["score"] == 0 for e in queue)
        codes = [{r["code"] for r in e["justification"]["reserves"]} for e in queue]
        assert all("no_relevance_rank" in c for c in codes)
        assert result["counts"] == {"records": 4,
                                    "with_verified_relevance_rank": 1,
                                    "presumed_only": 0, "queue": 3}

    def test_ties_are_broken_by_identifier(self):
        a = art("Un", doi="10.1/b")
        b = art("Deux", doi="10.1/a")
        result = rank_records([a, b], [src("hal", [a], HAL_SORT, [4]),
                                       src("crossref", [b], "score:desc", [4])])
        assert [e["id"] for e in result["entries"]] == ["doi:10.1/a", "doi:10.1/b"]

    def test_best_rank_per_source_for_merged_duplicates(self):
        # Deux notices HAL du même travail fusionnées : un seul terme HAL, au
        # meilleur rang.
        h1 = art("Même travail", doi="10.1/m", url="https://hal.science/hal-01",
                 sources=("HAL",))
        h2 = art("Même travail", doi="10.1/m", url="https://hal.science/hal-02",
                 sources=("HAL",))
        records, _ = deduplicate_with_report([h1, h2])
        assert len(records) == 1
        result = rank_records(records, [src("hal", [h1, h2], HAL_SORT, [9, 2])])
        (entry,) = result["entries"]
        assert entry["score"] == pytest.approx(1 / 62)
        assert len(entry["components"]["native_contributions"]) == 1


# === Proxies d'Isidore (invariant 6) ===

class TestIsidoreProxies:
    def _family(self):
        direct = isidore_item(7, "Les revues savantes en ligne", label="Isidore")
        cairn = isidore_item(7, "Les revues savantes en ligne", label="Cairn")
        oe = isidore_item(7, "Les revues savantes en ligne", label="OpenEdition")
        return direct, cairn, oe

    def test_proxies_count_once(self):
        direct, cairn, oe = self._family()
        records, _ = deduplicate_with_report([direct, cairn, oe])
        assert len(records) == 1
        result = rank_records(records, [
            src("isidore", [direct], "isidore_default", [3]),
            src("cairn", [cairn], "isidore_default", [3]),
            src("openedition", [oe], "isidore_default", [3]),
        ])
        (entry,) = result["entries"]
        contrib = entry["components"]["native_contributions"]
        assert len(contrib) == 1 and contrib[0]["provider"] == "isidore"
        assert entry["score"] == pytest.approx(0.5 / (RRF_K + 3))
        dims = entry["dimensions"]["sources"]
        assert dims["independent"] == 1
        assert dims["providers"] == ["Isidore"]
        assert set(dims["sources"]) == {"Isidore", "Cairn", "OpenEdition"}
        assert "presumed_order" in {r["code"] for r in entry["justification"]["reserves"]}

    def test_proxies_do_not_beat_an_independent_confirmation(self):
        direct, cairn, oe = self._family()
        hal = art("Autre travail", doi="10.1/h", sources=("HAL",))
        hal_isi = isidore_item(8, "Autre travail", label="Isidore", doi="10.1/h")
        records, _ = deduplicate_with_report([direct, cairn, oe, hal, hal_isi])
        assert len(records) == 2
        result = rank_records(records, [
            src("hal", [hal], HAL_SORT, [3]),
            src("isidore", [direct, hal_isi], "isidore_default", [3, 3]),
            src("cairn", [cairn], "isidore_default", [3]),
            src("openedition", [oe], "isidore_default", [3]),
        ])
        first, second = result["entries"]
        assert first["id"] == "doi:10.1/h"
        assert first["dimensions"]["sources"]["independent"] == 2
        assert second["dimensions"]["sources"]["independent"] == 1


# === Reproductibilité ===

def _corpus(seed=0, n_works=60):
    """Petit univers : travaux présents chez plusieurs sources, fusionnés par
    la vraie déduplication, rangs natifs par source."""
    rnd = random.Random(seed)
    words = ["discours", "numérique", "réseaux", "médias", "politique",
             "écriture", "archives", "plateformes", "controverse", "presse"]
    per_source = {"hal": [], "crossref": [], "openalex": [], "isidore": [],
                  "cairn": [], "semantic_scholar": [], "core": []}
    for w in range(n_works):
        title = " ".join(rnd.sample(words, 4)) + f" {w}"
        doi = f"10.9/{w}" if w % 3 else None
        abstract = " ".join(rnd.choice(words) for _ in range(30))
        for key in per_source:
            if rnd.random() < 0.35:
                label = ranking.display_name(key)
                if key in ("isidore", "cairn"):
                    item = isidore_item(w, title, label=label, doi=doi,
                                        abstract=abstract, author=f"Auteur{w}")
                else:
                    item = art(title, doi=doi, sources=(label,),
                               abstract=abstract if key != "core" else "",
                               author=f"Auteur{w}",
                               url=None if doi else f"https://example.org/{key}/{w}",
                               lines=(f"Cited by: {rnd.randint(0, 500)}",))
                per_source[key].append(item)
    sorts = {"hal": HAL_SORT, "crossref": "score:desc",
             "openalex": "relevance_score:desc", "isidore": "isidore_default",
             "cairn": "isidore_default", "semantic_scholar": "citationCount:desc",
             "core": None}
    sources = []
    for key, items in per_source.items():
        rnd.shuffle(items)
        sources.append(src(key, items, sorts[key]))
    flat = [it for s in sources for it in s.items]
    records, _ = deduplicate_with_report(flat)
    return records, sources


def _permuted(records, sources, seed):
    """
    Permutation des entrées : notices finales, ordre des sources, et notices
    de chaque source qui publie ses rangs (chaque notice garde son rang). La
    liste d'une source SANS rang publié (CORE, Scholar) n'est pas permutée :
    son ordre est sa seule information d'ordre natif (ce n'est pas un ordre
    d'arrivée).
    """
    rnd = random.Random(seed)
    recs = list(records)
    rnd.shuffle(recs)
    new_sources = []
    for s in sources:
        pairs = list(zip(s.items, s.aligned_ranks() or [None] * len(s.items)))
        if s.native_sort is not None and s.native_ranks:
            rnd.shuffle(pairs)
        clone = SourceResult(source=s.source, display_name=s.display_name,
                             items=[p[0] for p in pairs], status=s.status)
        if s.native_sort is not None and s.native_ranks:
            clone.native_sort = s.native_sort
            clone.native_ranks = [p[1] for p in pairs]
            clone.bind_ranks()
        new_sources.append(clone)
    rnd.shuffle(new_sources)
    return recs, new_sources


class TestReproducibility:
    @pytest.mark.parametrize("mode", ["native", "local"])
    def test_same_input_same_ranking(self, mode):
        records, sources = _corpus()
        plan = build_query_plan("discours numérique", ["hal", "crossref"])
        r1 = rank_records(records, sources, mode=mode, plan=plan,
                          reference_year=2026)
        r2 = rank_records(records, sources, mode=mode, plan=plan,
                          reference_year=2026)
        assert strip_durations(r1) == strip_durations(r2)
        # Sérialisable tel quel (manifeste d'OPT-16, retour MCP).
        assert json.loads(json.dumps(r1, ensure_ascii=False))["version"] == RANKING_VERSION

    @pytest.mark.parametrize("mode", ["native", "local"])
    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_permutation_has_no_effect(self, mode, seed):
        records, sources = _corpus()
        plan = build_query_plan("discours numérique", ["hal", "crossref"])
        base = rank_records(records, sources, mode=mode, plan=plan,
                            reference_year=2026)
        recs2, sources2 = _permuted(records, sources, seed)
        other = rank_records(recs2, sources2, mode=mode, plan=plan,
                             reference_year=2026)
        assert order_signature(base, records) == order_signature(other, recs2)
        assert [e["score"] for e in base["entries"]] == \
            [e["score"] for e in other["entries"]]
        assert [e["id"] for e in base["entries"]] == [e["id"] for e in other["entries"]]

    def test_groups_and_key_mapping_agree(self):
        records, sources = _corpus(seed=4)
        flat = [it for s in sources for it in s.items]
        recs2, report = deduplicate_with_report(flat, with_groups=True)
        by_keys = rank_records(records, sources)
        by_groups = rank_records(recs2, sources, groups=report["groups"])
        assert [e["id"] for e in by_keys["entries"]] == \
            [e["id"] for e in by_groups["entries"]]
        assert by_groups["mapping"]["method"] == "groups"


# === Invariant 1 ===

class TestNoNoticeModified:
    @pytest.mark.parametrize("mode", ["native", "local"])
    def test_records_and_source_items_untouched(self, mode):
        records, sources = _corpus(seed=5)
        before_records = copy.deepcopy(records)
        before_items = [copy.deepcopy(s.items) for s in sources]
        rank_records(records, sources, mode=mode, query="discours numérique")
        assert records == before_records
        assert [s.items for s in sources] == before_items
        for rec in records:
            assert not {"rank", "score", "ranking", "justification"} & set(rec)
            assert "rank" not in (rec.get("extra") or "").lower().split(":")[0]


# === Justification ===

class TestJustification:
    @pytest.mark.parametrize("mode", ["native", "local"])
    def test_every_top20_entry_is_explained(self, mode):
        records, sources = _corpus(seed=6, n_works=80)
        result = rank_records(records, sources, mode=mode,
                              query="discours numérique")
        top = result["entries"][:20]
        assert len(top) == 20
        for e in top:
            just = e["justification"]
            assert just["summary"].strip()
            # Rangs natifs retenus, ou raison de l'absence de rang.
            codes = {r["code"] for r in just["reserves"]}
            assert just["native"] or "no_relevance_rank" in codes
            if e["score"] > 0 and mode == "native":
                assert e["components"]["native_contributions"]
            if mode == "local":
                assert "concepts" in just and "missing" in just
                assert just["concepts"] or just["missing"]
            for dim in ("sources", "citations", "year", "age_years"):
                assert dim in e["dimensions"]

    def test_reserves(self):
        no_abstract = art("Sans résumé", doi="10.1/n")
        snippet = art("Extrait", sources=("Google Scholar",),
                      abstract="… discours numérique …",
                      url="https://example.org/s",
                      lines=("Scholar snippet: extrait de la page de résultats",
                             "Scholar truncation: auteurs et/ou revue tronqués"))
        partial_isi = isidore_item(3, "Fenêtre", label="Cairn", abstract="texte")
        result = rank_records(
            [no_abstract, snippet, partial_isi],
            [src("hal", [no_abstract], HAL_SORT),
             src("google_scholar", [snippet]),
             src("cairn", [partial_isi], "isidore_default", status="partial",
                 reason="provider_window")])
        codes = {e["id"]: {r["code"] for r in e["justification"]["reserves"]}
                 for e in result["entries"]}
        assert "no_abstract" in codes["doi:10.1/n"]
        scholar = next(v for k, v in codes.items() if k.startswith("url:"))
        assert {"scholar_snippet", "authors_truncated",
                "no_relevance_rank"} <= scholar
        assert "no_abstract" not in scholar
        cairn = next(v for k, v in codes.items() if k.startswith("isidore:"))
        assert {"source_partial", "presumed_order"} <= cairn
        label = next(r["label"] for e in result["entries"]
                     for r in e["justification"]["reserves"]
                     if r["code"] == "source_partial")
        assert "Cairn" in label and "fenêtre" in label
        assert result["notes"] and any("Cairn" in n for n in result["notes"])

    def test_fuzzy_and_ambiguous_identity_reserves(self):
        fuzzy = art("Titre", doi="10.1/f",
                    lines=("Dedup evidence: 2 notices ; floue titre 0.95 + année + auteur dupont",))
        ambiguous = art("Titre ambigu", url="https://example.org/amb",
                        lines=("Dedup evidence: ambigu non fusionné, clés partagées "
                               "avec des DOI distincts : doi:10.1/x, doi:10.1/y",))
        result = rank_records([fuzzy, ambiguous], [])
        codes = {e["id"]: {r["code"] for r in e["justification"]["reserves"]}
                 for e in result["entries"]}
        assert "identity_fuzzy" in codes["doi:10.1/f"]
        assert "identity_ambiguous" in codes["url:example.org/amb"]


# === Citations hors du score principal ===

class TestCitationsDoNotDominate:
    def _pair(self, citations):
        central = art("Le discours numérique des institutions publiques",
                      doi="10.1/central", abstract="Analyse du discours numérique.",
                      tags=("discours numérique",), lines=("Cited by: 2",))
        cited_hs = art("Une histoire de la chimie organique", doi="10.1/chimie",
                       abstract="Synthèse des composés carbonés.",
                       sources=("CrossRef",), lines=(f"Cited by: {citations}",))
        cited_s2 = art("Une histoire de la chimie organique", doi="10.1/chimie",
                       sources=("Semantic Scholar",), lines=(f"Cited by: {citations}",))
        records, _ = deduplicate_with_report([central, cited_hs, cited_s2])
        sources = [src("hal", [central], HAL_SORT, [30]),
                   src("crossref", [cited_hs], "score:desc", [1]),
                   src("semantic_scholar", [cited_s2], "citationCount:desc", [1])]
        return records, sources

    def test_central_notice_first_in_local_mode(self):
        records, sources = self._pair(900000)
        result = rank_records(records, sources, mode="local",
                              query="discours numérique")
        assert result["entries"][0]["id"] == "doi:10.1/central"
        chimie = entries_by_id(result)["doi:10.1/chimie"]
        assert chimie["dimensions"]["citations"] == 900000
        assert chimie["justification"]["missing"]

    def test_citations_never_change_the_scores(self):
        low = rank_records(*self._pair(1), mode="local", query="discours numérique")
        high = rank_records(*self._pair(10 ** 9), mode="local",
                            query="discours numérique")
        assert [(e["id"], e["score"]) for e in low["entries"]] == \
            [(e["id"], e["score"]) for e in high["entries"]]
        low_n = rank_records(*self._pair(1))
        high_n = rank_records(*self._pair(10 ** 9))
        assert [(e["id"], e["score"]) for e in low_n["entries"]] == \
            [(e["id"], e["score"]) for e in high_n["entries"]]

    def test_citation_order_of_semantic_scholar_has_no_weight(self):
        records, sources = self._pair(5)
        result = rank_records(records, sources)
        chimie = entries_by_id(result)["doi:10.1/chimie"]
        providers = {c["provider"] for c in chimie["components"]["native_contributions"]}
        assert providers == {"crossref"}


# === Concepts du plan de requête ===

class TestConcepts:
    def test_tokens(self):
        assert tokens("L'Analyse du discours numérique : Œuvres, socio-économiques") == \
            ["analyse", "du", "discour", "numerique", "oeuvre", "socio", "economique"]

    def test_simple_query_with_implicit_phrase(self):
        cs = concepts_from_parsed(parse_query("discours numérique"))
        labels = [(c.label, c.implicit, c.weight) for c in cs.concepts]
        assert labels == [("discours", False, 1.0), ("numérique", False, 1.0),
                          ("« discours numérique »", True, 0.5)]
        assert cs.concepts[2].expressions[0].kind == "phrase"

    def test_phrase_or_not_and_stopwords(self):
        cs = concepts_from_parsed(parse_query(
            '(twitter OR facebook) AND "analyse du discours" NOT politique de'))
        assert [c.label for c in cs.concepts] == [
            "twitter OU facebook", "« analyse du discours »"]
        assert [e.tokens for e in cs.concepts[0].expressions] == [("twitter",), ("facebook",)]
        assert cs.concepts[1].expressions[0].tokens == ("analyse", "du", "discour")
        assert [e.text for e in cs.excluded] == ["politique"]
        assert not any(c.implicit for c in cs.concepts)

    def test_stopword_only_query_kept(self):
        cs = concepts_from_parsed(parse_query("the"))
        assert [c.label for c in cs.concepts] == ["the"]

    def test_variants_from_the_plan(self):
        plan = build_query_plan("discours numérique", ["hal", "openalex"],
                                query_variants={"en": "digital discourse"})
        sets = ranking.build_concepts(plan=plan)
        assert [s.version for s in sets] == ["original", "en"]
        english = art("Digital discourse analysis on social media",
                      doi="10.1/en", sources=("OpenAlex",))
        result = rank_records([english], [src("openalex", [english],
                                               "relevance_score:desc")],
                              mode="local", plan=plan)
        (entry,) = result["entries"]
        assert entry["components"]["query_version"] == "en"
        assert entry["components"]["lexical_simple"] > 0
        assert {c["concept"] for c in entry["justification"]["concepts"]} >= \
            {"digital", "discourse"}


# === Pondérations lexicales ===

class TestLexicalModels:
    def test_simple_model_field_weights_and_phrase(self):
        in_title = art("Le discours numérique", doi="10.1/t")
        in_abstract = art("Autre chose", doi="10.1/a",
                          abstract="Un discours sur le numérique.")
        in_tags = art("Sans rapport", doi="10.1/g",
                      tags=("discours numérique",))
        result = rank_records([in_title, in_abstract, in_tags], [],
                              mode="local", query="discours numérique")
        simple = {e["id"]: e["components"]["lexical_simple"]
                  for e in result["entries"]}
        # Titre : 2 concepts × 3 + expression implicite 0,5 × 3 ; / 7,5.
        assert simple["doi:10.1/t"] == pytest.approx(1.0)
        # Résumé : deux concepts (1 + 1), expression non contiguë ; / 7,5.
        assert simple["doi:10.1/a"] == pytest.approx(2 / 7.5)
        # Mots-clés : 2 × 2 + 0,5 × 2 ; / 7,5.
        assert simple["doi:10.1/g"] == pytest.approx(5 / 7.5)
        assert [e["id"] for e in result["entries"]] == \
            ["doi:10.1/t", "doi:10.1/g", "doi:10.1/a"]

    def test_excluded_term_penalty_and_reserve(self):
        clean = art("Le discours numérique des villes", doi="10.1/c")
        excluded = art("Le discours numérique politique", doi="10.1/p")
        result = rank_records([clean, excluded], [], mode="local",
                              query='"discours numérique" NOT politique')
        by_id = entries_by_id(result)
        assert by_id["doi:10.1/p"]["components"]["lexical_simple"] == pytest.approx(
            by_id["doi:10.1/c"]["components"]["lexical_simple"] * 0.5)
        codes = {r["code"] for r in by_id["doi:10.1/p"]["justification"]["reserves"]}
        assert "excluded_term" in codes
        assert result["entries"][0]["id"] == "doi:10.1/c"

    def test_scholar_snippet_counts_half(self):
        snippet = art("Titre neutre", url="https://example.org/s1",
                      sources=("Google Scholar",), abstract="discours",
                      lines=("Scholar snippet: extrait",))
        real = art("Titre neutre", doi="10.1/r", abstract="discours")
        result = rank_records([snippet, real], [], mode="local", query="discours")
        by_id = entries_by_id(result)
        assert by_id["doi:10.1/r"]["components"]["lexical_simple"] == pytest.approx(1 / 3)
        assert by_id["url:example.org/s1"]["components"]["lexical_simple"] == \
            pytest.approx(0.5 / 3)

    def test_bm25_matches_the_formula(self):
        d1 = art("discours", doi="10.1/1")
        d2 = art("autre chose", doi="10.1/2", abstract="discours discours")
        result = rank_records([d1, d2], [], mode="local", query="discours",
                              lexical_model="bm25")
        by_id = entries_by_id(result)
        k1, b = 1.2, 0.75
        idf = math.log(1 + (2 - 2 + 0.5) / (2 + 0.5))
        avgdl = (3 + 8) / 2
        expected1 = idf * 3 * (k1 + 1) / (3 + k1 * (1 - b + b * 3 / avgdl))
        expected2 = idf * 2 * (k1 + 1) / (2 + k1 * (1 - b + b * 8 / avgdl))
        assert by_id["doi:10.1/1"]["components"]["bm25"] == pytest.approx(expected1)
        assert by_id["doi:10.1/2"]["components"]["bm25"] == pytest.approx(expected2)
        assert result["local"]["lexical_model"] == "bm25"

    def test_rarer_term_weighs_more_in_bm25(self):
        common = [art(f"discours {i}", doi=f"10.1/c{i}") for i in range(5)]
        rare = art("controverse", doi="10.1/rare")
        result = rank_records(common + [rare], [], mode="local",
                              query="discours OR controverse",
                              lexical_model="bm25")
        assert result["entries"][0]["id"] == "doi:10.1/rare"

    def test_both_models_are_computed_and_compared(self):
        records, sources = _corpus(seed=7)
        result = rank_records(records, sources, mode="local",
                              query="discours numérique")
        comp = result["local"]["comparison"]
        assert 0 <= comp["top_overlap"] <= 1
        assert comp["spearman"] is None or -1 <= comp["spearman"] <= 1
        for e in result["entries"]:
            assert "lexical_simple" in e["components"] and "bm25" in e["components"]
        assert result["local"]["lexical_model"] == "simple"
        assert result["local"]["concepts"][0]["version"] == "original"

    def test_no_match_gives_no_overlap(self):
        a = art("Rien à voir", doi="10.1/z")
        result = rank_records([a], [], mode="local", query="discours")
        assert result["local"]["comparison"]["top_overlap"] is None

    def test_local_combines_lexical_and_native(self):
        lexical_only = art("Le discours numérique", doi="10.1/l")
        native_only = art("Sans rapport", doi="10.1/n")
        both = art("Discours numérique et presse", doi="10.1/b")
        result = rank_records(
            [lexical_only, native_only, both],
            [src("hal", [native_only, both], HAL_SORT, [1, 2])],
            mode="local", query="discours numérique")
        by_id = entries_by_id(result)
        assert result["entries"][0]["id"] == "doi:10.1/b"
        e = by_id["doi:10.1/b"]
        lex = e["components"]["lexical_simple_rank"]
        nat = e["components"]["native_competition_rank"]
        assert e["score"] == pytest.approx(1 / (RRF_K + lex) + 1 / (RRF_K + nat))
        assert e["local"]["rank"] == e["rank"] == 1
        assert by_id["doi:10.1/l"]["native"]["score"] == 0


# === Rattachement ===

class TestMapping:
    def test_fixture_pairs_against_exact_groups(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        pairs = data["positives"] + data["negatives"]
        a_items = [copy.deepcopy(p["a"]) for p in pairs]
        b_items = [copy.deepcopy(p["b"]) for p in pairs]
        inputs = _source_inputs([{"source": "hal", "items": a_items},
                                 {"source": "crossref", "items": b_items}])
        records, report = deduplicate_with_report(a_items + b_items,
                                                  with_groups=True)
        positions = [(0, i) for i in range(len(a_items))] + \
            [(1, i) for i in range(len(b_items))]
        truth = {positions[m]: ri for ri, members in enumerate(report["groups"])
                 for m in members}
        mapping = map_source_items(records, inputs)
        wrong = {k: v for k, v in mapping.assigned.items() if truth[k] != v}
        assert wrong == {}
        assert len(mapping.assigned) == len(truth) == 240
        assert mapping.stats["title"] >= 1       # fusions floues sans clé

    def test_ambiguous_keys_are_not_resolved_by_chance(self):
        # Deux notices finales partagent l'URL de la notice de source et ne
        # s'en distinguent par rien : aucun rattachement, réserve des deux côtés.
        item = art("Page commune", url="https://example.org/page")
        r1 = dict(art("Page commune", url="https://example.org/page"),
                  extra="Sources: HAL\nPMID: 1")
        r2 = dict(art("Page commune", url="https://example.org/page"),
                  extra="Sources: HAL\nPMID: 2")
        result = rank_records([r1, r2], [src("hal", [item], HAL_SORT)])
        for e in result["entries"]:
            assert e["score"] == 0
            codes = {r["code"] for r in e["justification"]["reserves"]}
            assert "rank_unmapped" in codes
        assert result["mapping"]["ambiguous"] == 1

    def test_exact_candidate_wins_over_supersets(self):
        # Notice laissée à part (copie annotée) et deux notices à DOI
        # distincts qui partagent son URL : la copie, sans clé en plus, gagne.
        loose = art("Commun", url="https://example.org/u", sources=("Isidore",))
        with_x = art("Commun", doi="10.1/x", url="https://example.org/u")
        with_y = art("Commun", doi="10.1/y", url="https://example.org/u")
        records = [dict(loose, extra=loose["extra"] + "\nDedup evidence: ambigu x"),
                   dict(with_x), dict(with_y)]
        inputs = _source_inputs([{"source": "isidore", "items": [loose]},
                                 {"source": "hal", "items": [with_x, with_y]}])
        mapping = map_source_items(records, inputs)
        assert mapping.assigned[(0, 0)] == 0
        assert mapping.assigned[(1, 0)] == 1
        assert mapping.assigned[(1, 1)] == 2

    def test_record_id_priority_and_fingerprint(self):
        item = art("T", doi="10.1/Z", url="https://hal.science/hal-0123",
                   lines=("OpenAlex ID: https://openalex.org/W1",))
        assert record_id(item) == "doi:10.1/z"
        bare = {"itemType": "book", "title": "Sans identifiant", "date": "1999",
                "creators": [{"lastName": "Durand"}]}
        rid = record_id(bare)
        assert rid.startswith("fp:") and rid == record_id(copy.deepcopy(bare))


# === Extrait exposé ===

class TestTopRankedPayload:
    def test_compact_payload(self):
        records, sources = _corpus(seed=8, n_works=80)
        result = rank_records(records, sources)
        payload = top_ranked_payload(result)
        assert payload["version"] == RANKING_VERSION
        assert payload["mode"] == "native"
        assert payload["limit"] == 20 and len(payload["items"]) == 20
        assert payload["records"] == len(records)
        for item in payload["items"]:
            assert set(item) == {"rank", "score", "id", "index"}
            assert 0 <= item["index"] < len(records)
        assert [i["rank"] for i in payload["items"]] == list(range(1, 21))
        assert len(json.dumps(payload)) < 3000
        assert top_ranked_payload(None) is None

    def test_error_is_reported(self):
        payload = top_ranked_payload({"version": RANKING_VERSION, "mode": "local",
                                      "error": "Boom", "entries": []})
        assert payload["error"] == "Boom" and payload["items"] == []
