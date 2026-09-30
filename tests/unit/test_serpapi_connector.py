# -*- coding: utf-8 -*-
"""
Tests du connecteur Google Scholar (passerelle SerpAPI) et de son mapper.

Aucun réseau : la couche HTTP est simulée. Les deux résultats servant de
fixture sont repris **verbatim** de la documentation SerpAPI
(https://serpapi.com/google-scholar-organic-results), y compris les points de
suspension de troncature (U+2026), qui sont la difficulté principale du
parsing de ``publication_info.summary``.

Trois propriétés sont vérifiées en priorité, parce qu'elles portent le risque
de la source : le plafond de requêtes facturées est respecté, le solde du
compte bloque la moisson avant toute dépense, et le mapper ne fabrique ni DOI
ni auteur absent de la notice.
"""

from unittest.mock import patch

import pytest


# === Fixtures : réponses SerpAPI verbatim ===

RESULT_HARPER = {
    "position": 1,
    "title": "Population biology of plants.",
    "result_id": "JC4Acibs_4kJ",
    "link": "https://www.cabdirect.org/cabdirect/abstract/19782321379",
    "snippet": "The first chapter is concerned with experiments, analogies "
               "and models. There …",
    "publication_info": {
        "summary": "JL Harper - Population biology of plants., 1977 - "
                   "cabdirect.org",
        "authors": [{"name": "JL Harper"}],
    },
    "inline_links": {
        "cited_by": {
            "total": 14003,
            "cites_id": "9943926152122871332",
        },
        "versions": {"total": 6, "cluster_id": "9943926152122871332"},
    },
}

RESULT_LODISH = {
    "position": 2,
    "title": "Molecular cell biology",
    "result_id": "5BUDsdJ6ho0J",
    "link": "https://books.google.com/books?hl=en&id=K3JbjG1JiUMC",
    "snippet": "With its acclaimed author team …",
    "publication_info": {
        "summary": "H Lodish, A Berk, CA Kaiser, M Krieger, MP Scott… - "
                   "2008 - books.google.com",
        "authors": [{"name": "H Lodish"}, {"name": "A Berk"},
                    {"name": "CA Kaiser"}],
    },
    "resources": [{"title": "nsysu.edu.tw", "file_format": "PDF",
                   "link": "http://www2.nsysu.edu.tw/wzhlab/ch6.pdf"}],
    "inline_links": {
        "cited_by": {"total": 9196, "cites_id": "10197973451558557156"},
        "versions": {"total": 18, "cluster_id": "10197973451558557156"},
    },
}


def _page(results, total=100):
    return {
        "search_information": {"total_results": total},
        "organic_results": results,
    }


def _filler(n, start=0):
    """n résultats minimalement valides, pour tester la pagination."""
    out = []
    for i in range(n):
        out.append({
            "title": f"Item {start + i}",
            "result_id": f"id{start + i}",
            "link": f"https://example.org/{start + i}",
            "publication_info": {"summary": f"A Author - Revue X, 2020 - "
                                            f"example.org"},
        })
    return out


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, payloads=None):
        self._payloads = list(payloads or [])
        self.calls = []
        self.session = object()

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return _Resp(self._payloads.pop(0))

    def close(self):
        pass


# === Mapper : décomposition de la ligne de notice ===

