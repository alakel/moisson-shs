"""
OPT-04 / OPT-00 : schéma Zotero de test stable.

tests/fixtures/zotero_schema_test.json est le schéma officiel
(https://api.zotero.org/schema, version enregistrée dans le fichier) réduit :
seules les « locales » (libellés traduits, 390 Ko) sont retirées ; itemTypes,
meta et csl sont intacts. Il a été dérivé du cache
~/.cache/moisson_shs/zotero_schema.json. La suite de tests le désigne par
$MOISSON_ZOTERO_SCHEMA_FILE pour ne dépendre ni du réseau ni de ~/.cache.

Régénération (depuis un cache officiel frais) :
    python -c "import json,pathlib; p=pathlib.Path.home()/'.cache/moisson_shs/zotero_schema.json'; s=json.loads(p.read_text()); s.pop('locales',None); pathlib.Path('tests/fixtures/zotero_schema_test.json').write_text(json.dumps(s,ensure_ascii=False,indent=1)+'\\n')"

Ces tests vérifient que la fixture couvre tout ce que les mappers
produisent : itemTypes, champs, creatorTypes.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from mappers import doc_type_mapping, scholarly_types
from mappers import zotero_schema_loader as loader
from mappers.zotero_schema import COMMON_FIELDS, ZOTERO_ITEM_FIELDS

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "zotero_schema_test.json"


@pytest.fixture(scope="module")
def schema():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fields_by_type(schema):
    return {it["itemType"]: {f["field"] for f in it["fields"]}
            for it in schema["itemTypes"]}


@pytest.fixture(scope="module")
def creators_by_type(schema):
    return {it["itemType"]: {c["creatorType"] for c in it["creatorTypes"]}
            for it in schema["itemTypes"]}


def _produced_item_types():
    """itemTypes que les tables de correspondance des mappers peuvent émettre."""
    produced = set(doc_type_mapping.HAL_DOCTYPE_MAP.values())
    produced |= set(doc_type_mapping.DUBLIN_CORE_TYPE_MAP.values())
    for name in ("_CROSSREF", "_OPENALEX", "_S2", "_CORE", "_SCHOLAR_BANNER"):
        produced |= {v for v in getattr(scholarly_types, name).values() if v}
    # Sorties codées en dur de scholar_type_to_zotero (indices d'URL/revue)
    for url, venue in [("https://books.google.fr/x", None),
                       ("https://theses.fr/x", None),
                       ("https://arxiv.org/abs/x", None),
                       (None, "Proceedings of X"), (None, "Revue X"),
                       (None, None)]:
        produced.add(scholarly_types.scholar_type_to_zotero(None, venue, url))
    produced |= set(ZOTERO_ITEM_FIELDS)
    return produced


def _fields_assigned_by_mappers():
    """Noms de champs affectés littéralement par les mappers (item["x"] = …)."""
    pat = re.compile(
        r"item\[\s*[\"'](\w+)[\"']\s*\]\s*=|item\.setdefault\(\s*[\"'](\w+)[\"']"
    )
    found = set()
    sources = sorted((ROOT / "mappers").glob("*_mapper.py"))
    sources.append(ROOT / "utils" / "lookups.py")
    for path in sources:
        for m in pat.finditer(path.read_text(encoding="utf-8")):
            found.add(m.group(1) or m.group(2))
    return found


class TestFixtureStructure:
    def test_format_officiel_reduit(self, schema):
        assert set(schema) == {"version", "itemTypes", "meta", "csl"}
        assert "locales" not in schema
        assert isinstance(schema["version"], int) and schema["version"] > 0
        assert all({"itemType", "fields", "creatorTypes"} <= set(it)
                   for it in schema["itemTypes"])

    def test_taille_raisonnable(self):
        assert FIXTURE.stat().st_size < 150_000


class TestCoverage:
    def test_tous_les_item_types_produits(self, fields_by_type):
        produced = _produced_item_types()
        assert {"preprint", "thesis", "bookSection", "conferencePaper",
                "report", "presentation", "dataset"} <= produced
        missing = sorted(produced - set(fields_by_type))
        assert not missing, f"itemTypes absents de la fixture : {missing}"

    def test_table_embarquee_couverte(self, fields_by_type):
        # Chaque champ de la table embarquée est autorisé par le schéma
        # officiel pour le même type : la fixture n'appauvrit aucun type
        gaps = {t: sorted(fs - fields_by_type[t])
                for t, fs in ZOTERO_ITEM_FIELDS.items()
                if fs - fields_by_type[t]}
        assert not gaps, f"champs embarqués absents de la fixture : {gaps}"

    def test_champs_affectes_par_les_mappers_connus(self, fields_by_type):
        universe = set().union(*fields_by_type.values()) | COMMON_FIELDS
        assigned = _fields_assigned_by_mappers()
        assert len(assigned) > 10
        unknown = sorted(assigned - universe)
        assert not unknown, f"champs inconnus de la fixture : {unknown}"

    @pytest.mark.parametrize("item_type,fields", [
        ("journalArticle", {"publicationTitle", "volume", "issue", "pages",
                            "DOI", "ISSN"}),
        ("bookSection", {"bookTitle", "publisher", "ISBN", "pages"}),
        ("book", {"publisher", "ISBN", "numPages"}),
        ("conferencePaper", {"proceedingsTitle", "conferenceName", "DOI"}),
        ("thesis", {"university", "thesisType"}),
        ("report", {"institution", "reportType", "reportNumber"}),
        ("preprint", {"repository", "archiveID", "DOI"}),
        ("presentation", {"meetingName", "presentationType"}),
        ("dataset", {"repository", "DOI"}),
        ("document", {"publisher", "DOI"}),
    ])
    def test_champs_shs_par_type(self, fields_by_type, item_type, fields):
        assert fields <= fields_by_type[item_type]

    def test_creator_types_emis(self, creators_by_type):
        produced = _produced_item_types()
        # 'contributor' est valide pour tous les types produits
        assert all("contributor" in creators_by_type[t] for t in produced)
        # 'author' est valide pour les types SHS usuels
        for t in ("journalArticle", "book", "bookSection", "conferencePaper",
                  "thesis", "report", "preprint", "document", "dataset",
                  "webpage", "blogPost", "magazineArticle",
                  "encyclopediaArticle", "letter"):
            assert "author" in creators_by_type[t], t


class TestLoadedThroughEnv:
    def test_chargement_par_la_variable(self, monkeypatch, tmp_path, schema):
        saved = (loader._MEMORY_CACHE, loader._UNAVAILABLE_UNTIL,
                 loader._SCHEMA_ORIGIN, loader._INDEX)
        try:
            monkeypatch.setattr(loader, "CACHE_PATH", tmp_path / "schema.json")
            monkeypatch.setenv(loader.SCHEMA_FILE_ENV, str(FIXTURE))
            loader.reset_state()
            with patch.object(loader.requests, "get",
                              side_effect=AssertionError("réseau interdit")):
                loaded = loader.load_zotero_schema()
                assert loaded["version"] == schema["version"]
                for t in ("preprint", "thesis", "presentation", "dataset"):
                    assert loader.is_valid_item_type(t)
                from mappers.zotero_schema import validate_and_clean
                cleaned = validate_and_clean({
                    "itemType": "thesis", "title": "T", "university": "U",
                    "publicationTitle": "hors schéma",
                })
            assert cleaned["university"] == "U"
            assert "publicationTitle: hors schéma" in cleaned["extra"]
            assert not (tmp_path / "schema.json").exists()
        finally:
            (loader._MEMORY_CACHE, loader._UNAVAILABLE_UNTIL,
             loader._SCHEMA_ORIGIN, loader._INDEX) = saved
