"""
Tests de non-régression pour les corrections du Sprint 01.

Couvre les correctifs critiques apportés à :
- config.py : endpoint Cairn OAI, constantes URL_MARKER
- connectors/isidore.py : nouveau format de réponse, filtrage url_marker
- connectors/cairn.py : suppression du fallback oai:cairn.info:{doi} fautif
- mappers/isidore_mapper.py : nouvelle structure isidore.* multilingue
- mappers/doc_type_mapping.py : nouveaux types DC reconnus

Usage :
    python -m pytest tests/test_sprint_01.py -v
    ou
    python tests/test_sprint_01.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_cairn_oai_url_uses_oai_php():
    """Le endpoint Cairn doit pointer vers /oai.php (changement 2026)."""
    from config import CAIRN_OAI_URL
    assert "oai.php" in CAIRN_OAI_URL, (
        f"CAIRN_OAI_URL = {CAIRN_OAI_URL} — doit contenir 'oai.php'"
    )


def test_url_marker_constants_defined():
    """Les nouvelles constantes de filtrage par URL doivent exister."""
    from config import OPENEDITION_URL_MARKER, CAIRN_URL_MARKER
    assert OPENEDITION_URL_MARKER == "openedition.org"
    assert CAIRN_URL_MARKER == "cairn.info"


def _make_isidore_fixture():
    """Fixture représentative de la structure Isidore 2026."""
    return {
        "@uri": "20.500.13089/hzcj",
        "isidore": {
            "title": [
                {"@xml:lang": "fr", "$": "Test français"},
                {"@xml:lang": "en", "$": "Test english"},
            ],
            "url": "https://doi.org/10.4000/lidil.6033",
            "enrichedCreators": {
                "creator": {
                    "firstname": "Marie-Paule",
                    "lastname": "Jacques",
                }
            },
            "date": {"year": "2019", "normalizedDate": "2019-01-01"},
            "types": {"type": ["info:eu-repo/semantics/article", "Journal articles"]},
            "source": {"name": [{"@xml:lang": "fr", "$": "Lidil"}]},
            "language": "fr",
            "abstract": [{"@xml:lang": "fr", "$": "Résumé test"}],
            "subject": [{"@xml:lang": "fr", "$": "discours numérique"}],
            "ore": {
                "aggregates": [
                    {"$": "https://doi.org/10.4000/lidil.6033"},
                    {"$": "https://journals.openedition.org/lidil/6033"},
                ]
            },
        },
    }


def test_isidore_mapper_basic_fields():
    """Le mapper Isidore extrait les champs principaux du nouveau format."""
    from mappers.isidore_mapper import map_isidore_to_zotero
    item = map_isidore_to_zotero(_make_isidore_fixture())
    assert item["itemType"] == "journalArticle"
    assert item["title"] == "Test français"
    assert len(item["creators"]) == 1
    assert item["creators"][0]["firstName"] == "Marie-Paule"
    assert item["creators"][0]["lastName"] == "Jacques"


def test_isidore_mapper_date_extraction():
    """La date est extraite depuis date.normalizedDate."""
    from mappers.isidore_mapper import map_isidore_to_zotero
    item = map_isidore_to_zotero(_make_isidore_fixture())
    assert item["date"] == "2019-01-01"


def test_isidore_mapper_doi_from_url():
    """Le DOI est extrait quand l'URL primaire pointe vers doi.org."""
    from mappers.isidore_mapper import map_isidore_to_zotero
    item = map_isidore_to_zotero(_make_isidore_fixture())
    assert item.get("DOI") == "10.4000/lidil.6033"


def test_isidore_mapper_publication_title():
    """Le nom de revue est extrait depuis isidore.source.name."""
    from mappers.isidore_mapper import map_isidore_to_zotero
    item = map_isidore_to_zotero(_make_isidore_fixture())
    assert item.get("publicationTitle") == "Lidil"