class TestParseSummary:
    @pytest.mark.parametrize("summary,expected", [
        (
            "JL Harper - Population biology of plants., 1977 - cabdirect.org",
            {"authors": ["JL Harper"],
             "venue": "Population biology of plants",
             "year": "1977", "domain": "cabdirect.org", "truncated": False},
        ),
        (
            "H Lodish, A Berk, CA Kaiser, M Krieger, MP Scott… - 2008 - "
            "books.google.com",
            {"authors": ["H Lodish", "A Berk", "CA Kaiser", "M Krieger",
                         "MP Scott"],
             "venue": "", "year": "2008", "domain": "books.google.com",
             "truncated": True},
        ),
        (
            "A Vaswani, N Shazeer… - Advances in neural …, 2017 - "
            "proceedings.neurips.cc",
            {"authors": ["A Vaswani", "N Shazeer"],
             "venue": "Advances in neural", "year": "2017",
             "domain": "proceedings.neurips.cc", "truncated": True},
        ),
        (
            "Revue française de sociologie, 2019 - cairn.info",
            {"authors": [], "venue": "Revue française de sociologie",
             "year": "2019", "domain": "cairn.info", "truncated": False},
        ),
    ])
    def test_decomposition(self, summary, expected):
        from mappers.google_scholar_mapper import parse_publication_summary
        assert parse_publication_summary(summary) == expected

    @pytest.mark.parametrize("summary,venue,domain", [
        # Défaut trouvé à la première moisson réelle : Scholar termine souvent
        # ses notices par un nom de plateforme et non par un domaine. Le test
        # de forme laissait alors la provenance dans le nom de revue.
        ("BD Lund, KT Naheem - Learned Publishing, 2024 - Wiley Online Library",
         "Learned Publishing", "Wiley Online Library"),
        ("S Nazarovets - Accountability in Research, 2025 - Taylor & Francis",
         "Accountability in Research", "Taylor & Francis"),
        ("JC Cwik - Research Integrity and Peer Review, 2026 - Springer",
         "Research Integrity and Peer Review", "Springer"),
    ])
    def test_publisher_name_is_not_taken_for_a_journal(self, summary, venue,
                                                       domain):
        from mappers.google_scholar_mapper import parse_publication_summary
        out = parse_publication_summary(summary)
        assert out["venue"] == venue
        assert out["domain"] == domain

    def test_internal_ellipsis_flags_truncation(self):
        """Les points de suspension ne sont pas toujours en bordure."""
        from mappers.google_scholar_mapper import parse_publication_summary
        out = parse_publication_summary(
            "R Al-Jarf - International Conference on Artificial Intelligence "
            "and …, 2025 - Springer"
        )
        assert out["truncated"]
        assert out["venue"] == ("International Conference on Artificial "
                                "Intelligence and")

    def test_empty_summary_yields_nothing(self):
        from mappers.google_scholar_mapper import parse_publication_summary
        out = parse_publication_summary("")
        assert out == {"authors": [], "venue": "", "year": "", "domain": "",
                       "truncated": False}

    def test_author_only_summary(self):
        """Sans millésime ni domaine, le segment unique est une liste d'auteurs."""
        from mappers.google_scholar_mapper import parse_publication_summary
        out = parse_publication_summary("M Foucault")
        assert out["authors"] == ["M Foucault"]
        assert out["year"] == "" and out["venue"] == ""

    def test_year_inside_title_does_not_win(self):
        """Le millésime retenu est le dernier, pas une date dans le titre."""
        from mappers.google_scholar_mapper import parse_publication_summary
        out = parse_publication_summary(
            "A Auteur - La crise de 1929 dans la presse, 2015 - cairn.info"
        )
        assert out["year"] == "2015"
        assert "1929" in out["venue"]


