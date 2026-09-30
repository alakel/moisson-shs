# -*- coding: utf-8 -*-
"""
Tests de la résolution de DOI par appariement bibliographique CrossRef.

Aucun réseau. La propriété défendue ici n'est pas le taux de résolution mais
sa **précision** : un DOI manquant est récupérable à la main, un DOI faux entre
silencieusement dans Zotero et ressort dans une bibliographie. La majorité des
cas ci-dessous vérifient donc des rejets.
"""

from unittest.mock import patch

import pytest


def _item(title, year="", last="", doi=None, item_type="journalArticle"):
    item = {
        "itemType": item_type,
        "title": title,
        "date": year,
        "creators": (
            [{"creatorType": "author", "firstName": "X", "lastName": last}]
            if last else []
        ),
        "extra": "Sources: Google Scholar (SerpAPI)",
        "tags": [], "collections": [], "relations": {},
    }
    if doi:
        item["DOI"] = doi
    return item


def _cand(title, year=None, families=(), doi="10.1000/test"):
    c = {"title": [title], "DOI": doi,
         "author": [{"family": f} for f in families]}
    if year:
        c["issued"] = {"date-parts": [[year]]}
    return c


def _msg(items):
    return {"message": {"items": items}}


# === Comparaison de titres ===

class TestTitleMatching:
    def test_typography_is_neutralised(self):
        from utils.doi_resolver import title_similarity
        assert title_similarity(
            "L'herméneutique computationnelle : enjeux",
            "L hermeneutique computationnelle enjeux",
        ) == pytest.approx(1.0, abs=0.02)

    def test_subtitle_only_on_one_side_matches_by_prefix(self):
        from utils.doi_resolver import best_title_ratio
        ratio, by_prefix = best_title_ratio(
            "Your Brain on ChatGPT",
            "Your Brain on ChatGPT: Accumulation of Cognitive Debt",
        )
        assert by_prefix and ratio > 0.95

    def test_short_title_never_matches_by_prefix(self):
        """« Attention » est le préfixe de trop de titres pour être probant."""
        from utils.doi_resolver import best_title_ratio
        _, by_prefix = best_title_ratio(
            "Attention", "Attention is all you need")
        assert not by_prefix

    def test_unrelated_titles_score_low(self):
        from utils.doi_resolver import title_similarity
        assert title_similarity(
            "Attention is all you need", "Population biology of plants") < 0.6

    def test_empty_title_scores_zero(self):
        from utils.doi_resolver import title_similarity
        assert title_similarity("", "Un titre") == 0.0


# === Décision d'appariement ===

class TestEvaluateCandidate:
    def test_accepts_full_agreement(self):
        from utils.doi_resolver import evaluate_candidate
        ok, ratio, motif = evaluate_candidate(
            _item("Attention is all you need", "2017", "Vaswani"),
            _cand("Attention Is All You Need", 2017, ["Vaswani"]),
        )
        assert ok and ratio > 0.99
        assert "année exacte" in motif and "auteur" in motif

    def test_tolerates_one_year_gap(self):
        """En ligne d'abord, en numéro l'année suivante : cas courant."""
        from utils.doi_resolver import evaluate_candidate
        ok, _, motif = evaluate_candidate(
            _item("Digital sovereignty in Europe", "2023", "Dupont"),
            _cand("Digital Sovereignty in Europe", 2024, ["Dupont"]),
        )
        assert ok and "année à 1 près" in motif

    def test_rejects_distant_year(self):
        from utils.doi_resolver import evaluate_candidate
        ok, _, motif = evaluate_candidate(
            _item("Digital sovereignty in Europe", "2023", "Dupont"),
            _cand("Digital Sovereignty in Europe", 2011, ["Dupont"]),
        )
        assert not ok and "année" in motif

    def test_rejects_absent_author(self):
        """Même titre, autre auteur : compte rendu, réédition, homonymie."""
        from utils.doi_resolver import evaluate_candidate
        ok, _, motif = evaluate_candidate(
            _item("Digital sovereignty in Europe", "2023", "Dupont"),
            _cand("Digital Sovereignty in Europe", 2023, ["Martin", "Durand"]),
        )
        assert not ok and "auteur" in motif

    def test_rejects_below_title_threshold(self):
        from utils.doi_resolver import evaluate_candidate
        ok, _, motif = evaluate_candidate(
            _item("Une étude sur les modèles de langage", "2024", "Dupont"),
            _cand("Une étude sur les réseaux de transport", 2024, ["Dupont"]),
        )
        assert not ok and "seuil" in motif

    def test_uncorroborated_needs_near_identical_title(self):
        """Sans année ni auteur vérifiables, seul un titre quasi identique passe."""
        from utils.doi_resolver import evaluate_candidate
        ok, _, _ = evaluate_candidate(
            _item("Sociologie des controverses numeriques"),
            _cand("Sociologie des controverses numériques"),
        )
        assert ok
        ok2, _, motif2 = evaluate_candidate(
            _item("Sociologie des controverses numeriques en ligne"),
            _cand("Sociologie des controverses numériques"),
        )
        assert not ok2 and "corroboration" in motif2

    def test_prefix_match_alone_is_refused(self):
        """Un préfixe non corroboré ne suffit jamais, quel que soit son score."""
        from utils.doi_resolver import evaluate_candidate
        ok, ratio, motif = evaluate_candidate(
            _item("Your Brain on ChatGPT"),
            _cand("Your Brain on ChatGPT: Accumulation of Cognitive Debt"),
        )
        assert not ok and ratio > 0.95 and "préfixe" in motif

    def test_prefix_match_accepted_when_corroborated(self):
        from utils.doi_resolver import evaluate_candidate
        ok, _, motif = evaluate_candidate(
            _item("Your Brain on ChatGPT", "2025", "Kosmyna"),
            _cand("Your Brain on ChatGPT: Accumulation of Cognitive Debt",
                  2025, ["Kosmyna"]),
        )
        assert ok and "préfixe" in motif

    def test_threshold_can_be_raised_never_lowered(self):
        """
        Le seuil se durcit, il ne s'assouplit pas (invariant de précision,
        OPT-10). Avant OPT-10, ce test vérifiait qu'un seuil de 0,80 était
        accepté : c'est précisément ce qui est désormais refusé.
        """
        from utils.doi_resolver import DoiRuleError, evaluate_candidate
        item = _item("Modeles de langage et discours", "2024", "Lakel")
        cand = _cand("Modeles de langage et le discours", 2024, ["Lakel"])
        assert not evaluate_candidate(item, cand, threshold=0.99)[0]
        assert evaluate_candidate(item, cand)[0]            # défaut 0,90
        with pytest.raises(DoiRuleError, match="abaissé"):
            evaluate_candidate(item, cand, threshold=0.80)