def test_isidore_mapper_extra_contains_sources():
    """Le champ extra contient au moins la source Isidore."""
    from mappers.isidore_mapper import map_isidore_to_zotero
    item = map_isidore_to_zotero(_make_isidore_fixture())
    extra = item.get("extra", "")
    assert "Isidore" in extra
    assert "Lidil" in extra


def test_cairn_no_fake_doi_fallback():
    """L'extracteur OAI Cairn ne renvoie plus oai:cairn.info:{doi} fautif."""
    from connectors.cairn import CairnConnector
    item = {
        "DOI": "10.3917/comla.204.0045",
        "url": "https://hal.science/hal-12345",
        "extra": "Sources: HAL",
    }
    assert CairnConnector._extract_oai_id(item) is None


def test_cairn_extract_oai_from_load_pdf():
    """OAI Cairn doit être reconstruit depuis URL load_pdf.php."""
    from connectors.cairn import CairnConnector
    item = {
        "url": "https://www.cairn.info/load_pdf.php?ID_ARTICLE=AATC_125_0001",
        "extra": "",
    }
    assert CairnConnector._extract_oai_id(item) == "oai:cairn.info:AATC_125_0001"


def test_dublin_core_review_maps_to_journalarticle():
    """Le type DC 'review' (info:eu-repo/semantics/review) → journalArticle."""
    from mappers.doc_type_mapping import map_dublin_core_types_list
    assert map_dublin_core_types_list(["review"]) == "journalArticle"
    assert map_dublin_core_types_list(["info:eu-repo/semantics/review"]) == "journalArticle"


def test_dublin_core_journal_articles_label():
    """Le libellé 'Journal articles' (HAL/Isidore enrichi) → journalArticle."""
    from mappers.doc_type_mapping import map_dublin_core_types_list
    assert map_dublin_core_types_list(["Journal articles"]) == "journalArticle"


def test_dublin_core_doctoral_thesis_french_label():
    """Le libellé 'Thèse ou essai doctoral accepté' (UQAM) → thesis."""
    from mappers.doc_type_mapping import map_dublin_core_types_list
    assert (
        map_dublin_core_types_list(["Thèse ou essai doctoral accepté"]) == "thesis"
    )


# === Tests d'intégration (nécessitent réseau) ===

def test_integration_cairn_oai_responds_xml():
    """Le endpoint Cairn OAI corrigé répond bien en XML."""
    import requests
    from config import CAIRN_OAI_URL
    r = requests.get(
        f"{CAIRN_OAI_URL}?verb=Identify",
        timeout=20,
        headers={"User-Agent": "moisson_shs/test"},
    )
    assert r.status_code == 200
    assert "xml" in r.headers.get("content-type", "").lower()


def test_integration_isidore_real_query():
    """Test e2e mini : Isidore retourne ≥1 item exploitable."""
    from connectors.isidore import IsidoreConnector
    from utils.http import HTTPClient

    http = HTTPClient()
    try:
        iso = IsidoreConnector(http_client=http)
        items = iso.search("discours numérique", max_results=3)
        assert len(items) >= 1
        # Tous les items doivent avoir un titre non vide et un itemType
        for item in items:
            assert item.get("title"), f"Item sans titre : {item}"
            assert item.get("itemType")
    finally:
        http.close()


def test_integration_openedition_url_marker_filters():
    """Le filtrage url_marker openedition.org renvoie bien des items OE."""
    from connectors.isidore import IsidoreConnector
    from utils.http import HTTPClient

    http = HTTPClient()
    try:
        iso = IsidoreConnector(http_client=http)
        items = iso.search(
            "discours numérique", max_results=3, url_marker="openedition.org"
        )
        assert len(items) >= 1
        # Chaque item doit contenir openedition.org dans son URL ou ses aggregates
        for item in items:
            haystack = item.get("url", "") + (item.get("extra") or "")
            assert "openedition.org" in haystack, (
                f"Item censé être OpenEdition mais URL/extra ne contient pas "
                f"openedition.org : {item.get('title')}"
            )
    finally:
        http.close()


