"""
Tests OPT-13 — corrections de la revue du classement (``utils.ranking``,
version ``opt13-2``), sans réseau.

- classement natif : un ordre de citations ou de date (Semantic Scholar
  ``citationCount:desc``) n'ordonne jamais la queue ; bout en bout par
  ``moisson_search`` (stdio, exécutant doublé) ;
- classement local : troncature finale, proximité ``~N``, champs non
  textuels, jokers internes et recherche approchée (non évalués : ni
  appariés ni déclarés absents, réserve et note) ;
- classement local : à score local égal, la preuve lexicale passe avant les
  dimensions séparées (fournisseurs indépendants, citations) ;
- compteurs : pertinence vérifiée et ordre présumé seul distingués ;
  « relevance » (S2 ``/paper/search``, jamais observé) rangé en ordre
  présumé ;
- comparaison simple / BM25 : Spearman à rangs moyens sur les seules
  notices appariées, bloc non apparié publié à part.
"""

from __future__ import annotations

import copy
import json
import logging
import math
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
    CLASS_OTHER, CLASS_PRESUMED, CLASS_RELEVANCE, RANKING_VERSION,
    _spearman, classify_sort, concepts_from_parsed, is_dimension_sort,
    rank_records, rank_harvest,
)

HAL_SORT = "score desc,docid asc"
S2_SORT = "citationCount:desc"


def art(title, *, doi=None, year="2021", author="Dupont", abstract="",
        tags=(), sources=("HAL",), cites=None, lines=()):
    item = {
        "itemType": "journalArticle",
        "title": title,
        "creators": [{"creatorType": "author", "firstName": "Anne",
                      "lastName": author}],
        "date": year,
        "abstractNote": abstract,
        "tags": [{"tag": t} for t in tags],
    }
    if doi:
        item["DOI"] = doi
    extra = [f"Sources: {', '.join(sources)}", *lines]
    if cites is not None:
        extra.append(f"Cited by: {cites}")
    item["extra"] = "\n".join(extra)
    return item


def src(key, items, sort=None, ranks=None, status="complete"):
    result = SourceResult(source=key, display_name=ranking.display_name(key),
                          items=list(items), status=status)
    if sort is not None:
        result.native_sort = sort
        if ranks is None:
            ranks = list(range(1, len(items) + 1))
        result.native_ranks = [{"rank": r, "score": None, "sort": sort}
                               for r in ranks]
        result.bind_ranks()
    return result


def ids_of(result):
    return [e["id"] for e in result["entries"]]


def by_id(result):
    return {e["id"]: e for e in result["entries"]}


def test_version_changed_with_the_rules():
    assert RANKING_VERSION != "opt13-1"


# === Constat 1 : ordre des citations et queue du classement natif ===