class TestExtractDoi:
    @pytest.mark.parametrize("url,expected", [
        ("https://doi.org/10.1080/1369118X.2020.1716043",
         "10.1080/1369118X.2020.1716043"),
        ("https://dx.doi.org/10.1177/2053951719897945.pdf",
         "10.1177/2053951719897945"),
        ("https://www.tandfonline.com/doi/full/10.1080/abc.2020.123456",
         "10.1080/abc.2020.123456"),
    ])
    def test_accepts_unambiguous_patterns(self, url, expected):
        from mappers.google_scholar_mapper import extract_doi
        assert extract_doi(url) == expected

    @pytest.mark.parametrize("url", [
        "https://example.org/article?tracking=10.1234/xyz",   # paramètre
        "https://books.google.com/books?id=ABC",              # aucun DOI
        "",
        None,
    ])
    def test_rejects_everything_else(self, url):
        """Un DOI deviné est pire qu'un DOI absent : le champ reste vide."""
        from mappers.google_scholar_mapper import extract_doi
        assert extract_doi(url) == ""

    @pytest.mark.parametrize("url,expected", [
        # Défaut trouvé à la deuxième moisson réelle : les plateformes
        # ajoutent un segment de chemin APRÈS le DOI, que le suffixe absorbait.
        ("https://www.degruyter.com/document/doi/10.1515/sem-2024-0188/html",
         "10.1515/sem-2024-0188"),
        ("https://onlinelibrary.wiley.com/doi/full/10.1111/isj.12504",
         "10.1111/isj.12504"),
        ("https://journals.sagepub.com/doi/10.1177/2053951720919776/abstract",
         "10.1177/2053951720919776"),
    ])
    def test_platform_path_segments_are_stripped(self, url, expected):
        from mappers.google_scholar_mapper import extract_doi
        assert extract_doi(url) == expected

    @pytest.mark.parametrize("url,expected", [
        # Springer porte le DOI en clair dans /article/ ou /chapter/, sans le
        # segment /doi/ : l'écarter faisait perdre des DOI lisibles.
        ("https://link.springer.com/article/10.1007/s13347-021-00477-0",
         "10.1007/s13347-021-00477-0"),
        ("https://link.springer.com/chapter/10.1007/978-3-031-99965-9_39",
         "10.1007/978-3-031-99965-9_39"),
        # MIT Press ajoute un identifiant d'article numérique après le DOI.
        ("https://direct.mit.edu/qss/article/doi/10.1162/qss_a_00337/125096",
         "10.1162/qss_a_00337"),
    ])
    def test_platform_paths_carrying_a_doi(self, url, expected):
        from mappers.google_scholar_mapper import extract_doi
        assert extract_doi(url) == expected

    def test_bare_numeric_suffix_is_kept(self):
        """Un DOI dont le suffixe EST numérique ne doit pas être amputé."""
        from mappers.google_scholar_mapper import extract_doi
        assert extract_doi("https://doi.org/10.1086/709729") == "10.1086/709729"

    def test_casing_is_preserved(self):
        from mappers.google_scholar_mapper import extract_doi
        assert "X" in extract_doi("https://doi.org/10.1080/1369118X.2020.1")


class TestMapScholarItem:
    def test_journal_article(self):
        from mappers.google_scholar_mapper import map_scholar_item
        item = map_scholar_item(RESULT_HARPER)
        assert item["title"] == "Population biology of plants."
        assert item["creators"] == [
            {"creatorType": "author", "firstName": "JL", "lastName": "Harper"}
        ]
        assert item["date"] == "1977"
        assert item["itemType"] == "journalArticle"
        assert item["publicationTitle"] == "Population biology of plants"
        assert "Cited by: 14003" in item["extra"]
        assert "Scholar cluster ID: 9943926152122871332" in item["extra"]
        assert "Sources: Google Scholar (SerpAPI)" in item["extra"]

    def test_book_detected_from_link(self):
        from mappers.google_scholar_mapper import map_scholar_item
        item = map_scholar_item(RESULT_LODISH)
        assert item["itemType"] == "book"
        assert len(item["creators"]) == 5      # la notice, pas `authors` (3)
        assert item["date"] == "2008"
        assert "www2.nsysu.edu.tw" in item["extra"]

    def test_truncation_is_flagged(self):
        from mappers.google_scholar_mapper import map_scholar_item
        item = map_scholar_item(RESULT_LODISH)
        assert "Scholar truncation" in item["extra"]
        item2 = map_scholar_item(RESULT_HARPER)
        assert "Scholar truncation" not in item2["extra"]

    def test_snippet_is_labelled_not_an_abstract(self):
        from mappers.google_scholar_mapper import map_scholar_item
        item = map_scholar_item(RESULT_HARPER)
        assert item["abstractNote"].startswith("The first chapter")
        assert "Scholar snippet" in item["extra"]

    def test_no_doi_is_invented(self):
        from mappers.google_scholar_mapper import map_scholar_item
        for raw in (RESULT_HARPER, RESULT_LODISH):
            assert not map_scholar_item(raw).get("DOI")

    def test_zero_citations_is_preserved(self):
        """Un compteur à 0 est une information, pas une valeur absente."""
        from mappers.google_scholar_mapper import map_scholar_item
        raw = dict(RESULT_HARPER,
                   inline_links={"cited_by": {"total": 0}})
        assert "Cited by: 0" in map_scholar_item(raw)["extra"]