# === Tests export CSL-JSON ===

def _make_zotero_journal_article():
    """Fixture Zotero JSON représentative d'un article de revue."""
    return {
        "itemType": "journalArticle",
        "title": "Pour une pragmatique énonciative des discours numériques",
        "creators": [
            {"creatorType": "author", "firstName": "Amar", "lastName": "Lakel"},
            {"creatorType": "author", "firstName": "Franck", "lastName": "Cormerais"},
        ],
        "abstractNote": "Cet article propose...",
        "publicationTitle": "Communication & langages",
        "volume": "204",
        "issue": "2",
        "pages": "45-67",
        "date": "2020",
        "language": "fr",
        "DOI": "10.3917/comla.204.0045",
        "ISSN": "0336-1500",
        "url": "https://hal.science/hal-04123456",
        "tags": [
            {"tag": "discours numérique"},
            {"tag": "pragmatique"},
        ],
        "extra": "Sources: HAL, Isidore\nHAL ID: hal-04123456",
        "collections": [],
        "relations": {},
    }


def test_csl_type_mapping_journal_article():
    """itemType journalArticle → CSL type article-journal."""
    from mappers.csl_exporter import zotero_to_csl
    csl = zotero_to_csl(_make_zotero_journal_article())
    assert csl["type"] == "article-journal"


def test_csl_id_uses_doi_when_available():
    """L'id CSL utilise le DOI quand il existe."""
    from mappers.csl_exporter import zotero_to_csl
    csl = zotero_to_csl(_make_zotero_journal_article())
    assert csl["id"] == "10.3917/comla.204.0045"


def test_csl_creators_use_family_given():
    """Les creators Zotero deviennent author[].family/given."""
    from mappers.csl_exporter import zotero_to_csl
    csl = zotero_to_csl(_make_zotero_journal_article())
    assert "author" in csl
    assert len(csl["author"]) == 2
    assert csl["author"][0] == {"family": "Lakel", "given": "Amar"}
    assert csl["author"][1] == {"family": "Cormerais", "given": "Franck"}


def test_csl_date_parts_year_only():
    """Date '2020' devient {date-parts: [[2020]]}."""
    from mappers.csl_exporter import zotero_to_csl
    csl = zotero_to_csl(_make_zotero_journal_article())
    assert csl["issued"] == {"date-parts": [[2020]]}


def test_csl_date_parts_iso():
    """Date ISO complète YYYY-MM-DD devient date-parts à 3 niveaux."""
    from mappers.csl_exporter import zotero_to_csl
    item = _make_zotero_journal_article()
    item["date"] = "2020-05-15"
    csl = zotero_to_csl(item)
    assert csl["issued"] == {"date-parts": [[2020, 5, 15]]}


def test_csl_container_title_from_publication_title():
    """publicationTitle → container-title."""
    from mappers.csl_exporter import zotero_to_csl
    csl = zotero_to_csl(_make_zotero_journal_article())
    assert csl["container-title"] == "Communication & langages"


def test_csl_doi_issn_url_preserved():
    """DOI, ISSN, URL sont conservés tels quels."""
    from mappers.csl_exporter import zotero_to_csl
    csl = zotero_to_csl(_make_zotero_journal_article())
    assert csl["DOI"] == "10.3917/comla.204.0045"
    assert csl["ISSN"] == "0336-1500"
    assert csl["URL"] == "https://hal.science/hal-04123456"


def test_csl_tags_to_keyword_string():
    """Les tags Zotero sont joints en chaîne CSL keyword."""
    from mappers.csl_exporter import zotero_to_csl
    csl = zotero_to_csl(_make_zotero_journal_article())
    assert "keyword" in csl
    assert "discours numérique" in csl["keyword"]
    assert "pragmatique" in csl["keyword"]