class TestCitationOrderNeverRanksTheQueue:
    @pytest.mark.parametrize("sort", [
        "citationCount:desc", "citationCount:asc", "publicationDate:desc",
        "cited_by_count:desc", "is-referenced-by-count", "published",
        "updated",
    ])
    def test_dimension_sorts(self, sort):
        assert is_dimension_sort(sort)

    @pytest.mark.parametrize("sort", [
        None, "", "docid asc", "cursor_default", "paperId", HAL_SORT,
        "score:desc", "relevance_score:desc", "isidore_default", "relevance",
    ])
    def test_neutral_or_relevance_sorts(self, sort):
        assert not is_dimension_sort(sort)

    def test_semantic_scholar_alone_is_not_served_by_citations(self):
        # Ordre de S2 (citations décroissantes) opposé à l'ordre des
        # identifiants : le classement natif suit l'identifiant, et
        # inverser les positions de S2 n'y change rien.
        hs1 = art("Deep learning for protein folding", doi="10.1/z",
                  sources=("Semantic Scholar",), cites=120000)
        hs2 = art("Attention is all you need", doi="10.1/y",
                  sources=("Semantic Scholar",), cites=90000)
        central = art("Le discours numérique des institutions", doi="10.1/a",
                      sources=("Semantic Scholar",), cites=4)
        items = [hs1, hs2, central]
        served = rank_records(items, [src("semantic_scholar", items, S2_SORT)],
                              query="discours numérique")
        reversed_ = rank_records(items, [src("semantic_scholar", items[::-1],
                                             S2_SORT)],
                                 query="discours numérique")
        assert ids_of(served) == ids_of(reversed_) == \
            ["doi:10.1/a", "doi:10.1/y", "doi:10.1/z"]
        reserve = next(r["label"] for r in served["entries"][0]["justification"]["reserves"]
                       if r["code"] == "no_relevance_rank")
        assert "citations ou de date non utilisé" in reserve
        assert "départage par identifiant" in reserve
        assert any("citationCount:desc" in n for n in served["notes"])

    def test_cited_off_topic_notice_after_neutral_positions(self):
        # HAL en collecte (docid asc) : la notice centrale en position 2 ;
        # S2 sert en position 1 une notice hors sujet citée 90 000 fois.
        old = art("Dépôt ancien", doi="10.2/old")
        central = art("Le discours numérique des institutions publiques",
                      doi="10.2/central", abstract="Analyse du discours numérique.")
        off = art("Deep learning for protein folding", doi="10.1/off",
                  sources=("Semantic Scholar",), cites=90000)
        result = rank_records(
            [old, central, off],
            [src("hal", [old, central], "docid asc"),
             src("semantic_scholar", [off], S2_SORT)])
        assert ids_of(result) == ["doi:10.2/old", "doi:10.2/central", "doi:10.1/off"]

    def test_neutral_position_of_the_same_notice_is_kept(self):
        # Une notice servie par S2 (citations, position 1) ET par CORE
        # (position 2) garde sa position neutre de CORE : l'ordre de S2 ne
        # la fait pas remonter devant la première notice de CORE.
        first = art("Alpha", doi="10.3/b", sources=("CORE",))
        second = art("Beta", doi="10.3/a", sources=("CORE",))
        second_s2 = art("Beta", doi="10.3/a", sources=("Semantic Scholar",),
                        cites=10 ** 6)
        records, _ = deduplicate_with_report([first, second, second_s2])
        result = rank_records(records, [src("core", [first, second]),
                                        src("semantic_scholar", [second_s2], S2_SORT)])
        assert ids_of(result) == ["doi:10.3/b", "doi:10.3/a"]

    def test_mcp_top_ranked_does_not_follow_citations(self, tmp_path, monkeypatch):
        import config
        import mcp_server
        from engine import orchestrator

        s2_items = [
            art("ImageNet classification with deep convolutional networks",
                doi="10.9/c", sources=("Semantic Scholar",), cites=120000),
            art("Attention is all you need", doi="10.9/b",
                sources=("Semantic Scholar",), cites=90000),
            art("Le discours numérique des institutions publiques",
                doi="10.9/a", sources=("Semantic Scholar",), cites=4,
                abstract="Analyse du discours numérique."),
        ]

        def stub(name, cls, query, mps, output_dir, run_id, http_client,
                 from_date=None, to_date=None, *, run_context=None,
                 source_key=None, **kw):
            items = copy.deepcopy(s2_items)
            result = SourceResult(source=source_key, display_name=name,
                                  items=items, status="complete")
            result.native_sort = S2_SORT
            result.native_ranks = [{"rank": i + 1, "score": None, "sort": S2_SORT}
                                   for i in range(len(items))]
            result.bind_ranks()
            return result

        monkeypatch.setattr(orchestrator, "run_source", stub)
        monkeypatch.setattr(config, "RANKING_DEFAULT", "native")
        monkeypatch.setattr(mcp_server, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(mcp_server, "TRANSPORT_MODE", "stdio")
        # Retour « full » : extrait complet de top_ranked (OPT-17).
        _blocks, structured = mcp_server._run_moisson_sync(
            "discours numérique", 200, ["semantic_scholar"], False, True,
            cache=False, workers=1, mode="bibliographie", inline="full")
        top = structured["top_ranked"]
        assert top["mode"] == "native"
        # Ordre des identifiants, pas celui des citations (c, b, a).
        assert [it["id"] for it in top["items"]] == \
            ["doi:10.9/a", "doi:10.9/b", "doi:10.9/c"]


# === Constat 2 : opérateurs du classement local ===

class TestLocalOperators:
    def test_truncation_matches_by_prefix(self):
        both = art("La numérisation des archives", doi="10.1/both",
                   abstract="Un discours sur la numérisation.")
        only = art("Le discours politique", doi="10.1/only",
                   abstract="Rhétorique parlementaire.")
        result = rank_records([both, only], [src("hal", [both, only])],
                              mode="local", query="numéri* discours")
        assert ids_of(result)[0] == "doi:10.1/both"
        entries = by_id(result)
        assert entries["doi:10.1/both"]["justification"]["missing"] == []
        assert entries["doi:10.1/only"]["justification"]["missing"] == ["numéri*"]
        assert entries["doi:10.1/both"]["components"]["bm25"] > \
            entries["doi:10.1/only"]["components"]["bm25"]
        forms = result["local"]["concepts"][0]["concepts"][0]["forms"]
        assert forms == [{"text": "numéri*", "kind": "prefix"}]

    def test_truncation_of_a_whole_word_in_s(self):
        # « process* » apparie le mot « process » (normalisé « proces »).
        item = art("A process view of institutions", doi="10.1/p")
        result = rank_records([item], [], mode="local", query="process*")
        assert result["entries"][0]["components"]["lexical_simple"] == pytest.approx(1.0)

    def test_truncated_implicit_phrase(self):
        item = art("Le numérique discursif", doi="10.1/t")
        exact = art("Numérisation discours et archives", doi="10.1/e")
        result = rank_records([item, exact], [], mode="local",
                              query="numéri* discours")
        simple = {e["id"]: e["components"]["lexical_simple"]
                  for e in result["entries"]}
        # « numérisation discours » : deux concepts et l'expression
        # implicite tronquée, au titre.
        assert simple["doi:10.1/e"] == pytest.approx(1.0)

    def test_author_field_is_not_a_text_concept(self):
        lev = art("L'intelligence collective", doi="10.1/lev", author="Lévy",
                  abstract="Pour une anthropologie du cyberespace.")
        result = rank_records([lev], [src("hal", [lev])], mode="local",
                              query='authFullName_s:"Pierre Lévy" AND intelligence')
        (entry,) = result["entries"]
        just = entry["justification"]
        assert just["missing"] == []
        assert [c["concept"] for c in just["concepts"]] == ["intelligence"]
        assert just["unevaluated"] == ["authFullName_s:« Pierre Lévy »"]
        codes = {r["code"] for r in just["reserves"]}
        assert "query_part_unevaluated" in codes
        assert "absents" not in just["summary"]
        assert any("non évaluées localement" in n for n in result["notes"])
        assert entry["components"]["lexical_simple"] == pytest.approx(1.0)
        assert result["local"]["unevaluated"] == ["authFullName_s:« Pierre Lévy »"]

    def test_non_text_field_group_is_not_flattened(self):
        cs = concepts_from_parsed(parse_query(
            "authFullName_s:(Pierre AND Lévy) discours"))
        assert [c.label for c in cs.concepts] == ["discours"]
        assert cs.unevaluated == ("authFullName_s:(Pierre Lévy)",)

    def test_text_fields_are_still_evaluated(self):
        cs = concepts_from_parsed(parse_query(
            'title_t:discours AND fr_abstract_t:"analyse du discours"'))
        assert [c.label for c in cs.concepts] == ["discours", "« analyse du discours »"]
        assert cs.unevaluated == ()
        cs = concepts_from_parsed(parse_query("journalTitle_s:Réseaux discours"))
        assert [c.label for c in cs.concepts] == ["discours"]
        assert cs.unevaluated == ("journalTitle_s:Réseaux",)

    def test_date_range_is_listed_as_unevaluated(self):
        cs = concepts_from_parsed(parse_query(
            "discours AND producedDateY_i:[2020 TO 2024]"))
        assert [c.label for c in cs.concepts] == ["discours"]
        assert cs.unevaluated == ("producedDateY_i:[2020 TO 2024]",)

    def test_alternative_with_an_unevaluable_branch(self):
        cs = concepts_from_parsed(parse_query(
            "(discours OR authFullName_s:Lévy) numérique"))
        assert [c.label for c in cs.concepts] == ["numérique"]
        assert cs.unevaluated == ("(discours OU authFullName_s:Lévy)",)

    @pytest.mark.parametrize("term", ["num?rique", "*ique", "nu*rique",
                                      "discours~1", "d*"])
    def test_other_wildcards_and_fuzzy_search_are_unevaluated(self, term):
        cs = concepts_from_parsed(parse_query(f"{term} archives"))
        assert [c.label for c in cs.concepts] == ["archives"]
        assert cs.unevaluated == (term,)
        # Pas d'expression implicite bâtie sur un mot non évalué.
        assert not any(c.implicit for c in cs.concepts)

    def test_excluded_non_text_field_is_unevaluated(self):
        cs = concepts_from_parsed(parse_query("discours NOT authFullName_s:Lévy"))
        assert cs.excluded == ()
        assert cs.unevaluated == ("NON authFullName_s:Lévy",)

    def test_proximity_window(self):
        near = art("Le discours politique et numérique", doi="10.1/near")
        far = art("Le discours des villes, des régions, des États, des "
                  "institutions et des organisations numérique", doi="10.1/far")
        result = rank_records([near, far], [], mode="local",
                              query='"discours numérique"~3')
        form = result["local"]["concepts"][0]["concepts"][0]["forms"][0]
        assert form == {"text": "discours numérique~3", "kind": "near", "slop": 3}
        label = result["local"]["concepts"][0]["concepts"][0]["label"]
        assert label == "« discours numérique »~3"
        entries = by_id(result)
        assert entries["doi:10.1/near"]["justification"]["missing"] == []
        assert entries["doi:10.1/far"]["justification"]["missing"] == \
            ["« discours numérique »~3"]
        # Sans proximité : expression contiguë, aucune des deux.
        exact = rank_records([near, far], [], mode="local",
                             query='"discours numérique"')
        assert all(e["justification"]["missing"] for e in exact["entries"])

    def test_proximity_does_not_cross_two_keywords(self):
        tagged = art("Sans rapport", doi="10.1/k", tags=("discours", "numérique"))
        result = rank_records([tagged], [], mode="local",
                              query='"discours numérique"~2')
        assert result["entries"][0]["justification"]["missing"] == \
            ["« discours numérique »~2"]


# === Constat 3 : départage du classement local ===

def _international(off_cites=900000, off_also_in_s2=False):
    """CrossRef (pertinence) sert une notice hors sujet au rang 1 ; cinq
    notices centrales ne viennent que de Semantic Scholar (citations, poids
    0) ou de CORE (sans rang)."""
    off = art("Organic chemistry of carbon compounds", doi="10.9/off",
              sources=("CrossRef",), cites=off_cites)
    rel = art("Digital discourse in online forums", doi="10.9/rel",
              sources=("CrossRef",), cites=3)
    central = [art(f"Digital discourse and public institutions {i}",
                   doi=f"10.9/c{i}", author=f"Auteur{i}",
                   sources=("Semantic Scholar" if i < 3 else "CORE",),
                   cites=10 + i) for i in range(5)]
    s2 = list(central[:3])
    flat = [off, rel] + central
    if off_also_in_s2:
        off_s2 = art("Organic chemistry of carbon compounds", doi="10.9/off",
                     sources=("Semantic Scholar",), cites=off_cites)
        s2 = [off_s2] + s2
        flat.append(off_s2)
    records, _ = deduplicate_with_report(flat)
    sources = [src("crossref", [off, rel], "score:desc"),
               src("semantic_scholar", s2, S2_SORT),
               src("core", central[3:])]
    plan = build_query_plan("digital discourse",
                            ["crossref", "semantic_scholar", "core"])
    return records, sources, plan


class TestLexicalEvidenceBeforeSeparateDimensions:
    @pytest.mark.parametrize("model", ["simple", "bm25"])
    def test_very_cited_off_topic_after_central_notices(self, model):
        records, sources, plan = _international()
        result = rank_records(records, sources, mode="local", plan=plan,
                              lexical_model=model)
        ids = ids_of(result)
        off = by_id(result)["doi:10.9/off"]
        central = [by_id(result)[f"doi:10.9/c{i}"] for i in range(5)]
        # Même score local (rang natif 1 contre rang lexical 1)…
        if model == "simple":
            assert all(c["score"] == off["score"] for c in central)
        # … et pourtant la notice hors sujet vient après toutes les
        # notices centrales.
        assert ids.index("doi:10.9/off") > max(ids.index(c["id"]) for c in central)

    def test_multi_provider_off_topic_after_central_notices(self):
        records, sources, plan = _international(off_cites=5, off_also_in_s2=True)
        result = rank_records(records, sources, mode="local", plan=plan)
        off = by_id(result)["doi:10.9/off"]
        assert off["dimensions"]["sources"]["independent"] == 2
        assert ids_of(result)[-1] == "doi:10.9/off"

    def test_hal_collecte_central_notice(self):
        # HAL en collecte (docid asc, poids 0) : notice centrale sans rang
        # de pertinence ; CrossRef sert au rang 1 une notice hors sujet.
        central = art("Le discours numérique des institutions", doi="10.4/central")
        off = art("Chimie organique", doi="10.4/off", sources=("CrossRef",),
                  cites=10 ** 6)
        result = rank_records([central, off],
                              [src("hal", [central], "docid asc"),
                               src("crossref", [off], "score:desc")],
                              mode="local", query="discours numérique")
        assert by_id(result)["doi:10.4/central"]["score"] == \
            by_id(result)["doi:10.4/off"]["score"]
        assert ids_of(result) == ["doi:10.4/central", "doi:10.4/off"]

    def test_citations_still_break_exact_lexical_ties(self):
        # Deux notices de même score local et de même score lexical :
        # les citations (faible poids) les départagent, puis l'identifiant.
        a = art("Discours numérique", doi="10.5/a", sources=("CORE",), cites=1)
        b = art("Discours numérique", doi="10.5/b", author="Durand",
                sources=("CORE",), cites=50)
        result = rank_records([a, b], [src("core", [a, b])], mode="local",
                              query="discours numérique")
        assert ids_of(result) == ["doi:10.5/b", "doi:10.5/a"]
        assert "score lexical" in result["params"]["local_tie_break"]


# === Constat 4 : compteurs et ordre présumé ===

class TestCountsAndPresumedOrders:
    def test_relevance_of_paper_search_is_presumed(self):
        assert classify_sort("relevance") == CLASS_PRESUMED
        assert "relevance" not in ranking.RELEVANCE_SORTS
        assert classify_sort("isidore_default") == CLASS_PRESUMED
        assert classify_sort(HAL_SORT) == CLASS_RELEVANCE
        assert classify_sort(S2_SORT) == CLASS_OTHER

    def test_verified_and_presumed_only_are_counted_apart(self, caplog):
        hal = art("Classée par HAL", doi="10.6/h")
        both_hal = art("Classée par les deux", doi="10.6/b")
        both_isi = art("Classée par les deux", doi="10.6/b", sources=("Isidore",),
                       lines=("Isidore URI: https://isidore.science/document/10670/1.000002",))
        isi = art("Classée par Isidore seul", sources=("Isidore",), author="Martin",
                  lines=("Isidore URI: https://isidore.science/document/10670/1.000001",))
        queue = art("En queue", doi="10.6/q", sources=("CORE",))
        records, _ = deduplicate_with_report([hal, both_hal, both_isi, isi, queue])
        sources = [src("hal", [hal, both_hal], HAL_SORT),
                   src("isidore", [isi, both_isi], "isidore_default"),
                   src("core", [queue])]
        caplog.set_level(logging.INFO, logger="moisson.ranking")
        result = rank_harvest(sources, records, mode="native")
        assert result["counts"] == {"records": 4, "with_verified_relevance_rank": 2,
                                    "presumed_only": 1, "queue": 1}
        assert "2 avec rang de pertinence vérifié, 1 avec ordre présumé seul, " \
               "1 en queue" in caplog.text
        assert any(n.startswith("Isidore : ordre présumé") and
                   "un seul fournisseur" in n for n in result["notes"])

    def test_presumed_note_names_the_source(self):
        item = art("Article", doi="10.6/s", sources=("Semantic Scholar",))
        result = rank_records([item], [src("semantic_scholar", [item], "relevance")])
        (entry,) = result["entries"]
        assert entry["score"] == pytest.approx(0.5 / (ranking.RRF_K + 1))
        assert result["counts"]["presumed_only"] == 1
        assert result["counts"]["with_verified_relevance_rank"] == 0
        note = next(n for n in result["notes"] if "présumé" in n)
        assert note.startswith("Semantic Scholar") and "Isidore" not in note
        assert "presumed_order" in {r["code"] for r in entry["justification"]["reserves"]}


# === Constat 5 : comparaison simple / BM25 ===

class TestSpearman:
    def test_unmatched_block_does_not_inflate_the_correlation(self):
        # Trois notices appariées en ordre exactement inverse, cent notices
        # appariées par aucun modèle.
        a = {0: 3.0, 1: 2.0, 2: 1.0, **{i: 0.0 for i in range(3, 103)}}
        b = {0: 1.0, 1: 2.0, 2: 3.0, **{i: 0.0 for i in range(3, 103)}}
        rho, compared = _spearman(a, b)
        assert compared == 3
        assert rho == pytest.approx(-1.0)

    def test_average_ranks_for_ties(self):
        a = {0: 1.0, 1: 1.0, 2: 0.5}
        b = {0: 1.0, 1: 0.5, 2: 0.25}
        rho, compared = _spearman(a, b)
        # Rangs moyens (1,5 ; 1,5 ; 3) contre (1 ; 2 ; 3).
        assert compared == 3
        assert rho == pytest.approx(round(1.5 / math.sqrt(1.5 * 2.0), 4))

    def test_notice_matched_by_one_model_only(self):
        # Appariée par un seul modèle : dernier rang de l'autre, parmi les
        # notices comparées. Rangs (1, 2, 3) contre (1, 3, 2) : 0,5.
        rho, compared = _spearman({0: 2.0, 1: 1.0, 2: 0.0},
                                  {0: 2.0, 1: 0.0, 2: 1.0})
        assert compared == 3
        assert rho == pytest.approx(0.5)

    def test_too_few_matched_notices(self):
        assert _spearman({0: 1.0, 1: 0.0}, {0: 0.0, 1: 0.0}) == (None, 1)

    def test_comparison_publishes_the_unmatched_block(self):
        matched = [art(f"Discours numérique {i}", doi=f"10.8/m{i}",
                       abstract="discours " * i) for i in range(4)]
        unmatched = [art(f"Chimie {i}", doi=f"10.8/u{i}") for i in range(6)]
        result = rank_records(matched + unmatched, [], mode="local",
                              query="discours numérique")
        comp = result["local"]["comparison"]
        assert comp["spearman_records"] == 4
        assert comp["unmatched"] == 6
        assert comp["spearman_records"] + comp["unmatched"] == 10
        assert "rangs moyens" in comp["spearman_method"]
        json.dumps(result, ensure_ascii=False)