# === Boucle de résolution ===

class TestResolveDois:
    def test_resolves_and_traces_provenance(self):
        from utils import doi_resolver as dr
        payloads = [_msg([_cand("Attention Is All You Need", 2017,
                                ["Vaswani"], "10.48550/arXiv.1706.03762")])]
        with patch.object(dr, "get_json", side_effect=payloads), \
                patch("utils.http.time.sleep", lambda s: None):
            out, rep = dr.resolve_dois(
                [_item("Attention is all you need", "2017", "Vaswani")])
        assert rep["resolved"] == 1 and rep["rejected"] == 0
        assert out[0]["DOI"] == "10.48550/arXiv.1706.03762"
        assert "DOI source: appariement bibliographique CrossRef" in out[0]["extra"]
        assert "titre 1.00" in out[0]["extra"]

    def test_skips_items_that_already_have_a_doi(self):
        from utils import doi_resolver as dr
        calls = []

        def fake(*a, **k):
            calls.append(1)
            return _msg([])

        with patch.object(dr, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            out, rep = dr.resolve_dois(
                [_item("Un titre", "2020", "A", doi="10.1/x")])
        assert rep["already_had_doi"] == 1 and rep["attempted"] == 0
        assert calls == []

    def test_skips_items_whose_doi_is_only_in_extra(self):
        """Sur un type Zotero sans champ DOI, l'identifiant vit dans 'extra'."""
        from utils import doi_resolver as dr
        item = _item("Un livre", "2020", "A", item_type="book")
        item["extra"] += "\nDOI: 10.1/deja"
        with patch.object(dr, "get_json", side_effect=[_msg([])]), \
                patch("utils.http.time.sleep", lambda s: None):
            _, rep = dr.resolve_dois([item])
        assert rep["already_had_doi"] == 1 and rep["attempted"] == 0

    def test_records_rejections_with_their_reason(self):
        from utils import doi_resolver as dr
        payloads = [_msg([_cand("Un tout autre titre", 2017, ["Vaswani"])])]
        with patch.object(dr, "get_json", side_effect=payloads), \
                patch("utils.http.time.sleep", lambda s: None):
            out, rep = dr.resolve_dois(
                [_item("Attention is all you need", "2017", "Vaswani")])
        assert rep["rejected"] == 1 and rep["resolved"] == 0
        assert not out[0].get("DOI")
        assert rep["rejections"] and "seuil" in rep["rejections"][0][2]

    def test_counts_absence_of_candidate_separately(self):
        from utils import doi_resolver as dr
        with patch.object(dr, "get_json", side_effect=[_msg([])]), \
                patch("utils.http.time.sleep", lambda s: None):
            _, rep = dr.resolve_dois([_item("Un titre rare", "2020", "A")])
        assert rep["no_candidate"] == 1 and rep["rejected"] == 0

    def test_network_failure_does_not_stop_the_corpus(self):
        # Réponses indexées par titre, et non par ordre d'appel : depuis
        # OPT-10, les requêtes partent en parallèle (pool de 3), leur ordre
        # d'émission n'est plus celui du corpus.
        from utils import doi_resolver as dr

        def fake(url, params=None, **k):
            if params["query.bibliographic"] == "Premier titre en echec":
                raise RuntimeError("boom")
            return _msg([_cand("Population Biology of Plants", 1977,
                               ["Harper"], "10.2307/2259905")])

        with patch.object(dr, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            out, rep = dr.resolve_dois([
                _item("Premier titre en echec", "2020", "A"),
                _item("Population biology of plants", "1977", "Harper"),
            ])
        assert rep["resolved"] == 1
        assert out[1]["DOI"] == "10.2307/2259905"
        # L'échec est compté comme une erreur, pas comme une absence de
        # candidat (OPT-10).
        assert rep["errors"] == 1 and rep["no_candidate"] == 0

    def test_max_items_caps_the_number_of_queries(self):
        from utils import doi_resolver as dr
        calls = []

        def fake(*a, **k):
            calls.append(1)
            return _msg([])

        with patch.object(dr, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            _, rep = dr.resolve_dois(
                [_item(f"Titre numero {i}", "2020", "A") for i in range(5)],
                max_items=2,
            )
        assert len(calls) == 2 and rep["attempted"] == 2

    def test_first_acceptable_candidate_wins(self):
        """Seul candidat admissible parmi plusieurs : il est retenu, quel que
        soit son rang (depuis OPT-10, tous les candidats sont examinés ; deux
        admissibles distincts donneraient « ambigu »)."""
        from utils import doi_resolver as dr
        payloads = [_msg([
            _cand("Un titre sans rapport aucun", 2017, ["Vaswani"], "10.1/faux"),
            _cand("Attention Is All You Need", 2017, ["Vaswani"], "10.1/bon"),
        ])]
        with patch.object(dr, "get_json", side_effect=payloads), \
                patch("utils.http.time.sleep", lambda s: None):
            out, _ = dr.resolve_dois(
                [_item("Attention is all you need", "2017", "Vaswani")])
        assert out[0]["DOI"] == "10.1/bon"

    def test_candidate_without_doi_is_not_accepted(self):
        from utils import doi_resolver as dr
        payloads = [_msg([_cand("Attention Is All You Need", 2017,
                                ["Vaswani"], doi="")])]
        with patch.object(dr, "get_json", side_effect=payloads), \
                patch("utils.http.time.sleep", lambda s: None):
            out, rep = dr.resolve_dois(
                [_item("Attention is all you need", "2017", "Vaswani")])
        assert rep["resolved"] == 0 and not out[0].get("DOI")

    def test_author_is_sent_to_crossref_to_narrow_the_search(self):
        from utils import doi_resolver as dr
        captured = {}

        def fake(url, params=None, **k):
            captured.update(params)
            return _msg([])

        with patch.object(dr, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            dr.resolve_dois([_item("Un titre", "2020", "Vaswani")])
        assert captured["query.bibliographic"] == "Un titre"
        assert captured["query.author"] == "vaswani"
        assert "mailto" in captured

    def test_item_without_title_is_left_alone(self):
        from utils import doi_resolver as dr
        with patch.object(dr, "get_json", side_effect=[]), \
                patch("utils.http.time.sleep", lambda s: None):
            _, rep = dr.resolve_dois([_item("", "2020", "A")])
        assert rep["skipped_no_title"] == 1 and rep["attempted"] == 0

    def test_doi_migrates_to_extra_on_types_without_a_doi_field(self):
        """Un 'book' n'a pas de champ DOI dans Zotero : l'info va dans 'extra'."""
        from utils import doi_resolver as dr
        payloads = [_msg([_cand("Population Biology of Plants", 1977,
                                ["Harper"], "10.2307/2259905")])]
        with patch.object(dr, "get_json", side_effect=payloads), \
                patch("utils.http.time.sleep", lambda s: None):
            out, rep = dr.resolve_dois([
                _item("Population biology of plants", "1977", "Harper",
                      item_type="book")])
        assert rep["resolved"] == 1
        assert "DOI: 10.2307/2259905" in out[0]["extra"]

    def test_empty_corpus_is_harmless(self):
        from utils import doi_resolver as dr
        out, rep = dr.resolve_dois([])
        assert out == [] and rep["total"] == 0
        assert rep["resolution_rate_pct"] == 0.0


# === Outil MCP ===

class TestMcpTool:
    def test_tool_is_declared(self):
        import asyncio
        import mcp_server
        tools = asyncio.run(mcp_server.list_tools())
        names = {t.name for t in tools}
        assert "corpus_resolve_dois" in names
        tool = next(t for t in tools if t.name == "corpus_resolve_dois")
        assert "corpus_path" in tool.inputSchema["required"]
        assert "title_threshold" in tool.inputSchema["properties"]

    def test_missing_file_returns_a_message(self):
        import asyncio
        import mcp_server
        out = asyncio.run(mcp_server._tool_corpus_resolve_dois(
            {"corpus_path": "corpus_inexistant_xyz.json"}))
        assert isinstance(out, str) and "introuvable" in out