def test_csl_extra_preserved_in_note():
    """Le champ extra Zotero est intégralement préservé dans CSL note."""
    from mappers.csl_exporter import zotero_to_csl
    csl = zotero_to_csl(_make_zotero_journal_article())
    assert csl["note"] == "Sources: HAL, Isidore\nHAL ID: hal-04123456"


def test_csl_book_section_maps_to_chapter():
    """itemType bookSection → CSL type chapter avec container-title."""
    from mappers.csl_exporter import zotero_to_csl
    item = {
        "itemType": "bookSection",
        "title": "Chapitre 5",
        "bookTitle": "Manuel de SHS",
        "publisher": "PUF",
        "place": "Paris",
        "date": "2021",
        "creators": [{"creatorType": "author", "firstName": "X", "lastName": "Y"}],
    }
    csl = zotero_to_csl(item)
    assert csl["type"] == "chapter"
    assert csl["container-title"] == "Manuel de SHS"
    assert csl["publisher"] == "PUF"
    assert csl["publisher-place"] == "Paris"


def test_csl_thesis_uses_genre():
    """itemType thesis → genre = thesisType."""
    from mappers.csl_exporter import zotero_to_csl
    item = {
        "itemType": "thesis",
        "title": "Une thèse",
        "thesisType": "Thèse de doctorat",
        "university": "Université Bordeaux Montaigne",
        "date": "2022",
        "creators": [],
    }
    csl = zotero_to_csl(item)
    assert csl["type"] == "thesis"
    assert csl["genre"] == "Thèse de doctorat"
    assert csl["publisher"] == "Université Bordeaux Montaigne"


def test_csl_creator_institutional_uses_literal():
    """Un creator avec champ 'name' (institutionnel) devient {literal}."""
    from mappers.csl_exporter import zotero_to_csl
    item = {
        "itemType": "report",
        "title": "Rapport",
        "creators": [
            {"creatorType": "author", "name": "INSEE"},
        ],
        "date": "2024",
    }
    csl = zotero_to_csl(item)
    assert csl["author"] == [{"literal": "INSEE"}]


def test_csl_export_corpus_unique_ids():
    """L'export d'un corpus garantit l'unicité des id CSL."""
    from mappers.csl_exporter import export_corpus_to_csl
    items = [
        _make_zotero_journal_article(),
        _make_zotero_journal_article(),  # doublon volontaire (même DOI)
        _make_zotero_journal_article(),
    ]
    csl_items = export_corpus_to_csl(items)
    ids = [it["id"] for it in csl_items]
    assert len(set(ids)) == len(ids), f"IDs non uniques : {ids}"


def test_csl_export_handles_malformed_item():
    """Un item mal formé n'interrompt pas la conversion du corpus."""
    from mappers.csl_exporter import export_corpus_to_csl
    items = [
        _make_zotero_journal_article(),
        {"itemType": "weirdType", "title": "OK"},  # type inconnu mais valide
    ]
    csl_items = export_corpus_to_csl(items)
    assert len(csl_items) == 2
    assert csl_items[1]["type"] == "document"  # fallback


def test_csl_no_empty_fields():
    """Les champs vides Zotero ne sont pas exportés en CSL."""
    from mappers.csl_exporter import zotero_to_csl
    item = {
        "itemType": "journalArticle",
        "title": "Test",
        "creators": [],
        "publicationTitle": "",   # vide → ne doit pas apparaître
        "DOI": "",                # vide → ne doit pas apparaître
        "abstractNote": "Resume",
    }
    csl = zotero_to_csl(item)
    assert "container-title" not in csl
    assert "DOI" not in csl
    assert csl["abstract"] == "Resume"


if __name__ == "__main__":
    # Mode standalone : lance chaque test et compte les résultats
    import traceback

    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception:
            print(f"  ERROR {t.__name__}:")
            traceback.print_exc(limit=2)
            failed += 1

    print(f"\nRésultat : {passed} passés, {failed} échoués")
    sys.exit(0 if failed == 0 else 1)