# === Connecteur : pagination, plafonds, quota ===

def _connector(monkeypatch=None, **kw):
    from connectors import google_scholar as gs
    kw.setdefault("api_key", "K")
    kw.setdefault("min_credits", 0)     # pas d'appel Account API par défaut
    return gs.GoogleScholarConnector(http_client=_FakeHTTP(), **kw)


class TestGoogleScholarConnector:
    def test_paginates_until_short_page(self):
        from connectors import google_scholar as gs
        pages = [_page(_filler(20)), _page(_filler(5, start=20))]
        with patch.object(gs, "get_json", side_effect=pages), \
                patch("utils.http.time.sleep", lambda s: None):
            items = _connector().search("ia", max_results=100)
        assert len(items) == 25

    def test_max_pages_caps_billed_requests(self):
        """Le plafond de pages borne la dépense, pas seulement le volume."""
        from connectors import google_scholar as gs
        calls = []

        def fake(url, params=None, **k):
            calls.append(params)
            return _page(_filler(20, start=20 * len(calls)))

        with patch.object(gs, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            items = _connector(max_pages=2).search("ia", max_results=500)
        assert len(calls) == 2
        assert len(items) == 40

    def test_max_results_below_page_size_still_costs_one_request(self):
        from connectors import google_scholar as gs
        calls = []

        def fake(url, params=None, **k):
            calls.append(params)
            return _page(_filler(20))

        with patch.object(gs, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            items = _connector().search("ia", max_results=5)
        assert len(calls) == 1
        assert len(items) == 5

    def test_start_offset_follows_page_size(self):
        from connectors import google_scholar as gs
        calls = []

        def fake(url, params=None, **k):
            calls.append(params)
            return _page(_filler(20, start=20 * len(calls)))

        with patch.object(gs, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            _connector(max_pages=3).search("ia", max_results=60)
        assert [c["start"] for c in calls] == [0, 20, 40]

    def test_year_filters_map_to_as_ylo_as_yhi(self):
        from connectors import google_scholar as gs
        captured = {}

        def fake(url, params=None, **k):
            captured.update(params)
            return _page([])

        with patch.object(gs, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            _connector().search("ia", max_results=20,
                                from_date="2021-01-01", to_date="2026")
        assert captured["as_ylo"] == "2021"
        assert captured["as_yhi"] == "2026"
        assert captured["engine"] == "google_scholar"
        assert captured["as_sdt"] == "0"        # brevets exclus par défaut

    def test_serpapi_error_payload_stops_cleanly(self):
        """Scholar sans résultat répond 200 + `error` : fin, pas panne."""
        from connectors import google_scholar as gs
        payload = {"error": "Google Scholar hasn't returned any results."}
        with patch.object(gs, "get_json", side_effect=[payload]), \
                patch("utils.http.time.sleep", lambda s: None):
            assert _connector().search("zzz", max_results=40) == []

    def test_budget_exhausted_stops(self):
        from connectors import google_scholar as gs
        from utils.http_scholarly import BudgetExhausted
        with patch.object(gs, "get_json",
                          side_effect=BudgetExhausted("quota")), \
                patch("utils.http.time.sleep", lambda s: None):
            assert _connector().search("ia", max_results=40) == []

    def test_low_credit_balance_cancels_before_spending(self):
        """Le garde-fou principal : refuser avant de consommer le quota."""
        from connectors import google_scholar as gs
        calls = []

        def fake(url, params=None, **k):
            calls.append(url)
            if url == gs.SERPAPI_ACCOUNT:
                return {"plan_name": "Free", "total_searches_left": 2}
            return _page(_filler(20))

        with patch.object(gs, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            items = _connector(min_credits=5, max_pages=3).search(
                "ia", max_results=60)
        assert items == []
        assert calls == [gs.SERPAPI_ACCOUNT]     # aucune requête facturée

    def test_sufficient_balance_proceeds(self):
        from connectors import google_scholar as gs

        def fake(url, params=None, **k):
            if url == gs.SERPAPI_ACCOUNT:
                return {"plan_name": "Free", "total_searches_left": 80}
            return _page(_filler(3))

        with patch.object(gs, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            items = _connector(min_credits=5).search("ia", max_results=20)
        assert len(items) == 3

    def test_unknown_balance_does_not_block(self):
        """Account API muette : on laisse l'API arbitrer, sans faux blocage."""
        from connectors import google_scholar as gs

        def fake(url, params=None, **k):
            if url == gs.SERPAPI_ACCOUNT:
                return {}
            return _page(_filler(2))

        with patch.object(gs, "get_json", side_effect=fake), \
                patch("utils.http.time.sleep", lambda s: None):
            assert len(_connector(min_credits=5).search("ia", 20)) == 2

    def test_page_size_capped_at_api_maximum(self):
        c = _connector(page_size=100)
        assert c.page_size == 20

    def test_request_failure_never_logs_the_api_key(self, caplog):
        """L'URL SerpAPI porte la clé : aucun message d'erreur ne doit la citer."""
        from connectors import google_scholar as gs
        import logging

        def boom(url, params=None, **k):
            raise RuntimeError(
                "HTTPError for url: https://serpapi.com/search.json"
                "?api_key=SECRET123&q=ia"
            )

        with caplog.at_level(logging.ERROR), \
                patch.object(gs, "get_json", side_effect=boom), \
                patch("utils.http.time.sleep", lambda s: None):
            assert _connector().search("ia", max_results=20) == []
        assert "SECRET123" not in caplog.text


# === Registre : la source est disponible mais hors des groupes gratuits ===

class TestRegistration:
    def test_source_is_registered(self):
        from connectors.registry import CONNECTORS, get_connector_class
        from connectors.google_scholar import GoogleScholarConnector
        assert CONNECTORS["google_scholar"] is GoogleScholarConnector
        assert get_connector_class("google_scholar") is GoogleScholarConnector

    def test_excluded_from_free_groups(self):
        """Aucune moisson de routine ne doit consommer le quota mensuel."""
        from connectors.registry import SOURCE_GROUPS
        assert "google_scholar" not in SOURCE_GROUPS["all"]
        assert "google_scholar" not in SOURCE_GROUPS["international"]
        assert "google_scholar" not in SOURCE_GROUPS["francophone"]

    def test_reachable_by_name_and_by_group(self):
        from connectors.registry import resolve_sources
        assert resolve_sources(["scholar"]) == ["google_scholar"]
        assert resolve_sources(["international", "google_scholar"])[-1] == \
            "google_scholar"

    def test_flagged_as_metered(self):
        from connectors.registry import is_metered_source
        assert is_metered_source("google_scholar")
        assert not is_metered_source("crossref")

    def test_has_a_description(self):
        from connectors.registry import SOURCE_DESCRIPTIONS, CONNECTORS
        assert set(CONNECTORS) <= set(SOURCE_DESCRIPTIONS)
        assert "FACTURÉE" in SOURCE_DESCRIPTIONS["google_scholar"]
